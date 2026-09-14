"""Thin REAL-project path tests (small-memory host convergence).

The fat Hermes worker / ``swarm`` CLI loads the whole workspace and OOMs a
512 MB container ~40-80s into work (measured crash loop on the free host), so
on small-memory hosts EVERY real launch runs the thin in-process direct-DB
driver: real board, real 8-lane squad, real provider completion per lane, real
artifacts in the served workspace — no fat CLI subprocess anywhere. These tests
lock that path's honesty boundaries and its automatic memory-gate.
"""
from __future__ import annotations

import sqlite3

import main as main_mod
import hermes_client as hc
import demo_llm
import provider_pool


def _host(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    return tmp_path


def _mk_demo_board(tmp_path, monkeypatch, board="bdemo"):
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    hc._ensure_board_db(board)
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        c.execute("INSERT INTO tasks (id,title,assignee,status,created_at) "
                  "VALUES (?,?,?,?,?)", ("t1", "Task", "ecc-planner", "ready", 1000))
        c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                  "VALUES (?,?,?,?)", ("t1", "created", None, 1000))
        c.commit()
    finally:
        c.close()


def _db_events(board, tid):
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        return c.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    finally:
        c.close()


def test_projects_are_thin_falls_back_false():
    assert hc.projects_are_thin() is False          # desktop dev/CI (no cgroup)


def test_projects_are_thin_env_overrides(monkeypatch):
    monkeypatch.setenv("FLUXSWARM_PROJECT_MODE", "thin")
    assert hc.projects_are_thin() is True
    monkeypatch.setenv("FLUXSWARM_PROJECT_MODE", "fat")
    assert hc.projects_are_thin() is False


