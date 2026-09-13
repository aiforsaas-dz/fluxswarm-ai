"""Phase 1: SQLite -> PostgreSQL persistence layer tests.

Covers: pool/settings, CRUD parity vs db.py, concurrent credit-deduction race
(pg advisory semantics), referral single-reward race, FK enforcement, JSON
round-trip, payment idempotency and the SQLite->PG migration script idempotency.

These tests require a live PostgreSQL. In CI/CI the URL comes from
FLUXSWARM_DATABASE_URL; locally point it at a dev instance, e.g.
    postgresql://postgres:postgres@localhost:5432/fluxswarm_test
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

import asyncpg
import pytest

os.environ.setdefault(
    "FLUXSWARM_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/fluxswarm_test",
)

import db_postgres as pg  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_db():
    """Every test starts from an empty (but migrated) PostgreSQL schema."""
    pg.init_db()          # idempotent; also exercises Alembic in each run
    pg.reset_db()
    yield
    pg.reset_db()


@pytest.fixture(scope="module")
def loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop() if False else pg._loop()


# ---------- pool & settings ----------

def test_pool_settings_from_env():
    assert pg.DB_POOL_MIN >= 1
    assert pg.DB_POOL_MAX >= pg.DB_POOL_MIN
    assert pg.DB_TIMEOUT_S >= 1


def test_get_pool_reusable_and_consistent():
    a = pg._await(pg.get_pool())
    b = pg._await(pg.get_pool())
    assert a is b


def test_pool_connects_and_defaults(loop):
    async def probe():
        pool = await pg.get_pool()
        assert pool.get_min_size() == pg.DB_POOL_MIN
        assert pool.get_max_size() == pg.DB_POOL_MAX
    asyncio.run_coroutine_threadsafe(probe(), loop).result()


def test_migrations_are_idempotent():
    pg.init_db()
    pg.init_db()
    # 001 -> 006 applied exactly once; HEAD is 006 (Phase F provider_usage).

    async def ver():
        pool = await pg.get_pool()
        rows = await pool.fetch("SELECT version_num FROM alembic_version")
        return [r["version_num"] for r in rows]

    assert asyncio.run_coroutine_threadsafe(ver(), pg._loop()).result() == ["006"]


# ---------- users CRUD / auth ----------

def test_create_user_returns_row_and_seed_defaults():
    u = pg.create_user("alice@example.com", "Alice", "hunter2")
    assert u["email"] == "alice@example.com"
    assert u["plan"] == "demo"
    assert u["credits"] == pg.PLANS["demo"]["credits"]
    assert u["ref_code"].startswith("FLX-")


def test_create_user_normalizes_email_and_duplicate_raises():
    pg.create_user("BOB@example.com", "Bob", "pw")
    with pytest.raises(ValueError):
        pg.create_user("bob@example.com", "Bob2", "pw2")


def test_authenticate_roundtrip_and_case_insensitive():
    pg.create_user("carol@example.com", "Carol", "s3cret")
    u = pg.authenticate("CAROL@example.com", "s3cret")
    assert u and u["name"] == "Carol"
    assert pg.authenticate("carol@example.com", "wrong") is None


def test_legacy_sha256_hash_upgraded_on_login():
    import hashlib, secrets
    salt = secrets.token_hex(8)
    legacy = salt + "$" + hashlib.sha256((salt + "pw").encode()).hexdigest()
    async def inject():
        pool = await pg.get_pool()
        uid = await pool.fetchval(
            "INSERT INTO users (email,name,pw_hash,plan,credits,ref_code,referred_by,created_at) "
            "VALUES ($1,$2,$3,'demo',3,$4,NULL,$5) RETURNING id",
            "legacy@example.com", "Legacy", legacy, "FLX-LG", int(__import__("time").time()),
        )
        return uid
    pg._await(inject())
    u = pg.authenticate("legacy@example.com", "pw")
    assert u is not None
    # Hash must now be Argon2id (in-place upgrade).
    assert u["pw_hash"].startswith("$argon2")


def test_get_user_lookups_by_id_email_ref():
    u = pg.create_user("dave@example.com", "Dave", "pw")
    assert pg.get_user_by_id(u["id"])["email"] == "dave@example.com"
    assert pg.get_user_by_email("DAVE@example.com")["id"] == u["id"]
    assert pg.get_user_by_ref(u["ref_code"])["id"] == u["id"]
    assert pg.get_user_by_email("nobody@example.com") is None


# ---------- projects / credits / demo ----------

def test_add_and_list_projects_newest_first():
    u = pg.create_user("erin@example.com", "Erin", "pw")
    pg.add_project(u["id"], "p-one", "One", "goal1")
    pg.add_project(u["id"], "p-two", "Two", "goal2")
    projects = pg.list_user_projects(u["id"])
    assert [p["board_slug"] for p in projects] == ["p-two", "p-one"]


def test_add_project_duplicate_board_slug_raises():
    u = pg.create_user("frank@example.com", "Frank", "pw")
    pg.add_project(u["id"], "dup", "A", "g")
    with pytest.raises(ValueError):
        pg.add_project(u["id"], "dup", "B", "g")


def test_concurrent_deduct_credit_race_free():
    """Exactly `credits` of N concurrent debits can succeed (no overspend)."""
    u = pg.create_user("grace@example.com", "Grace", "pw")
    total = pg.PLANS["demo"]["credits"] + 7
    pg.add_credit(u["id"], 7)          # demo + 7 credits
    results = [None] * 20
    def worker():
        return pg.deduct_credit(u["id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(lambda _: worker(), range(20)))
    assert sum(results) == total
    assert pg.get_user_credits(u["id"]) == 0


def test_refund_launch_credit_increments():
    u = pg.create_user("heidi@example.com", "Heidi", "pw")
    before = pg.get_user_credits(u["id"])
    assert pg.refund_launch_credit(u["id"]) is True
    assert pg.get_user_credits(u["id"]) == before + 1


def test_demo_usage_bump_atomic():
    assert pg.bump_demo_usage("anon", "2026-09-04") == 1
    assert pg.bump_demo_usage("anon", "2026-09-04") == 2
    assert pg.bump_demo_usage("u7", "2026-09-04") == 1


def test_launch_outcome_persisted():
    u = pg.create_user("ivan@example.com", "Ivan", "pw")
    pid = pg.add_project(u["id"], "l-o", "L", "g")
    pg.set_launch_outcome(pid, "ok", "converged", "", refunded=True)
    p = next(x for x in pg.list_user_projects(u["id"]) if x["id"] == pid)
    assert p["launch_status"] == "ok"
    assert p["launch_outcome"] == "converged"
    assert p["launch_refunded"] == 1


# ---------- payments / referral race ----------

def test_payment_event_idempotent_by_event_id():
    u = pg.create_user("judy@example.com", "Judy", "pw")
    assert pg.record_payment_event("evt-1", "paddle", "payment.succeeded", u["id"], {"txn": "T1"}) is True
    assert pg.record_payment_event("evt-1", "paddle", "payment.succeeded", u["id"], {"txn": "T1"}) is False
    assert pg.payment_user_by_txn("T1") == u["id"]


def test_concurrent_reward_referrer_once_only_one_winner():
    a = pg.create_user("ref@example.com", "Referrer", "pw")
    pg.create_user("new@example.com", "New", "pw", ref_code=a["ref_code"])  # referral row inserted
    results = [None] * 10
    def worker():
        return pg.reward_referrer_once("new@example.com")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda _: worker(), range(10)))
    assert sum(1 for r in results if r) == 1
    assert pg.get_user_credits(a["id"]) == pg.PLANS["demo"]["credits"] + pg.REFERRAL_REWARD_CREDITS


def test_upgrade_then_downgrade_plan():
    u = pg.create_user("kate@example.com", "Kate", "pw")
    pg.upgrade_plan(u["id"], "pro")
    assert pg.get_user_by_id(u["id"])["plan"] == "pro"
    pg.downgrade_subscription(u["id"])
    assert pg.get_user_by_id(u["id"])["plan"] == "demo"
    with pytest.raises(ValueError):
        pg.upgrade_plan(u["id"], "gold")


# ---------- FK enforcement ----------

def test_fk_enforced_project_requires_valid_user():
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        pg.add_project(999999, "fk-test", "X", "g")


def test_fk_cascade_user_delete_removes_child_rows():
    u = pg.create_user("leo@example.com", "Leo", "pw")
    pg.add_project(u["id"], "cascade-one", "C", "g")
    pg.publish_template(u["id"], "Tpl", "d", ["dev"], 10)
    assert pg.delete_user(u["id"]) is True
    assert pg.list_user_projects(u["id"]) == []
    assert pg.list_templates(u["id"]) == []


# ---------- telegram linking ----------

def test_telegram_link_flow():
    u = pg.create_user("mike@example.com", "Mike", "pw")
    code = pg.new_telegram_link_code(u["id"])
    linked = pg.consume_telegram_link_code(code, 1111)
    assert linked["id"] == u["id"]
    assert pg.get_user_by_telegram_chat(1111)["id"] == u["id"]
    assert pg.unlink_telegram(u["id"]) is True
    assert pg.get_telegram_link(u["id"]) is None


def test_telegram_code_rejected_after_use():
    u = pg.create_user("nina@example.com", "Nina", "pw")
    code = pg.new_telegram_link_code(u["id"])
    assert pg.consume_telegram_link_code(code, 2222) is not None
    assert pg.consume_telegram_link_code(code, 3333) is None


# ---------- password reset ----------

def test_password_reset_single_use():
    u = pg.create_user("oscar@example.com", "Oscar", "pw")
    raw = pg.create_password_reset(u["id"])
    assert pg.consume_password_reset(raw) == u["id"]
    assert pg.consume_password_reset(raw) is None
    assert pg.consume_password_reset("garbage") is None


# ---------- marketplace ----------

def test_template_buy_credits_transfer_and_refund():
    author = pg.create_user("pat@example.com", "Pat", "pw")
    buyer = pg.create_user("quinn@example.com", "Quinn", "pw")
    tid = pg.publish_template(author["id"], "S", "d", ["dev", "arch"], 3)
    assert pg.buy_template(tid, buyer["id"]) is True
    base = pg.PLANS["demo"]["credits"]
    assert pg.get_user_credits(buyer["id"]) == base - 3
    assert pg.get_user_credits(author["id"]) == base + 1            # author earns max(1,3//2)=1
    # Below-budget purchase fails atomically (nothing debited).
    fail = pg.publish_template(author["id"], "Exp", "d", ["dev"], 100)
    assert pg.buy_template(fail, buyer["id"]) is False
    assert pg.get_user_credits(buyer["id"]) == base - 3
    # Refund reverses exactly the last purchase (author's share clawed back).
    assert pg.refund_template_purchase(tid, buyer["id"]) is True
    assert pg.get_user_credits(buyer["id"]) == base
    assert pg.get_user_credits(author["id"]) == base


def test_template_json_agents_roundtrip():
    author = pg.create_user("robin@example.com", "Robin", "pw")
    tid = pg.publish_template(author["id"], "J", "d", ["planner", "coder", {"k": 1}], 5)
    t = pg.get_template(tid)
    assert t["agents"] == ["planner", "coder", {"k": 1}]


# ---------- account payload / deletion ----------

def test_account_payload_and_delete_user():
    u = pg.create_user("sarah@example.com", "Sarah", "pw")
    pg.add_project(u["id"], "delmeplease", "D", "g")
    pg.record_payment_event("evt-del", "paddle", "payment.succeeded", u["id"], {"txn": "TX"})
    code = pg.new_telegram_link_code(u["id"])
    payload = pg.account_payload(u["id"])
    assert payload["user"]["email"] == "sarah@example.com"
    assert len(payload["projects"]) == 1
    assert len(payload["payment_events"]) == 1
    pg.delete_user(u["id"])
    assert pg.get_user_by_id(u["id"]) is None
    assert pg.get_telegram_link(u["id"]) is None
    assert pg.get_user_credits(u["id"]) == 0


# ---------- SQLite -> PG migration script ----------

def _make_sqlite_fixture():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, pw_hash TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'demo',
            credits INTEGER NOT NULL DEFAULT 3, ref_code TEXT UNIQUE NOT NULL, referred_by TEXT,
            created_at REAL NOT NULL, logged_out_at REAL);
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            board_slug TEXT NOT NULL, name TEXT NOT NULL, goal TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_code TEXT NOT NULL,
            referred_email TEXT NOT NULL, rewarded INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE squad_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER NOT NULL,
            name TEXT NOT NULL, description TEXT NOT NULL, agents TEXT NOT NULL,
            price_credits INTEGER NOT NULL DEFAULT 10, created_at REAL NOT NULL);
        CREATE TABLE template_purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, template_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE payment_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            gateway TEXT NOT NULL, kind TEXT NOT NULL, user_id INTEGER, detail TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL);
        CREATE TABLE telegram_links (telegram_chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            linked_at REAL NOT NULL);
        CREATE TABLE telegram_codes (code TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE password_resets (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE demo_usage (who TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (who, day));
        """
    )
    c.executemany(
        "INSERT INTO users (email,name,pw_hash,plan,credits,ref_code,referred_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("mig@a.com", "Mig A", "x", "demo", 3, "FLX-M1", None, 1700000000.0),
            ("mig@b.com", "Mig B", "y", "pro", 30, "FLX-M2", "FLX-M1", 1700000100.0),
        ],
    )
    c.execute("INSERT INTO projects (user_id,board_slug,name,goal,created_at) VALUES (1,'mig-board','B','g',1700000200.0)")
    c.commit()
    c.close()
    return Path(path)


