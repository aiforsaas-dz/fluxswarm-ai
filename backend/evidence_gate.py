"""Builder Evidence Gate — deterministic GO/NO-GO before final assembly.

The Builder (synthesizer) is the last lane; it must never ship a deliverable
without verified evidence from the preceding lanes.  This module collects that
evidence with purely deterministic checks (no LLM, no network, no subprocess)
and returns a GO / WARN / NO-GO verdict that the thin driver attaches to the
board's task_events.

GO    — all critical checks pass + a test artifact was produced.
WARN  — test artifact exists but has issues (syntax or weak web QA).
NO-GO — no test artifact at all, or workspace is empty.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path


def _parse_ok(path: Path) -> bool:
    """Return True if the file is syntactically valid Python (fast, no exec).

    Provider-generated tests occasionally contain a mojibake char (U+FFFD
    ``REPLACEMENT CHARACTER``) smuggled into a ``b"..."`` or ``"..."`` literal
    (e.g. ``b"Hu\uFFFDtres"``). That is not a *structural* defect — ``ast.parse``
    fails on the literal, not the program. We retry on a copy with every U+FFFD
    replaced by ``x`` so the compile check stays honest about structure while
    tolerating cosmetically-broken literals.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    candidates = [text]
    if "\ufffd" in text:
        candidates.append(text.replace("\ufffd", "x"))
    for candidate in candidates:
        try:
            ast.parse(candidate)
            return True
        except SyntaxError:
            continue
        except Exception:
            continue
    return False


