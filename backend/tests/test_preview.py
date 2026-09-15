"""Live project preview (/p/<slug>/...) tests.

The preview renders a board's generated workspace as a browsable live site
(index.html when present, else a directory listing) under /p/. Covers:
  * demo (flux-demo-*) boards are public — no auth needed;
  * owned (u<uid>-*) boards require the owner's preview ticket cookie (or their
    normal Bearer session) and assets served under /p carry the cookie too;
  * cross-tenant preview access is refused (403);
  * path traversal can never escape the workspace root (../ and encoded forms);
  * preview responses are iframe-embeddable (X-Frame-Options: SAMEORIGIN, no
    frame-ancestors directive — opaque sandboxed frames can never match one),
    while every other page keeps DENY + nonce CSP.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hermes_client as hc
import main as main_mod

client = TestClient(main_mod.app)
client2 = TestClient(main_mod.app)


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
def _host(monkeypatch, tmp_path):
    """Point HERMES_HOME at a temp dir so workspaces are fully isolated."""
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _ws(host, slug, *parts) -> object:
    root = hc.project_workspace_dir(slug)
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_demo_index_is_public_and_embeddable(_host):
    slug = "flux-demo-pub"
    _ws(_host, slug, "index.html").write_text(
        "<!doctype html><script>window.__ok=1</script><h1>Hi</h1>", encoding="utf-8")
    r = client.get(f"/p/{slug}/")
    assert r.status_code == 200, r.text
    assert "<h1>Hi</h1>" in r.text
    assert "text/html" in r.headers["content-type"]
    # embeddable by the dashboard iframe: relaxed CSP for generated apps.
    # The preview frame is sandboxed WITHOUT allow-same-origin, so the framed
    # document gets an opaque origin: Firefox refuses a X-Frame-Options
    # SAMEORIGIN check against that unique origin (shows up as a browser
    # "connection not authorized" page), so the embed side is governed by CSP
    # frame-ancestors 'self' instead and NO X-Frame-Options header is sent.
    assert "x-frame-options" not in r.headers
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" in csp
    assert "frame-ancestors 'self'" in csp


def test_preview_no_slash_redirects(_host):
    slug = "flux-demo-redir"
    _ws(_host, slug, "a.txt").write_text("x", encoding="utf-8")
    r = client.get(f"/p/{slug}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith(f"/p/{slug}/")


def test_demo_listing_without_index(_host):
    slug = "flux-demo-listing"
    _ws(_host, slug, "README.md").write_text("# Plan", encoding="utf-8")
    _ws(_host, slug, "styles", "app.css").write_text("body{}", encoding="utf-8")
    _ws(_host, slug, "tests", "test_app.py").write_text("def t(): pass", encoding="utf-8")
    r = client.get(f"/p/{slug}/")
    assert r.status_code == 200
    assert "README.md" in r.text
    assert "styles" in r.text
    assert "tests" in r.text
    # nested asset served with a text mimetype
    r2 = client.get(f"/p/{slug}/styles/app.css")
    assert r2.status_code == 200, r2.text
    assert r2.text == "body{}"
    assert "text/css" in r2.headers["content-type"]
    # directory listing works at intermediate levels too
    r3 = client.get(f"/p/{slug}/styles/")
    assert r3.status_code == 200
    assert "app.css" in r3.text


def test_missing_file_returns_404(_host):
    slug = "flux-demo-missing"
    _ws(_host, slug, "real.txt").write_text("x", encoding="utf-8")
    r = client.get(f"/p/{slug}/nope.txt")
    assert r.status_code == 404


def test_path_traversal_cannot_escape_workspace(_host):
    slug = "flux-demo-safe"
    _ws(_host, slug, "ok.txt").write_text("fine", encoding="utf-8")
    # a real secret just outside the workspace, inside HERMES_HOME
    secret = hc.project_workspace_dir(slug).parent.parent / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    for path in ("..%2f..%2fsecret.txt", "../..%2fsecret.txt",
                 "subdir/../../secret.txt", ".%2e/.../secret.txt"):
        r = client.get(f"/p/{slug}/{path}")
        # literal dot-segments may be normalized by the client/route (403); the
        # requirement is that the escape NEVER yields 200 or the secret.
        assert r.status_code in (403, 404), f"{path} -> {r.status_code} {r.text}"
        assert "TOP-SECRET" not in r.text
    # absolute-escaping attempts are likewise refused
    r = client.get(f"/p/{slug}/..%2f..%2fkanban.db")
    assert r.status_code in (403, 404)
    assert "TOP-SECRET" not in r.text


def _register(email: str, client_to_use=client):
    r = client_to_use.post("/api/auth/register",
                           json={"email": email, "name": "V",
                                 "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_private_board_requires_ticket(_host):
    user = _register("preview-own@fluxswarm.test")
    uid = user["user"]["id"]
    slug = f"u{uid}-pv"
    _ws(_host, slug, "index.html").write_text("<h1>mine</h1>", encoding="utf-8")
    # without the ticket -> 403
    assert client2.get(f"/p/{slug}/").status_code == 403
    # owner Bearer session works directly (like the workspace API)
    r = client.get(f"/p/{slug}/",
                   headers={"Authorization": f"Bearer {user['token']}"})
    assert r.status_code == 200 and "<h1>mine</h1>" in r.text
    # ticket endpoint mints a /p cookie
    rt = client.get(f"/api/projects/{slug}/preview-ticket",
                    headers={"Authorization": f"Bearer {user['token']}"})
    assert rt.status_code == 200, rt.text
    # frame loads with the cookie attached (index + relative assets)
    r2 = client.get(f"/p/{slug}/")
    assert r2.status_code == 200, r2.text


def test_cross_tenant_cannot_mint_or_access_ticket(_host):
    a = _register("preview-own-a@fluxswarm.test")
    b = _register("preview-own-b@fluxswarm.test")
    slug_a = f"u{a['user']['id']}-cross"
    _ws(_host, slug_a, "index.html").write_text("<h1>A</h1>", encoding="utf-8")
    # B cannot mint a ticket for A's board
    r = client.get(f"/api/projects/{slug_a}/preview-ticket",
                   headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403
    # B cannot read A's preview even with B's own session
    r2 = client.get(f"/p/{slug_a}/",
                    headers={"Authorization": f"Bearer {b['token']}"})
    assert r2.status_code == 403


def test_main_pages_keep_strict_framing_and_nonce_csp(_host):
    """Regression: only /p/* responses are relaxed; everything else retains the
    strict DENY + nonce-only CSP that protects the dashboard itself."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "nonce-" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp
    # the dashboard may embed its own preview pages (same-origin /p/*) in
    # addition to the Paddle checkout iframes
    assert "frame-src 'self'" in csp


def test_flag_set_only_on_preview_routes(_host):
    slug = "flux-demo-flag"
    _ws(_host, slug, "index.html").write_text("<h1>f</h1>", encoding="utf-8")
    r = client.get(f"/p/{slug}/")
    # preview responses never send X-Frame-Options (see test_demo_index_…)
    assert "x-frame-options" not in r.headers
    # the ticket endpoint itself is a regular API response (strict headers)
    user = _register("preview-ticket-hdr@fluxswarm.test")
    slug2 = f"u{user['user']['id']}-hdr"
    _ws(_host, slug2, "index.html").write_text("<h1>h</h1>", encoding="utf-8")
    rt = client.get(f"/api/projects/{slug2}/preview-ticket",
                    headers={"Authorization": f"Bearer {user['token']}"})
    assert rt.status_code == 200
    assert rt.headers.get("x-frame-options") == "DENY"