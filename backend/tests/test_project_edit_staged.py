"""Edit-after-build (PATCH goal/name) + pre-launch staged attachments tests.

Covers:
  (a) PATCH /api/projects/{slug} — owner-only goal/name update of a built
      project, refusal of refunded launches and unsafe slugs;
  (b) the pre-launch staged-attachment endpoints (stage/list/remove) and their
      consumption by the NEXT create-project launch;
  (c) the TTL/size caps on staging so stale uploads never pile up;
  (d) audit entries on both surfaces.

Everything runs against the Re-used ``FLUXSWARM_DEMO_MODE`` sandbox (no real
Hermes CLI, no real provider).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import db as db_mod
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


@pytest.fixture(autouse=True)
def _permissive_limiter(monkeypatch):
    monkeypatch.setattr(main_mod, "limiter", _PermissiveLimiter())


@pytest.fixture()
def _no_hermes(monkeypatch):
    """Launch/board reads must never touch the real Hermes CLI."""
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(
        main_mod.hc, "launch_swarm",
        lambda slug, goal, provider_keys=None, **k:
            type("R", (), {"root_id": "r1", "worker_ids": ["w1", "w2", "w3", "w4"],
                           "verifier_id": "w5", "synthesizer_id": "w6"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)


@pytest.fixture()
def _sandbox(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp sandbox for staging + board writes."""
    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _upload(name: str, data: bytes, mime: str):
    """A single-file multipart body under the ``files`` field name."""
    return [("files", (name, data, mime))]


