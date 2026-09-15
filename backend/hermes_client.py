"""
FluxSwarm -> Hermes bridge.

Runs the `hermes kanban` CLI as a subprocess to create per-project boards and
launch the full squad (swarm). Each FluxSwarm project maps 1:1 to a Hermes
kanban board (--board <slug>), which isolates its swarm from all others.

Key fixes (v0.5):
- Squad agents are shown WITHOUT the `ecc-` prefix to end users (display names).
- BYOK keys are injected into each agent PROFILE's .env so the squad can
  actually run (otherwise planner/architect block with no model key).
- A continuous dispatcher loop is run until the swarm reaches a terminal state,
  and generated outputs are read back from each task's workspace for review.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from provider import ProviderHealth, ProviderStatus, check_provider_health
import demo_llm
import board_store

# HERMES_BIN is overridable via FLUXSWARM_HERMES_BIN so the same code runs on
# Linux/Docker (e.g. /app/hermes/bin/hermes) as well as the dev Windows host.
# Falls back to the host's installed path when the env var is unset.
_HERMES_BIN_ENV = os.environ.get("FLUXSWARM_HERMES_BIN")
HERMES_BIN = Path(_HERMES_BIN_ENV) if _HERMES_BIN_ENV else Path("C:/Users/DELL/AppData/Local/hermes/bin/hermes.exe")
HERMES_HOME = os.environ.get("HERMES_HOME", "C:/Users/DELL/AppData/Local/hermes")
PROFILES_DIR = Path(HERMES_HOME) / "profiles"

# Session 3: memory-derived host concurrency budget ("null budget" cap).
# Hermes' dispatcher *independently* derives its own in-swarm limit
# (kanban.max_in_progress / lane resolution); this is OUR side's default
# max_spawn ceiling so no launch over-commits the container's RAM.
#   MEMORY_GUARD_MB_PER_WORKER = guard RAM per concurrent worker.
#   FLUXSWARM_MEM_TOTAL_MB     = the cgroup/container memory limit (docker
#                                compose `deploy.resources.limits.memory`).
# 8GB / 384MB = 21 -> clamped to MAX_IN_PROGRESS = 16.
_MEM_TOTAL_MB = int(os.environ.get("FLUXSWARM_MEM_TOTAL_MB", "8192"))
MEMORY_GUARD_MB_PER_WORKER = int(os.environ.get("MEMORY_GUARD_MB_PER_WORKER", "384"))
MAX_IN_PROGRESS = max(1, min(16, _MEM_TOTAL_MB // MEMORY_GUARD_MB_PER_WORKER))


def _cgroup_memory_mb() -> int | None:
    """Host memory LIMIT from the container cgroup (bytes) — the reliable
    "how much RAM can I actually use" figure. Returns None when not running
    under cgroup v1/v2 limits (desktop dev/CI), so callers fall back to the
    configured default (8 GB) and the fat path stays the default choice."""
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            val = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
            if val.isdigit():
                mb = int(val) // (1024 * 1024)
                if mb > 0:
                    return mb
        except Exception:
            continue
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8",
                                                    errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def projects_are_thin() -> bool:
    """True when this host cannot run the fat Hermes worker path.

    Measured on the 512 MB free tier: a real Hermes worker (or the fat
    ``swarm``/``boards create`` CLI subprocess that loads the whole workspace)
    crashes ~40-80 s into work and the OOM kills the container. On such small
    hosts every REAL launch must use the thin in-process direct-DB driver
    instead. ``FLUXSWARM_PROJECT_MODE=thin|fat`` overrides the auto-detect;
    the auto rule is: a cgroup/container memory limit <= 2 GB => thin.
    """
    mode = os.environ.get("FLUXSWARM_PROJECT_MODE", "").strip().lower()
    if mode == "thin":
        return True
    if mode == "fat":
        return False
    mem = _cgroup_memory_mb()
    if mem is not None:
        return mem <= 2048
    return False

# Safe board-slug charset. Slugs are server-generated (u{uid}-{time}-{rand},
# u{uid}-tg-…, flux-demo-…, tg-{chat}-…), but delete_boards validates every
# incoming name against this before touching the filesystem, so a corrupted DB
# row can never turn into an arbitrary path delete.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")

# Agent profiles (internal Hermes profile names) + display names (no ecc- prefix).
# Skill names MUST match real ECC skills under skills/ecc/skills (verified present).
SQUAD = [
    ("ecc-planner", "Planner", "Plan the feature breakdown", "plan-orchestrate"),
    ("ecc-architect", "Architect", "Design system architecture", "api-design,fastapi-patterns"),
    ("ecc-devops", "DevOps", "Set up CI/CD and containerization", "docker-patterns,deployment-patterns"),
    ("ecc-tdd", "TDD", "Write the test suite", "tdd-workflow"),
]
VERIFIER = ("ecc-reviewer", "Reviewer", "Review code quality", "agent-self-evaluation,verification-loop")
SYNTHESIZER = ("ecc-build-fixer", "Builder", "Assemble and make the build green", "orch-build-mvp")
# Extended squad members (P4 display-only lanes on the thin board — cheap
# bounded completions, no extra ECC profile is provisioned).
DESIGNER = ("ecc-designer", "Designer", "Define the visual design system", "design-system,ui-ux")
AUDITOR = ("ecc-auditor", "Auditor", "Audit the delivered artifact", "agent-self-evaluation,verification-loop")

# Map a marketplace template's DISPLAY names -> (internal profile, skills, role).
# Lets a bought squad launch with the real agents behind the friendly names.
AGENT_REGISTRY = {
    "Planner":   ("ecc-planner",   "plan-orchestrate", "worker"),
    "Architect": ("ecc-architect", "api-design,fastapi-patterns", "worker"),
    "DevOps":    ("ecc-devops",    "docker-patterns,deployment-patterns", "worker"),
    "TDD":       ("ecc-tdd",       "tdd-workflow", "worker"),
    "Reviewer":  ("ecc-reviewer",  "agent-self-evaluation,verification-loop", "verifier"),
    "Builder":   ("ecc-build-fixer", "orch-build-mvp", "synthesizer"),
    "Designer":  ("ecc-designer", "design-system,ui-ux", "worker"),
    "Auditor":   ("ecc-auditor", "agent-self-evaluation,verification-loop", "worker"),
}

# Map our BYOK provider names -> the env var each Hermes agent profile expects.
ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "kimi": "KIMI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Keyed BYOK providers, in precedence order (see _resolve_runtime). Includes the
# OpenRouter free tier: it needs a real (free) API key plus the user's provider
# agreement, so it is resolved exactly like the paid BYOK providers.
_KEYED_PROVIDERS = ("anthropic", "openai", "gemini", "kimi", "openrouter")

# Provider agreement gate (Phase 3): a launch that relies on a BYOK provider
# requires the user to have accepted that provider's terms first (see
# PROVIDER_AGREEMENT_VERSION + the /api/agreements endpoints). Kept here as the
# single source of truth so main.py and the DB layer agree on the set.
SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini", "kimi", "openrouter")
PROVIDER_AGREEMENT_VERSION = "1.0"

# Default model when an operator configures FLUXSWARM_DEFAULT_PROVIDER=openrouter
# (or a user brings an OpenRouter BYOK key) without a FLUXSWARM_MODEL_OPENROUTER
# override. z-ai/glm-5.2:free is the current best free TRYING endpoint on
# OpenRouter's free tier; free endpoints rotate, so ops can pin a different one
# via the env override. It does NOT broaden the "no silent fallback" rule to
# other providers: this default applies only once the operator/user has
# explicitly chosen openrouter.
OPENROUTER_DEFAULT_MODEL = "z-ai/glm-5.2:free"


class ProviderConfigError(RuntimeError):
    """Raised when no runtime is deliberately configured (never silently)."""


# Driver-loop ceiling for a swarm launch (seconds). This is the HARD upper
# bound the background dispatcher waits on a launch before it reports the true
# outcome; it is NOT the primary stuck-detection mechanism — that is the
# no-progress stall detector in `dispatch()` which stops much earlier when a
# provider outage stalls the swarm. 900s allows a healthy multi-wave swarm
# (workers -> verifier -> synthesizer) to finish while bounding the total wait
# so an upstream outage can never hold the driver for many hours. Overridable
# via FLUXSWARM_DISPATCH_TIMEOUT_S for ops.
DISPATCH_TIMEOUT_S = int(os.environ.get("FLUXSWARM_DISPATCH_TIMEOUT_S", "900"))


def _resolve_runtime(provider_keys: Optional[dict], provider: Optional[str] = None,
                     model: Optional[str] = None):
    """Return (model, provider) to pin every squad task to.

    Phase 3: there is NO free tier. Priority:
      0. An explicit request-scoped pin (provider/model passed by the caller):
         the demo path resolves a concrete healthy pool entry and passes it
         here as kwargs — a deliberate, race-free choice that never mutates the
         process-global ``os.environ``.
      1. User BYOK key -> use that provider's model (Claude/GPT/...). The model
         itself is resolved at pin time from the operator's env overrides.
      2. Nothing supplied -> the DEPLOYMENT default: operator-configured
         provider/model (FLUXSWARM_DEFAULT_PROVIDER / FLUXSWARM_DEFAULT_MODEL).
         An unconfigured runtime raises ProviderConfigError — there is no
         silent fallback to a free model, in any mode.
    """
    if provider:
        return model, provider
    if provider_keys:
        for prov in _KEYED_PROVIDERS:
            if provider_keys.get(prov):
                return None, prov  # model resolved/bound at pin time
    return _default_runtime()


def _default_runtime() -> tuple[Optional[str], str]:
    """Production runtime configured by the operator (no free fallback).

    FLUXSWARM_DEFAULT_PROVIDER set -> that provider, with a model from
    FLUXSWARM_DEFAULT_MODEL or FLUXSWARM_MODEL_<PROVIDER>. A provider without a
    model raises (a provider pin without a model cannot be persisted).
    Otherwise -> ProviderConfigError: a deployment without a deliberate
    provider default must not send squad work anywhere by guessing.
    """
    prov = os.environ.get("FLUXSWARM_DEFAULT_PROVIDER", "").strip()
    if prov:
        model = _operator_model_for(prov)
        if not model:
            raise ProviderConfigError(
                f"FLUXSWARM_DEFAULT_PROVIDER={prov!r} is set without a model: "
                "set FLUXSWARM_DEFAULT_MODEL or "
                f"FLUXSWARM_MODEL_{prov.upper().replace('-', '_')} "
                "(no silent fallback)."
            )
        return model, prov
    raise ProviderConfigError(
        "no runtime configured: set FLUXSWARM_DEFAULT_PROVIDER (with "
        "FLUXSWARM_DEFAULT_MODEL), or provide BYOK keys. Refusing to guess a "
        "provider or model (no anonymous free tier)."
    )


def _operator_model_for(provider: str) -> Optional[str]:
    """Optional operator-configured model override for a provider.

    Tries the per-provider override (FLUXSWARM_MODEL_<PROVIDER>) first, then the
    shared production default (FLUXSWARM_DEFAULT_MODEL). For ``openrouter`` — and
    only for it — the deliberately-chosen free demo model is the final fallback
    (OPENROUTER_DEFAULT_MODEL), because opting into openrouter is itself the
    deliberate runtime choice. Returns None when no model is declared — the
    caller must then fail loudly rather than guess.
    """
    per_provider = os.environ.get(
        "FLUXSWARM_MODEL_" + provider.upper().replace("-", "_"), "").strip()
    if per_provider:
        return per_provider
    shared = os.environ.get("FLUXSWARM_DEFAULT_MODEL", "").strip()
    if shared:
        return shared
    if provider == "openrouter":
        return OPENROUTER_DEFAULT_MODEL
    return None


def _resolve_launch_runtime(provider_keys: Optional[dict], provider: Optional[str] = None,
                            model: Optional[str] = None) -> tuple[str, str]:
    """Resolve a concrete, pinnable (model, provider) for a launch.

    An explicit request-scoped ``provider`` (with its ``model``) is used as-is —
    no env involvement, so concurrent launches cannot cross-pollute. BYOK
    providers get their model from the operator's env overrides; a launch
    whose runtime cannot be pinned fails fast BEFORE any board/worker exists.
    """
    if provider:
        m = model or _operator_model_for(provider)
        if not m:
            raise ProviderConfigError(
                f"cannot resolve a model for provider={provider!r}: set "
                "FLUXSWARM_MODEL_<PROVIDER> or FLUXSWARM_DEFAULT_MODEL. "
                "Refusing to fall back silently (free or paid)."
            )
        return m, provider
    model, provider = _resolve_runtime(provider_keys)
    if not model:
        model = _operator_model_for(provider)
    if not model:
        raise ProviderConfigError(
            f"cannot resolve a model for provider={provider!r}: set "
            "FLUXSWARM_MODEL_<PROVIDER> or FLUXSWARM_DEFAULT_MODEL. "
            "Refusing to fall back silently (free or paid)."
        )
    return model, provider


def preflight_provider(provider_keys: Optional[dict] = None) -> ProviderHealth:
    """Lightweight provider health-check before dispatch.

    Resolves the runtime that would be used, then probes the provider
    endpoint WITHOUT consuming credits.  Returns a ProviderHealth with
    the diagnostic status (CONFIG_ERROR, AUTH_ERROR, PROVIDER_UNAVAILABLE,
    TIMEOUT, MODEL_UNAVAILABLE, SUCCESS).
    """
    try:
        model, provider = _resolve_launch_runtime(provider_keys)
    except ProviderConfigError as exc:
        return ProviderHealth(
            status=ProviderStatus.CONFIG_ERROR,
            provider=provider_keys.get("provider", "unknown") if provider_keys else "none",
            model=None,
            detail=str(exc),
        )

    # Extract the credential for the resolved provider (BYOK or operator env —
    # there is no free tier in Phase 3, so a missing credential is an auth error).
    credential = None
    if provider_keys:
        credential = provider_keys.get(provider)

    return check_provider_health(provider, model=model, credential=credential)


# Which env vars we should NEVER inject into shared profile .env files.
# Provider keys stay in the subprocess environment only (never on disk).
_PROFILE_KEY_NAMES = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY"}


@dataclass
class SwarmResult:
    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str


def _squad_profiles() -> list[str]:
    return [p for p, *_ in SQUAD] + [VERIFIER[0], SYNTHESIZER[0]]


def cleanup_profile_keys():
    """One-time de-fang: remove provider API keys that older versions wrote
    in PLAINTEXT into the shared, cross-user profile .env files.

    Provider keys are now injected only into each subprocess environment
    (see `_run`), never persisted on disk, so they cannot leak between users.
    Idempotent and cheap; called on every swarm launch.
    """
    for prof in _squad_profiles():
        env_path = PROFILES_DIR / prof / ".env"
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        kept = []
        changed = False
        for line in lines:
            if "=" in line and not line.startswith("#"):
                k = line.split("=", 1)[0].strip()
                if k in _PROFILE_KEY_NAMES:
                    changed = True
                    continue
            kept.append(line)
        if changed:
            try:
                env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            except OSError:
                pass


class _DockerResult:
    """Adapt hermes_docker.run_dispatch() dict to the CompletedProcess surface
    _run callers expect (`.returncode`, `.stdout`, `.stderr`), so the rest of
    the dispatch/launch machinery is agnostic to the transport."""

    def __init__(self, result: dict):
        self.result = result
        self.returncode = 124 if result.get("timed_out") else (result.get("returncode") or 0)
        self.stdout = result.get("stdout_tail") or ""
        self.stderr = result.get("stderr_tail") or ""
        self.timed_out = bool(result.get("timed_out"))
        self.container = result.get("container")

    def __bool__(self):
        return self.returncode == 0


def _run(args: list[str], board: Optional[str] = None, capture=True,
         provider_keys: Optional[dict] = None) -> subprocess.CompletedProcess:
    # IMPORTANT: --board is an option on `hermes kanban`, BEFORE the subcommand.
    # Phase 2 opt-in: run the hermes CLI inside the hardened Docker sandbox
    # (hermes_docker.py) instead of directly on the host. Default OFF — the
    # existing host runtime remains the default until the runner image is
    # deployed to all nodes. FLUXSWARM_DOCKER_DISPATCH=1 enables it.
    if os.environ.get("FLUXSWARM_DOCKER_DISPATCH", "").strip() == "1":
        try:
            import hermes_docker
        except ImportError:
            pass
        else:
            if board:
                # build a hermes kanban argv without the --board front (the
                # container already knows the board via the read-only mount).
                hermes_argv = args
                try:
                    default_model, default_provider = _default_runtime()
                except ProviderConfigError:
                    default_model, default_provider = None, None
                result = hermes_docker.run_dispatch(
                    board=board, argv=hermes_argv, provider_keys=provider_keys,
                    provider=default_provider, model=default_model,
                )
                return _DockerResult(result)

    cmd = [str(HERMES_BIN), "kanban"]
    if board:
        cmd += ["--board", board]
    cmd += args
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME
    # Also inject at process level as a fallback. The declared default comes
    # from the operator (or Demo/dev); an unconfigured production gets NO
    # provider env here — launch paths fail fast in _resolve_launch_runtime.
    if provider_keys:
        for prov, tok in provider_keys.items():
            ev = ENV_MAP.get(prov)
            if ev and tok:
                env[ev] = tok
    else:
        try:
            model, provider = _default_runtime()
        except ProviderConfigError:
            pass  # non-launch command, unconfigured prod: no provider env at all
        else:
            if model:
                env.setdefault("HERMES_DEFAULT_MODEL", model)
            env.setdefault("HERMES_DEFAULT_PROVIDER", provider)
    return subprocess.run(cmd, capture_output=capture, text=True, env=env, timeout=300)


def ensure_board(slug: str) -> bool:
    _raise_preflight()
    r = _run(["boards", "create", slug, "--description", f"FluxSwarm project {slug}"])
    r2 = _run(["boards", "ls"])
    return slug in r2.stdout


def preflight() -> list[str]:
    """Fail-fast diagnostics for the Hermes runtime.

    Returns a list of unmet requirements (empty list == everything present).
    Only filesystem stat()s — cheap, safe to call on every launch. A clear
    message beats the raw FileNotFoundError/KeyError a missing install would
    otherwise surface to the operator.
    """
    problems = []
    if not HERMES_BIN.exists():
        problems.append(
            f"Hermes binary not found at {HERMES_BIN} "
            "(install Hermes or point FLUXSWARM_HERMES_BIN at it)")
    if not PROFILES_DIR.is_dir():
        problems.append(f"Hermes profiles dir not found at {PROFILES_DIR} (check HERMES_HOME)")
    for prof in _squad_profiles():
        if not (PROFILES_DIR / prof).is_dir():
            problems.append(f"squad profile missing: {PROFILES_DIR / prof}")
    return problems


def _raise_preflight():
    """Raise RuntimeError with an actionable message when the Hermes runtime is
    not usable. Fail fast BEFORE the caller pays for a broken board."""
    problems = preflight()
    if problems:
        raise RuntimeError("Hermes runtime not ready: " + "; ".join(problems))


# The Kanban Swarm CLI (`hermes kanban swarm`) hard-codes this skill name on the
# verifier task (`hermes_cli/kanban_swarm.py`) and offers no way to override it.
# The skill ships inside the bundled ``software-development`` collection, which
# is NOT visible to the ECC profiles (each scans only ``skills/ecc/skills`` +
# its own profile-local skills). A dispatcher-owned reviewer worker therefore
# receives ``--skills requesting-code-review`` and dies at startup with
# "Unknown skill(s): requesting-code-review". Candidate C fixes this by
# provisioning the REAL bundled skill into ``skills/ecc/skills/`` (byte-for-byte)
# BEFORE the swarm is created, so the verifier task resolves it normally.
_VERIFIER_SKILL_NAME = "requesting-code-review"
_ECC_SKILLS_DIR_NAME = "ecc/skills"


def _bundled_verifier_skill_dir() -> Path:
    """Locate the REAL bundled ``requesting-code-review`` skill directory.

    Deterministic repository-relative discovery first (the Hermes bundled
    ``software-development`` collection under HERMES_HOME/skills). Falls back to
    Hermes' own skill-directory discovery mechanism when present.
    """
    primary = Path(HERMES_HOME) / "skills" / "software-development" / _VERIFIER_SKILL_NAME
    if primary.is_dir():
        return primary
    try:
        from agent.skill_utils import get_all_skills_dirs
        for skills_dir in get_all_skills_dirs():
            cand = Path(skills_dir) / "software-development" / _VERIFIER_SKILL_NAME
            if cand.is_dir():
                return cand
            cand2 = Path(skills_dir) / _VERIFIER_SKILL_NAME
            if cand2.is_dir() and (cand2 / "SKILL.md").exists():
                return cand2
    except Exception:
        pass
    return primary  # caller reports it cleanly when absent


def _ensure_verifier_skill() -> Path:
    """Provision the real bundled ``requesting-code-review`` skill for the
    ``ecc-reviewer`` profile. Idempotent and non-destructive.

    - Target ``skills/ecc/skills/requesting-code-review/SKILL.md`` present ->
      no-op (NEVER overwrite, even a foreign/operator-tuned copy).
    - Target missing OR an EMPTY stale dir (e.g. left by an interrupted earlier
      provisioning) -> copy the REAL bundled skill from the
      ``software-development`` collection BYTE-FOR-BYTE. An empty dir must not
      silently re-create the MISSING-skill worker death.
    - Target EXISTS with content but no ``SKILL.md`` -> fail loudly and refuse to
      delete what may be a foreign directory.
    - Bundled source absent -> fail safely with a clear diagnostic; do NOT
      fabricate a skill or try to fake the review behavior.

    Returns the target skill directory (for callers/tests).
    """
    target = Path(HERMES_HOME) / "skills" / _ECC_SKILLS_DIR_NAME / _VERIFIER_SKILL_NAME
    if (target / "SKILL.md").exists():
        return target
    source = _bundled_verifier_skill_dir()
    if not source.is_dir() or not (source / "SKILL.md").exists():
        raise RuntimeError(
            f"cannot provision verifier skill '{_VERIFIER_SKILL_NAME}': bundled "
            f"source not found under {Path(HERMES_HOME) / 'skills'} "
            "(expected in the 'software-development' collection). Refusing to "
            "fabricate a replacement skill."
        )
    if target.exists():
        if any(target.iterdir()):
            raise RuntimeError(
                f"cannot provision verifier skill '{_VERIFIER_SKILL_NAME}': target "
                f"{target} exists with content but no SKILL.md; refusing to "
                "overwrite a possibly-foreign directory. Remove it manually or "
                "point the profile's skills.external_dirs elsewhere."
            )
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def launch_swarm(board: str, goal: str, provider_keys: Optional[dict] = None,
                 provider: Optional[str] = None, model: Optional[str] = None) -> SwarmResult:
    # Keys are passed via subprocess environment only (never written to disk).
    # Fail fast BEFORE any board/worker exists when no runtime is configured.
    # An explicit `provider`/`model` (demo pool pick) is honored as a
    # request-scoped pin; not passing them falls back to BYOK/env as before.
    _resolve_launch_runtime(provider_keys, provider, model)
    _raise_preflight()
    cleanup_profile_keys()
    # The swarm CLI hard-codes the verifier skill; make sure it resolves under
    # the ecc-reviewer profile BEFORE the swarm/board tasks are created.
    _ensure_verifier_skill()
    worker_args = []
    for prof, disp, title, skills in SQUAD:
        worker_args += ["--worker", f"{prof}:{title}:{skills}"]
    cmd = ["swarm", goal] + worker_args + [
        "--verifier", VERIFIER[0],
        "--synthesizer", SYNTHESIZER[0],
        "--created-by", "fluxswarm",
        "--json",
    ]
    r = _run(cmd, board=board, provider_keys=provider_keys)
    if r.returncode != 0:
        raise RuntimeError(f"swarm launch failed: {r.stderr}")
    data = json.loads(r.stdout)
    # Pin a concrete model/provider on every task so the dispatcher never
    # falls back to a keyless default that would mark the card blocked.
    _pin_runtime(board, provider_keys, provider, model)
    return SwarmResult(
        root_id=data["root_id"],
        worker_ids=data.get("worker_ids", []),
        verifier_id=data["verifier_id"],
        synthesizer_id=data["synthesizer_id"],
    )


def _pin_runtime(board: str, provider_keys: Optional[dict], provider: Optional[str] = None,
                 model: Optional[str] = None):
    """Set --model/--provider on every squad task via `kanban set-model`.

    Pinning an explicit model/provider is what fixes the 'blocked' issue: the
    dispatcher spawns workers using the profile's default model, which (without
    a key) fails and marks the card blocked. The runtime is resolved deliberately
    (operator default / Demo free / BYOK) — never a silent fallback — and any
    `set-model` failure stops the launch loudly.
    """
    pinned_model, pinned_provider = _resolve_launch_runtime(provider_keys, provider, model)
    tasks = list_tasks(board)
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        args = ["set-model", tid, pinned_model, "--provider", pinned_provider]
        r = _run(args, board=board, provider_keys=provider_keys, capture=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(
                f"kanban set-model failed (rc={r.returncode}) for task {tid}: {detail}"
            )


def launch_from_template(board: str, goal: str, agents: list[str],
                          provider_keys: Optional[dict] = None,
                          provider: Optional[str] = None,
                          model: Optional[str] = None) -> SwarmResult:
    """Launch a squad built from a marketplace template's display-name agents.

    Each name in `agents` is resolved via AGENT_REGISTRY to (profile, skills, role).
    """
    # Fail fast BEFORE any board/worker exists when no runtime is configured.
    _resolve_launch_runtime(provider_keys, provider, model)
    workers, verifier, synthesizer = [], None, None
    for name in agents:
        rec = AGENT_REGISTRY.get(name.strip())
        if not rec:
            continue
        prof, skills, role = rec
        if role == "worker":
            workers.append((prof, name, f"{name} task", skills))
        elif role == "verifier":
            verifier = (prof, name, f"{name} review")
        elif role == "synthesizer":
            synthesizer = (prof, name, f"{name} assemble")
    if not workers:
        raise ValueError("the template contains no valid agents")
    verifier = verifier or VERIFIER
    synthesizer = synthesizer or SYNTHESIZER

    _raise_preflight()
    cleanup_profile_keys()
    # Same verifier-skill provisioning as launch_swarm: template-launched
    # squads still create a verifier via the swarm CLI (hard-coded skill).
    _ensure_verifier_skill()
    worker_args = []
    for prof, disp, title, skills in workers:
        worker_args += ["--worker", f"{prof}:{title}:{skills}"]
    cmd = ["swarm", goal] + worker_args + [
        "--verifier", verifier[0],
        "--synthesizer", synthesizer[0],
        "--created-by", "fluxswarm",
        "--json",
    ]
    r = _run(cmd, board=board, provider_keys=provider_keys)
    if r.returncode != 0:
        raise RuntimeError(f"swarm launch failed: {r.stderr}")
    data = json.loads(r.stdout)
    _pin_runtime(board, provider_keys, provider, model)
    return SwarmResult(
        root_id=data["root_id"],
        worker_ids=data.get("worker_ids", []),
        verifier_id=data["verifier_id"],
        synthesizer_id=data["synthesizer_id"],
    )


# --------------------------------------------------------------------------
# Demo profile: a lightweight, convergent 2-lane board for the free demo.
# The full `hermes kanban swarm` squad (8 agents over the whole project
# workspace) cannot converge within the free-tier runtime budget on a
# throttled 0.1-CPU instance: each worker boots a fat CLI against a large
# context, and slow/heavy workers get reclaimed or crash (memory), so the
# board churns in a recover-and-requeue loop instead of finishing.
# The demo instead builds a DIRECT kanban graph (no swarm CLI) with:
#   * a deliberately TINY auto-seeded workspace (dir: specifier) so each
#     worker's context and memory footprint stay small;
#   * two dependent tasks (Planner -> Builder) — enough to show a real,
#     multi-phase agent run without the squad's width;
#   * a raised per-task --max-runtime so legitimate long turns survive;
#   * the picked (model, provider) pinned AT CREATE TIME (no per-task
#     set-model churn), so a keyless default can never block a card.
DEMO_PLANNER_TITLE = "Plan the demo deliverable"
DEMO_BUILDER_TITLE = "Build the demo deliverable"
_DEMO_PLANNER_ASSIGNEE = "ecc-planner"
_DEMO_BUILDER_ASSIGNEE = "ecc-build-fixer"
DEMO_TASK_MAX_RUNTIME_S = int(os.environ.get("FLUXSWARM_DEMO_TASK_MAX_RUNTIME_S", "1500"))
# Free-tier workers crash after ~40-80s of real work (512MB host). The demo
# tasks are therefore coached to finish in ONE short response: a long turn
# dies mid-run, a short one survives its window.
_DEMO_SPEED_BODY = (
    "\n\nSPEED RULES (strict):\n"
    "- This is a latency demo. Produce the deliverable IMMEDIATELY in one short "
    "response. Do not iterate, do not over-engineer, do not scan the workspace.\n"
    "- Write the smallest possible single file that satisfies the objective.\n"
    "- Do not use any tool that lists or reads directories; act directly.\n")


def demo_workspace_dir(board: str) -> Path:
    """The small, disposable workspace the demo tasks operate in.

    Lives inside the board's own directory (under ``HERMES_HOME/kanban/boards``)
    so the demo TTL sweep (``delete_demo_board``) reclaims it automatically.
    """
    return Path(HERMES_HOME) / "kanban" / "boards" / board / "demo-workspace"


def _seed_demo_workspace(ws: Path, goal: str) -> None:
    """Seed the tiny demo workspace with instructions before workers claim."""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "TASK.md").write_text(
        "FluxSwarm demo — disposable sandbox.\n\n"
        "OBJECTIVE:\n"
        f"{goal}\n\n"
        "RULES:\n"
        "- Work ONLY inside this directory; do not read or modify anything outside it.\n"
        "- Produce one small, self-contained deliverable as files here.\n"
        "- If the objective describes a website/web app/landing page, the deliverable\n"
        "  is a single self-contained index.html (inline CSS+JS, no external deps).\n",
        encoding="utf-8")


def _ensure_board_db(board: str) -> None:
    """Create the board directory and kanban.db with the Hermes schema (no CLI).

    The fat ``hermes boards create`` CLI loads the entire workspace into memory
    and takes 15-30s on a throttled 0.1-CPU free-tier host; running two of
    them (ensure_board + create) inside a 512 MB container triggers an OOM
    that kills the daemon driver thread and leaves the board stuck. Writing
    the schema directly avoids that entirely (sub-second, <1 MB RSS).
    """
    db = _board_db_path(board)
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        return
    c = sqlite3.connect(str(db))
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                created_at INTEGER, started_at INTEGER, completed_at INTEGER,
                last_heartbeat_at INTEGER, result TEXT, worker_pid INTEGER
            );
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
                payload TEXT, created_at INTEGER
            );
        """)
        c.commit()
    finally:
        c.close()


