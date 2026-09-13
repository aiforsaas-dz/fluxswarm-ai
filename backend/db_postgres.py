"""FluxSwarm PostgreSQL persistence layer (asyncpg-backed).

Drop-in replacement for :mod:`db` (the legacy SQLite layer). It mirrors the
exact public API of ``db.py`` — same function names, same arguments, same return
shapes — so ``main.py`` can switch with ``import db_postgres as db`` and no other
change. Unlike SQLite's synchronous drivers, the heavy lifting here is async
(asyncpg pool), but the API surface exposed to callers stays synchronous by
draining coroutines on a dedicated background event loop, so FastAPI handlers do
NOT need to become ``await``-aware for the persistence layer.

Configuration (env):
    FLUXSWARM_DATABASE_URL   postgresql://user:pass@host:5432/dbname (required)
    FLUXSWARM_DB_POOL_MIN    pool min_size (default 5)
    FLUXSWARM_DB_POOL_MAX    pool max_size (default 20)
    FLUXSWARM_DB_TIMEOUT_S   per-statement command_timeout (default 60)

Schema is managed by Alembic (see ``alembic/``); ``init_db()`` runs
``alembic upgrade head`` against the configured URL so a fresh deployment just
needs ``FLUXSWARM_DATABASE_URL`` + ``init_db()``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

import asyncpg

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError  # noqa: F401

    _PH = PasswordHasher()
except ImportError:  # pragma: no cover - argon2-cffi is in requirements.txt

    class VerificationError(Exception):  # noqa: F401
        pass

    _PH = None

BASE = Path(__file__).resolve().parent

DATABASE_URL = os.environ.get("FLUXSWARM_DATABASE_URL", "")
DB_POOL_MIN = int(os.environ.get("FLUXSWARM_DB_POOL_MIN", "10"))
DB_POOL_MAX = int(os.environ.get("FLUXSWARM_DB_POOL_MAX", "50"))
DB_TIMEOUT_S = int(os.environ.get("FLUXSWARM_DB_TIMEOUT_S", "60"))
# Production sizing for the asyncpg pool: reuse connections up to 50k queries,
# retire connections idle >5 minutes so a dropped DB link is noticed quickly.
DB_POOL_MAX_QUERIES = int(os.environ.get("FLUXSWARM_DB_POOL_MAX_QUERIES", "50000"))
DB_POOL_IDLE_LIFETIME_S = float(os.environ.get("FLUXSWARM_DB_POOL_IDLE_LIFETIME_S", "300.0"))

# Plan catalogue — one-time credit packs, not subscriptions. Must stay in sync
# with PLANS in db.py. "topup" is a pure credit refill (never changes plan tier).
PLANS = {
    "demo": {"name": "Demo", "price": 0, "credits": 5, "parallel": 1, "desc": "Free trial — no card required"},
    "starter": {"name": "Starter", "price": 19, "credits": 20, "parallel": 2, "desc": "For freelancers and small projects"},
    "pro": {"name": "Pro", "price": 49, "credits": 60, "parallel": 4, "desc": "For small teams"},
    "scale": {"name": "Scale", "price": 149, "credits": 200, "parallel": 6, "desc": "For companies and agencies"},
    "topup": {"name": "Top-up", "price": 9, "credits": 10, "parallel": 2, "desc": "Quick refill — credits never expire"},
}
PLAN_ORDER = ["demo", "starter", "pro", "scale", "topup"]

REFERRAL_REWARD_CREDITS = 15
FRIEND_BONUS_CREDITS = 10
REFERRAL_REWARD_CAP = 500

TELEGRAM_LINK_TTL = 600
SESSION_MIN_TIME = 0
RESET_TTL = 900

_POOL: asyncpg.Pool | None = None
_LOOP: asyncio.AbstractEventLoop | None = None
_STARTED = False
_POOL_LOCK: asyncio.Lock | None = None


def get_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "FLUXSWARM_DATABASE_URL is not set. PostgreSQL persistence requires "
            "it (see .env.example); set it before importing db_postgres."
        )
    return DATABASE_URL


async def _codecs(conn: asyncpg.Connection) -> None:
    """Round-trip JSON columns as native Python values on every pooled connection."""
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _loop() -> "asyncio.AbstractEventLoop":
    """Return (starting once) the background loop that drains asyncpg coroutines."""
    global _LOOP, _STARTED
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        t = threading.Thread(target=_LOOP.run_forever, name="db_postgres_loop", daemon=True)
        t.start()
        _STARTED = True
    return _LOOP


def _await(coro) -> object:
    """Run an asyncpg coroutine on the background loop and return its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop())
    return future.result()


async def get_pool() -> asyncpg.Pool:
    """Build (lazily) the shared asyncpg pool with production sizing."""
    global _POOL, _POOL_LOCK
    if _POOL is None:
        if _POOL_LOCK is None or _POOL_LOCK._loop is not _loop():
            _POOL_LOCK = asyncio.Lock()
        async with _POOL_LOCK:
            if _POOL is None:
                _POOL = await asyncpg.create_pool(
                    dsn=get_database_url(),
                    min_size=DB_POOL_MIN,
                    max_size=DB_POOL_MAX,
                    command_timeout=DB_TIMEOUT_S,
                    max_queries=DB_POOL_MAX_QUERIES,
                    max_inactive_connection_lifetime=DB_POOL_IDLE_LIFETIME_S,
                    init=_codecs,
                )
    return _POOL


async def close_pool() -> None:
    """Close the pool (used by tests/shutdown hooks)."""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


