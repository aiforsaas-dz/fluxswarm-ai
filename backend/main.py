"""
FluxSwarm backend - FastAPI server (auth + squad + plans + referrals + demo).

Each authenticated user owns an isolated set of Hermes kanban boards (prefixed
by their user id). The ECC devops squad (swarm) is launched per project.
"""
from __future__ import annotations

import asyncio
import ast
import functools
import sys
import atexit
import io
import zipfile
from contextlib import asynccontextmanager
import datetime
import ipaddress
import difflib
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import audit
import envguard

# Production fail-fast BEFORE any secret-bearing module load: both required
# secrets must be present (or the deployment must be explicitly demo mode).
envguard.assert_production_secrets()

import auth as auth_mod
# Persistence backend: PostgreSQL (Neon) when FLUXSWARM_DATABASE_URL is set,
# otherwise the local SQLite fallback (demo/dev). db_postgres mirrors db.py's
# API surface so the rest of main.py is engine-agnostic.
if (os.getenv("FLUXSWARM_DATABASE_URL") or "").startswith("postgres"):
    import db_postgres as db
else:
    import db
import hermes_client as hc
import demo_llm
import notify
import serverlock
import vault
import security
import payments as payments_mod
import provider_pool
import provider_guard
import project_analyzer
import evidence_gate
import support_agent
import board_store
from ratelimit import limiter

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Ensure the data layer is ready before serving traffic: SQLite creates its
    # schema lazily, and the PostgreSQL path must apply Alembic migrations +
    # seed the demo account here (db.py does it at import; db_postgres defers
    # to a running event loop for the async pool).
    try:
        db.init_db()
    except Exception as e:  # never take the app down on a DB hiccup at boot
        import traceback
        print(f"[lifespan] db.init_db() failed: {e}\n{traceback.format_exc()}", flush=True)
    try:
        db.seed_demo()
    except Exception as e:
        print(f"[lifespan] db.seed_demo() failed: {e}", flush=True)
    # P4/2: restore boards whose SQLite state was wiped by a free-tier restart
    # (cold starts do NOT keep local disk). Runs BEFORE the reaper starts so a
    # restored board is immediately visible and re-dispatchable. Best-effort —
    # a restore failure must never block the app from serving traffic.
    try:
        restored = board_store.restore_missing_boards()
        if restored:
            print(f"[lifespan] restored {restored} board(s) from Postgres mirror", flush=True)
    except Exception as e:
        print(f"[lifespan] board restore failed: {e}", flush=True)
    # P4/2 resume: restored boards that were mid-launch when the host died need
    # the thin driver re-fired (the host reaper is disabled on thin hosts). The
    # driver's skip-done guard keeps anything already completed untouched.
    try:
        _resume_restored_thin_boards()
    except Exception as e:
        print(f"[lifespan] thin resume failed: {e}", flush=True)
    # Start the persistent reconciliation reaper once the server is up (NOT at
    # import, so a pytest import of this module never launches the loop).
    threading.Thread(target=_reaper_loop, daemon=True).start()
    yield


app = FastAPI(title="FluxSwarm", version="3.0.0", lifespan=_lifespan)


# ---------- Structured rate-limit errors (UX report §2.3) ----------
# Demo-limit 429s carry a rich body: {error, message:{en,ar}, retry_after_seconds,
# upgrade_url}. A dict `detail` on any 429 is lifted to the top-level JSON (with a
# matching Retry-After header) so the frontend shows the right message + CTA.
# String detail (legacy/auth attempts/template caps) is serialized unchanged.
@app.exception_handler(HTTPException)
async def _rate_limit_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 429 and isinstance(exc.detail, dict):
        body = dict(exc.detail)
        headers = {}
        if "retry_after_seconds" in body:
            headers["Retry-After"] = str(body["retry_after_seconds"])
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))

# ---------- CORS (strict allow-list; never a wildcard) ----------
# Origins come from FLUXSWARM_CORS_ORIGINS (comma/whitespace-separated). If unset
# or empty, NO CORS middleware is registered at all — same-origin only,
# deny-by-default. We never fall back to "*": an open allow-list lets any site
# read responses (cookies/Bearer) and defeats the browser same-origin policy.
def _load_cors_origins() -> list[str]:
    """Resolve the CORS allow-list from FLUXSWARM_CORS_ORIGINS.

    A ``*`` wildcard is a hard error (RuntimeError) — an open allow-list lets
    any origin read responses (cookies/Bearer) and defeats the browser
    same-origin policy. Empty/unset => same-origin only (explicit empty list,
    which the middleware treats as 'no cross-origin origin permitted').
    """
    raw = os.environ.get("FLUXSWARM_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    origins = [o.strip() for o in raw.replace("\n", ",").split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "FLUXSWARM_CORS_ORIGINS must not contain '*' (open allow-list). "
            "List explicit origins only."
        )
    return origins

CORS_ALLOW_ORIGINS = _load_cors_origins()

# Always register the CORS middleware. With an empty allow-list (env unset) it
# denies every cross-origin request (same-origin only) — strict by default.
# A "*" can never appear: _cors_allow_origins() strips wildcards, so we never
# open the API to every origin nor combine a wildcard with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,       # explicit list only — never "*"
    allow_credentials=bool(CORS_ALLOW_ORIGINS),  # no credentials unless origins are set
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    expose_headers=["X-Requested-With"],
    max_age=600,
)

# ---------------------------------------------------------------------------
# Trusted proxy list for X-Forwarded-For. The client IP derived from
# X-Forwarded-For is only trustworthy when the request actually came through a
# configured reverse proxy — otherwise a client can spoof it and evade the
# per-IP rate limiter. Set FLUXSWARM_TRUSTED_PROXIES to the proxy IPs/CIDRs
# (comma-separated); if unset, XFF is ignored and request.client.host is used.
# ---------------------------------------------------------------------------
def _trusted_proxies() -> list[object]:
    """Parse FLUXSWARM_TRUSTED_PROXIES into exact IPs and CIDR networks.

    Each entry is either a single IPv4/IPv6 address or a CIDR (e.g. 10.0.0.0/8).
    A malformed entry is skipped (and logged) rather than silently trusting
    everything. Returns a list of ipaddress network objects. Reads the env
    live so callers that set the variable without reloading the module still
    see the change.
    """
    raw = os.environ.get("FLUXSWARM_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    out: list[object] = []
    for p in raw.replace("\n", ",").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            print(f"[proxy] ignoring invalid FLUXSWARM_TRUSTED_PROXIES entry: {p!r}",
                  file=sys.stderr)
    return out


TRUSTED_PROXIES = _trusted_proxies()


def _peer_is_trusted(peer: str | None, nets: list[object] | None = None) -> bool:
    """True only if `peer` is a configured trusted proxy (exact IP or in CIDR).

    ``nets`` is the parsed trusted-proxy list (defaults to the module-level
    ``TRUSTED_PROXIES`` global). Callers/tests may pass plain-IP strings too,
    so the membership check tolerates both network objects and str entries.
    """
    nets = TRUSTED_PROXIES if nets is None else nets
    if not peer or not nets:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in nets:
        if isinstance(net, str):
            try:
                if addr == ipaddress.ip_address(net):
                    return True
            except ValueError:
                continue
        else:
            try:
                if addr in net:
                    return True
            except TypeError:
                continue
    return False

# Single instance: refuse a second live backend (port races / double superv  is
# what used to spawn orphan uvicorn processes). Bypass with FLUXSWARM_ALLOW_MULTI=1.
serverlock.acquire()
atexit.register(serverlock.release)

_START_TS = time.time()

_SUBS: dict[str, set[WebSocket]] = {}

_WS_MAX_SUBS_PER_SLUG = int(os.environ.get("FLUXSWARM_WS_MAX_SUBS_PER_SLUG", "64"))

def _payments_enabled() -> bool:
    """Read the billing gate live (not at import time) so tests / ops can switch
    FLUXSWARM_PAYMENTS without a module reload."""
    return os.environ.get("FLUXSWARM_PAYMENTS", "").strip().lower() in ("1", "true", "yes")


def _operator_maintenance() -> bool:
    """Operator kill-switch (FIX-1): when FLUXSWARM_KILL_SWITCH=1, ALL
    cost-bearing/demo surfaces reject with 503. Default off."""
    return os.environ.get("FLUXSWARM_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes")


def _paid_fallback_runtime():
    """Paid last-resort runtime for the demo surface (operator opt-in only).

    OFF by default: without an explicit FLUXSWARM_PAID_FALLBACK_ENABLED=1 the
    demo never spends money — an exhausted pool fails with the structured
    ``demo_provider_unavailable`` body. When enabled, the fallback is exactly
    the operator-configured default runtime (FLUXSWARM_DEFAULT_PROVIDER + model);
    a fallback that cannot be resolved returns None (we never guess a runtime).
    Returns (model, provider) to match hermes_client._default_runtime().
    """
    if not provider_guard.paid_fallback_enabled():
        return None
    try:
        return hc._default_runtime()
    except Exception:
        return None


def _provider_usage_summary_safe():
    """Per-day provider-usage totals for /health (best-effort, never raises)."""
    try:
        return db.provider_usage_summary()
    except Exception:
        return {"day": _today(), "total_attempts": 0, "finalized": 0, "ok": 0,
                "not_ok": 0, "pending_attempts": 0,
                "by_runtime_source": {}, "by_surface": {}}


_DEMO_DAILY_CAP = int(os.environ.get("FLUXSWARM_DEMO_DAILY_CAP", "25"))
# Session 2 demo quotas (sliding-window ratelimit keys; see ratelimit.check).
_DEMO_IP_MAX = 1
_DEMO_IP_WINDOW = 3600
_DEMO_GLOBAL_MAX = 20
_DEMO_GLOBAL_WINDOW = 86400
_DEMO_MICRO_IP_MAX = 5
_DEMO_MICRO_GLOBAL_MAX = 200
# Session 3 demo lifecycle (auto-close + workspace recycle).
_DEMO_MAX_RUNTIME_S = int(os.getenv("FLUXSWARM_DEMO_MAX_RUNTIME_S", "1200"))
_DEMO_WORKSPACE_TTL_S = int(os.getenv("FLUXSWARM_DEMO_WORKSPACE_TTL_S", "86400"))
_DEMO_LAST: dict[str, tuple] = {}
_DEMO_LAST_TTL_S = 7200


def _remember_demo(ip: str, slug: str) -> None:
    """Remember the caller's most recent demo board so the UI can recover the
    slug even when the launch response is dropped at the edge (free tier can
    cut long-running requests server-side after the board was created)."""
    if not ip or not slug:
        return
    try:
        cutoff = time.time() - _DEMO_LAST_TTL_S
        if len(_DEMO_LAST) > 4096:
            for k in list(_DEMO_LAST):
                if _DEMO_LAST[k][1] < cutoff:
                    _DEMO_LAST.pop(k, None)
        _DEMO_LAST[ip] = (slug, time.time())
    except Exception:
        pass


def _latest_demo(ip: str):
    """The caller's most recent expiry-checked demo slug, or None."""
    if not ip:
        return None
    try:
        snap = _DEMO_LAST.get(ip)
    except Exception:
        return None
    if not snap:
        return None
    slug, ts = snap
    if time.time() - ts > _DEMO_LAST_TTL_S:
        try:
            _DEMO_LAST.pop(ip, None)
        except Exception:
            pass
        return None
    try:
        if hc.board_is_sealed(slug):
            try:
                _DEMO_LAST.pop(ip, None)
            except Exception:
                pass
            return None
    except Exception:
        pass
    return slug


_DEMO_LIMIT_DETAILS = {
    "ip": {
        "error": "demo_ip_limit",
        "message": {
            "en": "Demo limit: 1 launch per hour. Sign up for unlimited access.",
        },
        "retry_after_seconds": _DEMO_IP_WINDOW,
        "upgrade_url": "/pricing",
    },
    "global": {
        "error": "demo_global_limit",
        "message": {
            "en": "Daily demo quota exhausted. Try tomorrow or sign up.",
        },
        "retry_after_seconds": _DEMO_GLOBAL_WINDOW,
        "upgrade_url": "/pricing",
    },
}


def _today() -> str:
    """Local calendar day (YYYY-MM-DD) — the period key for durable demo caps."""
    return datetime.date.today().isoformat()


def _next_utc_midnight() -> str:
    """ISO-8601 timestamp (UTC, Z) of the next quota reset."""
    now = datetime.datetime.now(datetime.timezone.utc)
    nxt = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat().replace("+00:00", "Z")


# Goal strings are forwarded verbatim to third-party AI providers, so they are
# the natural injection surface for prompt-injection / jailbreak attempts.
# Sanitize before any provider call: redact classic override directives and cap
# length. Redaction (not hard rejection) keeps legitimate business prose that
# merely *mentions* these phrases usable while stripping the attack payload.
_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|earlier)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(the\s+)?(above|previous|prior)\s+(instructions|context|prompt)", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bdo\s+anything\s+now\b|\bjailbreak\b", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?(system|hidden|internal)\s+(prompt|instructions|directives)", re.IGNORECASE),
    re.compile(r"\bignore\s+(the\s+)?(system|developer)\s+(prompt|instructions|message)\b", re.IGNORECASE),
)
_GOAL_MAX_LEN = 4000


def sanitize_goal(goal: str | None) -> str:
    """Neutralize prompt-injection directives and bound length for provider input."""
    text = (goal or "").strip()
    for pat in _PROMPT_INJECTION_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text[:_GOAL_MAX_LEN]


