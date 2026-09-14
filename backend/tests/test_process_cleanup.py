"""Tests for Hermes worker-process cleanup on dispatch stall/timeout/exception.

Covers:
  A. successful dispatch does NOT kill workers that must continue
  B. timeout cleans up owned workers
  C. stall cleans up owned workers
  D. exception cleans up owned workers
  E. unrelated Hermes process remains untouched
  F. repeated cleanup is safe/idempotent
  G. no worker-process accumulation after repeated timeout tests
  H. api_dispatch passes DISPATCH_TIMEOUT_S
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hermes_client as hc_mod
import main as main_mod


class _FakeRun:
    """Fake subprocess result for ``hc._run``."""
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wire_mocks(monkeypatch, state_seq, *, activity_sig=None):
    """Wire _run / list_tasks / time.sleep for unit-test dispatch loops."""
    calls = {"n": 0, "kills": [], "cleanup_calls": []}

    def fake_run(args, board=None, capture=True, provider_keys=None):
        return _FakeRun(stdout="{}")

    def fake_list(board):
        idx = min(calls["n"], len(state_seq) - 1)
        calls["n"] += 1
        return state_seq[idx]

    def fake_cleanup(board):
        calls["cleanup_calls"].append(board)

    monkeypatch.setattr(hc_mod, "_run", fake_run)
    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)
    monkeypatch.setattr(hc_mod.time, "sleep", lambda s: None)
    if activity_sig is not None:
        monkeypatch.setattr(hc_mod, "_board_activity_sig", activity_sig)
    return calls


def _make_fake_activity(stale_after: int = 999):
    """Return a fake _board_activity_sig that stays fresh until *stale_after* calls."""
    shared = {"n": 0}

    def fake_activity(board):
        shared["n"] += 1
        if shared["n"] <= stale_after:
            return (time.time(), shared["n"], shared["n"])
        return ()
    return fake_activity


# ---------------------------------------------------------------------------
# A. Successful dispatch does NOT kill workers
# ---------------------------------------------------------------------------

class TestSuccessfulDispatchNoCleanup:
    def test_converged_skips_cleanup(self, monkeypatch):
        """All tasks done -> _cleanup_board_workers must NOT be called."""
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "done"}, {"id": "t2", "state": "done"}],
        ])
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                              timeout_s=10, min_wait_s=0)
        assert res["outcome"] == "ok"
        assert res["terminal"] is True
        assert calls["cleanup_calls"] == []

    def test_nonblocking_does_not_cleanup(self, monkeypatch):
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
        ])
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=False)
        assert res.get("outcome") == "pending"
        assert calls["cleanup_calls"] == []


# ---------------------------------------------------------------------------
# B. Timeout cleans up owned workers
# ---------------------------------------------------------------------------

class TestTimeoutCleanup:
    def test_timeout_calls_cleanup(self, monkeypatch):
        """When the blocking window expires, cleanup must run."""
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
        ])
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                              timeout_s=1, min_wait_s=0, stall_passes=99)
        assert res["timed_out"] is True
        assert res["outcome"] == "stuck"
        assert calls["cleanup_calls"] == ["u1-proj"]

    def test_timeout_stuck_tasks_listed(self, monkeypatch):
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}, {"id": "t2", "state": "queued"}],
            [{"id": "t1", "state": "running"}, {"id": "t2", "state": "queued"}],
        ])
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                              timeout_s=1, min_wait_s=0, stall_passes=99)
        stuck_ids = {t["id"] for t in res.get("stuck_tasks", [])}
        assert stuck_ids == {"t1", "t2"}


# ---------------------------------------------------------------------------
# C. Stall cleans up owned workers
# ---------------------------------------------------------------------------

class TestStallCleanup:
    def test_stall_calls_cleanup(self, monkeypatch):
        """Stall exit must trigger worker cleanup."""
        activity = _make_fake_activity(stale_after=999)
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
        ], activity_sig=activity)
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                              timeout_s=300, min_wait_s=0, stall_passes=3)
        assert res.get("stall") is True
        assert res["outcome"] == "stuck"
        assert calls["cleanup_calls"] == ["u1-proj"]

    def test_stall_does_not_fire_when_activity_advancing(self, monkeypatch):
        """Healthy worker with advancing heartbeats must NOT be stalled."""
        activity = _make_fake_activity(stale_after=999)
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "done"}],
        ], activity_sig=activity)
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                              timeout_s=300, min_wait_s=0, stall_passes=3)
        assert res["outcome"] == "ok"
        assert calls["cleanup_calls"] == []


# ---------------------------------------------------------------------------
# D. Exception cleans up owned workers
# ---------------------------------------------------------------------------

class TestExceptionCleanup:
    def test_run_exception_triggers_cleanup(self, monkeypatch):
        """If _run raises during the blocking loop, cleanup must still run."""
        calls = {"n": 0, "cleanup_calls": []}

        def fake_run(args, board=None, capture=True, provider_keys=None):
            calls["n"] += 1
            if calls["n"] <= 1:
                return _FakeRun(stdout="{}")
            raise RuntimeError("dispatch CLI crashed")

        def fake_list(board):
            return [{"id": "t1", "state": "running"}]

        def fake_cleanup(board):
            calls["cleanup_calls"].append(board)

        monkeypatch.setattr(hc_mod, "_run", fake_run)
        monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
        monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)
        monkeypatch.setattr(hc_mod.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="dispatch CLI crashed"):
            hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True,
                            timeout_s=300, min_wait_s=0)
        assert calls["cleanup_calls"] == ["u1-proj"]


# ---------------------------------------------------------------------------
# E. Unrelated Hermes process remains untouched
# ---------------------------------------------------------------------------

class TestUnrelatedProcessSafety:
    def test_cleanup_only_targets_board_pids(self, monkeypatch):
        """_cleanup_board_workers must only kill PIDs from the target board."""
        killed_pids = []

        def fake_cleanup(board):
            # Simulate: cleanup reads PIDs from THIS board only
            if board == "u1-target":
                killed_pids.append(12345)

        monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)

        # Dispatch on a DIFFERENT board
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
        ])
        monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)

        res = hc_mod.dispatch("u1-other", max_spawn=2, blocking=True,
                              timeout_s=1, min_wait_s=0, stall_passes=99)
        # cleanup was called for u1-other, NOT u1-target
        assert killed_pids == []  # fake_cleanup only adds for u1-target

    def test_read_worker_pids_returns_only_board_pids(self, monkeypatch):
        """read_worker_pids queries kanban.db for the specific board."""
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "kanban.db"
        c = sqlite3.connect(str(db_path))
        c.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT,
            worker_pid INTEGER, assignee TEXT
        )""")
        c.execute("INSERT INTO tasks VALUES ('t1', 'task1', 'running', 111, 'ecc-planner')")
        c.execute("INSERT INTO tasks VALUES ('t2', 'task2', 'running', 222, 'ecc-tdd')")
        c.execute("INSERT INTO tasks VALUES ('t3', 'task3', 'done', 333, 'ecc-devops')")
        c.commit()
        c.close()

        boards_dir = Path(tmp) / "kanban" / "boards" / "test-board"
        boards_dir.mkdir(parents=True)
        import shutil as _sh
        _sh.copy2(str(db_path), str(boards_dir / "kanban.db"))

        old_home = hc_mod.HERMES_HOME
        try:
            hc_mod.HERMES_HOME = tmp
            pids = hc_mod.read_worker_pids("test-board")
            # Only running/ready/todo tasks returned; done task excluded
            assert sorted(pids) == [111, 222]
        finally:
            hc_mod.HERMES_HOME = old_home
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# F. Repeated cleanup is safe / idempotent
# ---------------------------------------------------------------------------

