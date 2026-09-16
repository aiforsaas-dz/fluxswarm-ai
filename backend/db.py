"""
FluxSwarm auth + persistence layer.

Users are stored in a local SQLite DB (data/users.db). Passwords hashed with
Argon2id (argon2-cffi). Legacy sha256+salt hashes from older versions are still
verifiable and are **upgraded in place** on the user's next successful login.
Each user gets an isolated namespace; their FluxSwarm projects map to Hermes
kanban boards prefixed with their user id. Referral codes and plan tiers live
here too.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError

    _PH = PasswordHasher()
except ImportError:  # pragma: no cover - argon2-cffi is in requirements.txt
    _PH = None

    class VerificationError(Exception):
        pass

BASE = Path(__file__).resolve().parent
# Allow tests / isolated environments to redirect the DB via env (no default change).
DB = BASE / os.environ.get("FLUXSWARM_DB", "data/users.db")
DB.parent.mkdir(exist_ok=True)

# Plan catalogue — one-time credit packs, not subscriptions. Demo is free and
# pre-seeded. "topup" is a pure credit refill: it never changes the user's plan
# tier (parallel cap stays with their highest purchased plan).
PLANS = {
    "demo": {"name": "Demo", "price": 0, "credits": 5, "parallel": 1, "desc": "Free trial — no card required"},
    "starter": {"name": "Starter", "price": 19, "credits": 20, "parallel": 2, "desc": "For freelancers and small projects"},
    "pro": {"name": "Pro", "price": 49, "credits": 60, "parallel": 4, "desc": "For small teams"},
    "scale": {"name": "Scale", "price": 149, "credits": 200, "parallel": 6, "desc": "For companies and agencies"},
    "topup": {"name": "Top-up", "price": 9, "credits": 10, "parallel": 2, "desc": "Quick refill — credits never expire"},
}
PLAN_ORDER = ["demo", "starter", "pro", "scale", "topup"]

REFERRAL_REWARD_CREDITS = 15   # credits granted to referrer when referred user makes a first paid purchase
FRIEND_BONUS_CREDITS = 10      # credits granted to the referred friend on their first paid purchase
REFERRAL_REWARD_CAP = 500      # max credits one referrer can farm (anti-abuse)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    # Enforce FK constraints (DB-1): schema is FK-clean, and delete_user already
    # removes children before the user row, so this is safe to enable.
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pw_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'demo',
            credits INTEGER NOT NULL DEFAULT 3,
            ref_code TEXT UNIQUE NOT NULL,
            referred_by TEXT,
            created_at REAL NOT NULL,
            logged_out_at REAL,
            admt_opt_out INTEGER NOT NULL DEFAULT 0,
            tos_accepted_at REAL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            board_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            goal TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_code TEXT NOT NULL,
            referred_email TEXT NOT NULL,
            rewarded INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS squad_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            agents TEXT NOT NULL,
            price_credits INTEGER NOT NULL DEFAULT 10,
            created_at REAL NOT NULL,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS template_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(template_id) REFERENCES squad_templates(id),
            FOREIGN KEY(buyer_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS custom_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            objective TEXT NOT NULL,
            skills TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            gateway TEXT NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_links (
            telegram_chat_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            linked_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS telegram_codes (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS demo_usage (
            who TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (who, day)
        );
        CREATE TABLE IF NOT EXISTS provider_agreements (
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            agreed_at REAL NOT NULL,
            version TEXT NOT NULL DEFAULT '1.0',
            PRIMARY KEY (user_id, provider),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS admt_disclosures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER,
            disclosed_at REAL NOT NULL,
            acknowledged_at REAL,
            admt_type TEXT,
            logic_summary TEXT,
            human_review_status TEXT,
            requested_at REAL,
            reviewed_at REAL,
            reviewer_notes TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            day TEXT NOT NULL,
            surface TEXT NOT NULL,
            runtime_source TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            ok INTEGER,
            runtime_s INTEGER,
            tasks INTEGER,
            slug TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
        CREATE INDEX IF NOT EXISTS idx_purchases_template ON template_purchases(template_id);
        CREATE INDEX IF NOT EXISTS idx_purchases_buyer ON template_purchases(buyer_id);
        CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(referrer_code);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_day ON provider_usage(day);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_surface ON provider_usage(surface);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_slug ON provider_usage(slug);
        CREATE INDEX IF NOT EXISTS idx_custom_agents_user ON custom_agents(user_id);
        """
    )
    c.commit()
    c.close()
    _migrate()


