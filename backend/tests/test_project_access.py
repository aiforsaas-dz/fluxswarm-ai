"""Phase 1 — Authorization / Routing / Project Access (P0).

Ordered acceptance matrix for every project-scoped read surface:
  * owner PASS  — the owner reads their own board (tasks/workspace/files/security);
  * non-owner DENY  — the other tenant gets 403 and never touches the board;
  * anonymous DENY  — no session: API boards are 401 (required user) and owned
    previews are 403 (optional user);
  * demo PASS  — flux-demo-* boards remain public showcase (anonymous /p/ read
    and authenticated API read), but stay read-only for files;
  * refresh / new tab PASS  — the login/register responses set an HttpOnly
    ``fs_token`` session cookie, so a fresh page load (cookie only, no Bearer)
    and a direct URL navigation still see the signed-in user and their preview;
  * invalid project -> controlled 404  — an owned-but-never-created slug is a
    404 everywhere, never a 500 from the underlying store.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
            type("R", (), {"root_id": "r1", "worker_ids": ["w1", "w2", "w3", "w4"],
                           "verifier_id": "w5", "synthesizer_id": "w6"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [
        {"id": "t1", "title": "Plan", "status": "done"},
        {"id": "t2", "title": "Build", "status": "todo"},
    ])
    monkeypatch.setattr(main_mod.hc, "read_workspace", lambda s: "generated/index.html\n")
    monkeypatch.setattr(main_mod.hc, "list_project_files",
                        lambda s: {"attachments": [], "generated": ["generated/index.html"]})
    monkeypatch.setattr(main_mod.security, "scan_project",
                        lambda *a, **k: {"score": 0, "summary": "", "findings": []})


@pytest.fixture()
def _host(monkeypatch, tmp_path):
    """Point HERMES_HOME at a temp dir so workspaces are fully isolated."""
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(user: dict) -> str:
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "Access", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def _cookie_value(r) -> str | None:
    sc = r.headers.get("set-cookie", "")
    if not sc.startswith("fs_token="):
        return None
    return sc.split(";", 1)[0][len("fs_token="):]


# ---------- owner PASS ----------
def test_owner_reads_own_board(_no_hermes):
    a = _register("ph1-owner@fluxswarm.test")
    slug = _mk_project(a)
    for path in ("tasks", "workspace", "files", "security"):
        r = client.get(f"/api/projects/{slug}/{path}", headers=_auth(a["token"]))
        assert r.status_code == 200, (path, r.status_code, r.text)
    # owner may mint a preview ticket for their own board
    r = client.get(f"/api/projects/{slug}/preview-ticket", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == slug


# ---------- non-owner DENY ----------
def test_non_owner_denied_everywhere(_no_hermes):
    a = _register("ph1-other-a@fluxswarm.test")
    b = _register("ph1-other-b@fluxswarm.test")
    slug = _mk_project(a)
    for method, path in (("GET", "tasks"), ("GET", "workspace"), ("GET", "files"),
                         ("GET", "security"), ("GET", "preview-ticket")):
        r = client.get(f"/api/projects/{slug}/{path}", headers=_auth(b["token"]))
        assert r.status_code == 403, (path, r.status_code)
    r = client.post(f"/api/projects/{slug}/dispatch", headers=_auth(b["token"]))
    assert r.status_code == 403, r.text


# ---------- anonymous DENY ----------
def test_anonymous_denied(_no_hermes, _host):
    a = _register("ph1-anon@fluxswarm.test")
    uid = a["user"]["id"]
    slug = f"u{uid}-anon"
    d = hc.project_workspace_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<h1>private</h1>", encoding="utf-8")
    # A fresh client has no cookies and no JS-held Bearer token: a truly
    # anonymous caller (register sets the session cookie on the shared client).
    anon = TestClient(main_mod.app)
    # required-user API surfaces -> 401
    for path in ("tasks", "workspace", "files"):
        r = anon.get(f"/api/projects/{slug}/{path}")
        assert r.status_code == 401, (path, r.status_code)
    # optional-user surfaces never leak -> 403
    r = anon.get(f"/api/projects/{slug}/security")
    assert r.status_code == 403, r.text
    # owned preview without any session -> 403
    assert anon.get(f"/p/{slug}/").status_code == 403


# ---------- demo PASS ----------
def test_demo_board_public_showcase_read(_no_hermes, _host):
    slug = "flux-demo-ph1"
    d = hc.project_workspace_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    # any visitor can read the live preview
    r = client.get(f"/p/{slug}/")
    assert r.status_code == 200 and "<h1>hi</h1>" in r.text
    # any authenticated user can read the demo workspace / tasks
    b = _register("ph1-demo@fluxswarm.test")
    r = client.get(f"/api/projects/{slug}/workspace", headers=_auth(b["token"]))
    assert r.status_code == 200, r.text
    r = client.get(f"/api/projects/{slug}/tasks", headers=_auth(b["token"]))
    assert r.status_code == 200, r.text
    # but demo boards stay read-only: uploads are owner-only
    r = client.post(f"/api/projects/{slug}/attachments", headers=_auth(b["token"]),
                    files={"file": ("x.md", b"x", "text/markdown")})
    assert r.status_code == 403, r.text


# ---------- refresh / new tab PASS ----------
def test_login_sets_session_cookie_survives_fresh_page():
    a = _register("ph1-cookie@fluxswarm.test")
    r = client.post("/api/auth/login",
                    json={"email": "ph1-cookie@fluxswarm.test", "password": "s3cure-Pass-123"})
    assert r.status_code == 200, r.text
    raw = _cookie_value(r)
    assert raw, "login must set the fs_token session cookie"

    # A brand-new page load has no JS memory and no Bearer header — only the
    # cookie. get_current_user must treat it as a live session.
    r2 = client.get("/api/me", headers={"Cookie": f"fs_token={raw}"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["email"] == "ph1-cookie@fluxswarm.test"


def test_logout_clears_cookie_and_kills_session():
    a = _register("ph1-logout@fluxswarm.test")
    r = client.post("/api/auth/logout", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    # the response must expire the cookie (Max-Age=0 style clear)
    assert "fs_token=" in r.headers.get("set-cookie", "")
    assert "Max-Age=0" in r.headers.get("set-cookie", "") or "max-age=0" in r.headers.get("set-cookie", "").lower()
    raw = _cookie_value(r)
    # an expired value (``""`` may be quoted by the serializer) => cleared
    assert raw in ("", '""'), raw  # empty => expired
    # a fresh client that just lost the cookie can no longer authenticate
    fresh = TestClient(main_mod.app)
    assert fresh.get("/api/me").status_code == 401


def test_password_change_clears_cookie_and_session():
    a = _register("ph1-pw@fluxswarm.test")
    r = client.post("/api/auth/password", headers=_auth(a["token"]),
                    json={"current": "s3cure-Pass-123", "new": "N3w-Pass-4567"})
    assert r.status_code == 200, r.text
    assert "fs_token=" in r.headers.get("set-cookie", "")
    # old Bearer session is dead and the old cookie is dead
    assert client.get("/api/me", headers=_auth(a["token"])).status_code == 401


# ---------- direct URL PASS ----------
def test_direct_url_private_preview_with_cookie_only(_no_hermes, _host):
    a = _register("ph1-direct@fluxswarm.test")
    uid = a["user"]["id"]
    slug = f"u{uid}-direct"
    d = hc.project_workspace_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<h1>mine</h1>", encoding="utf-8")
    raw = _cookie_value(client.post("/api/auth/login",
                                    json={"email": "ph1-direct@fluxswarm.test",
                                          "password": "s3cure-Pass-123"}))
    # opening the preview from a fresh tab: only the HttpOnly cookie is present.
    r = client.get(f"/p/{slug}/", headers={"Cookie": f"fs_token={raw}"})
    assert r.status_code == 200, r.text
    assert "<h1>mine</h1>" in r.text
    # without the cookie (a complete stranger) it stays 403
    assert client.get(f"/p/{slug}/").status_code == 403


# ---------- invalid project -> controlled 404 ----------
def test_invalid_owned_project_is_404_not_500(_no_hermes, _host):
    a = _register("ph1-ghost@fluxswarm.test")
    uid = a["user"]["id"]
    ghost = f"u{uid}-never-created"
    for path in ("tasks", "workspace", "files", "security", "preview-ticket"):
        r = client.get(f"/api/projects/{ghost}/{path}", headers=_auth(a["token"]))
        assert r.status_code == 404, (path, r.status_code, r.text)
    r = client.post(f"/api/projects/{ghost}/dispatch", headers=_auth(a["token"]))
    assert r.status_code == 404, (r.status_code, r.text)
    # a guessed *other* user's slug stays 403 (never a data-leak signal)
    assert client.get("/api/projects/u999999-ph1/tasks",
                      headers=_auth(a["token"])).status_code == 403