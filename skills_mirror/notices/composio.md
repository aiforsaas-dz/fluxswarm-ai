# notice: composio-community/opencode-skills — NOT vendored

- repo: https://github.com/composio-community/opencode-skills
- ref: master (default branch at time of fetch)
- fetched: 2026-09-15
- license: **null** (GitHub API reports no license)
- skill inspected: `skills/multi-agent-orchestration/SKILL.md`

## What was adopted (idea only, no text copied)

The standard multi-agent orchestration pattern: a Planner/Orchestrator
decomposes a goal into tasks, dispatches them to distinct Worker roles,
a Reviewer validates outputs, and a Merge step assembles the result.
Roles are clearly separated (Orchestrator ≠ Workers ≠ Reviewer).

## What we did NOT copy

No file content was copied byte-for-byte or paraphrased into the
`skills_mirror` tree.  All text in `skills_mirror/internal/` is
fluxswarm's own authorship.

## Where it lives now

The decomposition/dispatch/merge pattern is captured in our own
`swarm-ops` skill (swarm lifecycle, stall detection, sealing) and
the SQUAD/AGENT_REGISTRY structure in hermes_client.py.  The
role separation is hardcoded: ecc-planner, ecc-architect, ecc-devops,
ecc-tdd (workers), ecc-reviewer (verifier), ecc-build-fixer (synthesizer).
