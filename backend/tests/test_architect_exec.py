"""Phase 8 — the Architect lane (ecc-architect, ARCHITECTURE.md) must ship an
EXECUTABLE architecture blueprint, not prose.

The blueprint is a single deterministic JSON object with:
  * `decisions` (AD-n id + title + rationale);
  * `components` each with a concrete `path`, `responsibility`, `interfaces`
    (in/out types) and a named `data_flow`;
  * `consistency` and `reproducibility` (pins/config/exact steps) so a fresh
    run reproduces the same blueprint;
  * `keys` naming the exact artifacts the build delivers;
plus a web-contract branch: when the objective is web, the prompt tells the
Architect to KEEP the web contract intact (sections/ids/CTA/palette, preserve
promised ids); when the objective is a CLI/library/API, web preservation does
not apply and it says so explicitly — identical to the Planner lane's web
contract-branch guard (Phase 7).
"""
from __future__ import annotations

import json

import demo_llm


def _valid_architect_text() -> str:
    return json.dumps({
        "decisions": [
            {"id": "AD-1", "title": "Thin 8-lane swarm",
             "rationale": "Planner/Architect/DevOps/TDD/Reviewer/Builder/"
                          "Auditor stay deterministic and executable."},
            {"id": "AD-2", "title": "Concrete component paths",
             "rationale": "Every component names a real repo path so the "
                          "blueprint is reproducible, not aspirational."},
        ],
        "components": [
            {"id": "C-1", "name": "API", "path": "backend/main.py",
             "responsibility": "Thin lane dispatch",
             "interfaces": [{"name": "dispatch", "in": "slug | goal",
                             "out": "task_id | artifact"}],
             "data_flow": "backend/main.py feeds backend/demo_llm.py",
             "storage": "SQLite"},
            {"id": "C-2", "name": "LLM engine", "path": "backend/demo_llm.py",
             "responsibility": "Lane prompts + validators",
             "interfaces": [{"name": "architect_prompt", "in": "objective",
                             "out": "ARCHITECTURE.md JSON text"}],
             "data_flow": "demo_llm feeds the Builder", "storage": "none"},
        ],
        "consistency": [
            "components share one ARCHITECTURE.md key set; ids unique",
        ],
        "reproducibility": [
            "pins: demo_llm._ARCH_MAX_TOKENS; exact path keys enumerate the "
            "concrete backend files so a fresh run rebuilds the same blueprint",
        ],
        "keys": ["ARCHITECTURE.md", "backend/main.py", "backend/demo_llm.py"],
    })


def test_architect_prompt_returns_executable_json_blueprint():
    cli = demo_llm.architect_prompt("t", "CLI objective")
    obj = json.loads(cli)
    assert obj["decisions"] and obj["components"] and obj["consistency"]
    assert obj["reproducibility"] and obj["keys"]
    for c in obj["components"]:
        assert c.get("path")
        assert c.get("interfaces")
        assert c.get("data_flow")


def test_arch_is_executable_accepts_the_deterministic_blueprint():
    text = _valid_architect_text()
    assert demo_llm.arch_is_executable(text) is True
    assert demo_llm.arch_executable_issues(text) == []


def test_arch_executable_flags_missing_paths():
    broken = json.loads(_valid_architect_text())
    for c in broken["components"]:
        c.pop("path", None)
    issues = demo_llm.arch_executable_issues(json.dumps(broken))
    assert any("no concrete 'path'" in i for i in issues)


def test_arch_executable_flags_undefined_references():
    broken = json.loads(_valid_architect_text())
    broken["components"][1]["data_flow"] = "feeds C-9 and AD-9"
    issues = demo_llm.arch_executable_issues(json.dumps(broken))
    assert any("C-9" in i and "undefined" in i for i in issues)
    assert any("AD-9" in i and "undefined" in i for i in issues)


def test_architect_prompt_web_contract_branch_is_deterministic():
    # Web objective: the Architect MUST keep the web contract intact
    # (sections/ids/CTA/palette preserved), like the Planner lane does.
    web = demo_llm.architect_prompt("t", "build a landing page website")
    assert "keep the web contract intact" in web
    assert "Preserve the promised ids" in web
    # CLI / API / library objective: web preservation explicitly does not apply.
    cli = demo_llm.architect_prompt("t", "Build a CLI tool")
    assert "web contract preservation does not apply" in cli
    assert "keep the web contract intact" not in cli