async def _truncate_all() -> None:
    """Reset all tables (test helper): TRUNCATE RESTART IDENTITY CASCADE."""
    pool = await get_pool()
    await pool.execute(
        "TRUNCATE TABLE users, projects, referrals, squad_templates, "
        "template_purchases, payment_events, telegram_links, telegram_codes, "
        "password_resets, demo_usage, provider_agreements, admt_disclosures, "
        "provider_usage, board_states, board_tasks, board_task_events, board_files "
        "RESTART IDENTITY CASCADE"
    )


def reset_db() -> None:
    """Drop all data (test fixture helper)."""
    _await(_truncate_all())


# ---------- password hashing (mirrors db.py exactly) ----------

def _hash_legacy(pw: str, salt: str) -> str:
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()


def _make_pw_hash(password: str) -> str:
    if _PH:
        return _PH.hash(password)
    salt = secrets.token_hex(8)
    return f"{salt}${_hash_legacy(password, salt)}"


def _is_legacy(stored: str) -> bool:
    return bool(stored) and not stored.startswith("$argon2") and stored.count("$") == 1


def _verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("$argon2"):
        if not _PH:
            return False
        try:
            return _PH.verify(stored, password)
        except Exception:
            return False
    if _is_legacy(stored):
        salt, h = stored.split("$", 1)
        return _hash_legacy(password, salt) == h
    return False


def _hash_password(password: str) -> str:
    return _make_pw_hash(password)


async def _update_user_password(email: str, password: str) -> None:
    email = email.lower().strip()
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET pw_hash=$1 WHERE email=$2",
        _hash_password(password),
        email,
    )


def _needs_rehash(stored: str) -> bool:
    if not stored.startswith("$argon2"):
        return True
    try:
        return bool(_PH and _PH.check_needs_rehash(stored))
    except Exception:
        return True


def make_ref_code() -> str:
    return "FLX-" + secrets.token_hex(4).upper()


def _rows(record) -> dict | None:
    return dict(record) if record else None


# ---------- migration / init ----------

def _migrate_sync() -> None:
    """Apply all Alembic migrations to the configured database.

    Runs synchronously on a dedicated thread: Alembic's env.py drives migrations
    through ``asyncio.run`` internally, so it must not execute inside a coroutine
    that is already running on the background loop (that would raise
    ``RuntimeError: cannot call asyncio.run() from a running event loop``).
    """
    from alembic import command
    from alembic.config import Config

    errors = []

    def _run():
        try:
            cfg = Config(str(BASE / "alembic.ini"))
            cfg.set_main_option("script_location", str(BASE / "alembic"))
            cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
            os.environ["FLUXSWARM_DATABASE_URL"] = DATABASE_URL  # env.py reads it
            command.upgrade(cfg, "head")
        except BaseException as e:  # noqa: BLE001 - re-raised on caller thread
            errors.append(e)

    t = threading.Thread(target=_run, name="fluxswarm_alembic", daemon=True)
    t.start()
    t.join()
    if errors:
        raise errors[0]


def init_db() -> None:
    """Run Alembic migrations (idempotent) — mirror of db.init_db()."""
    _migrate_sync()


async def _seed_demo() -> None:
    pool = await get_pool()
    exists = await pool.fetchval(
        "SELECT 1 FROM users WHERE email=$1", "demo@fluxswarm.ai"
    )
    pw = os.environ.get("FLUXSWARM_DEMO_PASSWORD")
    if not exists:
        # Never a public constant: operator override or a fresh random token.
        pw = pw or secrets.token_urlsafe(18)
        await _create_user("demo@fluxswarm.ai", "Demo User", pw, None)
    elif pw:
        # Deterministic test sandboxes pin the demo password via env; re-pin the
        # account so a prior random seed cannot strand logins.
        current = await pool.fetchval(
            "SELECT pw_hash FROM users WHERE email=$1", "demo@fluxswarm.ai"
        )
        if current and current != _hash_password(pw):
            await _update_user_password("demo@fluxswarm.ai", pw)


def seed_demo() -> None:
    _await(_seed_demo())


# ---------- users ----------

async def _create_user(email: str, name: str, password: str, ref_code: str | None,
                       tos_accepted_at: float | None = None) -> dict:
    email = email.lower().strip()
    pool = await get_pool()
    try:
        uid = await pool.fetchval(
            "INSERT INTO users (email,name,pw_hash,plan,credits,ref_code,referred_by,created_at,tos_accepted_at) "
            "VALUES ($1,$2,$3,'demo',$4,$5,$6,$7,$8) RETURNING id",
            email,
            name,
            _make_pw_hash(password),
            PLANS["demo"]["credits"],
            make_ref_code(),
            ref_code,
            int(time.time()),
            int(tos_accepted_at) if tos_accepted_at else None,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError("This email is already registered")
    if ref_code:
        await pool.execute(
            "INSERT INTO referrals (referrer_code,referred_email,rewarded,created_at) "
            "VALUES ($1,$2,0,$3) ON CONFLICT DO NOTHING",
            ref_code,
            email,
            int(time.time()),
        )
    user = await _get_user_by_id(uid)
    return user


async def _update_user_name(user_id: int, name: str) -> bool:
    pool = await get_pool()
    res = await pool.execute(
        "UPDATE users SET name=$1 WHERE id=$2",
        name.strip(),
        user_id,
    )
    return res.endswith(" 1")


def update_user_name(user_id: int, name: str) -> bool:
    return _await(_update_user_name(user_id, name))


def create_user(email: str, name: str, password: str, ref_code: str | None = None,
                tos_accepted_at: float | None = None) -> dict:
    return _await(_create_user(email, name, password, ref_code, tos_accepted_at))


async def _authenticate(email: str, password: str) -> dict | None:
    email = email.lower().strip()
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not row:
        return None
    stored = row["pw_hash"]
    if not _verify_password(password, stored):
        return None
    user = _rows(row)
    if _needs_rehash(stored):
        await pool.execute(
            "UPDATE users SET pw_hash=$1 WHERE id=$2",
            _make_pw_hash(password),
            user["id"],
        )
        user["pw_hash"] = _rows(await pool.fetchrow(
            "SELECT pw_hash FROM users WHERE id=$1", user["id"]))["pw_hash"]
    return user


def authenticate(email: str, password: str) -> dict | None:
    return _await(_authenticate(email, password))


async def _get_user_by_id(uid: int) -> dict | None:
    pool = await get_pool()
    return _rows(await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid))


