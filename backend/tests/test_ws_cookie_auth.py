"""Phase 6 — WebSocket cookie-first auth (query-token leak mitigation).

Before this phase /ws/{slug} authenticated exclusively with the Bearer token
placed in ``?token=``, which leaks through access/proxy logs, the Referer
header and browser history. This phase makes the HttpOnly ``fs_token`` session
cookie the PRIMARY credential (the same-origin handshake carries it
automatically, so browsers connect with no auth string in the URL at all):

  * cookie present  -> it alone is used and MUST validate (revocation included);
    a stale/foreign ``?token=`` in the URL is ignored, so a leaked token cannot
    redirect someone else's cookie-held session, and an invalid cookie cannot
    be side-stepped with a valid query token;
  * cookie absent   -> the ``?token=`` param is honoured ONLY for cookie-less
    (programmatic/non-browser) clients, through the identical validation path.

Ordering is fail-closed throughout: no credential -> refused, malformed ->
refused, deleted user -> refused, revoked session -> refused, unowned slug ->
refused.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import db
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


@pytest.fixture(autouse=True)
def _ws_isolated(monkeypatch):
    monkeypatch.setattr(main_mod, "_SUBS", {})
    monkeypatch.setattr(main_mod, "_SUBS_USER", {})
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [])
    yield
    main_mod._SUBS.clear()
    main_mod._SUBS_USER.clear()


@pytest.fixture()
def _board(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))

    def _mk(slug: str) -> str:
        d = main_mod.hc.project_workspace_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        return slug

    return _mk


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123",
                          "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _ws_detail(url: str, cookie_token: str | None = None,
               c: TestClient = client):
    """Connect and return the first frame's detail (or None on hard error)."""
    headers: dict = {}
    if cookie_token:
        headers = {"Cookie": f"fs_token={cookie_token}"}
    try:
        with c.websocket_connect(url, headers=headers) as ws:
            msg = ws.receive_json()
            return msg.get("detail") if isinstance(msg, dict) else msg
    except (WebSocketDisconnect, RuntimeError):
        return None


def _ws_connect(url: str, cookie_token: str | None = None,
                c: TestClient = client):
    """Connect and return the live session (caller must close it)."""
    headers: dict = {}
    if cookie_token:
        headers = {"Cookie": f"fs_token={cookie_token}"}
    return c.websocket_connect(url, headers=headers)


# ---------- cookie is the primary credential ----------
def test_ws_cookie_only_connects_to_owned_board(_board):
    a = _register("ph6-cookie@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    with _ws_connect(f"/ws/{slug}", cookie_token=a["token"]) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_cookie_only_connects_to_demo_board(_board):
    a = _register("ph6-cookie-demo@fluxswarm.test")
    with _ws_connect("/ws/flux-demo-1", cookie_token=a["token"]) as ws:
        assert ws.receive_json()["type"] == "snapshot"


# ---------- query token persists as a cookie-less fallback ----------
def test_ws_query_token_fallback_without_cookie(_board):
    a = _register("ph6-fallback@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    anon = TestClient(main_mod.app)  # fresh jar: no cookies at all
    with _ws_connect(f"/ws/{slug}?token={a['token']}", c=anon) as ws:
        assert ws.receive_json()["type"] == "snapshot"


# ---------- cookie wins over a foreign/leaked URL token ----------
def test_ws_cookie_wins_over_foreign_query_token(_board):
    a = _register("ph6-win-a@fluxswarm.test")
    b = _register("ph6-win-b@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    # A's cookie in the handshake + B's token in the URL: pre-change this was
    # "forbidden" (B does not own the board); post-change the cookie is
    # authoritative, so the session is A's and the snapshot is served.
    with _ws_connect(f"/ws/{slug}?token={b['token']}",
                     cookie_token=a["token"]) as ws:
        assert ws.receive_json()["type"] == "snapshot"


# ---------- an invalid cookie is NOT side-stepped by a valid query token -----
def test_ws_invalid_cookie_not_bypassed_by_query_token(_board):
    a = _register("ph6-no-bypass@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    detail = _ws_detail(f"/ws/{slug}?token={a['token']}",
                        cookie_token="garbage.not-a-token")
    assert detail == "unauthorized"


# ---------- cookie sessions honour revocation (logged_out_at) ----------
def test_ws_cookie_session_invalidated_on_logout(_board):
    a = _register("ph6-revoke@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    url = f"/ws/{slug}"
    with _ws_connect(url, cookie_token=a["token"]) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    db.mark_logged_out(a["user"]["id"])
    assert _ws_detail(url, cookie_token=a["token"]) == "unauthorized"


# ---------- no cookie + no token -> fail closed ----------
def test_ws_no_credential_refused(_board):
    a = _register("ph6-none@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    anon = TestClient(main_mod.app)  # no cookie jar at all
    assert _ws_detail(f"/ws/{slug}", c=anon) == "unauthorized"
    assert _ws_detail(f"/ws/{slug}?token=", c=anon) == "unauthorized"