def _ensure_demo_board_db(board: str) -> None:
    """Alias for the demo path: same direct-DB board creation, no CLI."""
    _ensure_board_db(board)


def _demo_insert_task(conn: sqlite3.Connection, board: str, *, task_id: str,
                      title: str, assignee: str, status: str,
                      created_by: str = "fluxswarm") -> None:
    """Insert one task + its 'created' event directly into the board DB."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,created_at) VALUES (?,?,?,?,?)",
        (task_id, title, assignee, status, now))
    conn.execute(
        "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        (task_id, "created", json.dumps({"created_by": created_by}), now))


def launch_demo_profile(board: str, goal: str, provider: Optional[str] = None,
                        model: Optional[str] = None) -> dict:
    """Build the 2-lane demo graph directly in an existing board.

    Returns ``{"planner_id", "builder_id", "workspace"}``. Raises
    RuntimeError on any step (fail fast BEFORE the board is dispatched).

    This writes tasks directly into kanban.db (no ``hermes kanban create``
    CLI subprocess) so the demo path never loads the fat Hermes workspace
    into memory on the throttled free-tier host.
    """
    ws = demo_workspace_dir(board)
    _seed_demo_workspace(ws, goal)
    _ensure_demo_board_db(board)

    planner_id = f"t_{secrets.token_hex(4)}"
    builder_id = f"t_{secrets.token_hex(4)}"

    db = _board_db_path(board)
    c = sqlite3.connect(str(db))
    try:
        _demo_insert_task(c, board, task_id=planner_id,
                          title=DEMO_PLANNER_TITLE, assignee=_DEMO_PLANNER_ASSIGNEE,
                          status="ready")
        _demo_insert_task(c, board, task_id=builder_id,
                          title=DEMO_BUILDER_TITLE, assignee=_DEMO_BUILDER_ASSIGNEE,
                          status="todo")
        c.commit()
    finally:
        c.close()

    # P4/2: mirror the fresh demo board (best-effort, never blocks).
    board_store.snapshot_board(board, kind="demo")
    return {"planner_id": planner_id, "builder_id": builder_id, "workspace": str(ws)}


def project_workspace_dir(board: str) -> Path:
    """The per-board workspaces root the real project lanes write into.

    Lives at ``HERMES_HOME/kanban/boards/<board>/workspaces`` — the exact root
    ``read_workspace`` serves to the user, so every artifact a thin project
    lane produces lands directly in the downloadable project result.
    """
    return Path(HERMES_HOME) / "kanban" / "boards" / board / "workspaces"


# ---- Project Files: customer uploads + the edit-after-build loop ----------
# A customer uploads files/images into their board's native ``attachments``
# store; each file is ALSO mirrored into the project workspace (``uploads/``)
# so the swarm can read it, the preview can render it, and the export bundles
# it with the generated deliverable. Everything is validated before touching
# the filesystem: slug and filename are both strict-charset checked and the
# resolved paths stay inside the board dir.
_ATTACH_MAX_BYTES = int(os.environ.get("FLUXSWARM_ATTACH_MAX_BYTES", str(50 * 1024 * 1024)))
_ATTACH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,159}$")


def project_attachments_dir(board: str) -> Path:
    """The per-board native attachments root (``<board>/attachments``)."""
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    return Path(HERMES_HOME) / "kanban" / "boards" / board / "attachments"


def _safe_attachment_name(name: str) -> str:
    """Normalize an uploaded filename to a path-safe basename or raise."""
    base = Path(name or "").name  # strip any path components first
    if not base or len(base) > 160 or not _ATTACH_NAME_RE.match(base):
        raise ValueError(f"unsafe attachment name: {name!r}")
    return base


def save_attachment(board: str, filename: str, data: bytes) -> dict:
    """Persist one customer-uploaded file for *board*.

    Writes into the board's native ``attachments`` store and mirrors a copy
    into ``workspaces/uploads/`` so the swarm, preview and export all see it.
    Raises ValueError on unsafe slug/name, empty payload, or size-cap breach.
    """
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    name = _safe_attachment_name(filename)
    if not data:
        raise ValueError("empty upload")
    if len(data) > _ATTACH_MAX_BYTES:
        raise ValueError("attachment exceeds the size cap")
    root = project_attachments_dir(board)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    ws = project_workspace_dir(board)
    try:
        (ws / "uploads").mkdir(parents=True, exist_ok=True)
        (ws / "uploads" / name).write_bytes(data)
    except OSError:  # pragma: no cover - defensive
        pass
    return {"name": name, "size": len(data)}


def list_project_files(board: str) -> dict:
    """Project Files browser: the attachment store + the generated workspace
    tree (name, size, mtime). Best-effort and never raises."""
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    att = project_attachments_dir(board)
    ws = project_workspace_dir(board)
    out: dict = {"attachments": [], "workspace": []}
    for root, key in ((att, "attachments"), (ws, "workspace")):
        try:
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                st = p.stat()
                out[key].append({
                    "name": str(p.relative_to(root)).replace("\\", "/"),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
        except OSError:
            continue
        except Exception:
            continue
    return out


def delete_project_file(board: str, name: str) -> bool:
    """Remove one customer-uploaded file (both copies). Returns True when at
    least one copy was removed."""
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    safe = _safe_attachment_name(name)
    removed = False
    try:
        p = project_attachments_dir(board) / safe
        if p.exists():
            p.unlink()
            removed = True
    except OSError:
        pass
    try:
        p = project_workspace_dir(board) / "uploads" / safe
        if p.exists():
            p.unlink()
            removed = True
    except OSError:
        pass
    return removed


def unseal_board(board: str, reason: str = "reopened for edits") -> dict:
    """Reopen an operator-sealed board so the customer can edit it and
    re-dispatch (the edit-after-build loop).

    Drops the ``board.sealed`` marker and re-arms every agent lane that was
    parked by the seal (done/blocked -> ready) so a re-dispatch rebuilds the
    project against the updated workspace. Best-effort and idempotent: per-row
    failures degrade the report and never raise; task-state recovery is
    column-tolerant (only touches columns this board's schema actually has).
    """
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    report: dict = {"marker": False, "rearmed": 0, "errors": []}
    board_dir = Path(HERMES_HOME) / "kanban" / "boards" / board
    marker = board_dir / SEAL_MARKER_NAME
    try:
        if marker.exists():
            marker.unlink()
            report["marker"] = True
    except OSError as exc:
        report["errors"].append(f"marker: {exc}")
    db_path = board_dir / "kanban.db"
    if db_path.exists():
        try:
            c = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
                clauses = ["status='ready'"]
                for col in ("worker_pid", "claim_lock", "claim_expires"):
                    if col in cols:
                        clauses.append(f"{col}=NULL")
                cur = c.execute(
                    "UPDATE tasks SET %s WHERE status IN ('done','blocked') "
                    "AND assignee != 'fluxswarm'" % ", ".join(clauses))
                report["rearmed"] = cur.rowcount
                c.commit()
            finally:
                c.close()
        except sqlite3.Error as exc:
            report["errors"].append(f"db: {exc}")
    try:
        board_store.snapshot_board(board)
    except Exception:
        pass
    return report


def _seed_attachments_into_workspace(board: str, ws: Path) -> None:
    """Copy the board's native attachments into the seeded workspace and note
    them in TASK.md so every fresh(e) launch surfaces the uploads. Best-effort."""
    try:
        src = project_attachments_dir(board)
        if not src.is_dir():
            return
        (ws / "uploads").mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for p in sorted(src.iterdir()):
            if p.is_file():
                try:
                    shutil.copy2(p, ws / "uploads" / p.name)
                    names.append(p.name)
                except OSError:
                    pass
        if names and (ws / "TASK.md").exists():
            with (ws / "TASK.md").open("a", encoding="utf-8") as fh:
                fh.write("\n\nUSER UPLOADS (available under uploads/):\n- "
                         + "\n- ".join(names) + "\n")
    except OSError:
        pass
    except Exception:
        pass


def _seed_project_workspace(ws: Path, goal: str) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "TASK.md").write_text(
        "FluxSwarm project deliverable — thin squad workspace.\n\n"
        "OBJECTIVE:\n"
        f"{goal}\n\n"
        "The eight lanes below each produce a REAL artifact in this directory:\n"
        "PLAN.md, ARCHITECTURE.md, Dockerfile, tests/test_app.py, REVIEW.md,\n"
        "DESIGN.md, AUDIT.md, and the final deliverable (README.md, the project\n"
        "code file, or a single self-contained index.html when the objective is a\n"
        "website/web app/landing page).\n"
        "Confirm the deliverable actually satisfies the objective.\n",
        encoding="utf-8")


# Thin 8-lane project graph: (profile, UI role, task title, status).
# Mirrors the fat squad (SQUAD + VERIFIER + SYNTHESIZER) + the two extended
# members (Designer, Auditor) so the board renders the SAME 8-agent swarm the
# paid path shows.
def _thin_project_lanes() -> list[tuple[str, str, str, str]]:
    return [
        (SQUAD[0][0], "Planner",    SQUAD[0][2], "ready"),
        (SQUAD[1][0], "Architect",  SQUAD[1][2], "todo"),
        (SQUAD[2][0], "DevOps",     SQUAD[2][2], "todo"),
        (SQUAD[3][0], "TDD",        SQUAD[3][2], "todo"),
        (VERIFIER[0], "Reviewer",   VERIFIER[2], "todo"),
        (DESIGNER[0], "Designer",   DESIGNER[2], "todo"),
        (SYNTHESIZER[0], "Builder", SYNTHESIZER[2], "todo"),
        (AUDITOR[0], "Auditor",     AUDITOR[2], "todo"),
    ]


def launch_project_thin(board: str, goal: str, provider: Optional[str] = None,
                        model: Optional[str] = None,
                        custom_agents: Optional[list[dict]] = None) -> dict:
    """Build the 8-lane project squad directly in kanban.db (no CLI).

    Same honesty contract as ``launch_swarm`` but for small-memory hosts: the
    full ``hermes swarm`` CLI subprocess loads the entire workspace and OOMs a
    512 MB container. This writes the real squad tasks + seeds the real
    workspace (sub-second, <1 MB RSS); the thin project driver then executes
    each lane with a real provider completion.

    ``custom_agents`` (P4): optional list of user-defined agents
    ``{"id", "name", "objective", "skills"}``. Each becomes one extra queued
    lane on the board (assignee ``ca-<id>``) executed by the thin driver.

    Returns a SwarmResult-shaped dict (``root_id``/``worker_ids``/
    ``verifier_id``/``synthesizer_id``) plus ``planner_id`` and ``workspace``.
    """
    ws = project_workspace_dir(board)
    _seed_project_workspace(ws, goal)
    _seed_attachments_into_workspace(board, ws)
    _ensure_board_db(board)
    lanes = _thin_project_lanes()
    ids: list[str] = []
    plan_id = ""
    verifier_id = ""
    synth_id = ""
    c = sqlite3.connect(str(_board_db_path(board)))
    try:
        for prof, _role, title, status in lanes:
            tid = f"t_{secrets.token_hex(4)}"
            _demo_insert_task(c, board, task_id=tid, title=title,
                              assignee=prof, status=status)
            ids.append(tid)
            if prof == SQUAD[0][0]:
                plan_id = tid
            elif prof == VERIFIER[0]:
                verifier_id = tid
            elif prof == SYNTHESIZER[0]:
                synth_id = tid
        for agent in (custom_agents or []):
            aid = str(agent.get("id", "")).strip()
            name = (agent.get("name") or "Custom agent").strip() or "Custom agent"
            tid = f"t_{secrets.token_hex(4)}"
            _demo_insert_task(c, board, task_id=tid,
                              title=f"{name} (custom)",
                              assignee=f"ca-{aid}" if aid else f"ca-{secrets.token_hex(3)}",
                              status="todo")
            ids.append(tid)
        c.commit()
    finally:
        c.close()
    # P4/2: mirror the freshly-built board into Postgres so a restart before
    # the first lane runs still shows the squad (best-effort, never blocks).
    board_store.snapshot_board(board, kind="project")
    return {
        "root_id": None,
        "worker_ids": [ids[i] for i in range(len(lanes))],
        "verifier_id": verifier_id,
        "synthesizer_id": synth_id,
        "planner_id": plan_id,
        "workspace": str(ws),
    }


def _emit_working(board_display_log: str, task_id: str, note: str, at: float | None = None) -> None:
    """Best-effort display-only 'Working…' event for the thin demo executor.

    A real worker emits heartbeats (~60s) which the log collapses to a single
    "Working…" row; the thin executor emits one at its true start so the UI
    timeline shows activity. Failures here never raise.
    """
    _insert_event(board_display_log, task_id, "heartbeat", note, at)


def _emit_error(board_display_log: str, task_id: str, reason: str, at: float | None = None) -> None:
    """Best-effort 'Demo error: …' event so a failed lane is VISIBLE on the
    board timeline instead of silently stuck. Failures here never raise."""
    _insert_event(board_display_log, task_id, "error", reason, at)


def _demo_fail_lane(board: str, task_id: str, reason: str) -> None:
    """Mark one demo lane done-with-error: writes the error row AND completes
    the task as terminal so the board resolves instead of parking 'running'.
    Best-effort: never raises."""
    _emit_error(board, task_id, reason)
    db = _board_db_path(board)
    try:
        c = sqlite3.connect(str(db))
        try:
            now = int(time.time())
            c.execute("UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                      (now, f"ERROR: {reason[:200]}", task_id))
            c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                      (task_id, "completed", json.dumps({"summary": f"ERROR: {reason[:200]}"}), now))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def _insert_event(board_display_log: str, task_id: str, kind: str, note: str, at: float | None = None) -> None:
    db = _board_db_path(board_display_log)
    try:
        if not db.exists():
            return
        c = sqlite3.connect(str(db))
        try:
            c.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, kind,
                 json.dumps({"note" if kind == "heartbeat" else "message": str(note)[:400]}),
                 float(at if at is not None else time.time())),
            )
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


_FENCE_LINE_RE = re.compile(r"^```[a-zA-Z0-9+-]*\s*$")


def _strip_code_fences(text: str) -> str:
    """Remove a single wrapping markdown fence the model sometimes adds (the
    prompts forbid it, but a lenient completion may wrap HTML/code anyway)."""
    lines = text.splitlines()
    if len(lines) >= 2 and _FENCE_LINE_RE.match(lines[0].strip()) \
            and _FENCE_LINE_RE.match(lines[-1].strip()):
        return "\n".join(lines[1:-1]).strip("\n")
    return text


def thin_execute(board: str, task_id: str, workspace: str, provider: str,
                 model: str, prompt: str, objective: str = "",
                 artifact_name: str | None = None, api_key: str | None = None,
                 max_tokens: int = 400) -> dict:
    """Execute one thin lane with a bounded in-process executor.

    A full Hermes worker crashes ~40-80s into real work on the 512MB free host
    (measured: ``recovered reason=crash`` on every attempt, regardless of task
    size), so the free demo CANNOT converge with real agents. The thin executor
    still runs against the REAL board and REAL provider:

      * direct DB claim  -> real ``claimed`` ("Started") event,
      * a real completion call to the resolved provider (demo_llm),
      * a real artifact file written into the task workspace,
      * direct DB attach -> real "Produced" event,
      * direct DB complete -> real result summary.

    ``api_key``/``max_tokens`` let the project path honor a user's BYOK key (or
    a larger artifact budget like a Dockerfile) without touching the env or
    disk. All board writes go directly into kanban.db (no CLI subprocess) so
    the thin path never loads the fat Hermes workspace into memory.
    """
    start = time.time()
    db = _board_db_path(board)

    def _claim():
        now = int(start)
        c = sqlite3.connect(str(db))
        try:
            cur = c.execute("UPDATE tasks SET status='running', started_at=?, worker_pid=0 WHERE id=?",
                            (now, task_id))
            if cur.rowcount == 0:
                raise RuntimeError(f"demo task {task_id} not found on board")
            c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                      (task_id, "claimed", None, now))
            c.commit()
        finally:
            c.close()

    def _attach(name: str):
        c = sqlite3.connect(str(db))
        try:
            c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                      (task_id, "attached", json.dumps({"filename": name}), int(time.time())))
            c.commit()
        finally:
            c.close()

    def _complete(summary: str):
        c = sqlite3.connect(str(db))
        try:
            now = int(time.time())
            c.execute("UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                      (now, summary, task_id))
            c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                      (task_id, "completed", json.dumps({"summary": summary}), now))
            c.commit()
        finally:
            c.close()

    try:
        _claim()
        _emit_working(board, task_id, "thin worker: provider completion in flight")
        text = _strip_code_fences(demo_llm.completion(
            provider, model, prompt, max_tokens=max_tokens, api_key=api_key))
        name = artifact_name or demo_llm.deliverable_filename(objective or "")
        ws = Path(workspace)
        ws.mkdir(parents=True, exist_ok=True)
        artifact = ws / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(text, encoding="utf-8")
        _emit_working(board, task_id, "artifact written; attaching")
        _attach(name)
        summary = (text.strip().splitlines() or [name])[0][:160]
        _complete(summary)
    except BaseException as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        _demo_fail_lane(board, task_id, reason)
        raise
    finally:
        # P4/2: mirror the board AFTER the lane lands (artifact + events too),
        # so a mid-launch restart keeps real progress, not an empty board.
        # Fires on success AND on failure (the failed lane is mirrored as-is).
        board_store.snapshot_board(board)
    return {"result": summary, "artifact": str(artifact), "ok": True,
            "elapsed_s": round(time.time() - start, 1)}


def _board_activity_sig(board: str) -> tuple:
    """Monotonic worker-activity fingerprint for the board, read from ``kanban.db``.

    A healthy worker keeps calling the LLM and using tools while a task's
    ``state`` may not have changed yet (the classic false-stall: single planner
    doing reasoning + ``kanban_show`` before ever completing). The state-only
    signature used to flag that healthy worker as ``no_progress``. This helper
    adds a real activity signal — the highest worker heartbeat timestamp and the
    task-event stream (count + last created_at) — so the stall detector only
    trips when a worker is BOTH stuck in an unchanged state AND producing no
    heartbeats / task-events.

    Returns () when the board DB is unavailable (synthetic/offline/unit-test
    boards), in which case the caller falls back to the state-only signature and
    the pre-existing stall semantics are preserved.
    """
    db = Path(HERMES_HOME) / "kanban" / "boards" / board / "kanban.db"
    try:
        if not db.exists():
            return ()
        c = sqlite3.connect(str(db))
        try:
            max_hb = c.execute(
                "SELECT MAX(COALESCE(last_heartbeat_at, 0)) FROM tasks"
            ).fetchone()[0] or 0
            evt_count = c.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0] or 0
            evt_max = c.execute(
                "SELECT MAX(COALESCE(created_at, 0)) FROM task_events"
            ).fetchone()[0] or 0
            return (int(max_hb), int(evt_count), int(evt_max))
        finally:
            c.close()
    except Exception:
        return ()


# ---------------------------------------------------------------------------
# Process-tree cleanup for orphaned Hermes workers.
#
# Windows:  Win32 snapshot (CreateToolhelp32Snapshot) enumerates descendants
#           and TerminateProcess hard-kills them, children deepest-first.
# POSIX:    `ps --ppid` scoped descendant enumeration + graceful SIGTERM (with
#           a bounded wait) escalating to SIGKILL, children deepest-first.
# Both platforms ONLY ever touch the PID supplied by the caller (the current
# board's tracked worker set) and that PID's descendants; unrelated processes
# are never enumerated or killed.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt

    _STILL_ACTIVE = 259
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _PROCESS_QUERY_INFORMATION = 0x0400

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", _wt.DWORD),
            ("cntUsage", _wt.DWORD),
            ("th32ProcessID", _wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", _wt.DWORD),
            ("cntThreads", _wt.DWORD),
            ("th32ParentProcessID", _wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", _wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def _get_child_pids_win32(pid: int) -> list[int]:
        """Return direct child PIDs of *pid* using a Win32 process snapshot."""
        children: list[int] = []
        snap = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snap == _wt.HANDLE(-1).value:
            return children
        try:
            pe = _PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            if _kernel32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    if pe.th32ParentProcessID == pid:
                        children.append(pe.th32ProcessID)
                    if not _kernel32.Process32Next(snap, ctypes.byref(pe)):
                        break
        finally:
            _kernel32.CloseHandle(snap)
        return children

    def _sigterm(pid: int, timeout_s: float = 3.0) -> bool:
        """Best-effort graceful termination; returns True if process exited."""
        try:
            h = _kernel32.OpenProcess(
                _PROCESS_TERMINATE | _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                False, pid,
            )
            if not h:
                return False
            try:
                _kernel32.TerminateProcess(h, 1)
                wait_ms = int(timeout_s * 1000)
                _kernel32.WaitForSingleObject(h, wait_ms)
                ec = _wt.DWORD()
                _kernel32.GetExitCodeProcess(h, ctypes.byref(ec))
                return ec.value != _STILL_ACTIVE
            finally:
                _kernel32.CloseHandle(h)
        except Exception:
            return False

    def _get_child_pids(pid: int) -> list[int]:
        """Return direct child PIDs for the current platform."""
        return _get_child_pids_win32(pid)

    def _force_kill(pid: int) -> None:
        """Hard-kill fallback after a failed graceful terminate (Windows)."""
        try:
            h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
            if h:
                try:
                    _kernel32.TerminateProcess(h, 1)
                finally:
                    _kernel32.CloseHandle(h)
        except Exception:
            pass

else:
    import signal as _signal

    def _get_child_pids_posix(pid: int) -> list[int]:
        """Return direct child PIDs of *pid* via `ps --ppid` (POSIX, no new deps).

        Only enumerates descendants of the given PID — never a global scan —
        so it can never turn up unrelated processes to terminate.
        """
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=", "--ppid", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return []
        children: list[int] = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                children.append(int(line))
        return children

    def _get_child_pids(pid: int) -> list[int]:
        """Return direct child PIDs for the current platform."""
        return _get_child_pids_posix(pid)

    def _pid_alive(pid: int) -> bool:
        """POSIX existence probe (signal 0). ESRCH => the process is gone."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True  # e.g. EPERM: stay conservative and assume it exists
        except Exception:
            return True

    def _sigterm(pid: int, timeout_s: float = 3.0) -> bool:
        """POSIX SIGTERM then a bounded wait; True if the process exited."""
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            return True  # already gone
        except OSError:
            return True  # wrong-user/zombie: treat as done to avoid a hang
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.05)
        return not _pid_alive(pid)

    def _force_kill(pid: int) -> None:
        """POSIX SIGKILL fallback."""
        try:
            os.kill(pid, _signal.SIGKILL)
        except Exception:
            pass