def _python_import_targets(path: Path) -> set[str]:
    """Top-level import targets (module or package bases) used in a Python file.

    Used by the code-aware gate: tests should ideally reference the project's
    own code, not only the standard library. Best-effort and tolerant.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return set()
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.add((a.name.split(".")[0] or "").strip())
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets.add((node.module.split(".")[0] or "").strip())
    return {t for t in targets if t}


def _non_empty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _web_goal(goal: str) -> bool:
    g = goal.lower()
    return any(w in g for w in ("website", "landing", "html", "web app",
                                 "web page", "single page", "single-file"))


_REQUIRED_ARTIFACTS = ("PLAN.md", "ARCHITECTURE.md", "REVIEW.md")
_TEST_FILE = "tests/test_app.py"
_WEB_FILE = "index.html"


def collect_evidence(workspace: str, goal: str = "") -> dict:
    """Collect deterministic evidence from a project workspace.

    Returns:
      {
        "verdict": "GO" | "WARN" | "NO-GO",
        "checks": [{"name": str, "status": "pass"|"fail"|"warn", "detail": str}],
        "summary": str
      }
    """
    root = Path(workspace)
    checks: list[dict] = []
    test_path = root / _TEST_FILE

    # --- 1. test artifact present? (critical) ---
    if _non_empty(test_path):
        checks.append({"name": "test_artifact_present", "status": "pass",
                        "detail": f"{_TEST_FILE} exists and is non-empty"})
    else:
        checks.append({"name": "test_artifact_present", "status": "fail",
                        "detail": f"{_TEST_FILE} missing or empty"})

    # --- 2. test artifact compiles? (critical when present) ---
    if _non_empty(test_path):
        if _parse_ok(test_path):
            checks.append({"name": "test_artifact_compiles", "status": "pass",
                            "detail": "Python syntax is valid"})
        else:
            checks.append({"name": "test_artifact_compiles", "status": "warn",
                            "detail": "Python syntax error in test file"})

    # --- 3. required meta-artifacts present (warning-level) ---
    missing = [a for a in _REQUIRED_ARTIFACTS if not (root / a).is_file()]
    if not missing:
        checks.append({"name": "meta_artifacts_complete", "status": "pass",
                        "detail": "PLAN.md + ARCHITECTURE.md + REVIEW.md all present"})
    else:
        checks.append({"name": "meta_artifacts_complete", "status": "warn",
                        "detail": f"missing: {', '.join(missing)}"})

    # --- 4. workspace file count (informational) ---
    file_count = sum(1 for _ in root.rglob("*") if _.is_file())
    checks.append({"name": "workspace_file_count", "status": "pass" if file_count >= 3 else "warn",
                    "detail": f"{file_count} file(s) in workspace"})

    # --- 4.5 code-aware check (uploaded-project launches only) -------------
    # When a codebase snapshot was seeded into _SOURCE/, the tests should
    # reference the project's OWN modules (not only stdlib) — strong signal the
    # TDD lane actually looked at the real code.  NO-GO only when source exists
    # but the test artifact is entirely stdlib-only (failed to engage).
    source_dir = root / "_SOURCE"
    if source_dir.is_dir() and any(source_dir.rglob("*.py")):
        source_py = [p for p in source_dir.rglob("*.py")
                     if not p.name.startswith("__")]
        source_bases = {p.stem for p in source_py if p.stem}
        if not source_bases:
            source_bases = {p.parent.name for p in source_py if p.parent.name and p.parent != source_dir}
        test_imports = _python_import_targets(test_path) if _non_empty(test_path) else set()
        hits = source_bases & test_imports
        if hits:
            checks.append({"name": "tests_reference_source", "status": "pass",
                            "detail": f"tests import project module(s): {', '.join(sorted(hits)[:5])}"})
        elif _non_empty(test_path):
            checks.append({"name": "tests_reference_source", "status": "warn",
                            "detail": "uploaded source exists but tests import no project module "
                                      "(stdlib-only?) — verify tests target the real code"})
        else:
            checks.append({"name": "tests_reference_source", "status": "warn",
                            "detail": "uploaded source exists but no test artifact produced"})

    # --- 5. web QA (critical for web goals) ---
    if _web_goal(goal):
        web_path = root / _WEB_FILE
        if _non_empty(web_path):
            from demo_llm import web_artifact_needs_repair, web_qa_issues
            text = web_path.read_text(encoding="utf-8", errors="ignore")
            truncated = web_artifact_needs_repair(text)
            issues = web_qa_issues(text)
            hard = [i for i in issues if i.startswith((
                "truncated page:", "skeleton page:"))]
            if not truncated and not hard:
                checks.append({"name": "web_qa", "status": "pass",
                                "detail": "index.html passes structural QA"})
            else:
                detail = "; ".join(hard[:3]) if hard else "truncated artifact detected"
                checks.append({"name": "web_qa", "status": "warn",
                                "detail": detail})
        else:
            checks.append({"name": "web_qa", "status": "warn",
                            "detail": f"{_WEB_FILE} not present (non-web goal?)"})

    # --- 5.5 multi-file build sanity (informational for complex goals) ------
    # A complex builder pack ships extra files (pages/, css/, js/, src/, …).
    # Check that referenced relative href/src targets inside the page actually
    # exist in the workspace and that a web goal's pack is not a broken stub.
    # Informational: never fails the gate on its own, but flags a dangling
    # multi-file structure so the Auditor/human sees it.
    extra_files = sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and str(p.relative_to(root)) not in (
            _WEB_FILE, *_REQUIRED_ARTIFACTS, "EVIDENCE.md")
    )
    if extra_files:
        referenced = set()
        for p in root.rglob(_WEB_FILE):
            try:
                html = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                html = ""
            referenced |= set(re.findall(r'(?:href|src)\s*=\s*["\']([^"\'#?]+)', html))
        dangling = sorted(
            ref for ref in referenced
            if not ref.startswith(("http://", "https://", "mailto:", "tel:"))
            and not (root / ref).is_file())
        if dangling:
            checks.append({"name": "multi_file_links", "status": "warn",
                            "detail": f"{len(dangling)} dangling relative reference(s): "
                                      f"{', '.join(dangling[:5])}"})
        else:
            checks.append({"name": "multi_file_links", "status": "pass",
                            "detail": f"{len(extra_files)} extra workspace file(s), "
                                      "all relative references resolve"})
    else:
        checks.append({"name": "multi_file_links", "status": "info",
                        "detail": "single-file deliverable (no extra workspace files)"})

    # --- Verdict ---
    fail_names = {c["name"] for c in checks if c["status"] == "fail"}
    warn_names = {c["name"] for c in checks if c["status"] == "warn"}

    if "test_artifact_present" in fail_names:
        verdict = "NO-GO"
    elif fail_names or warn_names:
        verdict = "WARN"
    else:
        verdict = "GO"

    summary_parts = []
    for c in checks:
        tag = {"pass": "✓", "warn": "⚠", "fail": "✗"}.get(c["status"], "?")
        summary_parts.append(f"{tag} {c['name']}: {c['detail']}")

    return {
        "verdict": verdict,
        "checks": checks,
        "summary": "\n".join(summary_parts),
    }


def write_evidence_md(workspace: str, goal: str = "") -> dict:
    """Collect evidence and write EVIDENCE.md into the workspace.
    Returns the evidence dict."""
    ev = collect_evidence(workspace, goal)
    root = Path(workspace)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "EVIDENCE.md").write_text(
            f"# Builder Evidence Gate\n\n"
            f"**Verdict:** `{ev['verdict']}`\n\n"
            f"## Checks\n\n" + "\n".join(
                f"| {c['status'].upper()} | {c['name']} | {c['detail']} |"
                for c in ev["checks"]
            ) + "\n\n## Summary\n\n```\n" + ev["summary"] + "\n```\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    return ev
