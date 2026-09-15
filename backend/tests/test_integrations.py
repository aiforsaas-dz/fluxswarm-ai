"""Tests for the optional knowledge-graph integrations (Cognee + Understand Anything).

Both tools are opt-in and best-effort; all integration paths are tested
in their "not installed / disabled" state so the test suite stays green
without the heavy Cognee pip deps or the Node-based UA runtime.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import integrations
import hermes_client as hc
import main as main_mod


def _seed_test_board(tmp_path, monkeypatch, slug="inttest", tasks=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    board_dir = Path(tmp_path) / "kanban" / "boards" / slug
    board_dir.mkdir(parents=True)
    db_path = board_dir / "kanban.db"
    c = sqlite3.connect(str(db_path))
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, board TEXT, title TEXT, status TEXT,
        assignee TEXT, created_at INTEGER, completed_at INTEGER,
        result TEXT, worker_pid INTEGER, claim_lock TEXT, claim_expires REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
        payload TEXT, created_at REAL
    )""")
    for t in (tasks or []):
        c.execute(
            "INSERT INTO tasks (id, board, title, status, assignee, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (t["id"], slug, t.get("title", t["id"]), t.get("status", "done"),
             t.get("assignee", "worker"), 1000.0),
        )
    c.commit()
    c.close()
    att_dir = board_dir / "attachments"
    att_dir.mkdir()
    (att_dir / "spec.txt").write_text("hello spec")
    return slug


class TestCollectBoardKnowledge:
    def test_reads_tasks_and_attachments(self, tmp_path, monkeypatch):
        slug = _seed_test_board(tmp_path, monkeypatch, tasks=[
            {"id": "t1", "title": "Plan", "status": "done", "assignee": "planner"},
            {"id": "t2", "title": "Build", "status": "running", "assignee": "builder"},
        ])
        knowledge = integrations.collect_board_knowledge(slug)
        assert knowledge["board"] == slug
        assert len(knowledge["tasks"]) == 2
        assert knowledge["attachments"] == [{"name": "spec.txt", "size": len("hello spec")}]

    def test_missing_board_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
        k = integrations.collect_board_knowledge("no-such-board")
        assert k["tasks"] == []
        assert k["attachments"] == []


class TestSeedCogneeMemory:
    def test_skipped_when_env_off(self, monkeypatch):
        monkeypatch.delenv(integrations._ENV_COGNEE, raising=False)
        report = integrations.seed_cognee_memory("any")
        assert report["skipped"] is True

    def test_skipped_when_cognee_unavailable(self, monkeypatch):
        monkeypatch.setenv(integrations._ENV_COGNEE, "1")
        monkeypatch.setattr(integrations, "_cognee_available", lambda: False)
        report = integrations.seed_cognee_memory("any")
        assert report["skipped"] is True

    def test_error_caught_when_seed_fails(self, monkeypatch, tmp_path):
        monkeypatch.setenv(integrations._ENV_COGNEE, "1")
        fake_mod = types.ModuleType("cognee")
        fake_mod.add = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        fake_mod.cognify = lambda *a, **kw: None
        sys.modules["cognee"] = fake_mod
        try:
            monkeypatch.setattr(integrations, "_cognee_available", lambda: True)
            monkeypatch.setattr(integrations, "collect_board_knowledge",
                                lambda b: {"board": b, "tasks": [], "events": [], "attachments": []})
            report = integrations.seed_cognee_memory("x")
            assert "error" in report
        finally:
            del sys.modules["cognee"]


class TestRunUnderstandAnything:
    def test_skipped_when_env_off(self, monkeypatch):
        monkeypatch.delenv(integrations._ENV_UA, raising=False)
        report = integrations.run_understand_anything("any")
        assert report["skipped"] is True

    def test_skipped_when_env_on_but_uua_unavailable(self, monkeypatch):
        monkeypatch.setenv(integrations._ENV_UA, "1")
        monkeypatch.setattr(integrations, "_ua_available", lambda: False)
        report = integrations.run_understand_anything("any")
        assert report["skipped"] is True

    def test_workspace_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setenv(integrations._ENV_UA, "1")
        monkeypatch.setattr(integrations, "_ua_available", lambda: True)
        report = integrations.run_understand_anything("any", workspace="/nonexistent")
        assert report["skipped"] is True
        assert "not found" in report["reason"]


class TestOnProjectCompleted:
    def test_never_raises_even_on_errors(self, monkeypatch):
        monkeypatch.setattr(integrations, "seed_cognee_memory",
                            lambda b: (_ for _ in ()).throw(RuntimeError("cognee boom")))
        monkeypatch.setattr(integrations, "run_understand_anything",
                            lambda b, ws=None, **kw: (_ for _ in ()).throw(RuntimeError("ua boom")))
        report = integrations.on_project_completed("board-x")
        assert "cognee" in report
        assert "understand_anything" in report
        assert report["cognee"]["error"] == "cognee boom"
        assert report["understand_anything"]["error"] == "ua boom"

    def test_success_when_both_skipped(self, monkeypatch):
        monkeypatch.delenv(integrations._ENV_COGNEE, raising=False)
        monkeypatch.delenv(integrations._ENV_UA, raising=False)
        report = integrations.on_project_completed("board-y")
        assert report["cognee"]["skipped"] is True
        assert report["understand_anything"]["skipped"] is True


class TestFinalizeHookIntegration:
    def test_finalize_calls_on_project_completed(self, tmp_path, monkeypatch):
        slug = _seed_test_board(tmp_path, monkeypatch, tasks=[
            {"id": "t1", "title": "Plan", "status": "done"},
        ])
        called_with = []
        monkeypatch.setattr(
            integrations, "on_project_completed",
            lambda b, **kw: called_with.append(b) or {"cognee": {}, "understand_anything": {}},
        )
        monkeypatch.delenv(integrations._ENV_COGNEE, raising=False)
        monkeypatch.delenv(integrations._ENV_UA, raising=False)
        main_mod._finalize_launch(slug, 9999, status="ok", outcome="converged", reason="")
        assert called_with == [slug]

    def test_finalize_skips_hook_on_stuck(self, tmp_path, monkeypatch):
        slug = _seed_test_board(tmp_path, monkeypatch)
        called = []
        monkeypatch.setattr(
            integrations, "on_project_completed",
            lambda b, **kw: called.append(b),
        )
        monkeypatch.setattr(main_mod.hc, "board_has_completed_work", lambda b: False)
        monkeypatch.setattr(main_mod.db, "refund_launch_credit", lambda uid: False)
        monkeypatch.setattr(main_mod.db, "set_launch_outcome", lambda *a, **kw: None)
        monkeypatch.setattr(main_mod.db, "update_provider_usage_outcome", lambda *a, **kw: None)
        monkeypatch.setattr(main_mod.hc, "seal_board", lambda b, **kw: {"killed": 0, "blocked": 0})
        monkeypatch.setattr(main_mod.audit, "audit", lambda *a, **kw: None)
        main_mod._finalize_launch(slug, 9998, status="stuck", outcome="stuck", reason="timeout")
        assert called == []