def _migrate():
    """Bring pre-migration databases up to date.

    Schema additions target databases created by older versions (CREATE IF NOT
    EXISTS never alters an existing table), so we ALTER in place for the columns
    that may be missing. New columns are additive — nothing is ever dropped.
    """
    c = _conn()
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "logged_out_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN logged_out_at REAL")
        if "admt_opt_out" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN admt_opt_out INTEGER NOT NULL DEFAULT 0")
        if "tos_accepted_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN tos_accepted_at REAL")
        pcols = [r[1] for r in c.execute("PRAGMA table_info(projects)")]
        # Launch-outcome bookkeeping: lets the driver (and UI) distinguish a
        # converged launch from one cut short by a provider/worker stall, and
        # tracks whether the single launch credit was already refunded so a
        # refund can never happen twice for the same project.
        if "launch_status" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN launch_status TEXT")
        if "launch_outcome" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN launch_outcome TEXT")
        if "launch_reason" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN launch_reason TEXT")
        if "launch_refunded" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN launch_refunded INTEGER NOT NULL DEFAULT 0")
        if "launch_updated_at" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN launch_updated_at REAL")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS admt_disclosures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                project_id INTEGER,
                disclosed_at REAL NOT NULL,
                acknowledged_at REAL,
                admt_type TEXT,
                logic_summary TEXT,
                human_review_status TEXT,
                requested_at REAL,
                reviewed_at REAL,
                reviewer_notes TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
            """
        )
        c.commit()
    finally:
        c.close()


def _hash_legacy(pw: str, salt: str) -> str:
    """Legacy format from pre-Argon2 versions: sha256(salt+pw), salt stored inline."""
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()


def _make_pw_hash(password: str) -> str:
    """Argon2id hash (salt + params embedded in the string)."""
    if _PH:
        return _PH.hash(password)
    # Should never happen in prod (argon2-cffi is required) — belt & suspenders.
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


def _needs_rehash(stored: str) -> bool:
    if not stored.startswith("$argon2"):
        return True
    try:
        return bool(_PH and _PH.check_needs_rehash(stored))
    except Exception:
        return True


def make_ref_code() -> str:
    return "FLX-" + secrets.token_hex(4).upper()


def create_user(email: str, name: str, password: str, ref_code: str | None = None,
                tos_accepted_at: float | None = None) -> dict:
    email = email.lower().strip()
    c = _conn()
    try:
        salt = secrets.token_hex(8)
        ref = make_ref_code()
        cur = c.execute(
            "INSERT INTO users (email,name,pw_hash,plan,credits,ref_code,referred_by,created_at,tos_accepted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (email, name, _make_pw_hash(password), "demo", PLANS["demo"]["credits"],
             ref, ref_code, time.time(), tos_accepted_at),
        )
        uid = cur.lastrowid
        c.commit()
        # Record referral relationship if a valid code was supplied.
        if ref_code:
            c.execute(
                "INSERT INTO referrals (referrer_code,referred_email,rewarded,created_at) VALUES (?,?,0,?)",
                (ref_code, email, time.time()),
            )
            c.commit()
        return get_user_by_id(uid)
    except sqlite3.IntegrityError:
        raise ValueError("This email is already registered")
    finally:
        c.close()


def update_user_password(email: str, password: str) -> bool:
    email = email.lower().strip()
    c = _conn()
    try:
        cur = c.execute("UPDATE users SET pw_hash=? WHERE email=?", (_make_pw_hash(password), email))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def update_user_name(user_id: int, name: str) -> bool:
    """Rectify the display name (CCPA/CPRA right to correct). False when the
    user does not exist."""
    c = _conn()
    try:
        cur = c.execute("UPDATE users SET name=? WHERE id=?", (name.strip(), user_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def authenticate(email: str, password: str) -> dict | None:
    email = email.lower().strip()
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        c.close()
        return None
    if not _verify_password(password, row["pw_hash"]):
        c.close()
        return None
    user = dict(row)
    # Silent upgrade: legacy sha256+salt (or outdated argon2 params) -> fresh Argon2id.
    if _needs_rehash(user["pw_hash"]):
        new_hash = _make_pw_hash(password)
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (new_hash, user["id"]))
        c.commit()
        user["pw_hash"] = new_hash
    c.close()
    return user


def get_user_by_id(uid: int) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user_by_ref(ref_code: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE ref_code=?", (ref_code,)).fetchone()
    c.close()
    return dict(row) if row else None


def add_project(user_id: int, board_slug: str, name: str, goal: str) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO projects (user_id,board_slug,name,goal,created_at) VALUES (?,?,?,?,?)",
        (user_id, board_slug, name, goal, time.time()),
    )
    pid = cur.lastrowid
    c.commit()
    c.close()
    return pid


def update_project(pid: int, name: str | None = None, goal: str | None = None) -> bool:
    """Update a project's display name and/or build goal in place. Returns True
    when at least one field changed; an empty update is a no-op."""
    sets, vals = [], []
    if name is not None and str(name).strip():
        sets.append("name=?"); vals.append(str(name).strip())
    if goal is not None:
        sets.append("goal=?"); vals.append(str(goal).strip())
    if not sets:
        return False
    vals.append(pid)
    c = _conn()
    try:
        cur = c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def set_launch_outcome(pid: int, status: str, outcome: str, reason: str = "", refunded: bool = False) -> None:
    """Persist a project's launch-terminal state.

    ``status`` is the coarse surface state (``ok`` / ``stuck`` / ``error``),
    ``outcome`` the specific category (``converged`` / ``timeout`` /
    ``provider_failure`` / ``launch_error``) and ``reason`` an internal category
    label for diagnostics. ``refunded`` records on this project that its single
    launch credit was already returned, making a later refund no-op.
    """
    c = _conn()
    try:
        c.execute(
            "UPDATE projects SET launch_status=?, launch_outcome=?, "
            "launch_reason=?, launch_refunded=?, launch_updated_at=? WHERE id=?",
            (status, outcome, reason, (1 if refunded else 0), time.time(), pid),
        )
        c.commit()
    finally:
        c.close()


def refund_launch_credit(user_id: int) -> bool:
    """Return exactly one launch credit to a user (idempotent no-op detection).

    Returns True when the credit was actually returned, False when there was
    nothing to do. The caller is responsible for deciding *whether* the refund
    is earned (via project outcome + ``board_has_completed_work``); this helper
    only guarantees the balance change is atomic and never applied twice for
    the same logical launch (callers gate on the project's ``launch_refunded``
    flag before invoking).
    """
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            c.rollback()
            return False
        c.execute("UPDATE users SET credits = credits + 1 WHERE id=?", (user_id,))
        c.commit()
        return True
    finally:
        c.close()


def bump_demo_usage(who: str, day: str) -> int:
    """Increment a daily demo-usage bucket atomically; returns the NEW count.

    `who` = "anon" for the public instant demo, or "u{user_id}" for an
    authenticated user dispatching shared flux-demo-* boards. The front-line
    abuse cap (P0, FIX-1): demo surfaces are cost-bearing, so they cannot be
    unlimited regardless of plan.
    """
    c = _conn()
    try:
        c.execute(
            "INSERT INTO demo_usage(who, day, count) VALUES(?,?,1) "
            "ON CONFLICT(who,day) DO UPDATE SET count = count + 1",
            (who, day),
        )
        c.commit()
        row = c.execute("SELECT count FROM demo_usage WHERE who=? AND day=?", (who, day)).fetchone()
        return int(row["count"])
    finally:
        c.close()


def _usage_day() -> str:
    """Local calendar day (YYYY-MM-DD) — the period key for usage ledgers."""
    return time.strftime("%Y-%m-%d")


def record_provider_usage(surface: str, runtime_source: str, provider: str,
                          model: str, *, ok: int | None = None,
                          runtime_s: int | None = None, tasks: int | None = None,
                          slug: str | None = None) -> int:
    """Append one provider-usage ledger row (Phase F observability).

    ``surface`` = "demo" | "project"; ``runtime_source`` = "pool" |
    "paid_fallback" | "byok" | "default". ``ok`` stays NULL until the launch
    finalizes (outcome update keys on the unique board slug). This is an
    after-the-fact record — it never gates spend (provider_guard does that).
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO provider_usage(created_at, day, surface, runtime_source, "
            "provider, model, ok, runtime_s, tasks, slug) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (time.time(), _usage_day(), surface, runtime_source, provider, model,
             ok, runtime_s, tasks, slug),
        )
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def update_provider_usage_outcome(slug: str, *, ok: int | None = None,
                                  runtime_s: int | None = None,
                                  tasks: int | None = None) -> bool:
    """Fill the terminal outcome on the latest open attempt for *slug*.

    No-op when no open (ok IS NULL) row exists for the slug; returns whether
    anything was updated. Best-effort: callers must never fail a launch when
    the ledger write fails.
    """
    c = _conn()
    try:
        sets, params = [], []
        if ok is not None:
            sets.append("ok = ?")
            params.append(int(ok))
        if runtime_s is not None:
            sets.append("runtime_s = ?")
            params.append(int(runtime_s))
        if tasks is not None:
            sets.append("tasks = ?")
            params.append(int(tasks))
        if not sets:
            return False
        params.append(slug)
        cur = c.execute(
            "UPDATE provider_usage SET {} WHERE id = "
            "(SELECT id FROM provider_usage WHERE slug = ? AND ok IS NULL "
            "ORDER BY id DESC LIMIT 1)".format(", ".join(sets)),
            params,
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def provider_usage_summary(day: str | None = None) -> dict:
    """Per-day provider-usage totals for /health and ops visibility.

    Aggregates attempts/final outcomes by runtime_source and surface. Never
    gates anything; informative only.
    """
    day = day or _usage_day()
    c = _conn()
    try:
        rows = c.execute(
            "SELECT surface, runtime_source, ok, COUNT(*) AS n FROM provider_usage "
            "WHERE day=? GROUP BY surface, runtime_source, ok", (day,),
        ).fetchall()
    finally:
        c.close()
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


def list_user_projects(user_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def deduct_credit(user_id: int) -> bool:
    """Atomically spend one credit. Uses BEGIN IMMEDIATE so two concurrent
    launches cannot both pass the balance check (race-free debit)."""
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or row["credits"] <= 0:
            c.rollback()
            return False
        c.execute("UPDATE users SET credits = credits - 1 WHERE id=?", (user_id,))
        c.commit()
        return True
    finally:
        c.close()


def reward_referrer_once(referred_email: str) -> bool:
    """Reward the referrer exactly once per referred email (first paid
    subscription that reaches the billing gate) AND grant the referred friend
    their one-time welcome bonus — all inside the same atomic claim so neither
    reward can double-fire under concurrency. Prevents credit farming by
    oscillating subscriptions AND prevents double-credit under concurrency.

    The read-modify-write runs inside BEGIN IMMEDIATE so only one caller can
    hold the pending row at a time; the claim UPDATE targets the specific row id
    and relies on its rowcount, so a concurrent loser (whose SELECT returned the
    same pending row before we committed) gets rowcount==0 and returns False
    instead of minting a second reward. This closes the double-credit race that a
    plain ``BEGIN IMMEDIATE`` around a ``WHERE referred_email=? AND rewarded=0``
    UPDATE does NOT fix.

    Referrer earnings are capped at REFERRAL_REWARD_CAP credits (anti-farming):
    once the cap is reached the row is still claimed so the friend bonus fires
    exactly once and no retry loop can mint further rewards.
    """
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT id, referrer_code FROM referrals "
            "WHERE referred_email=? AND rewarded=0 ORDER BY id LIMIT 1",
            (referred_email,),
        ).fetchone()
        if not row:
            c.rollback()
            return False
        ref_user = c.execute("SELECT id FROM users WHERE ref_code=?", (row["referrer_code"],)).fetchone()
        if ref_user:
            cur = c.execute("SELECT credits FROM users WHERE id=?", (ref_user["id"],)).fetchone()
            held = cur["credits"] if cur else 0
            grant = min(REFERRAL_REWARD_CREDITS, max(0, REFERRAL_REWARD_CAP - held))
            if grant:
                c.execute("UPDATE users SET credits = credits + ? WHERE id=?",
                          (grant, ref_user["id"]))
        friend = c.execute("SELECT id FROM users WHERE email=?", (referred_email,)).fetchone()
        if friend:
            c.execute("UPDATE users SET credits = credits + ? WHERE id=?",
                      (FRIEND_BONUS_CREDITS, friend["id"]))
        # Claim THIS specific row; only the winner (rowcount==1) commits.
        if c.execute("UPDATE referrals SET rewarded=1 WHERE id=? AND rewarded=0",
                     (row["id"],)).rowcount == 0:
            c.rollback()
            return False
        c.commit()
        return bool(ref_user)
    except Exception:
        c.rollback()
        return False
    finally:
        c.close()


def add_credits(user_id: int, credits: int) -> bool:
    """Top-up a credit balance without touching the user's plan tier. Used by the
    one-time "topup" pack; the plan/parallel cap stays with the highest plan the
    user purchased (topup is a refill, not a tier)."""
    try:
        c = _conn()
        try:
            cur = c.execute("UPDATE users SET credits = credits + ? WHERE id=?", (credits, user_id))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()
    except Exception:
        return False


def upgrade_plan(user_id: int, plan: str):
    if plan not in PLANS:
        raise ValueError("Invalid plan")
    # Grant the plan's credit allowance without ever clawing back credits the
    # user already holds, and without refilling on repeated subscriptions to the
    # same tier (closes the infinite-credit-refill exploit once billing is live).
    c = _conn()
    try:
        cur = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        current = cur["credits"] if cur else 0
        new_credits = max(current, PLANS[plan]["credits"])
        c.execute("UPDATE users SET plan=?, credits=? WHERE id=?", (plan, new_credits, user_id))
        c.commit()
    finally:
        c.close()


def downgrade_subscription(user_id: int):
    """Drop a user to the free plan (used on a verified payment refund).

    Credits already held are kept — we never claw back pre-paid credits, we only
    prevent further top-ups after the subscription is revoked. The stored value
    is the plan *key* (``demo``), never the display name, so downstream
    ``PLANS[plan]`` lookups keep working after a refund.
    """
    c = _conn()
    try:
        c.execute("UPDATE users SET plan=? WHERE id=? AND plan != ?",
                  ("demo", user_id, "demo"))
        c.commit()
    finally:
        c.close()


def record_payment_event(event_id: str, gateway: str, kind: str, user_id: int, detail: dict) -> bool:
    """Persist a processed payment event; returns False when already seen
    (UNIQUE constraint) so webhook replay is idempotent."""
    import json
    c = _conn()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO payment_events (event_id,gateway,kind,user_id,detail,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (event_id, gateway, kind, user_id, json.dumps(detail, ensure_ascii=False), time.time()),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def payment_user_by_txn(txn_id: str) -> int | None:
    """Map a Paddle transaction id back to the user who paid for it.

    Paddle v1 delivers refunds as ``adjustment.created`` events whose payload
    carries only the original transaction id (no ``custom_data``), so a refund
    cannot name its user directly. We store the txn id on each granted payment
    event (``detail.txn``); the adjustment handler looks it up here to decide
    which subscription to downgrade.
    """
    if not txn_id:
        return None
    import json
    c = _conn()
    try:
        # JSON1 is not guaranteed at runtime; scan explicitly to stay robust.
        found = None
        for r in c.execute(
            "SELECT id, user_id, detail FROM payment_events "
            "WHERE kind='payment.succeeded' ORDER BY id DESC"
        ):
            try:
                detail = json.loads(r["detail"] or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(detail, dict) and detail.get("txn") == txn_id:
                found = r["user_id"]
                break
        return found
    finally:
        c.close()


# ---------- Telegram account linking ----------
TELEGRAM_LINK_TTL = 600  # seconds a pairing code stays valid


def new_telegram_link_code(user_id: int, ttl: int = TELEGRAM_LINK_TTL) -> str:
    """Issue a one-time pairing code for a user.

    A user holds at most one *active* (unused + unexpired) code at a time; a
    still-valid code is re-served instead of minting a new one, which throttles
    code generation (one per 10 min) without a separate limiter.
    """
    c = _conn()
    try:
        now = time.time()
        c.execute("DELETE FROM telegram_codes WHERE used=1 OR expires_at < ?", (now,))
        row = c.execute(
            "SELECT code FROM telegram_codes WHERE user_id=? AND used=0 AND expires_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()
        if row:
            return row["code"]
        for _ in range(8):
            code = secrets.token_hex(3).upper()
            try:
                c.execute(
                    "INSERT INTO telegram_codes (code,user_id,expires_at,used,created_at) VALUES (?,?,?,0,?)",
                    (code, user_id, now + ttl, now),
                )
                c.commit()
                return code
            except sqlite3.IntegrityError:
                continue
        raise ValueError("could_not_generate_code")
    finally:
        c.close()


def consume_telegram_link_code(code: str, telegram_chat_id: int) -> dict | None:
    """Redeem a pairing code as PLAIN chat id -> account; None when invalid.

    Expired or already-used codes are cleared and rejected. The resulting link
    is upserted so re-pairing a chat to a different account (or vice versa) just
    overwrites the old binding. Called from the bot process on ``/link``.
    """
    c = _conn()
    try:
        now = time.time()
        c.execute("DELETE FROM telegram_codes WHERE used=1 OR expires_at < ?", (now,))
        row = c.execute(
            "SELECT * FROM telegram_codes WHERE code=?", (str(code).strip().upper(),)
        ).fetchone()
        if not row:
            return None
        if row["used"] or row["expires_at"] < now:
            return None
        uid = row["user_id"]
        # Replace any existing binding for this chat/user (move link, not stack).
        c.execute("DELETE FROM telegram_links WHERE telegram_chat_id=? OR user_id=?", (telegram_chat_id, uid))
        c.execute("INSERT INTO telegram_links (telegram_chat_id,user_id,linked_at) VALUES (?,?,?)",
                  (telegram_chat_id, uid, now))
        c.execute("UPDATE telegram_codes SET used=1 WHERE code=? AND used=0", (row["code"],))
        c.commit()
        user = c.execute("SELECT id, name, plan FROM users WHERE id=?", (uid,)).fetchone()
        return dict(user)
    finally:
        c.close()


def get_user_by_telegram_chat(telegram_chat_id: int) -> dict | None:
    """Resolve which (if any) platform account a Telegram chat is linked to."""
    c = _conn()
    try:
        row = c.execute(
            "SELECT u.* FROM telegram_links t JOIN users u ON u.id=t.user_id "
            "WHERE t.telegram_chat_id=?",
            (telegram_chat_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def get_telegram_link(user_id: int) -> dict | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT telegram_chat_id, linked_at FROM telegram_links WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def unlink_telegram(user_id: int) -> bool:
    c = _conn()
    try:
        cur = c.execute("DELETE FROM telegram_links WHERE user_id=?", (user_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


# ---------- CCPA / account rights ----------
def account_payload(user_id: int) -> dict:
    """Everything the platform holds about a user (CCPA/CPRA right to access)."""
    import json as _json
    c = _conn()
    try:
        u = c.execute("SELECT id,email,name,plan,credits,ref_code,referred_by,created_at "
                      "FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            raise ValueError("user_not_found")
        projects = [dict(r) for r in c.execute(
            "SELECT id,board_slug,name,goal,created_at FROM projects WHERE user_id=?", (user_id,))]
        refs = [dict(r) for r in c.execute(
            "SELECT referrer_code,referred_email,rewarded,created_at FROM referrals WHERE referrer_code=?",
            (u["ref_code"],))]
        tpls = [dict(r) for r in c.execute(
            "SELECT id,name,price_credits,created_at FROM squad_templates WHERE author_id=?", (user_id,))]
        buys = [dict(r) for r in c.execute(
            "SELECT template_id,created_at FROM template_purchases WHERE buyer_id=?", (user_id,))]
        pays = [dict(r) for r in c.execute(
            "SELECT id,gateway,kind,created_at FROM payment_events WHERE user_id=?", (user_id,))]
        tg = [dict(r) for r in c.execute(
            "SELECT telegram_chat_id,linked_at FROM telegram_links WHERE user_id=?", (user_id,))]
        agrees = [dict(r) for r in c.execute(
            "SELECT provider,agreed_at,version FROM provider_agreements WHERE user_id=?", (user_id,))]
        agents = [dict(r) for r in c.execute(
            "SELECT id,name,objective,skills,created_at FROM custom_agents WHERE user_id=?", (user_id,))]
        return {"user": dict(u), "projects": projects, "referrals": refs,
                "templates": tpls, "template_purchases": buys, "payment_events": pays,
                "telegram_links": tg, "provider_agreements": agrees,
                "custom_agents": agents}
    finally:
        c.close()


def delete_user(user_id: int) -> bool:
    """Permanently erase a user and all their rows (CCPA/CPRA right to delete).
    Returns True if a user row was removed."""
    c = _conn()
    try:
        u = c.execute("SELECT email,ref_code FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            return False
        for sql in (
            "DELETE FROM projects WHERE user_id=?",
            "DELETE FROM squad_templates WHERE author_id=?",
            "DELETE FROM template_purchases WHERE buyer_id=?",
            "DELETE FROM referrals WHERE referrer_code=?",
            "DELETE FROM payment_events WHERE user_id=?",
            "DELETE FROM telegram_links WHERE user_id=?",
            "DELETE FROM telegram_codes WHERE user_id=?",
            "DELETE FROM password_resets WHERE user_id=?",
            "DELETE FROM provider_agreements WHERE user_id=?",
            "DELETE FROM admt_disclosures WHERE user_id=?",
            "DELETE FROM custom_agents WHERE user_id=?",
            "DELETE FROM users WHERE id=?",
        ):
            try:
                c.execute(sql, (user_id,))
            except sqlite3.OperationalError:
                pass
        c.commit()
        return True
    finally:
        c.close()


# ---------- provider agreements (Phase 3 BYOK gate) ----------

def agree_provider(user_id: int, provider: str, version: str = "1.0") -> bool:
    """Record a user's acceptance of a provider's terms (idempotent upsert).

    Re-acceptance refreshes ``agreed_at`` and the accepted ``version``.
    Returns True on first acceptance, False when already agreed.
    """
    c = _conn()
    try:
        now = time.time()
        existing = c.execute(
            "SELECT agreed_at FROM provider_agreements WHERE user_id=? AND provider=?",
            (user_id, provider)).fetchone()
        c.execute(
            "INSERT INTO provider_agreements (user_id, provider, agreed_at, version) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, provider) DO UPDATE SET agreed_at=?, version=?",
            (user_id, provider, now, version, now, version))
        c.commit()
        return existing is None
    finally:
        c.close()


def provider_agreements(user_id: int) -> dict[str, dict]:
    """{provider: {agreed_at, version}} for a user (empty dict when none)."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT provider, agreed_at, version FROM provider_agreements WHERE user_id=?",
            (user_id,)).fetchall()
        return {r["provider"]: {"agreed_at": r["agreed_at"], "version": r["version"]} for r in rows}
    finally:
        c.close()


def has_provider_agreement(user_id: int, provider: str) -> bool:
    c = _conn()
    try:
        r = c.execute(
            "SELECT 1 FROM provider_agreements WHERE user_id=? AND provider=?",
            (user_id, provider)).fetchone()
        return r is not None
    finally:
        c.close()


def provider_agreements_summary() -> list[dict]:
    """Operator view of accepted provider agreements (admin endpoint).

    The ledger lives in alembic 003 (user_id/provider/agreed_at/version). The
    session-2 prompt's registry columns (agreement_type/signed_at/expires_at/
    jurisdiction/document_url) do not exist in the schema — the authentic table
    IS the acceptance ledger; vendor DPA links are served by the legal pages
    (see docs/CCPA_RISK_ASSESSMENT.md).
    """
    c = _conn()
    try:
        rows = c.execute(
            "SELECT pa.provider, pa.agreed_at, pa.version, u.email "
            "FROM provider_agreements pa JOIN users u ON u.id = pa.user_id "
            "ORDER BY pa.agreed_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


# ---------- CCPA/CPRA ADMT (Phase 5) ----------

def record_admt_notice_ack(user_id: int) -> int:
    """Persist a pre-use notice acknowledgment (disclosed_at = acknowledged_at).

    Each acknowledgment (incl. re-reads before opt-in) appends a ledger row so
    the disclosure history is transparent to the data-subject.
    """
    c = _conn()
    try:
        now = time.time()
        cur = c.execute(
            "INSERT INTO admt_disclosures (user_id,disclosed_at,acknowledged_at,admt_type) "
            "VALUES (?,?,?,?)",
            (user_id, now, now, "pre-use-notice"),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def has_admt_notice_ack(user_id: int) -> bool:
    c = _conn()
    try:
        r = c.execute(
            "SELECT 1 FROM admt_disclosures WHERE user_id=? AND admt_type='pre-use-notice' "
            "AND acknowledged_at IS NOT NULL LIMIT 1",
            (user_id,),
        ).fetchone()
        return r is not None
    finally:
        c.close()


def get_admt_opt_out(user_id: int) -> bool:
    c = _conn()
    try:
        r = c.execute("SELECT admt_opt_out FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(r and r["admt_opt_out"])
    finally:
        c.close()


def set_admt_opt_out(user_id: int, value: bool) -> None:
    c = _conn()
    try:
        c.execute("UPDATE users SET admt_opt_out=? WHERE id=?", (1 if value else 0, user_id))
        c.commit()
    finally:
        c.close()


def request_human_review(user_id: int, project_id: int) -> int:
    """Create a human-review request for a project (admt_disclosures row)."""
    c = _conn()
    try:
        now = time.time()
        cur = c.execute(
            "INSERT INTO admt_disclosures (user_id,project_id,disclosed_at,admt_type,"
            "human_review_status,requested_at) VALUES (?,?,?,?,?,?)",
            (user_id, project_id, now, "human-review-request", "requested", now),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def get_human_review(review_id: int) -> dict | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT d.*, u.email AS user_email, u.name AS user_name, "
            "p.name AS project_name, p.board_slug "
            "FROM admt_disclosures d JOIN users u ON u.id=d.user_id "
            "LEFT JOIN projects p ON p.id=d.project_id WHERE d.id=?",
            (review_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def list_human_review_queue(limit: int = 100) -> list[dict]:
    """Pending (status='requested') human-review requests, oldest first."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT d.*, u.email AS user_email, u.name AS user_name, "
            "p.name AS project_name, p.board_slug, p.goal "
            "FROM admt_disclosures d JOIN users u ON u.id=d.user_id "
            "LEFT JOIN projects p ON p.id=d.project_id "
            "WHERE d.human_review_status='requested' ORDER BY d.requested_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def update_human_review(review_id: int, status: str, notes: str = "") -> bool:
    """Resolve a pending request; returns False when already resolved/missing."""
    c = _conn()
    try:
        cur = c.execute(
            "UPDATE admt_disclosures SET human_review_status=?, reviewer_notes=?, reviewed_at=? "
            "WHERE id=? AND human_review_status='requested'",
            (status, notes, time.time(), review_id),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


# ---------- session invalidation / password lifecycle ----------
SESSION_MIN_TIME = 0  # `logged_out_at` compare base for tokens minted pre-`iat`


def mark_logged_out(user_id: int) -> None:
    """Invalidate every currently-issued session token for a user.

    `logged_out_at` is set to now. Any JWT whose `iat` predates this timestamp
    is rejected by the auth dependency (logout, password change, reset) — all
    sessions die at once, which is the safe behaviour for a full reset and the
    simplest correct semantics for logout with a stateless token.
    """
    c = _conn()
    try:
        # Float (not int): a token minted a fraction of a second after logout
        # must have iat > logged_out_at even within the same second, while a
        # pre-logout token keeps iat < logged_out_at. Int seconds would conflate
        # the two (both floor to the same integer).
        c.execute("UPDATE users SET logged_out_at=? WHERE id=?", (time.time(), user_id))
        c.commit()
    finally:
        c.close()


def get_logged_out_at(user_id: int) -> float:
    c = _conn()
    try:
        row = c.execute("SELECT logged_out_at FROM users WHERE id=?", (user_id,)).fetchone()
        return float(row["logged_out_at"] or 0) if row else 0.0
    finally:
        c.close()


def set_password(user_id: int, new_password: str) -> None:
    """Set a new password hash and invalidate existing sessions."""
    c = _conn()
    try:
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (_make_pw_hash(new_password), user_id))
        c.commit()
    finally:
        c.close()
    mark_logged_out(user_id)


# ---------- password reset tokens (single-use, expiring, hashed at rest) ----------
RESET_TTL = 900  # seconds: reset link window


def create_password_reset(user_id: int, ttl: int = RESET_TTL) -> str:
    """Mint a reset token for a user. Returns the RAW token (the caller
    delivers it out-of-band — a mailer, or the dev/test echo flag). Only the
    sha256 of the token is ever stored, so a DB leak cannot be replayed.
    Purging: expired and used rows are cleared on each issuance.
    """
    raw = secrets.token_urlsafe(32)
    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    c = _conn()
    try:
        now = time.time()
        c.execute("DELETE FROM password_resets WHERE used=1 OR expires_at < ?", (now,))
        c.execute(
            "INSERT INTO password_resets (token_hash,user_id,expires_at,used,created_at) "
            "VALUES (?,?,?,0,?)",
            (th, user_id, now + ttl, now),
        )
        c.commit()
    finally:
        c.close()
    return raw


def consume_password_reset(raw: str) -> int | None:
    """Redeem a reset token. Returns the user id on success, None otherwise.
    Expired/used/unknown tokens are rejected; a successful redeem is single-use."""
    if not raw:
        return None
    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    c = _conn()
    try:
        now = time.time()
        c.execute("DELETE FROM password_resets WHERE used=1 OR expires_at < ?", (now,))
        row = c.execute(
            "SELECT user_id FROM password_resets WHERE token_hash=? AND used=0 AND expires_at >= ?",
            (th, now),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE password_resets SET used=1 WHERE token_hash=? AND used=0", (th,))
        c.commit()
        return row["user_id"]
    finally:
        c.close()


# ---------- squad marketplace ----------
def publish_template(author_id: int, name: str, description: str, agents: list[str], price_credits: int = 10) -> int:
    import json
    c = _conn()
    cur = c.execute(
        "INSERT INTO squad_templates (author_id,name,description,agents,price_credits,created_at) VALUES (?,?,?,?,?,?)",
        (author_id, name, description, json.dumps(agents), price_credits, time.time()),
    )
    tid = cur.lastrowid
    c.commit()
    c.close()
    return tid


def list_templates(author_id: int | None = None) -> list[dict]:
    c = _conn()
    if author_id:
        rows = c.execute("SELECT * FROM squad_templates WHERE author_id=? ORDER BY created_at DESC", (author_id,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM squad_templates ORDER BY created_at DESC").fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["agents"] = json.loads(d["agents"])
        except Exception:
            d["agents"] = []
        out.append(d)
    return out


def get_template(tid: int) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM squad_templates WHERE id=?", (tid,)).fetchone()
    c.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["agents"] = json.loads(d["agents"])
    except Exception:
        d["agents"] = []
    return d


# ---------- custom agents (P4) ----------
def create_custom_agent(user_id: int, name: str, objective: str, skills: str = "") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO custom_agents (user_id,name,objective,skills,created_at) VALUES (?,?,?,?,?)",
        (user_id, name, objective, skills, time.time()),
    )
    aid = cur.lastrowid
    c.commit()
    c.close()
    return aid


def list_custom_agents(user_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT id,name,objective,skills,created_at FROM custom_agents "
        "WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_custom_agent(user_id: int, aid: int) -> dict | None:
    c = _conn()
    row = c.execute(
        "SELECT id,name,objective,skills,created_at FROM custom_agents "
        "WHERE user_id=? AND id=?", (user_id, aid)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_custom_agent(user_id: int, aid: int) -> bool:
    c = _conn()
    cur = c.execute(
        "DELETE FROM custom_agents WHERE user_id=? AND id=?", (user_id, aid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def buy_template(tid: int, buyer_id: int) -> bool:
    """Atomic purchase: buyer spends price_credits; author earns half the price.

    Wrapped in BEGIN IMMEDIATE so two concurrent purchases cannot both pass the
    balance check (race-free credit debit). Returns False if insufficient funds
    or the template does not exist.
    """
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM squad_templates WHERE id=?", (tid,)).fetchone()
        if not row:
            c.rollback()
            return False
        price = row["price_credits"]
        bal = c.execute("SELECT credits FROM users WHERE id=?", (buyer_id,)).fetchone()
        if not bal or bal["credits"] < price:
            c.rollback()
            return False
        c.execute("UPDATE users SET credits = credits - ? WHERE id=?", (price, buyer_id))
        # Author earns 50% of price (rounded, min 1).
        author_earn = max(1, price // 2)
        c.execute("UPDATE users SET credits = credits + ? WHERE id=?", (author_earn, row["author_id"]))
        c.execute("INSERT INTO template_purchases (template_id,buyer_id,created_at) VALUES (?,?,?)",
                  (tid, buyer_id, time.time()))
        c.commit()
        return True
    except Exception:
        c.rollback()
        return False
    finally:
        c.close()


def refund_template_purchase(tid: int, buyer_id: int) -> bool:
    """Undo a buy_template charge when the squad launch failed after the debit.

    Runs inside BEGIN IMMEDIATE so it cannot double-refund: we only reverse the
    most recent, still-unrefunded purchase of (tid, buyer_id). The author's
    earned share is clawed back too so no credits are minted on a failed run.
    Returns True if a purchase was reversed.
    """
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM template_purchases WHERE template_id=? AND buyer_id=? "
            "ORDER BY id DESC LIMIT 1",
            (tid, buyer_id),
        ).fetchone()
        if not row:
            c.rollback()
            return False
        tpl = c.execute("SELECT price_credits, author_id FROM squad_templates WHERE id=?", (tid,)).fetchone()
        if not tpl:
            c.rollback()
            return False
        price = tpl["price_credits"]
        refund = max(0, price)
        c.execute("UPDATE users SET credits = credits + ? WHERE id=?", (refund, buyer_id))
        # Claw back the author's 50% share so totals stay consistent.
        author_earn = max(1, price // 2)
        c.execute(
            "UPDATE users SET credits = credits - ? WHERE id=(SELECT author_id FROM squad_templates WHERE id=?)",
            (author_earn, tid),
        )
        c.execute("DELETE FROM template_purchases WHERE id=?", (row["id"],))
        c.commit()
        return True
    except Exception:
        c.rollback()
        return False
    finally:
        c.close()


def get_user_credits(user_id: int) -> int:
    c = _conn()
    try:
        row = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        return int(row["credits"]) if row else 0
    finally:
        c.close()


def add_credit(user_id: int, amount: int = 1) -> bool:
    """Top up credits (used to refund a spent credit on launch failure)."""
    if amount <= 0:
        return False
    c = _conn()
    try:
        cur = c.execute("UPDATE users SET credits = credits + ? WHERE id=?", (amount, user_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


# Pre-seed a demo account so visitors can try instantly.
# The demo password is never a public constant: it comes from the operator
# (FLUXSWARM_DEMO_PASSWORD) or is a fresh random token no human knows. The
# demo account exists to back anonymous /api/demo/launch runs, not for
# interactive login, so its secret is disposable by design.
def seed_demo():
    c = _conn()
    existing = c.execute("SELECT 1 FROM users WHERE email=?", ("demo@fluxswarm.ai",)).fetchone()
    pw = os.environ.get("FLUXSWARM_DEMO_PASSWORD")
    if not existing:
        pw = pw or secrets.token_urlsafe(18)
        create_user("demo@fluxswarm.ai", "Demo User", pw)
    elif pw:
        # An operator/test pinned FLUXSWARM_DEMO_PASSWORD after a prior seed:
        # re-pin the account so callers that log into the demo user cannot be
        # stranded by an earlier random secret (deterministic test sandboxes).
        stored = c.execute(
            "SELECT pw_hash FROM users WHERE email=?", ("demo@fluxswarm.ai",)).fetchone()[0]
        if stored and not _verify_password(pw, stored):
            update_user_password("demo@fluxswarm.ai", pw)
    c.close()


init_db()
seed_demo()
