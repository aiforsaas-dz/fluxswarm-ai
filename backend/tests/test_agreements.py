"""Phase 3 — provider agreements (BYOK gate).

Covers (a) DB layer: agree_provider upsert is idempotent, has_provider_agreement
exists, provider_agreements listing, account_payload includes them, and
delete_user removes them; (b) API: GET /api/agreements lists required versions,
POST /api/agreements/{provider} records acceptance, unknown providers 400;
(c) the launch gate in main._user_provider_keys: a BYOK key is only injected
once the user has agreed that provider's terms.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import db as db_mod
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


def _register(email: str):
    r = client.post("/api/auth/register",
                    json={"email": email, "name": "T", "password": "s3cure-Pass-123", "tos_accept": True})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["user"], data["token"]


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------
class TestAgreementDb:
    def test_agree_is_idempotent_upsert(self):
        u, _ = _register("agree-db@fluxswarm.test")
        assert db_mod.agree_provider(u["id"], "anthropic") is True
        # Re-acceptance: returns False (already agreed) and refreshes timestamp.
        assert db_mod.agree_provider(u["id"], "anthropic") is False
        agrees = db_mod.provider_agreements(u["id"])
        assert "anthropic" in agrees
        assert agrees["anthropic"]["version"] == hc.PROVIDER_AGREEMENT_VERSION
        assert agrees["anthropic"]["agreed_at"] > 0

    def test_has_provider_agreement(self):
        u, _ = _register("agree-has@fluxswarm.test")
        assert not db_mod.has_provider_agreement(u["id"], "openai")
        db_mod.agree_provider(u["id"], "openai")
        assert db_mod.has_provider_agreement(u["id"], "openai")
        assert not db_mod.has_provider_agreement(u["id"], "kimi")

    def test_account_payload_includes_agreements_and_delete_removes(self):
        u, _ = _register("agree-acc@fluxswarm.test")
        db_mod.agree_provider(u["id"], "gemini")
        payload = db_mod.account_payload(u["id"])
        assert {a["provider"] for a in payload["provider_agreements"]} == {"gemini"}
        assert db_mod.delete_user(u["id"]) is True
        assert not db_mod.has_provider_agreement(u["id"], "gemini")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class TestAgreementApi:
    def test_list_agreements_shape(self):
        u, tok = _register("agree-api@fluxswarm.test")
        db_mod.agree_provider(u["id"], "anthropic")
        r = client.get("/api/agreements",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert set(data["required_versions"]) == set(hc.SUPPORTED_PROVIDERS)
        assert all(v == hc.PROVIDER_AGREEMENT_VERSION
                   for v in data["required_versions"].values())
        assert "anthropic" in data["accepted"]

    def test_accept_agreement_and_unknown_provider(self):
        u, tok = _register("agree-accept@fluxswarm.test")
        r = client.post("/api/agreements/gemini",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json()["provider"] == "gemini"
        assert r.json()["first_accept"] is True
        # second accept: idempotent refresh
        r2 = client.post("/api/agreements/gemini",
                         headers={"Authorization": f"Bearer {tok}"})
        assert r2.json()["first_accept"] is False

        r3 = client.post("/api/agreements/opencode-free",
                         headers={"Authorization": f"Bearer {tok}"})
        assert r3.status_code == 400  # unknown provider, no free tier

    def test_agreements_require_auth(self):
        fresh = TestClient(main_mod.app)  # no session cookie from prior registers
        assert fresh.get("/api/agreements").status_code == 401
        assert fresh.post("/api/agreements/anthropic").status_code == 401


# ---------------------------------------------------------------------------
# Launch gate: BYOK keys only flow after an agreement
# ---------------------------------------------------------------------------
class TestAgreementLaunchGate:
    def test_unagreed_provider_key_is_not_used(self, monkeypatch):
        import vault
        u, _ = _register("gate-none@fluxswarm.test")
        vault.set_user_key(u["id"], "openai", "sk-openai-tok")
        keys = main_mod._user_provider_keys(u)
        assert keys == {}

    def test_agreed_provider_key_is_used(self, monkeypatch):
        import vault
        u, _ = _register("gate-yes@fluxswarm.test")
        vault.set_user_key(u["id"], "openai", "sk-openai-tok")
        db_mod.agree_provider(u["id"], "openai")
        keys = main_mod._user_provider_keys(u)
        assert keys == {"openai": "sk-openai-tok"}

    def test_other_provider_without_agreement_stays_locked(self, monkeypatch):
        import vault
        u, _ = _register("gate-mix@fluxswarm.test")
        vault.set_user_key(u["id"], "anthropic", "sk-ant")
        vault.set_user_key(u["id"], "kimi", "sk-kim")
        db_mod.agree_provider(u["id"], "anthropic")
        keys = main_mod._user_provider_keys(u)
        assert keys == {"anthropic": "sk-ant"}

    def test_openrouter_free_key_flows_after_agreement(self, monkeypatch):
        # OpenRouter free tier is a first-class BYOK provider: until the user
        # accepts its terms, their free key is never used (same gate as paid).
        import auth
        import vault
        u, tok = _register("gate-or@fluxswarm.test")
        vault.set_user_key(u["id"], "openrouter", "sk-or-free")
        assert main_mod._user_provider_keys(u) == {}

        r = client.post("/api/agreements/openrouter",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["provider"] == "openrouter"
        keys = main_mod._user_provider_keys(u)
        assert keys == {"openrouter": "sk-or-free"}

    def test_api_save_openrouter_key_part_of_hint(self, monkeypatch):
        # Endpoint accepts openrouter so the free demo path is usable end-to-end.
        import vault
        u, tok = _register("gate-or-api@fluxswarm.test")
        r = client.post("/api/keys",
                        json={"provider": "openrouter", "token": "sk-or-free"},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert vault.get_user_key(u["id"], "openrouter") == "sk-or-free"

    def test_api_save_key_then_launch_uses_agreed_runtime(self):
        u, tok = _register("gate-api@fluxswarm.test")
        auth = {"Authorization": f"Bearer {tok}"}
        assert client.post("/api/agreements/openai", headers=auth).status_code == 200
        r = client.post("/api/keys", headers={**auth, "Content-Type": "application/json"},
                        json={"provider": "openai", "token": "sk-openai-tok"})
        assert r.status_code == 200, r.text
        data = client.get("/api/keys", headers=auth).json()
        assert "openai" in data["keys"]
        # And the other providers are never reported unless agreed+set.
        assert "opencode-free" not in data["keys"]