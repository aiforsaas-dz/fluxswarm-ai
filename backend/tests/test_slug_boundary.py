"""Phase 5 — Slug path-param boundary validation (cross-tenant traversal).

Slug path params flow into filesystem path builders (kanban.db, workspaces,
attachments, export) and are echoed into page markup. The auth guards only
check PREFIXES (``u{uid}-`` / ``flux-demo-``), so a crafted slug that still
satisfies a legal prefix and carries a path separator could escape the board
root once joined into a path (``u5-..\\..\\u9-proj`` on the board-node or an
encoded dot-dot slug) or smuggle HTML into demo page markup
(``flux-demo-<script>``).

Starlette decodes the request path BEFORE matching routes, so a slug
containing ``/`` can never reach these handlers (the router 404s it). But
single-segment slugs that decodes into dots, backslashes or HTML reach the
handlers untouched — the auth prefix check passes and the slug flows into
``_board_db_path``/``project_workspace_dir`` and the demo page echo. Every
slug route now rejects non-``[A-Za-z0-9_-]{1,120}`` slugs (HTTP 400,
``invalid-slug`` frame on the WS) at the boundary, before any prefix check or
path construction, and the store functions are proven untouched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    calls = []

    def _rec(name, val):
        def fn(*a, **k):
            calls.append(name)
            return val
        return fn

    monkeypatch.setattr(main_mod.hc, "projects_are_thin", lambda: False)
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(
        main_mod.hc, "launch_swarm",
        lambda slug, goal, provider_keys=None:
            type("R", (), {"root_id": "r1", "worker_ids": ["w1"],
                           "verifier_id": "w2", "synthesizer_id": "w3"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.hc, "list_tasks", _rec("list_tasks", []))
    monkeypatch.setattr(main_mod.hc, "read_workspace", _rec("read_workspace", ""))
    monkeypatch.setattr(main_mod.hc, "list_project_files",
                        _rec("list_project_files", {"attachments": [], "workspace": []}))
    monkeypatch.setattr(main_mod.security, "scan_project",
                        lambda *a, **k: {"score": 0, "summary": "", "findings": []})
    return calls


@pytest.fixture()
def _host(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123",
                          "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(user: dict) -> str:
    r = client.post("/api/projects", headers=_auth(user["token"]),
                    json={"name": "Boundary", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def _owner_project_requests(slug: str, token: str):
    tok = _auth(token)
    cases = (
        ("GET", f"/api/projects/{slug}/workspace", None),
        ("GET", f"/api/projects/{slug}/files", None),
        ("GET", f"/api/projects/{slug}/security", None),
        ("GET", f"/api/projects/{slug}/preview-ticket", None),
        ("GET", f"/api/projects/{slug}/tasks", None),
        ("GET", f"/api/projects/{slug}/export", None),
        ("POST", f"/api/projects/{slug}/dispatch", None),
        ("POST", f"/api/projects/{slug}/reopen", None),
        ("DELETE", f"/api/projects/{slug}/attachments/spec.md", None),
    )
    for method, path, _json in cases:
        yield client.request(method, path, headers=tok)
    r = client.post(f"/api/projects/{slug}/attachments",
                    headers=tok, files={"file": ("x.md", b"# hi", "text/markdown")})
    yield r


def _ws_refusal(url: str):
    """Return the server's error-frame detail, or None on hard disconnect."""
    try:
        with client.websocket_connect(url) as ws:
            msg = ws.receive_json()
            return msg.get("detail") if isinstance(msg, dict) else None
    except (WebSocketDisconnect, RuntimeError):
        return None


# ---------- single-segment hostile slugs reach the boundary and are 400ed ----
def test_dot_slugs_rejected_on_every_owned_endpoint(_no_hermes, _host):
    calls = _no_hermes
    a = _register("ph5-dot@fluxswarm.test")
    slug = f"u{a['user']['id']}-.."
    for r in _owner_project_requests(slug, a["token"]):
        assert r.status_code == 400, (r.request.method, r.request.url,
                                      r.status_code, r.text)
    assert not calls, "hostile slugs must be rejected before any store call"