def kill_process_tree(pid: int, grace_s: float = 3.0) -> None:
    """Terminate a process and its descendants.

    1. Recursively find direct children for the current platform.
    2. Kill children deepest-first (leaf processes first).
    3. Terminate the root (TerminateProcess on Windows, graceful SIGTERM on
       POSIX); escalate to a hard kill (TerminateProcess / SIGKILL) if it does
       not exit within *grace_s*.

    Only the supplied PID and its descendants are ever touched; unrelated
    processes are never enumerated or terminated. Semantics are idempotent —
    calling again on an already-dead or recycled-free PID is a safe no-op.
    """
    if pid <= 0:
        return
    try:
        children = _get_child_pids(pid)
    except Exception:
        children = []
    for child in children:
        try:
            kill_process_tree(child, grace_s=grace_s)
        except Exception:
            pass
    try:
        if _sigterm(pid, timeout_s=grace_s):
            return
    except Exception:
        pass
    try:
        _force_kill(pid)
    except Exception:
        pass


def read_worker_pids(board: str) -> list[int]:
    """Read worker process IDs for *board* from kanban.db (non-destructive)."""
    db = Path(HERMES_HOME) / "kanban" / "boards" / board / "kanban.db"
    try:
        if not db.exists():
            return []
        c = sqlite3.connect(str(db))
        try:
            rows = c.execute(
                "SELECT worker_pid FROM tasks "
                "WHERE worker_pid IS NOT NULL AND worker_pid > 0 "
                "AND status IN ('running', 'ready', 'todo')",
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            c.close()
    except Exception:
        return []


def _cleanup_board_workers(board: str) -> None:
    """Terminate Hermes worker processes owned by *board*.

    Called on dispatch timeout / stall / exception to prevent orphaned
    processes from accumulating.  Reads worker PIDs from kanban.db (the
 authoritative source managed by the Hermes CLI) and kills each
 process tree.  Already-dead processes are ignored safely.
    """
    try:
        pids = read_worker_pids(board)
    except Exception:
        return
    for pid in pids:
        try:
            kill_process_tree(pid)
        except Exception:
            pass


SEAL_MARKER_NAME = "board.sealed"


def seal_board(board: str, reason: str = "launch finalized") -> dict:
    """Force an abandoned / refunded / errored board terminal so it stops
    consuming the host-level kanban concurrency budget.

    Hermes dispatch accounting counts ``running`` tasks across EVERY board
    against the memory-derived host cap (``kanban.max_in_progress``). A launch
    that ends stuck / timed-out / errored leaves its worker processes alive and
    its tasks parked in ``running`` unless someone reclaims them. Without this,
    the reconciliation reaper re-dispatchs such boards on every sweep — the
    dead workers keep coming back, the board never reaches a terminal state,
    and the ``running`` corpses hold the cap forever: every NEW launch then
    lands in "waiting for dependency" and gets refunded as ``no_progress``.

    Sealing makes the board terminal and drives its ``running`` count to zero:

      1. kill every worker process tree recorded for the board;
      2. flip every non-terminal AGENT task to ``blocked`` (claim/pid cleared);
      3. drop a durable ``board.sealed`` marker so the reconciliation reaper
         skips the board on later sweeps (it is operator-final, not a
         transient provider blip that deserves a retry).

    Best-effort and idempotent: per-row failures degrade the seal and never
    raise (the caller always lands the launch bookkeeping first). Returns a
    small report dict for auditing.
    """
    report: dict = {"killed": 0, "blocked": 0, "errors": []}
    for pid in read_worker_pids(board):
        try:
            kill_process_tree(pid)
            report["killed"] += 1
        except Exception as exc:
            report["errors"].append(f"kill {pid}: {exc}")
    board_dir = Path(HERMES_HOME) / "kanban" / "boards" / board
    db_path = board_dir / "kanban.db"
    if db_path.exists():
        try:
            c = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
                set_clauses = ["status=?"]
                values_suffix = ["blocked"]
                if "worker_pid" in cols:
                    set_clauses.append("worker_pid=?"); values_suffix.append(None)
                if "claim_lock" in cols:
                    set_clauses.append("claim_lock=?"); values_suffix.append(None)
                if "claim_expires" in cols:
                    set_clauses.append("claim_expires=?"); values_suffix.append(None)
                stmt = "UPDATE tasks SET %s WHERE id=?" % ", ".join(set_clauses)
                rows = c.execute(
                    "SELECT id FROM tasks WHERE status NOT IN ('done', 'blocked')"
                ).fetchall()
                for (tid,) in rows:
                    try:
                        c.execute(stmt, tuple(values_suffix) + (tid,))
                        report["blocked"] += 1
                    except Exception as exc:
                        report["errors"].append(f"block {tid}: {exc}")
                c.commit()
            finally:
                c.close()
        except Exception as exc:
            report["errors"].append(f"db: {exc}")
    try:
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / SEAL_MARKER_NAME).touch()
    except Exception as exc:
        report["errors"].append(f"marker: {exc}")
    # P4/2: mirror the terminal seal so a restart doesn't resurrect a closed board.
    board_store.snapshot_board(board)
    return report


