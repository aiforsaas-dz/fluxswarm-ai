"""Phase 14 — the Auditor lane (ecc-auditor, AUDIT.md) must ship an EXECUTABLE
acceptance report, not prose opinion. The Auditor blueprint is a single
deterministic json.loads()-able JSON object derived from the post-build QA
digest, with:
  * gates (each id AC-n + name + target + deterministic status pass/warn/fail +
    detail) for completeness/buildability/quality/safety, computed by
    keyword/score rules (never an LLM opinion);
  * a verdict (pass when every gate passes, warn on soft gaps, fail on any
    hard/structural failure or an unbuilt page);
  * qa_verbatim — the deterministic QA digest restated VERBATIM;
  * consistency + reproducibility + keys;
  * decisions (each AD-n with id + title + rationale);
  * deterministic: a fresh audit of the same deliverable rebuilds the identical
    gates + verdict from the same digest.
The Auditor is the final lane, so its artifact completes the workspace record;
it must also be validated (unparseable/missing-gate reports are recorded, not
silent).
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
assert "def audit_is_executable(" in _src
assert "def audit_executable_issues(" in _src
assert "_AUDIT_MAX_TOKENS" in _src
assert 'if name == "audit.md":' in _src


def _audit(qa: str = "web_deliverable_score=88/100; none") -> str:
    return demo_llm.auditor_prompt("ecc-auditor", "a landing page", qa=qa)


def test_auditor_prompt_returns_executable_json_blueprint():
    obj = json.loads(_audit())
    assert obj["verdict"] == "pass"
    assert {g["id"] for g in obj["gates"]} == {"AC-1", "AC-2", "AC-3", "AC-4"}
    assert all(g["status"] == "pass" for g in obj["gates"])
    assert obj["qa_verbatim"] == "web_deliverable_score=88/100; none"
    assert obj["consistency"]
    assert obj["reproducibility"]
    assert obj["keys"] == ["AUDIT.md"]
    assert any(d["id"].startswith("AD-") for d in obj["decisions"])


def test_audit_verdict_and_gates_derived_from_digest():
    aud = _audit("web_deliverable_score=45/100; issues (1):\n  - truncated: no closing </html>")
    assert demo_llm.audit_verdict(aud) == "fail"
    statuses = {g["name"]: g["status"] for g in demo_llm.audit_gates(aud)}
    # quality follows the score (<50 fail); buildability fails on truncation.
    assert statuses["quality"] == "fail"
    assert statuses["buildability"] == "fail"

    soft = _audit("web_deliverable_score=66/100; issues (1):\n  - no lang attribute")
    assert demo_llm.audit_verdict(soft) == "warn"
    st = {g["name"]: g["status"] for g in demo_llm.audit_gates(soft)}
    assert st["quality"] == "warn"

    safe_pass = _audit("external resource: https://cdn.example.com/x.js")
    assert demo_llm.audit_verdict(safe_pass) == "fail"
    assert all(g["status"] == "fail"
               for g in demo_llm.audit_gates(safe_pass)
               if g["id"] == "AC-4")


def test_audit_unbuilt_page_fails_every_gate():
    aud = _audit("web page not built / not auditable")
    assert demo_llm.audit_verdict(aud) == "fail"
    assert all(g["status"] == "fail" for g in demo_llm.audit_gates(aud))


def test_audit_is_executable_accepts_deterministic_report():
    assert demo_llm.audit_is_executable(_audit()) is True


def test_audit_executable_flags_missing_gates():
    broken = dict(demo_llm.parse_audit(_audit()))
    broken.pop("gates")
    issues = demo_llm.audit_executable_issues(json.dumps(broken))
    assert any("gates" in i.lower() for i in issues)


def test_audit_executable_flags_missing_qa_verbatim():
    broken = dict(demo_llm.parse_audit(_audit()))
    broken.pop("qa_verbatim")
    issues = demo_llm.audit_executable_issues(json.dumps(broken))
    assert any("verbatim" in i.lower() for i in issues)


def test_audit_executable_flags_non_parseable():
    issues = demo_llm.audit_executable_issues("fully approved, shipping it")
    assert any("not a parseable JSON blueprint" in i for i in issues)


def test_audit_executable_flags_undefined_references():
    broken = dict(demo_llm.parse_audit(_audit()))
    broken["gates"] = list(broken["gates"]) + [
        {"id": "AC-9", "name": "ghost AC-99 / AD-99",
         "target": "deliverable", "status": "pass",
         "detail": "AC-99 -> AD-99"},
    ]
    issues = demo_llm.audit_executable_issues(json.dumps(broken))
    assert any("AC-99" in i for i in issues)
    assert any("AD-99" in i for i in issues)


def test_audit_is_deterministic_for_same_digest():
    a = _audit()
    b = _audit()
    assert a == b
    fail_a = _audit("web page not built / not auditable")
    fail_b = _audit("web page not built / not auditable")
    assert fail_a == fail_b
    assert a != fail_a


def test_record_audit_validation_note_writes_event_only_when_not_executable(monkeypatch, tmp_path):
    """The thin drive must record (without blocking) that an AUDIT.md is not
    executable; an executable report stays silent."""
    import main as main_mod

    ws = tmp_path / "wsNote"
    ws.mkdir()
    notes = []

    def fake_insert_event(board, task_id, kind, payload):
        notes.append((board, task_id, kind, payload))

    monkeypatch.setattr(main_mod.hc, "_insert_event", fake_insert_event)
    (ws / "AUDIT.md").write_text(demo_llm.auditor_prompt("ecc-auditor", "a landing page"),
                                 encoding="utf-8")
    main_mod._record_audit_validation_note("b", "t-auditor", ws)
    assert notes == []

    (ws / "AUDIT.md").write_text("shipping it", encoding="utf-8")
    main_mod._record_audit_validation_note("b", "t-auditor", ws)
    assert len(notes) == 1
    assert "Audit validation" in notes[0][3]


def test_lane_max_tokens_audit_branch():
    assert demo_llm.lane_max_tokens("a landing page", "AUDIT.md") == \
        demo_llm._AUDIT_MAX_TOKENS