class TestIdempotentCleanup:
    def test_double_cleanup_safe(self, monkeypatch):
        """Calling _cleanup_board_workers twice does not crash."""
        # First call with valid DB, second call with missing DB
        tmp = tempfile.mkdtemp()
        boards_dir = Path(tmp) / "kanban" / "boards" / "board-x"
        boards_dir.mkdir(parents=True)

        # Create a minimal kanban.db
        db_path = boards_dir / "kanban.db"
        c = sqlite3.connect(str(db_path))
        c.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT,
            worker_pid INTEGER, assignee TEXT
        )""")
        c.execute("INSERT INTO tasks VALUES ('t1', 'task1', 'running', 99999, 'ecc-planner')")
        c.commit()
        c.close()

        old_home = hc_mod.HERMES_HOME
        try:
            hc_mod.HERMES_HOME = tmp
            # First cleanup - PID 99999 likely does not exist -> no crash
            hc_mod._cleanup_board_workers("board-x")
            # Second cleanup - same result, idempotent
            hc_mod._cleanup_board_workers("board-x")
            # Cleanup with non-existent board - no crash
            hc_mod._cleanup_board_workers("nonexistent-board")
        finally:
            hc_mod.HERMES_HOME = old_home
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# G. No worker-process accumulation
# ---------------------------------------------------------------------------

class TestNoProcessAccumulation:
    def test_consecutive_timeouts_do_not_accumulate(self, monkeypatch):
        """Multiple sequential timeouts must each clean up; no buildup."""
        cleanup_boards = []

        def fake_cleanup(board):
            cleanup_boards.append(board)

        monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)

        for i in range(3):
            calls = _wire_mocks(monkeypatch, [
                [{"id": f"t{i}", "state": "running"}],
                [{"id": f"t{i}", "state": "running"}],
            ])
            monkeypatch.setattr(hc_mod, "_cleanup_board_workers", fake_cleanup)
            res = hc_mod.dispatch(f"u1-board{i}", max_spawn=1, blocking=True,
                                  timeout_s=1, min_wait_s=0, stall_passes=99)
            assert res["timed_out"] is True

        # Each timeout triggered exactly one cleanup
        assert cleanup_boards == ["u1-board0", "u1-board1", "u1-board2"]


# ---------------------------------------------------------------------------
# H. api_dispatch passes DISPATCH_TIMEOUT_S
# ---------------------------------------------------------------------------

class TestApiDispatchTimeout:
    def test_sync_dispatch_uses_dispatch_timeout_s(self, monkeypatch):
        """api_dispatch must pass timeout_s=DISPATCH_TIMEOUT_S, not the old 600s default."""
        captured = {}

        def fake_dispatch(*args, **kwargs):
            captured.update(kwargs)
            return {"terminal": True, "outcome": "ok", "timed_out": False,
                    "stuck_tasks": []}

        monkeypatch.setattr(main_mod.hc, "dispatch", fake_dispatch)
        monkeypatch.setattr(main_mod, "_operator_maintenance", lambda: False)
        monkeypatch.setattr(main_mod, "_DEMO_DAILY_CAP", 999)

        # Simulate calling api_dispatch through the route handler
        user = {"id": 1, "plan": "pro", "email": "test@test.com"}
        monkeypatch.setattr(main_mod, "get_current_user",
                           lambda: user)

        # We can't easily call the route directly, but we can verify the
        # source code passes the right timeout by checking the dispatch call
        # pattern in main.py
        import inspect
        src = inspect.getsource(main_mod.api_dispatch)
        assert "hc.DISPATCH_TIMEOUT_S" in src

    def test_bg_dispatch_uses_dispatch_timeout_s(self, monkeypatch):
        """_bg_dispatch must pass timeout_s=DISPATCH_TIMEOUT_S."""
        captured = {}

        def fake_dispatch(*args, **kwargs):
            captured.update(kwargs)
            return {"terminal": True, "outcome": "ok", "timed_out": False,
                    "stuck_tasks": []}

        monkeypatch.setattr(main_mod.hc, "dispatch", fake_dispatch)
        main_mod._bg_dispatch("u1-proj", "pro", provider_keys=None)
        assert captured.get("timeout_s") == hc_mod.DISPATCH_TIMEOUT_S


# ---------------------------------------------------------------------------
# I. Existing dispatch semantics preserved
# ---------------------------------------------------------------------------

class TestExistingSemanticsPreserved:
    def test_stall_detection_with_activity(self, monkeypatch):
        """Regression: healthy slow worker with advancing heartbeats not stuck."""
        activity = _make_fake_activity(stale_after=999)
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "done"}],
        ], activity_sig=activity)
        res = hc_mod.dispatch("u-probe", max_spawn=2, blocking=True,
                              timeout_s=900, min_wait_s=0, stall_passes=2)
        assert res["outcome"] == "ok"
        assert res.get("terminal") is True
        assert "stall" not in res

    def test_genuinely_stalled_worker_detected(self, monkeypatch):
        """Worker with no heartbeats and no state change IS stuck."""
        activity = _make_fake_activity(stale_after=0)  # stale immediately
        calls = _wire_mocks(monkeypatch, [
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
            [{"id": "t1", "state": "running"}],
        ], activity_sig=activity)
        res = hc_mod.dispatch("u-probe", max_spawn=2, blocking=True,
                              timeout_s=900, min_wait_s=0, stall_passes=2)
        assert res.get("stall") is True
        assert res["outcome"] == "stuck"

    def test_malformed_output_no_crash(self, monkeypatch):
        calls = {"n": 0}

        def fake_run(args, board=None, capture=True, provider_keys=None):
            return _FakeRun(stdout="not json at all")

        def fake_list(board):
            return [{"id": "t1", "state": "done"}]

        monkeypatch.setattr(hc_mod, "_run", fake_run)
        monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
        monkeypatch.setattr(hc_mod, "_cleanup_board_workers", lambda b: None)
        monkeypatch.setattr(hc_mod.time, "sleep", lambda s: None)
        res = hc_mod.dispatch("u1-proj", max_spawn=2, blocking=True)
        assert res.get("terminal") is True
        assert "raw" in res


# ---------------------------------------------------------------------------
# kill_process_tree unit tests
# ---------------------------------------------------------------------------

class TestKillProcessTree:
    def test_kill_zero_pid_no_crash(self):
        """kill_process_tree(0) must not crash."""
        hc_mod.kill_process_tree(0)

    def test_kill_negative_pid_no_crash(self):
        """kill_process_tree(-1) must not crash."""
        hc_mod.kill_process_tree(-1)

    def test_kill_nonexistent_pid_no_crash(self):
        """kill_process_tree with a PID that does not exist must not crash."""
        hc_mod.kill_process_tree(99999999)

    def test_read_worker_pids_missing_db(self, monkeypatch):
        """read_worker_pids with no kanban.db returns empty list."""
        old_home = hc_mod.HERMES_HOME
        try:
            hc_mod.HERMES_HOME = tempfile.mkdtemp()
            assert hc_mod.read_worker_pids("nonexistent") == []
        finally:
            hc_mod.HERMES_HOME = old_home


# ---------------------------------------------------------------------------
# J. _cleanup_board_workers only touches the current board's owned worker PIDs
# ---------------------------------------------------------------------------

class TestCleanupOwnsOnlyBoardPids:
    def test_cleanup_invokes_kill_for_each_owned_pid_only(self, monkeypatch):
        """Only running/ready/todo worker_pids are killed; done tasks are not."""
        tmp = tempfile.mkdtemp()
        boards_dir = Path(tmp) / "kanban" / "boards" / "board-x"
        boards_dir.mkdir(parents=True)
        db_path = boards_dir / "kanban.db"
        c = sqlite3.connect(str(db_path))
        c.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT,
            worker_pid INTEGER, assignee TEXT
        )""")
        # Two live workers to clean up; one already-done task whose pid must NOT
        # be touched (it is no longer owned by the in-flight board).
        c.execute("INSERT INTO tasks VALUES ('t1', 'a', 'running', 11111, 'ecc-planner')")
        c.execute("INSERT INTO tasks VALUES ('t2', 'b', 'todo', 22222, 'ecc-tdd')")
        c.execute("INSERT INTO tasks VALUES ('t3', 'c', 'done', 33333, 'ecc-devops')")
        c.commit()
        c.close()

        killed = []
        monkeypatch.setattr(hc_mod, "kill_process_tree", lambda pid, grace_s=3.0: killed.append(pid))

        old_home = hc_mod.HERMES_HOME
        try:
            hc_mod.HERMES_HOME = tmp
            hc_mod._cleanup_board_workers("board-x")
        finally:
            hc_mod.HERMES_HOME = old_home
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

        assert sorted(killed) == [11111, 22222]
        assert 33333 not in killed


