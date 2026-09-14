"""Minimal transactional email (Resend) — env-gated, never blocking.

Everything here is a no-op unless FLUXSWARM_RESEND_API_KEY is configured, so the
feature lights up at deploy time without a single code change and can never take
the request path down (all sends run on a daemon thread).

Env knobs:
  FLUXSWARM_RESEND_API_KEY  -> enables sending
  FLUXSWARM_MAIL_FROM       -> sender, default "FluxSwarm <onboarding@resend.dev>"
"""
from __future__ import annotations

import os
import threading
import urllib.request

_ACTIVE = bool(os.environ.get("FLUXSWARM_RESEND_API_KEY", "").strip())
_FROM = os.environ.get("FLUXSWARM_MAIL_FROM", "FluxSwarm <onboarding@resend.dev>")
_API = "https://api.resend.com/emails"
_TIMEOUT_S = 10

WELCOME_SUBJECT = "Welcome to FluxSwarm"
DEPLETION_SUBJECT = "You are out of FluxSwarm credits"


def active() -> bool:
    return _ACTIVE


def _html_welcome(name: str) -> str:
    return (
        "<p>Hi" + (f" {_esc(name)}" if name else "") + ",</p>"
        "<p>Welcome to FluxSwarm — your AI development squad.</p>"
        "<p>Type a goal and an 8-agent crew plans, builds, tests, reviews and "
        "assembles it on a live board. Your account includes a free Demo plan "
        "with 5 credits (1 credit per launch, credits never expire).</p>"
        "<p>Bring your own AI provider key any time for stronger output.</p>"
    )


def _html_depletion(name: str) -> str:
    return (
        "<p>Hi" + (f" {_esc(name)}" if name else "") + ",</p>"
        "<p>You have run out of credits, so new launches are paused until you "
        "top up. Your boards and data stay fully accessible.</p>"
        "<p>Credit packs start at $9 for 10 credits — they never expire and "
        "each one covers one launch.</p>"
    )


def _esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def send_welcome_async(email: str, name: str) -> None:
    _spawn(email, name, WELCOME_SUBJECT, _html_welcome)


def send_depletion_async(email: str, name: str) -> None:
    _spawn(email, name, DEPLETION_SUBJECT, _html_depletion)


def _spawn(email: str, name: str, subject: str, html: str) -> None:
    if not _ACTIVE or not email:
        return
    threading.Thread(
        target=_send_sync,
        args=(email, subject, html),
        name="fluxswarm_email", daemon=True,
    ).start()


def _send_sync(email: str, subject: str, html: str) -> None:
    """Fire the Resend API call on a background thread. Failures are swallowed:
    email is best-effort and must never bubble into the request path."""
    try:
        key = os.environ.get("FLUXSWARM_RESEND_API_KEY", "").strip()
        if not key:
            return
        payload = ("{\"from\":" + _to_json(_FROM) + ",\"to\":[" + _to_json(email)
                   + "],\"subject\":" + _to_json(subject)
                   + ",\"html\":" + _to_json(html) + "}")
        data = payload.encode("utf-8")
        req = urllib.request.Request(
            _API, data=data, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            resp.read()
    except Exception:
        pass


def _to_json(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'