def test_list_boards_fs_scan_on_thin(monkeypatch, tmp_path):
    _host(monkeypatch, tmp_path)
    monkeypatch.setattr(hc, "projects_are_thin", lambda: True)
    (tmp_path / "kanban" / "boards" / "u1-x").mkdir(parents=True)
    (tmp_path / "kanban" / "boards" / "flux-demo-y").mkdir(parents=True)
    # Thin hosts never run the fat `boards ls` CLI.
    monkeypatch.setattr(hc, "_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("CLI must not run on thin hosts")))
    slugs = [b["slug"] for b in hc.list_boards()]
    assert slugs == ["flux-demo-y", "u1-x"]


def test_launch_project_thin_builds_eight_lanes_no_cli(monkeypatch, tmp_path):
    """The thin project build writes the REAL 8-agent squad directly into
    kanban.db + seeds the served workspace, with zero CLI subprocesses."""
    _host(monkeypatch, tmp_path)
    monkeypatch.setattr(hc, "_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("CLI must not run for a thin project build")))

    swarm = hc.launch_project_thin("u1-proj", "Build a CLI tool", provider="gemini")
    assert len(swarm["worker_ids"]) == 8
    assert swarm["planner_id"] == swarm["worker_ids"][0]
    assert swarm["verifier_id"] and swarm["synthesizer_id"]

    ws = hc.project_workspace_dir("u1-proj")
    assert (ws / "TASK.md").exists()

    c = sqlite3.connect(str(hc._board_db_path("u1-proj")))
    try:
        rows = c.execute(
            "SELECT assignee, status FROM tasks ORDER BY created_at").fetchall()
        created = c.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='created'").fetchone()[0]
    finally:
        c.close()
    assert [r[0] for r in rows] == [
        "ecc-planner", "ecc-architect", "ecc-devops",
        "ecc-tdd", "ecc-reviewer", "ecc-designer",
        "ecc-build-fixer", "ecc-auditor"]
    assert rows[0][1] == "ready"
    assert all(r[1] == "todo" for r in rows[1:])
    assert created == 8


def test_list_tasks_renders_thin_squad(monkeypatch, tmp_path):
    _host(monkeypatch, tmp_path)
    hc.launch_project_thin("u1-r", "Ship a landing page")
    tasks = hc.list_tasks("u1-r")
    roles = [t.get("role_name") for t in tasks]
    assert roles == ["Planner", "Architect", "DevOps", "TDD", "Reviewer",
                     "Designer", "Builder", "Auditor"]
    assert all(t.get("state") == "queued" for t in tasks)


def test_thin_execute_writes_nested_artifact(monkeypatch, tmp_path):
    """Lanes write nested artifacts (e.g. tests/test_app.py) into the workspace,
    with the attachment event still naming the produced file."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk_demo_board(tmp_path, monkeypatch)
    monkeypatch.setattr(demo_llm, "completion",
                        lambda p, m, prompt, max_tokens=400, api_key=None:
                        "def test_main(): assert True")
    res = hc.thin_execute("bdemo", "t1", str(ws), "gemini", "g",
                          "P", objective="x",
                          artifact_name="tests/test_app.py", max_tokens=800)
    assert res["ok"] is True
    artifact = ws / "tests" / "test_app.py"
    assert artifact.exists()
    kinds = [k for k, in _db_events("bdemo", "t1")]
    assert kinds == ["created", "claimed", "heartbeat", "heartbeat",
                     "attached", "completed"]


def test_thin_runtime_byok_then_pool(monkeypatch):
    prov, model, key = main_mod._thin_runtime({"gemini": " gk-secret "})
    assert (prov, model, key) == ("gemini", "gemini-3.5-flash-lite", "gk-secret")

    prov, model, key = main_mod._thin_runtime({"openrouter": "or-secret"})
    assert (prov, model) == ("openrouter", "nvidia/nemotron-3.5-lightning:free")
    assert key == "or-secret"

    monkeypatch.setattr(main_mod.provider_pool, "pick_demo_provider",
                        lambda: {"provider": "google", "model": "gemini-3.5-flash-lite"})
    prov, model, key = main_mod._thin_runtime({})
    assert (prov, model, key) == ("gemini", "gemini-3.5-flash-lite", None)

    monkeypatch.setattr(main_mod.provider_pool, "pick_demo_provider", lambda: None)
    try:
        main_mod._thin_runtime({})
        raise AssertionError("expected RuntimeError with no providers")
    except RuntimeError:
        pass


def test_bg_thin_project_drives_all_eight_lanes_in_order(monkeypatch, tmp_path):
    """The thin driver executes Planner -> Architect -> DevOps -> TDD ->
    Reviewer -> Designer -> Builder -> Auditor IN ORDER, passing the pooled
    runtime + the real artifact name, then finalizes the launch as ok."""
    _host(monkeypatch, tmp_path)
    hc.launch_project_thin("u1-drive", "Build a REST API")
    by_role = {t["assignee"]: t["id"] for t in hc.list_tasks("u1-drive")}

    ran: list[dict] = []

    def fake_execute(**kw):
        ran.append({"task_id": kw["task_id"], "board": kw["board"],
                    "artifact_name": kw["artifact_name"],
                    "provider": kw["provider"], "api_key": kw.get("api_key")})
        return {"ok": True, "result": "done", "artifact": str(kw["artifact_name"]),
                "elapsed_s": 1}

    finalized = {}
    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    monkeypatch.setattr(main_mod.provider_pool, "pick_demo_provider",
                        lambda: {"provider": "google", "model": "gemini-3.5-flash-lite"})
    monkeypatch.setattr(main_mod, "_finalize_launch",
                        lambda slug, pid, **kw: finalized.update(kw))
    monkeypatch.setattr(main_mod.audit, "audit", lambda **k: None)
    monkeypatch.setattr(main_mod.db, "update_provider_usage_outcome", lambda *a, **k: None)

    main_mod._bg_thin_project("u1-drive", "Build a REST API", pid=7)

    expect_order = ["ecc-planner", "ecc-architect", "ecc-devops",
                    "ecc-tdd", "ecc-reviewer", "ecc-designer",
                    "ecc-build-fixer", "ecc-auditor"]
    assert [r["task_id"] for r in ran] == [by_role[a] for a in expect_order]
    assert [r["artifact_name"] for r in ran] == [
        "PLAN.md", "ARCHITECTURE.md", "Dockerfile",
        "tests/test_app.py", "REVIEW.md", "DESIGN.md",
        "deliverable.md", "AUDIT.md"]
    assert all(r["board"] == "u1-drive" for r in ran)
    assert all(r["provider"] == "gemini" for r in ran)
    assert all(r["api_key"] is None for r in ran)
    assert finalized.get("status") == "ok"
    assert finalized.get("outcome") == "converged"


def test_bg_thin_project_error_finalizes_and_never_raises(monkeypatch, tmp_path):
    """A provider failure mid-swarm finalizes the launch as error (refund
    policy handled by _finalize_launch) and the driver never raises."""
    _host(monkeypatch, tmp_path)
    hc.launch_project_thin("u1-fail", "Build a thing")

    def boom(**kw):
        raise demo_llm.DemoLLMError("gemini HTTP 503: overloaded")

    finalized = {}
    monkeypatch.setattr(main_mod.hc, "thin_execute", boom)
    monkeypatch.setattr(main_mod, "_finalize_launch",
                        lambda slug, pid, **kw: finalized.update({"status": kw.get("status"),
                                                                  "outcome": kw.get("outcome")}))
    monkeypatch.setattr(main_mod.audit, "audit", lambda **k: None)

    main_mod._bg_thin_project("u1-fail", "Build a thing", pid=42)   # must not raise
    assert finalized.get("status") == "error"
    assert finalized.get("outcome") == "launch_error"


def test_bg_thin_project_skips_already_done_lanes(monkeypatch, tmp_path):
    """P4/2 resume: when a restored board survived a restart with some lanes
    already terminal ('done'), the driver MUST NOT re-execute them — only the
    lanes that were still queued/running drive again."""
    _host(monkeypatch, tmp_path)
    hc.launch_project_thin("u1-resume", "Build a REST API")
    # Simulate a mid-launch kill: Planner + Architect completed before the host
    # died; the mirrored board restores exactly this queue.
    c = sqlite3.connect(str(hc._board_db_path("u1-resume")))
    try:
        c.execute("UPDATE tasks SET status='done', result='x' "
                  "WHERE assignee IN ('ecc-planner','ecc-architect')")
        c.commit()
    finally:
        c.close()

    by_role = {t["assignee"]: t["id"] for t in hc.list_tasks("u1-resume")}
    ran: list[str] = []

    def fake_execute(**kw):
        ran.append(kw["task_id"])
        return {"ok": True, "result": "done", "artifact": str(kw["artifact_name"]),
                "elapsed_s": 1}

    finalized = {}
    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    monkeypatch.setattr(main_mod.provider_pool, "pick_demo_provider",
                        lambda: {"provider": "google", "model": "gemini-3.5-flash-lite"})
    monkeypatch.setattr(main_mod, "_finalize_launch",
                        lambda slug, pid, **kw: finalized.update(kw))
    monkeypatch.setattr(main_mod.audit, "audit", lambda **k: None)
    monkeypatch.setattr(main_mod.db, "update_provider_usage_outcome", lambda *a, **k: None)

    main_mod._bg_thin_project("u1-resume", "Build a REST API", pid=9)

    # Only the 6 unfinished lanes re-run; Planner/Architect stay untouched.
    assert sorted(ran) == sorted(by_role[a] for a in
                                  ["ecc-devops", "ecc-tdd", "ecc-reviewer",
                                   "ecc-designer", "ecc-build-fixer", "ecc-auditor"])
    assert finalized.get("status") == "ok"


def test_web_qa_summary_feeds_auditor(tmp_path):
    """The Auditor lane gets a deterministic QA digest of the FINAL page (not
    the pre-build one), mirroring the evidence gate instead of inventing a
    verdict. A missing/truncated page must be reported honestly."""
    root = tmp_path / "wsA"
    root.mkdir()
    good = ("<!doctype html><html><head><style>"
            + "html{background:#0b1220;color:#f5f5f5} h1{color:#fff}</style>"
            + "</head><body><h1>T</h1><footer>F</footer></body></html>")
    (root / "index.html").write_text(good, encoding="utf-8")
    s = main_mod._web_qa_summary(str(root))
    assert "web_deliverable_score" in s

    empty = tmp_path / "wsB"
    empty.mkdir()
    assert "not built" in main_mod._web_qa_summary(str(empty))


def test_swarm_payload_maps_dict_and_result_object():
    """The launch response must survive BOTH drivers: thin (plain dict) and fat
    (SwarmResult object). Before the fix, thin hosts raised
    ``AttributeError: 'dict' object has no attribute 'root_id'`` -> FastAPI 500
    -> bare 'Internal Server Error' text -> frontend JSON-parse crash."""

    class _Result:
        root_id = "r1"
        worker_ids = ["w1", "w2"]
        verifier_id = "v1"
        synthesizer_id = "s1"

    d = main_mod._swarm_payload("u1-x", "Goal", {"root_id": "r1",
                                                 "worker_ids": ["w1", "w2"],
                                                 "verifier_id": "v1",
                                                 "synthesizer_id": "s1"})
    assert d == {"slug": "u1-x", "goal": "Goal", "root_id": "r1",
                 "workers": ["w1", "w2"], "verifier_id": "v1",
                 "synthesizer_id": "s1"}

    o = main_mod._swarm_payload("u1-x", "Goal", _Result())
    assert o == d


def test_completion_retries_transient_503_then_succeeds(monkeypatch):
    """A transient upstream 503 must be retried (with backoff) rather than
    parking the whole project board at the first lane that sneezes."""
    import demo_llm as llm
    attempts = {"n": 0}

    def flaky(url, payload, headers=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise demo_llm.DemoLLMError("gemini HTTP 503: Service Unavailable: ...")
        return {"candidates": [{"content": {"parts": [{"text": "OK text"}]}}]}

    monkeypatch.setattr(llm, "time", type("T", (), {"sleep": staticmethod(lambda *a, **k: None)})())
    monkeypatch.setattr(llm, "_post_json", flaky)
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    out = llm.completion("gemini", "gemini-3.5-flash-lite", "P")
    assert out == "OK text"
    assert attempts["n"] == 3


def test_completion_does_not_retry_401(monkeypatch):
    import demo_llm as llm
    attempts = {"n": 0}

    def bad(url, payload, headers=None):
        attempts["n"] += 1
        raise demo_llm.DemoLLMError("gemini HTTP 401: API key not valid: ...")

    monkeypatch.setattr(llm, "_post_json", bad)
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    try:
        llm.completion("gemini", "m", "P")
        raise AssertionError("expected DemoLLMError")
    except demo_llm.DemoLLMError as e:
        assert "401" in str(e)
    assert attempts["n"] == 1


def test_read_workspace_includes_suffix_less_artifacts(monkeypatch, tmp_path):
    """A thin lane writes real artifacts into the project workspace; the served
    workspace view must include extension-less project files like Dockerfile
    (the DevOps lane's real deliverable), not just .md/.py files."""
    _host(monkeypatch, tmp_path)
    ws = hc.project_workspace_dir("u1-ws")
    ws.mkdir(parents=True)
    (ws / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (ws / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text("def t(): pass\n", encoding="utf-8")
    content = hc.read_workspace("u1-ws")
    assert "--- Dockerfile ---" in content
    assert "FROM python:3.11-slim" in content
    assert "PLAN.md" in content
    assert "test_app.py" in content


def test_thin_driver_pass_goal_artifact_for_readme_goal(monkeypatch, tmp_path):
    """A README-style goal makes the final builder lane deliver README.md."""
    _host(monkeypatch, tmp_path)
    hc.launch_project_thin("u1-r2", "Write a good README")
    by_role = {t["assignee"]: t["id"] for t in hc.list_tasks("u1-r2")}
    last_assignee_ids = [by_role["ecc-build-fixer"]]

    ran: list[dict] = []

    def fake_execute(**kw):
        ran.append(kw)
        return {"ok": True, "result": "x", "artifact": "x", "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    monkeypatch.setattr(main_mod.provider_pool, "pick_demo_provider",
                        lambda: {"provider": "google", "model": "gemini-3.5-flash-lite"})
    monkeypatch.setattr(main_mod, "_finalize_launch", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.audit, "audit", lambda **k: None)
    monkeypatch.setattr(main_mod.db, "update_provider_usage_outcome", lambda *a, **k: None)

    main_mod._bg_thin_project("u1-r2", "Write a good README")
    # Builder lane delivers README.md (Auditor runs after it and is NOT the
    # deliverable-producing lane).
    builder_runs = [r for r in ran if r["task_id"] == last_assignee_ids[0]]
    assert builder_runs and builder_runs[-1]["artifact_name"] == "README.md"
    assert ran[-1]["artifact_name"] == "AUDIT.md"   # Auditor is the last lane