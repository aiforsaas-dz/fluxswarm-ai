"""FluxSwarm in-app support assistant (hybrid).

Rule-based knowledge engine: deterministic, product-accurate answers for the
common questions (pricing, credits, BYOK, referrals, refunds, Telegram, account
data, security) — zero model cost and no API key required.

AI fallback (optional): when no rule matches and the operator has configured an
OpenAI-compatible endpoint (FLUXSWARM_SUPPORT_AI_*), the engine asks the model to
answer strictly from the product handbook below; on any error or missing config
it escalates to the support email. Answers are plain text only (never HTML-the
client renders, so nothing here can inject markup).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

# ---- knowledge base ----------------------------------------------------------

_HANDBOOK = """FluxSwarm product facts (use ONLY these facts, keep answers concise):
- Pricing is one-time credit packs, never a subscription. Demo free with 5 credits;
  Starter $19/20 credits; Pro $49/60 credits; Scale $149/200 credits; Top-up $9/10
  credits (refill that adds credits without changing plan tier).
- 1 credit = 1 launched project = 1 live squad board. Credits never expire.
- A launch that fails before any work starts is refunded automatically (credit returned).
- BYOK: users can save their own AI provider key (Anthropic/OpenAI/Google/Kimi) and
  pay their provider's token rate; FluxSwarm still charges only the 1-credit
  coordination fee. BYOK keys are encrypted before storage and never shared.
- Referrals: referrer earns 15 credits per referred friend's first paid purchase
  (capped 500 credits), the friend gets 10 bonus credits; once per referred email.
- Refunds: handled by Paddle (merchant of record); a merchant refund downgrades the
  plan to Demo and keeps the current credit balance.
- Account deletion erases identifiable data immediately; only the append-only
  security audit log is retained.
- Concurrent agents per launch: Demo 1, Starter 2, Pro 4, Scale 6.
- The squad has 8 agents: Planner, Architect, DevOps, TDD, Reviewer, Designer,
  Builder, Auditor.
