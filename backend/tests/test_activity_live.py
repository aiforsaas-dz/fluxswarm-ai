"""Gate — live agent activity (Issue 2) and the persistent reaper (Issue 1).

Contract being pinned:
- Activity surfaced to the UI comes ONLY from REAL, persisted Hermes runtime
  state (task status + the ``task_events`` operational log). Nothing is
  fabricated, randomized, or chain-of-thought: no invented "Reading
  requirements", no rotating fake statuses, no fake progress timers.
- ``current_action`` mirrors the authoritative task state (running/done/queued/
  blocked); ``activity_log`` is built from real event kinds; ``result_preview``
  comes from the completed event's summary or the task's stored ``result``.
- The persistent reaper re-runs a single, non-blocking dispatch pass only for
  boards that still have unfinished agent work, so a worker that dies after the
  bounded launch window is reclaimed instead of freezing the board forever.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import hermes_client as hc_mod
import main as main_mod


def _make_board(tmp_path: Path, slug: str) -> Path:
    """Create a minimal Hermes-shaped board DB under a temp HERMES_HOME."""
    bdir = tmp_path / "kanban" / "boards" / slug
    bdir.mkdir(parents=True, exist_ok=True)
    db = bdir / "kanban.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
          created_at INTEGER, started_at INTEGER, completed_at INTEGER,
          last_heartbeat_at INTEGER, result TEXT, worker_pid INTEGER
        );
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
          payload TEXT, created_at INTEGER
        );
        """
    )
    c.commit()
    c.close()
    return bdir


def _add_task(bdir: Path, **kw):
    c = sqlite3.connect(str(bdir / "kanban.db"))
    c.execute(
        "INSERT INTO tasks (id,title,assignee,status,created_at,started_at,"
        "completed_at,last_heartbeat_at,result,worker_pid) "
        "VALUES (:id,:title,:assignee,:status,:created_at,:started_at,"
        ":completed_at,:last_heartbeat_at,:result,:worker_pid)",
        {
            "id": kw.get("id", "t1"), "title": kw.get("title", "Plan"),
            "assignee": kw.get("assignee", "ecc-planner"),
            "status": kw.get("status", "running"),
            "created_at": kw.get("created_at", 1000),
            "started_at": kw.get("started_at", 1001),
            "completed_at": kw.get("completed_at"),
            "last_heartbeat_at": kw.get("last_heartbeat_at"),
            "result": kw.get("result"), "worker_pid": kw.get("worker_pid"),
        },
    )
    c.commit()
    c.close()


def _add_event(bdir: Path, task_id: str, kind: str, created_at: int, payload=None):
    c = sqlite3.connect(str(bdir / "kanban.db"))
    c.execute(
        "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
        (task_id, kind, json.dumps(payload) if payload is not None else None, created_at),
    )
    c.commit()
    c.close()


def _base_task(**kw):
    """A task dict shaped like `hermes kanban list --json` + the list_tasks norm."""
    t = {
        "id": kw.get("id", "t1"), "title": kw.get("title", "Plan the feature"),
        "assignee": kw.get("assignee", "ecc-planner"), "status": kw.get("status", "running"),
        "completed_at": kw.get("completed_at"), "last_heartbeat_at": kw.get("last_heartbeat_at"),
        "result": kw.get("result"),
    }
    t["state"] = {"done": "done", "running": "running", "ready": "queued",
                  "todo": "queued"}.get(t["status"], t["status"])
    return t


