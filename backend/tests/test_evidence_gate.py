"""Evidence Gate tests: deterministic GO/WARN/NO-GO for the Builder lane."""
from __future__ import annotations

from pathlib import Path

import evidence_gate as eg


def _ws(tmp_path, files: dict[str, str]) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_go_when_full_evidence(tmp_path):
    root = _ws(tmp_path, {
        "PLAN.md": "# Plan", "ARCHITECTURE.md": "# Arch",
        "REVIEW.md": "# Review", "tests/test_app.py": "def test_x():\n    assert 1\n",
    })
    ev = eg.collect_evidence(str(root), goal="build a python tool")
    assert ev["verdict"] == "GO"
    names = {c["name"] for c in ev["checks"]}
    assert "test_artifact_present" in names
    assert "test_artifact_compiles" in names


def test_no_go_without_test_artifact(tmp_path):
    root = _ws(tmp_path, {"PLAN.md": "# Plan"})
    ev = eg.collect_evidence(str(root), goal="build something")
    assert ev["verdict"] == "NO-GO"


def test_warn_when_test_artifact_syntax_error(tmp_path):
    root = _ws(tmp_path, {
        "PLAN.md": "# Plan", "tests/test_app.py": "def broken(:\n",
    })
    ev = eg.collect_evidence(str(root))
    assert ev["verdict"] == "WARN"
    compiles = next(c for c in ev["checks"] if c["name"] == "test_artifact_compiles")
    assert compiles["status"] == "warn"


def test_web_qa_warning_surfaces(tmp_path):
    root = _ws(tmp_path, {
        "PLAN.md": "# Plan", "tests/test_app.py": "def t():\n    pass\n",
        "index.html": "<html><body></body></html>",
    })
    ev = eg.collect_evidence(str(root), goal="build a landing page")
    assert ev["verdict"] in ("GO", "WARN")
    names = {c["name"] for c in ev["checks"]}
    assert "web_qa" in names


def test_write_evidence_md_persists(tmp_path):
    root = _ws(tmp_path, {
        "PLAN.md": "# P", "ARCHITECTURE.md": "# A", "REVIEW.md": "# R",
        "tests/test_app.py": "def fine():\n    pass\n",
    })
    ev = eg.write_evidence_md(str(root))
    md = (root / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "Verdict" in md
    assert ev["verdict"] == "GO"
    assert (root / "EVIDENCE.md").is_file()


def test_empty_workspace_is_no_go(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    ev = eg.collect_evidence(str(root))
    assert ev["verdict"] == "NO-GO"


def test_meta_artifacts_missing_is_warn_not_fail(tmp_path):
    root = _ws(tmp_path, {"tests/test_app.py": "def t():\n    pass\n"})
    ev = eg.collect_evidence(str(root))
    # test artifact exists -> not NO-GO; missing REVIEW.md -> WARN
    assert ev["verdict"] == "WARN"