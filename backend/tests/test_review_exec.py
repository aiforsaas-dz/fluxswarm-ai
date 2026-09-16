"""Phase 12 — the Reviewer lane (ecc-reviewer, REVIEW.md) must ship an
EXECUTABLE evidence review, not a prose critique. The Reviewer blueprint is a
single deterministic json.loads()-able JSON object derived from the workspace
brief, with:
  * checks (each id RC-n + target + deterministic status pass/warn/fail +
    detail) grading each upstream artifact (PLAN.md, ARCHITECTURE.md,
    DEVOPS.md, tests/test_app.py) by presence in the brief;
  * a verdict (pass when every graded artifact is present, else warn);
  * consistency + reproducibility + keys;
  * decisions (each DR-n with id + title + rationale);
  * deterministic: a fresh run on the same workspace brief rebuilds the
    identical REVIEW.md checks + verdict (never an LLM opinion).
The artifact gates the Builder via the evidence gate, so it must also be
validated (non-parseable/missing-checks reviews are recorded, not silent).
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
assert "def review_is_executable(" in _src
assert "def review_executable_issues(" in _src
assert '_REVIEW_MAX_TOKENS' in _src
assert 'if name == "review.md":' in _src


def _full_brief() -> str:
    return ("--- PLAN.md ---\n# plan\n\n--- ARCHITECTURE.md ---\n# arch\n\n"
            "--- DEVOPS.md ---\n# devops\n\n--- tests/test_app.py ---\n"
            "def test_x():\n    assert 1\n")


def _empty_brief() -> str:
    return "--- nothing produced ---\n"


def _review_prompt(brief: str) -> str:
    return demo_llm.reviewer_prompt("Review", "a CLI tool", brief=brief)


def test_reviewer_prompt_returns_executable_json_blueprint():
    text = _review_prompt(_full_brief())
    obj = json.loads(text)
    assert obj["verdict"] == "pass"
    assert len(obj["checks"]) == 4
    assert all(c["status"] == "pass" for c in obj["checks"])
    assert obj["consistency"]
    assert obj["reproducibility"]
    assert obj["keys"]
    assert any(d["id"].startswith("DR-") for d in obj["decisions"])


def test_review_is_executable_accepts_deterministic_review():
    text = _review_prompt(_full_brief())
    assert demo_llm.review_is_executable(text) is True


def test_review_verdict_warns_when_artifacts_missing():
    text = _review_prompt(_empty_brief())
    assert demo_llm.review_verdict(text) == "warn"
    statuses = {c["status"] for c in demo_llm.review_checks(text)}
    assert statuses == {"warn"}


def test_review_executable_flags_missing_checks():
    broken = dict(demo_llm.parse_review(_review_prompt(_full_brief())))
    broken.pop("checks")
    issues = demo_llm.review_executable_issues(json.dumps(broken))
    assert any("checks" in i.lower() for i in issues)


def test_review_executable_flags_non_parseable():
    issues = demo_llm.review_executable_issues("so the plan is good overall")
    assert any("not a parseable JSON blueprint" in i for i in issues)


def test_review_executable_flags_undefined_references():
    broken = dict(demo_llm.parse_review(_review_prompt(_full_brief())))
    broken["checks"] = list(broken["checks"]) + [
        {"id": "RC-9", "name": "ghost RC-99 / DR-99",
         "target": "PLAN.md", "status": "pass", "detail": "RC-99 -> DR-99"},
    ]
    issues = demo_llm.review_executable_issues(json.dumps(broken))
    assert any("RC-99" in i for i in issues)
    assert any("DR-99" in i for i in issues)


def test_review_is_deterministic_for_same_brief():
    a = _review_prompt(_full_brief())
    b = _review_prompt(_full_brief())
    assert a == b
    missing_a = _review_prompt(_empty_brief())
    missing_b = _review_prompt(_empty_brief())
    assert missing_a == missing_b
    assert a != missing_a


def test_record_review_validation_note_writes_event_only_when_not_executable(monkeypatch, tmp_path):
    """The thin drive must record (without blocking) that a review is not
    executable; an executable review stays silent."""
    import main as main_mod

    ws = tmp_path / "wsNote"
    ws.mkdir()
    notes = []

    def fake_insert_event(board, task_id, kind, payload):
        notes.append((board, task_id, kind, payload))

    monkeypatch.setattr(main_mod.hc, "_insert_event", fake_insert_event)
    (ws / "REVIEW.md").write_text(_review_prompt(_full_brief()), encoding="utf-8")
    main_mod._record_review_validation_note("b", "t-reviewer", ws)
    assert notes == []

    (ws / "REVIEW.md").write_text("looks good to me", encoding="utf-8")
    main_mod._record_review_validation_note("b", "t-reviewer", ws)
    assert len(notes) == 1
    assert "Review validation" in notes[0][3]