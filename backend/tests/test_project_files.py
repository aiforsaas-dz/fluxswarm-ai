"""Project Files + edit-after-build tests.

Covers the customer file-upload loop and the reopen policy on top of the
other gonca surfaces: (a) authentic uploads to a board's native attachments
store that fail closed on unsafe slug/filename and the size cap; (b) ownership
is enforced on upload/delete (owner-only, demo boards are read-only for
files); (c) the Project Files browser lists both the attachments store and the
generated workspace; (d) reopen unseals a sealed board and re-arms parked
lanes but refuses refunded launches; (e) every call stays on the Re-used
``FLUXSWARM_DEMO_MODE`` sandbox (no real Hermes CLI, no real provider).

Everything is run against the shared temp DB + a temp HERMES_HOME sandbox, so
no test touches the operator's real boards.
"""
from __future__ import annotations

import sqlite3

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
        lambda slug, goal, provider_keys=None:
            type("R", (), {"root_id": "r1", "worker_ids": ["w1", "w2", "w3", "w4"],
                           "verifier_id": "w5", "synthesizer_id": "w6"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [
        {"id": "t1", "title": "Plan", "status": "done"},
        {"id": "t2", "title": "Build", "status": "todo"},
    ])
    monkeypatch.setattr(main_mod.hc, "read_workspace",
                        lambda s: "generated/index.html\n")


@pytest.fixture()
def _sandbox(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp sandbox + create a real board DB so the
    file-store helpers actually write under the test temp."""
    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _register(email: str, name: str = "File Tester"):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": name,
                          "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(user: dict) -> str:
    """Create a project through the real API (no-hermes stubs the driver)."""
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "Files", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


# ---------- upload endpoint ----------
def test_upload_requires_auth(_no_hermes):
    r = client.post("/api/projects/u1-x/attachments",
                    files={"file": ("spec.md", b"# Spec", "text/markdown")})
    assert r.status_code == 401


def test_upload_owner_only(_no_hermes, _sandbox):
    a = _register("files-a@fluxswarm.test")
    b = _register("files-b@fluxswarm.test")
    slug = _mk_project(a)
    r = client.post(f"/api/projects/{slug}/attachments",
                    headers=_auth(b["token"]),
                    files={"file": ("spec.md", b"# Spec", "text/markdown")})
    assert r.status_code == 403, r.text
    # demo boards are public showcase but read-only for files
    r = client.post("/api/projects/flux-demo-1/attachments",
                    headers=_auth(b["token"]),
                    files={"file": ("x.md", b"x", "text/markdown")})
    assert r.status_code == 403, r.text


def test_upload_roundtrip_and_list(_no_hermes, _sandbox):
    a = _register("files-c@fluxswarm.test")
    slug = _mk_project(a)
    r = client.post(f"/api/projects/{slug}/attachments",
                    headers=_auth(a["token"]),
                    files={"file": ("spec.md", b"# Spec", "text/markdown")})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "spec.md"
    assert r.json()["size"] == len(b"# Spec")
    # mirrored into the board's native attachments dir + uploads/
    att_dir = main_mod.hc.project_attachments_dir(slug)
    assert (att_dir / "spec.md").is_file()
    ws = main_mod.hc.project_workspace_dir(slug)
    assert (ws / "uploads" / "spec.md").is_file()
    # the browser lists both stores
    r = client.get(f"/api/projects/{slug}/files", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    assert any(f["name"] == "spec.md" for f in r.json()["attachments"])


def test_upload_rejects_unsafe_names_and_empty(_no_hermes, _sandbox):
    a = _register("files-d@fluxswarm.test")
    slug = _mk_project(a)
    tok = _auth(a["token"])
    # path components are stripped to a safe basename — never a traversal
    r = client.post(f"/api/projects/{slug}/attachments", headers=tok,
                    files={"file": ("../evil.txt", b"data", "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "evil.txt"
    r = client.post(f"/api/projects/{slug}/attachments", headers=tok,
                    files={"file": ("a/b.txt", b"data", "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "b.txt"
    # names that reduce to nothing or carry illegal chars are rejected
    for bad in ("..", "?", "?" * 200, f"{'x' * 161}.txt"):
        r = client.post(f"/api/projects/{slug}/attachments", headers=tok,
                        files={"file": (bad, b"data", "text/plain")})
        assert r.status_code in (400, 422), (bad, r.text)
    # genuine empty filename → rejected
    r = client.post(f"/api/projects/{slug}/attachments", headers=tok,
                    files={"file": ("", b"data", "text/plain")})
    assert r.status_code in (400, 422), r.text
    # missing file field → 422 validation error
    r = client.post(f"/api/projects/{slug}/attachments",
                    headers=tok, data={})
    assert r.status_code == 422


def test_upload_size_cap(_no_hermes, _sandbox, monkeypatch):
    monkeypatch.setattr(main_mod.hc, "_ATTACH_MAX_BYTES", 4)
    a = _register("files-e@fluxswarm.test")
    slug = _mk_project(a)
    r = client.post(f"/api/projects/{slug}/attachments",
                    headers=_auth(a["token"]),
                    files={"file": ("big.bin", b"0123456789", "application/octet-stream")})
    assert r.status_code == 400, r.text
    assert "size cap" in r.json()["detail"]


def test_files_browser_ownership(_no_hermes, _sandbox):
    a = _register("files-f@fluxswarm.test")
    b = _register("files-g@fluxswarm.test")
    slug = _mk_project(a)
    r = client.get(f"/api/projects/{slug}/files", headers=_auth(b["token"]))
    assert r.status_code == 403, r.text
    # guessed / demo boards follow the read rules
    assert client.get("/api/projects/u999999-x/files",
                      headers=_auth(b["token"])).status_code == 403


def test_delete_owner_only(_no_hermes, _sandbox):
    a = _register("files-h@fluxswarm.test")
    b = _register("files-i@fluxswarm.test")
    slug = _mk_project(a)
    tok = _auth(a["token"])
    r = client.post(f"/api/projects/{slug}/attachments", headers=tok,
                    files={"file": ("spec.md", b"# Spec", "text/markdown")})
    assert r.status_code == 200
    # cross-tenant delete denied
    r = client.delete(f"/api/projects/{slug}/attachments/spec.md",
                      headers=_auth(b["token"]))
    assert r.status_code == 403, r.text
    # owner can remove it
    r = client.delete(f"/api/projects/{slug}/attachments/spec.md", headers=tok)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert not (main_mod.hc.project_attachments_dir(slug) / "spec.md").exists()


# ---------- reopen (edit-after-build policy) ----------
def _seal_board(slug: str) -> None:
    main_mod.hc.seal_board(slug, reason="test seal")


def test_reopen_requires_auth(_no_hermes):
    r = client.post("/api/projects/u1-x/reopen")
    assert r.status_code == 401


def test_reopen_unseals_and_rearms(_no_hermes, _sandbox):
    a = _register("files-j@fluxswarm.test")
    slug = _mk_project(a)
    # ensure a real board DB exists under the temp sandbox (the no-hermes stub
    # does not create one), then park one lane and seal the board
    main_mod.hc._ensure_board_db(slug)
    db_path = main_mod.hc._board_db_path(slug)
    c = sqlite3.connect(str(db_path))
    try:
        for i, tid in enumerate(("t1", "t2")):
            c.execute("INSERT INTO tasks (id,title,assignee,status,created_at) "
                      "VALUES (?,?,?,?,?)",
                      (tid, "Lane", "ecc-planner", "done", 1000 + i))
        c.execute("UPDATE tasks SET status=? WHERE id=?", ("blocked", "t2"))
        c.commit()
    finally:
        c.close()
    _seal_board(slug)
    assert main_mod.hc.board_is_sealed(slug)
    r = client.post(f"/api/projects/{slug}/reopen", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["reopened"] is True
    assert not main_mod.hc.board_is_sealed(slug)
    # re-armed lane is ready again
    c = sqlite3.connect(str(db_path))
    try:
        rows = {tid: st for tid, st in c.execute("SELECT id, status FROM tasks")}
    finally:
        c.close()
    assert rows.get("t2") == "ready"
    assert rows.get("t1") == "ready"


def test_reopen_owner_only_and_refunds_blocked(_no_hermes, _sandbox):
    a = _register("files-k@fluxswarm.test")
    b = _register("files-l@fluxswarm.test")
    slug = _mk_project(a)
    _seal_board(slug)
    # cross-tenant reopen denied
    r = client.post(f"/api/projects/{slug}/reopen", headers=_auth(b["token"]))
    assert r.status_code == 403, r.text
    # refunded launch cannot be reopened
    proj = main_mod._project_by_board_slug(slug)
    assert proj, "project row must exist"
    db_mod.set_launch_outcome(proj["id"], "stuck", "stuck", refunded=True)
    r = client.post(f"/api/projects/{slug}/reopen", headers=_auth(a["token"]))
    assert r.status_code == 409, r.text
    assert "Refunded" in r.json()["detail"]


def test_reopen_returns_400_on_unsafe_slug(_no_hermes):
    a = _register("files-m@fluxswarm.test")
    r = client.post("/api/projects/u1-../../etc/reopen", headers=_auth(a["token"]))
    assert r.status_code == 403, r.text