def get_user_by_id(uid: int) -> dict | None:
    return _await(_get_user_by_id(uid))


async def _get_user_by_email(email: str) -> dict | None:
    pool = await get_pool()
    return _rows(await pool.fetchrow("SELECT * FROM users WHERE email=$1", email.strip().lower()))


def get_user_by_email(email: str) -> dict | None:
    return _await(_get_user_by_email(email))


async def _get_user_by_ref(ref_code: str) -> dict | None:
    pool = await get_pool()
    return _rows(await pool.fetchrow("SELECT * FROM users WHERE ref_code=$1", ref_code))


def get_user_by_ref(ref_code: str) -> dict | None:
    return _await(_get_user_by_ref(ref_code))


# ---------- projects ----------

async def _add_project(user_id: int, board_slug: str, name: str, goal: str) -> int:
    pool = await get_pool()
    try:
        return await pool.fetchval(
            "INSERT INTO projects (user_id,board_slug,name,goal,created_at) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            user_id, board_slug, name, goal, int(time.time()),
        )
    except asyncpg.UniqueViolationError:
        raise ValueError("board_slug_exists")


def add_project(user_id: int, board_slug: str, name: str, goal: str) -> int:
    return _await(_add_project(user_id, board_slug, name, goal))


async def _set_launch_outcome(pid: int, status: str, outcome: str, reason: str = "", refunded: bool = False) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE projects SET launch_status=$1, launch_outcome=$2, launch_reason=$3, "
        "launch_refunded=$4, launch_updated_at=$5 WHERE id=$6",
        status, outcome, reason, (1 if refunded else 0), int(time.time()), pid,
    )


def set_launch_outcome(pid: int, status: str, outcome: str, reason: str = "", refunded: bool = False) -> None:
    return _await(_set_launch_outcome(pid, status, outcome, reason, refunded))


async def _refund_launch_credit(user_id: int) -> bool:
    pool = await get_pool()
    return await pool.fetchval(
        "UPDATE users SET credits = credits + 1 WHERE id=$1 RETURNING id", user_id
    ) is not None


def refund_launch_credit(user_id: int) -> bool:
    return _await(_refund_launch_credit(user_id))


async def _bump_demo_usage(who: str, day: str) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        "INSERT INTO demo_usage (who,day,count) VALUES ($1,$2,1) "
        "ON CONFLICT (who,day) DO UPDATE SET count = demo_usage.count + 1 "
        "RETURNING count",
        who, day,
    )


def bump_demo_usage(who: str, day: str) -> int:
    return _await(_bump_demo_usage(who, day))


async def _record_provider_usage(surface: str, runtime_source: str,
                                 provider: str, model: str, *,
                                 ok: int | None = None,
                                 runtime_s: int | None = None,
                                 tasks: int | None = None,
                                 slug: str | None = None) -> int:
    """Append one provider-usage ledger row (Phase F observability).

    ``surface`` = "demo" | "project"; ``runtime_source`` = "pool" |
    "paid_fallback" | "byok" | "default". ``ok`` stays NULL until the launch
    finalizes. Observability only — never gates spend.
    """
    pool = await get_pool()
    rid = await pool.fetchval(
        "INSERT INTO provider_usage(created_at, day, surface, runtime_source, "
        "provider, model, ok, runtime_s, tasks, slug) "
        "VALUES(EXTRACT(EPOCH FROM now())::bigint, "
        "to_char(now(), 'YYYY-MM-DD'), $1, $2, $3, $4, $5, $6, $7, $8) "
        "RETURNING id",
        surface, runtime_source, provider, model, ok, runtime_s, tasks, slug,
    )
    return int(rid)


def record_provider_usage(surface: str, runtime_source: str, provider: str,
                          model: str, *, ok: int | None = None,
                          runtime_s: int | None = None, tasks: int | None = None,
                          slug: str | None = None) -> int:
    return _await(_record_provider_usage(
        surface, runtime_source, provider, model, ok=ok,
        runtime_s=runtime_s, tasks=tasks, slug=slug))


async def _update_provider_usage_outcome(slug: str, *, ok: int | None = None,
                                         runtime_s: int | None = None,
                                         tasks: int | None = None) -> bool:
    """Fill terminal outcome on the latest open attempt for *slug*."""
    pool = await get_pool()
    val = await pool.fetchval(
        "UPDATE provider_usage SET ok = COALESCE($2, ok), "
        "runtime_s = COALESCE($3, runtime_s), tasks = COALESCE($4, tasks) "
        "WHERE id = (SELECT id FROM provider_usage WHERE slug = $1 AND ok IS NULL "
        "ORDER BY id DESC LIMIT 1) RETURNING id",
        slug, ok, runtime_s, tasks,
    )
    return val is not None


