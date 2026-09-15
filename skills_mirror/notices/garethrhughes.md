# notice: garethrhughes/skills — NOT vendored

- repo: https://github.com/garethrhughes/skills
- ref: main (default branch at time of fetch)
- fetched: 2026-09-15
- license: **null** (GitHub API reports no license)
- skills inspected: `reviewer/SKILL.md`, `infosec/SKILL.md`

## What was adopted (idea only, no text copied)

Stage-gate review methodology: artifacts are evaluated against a
checklist and assigned a verdict of PASS or BLOCK (hard gate, no
partial).  ISO-27001 / compliance-aligned scoring is referenced
as a quality standard for code review.

## What we did NOT copy

No file content was copied byte-for-byte or paraphrased into the
`skills_mirror` tree.  All text in `skills_mirror/internal/` is
fluxswarm's own authorship.

## Where it lives now

The PASS/BLOCK evidence-gate pattern is captured in our own
`artifact-review` skill.  The quality-gate / completeness-check
structure is embedded in that skill's scoring table (completeness,
buildability, quality, safety gates).  Our existing `GATE-4`
methodology (in the `audit/` directory) independently uses a
similar evidence-gate approach; the garethrhughes pattern validated
and reinforced that existing design rather than introducing it.