def test_sqlite_to_postgres_migration_copy_and_idempotent():
    from scripts.migrate_sqlite_to_postgres import migrate_one

    src = _make_sqlite_fixture()
    try:
        async def run():
            pool = await asyncpg.create_pool(dsn=pg.DATABASE_URL, min_size=1, max_size=2)
            try:
                n_users = await migrate_one(pool, "users", src)
                n_proj = await migrate_one(pool, "projects", src)
                n2_users = await migrate_one(pool, "users", src)   # re-run: idempotent
                n2_proj = await migrate_one(pool, "projects", src)
                return n_users, n_proj, n2_users, n2_proj
            finally:
                await pool.close()
        u1, p1, u2, p2 = asyncio.run_coroutine_threadsafe(run(), pg._loop()).result()
    finally:
        src.unlink(missing_ok=True)
    assert (u1, p1) == (2, 1)
    assert (u2, p2) == (0, 0)          # ON CONFLICT DO NOTHING -> no dupes
    assert pg.get_user_by_email("mig@a.com")["credits"] == 3
    assert pg.get_user_by_email("mig@b.com")["plan"] == "pro"


# ---------- Phase F: provider usage accounting ledger ----------

def test_record_provider_usage_roundtrip():
    rid = pg.record_provider_usage(
        "demo", "paid_fallback", "gemini", "gemini-3.5-flash-lite", slug="flux-demo-1")
    assert rid >= 1
    s = pg.provider_usage_summary()
    assert s["total_attempts"] >= 1
    assert s["pending_attempts"] >= 1  # ok is NULL until finalized
    assert s["by_runtime_source"]["paid_fallback"]["attempts"] >= 1
    assert s["by_surface"]["demo"]["attempts"] >= 1


