# fluxswarm skills_mirror — MANIFEST

Guarantee: no runtime dependency on GitHub accounts. Every skill fluxswarm
consumes has a local, versioned, license-clean copy under this tree. If the
upstream accounts are deleted, the project builds, provisions, and runs from
this mirror alone.

## Layout

- `vendor/` — byte-for-byte copies of permissively-licensed upstream skills,
  each shipped with its original LICENSE and a per-dir PROVENANCE.md.
- `internal/` — fluxswarm-authored skills (origin: fluxswarm), written by us so
  no unlicensed upstream text is copied. One dir per skill.
- `notices/` — documents upstream repos we studied but could NOT vendor
  (no license exposed by the GitHub API), stating the exact idea we took and
  that no text was copied.
- `provision_skills.py` — idempotent, non-destructive copy into
  `HERMES_HOME/skills/ecc/skills/`, mirroring `_ensure_verifier_skill` semantics.

## Provenance summary

| upstream | repo / ref | license (API) | vendored | how used |
|---|---|---|---|---|
| dimkurilo wave-spec | dimkurilo/opencode-skills @ main | MIT | yes, byte-for-byte + LICENSE | reference / gate methodology |
| multi-agent-orchestration | composio-community/opencode-skills @ master | none (null) | NO — no license | idea only (see notices/composio.md) |
| reviewer / infosec | garethrhughes/skills @ main | none (null) | NO — no license | idea only (see notices/garethrhughes.md) |

## Licensing rule

Anything with `license: null` in the GitHub API is all-rights-reserved by
default. We never copy those files. We may adopt the *idea/pattern* and encode
it in our own words in `internal/`.

Full per-file detail: see `PROVENANCE.md`.