def board_is_sealed(board: str) -> bool:
    """True when *board* carries the durable seal marker (operator-final)."""
    try:
        return (Path(HERMES_HOME) / "kanban" / "boards" / board / SEAL_MARKER_NAME).exists()
    except Exception:
        return False


def dispatch(board: str, max_spawn: int | None = None, dry_run: bool = False,
             provider_keys: Optional[dict] = None, blocking: bool = True,
             timeout_s: int = 600, stall_passes: int = 4,
             min_wait_s: int = 60) -> dict:
    """Run the dispatcher. If blocking, poll until terminal state or timeout.

    ``max_spawn`` defaults to the memory-derived host ceiling
    (``MAX_IN_PROGRESS``) so a caller that forgets the cap never over-commits
    RAM (the pre-callers conceptually kept 8). Plan/reaper callers pass their
    own smaller caps; the Hermes dispatcher applies its own lane limits too.

    Returns a dict that always carries ``outcome`` so the caller can tell a
    launch that converged from one that was cut short by the bounded wall-clock
    window (provider/worker stall). ``timed_out`` is True only when we left the
    loop with non-terminal tasks still pending. ``stuck_tasks`` lists the tasks
    that were still ``running``/``queued`` when we stopped (i.e. what would
    otherwise be left stranded).

    The blocking window is deliberately bounded (``timeout_s``) — a provider
    outage must not let the driver wait for many hours. Hermes's own reclamation
    loop keeps re-dispatching inside this window; when the window expires we
    report the residual state truthfully instead of silently dripping a
    forever-stuck board.

    ``stall_passes`` (default 4) triggers an early no-progress break: if the
    board's task states stop changing across several consecutive passes while
    work is still pending, the swarm is not converging (provider/worker stuck),
    so the driver stops well before ``timeout_s`` instead of waiting many hours.
    ``min_wait_s`` is a floor so a healthy multi-wave swarm that is still
    advancing is never cut short by the stall detector in its opening moments.
    """
    args = ["dispatch"]
    if dry_run:
        args.append("--dry-run")
    if max_spawn is None:
        max_spawn = MAX_IN_PROGRESS
    if max_spawn:
        args += ["--max", str(max_spawn)]
    r = _run(args, board=board, provider_keys=provider_keys)
    if r.returncode != 0:
        raise RuntimeError(f"dispatch failed: {r.stderr}")
    try:
        first = json.loads(r.stdout)
    except json.JSONDecodeError:
        first = {"raw": r.stdout.strip()}

    # Track whether worker cleanup is needed.  Set to False only on successful
    # convergence; the finally block then skips cleanup.  On stall, timeout, or
    # any exception the flag stays True and cleanup runs.
    _needs_cleanup = True

    try:
        if blocking:
            # Keep dispatching in passes until everything is done/blocked or we
            # conclude the board is stuck (no forward progress).
            deadline = time.time() + timeout_s
            start = time.time()
            last_sig = None
            unchanged = 0
            while time.time() < deadline:
                tasks = list_tasks(board)
                states = [t.get("state") for t in tasks]
                # Robust progress signature: worker ACTIVITY (heartbeat / task events)
                # combined with task state. A healthy worker that is steadily calling
                # the LLM and using tools advances heartbeats/events even while no
                # task state has changed yet, so it is NOT misclassified as stalled.
                # A genuinely stalled worker (provider/worker hang) advances neither,
                # so it is still caught after stall_passes unchanged passes.
                activity = _board_activity_sig(board)
                # Heartbeat freshness is the liveness gate. Heartbeats land roughly
                # every ~60s — far slower than the ~10-15s dispatcher pass cadence —
                # so the raw activity TUPLE (which only changes on a new heartbeat)
                # must NOT be the stall signal by itself: that would falsely stall a
                # healthy long-running worker in the gap between two heartbeats.
                # Instead, a worker with a RECENT heartbeat is demonstrably alive and
                # progressing (mid-LLM/tool work), so we only declare a stall once the
                # board's heartbeat has actually gone STALE (> heartbeat_grace) while
                # task states stay unchanged for stall_passes passes. `()` (DB
                # unavailable) counts as stale so the detector still works without
                # board DB access (and in unit tests).
                heartbeat_grace = 180
                stale = (activity == () or (time.time() - activity[0]) > heartbeat_grace)
                sig = (activity, tuple(sorted(states)))
                if not states or all(s in ("done", "blocked") for s in states):
                    _needs_cleanup = False
                    first["terminal"] = True
                    first["timed_out"] = False
                    first["outcome"] = "ok"
                    first["stuck_tasks"] = []
                    return first
                if sig == last_sig:
                    unchanged += 1
                else:
                    unchanged = 0
                last_sig = sig
                time.sleep(8)
                rr = _run(["dispatch", "--max", str(max_spawn)], board=board,
                          provider_keys=provider_keys)
                # Early no-progress break: the same non-terminal states for several
                # consecutive passes means the swarm is stuck (provider/worker hang),
                # not converging. Give a healthy launch a grace floor so its first
                # waves have time to start before we ever evaluate the stall.
                if (unchanged >= stall_passes
                        and time.time() - start >= min_wait_s
                        and stale):
                    _cleanup_board_workers(board)
                    first["terminal"] = False
                    first["timed_out"] = True
                    first["stall"] = True
                    first["stall_passes"] = unchanged
                    first["outcome"] = "stuck"
                    first["stuck_tasks"] = [t for t in tasks
                                            if t.get("state") in ("running", "queued")]
                    first["stuck_run_count"] = sum(
                        1 for t in tasks if t.get("state") == "running")
                    first["done_count"] = sum(1 for t in tasks if t.get("state") == "done")
                    first["deadline_s"] = timeout_s
                    first["early"] = True
                    _needs_cleanup = False
                    return first
            # Wall-clock window expired with non-terminal work still pending.
            _cleanup_board_workers(board)
            tasks = list_tasks(board)
            stuck = [t for t in tasks if t.get("state") in ("running", "queued")]
            first["terminal"] = False
            first["timed_out"] = True
            first["outcome"] = "stuck"
            first["stuck_tasks"] = stuck
            first["stuck_run_count"] = sum(
                1 for t in stuck if t.get("state") == "running")
            first["done_count"] = sum(1 for t in list_tasks(board) if t.get("state") == "done")
            first["deadline_s"] = timeout_s
            _needs_cleanup = False
        else:
            # Non-blocking single pass: we deliberately do NOT stamp `terminal` —
            # the caller must treat a non-blocking dispatch as an in-flight launch
            # that may still have work pending (see pragmatics/contract pinned by
            # test_dispatch_completion.py). Provide an outcome label for callers
            # that want one, but leave terminality untouched.
            first.setdefault("timed_out", False)
            first.setdefault("outcome", "pending")
            _needs_cleanup = False
        return first
    finally:
        if _needs_cleanup:
            _cleanup_board_workers(board)


