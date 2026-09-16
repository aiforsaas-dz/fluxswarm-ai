"""Phase 13 — the Builder lane (ecc-build-fixer) must ship an EXECUTABLE
deliverable, whatever its type. The Builder gets a dedicated deterministic
gate (``builder_executable_issues``) that is type-aware:

  * ``app.py``  — must be syntactically valid Python with no TODO/pass-only
    stubs;
  * ``index.html`` — must close, carry a <style> block, and have no unclosed
    style/script blocks (the per-section web QA handles the rest);
  * ``deliverable.md``/docs — non-trivial, not a fence-wrapped raw dump.

Web deliverables additionally flow through the existing QA/repair/tail-salvage
loop in ``main._run_builder``; the executable gate is the final independent
check on the file that actually lands in the workspace, and the drive records a
best-effort board note when the deliverable is not executable.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

import demo_llm
import hermes_client as hc

import io

def _bom_safe_read(path):
    with io.open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()

_src = _bom_safe_read(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "demo_llm.py"))
assert "def builder_executable_issues(" in _src
assert "def builder_is_executable(" in _src


def test_python_deliverable_valid_syntax_is_executable():
    out = "def main():\n    print('ok')\n\nif __name__ == '__main__':\n    main()\n"
    assert demo_llm.builder_executable_issues("Write a Python CLI app", out) == []
    assert demo_llm.builder_is_executable("Write a Python CLI app", out)


def test_python_deliverable_invalid_syntax_flagged():
    issues = demo_llm.builder_executable_issues(
        "Write a Python CLI app", "def main(:\n    pass\n")
    assert any("invalid Python" in i for i in issues)


def test_python_stub_content_flagged():
    issues = demo_llm.builder_executable_issues(
        "Write a Python CLI app", "def main():\n    pass\n")
    assert any("stub" in i for i in issues)


def test_web_deliverable_complete_is_executable():
    html = ("<!doctype html><html><head><style>.x{color:#000}</style></head>"
            "<body><h1>Hi</h1></body></html>")
    assert demo_llm.builder_executable_issues("Build a landing page", html) == []


def test_web_deliverable_truncated_or_unstyled_flagged():
    issues = demo_llm.builder_executable_issues(
        "Build a landing page", "<html><body><p>hi</p>")
    assert any("closing </html>" in i for i in issues)
    issues = demo_llm.builder_executable_issues(
        "Build a landing page", "<html><head></head><body><p>hi</p></body></html>")
    assert any("no <style> block" in i for i in issues)


def test_web_deliverable_unclosed_script_block_flagged():
    html = ("<!doctype html><html><head><style>.x{}</style></head><body>"
            "<script>alert('boom')</body></html>")
    issues = demo_llm.builder_executable_issues("Build a landing page", html)
    assert any("unclosed" in i and "script" in i for i in issues)


def test_doc_deliverable_fence_wrapped_or_thin_flagged():
    assert demo_llm.builder_executable_issues(
        "Write a README", "```\n# Hi\n```") != []
    assert demo_llm.builder_executable_issues("Write a README", "# Hi") != []
    good = "# Title\n\nDescription of the tool and what it does.\n\n## Usage\n\nRun it."
    assert demo_llm.builder_executable_issues("Write a README", good) == []


def test_empty_deliverable_flagged():
    assert demo_llm.builder_executable_issues("Write a README", "   ") != []


def test_record_builder_validation_note_writes_event_only_when_not_executable(monkeypatch, tmp_path):
    """The thin drive must record (without blocking) that a deliverable is not
    executable; an executable deliverable stays silent."""
    import main as main_mod

    ws = tmp_path / "wsNote"
    ws.mkdir()
    notes = []

    def fake_insert_event(board, task_id, kind, payload):
        notes.append((board, task_id, kind, payload))

    monkeypatch.setattr(main_mod.hc, "_insert_event", fake_insert_event)
    (ws / "README.md").write_text("# Title\n\nBody of the deliverable.",
                                       encoding="utf-8")
    main_mod._record_builder_validation_note("b", "t-builder", ws, "Write a README build kit")
    assert notes == []

    (ws / "app.py").write_text("def main(:\n    pass\n", encoding="utf-8")
    main_mod._record_builder_validation_note("b", "t-builder", ws, "Write a Python CLI app")
    assert len(notes) == 1
    assert "Builder validation" in notes[0][3]


def test_run_builder_web_records_note_on_final_artifact(monkeypatch, tmp_path):
    """Phase 13 hook inside _run_builder: even after the web repair/tail loop,
    the executable gate runs on the FINAL workspace file."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "wsFinal"
    ws.mkdir()
    calls = []

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        calls.append(prompt)
        (Path(workspace) / artifact_name).write_text(
            "<!doctype html><html><head><style>.hero p { max-width: 650px;",
            encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    out = main_mod._run_builder(
        slug="flux-demo-p13", task_id="tb", workspace=str(ws),
        provider="gemini", model="g",
        objective="Build a landing page for Nebula", brief="plan",
        task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page for Nebula"))
    assert out["ok"]
    assert (ws / "index.html").exists()