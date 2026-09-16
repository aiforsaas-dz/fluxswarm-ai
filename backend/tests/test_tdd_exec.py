"""Phase 11 — the TDD lane (ecc-tdd, tests/test_app.py) must ship an
EXECUTABLE pytest blueprint, not a prose test request. The TDD blueprint is a
deterministic, ast-parseable Python test file whose top-level _CONTRACT dict
declares:
  * focused cases, each with id (T-n) + name + target + inputs + expected;
  * consistency + reproducibility + keys;
  * decisions (each AT-n with id + title + rationale);
  * a web-contract branch mirroring the Planner/Architect/Designer/DevOps
    lanes: when the objective is web, cases target the promised ids
    (index.html#hero, index.html#grid); when it is CLI / library / API,
    web-contract preservation does not apply and it says so explicitly.
The file MUST remain valid Python (the evidence gate parses it) and a
collectable pytest suite (module-level test_* functions).
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo_llm
import json

import io

def _bom_safe_read(path):
    with io.open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()

_src = _bom_safe_read(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "demo_llm.py"))
assert "def tdd_is_executable(" in _src
assert "def tdd_executable_issues(" in _src
assert '_TDD_MAX_TOKENS' in _src
assert 'if name == "tests/test_app.py":' in _src


def _tdd_prompt(objective_web: bool) -> str:
    objective = "a professional dark-themed marketing landing page" \
        if objective_web else "a CLI tool that prints JSON"
    return demo_llm.tdd_prompt(
        "Test Suite",
        objective,
        brief="deploy blueprint",
    )


def test_tdd_prompt_returns_valid_python_with_contract():
    text = _tdd_prompt(objective_web=True)
    obj = demo_llm.parse_tdd(text)
    assert obj
    assert len(demo_llm.tdd_cases(text)) >= 3
    assert obj["consistency"]
    assert obj["reproducibility"]
    assert obj["keys"]
    assert obj["web_contract"]
    assert any(d["id"].startswith("AT-") for d in obj["decisions"])


def test_tdd_is_executable_accepts_deterministic_blueprint():
    text = _tdd_prompt(objective_web=True)
    assert demo_llm.tdd_is_executable(text) is True


def test_tdd_test_functions_collectable():
    text = _tdd_prompt(objective_web=True)
    names = demo_llm.tdd_test_function_names(text)
    assert any(n.startswith("test_") for n in names)


def test_tdd_executable_flags_missing_cases():
    broken = dict(demo_llm.parse_tdd(_tdd_prompt(objective_web=True)))
    broken.pop("cases")
    issues = demo_llm.tdd_executable_issues("_CONTRACT = "
                                            + json.dumps(broken) + "\n")
    assert any("cases" in i.lower() for i in issues)


def test_tdd_executable_flags_non_python():
    issues = demo_llm.tdd_executable_issues("def broken(:\n pass\n")
    assert any("not valid Python" in i for i in issues)


def test_tdd_executable_flags_undefined_references():
    broken = dict(demo_llm.parse_tdd(_tdd_prompt(objective_web=True)))
    broken["cases"] = list(demo_llm.tdd_cases(
        demo_llm.tdd_prompt("x", "a CLI tool"))) + [
        {"id": "T-9", "name": "ghost T-99 / AT-99",
         "target": "app", "inputs": "T-99", "expected": "AT-99"},
    ]
    text = ("_CONTRACT = " + json.dumps(broken) + "\n\n"
            "def test_something():\n    assert 1\n")
    issues = demo_llm.tdd_executable_issues(text)
    assert any("T-99" in i for i in issues)
    assert any("AT-99" in i for i in issues)


def test_tdd_web_contract_branch_deterministic():
    web = _tdd_prompt(objective_web=True)
    assert "index.html#hero" in web
    assert "index.html#grid" in web
    assert "web-contract preservation does not apply" not in web

    cli = _tdd_prompt(objective_web=False)
    assert "web-contract preservation does not apply" in cli
    assert "index.html#hero" not in cli


def test_tdd_prompt_web_branch_preserves_buildable_ids():
    """The TDD blueprint for a web goal keeps the promised ids exactly like
    the Designer/DevOps lanes, so the page stays buildable."""
    web = _tdd_prompt(objective_web=True)
    assert any("hero" in c.get("target", "") for c in demo_llm.tdd_cases(web))
    assert any("grid" in c.get("target", "") for c in demo_llm.tdd_cases(web))


def test_record_tdd_validation_note_writes_event_only_when_not_executable(monkeypatch, tmp_path):
    """The thin drive must record (without blocking) that a test suite is not
    executable; an executable suite stays silent."""
    import main as main_mod

    ws = tmp_path / "wsNote"
    ws.mkdir()
    notes = []

    def fake_insert_event(board, task_id, kind, payload):
        notes.append((board, task_id, kind, payload))

    monkeypatch.setattr(main_mod.hc, "_insert_event", fake_insert_event)
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text(
        _tdd_prompt(objective_web=False), encoding="utf-8")
    main_mod._record_tdd_validation_note("b", "t-tdd", ws)
    assert notes == []

    (ws / "tests" / "test_app.py").write_text(
        "def broken(:\n pass\n", encoding="utf-8")
    main_mod._record_tdd_validation_note("b", "t-tdd", ws)
    assert len(notes) == 1
    assert "TDD validation" in notes[0][3]