def test_update_provider_usage_outcome():
    pg.record_provider_usage("demo", "pool", "gemini", "m", slug="flux-demo-2")
    assert pg.update_provider_usage_outcome("flux-demo-2", ok=1) is True
    s = pg.provider_usage_summary()
    assert s["finalized"] >= 1
    assert s["ok"] >= 1
    assert s["pending_attempts"] == 0
    # a second update is a no-op (no open row left)
    assert pg.update_provider_usage_outcome("flux-demo-2", ok=0) is False


def test_update_targets_latest_open_row():
    pg.record_provider_usage("project", "byok", "anthropic", "claude", slug="u1-p")
    pg.record_provider_usage("project", "byok", "anthropic", "claude", slug="u1-p")
    assert pg.update_provider_usage_outcome("u1-p", ok=1) is True

    async def check():
        pool = await pg.get_pool()
        rows = await pool.fetch(
            "SELECT ok FROM provider_usage WHERE slug=$1 ORDER BY id", "u1-p")
        return [r["ok"] for r in rows]

    vals = asyncio.run_coroutine_threadsafe(check(), pg._loop()).result()
    assert vals == [None, 1]  # oldest attempt stays pending after reset_db baseline


def test_update_noop_when_no_open_row():
    assert pg.update_provider_usage_outcome("no-such-slug", ok=1) is False