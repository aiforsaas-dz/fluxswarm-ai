"""Regression tests for Phase 15 fixes.

FIX-1  demo daily cap (POST /api/projects/{slug}/dispatch on flux-demo-*)
       + operator kill-switch (503 on cost-bearing surfaces).
FIX-3  password max_length (schema guard pre-hash).
FIX-5  foreign_keys pragma active per connection.
FIX-6  indexes created.
"""
from __future__ import annotations

import datetime

from fastapi.testclient import TestClient

import db as db_mod
import main as main_mod

client = TestClient(main_mod.app)


def _reg(email: str, pw: str = "passw0rd", name: str = "T"):
    return client.post("/api/auth/register",
                       json={"email": email, "name": name, "password": pw, "tos_accept": True})


def _demo_dispatch(tok: str, slug: str = "flux-demo-1"):
    return client.post(f"/api/projects/{slug}/dispatch",
                       headers={"Authorization": "Bearer " + tok})


def test_fix1_demo_daily_cap(monkeypatch):
    monkeypatch.setattr(main_mod, "_DEMO_DAILY_CAP", 3)
    monkeypatch.setattr(main_mod.hc, "dispatch", lambda *a, **k: {"ok": True})
    r1 = _reg("fixcap@x.com")
    assert r1.status_code == 200, r1.text
    tok = r1.json()["token"]
    day = datetime.date.today().isoformat()  # noqa: F841  (schema sanity only)

    for i in range(3):
        r = _demo_dispatch(tok)
        assert r.status_code == 200, r.text
    r = _demo_dispatch(tok)
    assert r.status_code == 429, r.text  # capped on the 4th

    # Own-board dispatch is NOT metered by the demo cap.
    uid = r1.json()["user"]["id"]
    own = f"u{uid}-own"
    db_mod.add_project(uid, own, "own", "goal")
    r = client.post(f"/api/projects/{own}/dispatch",
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text


def test_fix1_kill_switch_503(monkeypatch):
    monkeypatch.setattr(main_mod.hc, "dispatch", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(main_mod.hc, "launch_swarm",
                        lambda slug, goal: type("R", (), {"root_id": "r"})(),
                        raising=True)
    r1 = _reg("fixkill@x.com")
    assert r1.status_code == 200, r1.text
    monkeypatch.setattr(main_mod, "_operator_maintenance", lambda: True)
    # public demo
    r = client.get("/api/demo/launch")
    assert r.status_code == 503, r.text
    # authenticated dispatch (any board)
    r = _demo_dispatch(r1.json()["token"])
    assert r.status_code == 503, r.text


def test_fix3_password_max_length_rejected():
    r = _reg("fixlong@x.com", pw="a" * 8192)
    assert r.status_code in (400, 422), r.status_code


def test_fix5_foreign_keys_enforced():
    c = db_mod._conn()
    try:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        c.close()


def test_fix6_indexes_present():
    c = db_mod._conn()
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_projects_user", "idx_purchases_template",
                "idx_purchases_buyer", "idx_referrals_code"} <= names
    finally:
        c.close()