def _list_tasks_from_db(board: str) -> list[dict] | None:
    """Read the board's ``kanban.db`` directly (no CLI subprocess) so live reads
    stay milliseconds even on a throttled free-tier instance; the shape matches
    ``hermes kanban list --json`` post-normalization. Returns None when the DB
    is missing or its schema is unreadable, so the caller falls back to the CLI."""
    db = _board_db_path(board)
    try:
        if not db.exists():
            return None
        c = sqlite3.connect(str(db))
    except Exception:
        return None
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
        want = ("id", "title", "assignee", "status", "state", "created_at",
                "started_at", "completed_at", "last_heartbeat_at", "result",
                "worker_pid", "role_name")
        pick = [k for k in want if k in cols]
        if not {"id", "assignee", "status"}.issubset(pick):
            return None
        rows = c.execute(
            "SELECT %s FROM tasks ORDER BY created_at" % ", ".join(pick)
        ).fetchall()
        norm = {"done": "done", "running": "running", "ready": "queued",
                "todo": "queued", "blocked": "blocked"}
        out = []
        for row in rows:
            d = dict(zip(pick, row))
            raw = d.get("status") or d.get("state") or "unknown"
            t = {
                "id": d.get("id"), "title": d.get("title"),
                "assignee": d.get("assignee"),
                "status": d.get("status") or d.get("state"),
                "created_at": d.get("created_at"),
                "started_at": d.get("started_at"),
                "completed_at": d.get("completed_at"),
                "last_heartbeat_at": d.get("last_heartbeat_at"),
                "result": d.get("result"), "worker_pid": d.get("worker_pid"),
            }
            if "role_name" in d:
                t["role_name"] = d.get("role_name")
            t["state"] = norm.get(raw, raw)
            a = (t.get("assignee") or "").strip()
            if a.startswith("ecc-"):
                a = a[len("ecc-"):]
            if ":" in a:
                pre, post = a.split(":", 1)
                if pre.startswith("ecc-"):
                    a = pre[len("ecc-"):] + ":" + post
            t["assignee_display"] = a
            out.append(t)
        return out
    except Exception:
        return None
    finally:
        c.close()


