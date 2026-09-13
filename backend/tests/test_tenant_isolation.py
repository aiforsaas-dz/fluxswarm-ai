"""Gate 2 — tenant isolation across two independent accounts.

Two accounts (A, B): each owns its own boards/keys/projects. Cross-tenant
reads, dispatches, security scans and WebSockets must be refused (403/401),
and a deleted user's token must stop working immediately.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    """Launch path must never touch the real Hermes CLI in these tests."""
    monkeypatch.setattr(main_mod.hc, "ensure_board", lambda s: None)
    monkeypatch.setattr(
        main_mod.hc, "launch_swarm",
        lambda slug, goal, provider_keys=None:
            type("R", (), {"root_id": "r", "worker_ids": [], "verifier_id": "v",
                           "synthesizer_id": "s"})(),
    )
    monkeypatch.setattr(main_mod, "_fire_dispatch", lambda *a, **k: None)


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_cross_account_task_and_dispatch_denied(_no_hermes):
    a = _register("tenant-a@fluxswarm.test")
    b = _register("tenant-b@fluxswarm.test")
    aid = a["user"]["id"]
    a_slug = f"u{aid}-static-proj"

    # A creates a project (slug prefix owned by A).
    r = client.post("/api/projects",
                    headers={"Authorization": f"Bearer {a['token']}"},
                    json={"name": "A", "goal": "Build A"})
    assert r.status_code == 200, r.text
    a_slug = r.json()["slug"]

    # B tries to read A's tasks -> 403 (prefix guard).
    r = client.get(f"/api/projects/{a_slug}/tasks",
                   headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403, r.text

    # B tries to dispatch A's board -> 403.
    r = client.post(f"/api/projects/{a_slug}/dispatch",
                    headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403, r.text

    # A on a nonexistent (guessed) board slug for B also 403, not 404/data leak.
    r = client.get("/api/projects/u999999-guess/tasks",
                   headers={"Authorization": f"Bearer {a['token']}"})
    assert r.status_code == 403, r.text


def test_cross_account_security_scan_denied():
    a = _register("tenant-sec-a@fluxswarm.test")
    b = _register("tenant-sec-b@fluxswarm.test")
    a_slug = f"u{a['user']['id']}-secproj"
    db_mod.add_project(a["user"]["id"], a_slug, "A", "goal")

    r = client.get(f"/api/projects/{a_slug}/security",
                   headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403, r.text


def test_keys_never_visible_across_tenants():
    a = _register("tenant-key-a@fluxswarm.test")
    b = _register("tenant-key-b@fluxswarm.test")

    r = client.post("/api/keys",
                    headers={"Authorization": f"Bearer {a['token']}"},
                    json={"provider": "anthropic", "token": "sk-ant-tr33leaksecret123"})
    assert r.status_code == 200, r.text

    # B's key list is empty — A's key must never appear.
    rb = client.get("/api/keys", headers={"Authorization": f"Bearer {b['token']}"})
    assert rb.status_code == 200
    assert rb.json()["keys"] == {}
    assert rb.json()["byok_active"] is False

    # A only ever sees a masked form; the raw token is never returned.
    ra = client.get("/api/keys", headers={"Authorization": f"Bearer {a['token']}"})
    assert ra.status_code == 200
    assert "sk-ant-tr33leaksecret123" not in ra.text
    assert "…" in ra.text


def _ws_rejected(url: str) -> bool:
    """True only when the server refuses the socket (error detail message or
    hard disconnect). TestClient delivery of the pre-close message can vary by
    version, so both paths count as fail-closed."""
    try:
        with client.websocket_connect(url) as ws:
            msg = ws.receive_json()
            return isinstance(msg, dict) and msg.get("type") == "error"
    except WebSocketDisconnect:
        return True
    except RuntimeError:
        return True


def test_websocket_cross_tenant_forbidden(monkeypatch):
    a = _register("tenant-ws-a@fluxswarm.test")
    b = _register("tenant-ws-b@fluxswarm.test")
    a_slug = f"u{a['user']['id']}-proj"
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [])

    # B's token on A's board must be refused (fail closed, never anonymous).
    assert _ws_rejected(f"/ws/{a_slug}?token={b['token']}") is True
    # Matching user on their own board is accepted.
    with client.websocket_connect(f"/ws/{a_slug}?token={a['token']}") as ws:
        pass


def test_deleted_user_sessions_and_ws_die():
    a = _register("tenant-del@fluxswarm.test")
    token = a["token"]
    a_slug = f"u{a['user']['id']}-proj"

    r = client.delete("/api/account", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is True

    # HTTP: gone.
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    # WS: token maps to no user -> rejected.
    assert _ws_rejected(f"/ws/{a_slug}?token={token}") is True


def test_websocket_socket_logged_out_rejected():
    a = _register("tenant-ws-logout@fluxswarm.test")
    a_slug = f"u{a['user']['id']}-proj"
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {a['token']}"})
    assert _ws_rejected(f"/ws/{a_slug}?token={a['token']}") is True


def test_websocket_per_slug_subscriber_cap(monkeypatch):
    a = _register("tenant-ws-cap@fluxswarm.test")
    a_slug = f"u{a['user']['id']}-proj"
    monkeypatch.setattr(main_mod.hc, "list_tasks", lambda s: [])
    monkeypatch.setattr(main_mod, "_WS_MAX_SUBS_PER_SLUG", 2)

    url = f"/ws/{a_slug}?token={a['token']}"
    with client.websocket_connect(url) as ws1:
        with client.websocket_connect(url) as ws2:
            # A third viewer on the same board must be refused.
            assert _ws_rejected(url) is True