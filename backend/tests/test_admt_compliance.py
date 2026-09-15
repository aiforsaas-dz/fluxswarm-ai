"""CCPA/CPRA ADMT compliance suite (Phase 5).

Covers the pre-use notice + acknowledgment, opt-out/opt-in (dispatch gate),
human-review request + admin queue/resolution + notification, per-project
logic disclosure, the risk-assessment document, and a full end-to-end flow.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import audit
import db
import main as main_mod
from main import app

client = TestClient(app)

_ADMIN_TOKEN = os.environ.get("FLUXSWARM_ADMIN_TOKEN", "test-admin-token-not-secret")
_ADMT_VERSION = "2026-09-04"


def _fresh_user() -> tuple[str, str]:
    email = f"admtc-{uuid.uuid4().hex[:10]}@fluxswarm.test"
    r = client.post("/api/auth/register", json={
        "email": email, "name": "ADMT User", "password": "pw-12345678", "tos_accept": True,
    })
    assert r.status_code == 200, r.text
    tok = client.post("/api/auth/login", json={
        "email": email, "password": "pw-12345678",
    }).json()["token"]
    return email, tok


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin() -> dict:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _uid(email: str) -> int:
    return db.get_user_by_email(email)["id"]


def _make_project(uid: int, goal: str = "Build a scalable API") -> int:
    return db.add_project(uid, f"u{uid}-admttest-{uuid.uuid4().hex[:6]}", "Proj", goal)


def _read_trail() -> list[dict]:
    entries = []
    with open(audit.AUDIT_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
    return entries


def _assert_dispatch_blocked(slug: str, token: str):
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(token))
    assert r.status_code == 403, r.text
    assert "ADMT opt-out active" in r.json()["detail"]


# 1 ---------------------------------------------------------------------------
def test_pre_use_notice_content():
    _, tok = _fresh_user()
    r = client.get("/api/account/admt-notice", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == "FluxSwarm"
    assert set(body.keys()) >= {
        "admt_types", "description", "logic_summary", "human_review_available",
        "opt_out_available", "last_updated",
    }
    assert body["admt_types"] == ["planning", "architecture", "coding", "review", "deployment"]
    assert body["human_review_available"] is True
    assert body["opt_out_available"] is True
    assert body["last_updated"] == _ADMT_VERSION


# 2 ---------------------------------------------------------------------------
def test_notice_acknowledgment():
    email, tok = _fresh_user()
    r = client.post("/api/account/admt-notice/acknowledge", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged"] is True
    assert r.json()["notice_version"] == _ADMT_VERSION
    assert db.has_admt_notice_ack(_uid(email)) is True


# 3 ---------------------------------------------------------------------------
def test_opt_out_blocks_dispatch():
    email, tok = _fresh_user()
    uid = _uid(email)
    pid = _make_project(uid)
    slug = db.get_user_by_id(uid) and next(
        (p["board_slug"] for p in db.list_user_projects(uid) if p["id"] == pid), None)
    r = client.post("/api/account/opt-out-admt", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert db.get_admt_opt_out(uid) is True
    _assert_dispatch_blocked(slug, tok)


# 4 ---------------------------------------------------------------------------
def test_opt_in_allows_dispatch(monkeypatch):
    email, tok = _fresh_user()
    uid = _uid(email)
    pid = _make_project(uid)
    slug = next(p["board_slug"] for p in db.list_user_projects(uid) if p["id"] == pid)
    monkeypatch.setattr(main_mod.hc, "dispatch", lambda *a, **k: {"ok": True})

    client.post("/api/account/opt-out-admt", headers=_auth(tok))
    _assert_dispatch_blocked(slug, tok)

    r = client.post("/api/account/opt-in-admt", json={
        "acknowledge": True, "last_updated": _ADMT_VERSION,
    }, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["admt_opt_out"] is False
    assert db.get_admt_opt_out(uid) is False

    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(tok))
    assert r.status_code == 200, r.text


# 5 ---------------------------------------------------------------------------
def test_opt_out_idempotent():
    email, tok = _fresh_user()
    uid = _uid(email)
    first = client.post("/api/account/opt-out-admt", headers=_auth(tok)).json()
    second = client.post("/api/account/opt-out-admt", headers=_auth(tok)).json()
    assert first["ok"] is True and first["was_already"] is False
    assert second["ok"] is True and second["was_already"] is True
    assert db.get_admt_opt_out(uid) is True


# 6 ---------------------------------------------------------------------------
def test_human_review_request():
    email, tok = _fresh_user()
    pid = _make_project(_uid(email))
    r = client.post(f"/api/projects/{pid}/request-human-review", headers=_auth(tok))
    assert r.status_code == 200, r.text
    review_id = r.json()["review_id"]
    review = db.get_human_review(review_id)
    assert review["human_review_status"] == "requested"
    assert review["user_id"] == _uid(email)
    assert review["requested_at"]


# 7 ---------------------------------------------------------------------------
def test_human_review_admin_queue():
    email, tok = _fresh_user()
    pid = _make_project(_uid(email))
    client.post(f"/api/projects/{pid}/request-human-review", headers=_auth(tok)).json()

    r = client.get("/api/admin/human-review-queue", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 1
    assert any(item["user_email"] == email for item in body["pending"])

    assert client.get("/api/admin/human-review-queue",
                      headers=_auth(tok)).status_code == 401


# 8 ---------------------------------------------------------------------------
def test_human_review_approval():
    email, tok = _fresh_user()
    uid = _uid(email)
    pid = _make_project(uid)
    review_id = client.post(f"/api/projects/{pid}/request-human-review",
                            headers=_auth(tok)).json()["review_id"]

    r = client.patch(f"/api/admin/human-review/{review_id}", json={
        "status": "approved", "reviewer_notes": "Code matches goal.",
    }, headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    review = db.get_human_review(review_id)
    assert review["human_review_status"] == "approved"
    assert review["reviewer_notes"] == "Code matches goal."
    assert review["reviewed_at"]

    trail = _read_trail()
    assert any(e.get("event") == "admt.notify" and e.get("uid") == uid
               for e in trail)
    assert any(e.get("event") == "admt.review.update" and e.get("review_status") == "approved"
               and e.get("uid") == uid for e in trail)

    queue = client.get("/api/admin/human-review-queue", headers=_admin()).json()["pending"]
    assert all(item["id"] != review_id for item in queue)


# 9 ---------------------------------------------------------------------------
def test_logic_access(monkeypatch):
    email, tok = _fresh_user()
    uid = _uid(email)
    pid = _make_project(uid, goal="A scalable multi-tenant web API with PostgreSQL")
    monkeypatch.setenv("FLUXSWARM_DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setenv("FLUXSWARM_DEFAULT_MODEL", "claude-3-5-sonnet")

    r = client.get(f"/api/projects/{pid}/admt-logic", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert "scalable" in body["goal"]
    assert body["agents_used"] == [
        "ecc-planner", "ecc-architect", "ecc-devops", "ecc-tdd",
        "ecc-reviewer", "ecc-designer", "ecc-build-fixer", "ecc-auditor",
    ]
    assert body["provider"] == "anthropic"
    assert body["runtime_model"] == "claude-3-5-sonnet"
    assert body["generated_at"]
    assert body["decisions"]
    for d in body["decisions"]:
        assert d["agent"] and d["decision"] and d["rationale"]
    assert any(d["decision"] == "Selected microservices architecture" for d in body["decisions"])


# 10 --------------------------------------------------------------------------
def test_logic_access_other_user():
    email1, tok1 = _fresh_user()
    _, tok2 = _fresh_user()
    pid = _make_project(_uid(email1))
    r = client.get(f"/api/projects/{pid}/admt-logic", headers=_auth(tok2))
    assert r.status_code == 403, r.text
    assert client.get(f"/api/projects/{pid}/admt-logic", headers=_auth(tok1)).status_code == 200


# 11 --------------------------------------------------------------------------
def test_admt_disclosure_table():
    c = db._conn()
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(admt_disclosures)")]
    finally:
        c.close()
    for required in ("user_id", "project_id", "disclosed_at", "acknowledged_at",
                     "admt_type", "logic_summary", "human_review_status",
                     "requested_at", "reviewed_at", "reviewer_notes"):
        assert required in cols, required

    c = db._conn()
    try:
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    finally:
        c.close()
    assert "admt_opt_out" in ucols


# 12 --------------------------------------------------------------------------
def test_audit_log_opt_out():
    email, tok = _fresh_user()
    uid = _uid(email)
    client.post("/api/account/opt-out-admt", headers=_auth(tok))
    trail = _read_trail()
    assert any(e.get("event") == "admt.optout" and e.get("uid") == uid
               for e in trail)


# 13 --------------------------------------------------------------------------
def test_pre_use_notice_i18n():
    _, tok = _fresh_user()
    en = client.get("/api/account/admt-notice?lang=en", headers=_auth(tok)).json()
    ar = client.get("/api/account/admt-notice?lang=ar", headers=_auth(tok)).json()
    assert set(en.keys()) == set(ar.keys())
    assert "argo" not in en["description"] and en["description"].startswith("AI agents")
    # Platform is English-only: both lang params return the same English content.
    assert ar["description"] == en["description"] and ar["description"].strip()
    assert ar["logic_summary"] == en["logic_summary"]
    assert ar["last_updated"] == en["last_updated"] == _ADMT_VERSION


# 14 --------------------------------------------------------------------------
def test_risk_assessment_doc_exists():
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "docs" / "CCPA_RISK_ASSESSMENT.md",
        Path(__file__).resolve().parent.parent / "docs" / "CCPA_RISK_ASSESSMENT.md",
    ]
    doc = next((p for p in candidates if p.exists()), None)
    assert doc is not None, "docs/CCPA_RISK_ASSESSMENT.md not found"
    text = doc.read_text(encoding="utf-8")
    for section in ("ADMT Inventory", "Data Minimization", "Security Measures",
                    "Human Review Workflow", "Annual Review Schedule"):
        assert section in text, section
    assert "privacy@fluxswarm.ai" in text
    assert "2027-01-01" in text
    assert "48 hours" in text


# 15 --------------------------------------------------------------------------
def test_admt_integration_full(monkeypatch):
    # Real Hermes CLI would run at project creation; loop it out of the flow.
    def _fake_swarm(*a, **k):
        return SimpleNamespace(
            root_id="r1", worker_ids=["w1"], verifier_id="v1", synthesizer_id="s1")
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda slug: True)
    monkeypatch.setattr(main_mod.hc, "launch_swarm", _fake_swarm)
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.hc, "dispatch", lambda *a, **k: {"ok": True})

    email, tok = _fresh_user()
    uid = _uid(email)

    # notice -> acknowledge
    notice = client.get("/api/account/admt-notice", headers=_auth(tok)).json()
    assert notice["last_updated"] == _ADMT_VERSION
    client.post("/api/account/admt-notice/acknowledge", headers=_auth(tok))

    # launch (create + dispatch)
    created = client.post("/api/projects", json={
        "name": "Full Flow", "goal": "Build a billing dashboard",
    }, headers=_auth(tok))
    assert created.status_code == 200, created.text
    slug = created.json()["slug"]
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(tok))
    assert r.status_code == 200, r.text

    # opt-out -> blocked
    client.post("/api/account/opt-out-admt", headers=_auth(tok))
    _assert_dispatch_blocked(slug, tok)

    # manual project creation still allowed (no AI agents, no credit debit)
    manual = client.post("/api/projects", json={
        "name": "Manual", "goal": "Write a small script",
    }, headers=_auth(tok))
    assert manual.status_code == 200, manual.text
    assert manual.json().get("manual") is True

    # human review -> admin approval -> notified
    pid = created.json()["slug"] and next(
        p["id"] for p in db.list_user_projects(uid) if p["board_slug"] == slug)
    review_id = client.post(f"/api/projects/{pid}/request-human-review",
                            headers=_auth(tok)).json()["review_id"]
    queue = client.get("/api/admin/human-review-queue", headers=_admin()).json()["pending"]
    assert any(item["id"] == review_id for item in queue)
    r = client.patch(f"/api/admin/human-review/{review_id}", json={
        "status": "approved", "reviewer_notes": "OK to ship.",
    }, headers=_admin())
    assert r.status_code == 200, r.text
    trail = _read_trail()
    assert any(e.get("event") == "admt.notify" and e.get("uid") == uid for e in trail)

    # opt-in (re-read notice) -> dispatch unblocked
    r = client.post("/api/account/opt-in-admt", json={
        "acknowledge": True, "last_updated": _ADMT_VERSION,
    }, headers=_auth(tok))
    assert r.status_code == 200, r.text
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(tok))
    assert r.status_code == 200, r.text


# 16 --------------------------------------------------------------------------
def _smoke_demo(monkeypatch):
    """Demo launch without the real swarm/provider machinery (hermetic + fast)."""
    # A REAL (fake) pick: since quota gates now run AFTER a provider resolves
    # (a pool-down launch must NOT burn a user's launch slot), a hermetic pick
    # is required for the rate-limit tests to consume slots like real users do.
    monkeypatch.setattr(
        main_mod.provider_pool, "pick_demo_provider",
        lambda: {"provider": "google", "model": "gemini-3.5-flash-lite",
                 "probe_key": "gemini", "requires_key": False})
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda slug: True)

    def _fake_profile(board, goal, provider=None, model=None):
        return {"planner_id": "p1", "builder_id": "b1", "workspace": "/tmp/ws"}

    monkeypatch.setattr(main_mod.hc, "launch_demo_profile", _fake_profile)
    monkeypatch.setattr(main_mod, "_demo_drive", lambda **k: None)


def test_opt_out_blocks_demo():
    email, tok = _fresh_user()
    uid = _uid(email)
    client.post("/api/account/opt-out-admt", headers=_auth(tok))
    assert db.get_admt_opt_out(uid) is True
    r = client.get("/api/demo/launch", headers=_auth(tok))
    assert r.status_code == 403, r.text
    assert "opt-out" in r.json()["detail"]


# 17 --------------------------------------------------------------------------
def test_human_review_unauthorized():
    email, tok = _fresh_user()
    pid = _make_project(_uid(email))
    fresh = TestClient(main_mod.app)  # no session cookie from prior registers
    r = fresh.post(f"/api/projects/{pid}/request-human-review")
    assert r.status_code == 401, r.text
    r = client.post(f"/api/projects/{pid}/request-human-review", headers=_auth(tok))
    assert r.status_code == 200, r.text
    r = fresh.get("/api/admin/human-review-queue")
    assert r.status_code == 401, r.text


# 18 --------------------------------------------------------------------------
def test_human_review_rejection():
    email, tok = _fresh_user()
    uid = _uid(email)
    pid = _make_project(uid)
    review_id = client.post(f"/api/projects/{pid}/request-human-review",
                            headers=_auth(tok)).json()["review_id"]
    r = client.patch(f"/api/admin/human-review/{review_id}", json={
        "status": "rejected", "reviewer_notes": "Out of scope.",
    }, headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"

    review = db.get_human_review(review_id)
    assert review["human_review_status"] == "rejected"
    assert review["reviewer_notes"] == "Out of scope."
    assert review["reviewed_at"]

    trail = _read_trail()
    assert any(e.get("event") == "admt.notify" and e.get("uid") == uid for e in trail)


# 19 --------------------------------------------------------------------------
def test_demo_rate_limit_ip(monkeypatch):
    _smoke_demo(monkeypatch)
    first = client.get("/api/demo/launch")
    assert first.status_code == 200, first.text
    second = client.get("/api/demo/launch")
    assert second.status_code == 429, second.text
    body = second.json()
    assert body["error"] == "demo_ip_limit"
    assert "en" in body["message"] and "ar" not in body["message"]
    assert body["retry_after_seconds"] == 3600
    assert body["upgrade_url"] == "/pricing"


# 20 --------------------------------------------------------------------------
def test_demo_rate_limit_global(monkeypatch):
    _smoke_demo(monkeypatch)
    seq = {"n": 0}

    def _uniq_ip(request):
        seq["n"] += 1
        return f"10.20.{seq['n'] // 240}.{seq['n'] % 240}"

    monkeypatch.setattr(main_mod, "_client_ip", _uniq_ip)
    for _ in range(20):
        r = client.get("/api/demo/launch")
        assert r.status_code == 200, r.text
    blocked = client.get("/api/demo/launch")
    assert blocked.status_code == 429, blocked.text
    body = blocked.json()
    assert body["error"] == "demo_global_limit"
    assert body["message"]["en"] and "ar" not in body["message"]


# 21 --------------------------------------------------------------------------
def test_demo_micro_fast():
    t0 = time.monotonic()
    r = client.get("/api/demo/micro", params={"goal": "Build a billing dashboard"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"plan", "tech_stack", "estimated_time"}
    assert body["estimated_time"] == "2 hours"
    assert body["tech_stack"] == "FastAPI + PostgreSQL"
    assert elapsed < 2.0


# 22 --------------------------------------------------------------------------
def test_demo_status_quota_shape():
    r = client.get("/api/demo/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["daily_quota_total"] == 20
    assert body["daily_quota_remaining"] <= body["daily_quota_total"]
    assert body["your_ip_limit_window"] == "3600s"
    assert body["next_reset"].endswith("Z")


# 23 --------------------------------------------------------------------------
def test_admin_provider_agreements_endpoint():
    email, tok = _fresh_user()
    uid = _uid(email)
    db.agree_provider(uid, "openrouter", "1.0")
    r = client.get("/api/admin/provider-agreements", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["count"], int)
    assert isinstance(body["by_provider"], list)
    assert client.get("/api/admin/provider-agreements", headers=_auth(tok)).status_code == 401