# --------------------------------------------------------------------------- #
# Issue 2 — activity is derived from REAL events only.
# --------------------------------------------------------------------------- #
def test_activity_log_built_from_real_events(tmp_path, monkeypatch):
    slug = "u1-proj"
    bdir = _make_board(tmp_path, slug)
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(tmp_path))
    _add_task(bdir, id="t1", assignee="ecc-planner", status="running",
              last_heartbeat_at=2000)
    _add_event(bdir, "t1", "claimed", 1001)
    _add_event(bdir, "t1", "spawned", 1002, {"pid": 61})
    _add_event(bdir, "t1", "heartbeat", 1100)
    _add_event(bdir, "t1", "heartbeat", 1160)  # >60s later -> a 2nd "Working…"
    _add_event(bdir, "t1", "attached", 1200, {"filename": "plan.md"})

    t = _base_task(id="t1", status="running", last_heartbeat_at=2000)
    out = hc_mod._attach_activity(slug, t)

    labels = [e["label"] for e in out["activity_log"]]
    assert "Started" in labels                       # claimed -> Started
    assert any("Worker spawned (PID 61)" in l for l in labels)
    assert labels.count("Working…") == 2             # heartbeats collapsed, not spammy
    assert "Produced plan.md" in labels
    # No invented chain-of-thought labels ever appear.
    assert all(not ("Reading requirements" in l or "invoking tool" in l)
               for l in labels)


def test_current_action_reflects_authoritative_state(tmp_path, monkeypatch):
    slug = "u1-proj"
    bdir = _make_board(tmp_path, slug)
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(tmp_path))
    _add_task(bdir, id="t1", assignee="ecc-planner", status="running", last_heartbeat_at=1500)
    t = _base_task(id="t1", status="running", last_heartbeat_at=1500)
    assert hc_mod._attach_activity(slug, t)["current_action"] == \
        hc_mod.ROLE_INFO["ecc-planner"]["action"]

    _add_task(bdir, id="t2", assignee="ecc-planner", status="done", completed_at=2000,
              result="Plan ready")
    t2 = _base_task(id="t2", status="done", completed_at=2000, result="Plan ready")
    assert hc_mod._attach_activity(slug, t2)["current_action"] == \
        hc_mod.ROLE_INFO["ecc-planner"]["done"]

    _add_task(bdir, id="t3", assignee="ecc-architect", status="ready")
    t3 = _base_task(id="t3", assignee="ecc-architect", status="ready")
    assert hc_mod._attach_activity(slug, t3)["current_action"] == "Waiting for dependency"


def test_result_preview_from_event_summary_and_stored_result(tmp_path, monkeypatch):
    slug = "u2-proj"
    bdir = _make_board(tmp_path, slug)
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(tmp_path))
    # No stored result -> use the completed event summary.
    _add_task(bdir, id="t1", assignee="ecc-planner", status="done", completed_at=2000)
    _add_event(bdir, "t1", "completed", 2000, {"summary": "Created the feature plan.",
                                              "result_len": 0})
    t1 = _base_task(id="t1", status="done", completed_at=2000)
    prev = hc_mod._attach_activity(slug, t1)["result_preview"]
    assert prev and prev.startswith("Created the feature plan")
    # Stored result takes precedence once present.
    _add_task(bdir, id="t2", assignee="ecc-planner", status="done", completed_at=3000,
              result="stored result text")
    t2 = _base_task(id="t2", status="done", completed_at=3000, result="stored result text")
    assert hc_mod._attach_activity(slug, t2)["result_preview"].startswith("stored result")
    # A running task exposes no result preview yet.
    _add_task(bdir, id="t3", assignee="ecc-architect", status="running", last_heartbeat_at=3010)
    t3 = _base_task(id="t3", assignee="ecc-architect", status="running", last_heartbeat_at=3010)
    assert hc_mod._attach_activity(slug, t3)["result_preview"] is None


def test_role_description_exposed_for_all_eight_agents():
    assert {v["name"] for v in hc_mod.ROLE_INFO.values()} == {
        "Planner", "Architect", "DevOps", "TDD", "Reviewer",
        "Designer", "Builder", "Auditor"}
    for role in hc_mod.ROLE_INFO.values():
        assert role["name"] and role["action"] and role["done"] and role["desc"]


def test_board_has_unfinished_work(tmp_path, monkeypatch):
    def fake_list(board):
        return [{"assignee": "fluxswarm", "state": "done"},  # root: ignored
                {"assignee": "ecc-planner", "state": "running"},
                {"assignee": "ecc-tdd", "state": "queued"}]
    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    assert hc_mod.board_has_unfinished_work("flux-demo-x") is True


