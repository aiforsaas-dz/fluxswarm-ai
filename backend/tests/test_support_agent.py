"""In-app support assistant: rule engine accuracy + prefer-AI-then-escalate
fallback + the /api/support/chat endpoint (open, rate-limited, audited)."""
from __future__ import annotations

import fastapi.testclient
import pytest

import support_agent

from main import app as main_app

client = fastapi.testclient.TestClient(main_app)


def test_greeting_empty_message():
    r = support_agent.answer("")
    assert r["source"] == "kb"
    assert "support assistant" in r["text"].lower()
    assert r["actions"]


def test_pricing_intent():
    r = support_agent.answer("how much does the pro plan cost?")
    assert r["source"] == "kb"
    assert "$49" in r["text"]
    assert "60" in r["text"]
    assert "never a subscription" in r["text"]


def test_credits_intent():
    r = support_agent.answer("do credits expire?")
    assert r["source"] == "kb"
    assert "never expire" in r["text"]
    assert "Top-up" in r["text"]


def test_byok_intent():
    r = support_agent.answer("can I bring my own api key to save money?")
    assert r["source"] == "kb"
    assert "Bring Your Own Key" in r["text"]


def test_referral_intent():
    r = support_agent.answer("tell me about the referral program")
    assert r["source"] == "kb"
    assert "15 credits" in r["text"]
    assert "500" in r["text"]
    assert "10 bonus" in r["text"]


def test_refund_intent():
    r = support_agent.answer("I want a money back refund")
    assert r["source"] == "kb"
    assert "Paddle" in r["text"]
    assert any(a["type"] == "link" and a["value"] == "/refund" for a in r["actions"])


def test_delete_intent():
    r = support_agent.answer("how do I delete my account?")
    assert r["source"] == "kb"
    assert "Delete my" in r["text"]


def test_telegram_intent():
    r = support_agent.answer("link telegram bot")
    assert r["source"] == "kb"
    assert "Telegram" in r["text"]


def test_how_it_works_intent():
    r = support_agent.answer("how does the squad work")
    assert r["source"] == "kb"
    assert "8-agent" in r["text"]


def test_limits_intent():
    r = support_agent.answer("what is the concurrency limit?")
    assert r["source"] == "kb"
    assert "Demo 1" in r["text"] and "Scale 6" in r["text"]


def test_support_intent_mailto():
    r = support_agent.answer("talk to a human support agent")
    assert r["source"] == "kb"
    assert any(a["type"] == "mailto" and a["value"] == "support@fluxswarm.ai"
               for a in r["actions"])


def test_escalate_when_no_ai_configured(monkeypatch):
    monkeypatch.delenv("FLUXSWARM_SUPPORT_AI_KEY", raising=False)
    r = support_agent.answer("formula for something made up?")
    assert r["source"] == "escalate"
    assert "support@fluxswarm.ai" in r["text"]
    assert any(a["type"] == "mailto" for a in r["actions"])


def test_ai_failure_falls_back_to_escalate(monkeypatch):
    monkeypatch.setenv("FLUXSWARM_SUPPORT_AI_KEY", "test-key-not-real")
    monkeypatch.setenv("FLUXSWARM_SUPPORT_AI_BASE", "http://127.0.0.1:1")
    monkeypatch.setenv("FLUXSWARM_SUPPORT_AI_MODEL", "test-model")
    r = support_agent.answer("some completely unknown question about nothing")
    assert r["source"] in ("ai", "escalate")
    assert r["text"]


def test_answer_never_contains_banned_claims():
    for msg in ("how much is it", "what is the demo", "is it unlimited billing",
                "refund please", "delete account", "referral", "security of keys"):
        r = support_agent.answer(msg)
        low = r["text"].lower()
        for claim in ("unlimited", "/month", "hourly", "guaranteed"):
            assert claim not in low, f"banned claim {claim!r} in {r['text']!r}"


# ---- /api/support/chat endpoint ----
def test_endpoint_open_and_answers_kb():
    r = client.post("/api/support/chat", json={"message": "what are the plans?"})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] in ("kb", "ai", "escalate")
    assert d["text"]


def test_endpoint_empty_message_returns_greeting():
    r = client.post("/api/support/chat", json={"message": ""})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "kb"
    assert "support assistant" in d["text"].lower()


def test_endpoint_requires_message_limit():
    r = client.post("/api/support/chat", json={"message": "x" * 3000})
    assert r.status_code == 422


def test_endpoint_rate_limited():
    # Burst past the per-IP window (20/60s) to verify 429. Kept as the last test
    # in this module: tripping the limiter would 429 later calls in this session.
    code = None
    for _ in range(21):
        r = client.post("/api/support/chat", json={"message": "pricing"})
        code = r.status_code
        if code == 429:
            break
    assert code == 429
    assert r.json()["detail"]