def list_tasks(board: str) -> list[dict]:
    fast = _list_tasks_from_db(board)
    if fast is not None:
        for t in fast:
            _attach_activity(board, t)
        return fast
    r = _run(["list", "--json"], board=board)
    if r.returncode != 0:
        raise RuntimeError(f"list failed: {r.stderr}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    norm = {"done": "done", "running": "running", "ready": "queued",
            "todo": "queued", "blocked": "blocked"}
    for t in data:
        t["state"] = norm.get(t.get("status", ""), t.get("status", "unknown"))
        # Strip the ecc- prefix from assignee for display.
        a = t.get("assignee", "")
        if a.startswith("ecc-"):
            a = a[len("ecc-"):]
        # verifier/synthesizer show as "ecc-reviewer:..." -> keep readable
        if ":" in a:
            pre, post = a.split(":", 1)
            if pre.startswith("ecc-"):
                a = pre[len("ecc-"):] + ":" + post
        t["assignee_display"] = a
    # Enrich every task with live agent activity (issue-2 contract). This is
    # the single funnel for both the REST tasks endpoint and the /ws poll loop,
    # so activity propagates everywhere automatically and stays in the same
    # authoritative shape.
    for t in data:
        _attach_activity(board, t)
    return data


def board_has_completed_work(board: str) -> bool:
    """True when any AGENT task on the board reached ``done`` (meaningful work).

    Used for credit reconciliation: a launch that never completed a real agent
    task (e.g. the provider fails before any agent finishes) produced no work
    and is eligible for a credit refund; one that finished at least one agent
    task consumed real work and must not be refunded.

    The swarm ROOT planning card (assignee ``fluxswarm``) is auto-completed
    immediately as the shared blackboard/anchor — it represents no agent work
    and is deliberately excluded, so a board where every agent failed still
    counts as "no completed work" and qualifies for the refund.
    """
    try:
        tasks = list_tasks(board)
    except Exception:
        return False
    for t in tasks:
        if t.get("state") == "done":
            assignee = (t.get("assignee") or "").strip().lower()
            if assignee and assignee != "fluxswarm":
                return True
    return False