def test_board_with_only_root_done_is_unfinished(tmp_path, monkeypatch):
    # Only the root planning card done -> still unfinished (agents haven't run).
    def fake_list(board):
        return [{"assignee": "fluxswarm", "state": "done"},
                {"assignee": "ecc-planner", "state": "queued"}]
    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    assert hc_mod.board_has_unfinished_work("flux-demo-x") is True


def test_board_terminal_after_all_agents_done(tmp_path, monkeypatch):
    slug = "flux-demo-x"
    bdir = _make_board(tmp_path, slug)
    # simulate a fully-converged board: root + all agents done
    def fake_list(board):
        return [{**{"assignee": "fluxswarm", "state": "done"}},
                {**{"assignee": "ecc-planner", "state": "done"}},
                {**{"assignee": "ecc-architect", "state": "done"}},
                {**{"assignee": "ecc-tdd", "state": "done"}},
                {**{"assignee": "ecc-devops", "state": "done"}},
                {**{"assignee": "ecc-reviewer", "state": "done"}},
                {**{"assignee": "ecc-build-fixer", "state": "done"}}]
    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    assert hc_mod.board_has_unfinished_work(slug) is False


def test_bump_blocked_to_ready_promotes_only_blocked_agent_tasks(tmp_path, monkeypatch):
    promoted = []

    def fake_list(board):
        return [{"id": "t_root", "assignee": "fluxswarm", "state": "done"},
                {"id": "t_planner", "assignee": "ecc-planner", "state": "blocked"},
                {"id": "t_tdd", "assignee": "ecc-tdd", "state": "blocked"},
                {"id": "t_reviewer", "assignee": "ecc-reviewer", "state": "queued"}]

    def fake_run(args, board=None, capture=True, provider_keys=None):
        assert args[0] == "promote"
        promoted.append(args[1])
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    monkeypatch.setattr(hc_mod, "_run", fake_run)

    n = hc_mod.bump_blocked_to_ready("flux-demo-x")
    # Only the two blocked AGENT tasks are promoted; root and queued are not.
    assert sorted(promoted) == ["t_planner", "t_tdd"]
    assert n == 2


def test_bump_blocked_to_ready_noop_when_none_blocked(tmp_path, monkeypatch):
    class _R_ok:
        returncode = 0
        stderr = ""

    def fake_list(board):
        return [{"id": "t_root", "assignee": "fluxswarm", "state": "done"},
                {"id": "t_planner", "assignee": "ecc-planner", "state": "running"},
                {"id": "t_reviewer", "assignee": "ecc-reviewer", "state": "queued"}]

    calls = []
    monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
    monkeypatch.setattr(hc_mod, "_run", lambda *a, **k: calls.append(a) or _R_ok())

    assert hc_mod.bump_blocked_to_ready("flux-demo-x") == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# Issue 1 — persistent reaper.
# --------------------------------------------------------------------------- #
def test_reaper_ticks_only_unfinished_boards(monkeypatch):
    disp = []
    seen = []
    bump_calls = []
    monkeypatch.setattr(main_mod.hc, "list_boards",
                        lambda: [{"slug": "u1-a"}, {"slug": "u1-b"},
                                 {"slug": "u1-c"}])
    monkeypatch.setattr(main_mod.hc, "board_has_unfinished_work",
                        lambda s: s in ("u1-a", "u1-c"))
    monkeypatch.setattr(main_mod.hc, "bump_blocked_to_ready",
                        lambda s: bump_calls.append(s) or 0)
    monkeypatch.setattr(main_mod.hc, "dispatch",
                        lambda *a, **k: (disp.append((a[0], k))))

    main_mod._reaper_last.clear()
    main_mod._reconcile_boards_once()

    assert [d[0] for d in disp] == ["u1-a", "u1-c"]
    # Every board gets a transient-block recovery pass BEFORE the dispatch
    # filter, so a board whose agents are ALL blocked can recover too.
    assert sorted(bump_calls) == ["u1-a", "u1-b", "u1-c"]
    # Single, NON-blocking pass (the reaper must not hold a converger loop).
    for _, kw in disp:
        assert kw.get("blocking") is False
    assert main_mod._reaper_last.get("u1-b") is None  # terminal board skipped