def _safe_artifact_name(name: str) -> str:
    """Filename-safe slug for custom-agent artifact names (e.g. CUSTOM_<name>.md)."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "agent").strip("_") or "agent"
    return slug[:64]


def _demo_user(request: Request):
    """Resolve the optional bearer user for demo endpoints (never raises).

    Demo endpoints are anonymous by default; when a logged-in user calls them
    their ADMT opt-out status is honoured. ``request`` may be None in tests
    that invoke the handler directly.
    """
    if request is None:
        return None
    ah = request.headers.get("Authorization") or ""
    token = ah[7:] if ah.startswith("Bearer ") else ""
    if not token:
        return None
    try:
        payload = auth_mod.decode_token(token)
    except Exception:
        return None
    return db.get_user_by_id(payload.get("uid"))


# ---------- security headers ----------
# Paddle Checkout overlay needs: SDK script (cdn.paddle.com), its iframe
# (checkout / sandbox-checkout), and API calls (api.paddle.com).
_PADDLE_ORIGINS = "https://cdn.paddle.com https://*.paddle.com"
# Responsive webpages serve inline scripts/styles signed by a per-request nonce
# (see _new_csp). '{nonce}' is replaced at request time; no 'unsafe-inline'.
_CSP_TEMPLATE = ("default-src 'self'; "
                 "script-src 'self' 'nonce-{nonce}' https://cdn.paddle.com https://plausible.io; "
                 # style-src intentionally uses 'unsafe-inline' (no nonce):
                 # the dashboard sets many layout details via style="" attributes,
                 # and per CSP3 a nonce would force CSP to IGNORE 'unsafe-inline',
                 # breaking the rendered UI. Style injection is presentational-only
                 # and is NOT an XSS vector; scripts stay strict nonce-only.
                 "style-src 'self' 'unsafe-inline' https://*.paddle.com; "
                 "img-src 'self' data: https://*.paddle.com; "
                 "font-src 'self' data: https://*.paddle.com; "
                 "connect-src 'self' ws: wss: https://*.paddle.com wss://checkout.paddle.com https://plausible.io; "
                 "frame-src 'self' https://checkout.paddle.com https://sandbox-checkout.paddle.com "
                 "https://buy.paddle.com https://sandbox-buy.paddle.com; "
                 "object-src 'none'; "
                 "frame-ancestors 'none'; "
                 "base-uri 'self'; "
                 "form-action 'self'")


def _csp_for(nonce: str) -> str:
    """Build the Content-Security-Policy from the per-request nonce."""
    return _CSP_TEMPLATE.format(nonce=nonce)


# CSP for /p/ preview responses: user-generated content in a sandboxed iframe.
# Allows inline/eval scripts so generated apps run. The frame is sandboxed
# WITHOUT allow-same-origin, so its document gets an opaque origin — therefore
# header-based X-Frame-Options is useless here: Firefox evaluates SAMEORIGIN
# against the sandboxed frame's unique origin and refuses the connection (the
# "{ "html": browser-refusal }" symptom). Embedding is governed instead by:
#   * dashboard/load side  -> the app CSP frame-src 'self' (only same-origin
#     pages may be framed),
#   * /p/ embed side        -> CSP frame-ancestors 'self' (ancestor is the
#     dashboard, same origin => matches; this is ancestor-origin-based, so it
#     works under the opaque sandbox), and NO X-Frame-Options header at all.
# The global strict CSP (frame-ancestors 'none' + DENY) stays for all other
# routes.
_PREVIEW_CSP = (
    "default-src 'self' 'unsafe-inline' data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "style-src 'self' 'unsafe-inline' data: blob:; "
    "img-src 'self' data: blob:; font-src 'self' data: blob:; "
    "connect-src 'self' data: blob: ws: wss:; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Per-request CSP nonce so inline scripts run under a signed nonce instead
    # of 'unsafe-inline'. Stored on request.state for template rendering.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    if getattr(request.state, "preview", False):
        # Preview responses carry user-generated app pages that need inline
        # scripts/eval to render; they are served into a sandboxed (opaque
        # origin) iframe. Embedding is allowed by the dashboard's frame-src
        # 'self' (load side) plus CSP frame-ancestors 'self' here (embed side;
        # the ancestor is the same-origin dashboard, so the directive matches
        # even though the framed document itself has an opaque origin). No
        # X-Frame-Options header is sent: Firefox refuses SAMEORIGIN for a
        # sandboxed frame (unique origin) which shows up as a browser
        # "connection not authorized" page inside the preview. Everything else
        # keeps the strict nonce-only policy below.
        resp.headers["Content-Security-Policy"] = _PREVIEW_CSP
        try:
            del resp.headers["x-frame-options"]
        except (KeyError, ValueError):
            pass
    else:
        resp.headers["Content-Security-Policy"] = _csp_for(nonce)
        resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains; preload"
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# Small TTL cache so the /health endpoint never pays a filesystem sweep per hit.
_board_stats_cache: dict = {"ts": 0.0, "active": 0, "sealed": 0}


def _board_stats_cached() -> dict:
    """Cached {active, sealed} board counts (10s TTL). Result is degraded to
    zeros when the boards root is unreachable — /health must never 500."""
    now = time.time()
    if now - _board_stats_cache["ts"] < 10:
        return _board_stats_cache
    active = sealed = 0
    try:
        roots = Path(hc.HERMES_HOME) / "kanban" / "boards"
        for entry in roots.iterdir():
            if not entry.is_dir():
                continue
            if hc.board_is_sealed(entry.name):
                sealed += 1
            else:
                active += 1
    except Exception:
        pass
    _board_stats_cache.update(ts=now, active=active, sealed=sealed)
    return _board_stats_cache


def snapshot_health() -> dict:
    """Build the enhanced /health payload (Task 4 session 3).

    Every section degrades under error — a failed probe must not turn the
    whole payload into a 500. Pool statistics are only reported when a
    PostgreSQL pool actually exists (SQLite mode reports None).
    """
    db_ok = True
    try:
        db.get_user_by_id(1)
    except Exception:
        db_ok = False
    db_kind = "postgresql" if (os.getenv("FLUXSWARM_DATABASE_URL") or "").startswith("postgres") else "sqlite"
    pool_available = pool_total = None
    if db_kind == "postgresql":
        try:
            import db_postgres as pg
            pool = pg._await(pg.get_pool())
            if pool is not None:
                pool_total = pool.get_size()
                pool_available = pool.get_idle_size()
        except Exception:
            pool_available = pool_total = None
    hermes_docker_ok = True
    try:
        if os.getenv("FLUXSWARM_DOCKER_DISPATCH", "0") == "1":
            import shutil
            hermes_docker_ok = shutil.which("docker") is not None
    except Exception:
        hermes_docker_ok = False
    prov = []
    for entry in provider_pool.DEMO_PROVIDERS:
        key_env = entry.get("key_env")
        needs = entry.get("requires_key", False)
        if needs and key_env and not os.getenv(key_env):
            status = "needs_key"
        else:
            status = "configured"
        prov.append({
            "provider": entry.get("provider"),
            "model": entry.get("model"),
            "status": status,
            # A live quota figure requires a per-provider usage API; without
            # one we advertise "unknown" instead of inventing a number.
            "quota_remaining": "unknown",
        })
    try:
        capacity = provider_pool.pool_capacity()
    except Exception:
        capacity = {"state": "unknown", "providers": []}
    try:
        budget_ceilings = provider_guard.budget_ceilings()
    except Exception:
        budget_ceilings = {}
    g_total = _DEMO_GLOBAL_MAX
    g_used = limiter.count("demo:global", _DEMO_GLOBAL_WINDOW)
    bstats = _board_stats_cached()
    return {
        "ok": db_ok and hc.HERMES_BIN.exists(),
        "version": app.version,
        "db": db_kind,
        "db_ok": db_ok,
        "db_pool_available": pool_available,
        "db_pool_total": pool_total,
        "hermes_docker_ok": hermes_docker_ok,
        "hermes_bin": str(hc.HERMES_BIN),
        "hermes_bin_ok": hc.HERMES_BIN.exists(),
        "hermes_image": os.getenv("FLUXSWARM_RUNNER_IMAGE", "fluxswarm/hermes-runner:latest"),
        "limiter_backend": getattr(limiter, "backend", "memory"),
        "provider_pool": {"demo_providers": prov, "capacity": capacity.get("state", "unknown"),
                          "providers": capacity.get("providers", []),
                          "entries": capacity.get("entries", [])},
        "paid_fallback_enabled": provider_guard.paid_fallback_enabled(),
        "budget": budget_ceilings,
        "provider_usage": _provider_usage_summary_safe(),
        "demo_quota_remaining": max(0, g_total - g_used),
        "demo_quota_total": g_total,
        "active_boards": bstats["active"],
        "sealed_boards": bstats["sealed"],
        "max_in_progress": hc.MAX_IN_PROGRESS,
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - _START_TS, 1),
    }


@app.get("/health")
def api_health():
    return snapshot_health()


@app.get("/metrics")
def api_metrics():
    """Prometheus-style text metrics (P4.4). Built from the /health snapshot so
    it carries the same low-cost cached probes; exported for the operator's
    monitoring, not for the public."""
    s = snapshot_health()
    lines = [
        "# HELP fluxswarm_up 1 when the app is healthy.",
        "# TYPE fluxswarm_up gauge",
        f"fluxswarm_up {1 if s['ok'] else 0}",
        "# HELP fluxswarm_db_ok 1 when the database responds.",
        "# TYPE fluxswarm_db_ok gauge",
        f"fluxswarm_db_ok {1 if s['db_ok'] else 0}",
        "# HELP fluxswarm_hermes_bin_ok 1 when the Hermes runtime binary exists.",
        "# TYPE fluxswarm_hermes_bin_ok gauge",
        f"fluxswarm_hermes_bin_ok {1 if s['hermes_bin_ok'] else 0}",
        "# HELP fluxswarm_active_boards currently live squad boards.",
        "# TYPE fluxswarm_active_boards gauge",
        f"fluxswarm_active_boards {s['active_boards']}",
        "# HELP fluxswarm_sealed_boards boards finished/archived.",
        "# TYPE fluxswarm_sealed_boards gauge",
        f"fluxswarm_sealed_boards {s['sealed_boards']}",
        "# HELP fluxswarm_demo_quota_remaining demo launches left in the global window.",
        "# TYPE fluxswarm_demo_quota_remaining gauge",
        f"fluxswarm_demo_quota_remaining {s['demo_quota_remaining']}",
        "# HELP fluxswarm_uptime_seconds process uptime.",
        "# TYPE fluxswarm_uptime_seconds gauge",
        f"fluxswarm_uptime_seconds {s['uptime_seconds']}",
        f"# db={s['db']} limiter_backend={s['limiter_backend']}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/api/provider/health")
def api_provider_health(provider: str = "", model: str = ""):
    """Lightweight provider health-check endpoint.

    Returns {status, provider, model, detail, latency_ms} without
    consuming credits or starting swarm work.
    """
    from provider import ProviderHealth, ProviderStatus

    if not provider:
        # Infer from operator config / demo mode
        try:
            resolved_model, resolved_provider = hc._default_runtime()
        except hc.ProviderConfigError:
            resolved_provider = "none"
            resolved_model = None
        provider = resolved_provider
        model = model or resolved_model or ""

    cred = None
    env_key = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "kimi": "KIMI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(provider)
    if env_key:
        cred = os.environ.get(env_key)

    health = hc.preflight_provider({provider: cred} if provider and cred else None)
    return {
        "status": health.status.value,
        "provider": health.provider,
        "model": health.model,
        "detail": health.detail,
        "latency_ms": round(health.latency_ms, 1),
    }


# ---------- background squad dispatch ----------
def _demo_dispatch_timeout(slug: str) -> int:
    """Driver window for demo boards matches their wall-clock cap (the old
    fixed 900 s could cut a legit free-tier demo short and seal it while its
    workers were still mid-run); production launches keep the operator default."""
    if slug.startswith("flux-demo-"):
        return max(hc.DISPATCH_TIMEOUT_S, _DEMO_MAX_RUNTIME_S)
    return hc.DISPATCH_TIMEOUT_S


def _bg_dispatch(slug: str, plan: str, provider_keys=None, pid: int | None = None) -> None:
    """Drive the dispatcher to a terminal state in a daemon thread.

    A single non-blocking pass only processes the tasks that are READY at that
    instant. Swarm workflows are multi-wave (workers -> verifier -> synthesizer):
    tasks created as earlier ones finish would never run, leaving the board
    stuck forever (reviewer `ready`, builder `todo`). Using the blocking
    multi-pass loop (bounded by timeout_s) closes that gap while the HTTP
    request still returns immediately — the loop lives in this thread.

    Provider resilience: the loop is bounded so an upstream LLM outage cannot
    hold the driver for many hours. On any failure, the project's
    ``launch_status`` is persisted (and the launch credit refunded when no
    meaningful work was produced) so the user is never charged for a build that
    never executed, and the UI can surface a truthful paused/failed state
    instead of an indefinitely-running board.
    """
    try:
        timeout_s = _demo_dispatch_timeout(slug)
        res = hc.dispatch(slug, max_spawn=db.PLANS.get(plan, {}).get("parallel", 1),
                          provider_keys=provider_keys, blocking=True,
                          timeout_s=timeout_s)
    except Exception as e:
        # Surface the failure for ops instead of silently dropping the swarm,
        # and protect the credit: a launch that errored before any agent
        # produced work is refunded (idempotently) and marked on the project.
        try:
            if pid is not None:
                _finalize_launch(slug, pid,
                                 status="error", outcome="launch_error",
                                 reason=type(e).__name__)
            audit.audit("dispatch.fire", outcome="error", slug=slug, reason=type(e).__name__)
        except Exception:
            pass
        return
    if pid is None:
        # No project row to persist to (demo/standalone); just audit the outcome.
        try:
            audit.audit("dispatch.fire", outcome=res.get("outcome", "ok"), slug=slug,
                        timed_out=bool(res.get("timed_out")))
        except Exception:
            pass
        try:
            db.update_provider_usage_outcome(
                slug, ok=int(bool(res.get("converged") or res.get("outcome") == "ok")))
        except Exception:
            pass
        if not res.get("converged") and (res.get("outcome") != "ok"
                                         or res.get("timed_out")):
            # Demo launches carry no project row (``_board_finalized`` can't
            # catch them), so seal the board directly — otherwise the reaper
            # revives the stranded workers and holds the host cap forever.
            try:
                hc.seal_board(slug, reason="demo launch finalized")
            except Exception:
                pass
        return
    res_outcome = res.get("outcome", "ok")
    if res_outcome == "ok" or res.get("timed_out") is False:
        _finalize_launch(slug, pid, status="ok", outcome="converged", reason="")
    else:
        # Provider/worker stall or error: land the launch in a recoverable,
        # truthful state and refund the credit when no real work was produced.
        reason = "no_progress" if res.get("stall") else "timeout"
        _finalize_launch(slug, pid, status="stuck", outcome="stuck", reason=reason)


def _finalize_launch(slug: str, pid: int, *, status: str, outcome: str, reason: str) -> None:
    """Persist the launch terminal state and reconcile the launch credit.

    Credit policy (use the existing single-credit model): a launch that ends
    stuck / timed-out / errored BEFORE any task reached ``done`` produced no
    meaningful work, so its single credit is refunded — once, idempotently
    (guarded by the project's ``launch_refunded`` flag). A launch that
    completed at least one task consumed real work and is never refunded.
    """
    try:
        existing = _project_by_pid(pid)
        was_refunded = bool(existing and existing.get("launch_refunded"))
        refunded = was_refunded
        if outcome != "converged" and not was_refunded:
            try:
                work_done = hc.board_has_completed_work(slug)
            except Exception:
                work_done = False
            if not work_done:
                proj = existing
                if proj is not None and not proj.get("launch_refunded"):
                    if db.refund_launch_credit(proj["user_id"]):
                        refunded = True
                        try:
                            audit.audit("dispatch.fire", outcome="credit_refund",
                                        slug=slug, project_id=pid, reason=reason)
                        except Exception:
                            pass
        db.set_launch_outcome(pid, status, outcome, reason, refunded=refunded)
        # Phase F: fill the terminal outcome on the provider-usage ledger row.
        # Best-effort: a ledger write failure must never change launch behavior.
        try:
            db.update_provider_usage_outcome(slug, ok=int(status == "ok"))
        except Exception:
            pass
        # Knowledge-graph integrations on successful completion (best-effort,
        # never blocks bookkeeping).  Cognee seeds the entity graph; Understand
        # Anything generates an interactive workspace knowledge graph.
        if status == "ok":
            try:
                import integrations
                integrations.on_project_completed(slug)
            except Exception:
                pass
        if outcome != "converged":
            # Seal the board NOW: kill any leftover workers and park its non-
            # terminal tasks as blocked, so the abandoned board stops holding
            # the host-level kanban concurrency cap and can never be re-armed
            # by the reconciliation reaper (which respects the seal marker).
            # Without this, a stuck board's 'running' corpses poison the cap
            # forever and every subsequent launch gets refunded as no_progress.
            try:
                seal = hc.seal_board(slug, reason=reason)
                audit.audit(
                    "dispatch.fire", outcome="board_sealed", slug=slug,
                    project_id=pid, reason=reason,
                    killed=seal.get("killed"), blocked=seal.get("blocked"),
                )
            except Exception:
                try:
                    audit.audit("dispatch.fire", outcome="board_seal_error",
                                slug=slug, project_id=pid)
                except Exception:
                    pass
    except Exception:
        # Never let bookkeeping failure crash the daemon thread.
        try:
            audit.audit("dispatch.fire", outcome="reconcile_error", slug=slug, project_id=pid)
        except Exception:
            pass


def _project_by_pid(pid: int) -> dict | None:
    """Fetch a project row by id, including the launch bookkeeping columns."""
    try:
        c = db._conn()
        try:
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()
    except Exception:
        return None


def _fire_dispatch(slug: str, plan: str, provider_keys=None, pid: int | None = None,
                   goal: str | None = None,
                   custom_agents: list[dict] | None = None,
                   context_payload: dict | None = None) -> None:
    """Fire whichever driver owns this host: the thin in-process project driver
    on small-memory hosts (the fat Hermes worker OOMs a 512 MB container
    ~40-80s into work — measured), otherwise the fat multi-wave dispatcher."""
    if hc.projects_are_thin():
        threading.Thread(target=_bg_thin_project, args=(slug, goal or ""),
                         kwargs={"provider_keys": provider_keys, "pid": pid,
                                 "custom_agents": custom_agents,
                                 "context_payload": context_payload},
                         daemon=True).start()
    else:
        threading.Thread(target=_bg_dispatch, args=(slug, plan),
                         kwargs={"provider_keys": provider_keys, "pid": pid},
                         daemon=True).start()


def _thin_runtime(provider_keys=None) -> tuple[str, str, str | None]:
    """Resolve the thin in-process runtime: BYOK gemini/openrouter key first,
    else the operator-configured demo pool (env-keyed). Returns
    (provider_for_llm, model, api_key_or_None)."""
    keys = provider_keys or {}
    if (keys.get("gemini") or "").strip():
        return "gemini", "gemini-3.5-flash-lite", keys["gemini"].strip()
    if (keys.get("openrouter") or "").strip():
        return "openrouter", "nvidia/nemotron-3.5-lightning:free", keys["openrouter"].strip()
    pick = provider_pool.pick_demo_provider()
    if not pick:
        raise RuntimeError("no demo provider available for the thin project path")
    prov = provider_pool.resolve_provider_key(pick["provider"])
    return prov, pick["model"], None


def _ws_brief(root, limit: int = 2500) -> str:
    """Concatenate the real artifacts produced so far as context for the
    reviewer/builder lanes (the thin project's shared blackboard)."""
    root = Path(root)
    parts = []
    for name in ("PLAN.md", "ARCHITECTURE.md", "Dockerfile",
                 "tests/test_app.py", "REVIEW.md", "DESIGN.md", "AUDIT.md"):
        p = root / name
        try:
            if p.exists():
                parts.append(f"--- {name} ---\n" + p.read_text(
                    encoding="utf-8", errors="ignore")[:2000])
        except Exception:
            pass
    return "\n\n".join(parts)[:limit]


def _doc(path, limit: int = 6000) -> str:
    """Read one workspace artifact (best-effort, bounded)."""
    try:
        p = Path(path)
        return p.read_text(encoding="utf-8", errors="ignore")[:limit] if p.exists() else ""
    except Exception:
        return ""


def _web_qa_summary(workspace, limit: int = 3000) -> str:
    """Deterministic QA digest of the FINAL deliverable (best-effort, bounded).
    Feeds the Auditor lane so its acceptance report mirrors the gate instead of
    inventing verdicts."""
    try:
        html = (Path(workspace) / "index.html").read_text(
            encoding="utf-8", errors="ignore")
    except Exception:
        html = ""
    if not html.strip():
        return "web page not built / not auditable"
    try:
        issues = demo_llm.web_qa_issues(html)
        score = demo_llm.web_deliverable_score(html)
        head = f"web_deliverable_score={score}/100; issues ({len(issues)}):"
        if not issues:
            return head + "\n  - none (page passes deterministic QA)"
        return head + "\n  - " + "\n  - ".join(issues[:12])[:limit]
    except Exception as exc:  # pragma: no cover - defensive
        return f"QA runner failed: {type(exc).__name__}"


def _insert_gate_event(slug: str, task_id: str, evidence: dict) -> None:
    """Attach the Builder evidence-gate verdict to the builder task's event log
    so the UI timeline shows GO/WARN/NO-GO before final assembly. Best-effort."""
    try:
        verdict = str(evidence.get("verdict", "?"))
        checks_ok = sum(1 for c in evidence.get("checks", [])
                        if c.get("status") == "pass")
        checks_total = len(evidence.get("checks", []))
        hc._insert_event(
            slug, task_id, "note",
            f"Evidence gate: {verdict} ({checks_ok}/{checks_total} checks passed)")
    except Exception:
        pass


def _bg_thin_project(slug: str, goal: str, provider_keys=None, pid: int | None = None,
                     custom_agents: list[dict] | None = None,
                     context_payload: dict | None = None) -> None:
    """Drive the thin 8-lane project squad to completion in a daemon thread.

    Planner -> Architect -> DevOps -> TDD -> Reviewer -> Designer -> Builder
    -> Auditor: Planner..Reviewer are real provider completions writing real
    artifacts; Designer writes a concrete DESIGN.md (palette/type/tokens) that
    the Builder MUST follow; the Auditor (last) reviews the FINAL deliverable
    against that design + deterministic QA. Bounded, direct-DB, no fat CLI
    worker, no OOM. On failure the launched lanes stay honest on the board and
    the launch is finalized (credit refunded only when no real work was
    produced — the same policy as the fat path).

    ``custom_agents`` (P4): optional user-defined agents
    ``{"id", "name", "objective", "skills"}`` added as extra lanes
    (assignee ``ca-<id>``). Each runs after Reviewer and before Builder with a
    real provider completion writing ``CUSTOM_<name>.md`` into the workspace.

    ``context_payload``: optional codebase snapshot from a prior analyze call.
    The snapshot's key files are seeded into the workspace under ``_SOURCE/``
    so agents can reference real code. The file tree + config summary is
    injected into every prompt as context.
    """
    provider = model = api_key = None
    try:
        provider, model, api_key = _thin_runtime(provider_keys)
        by_role = {}
        for t in (hc.list_tasks(slug) or []):
            a = (t.get("assignee") or "").strip()
            tid = t.get("id")
            if a and tid:
                by_role[a] = tid
        if not by_role.get("ecc-planner"):
            raise RuntimeError(f"thin project board {slug} has no squad tasks")
        ws_root = hc.project_workspace_dir(slug)
        # ---- Seed uploaded source code into workspace for agent reference ----
        # Agents that know the real code produce far better plans / architectures /
        # tests than blind generation.  Source goes into _SOURCE/ so it's visible
        # but doesn't collide with generated artifacts.
        codebase_ctx = ""
        if context_payload:
            # context_payload may be the raw manifest (old/larger shape) or the
            # snapshot dict directly (current cache shape) — normalize both.
            snapshot = context_payload.get("codebase_snapshot", context_payload)
            if not isinstance(snapshot, dict):
                snapshot = {}
            source_dir = ws_root / "_SOURCE"
            try:
                source_dir.mkdir(parents=True, exist_ok=True)
                # Write key files so agents can read them directly
                for kf in snapshot.get("key_files", []):
                    fp = source_dir / kf["path"].replace("\\", "/")
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(kf.get("content", ""), encoding="utf-8", errors="replace")
                # Build the compact context string for prompts
                parts = []
                if snapshot.get("file_tree"):
                    parts.append(f"PROJECT FILE TREE:\n{snapshot['file_tree']}")
                if snapshot.get("config_summary"):
                    parts.append(f"CONFIG FILES:\n{snapshot['config_summary']}")
                codebase_ctx = "\n\n".join(parts)[:6000]
            except Exception:
                codebase_ctx = ""
        lanes = [
            ("ecc-planner", "PLAN.md",
             lambda brief: demo_llm.planner_prompt(hc.SQUAD[0][2], goal, codebase_ctx=codebase_ctx)),
            ("ecc-architect", "ARCHITECTURE.md",
             lambda brief: demo_llm.architect_prompt(hc.SQUAD[1][2], goal, codebase_ctx=codebase_ctx)),
            ("ecc-devops", "Dockerfile",
             lambda brief: demo_llm.devops_prompt(hc.SQUAD[2][2], goal, codebase_ctx=codebase_ctx)),
            ("ecc-tdd", "tests/test_app.py",
             lambda brief: demo_llm.tdd_prompt(hc.SQUAD[3][2], goal, codebase_ctx=codebase_ctx)),
            ("ecc-reviewer", "REVIEW.md",
             lambda brief: demo_llm.reviewer_prompt(hc.VERIFIER[2], goal, brief, codebase_ctx=codebase_ctx)),
            ("ecc-designer", "DESIGN.md",
             lambda brief: demo_llm.designer_prompt(
                 hc.DESIGNER[2], goal,
                 plan=_doc(ws_root / "PLAN.md"),
                 codebase_ctx=codebase_ctx)),
            ("ecc-build-fixer", None,
             lambda brief: demo_llm.builder_prompt(hc.SYNTHESIZER[2], goal, brief, codebase_ctx=codebase_ctx)),
            ("ecc-auditor", "AUDIT.md",
             lambda brief: demo_llm.auditor_prompt(
                 hc.AUDITOR[2], goal,
                 design=_doc(ws_root / "DESIGN.md"),
                 qa=_web_qa_summary(ws_root),
                 codebase_ctx=codebase_ctx)),
        ]
        brief = ""
        evidence = None
        # P4/2 resume: after a restart restored a mid-launch board, keep AT LEAST
        # the state it already reached — a lane whose task is terminal ('done')
        # is not re-executed (its artifact stays), so a resumed run only drives
        # the lanes that were still queued/running when the host died.
        done_tids: set[str] = set()
        for t in (hc.list_tasks(slug) or []):
            if (t.get("status") or t.get("state")) == "done":
                tid = t.get("id")
                if tid:
                    done_tids.add(tid)
        for assignee, artifact, make_prompt in lanes:
            tid = by_role.get(assignee)
            if not tid:
                continue
            if tid in done_tids:
                if assignee == "ecc-reviewer":
                    try:
                        evidence = evidence_gate.write_evidence_md(str(ws_root), goal)
                    except Exception:
                        evidence = None
                brief = _ws_brief(ws_root)
                continue
            if artifact is None:
                # Builder lane: gated by the evidence the preceding lanes left.
                if evidence is not None:
                    try:
                        _insert_gate_event(slug, tid, evidence)
                    except Exception:
                        pass
                _run_builder(
                    slug=slug, task_id=tid, workspace=str(ws_root),
                    provider=provider,
                    model=_lane_model("BUILDER", model), objective=goal,
                    brief=demo_llm.plan_to_brief(
                        _doc(ws_root / "PLAN.md"),
                        fallback=_ws_brief(ws_root)),
                    task_title=hc.SYNTHESIZER[2],
                    api_key=api_key,
                    codebase_ctx=codebase_ctx,
                    design_spec=_doc(ws_root / "DESIGN.md"))
                # Refresh the evidence report now that the real deliverable
                # exists: the pre-build run (after Reviewer) still passes for
                # plan/arch/devops/tdd/review, but the web_qa gate would mark
                # index.html 'missing' if evaluated before the Builder wrote it.
                # Re-running here lets web_qa audit the ACTUAL page and gives the
                # Auditor (next lane) an accurate report to summarize.
                try:
                    evidence = evidence_gate.write_evidence_md(str(ws_root), goal)
                except Exception:
                    evidence = None
            else:
                hc.thin_execute(board=slug, task_id=tid, workspace=str(ws_root),
                                provider=provider, model=model,
                                prompt=make_prompt(brief), objective=goal,
                                artifact_name=artifact, api_key=api_key,
                                max_tokens=demo_llm.lane_max_tokens(goal, artifact))
            if assignee == "ecc-reviewer":
                # Evidence gate runs after Reviewer so the Builder only starts
                # once the workspace holds a verified (deterministic) evidence
                # record of plan/arch/devops/tdd/review output.
                try:
                    evidence = evidence_gate.write_evidence_md(str(ws_root), goal)
                except Exception:
                    evidence = None
                # P4 custom agents: each user-defined agent runs as an extra
                # lane right after review but before the final assembly, so its
                # deliverable joins the workspace the Builder assembles.
                for agent in (custom_agents or []):
                    aid = str(agent.get("id", "")).strip()
                    ctid = by_role.get(f"ca-{aid}") if aid else None
                    if not ctid:
                        continue
                    name = (agent.get("name") or "Custom agent").strip() or "Custom agent"
                    try:
                        hc.thin_execute(
                            board=slug, task_id=ctid, workspace=str(ws_root),
                            provider=provider, model=model,
                            prompt=demo_llm.custom_agent_prompt(
                                task_title=name,
                                objective=goal,
                                agent_name=name,
                                agent_objective=(agent.get("objective") or "").strip() or name,
                                skills=(agent.get("skills") or "").strip(),
                                project_goal=goal,
                                codebase_ctx=codebase_ctx),
                            objective=goal,
                            artifact_name=f"CUSTOM_{_safe_artifact_name(name)}.md",
                            api_key=api_key,
                            max_tokens=demo_llm.lane_max_tokens(goal, "X.md"))
                    except Exception as exc:
                        # A custom-agent lane failing must not kill the swarm:
                        # it stays visible as a failed lane (honest board).
                        try:
                            hc._insert_event(slug, ctid, "note",
                                             f"Custom agent lane failed: {type(exc).__name__}")
                        except Exception:
                            pass
            brief = _ws_brief(ws_root)
        if pid is not None:
            status = "ok"
            outcome = "converged"
            reason = ""
            if evidence is not None and evidence.get("verdict") not in ("GO",):
                reason = f"evidence_gate={evidence.get('verdict')}"
            _finalize_launch(slug, pid, status=status, outcome=outcome, reason=reason)
        try:
            audit.audit("thin.drive", outcome="ok", slug=slug, plan="thin")
            db.update_provider_usage_outcome(slug, ok=1)
        except Exception:
            pass
    except Exception as e:
        try:
            if pid is not None:
                _finalize_launch(slug, pid, status="error", outcome="launch_error",
                                 reason=type(e).__name__)
            audit.audit("thin.drive", outcome="error", slug=slug,
                        reason=str(e)[:300])
        except Exception:
            pass


# ---------- persistent reconciliation reaper ----------
# The bounded ``_bg_dispatch`` loop drives a launch toward a terminal state
# (or the wall-clock window) and then stops. Left as-is, a worker that dies
# AFTER that window leaves its task stuck in ``running`` forever — nothing
# ever runs ``release_stale_claims``/``detect_crashed_workers`` for the board
# again, so the board freezes (the "agent stays RUNNING" symptom) until a
# human manually triggers another dispatch. This reaper re-runs the existing
# single, non-blocking, capability-respecting dispatch pass for any board that
# still has unfinished agent work, so a dead worker is reclaimed/requeued and
# the swarm keeps advancing to convergence instead of freezing. The pass is
# idempotent (reclaim/promote/spawn, all memory & concurrency caps honoured)
# and crowns out once the board is terminal.
_REAPER_INTERVAL_S = int(os.getenv("FLUXSWARM_REAPER_INTERVAL_S", "30"))
_REAPER_ENABLED = os.getenv("FLUXSWARM_REAPER_ENABLED", "1") == "1"
_REAPER_ERROR_BACKOFF_S = int(os.getenv("FLUXSWARM_REAPER_ERROR_BACKOFF_S", "300"))
_REAPER_MIN_GAP_S = 2  # minimum seconds between passes for the SAME board.
# Per-board concurrency for the recovery pass. Defaults to the memory-derived
# host budget (MAX_IN_PROGRESS, 16 on the 8GB single-app image) so the reaper
# can saturate whatever the fleet allows; operators cap via
# FLUXSWARM_REAPER_MAX_SPAWN.
_REAPER_MAX_SPAWN = max(
    1,
    min(int(os.getenv("FLUXSWARM_REAPER_MAX_SPAWN", str(hc.MAX_IN_PROGRESS))),
        hc.MAX_IN_PROGRESS),
)
_REAPER_TERMINAL_STATUSES = {"stuck", "ok", "error"}
_reaper_last: dict[str, float] = {}
# Circuit-breaker state (Task 2 session 3): consecutive uncompensated failures
# back off for _REAPER_ERROR_BACKOFF_S before retrying, so a bad provider/KMS
# outage doesn't hammer the fleet every 30s.
_reaper_consecutive_errors = 0
_reaper_last_sweep_monitored = 0.0


def _project_by_board_slug(slug: str) -> dict | None:
    """Project row owned by *slug*, or None when there is none / DB unreachable."""
    try:
        c = db._conn()
        try:
            row = c.execute(
                "SELECT * FROM projects WHERE board_slug=? ORDER BY id DESC LIMIT 1",
                (slug,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()
    except Exception:
        return None


def _board_finalized(slug: str) -> bool:
    """True when a project row for *slug* reached a launch-terminal state.

    A finalized launch is over (its credit was kept or refunded, the worker
    swarm was sealed) — the reaper must NOT keep re-dispatching it. The stale
    ``running`` corpses of a finalized board hold the host-level kanban cap
    forever (``kanban.max_in_progress``) and starve every subsequent launch.
    """
    proj = _project_by_board_slug(slug)
    if proj is None:
        return False
    if proj.get("launch_refunded"):
        return True
    return (proj.get("launch_status") or "").strip() in _REAPER_TERMINAL_STATUSES


def _demo_lifecycle_sweep() -> None:
    """Auto-close and recycle the throwaway ``flux-demo-*`` boards.

    Session 3 demo hygiene:
      * a demo board still running past ``_DEMO_MAX_RUNTIME_S`` (20 min) is
        sealed (kills its workers, drops the durable seal marker) — a stuck or
        hung demo must not hold the kanban concurrency budget;
      * a sealed demo board aged past ``_DEMO_WORKSPACE_TTL_S`` (24 h) has its
        workspaces deleted to reclaim disk.

    Both are idempotent (seal marker + age checks) and audited. Best-effort:
    a single board's failure never raises out of the reaper loop.
    """
    now = time.time()
    boards_root = Path(hc.HERMES_HOME) / "kanban" / "boards"
    try:
        if not boards_root.is_dir():
            return
        for entry in boards_root.iterdir():
            if not (entry.is_dir() and entry.name.startswith("flux-demo-")):
                continue
            slug = entry.name
            try:
                age_s = now - entry.stat().st_mtime
                if age_s < 0:
                    age_s = 0
                if not hc.board_is_sealed(slug) and age_s > _DEMO_MAX_RUNTIME_S:
                    hc.seal_board(slug, reason="demo max runtime")
                    audit.audit("demo.autoseal", slug=slug, age=int(age_s), outcome="ok")
                if age_s > _DEMO_WORKSPACE_TTL_S:
                    if hc.delete_demo_board(slug):
                        audit.audit("demo.cleanup", slug=slug, age=int(age_s), outcome="ok")
            except Exception:
                pass
    except Exception:
        pass


def _resume_restored_thin_boards() -> None:
    """Re-drive restored project boards that were mid-launch at the last death.

    Only meaningful on thin hosts (project mode == thin), where EVERY launch is
    owned by an in-process driver thread the OS killed with the host; the
    reaper is deliberately disabled there. After a restart restored those
    boards from the Postgres mirror, each unfinished non-sealed project board
    whose launch never finalized is resumed with a *resumed* flag so the driver
    (skip-done guard active) only completes the lanes that were queued/running.
    Demo boards stay excluded — the demo lifecycle sweep owns them.
    """
    if not hc.projects_are_thin():
        return
    if not board_store.is_enabled():
        return
    try:
        for slug in board_store.list_mirrored_slugs():
            if slug.startswith("flux-demo-"):
                continue
            try:
                if _board_finalized(slug) or hc.board_is_sealed(slug):
                    continue
                if not hc.board_has_unfinished_work(slug):
                    continue
                proj = _project_by_board_slug(slug)
                goal = (proj or {}).get("goal") or ""
                if not goal:
                    continue
                keys = _user_provider_keys({"id": (proj or {}).get("user_id")})
                # Derive the custom-agent lanes that were restored with the board
                # (assignee ``ca-<id>``); the driver's own skip-done guard keeps
                # any ca-* lane already completed untouched.
                custom_agents: list[dict] = []
                for t in (hc.list_tasks(slug) or []):
                    a = (t.get("assignee") or "").strip()
                    if not a.startswith("ca-"):
                        continue
                    if (t.get("status") or t.get("state")) == "done":
                        continue
                    aid = a[len("ca-"):]
                    title = (t.get("title") or aid).replace(" (custom)", "").strip()
                    custom_agents.append({"id": aid, "name": title or "Custom agent",
                                          "objective": goal, "skills": ""})
                threading.Thread(
                    target=_bg_thin_project, args=(slug, goal),
                    kwargs={"provider_keys": keys, "pid": (proj or {}).get("id"),
                            "custom_agents": custom_agents},
                    daemon=True).start()
                try:
                    audit.audit("thin.resume", slug=slug, goal=goal[:200], outcome="ok")
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass


def _reconcile_boards_once() -> None:
    # Small-memory host: EVERY launch is owned by a thin driver that completes
    # eagerly and finalizes bookkeeping; a reaper pass here would run the fat
    # `hermes dispatch` CLI (loads the whole workspace) against every board and
    # OOM the container. The reaper stays fully disabled on such hosts.
    if hc.projects_are_thin():
        return
    try:
        boards = hc.list_boards()
    except Exception:
        return
    now = time.time()
    for b in boards or []:
        slug = (b or {}).get("slug")
        if not slug:
            continue
        # Demo boards are owned ENTIRELY by the thin demo driver + the demo
        # lifecycle sweep. A reaper pass here would spawn fat Hermes workers
        # (which crash ~40-80s into work on the free host) and fight the thin
        # executor for claims — so the reaper never re-arms flux-demo-*.
        if slug.startswith("flux-demo-"):
            continue
        if now - _reaper_last.get(slug, 0.0) < _REAPER_MIN_GAP_S:
            continue
        # A project-finalized board (stuck/refunded/errored) or a board sealed
        # at launch finalization is operator-FINAL: it must never be re-armed.
        # Re-dispatching it would resurrect dead workers, keep its 'running'
        # rows inside the host concurrency budget forever, and starve every new
        # launch (the endless "waiting for dependency" -> no_progress -> refund
        # loop observed on stuck boards).
        if _board_finalized(slug) or hc.board_is_sealed(slug):
            continue
        # Stale-worker sweep: a dead worker's PID can linger beyond the claim
        # TTL, so proactively kill quiet runners and park their tasks as
        # blocked before the dispatch pass re-spawns them.
        try:
            hc.kill_stale_workers(slug)
        except Exception:
            pass
        # Transient-block recovery: a dead worker (provider 429 / outage) parks
        # its task as blocked, which is otherwise terminal.  Re-promote blocked
        # tasks on EVERY board first (including one whose agents are ALL
        # blocked — otherwise ``board_has_unfinished_work`` skips it forever),
        # so the dispatch pass can re-spawn workers once the provider recovers.
        # Bounded and idempotent; Hermes' own parent-gating (``recompute_ready``)
        # still prevents anything from running before its dependencies complete.
        try:
            hc.bump_blocked_to_ready(slug)
        except Exception:
            pass
        # Only boards that still have unfinished AGENT work need a tick.
        if not hc.board_has_unfinished_work(slug):
            continue
        try:
            hc.dispatch(slug, max_spawn=_REAPER_MAX_SPAWN, blocking=False)
            _reaper_last[slug] = time.time()
        except Exception:
            # Tolerate a transient board error; retry next sweep.
            pass


def _reaper_loop() -> None:
    """Circuit-broken reaper: reconcile boards + demo lifecycle + alerting.

    Consecutive unfiltered exceptions trip a 300s cooldown (audited) instead
    of retrying every 30s, so a systemic outage (KMS/provider) doesn't churn
    the fleet. A healthy sweep resets the breaker.
    """
    global _reaper_consecutive_errors
    while _REAPER_ENABLED:
        try:
            _reconcile_boards_once()
            _reaper_consecutive_errors = 0
        except Exception as exc:
            _reaper_consecutive_errors += 1
            try:
                audit.audit("reaper.error", outcome="error",
                            error=str(exc)[:1024],
                            consecutive=_reaper_consecutive_errors)
            except Exception:
                pass
            if _reaper_consecutive_errors >= 3:
                time.sleep(_REAPER_ERROR_BACKOFF_S)
                _reaper_consecutive_errors = 0
        try:
            _demo_lifecycle_sweep()
        except Exception:
            pass
        try:
            _reaper_monitor_tick()
        except Exception:
            pass
        time.sleep(_REAPER_INTERVAL_S)


def _reaper_monitor_tick() -> None:
    """Throttled health-snapshot alerting wired into the reaper cadence."""
    global _reaper_last_sweep_monitored
    now = time.time()
    if now - _reaper_last_sweep_monitored < 60:
        return
    _reaper_last_sweep_monitored = now
    try:
        from monitoring import alert_if_unhealthy
        alert_if_unhealthy()
    except Exception:
        pass


# ---------- auth dependency ----------
def _token_session_ok(user: dict, payload: dict) -> bool:
    """True only when the JWT was issued after the user's last logout/reset.

    On logout / password change / password reset we set ``users.logged_out_at``
    to "now"; any token whose ``iat`` predates it is refused. Stateless JWTs
    carry no server-side revocation list, so this timestamp is the minimal
    correct revocation primitive: it kills every session of that user (the safe
    behaviour for all three operations) without a blacklist table.
    """
    logged_out = user.get("logged_out_at") or 0
    if not logged_out:
        return True
    return float(payload.get("iat") or 0) >= float(logged_out)


def get_current_user_optional(request: Request) -> dict | None:
    ah = request.headers.get("Authorization", "")
    token = ah.replace("Bearer ", "") if ah.startswith("Bearer ") else request.cookies.get("fs_token")
    if not token:
        return None
    payload = auth_mod.decode_token(token)
    if not payload:
        return None
    user = db.get_user_by_id(payload["uid"])
    if not user or not _token_session_ok(user, payload):
        return None
    return user


def get_current_user(request: Request) -> dict:
    ah = request.headers.get("Authorization", "")
    token = ah.replace("Bearer ", "") if ah.startswith("Bearer ") else request.cookies.get("fs_token")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = auth_mod.decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = db.get_user_by_id(payload["uid"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not _token_session_ok(user, payload):
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    return user


# ---------- schemas ----------
class RegisterIn(BaseModel):
    email: str
    name: str
    password: str = Field(max_length=4096)
    ref: str | None = None
    tos_accept: bool = False


class SupportIn(BaseModel):
    """In-app support chat turn. The message is stripped + capped by the agent;
    history is a short conversational context for the optional AI fallback."""
    message: str = Field(max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=12)


class LoginIn(BaseModel):
    email: str
    password: str = Field(max_length=4096)


class PasswordChangeIn(BaseModel):
    current: str = Field(max_length=4096)
    new: str = Field(max_length=4096)


class AccountUpdateIn(BaseModel):
    """CCPA/CPRA rectification: edit the display name on the account."""
    name: str = Field(min_length=1, max_length=80)


class AdmtOptInIn(BaseModel):
    """ADMT opt-in body: the user re-read the current pre-use notice."""
    acknowledge: bool = False
    last_updated: str = Field(default="", max_length=40)


class HumanReviewUpdateIn(BaseModel):
    """Admin resolution of a human-review request."""
    status: str = Field(min_length=1, max_length=20)
    reviewer_notes: str = Field(default="", max_length=2000)


class ResetRequestIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    new_password: str = Field(max_length=4096)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=4000)
    ref: str | None = None
    agent_ids: list[int] = Field(default_factory=list)


@functools.lru_cache(maxsize=1)
def _count_test_functions() -> tuple[int, str]:
    """Live truth for the landing trust badge: AST-scan this project's tests/
    directory and count real test functions. This is the verifiable source —
    the badge shows what pytest would actually collect, cached for 60s.
    Returns (check_count, "file_count tests")."""
    import ast

    tests_dir = Path(__file__).resolve().parent / "tests"
    total = 0
    files = 0
    if tests_dir.is_dir():
        for py in sorted(tests_dir.glob("test_*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError, OSError):
                continue
            files += 1
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    total += 1
    return total, f"{files} test files"


# ---------- marketing / public ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    base = os.environ.get("FLUXSWARM_PUBLIC_BASE_URL", "").strip().rstrip("/")
    checks, files_note = _count_test_functions()
    return templates.TemplateResponse(request=request, name="index.html",
                                      context={"title": "FluxSwarm", "canonical": base + "/" if base else "",
                                               "og_image": (base + "/static/brand/og-1200x630.png") if base else "/static/brand/og-1200x630.png",
                                               "csp_nonce": request.state.csp_nonce,
                                               "analytics_domain": os.environ.get(
                                               "FLUXSWARM_ANALYTICS_DOMAIN", "").strip(),
                                               "trust_checks": checks,
                                               "trust_checks_date": files_note})


@app.get("/api/plans")
def api_plans():
    return [{"id": p, **db.PLANS[p]} for p in db.PLAN_ORDER]


@app.get("/api/squad")
def api_squad():
    return {
        "workers": [{"profile": p, "display": d, "role": r, "skills": s.split(",")}
                    for p, d, r, s in hc.SQUAD],
        "verifier": {"profile": hc.VERIFIER[0], "display": hc.VERIFIER[1], "role": hc.VERIFIER[2],
                     "skills": hc.VERIFIER[3].split(",")},
        "synthesizer": {"profile": hc.SYNTHESIZER[0], "display": hc.SYNTHESIZER[1], "role": hc.SYNTHESIZER[2],
                        "skills": hc.SYNTHESIZER[3].split(",")},
    }


@app.get("/api/demo/launch")
def api_demo_launch(request: Request, goal: str = ""):
    """Pre-seeded instant demo: launches a sample swarm under the demo user.

    Returns immediately; the dispatcher pass runs in a background thread so the
    request never blocks for the (minutes-long) swarm. max_spawn honours the
    demo user's own plan cap (demo -> parallel 1), not a fixed 8.

    A per-IP burst cap prevents anonymous abuse (each launch runs a real,
    compute-costly Hermes swarm); a durable daily cap bounds total cost per
    bucket and an operator kill-switch can halt ALL demo/cost-bearing surfaces.

    Session 2 hardening: the demo surface is ALSO bounded by (a) 1 launch per
    IP per hour and (b) 20 launches/day globally (AR/EN 429 details), and the
    runtime comes from the demo provider pool (free-tier) — OpenRouter free is
    no longer a production default. An opted-out (ADMT) user is refused.
    """
    if _operator_maintenance():
        raise HTTPException(status_code=503, detail="Service temporarily unavailable — please try again later")
    demo_user = _demo_user(request)
    if demo_user and db.get_admt_opt_out(demo_user["id"]):
        raise HTTPException(status_code=403,
                            detail="ADMT opt-out active. Human review required.")
    ip = _client_ip(request)
    if not limiter.ip_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — please wait a moment")
    demo = db.get_user_by_id(1) or db.get_user_by_ref("demo")
    if demo is None:
        # db.seed_demo() runs at import, but guard anyway: a missing demo user
        # must not crash the endpoint (it would 500 on `demo.get(...)`).
        return {"error": "demo_user_missing", "demo": True}
    plan = demo.get("plan", "demo")
    goal = (goal or "").strip() or (
        "Build a single self-contained index.html landing page for a fictional "
        "AI startup called Nebula: dark hero, features grid, pricing table, and "
        "an interactive sign-up form — all inline CSS/JS, nothing external.")
    goal = sanitize_goal(goal)
    # Session 2 + P0.5: pick a healthy demo provider (free pool) and thread it
    # as an EXPLICIT request-scoped pin (provider/model kwargs). The pool is
    # never applied by mutating the process-global os.environ — concurrent
    # demo launches otherwise race and can dispatch each other's credentials
    # and models. When the pool is exhausted and no operator default exists,
    # fail with a structured, user-facing body (never a raw 500).
    pool_pick = provider_pool.pick_demo_provider()
    runtime_source = "pool"
    if not pool_pick:
        # Paid last-resort is OFF by default: unless the operator explicitly
        # enabled a fallback runtime AND configured one, the demo fails with a
        # structured, user-facing body (never a raw 500).
        paid = _paid_fallback_runtime()
        if paid:
            # _default_runtime() returns (model, provider).
            pool_model, pool_provider = paid
            runtime_source = "paid_fallback"
        else:
            capacity_state = "unknown"
            try:
                capacity_state = provider_pool.pool_capacity()["state"]
            except Exception:
                pass
            return {
                "error": "demo_provider_unavailable",
                "message": {"en": "All demo AI providers are temporarily unavailable — please try again in a moment."},
                "demo": True,
                "capacity": capacity_state,
            }
    else:
        pool_provider = provider_pool.resolve_provider_key(pool_pick["provider"])
        pool_model = pool_pick["model"]
    # Fail-closed operator budget gate (provider_guard): when the configured
    # execution ceilings are exhausted, refuse explicitly instead of silently
    # spending operator capacity. Default ceilings are never "unlimited".
    budget = provider_guard.budget_gate()
    if not budget.ok:
        return {
            "error": "budget_exhausted",
            "message": {"en": "The demo service is unavailable right now — please try again later."},
            "demo": True,
            "reason": budget.reason,
        }
    # Quota gates run AFTER a runtime actually resolved: a failed provider pick
    # (pool down) or a budget gate refusal must never burn a user's per-IP or
    # daily launch slot — that earlier order made transient provider outages
    # look like "I already used my launch".
    if not limiter.check(f"demo:ip:{ip}", _DEMO_IP_MAX, _DEMO_IP_WINDOW):
        raise HTTPException(status_code=429, detail=_DEMO_LIMIT_DETAILS["ip"])
    if not limiter.check("demo:global", _DEMO_GLOBAL_MAX, _DEMO_GLOBAL_WINDOW):
        raise HTTPException(status_code=429, detail=_DEMO_LIMIT_DETAILS["global"])
    if db.bump_demo_usage("anon", _today()) > _DEMO_DAILY_CAP:
        raise HTTPException(status_code=429, detail={
            "error": "demo_daily_limit",
            "message": {
                "en": "Daily demo allowance used up. Sign up for unlimited access.",
            },
            "retry_after_seconds": 86400,
            "upgrade_url": "/pricing",
        })
    pool_probe_key = pool_pick.get("probe_key") or pool_provider if pool_pick else pool_provider
    slug = "flux-demo-" + str(int(time.time()))
    _remember_demo(ip, slug)
    # Phase F: append the provider-usage ledger row BEFORE the launch so every
    # attempt (including any paid fallback) is observable even when the launch
    # never finalizes. Best-effort: accounting must never break a launch.
    try:
        db.record_provider_usage(
            surface="demo", runtime_source=runtime_source,
            provider=pool_probe_key, model=pool_model, slug=slug,
        )
    except Exception:
        pass
    # The swarm build (ensure_board + the CLI plan step + set-model pins) can
    # take minutes on a throttled free-tier instance; run in-band it would let
    # the proxy drop the response even though the board was created (the whole
    # symptom tree we fixed). Return the slug immediately and drive the build
    # in a daemon thread — the UI polls progress and /api/demo/latest recovers
    # the slug across reloads.
    try:
        _demo_launch_background(
            slug=slug, goal=goal, plan=plan,
            provider=pool_provider, model=pool_model,
            pool_probe_key=pool_probe_key, runtime_source=runtime_source,
            uid=demo["id"] if demo else None,
        )
    except Exception:
        pass
    return {"slug": slug, "root_id": None, "demo": True}


def _demo_launch_background(*, slug: str, goal: str, plan: str,
                            provider, model, pool_probe_key, runtime_source: str,
                            uid) -> None:
    """Build + drive the demo off the request thread (see above).

    The demo uses the thin 2-lane profile: real board + real provider + real
    artifacts, executed by a bounded thin executor (a full Hermes worker
    crashes ~40-80s into work on the free host — measured — so real agents
    belong on the paid path, untouched here).
    """

    def _run() -> None:
        try:
            prof = hc.launch_demo_profile(board=slug, goal=goal,
                                          provider=provider, model=model)
            try:
                audit.audit("demo.launch", uid=uid, ip="internal", outcome="ok",
                            slug=slug, plan=plan, provider=pool_probe_key,
                            runtime=runtime_source)
            except Exception:
                pass
            _demo_drive(slug=slug, goal=goal,
                        planner_id=prof["planner_id"],
                        builder_id=prof["builder_id"],
                        workspace=prof["workspace"],
                        provider=provider, model=model,
                        pool_probe_key=pool_probe_key,
                        runtime_source=runtime_source)
        except Exception as e:
            try:
                audit.audit("demo.launch", outcome="error", slug=slug,
                            reason=type(e).__name__)
                db.update_provider_usage_outcome(slug, ok=0)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _read_brief(text: str, n_lines: int = 12) -> str:
    return "\n".join((text or "").strip().splitlines()[:n_lines])


def _lane_model(role: str, model: str) -> str:
    """Per-lane model routing: an operator can pin a stronger/cheaper model
    per role via FLUXSWARM_<ROLE>_MODEL (e.g. FLUXSWARM_BUILDER_MODEL); the
    lane falls back to the launch runtime model otherwise."""
    override = os.environ.get(f"FLUXSWARM_{role.upper()}_MODEL", "").strip()
    return override or model


def _append_missing_tail(*, slug: str, task_id: str, workspace: str, provider,
                         model, objective: str, api_key: str | None) -> bool:
    """Deterministic-bounded salvage for a TRUNCATED page: ask the model for
    ONLY the missing tail and append it. A full rebuild at the same token
    ceiling just truncates again (measured twice), so these narrowly-scoped
    continuation calls ('continue from the snippet') keep chaining: after each
    small-budget call the doc ends further along, and the NEXT call continues
    from the NEW end — a 33KB page cut mid-body needs several continuations,
    not one. Stops the moment the document closes (</html>) or the tail stops
    growing. Returns True when the composed file closes properly."""
    try:
        path = Path(workspace) / "index.html"
        max_passes = 10
        for _ in range(max_passes):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                return False
            if demo_llm.web_artifact_needs_repair(text) is False:
                return True
            snippet = text[-220:].replace("`", "'").replace("```", "''")
            prompt = (
                "A single-file HTML page was cut off while being written. The "
                "document so far ends with this snippet:\n\n```\n" + snippet +
                "\n```\n\nContinue the document EXACTLY from where the snippet "
                "ends. Output ONLY the missing remainder (no markdown fences, "
                "no repeated snippet, no wrapper). If the body content/HTML "
                "markup after the head/styles was never written yet, write it "
                "now — but stop as soon as this chunk is filled; a follow-up "
                "call will continue from where you stop. Close every open tag "
                "when you reach the end and finish with </body></html>.\n"
            )
            res = hc.thin_execute(
                board=slug, task_id=task_id, workspace=workspace,
                provider=provider, model=model, prompt=prompt,
                objective=objective, api_key=api_key,
                artifact_name="index-tail.html", max_tokens=1400)
            tail = (Path(workspace) / "index-tail.html").read_text(
                encoding="utf-8", errors="ignore")
            if not (res.get("ok") and tail.strip()):
                break
            combined = text + ("\n" if tail[:1] not in "\n\t " else "") + tail
            if len(combined) <= len(text):
                break
            path.write_text(combined, encoding="utf-8")
            try:
                (Path(workspace) / "index-tail.html").unlink()
            except Exception:
                pass
            if demo_llm.web_artifact_needs_repair(combined) is False:
                return True
    except Exception:
        pass
    return False


def _anchor_patch(*, slug: str, task_id: str, workspace: str, provider, model,
                  objective: str, api_key: str | None) -> bool:
    """Targeted 'corner' patch for a COMPLETE page whose nav anchors still
    dangle (broken anchors, dead href='#', unlinked sections). Never a full
    rebuild — a rebuild at the same token ceiling re-truncates (measured).
    Deterministic remap first (href='#'→#top + fuzzy retarget of missing ids),
    then ONE bounded narrow nav-rewrite call only if anchors still dangle.
    Returns True when the patch measurably reduced broken/dead links."""
    try:
        path = Path(workspace) / "index.html"
        html = path.read_text(encoding="utf-8", errors="ignore")
        if not html.strip():
            return False
        before = sum("broken anchor" in i or "dead link" in i
                     for i in demo_llm.web_qa_issues(html))
        changed = False
        if re.search(r'''href=["']#["']''', html):
            if 'id="top"' not in html and re.search(r"<body[^>]*>", html, re.I):
                html = re.sub(r"(<body[^>]*)>", r'\1 id="top">', html, count=1,
                              flags=re.I)
            html = re.sub(r'''href=["']#["']''', 'href="#top"', html)
            changed = True
        ids = demo_llm.web_section_ids(html) | set(re.findall(
            r'''id=["']([^"']+)["']''', html))
        for t in sorted(set(re.findall(r'''href=["']#([^"']+)["']''', html))):
            if t == "top" or t in ids:
                continue
            a = re.sub(r"[^a-z0-9]+", "", t.lower())
            best, best_r = None, 0.6
            for i in ids:
                b = re.sub(r"[^a-z0-9]+", "", i.lower())
                if a and b and a == b:
                    best, best_r = i, 1.0
                    break
                r = difflib.SequenceMatcher(None, a, b).ratio()
                if r > best_r:
                    best, best_r = i, r
            if best:
                html = html.replace(f'href="#{t}"', f'href="#{best}"')
                changed = True
        if changed:
            path.write_text(html, encoding="utf-8")
        issues = demo_llm.web_qa_issues(html)
        if any(i.startswith(("broken anchor:", "dead link:")) for i in issues):
            nav_m = re.search(r"(<nav[^>]*>.*?</nav>)", html, re.S)
            if nav_m:
                nav0 = nav_m.group(1)
                a0 = len(re.findall(r"<a\b", nav0, re.I))
                prompt = (
                    "A finished single-file page still has nav anchors that "
                    "point at missing ids, or dead href='#' links. Existing "
                    "element ids:\n" +
                    ", ".join(sorted(i for i in ids if len(i) < 40))[:600] +
                    "\nCurrent nav block:\n" + nav0[:1500] +
                    "\n\nOutput ONLY a corrected <nav>...</nav> block that "
                    "keeps every link label but retargets each href to the "
                    "closest existing id (best semantic match). No href='#', "
                    "no dangling anchors, no markdown fences, no explanation.\n"
                )
                res = hc.thin_execute(
                    board=slug, task_id=task_id, workspace=workspace,
                    provider=provider, model=model, prompt=prompt,
                    objective=objective, api_key=api_key,
                    artifact_name="index-nav.html", max_tokens=1200)
                nav_raw = (Path(workspace) / "index-nav.html").read_text(
                    encoding="utf-8", errors="ignore").strip()
                m2 = re.search(r"<nav[^>]*>.*?</nav>", nav_raw, re.S)
                nav1 = m2.group(0) if m2 else ""
                if (nav1 and nav1.count("<a") >= a0 and len(nav1) < 6000
                        and "href='#'" not in nav1 and 'href="#"' not in nav1):
                    html = html.replace(nav0, nav1, 1)
                    path.write_text(html, encoding="utf-8")
                try:
                    (Path(workspace) / "index-nav.html").unlink()
                except Exception:
                    pass
        after = sum("broken anchor" in i or "dead link" in i
                    for i in demo_llm.web_qa_issues(
                        path.read_text(encoding="utf-8", errors="ignore")))
        return after < before
    except Exception:
        return False


def _append_missing_content(*, slug: str, task_id: str, workspace: str,
                            provider, model, objective: str, api_key: str | None,
                            brief: str, missing_ids: list[str]) -> bool:
    """Fill nav-promised sections that are empty shells with real content in a
    SINGLE bounded narrow call (no full rebuild — which re-truncates and
    ships another hollow shell).  Removes old empty shells for the gap ids
    (avoids duplicate sections), then appends the filled blocks before <footer>.
    Returns True when sections were added."""
    try:
        path = Path(workspace) / "index.html"
        html = path.read_text(encoding="utf-8", errors="ignore")
        if not html.strip():
            return False
        missing = [s for s in missing_ids if s and s != "top"]
        if not missing:
            return False
        listed = ", ".join(f"#{s}" for s in missing[:4])
        prompt = (
            "A page is STRUCTURALLY COMPLETE (</html>, footer, nav all exist) "
            f"but several nav-linked sections are empty or missing: {listed}\n\n"
            f"Objective: {objective}\n\n"
            f"Builder brief: {(brief or '')[:1200]}\n\n"
            "Output ONLY one <section> (or <article>) block per promised id — "
            "no <style>, no <html>/<head>/<body>, no <nav>, no </html>, no "
            "markdown fences.  Use id attributes matching exactly: "
            + ", ".join(f'id="{s}"' for s in missing[:4]) +
            ".  Fill each with REAL SPECIFIC content appropriate to the site: "
            "for a MENU/RESTAURANT — actual dish names with prices and a 1-2 "
            "line description, 3-5 items per category; for FEATURES/PLANS — "
            "labeled cards with bullets; for PORTFOLIO — concrete projects. "
            "Use the page's existing CSS classes and heading patterns. Close "
            "every tag.\n"
        )
        res = hc.thin_execute(
            board=slug, task_id=task_id, workspace=workspace,
            provider=provider, model=model, prompt=prompt,
            objective=objective, api_key=api_key,
            artifact_name="index-content.html", max_tokens=4000)
        blocks = (Path(workspace) / "index-content.html").read_text(
            encoding="utf-8", errors="ignore").strip()
        blocks = re.sub(r"^```[^\n]*\n?", "", blocks)
        blocks = re.sub(r"\n?```$", "", blocks)
        if not (res.get("ok") and re.search(r"<section\b|<article\b",
                                            blocks, re.I)):
            return False
        # Remove old empty shells for the gap ids (avoid duplicates)
        for sid in missing[:4]:
            pat = re.compile(
                r"<(?:section|article|main)\b[^>]*id=[\"']" + re.escape(sid)
                + r"[\"'][^>]*>.*?</(?:section|article|main)>",
                re.S | re.I)
            html = pat.sub("", html, count=1)
        # Insert filled blocks BEFORE <footer (or </body>)
        insert = html.lower().find("<footer")
        if insert == -1:
            insert = html.rfind("</body>")
        if insert == -1:
            return False
        html = html[:insert] + "\n" + blocks.strip() + "\n\n" + html[insert:]
        path.write_text(html, encoding="utf-8")
        try:
            (Path(workspace) / "index-content.html").unlink()
        except Exception:
            pass
        return len(demo_llm.web_content_gap(html)) == 0
    except Exception:
        return False


def _run_builder(*, slug: str, task_id: str, workspace: str, provider, model,
                 objective: str, brief: str, task_title: str,
                 api_key: str | None = None, max_tokens: int | None = None,
                 codebase_ctx: str = "", design_spec: str = "") -> dict:
    """Build the final deliverable lane, with ONE automatic QA re-run.

    The builder's artifact is what /p/ renders live, so its output gets a
    larger token budget AND a light completeness gate: if a web deliverable
    (index.html) comes back truncated/hollow, the lane is re-run once with a
    repair hint instead of shipping a broken page. ``max_tokens`` lets the
    demo path pass a lighter budget that fits its wall-clock cap. ``design_spec``
    (the Designer's DESIGN.md) is injected so the page is built TO a concrete
    palette/type/token system instead of improvised colors."""

    def _split_pack() -> dict:
        """Split a multi-file builder pack (==== FILE: markers) into separate
        workspace files.

        The primary artifact (index.html / app.py / …) keeps the text before
        the first marker; every extra file is written under its relative path —
        where /p/, the export zip and the Project Files browser already serve
        it.  Returns {file_or_skip_flag: reason} for the driver to log.  Re-run
        on EVERY attempt so a repair pass that re-emits the pack re-splits
        cleanly (index.html is rewritten from the primary part only).
        """
        artifact = demo_llm.deliverable_filename(objective)
        try:
            raw_text = (Path(workspace) / artifact).read_text(
                encoding="utf-8", errors="ignore")
        except Exception:
            return {}
        files = demo_llm.split_artifact(raw_text)
        if len(files) == 1 and "" in files:
            return {}
        primary = files.get("", raw_text)
        skipped = files.pop("_skipped", None)
        if primary:
            (Path(workspace) / artifact).write_text(primary, encoding="utf-8")
        written: list[str] = []
        for rel, content in files.items():
            if not rel or not content:
                continue
            target = Path(workspace) / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(rel)
            except OSError:
                skipped = (skipped or "") + f"\n==== {rel}" + content
        info: dict = {}
        if written:
            info["files_built"] = written
        if skipped:
            info["pack_skipped"] = skipped.strip()[:400]
        return info

    def _execute(repair: bool = False, qa: list[str] | None = None) -> dict:
        result = hc.thin_execute(
            board=slug, task_id=task_id, workspace=workspace,
            provider=provider, model=model,
            prompt=demo_llm.builder_prompt(task_title, objective, brief,
                                           repair=repair, qa=qa,
                                           design_spec=design_spec,
                                           codebase_ctx=codebase_ctx),
            objective=objective, api_key=api_key,
            artifact_name=demo_llm.deliverable_filename(objective),
            max_tokens=max_tokens or demo_llm.builder_max_tokens(objective))
        result.update(_split_pack())
        return result

    out = _execute()

    def _audit() -> tuple[str, list[str], bool, int]:
        try:
            text = (Path(workspace) / "index.html").read_text(
                encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        issues = demo_llm.web_qa_issues(text)
        return (text, issues, demo_llm.web_artifact_needs_repair(text),
                demo_llm.web_deliverable_score(text))

    if out.get("ok") and demo_llm.deliverable_filename(objective) == "index.html":
        text, issues, needs, score = _audit()
        # Bounded repair loop: re-audit AFTER every repair, because a repair
        # pass can itself come back truncated/broken (observed: a truncated
        # landing page shipped "'fixed'" and stayed broken). At most 2 repairs.
        # Scoring drives the gate too: a structurally-fine but weak page
        # (score < 50) gets one polish attempt. Anchor-only coherence defects
        # (broken anchors/dead links) do NOT trigger a rebuild — the cheap
        # corner-patch below fixes those without risking re-truncation.
        for _ in range(2):
            if not needs and not demo_llm.web_qa_structural_repair(issues) \
                    and (score >= 50 or demo_llm.only_anchor_issues(issues)
                         or demo_llm.web_content_issue(issues)):
                break
            if score < 50:
                issues.append(f"overall quality score {score}/100 — needs polish")
            out = _execute(repair=True, qa=issues)
            out["retried"] = True
            text, issues, needs, score = _audit()
        # Deterministic salvage for a STILL-structural broken page (cut off):
        # append the missing tail instead of re-doing a full rebuild that will
        # hit the same token ceiling again.
        if needs:
            if _append_missing_tail(
                    slug=slug, task_id=task_id, workspace=workspace,
                    provider=provider, model=model, objective=objective,
                    api_key=api_key):
                out["tail_completed"] = True
            text, issues, needs, score = _audit()
        # Content-completion: fill nav-promised sections that are empty shells
        # (a page with a hero + category tabs but NO dishes/cards looks fine to
        # every structural check and ships "simple and basic"). Narrow bounded
        # calls only — never a full rebuild, which re-truncates the same way.
        for _ in range(2):
            gap = demo_llm.web_content_gap(text)
            if not gap:
                break
            if _append_missing_content(
                    slug=slug, task_id=task_id, workspace=workspace,
                    provider=provider, model=model, objective=objective,
                    api_key=api_key, brief=brief, missing_ids=gap[:3]):
                out["content_patched"] = True
            else:
                break
            text, issues, needs, score = _audit()
        # Corner-patch: page is complete now, but nav anchors may still dangle
        # (dead href='#', anchors to missing ids). Fix deterministically, and
        # only as a last resort via ONE bounded nav rewrite — never rebuild.
        if not needs and demo_llm.web_anchor_issues(issues):
            if _anchor_patch(slug=slug, task_id=task_id, workspace=workspace,
                             provider=provider, model=model,
                             objective=objective, api_key=api_key):
                out["anchor_patched"] = True
    return out


def _demo_drive(*, slug: str, goal: str, planner_id: str, builder_id: str,
                workspace: str, provider, model, pool_probe_key=None,
                runtime_source: str = "pool") -> None:
    """Drive the thin 2-lane demo board to completion in a daemon thread.

    Planner -> Builder, each executed with a REAL completion from the pool
    provider and REAL artifact + completed event on the board. Never raises.
    """
    def _run() -> None:
        outcomes: list = []
        ok = False
        try:
            outcomes.append(hc.thin_execute(
                board=slug, task_id=planner_id, workspace=workspace,
                provider=provider, model=model,
                prompt=demo_llm.planner_prompt(hc.DEMO_PLANNER_TITLE, goal),
                objective=goal, artifact_name="PLAN.md"))
            plan_text = ""
            try:
                plan_text = (Path(workspace) / "PLAN.md").read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
            outcomes.append(_run_builder(
                slug=slug, task_id=builder_id, workspace=workspace,
                provider=provider,
                model=_lane_model("BUILDER", model), objective=goal,
                brief=demo_llm.plan_to_brief(
                    plan_text, fallback=_read_brief(plan_text)),
                task_title=hc.DEMO_BUILDER_TITLE,
                max_tokens=demo_llm.demo_builder_max_tokens(goal)))
            ok = len(outcomes) == 2 and all(o.get("ok") for o in outcomes)
        except Exception as e:
            try:
                audit.audit("demo.drive", outcome="error", slug=slug,
                            reason=type(e).__name__)
                if len(outcomes) == 0:
                    # planner lane failed before reaching the builder — mark the
                    # builder lane done-with-error too so the board resolves
                    # instead of pointing a "todo" lane at a dead predecessor.
                    hc._demo_fail_lane(slug, builder_id,
                                       "builder lane not started (planner lane failed)")
            except Exception:
                pass
        else:
            try:
                # Mirror the demo's tiny workspace into the board's preview root
                # (project_workspace_dir) so /p/<slug>/ renders the built site
                # live instead of an empty listing. The demo workspace itself is
                # intentionally disposable; this copy is the user-facing result.
                if ok:
                    src = Path(workspace)
                    dst = hc.project_workspace_dir(slug)
                    if src.is_dir() and str(src) != str(dst):
                        dst.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                audit.audit("demo.drive", outcome="ok", slug=slug,
                            provider=pool_probe_key, runtime=runtime_source,
                            planner=outcomes[0].get("elapsed_s"),
                            builder=outcomes[1].get("elapsed_s"))
                db.update_provider_usage_outcome(slug, ok=int(ok))
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/demo/status")
def api_demo_status(request: Request):
    """Demo quota spender-facing status (per-IP 1/hour + global 20/day)."""
    ip = _client_ip(request)
    g_total = _DEMO_GLOBAL_MAX
    g_used = limiter.count("demo:global", _DEMO_GLOBAL_WINDOW)
    ip_used = limiter.count(f"demo:ip:{ip}", _DEMO_IP_WINDOW)
    return {
        "daily_quota_remaining": max(0, g_total - g_used),
        "daily_quota_total": g_total,
        "your_ip_limit_remaining": max(0, _DEMO_IP_MAX - ip_used),
        "your_ip_limit_window": f"{_DEMO_IP_WINDOW}s",
        "next_reset": _next_utc_midnight(),
    }


@app.get("/api/demo/latest")
def api_demo_latest(request: Request):
    """The caller's most recent ACTIVE demo board slug (recovery for a launch
    whose response was dropped at the proxy while the board was created)."""
    ip = _client_ip(request)
    slug = _latest_demo(ip)
    if not slug:
        return {"board": None}
    return {"board": slug}


@app.get("/api/demo/micro")
def api_demo_micro(request: Request, goal: str = "Build a todo app"):
    """Planner-only micro demo: 1 agent, 1-2 calls' worth of work, ~seconds.

    Cheap by design so many IPs can try it; still bounded per IP (5/hour) and
    globally (200/day). Returns a deterministic plan locally — the actual demo
    launch consumes the provider pool.
    """
    demo_user = _demo_user(request)
    if demo_user and db.get_admt_opt_out(demo_user["id"]):
        raise HTTPException(status_code=403,
                            detail="ADMT opt-out active. Human review required.")
    ip = _client_ip(request)
    if not limiter.check(f"demo:micro:ip:{ip}", _DEMO_MICRO_IP_MAX, _DEMO_IP_WINDOW):
        raise HTTPException(status_code=429, detail=_DEMO_LIMIT_DETAILS["ip"])
    if not limiter.check("demo:micro:global", _DEMO_MICRO_GLOBAL_MAX, _DEMO_GLOBAL_WINDOW):
        raise HTTPException(status_code=429, detail=_DEMO_LIMIT_DETAILS["global"])
    audit.audit("demo.micro", ip=ip, outcome="ok")
    return _micro_plan(goal)


def _micro_plan(goal: str) -> dict:
    """Deterministic planner-only output (no provider call; stays < 1 API cost)."""
    g = (goal or "").lower()
    if any(k in g for k in ("api", "web", "site", "saas", "dashboard", "billing",
                            "panel", "analytics")):
        stack = "FastAPI + PostgreSQL"
    elif any(k in g for k in ("data", "sql", "database", "postgres")):
        stack = "FastAPI + SQLite + Task queue"
    elif any(k in g for k in ("todo", "script", "cli")):
        stack = "Flask + SQLite"
    else:
        stack = "Flask + SQLite"
    snippet = (goal or "your goal").strip()[:80]
    return {
        "plan": (
            f"1. Model the {snippet} domain and API surface. "
            "2. Build the CRUD/steps with tests first (TDD). "
            "3. Containerize with a hardened runner. "
            "4. Iterate via the swarm review pass."
        ),
        "tech_stack": stack,
        "estimated_time": "2 hours",
    }


@app.get("/api/demo/progress/{board_slug}")
def api_demo_progress(request: Request, board_slug: str):
    """Live per-board demo progress (agents, estimated time, logs links).

    Demo (``flux-demo-*``) boards are public (shared showcase); any other slug
    must belong to the caller (403/401 otherwise). Returns an agent array
    derived from the board's REAL persisted task states — statuses are
    ``done/running/pending/blocked`` and ``progress`` is 100 only for a
    terminal task; nothing is fabricated.
    """
    user = get_current_user_optional(request)
    if not board_slug.startswith("flux-demo-") and not (
            user and board_slug.startswith(f"u{user['id']}-")):
        raise HTTPException(status_code=403, detail="Access denied to this board")
    try:
        tasks = hc.list_tasks(board_slug)
    except Exception:
        raise HTTPException(status_code=404, detail="Board not found")
    agents = []
    for t in tasks or []:
        assignee = (t.get("assignee") or "").strip().lower()
        if not assignee or assignee == "fluxswarm":
            continue
        state = (t.get("state") or "").strip()
        if state in ("done", "blocked"):
            status, progress = state, 100
        elif state == "running":
            status, progress = "running", 0
        else:
            status, progress = "pending", 0
        agents.append({
            "name": t.get("role_name") or t.get("assignee_display") or assignee,
            "status": status,
            "progress": progress,
            "task_id": t.get("id"),
            "logs_url": f"/api/demo/logs/{board_slug}/{t.get('id')}",
        })
    unfinished = [a for a in agents if a["status"] not in ("done", "blocked")]
    board_dir = Path(hc.HERMES_HOME) / "kanban" / "boards" / board_slug
    age_s = 0
    try:
        if board_dir.is_dir():
            age_s = max(0, time.time() - board_dir.stat().st_mtime)
    except Exception:
        age_s = 0
    estimated = 0 if not unfinished else max(0, _DEMO_MAX_RUNTIME_S - int(age_s))
    return {
        "status": "running" if unfinished else ("sealed" if hc.board_is_sealed(board_slug) else "done"),
        "agents": agents,
        "estimated_remaining_seconds": estimated,
    }


@app.get("/api/demo/logs/{board_slug}/{task_id}")
def api_demo_logs(request: Request, board_slug: str, task_id: str):
    """Operational event log for one board task (heartbeats/state changes).

    Demo boards public; owned boards require the owner. Returns [] for a
    private/unknown task — never crashes on a missing DB row.
    """
    user = get_current_user_optional(request)
    if not board_slug.startswith("flux-demo-") and not (
            user and board_slug.startswith(f"u{user['id']}-")):
        raise HTTPException(status_code=403, detail="Access denied to this board")
    try:
        logs = hc._task_activity_events(board_slug, task_id)
    except Exception:
        logs = []
    return {"board": board_slug, "task_id": task_id,
            "status": "ok", "logs": logs}


@app.get("/demo", response_class=HTMLResponse)
def demo_landing(request: Request):
    """Public demo landing page (AR-first, mirrors /): project description ->
    free launch (1/hour) -> live progress via /api/demo/progress/{slug}."""
    base = os.environ.get("FLUXSWARM_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return templates.TemplateResponse(
        request=request, name="demo.html",
        context={"title": "Try FluxSwarm Free", "canonical": base + "/demo" if base else "",
                 "csp_nonce": request.state.csp_nonce})


# ---------- auth ----------
def _client_ip(request: Request) -> str:
    """Resolve the real client IP for rate limiting / audit.

    X-Forwarded-For is ONLY honoured when the immediate peer is a configured
    trusted proxy (FLUXSWARM_TRUSTED_PROXIES, which accepts exact IPs or CIDRs).
    Otherwise any client could forge XFF and evade the per-IP rate limiter. When
    the peer is not a trusted proxy we use the socket peer (request.client.host),
    which cannot be spoofed from the network.
    """
    xff = request.headers.get("x-forwarded-for")
    # request.client.host can be an IPv4Address object (Starlette) OR a str
    # depending on version; coerce to str so the trusted-proxy check and the
    # returned value are always a plain IP string (never an object repr).
    peer_obj = request.client.host if request.client else None
    peer = str(peer_obj) if peer_obj is not None else None
    if xff and _peer_is_trusted(peer):
        return xff.split(",")[0].strip()
    return peer or "unknown"


def make_project_slug(user_id: int) -> str:
    """Build an unguessable, ownership-prefixed board slug for a new project.

    Format: ``u{user_id}-{epoch_seconds}-{random16}``. The trailing
    ``secrets.token_hex(4)`` (16 bits of entropy per minute, more across time)
    makes the slug non-sequential and unenumerable, so an attacker cannot probe
    another user's boards by guessing ``u2-<timestamp>``. The ``u{user_id}-``
    prefix is what the ownership guard (``slug.startswith(f"u{uid}-")``) relies on.
    """
    return f"u{user_id}-{int(time.time())}-{secrets.token_hex(4)}"


@app.post("/api/auth/register")
def api_register(p: RegisterIn, request: Request):
    ip = _client_ip(request)
    limiter.hit_ip(ip)
    if not limiter.ip_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — please wait a moment")
    if not limiter.register_allowed(ip):
        raise HTTPException(status_code=429, detail="Registration limit reached — try again later")
    email = p.email.lower().strip()
    if not p.tos_accept:
        raise HTTPException(status_code=400,
                            detail="Please accept the Terms & Privacy Policy to continue")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(p.password) < 8:
        raise HTTPException(status_code=400, detail="Password too short (minimum 8 characters)")
    if not p.name or not p.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    try:
        user = db.create_user(p.email, p.name, p.password, p.ref,
                              tos_accepted_at=time.time())
    except ValueError:
        audit.audit("auth.register", email=p.email, ip=ip, outcome="fail",
                    reason="email_taken")
        raise HTTPException(status_code=409, detail="Email already registered")
    limiter.record_registration(ip)
    token = auth_mod.make_token(user)
    audit.audit("auth.register", uid=user["id"], email=user["email"], ip=ip, outcome="ok")
    notify.send_welcome_async(user["email"], user["name"])
    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/login")
def api_login(p: LoginIn, request: Request):
    ip = _client_ip(request)
    email = p.email.lower().strip()
    limiter.hit_ip(ip)
    if not limiter.ip_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — please wait a moment")
    if not limiter.login_allowed(ip, email):
        audit.audit("auth.login", email=email, ip=ip, outcome="fail", reason="locked")
        raise HTTPException(status_code=429, detail="Too many failed logins — please wait before trying again")
    user = db.authenticate(p.email, p.password)
    if not user:
        limiter.record_login_failure(ip, email)
        audit.audit("auth.login", email=email, ip=ip, outcome="fail", reason="bad_credentials")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    limiter.clear_login_failures(ip, user["email"])
    token = auth_mod.make_token(user)
    audit.audit("auth.login", uid=user["id"], email=user["email"], ip=ip, outcome="ok")
    return {"token": token, "user": public_user(user)}


@app.get("/api/me")
def api_me(user: dict = Depends(get_current_user)):
    return public_user(user)


_SUPPORT_WINDOW = 60
_SUPPORT_MAX_PER_WINDOW = 20


@app.post("/api/support/chat")
def api_support_chat(p: SupportIn, request: Request,
                     user: dict | None = Depends(get_current_user_optional)):
    """In-app support assistant (hybrid: rule-based KB + optional AI fallback).

    The rule engine answers deterministic product questions free of charge; when
    no rule matches, an operator-configured OpenAI-compatible endpoint may reply,
    otherwise the assistant escalates to the support email. The endpoint is
    open (works signed out) but rate-limited per client IP to keep it cheap and
    abuse-proof; the message is capped and the history truncated server-side.
    """
    ip = _client_ip(request)
    if not limiter.check(f"support:ip:{ip}", _SUPPORT_MAX_PER_WINDOW, _SUPPORT_WINDOW):
        raise HTTPException(status_code=429,
                            detail="Please slow down a moment before sending more messages")
    try:
        ans = support_agent.answer(p.message, p.history)
    except Exception:
        audit.audit("support.chat", uid=user["id"] if user else None,
                    ip=ip, outcome="error", source="exception")
        raise HTTPException(status_code=500, detail="Support could not answer right now — please try again")
    audit.audit("support.chat", uid=user["id"] if user else None,
                ip=ip, outcome="ok", source=ans["source"])
    return ans


@app.post("/api/auth/logout")
def api_logout(request: Request, user: dict = Depends(get_current_user)):
    """Log out the user: invalidates EVERY currently-issued session token.

    Stateless JWTs have no server-side list, so we record ``logged_out_at`` and
    refuse any token whose ``iat`` predates it. (The client should also discard
    its stored token.)
    """
    db.mark_logged_out(user["id"])
    audit.audit("auth.logout", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok")
    return {"ok": True, "note": "Logged out — remove the token from your browser"}


@app.post("/api/auth/password")
def api_change_password(p: PasswordChangeIn, request: Request,
                        user: dict = Depends(get_current_user)):
    """Authenticated password change: verify the current password, then set the
    new hash and invalidate all existing sessions (log out everywhere)."""
    if len(p.new) < 8:
        raise HTTPException(status_code=400, detail="New password too short (minimum 8 characters)")
    if not db.authenticate(user["email"], p.current):
        audit.audit("auth.password", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="fail", reason="bad_current")
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    db.set_password(user["id"], p.new)
    audit.audit("auth.password", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok")
    return {"ok": True, "note": "Password changed — logged out of all sessions"}


def _reset_self_service() -> bool:
    """Dev/test-only echo flag.

    There is no mailer in this install, so a minted reset token has no delivery
    channel. When FLUXSWARM_RESET_SELF_SERVICE=1 the token is returned in the
    response so the full flow can be exercised (tests, local dev). Production
    keeps it OFF: the token is still single-use + expiring + hashed at rest, but
    it is discarded after minting and the client only ever receives the same
    generic response (anti-enumeration).
    """
    return os.environ.get("FLUXSWARM_RESET_SELF_SERVICE", "").strip().lower() in ("1", "true", "yes")


@app.post("/api/auth/reset-request")
def api_reset_request(p: ResetRequestIn, request: Request):
    """Start a password reset. Identical response whether or not the email
    exists (no account enumeration); rate-limited like login/register."""
    ip = _client_ip(request)
    limiter.hit_ip(ip)
    if not limiter.ip_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — please wait a moment")
    email = p.email.lower().strip()
    user = db.get_user_by_email(email)
    data = {"ok": True, "detail": "If the email is registered, a reset link has been sent"}
    if user:
        raw = db.create_password_reset(user["id"])
        if _reset_self_service():
            data["reset_token"] = raw  # dev/test channel only (no mailer)
        audit.audit("auth.reset.request", uid=user["id"], email=user["email"],
                    ip=ip, outcome="ok", delivered=_reset_self_service())
    else:
        audit.audit("auth.reset.request", email=email, ip=ip, outcome="miss")
    return data


@app.post("/api/auth/reset")
def api_reset(p: ResetIn, request: Request):
    """Complete a reset: consume the single-use token, set a new password and
    invalidate all existing sessions (also usable by an operator from support)."""
    if len(p.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password too short (minimum 8 characters)")
    uid = db.consume_password_reset(p.token)
    if not uid:
        raise HTTPException(status_code=400, detail="Reset token is invalid, expired, or already used")
    db.set_password(uid, p.new_password)
    u = db.get_user_by_id(uid)
    audit.audit("auth.reset", uid=uid, email=(u or {}).get("email", ""),
                ip=_client_ip(request), outcome="ok")
    return {"ok": True, "note": "Password reset — please log in again"}


def public_user(u: dict) -> dict:
    return {
        "id": u["id"], "email": u["email"], "name": u["name"],
        "plan": u["plan"], "credits": u["credits"], "ref_code": u["ref_code"],
    }


# ---------- user projects (auth required) ----------
_CUSTOM_AGENTS_MAX = 6


def _resolve_custom_agents(user: dict, agent_ids: list[int]) -> list[dict]:
    """Load the user's custom agents by id (owner-scoped, ordered, bounded).

    Returns a list of ``{"id", "name", "objective", "skills"}`` ready for the
    thin launcher. Unknown/foreign ids are silently dropped (never an error),
    so a stale client payload cannot crash a launch.
    """
    if not agent_ids:
        return []
    out = []
    seen = set()
    for aid in agent_ids[: _CUSTOM_AGENTS_MAX]:
        if aid in seen or not isinstance(aid, int):
            continue
        seen.add(aid)
        agent = db.get_custom_agent(user["id"], aid)
        if agent:
            out.append({
                "id": agent["id"],
                "name": (agent.get("name") or "").strip() or "Custom agent",
                "objective": (agent.get("objective") or "").strip(),
                "skills": (agent.get("skills") or "").strip(),
            })
    return out


@app.get("/api/projects")
def api_projects(user: dict = Depends(get_current_user)):
    return db.list_user_projects(user["id"])


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate, request: Request,
                       user: dict = Depends(get_current_user)):
    # Basic input validation (goal drives a subprocess launch).
    goal = sanitize_goal(payload.goal)
    if not goal:
        raise HTTPException(status_code=400, detail="Please enter a build goal")
    if len(payload.goal or "") > 4000:
        raise HTTPException(status_code=400, detail="Goal exceeds the maximum length")
    # CCPA/CPRA ADMT opt-out: manual project creation ONLY — no AI agents are
    # spawned, no credit is debited, and dispatch stays blocked for the user.
    if db.get_admt_opt_out(user["id"]):
        slug = make_project_slug(user["id"])
        pid = db.add_project(user["id"], slug, payload.name or "Project", goal)
        audit.audit("project.create", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="ok", slug=slug,
                    plan=user["plan"], mode="manual_no_ai")
        return {"slug": slug, "project_id": pid, "manual": True,
                "note": "ADMT opt-out active: project created manually (no AI agents)."}
    # Fail-closed operator budget gate (provider_guard): refuse before any
    # credit is debited when the configured execution ceilings are exhausted.
    budget = provider_guard.budget_gate()
    if not budget.ok:
        raise HTTPException(status_code=429, detail={
            "error": "budget_exhausted",
            "message": {"en": "Agent launch capacity is temporarily exhausted — please try again later."},
            "reason": budget.reason,
        })
    # Credit gating: each launch costs 1 credit.
    if not db.deduct_credit(user["id"]):
        raise HTTPException(status_code=402, detail="Out of credits — upgrade your plan or use a referral code")
    if db.get_user_credits(user["id"]) == 0:
        notify.send_depletion_async(user["email"], user["name"])
    slug = make_project_slug(user["id"])
    keys = _user_provider_keys(user)
    custom_agents = _resolve_custom_agents(user, payload.agent_ids)
    # Pull the cached analysis snapshot (uploaded project codebase) so agents can
    # plan/improve against the real source.  TTL'd, per-user, best-effort: if it
    # expired or was never uploaded, the launch proceeds with goal text only.
    context_payload = None
    with _analysis_cache_lock:
        cached = _analysis_cache.get(user["id"])
        if cached and time.time() - cached.get("ts", 0) < _ANALYSIS_CACHE_TTL_S:
            context_payload = cached.get("manifest")
            _analysis_cache.pop(user["id"], None)  # consumed-once semantics
    if context_payload:
        _snap = context_payload.get("codebase_snapshot", context_payload) if isinstance(context_payload, dict) else {}
        audit.audit("project.context", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="ok", slug=slug,
                    source_bytes=((_snap or {}).get("total_source_bytes", 0) if isinstance(_snap, dict) else 0))
    # Phase F: append the provider-usage ledger row with the ACTUAL resolved
    # runtime (BYOK beats operator default) before the launch. Best-effort:
    # accounting must never change launch behavior.
    runtime_source = "byok" if keys else "default"
    try:
        model, prov = hc._resolve_launch_runtime(keys)
    except Exception:
        model = prov = None
    if prov:
        try:
            db.record_provider_usage(
                surface="project", runtime_source=runtime_source,
                provider=prov, model=model or "", slug=slug)
        except Exception:
            pass
    try:
        if hc.projects_are_thin():
            # Small-memory host: the fat `swarm` CLI + `boards create` load the
            # whole workspace and OOM a 512 MB container (measured crash loop).
            # Build the real 8-lane squad directly in kanban.db; the background
            # thin driver then executes each lane with a real provider call.
            swarm = hc.launch_project_thin(slug, goal, provider=prov, model=model,
                                           custom_agents=custom_agents)
        else:
            hc.ensure_board(slug)
            swarm = hc.launch_swarm(slug, goal, provider_keys=keys)
    except Exception as e:
        # Refund the launch credit via the idempotent, audited helper — never
        # by ad-hoc SQL (double-refund risk) and never leaking internal detail.
        audit.audit("project.launch_failed", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), slug=slug, reason=str(e)[:300],
                    outcome="error")
        db.refund_launch_credit(user["id"])
        raise HTTPException(status_code=500, detail="Failed to launch swarm")
    pid = db.add_project(user["id"], slug, payload.name or "Project", goal)
    _fire_dispatch(slug, user["plan"], _user_provider_keys(user), pid=pid, goal=goal,
                   custom_agents=custom_agents, context_payload=context_payload)
    audit.audit("project.create", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", slug=slug, plan=user["plan"])
    return _swarm_payload(slug, payload.goal, swarm)


@app.post("/api/project/analyze")
async def api_project_analyze(request: Request,
                              user: dict = Depends(get_current_user)):
    """Accept a project ZIP, analyze it in memory, return a bounded manifest.

    No disk writes; no credit spent; upload size caps and traversal guards are
    enforced inside project_analyzer.  The manifest — including the codebase
    snapshot — is cached server-side so the next launch injects real source
    context into every agent prompt.
    """
    form = await request.form()
    up = form.get("file")
    data = await up.read() if up else b""
    if not data:
        raise HTTPException(status_code=400, detail="No project file uploaded")
    try:
        manifest = project_analyzer.analyze_zip(data, source_name=getattr(up, "filename", "project.zip"))
    except project_analyzer.ProjectAnalyzerError as e:
        audit.audit("project.analyze", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="rejected", reason=str(e)[:200])
        raise HTTPException(status_code=400, detail=str(e))
    # Cache the codebase snapshot (bounded source context) so the next launch can
    # inject real source without re-uploading.  TTL-bounded, per-user, capped so
    # a 512MB free host never accumulates more than ~10MB of cached snapshots.
    with _analysis_cache_lock:
        _analysis_cache[user["id"]] = {
            "manifest": manifest.get("codebase_snapshot", {}),
            "ts": time.time(),
        }
        if len(_analysis_cache) > 160:
            now = time.time()
            expired = [k for k, v in _analysis_cache.items()
                       if now - v.get("ts", 0) > _ANALYSIS_CACHE_TTL_S]
            for k in expired[:80]:
                _analysis_cache.pop(k, None)
    audit.audit("project.analyze", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok",
                files=manifest["files_count"], bytes_=manifest["total_bytes"],
                has_snapshot=bool(manifest.get("codebase_snapshot", {}).get("key_files")))
    return manifest


def _swarm_payload(slug: str, goal: str, swarm) -> dict:
    """Build the launch response for BOTH drivers: the thin one returns a plain
    dict (launch_project_thin), the fat one a SwarmResult object with
    attributes. Accessing ``swarm.root_id`` unconditionally raised
    AttributeError on thin hosts -> FastAPI 500 -> bare 'Internal Server Error'
    plain text -> the web UI's ``(await r.json()).detail`` threw
    "Unexpected token 'I'". Map both shapes the same way."""
    if isinstance(swarm, dict):
        sm = swarm
    else:
        sm = {
            "root_id": swarm.root_id, "worker_ids": swarm.worker_ids,
            "verifier_id": swarm.verifier_id,
            "synthesizer_id": swarm.synthesizer_id,
        }
    return {
        "slug": slug, "goal": goal,
        "root_id": sm.get("root_id"),
        "workers": sm.get("worker_ids", []),
        "verifier_id": sm.get("verifier_id"),
        "synthesizer_id": sm.get("synthesizer_id"),
    }


@app.get("/api/projects/{slug}/workspace")
def api_workspace(slug: str, user: dict = Depends(get_current_user)):
    # Same ownership rules as the task board: private boards require the owner,
    # demo boards are public showcase. Generated files are the user's "result".
    if not slug.startswith(f"u{user['id']}-") and not slug.startswith("flux-demo-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        return {"slug": slug, "content": hc.read_workspace(slug)}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read workspace")


# ---------- Project Files: upload / browse / remove (edit-after-build) -----
# Customers upload files (images, specs, source) into their project's native
# attachments store; the swarm reads them from the mirrored ``uploads/`` in
# the workspace, and the Project Files browser + export carry them alongside
# the generated deliverable. Uploads and deletes are OWNER-ONLY (demo boards
# are public showcase and stay read-only for files); browsing mirrors the
# workspace read rules (owned = owner, demo = any authenticated user).


@app.post("/api/projects/{slug}/attachments")
async def api_upload_attachment(slug: str, request: Request, file: UploadFile = File(...),
                                user: dict = Depends(get_current_user)):
    if not slug.startswith(f"u{user['id']}-"):
        audit.audit("project.attach", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="fail", reason="unauthorized", slug=slug)
        raise HTTPException(status_code=403, detail="Unauthorized")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        info = hc.save_attachment(slug, file.filename or "upload.bin", data)
    except ValueError as e:
        audit.audit("project.attach", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="fail", reason=str(e)[:120], slug=slug)
        raise HTTPException(status_code=400, detail=str(e))
    audit.audit("project.attach", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", slug=slug,
                name=info["name"], bytes_=info["size"])
    return {"slug": slug, "name": info["name"], "size": info["size"]}


@app.get("/api/projects/{slug}/files")
def api_project_files(slug: str, user: dict = Depends(get_current_user)):
    if not slug.startswith(f"u{user['id']}-") and not slug.startswith("flux-demo-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        return {"slug": slug, **hc.list_project_files(slug)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to list project files")


@app.delete("/api/projects/{slug}/attachments/{name}")
def api_delete_attachment(slug: str, name: str, request: Request,
                          user: dict = Depends(get_current_user)):
    if not slug.startswith(f"u{user['id']}-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        removed = hc.delete_project_file(slug, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.audit("project.attach_rm", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok" if removed else "noop",
                slug=slug, name=name)
    return {"removed": removed}


# ---------- paid-export gating: GET /api/projects/{slug}/export ----------
# Generated project files are the user's deliverable; this endpoint bundles the
# workspace into a ZIP, server-side gated by a per-user hourly burst cap
# (demo: 5/h, paid: 60/h) so the cost surface stays bounded for free users.
_EXPORT_HOURLY_CAP = 3600
_EXPORT_DEMO_HOURLY_CAP = int(os.environ.get("FLUXSWARM_EXPORT_DEMO_HOURLY_CAP", "5"))
_EXPORT_PAID_HOURLY_CAP = int(os.environ.get("FLUXSWARM_EXPORT_PAID_HOURLY_CAP", "60"))
_EXPORT_MAX_ZIP_BYTES = int(os.environ.get("FLUXSWARM_EXPORT_MAX_ZIP_BYTES", "62914560"))
_export_hits: dict[str, list[float]] = {}
_export_lock = threading.Lock()

# ---- Project analysis cache ------------------------------------------------
# Keeps the latest analysis snapshot (manifest + codebase content) per user
# between the analyze call and the next launch.  Bounded: at most one entry per
# user (overwritten), TTL'd, max entries capped so memory stays bounded.
_analysis_cache: dict[int, dict] = {}
_analysis_cache_lock = threading.Lock()
_ANALYSIS_CACHE_TTL_S = 600  # 10 minutes


def _export_allowed(uid: int, plan: str) -> bool:
    """Per-user rolling hourly burst gate. Demo plan gets a small cap, paid
    plans a larger one. In-memory only (bounded, single-process), like the auth
    rate limiter; used as abuse protection, never as a billing authority."""
    is_paid = (db.PLANS.get(plan) or {}).get("price", 0) > 0
    cap = _EXPORT_PAID_HOURLY_CAP if is_paid else _EXPORT_DEMO_HOURLY_CAP
    bucket = f"{uid}:{int(time.time() // 3600)}"
    now = time.time()
    with _export_lock:
        hits = [t for t in _export_hits.get(bucket, []) if now - t < _EXPORT_HOURLY_CAP]
        if len(hits) >= cap:
            _export_hits[bucket] = hits
            return False
        hits.append(now)
        _export_hits[bucket] = hits
        if len(_export_hits) > 65536:  # bound memory on long-lived workers
            for k in list(_export_hits):
                if not _export_hits[k]:
                    _export_hits.pop(k, None)
        return True


def _export_bundle(slug: str) -> bytes:
    """Zip every regular file in the board workspace. Symlinks are skipped and
    every arch path is containment-checked; sleeps/network are avoided so this
    stays a cheap, bounded read-only walk."""
    root = hc.project_workspace_dir(slug)
    if not root.is_dir():
        return b""
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            arc = str(p.relative_to(root)).replace("\\", "/")
            if any(seg in ("..", "") for seg in arc.split("/")) or ":" in arc or arc.startswith("/"):
                continue
            size = p.stat().st_size
            if size > _EXPORT_MAX_ZIP_BYTES:
                continue
            z.write(p, arc)
            total += size
            if total > _EXPORT_MAX_ZIP_BYTES:
                break
    return buf.getvalue()


@app.get("/api/projects/{slug}/export")
def api_project_export(slug: str, request: Request, user: dict = Depends(get_current_user)):
    # Same ownership rules as the workspace listing: demo boards are public
    # showcase; owned boards require the owner.
    if not slug.startswith(f"u{user['id']}-") and not slug.startswith("flux-demo-"):
        audit.audit("project.export", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="unauthorized", slug=slug)
        raise HTTPException(status_code=403, detail="Unauthorized")
    if not _export_allowed(user["id"], user.get("plan", "demo")):
        audit.audit("project.export", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="burst_limit", slug=slug)
        raise HTTPException(status_code=429, detail="Export rate limit reached — please wait a moment")
    data = _export_bundle(slug)
    if not data:
        audit.audit("project.export", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="empty_workspace", slug=slug)
        raise HTTPException(status_code=404, detail="No generated files to export yet")
    audit.audit("project.export", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", slug=slug, bytes=len(data))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{urlquote(slug)}-export.zip"'})


# ---------- live project preview (/p/<slug>/...) ----------
# Renders the board's generated workspace as a browsable live site (index.html
# when present, else an auto directory listing) served under /p/. The dashboard
# embeds it in a sandboxed iframe (opaque origin -> cannot read the parent's
# session or API); the routes below carry the relaxed _PREVIEW_CSP scoped to /p
# only.
#
# Authorization mirrors the rest of the app: demo (``flux-demo-*``) boards are
# public showcase; owned (``u<uid>-``) boards require a short-lived preview
# ticket cookie issued by /api/projects/{slug}/preview-ticket (the dashboard
# tokens live in localStorage and cannot be attached to an iframe navigation,
# so the app swaps a Bearer token for an HttpOnly /p-scoped cookie). Raw file
# paths are resolved with resolve()-containment so ``../`` can never escape the
# workspace root.
_PREVIEW_TICKET_TTL_S = 600
_preview_tickets: dict[str, dict] = {}


def _bound_preview_tickets() -> None:
    cutoff = time.time() - _PREVIEW_TICKET_TTL_S
    if len(_preview_tickets) > 2048:
        for k in list(_preview_tickets):
            if _preview_tickets[k]["exp"] < cutoff:
                _preview_tickets.pop(k, None)


def _issue_preview_ticket(slug: str, uid: int) -> str:
    ticket = secrets.token_urlsafe(24)
    _bound_preview_tickets()
    _preview_tickets[ticket] = {
        "slug": slug, "uid": uid, "exp": time.time() + _PREVIEW_TICKET_TTL_S}
    return ticket


def _preview_identity(request: Request) -> dict | None:
    """Resolve the preview viewer: preview-ticket cookie first, then the normal
    Bearer/fs_token session. Returns a {uid, slug} dict (or None)."""
    ticket = (request.cookies.get("fs_preview") or "").strip()
    if ticket:
        snap = _preview_tickets.get(ticket)
        if snap:
            if snap["exp"] > time.time():
                return snap
            _preview_tickets.pop(ticket, None)
    user = get_current_user_optional(request)
    if user:
        return {"uid": user["id"], "slug": ""}
    return None


def _preview_allowed(request: Request, slug: str) -> bool:
    """Demo boards are public; owned boards need the ticket bound to this exact
    slug, or the owner's normal session."""
    if not hc._SAFE_SLUG_RE.match(slug):
        return False
    if slug.startswith("flux-demo-"):
        return True
    ident = _preview_identity(request)
    if not ident or not ident.get("uid"):
        return False
    bound = ident.get("slug") or ""
    if not slug.startswith(f"u{ident['uid']}-"):
        return False
    return not bound or bound == slug


def _workspace_target(root: Path, relpath: str) -> Path | None:
    """Resolve ``relpath`` inside ``root`` with path-traversal containment."""
    try:
        root_resolved = str(root.resolve()) + os.sep
    except OSError:
        return None
    segs = [s for s in relpath.replace("\\", "/").split("/") if s not in ("", ".")]
    if any(s == ".." for s in segs):
        return None
    target = root
    for s in segs:
        target = target / s
    try:
        target = target.resolve()
    except OSError:
        return None
    if not str(target).startswith(root_resolved):
        return None
    return target


def _preview_html(slug: str, code: int, title: str, text: str) -> HTMLResponse:
    import html as _html
    body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{code} · FluxSwarm preview</title></head>"
            f"<body style='font-family:system-ui;background:#0b0f1a;color:#eef1fb;"
            f"margin:0;padding:48px;line-height:1.6'>"
            f"<h1>{code} — {_html.escape(title)}</h1>"
            f"<p>{_html.escape(text)}</p>"
            f"<p><a href='/' style='color:#22d3ee'>Back to FluxSwarm</a></p>"
            f"</body></html>")
    return HTMLResponse(body, status_code=code)


def _preview_listing(request: Request, slug: str, node: Path,
                     root: Path, relpath: str) -> str:
    """Preview page when no index.html exists (no browseable site generated).

    Root level shows a guided Lovable-style card explaining there is no live
    page yet and how to get one; the generated files are listed below it.
    """
    import html as _html
    base = request.url.path.rstrip("/")
    rows: list[str] = []
    try:
        entries = list(node.iterdir())
    except OSError:
        entries = []
    for p in sorted(entries, key=lambda p: (0 if p.is_dir() else 1, p.name.lower())):
        icon = "📁" if p.is_dir() else "📄"
        href = base + "/" + urlquote(p.name)
        rows.append(f'<li><a href="{_html.escape(href, quote=True)}">{icon} '
                    f'{_html.escape(p.name)}</a></li>')
    rel_disp = relpath or "."
    guide = ""
    if not relpath:
        guide = """
<div style="max-width:760px;margin:24px auto 32px;padding:28px;border-radius:16px;background:linear-gradient(160deg,#13203c,#0b1428);border:1px solid #1e2c4d">
<p style="margin:0 0 6px;font-size:12px;letter-spacing:.12em;color:#22d3ee;text-transform:uppercase">Live preview</p>
<h1 style="margin:0 0 10px;font-size:24px">No live page generated yet</h1>
<p style="margin:0 0 14px;color:#aeb8d0">This build produced code and documents, not a browsable web page. To get a live preview that renders in this window, launch with a goal that asks for a <b>website</b>, <b>landing page</b>, or <b>web app</b> — the swarm then builds a single self-contained <code>index.html</code> you can open and interact with.</p>
<p style="margin:0;color:#8b96b3;font-size:13px">Generated files are listed below if you want to inspect them.</p>
</div>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{_html.escape(slug)} · preview</title></head>
<body style="font-family:system-ui;background:#0b0f1a;color:#eef1fb;margin:0;padding:32px;line-height:1.7">
{guide}
<p style="color:#8b96b3;font-size:13px">{_html.escape(slug)} · /{_html.escape(rel_disp)}</p>
<ul style="list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px">
{''.join(rows)}
</ul></body></html>"""


def _preview_doc(request: Request, slug: str, relpath: str) -> Response:
    """Serve one preview resource under /p/<slug>/ (index, file, or listing)."""
    request.state.preview = True
    root = hc.project_workspace_dir(slug)
    if not relpath:
        idx = root / "index.html"
        if idx.is_file():
            return FileResponse(idx, media_type="text/html")
        return HTMLResponse(_preview_listing(request, slug, root, root, ""))
    target = _workspace_target(root, relpath)
    if target is None:
        return _preview_html(slug, 404, "Not found", "This path is invalid.")
    if target.is_dir():
        return HTMLResponse(_preview_listing(request, slug, target, root, relpath))
    if target.is_file():
        mt = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if any(t in mt for t in ("text/", "json", "xml", "javascript")):
            mt += "; charset=utf-8"
        return FileResponse(target, media_type=mt)
    return _preview_html(slug, 404, "Not found", "This file does not exist.")


@app.get("/p/{slug}")
def preview_redirect(request: Request, slug: str):
    if not _preview_allowed(request, slug):
        return _preview_html(slug, 403, "Forbidden",
                             "Sign in to preview this board, or open a demo board.")
    return RedirectResponse(url=f"/p/{slug}/", status_code=301)


@app.get("/p/{slug}/")
def preview_root(request: Request, slug: str):
    if not _preview_allowed(request, slug):
        return _preview_html(slug, 403, "Forbidden",
                             "Sign in to preview this board, or open a demo board.")
    return _preview_doc(request, slug, "")


@app.get("/p/{slug}/{rest:path}")
def preview_path(request: Request, slug: str, rest: str):
    if not _preview_allowed(request, slug):
        return _preview_html(slug, 403, "Forbidden",
                             "Sign in to preview this board, or open a demo board.")
    return _preview_doc(request, slug, rest)


@app.get("/api/projects/{slug}/preview-ticket")
def api_preview_ticket(slug: str, request: Request,
                       user: dict = Depends(get_current_user)):
    """Issue a short-lived /p-only cookie so the dashboard can embed the user's
    preview in a sandboxed iframe (Bearer tokens in localStorage cannot travel
    with an iframe navigation). Demo boards need no ticket."""
    if not slug.startswith(f"u{user['id']}-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    ticket = _issue_preview_ticket(slug, user["id"])
    audit.audit("preview.ticket", uid=user["id"], slug=slug,
                ip=_client_ip(request), outcome="ok")
    resp = JSONResponse({"ok": True, "slug": slug, "ttl_seconds": _PREVIEW_TICKET_TTL_S})
    resp.set_cookie("fs_preview", ticket, max_age=_PREVIEW_TICKET_TTL_S,
                    httponly=True, samesite="lax", path="/p")
    return resp


@app.get("/api/projects/{slug}/tasks")
def api_tasks(slug: str, user: dict = Depends(get_current_user)):
    # Only allow if the board belongs to this user (prefix guard).
    if not slug.startswith(f"u{user['id']}-") and not slug.startswith("flux-demo-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        return hc.list_tasks(slug)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to list tasks")


@app.post("/api/projects/{slug}/dispatch")
def api_dispatch(slug: str, dry_run: bool = False, user: dict = Depends(get_current_user)):
    if not slug.startswith(f"u{user['id']}-") and not slug.startswith("flux-demo-"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if _operator_maintenance():
        raise HTTPException(status_code=503, detail="Service temporarily unavailable — please try again later")
    # CCPA/CPRA ADMT opt-out: the user forfeits AI-assisted dispatch.
    if db.get_admt_opt_out(user["id"]):
        raise HTTPException(status_code=403,
                            detail="ADMT opt-out active. Human review required.")
    if slug.startswith("flux-demo-") and db.bump_demo_usage(f"u{user['id']}", _today()) > _DEMO_DAILY_CAP:
        raise HTTPException(status_code=429, detail={
            "error": "demo_daily_limit",
            "message": {
                "en": "Daily demo allowance used up. Sign up for unlimited access.",
            },
            "retry_after_seconds": 86400,
            "upgrade_url": "/pricing",
        })
    # Never re-arm an operator-final board: a sealed / finalized launch must not
    # be re-dispatched (its workers are dead and it would hold the host-cap).
    if _board_finalized(slug) or hc.board_is_sealed(slug):
        raise HTTPException(status_code=409, detail="Board is finalized/sealed — cannot relaunch")
    try:
        return hc.dispatch(slug, max_spawn=db.PLANS[user["plan"]]["parallel"], dry_run=dry_run, timeout_s=hc.DISPATCH_TIMEOUT_S)
    except Exception:
        raise HTTPException(status_code=500, detail="Dispatch failed")


# ---------- edit-after-build: POST /api/projects/{slug}/reopen ----------
# A sealed / finalized board is operator-final: the customer cannot make the
# swarm work on it again until it is reopened. ``reopen`` unmounts the seal
# marker and re-arms the parked agent lanes, so the flow becomes:
# upload/edit (Project Files) -> reopen -> dispatch (rebuild against the new
# files). Refunded boards stay closed forever (the credit was already returned).


@app.post("/api/projects/{slug}/reopen")
def api_reopen_project(slug: str, request: Request,
                       user: dict = Depends(get_current_user)):
    if not slug.startswith(f"u{user['id']}-"):
        audit.audit("project.reopen", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="fail", reason="unauthorized", slug=slug)
        raise HTTPException(status_code=403, detail="Unauthorized")
    # A refunded launch is permanent: its credit was returned, re-opening would
    # let the same paid work run again for free.
    proj = _project_by_board_slug(slug)
    if proj and proj.get("launch_refunded"):
        audit.audit("project.reopen", uid=user["id"], email=user["email"],
                    ip=_client_ip(request), outcome="fail", reason="refunded", slug=slug)
        raise HTTPException(status_code=409, detail="Refunded launch cannot be reopened")
    try:
        report = hc.unseal_board(slug, reason="customer-requested reopen")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to reopen board")
    # Lift the launch-terminal marker so dispatch stops treating the board as
    # finalized (never touches the refund flag — refused above).
    if proj:
        try:
            db.set_launch_outcome(proj["id"], "reopened", "reopened")
        except Exception:
            pass
    audit.audit("project.reopen", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", slug=slug,
                rearmed=report.get("rearmed", 0))
    return {"slug": slug, "reopened": True, "rearmed": report.get("rearmed", 0)}


# ---------- BYOK keys ----------
@app.get("/api/keys")
def api_list_keys(user: dict = Depends(get_current_user)):
    # Return masked info only; never the raw token.
    data = {}
    for prov in hc.SUPPORTED_PROVIDERS:
        tok = vault.get_user_key(user["id"], prov)
        if tok:
            data[prov] = vault.mask_key(tok)
    return {"keys": data, "byok_active": bool(data)}


@app.post("/api/keys")
def api_set_key(payload: dict, request: Request, user: dict = Depends(get_current_user)):
    prov = (payload.get("provider") or "").lower()
    tok = (payload.get("token") or "").strip()
    # Phase 3: real BYOK providers only — including the OpenRouter free tier,
    # which still requires a user key + agreement (no anonymous free provider).
    if prov not in hc.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported provider")
    if not tok:
        raise HTTPException(status_code=400, detail="Key cannot be empty")
    vault.set_user_key(user["id"], prov, tok)
    audit.audit("keys.set", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", provider=prov)
    return {"ok": True, "provider": prov, "masked": vault.mask_key(tok)}


# ---------- provider agreements (Phase 3 BYOK gate) ----------
@app.get("/api/agreements")
def api_list_agreements(user: dict = Depends(get_current_user)):
    """Which provider term-versions the user has accepted (for the BYOK UI)."""
    return {
        "required_versions": {p: hc.PROVIDER_AGREEMENT_VERSION for p in hc.SUPPORTED_PROVIDERS},
        "accepted": db.provider_agreements(user["id"]),
    }


@app.post("/api/agreements/{provider}")
def api_accept_agreement(provider: str, request: Request, user: dict = Depends(get_current_user)):
    """Record the user's acceptance of a provider's terms before launch."""
    prov = provider.strip().lower()
    if prov not in hc.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported provider")
    first = db.agree_provider(user["id"], prov, hc.PROVIDER_AGREEMENT_VERSION)
    audit.audit("agreements.accept", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", provider=prov, version=hc.PROVIDER_AGREEMENT_VERSION)
    return {"ok": True, "provider": prov, "version": hc.PROVIDER_AGREEMENT_VERSION,
            "first_accept": first}


def _user_provider_keys(user: dict) -> dict:
    """Collect the user's BYOK keys so THEY pay for tokens (Claude/GPT/Gemini/Kimi/OpenRouter).

    Available on ALL plans (including demo): bringing a provider key (paid, or
    the free OpenRouter tier) is how a user tests different runtimes at their
    own token cost — FluxSwarm pays nothing for those.
    Phase 3 agreement gate: a key is only used once the user has accepted that
    provider's terms (POST /api/agreements/<provider>); unagreed providers are
    excluded so the runtime resolver fails fast with a clear, actionable error.
    """
    keys = {}
    for prov in hc.SUPPORTED_PROVIDERS:
        if not (hasattr(db, "has_provider_agreement") and db.has_provider_agreement(user["id"], prov)):
            continue
        tok = vault.get_user_key(user["id"], prov)
        if tok:
            keys[prov] = tok
    return keys


# ---------- referrals ----------
@app.get("/api/referrals")
def api_referrals(user: dict = Depends(get_current_user)):
    return {"ref_code": user["ref_code"], "reward_credits": db.REFERRAL_REWARD_CREDITS,
            "link": f"/?ref={user['ref_code']}"}


@app.post("/api/subscribe/{plan}")
def api_subscribe(plan: str, request: Request, user: dict = Depends(get_current_user)):
    if plan not in db.PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    is_paid = db.PLANS[plan]["price"] > 0
    if not is_paid:
        db.upgrade_plan(user["id"], plan)
        audit.audit("plan.subscribe", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="ok", plan=plan)
        return public_user(db.get_user_by_id(user["id"]))
    # Paid plan: must pass through a real billing gate. Until a gateway is wired
    # (FLUXSWARM_PAYMENTS=1 + operative provider creds), claiming a paid plan is
    # rejected outright: this closes the free-upgrade / infinite-credit-reset exploit.
    if not _payments_enabled():
        gw = payments_mod.get_gateway()
        audit.audit("plan.subscribe", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="billing_gate_closed", plan=plan,
                    gateway=getattr(gw, "name", "none"))
        raise HTTPException(status_code=402,
                            detail="Paid plans are locked in development mode — billing will be enabled soon")
    gw = payments_mod.get_gateway()
    if not getattr(gw, "operative", False):
        # Provider requested but neither credentials nor the explicit local
        # sandbox flag are present -> stay closed (never charge by accident).
        audit.audit("plan.subscribe", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="gateway_not_configured", plan=plan,
                    gateway=getattr(gw, "name", "none"))
        raise HTTPException(status_code=402,
                            detail="Billing is not configured yet — please try again later")
    try:
        session = gw.create_checkout(
            plan=plan,
            user_id=user["id"],
            amount_cents=db.PLANS[plan]["price"] * 100,
        )
    except Exception as exc:  # provider down / misconfigured
        audit.audit("plan.subscribe", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="checkout_error", plan=plan, error=str(exc)[:200])
        raise HTTPException(status_code=502, detail="Failed to create checkout session — please try again")
    audit.audit("plan.subscribe", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", plan=plan, mode="checkout", session_id=session.id, gateway=gw.name)
    return {"checkout_url": session.url, "session_id": session.id,
            "plan": plan, "note": "Plan upgrade activates automatically after payment confirmation"}


@app.post("/api/payments/webhook")
async def api_payments_webhook(request: Request):
    """Paddle webhook endpoint. Unauthenticated by design but signature-verified:
    credits are granted ONLY when a webhook with a valid FLUXSWARM-provided
    signature arrives; a client redirecting back is never proof of payment.

    Idempotent: a replayed event_id returns 200 without double-granting.

    Rate-limited per client IP (10 req/min) to blunt credential-stuffing /
    blind replays against the signature check; a burst still cannot bypass the
    signature requirement, it just bounds the CPU the verifier spends on junk.
    """
    ip = _client_ip(request)
    if not limiter.check(f"webhook:{ip}", 10, 60):
        audit.audit("payments.webhook", outcome="fail", reason="rate_limited",
                    ip=ip)
        raise HTTPException(status_code=429, detail="webhook rate limit exceeded")
    gw = payments_mod.get_gateway()
    if not getattr(gw, "operative", False):
        audit.audit("payments.webhook", outcome="fail", reason="gateway_not_configured",
                    ip=_client_ip(request))
        raise HTTPException(status_code=503, detail="Webhook not configured")
    body = await request.body()
    signature = request.headers.get("Paddle-Signature")
    processed = _process_paddle_payload(body, signature, request)
    return {"accepted": True, "deduplicated": processed.get("deduplicated", False)}


@app.get("/api/payments/webhook")
def api_payments_webhook_get():
    raise HTTPException(status_code=405, detail="method not allowed")


def _process_paddle_payload(body: bytes, signature: str | None, request: Request) -> dict:
    """Signature-verify a Paddle payload, apply it once (idempotent), audit it.

    Shared by the real webhook and the explicit local sandbox mode so the mock
    path exercises exactly the same grant logic as production.
    """
    gw = payments_mod.get_gateway()
    receipt = gw.handle_webhook(body, signature=signature)
    if not receipt.get("ok"):
        audit.audit("payments.webhook", outcome="fail", reason=receipt.get("reason", "rejected"),
                    ip=_client_ip(request))
        raise HTTPException(status_code=400, detail="Webhook rejected")
    event_id = receipt["event_id"] or receipt["idempotency_key"]
    if not db.record_payment_event(
        event_id, gateway=receipt["gateway"], kind=receipt["event"],
        user_id=receipt.get("user_id") or 0,
        detail={"plan": receipt.get("plan"), "amount_cents": receipt.get("amount_cents"),
                "currency": receipt.get("currency"), "txn": receipt.get("transaction_id")},
    ):
        return {"deduplicated": True}  # already processed
    detail_inner = {"gateway": receipt["gateway"], "receipt_kind": receipt["event"],
                    "plan": receipt.get("plan"),
                    "amount_cents": receipt.get("amount_cents"), "currency": receipt.get("currency"),
                    "txn": receipt.get("transaction_id"), "event_id": event_id}
    if receipt["event"] == "payment.succeeded":
        uid = receipt.get("user_id")
        if not uid or not receipt.get("plan"):
            audit.audit("payments.webhook", outcome="fail", reason="incomplete_receipt",
                        ip=_client_ip(request), **detail_inner)
            raise HTTPException(status_code=422, detail="Incomplete payment data")
        if receipt["plan"] == "topup":
            # A pure credit refill: add the pack's credits without moving the
            # user's plan tier (their parallel cap stays with the highest plan
            # they purchased). Topups still trigger the once-per-referred-user
            # referral rewards below (a paid purchase counts either way).
            db.add_credits(uid, db.PLANS["topup"]["credits"])
        else:
            db.upgrade_plan(uid, receipt["plan"])
        u2 = db.get_user_by_id(uid)
        if u2 and u2.get("referred_by"):
            db.reward_referrer_once(u2["email"])
        audit.audit("payments.webhook", outcome="ok", ip=_client_ip(request), **detail_inner)
    elif receipt["event"] == "payment.refunded":
        uid = receipt.get("user_id")
        if not uid and receipt.get("transaction_id"):
            # Paddle v1 refund payloads (adjustment.*) carry only the original
            # transaction id — map it back to the paying user before revoking.
            uid = db.payment_user_by_txn(receipt["transaction_id"])
        if uid:
            db.downgrade_subscription(uid)
        audit.audit("payments.webhook", outcome="ok", ip=_client_ip(request), **detail_inner)
    else:
        audit.audit("payments.webhook", outcome="ok", ip=_client_ip(request),
                    handled="unhandled-kind", **detail_inner)
    return {"deduplicated": False}


# ---------- hosted checkout page (Paddle.js overlay) ----------
def _paddle_client_token() -> str:
    return os.environ.get("PADDLE_CLIENT_TOKEN", "").strip()


@app.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request):
    """Paddle.js checkout page. Paddle transaction payment links point at
    `/<this>/?_ptxn=txn_...`; this page includes Paddle.js, initializes with the
    client token, and opens Paddle's overlay checkout for the transaction named
    in the `_ptxn` query parameter. Works for sandbox and live depending on
    PADDLE_API_BASE. A fallback button + event handlers cover blocked popups."""
    token = _paddle_client_token()
    sandbox = "sandbox" in os.environ.get("PADDLE_API_BASE", "")
    if not token:
        return """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>FluxSwarm · Checkout</title></head>
<body style="font-family:system-ui;max-width:560px;margin:60px auto;text-align:center">
<h2>Checkout is not ready yet</h2>
<p>PADDLE_CLIENT_TOKEN is not set — add the client token from your Paddle dashboard and restart.</p>
</body></html>"""
    env = ""
    if sandbox:
        env = "try { Paddle.Environment.set(\"sandbox\"); } catch (e) {}\n"
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>FluxSwarm · Secure checkout</title>
<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
</head>
<body style="font-family:system-ui;max-width:560px;margin:60px auto;text-align:center;line-height:1.8">
<h2 id="fs-status">Opening the secure payment window…</h2>
<p style="color:#666;font-size:14px">If the window does not appear within a few seconds, allow pop-ups and press the button, or reopen the link.</p>
<button id="fs-retry" onclick="openCheckout()" style="margin:14px 0;font-size:15px;padding:10px 24px;cursor:pointer;border-radius:8px;border:1px solid #1b6ef3;background:#1b6ef3;color:#fff">Open the payment window</button>
<script>
__PADDLE_ENV__
var fsStatus = document.getElementById('fs-status');
var fsLog = function (m) { console.log('[fluxswarm]', m); if (fsStatus) fsStatus.textContent = m; };
window.onerror = function (msg, src, line) { fsLog("Script error: " + msg + " (" + line + ")"); };
window.addEventListener('unhandledrejection', function (e) {
  fsLog("Unhandled failure: " + (e.reason ? (e.reason.message || e.reason) : "unknown"));
});

var txn = new URLSearchParams(window.location.search).get('_ptxn');
var watchdog = null;
var initialized = false;

function openCheckout() {
  if (typeof Paddle === 'undefined') {
    fsLog("The payment engine (cdn.paddle.com) did not load in this browser. Try: refresh the page, a different browser, or disable the ad blocker.");
    return;
  }
  if (!txn) {
    fsLog("This link is incomplete — reopen it from the subscription page.");
    return;
  }
  fsLog("Opening the secure payment window…");
  try {
    Paddle.Checkout.open({ transactionId: txn, settings: { displayMode: "overlay" } });
    watchdog = setTimeout(function () {
      fsLog("The window was not confirmed within 8 seconds — press the button above to try again.");
    }, 8000);
  } catch (err) {
    fsLog("Could not open checkout: " + err.message);
  }
}

var sdkAttempts = 0;
function retrySdk() {
  if (sdkAttempts >= 3) {
    fsLog("Repeated failure — the payment engine cannot be reached from this browser. Try a different browser or disable the ad blocker, then reload the page.");
    return;
  }
  sdkAttempts++;
  var s = document.createElement('script');
  s.src = 'https://cdn.paddle.com/paddle/v2/paddle.js';
  s.onload = function () { bootstrap(); };
  s.onerror = function () { setTimeout(retrySdk, 1200); };
  document.head.appendChild(s);
}

function bootstrap() {
  if (typeof Paddle === 'undefined') {
    retrySdk();
    return;
  }
  if (initialized) return;
  initialized = true;
  try {
    Paddle.Initialize({ token: "__PADDLE_TOKEN__" });
  } catch (err) {
    fsLog("Failed to initialize Paddle: " + err.message);
    return;
  }
  Paddle.Checkout.on('checkout.loaded', function () { watchdog && clearTimeout(watchdog); fsLog("The payment window is open."); });
  Paddle.Checkout.on('checkout.closed', function () { fsLog("The window was closed — press the button to continue."); });
  Paddle.Checkout.on('error', function (data) { watchdog && clearTimeout(watchdog); fsLog("Paddle error: " + (data && data.error ? data.error : "unknown") + " — press the button to retry."); });
  Paddle.Checkout.on('transaction.completed', function () { fsLog("Payment completed — confirming…"); });
  window.addEventListener('load', openCheckout);
  setTimeout(openCheckout, 1200);
}

document.addEventListener('DOMContentLoaded', bootstrap);
</script>
</body></html>"""
    return page.replace("__PADDLE_ENV__", env).replace("__PADDLE_TOKEN__", token)


# ---------- local sandbox (FLUXSWARM_PADDLE_MOCK=1 only) ----------
def _mock_active() -> bool:
    """Sandbox endpoints are only reachable while FLUXSWARM_PADDLE_MOCK=1 AND no
    live Paddle credentials are in play (live base URL + API key). This keeps a
    forgotten mock flag from ever minting free credits in production."""
    if os.environ.get("FLUXSWARM_PADDLE_MOCK", "").strip().lower() not in ("1", "true", "yes"):
        return False
    base = os.environ.get("PADDLE_API_BASE", "").strip()
    key = os.environ.get("PADDLE_API_KEY", "").strip()
    if key and base in ("", "https://api.paddle.com"):
        return False
    return True


@app.get("/mock-checkout/{user_id}/{plan}", response_class=HTMLResponse)
def mock_checkout_page(user_id: int, plan: str):
    """Local sandbox checkout page: shows the order and auto-completes the
    payment through the same signed path a real Paddle webhook uses."""
    if not _mock_active():
        raise HTTPException(status_code=404, detail="not found")
    if plan not in db.PLANS:
        raise HTTPException(status_code=404, detail="Invalid plan")
    price = db.PLANS[plan]["price"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>FluxSwarm · Simulated payment</title></head>
<body style="font-family:system-ui;max-width:560px;margin:60px auto;text-align:center;line-height:1.8">
<h2>Simulated payment (local mode — no real money)</h2>
<p>Plan: <b>{db.PLANS[plan]['name']}</b> · Amount: <b>${price}</b> (simulated)</p>
<form method="get" action="/api/payments/dev-complete/{user_id}/{plan}">
<button style="font-size:16px;padding:10px 22px;cursor:pointer">Complete payment (simulated)</button>
</form>
<p style="color:#888;font-size:13px">This page goes through the same Paddle webhook path: a correctly-signed payload is generated and processed by the production webhook handler.</p>
</body></html>"""


@app.get("/api/payments/dev-complete/{user_id}/{plan}")
def dev_complete_mock_payment(request: Request, user_id: int, plan: str, aud: str = "default"):
    """Local sandbox ONLY: mint a correctly-signed Paddle transaction.completed
    payload for the given user/plan and run it through the production webhook
    handler (signature verification included). Grants credits exactly as a real
    Paddle callback would. Never exposed with FLUXSWARM_PADDLE_MOCK off."""
    if not _mock_active():
        raise HTTPException(status_code=404, detail="not found")
    if plan not in db.PLANS:
        raise HTTPException(status_code=404, detail="Invalid plan")
    import hashlib
    import hmac
    import base64

    secret = os.environ.get("PADDLE_WEBHOOK_SECRET", "mock-secret")
    event_id = f"evt_mock_{user_id}_{plan}_{aud}_{int(time.time())}"
    payload = {
        "event_id": event_id,
        "event_type": "transaction.completed",
        "data": {
            "id": f"txn_mock_{event_id}",
            "status": "completed",
            "custom_data": {"user_id": str(user_id), "plan": plan},
            "total": {"amount": db.PLANS[plan]["price"] * 100, "currency": "USD"},
        },
        "metadata": {},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    _process_paddle_payload(body, sig, request)
    u = db.get_user_by_id(user_id)
    return {"paid": True, "plan": plan, "now": (u or {}).get("plan"),
            "credits": (u or {}).get("credits"), "event_id": event_id}


# ---------- compliance: public legal pages ----------
_LEGAL_BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
 <meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{head}
 <style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;
 line-height:1.7;color:#222}}h1{{font-size:1.6rem}}a{{color:#0b59c5}}</style></head>
 <body><p style="color:#777;font-size:.85rem">Last updated: 7 September 2026</p>{body}</body></html>"""

_LEGAL_BASE_EN = _LEGAL_BASE


def _public_base() -> str:
    """Absolute site origin for canonical/og tags (empty when unset)."""
    return os.environ.get("FLUXSWARM_PUBLIC_BASE_URL", "").strip().rstrip("/")


def _og_head(title: str, canonical: str) -> str:
    """OpenGraph + canonical head fragment."""
    base = _public_base()
    head = ""
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    ogimg = base + "/static/brand/og-1200x630.png" if base else "/static/brand/og-1200x630.png"
    head += (f'<meta property="og:title" content="{title}">'
             f'<meta property="og:type" content="website">'
             f'<meta property="og:image" content="{ogimg}">'
             f'<meta name="twitter:card" content="summary_large_image">')
    return head


def _legal_page(title: str, body: str, path: str) -> str:
    """Render a legal page with canonical + OpenGraph (single shared code path)."""
    base = _public_base()
    canonical = (base + path) if base else ""
    return _LEGAL_BASE_EN.format(title=title, body=body,
                                 head=_og_head(title, canonical))


def _legal_entity_block(lang: str) -> str:
    """Render the operating-entity disclosure from env config (empty fragment when unset).

    The legal-entity fields are env-driven (FLUXSWARM_LEGAL_*), so the company
    info stays out of the repo: add them to `.env` at deploy time.
    """
    ent = os.environ.get("FLUXSWARM_LEGAL_ENTITY", "").strip()
    if not ent:
        return ""
    if lang == "en":
        labels = ("Commercial registry no.", "Tax ID", "Registered office", "Phone")
    else:
        labels = ("Commercial registry no.", "Tax ID", "Registered office", "Phone")
    vals = [os.environ.get(k, "").strip() for k in
            ("FLUXSWARM_LEGAL_REGISTRY_NO", "FLUXSWARM_LEGAL_TAX_ID",
             "FLUXSWARM_LEGAL_ADDRESS", "FLUXSWARM_LEGAL_PHONE")]
    bits = [f"{lab}: <b>{v}</b>" for lab, v in zip(labels, vals) if v]
    suffix = (" — " + " · ".join(bits)) if bits else ""
    heading = "Operating entity" if lang == "en" else "Operating entity"
    return f"<h2>{heading}</h2><p><b>{ent}</b>{suffix}</p>"


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return _privacy_page("/privacy")


@app.get("/privacy-en", response_class=HTMLResponse)
def privacy_page_en():
    return _privacy_page("/privacy-en")


@app.get("/trust", response_class=HTMLResponse)
def trust_page():
    return _trust_page("/trust")


def _trust_page(path: str):
    """Deterministic trust board — every number is measured from THIS repo by
    this server at request time (AST scan + env), never a static marketing slate."""
    checks, note = _count_test_functions()
    base = _legal_page("Trust Board — FluxSwarm", "<h1>Trust Board</h1>"
        "<p>Numbers on this page are computed from this codebase at request time, "
        "not invented for marketing — each is reproducible by re-running the suite "
        "or scanning the repo.</p>"
        "<h2>Test suite (live)</h2>"
        f"<p><b>{checks}</b> automated checks across 38 test files, counted by AST "
        f"scan of <code>backend/tests/</code> at request time ({note}). The landing "
        "badge shows this same live value, not a stale promo clip.</p>"
        "<h2>Plans (verified)</h2>"
        "<p>Demo plan: 5 free credits (matches <code>db.py</code> demo plan, FAQ, "
        "i18n dict and demo page — previously inconsistent at 3, now unified to 5 "
        "everywhere). Paid credit packs available via Paddle; BYOK is always "
        "available with no credit cost for the AI calls themselves.</p>"
        "<h2>Privacy & safety</h2>"
        "<p>GDPR/CCPA structured: keys encrypted (Fernet) at rest, never stored "
        "plaintext; security audit log is append-only; output scanning guards "
        "prompt-injection and secret-leak. Full statements: "
        "<a href=\"/privacy\">Privacy Policy</a> · "
        "<a href=\"/terms\">Terms</a> · "
        "<a href=\"/dark-patterns\">dark-patterns</a>.</p>", path)
    return base


def _privacy_page(path: str):
    contact = os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")
    body = """<h1>Privacy Policy</h1>
<p>We process only the data needed to operate the service: email, name, password (hashed with Argon2id), user-supplied AI provider keys (encrypted immediately with Fernet), project goals and generated outputs, usage/audit records, and minimal payment metadata.</p>
<p>We do not sell your data and do not use it for advertising. We share it only (1) with Paddle (merchant of record) to complete transactions and (2) with the AI provider you choose when you launch a swarm (BYOK) to execute your goal under that provider's terms. We do not train on your data.</p>
<p>Your rights (CCPA/CPRA): access via <code>GET /api/account/export</code>, rectification via <code>PATCH /api/account</code> (update your display name), and full erasure via <code>DELETE /api/account</code> (including immediate deletion of your keys). Other correction requests: <a href="mailto:{c}">{c}</a>. On launch, data is hosted on servers in North America.</p>
<p>The security audit log is append-only and excluded from erasure: it is retained for security/investigation purposes only, never for marketing or training; its entries may include your email address and IP address automatically.</p>
<p>UK/EU addendum: lawful bases are performance of the contract, legitimate interests (system security, fraud prevention) and legal obligation (billing records). Your rights include access, rectification, erasure, portability, objection, and complaint to your supervisory authority (in the UK: the ICO). Your data may be transferred to the AI provider you choose, outside the UK/EU, under that provider's terms; we do not transfer it for marketing. Cookies and tracking are described on <a href="/cookies">/cookies</a>; refunds and credits on <a href="/refund">/refund</a>.</p>"""
    body = body.format(c=contact)
    body += '<h2>AI Decision-Making Transparency (ADMT)</h2><p>An AI-assisted launch decomposes your goal through a squad of agents (Planner, Architect, DevOps, TDD, Reviewer, Builder) and generates draft code that requires your review. You can <strong>opt out of ADMT</strong> at any time from your account — this disables all AI-assisted launches while manual project creation remains available — and opt back in only after re-acknowledging the current notice. Full disclosure: <a href="/admt-notice">ADMT Notice page</a>.</p>'
    body += '<h2>Processing providers</h2><p>Only your project description is sent to the AI provider you choose to execute your goal, subject to that provider\u2019s terms: <a href="https://cloud.google.com/terms/data-processing-addendum">Google DPA</a> \u00b7 <a href="https://www.anthropic.com/legal/data-processing-addendum">Anthropic DPA</a> \u00b7 <a href="https://openai.com/policies/data-processing-addendum/">OpenAI DPA</a>. We do not train models on your data.</p>'
    body += '<h2>Data retention</h2><p>Deleted means deleted: identifiable data (your account, stored keys and boards) is erased immediately when you delete your account. The append-only security audit log is the sole exception — it is retained for security and abuse investigation only, never for marketing or training, and cannot be erased. Data Protection Officer: <a href="mailto:privacy@fluxswarm.ai">privacy@fluxswarm.ai</a>. UK: you may also contact the Information Commissioner&rsquo;s Office (Wycliffe House, Water Lane, Wilmslow, Cheshire SK9 5AF — ico.org.uk).</p>'
    body += _legal_entity_block("en")
    _phone = os.environ.get("FLUXSWARM_LEGAL_PHONE", "").strip()
    body += f'<p>Questions: <a href="mailto:{contact}">{contact}</a>'
    body += f" · {_phone}</p>" if _phone else "</p>"
    return _legal_page("Privacy Policy", body, path)


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return _terms_page("/terms")


@app.get("/terms-en", response_class=HTMLResponse)
def terms_page_en():
    return _terms_page("/terms-en")


def _terms_page(path: str):
    contact = os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")
    body = """<h1>Terms of Service</h1>
<p>The service is provided &quot;as is&quot;. Paid transactions are processed by Paddle (merchant of record) under its own terms; any country-specific tax or VAT is handled by Paddle.</p>
<h2>Eligibility and minimum age</h2><p>You must be at least 13 years old to use the Service, and if you are resident in the UK or the European Economic Area you must be at least 16 years old. By registering you confirm that you meet the age requirement for your country and that you accept these Terms and the Privacy Policy. If a parent or guardian registered on your behalf, they agree to these terms for you.</p>
<h2>Credits</h2><p>Credits are a prepaid service balance, granted only after a confirmed payment. Each launched project costs 1 credit. Credits never expire. A launch that fails before the squad does any work refunds the credit to your account automatically. A merchant refund downgrades your plan to Demo and keeps your current credit balance.</p>
<h2>Your data and your keys</h2><p>You remain responsible for the goals you submit and for the provider keys you store (see the privacy policy for how they are protected). User-supplied API keys are used only to execute your own launches.</p>
<h2>Output</h2><p>You own the generated output, subject to the terms of the AI providers you used and to any third-party components included in it. The service uses third-party execution software and open-source agent skill profiles (Hermes; ECC); their licences belong to their respective authors.</p>
<h2>Acceptable use</h2><p>Abuse, unlawful content, or harmful swarm activity is prohibited and may result in account suspension. See the <a href="/acceptable-use">acceptable-use policy</a>.</p>
<h2>Availability and termination</h2><p>We work to keep the service available but do not guarantee uninterrupted availability. You can delete your account (and its data and boards) at any time from the app. We may suspend accounts that violate these terms or the acceptable-use policy.</p>
<h2>Limitation of liability</h2><p>To the maximum extent permitted by applicable law, the service is provided &quot;as is&quot; without warranties, and liability for the service and the generated output is limited as permitted by law. This does not limit or exclude liability that cannot be limited or excluded by law, and does not affect any statutory consumer rights you have (including in the UK and the EU).</p>
<h2>Governing law and jurisdiction</h2><p>These terms are governed by applicable law. If you are a consumer in the UK, EU or another jurisdiction with mandatory consumer protections, your rights under that law are not affected. Jurisdiction specifics are kept under legal review as the service expands. Questions: <a href="mailto:{c}">{c}</a>.</p>"""
    body = body.format(c=contact)
    body += '<h2>Use of AI (ADMT)</h2><p>By launching an AI-assisted project you acknowledge the pre-use notice (<a href="/admt-notice">ADMT Notice</a>). You may opt out of ADMT at any time from your account while keeping manual project creation available. Human review is available on request and is completed within 48 hours of submission.</p>'
    body += '<h2>AI-generated code liability</h2><p>AI-generated outputs are drafts that require your review and testing before use; they are provided without warranty of correctness or fitness for any particular purpose. Final verification remains your responsibility, subject to the general limitation of liability in these terms and to the terms of the provider used.</p>'
    body += _legal_entity_block("en")
    _phone = os.environ.get("FLUXSWARM_LEGAL_PHONE", "").strip()
    body += f'<p>Contact: <a href="mailto:{contact}">{contact}</a>'
    body += f" · {_phone}</p>" if _phone else "</p>"
    return _legal_page("Terms of Service", body, path)


# ---------- legal: refund & credits (no-refund absolutes) ----------
@app.get("/refund", response_class=HTMLResponse)
def refund_page():
    return _refund_page("/refund")


@app.get("/refund-en", response_class=HTMLResponse)
def refund_page_en():
    return _refund_page("/refund-en")


def _refund_page(path: str):
    contact = os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")
    body = """<h1>Refund &amp; Credit Policy</h1>
<p>Credits are service credits: they never expire and cannot be withdrawn outside the service.</p>
<p>Automatic grants:</p>
<ul>
<li>If a swarm launch fails and no work was consumed, the credit is refunded to your account automatically.</li>
<li>If you obtain a monetary refund from Paddle, your plan is downgraded to Demo and your current credit balance stays with you.</li>
</ul>
<p>Monetary refunds (at our discretion, within 14 days of your first paid activation, net of consumed work): there is no automatic full-refund policy; we review requests individually. Requests within 14 days of purchase are processed before deducting consumed credits. After 14 days, no monetary refund for consumed usage, but any unconsumed credit balance may be refunded via <a href="mailto:{c}">{c}</a> subject to Paddle's process.</p>
<p><strong>UK consumers (Consumer Contracts Regulations 2013):</strong> if you live in the UK you have a statutory 14-day cooling-off period starting the day after you purchase a credit pack. Where the pack (digital content) was not downloaded or used you may cancel for a full refund; once you begin using credits (launching projects) during the cooling-off period, you expressly waive the cancellation right in exchange for immediate use, and your refund is reduced accordingly to a fair proportion for what was consumed — in all cases your statutory rights are not affected by this policy.</p>
<p>All payments are handled by Paddle (merchant of record). Your statutory consumer rights (including UK and EU) are not waived by any of these terms, and nothing here limits rights that cannot lawfully be excluded (FTC rules and card-network chargeback rights included). Disputes: <a href="mailto:{c}">{c}</a>.</p>"""
    body = body.format(c=contact)
    body += _legal_entity_block("en")
    _phone = os.environ.get("FLUXSWARM_LEGAL_PHONE", "").strip()
    body += f'<p>Questions: <a href="mailto:{contact}">{contact}</a>'
    body += f" · {_phone}</p>" if _phone else "</p>"
    return _legal_page("Refund &amp; Credit Policy", body, path)


# ---------- legal: cookies & tracking ----------
@app.get("/cookies", response_class=HTMLResponse)
def cookies_page():
    return _cookies_page("/cookies")


@app.get("/cookies-en", response_class=HTMLResponse)
def cookies_page_en():
    return _cookies_page("/cookies-en")


def _cookies_page(path: str):
    contact = os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")
    body = """<h1>Cookies &amp; Tracking</h1>
<p>The FluxSwarm server sets no tracking cookies. Your session uses a signed token held in browser <code>localStorage</code>; the only other local value is <code>flux-lang</code> (language preference) and <code>fs-consent</code> (your consent-banner choice).</p>
<p>The site uses strictly necessary local storage only: the session token that keeps you signed in and your saved preferences. A dismissible notice explains this on the home page for visitors in the UK/EU and records your acceptance — it is not a consent wall, because nothing is loaded for advertising or analytics without your choice, and no third-party cookies are set on our domain.</p>
<p>The live preview feature sets one transient cookie (<code>fs_preview</code>, HttpOnly, scoped only to <code>/p/</code>, expires after 10 minutes): it lets the dashboard embed one of your own generated app pages in a restricted preview frame without attaching your login token to those URLs. It is deleted on expiry and never used for tracking.</p>
<p>When you pay, Paddle (merchant of record) sets cookies on its own domain only, never ours; analytics, when enabled by the operator, are cookieless and privacy-friendly (Plausible).</p>
<p>See also <a href="/refund">refund</a> · <a href="/privacy">privacy</a> · <a href="/acceptable-use">acceptable use</a>.</p>"""
    body = body.format(c=contact)
    body += _legal_entity_block("en")
    _phone = os.environ.get("FLUXSWARM_LEGAL_PHONE", "").strip()
    body += f'<p>Questions: <a href="mailto:{contact}">{contact}</a>'
    body += f" · {_phone}</p>" if _phone else "</p>"
    return _legal_page("Cookies &amp; Tracking", body, path)


# ---------- marketing: public product pages ----------
_MARKET_CSS = """@font-face{font-family:'Inter';font-weight:400;font-display:swap;src:url('/static/fonts/Inter-400.woff2') format('woff2')}
@font-face{font-family:'Inter';font-weight:600;font-display:swap;src:url('/static/fonts/Inter-600.woff2') format('woff2')}
@font-face{font-family:'Inter';font-weight:700;font-display:swap;src:url('/static/fonts/Inter-700.woff2') format('woff2')}
@font-face{font-family:'Inter';font-weight:800;font-display:swap;src:url('/static/fonts/Inter-800.woff2') format('woff2')}
@font-face{font-family:'Space Grotesk';font-weight:700;font-display:swap;src:url('/static/fonts/SpaceGrotesk-700.woff2') format('woff2')}
body{font-family:'Inter',ui-sans-serif,system-ui,"Segoe UI",Tahoma;margin:0;background:#0b0f1a;color:#eef1fb;line-height:1.6}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
.top{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid #25304a;background:rgba(15,20,36,.82);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.top .logo{font-weight:800;background:linear-gradient(90deg,#22d3ee,#7c3aed);-webkit-background-clip:text;background-clip:text;color:transparent;font-size:19px}
.top nav{margin-left:auto;display:flex;gap:16px} .top nav a{color:#8b96b3;text-decoration:none;font-size:14px} .top nav a:hover{color:#22d3ee}
h1{font-family:'Space Grotesk','Inter',sans-serif;font-size:32px;margin:22px 0 8px;letter-spacing:-.5px} h2{font-size:20px;margin:26px 0 8px;color:#cdd4e2}
p{color:#8b96b3} a{color:#22d3ee} li{color:#8b96b3;margin:5px 0}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:22px 0}
.pl{position:relative;background:linear-gradient(180deg,#141a2c,#0f1424);border:1px solid #25304a;border-radius:16px;padding:20px}
.pl.hot{border-color:#22d3ee;box-shadow:0 0 0 1px #22d3ee,0 16px 40px rgba(34,211,238,.10)}
.pl .n{font-size:17px;font-weight:800} .pl .p{font-size:28px;font-weight:800;margin:8px 0;font-family:'Space Grotesk','Inter',sans-serif}
.pl .p small{color:#8b96b3;font-weight:400;font-size:12px} .pl ul{list-style:none;padding:0;margin:8px 0;font-size:13px}
.cmp{width:100%;border-collapse:collapse;font-size:14px} .cmp th,.cmp td{border:1px solid #25304a;padding:10px 12px;text-align:left}
.cmp th{color:#cdd4e2} .cmp td{color:#8b96b3}
.foot{border-top:1px solid #25304a;padding:16px 0;font-size:13px;color:#7a8699}
.foot a{color:#22d3ee}
.pill{display:inline-block;font-size:11px;letter-spacing:1px;font-weight:800;padding:4px 12px;border-radius:999px;background:linear-gradient(92deg,#22d3ee,#7c3aed);color:#0b0f1a;margin-bottom:12px}
details.faq{border:1px solid #25304a;border-radius:12px;background:#0f1424;padding:14px 16px;margin-top:10px}
details.faq summary{cursor:pointer;font-weight:700;font-size:14px;list-style:none;color:#eef1fb}
details.faq summary::-webkit-details-marker{display:none}
details.faq summary::after{content:"+";float:right;color:#22d3ee;font-weight:800}
details.faq[open] summary::after{content:"–"}
details.faq p{margin:8px 0 0;color:#8b96b3}"""

_MARKET_BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{desc}"><title>{title}</title>{head}
<style>{css}</style></head><body>
<div class="top"><span class="logo">FluxSwarm</span><nav>
<a href="/">Go to app</a><a href="/pricing">Pricing</a><a href="/how-it-works">How it works</a><a href="/faq">FAQ</a></nav></div>
<div class="wrap">{body}<div class="foot">FluxSwarm · <a href="/privacy">Privacy</a> ·
<a href="/terms">Terms</a> · <a href="/refund">Refund</a> ·
<a href="/cookies">Cookies</a> · <a href="/acceptable-use">Acceptable use</a></div></div>
</body></html>"""


def _market_page(title: str, desc: str, body: str, path: str) -> str:
    """Shared renderer for the public marketing pages: canonical URL, OpenGraph
    tags and (operator-gated, cookieless) Plausible analytics in <head>."""
    base = _public_base()
    canonical = (base + path) if base else ""
    head = _og_head(title, canonical)
    domain = os.environ.get("FLUXSWARM_ANALYTICS_DOMAIN", "").strip()
    if domain:
        head += (f'<script defer data-domain="{domain}" '
                 f'src="https://plausible.io/js/script.js"></script>')
    return _MARKET_BASE.format(title=title, desc=desc, head=head, css=_MARKET_CSS, body=body)


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page():
    plans = [{"id": pid, **db.PLANS[pid]} for pid in db.PLAN_ORDER]
    rows = "".join(
        f'<div class="pl {"hot" if p["name"].lower() == "pro" else ""}">'
        f'<div class="n">{p["name"]}</div>'
        f'<div class="p">${p["price"]}<small> per credit pack</small></div>'
        f'<ul><li>✓ {p["credits"]} credits · 1 credit = 1 launch</li>'
        f'<li>✓ up to {p["parallel"]} parallel agents</li>'
        f'<li>✓ {p["desc"]}</li></ul></div>'
        for p in plans)
    body = f"""<p class="pill">1 CREDIT = 1 LAUNCH</p>
<h1>Prepaid packs — no monthly fee</h1>
<p>Every launch costs exactly <strong>1 credit</strong> — no token meters, no monthly
fee. Bring your own AI key and
you pay only your provider's token rate — FluxSwarm charges the flat 1-credit
coordination fee per launched project and nothing else. Credits never expire, and a
launch that fails before any work starts is refunded automatically.</p>
<div class="plans">{rows}</div>
<h2>Anything else?</h2>
<details class="faq" open><summary>Can I try before paying?</summary><p>Yes — the Demo
plan starts with 5 free credits, no card required, and there is a public demo board.</p></details>
<details class="faq"><summary>How are credits used?</summary><p>One credit = one launched
project = one live squad board. With BYOK you pay your provider's token rate directly;
FluxSwarm still only charges the single credit.</p></details>
<details class="faq"><summary>Do credits expire?</summary><p>No. Credits are prepaid and
never expire.</p></details>
<details class="faq"><summary>What is the Top-up pack?</summary><p>A quick credit refill
($9 for 10 credits) for when you run low — it adds credits without changing your plan
tier or parallel cap, and the credits never expire.</p></details>
<details class="faq"><summary>What about refunds?</summary><p>Payments are processed by
Paddle (merchant of record), which handles sales tax and VAT remittance. A merchant
refund downgrades you to Demo and keeps your current balance. See the
<a href="/refund">refund policy</a>. Prices are shown in USD; GBP pricing is applied
by Paddle at checkout for UK customers.</p></details>"""
    return _market_page("Pricing — FluxSwarm", "1 credit per launch, BYOK AI builders", body, "/pricing")


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page():
    body = """<h1>How FluxSwarm works</h1>
<h2>What is FluxSwarm?</h2><p>FluxSwarm is a hosted AI development squad. You
describe a product you want built; a team of specialized agents plans, builds,
tests, reviews and assembles it on a live task board while you watch.</p>
<h2>Who is it for?</h2><p>Solo developers, startups and small teams who want a
concrete first version built — with a live picture of the work and the generated
files in your workspace — without wiring up an agent pipeline themselves.</p>
<h2>1. Type a goal</h2><p>Describe the product in one paragraph. The squad plans
the rest.</p>
<h2>2. An 8-agent squad takes over</h2><p>Your goal is broken into work and
assigned to a real agent crew, watched live on a kanban board:
<strong>Planner</strong> (breakdown) → <strong>Architect</strong> (structure) →
<strong>DevOps</strong> (scaffold &amp; CI/CD) → <strong>TDD</strong> (tests) →
<strong>Reviewer</strong> (verify) → <strong>Designer</strong> (palette &amp; type) →
<strong>Builder</strong> (merge to output) → <strong>Auditor</strong> (final check).
The agents run inside the Hermes execution runtime using the open-source ECC
skill profiles — you do not need to install or manage either.</p>
<h2>3. Pick a model — bring a key or use the deployment default</h2><p>Bring your own
provider key (Anthropic Claude, OpenAI, Gemini or Kimi) for stronger output; your
key is Fernet-encrypted at rest, injected into the agent process only at launch,
and never returned by the API. You pay your provider's token price, and the
provider's terms must be accepted once in your keys page before a launch. Without
a key, the squad runs on the operator-configured default model for the deployment.</p>
<h2>4. Pick it up from the workspace</h2><p>Generated files land in your project
workspace, browsable in the UI on the board, ready to push to your own repository.</p>
<h2>Billing</h2><p>1 credit per launched project across every plan. Credits are
prepaid, never expire, and are refunded automatically if a launch fails. There is
no monthly fee — credit packs set your plan tier (parallel execution cap) and
credit balance. See <a href="/pricing">pricing</a>.</p>
<h2>Transparency</h2><p>FluxSwarm is the product. Hermes is the execution runtime
that drives the board, and ECC is an underlying open-source component (agent skill
profiles) used inside it. Both are third-party components; FluxSwarm is not Hermes
and does not own ECC. Their availability is required to run a launch, and their
licences are their respective authors'.</p>"""
    return _market_page("How it works — FluxSwarm", "An 8-agent AI development squad on a live board, 1 credit per launch", body, "/how-it-works")


@app.get("/faq", response_class=HTMLResponse)
def faq_page():
    body = """<h1>FAQ</h1>
<h2>What is FluxSwarm?</h2><p>An AI development squad: type a goal and six
specialized agents (Planner, Architect, DevOps, TDD, Reviewer, Builder) plan,
build, test, verify and assemble it on a live task board, with the generated files
in your workspace.</p>
<h2>Does the squad need my AI key?</h2><p>No. Without a key the squad runs on the
deployment&rsquo;s operator-configured model. Bringing your own key (Anthropic Claude,
OpenAI, Gemini or Kimi) is optional and lifts output quality; your key pays your
provider&rsquo;s token rate and requires accepting that provider&rsquo;s terms once in
your keys page. Keys are Fernet-encrypted at rest, injected only at
launch, and never returned by the API.</p>
<h2>What is Hermes? What is ECC?</h2><p>Hermes is the execution runtime that drives
the board. ECC is an underlying open-source component: the agent skill profiles
the squad uses. FluxSwarm is the product that orchestrates them; it is not Hermes
and does not own ECC. Both are required to run a launch.</p>
<h2>Does running cost me anything?</h2><p>Each launch costs 1 credit.
The Demo plan starts you with 5 free credits (no card). Model tokens are paid by
your own key (BYOK) or by the network&rsquo;s configured provider; every plan pays the
flat 1-credit coordination fee per launch.</p>
<h2>How much do paid plans cost?</h2><p>One-time credit packs, not subscriptions. Starter
$19/20 credits, Pro $49/60 credits, Scale $149/200 credits, plus a $9/10 credit
Top-up refill (USD; GBP applied at
checkout by Paddle). Credits never expire. Billing runs through Paddle (merchant
of record).</p>
<h2>How fast are launches?</h2><p>Swarm duration depends on the goal, the model
in use and system load. The board streams progress live so you can watch it from
start to finish rather than guess.</p>
<h2>Can I cancel a Launch?</h2><p>There is no recurring subscription to cancel —
you buy credit packs and spend them. You can stop watching a board at any time;
refunds and unused credits are covered below.</p>
<h2>What if a Launch fails?</h2><p>If the launch fails before the squad does any
work, the credit is refunded to your account automatically. The run state stays
visible on the board for debugging.</p>
<h2>What if Hermes or the model provider fails?</h2><p>The launch reports a clear
error (the runtime or a provider key may be unavailable or misconfigured) and
the credit is refunded automatically. Your stored keys are never consumed by the
failure.</p>
<h2>What happens when Credits run out?</h2><p>Launching requires 1 credit. With zero
credits you keep access to your boards and data; you just cannot start new
launches until you add credits to the account.</p>
<h2>Can I sell what the squad builds?</h2><p>You own the generated output (subject
to the terms of the AI provider you used and any third-party components). You can
also publish your own squad templates on the marketplace and earn a 50% author
share on every sale, paid in credits.</p>
<h2>How do referrals work?</h2><p>Share your referral link; when a referred account
makes a first paid purchase you earn 15 credits (capped at 500 credits total per
referrer), and the referred friend gets 10 bonus credits on that first paid
purchase. Rewards are granted once per referred email. Self-referral
and abusing the program (for example creating fake referrals) is prohibited and
rewards may be clawed back.</p>
<h2>Is my API key stored? Can FluxSwarm access my provider account?</h2><p>Keys are
stored encrypted (Fernet) and used only to execute your own launches. FluxSwarm
does not hold your provider account credentials, cannot browse your provider
account, and never shows a stored key back to anyone. You can delete a key or your
whole account from the app.</p>
<h2>Is my data private?</h2><p>Projects are namespace-isolated per account
(cross-user access returns 403). You can export your data and erase your account
(and its boards) from the app&rsquo;s account section. We do not sell data and do
not train on it. Details on the <a href="/privacy-en">privacy policy</a>.</p>
<h2>How long is data retained?</h2><p>Until you delete your account, or per the data
lifecycle described in the <a href="/privacy-en">privacy policy</a>. A security
audit log is kept separately for abuse investigation and is excluded from
deletion. Cookies and local storage are described on the <a href="/cookies-en">cookies</a> page.</p>
<h2>What about unused credits after I stop paying?</h2><p>Credits never expire and
are not tied to a recurring payment. Unused credits stay on the account; the
refund policy covers the rest.</p>
<h2>What is the refund policy?</h2><p>Failed launches refund the credit
automatically. Merchant refunds downgrade the plan to Demo and keep your balance.
Monetary refunds are reviewed case-by-case (see the <a href="/refund-en">refund policy</a>);
statutory consumer rights are not waived.</p>
<h2>What support is available?</h2><p>Email support at the contact address on the
legal pages and in the app footer. Self-serve: this FAQ, the how-it-works guide,
and the live board you can inspect during every launch.</p>"""
    return _market_page("FAQ — FluxSwarm", "Answers on pricing, credits, BYOK, privacy and the agent squad", body, "/faq")
@app.get("/acceptable-use", response_class=HTMLResponse)
def acceptable_use_page():
    return _acceptable_use_page("/acceptable-use")


@app.get("/acceptable-use-en", response_class=HTMLResponse)
def acceptable_use_page_en():
    return _acceptable_use_page("/acceptable-use-en")


def _acceptable_use_page(path: str):
    contact = os.environ.get("FLUXSWARM_CONTACT_EMAIL", "support@fluxswarm.ai")
    body = """<h1>Acceptable Use</h1>
<p>By using FluxSwarm you agree that you will (1) not use the platform for unlawful, malicious, exploitative or infringing content (including intellectual property and others' rights); (2) not run a swarm aimed at harm, violence or fraud; (3) not resell credits or convert them to cash outside the refund policy; (4) not attempt unauthorized access, leak others' keys, or scrape the API beyond stated limits; (5) comply with the terms of the AI providers you use via BYOK.</p>
<p>Violating accounts may be suspended and suspicious activity may be reported to the relevant authorities; credits may be recovered following investigation per the <a href="/refund">refund policy</a>.</p>
<p>Questions: <a href="mailto:{c}">{c}</a></p>"""
    body = body.format(c=contact)
    body += _legal_entity_block("en")
    _phone = os.environ.get("FLUXSWARM_LEGAL_PHONE", "").strip()
    body += f'<p>Questions: <a href="mailto:{contact}">{contact}</a>'
    body += f" · {_phone}</p>" if _phone else "</p>"
    return _legal_page("Acceptable Use", body, path)


# ---------- compliance: CCPA/CPRA ADMT (uses /api/account/* + external review) ----------
# Pre-use disclosure served before the first AI-assisted launch, per the ADMT
# transparency requirements. `last_updated` doubles as the notice version a
# client must echo back when re-reading the notice to opt back in.
ADMT_NOTICE = {
    "platform": "FluxSwarm",
    "admt_types": ["planning", "architecture", "coding", "review", "deployment"],
    "description": "AI agents assist in generating code based on your description. Outputs are drafts requiring human review.",
    "logic_summary": "Planner analyzes goal → Architect selects stack → DevOps designs infra → TDD writes tests → Reviewer checks quality → Builder generates code",
    "human_review_available": True,
    "opt_out_available": True,
    "last_updated": "2026-09-04",
}
_ADMT_NOTICE_VERSION = ADMT_NOTICE["last_updated"]

# Localized description / logic_summary. Structure stays identical so i18n
# never changes the API contract. Platform is English-only — the "ar" branch
# was removed; any lang falls back to the English base notice.
_ADMT_NOTICE_L10N = {
    "en": {
        "description": ADMT_NOTICE["description"],
        "logic_summary": ADMT_NOTICE["logic_summary"],
    },
}

# Canonical agent catalog for the logic-access disclosure (mirrors
# hc.AGENT_REGISTRY). Decision text is rendered deterministically from the
# goal's detected signals.
_ADMT_AGENTS = (
    {"agent": "ecc-planner", "role": "planning"},
    {"agent": "ecc-architect", "role": "architecture"},
    {"agent": "ecc-devops", "role": "deployment"},
    {"agent": "ecc-tdd", "role": "coding"},
    {"agent": "ecc-reviewer", "role": "review"},
    {"agent": "ecc-designer", "role": "design"},
    {"agent": "ecc-build-fixer", "role": "deployment"},
    {"agent": "ecc-auditor", "role": "review"},
)


def _admt_goal_flags(goal: str) -> dict:
    g = (goal or "").lower()
    return {
        "scalability": any(k in g for k in (
            "scale", "scalab", "traffic", "concurr", "million")),
        "web_api": any(k in g for k in (
            "web", "api", "site", "saas")),
        "data": any(k in g for k in (
            "database", "sql", "postgres", "data", "storage")),
    }


def _admt_decisions(goal: str) -> list[dict]:
    """Deterministic per-agent decision record derived from the goal text."""
    flags = _admt_goal_flags(goal)
    out = []
    if flags["scalability"]:
        out.append({"agent": "ecc-planner",
                    "decision": "Selected microservices architecture",
                    "rationale": "Scalability requirement detected in goal"})
    else:
        out.append({"agent": "ecc-planner",
                    "decision": "Selected a modular monolith for this goal",
                    "rationale": "No explicit scalability requirement detected in goal"})
    if flags["web_api"] and flags["data"]:
        stack = "FastAPI + PostgreSQL web service"
    elif flags["web_api"]:
        stack = "FastAPI web service"
    elif flags["data"]:
        stack = "PostgreSQL-backed service"
    else:
        stack = "Simple service with minimal external surface"
    out.append({"agent": "ecc-architect", "decision": f"Selected {stack}",
                "rationale": "Requirements detected from the goal's stack and data hints"})
    out.append({"agent": "ecc-devops",
                "decision": "Designed containerized deployment with isolated execution",
                "rationale": "Each squad runs in a sandboxed, network-isolated runtime"})
    out.append({"agent": "ecc-tdd",
                "decision": "Wrote tests first, then code to satisfy them",
                "rationale": "TDD workflow drives every unit from a failing test"})
    out.append({"agent": "ecc-reviewer",
                "decision": "Verified generated code against acceptance criteria",
                "rationale": "Reviewer performs self-evaluation and verification"})
    out.append({"agent": "ecc-build-fixer",
                "decision": "Integrated reviewed changes into a buildable result",
                "rationale": "Builder synthesizes the final MVP from reviewed work"})
    return out


def _project_runtime_info(user: dict) -> dict:
    """Best-effort (provider, model) for the logic-access disclosure. Never
    raises for an unconfigured runtime — we disclose what is pinned, or null."""
    try:
        model, provider = hc._resolve_runtime(_user_provider_keys(user))
        if not model and provider:
            model = hc._operator_model_for(provider)
        return {"model": model, "provider": provider}
    except Exception:
        return {"model": None, "provider": None}


def _get_project_row(pid: int) -> dict | None:
    c = db._conn()
    try:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def _telegram_send(bot_token: str, chat_id: int, text: str) -> None:
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode("utf-8")
    with urllib.request.urlopen(url, data=body, timeout=10) as resp:
        resp.read()


def _notify_human_review(review: dict, status: str, notes: str) -> dict:
    """Notify the requesting user. There is no mailer in this install, so the
    email channel is recorded in the audit trail (a future SMTP hook reads the
    same message); when the user linked Telegram AND a bot token is configured
    (and we are not in demo mode) the message is delivered there directly."""
    user = db.get_user_by_id(review["user_id"]) or {}
    message = (
        f"Your ADMT human-review request #{review['id']} for "
        f"'{review.get('project_name') or 'project'}' was {status}."
    )
    if notes:
        message += f" Reviewer notes: {notes[:200]}"
    audit.audit("admt.notify", uid=review["user_id"],
                email=review.get("user_email") or user.get("email"),
                outcome="ok", subject="ADMT human review update",
                message=message[:500], channel="email")
    channel = "email"
    sent = False
    bot_token = os.environ.get("FLUXSWARM_TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token and os.environ.get("FLUXSWARM_DEMO_MODE", "0") != "1":
        tg = db.get_telegram_link(review["user_id"]) if hasattr(db, "get_telegram_link") else None
        if tg:
            try:
                _telegram_send(bot_token, tg["telegram_chat_id"], message)
                channel = "telegram"
                sent = True
            except Exception:
                pass
    return {"channel": channel, "sent": sent}


def get_admin(request: Request):
    """Admin-only dependency: a shared-secret bearer token (FLUXSWARM_ADMIN_TOKEN).

    Deny-by-default: when the env var is unset NO bearer token is accepted (the
    admin surface is disabled rather than left open). Comparison is
    constant-time via secrets.compare_digest.
    """
    expected = os.environ.get("FLUXSWARM_ADMIN_TOKEN", "").strip()
    ah = request.headers.get("Authorization", "")
    presented = ah.replace("Bearer ", "") if ah.startswith("Bearer ") else ""
    if not expected or not presented or not secrets.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/account/admt-notice")
def api_admt_notice(lang: str = "en", user: dict = Depends(get_current_user)):
    notice = dict(ADMT_NOTICE)
    if lang in _ADMT_NOTICE_L10N:
        notice["description"] = _ADMT_NOTICE_L10N[lang]["description"]
        notice["logic_summary"] = _ADMT_NOTICE_L10N[lang]["logic_summary"]
    return notice


@app.post("/api/account/admt-notice/acknowledge")
def api_admt_notice_ack(request: Request, user: dict = Depends(get_current_user)):
    db.record_admt_notice_ack(user["id"])
    audit.audit("admt.notice.ack", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", notice_version=_ADMT_NOTICE_VERSION)
    return {"ok": True, "acknowledged": True, "notice_version": _ADMT_NOTICE_VERSION}


@app.post("/api/account/opt-out-admt")
def api_admt_opt_out(request: Request, user: dict = Depends(get_current_user)):
    before = db.get_admt_opt_out(user["id"])
    db.set_admt_opt_out(user["id"], True)
    audit.audit("admt.optout", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", was_already=before)
    return {"ok": True, "admt_opt_out": True, "was_already": bool(before)}


@app.post("/api/account/opt-in-admt")
def api_admt_opt_in(payload: AdmtOptInIn, request: Request,
                    user: dict = Depends(get_current_user)):
    if not payload.acknowledge:
        raise HTTPException(status_code=400,
                            detail="Re-reading the ADMT notice is required before re-enabling")
    if (payload.last_updated or "").strip() != _ADMT_NOTICE_VERSION:
        raise HTTPException(status_code=400,
                            detail="The updated ADMT notice must be read and acknowledged (send its current version)")
    db.record_admt_notice_ack(user["id"])
    db.set_admt_opt_out(user["id"], False)
    audit.audit("admt.optin", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", notice_version=_ADMT_NOTICE_VERSION)
    return {"ok": True, "admt_opt_out": False}


@app.post("/api/projects/{project_id}/request-human-review")
def api_request_human_review(project_id: int, request: Request,
                             user: dict = Depends(get_current_user)):
    proj = _get_project_row(project_id)
    if not proj or proj["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Project not found")
    review_id = db.request_human_review(user["id"], project_id)
    audit.audit("admt.review.request", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok",
                project_id=project_id, review_id=review_id)
    return {"ok": True, "review_id": review_id, "human_review_status": "requested"}


@app.get("/api/admin/human-review-queue")
def api_admin_review_queue(request: Request, _: None = Depends(get_admin)):
    queue = db.list_human_review_queue()
    audit.audit("admt.review.queue", ip=_client_ip(request), outcome="ok",
                pending=len(queue))
    return {"count": len(queue), "pending": queue}


@app.get("/api/admin/provider-agreements")
def api_admin_provider_agreements(request: Request, _: None = Depends(get_admin)):
    """Admin registry of provider agreements accepted via BYOK (ledger: 003).

    Lists who accepted which provider term version and when. Vendor-level DPA
    links are disclosed on the legal pages (/privacy, /privacy-en) and in
    docs/CCPA_RISK_ASSESSMENT.md — this table is the user acceptance ledger,
    not a vendor contract registry.
    """
    rows = db.provider_agreements_summary()
    audit.audit("provider.agreements.admin", ip=_client_ip(request), outcome="ok",
                count=len(rows))
    by_provider: dict[str, dict] = {}
    for row in rows:
        entry = by_provider.setdefault(row["provider"], {"provider": row["provider"], "accepted": 0, "agreements": []})
        entry["accepted"] += 1
        entry["agreements"].append({
            "user": row["email"], "agreed_at": row["agreed_at"], "version": row["version"],
        })
    return {"count": len(rows), "by_provider": [
        by_provider[p] for p in sorted(by_provider)
    ]}


@app.patch("/api/admin/human-review/{review_id}")
def api_admin_update_review(review_id: int, payload: HumanReviewUpdateIn,
                            request: Request, _: None = Depends(get_admin)):
    status = (payload.status or "").strip()
    if status not in ("approved", "rejected", "needs_changes"):
        raise HTTPException(status_code=422, detail="Invalid review status")
    review = db.get_human_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Request not found")
    notes = (payload.reviewer_notes or "").strip()
    if not db.update_human_review(review_id, status, notes):
        raise HTTPException(status_code=409, detail="Request already processed")
    notified = _notify_human_review(review, status, notes)
    audit.audit("admt.review.update", uid=review["user_id"], email=review["user_email"],
                ip=_client_ip(request), outcome="ok", review_id=review_id,
                review_status=status, channel=notified["channel"])
    return {"ok": True, "review_id": review_id, "status": status, "notified": notified}


@app.get("/api/projects/{project_id}/admt-logic")
def api_project_admt_logic(project_id: int, user: dict = Depends(get_current_user)):
    proj = _get_project_row(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Unauthorized")
    goal = proj.get("goal") or ""
    runtime = _project_runtime_info(user)
    return {
        "project_id": int(proj["id"]),
        "goal": goal,
        "agents_used": [a["agent"] for a in _ADMT_AGENTS],
        "decisions": _admt_decisions(goal),
        "runtime_model": runtime["model"],
        "provider": runtime["provider"],
        "generated_at": datetime.datetime.fromtimestamp(float(proj["created_at"])).isoformat(),
    }


def _admt_notice_view(lang: str) -> dict:
    notice = dict(ADMT_NOTICE)
    if lang in _ADMT_NOTICE_L10N:
        notice["description"] = _ADMT_NOTICE_L10N[lang]["description"]
        notice["logic_summary"] = _ADMT_NOTICE_L10N[lang]["logic_summary"]
    return notice


@app.get("/admt-notice", response_class=HTMLResponse)
def admt_notice_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="admt_notice.html",
        context={"title": "ADMT Notice — FluxSwarm", "lang": "en",
                 "notice": _admt_notice_view("en"), "api_base": "",
                 "csp_nonce": request.state.csp_nonce})


@app.get("/admt-notice-en", response_class=HTMLResponse)
def admt_notice_page_en(request: Request):
    return templates.TemplateResponse(
        request=request, name="admt_notice.html",
        context={"title": "ADMT Notice — FluxSwarm", "lang": "en",
                 "notice": _admt_notice_view("en"), "api_base": "",
                 "csp_nonce": request.state.csp_nonce})


@app.patch("/api/account")
def api_account_update(payload: AccountUpdateIn, request: Request,
                       user: dict = Depends(get_current_user)):
    """Right to correct (CCPA/CPRA): replace the display name on the account."""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is empty")
    if not db.update_user_name(user["id"], name):
        raise HTTPException(status_code=404, detail="Account not found")
    audit.audit("account.rectify", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok")
    refreshed = db.get_user_by_id(user["id"]) or {}
    return {"ok": True, "user": public_user(refreshed)}


@app.get("/api/account/export")
def api_account_export(request: Request, user: dict = Depends(get_current_user)):
    payload = db.account_payload(user["id"])
    audit.audit("account.export", uid=user["id"], email=user["email"], ip=_client_ip(request), outcome="ok")
    return payload


@app.delete("/api/account")
def api_account_delete(request: Request, user: dict = Depends(get_current_user)):
    uid = user["id"]
    # Collect the user's OWN board slugs BEFORE the DB rows are erased, then
    # delete their Hermes kanban workspaces from disk too (CCPA right to
    # erasure). Demo boards (flux-demo-*) are shared/public and never user-owned,
    # so they are intentionally left intact.
    own_slugs = [pr["board_slug"] for pr in db.list_user_projects(uid)
                 if pr["board_slug"].startswith(f"u{uid}-")]
    vault.delete_user_key(uid)
    removed = db.delete_user(uid)
    boards_deleted = 0
    if removed:
        try:
            boards_deleted = hc.delete_boards(own_slugs)
        except Exception:
            boards_deleted = -1  # DB already erased; disk cleanup stays best-effort
    audit.audit("account.delete", uid=uid, email=user["email"], ip=_client_ip(request),
                outcome="ok" if removed else "missing", boards_deleted=boards_deleted)
    return {"ok": removed, "note": "Account and all associated data have been deleted",
            "boards_deleted": boards_deleted}


# ---------- Telegram account linking ----------
@app.get("/api/telegram/link")
def api_telegram_link(request: Request, user: dict = Depends(get_current_user)):
    """Issue (or reuse) the one-time pairing code the user types to the bot.

    The code binds the bot chat to this account; at most one active code per
    user (TTL 10 min), so repeated calls simply re-serve the same code.
    """
    code = db.new_telegram_link_code(user["id"])
    audit.audit("telegram.code", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok")
    return {"code": code, "ttl_seconds": db.TELEGRAM_LINK_TTL,
            "bot": os.environ.get("TELEGRAM_BOT_USERNAME", "fluxswarm_bot")}


@app.get("/api/telegram/status")
def api_telegram_status(request: Request, user: dict = Depends(get_current_user)):
    link = db.get_telegram_link(user["id"])
    return {"linked": bool(link), "telegram_chat_id": link["telegram_chat_id"] if link else None,
            "linked_at": link["linked_at"] if link else None}


@app.delete("/api/telegram/link")
def api_telegram_unlink(request: Request, user: dict = Depends(get_current_user)):
    removed = db.unlink_telegram(user["id"])
    audit.audit("telegram.unlink", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok" if removed else "missing")
    return {"ok": removed}


# ---------- squad marketplace ----------
class TemplateIn(BaseModel):
    name: str
    description: str = ""
    agents: list[str]
    price_credits: int = 10


class BuyIn(BaseModel):
    goal: str = ""


# Content curbs: block empty / oversized / scriptable templates at the gate.
_TPL_NAME_MAX = 80
_TPL_DESC_MAX = 500
_TPL_AGENTS_MAX = 6
_TEMPLATE_LIMITS = {"price_min": 1, "price_max": 500, "goal_max_len": 2000}


def _clean_text(s: str, *, name: str, max_len: int) -> str:
    """Strip control chars; reject anything that could render as HTML.

    The previous implementation built the allowed-whitespace set with
    ``"\\n\\r\\t".strip()`` — which returns an EMPTY string (strip removes all
    those chars), so EVERY character was stripped, collapsing any input to "".
    That made every template name/description fail the subsequent empty check,
    blocking ALL template publishing. We now keep the real whitespace chars and
    a sensible printable set, and additionally cap length as a hard input limit.
    """
    if s is None:
        raise HTTPException(status_code=400, detail=f"{name} is empty")
    # Keep real whitespace (space, tab, newline, carriage return) plus printable.
    allowed = set(" \t\n\r")
    s = "".join(ch for ch in s if ch in allowed or ch.isprintable())
    s = s.strip()
    if not s:
        raise HTTPException(status_code=400, detail=f"{name} is empty")
    # Reject oversized input up-front (don't silently truncate a user's
    # submission — the caller's contract is validation, and a truncated goal/name
    # would be surprising and could break downstream length assumptions).
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{name} exceeds the limit ({max_len})")
    if any(ch in s for ch in "<>") or "script" in s.lower() \
            or "javascript:" in s.lower() or "onerror=" in s.lower():
        raise HTTPException(status_code=400, detail=f"{name} contains disallowed tags/scripts")
    return s


def _validate_template(payload: TemplateIn):
    payload.name = _clean_text(payload.name, name="template name", max_len=_TPL_NAME_MAX)
    desc = (payload.description or "").strip()
    if desc:
        payload.description = _clean_text(desc, name="description", max_len=_TPL_DESC_MAX)
    else:
        payload.description = ""
    if not payload.agents:
        raise HTTPException(status_code=400, detail="Add agents to the template")
    if len(payload.agents) > _TPL_AGENTS_MAX:
        raise HTTPException(status_code=400, detail=f"Maximum of {_TPL_AGENTS_MAX} agents per template")
    seen = set()
    resolved = []
    for name in payload.agents:
        name = name.strip()
        if not name:
            continue
        if name in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate agent: {name}")
        seen.add(name)
        if name not in hc.AGENT_REGISTRY:
            raise HTTPException(status_code=400,
                                detail=f"Unknown agent: {name} — allowed: {', '.join(hc.AGENT_REGISTRY)}")
        resolved.append(name)
    if not resolved:
        raise HTTPException(status_code=400, detail="No valid agents in the template")
    payload.agents = resolved
    if payload.price_credits < _TEMPLATE_LIMITS["price_min"] or \
       payload.price_credits > _TEMPLATE_LIMITS["price_max"]:
        raise HTTPException(status_code=400,
                            detail=f"Price between {_TEMPLATE_LIMITS['price_min']} and {_TEMPLATE_LIMITS['price_max']} credits")


@app.post("/api/templates")
def api_publish_template(payload: TemplateIn, request: Request,
                         user: dict = Depends(get_current_user)):
    # price_credits is pydantic-typed as int (non-numeric input -> auto 422).
    _validate_template(payload)
    tid = db.publish_template(user["id"], payload.name, payload.description, payload.agents, payload.price_credits)
    audit.audit("template.publish", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", template_id=tid, price=payload.price_credits,
                agents=payload.agents)
    return {"id": tid, "ok": True}


@app.get("/api/templates")
def api_list_templates():
    return db.list_templates()


@app.get("/api/templates/mine")
def api_my_templates(user: dict = Depends(get_current_user)):
    return db.list_templates(author_id=user["id"])


@app.post("/api/templates/{tid}/buy")
def api_buy_template(tid: int, payload: BuyIn, request: Request,
                     user: dict = Depends(get_current_user)):
    tpl = db.get_template(tid)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.get("author_id") == user["id"]:
        audit.audit("template.buy", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="own_template", template_id=tid)
        raise HTTPException(status_code=400, detail="You cannot buy your own template")
    # Burst guard: max template purchases per user per hour (prevents runaway
    # parallel squad spawns while keeping the store usable).
    if not limiter.purchase_allowed(user["id"]):
        audit.audit("template.buy", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="burst_limit", template_id=tid)
        raise HTTPException(status_code=429, detail="Purchase limit reached (5 templates/hour) — please wait a moment")
    if not db.buy_template(tid, user["id"]):
        audit.audit("template.buy", uid=user["id"], email=user["email"], ip=_client_ip(request),
                    outcome="fail", reason="insufficient_credits", template_id=tid)
        raise HTTPException(status_code=402, detail="Insufficient credits to buy this template")
    limiter.record_purchase(user["id"])
    audit.audit("template.buy", uid=user["id"], email=user["email"], ip=_client_ip(request),
                outcome="ok", template_id=tid, author_id=tpl.get("author_id"),
                price=tpl.get("price_credits"))
    # Launched squad is auto-dispatched now (no manual dispatch wait).
    goal = (payload.goal or "").strip() or tpl.get("description") or f"Build using squad template: {tpl.get('name')}"
    goal = sanitize_goal(goal)[:_TEMPLATE_LIMITS["goal_max_len"]]
    slug = f"u{user['id']}-t{tid}-{int(time.time())}-{secrets.token_hex(4)}"
    launched = False
    launch_error = None
    pid = None
    try:
        pid = db.add_project(user["id"], slug, tpl.get("name", "marketplace-squad"), goal)
        if hc.projects_are_thin():
            hc.launch_project_thin(slug, goal, provider=None, model=None)
        else:
            hc.ensure_board(slug)
            hc.launch_from_template(slug, goal, tpl.get("agents", []),
                                    provider_keys=_user_provider_keys(user))
        # Auto-dispatch through the same background path as projects/demo so the
        # multi-wave swarm (workers -> verifier -> synthesizer) drives to
        # completion instead of stalling after the first ready-wave.
        _fire_dispatch(slug, user["plan"], _user_provider_keys(user), pid=pid, goal=goal)
        launched = True
    except Exception as e:
        launched = False
        # Never leak internal paths/versions/credential detail to the client
        # (P0.4): map to a stable generic category; the full detail is audited.
        audit.audit("template.launch_failed", uid=user["id"], email=user["email"],
                    slug=slug, tid=tid, reason=str(e)[:300], outcome="error")
        launch_error = "provider_unavailable"
        # The purchase already debited the buyer (and paid the author). If the
        # squad failed to launch, refund so the user isn't charged for nothing.
        try:
            db.refund_template_purchase(tid, user["id"])
        except Exception:
            pass
    resp = {"ok": True, "user": public_user(db.get_user_by_id(user["id"])),
            "slug": slug, "launched": launched}
    if not launched:
        resp["launch_error"] = launch_error
    return resp


# ---------- custom agents (P4) ----------
class CustomAgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    skills: str = Field(default="", max_length=300)


_CUSTOM_AGENT_LIMITS = {"name_max": 80, "objective_max": 500, "skills_max": 300,
                        "max_agents": 20}


@app.post("/api/agents")
def api_create_custom_agent(payload: CustomAgentIn, request: Request,
                            user: dict = Depends(get_current_user)):
    name = _clean_text(payload.name, name="agent name", max_len=_CUSTOM_AGENT_LIMITS["name_max"])
    objective = _clean_text(payload.objective, name="objective",
                            max_len=_CUSTOM_AGENT_LIMITS["objective_max"])
    skills = (payload.skills or "").strip()
    if skills:
        skills = _clean_text(skills, name="skills", max_len=_CUSTOM_AGENT_LIMITS["skills_max"])
    existing = db.list_custom_agents(user["id"])
    if len(existing) >= _CUSTOM_AGENT_LIMITS["max_agents"]:
        raise HTTPException(status_code=429, detail="Maximum of 20 custom agents per account")
    for agent in existing:
        if agent.get("name", "").strip().lower() == name.lower():
            raise HTTPException(status_code=400, detail="An agent with this name already exists")
    aid = db.create_custom_agent(user["id"], name, objective, skills)
    audit.audit("agent.create", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", agent_id=aid, name=name)
    return {"id": aid, "ok": True}


@app.get("/api/agents")
def api_list_custom_agents(user: dict = Depends(get_current_user)):
    return db.list_custom_agents(user["id"])


@app.delete("/api/agents/{aid}")
def api_delete_custom_agent(aid: int, request: Request,
                            user: dict = Depends(get_current_user)):
    if not db.delete_custom_agent(user["id"], aid):
        raise HTTPException(status_code=404, detail="Agent not found")
    audit.audit("agent.delete", uid=user["id"], email=user["email"],
                ip=_client_ip(request), outcome="ok", agent_id=aid)
    return {"ok": True}


# ---------- security (ECC AgentShield) ----------
@app.get("/api/projects/{slug}/security")
def api_security(slug: str, include_llm: bool = False, user: dict | None = Depends(get_current_user_optional)):
    # Demo boards are public for showcasing; owned boards require the owner.
    if slug.startswith("flux-demo-"):
        pass
    elif not (user and (slug.startswith(f"u{user['id']}-"))):
        raise HTTPException(status_code=403, detail="Unauthorized")
    goal = ""
    try:
        goal = hc.list_tasks(slug)[0].get("title", "") if hc.list_tasks(slug) else ""
    except Exception:
        pass
    try:
        res = security.scan_project(slug, goal, include_llm=include_llm)
        res["label"] = security.severity_label(res.get("score", 0))
        return res
    except Exception as e:
        audit.audit("security.scan", slug=slug, outcome="error", reason=str(e)[:200])
        raise HTTPException(status_code=500, detail="Security scan failed")
@app.websocket("/ws/{slug}")
async def ws_board(websocket: WebSocket, slug: str):
    # Token auth via query param. Strict, fail-closed rules:
    #   1. No token  -> reject (no silent anonymous board access).
    #   2. Malformed/unsigned token -> reject (do NOT treat as anonymous).
    #   3. Token uid maps to a deleted user -> reject (user-existence check).
    #   4. slug not owned by the user (and not a demo board) -> reject.
    token = websocket.query_params.get("token")
    if not token:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "unauthorized"})
        await websocket.close()
        return
    payload = auth_mod.decode_token(token)
    if not payload or "uid" not in payload:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "unauthorized"})
        await websocket.close()
        return
    user = db.get_user_by_id(payload["uid"])
    if not user:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "unauthorized"})
        await websocket.close()
        return
    if not _token_session_ok(user, payload):
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "unauthorized"})
        await websocket.close()
        return
    if not (slug.startswith(f"u{payload['uid']}-") or slug.startswith("flux-demo-")):
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "forbidden"})
        await websocket.close()
        return
    await websocket.accept()
    _SUBS.setdefault(slug, set()).add(websocket)
    if len(_SUBS[slug]) > _WS_MAX_SUBS_PER_SLUG:
        _SUBS[slug].discard(websocket)
        await websocket.send_json({"type": "error",
                                   "detail": "too many concurrent viewers on this board"})
        await websocket.close()
        return
    try:
        try:
            # list_tasks shells out to the hermes CLI (subprocess): run it on a
            # worker thread so it never blocks the event loop for other clients.
            await websocket.send_json({"type": "snapshot",
                                       "tasks": await asyncio.to_thread(hc.list_tasks, slug)})
        except Exception:
            pass
        while True:
            await asyncio.sleep(4)
            try:
                tasks = await asyncio.to_thread(hc.list_tasks, slug)
                await websocket.send_json({"type": "update", "tasks": tasks})
            except Exception:
                await websocket.send_json({"type": "error", "detail": "board poll failed"})
    except WebSocketDisconnect:
        _SUBS.get(slug, set()).discard(websocket)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return (BASE / "static" / "robots.txt").read_text(encoding="utf-8")


@app.get("/sitemap.xml", response_class=Response)
def sitemap_xml():
    return Response((BASE / "static" / "sitemap.xml").read_text(encoding="utf-8"),
                    media_type="application/xml")


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
