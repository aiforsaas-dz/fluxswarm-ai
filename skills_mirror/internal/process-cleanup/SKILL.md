---
name: process-cleanup
description: >
  Orphan-worker reaping, process-tree killing, and stale process cleanup
  for fluxswarm boards.  Use when recovering from a crashed dispatch,
  cleaning up orphaned Hermes workers, or diagnosing zombie PIDs on
  the host.
version: 1.0.0
author: fluxswarm
license: MIT
platforms: [linux, macos, windows]
metadata:
  origin: fluxswarm
  hermes:
    tags: [process, cleanup, orphans, reaper, kill, zombies]
    related_skills: [swarm-ops, process-governance]
---

# process-cleanup

How fluxswarm cleans up worker processes after a failed, stalled, or
completed launch.

## When cleanup runs

| trigger | function called |
|---|---|
| Stall detected (no-progress) | `cleanup_board_workers(board)` then seal |
| Timeout (wall-clock expiry) | `cleanup_board_workers(board)` then seal |
| Exception during dispatch | `cleanup_board_workers(board)` |
| Manual seal (`seal_board`) | inline `kill_process_tree` for each PID |

Cleanup is always **inside a finally block** in the driver loop:
if the driver exits for any reason other than clean convergence,
workers are terminated.

## read_worker_pids(board)

Reads `worker_pid` from kanban.db for tasks in state `running`,
`ready`, or `todo`.  Returns a list of PIDs — non-destructive read
only.

## kill_process_tree(pid, grace_s=3.0)

Platform-specific, recursive, leaf-first process-tree termination.

### Windows (Win32 snapshot)

```
1. CreateToolhelp32Snapshot → enumerate all processes
2. Filter direct children of pid
3. Recurse into each child (deepest first)
4. TerminateProcess(root, graceful) → wait grace_s
5. If still alive → TerminateProcess(root, hard)
```

Direct children only are touched; unrelated processes are never
enumerated.

### POSIX (ps --ppid)

```
1. ps --ppid <pid> → list direct children
2. Recurse into each child (deepest first)
3. SIGTERM(root) → wait grace_s
4. If still alive → SIGKILL(root)
```

### Key properties

- **Idempotent**: calling on an already-dead or recycled PID is a safe no-op.
- **Scoped**: only the supplied PID and its descendants are touched.
- **Leaf-first**: deepest children are terminated before the parent.

## Cleanup flow

```
_cleanup_board_workers(board):
  pids = read_worker_pids(board)
  for pid in pids:
    kill_process_tree(pid)
```

Always called on stall/timeout/exception.  On clean convergence
(outcome=ok), the finally block skips cleanup entirely (no workers
to reap).

## Sealing + cleanup

`seal_board` runs its own `kill_process_tree` loop (via
`read_worker_pids`) before flipping tasks to `blocked` and dropping
`board.sealed`.  This is separate from the driver loop's cleanup
and runs on-demand — but is also idempotent and scoped.

## Notes for the human

- This skill is about HOST-LEVEL process cleanup, not about deleting
  Kanban tasks or boards.  Board lifecycle is in `swarm-ops`.
- The `grace_s` default of 3.0 seconds is enough for most Hermes
  workers to exit cleanly.  If a worker is stuck on a provider
  stream (0 CPU, network I/O wait), the process will not exit from
  SIGTERM — the hard kill handles it.
- Killing a recycled PID is a safe no-op.  The reaper does not need
  to verify "is this the same process that was originally spawned"
  before calling `kill_process_tree`.
