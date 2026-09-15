---
name: provider-resilience
description: >
  BYOK provider onboarding, health probing, runtime resolution, and the
  no-silent-fallback contract for fluxswarm.  Use when adding or fixing
  an LLM provider, or when a launch fails with ProviderConfigError or
  blocked tasks due to missing provider pins.
version: 1.0.0
author: fluxswarm
license: MIT
platforms: [linux, macos, windows]
metadata:
  origin: fluxswarm
  hermes:
    tags: [provider, byok, health, resilience, config, env, pinned]
    related_skills: [team-agent-orchestration, dash-operator]
---

# provider-resilience

How fluxswarm discovers, validates, and pins LLM providers — and what
happens when a provider is unavailable.

## The zero-silent-fallback contract

fluxswarm **never** guesses a provider.  If no runtime is configured,
it raises `ProviderConfigError` immediately (pre-flight), before any
board or worker is created.  There is no anonymous free tier in
Phase 3 — every launch is backed by a deliberate operator BYOK key
or an operator env default (`FLUXSWARM_DEFAULT_PROVIDER`).

This means: if a human says "run the swarm" and no provider is
configured, the correct response is to explain which env var or
BYOK key to set, NOT to try another provider or assume a default.

## Runtime resolution order

```
_resolve_launch_runtime(provider_keys, provider, model):

  0. explicit request-scoped pin (demo path passes model/provider):
     → use them directly, no env mutation, race-free

  1. BYOK key present in provider_keys → use that provider
     model resolved at pin time from operator env overrides

  2. nothing supplied → FLUXSWARM_DEFAULT_PROVIDER + model
     model from FLUXSWARM_DEFAULT_MODEL or FLUXSWARM_MODEL_<PROVIDER>

  3. nothing configured → ProviderConfigError
```

### Supported keyed providers (BYOK precedence)

1. anthropic  (ANTHROPIC_API_KEY)
2. openai     (OPENAI_API_KEY)
3. gemini     (GEMINI_API_KEY)
4. kimi       (KIMI_API_KEY)
5. openrouter  (OPENROUTER_API_KEY)

Keys are injected into subprocess env only — **never** written to
profile `.env` files on disk (see `cleanup_profile_keys()`).

## Provider health probing

```
preflight_provider(provider_keys) → ProviderHealth
```

Status codes:
- `SUCCESS`          — endpoint reachable, model available, ready
- `CONFIG_ERROR`     — no provider configured (ProviderConfigError caught)
- `AUTH_ERROR`       — key invalid or missing
- `PROVIDER_UNAVAILABLE` — endpoint unreachable
- `TIMEOUT`          — probe timed out
- `MODEL_UNAVAILABLE` — provider reachable but specific model unavailable

The probe does **not** consume credits.  It is called once before every
launch.

## Pinning at launch time

After `hermes swarm` creates the board and tasks, `_pin_runtime(board,
...)` iterates every task and calls `kanban set-model` to bind the
concrete model and provider.  This prevents the dispatcher from
spawning workers with a keyless profile default, which would fail and
mark the card `blocked`.

`set-model` failure stops the launch loudly — it is never silently
swallowed.

## Key env vars

| var | purpose |
|---|---|
| `FLUXSWARM_DEFAULT_PROVIDER` | operator-set default provider name |
| `FLUXSWARM_DEFAULT_MODEL` | operator-set default model name |
| `FLUXSWARM_MODEL_<PROVIDER>` | per-provider model override |
| `FLUXSWARM_HERMES_BIN` | path to hermes executable |
| `HERMES_HOME` | hermes data home directory |
| `ANTHROPIC_API_KEY` | BYOK anthropic key |
| `OPENAI_API_KEY` | BYOK openai key |
| `GEMINI_API_KEY` | BYOK gemini key |
| `KIMI_API_KEY` | BYOK kimi key |
| `OPENROUTER_API_KEY` | BYOK openrouter key |

## Common failure modes

| symptom | cause | fix |
|---|---|---|
| `ProviderConfigError: no runtime configured` | No BYOK key + no `FLUXSWARM_DEFAULT_PROVIDER` | Set one of these env vars |
| `ProviderConfigError: ... without a model` | Provider set but no model | Set `FLUXSWARM_DEFAULT_MODEL` |
| `AUTH_ERROR` in preflight | Invalid/expired API key | Rotate the key |
| `PROVIDER_UNAVAILABLE` | Endpoint down or network issue | Wait or check status page; stall detector will catch mid-launch |
| Tasks show `blocked` after launch | `_pin_runtime` failed or key not injected | Check BYOK env injection in `_run` |

## Notes for the human

- Provider keys are **never** written to profile `.env` on disk.  If the
  human asks why keys aren't in `.env`, explain `cleanup_profile_keys()`
  exists to de-fang old plaintext leakage.
- Free tier was removed in Phase 3.  If the human asks for a free
  provider path, explain the `FLUXSWARM_DEFAULT_PROVIDER` fallback exists
  only for operator-configured defaults, not anonymous free use.
- `FLUXSWARM_DISPATCH_TIMEOUT_S` (default 900) controls the driver-loop
  ceiling; the stall detector inside `dispatch()` uses `timeout_s` (600)
  and fires much earlier on no-progress.