def update_provider_usage_outcome(slug: str, *, ok: int | None = None,
                                  runtime_s: int | None = None,
                                  tasks: int | None = None) -> bool:
    return _await(_update_provider_usage_outcome(
        slug, ok=ok, runtime_s=runtime_s, tasks=tasks))


async def _provider_usage_summary(day: str | None = None) -> dict:
    """Per-day provider-usage totals (attempts/final outcomes) for /health."""
    pool = await get_pool()
    day = day or (await pool.fetchval("SELECT to_char(now(), 'YYYY-MM-DD')"))
    rows = await pool.fetch(
        "SELECT surface, runtime_source, ok, COUNT(*) AS n FROM provider_usage "
        "WHERE day=$1 GROUP BY surface, runtime_source, ok", day,
    )
    total = finalized = ok_count = not_ok = 0
    by_source: dict[str, dict] = {}
    by_surface: dict[str, dict] = {}
    for r in rows:
        src, surf, okf, n = r["runtime_source"], r["surface"], r["ok"], r["n"]
        total += n
        bs = by_source.setdefault(src, {"attempts": 0, "ok": 0})
        bs["attempts"] += n
        bb = by_surface.setdefault(surf, {"attempts": 0, "ok": 0})
        bb["attempts"] += n
        if okf is not None:
            finalized += n
            if okf:
                ok_count += n
                bs["ok"] += n
                bb["ok"] += n
            else:
                not_ok += n
    return {
        "day": day,
        "total_attempts": total,
        "finalized": finalized,
        "ok": ok_count,
        "not_ok": not_ok,
        "pending_attempts": total - finalized,
        "by_runtime_source": by_source,
        "by_surface": by_surface,
    }


def provider_usage_summary(day: str | None = None) -> dict:
    return _await(_provider_usage_summary(day))


async def _list_user_projects(user_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM projects WHERE user_id=$1 ORDER BY created_at DESC, id DESC", user_id
    )
    return [dict(r) for r in rows]


def list_user_projects(user_id: int) -> list[dict]:
    return _await(_list_user_projects(user_id))


async def _deduct_credit(user_id: int) -> bool:
    """Atomic race-free debit: the guard is in the row, not the app."""
    pool = await get_pool()
    return await pool.fetchval(
        "UPDATE users SET credits = credits - 1 WHERE id=$1 AND credits > 0 "
        "RETURNING id",
        user_id,
    ) is not None


def deduct_credit(user_id: int) -> bool:
    return _await(_deduct_credit(user_id))


