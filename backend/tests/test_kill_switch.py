"""Phase 4 — operator kill-switch coverage on the remaining cost-bearing surfaces.

The switch contract (``_operator_maintenance``) promises a 503 on ALL
cost-bearing/demo surfaces. Before this phase it gated /api/demo/launch and
POST dispatch, but NOT the biggest spender of all — project creation (which
debits a credit and fires a real agent team) — nor the /api/demo/micro surface.

Accepted behaviour under FLUXSWARM_KILL_SWITCH=1:
  * POST /api/projects               -> 503, credit untouched, no board created;
  * GET  /api/demo/micro             -> 503;
  * GET  /api/demo/launch & dispatch -> 503 (regression pin).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import db as db_mod
import hermes_client as hc
import main as main_mod

client = TestClient(main_mod.app)


class _PermissiveLimiter:
    def ip_allowed(self, ip): return True
    def hit_ip(self, ip): return None
    def register_allowed(self, ip): return True
    def record_registration(self, ip): return None
    def login_allowed(self, ip, email): return True
    def record_login_failure(self, ip, email): return None
    def clear_login_failures(self, ip, email): return None
    def purchase_allowed(self, uid): return True
    def record_purchase(self, uid): return None

    def check(self, key: str, max_calls: int, window: int) -> bool:
        return True


@pytest.fixture(autouse=True)
def _permissive_limiter(monkeypatch):
    monkeypatch.setattr(main_mod, "limiter", _PermissiveLimiter())


@pytest.fixture()
def _no_hermes(monkeypatch):
    """Launch path must never touch the real Hermes CLI in these tests."""
    monkeypatch.setattr(main_mod.hc, "projects_are_thin", lambda: False)
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(
        main_mod.hc, "launch_swarm",
        lambda slug, goal, provider_keys=None:
            type("R", (), {"root_id": "r1", "worker_ids": ["w1", "w2"],
                           "verifier_id": "w3", "synthesizer_id": "w4"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: setattr(main_mod, "_fired", True))


@pytest.fixture()
def _kill_switch(monkeypatch):
    monkeypatch.setenv("FLUXSWARM_KILL_SWITCH", "1")


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- project creation honoured ----------
def test_create_project_blocked_by_kill_switch(_no_hermes, _kill_switch):
    a = _register("ks-create@fluxswarm.test")
    r = client.post("/api/projects", headers=_auth(a["token"]),
                    json={"name": "KS", "goal": "Build a landing page"})
    assert r.status_code == 503, r.text
    assert "temporarily unavailable" in r.json()["detail"]
    # nothing was debited (5 starting credits survive) and no board was created
    me = client.get("/api/me", headers=_auth(a["token"])).json()
    assert me["credits"] == 5, "kill-switch refusal must not debit a credit"
    assert db_mod.list_user_projects(a["user"]["id"]) == []


# ---------- demo micro honoured ----------
def test_demo_micro_blocked_by_kill_switch(_kill_switch):
    r = client.get("/api/demo/micro", params={"goal": "Build a todo app"})
    assert r.status_code == 503, r.text


# ---------- regression pin: launch + dispatch already honoured ----------
def test_demo_launch_still_blocked_by_kill_switch(_kill_switch):
    r = client.get("/api/demo/launch")
    assert r.status_code == 503, r.text


def test_dispatch_still_blocked_by_kill_switch(_kill_switch):
    a = _register("ks-dispatch@fluxswarm.test")
    r = client.post("/api/projects/flux-demo-ks/dispatch",
                    headers=_auth(a["token"]))
    assert r.status_code == 503, r.text


# ---------- switch OFF keeps everything normal (regression) ----------
def _mk_project(user: dict) -> str:
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "KS-off", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def test_create_project_works_when_switch_off(_no_hermes):
    a = _register("ks-off@fluxswarm.test")
    slug = _mk_project(a)
    assert slug.startswith(f"u{a['user']['id']}-")


def test_demo_micro_works_when_switch_off():
    r = client.get("/api/demo/micro", params={"goal": "Build a billing dashboard"})
    assert r.status_code == 200, r.text
    assert "tech_stack" in r.json()