- Telegram bot exists; users link it under Account -> Link Telegram.
- Support email: support@fluxswarm.ai; operator contact may differ at runtime.
NEVER invent prices, features or claims not listed here."""


def _answer(text: str) -> dict | None:
    """Static answer keyed by intent. Returns None when no rule matches."""
    return _ANSWER_MAP.get(text)


ENTRY_TEXT = (
    "Hi! I'm the FluxSwarm support assistant. I can answer questions about "
    "pricing, credits, BYOK, referrals, refunds, your data, the Telegram bot "
    "and the agent squad. Pick a quick topic below or ask in your own words."
)


def _mailto() -> dict:
    return {"label": "Email support", "type": "mailto",
            "value": os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")}


_ANSWER_MAP = {
    "greeting": {
        "text": ENTRY_TEXT,
        "actions": [
            {"label": "Pricing", "type": "goto", "value": "pricing"},
            {"label": "Credits", "type": "goto", "value": "pricing"},
            {"label": "How it works", "type": "link", "value": "/how-it-works"},
            {"label": "FAQ", "type": "link", "value": "/faq"},
            _mailto(),
        ],
    },
    "pricing": {
        "text": ("FluxSwarm sells one-time credit packs, never a subscription:\n"
                 "• Demo — free, 5 credits (no card)\n"
                 "• Starter — $19 / 20 credits\n"
                 "• Pro — $49 / 60 credits\n"
                 "• Scale — $149 / 200 credits\n"
                 "• Top-up — $9 / 10 credits (refill, credits never expire)\n"
                 "1 credit = 1 launch. See the pricing page for details."),
        "actions": [{"label": "Open pricing", "type": "goto", "value": "pricing"},
                    {"label": "Refund policy", "type": "link", "value": "/refund"}],
    },
    "demo": {
        "text": ("You can try FluxSwarm free: the Demo plan starts with 5 credits "
                 "and no card is required — new accounts get them automatically at "
                 "sign-up. There is also a public demo board you can browse signed out."),
        "actions": [{"label": "Sign up", "type": "goto", "value": "pricing"}],
    },
    "credits": {
        "text": ("One credit = one launched project = one live squad board. "
                 "A launch that fails before any work starts is refunded (your credit "
                 "is returned). Credits are prepaid and never expire; the Top-up pack "
                 "($9 / 10 credits) refills your balance without changing your plan tier."),
        "actions": [{"label": "Pricing page", "type": "goto", "value": "pricing"}],
    },
    "byok": {
        "text": ("Yes — Bring Your Own Key: save your Anthropic, OpenAI, Google or "
                 "Kimi key under 'My keys' and your launches run on your provider at "
                 "your provider's token rate. FluxSwarm still charges only the flat "
                 "1-credit coordination fee. Keys are encrypted before storage and "
                 "never shared with third parties."),
        "actions": [{"label": "My keys", "type": "goto", "value": "byok"}],
    },
    "referral": {
        "text": ("Refer and earn: share your referral link. When a referred account "
                 "makes a first paid purchase you earn 15 credits (capped at 500 "
                 "credits total), and the friend gets 10 bonus credits. Rewards are "
                 "granted once per referred email; self-referrals and abuse are "
                 "prohibited."),
        "actions": [{"label": "Referrals", "type": "goto", "value": "referrals"}],
    },
    "refund": {
        "text": ("Payments are processed by Paddle (merchant of record), which handles "
                 "sales tax and VAT. A merchant refund downgrades your plan to Demo and "
                 "keeps your current credit balance. Packs are one-time, so there is no "
                 "recurring subscription to cancel. Full policy on the refund page."),
        "actions": [{"label": "Refund policy", "type": "link", "value": "/refund"}],
    },
    "delete": {
        "text": ("You can erase your account and data at any time: Account > 'Delete my "
                 "account and data'. Identifiable data is erased immediately; only the "
                 "append-only security audit log is retained for security and abuse "
                 "investigation."),
        "actions": [{"label": "Privacy policy", "type": "link", "value": "/privacy"}],
    },
    "telegram": {
        "text": ("Yes — FluxSwarm has a Telegram bot. Link your account under Account > "
                 "'Link Telegram', then send a goal to the bot from your chat: the squad "
                 "launches on your account and each launch costs 1 credit."),
        "actions": [{"label": "Link Telegram", "type": "goto", "value": "settings"}],
    },
    "how": {
        "text": ("FluxSwarm launches an 8-agent AI squad (Planner, Architect, DevOps, "
                 "TDD, Reviewer, Designer, Builder, Auditor) on a live board. Describe "
                 "your project and the squad plans, implements and tests it; a Reviewer "
                 "checks the work and a Synthesizer finalises it. You can watch the "
                 "activity and read the workspace as it progresses."),
        "actions": [{"label": "How it works", "type": "link", "value": "/how-it-works"}],
    },
    "security": {
        "text": ("Your BYOK keys are encrypted immediately before storage and never "
                 "logged in plaintext or shared. Boards are scoped per account and "
                 "cross-account access is denied. The security audit log is append-only. "
                 "See the privacy policy for the full details."),
        "actions": [{"label": "Privacy policy", "type": "link", "value": "/privacy"}],
    },
    "limits": {
        "text": ("Concurrent agents per launch are capped by your plan: Demo 1, Starter "
                 "2, Pro 4, Scale 6. The platform also enforces a global demo quota and "
                 "per-account rate limits so the free tier stays fair."),
        "actions": [{"label": "Pricing", "type": "goto", "value": "pricing"}],
    },
    "support": {
        "text": ("A human will get back to you at support@fluxswarm.ai. For the fastest "
                 "help, include the email on your account and a short description of "
                 "what you were doing when the issue happened."),
        "actions": [_mailto()],
    },
}

_ESCALATE = {
    "text": ("I couldn't find that in the help pages, so a human will answer you at "
             "support@fluxswarm.ai instead. You can also browse the FAQ while you "
             "wait."),
    "actions": [_mailto(), {"label": "FAQ", "type": "link", "value": "/faq"}],
}

# intent -> keyword tokens (matched case-insensitively as whole words)
_INTENTS = {
    "support": ["human", "support", "contact", "talk to", "agent please", "live agent"],
    "delete": ["delete account", "delete my", "erase", "remove my data", "gdpr", "drop my account"],
    "refund": ["refund", "revert", "money back", "chargeback", "cancel payment"],
    "referral": ["referral", "refer", "invite", "friend", "earn credits"],
    "telegram": ["telegram", "telegram bot", "link telegram", "tg bot"],
    "byok": ["byok", "bring your own", "api key", "my own key", "provider key", "use my"],
    "pricing": ["price", "pricing", "plans", "plan", "cost", "how much", "pay", "subscription", "subscribe"],
    "credits": ["credit", "credits", "balance", "expire", "top-up", "topup", "refill"],
    "demo": ["free", "trial", "demo", "try before"],
    "security": ["security", "secure", "encrypted", "encrypt", "private", "safe"],
    "limits": ["limit", "limits", "parallel", "concurrent", "max agents"],
    "how": ["how it works", "how does", "agents", "agent", "squad", "swarm", "board", "workspace"],
    "greeting": ["hi", "hello", "hey", "howdy", "good morning", "good evening"],
}


def _match_intent(message: str) -> str | None:
    m = message.lower()
    for intent, tokens in _INTENTS.items():
        for tok in tokens:
            if re.search(r"\b" + re.escape(tok) + r"s?\b", m):
                return intent
    return None


# ---- AI fallback (optional, operator-configured) -----------------------------

def _ai_configured() -> bool:
    return bool(os.environ.get("FLUXSWARM_SUPPORT_AI_KEY", "").strip())


def _ai_answer(message: str, history: list[dict] | None) -> str | None:
    """One chat completion over an OpenAI-compatible endpoint. Returns None on
    any failure (never lets a provider error break the assistant)."""
    if not _ai_configured():
        return None
    base = os.environ.get("FLUXSWARM_SUPPORT_AI_BASE", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.environ.get("FLUXSWARM_SUPPORT_AI_MODEL", "gpt-4o-mini").strip()
    key = os.environ.get("FLUXSWARM_SUPPORT_AI_KEY", "").strip()
    history = (history or [])[-6:]
    messages = [{"role": "system", "content": _HANDBOOK}]
    for turn in history:
        if turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
            messages.append({"role": turn["role"], "content": turn["content"][:2000]})
    messages.append({"role": "user", "content": message[:2000]})
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0.3, "max_tokens": 420}).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return text if text else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return None


# ---- public API --------------------------------------------------------------

def answer(message: str, history: list[dict] | None = None) -> dict:
    """Return {'source': 'kb'|'ai'|'escalate', 'text': str, 'actions': [...]}."""
    message = (message or "").strip()[:2000]
    if not message:
        return {"source": "kb", "text": ENTRY_TEXT, "actions": _ANSWER_MAP["greeting"]["actions"]}
    intent = _match_intent(message)
    if intent:
        ans = _answer(intent)
        if ans:
            return {"source": "kb", **ans}
    ai = _ai_answer(message, history)
    if ai:
        return {"source": "ai", "text": ai, "actions": [_mailto()]}
    return {"source": "escalate", **_ESCALATE}