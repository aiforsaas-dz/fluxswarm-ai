"""Demo provider pool (FLUXSWARM_DEMO_MODE=1 only).

Session 2 (Compliance & Demo Protection): production no longer defaults to a
single zero-cost OpenRouter free model. The demo surface instead tries a small
pool of free-tier providers, in order, and picks the first that passes a
lightweight health probe. When the whole pool is unavailable the caller falls
back to the operator-configured runtime (env) or fails fast — providers are
NEVER probed with real completions here.

Phase C (provider_guard): the pool now routes through circuit breakers,
per-provider cooldowns and round-robin selection instead of always-first. The
public helpers (`get_demo_provider`, `pick_demo_provider`, `resolve_provider_key`)
keep their contracts; only the arbitration inside changed.

Every entry that `requires_key` reads its key from the named env var; without a
key the entry is skipped before any network probe (no pointless AUTH_ERROR
round-trips). `provider` matches an endpoint known to provider.py; the prompt
spelled the first entry "google" (Gemini's provider evolution), which is mapped
to the provider key "gemini" at probe time — CanonicalNames stay in the pool.
"""
from __future__ import annotations

import os
from typing import Optional

from provider import ProviderStatus, check_provider_health
import provider_guard as guard

# Provider -> probe key in provider.py's endpoint table (name drift handled here).
_PROBE_KEY = {"google": "gemini"}

# Demo free-tier pool, verified live BEFORE listing (2026-09-09):
#   - nvidia/nemotron-3.5-lightning:free is the ONLY OpenRouter :free chat model
#     that returned a real completion (HTTP 200, ~1.8s) on that date. Slots that
#     used to work are dead today: google/gemini-flash-1.5:free -> 404 "No
#     endpoints found", meta-llama/llama-3.1-8b-instruct:free -> 404 "only paid".
#   - The legacy google/gemini-1.5-flash slot is dead (404 on v1beta REST);
#     gemini-3.5-flash-lite is the cheapest/fastest free-tier Gemini model as of
#     Sept 2026 (AI Studio free: 5-15 RPM, ~1000/day, $0 input/output).
# Free-tier availability FLUCTUATES; this pool is opportunistic capacity, not
# guaranteed infrastructure — re-verify slots before relying on them.
DEMO_PROVIDERS = [
    {"provider": "google", "model": "gemini-3.5-flash-lite",
     "requires_key": True, "key_env": "GEMINI_API_KEY"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3.5-lightning:free",
     "requires_key": True, "key_env": "OPENROUTER_API_KEY"},
]


def _pool_enabled() -> bool:
    """Operator switch for the demo provider pool (default on)."""
    return os.environ.get("FLUXSWARM_DEMO_POOL_ENABLED", "1").strip() in ("1", "true", "yes")


class ProviderUnavailableError(RuntimeError):
    """Raised when the demo provider pool is exhausted (all entries unhealthy)."""


def _keyed_entries() -> list[dict]:
    """DEMO_PROVIDERS with the runtime probe key attached, minus entries whose
    required key env var is absent (no pointless AUTH_ERROR round-trips)."""
    out = []
    for entry in DEMO_PROVIDERS:
        if not _entry_keyed(entry):
            continue
        e = dict(entry)
        e["probe_key"] = resolve_provider_key(entry["provider"])
        out.append(e)
    return out


def _probe_provider(provider: str, model: str) -> bool:
    """Health-probe one pool entry without consuming credits."""
    probe_key = _PROBE_KEY.get(provider, provider)
    health = check_provider_health(probe_key, model)
    return health.status == ProviderStatus.SUCCESS


def _entry_keyed(entry: dict) -> bool:
    """A requires_key entry only counts when its env key is present."""
    key_env = entry.get("key_env")
    if not entry.get("requires_key") or not key_env:
        return True
    return bool(os.environ.get(key_env, "").strip())


# The free demo's only latency-friendly slot. gemini-1.5-flash converts a
# squad lane in seconds; the nemotron free slot is a slow fallback that (alone)
# makes a full 8-lane swarm outpace the demo runtime cap on a throttled
# instance. Prefer the fast slot whenever its key is configured and healthy.
_FAST_DEMO_PROVIDER = "google"
_FAST_DEMO_MODEL_PREFIX = "gemini-"


def get_demo_provider() -> dict:
    """Return the next available demo provider (fast-slot preference: gemini-1.5
    flash when keyed and healthy, otherwise the normal rotation).

    Synchronous core: provider.py's health probe is sync and the demo launch
    endpoint is sync; `get_demo_provider_async` wraps this for async callers.
    The returned dict matches the DEMO_PROVIDERS shape, extended with
    ``probe_key`` (the provider.py runtime key) and ``reason``.
    """
    try:
        keyed = _keyed_entries()
        fast = [e for e in keyed
                if e.get("provider") == _FAST_DEMO_PROVIDER
                and str(e.get("model", "")).startswith(_FAST_DEMO_MODEL_PREFIX)]
        if fast:
            try:
                return guard.pick_rotation(fast)
            except guard.ProviderPoolBlocked:
                pass  # fast slot down / mid-cooldown: fall through to the pool
        return guard.pick_rotation(keyed)
    except guard.ProviderPoolBlocked:
        raise ProviderUnavailableError("All demo providers exhausted")


async def get_demo_provider_async() -> dict:
    """Async shim over get_demo_provider (the prompt's ``await rat`` API)."""
    return get_demo_provider()


def resolve_provider_key(provider: str) -> str:
    """Map a pool provider name to the provider.py runtime key ("google" -> "gemini")."""
    return _PROBE_KEY.get(provider, provider)


def pick_demo_provider() -> Optional[dict]:
    """Safe wrapper: never raises — None means "fall back to operator runtime"."""
    try:
        return get_demo_provider()
    except ProviderUnavailableError:
        return None
    except Exception:
        return None


def pool_capacity() -> dict:
    """Truthful health/capacity report over the full demo pool (which entries
    are viable, breaker state, availability) for /health and the UI."""
    enabled = _pool_enabled()
    report = guard.capacity_report(DEMO_PROVIDERS, pool_enabled=enabled)
    entries = []
    for entry in DEMO_PROVIDERS:
        entries.append({
            "provider": entry.get("provider"),
            "model": entry.get("model"),
            "key_env": entry.get("key_env"),
            "requires_key": bool(entry.get("requires_key")),
            "keyed": _entry_keyed(entry),
        })
    report["entries"] = entries
    return report