def test_reaper_bumps_blocked_then_dispatches(monkeypatch):
    """A board whose agents are ALL blocked must still be re-promoted so the
    next dispatch pass re-activates it once the provider recovers."""
    order = []

    def fake_bump(slug):
        order.append("bump:" + slug)
        return 4

    def fake_unfinished(slug):
        order.append("check:" + slug)
        # After the bump the board counts as unfinished (blocked -> ready).
        return True

    def fake_dispatch(*args, **kwargs):
        order.append("dispatch:" + args[0])

    monkeypatch.setattr(main_mod.hc, "list_boards", lambda: [{"slug": "u1-dead"}])
    monkeypatch.setattr(main_mod.hc, "bump_blocked_to_ready", fake_bump)
    monkeypatch.setattr(main_mod.hc, "board_has_unfinished_work", fake_unfinished)
    monkeypatch.setattr(main_mod.hc, "dispatch", fake_dispatch)

    main_mod._reaper_last.clear()
    main_mod._reconcile_boards_once()

    assert order == ["bump:u1-dead", "check:u1-dead", "dispatch:u1-dead"]


def test_reaper_respects_per_board_min_gap(monkeypatch):
    disp = []
    monkeypatch.setattr(main_mod.hc, "list_boards", lambda: [{"slug": "u1-a"}])
    monkeypatch.setattr(main_mod.hc, "board_has_unfinished_work", lambda s: True)
    monkeypatch.setattr(main_mod.hc, "bump_blocked_to_ready", lambda s: 0)
    monkeypatch.setattr(main_mod.hc, "dispatch",
                        lambda *a, **k: disp.append(a[0]))

    main_mod._reaper_last.clear()
    # First pass dispatches it.
    main_mod._reconcile_boards_once()
    # Second pass immediately after is throttled by the min-gap window.
    main_mod._reconcile_boards_once()
    main_mod._reaper_last["u1-a"] = 0.0  # pretend long ago -> allowed again
    main_mod._reconcile_boards_once()

    assert disp.count("u1-a") == 2  # not 3: the immediate retry was throttled


def test_reaper_reclaim_uses_existing_single_pass_dispatch(monkeypatch):
    """The reaper must reuse the existing idempotent dispatch pass (which runs
    release_stale_claims / detect_crashed_workers / recompute_ready), exposing
    `blocking=False`. This is the mechanism that turns a dead worker's stale
    `running` task back into a reclaimable/ready task instead of freezing."""
    captured = {}

    def fake_dispatch(*args, **kwargs):
        captured.update(kwargs)
        return {"terminal": False, "outcome": "pending"}

    monkeypatch.setattr(main_mod.hc, "dispatch", fake_dispatch)
    main_mod.hc.dispatch("u1-dead", max_spawn=main_mod._REAPER_MAX_SPAWN,
                         blocking=False)
    assert captured.get("blocking") is False
    assert captured.get("max_spawn") == main_mod._REAPER_MAX_SPAWN


def test_board_finalized_classifies_terminal_states(monkeypatch):
    """_board_finalized must return True ONLY for launch-terminal projects. A
    stuck/refunded board is operator-final: the reaper must never re-arm it,
    otherwise its stale 'running' tasks hold the host kanban cap forever and
    starve every subsequent launch."""
    cases = {
        "stuck": ({"launch_status": "stuck", "launch_refunded": 1}, True),
        "ok": ({"launch_status": "ok", "launch_refunded": 0}, True),
        "error": ({"launch_status": "error", "launch_refunded": 0}, True),
        "refunded-but-unset": ({"launch_status": None, "launch_refunded": 1}, True),
        "running": ({"launch_status": "running", "launch_refunded": 0}, False),
        "launching": ({"launch_status": None, "launch_refunded": 0}, False),
        "no-project": (None, False),
    }
    for name, (proj, expected) in cases.items():
        monkeypatch.setattr(main_mod, "_project_by_board_slug", lambda s: proj)
        assert main_mod._board_finalized("x") is expected, name


