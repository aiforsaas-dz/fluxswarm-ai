# PROVENANCE — per-file source records

Fetched 2026-09-15 (GMT). All raw fetches via raw.githubusercontent.com over
TLS. Checksums are SHA-256 of the fetched bytes as stored in this tree.

## vendor/dimkurilo-wave-spec

- source: https://github.com/dimkurilo/opencode-skills
  ref: main (default branch)
- files:
  - `SKILL.md` — raw.githubusercontent.com/dimkurilo/opencode-skills/main/skills/wave-spec/SKILL.md
  - `LICENSE` — raw.githubusercontent.com/dimkurilo/opencode-skills/main/LICENSE
- license: MIT (LICENSE file present upstream; GitHub API reports mit)
- vendored: yes, byte-for-byte, license kept
- hashes (SHA-256):
  - SKILL.md: 9A0C94E256A83B686747394DF137107CE1FFCC9E01C2AB57B6643BCAF83E3B7E
  - LICENSE: 8ABCFF0A24126195E0FA45EB687227B64AF746BA4D7FB68444F324B2C3AFCD00
- use: reference / gate methodology only; not auto-provisioned by default.

The authoritative record is the file bytes in this tree (hashes above computed
from the stored copies).

## vendor/* NOT vendored (license: null upstream)

| upstream repo | license via API | decision |
|---|---|---|
| composio-community/opencode-skills @ master | null | NOT copied. Idea only: decompose dispatch/merge with distinct Orchestrator / Worker / Reviewer roles. See notices/composio.md. |
| garethrhughes/skills @ main | null | NOT copied. Idea only: stage-gate (PASS/BLOCK) review + ISO-27001 scoring. See notices/garethrhughes.md. |

Both were already incorporated into our own-authored `internal/*` skills in
fluxswarm's own words — see notices for the mapping.