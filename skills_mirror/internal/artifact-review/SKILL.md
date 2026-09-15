---
name: artifact-review
description: >
  Evidence-gate review of board artifacts: read-back verification,
  quality scoring, and the PASS/BLOCK verdict for fluxswarm swarm
  outputs.  Use when a board reaches terminal state and the human
  wants to inspect or review the produced artifacts.
version: 1.0.0
author: fluxswarm
license: MIT
platforms: [linux, macos, windows]
metadata:
  origin: fluxswarm
  hermes:
    tags: [review, artifacts, evidence, quality, pass-block, verification]
    related_skills: [verification-loop, requesting-code-review]
---

# artifact-review

Evidence-gate review for fluxswarm board outputs.  When a swarm
reaches terminal state (`outcome=ok`), this skill guides the human
through reading, verifying, and scoring the produced artifacts.

## When to use

- Board reaches `outcome=ok` with `terminal=true`
- Human says "review the output", "what did the swarm produce",
  or "what's on the board"
- Before marking a deliverable as shippable

## Board artifact locations

Board artifacts live under:
```
HERMES_HOME/kanban/boards/<board-slug>/
  kanban.db          ← task state, events, worker_pids
  <task-id>/         ← per-task workspace (artifacts, evidence)
  board.sealed       ← present = operator-final, reaper skips
```

`kanban.db` schema fields of interest:
- `tasks.state` — done / blocked / running / ready / todo
- `tasks.worker_pid` — PID that last claimed the task
- `tasks.claim_lock` — locks task to a worker
- `tasks.last_heartbeat_at` — worker liveness signal
- `task_events` — timestamped event stream (count + recency matter)

## Review flow

### Step 1: read board state

```
list_tasks(board) → [{id, state, title, ...}]
board_is_sealed(board) → bool
```

### Step 2: read artifacts per task

For each task in state `done`:
- Read the task's workspace directory for produced files
- Check for generated code (.py), content (.md/.txt), structured data (.json/.yaml)
- Note file sizes and timestamps

### Step 3: evidence-gate scoring

Each produced artifact is checked against:

| gate | check | outcome |
|---|---|---|
| **completeness** | Does the artifact match the task title/goal? | PARTIAL / COMPLETE |
| **buildability** | Does code compile/run without errors? | PASS / BLOCK |
| **quality** | Does it follow project conventions? | PASS / BLOCK |
| **safety** | No secrets, no unsafe patterns? | PASS / BLOCK |

### Step 4: verdict

- **PASS**: artifact is production-ready or human-reviewable
- **BLOCK**: artifact has defects that must be fixed; human decides
  whether to re-dispatch or fix manually

## Board sealed = final

A sealed board (`board.sealed` marker present) is operator-final.
No re-dispatch, no re-approval, no artifact mutation.  The reaper
skips it.  If the human asks "can we re-run on this board",
the answer is: create a new board.

## Reading task_events

The event stream is monotonic and timestamped.  The dispatcher's
stall detector relies on event count + recency as a liveness
signal (see `swarm-ops` skill).  In artifact review, the event
stream helps reconstruct what the worker actually did — useful
for debugging a BLOCK verdict.

## Practical examples

```
# inspect a board
dispatch(board, blocking=False) → first_pass snapshot with task list

# list completed tasks
list_tasks(board) → filter state=done

# check if board is sealed
board_is_sealed(board) → True means final, no changes possible
```

## Notes for the human

- Artifact review is a READ-ONLY operation — it does not modify
  board state or task assignments.
- For code-specific review (security, linting, type-checking), use
  `verification-loop` or `requesting-code-review` as complementary
  skills — this skill provides the structural/evidence framework.
- Sealed boards cannot be unsealed.  If the human wants to undo a
  seal, explain this is an operator-final action and a new board
  should be created.
