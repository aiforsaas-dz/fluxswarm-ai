# FluxSwarm Backlog

Tracked, non-fixed items discovered during the hardening phases. Items here are
PRE-EXISTING (not introduced by any phase) unless stated otherwise, and are
recorded for follow-up only. Fixing an item is scheduled explicitly and is
NOT implied by this list.

## FLUXSWARM_DATABASE_URL cross-file import pollution (PRE-EXISTING)

- **Problem:** tests/files that set the `FLUXSWARM_DATABASE_URL` environment
  variable cause `main.py` to import `db_postgres as db` at process level; this
  selection is made once, at import time, and later persists. When such a file
  runs BEFORE others in a single pytest process (non-canonical test order), the
  later files hit `db_postgres` instead of the sqlite `db` module and fail with
  `ConnectionRefusedPostgreSQL` / PG-mixin CORS-coupling errors.
- **Relevant locations:** `backend/db.py:35` (DB backend selected from
  `FLUXSWARM_DB`/`FLUXSWARM_DATABASE_URL` at import); `backend/db_postgres.py:247`
  (migration mutates `os.environ` with the module-level `DATABASE_URL`).
- **Reproduction:** non-canonical combined test order, e.g. running
  `tests/test_board_persistence.py` ahead of `tests/test_admt_compliance.py`,
  `tests/test_production_prep.py`, `tests/test_ws_hardening.py` in one process.
- **Current status:** PRE-EXISTING / NOT A PHASE 7 REGRESSION.
- **Evidence:** identical A/B failure set (21 failed / 8 errors) reproduced on
  the pre-Phase-7 tree (Phase 7 stashed) and on the Phase-7 tree for the same
  combined command; every affected file passes when run alone or in canonical
  alphabetical order. Full canonical suite: 680 passed / 7 skipped / 39 errors
  (all 39 = PG-only environment, no local PostgreSQL installed).
- **Do not claim fixed.** Only mark resolved when a phase explicitly schedules
  and verifies a fix (e.g. isolating the env mutation at test teardown).