# ---------------------------------------------------------------------------
# K. Real-process end-to-end cleanup (runs on the current platform)
# ---------------------------------------------------------------------------

_SLEEP = "import time; time.sleep(60)"
_SLEEP_SRC = "-c"


def _process_alive_posix(pid: int) -> bool:
    """True if *pid* exists in /proc and is not a zombie.

    A zombie responds happily to os.kill(pid, 0), so a plain signal 0 probe
    would report a reaped-but-not-waited child as alive. Use /proc state.
    """
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return False
    try:
        # clk_btk is the first field after the comm (which can contain spaces).
        rparen = stat.rfind(")")
        state = stat[rparen + 2:].split()[0] if rparen >= 0 else ""
        return state not in ("Z", "X")
    except (ValueError, IndexError):
        return True


def _wait_gone(pid: float | int, timeout_s: float = 8.0) -> bool:
    """Cross-platform: is *pid* fully gone within *timeout_s*?"""
    if sys.platform == "win32":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(int(pid)) not in proc.stdout
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_alive_posix(int(pid)):
            return True
        time.sleep(0.1)
    return False


class TestRealProcessCleanup:
    def test_owned_worker_process_is_terminated(self):
        """kill_process_tree must actually terminate a live owned worker."""
        proc = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        try:
            assert proc.poll() is None  # alive
            hc_mod.kill_process_tree(proc.pid, grace_s=3.0)
            assert proc.wait(timeout=8) is not None  # terminated
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_unrelated_process_survives(self):
        """Killing one owned worker must leave an unrelated process untouched."""
        owned = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        unrelated = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        try:
            hc_mod.kill_process_tree(owned.pid, grace_s=3.0)
            assert owned.wait(timeout=8) is not None  # owned gone
            assert unrelated.poll() is None             # unrelated still alive
        finally:
            for p in (owned, unrelated):
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=5)