async def _reward_referrer_once(referred_email: str) -> bool:
    """Reward referrer once per referred email (race-free via SKIP LOCKED), cap
    referrer earnings at REFERRAL_REWARD_CAP, and grant the referred friend their
    one-time welcome bonus — all inside the same transaction."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, referrer_code FROM referrals "
                "WHERE referred_email=$1 AND rewarded=0 ORDER BY id LIMIT 1 "
                "FOR UPDATE SKIP LOCKED",
                referred_email,
            )
            if not row:
                return False
            ref_user = await conn.fetchval(
                "SELECT id FROM users WHERE ref_code=$1", row["referrer_code"]
            )
            if ref_user:
                held = await conn.fetchval(
                    "SELECT credits FROM users WHERE id=$1", ref_user
                ) or 0
                grant = min(REFERRAL_REWARD_CREDITS,
                            max(0, REFERRAL_REWARD_CAP - held))
                if grant:
                    await conn.execute(
                        "UPDATE users SET credits = credits + $1 WHERE id=$2",
                        grant, ref_user,
                    )
            friend = await conn.fetchval(
                "SELECT id FROM users WHERE email=$1", referred_email
            )
            if friend:
                await conn.execute(
                    "UPDATE users SET credits = credits + $1 WHERE id=$2",
                    FRIEND_BONUS_CREDITS, friend,
                )
            claimed = await conn.execute(
                "UPDATE referrals SET rewarded=1 WHERE id=$1 AND rewarded=0", row["id"]
            )
            if claimed == "UPDATE 0":
                return False
            return bool(ref_user)


def add_credits(user_id: int, credits: int) -> bool:
    """Top-up a balance without touching the plan tier (topup pack)."""
    try:
        return _await(_add_credits(user_id, credits))
    except Exception:
        return False


async def _add_credits(user_id: int, credits: int) -> bool:
    pool = await get_pool()
    res = await pool.fetchval(
        "UPDATE users SET credits = credits + $1 WHERE id=$2 RETURNING id",
        credits, user_id,
    )
    return res is not None


def reward_referrer_once(referred_email: str) -> bool:
    return _await(_reward_referrer_once(referred_email))


async def _upgrade_plan(user_id: int, plan: str) -> None:
    if plan not in PLANS:
        raise ValueError("Invalid plan")
    pool = await get_pool()
    current = await pool.fetchval("SELECT credits FROM users WHERE id=$1", user_id) or 0
    new_credits = max(current, PLANS[plan]["credits"])
    await pool.execute(
        "UPDATE users SET plan=$1, credits=$2 WHERE id=$3", plan, new_credits, user_id
    )


def upgrade_plan(user_id: int, plan: str) -> None:
    return _await(_upgrade_plan(user_id, plan))


async def _downgrade_subscription(user_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET plan='demo' WHERE id=$1 AND plan != 'demo'", user_id
    )


def downgrade_subscription(user_id: int) -> None:
    return _await(_downgrade_subscription(user_id))


async def _record_payment_event(event_id: str, gateway: str, kind: str, user_id: int, detail: dict) -> bool:
    pool = await get_pool()
    return await pool.fetchval(
        "INSERT INTO payment_events (event_id,gateway,kind,user_id,detail,created_at) "
        "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (event_id) DO NOTHING RETURNING id",
        event_id, gateway, kind, user_id, detail, int(time.time()),
    ) is not None


def record_payment_event(event_id: str, gateway: str, kind: str, user_id: int, detail: dict) -> bool:
    return _await(_record_payment_event(event_id, gateway, kind, user_id, detail))


async def _payment_user_by_txn(txn_id: str) -> int | None:
    if not txn_id:
        return None
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT user_id FROM payment_events WHERE kind='payment.succeeded' "
        "AND detail->>'txn' = $1 ORDER BY id DESC LIMIT 1",
        txn_id,
    )


def payment_user_by_txn(txn_id: str) -> int | None:
    return _await(_payment_user_by_txn(txn_id))


# ---------- Telegram account linking ----------

async def _new_telegram_link_code(user_id: int, ttl: int = TELEGRAM_LINK_TTL) -> str:
    pool = await get_pool()
    now = int(time.time())
    await pool.execute(
        "DELETE FROM telegram_codes WHERE used=1 OR expires_at < $1", now
    )
    existing = await pool.fetchval(
        "SELECT code FROM telegram_codes WHERE user_id=$1 AND used=0 AND expires_at >= $2 "
        "ORDER BY created_at DESC LIMIT 1",
        user_id, now,
    )
    if existing:
        return existing
    for _ in range(8):
        code = secrets.token_hex(3).upper()
        try:
            await pool.execute(
                "INSERT INTO telegram_codes (code,user_id,expires_at,used,created_at) "
                "VALUES ($1,$2,$3,0,$4)",
                code, user_id, now + ttl, now,
            )
            return code
        except asyncpg.UniqueViolationError:
            continue
    raise ValueError("could_not_generate_code")


def new_telegram_link_code(user_id: int, ttl: int = TELEGRAM_LINK_TTL) -> str:
    return _await(_new_telegram_link_code(user_id, ttl))


async def _consume_telegram_link_code(code: str, telegram_chat_id: int) -> dict | None:
    pool = await get_pool()
    now = int(time.time())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM telegram_codes WHERE used=1 OR expires_at < $1", now)
            row = await conn.fetchrow(
                "SELECT * FROM telegram_codes WHERE code=$1",
                str(code).strip().upper(),
            )
            if not row or row["used"] or row["expires_at"] < now:
                return None
            uid = row["user_id"]
            await conn.execute(
                "DELETE FROM telegram_links WHERE telegram_chat_id=$1 OR user_id=$2",
                telegram_chat_id, uid,
            )
            await conn.execute(
                "INSERT INTO telegram_links (telegram_chat_id,user_id,linked_at) VALUES ($1,$2,$3)",
                telegram_chat_id, uid, now,
            )
            await conn.execute(
                "UPDATE telegram_codes SET used=1 WHERE code=$1 AND used=0", row["code"]
            )
            user = await conn.fetchrow("SELECT id, name, plan FROM users WHERE id=$1", uid)
            return _rows(user)


def consume_telegram_link_code(code: str, telegram_chat_id: int) -> dict | None:
    return _await(_consume_telegram_link_code(code, telegram_chat_id))


async def _get_user_by_telegram_chat(telegram_chat_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT u.* FROM telegram_links t JOIN users u ON u.id=t.user_id "
        "WHERE t.telegram_chat_id=$1",
        telegram_chat_id,
    )
    return _rows(row)


def get_user_by_telegram_chat(telegram_chat_id: int) -> dict | None:
    return _await(_get_user_by_telegram_chat(telegram_chat_id))


async def _get_telegram_link(user_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT telegram_chat_id, linked_at FROM telegram_links WHERE user_id=$1", user_id
    )
    return _rows(row)


def get_telegram_link(user_id: int) -> dict | None:
    return _await(_get_telegram_link(user_id))


async def _unlink_telegram(user_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchval(
        "DELETE FROM telegram_links WHERE user_id=$1 RETURNING telegram_chat_id",
        user_id,
    )
    return row is not None


def unlink_telegram(user_id: int) -> bool:
    return _await(_unlink_telegram(user_id))


# ---------- CCPA / account rights ----------

async def _account_payload(user_id: int) -> dict:
    pool = await get_pool()
    u = await pool.fetchrow(
        "SELECT id,email,name,plan,credits,ref_code,referred_by,created_at "
        "FROM users WHERE id=$1",
        user_id,
    )
    if not u:
        raise ValueError("user_not_found")
    user = dict(u)
    projects = [dict(r) for r in await pool.fetch(
        "SELECT id,board_slug,name,goal,created_at FROM projects WHERE user_id=$1", user_id)]
    refs = [dict(r) for r in await pool.fetch(
        "SELECT referrer_code,referred_email,rewarded,created_at FROM referrals "
        "WHERE referrer_code=$1",
        user["ref_code"])]
    tpls = [dict(r) for r in await pool.fetch(
        "SELECT id,name,price_credits,created_at FROM squad_templates WHERE author_id=$1", user_id)]
    buys = [dict(r) for r in await pool.fetch(
        "SELECT template_id,created_at FROM template_purchases WHERE buyer_id=$1", user_id)]
    pays = [dict(r) for r in await pool.fetch(
        "SELECT id,gateway,kind,created_at FROM payment_events WHERE user_id=$1", user_id)]
    tg = [dict(r) for r in await pool.fetch(
        "SELECT telegram_chat_id,linked_at FROM telegram_links WHERE user_id=$1", user_id)]
    agrees = [dict(r) for r in await pool.fetch(
        "SELECT provider,agreed_at,version FROM provider_agreements WHERE user_id=$1", user_id)]
    return {"user": user, "projects": projects, "referrals": refs,
            "templates": tpls, "template_purchases": buys, "payment_events": pays,
            "telegram_links": tg, "provider_agreements": agrees}


def account_payload(user_id: int) -> dict:
    return _await(_account_payload(user_id))


async def _delete_user(user_id: int) -> bool:
    pool = await get_pool()
    u = await pool.fetchrow("SELECT email,ref_code FROM users WHERE id=$1", user_id)
    if not u:
        return False
    for sql in (
        "DELETE FROM projects WHERE user_id=$1",
        "DELETE FROM squad_templates WHERE author_id=$1",
        "DELETE FROM template_purchases WHERE buyer_id=$1",
        "DELETE FROM referrals WHERE referrer_code=$1",
        "DELETE FROM payment_events WHERE user_id=$1",
        "DELETE FROM telegram_links WHERE user_id=$1",
        "DELETE FROM telegram_codes WHERE user_id=$1",
        "DELETE FROM password_resets WHERE user_id=$1",
        "DELETE FROM provider_agreements WHERE user_id=$1",
        "DELETE FROM admt_disclosures WHERE user_id=$1",
        "DELETE FROM users WHERE id=$1",
    ):
        try:
            await pool.execute(sql, user_id)
        except asyncpg.PostgresError:
            pass
    return True


def delete_user(user_id: int) -> bool:
    return _await(_delete_user(user_id))


# ---------- provider agreements (Phase 3 BYOK gate) ----------

async def _agree_provider(user_id: int, provider: str, version: str) -> bool:
    pool = await get_pool()
    now = int(time.time())
    existing = await pool.fetchval(
        "SELECT agreed_at FROM provider_agreements WHERE user_id=$1 AND provider=$2",
        user_id, provider,
    )
    await pool.execute(
        "INSERT INTO provider_agreements (user_id,provider,agreed_at,version) "
        "VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (user_id,provider) DO UPDATE SET agreed_at=EXCLUDED.agreed_at, "
        "version=EXCLUDED.version",
        user_id, provider, now, version,
    )
    return existing is None


def agree_provider(user_id: int, provider: str, version: str = "1.0") -> bool:
    return _await(_agree_provider(user_id, provider, version))


async def _provider_agreements(user_id: int) -> dict[str, dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT provider, agreed_at, version FROM provider_agreements WHERE user_id=$1",
        user_id,
    )
    return {r["provider"]: {"agreed_at": r["agreed_at"], "version": r["version"]} for r in rows}


def provider_agreements(user_id: int) -> dict[str, dict]:
    return _await(_provider_agreements(user_id))


async def _has_provider_agreement(user_id: int, provider: str) -> bool:
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT 1 FROM provider_agreements WHERE user_id=$1 AND provider=$2",
        user_id, provider,
    ) is not None


def has_provider_agreement(user_id: int, provider: str) -> bool:
    return _await(_has_provider_agreement(user_id, provider))


async def _provider_agreements_summary() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT pa.provider, pa.agreed_at, pa.version, u.email "
        "FROM provider_agreements pa JOIN users u ON u.id = pa.user_id "
        "ORDER BY pa.agreed_at DESC"
    )
    return [dict(r) for r in rows]


def provider_agreements_summary() -> list[dict]:
    return _await(_provider_agreements_summary())


# ---------- CCPA/CPRA ADMT (Phase 5) ----------

async def _record_admt_notice_ack(user_id: int) -> int:
    pool = await get_pool()
    now = int(time.time())
    return await pool.fetchval(
        "INSERT INTO admt_disclosures (user_id,disclosed_at,acknowledged_at,admt_type) "
        "VALUES ($1,$2,$3,'pre-use-notice') RETURNING id",
        user_id, now, now,
    )


def record_admt_notice_ack(user_id: int) -> int:
    return _await(_record_admt_notice_ack(user_id))


async def _has_admt_notice_ack(user_id: int) -> bool:
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT 1 FROM admt_disclosures WHERE user_id=$1 AND admt_type='pre-use-notice' "
        "AND acknowledged_at IS NOT NULL LIMIT 1",
        user_id,
    ) is not None


def has_admt_notice_ack(user_id: int) -> bool:
    return _await(_has_admt_notice_ack(user_id))


async def _get_admt_opt_out(user_id: int) -> bool:
    pool = await get_pool()
    val = await pool.fetchval("SELECT admt_opt_out FROM users WHERE id=$1", user_id)
    return bool(val)


def get_admt_opt_out(user_id: int) -> bool:
    return _await(_get_admt_opt_out(user_id))


async def _set_admt_opt_out(user_id: int, value: bool) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET admt_opt_out=$1 WHERE id=$2", 1 if value else 0, user_id
    )


def set_admt_opt_out(user_id: int, value: bool) -> None:
    return _await(_set_admt_opt_out(user_id, value))


async def _request_human_review(user_id: int, project_id: int) -> int:
    pool = await get_pool()
    now = int(time.time())
    return await pool.fetchval(
        "INSERT INTO admt_disclosures (user_id,project_id,disclosed_at,admt_type,"
        "human_review_status,requested_at) VALUES ($1,$2,$3,'human-review-request',"
        "'requested',$4) RETURNING id",
        user_id, project_id, now, now,
    )


def request_human_review(user_id: int, project_id: int) -> int:
    return _await(_request_human_review(user_id, project_id))


async def _get_human_review(review_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT d.*, u.email AS user_email, u.name AS user_name, "
        "p.name AS project_name, p.board_slug "
        "FROM admt_disclosures d JOIN users u ON u.id=d.user_id "
        "LEFT JOIN projects p ON p.id=d.project_id WHERE d.id=$1",
        review_id,
    )
    return _rows(row)


def get_human_review(review_id: int) -> dict | None:
    return _await(_get_human_review(review_id))


async def _list_human_review_queue(limit: int = 100) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT d.*, u.email AS user_email, u.name AS user_name, "
        "p.name AS project_name, p.board_slug, p.goal "
        "FROM admt_disclosures d JOIN users u ON u.id=d.user_id "
        "LEFT JOIN projects p ON p.id=d.project_id "
        "WHERE d.human_review_status='requested' ORDER BY d.requested_at ASC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


def list_human_review_queue(limit: int = 100) -> list[dict]:
    return _await(_list_human_review_queue(limit))


async def _update_human_review(review_id: int, status: str, notes: str = "") -> bool:
    pool = await get_pool()
    res = await pool.execute(
        "UPDATE admt_disclosures SET human_review_status=$1, reviewer_notes=$2, "
        "reviewed_at=$3 WHERE id=$4 AND human_review_status='requested'",
        status, notes, int(time.time()), review_id,
    )
    return res.startswith("UPDATE") and res.endswith(" 1")


def update_human_review(review_id: int, status: str, notes: str = "") -> bool:
    return _await(_update_human_review(review_id, status, notes))


# ---------- session invalidation / password lifecycle ----------

async def _mark_logged_out(user_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET logged_out_at=$1 WHERE id=$2", int(time.time()), user_id
    )


def mark_logged_out(user_id: int) -> None:
    return _await(_mark_logged_out(user_id))


async def _get_logged_out_at(user_id: int) -> float:
    pool = await get_pool()
    val = await pool.fetchval("SELECT logged_out_at FROM users WHERE id=$1", user_id)
    return float(val or 0) if val else 0.0


def get_logged_out_at(user_id: int) -> float:
    return _await(_get_logged_out_at(user_id))


async def _set_password(user_id: int, new_password: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET pw_hash=$1 WHERE id=$2", _make_pw_hash(new_password), user_id
    )
    await _mark_logged_out(user_id)


def set_password(user_id: int, new_password: str) -> None:
    return _await(_set_password(user_id, new_password))


async def _create_password_reset(user_id: int, ttl: int = RESET_TTL) -> str:
    raw = secrets.token_urlsafe(32)
    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    pool = await get_pool()
    now = int(time.time())
    await pool.execute("DELETE FROM password_resets WHERE used=1 OR expires_at < $1", now)
    await pool.execute(
        "INSERT INTO password_resets (token_hash,user_id,expires_at,used,created_at) "
        "VALUES ($1,$2,$3,0,$4)",
        th, user_id, now + ttl, now,
    )
    return raw


def create_password_reset(user_id: int, ttl: int = RESET_TTL) -> str:
    return _await(_create_password_reset(user_id, ttl))


async def _consume_password_reset(raw: str) -> int | None:
    if not raw:
        return None
    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    pool = await get_pool()
    now = int(time.time())
    await pool.execute("DELETE FROM password_resets WHERE used=1 OR expires_at < $1", now)
    row = await pool.fetchrow(
        "SELECT user_id FROM password_resets WHERE token_hash=$1 AND used=0 AND expires_at >= $2",
        th, now,
    )
    if not row:
        return None
    await pool.execute(
        "UPDATE password_resets SET used=1 WHERE token_hash=$1 AND used=0", th
    )
    return row["user_id"]


def consume_password_reset(raw: str) -> int | None:
    return _await(_consume_password_reset(raw))


# ---------- squad marketplace ----------

async def _publish_template(author_id: int, name: str, description: str, agents: list[str], price_credits: int = 10) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        "INSERT INTO squad_templates (author_id,name,description,agents,price_credits,created_at) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        author_id, name, description, agents, price_credits, int(time.time()),
    )


def publish_template(author_id: int, name: str, description: str, agents: list[str], price_credits: int = 10) -> int:
    return _await(_publish_template(author_id, name, description, agents, price_credits))


async def _list_templates(author_id: int | None = None) -> list[dict]:
    pool = await get_pool()
    if author_id:
        rows = await pool.fetch(
            "SELECT * FROM squad_templates WHERE author_id=$1 ORDER BY created_at DESC", author_id
        )
    else:
        rows = await pool.fetch("SELECT * FROM squad_templates ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def list_templates(author_id: int | None = None) -> list[dict]:
    return _await(_list_templates(author_id))


async def _get_template(tid: int) -> dict | None:
    pool = await get_pool()
    return _rows(await pool.fetchrow("SELECT * FROM squad_templates WHERE id=$1", tid))


def get_template(tid: int) -> dict | None:
    return _await(_get_template(tid))


async def _buy_template(tid: int, buyer_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM squad_templates WHERE id=$1 FOR UPDATE", tid
            )
            if not row:
                return False
            price = row["price_credits"]
            bal = await conn.fetchrow(
                "SELECT credits FROM users WHERE id=$1 FOR UPDATE", buyer_id
            )
            if not bal or bal["credits"] < price:
                return False
            await conn.execute("UPDATE users SET credits = credits - $1 WHERE id=$2", price, buyer_id)
            author_earn = max(1, price // 2)
            await conn.execute("UPDATE users SET credits = credits + $1 WHERE id=$2", author_earn, row["author_id"])
            await conn.execute(
                "INSERT INTO template_purchases (template_id,buyer_id,created_at) VALUES ($1,$2,$3)",
                tid, buyer_id, int(time.time()),
            )
            return True


def buy_template(tid: int, buyer_id: int) -> bool:
    return _await(_buy_template(tid, buyer_id))


async def _refund_template_purchase(tid: int, buyer_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM template_purchases WHERE template_id=$1 AND buyer_id=$2 "
                "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                tid, buyer_id,
            )
            if not row:
                return False
            tpl = await conn.fetchrow(
                "SELECT price_credits, author_id FROM squad_templates WHERE id=$1 FOR UPDATE", tid
            )
            if not tpl:
                return False
            price = tpl["price_credits"]
            await conn.execute("UPDATE users SET credits = credits + $1 WHERE id=$2", price, buyer_id)
            author_earn = max(1, price // 2)
            await conn.execute("UPDATE users SET credits = credits - $1 WHERE id=$2", author_earn, tpl["author_id"])
            await conn.execute("DELETE FROM template_purchases WHERE id=$1", row["id"])
            return True


def refund_template_purchase(tid: int, buyer_id: int) -> bool:
    return _await(_refund_template_purchase(tid, buyer_id))


# ---------- custom agents (P4) ----------
async def _create_custom_agent(user_id: int, name: str, objective: str, skills: str = "") -> int:
    pool = await get_pool()
    return await pool.fetchval(
        "INSERT INTO custom_agents (user_id,name,objective,skills,created_at) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING id",
        user_id, name, objective, skills, int(time.time()),
    )


def create_custom_agent(user_id: int, name: str, objective: str, skills: str = "") -> int:
    return _await(_create_custom_agent(user_id, name, objective, skills))


async def _list_custom_agents(user_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id,name,objective,skills,created_at FROM custom_agents "
        "WHERE user_id=$1 ORDER BY created_at DESC", user_id)
    return [dict(r) for r in rows]


def list_custom_agents(user_id: int) -> list[dict]:
    return _await(_list_custom_agents(user_id))


async def _get_custom_agent(user_id: int, aid: int) -> dict | None:
    pool = await get_pool()
    return _rows(await pool.fetchrow(
        "SELECT id,name,objective,skills,created_at FROM custom_agents "
        "WHERE user_id=$1 AND id=$2", user_id, aid))


def get_custom_agent(user_id: int, aid: int) -> dict | None:
    return _await(_get_custom_agent(user_id, aid))


async def _delete_custom_agent(user_id: int, aid: int) -> bool:
    pool = await get_pool()
    res = await pool.execute(
        "DELETE FROM custom_agents WHERE user_id=$1 AND id=$2", user_id, aid)
    return res.replace("DELETE ", "").isdigit() and int(res.split()[-1]) > 0


def delete_custom_agent(user_id: int, aid: int) -> bool:
    return _await(_delete_custom_agent(user_id, aid))


async def _get_user_credits(user_id: int) -> int:
    pool = await get_pool()
    val = await pool.fetchval("SELECT credits FROM users WHERE id=$1", user_id)
    return int(val) if val else 0


def get_user_credits(user_id: int) -> int:
    return _await(_get_user_credits(user_id))


async def _add_credit(user_id: int, amount: int = 1) -> bool:
    if amount <= 0:
        return False
    pool = await get_pool()
    return await pool.fetchval(
        "UPDATE users SET credits = credits + $1 WHERE id=$2 RETURNING id",
        amount, user_id,
    ) is not None


def add_credit(user_id: int, amount: int = 1) -> bool:
    return _await(_add_credit(user_id, amount))


# Drop-in alias used by main.py's _project_by_pid / dispatch rollback blocks.
def _conn():
    """Compatibility shim: return a lightweight query helper backed by the pool.

    Main.py uses ``c.execute(sql, params).fetchone()`` against ``db._conn`` for a
    couple of low-level blocks. With asyncpg we cannot expose a real DB-API
    cursor, so we return a minimal facade that dispatches to the pool; it only
    supports the ``execute``->``fetchone``/``fetchall`` pattern used in
    :mod:`main`. ``?`` positional placeholders (SQLite style) are translated to
    ``$1, $2, ...`` so the same SQL strings work under both engines.
    ``.close()`` is a no-op (pool-managed).
    """
    return _ConnFacade()


def _translate_placeholders(sql: str, params):
    """Rewrite SQLite-style ``?`` positional params to asyncpg ``$1..$n``."""
    if "?" not in sql or not params:
        return sql, params
    parts = sql.split("?")
    out = [parts[0]]
    for i, chunk in enumerate(parts[1:], start=1):
        out.append(f"${i}")
        out.append(chunk)
    return "".join(out), params


class _ConnFacade:
    """Tiny DB-API-ish facade over the asyncpg pool for main.py's two call sites."""

    def execute(self, sql: str, params=()) -> "_QueryResult":
        return _QueryResult(sql, params)

    def close(self) -> None:
        pass


class _QueryResult:
    def __init__(self, sql: str, params):
        self._sql, self._params = _translate_placeholders(sql, params)

    def fetchone(self):
        return _await(self._fetchrow())

    def fetchall(self):
        return _await(self._fetchall())

    async def _fetchrow(self):
        pool = await get_pool()
        return _rows(await pool.fetchrow(self._sql, *self._params))

    async def _fetchall(self):
        pool = await get_pool()
        return [dict(r) for r in await pool.fetch(self._sql, *self._params)]

    def rowcount(self):
        return 0