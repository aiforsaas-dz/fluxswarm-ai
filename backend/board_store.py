"""Postgres mirror of board + workspace state (P4/2 persistence).

On the Render free tier the instance's local disk is NOT persistent: any
sleep/cold-start restart wipes ``HERMES_HOME/kanban/boards/<slug>`` (kanban.db
+ workspace artifacts) while the Neon ``projects`` rows survive, leaving dead
empty boards. This module snapshots each board (tasks, task_events, workspace
files, seal flag) into the same Postgres that holds the users, and lets a fresh
boot restore any board missing from disk exactly as it was.

Design rules (mirrors the rest of the thin path):

  * NEVER raise and NEVER block: every public function is best-effort and
    swallows its own errors, because persistence is a mirror, not a gate.
  * Only active when Postgres is configured (``FLUXSWARM_DATABASE_URL``
    starting with ``postgres``). Local SQLite development has a persistent
    disk, so the mirror is a no-op there.
  * Idempotent: snapshot + restore can be re-run freely.
  * :mod:`hermes_client` imports this module (never the reverse at import
    time); ``HERMES_HOME`` is read lazily from hermes_client at call time so
    tests that override it stay coherent.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_KANBAN_NAME = "kanban.db"
_SEAL_MARKER_NAME = "board.sealed"
_MAX_FILE_CHARS = 2_000_000  # 2 MB cap per stored artifact (sanity bound).


def _boards_root() -> Path:
    """The kanban boards root, resolved lazily from :mod:`hermes_client`.

    hermes_client imports this module (not the reverse, so no cycle) and owns
    ``HERMES_HOME`` as the single source of truth — including test overrides.
    """
    import hermes_client as hc  # lazy: avoid an import-time cycle
    return Path(hc.HERMES_HOME) / "kanban" / "boards"


def _board_dir(slug: str) -> Path:
    return _boards_root() / slug


def is_enabled() -> bool:
    """True when the Postgres mirror should run (production thin hosts)."""
    return (os.environ.get("FLUXSWARM_DATABASE_URL") or "").startswith("postgres")


def _kanban_path(slug: str) -> Path:
    return _board_dir(slug) / _KANBAN_NAME


def _kind_of(slug: str) -> str:
    return "demo" if slug.startswith("flux-demo-") else "project"


def _pg():
    import db_postgres  # lazy: only needed when the mirror is enabled
    return db_postgres


# ---------------------------------------------------------------------------
# Snapshot: SQLite board on disk -> Postgres mirror tables.
# ---------------------------------------------------------------------------

def _snapshot_tasks(slug: str) -> list[tuple]:
    db = _kanban_path(slug)
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db), timeout=5.0)
        try:
            rows = c.execute("SELECT * FROM tasks").fetchall()
        finally:
            c.close()
        return [tuple(r) for r in rows]
    except Exception:
        return []


def _snapshot_events(slug: str) -> list[tuple]:
    db = _kanban_path(slug)
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db), timeout=5.0)
        try:
            rows = c.execute("SELECT * FROM task_events").fetchall()
        finally:
            c.close()
        return [tuple(r) for r in rows]
    except Exception:
        return []


def _snapshot_files(slug: str) -> list[tuple]:
    """Walk the board dir, storing every non-kanban file as (rel_path, content)."""
    root = _board_dir(slug)
    out: list[tuple[str, str]] = []
    try:
        if not root.is_dir():
            return []
        for p in root.rglob("*"):
            if not p.is_file() or p.name == _KANBAN_NAME or p.name == _SEAL_MARKER_NAME:
                continue
            try:
                rel = p.relative_to(root).as_posix()
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > _MAX_FILE_CHARS:
                    text = text[:_MAX_FILE_CHARS]
                out.append((rel, text))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _seal_flag(slug: str) -> int:
    try:
        return 1 if (_board_dir(slug) / _SEAL_MARKER_NAME).exists() else 0
    except Exception:
        return 0


def snapshot_board(slug: str, kind: str | None = None) -> None:
    """Mirror one board's current on-disk state into Postgres. Best-effort."""
    if not is_enabled():
        return
    try:
        pg = _pg()
        kind = kind or _kind_of(slug)
        now = int(time.time())
        tasks = _snapshot_tasks(slug)
        events = _snapshot_events(slug)
        files = _snapshot_files(slug)
        sealed = _seal_flag(slug)

        async def _do():
            pool = await pg.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM board_tasks WHERE board_slug=$1", slug)
                    await conn.execute(
                        "DELETE FROM board_task_events WHERE board_slug=$1", slug)
                    await conn.execute(
                        "DELETE FROM board_files WHERE board_slug=$1", slug)
                    await conn.execute(
                        """INSERT INTO board_states (board_slug, kind, sealed,
                               created_at, updated_at)
                           VALUES ($1,$2,$3,$4,$5)
                           ON CONFLICT (board_slug) DO UPDATE SET
                               kind=EXCLUDED.kind, sealed=EXCLUDED.sealed,
                               updated_at=EXCLUDED.updated_at""",
                        slug, kind, sealed, now, now)
                    if tasks:
                        await conn.executemany(
                            """INSERT INTO board_tasks
                               (board_slug, task_id, title, assignee, status,
                                created_at, started_at, completed_at,
                                last_heartbeat_at, result, worker_pid)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                            [tuple([slug] + list(base)) for base in tasks])
                    if events:
                        # sqlite rows are (id, task_id, kind, payload, created_at);
                        # PG regenerates its own identity id, so the mirror stores
                        # (slug, task_id, kind, payload, created_at).
                        await conn.executemany(
                            """INSERT INTO board_task_events
                               (board_slug, task_id, kind, payload, created_at)
                               VALUES ($1, $2, $3, $4, $5)""",
                            [(slug, base[1], base[2], base[3], base[4])
                             for base in events])
                    if files:
                        await conn.executemany(
                            """INSERT INTO board_files
                               (board_slug, rel_path, content, updated_at)
                               VALUES ($1, $2, $3, $4)""",
                            [(slug, rel, content, now) for rel, content in files])

        pg._await(_do())
    except Exception:
        pass


def purge_board(slug: str) -> None:
    """Drop every mirror row for one board (demo cleanup / account erasure)."""
    if not is_enabled():
        return
    try:
        pg = _pg()

        async def _do():
            pool = await pg.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for table in ("board_tasks", "board_task_events",
                                  "board_files", "board_states"):
                        await conn.execute(
                            f"DELETE FROM {table} WHERE board_slug=$1", slug)

        pg._await(_do())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Restore: Postgres mirror -> missing SQLite board on disk.
# ---------------------------------------------------------------------------

def _restore_kanban_db(slug: str, tasks: list[tuple],
                       events: list[tuple]) -> None:
    """Recreate the board's kanban.db with the Hermes schema + mirrored rows."""
    db = _kanban_path(slug)
    db.parent.mkdir(parents=True, exist_ok=True)
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
        if tasks:
            c.executemany(
                """INSERT OR REPLACE INTO tasks
                   (id, title, assignee, status, created_at, started_at,
                    completed_at, last_heartbeat_at, result, worker_pid)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", tasks)
        if events:
            c.executemany(
                """INSERT OR REPLACE INTO task_events
                   (id, task_id, kind, payload, created_at)
                   VALUES (?,?,?,?,?)""", events)
        c.commit()
    finally:
        c.close()


def restore_missing_boards() -> int:
    """Recreate from Postgres every board missing on disk; returns count.

    A board is 'missing' when its kanban.db does not exist (the board dir may
    have survived partially). Each restored board gets its kanban.db,
    workspace artifacts and seal marker re-created, so the filesystem-based
    board discovery, the /ws live stream and the workspace API all work again
    untouched after a restart wiped the local disk.
    """
    if not is_enabled():
        return 0
    pg = _pg()
    restored = 0

    def _scalar(v):
        """asyncpg values -> plain scalars the SQLite driver can bind."""
        return None if v is None else int(v) if isinstance(v, (int, float)) else str(v)

    async def _exec():
        nonlocal restored
        pool = await pg.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT board_slug, kind, sealed FROM board_states ORDER BY board_slug")
                for r in rows:
                    slug = r["board_slug"]
                    if _kanban_path(slug).exists():
                        continue
                    tasks = await conn.fetch(
                        """SELECT task_id, title, assignee, status, created_at,
                                  started_at, completed_at, last_heartbeat_at,
                                  result, worker_pid
                           FROM board_tasks WHERE board_slug=$1
                           ORDER BY created_at""", slug)
                    events = await conn.fetch(
                        """SELECT id, task_id, kind, payload, created_at
                           FROM board_task_events WHERE board_slug=$1
                           ORDER BY id""", slug)
                    files = await conn.fetch(
                        "SELECT rel_path, content FROM board_files WHERE board_slug=$1",
                        slug)
                    _restore_kanban_db(
                        slug,
                        [tuple(_scalar(x) for x in t.values()) for t in tasks],
                        [tuple(_scalar(x) for x in e.values()) for e in events])
                    root = _board_dir(slug).resolve()
                    for f in files:
                        try:
                            rel = f["rel_path"] or ""
                            p = (_board_dir(slug) / rel).resolve()
                            if not str(p).startswith(str(root) + os.sep):
                                continue
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(f["content"] or "", encoding="utf-8")
                        except Exception:
                            continue
                    if r["sealed"]:
                        try:
                            (_board_dir(slug) / _SEAL_MARKER_NAME).touch()
                        except Exception:
                            pass
                    restored += 1

    try:
        pg._await(_exec())
    except Exception:
        return 0
    return restored


def list_mirrored_slugs() -> list[str]:
    """All board slugs currently mirrored (used by tests / diagnostics)."""
    if not is_enabled():
        return []
    pg = _pg()
    try:

        async def _fetch():
            pool = await pg.get_pool()
            rows = await pool.fetch(
                "SELECT board_slug FROM board_states ORDER BY board_slug")
            return [r["board_slug"] for r in rows]

        return pg._await(_fetch())
    except Exception:
        return []