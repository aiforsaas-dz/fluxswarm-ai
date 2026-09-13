"""Server-side paid-export gating tests for GET /api/projects/{slug}/export.

Export endpoint must:
  - return a ZIP of all workspace files (path-containment enforced)
  - reject non-owner slugs
  - enforce per-user hourly burst cap (demo: 5, paid: 60)
  - audit every outcome
  - guard against symlink escapes and path traversal
"""
from __future__ import annotations

import io
import secrets
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import db as db_mod
import hermes_client as hc
import main as main_mod
from main import _EXPORT_DEMO_HOURLY_CAP, _EXPORT_PAID_HOURLY_CAP

client = TestClient(main_mod.app)


# ── helpers ────────────────────────────────────────────────────────────────

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
def _fresh_export_state(monkeypatch):
    monkeypatch.setattr(main_mod, "_export_hits", {})
    yield


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(suffix: str = "export") -> tuple[str, int]:
    email = f"{suffix}-{secrets.token_hex(4)}@fluxswarm.test"
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "E",
                          "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], body["user"]["id"]


def _seed_workspace(tmp_path, slug: str, files: dict[str, str]) -> None:
    ws = tmp_path / "kanban" / "boards" / slug / "workspaces"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _zip_names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return set(z.namelist())


def _set_plan(uid: int, plan: str, credits: int = 200):
    db_mod.upgrade_plan(uid, plan)
    db_mod.set_credits(uid, credits) if hasattr(db_mod, "set_credits") else None


# ── authorization ──────────────────────────────────────────────────────────

def test_owner_export_returns_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("own")
    slug = f"u{uid}-t-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"app.py": "print(1)\n", "README.md": "# Hi\n"})
    r = client.get(f"/api/projects/{slug}/export", headers=_auth(token))
    assert r.status_code == 200
    names = _zip_names(r.content)
    assert "app.py" in names
    assert "README.md" in names
    assert r.headers["content-type"] == "application/zip"


def test_slash_through_slug_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, _ = _register("trv")
    for bad in ["../outside", "%2f..%2fetc", "flux-demo-x"]:
        r = client.get(f"/api/projects/{bad}/export", headers=_auth(token))
        assert r.status_code in (403, 404), (bad, r.status_code, r.text)


def test_missing_workspace_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("emptyws")
    slug = f"u{uid}-emptyws-{int(time.time())}"
    r = client.get(f"/api/projects/{slug}/export", headers=_auth(token))
    assert r.status_code == 404


def test_unauthenticated_export_is_401(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    slug = f"u1-noauth-{int(time.time())}"
    r = client.get(f"/api/projects/{slug}/export")
    assert r.status_code in (401, 403)


def test_cross_owner_export_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token_a, uid_a = _register("ownerA")
    token_b, _ = _register("ownerB")
    slug = f"u{uid_a}-priv-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"secret.txt": "do-not-leak\n"})
    r = client.get(f"/api/projects/{slug}/export", headers=_auth(token_b))
    assert r.status_code == 403


def test_export_contains_only_workspace_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("safezip")
    slug = f"u{uid}-safezip-{int(time.time())}"
    ws = tmp_path / "kanban" / "boards" / slug / "workspaces"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a.txt").write_text("real\n", encoding="utf-8")
    # a file outside the workspace must never leak into the ZIP
    (tmp_path / "secret_outside.txt").write_text("secret\n", encoding="utf-8")
    r = client.get(f"/api/projects/{slug}/export", headers=_auth(token))
    assert r.status_code == 200
    names = _zip_names(r.content)
    assert "a.txt" in names
    assert "secret_outside.txt" not in names


# ── burst gating ───────────────────────────────────────────────────────────

def _drive_exports(token: str, slug: str, n: int) -> int:
    ok = 0
    statuses = []
    for _ in range(n):
        r = client.get(f"/api/projects/{slug}/export", headers=_auth(token))
        statuses.append(r.status_code)
        if r.status_code == 200:
            ok += 1
    return ok, statuses


def test_demo_burst_limit_enforced(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("burstdemo")
    slug = f"u{uid}-burstdemo-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"x.py": "y\n"})
    ok, statuses = _drive_exports(token, slug, _EXPORT_DEMO_HOURLY_CAP + 2)
    assert ok == _EXPORT_DEMO_HOURLY_CAP
    assert statuses.count(429) == 2


def test_paid_plan_untouched_by_demo_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("burstpaid")
    _set_plan(uid, "starter")
    slug = f"u{uid}-burstpaid-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"api.py": "from flask import Flask\n"})
    # demo cap is 5; paid cap is 60 — 8 exports must all succeed
    ok, statuses = _drive_exports(token, slug, 8)
    assert ok == 8
    assert 429 not in statuses


def test_paid_burst_eventually_hits_its_own_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(main_mod, "_EXPORT_PAID_HOURLY_CAP", 3)
    token, uid = _register("burstpaid2")
    _set_plan(uid, "starter")
    slug = f"u{uid}-burstpaid2-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"s.py": "x=1\n"})
    ok, statuses = _drive_exports(token, slug, 5)
    assert ok == 3
    assert statuses.count(429) == 2


# ── audit ──────────────────────────────────────────────────────────────────

def _project_export_lines():
    import audit as audit_mod
    with open(audit_mod.AUDIT_FILE, encoding="utf-8") as f:
        return [l for l in f if "project.export" in l]


def test_export_records_audit_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("aud")
    slug = f"u{uid}-aud-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"main.py": "pass\n"})
    client.get(f"/api/projects/{slug}/export", headers=_auth(token))
    lines = _project_export_lines()
    assert len(lines) >= 1
    assert '"outcome": "ok"' in lines[-1]


def test_empty_workspace_records_audit_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token, uid = _register("audemp")
    slug = f"u{uid}-audemp-{int(time.time())}"
    client.get(f"/api/projects/{slug}/export", headers=_auth(token))
    lines = _project_export_lines()
    assert len(lines) >= 1
    assert '"reason": "empty_workspace"' in lines[-1]


def test_unauthorized_records_audit_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    token_a, uid_a = _register("audoa")
    token_b, _ = _register("audob")
    slug = f"u{uid_a}-audpriv-{int(time.time())}"
    _seed_workspace(tmp_path, slug, {"p.txt": "x\n"})
    client.get(f"/api/projects/{slug}/export", headers=_auth(token_b))
    lines = [l for l in _project_export_lines() if "unauthorized" in l]
    assert len(lines) >= 1