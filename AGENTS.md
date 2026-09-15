# FluxSwarm — Project Instructions

## Response Style
- Execute tool calls first, then reply with ≤5 lines: **what did you do / result / blockers**.
- No explanations or rationale unless explicitly asked.
- Analysis prompts → short structured answer, no preamble.
- If the user requests a verbose deliverable (proposal/analysis/prompt), produce exactly that and nothing more.

## Environment / Procedure
- Repo: `aiforsaas-dz/fluxswarm-ai` — SOLE canonical remote for this project; always push there (NEVER anywhere else). Local dev in `D:\Projects\fluxswarm`. Windows PowerShell 5.1.
- Git push: remote `origin = https://github.com/aiforsaas-dz/fluxswarm-ai.git`. The stale machine credential `Zaindev-lab` lacks push access — do NOT push with it. Use the `aiforsaas-dz` PAT via session-only header: `$basic=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("ZINELABIDINE:$tok"))` then `git -c "http.extraHeader=Authorization: Basic $basic" -c credential.helper= push origin master` (never write the token to a file).
- No direct `python`; use `uv run --python 3.11` (py_compile) and `uv run --with-requirements backend/requirements.txt --with pytest pytest backend/tests [file::test] -q` for tests.
- PowerShell mangles JSON quotes in curl `-d`: write JSON to a temp file (UTF-8, no BOM) and use `curl.exe --data-binary "@file"`. Uploads via `curl.exe -F`.
- Render service id `srv-dajumhrm8hqs739pqajg`, URL `https://fluxswarm-i1br.onrender.com`.
- Deploy: POST `/v1/services/<svc>/deploys` with `{"clearCache":"do_not_clear"}` + Render API key. Verify live after deploy (page 200, login 200, analyze snapshot).
- Keys live in dedicated env/secret store; never print or commit them.

## Web Delivery Contract (visual QA gate — `_WEB_BUILD_SPEC` + `web_qa_issues` in `backend/demo_llm.py`)
- Every web goal (EN/AR/FR/ES/PT/TR/ID/DE/HI/ZH and more) MUST produce a designed single-file `index.html`: exactly one `<style>` block, >=6 CSS rules, >=3 distinct colors, border-radius, gradients/shadows, >=2 CTA buttons, clamp() typography, ~1140px layout, sticky nav + anchors + footer, responsive mobile menu, motion/hover, NO placeholder/filler copy.
- Hard visual defects auto-trigger a full rebuild round (repair prompt): missing `<style>`, <6 rules ("unstyled page"), <3 colors ("flat palette"), no border-radius, no gradients/shadows, no buttons/CTAs, truncated, malformed, sandbox-unsafe.
- Non-web goals ship a `.md` deliverable instead (budget `_DELIVERABLE_MAX_TOKENS`); web goals get web-scaled builder budget (default up to `_BUILDER_MAX_TOKENS`).
- Lighter patches (no rebuild): dead-link anchor retarget to `#top`, hollow/gap sections filled via content-patch, missing `</body></html>` tail. These call the model once more but do NOT count as rebuild rounds.

## Fixture convention (backend/tests/test_demo_thin.py)
- Any fixture asserting "no rebuild round" must be visually designed: wrap body in `_styled_body(...)` (uses `_CSS_FOUNDATION`) and include a CTA `<button class='btn'>` in the footer so the visual gate passes and only the tested issue remains.
- `_polished_page()` = fully passing reference page; use it as the `good` case in QA tests.
- Tests asserting token budgets: non-web builder == `_DELIVERABLE_MAX_TOKENS`; web builder == `demo_builder_max_tokens(...)`.

## Multi-file build protocol (complex goals — `backend/demo_llm.py` + `backend/main.py`)
- `complex_objective(objective)` flags real complexity (multi-page, api/backend, auth/login, database, payments, admin/dashboard/portal, "with an X" big modules). Conservative: "menu", "landing page" alone are NOT complex.
- For complex goals the Builder emits a file pack: primary file first (still `index.html` for web — `/p/` renders it live), then extra files delimited by `==== FILE: <relpath>` lines (pages/, css/, js/, src/, requirements.txt…). `split_artifact()` parses it (path-safe: rejects `..`, absolute, dupes → `_skipped`). `_run_builder._split_pack()` writes each extra file into the workspace right after EVERY build attempt (incl. repairs). Files flow to `/p/`, export zip and the Project Files browser automatically.
- Budgets: complex goals get `_COMPLEX_EXTRA_TOKENS` (default 4000) headroom above `_BUILDER_MAX_TOKENS`, capped at `_COMPLEX_MAX_TOKENS` (default 12000). Non-web simple deliverables keep `_DELIVERABLE_MAX_TOKENS`.
- Evidence gate adds `multi_file_links` check: verifies relative href/src targets resolve inside the workspace (WARN on dangling); informational `info` for single-file builds.

## Known history
- Deployed: `de2a24e` (codebase-context: snapshot extraction, per-user cache, code-aware evidence gate) then `d1ebd9b` (visual QA gate + multilingual web detection + 8000-token builder budget) then `3a55d80` (preview fix), then `e95bb2d` (chained tail-completion for truncated pages).
- Multi-file build protocol (complex multi-page / API+frontend goals) added on top of the 8-lane squad: `complex_objective`, `split_artifact`, `multi_file_protocol`, `_split_pack` in `_run_builder`, `multi_file_links` evidence check. Tests in `backend/tests/test_multi_file_build.py` (17 tests).
- Thin project squad is now **8 lanes**: Planner → Architect → DevOps → TDD → Reviewer → **Designer** (writes `DESIGN.md`: concrete palette/type/token system, fed verbatim into the Builder) → Builder → **Auditor** (writes `AUDIT.md` summarizing the FINAL page's deterministic QA). Lanes live in `_thin_project_lanes()` (`backend/hermes_client.py`), role labels in `ROLE_INFO`, prompts in `backend/demo_llm.py` (`designer_prompt`/`auditor_prompt`), rank in `_ADMT_AGENTS`.
- Evidence gate (`backend/evidence_gate.py`): `_parse_ok` tolerates mojibake (U+FFFD) inside string/bytes literals — cosmetic, not structural. The thin driver re-runs `write_evidence_md` AFTER the Builder so the `web_qa` check audits the ACTUAL page (before the fix it ran pre-build and wrongly reported "index.html not present"). Auditor lane prompt is fed via `_web_qa_summary()` in `backend/main.py`.
- Preview embed contract (`/p/*`): the dashboard frames previews WITHOUT `allow-same-origin` → opaque origin. NEVER send `X-Frame-Options: SAMEORIGIN` on `/p/` (Firefox refuses it against the sandbox origin — user sees a "connection not authorized" browser page). Embed side = CSP `frame-ancestors 'self'` only. Global routes keep `frame-ancestors 'none'` + `X-Frame-Options: DENY`.
- `read_workspace()` must include `.html` (it did not until `3a55d80`), otherwise evidence gate and workspace listing hide the built `index.html`.
- Earlier root-cause fix: JS syntax error (missing comma in I18N object) killed the whole inline script.
- Secrets rotated previously after accidental exposure; treat any printed key as compromised.