def test_backslash_slugs_rejected(_no_hermes, _host):
    calls = _no_hermes
    a = _register("ph5-bs@fluxswarm.test")
    slug = f"u{a['user']['id']}-%5C..%5C..%5Cu9-proj"
    for r in _owner_project_requests(slug, a["token"]):
        assert r.status_code == 400, (r.request.method, r.request.url,
                                      r.status_code, r.text)
    assert not calls, "backslash traversal must never reach the store"


def test_demo_dot_and_backslash_slugs_rejected(_no_hermes, _host):
    calls = _no_hermes
    for evil in ("flux-demo-..", "flux-demo-%5C..%5C"):
        r = client.get(f"/api/demo/progress/{evil}")
        assert r.status_code == 400, (evil, r.status_code, r.text)
        r = client.get(f"/api/demo/logs/{evil}/t1")
        assert r.status_code == 400, (evil, r.status_code, r.text)
    assert not calls


def test_preview_rejects_hostile_slugs(_no_hermes, _host):
    for evil in ("flux-demo-..", "flux-demo-%5Csecret", "flux-demo-%20"):
        assert client.get(f"/p/{evil}/").status_code == 400, evil
        assert client.get(f"/p/{evil}").status_code == 400, evil
        assert client.get(f"/p/{evil}/nested/index.html").status_code == 400, evil


# ---------- slash-carrying slugs: the router already 404s them; the store must
#            stay untouched and no board data may leak ----------
def test_slash_traversal_never_leaks(_no_hermes, _host):
    calls = _no_hermes
    a = _register("ph5-slash@fluxswarm.test")
    evil = f"u{a['user']['id']}-..%2F..%2Fother-board"
    r = client.get(f"/api/projects/{evil}/tasks", headers=_auth(a["token"]))
    assert r.status_code in (400, 404), r.text
    r = client.get(f"/api/demo/progress/{TRAVERSAL}secret")
    assert r.status_code in (400, 404), r.text
    assert not calls, "slash-traversal must never reach any store function"


TRAVERSAL = "..%2F..%2F"


# ---------- HTML injection into slug echoes is blocked ----------
def test_html_injection_slugs_rejected(_no_hermes, _host):
    for evil in ("flux-demo-%3Cimg%3E", "flux-demo-%22x%22",
                 "flux-demo-%27onload%27"):
        r = client.get(f"/api/demo/progress/{evil}")
        assert r.status_code == 400, (evil, r.status_code, r.text)
        r = client.get(f"/api/demo/logs/{evil}/t1")
        assert r.status_code == 400, (evil, r.status_code, r.text)


# ---------- WS: hostile slugs get an invalid-slug frame (or refuse to
#            connect for slash-carrying ones) ----------
def test_ws_hostile_slugs_rejected(_no_hermes, _host):
    a = _register("ph5-ws@fluxswarm.test")
    for evil in (f"u{a['user']['id']}-..", f"u{a['user']['id']}-%5C..%5C"):
        assert _ws_refusal(f"/ws/{evil}?token={a['token']}") == "invalid-slug"
    assert _ws_refusal(f"/ws/flux-demo-..?token={a['token']}") == "invalid-slug"
    # a slug that decodes to slash segments can never connect at all
    assert _ws_refusal(
        f"/ws/u{a['user']['id']}-{TRAVERSAL}x?token={a['token']}") is None


# ---------- legitimate slugs keep working end to end ----------
def test_legitimate_slugs_unaffected(_no_hermes, _host):
    a = _register("ph5-ok@fluxswarm.test")
    slug = _mk_project(a)
    tok = _auth(a["token"])
    for path in ("tasks", "workspace", "files", "security"):
        r = client.get(f"/api/projects/{slug}/{path}", headers=tok)
        assert r.status_code == 200, (path, r.status_code, r.text)
    assert client.get(f"/api/projects/{slug}/preview-ticket",
                      headers=tok).status_code == 200
    r = client.get("/api/demo/progress/flux-demo-1")
    assert r.status_code == 200, r.text
    r = client.get("/api/demo/logs/flux-demo-1/t1")
    assert r.status_code == 200, r.text