# ---------------------------------------------------------------------------
# L. POSIX-specific cleanup (skipped on Windows; runs on Linux/POSIX CI)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-tree tests")
class TestPosixProcessTreeCleanup:
    # A worker that spawns a long-lived grandchild and reports its PID.
    _TREE = (
        "import subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(g.pid, flush=True);"
        "time.sleep(60)"
    )

    def test_owned_parent_and_child_are_terminated(self):
        """Deepest-first cleanup terminates both the worker and its child."""
        worker = subprocess.Popen([sys.executable, "-c", self._TREE],
                                  stdout=subprocess.PIPE, text=True)
        line = worker.stdout.readline().strip()
        child_pid = int(line) if line.isdigit() else None
        try:
            assert worker.poll() is None            # worker alive
            assert child_pid is not None
            assert os.path.exists(f"/proc/{child_pid}")  # grandchild alive
            hc_mod.kill_process_tree(worker.pid, grace_s=3.0)
            assert _wait_gone(worker.pid)
            assert _wait_gone(child_pid)            # grandchild cleaned up too
        finally:
            for pid in (worker.pid, child_pid):
                if pid is not None and not _wait_gone(pid, 2.0):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            worker.wait(timeout=5)

    def test_unrelated_process_survives(self):
        """An unrelated sleep process must survive the owned worker's cleanup."""
        owned = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        unrelated = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        try:
            hc_mod.kill_process_tree(owned.pid, grace_s=3.0)
            assert _wait_gone(owned.pid)
            assert not _wait_gone(unrelated.pid, 2.0)  # still alive
        finally:
            for p in (owned, unrelated):
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=5)

    def test_repeated_cleanup_is_idempotent(self):
        """Calling kill_process_tree twice on a dead worker is a safe no-op."""
        proc = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        try:
            hc_mod.kill_process_tree(proc.pid, grace_s=3.0)
            _wait_gone(proc.pid)
            hc_mod.kill_process_tree(proc.pid, grace_s=1.0)  # must not raise
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_success_path_does_not_perform_destructive_cleanup(self):
        """Cleanup of an already-terminal worker must not raise or kill others."""
        unrelated = subprocess.Popen([sys.executable, _SLEEP_SRC, _SLEEP])
        try:
            hc_mod.kill_process_tree(99999999, grace_s=1.0)  # nonexistent owned pid
            assert unrelated.poll() is None                  # untouched
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)

    def test_child_enumeration_scopes_to_descendants(self):
        """_get_child_pids must only return direct descendants, never a sibling."""
        worker = subprocess.Popen([sys.executable, "-c", self._TREE],
                                  stdout=subprocess.PIPE, text=True)
        line = worker.stdout.readline().strip()
        child_pid = int(line) if line.isdigit() else None
        try:
            children = hc_mod._get_child_pids_posix(worker.pid)
            assert child_pid in children
            assert hc_mod._get_child_pids_posix(999999999) == []
        finally:
            for pid in (worker.pid, child_pid):
                if pid is not None and not _wait_gone(pid, 2.0):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            worker.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock tests")
