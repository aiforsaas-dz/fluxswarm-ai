"""Phase 7 — executable implementation-plan tests.

The Planner lane (PLAN.md) must produce an EXECUTABLE implementation plan, not
prose: a single parseable JSON carrying requirements, features, tasks with
traceable requirement/acceptance-criterion links, dependencies, risks, affected
components, test expectations and deployment impact. These tests lock the
prompt contract, the tolerant parser, the deterministic validator, the
task<->requirement/ac traceability map, and the non-blocking board note the
thin drive records when a plan is not executable.
"""
from __future__ import annotations

import json

import demo_llm


def _valid_plan_text():
    return json.dumps({
        "overview": "Add team sharing to the board API.",
        "requirements": [
            {"id": "REQ-1", "title": "Invite a teammate",
             "detail": "A board owner can invite a user by email."},
            {"id": "REQ-2", "title": "Shared board access",
             "detail": "Invited users can read and dispatch the board."},
            {"id": "REQ-3", "title": "Revoke access",
             "detail": "An owner can remove a teammate."},
        ],
        "features": ["sharing", "permission enforcement"],
        "tasks": [
            {"id": "T-1", "title": "POST /api/share", "requirement_ids": ["REQ-1"],
             "acceptance_criteria_ids": ["AC-1"], "depends_on": [],
             "components": ["backend/main.py"],
             "test_expectations": ["test_share_invite.py"]},
            {"id": "T-2", "title": "Ownership checks in dispatch",
             "requirement_ids": ["REQ-2"], "acceptance_criteria_ids": ["AC-2"],
             "depends_on": ["T-1"], "components": ["backend/main.py"],
             "test_expectations": ["test_tenant_isolation.py"]},
            {"id": "T-3", "title": "POST /api/share/revoke",
             "requirement_ids": ["REQ-3"], "acceptance_criteria_ids": ["AC-3"],
             "depends_on": ["T-1"], "components": ["backend/main.py"],
             "test_expectations": ["test_share_revoke.py"]},
            {"id": "T-4", "title": "Audit share events",
             "requirement_ids": ["REQ-1", "REQ-3"],
             "acceptance_criteria_ids": ["AC-3"],
             "depends_on": ["T-2", "T-3"], "components": ["backend/audit.py"],
             "test_expectations": ["test_audit.py"]},
        ],
        "acceptance_criteria": [
            {"id": "AC-1", "title": "Invite returns the teammate row"},
            {"id": "AC-2", "title": "Invited user can dispatch the shared board"},
            {"id": "AC-3", "title": "Revoked user returns 404 on that board"},
        ],
        "dependencies": ["T-1 before T-2", "T-1 before T-3"],
        "risks": [
            {"id": "R-1", "risk": "Cross-tenant id confusion",
             "mitigation": "slug-boundary validation keeps 404s tenant-safe"},
        ],
        "affected_components": ["backend/main.py", "backend/db.py"],
        "test_expectations": ["pytest tests/test_share_*.py -q"],
        "deployment_impact": "New endpoints are additive; no schema migration.",
        "constraints": ["keep the cookie-first auth model"],
    })


def test_planner_prompt_requires_all_executable_plan_sections():
    p = demo_llm.planner_prompt("Implement sharing", "Add team sharing")
    for token in ("JSON", "requirements", "features", "tasks",
                  "acceptance_criteria", "dependencies", "risks",
                  "affected_components", "test_expectations",
                  "deployment_impact", "TRACEABILITY"):
        assert token in p


def test_planner_prompt_traceability_rule_is_explicit():
    p = demo_llm.planner_prompt("Implement sharing", "Add team sharing")
    assert "every task MUST link" in p
    assert "requirement_ids" in p
    assert '"depends_on"' in p


def test_planner_prompt_keeps_web_section_contract_for_web_goals():
    web = demo_llm.planner_prompt("Build lander", "A landing page for Nebula")
    assert '"sections"' in web and '"ids"' in web and '"cta"' in web
    non_web = demo_llm.planner_prompt("Implement sharing", "Add team sharing")
    assert '"sections"' not in non_web


def test_parse_plan_tolerates_fences_and_stray_prose():
    plan = _valid_plan_text()
    for wrapped in (plan, "```json\n" + plan + "\n```",
                    "Here is my structured plan:\n" + plan + "\nRegards."):
        obj = demo_llm.parse_plan(wrapped)
        assert obj.get("overview")
    assert demo_llm.parse_plan("") == {}
    assert demo_llm.parse_plan("just prose") == {}
    assert demo_llm.parse_plan("[1,2,3]") == {}


def test_valid_executable_plan_has_no_issues_and_is_executable():
    plan = _valid_plan_text()
    assert demo_llm.plan_executable_issues(plan) == []
    assert demo_llm.plan_is_executable(plan) is True


def test_garbage_and_empty_plans_are_not_executable():
    assert demo_llm.plan_is_executable("") is False
    assert demo_llm.plan_is_executable("no json here") is False
    assert demo_llm.plan_is_executable("[1]") is False
    assert demo_llm.plan_executable_issues("")[0] == "plan is not structured JSON"


