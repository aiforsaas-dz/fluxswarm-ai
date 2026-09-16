"""Phase 3 — WebSocket surface hardening (POST-authz live surface).

Before this phase the /ws/{slug} socket was fail-closed on identity/ownership
but had three reuse/availability holes:

  * an OWNED but never-created (ghost) board was accepted and then polled
    every 4s forever (wasted subprocess + slot hog);
  * only a per-SLUG cap existed — one authenticated user could fill every
    demo-board slot (64), starving other viewers and spawning a CLI poll per
    slot while their sockets were all the SAME account;
  * a client that died mid-send leaked its socket inside _SUBS forever
    (the set was only cleaned on clean WebSocketDisconnect).

Accepted behaviour:
  * owned-but-missing board  -> fail-fast "not-found" close, no poll at all;
  * per-(user, slug) sub cap in addition to the per-slug cap (default 2);
  * ANY exit path releases both per-slug and per-user slots (verify by
    reconnecting after a forced close).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    """Keep the socket registries isolated and the CLI poll hermetic."""
    monkeypatch.setattr(main_mod, "_SUBS", {})
    monkeypatch.setattr(main_mod, "_SUBS_USER", {})
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [])
    yield
    # tidy-up in case a test left a registry behind
    main_mod._SUBS.clear()
    main_mod._SUBS_USER.clear()


@pytest.fixture()
def _board(monkeypatch, tmp_path):
    """A real owned workspace so the ghost-board gate passes (Phase 3)."""
    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))

    def _mk(slug: str) -> str:
        d = main_mod.hc.project_workspace_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        return slug

    return _mk


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _ws_reject_detail(url: str):
    """Return the server's close detail, or None when it hard-disconnects."""
    try:
        with client.websocket_connect(url) as ws:
            msg = ws.receive_json()
            return msg.get("detail") if isinstance(msg, dict) else None
    except (WebSocketDisconnect, RuntimeError):
        return None


def _ws_snapshot(url: str):
    """Connect, expect the snapshot frame, return the frame."""
    with client.websocket_connect(url) as ws:
        return ws.receive_json()


# ---------- ghost board fails fast ----------
def test_ws_ghost_owned_board_rejected_without_polling(monkeypatch):
    a = _register("ws-ghost@fluxswarm.test")
    slug = f"u{a['user']['id']}-never-created"
    calls = []
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [])
    assert _ws_reject_detail(f"/ws/{slug}?token={a['token']}") == "not-found"
    # Other boards' sockets may still be polling from earlier tests in this
    # process; only THIS missing board must never be polled.
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: calls.append(s) or [])
    mono = _ws_reject_detail(f"/ws/{slug}?token={a['token']}")
    assert mono == "not-found"
    assert [c for c in calls if c == slug] == [], "a missing board must never be polled"


# ---------- per-(user, slug) cap ----------
def test_ws_per_user_slug_cap(_board, monkeypatch):
    a = _register("ws-usercap@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_USER_SLUG", 1)
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_SLUG", 64)
    url = f"/ws/{slug}?token={a['token']}"
    with client.websocket_connect(url) as ws1:
        assert ws1.receive_json()["type"] == "snapshot"
        # second live socket from the SAME account on the same board is refused
        assert _ws_reject_detail(url) == "too many live connections from this account on this board"


def test_ws_per_slug_cap_still_honoured_across_users(monkeypatch):
    a = _register("ws-slugcap-a@fluxswarm.test")
    b = _register("ws-slugcap-b@fluxswarm.test")
    slug = "flux-demo-ws"
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_SLUG", 2)
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_USER_SLUG", 64)
    url_a = f"/ws/{slug}?token={a['token']}"
    url_b = f"/ws/{slug}?token={b['token']}"
    with client.websocket_connect(url_a) as ws1, client.websocket_connect(url_b) as ws2:
        assert ws1.receive_json()["type"] == "snapshot"
        assert ws2.receive_json()["type"] == "snapshot"
        # a third viewer on the SAME board (a different account) stays refused
        c = _register("ws-slugcap-c@fluxswarm.test")
        assert _ws_reject_detail(f"/ws/{slug}?token={c['token']}") == "too many concurrent viewers on this board"


# ---------- no registry leak: slots release on ANY exit ----------
def test_ws_slots_released_after_disconnect(_board, monkeypatch):
    a = _register("ws-release@fluxswarm.test")
    slug = _board(f"u{a['user']['id']}-proj")
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_USER_SLUG", 2)
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_SLUG", 64)
    url = f"/ws/{slug}?token={a['token']}"
    # fill both per-user slots simultaneously, then drop both sockets
    with client.websocket_connect(url) as ws2:
        assert ws2.receive_json()["type"] == "snapshot"
        with client.websocket_connect(url) as ws3:
            assert ws3.receive_json()["type"] == "snapshot"
            assert len(main_mod._SUBS_USER[(a["user"]["id"], slug)]) == 2
    # the server only notices the drop on its next poll tick (<=4s): wait it out
    import time
    deadline = time.time() + 6
    while (len(main_mod._SUBS.get(slug, set()))
           or len(main_mod._SUBS_USER.get((a["user"]["id"], slug), set()))) \
            and time.time() < deadline:
        time.sleep(0.2)
    assert len(main_mod._SUBS.get(slug, set())) == 0, "per-slug slot leaked"
    assert len(main_mod._SUBS_USER.get((a["user"]["id"], slug), set())) == 0, "per-user slot leaked"
    # the freed slots are reusable: a new socket is accepted right away
    with client.websocket_connect(url) as ws4:
        assert ws4.receive_json()["type"] == "snapshot"