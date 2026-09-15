---
name: swarm-ops
description: >
  Operator playbook for fluxswarm swarm lifecycle: board creation, launch,
  dispatch loop control, stall detection, sealing, refund, and outcome
  interpretation.  Use when the human asks to run, diagnose, or recover
  a fluxswarm swarm launch.
version: 1.0.0
author: fluxswarm
license: MIT
platforms: [linux, macos, windows]
metadata:
  origin: fluxswarm
  hermes:
    tags: [swarm, dispatcher, kanban, fluxswarm, operations, stall, seal]
    related_skills: [plan-orchestrate, team-agent-orchestration, verification-loop]
---

# swarm-ops

Operator playbook for the fluxswarm swarm lifecycle.  When a human says
"run the swarm", "what happened to board X", or "recover the stalled
launch", this skill is the entry-point.

## Pre-flight checklist (fail-fast, never silent)

```
pre-flight:
  1. resolve_launch_runtime(provider_keys)       # ProviderConfigError if unconfigured
  2. _raise_preflight()                           # HERMES_HOME + hermes.exe exist
  3. preflight_provider(provider_keys)            # probe endpoint, no credits consumed
  4. cleanup_profile_keys()                       # de-fang stale plaintext .env keys
  5. _ensure_verifier_skill()                     # provisioning, byte-for-byte
```

If step 3 returns `CONFIG_ERROR` or `AUTH_ERROR`, DO NOT dispatch — tell
the operator exactly which env var or BYOK key is missing.

## Launch sequence

```
launch_swarm(board, goal, provider_keys):
  1. _resolve_launch_runtime(...)                 # resolve concrete model + provider
  2. _raise_preflight()
  3. cleanup_profile_keys()
  4. _ensure_verifier_skill()
  5. hermes swarm goal --worker ... --verifier ... --synthesizer ... --json
  6. _pin_runtime(board, ...)                     # set-model on every task
```

`launch_from_template` follows the same sequence but builds the squad list
from a marketplace template (AGENT_REGISTRY).

## Dispatch loop & stall detection

```
dispatch(board, timeout_s=600, stall_passes=4, min_wait_s=60):
  deadline = now + timeout_s
  last_sig = None
  unchanged = 0
  heartbeat_grace = 180 s

  loop until deadline:
    sig = (board_activity_sig(board), sorted(states))

    # terminal check
    if all done/blocked → return {outcome: "ok"}

    # progress tracking
    if sig == last_sig → unchanged += 1
    else               → unchanged = 0

    # stall detection (three gates, all required)
    if unchanged >= stall_passes
       AND elapsed >= min_wait_s
       AND heartbeat stale (> heartbeat_grace):
      → cleanup_board_workers(board)
      → return {outcome: "stuck", stall: true, timed_out: true, ...}

  # wall-clock expiry
  → cleanup_board_workers(board)
  → return {outcome: "stuck", timed_out: true}
```

### Critical thresholds

| parameter | default | meaning |
|---|---|---|
| `timeout_s` | 600 | hard loop ceiling per dispatch cycle |
| `DISPATCH_TIMEOUT_S` | 900 | driver-loop hard ceiling (env: FLUXSWARM_DISPATCH_TIMEOUT_S) |
| `stall_passes` | 4 | unchanged passes before no-progress break |
| `min_wait_s` | 60 | grace floor so opening waves are never cut short |
| `heartbeat_grace` | 180 | seconds since last heartbeat before worker is "stale" |
| loop poll | 8 s | sleep between dispatch passes |

### Why three gates for stall detection

A healthy planner doing long reasoning + `kanban_show` may not change
task state for several passes.  The heartbeat signal prevents false
positives: only when (a) signature unchanged for N passes AND
(b) time since start >= min_wait AND (c) last heartbeat > grace
does the detector fire.

## Outcome interpretation

| outcome | meaning | operator action |
|---|---|---|
| `ok` + `terminal=true` | all tasks done or blocked — success | read board artifacts; no re-dispatch needed |
| `stuck` + `stall=true` + `early=true` | no-progress stall detected before timeout | `seal_board(board, reason="stall")`, refund, diagnose |
| `stuck` + `timed_out=true` | wall-clock expired | same as above |
| `error` | launch/exception | inspect exception message; retry with pre-flight fix |

### `stuck_tasks` / `stuck_run_count`

Tasks still `running` or `queued` when the driver stopped.  These are
the ones that would be orphaned without sealing.

## Sealing a board

```
seal_board(board, reason):
  1. kill every tracked worker process tree (read_worker_pids → kill_process_tree)
  2. flip every non-terminal task to blocked (clear claim_lock, worker_pid, claim_expires)
  3. drop board.sealed marker  →  reaper skips this board forever
```

- Idempotent and best-effort; per-row failures degrade the seal.
- Sealing is operator-final: the reaper will not re-dispatch.

### Why seal matters

Without sealing, the reconciliation reaper keeps re-dispatching
stuck boards.  The `running` corpses hold the host concurrency cap,
so every NEW launch lands in "waiting for dependency" and gets
refunded as `no_progress`.

## Refund

After sealing, the operator (or automated caller) triggers a refund
to free the user's task budget.  The sequence is:
seal → refund → end.  Never refund without sealing first, or the
reaper will resurrect the board.

## Process cleanup

`cleanup_board_workers(board)` is called on stall, timeout, or any
exception in the driver loop.  It reads `worker_pid` from kanban.db
and calls `kill_process_tree` on each — only on the current board's
tracked workers, never touching unrelated processes.

## IDLE worker detection (service path)

A worker is IDLE when:
- PID alive with 0 CPU (I/O blocked on provider stream)
- state = ready (released claim / no task assigned)
- NOT board.sealed

This is NOT a swarm-ops lifecycle state — it is a post-dispatch
diagnostic for long-running host workers.  See the IDLE detection
path in hermes_client.py if the human asks about background
resource waste.

## Practical examples

```
# diagnose a board
dispatch(board, blocking=False)  → see first_pass snapshot

# seal and refund a stuck board
seal_board(board, reason="diagnosed: provider outage")
# then trigger refund from the caller / operator panel
```

## Notes for the human

- The dispatcher writes a bounded report — no Prometheus, no Grafana,
  no cost anomalies dashboard in this skill.  If the human asks for
  observability, route to the `infra-observability` skill (separate).
- Never attempt to revive a sealed board.  Sealing is the final word.
