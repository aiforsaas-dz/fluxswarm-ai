"""Phase 2 — Cost-surface authorization for POST /api/projects/{slug}/dispatch.

The dispatch endpoint re-runs an agent team, so it spends provider capacity
exactly like a launch. Before Phase 2 it carried none of the launch-side cost
controls:

  * owned dispatch ignored the operator budget gate entirely (unbounded
    re-dispatch after a reopen);
  * shared flux-demo-* dispatch bypassed /api/demo/launch's per-IP + global
    windows, so any set of authenticated users could jointly drain the demo
    pool while each stayed under their own daily cap.

Accepted behaviour after this phase:

  * EVERY dispatch (owned or demo) fails closed with 429 budget_exhausted when
    the operator ceilings are exhausted, and the user's quotas are NOT burned;
  * flux-demo-* dispatch is bounded by its own per-IP + global windows IN
    ADDITION to the existing per-user daily cap;
  * a healthy budget leaves dispatch fully working and the daily cap intact.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hermes_client as hc
import main as main_mod
import provider_guard as pg

client = TestClient(main_mod.app)


class _PermissiveLimiter:
    def __init__(self):
        self._hits: dict[str, int] = {}

    def check(self, key: str, max_calls: int, window: int) -> bool:
        n = self._hits.get(key, 0) + 1
        self._hits[key] = n
        return n <= max_calls  # permissive: only the window max is honoured

    def ip_allowed(self, ip): return True
    def hit_ip(self, ip): return None
    def register_allowed(self, ip): return True
    def record_registration(self, ip): return None
    def login_allowed(self, ip, email): return True
    def record_login_failure(self, ip, email): return None
    def clear_login_failures(self, ip, email): return None
    def purchase_allowed(self, uid): return True
    def record_purchase(self, uid): return None


@pytest.fixture(autouse=True)
def _permissive_limiter(monkeypatch):
    monkeypatch.setattr(main_mod, "limiter", _PermissiveLimiter())


@pytest.fixture()
def _no_hermes(monkeypatch):
    """API flows must never touch the real Hermes CLI."""
    monkeypatch.setattr(main_mod.hc, "projects_are_thin", lambda: False)
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(
        main_mod.hc, "launch_swarm",
        lambda slug, goal, provider_keys=None:
            type("R", (), {"root_id": "r1", "worker_ids": ["w1", "w2"],
                           "verifier_id": "w3", "synthesizer_id": "w4"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)


@pytest.fixture()
def _dispatch_ok(monkeypatch):
    """Make the dispatch endpoint succeed hermetically (no real Hermes)."""
    calls = []
    monkeypatch.setattr(main_mod.hc, "dispatch",
                        lambda *a, **k: calls.append(a) or {"ok": True, "dry_run": False})
    monkeypatch.setattr(main_mod, "_board_finalized", lambda s: False)
    monkeypatch.setattr(main_mod.hc, "board_is_sealed", lambda s: False)
    return calls


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(user: dict) -> str:
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "Budget", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


# ---------- operator budget gate (owned dispatch) ----------
def test_owned_dispatch_obeys_operator_budget_gate(_no_hermes, _dispatch_ok, monkeypatch):
    a = _register("dbg-owner@fluxswarm.test")
    slug = _mk_project(a)  # project created while the budget is healthy
    # The operator's ceilings are now exhausted: dispatch must fail closed
    # without running the agent team and without burning the daily quota.
    monkeypatch.setattr(main_mod.provider_guard, "budget_gate",
                        lambda *a, **k: pg.BudgetDecision(False, "BUDGET_EXHAUSTED", {}))
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "budget_exhausted"
    assert r.json()["reason"] == "BUDGET_EXHAUSTED"
    assert not _dispatch_ok, "hc.dispatch must never run on a budget-exhausted board"


# ---------- operator budget gate (demo dispatch) ----------
def test_demo_dispatch_obeys_operator_budget_gate(_no_hermes, _dispatch_ok, monkeypatch):
    a = _register("dbg-demo@fluxswarm.test")
    slug = "flux-demo-dbg"
    monkeypatch.setattr(main_mod.provider_guard, "budget_gate",
                        lambda *a, **k: pg.BudgetDecision(False, "BUDGET_EXHAUSTED", {}))
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "budget_exhausted"
    assert not _dispatch_ok


# ---------- demo dispatch: per-IP sliding window ----------
def test_demo_dispatch_bounded_by_per_ip_window(_no_hermes, _dispatch_ok, monkeypatch):
    a = _register("dbg-ip@fluxswarm.test")
    slug = "flux-demo-dbg-ip"
    monkeypatch.setattr(main_mod, "_DEMO_DAILY_CAP", 1000)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_GLOBAL_MAX", 1000)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_IP_MAX", 2)
    for _ in range(2):  # the first two hits pass (max inclusive)
        assert client.post(f"/api/projects/{slug}/dispatch",
                           headers=_auth(a["token"])).status_code == 200
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "demo_dispatch_ip_limit"


# ---------- demo dispatch: global window is aggregate across users ----------
def test_demo_dispatch_global_window_is_aggregate_across_users(_no_hermes, _dispatch_ok, monkeypatch):
    a = _register("dbg-g1@fluxswarm.test")
    b = _register("dbg-g2@fluxswarm.test")
    slug = "flux-demo-dbg-g"
    monkeypatch.setattr(main_mod, "_DEMO_DAILY_CAP", 1000)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_IP_MAX", 1000)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_GLOBAL_MAX", 2)
    # user A consumes the whole global window...
    assert client.post(f"/api/projects/{slug}/dispatch",
                       headers=_auth(a["token"])).status_code == 200
    assert client.post(f"/api/projects/{slug}/dispatch",
                       headers=_auth(a["token"])).status_code == 200
    # ...so user B is denied even though their own daily cap is untouched.
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(b["token"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "demo_dispatch_global_limit"


# ---------- daily cap preserved: owned dispatch still works under healthy budget ----------
def test_owned_dispatch_works_when_budget_healthy(_no_hermes, _dispatch_ok):
    a = _register("dbg-ok2@fluxswarm.test")
    slug = _mk_project(a)
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ---------- per-user daily cap still enforced together with the new windows ----------
def test_demo_daily_cap_still_enforced_with_bounded_windows(_no_hermes, _dispatch_ok, monkeypatch):
    a = _register("dbg-daily@fluxswarm.test")
    slug = "flux-demo-dbg-daily"
    monkeypatch.setattr(main_mod, "_DEMO_DAILY_CAP", 3)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_GLOBAL_MAX", 1000)
    monkeypatch.setattr(main_mod, "_DEMO_DISPATCH_IP_MAX", 1000)
    for _ in range(3):  # 3 hits == the daily allowance
        assert client.post(f"/api/projects/{slug}/dispatch",
                           headers=_auth(a["token"])).status_code == 200
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 429, r.text
    assert r.json()["error"] == "demo_daily_limit"
    assert len(_dispatch_ok) == 3, "the 4th dispatch inside the daily cap must not run the swarm"