def test_missing_required_sections_are_flagged():
    plan = json.loads(_valid_plan_text())
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert issues == []
    for key in demo_llm._PLAN_REQUIRED_SECTIONS:
        broken = {k: v for k, v in plan.items() if k != key}
        issues = demo_llm.plan_executable_issues(json.dumps(broken))
        assert any(i == f"missing required section '{key}'" for i in issues), key


def test_untraceable_task_is_flagged():
    plan = json.loads(_valid_plan_text())
    plan["tasks"][0].pop("requirement_ids")
    plan["tasks"][0].pop("acceptance_criteria_ids")
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any("no requirement or acceptance-criterion link" in i
               and "T-1" in i for i in issues)


def test_task_references_undefined_requirement_flagged():
    plan = json.loads(_valid_plan_text())
    plan["tasks"][0]["requirement_ids"] = ["REQ-99"]
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any("undefined requirement 'REQ-99'" in i for i in issues)


def test_task_references_undefined_acceptance_criterion_flagged():
    plan = json.loads(_valid_plan_text())
    plan["tasks"][0]["acceptance_criteria_ids"] = ["AC-99"]
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any("undefined acceptance criterion 'AC-99'" in i for i in issues)


def test_orphan_requirement_and_criterion_are_flagged():
    plan = json.loads(_valid_plan_text())
    plan["requirements"] = plan["requirements"] + [
        {"id": "REQ-NEW", "title": "Never traced"}]
    plan["acceptance_criteria"] = plan["acceptance_criteria"] + [
        {"id": "AC-NEW", "title": "Never traced"}]
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any("'REQ-NEW' is not traced to by any task" in i for i in issues)
    assert any("'AC-NEW' is not traced to by any task" in i for i in issues)


def test_unknown_dependency_task_flagged():
    plan = json.loads(_valid_plan_text())
    plan["tasks"][0]["depends_on"] = ["T-99"]
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any("depends on undefined task 'T-99'" in i for i in issues)


def test_empty_task_list_is_flagged():
    plan = json.loads(_valid_plan_text())
    plan["tasks"] = []
    issues = demo_llm.plan_executable_issues(json.dumps(plan))
    assert any(i == "plan has no tasks (nothing to execute)" for i in issues)


def test_plan_trace_mapping_links_tasks_to_reqs_and_criteria():
    trace = demo_llm.plan_trace(_valid_plan_text())
    assert trace["T-2"]["requirements"] == ["REQ-2"]
    assert trace["T-4"]["requirements"] == ["REQ-1", "REQ-3"]
    assert trace["T-4"]["acceptance_criteria"] == ["AC-3"]
    assert trace["T-1"]["acceptance_criteria"] == ["AC-1"]


def test_plan_section_and_id_entry_extractors():
    plan = _valid_plan_text()
    assert [r["id"] for r in demo_llm.plan_requirements(plan)][:2] == ["REQ-1", "REQ-2"]
    assert [c["id"] for c in demo_llm.plan_acceptance_criteria(plan)][:1] == ["AC-1"]
    assert demo_llm.plan_section(plan, "risks") and demo_llm.plan_section(plan, "features")
    assert demo_llm.plan_section(plan, "missing_key") == []
    # string entries are promoted (tolerant schema)
    loose = json.dumps({"requirements": ["REQ-1"], "acceptance_criteria": ["AC-1"],
                        "tasks": [{"title": "do it"}], "features": [], "risks": [],
                        "affected_components": [], "test_expectations": [],
                        "dependencies": [], "deployment_impact": "x"})
    assert demo_llm.plan_requirements(loose)[0]["id"] == "REQ-1"


def test_web_contract_still_extracts_from_executable_plan():
    plan = json.loads(_valid_plan_text())
    plan.update({"palette": "dark + indigo", "sections": ["Features", "Pricing"],
                 "ids": {"Features": "features", "Pricing": "pricing"},
                 "cta": "Start free"})
    text = json.dumps(plan)
    assert demo_llm.plan_executable_issues(text) == []
    assert demo_llm.plan_nav_items(text) == {"features": "Features", "pricing": "Pricing"}
    assert demo_llm.plan_to_brief(text)


def test_lane_max_tokens_plan_gets_dedicated_budget():
    assert demo_llm.lane_max_tokens("a CLI tool", "PLAN.md") \
        >= demo_llm._NORMAL_MAX_TOKENS * 2


def test_record_plan_validation_note_writes_event_only_when_not_executable(monkeypatch, tmp_path):
    """The thin drive must record (without blocking) that a plan is not
    executable; an executable plan stays silent."""
    import main as main_mod

    ws = tmp_path / "wsNote"
    ws.mkdir()
    notes = []

    def fake_insert_event(board, task_id, kind, payload):
        notes.append((board, task_id, kind, payload))

    monkeypatch.setattr(main_mod.hc, "_insert_event", fake_insert_event)
    (ws / "PLAN.md").write_text(_valid_plan_text(), encoding="utf-8")
    main_mod._record_plan_validation_note("b", "t-planner", ws)
    assert notes == []

    (ws / "PLAN.md").write_text('{"overview": "unstructured results" }', encoding="utf-8")
    main_mod._record_plan_validation_note("b", "t-planner", ws)
    assert len(notes) == 1
    board, task_id, kind, payload = notes[0]
    assert (board, task_id, kind) == ("b", "t-planner", "note")
    assert payload.startswith("Plan validation: ")
    assert "missing required section" in payload