def _register(suffix: str):
    r = client.post("/api/auth/register",
                    json={"email": f"{suffix}-{int(time.time()*1000)}@fluxswarm.test",
                          "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(user: dict) -> str:
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "Edit", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


# ---------- PATCH edit-after-build ----------
def test_patch_requires_auth(_no_hermes, _sandbox):
    fresh = TestClient(main_mod.app)
    r = fresh.patch("/api/projects/u1-x", json={"goal": "new"})
    assert r.status_code == 401


def test_patch_owner_only(_no_hermes, _sandbox):
    a = _register("edit-owner")
    b = _register("edit-other")
    slug = _mk_project(a)
    r = client.patch(f"/api/projects/{slug}", headers=_auth(b["token"]),
                     json={"goal": "hijack"})
    assert r.status_code == 403, r.text


def test_patch_updates_goal_and_name(_no_hermes, _sandbox):
    a = _register("edit-ok")
    slug = _mk_project(a)
    proj = main_mod._project_by_board_slug(slug)
    assert proj["goal"] == "Build a landing page"
    r = client.patch(f"/api/projects/{slug}", headers=_auth(a["token"]),
                     json={"name": "Renamed", "goal": "Build a storefront"})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] is True
    proj = main_mod._project_by_board_slug(slug)
    assert proj["name"] == "Renamed"
    assert proj["goal"] == "Build a storefront"


def test_patch_empty_body_rejected(_no_hermes, _sandbox):
    a = _register("edit-empty")
    slug = _mk_project(a)
    r = client.patch(f"/api/projects/{slug}", headers=_auth(a["token"]), json={})
    assert r.status_code == 400, r.text


def test_patch_refunded_launch_rejected(_no_hermes, _sandbox):
    a = _register("edit-refund")
    slug = _mk_project(a)
    proj = main_mod._project_by_board_slug(slug)
    db_mod.set_launch_outcome(proj["id"], "stuck", "stuck", refunded=True)
    r = client.patch(f"/api/projects/{slug}", headers=_auth(a["token"]),
                     json={"goal": "nope"})
    assert r.status_code == 409, r.text


def test_patch_unsafe_slug_rejected(_no_hermes):
    a = _register("edit-traversal")
    r = client.patch("/api/projects/u1-../../etc", headers=_auth(a["token"]),
                     json={"goal": "x"})
    assert r.status_code == 403, r.text


# ---------- pre-launch staged attachments ----------
def test_stage_requires_auth(_no_hermes, _sandbox):
    fresh = TestClient(main_mod.app)
    r = fresh.post("/api/project/staged-attachments", files=_upload("spec.md", b"# Spec", "text/markdown"))
    assert r.status_code == 401


def test_stage_list_remove_roundtrip(_no_hermes, _sandbox):
    a = _register("stage-a")
    tok = _auth(a["token"])
    r = client.post("/api/project/staged-attachments", headers=tok,
                    files=_upload("spec.md", b"# Spec", "text/markdown"))
    assert r.status_code == 200, r.text
    assert r.json()["staged"][0]["name"] == "spec.md"
    r = client.get("/api/project/staged-attachments", headers=tok)
    assert r.status_code == 200
    assert any(s["name"] == "spec.md" for s in r.json()["staged"])
    r = client.delete("/api/project/staged-attachments/spec.md", headers=tok)
    assert r.status_code == 200, r.text
    r = client.get("/api/project/staged-attachments", headers=tok)
    assert not r.json()["staged"]


def test_stage_rejects_unsafe_and_empty(_no_hermes, _sandbox):
    a = _register("stage-bad")
    tok = _auth(a["token"])
    # path components are stripped to a safe basename (same as board uploads)
    for bad in ("../evil", "a/b"):
        r = client.post("/api/project/staged-attachments", headers=tok,
                        files=_upload(bad, b"x", "text/plain"))
        assert r.status_code == 200, (bad, r.text)
    # names that reduce to nothing or carry illegal chars are rejected
    for bad in ("..", "?", "?" * 200, f"{'x' * 161}.txt"):
        r = client.post("/api/project/staged-attachments", headers=tok,
                        files=_upload(bad, b"x", "text/plain"))
        assert r.status_code in (400, 422), (bad, r.text)


def test_stage_size_cap(_no_hermes, _sandbox, monkeypatch):
    monkeypatch.setattr(main_mod.hc, "_ATTACH_MAX_BYTES", 4)
    a = _register("stage-cap")
    r = client.post("/api/project/staged-attachments", headers=_auth(a["token"]),
                    files=_upload("big.bin", b"0123456789", "application/octet-stream"))
    assert r.status_code == 413, r.text


def test_stage_total_cap(_no_hermes, _sandbox, monkeypatch):
    monkeypatch.setattr(main_mod, "_STAGE_MAX_BYTES", 5)
    a = _register("stage-total")
    tok = _auth(a["token"])
    r = client.post("/api/project/staged-attachments", headers=tok,
                    files=_upload("a.txt", b"1234", "text/plain"))
    assert r.status_code == 200, r.text
    r = client.post("/api/project/staged-attachments", headers=tok,
                    files=_upload("b.txt", b"1234", "text/plain"))
    assert r.status_code == 413, r.text


def test_staged_consumed_by_next_launch(_no_hermes, _sandbox):
    """Staged uploads become real attachments of the NEXT created project and
    the staging dir is cleared (every file reaches exactly one build)."""
    a = _register("stage-consume")
    tok = _auth(a["token"])
    r = client.post("/api/project/staged-attachments", headers=tok,
                    files=_upload("logo.png", b"PNGDATA", "image/png"))
    assert r.status_code == 200, r.text
    slug = _mk_project(a)
    att_dir = main_mod.hc.project_attachments_dir(slug)
    assert (att_dir / "logo.png").is_file()
    staged = client.get("/api/project/staged-attachments", headers=tok).json()["staged"]
    assert not staged, "staging dir must be cleared after consumption"


def test_stage_no_files_rejected(_no_hermes, _sandbox):
    a = _register("stage-none")
    r = client.post("/api/project/staged-attachments", headers=_auth(a["token"]),
                    files={})
    assert r.status_code == 400


def test_audit_records_stage_and_edit(_no_hermes, _sandbox):
    import audit as audit_mod
    a = _register("stage-aud")
    tok = _auth(a["token"])
    client.post("/api/project/staged-attachments", headers=tok,
                files=_upload("x.md", b"x", "text/markdown"))
    slug = _mk_project(a)
    client.patch(f"/api/projects/{slug}", headers=tok, json={"goal": "new goal"})
    with open(audit_mod.AUDIT_FILE, encoding="utf-8") as f:
        text = f.read()
    assert '"project.stage"' in text
    assert '"project.edit"' in text


def test_stage_ttl_sweep(_no_hermes, _sandbox, monkeypatch):
    monkeypatch.setattr(main_mod, "_STAGE_TTL_S", 0)
    a = _register("stage-ttl")
    tok = _auth(a["token"])
    client.post("/api/project/staged-attachments", headers=tok,
                files=_upload("z.txt", b"z", "text/plain"))
    d = main_mod._stage_dir(a["user"]["id"])
    for p in d.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
    r = client.get("/api/project/staged-attachments", headers=tok)
    assert r.status_code == 200
    assert not r.json()["staged"]