def board_has_unfinished_work(board: str) -> bool:
    """True when any AGENT task is not yet terminal (done/blocked).

    Used by the reconciliation reaper to decide whether a board still needs a
    dispatch tick. The swarm ROOT planning card (assignee ``fluxswarm``) is
    auto-completed immediately and represents no agent work, so it never keeps
    a board "unfinished".
    """
    try:
        tasks = list_tasks(board)
    except Exception:
        return False
    if not tasks:
        return False
    for t in tasks:
        assignee = (t.get("assignee") or "").strip().lower()
        if not assignee or assignee == "fluxswarm":
            continue
        if t.get("state") not in ("done", "blocked"):
            return True
    return False


# ---------------------------------------------------------------------------
# Transient-block recovery (auto re-dispatch).
#
# A worker run that crashes mid-flight (provider HTTP 429 / transient outage)
# is retried by the dispatcher up to its ``effective_limit`` and then parked
# as ``blocked``.  ``board_has_unfinished_work`` treats ``blocked`` as
# terminal, so without help the swarm freezes even after the provider
# recovers.  ``bump_blocked_to_ready`` re-promotes blocked agent tasks back
# to ``ready`` on a reaper cadence; the next normal dispatch pass re-spawns
# them, and Hermes' parent-gating (``recompute_ready``) still prevents any
# task from running before its dependencies complete.
# ---------------------------------------------------------------------------

def bump_blocked_to_ready(board: str, cap: int = 8) -> int:
    """Re-promote blocked AGENT tasks on *board* back to ``ready``.

    Returns the number of tasks re-promoted (0 when none are blocked).
    Used by the reconciliation reaper so transient provider failures
    recover automatically instead of freezing the board.  The ROOT planning
    card and anything still waiting on open parents are left untouched by
    Hermes' own ``promote`` parent-gating; the dispatcher decides whether a
    re-promoted task may actually spawn.

    A sealed board (operator-final) is a no-op: waking its workers would
    resurrect a terminal launch and hold the host concurrency budget forever.
    """
    if board_is_sealed(board):
        try:
            from audit import audit
            audit("reaper.skip_sealed", board=board)
        except Exception:
            pass
        return 0
    try:
        tasks = list_tasks(board)
    except Exception:
        return 0
    blocked = [t for t in tasks
               if t.get("state") == "blocked"
               and (t.get("assignee") or "").strip().lower() != "fluxswarm"]
    if not blocked:
        return 0
    for t in blocked[:cap]:
        tid = t.get("id")
        if not tid:
            continue
        r = _run(["promote", tid], board=board)
    return min(len(blocked), cap)


def kill_stale_workers(board: str, stale_s: int = 300) -> list[str]:
    """Kill workers whose ``running`` task heartbeat went quiet.

    A worker that dies without a clean terminal event leaves its task in
    ``running`` with a frozen PID and an old ``last_heartbeat_at``; it then
    holds the host concurrency budget until the dispatcher's claim TTL reaps
    it. This is the reaper's proactive sweep: kill the stale process tree and
    park the task as ``blocked`` so the next dispatch pass re-spawns it once
    the provider recovers.

    Column-tolerant (queries only columns that exist in this board's schema)
    and best-effort: per-row failures degrade the sweep, never raise. Returns
    the list of task ids parked.
    """
    parked: list[str] = []
    db_path = Path(HERMES_HOME) / "kanban" / "boards" / board / "kanban.db"
    try:
        if not db_path.exists():
            return parked
        c = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "worker_pid" not in cols or "last_heartbeat_at" not in cols:
                return parked
            cutoff = time.time() - max(60, int(stale_s))
            rows = c.execute(
                "SELECT id, worker_pid FROM tasks "
                "WHERE status IN ('running','queued') "
                "AND worker_pid IS NOT NULL "
                "AND COALESCE(last_heartbeat_at, 0) < ?",
                (cutoff,),
            ).fetchall()
            for tid, pid in rows:
                if not pid:
                    continue
                try:
                    kill_process_tree(int(pid))
                except Exception:
                    pass
                set_clauses = ["status=?", "worker_pid=?"]
                values = ["blocked", None]
                if "claim_lock" in cols:
                    set_clauses.append("claim_lock=?"); values.append(None)
                if "claim_expires" in cols:
                    set_clauses.append("claim_expires=?"); values.append(None)
                c.execute(
                    "UPDATE tasks SET %s WHERE id=? AND status IN ('running','queued')"
                    % ", ".join(set_clauses),
                    tuple(values) + (tid,),
                )
                parked.append(str(tid))
                c.commit()
        finally:
            c.close()
    except Exception:
        pass
    return parked


def delete_demo_board(board: str) -> bool:
    """Remove an expired demo board's directory (workspace + kanban.db).

    Demo boards (``flux-demo-*``) are shared/throwaway: once sealed and aged
    past the workspace TTL they are deleted wholesale. Defensive by
    construction — the slug must be a ``flux-demo-`` slug, match the safe
    charset, and resolve inside the boards root, so a corrupted call can never
    escalate to an arbitrary filesystem delete (same rules as
    ``delete_boards``, which deliberately refuses demo boards).
    """
    if not board.startswith("flux-demo-"):
        return False
    if not _SAFE_SLUG_RE.match(board):
        return False
    root = Path(HERMES_HOME) / "kanban" / "boards"
    target = (root / board).resolve()
    root_resolved = str(root.resolve()) + os.sep
    if not str(target).startswith(root_resolved):
        return False
    try:
        if target.exists():
            shutil.rmtree(target)
            # P4/2: drop the mirror rows so the demo board isn't restored later.
            board_store.purge_board(board)
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Live agent activity (Issue 2).
#
# Every field below is derived from REAL, persisted Hermes runtime state in the
# board's ``kanban.db`` (task status + the ``task_events`` operational log).
# Nothing is fabricated, randomized, or rotated: if a worker is genuinely
# running it emits heartbeats (visible as "Working…"), and a completion records
# its summary/artifacts. No chain-of-thought is ever surfaced — only safe
# operational events.
# ---------------------------------------------------------------------------

