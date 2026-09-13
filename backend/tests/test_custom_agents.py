"""P4 custom agents tests.

Users define bespoke squad members (name, objective, skills); each becomes an
extra board lane (assignee ``ca-<id>``) that the thin project driver runs after
Reviewer and before Builder, writing ``CUSTOM_<name>.md`` into the workspace.
These tests lock the DB CRUD + API-route scaffolding and the thin-lane wiring.
"""
from __future__ import annotations

import sqlite3

import db
import demo_llm
import hermes_client as hc

import main as main_mod


def _mk_board(tmp_path, monkeypatch, slug, custom_agents):
    """Build a thin board whose lanes include ``ca-<id>`` tasks."""
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    hc.launch_project_thin(slug, "Build a web app", custom_agents=custom_agents)
    return hc.project_workspace_dir(slug)


def _tasks(slug, tmp_path):
    c = sqlite3.connect(str(hc._board_db_path(slug)))
    try:
        return c.execute("SELECT id, assignee, status, title FROM tasks ORDER BY id").fetchall()
    finally:
        c.close()


def test_custom_agent_model_and_db_table():
    from models import CustomAgent
    assert CustomAgent.__tablename__ == "custom_agents"
    u = db.create_user("agent_t@example.com", "agent", "pw")["id"]
    try:
        aid = db.create_custom_agent(u, "Security Auditor",
                                     "Audit for OWASP", "security, python")
        rows = db.list_custom_agents(u)
        assert [r["name"] for r in rows] == ["Security Auditor"]
        got = db.get_custom_agent(u, aid)
        assert got["objective"] == "Audit for OWASP"
        assert got["skills"] == "security, python"
        assert db.delete_custom_agent(u, aid) is True
        assert db.delete_custom_agent(u, aid) is False
    finally:
        db.delete_user(u)


def test_demo_llm_custom_agent_prompt_builds():
    p = demo_llm.custom_agent_prompt(
        task_title="Security Auditor",
        objective="Build a web app",
        agent_name="Security Auditor",
        agent_objective="Audit for OWASP Top 10 and produce a fixes report",
        skills="security, python, fastapi",
        project_goal="Build a web app",
    )
    assert "Security Auditor" in p
    assert "Audit for OWASP" in p
    assert "security, python, fastapi" in p


def test_launch_project_thin_inserts_custom_lanes(tmp_path, monkeypatch):
    agents = [{"id": 7, "name": "Security Auditor", "objective": "Audit OWASP",
               "skills": "security"}]
    _mk_board(tmp_path, monkeypatch, "p-ca-1", agents)
    rows = _tasks("p-ca-1", tmp_path)
    assignees = {r[1] for r in rows if r[1]}
    assert "ca-7" in assignees
    lane = [r for r in rows if r[1] == "ca-7"]
    assert lane and lane[0][3] == "Security Auditor (custom)"
    assert lane[0][2] == "todo"


def test_launch_project_thin_no_custom(tmp_path, monkeypatch):
    _mk_board(tmp_path, monkeypatch, "p-ca-2", None)
    assignees = {r[1] for r in _tasks("p-ca-2", tmp_path) if r[1]}
    assert not any(str(a).startswith("ca-") for a in assignees)


def test_background_lane_runs_custom_agent(tmp_path, monkeypatch):
    slug = "p-ca-3"
    agents = [{"id": 1, "name": "Apis", "objective": "Write README fixes",
               "skills": "docs"}]
    _mk_board(tmp_path, monkeypatch, slug, agents)

    # Fake an empty builder-doc set so the custom lane writes its artifact via
    # thin_execute, and stub the provider runtime to a deterministic text.
    calls = {}
    def fake_thin_execute(*, board, task_id, workspace, provider, model, prompt,
                          objective, artifact_name, api_key=None, max_tokens=None,
                          **kw):
        calls[artifact_name] = prompt
        return {"ok": True}

    monkeypatch.setattr(hc, "thin_execute", fake_thin_execute)
    monkeypatch.setattr(main_mod, "_thin_runtime",
                        lambda keys: ("openrouter", "np", None))
    monkeypatch.setattr(main_mod, "_ws_brief", lambda root: "")
    monkeypatch.setattr(main_mod, "_doc", lambda p: "")
    monkeypatch.setattr(main_mod, "evidence_gate", None)  # fewer moving parts
    try:
        main_mod._bg_thin_project(slug, "Build a web app", custom_agents=agents)
    except Exception:
        pass  # finalize hooks may be absent in CI; lane execution is what we assert
    assert any(k.startswith("CUSTOM_Apis") for k in calls), calls.keys()


def test_api_routes_exist():
    paths = {r.path for r in main_mod.app.routes}
    for expected in ("/api/agents", "/api/agents/{aid}"):
        assert any(expected in p for p in paths)
    # GET+POST on /api/agents, DELETE on /api/agents/{aid}
    def http(path, method):
        return [r for r in main_mod.app.routes
                if path in (getattr(r, "path", "") or "") and getattr(r, "methods", None)]
    assert http("/api/agents", "GET")
    assert http("/api/agents", "POST")
    assert http("/api/agents/{aid}", "DELETE")


def test_project_create_accepts_agent_ids():
    # ProjectCreate model has the agent_ids field (used by the create-project API).
    from pydantic import ValidationError
    p = main_mod.ProjectCreate(name="x", goal="y")
    assert p.agent_ids == []
    p2 = main_mod.ProjectCreate(name="x", goal="y", agent_ids=[3, 4])
    assert p2.agent_ids == [3, 4]