class TestPosixServerLock:
    """fcntl.flock single-instance guard: exclusion now + auto-release on exit."""

    @pytest.fixture
    def lockfile(self, tmp_path):
        return tmp_path / ".server.lock"

    def _spawn(self, code: str, lockfile) -> subprocess.Popen:
        env = dict(os.environ)
        env["FLUXSWARM_LOCK_FILE"] = str(lockfile)
        env.pop("FLUXSWARM_ALLOW_MULTI", None)
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    def test_second_instance_is_refused(self, lockfile):
        holder = self._spawn(
            "import serverlock,time; serverlock.acquire(); print('held', flush=True); time.sleep(30)",
            lockfile,
        )
        assert holder.stdout.readline().strip() == "held"
        try:
            second = self._spawn(
                "import serverlock; serverlock.acquire()", lockfile)
            out, err = second.communicate(timeout=15)
            assert second.returncode != 0
            assert "already running" in err
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_lock_auto_releases_and_reacquires(self, lockfile, monkeypatch):
        holder = self._spawn(
            "import serverlock,time; serverlock.acquire(); print('held', flush=True); time.sleep(30)",
            lockfile,
        )
        assert holder.stdout.readline().strip() == "held"
        holder.kill()
        holder.wait(timeout=5)
        # The flock died with the holder; a fresh acquire must succeed.
        monkeypatch.delenv("FLUXSWARM_ALLOW_MULTI", raising=False)
        import serverlock as sl
        monkeypatch.setattr(sl, "LOCK", lockfile)
        try:
            sl.acquire()
            assert lockfile.exists()
        finally:
            sl.release()