def test_reaper_skips_sealed_and_finalized_boards(monkeypatch):
    """A finalized (stuck/refunded) project board or a sealed board must be
    skipped ENTIRELY — not even transient-block recovery may re-arm it. This
    is what stops the endless worker-resurrection loop that poisoned the
    host-level kanban concurrency budget."""
    disp = []
    bump_calls = []

    project_rows = {
        "finalized-a": {"launch_status": "stuck", "launch_refunded": 1},
        "sealed-b": {"launch_status": None, "launch_refunded": 0},
        "fresh-c": None,
    }
    sealed = {"sealed-b"}

    monkeypatch.setattr(main_mod.hc, "list_boards",
                        lambda: [{"slug": s} for s in project_rows])
    monkeypatch.setattr(main_mod, "_project_by_board_slug",
                        lambda s: project_rows.get(s))
    monkeypatch.setattr(main_mod.hc, "board_is_sealed",
                        lambda s: s in sealed)
    monkeypatch.setattr(main_mod.hc, "bump_blocked_to_ready",
                        lambda s: bump_calls.append(s) or 0)
    monkeypatch.setattr(main_mod.hc, "board_has_unfinished_work", lambda s: True)
    monkeypatch.setattr(main_mod.hc, "dispatch",
                        lambda *a, **k: disp.append((a[0], k)))

    main_mod._reaper_last.clear()
    main_mod._reconcile_boards_once()

    assert [d[0] for d in disp] == ["fresh-c"]
    bumped = sorted(bump_calls)
    assert bumped == ["fresh-c"], bumped
    for _, kw in disp:
        assert kw.get("blocking") is False


def test_seal_board_parks_stale_tasks_and_marks_board(tmp_path, monkeypatch):
    """seal_board must kill the board's worker processes, flip every non-
    terminal agent task to blocked (clearing pid/claim), preserve done tasks,
    and drop a durable marker the reaper respects."""
    slug = "u1-stuck-1"
    bdir = _make_board(tmp_path, slug)
    _add_task(bdir, id="t_running", status="running", worker_pid=999)
    _add_task(bdir, id="t_ready", status="ready", worker_pid=55)
    _add_task(bdir, id="t_done", status="done", worker_pid=None)
    _add_task(bdir, id="t_root", assignee="fluxswarm", status="done")

    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(tmp_path))
    kills = []
    monkeypatch.setattr(hc_mod, "kill_process_tree",
                        lambda pid, **k: kills.append(pid))

    report = hc_mod.seal_board(slug, reason="test")

    assert sorted(kills) == [55, 999]
    assert report["killed"] == 2
    assert report["blocked"] == 2
    assert hc_mod.board_is_sealed(slug)
    c = sqlite3.connect(str(bdir / "kanban.db"))
    try:
        rows = {r[0]: r for r in c.execute(
            "SELECT id,status,worker_pid FROM tasks")}
    finally:
        c.close()
    assert rows["t_running"][1] == "blocked" and rows["t_running"][2] is None
    assert rows["t_ready"][1] == "blocked" and rows["t_ready"][2] is None
    assert rows["t_done"][1] == "done"
    assert rows["t_root"][1] == "done"


def test_seal_board_idempotent(tmp_path, monkeypatch):
    slug = "u1-stuck-2"
    bdir = _make_board(tmp_path, slug)
    _add_task(bdir, id="t_a", status="blocked")
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hc_mod, "kill_process_tree", lambda pid, **k: None)
    r1 = hc_mod.seal_board(slug)
    r2 = hc_mod.seal_board(slug)
    assert r1["blocked"] == 0 and r2["blocked"] == 0
    assert hc_mod.board_is_sealed(slug)
