"""
Optional knowledge-graph integrations for completed FluxSwarm projects.

Cognee (Apache-2.0) — seeds kanban board data into an entity-relationship
graph for cross-project memory and retrieval.
    Enable: pip install cognee  +  FLUXSWARM_COGNEE_SEED=1

Understand Anything (MIT) — generates an interactive knowledge graph of the
project workspace via the ``/understand`` skill running in an installed agent
CLI (opencode).  Enable the skill, then:
    Enable: install understand-anything  +  FLUXSWARM_UNDERSTAND_ANY=1

Both integrations are best-effort and never raise; callers can invoke
``on_project_completed`` unconditionally from the finalize path.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

_ENV_COGNEE = "FLUXSWARM_COGNEE_SEED"
_ENV_UA = "FLUXSWARM_UNDERSTAND_ANY"
_UA_TIMEOUT_S = int(os.environ.get("FLUXSWARM_UNDERSTAND_ANY_TIMEOUT_S", "600"))
_UA_CLI = os.environ.get("FLUXSWARM_UNDERSTAND_ANY_CLI", "opencode")
_OPCODE_SKILL_DIRS = (
    Path(os.path.expanduser("~/.understand-anything-plugin")),
    Path(os.path.expanduser("~/.config/opencode/skills/understand")),
    Path(os.path.expanduser("~/.agents/skills/understand")),
)


def _cognee_available() -> bool:
    if os.environ.get(_ENV_COGNEE) != "1":
        return False
    try:
        import cognee  # noqa: F401
        return True
    except ImportError:
        return False


def _ua_available() -> bool:
    if os.environ.get(_ENV_UA) != "1":
        return False
    try:
        r = subprocess.run([_UA_CLI, "--version"], capture_output=True, timeout=10)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    # The Understand-Anything skill must be installed for the CLI (junction or
    # plugin root present) before a headless skill invocation can do anything.
    return any(p.exists() for p in _OPCODE_SKILL_DIRS)


def collect_board_knowledge(board: str) -> dict:
    from hermes_client import HERMES_HOME

    db_path = Path(HERMES_HOME) / "kanban" / "boards" / board / "kanban.db"
    if not db_path.exists():
        return {"board": board, "tasks": [], "events": [], "attachments": []}

    c = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        tasks = [
            {"id": r[0], "title": r[1], "status": r[2], "assignee": r[3]}
            for r in c.execute(
                "SELECT id, title, status, COALESCE(assignee, '') FROM tasks"
            ).fetchall()
        ]
        events = [
            {"task_id": r[0], "kind": r[1], "payload": r[2], "ts": r[3]}
            for r in c.execute(
                "SELECT task_id, kind, payload, created_at FROM task_events "
                "ORDER BY created_at"
            ).fetchall()
        ]
    finally:
        c.close()

    from hermes_client import project_attachments_dir

    attach_dir = project_attachments_dir(board)
    attachments = []
    if attach_dir.exists():
        for p in sorted(attach_dir.iterdir()):
            if p.is_file():
                attachments.append({"name": p.name, "size": p.stat().st_size})

    return {"board": board, "tasks": tasks, "events": events, "attachments": attachments}


def seed_cognee_memory(board: str) -> dict:
    if not _cognee_available():
        return {"skipped": True, "reason": "cognee not installed or disabled"}

    knowledge = collect_board_knowledge(board)
    try:
        import cognee

        texts = [json.dumps(knowledge, ensure_ascii=False)]
        for att in knowledge.get("attachments", []):
            texts.append(f"Attachment: {att['name']} ({att['size']} bytes)")

        for t in texts:
            cognee.add(t)

        cognee.cognify()
        log.info("cognee: seeded board %s (%d tasks)", board, len(knowledge.get("tasks", [])))
        return {"skipped": False, "tasks": len(knowledge.get("tasks", []))}
    except Exception as exc:
        log.warning("cognee seed failed for %s: %s", board, exc)
        return {"skipped": False, "error": str(exc)}


def run_understand_anything(board: str, workspace: str | None = None) -> dict:
    if not _ua_available():
        return {"skipped": True, "reason": "ua not installed or disabled"}

    from hermes_client import project_workspace_dir

    ws = Path(workspace) if workspace else project_workspace_dir(board)
    if not ws.exists():
        return {"skipped": True, "reason": "workspace not found"}

    try:
        result = subprocess.run(
            [_UA_CLI, "run", f"Use the understand skill to analyze this project",
             "--project", str(ws)],
            capture_output=True, text=True,
            timeout=_UA_TIMEOUT_S, cwd=str(ws),
        )
        # Skill success is measured by its output artifact, not the exit code.
        graphs = list(ws.glob(".ua/knowledge-graph.json"))
        graphs += list(ws.glob(".understand-anything/knowledge-graph.json"))
        if graphs:
            log.info("understand-anything: knowledge graph built for board %s", board)
            return {"skipped": False, "ok": True, "graph": str(graphs[0])}
        tail = (result.stderr or result.stdout or "")[-300:]
        log.warning("understand-anything: no graph produced for %s: %s", board, tail)
        return {"skipped": False, "ok": False, "output": tail}
    except subprocess.TimeoutExpired:
        return {"skipped": False, "error": "timeout"}
    except FileNotFoundError:
        return {"skipped": True, "reason": f"ua skill/CLI not installed ({_UA_CLI})"}
    except Exception as exc:
        return {"skipped": False, "error": str(exc)}


def on_project_completed(board: str, workspace: str | None = None) -> dict:
    report: dict = {"cognee": {}, "understand_anything": {}}
    try:
        report["cognee"] = seed_cognee_memory(board)
    except Exception as exc:
        report["cognee"] = {"error": str(exc)}
    try:
        report["understand_anything"] = run_understand_anything(board, workspace)
    except Exception as exc:
        report["understand_anything"] = {"error": str(exc)}
    return report