# User-facing role info for the demo/ECC swarm assignees. ``action`` is the
# truthful current focus while the agent is RUNNING (a worker performing its
# role's job); ``done`` is what we show once it completes. Display names strip
# the internal ``ecc-`` prefix as everywhere else in the UI.
ROLE_INFO = {
    "ecc-planner":     {"name": "Planner",  "action": "Creating the implementation plan",
                        "done": "Plan completed",
                        "desc": "Expert planning specialist: breaks the feature into an actionable, dependency-ordered implementation plan."},
    "ecc-architect":   {"name": "Architect", "action": "Designing the system architecture",
                        "done": "Architecture completed",
                        "desc": "Software architecture specialist: designs the system structure, interfaces and scalability."},
    "ecc-devops":      {"name": "DevOps",   "action": "Setting up CI/CD and containerization",
                        "done": "CI/CD and containerization set up",
                        "desc": "Sets up CI/CD pipelines and containerization so the project builds and deploys reproducibly."},
    "ecc-tdd":         {"name": "TDD",      "action": "Writing the test suite",
                        "done": "Test suite written",
                        "desc": "Test-driven development specialist: writes the test suite before the implementation."},
    "ecc-reviewer":    {"name": "Reviewer", "action": "Reviewing outputs and gating the swarm",
                        "done": "Review passed",
                        "desc": "Reviews every worker handoff and gates the swarm, completing only when the evidence is sufficient."},
    "ecc-designer":    {"name": "Designer", "action": "Defining the visual design system",
                        "done": "Design system defined",
                        "desc": "Visual design specialist: produces the concrete palette, type scale and token set the Builder must follow."},
    "ecc-build-fixer": {"name": "Builder",  "action": "Assembling the final build",
                        "done": "Build assembled",
                        "desc": "Synthesizes the verified worker outputs into the final deliverable and makes the build green."},
    "ecc-auditor":     {"name": "Auditor",  "action": "Auditing the delivered artifact",
                        "done": "Audit completed",
                        "desc": "Runs the final acceptance check over the delivered artifact against the design system and deterministic QA."},
}

_BOARD_DB_CACHE: dict = {}


def _board_db_path(board: str) -> Path:
    return Path(HERMES_HOME) / "kanban" / "boards" / board / "kanban.db"


def _task_activity_events(board: str, task_id: str) -> list[dict]:
    """Return the chronological OPERATIONAL event log for one task, in display
    form (``label`` + ``at``) built from the persisted ``task_events`` rows.
    Consecutive heartbeats collapse into a single "Working…" entry so the log
    stays compact instead of repeating every ~60s."""

    db = _board_db_path(board)
    try:
        if not db.exists():
            return []
        c = sqlite3.connect(str(db))
        try:
            rows = c.execute(
                "SELECT kind, payload, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        finally:
            c.close()
    except Exception:
        return []

    out: list[dict] = []
    last_heartbeat = 0
    for kind, payload, created_at in rows:
        pd = None
        if payload:
            try:
                pd = json.loads(payload)
            except Exception:
                pd = None
        if kind == "heartbeat":
            if created_at - last_heartbeat >= 60:
                out.append({"kind": "working", "label": "Working…", "at": int(created_at)})
                last_heartbeat = int(created_at)
            continue
        if kind == "claimed":
            out.append({"kind": "started", "label": "Started", "at": int(created_at)})
        elif kind == "spawned":
            pid = (pd or {}).get("pid")
            out.append({"kind": "spawned",
                        "label": f"Worker spawned (PID {pid})" if pid else "Worker spawned",
                        "at": int(created_at)})
        elif kind == "attached":
            fname = (pd or {}).get("filename")
            out.append({"kind": "produced",
                        "label": f"Produced {fname}" if fname else "Produced an artifact",
                        "at": int(created_at)})
        elif kind == "completed":
            summary = (pd or {}).get("summary") or ""
            label = "Completed"
            if summary:
                label = f"Completed: {summary.strip()[:220]}"
            out.append({"kind": "completed", "label": label, "at": int(created_at)})
        elif kind == "failed":
            out.append({"kind": "failed", "label": "Failed", "at": int(created_at)})
        elif kind == "note":
            msg = (pd or {}).get("message") or "Note"
            out.append({"kind": "note", "label": f"ℹ {str(msg)[:240]}",
                        "at": int(created_at)})
        elif kind == "error":
            msg = (pd or {}).get("message") or kind
            out.append({"kind": "error", "label": f"Demo error: {str(msg)[:240]}",
                        "at": int(created_at)})
        elif kind == "blocked":
            out.append({"kind": "blocked", "label": "Blocked", "at": int(created_at)})
        elif kind in ("reclaimed", "crashed", "timed_out"):
            reason = {"reclaimed": "stall-reclaim", "crashed": "crash",
                      "timed_out": "max-runtime"}.get(kind, kind)
            out.append({"kind": "recovered", "reason": reason,
                        "label": f"Worker recovered and requeued ({reason})",
                        "at": int(created_at)})
    return out


def _result_preview(board: str, task: dict) -> str | None:
    """A short real result preview: the task's stored ``result`` if present,
    else the ``completed`` event summary, truncated for the compact UI."""
    result = (task.get("result") or "").strip()
    if result:
        return result[:280]
    db = _board_db_path(board)
    try:
        if not db.exists():
            return None
        c = sqlite3.connect(str(db))
        try:
            row = c.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
                (task.get("id"),),
            ).fetchone()
        finally:
            c.close()
        if row and row[0]:
            pd = json.loads(row[0])
            s = (pd.get("summary") or "").strip()
            if s:
                return s[:280]
    except Exception:
        pass
    return None


def _attach_activity(board: str, task: dict) -> dict:
    """Enrich a single task dict with the live-activity fields surfaced to the
    UI. All values come from real Hermes state/events (issue-2 contract)."""
    assignee = (task.get("assignee") or "").strip()
    role = ROLE_INFO.get(assignee) or {}
    state = task.get("state", "unknown")

    events = _task_activity_events(board, task.get("id", ""))
    completed_at = task.get("completed_at")
    last_heartbeat_at = task.get("last_heartbeat_at")

    last_activity_at = None
    for e in events:
        at = e.get("at")
        if at is not None and (last_activity_at is None or at > last_activity_at):
            last_activity_at = at
    for ts in (completed_at, last_heartbeat_at):
        if ts is not None and (last_activity_at is None or int(ts) > last_activity_at):
            last_activity_at = int(ts)

    if role:
        if state == "running":
            current_action = role["action"]
        elif state == "done":
            current_action = role["done"]
        elif state == "blocked":
            current_action = "Blocked"
        else:
            current_action = "Waiting for dependency"
        display = role["name"]
        description = role["desc"]
    else:
        current_action = { "running": "Working…",
                           "done": "Completed",
                           "blocked": "Blocked" }.get(state, "Waiting")
        display = task.get("assignee_display") or assignee or "Agent"
        description = ""

    result_preview = _result_preview(board, task) if state == "done" else None

    task["role_name"] = display
    task["role_description"] = description
    task["current_action"] = current_action
    task["activity_log"] = events
    task["last_activity_at"] = last_activity_at
    task["result_preview"] = result_preview
    return task


def show_task(board: str, task_id: str) -> dict:
    r = _run(["show", task_id, "--json"], board=board)
    if r.returncode != 0:
        raise RuntimeError(f"show failed: {r.stderr}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def read_workspace(board: str) -> str:
    """Collect generated files from the board's task workspaces for review."""
    if not _SAFE_SLUG_RE.match(board):
        raise ValueError(f"unsafe board slug: {board!r}")
    ws_root = Path(HERMES_HOME) / "kanban" / "boards" / board / "workspaces"
    _EXTRA_NAME = {"Dockerfile", "Makefile", "Procfile", "LICENSE"}
    out = []
    try:
        for f in ws_root.rglob("*"):
            if f.is_file() and (f.suffix in (".py", ".md", ".txt", ".json", ".yaml", ".yml", ".html")
                                or f.name in _EXTRA_NAME):
                out.append(f"--- {f.relative_to(ws_root)} ---\n")
                out.append(f.read_text(encoding="utf-8", errors="ignore")[:3000])
    except Exception:
        pass
    return "\n".join(out)


def list_boards() -> list[dict]:
    # On small hosts the fat `boards ls` CLI (loads the workspace) is exactly
    # the OOM risk we route around; a direct filesystem scan is sub-millisecond
    # and equally truthful for the reaper/health surfaces.
    if projects_are_thin():
        try:
            root = Path(HERMES_HOME) / "kanban" / "boards"
            if root.is_dir():
                return [{"slug": e.name} for e in sorted(root.iterdir())
                        if (e.is_dir() and _SAFE_SLUG_RE.match(e.name))]
            return []
        except Exception:
            pass
    r = _run(["boards", "ls"])
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("SLUG", "Board:"):
            continue
        slug = parts[0].strip("● ")
        if slug and slug != "SLUG":
            out.append({"slug": slug})
    return out


def delete_boards(slugs: list[str], boards_root: Path | None = None) -> int:
    """Delete a user's Hermes kanban board directories (account erasure).

    Defensive by construction: every slug must match the safe charset AND its
    resolved path must stay inside the kanban boards root, so a corrupted slug
    can never escalate into an arbitrary filesystem delete. Demo boards
    (``flux-demo-*``) are shared and deliberately never passed here. Returns the
    number of boards actually removed.
    """
    root = Path(boards_root) if boards_root is not None else Path(HERMES_HOME) / "kanban" / "boards"
    root_resolved = str(root.resolve()) + os.sep
    removed = 0
    for raw in slugs or []:
        slug = str(raw or "")
        if not _SAFE_SLUG_RE.match(slug):
            continue
        target = (root / slug).resolve()
        # Containment: resolve() normalises symlinks, so targets must literally
        # live under the boards root. slug has no separators, but the check stays
        # for defence when the boards root itself is redirected.
        if not str(target).startswith(root_resolved):
            continue
        try:
            if target.exists():
                shutil.rmtree(target)
                # P4/2: drop the mirror rows so account erasure is complete.
                board_store.purge_board(slug)
                removed += 1
        except OSError:
            continue
    return removed
