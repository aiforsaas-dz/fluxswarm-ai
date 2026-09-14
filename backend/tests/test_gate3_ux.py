"""Gate 3 — product, UX and US/UK-market readiness evidence.

Covers (a) every public page a US/UK customer reaches returns 200 in English
without mojibake; (b) the landing defaults to English and carries no marketing
claims the product cannot honour; (c) legal pages keep the operator entity and
mandatory consumer rights; (d) /pricing and /faq are accurate to db.PLANS and to
the real product (BYOK + operator-configured default runtime, Hermes + ECC
attribution, 25-credit referrals, 50% template author share, workspace browsing);
(e) the workspace endpoint applies board ownership rules; (f) the full customer
journey works over HTTP against a stubbed Hermes runtime.

Nothing here invokes a real Hermes/agent subprocess or the real limiter (same
permissive-limiter convention used by every auth-bearing test module).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import db as db_mod
import main as main_mod

client = TestClient(main_mod.app)

PUBLIC_PAGES = [
    "/", "/pricing", "/how-it-works", "/faq",
    "/privacy", "/privacy-en", "/terms", "/terms-en",
    "/refund", "/refund-en", "/cookies", "/cookies-en",
    "/acceptable-use", "/acceptable-use-en",
    "/robots.txt", "/sitemap.xml",
]

# Claims the previous copy made that the product cannot honestly honour.
# Note: "openrouter" used to be banned as a false promise of a free provider;
# it is now the deliberately supported free-tier BYOK runtime (Qwen3 Coder 480B),
# so marketing mentioning it is factual — and the marketing copy never promises
# unlimited/free tokens or an anonymous tier (that still requires a key).
BANNED_CLAIMS = [
    "unlimited", "/month", "hourly", "infallible", "guaranteed",
    "enterprise-grade", "enterprise grade", "100% accurate",
    "zero data retention", "fully compliant", "best-in-class",
]

MARKETING_PAGES = ["/", "/pricing", "/how-it-works", "/faq"]


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


def _register(email: str, name: str = "UX Tester"):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": name, "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    return r.json()


def _login(email: str, password: str = "s3cure-Pass-123") -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- page availability / language ----------
def test_all_public_pages_serve():
    for path in PUBLIC_PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "\ufffd" not in r.text, f"mojibake in {path}"


def test_landing_english_default():
    r = client.get("/")
    assert r.status_code == 200
    assert '<html lang="en" dir="ltr">' in r.text          # EN by default
    assert 'dir="rtl"' not in r.text                         # not wired RTL
    assert "1 credit per launch" in r.text                  # accurate meta copy
    assert "unlimited" not in r.text.lower()


def test_claims_scan_no_hype():
    for path in MARKETING_PAGES:
        r = client.get(path)
        assert r.status_code == 200
        low = r.text.lower()
        for claim in BANNED_CLAIMS:
            assert claim not in low, f"banned claim {claim!r} on {path}"


# ---------- accuracy to product model ----------
def test_pricing_page_accurate_to_plans():
    r = client.get("/pricing")
    assert r.status_code == 200
    for pid in db_mod.PLAN_ORDER:
        assert db_mod.PLANS[pid]["name"] in r.text
        assert f"${db_mod.PLANS[pid]['price']}" in r.text
        assert f"{db_mod.PLANS[pid]['credits']} credits" in r.text
    assert "credit pack" in r.text           # NOT a subscription
    assert "1 credit per launch" in r.text
    assert "no token meters" in r.text
    assert "USD" in r.text
    assert "GBP" in r.text
    assert "/month" not in r.text.lower()


def test_api_plans_match_db():
    plans = client.get("/api/plans").json()
    assert [p["id"] for p in plans] == db_mod.PLAN_ORDER
    for p in plans:
        assert p["credits"] == db_mod.PLANS[p["id"]]["credits"]
        assert p["price"] == db_mod.PLANS[p["id"]]["price"]
    assert db_mod.REFERRAL_REWARD_CREDITS == 15
    assert db_mod.REFERRAL_REWARD_CAP == 500
    assert db_mod.FRIEND_BONUS_CREDITS == 10


def test_faq_believes_product_claims():
    r = client.get("/faq")
    assert r.status_code == 200
    # default is the operator-configured runtime — a key is optional, never free-silent
    assert "Does the squad need my AI key?" in r.text
    assert "operator-configured model" in r.text
    # honest about the credit/plan model (collapse HTML line-wraps before matching)
    page = " ".join(r.text.split())
    assert "One-time credit packs, not subscriptions" in page
    assert "$19/20 credits, Pro $49/60 credits, Scale $149/200 credits, plus a $9/10 credit Top-up refill" in page
    # attribution + provider list + security facts
    assert "Hermes" in r.text and "ECC" in r.text
    assert "does not own ECC" in r.text
    assert "Anthropic Claude" in r.text and "OpenAI" in r.text
    assert "Gemini" in r.text and "Kimi" in r.text
    assert "Fernet-encrypted" in r.text
    # referrals + template economics
    assert "15 credits" in r.text and "10 bonus credits" in r.text and "500" in r.text
    assert "50% author" in r.text
    # no stale BYOK-first framing
    assert "need a key" not in r.text.lower()
    assert "OpenRouter" not in r.text


def test_how_it_works_honest_about_scope():
    r = client.get("/how-it-works")
    assert r.status_code == 200
    for agent in ("Planner", "Architect", "DevOps", "TDD", "Reviewer",
                  "Designer", "Builder", "Auditor"):
        assert agent in r.text
    assert "8-agent squad" in r.text
    assert "browsable in the UI" in r.text          # workspace reader exists now
    assert "Hermes" in r.text and "ECC" in r.text
    assert "is not Hermes" in r.text and "does not own ECC" in r.text
    assert "bring a key or use the deployment default" in r.text
    assert "no monthly fee" in r.text.lower()


def test_legal_pages_cover_consumer_rights_and_entity():
    for path in ("/privacy-en", "/terms-en"):
        r = client.get(path)
        assert r.status_code == 200
        assert "CCPA" in r.text or "consumer rights" in r.text
    terms = client.get("/terms-en").text
    assert "consumer rights" in terms
    assert "CCPA" in client.get("/privacy-en").text
    # entity block is env-driven: assert it whenever configured
    entity = os.environ.get("FLUXSWARM_LEGAL_ENTITY", "").strip()
    if entity:
        for path in ("/privacy", "/privacy-en", "/terms", "/terms-en"):
            assert entity in client.get(path).text


def test_legal_pages_stable_translation_pairs():
    # Platform is English-only: /terms and /terms-en both serve English.
    en = client.get("/terms").text
    en2 = client.get("/terms-en").text
    assert "Terms of Service" in en
    assert "Governing law" in en
    assert "Terms of Service" in en2
    assert "Governing law" in en2


def test_squad_api_shape():
    data = client.get("/api/squad").json()
    assert len(data["workers"]) == 4
    profiles = [w["profile"] for w in data["workers"]]
    assert profiles == [p[0] for p in main_mod.hc.SQUAD]
    assert data["verifier"]["profile"] == main_mod.hc.VERIFIER[0]
    assert data["synthesizer"]["profile"] == main_mod.hc.SYNTHESIZER[0]
    # Phase H fix: verifier/synthesizer must expose skills like workers —
    # templates render a.skills.join() for every squad member (missing key
    # threw a TypeError swallowed as "Could not load squad").
    assert data["verifier"]["skills"] == main_mod.hc.VERIFIER[3].split(",")
    assert data["synthesizer"]["skills"] == main_mod.hc.SYNTHESIZER[3].split(",")
    # Phase 3: real BYOK providers only — OpenRouter free tier is the sole
    # zero-cost option and still requires a key + agreement (no anonymous tier).
    assert main_mod.hc.SUPPORTED_PROVIDERS == (
        "anthropic", "openai", "gemini", "kimi", "openrouter")
    assert main_mod.hc.PROVIDER_AGREEMENT_VERSION == "1.0"
    assert not hasattr(main_mod.hc, "PROVIDER_OPENCODE_FREE")


# ---------- workspace endpoint ----------
def test_workspace_requires_auth():
    r = client.get("/api/projects/flux-demo-1/workspace")
    assert r.status_code == 401


def test_workspace_access_rules(_no_hermes):
    a = _register("g3ws-a@fluxswarm.test")
    b = _register("g3ws-b@fluxswarm.test")
    r = client.post("/api/projects", headers=_auth(a["token"]),
                    json={"name": "A", "goal": "Build A"})
    assert r.status_code == 200, r.text
    a_slug = r.json()["slug"]

    # owner sees the result workspace
    r = client.get(f"/api/projects/{a_slug}/workspace", headers=_auth(a["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == a_slug
    assert "generated/index.html" in r.json()["content"]

    # cross-tenant read is 403 (same rule as the task board)
    r = client.get(f"/api/projects/{a_slug}/workspace", headers=_auth(b["token"]))
    assert r.status_code == 403, r.text

    # guessed foreign board is 403, not 404/leak
    r = client.get("/api/projects/u999999-guess/workspace", headers=_auth(b["token"]))
    assert r.status_code == 403

    # demo boards are public showcase for any authenticated user
    r = client.get("/api/projects/flux-demo-1/workspace", headers=_auth(b["token"]))
    assert r.status_code == 200, r.text


# ---------- full journey ----------
def test_full_journey_register_to_delete(_no_hermes):
    email = "g3journey@fluxswarm.test"
    u = _register(email, "UX Tester")
    uid = u["user"]["id"]
    assert u["user"]["credits"] == db_mod.PLANS["demo"]["credits"]  # 5 free
    tok = u["token"]

    # sign in again (customer-style)
    tok = _login(email)

    # create a 1-credit project
    r = client.post("/api/projects", headers=_auth(tok),
                    json={"name": "My app", "goal": "Build a landing page"})
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    assert slug.startswith(f"u{uid}-")
    assert r.json()["workers"] == ["w1", "w2", "w3", "w4"]

    # credit was consumed, balance now 2
    assert db_mod.get_user_by_id(uid)["credits"] == db_mod.PLANS["demo"]["credits"] - 1

    # history lists it
    hist = client.get("/api/projects", headers=_auth(tok))
    assert hist.status_code == 200
    assert any(p["board_slug"] == slug for p in hist.json())

    # tasks + workspace readable
    tasks = client.get(f"/api/projects/{slug}/tasks", headers=_auth(tok))
    assert tasks.status_code == 200 and len(tasks.json()) == 2
    ws = client.get(f"/api/projects/{slug}/workspace", headers=_auth(tok))
    assert ws.status_code == 200 and "generated/index.html" in ws.json()["content"]

    # change password -> old session is dead
    r = client.post("/api/auth/password", headers=_auth(tok),
                    json={"current": "s3cure-Pass-123", "new": "NewPass-4567"})
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=_auth(tok)).status_code == 401

    # fresh login with the new password
    tok2 = _login(email, "NewPass-4567")

    # CCPA export
    r = client.get("/api/account/export", headers=_auth(tok2))
    assert r.status_code == 200
    assert r.json()["user"]["email"] == email

    # account erasure
    r = client.delete("/api/account", headers=_auth(tok2))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db_mod.get_user_by_id(uid) is None
    assert client.get("/api/me", headers=_auth(tok2)).status_code == 401


def test_failed_launch_refunds_credit(_no_hermes):
    u = _register("g3refund@fluxswarm.test")
    uid = u["user"]["id"]
    before = db_mod.get_user_by_id(uid)["credits"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main_mod.hc, "launch_swarm",
                   lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        r = client.post("/api/projects", headers=_auth(u["token"]),
                        json={"name": "B", "goal": "This will fail"})
        assert r.status_code == 500
    assert db_mod.get_user_by_id(uid)["credits"] == before  # refunded