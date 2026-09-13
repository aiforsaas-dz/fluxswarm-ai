"""P4/2 board & workspace persistence tests.

Boards (tasks + task_events) and workspace artifacts live on the instance's
non-persistent local disk; on the free host a restart wipes them while Neon
projects survive. ``board_store`` mirrors every board into Postgres and
restores any board missing from disk on boot. These tests lock that round-trip.

Require a live PostgreSQL (FLUXSWARM_DATABASE_URL), same convention as
tests/test_db_postgres.py.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault(
    "FLUXSWARM_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/fluxswarm_test",
)

import board_store  # noqa: E402
import hermes_client as hc  # noqa: E402

import db_postgres as pg  # noqa: E402


@pytest.fixture(autouse=True)
def clean_pg():
    pg.init_db()
    pg.reset_db()
    yield
    pg.reset_db()


def _mk_board(tmp_path, monkeypatch, slug="u1-p1", custom_agents=None):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    hc.launch_project_thin(slug, "Build a web app", custom_agents=custom_agents or [])
    return slug


def _board_tasks(tmp_path, slug):
    c = sqlite3.connect(str(tmp_path / "kanban" / "boards" / slug / "kanban.db"))
    try:
        return c.execute("SELECT id, assignee, status, title FROM tasks ORDER BY id").fetchall()
    finally:
        c.close()


def test_is_enabled_tracks_pg_url(monkeypatch):
    monkeypatch.setenv("FLUXSWARM_DATABASE_URL", "postgresql://x")
    assert board_store.is_enabled() is True
    monkeypatch.setenv("FLUXSWARM_DATABASE_URL", "")
    assert board_store.is_enabled() is False


def test_snapshot_restore_roundtrip(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    before = _board_tasks(tmp_path, slug)
    assert before  # squad lanes exist

    board_store.snapshot_board(slug)
    assert slug in board_store.list_mirrored_slugs()

    # Simulate a free-tier restart: local disk wiped, Neon survives.
    shutil.rmtree(str(tmp_path / "kanban" / "boards" / slug))
    assert not (tmp_path / "kanban" / "boards" / slug).exists()

    n = board_store.restore_missing_boards()
    assert n == 1
    after = _board_tasks(tmp_path, slug)
    assert [t[:3] for t in after] == [t[:3] for t in before]  # identical queue


def test_snapshot_restore_keeps_workspace_files(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    ws = hc.project_workspace_dir(slug)
    (ws / "PLAN.md").write_text("# plan\nobjective\n", encoding="utf-8")
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    board_store.snapshot_board(slug)
    shutil.rmtree(str(tmp_path / "kanban" / "boards" / slug))

    board_store.restore_missing_boards()
    assert (ws / "PLAN.md").read_text(encoding="utf-8") == "# plan\nobjective\n"
    assert (ws / "tests" / "test_app.py").read_text(encoding="utf-8") == (
        "def test_x():\n    assert True\n")


def test_snapshot_restore_marks_seal(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    hc.seal_board(slug)
    assert hc.board_is_sealed(slug)

    board_store.snapshot_board(slug)
    shutil.rmtree(str(tmp_path / "kanban" / "boards" / slug))

    board_store.restore_missing_boards()
    assert hc.board_is_sealed(slug)


def test_snapshot_does_not_mirror_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXSWARM_DATABASE_URL", "")
    slug = _mk_board(tmp_path, monkeypatch)
    board_store.snapshot_board(slug)
    assert board_store.list_mirrored_slugs() == []


def test_purge_removes_mirror(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    board_store.snapshot_board(slug)
    assert slug in board_store.list_mirrored_slugs()

    board_store.purge_board(slug)
    assert board_store.list_mirrored_slugs() == []
    # purge must not touch the on-disk board
    assert _board_tasks(tmp_path, slug)


def test_idempotent_snapshot_same_rows(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    board_store.snapshot_board(slug)
    first = len(board_store._snapshot_tasks(slug))
    board_store.snapshot_board(slug)

    async def _count():
        pool = await pg.get_pool()
        return (await pool.fetchrow(
            "SELECT COUNT(*) AS n FROM board_tasks WHERE board_slug=$1", slug))["n"]

    # Snapshot replaces rows wholesale, never duplicates.
    assert pg._await(_count()) == first


def test_restore_skips_existing_boards(tmp_path, monkeypatch):
    slug = _mk_board(tmp_path, monkeypatch)
    board_store.snapshot_board(slug)
    assert board_store.restore_missing_boards() == 0  # already on disk