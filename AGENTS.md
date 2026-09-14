# FluxSwarm — Project Instructions

## Response Style
- Execute tool calls first, then reply with ≤5 lines: **what did you do / result / blockers**.
- No explanations or rationale unless explicitly asked.
- Analysis prompts → short structured answer, no preamble.
- If the user requests a verbose deliverable (proposal/analysis/prompt), produce exactly that and nothing more.

## Environment / Procedure
- Repo: `aiforsaas-dz/fluxswarm-ai` (local dev in `D:\Projects\fluxswarm`). Windows PowerShell 5.1.
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

## Known history
- Deployed: `de2a24e` (codebase-context: snapshot extraction, per-user cache, code-aware evidence gate) then `d1ebd9b` (visual QA gate + multilingual web detection + 8000-token builder budget) then `3a55d80` (preview fix), then `e95bb2d` (chained tail-completion for truncated pages).
- Thin project squad is now **8 lanes**: Planner → Architect → DevOps → TDD → Reviewer → **Designer** (writes `DESIGN.md`: concrete palette/type/token system, fed verbatim into the Builder) → Builder → **Auditor** (writes `AUDIT.md` summarizing the FINAL page's deterministic QA). Lanes live in `_thin_project_lanes()` (`backend/hermes_client.py`), role labels in `ROLE_INFO`, prompts in `backend/demo_llm.py` (`designer_prompt`/`auditor_prompt`), rank in `_ADMT_AGENTS`.
- Evidence gate (`backend/evidence_gate.py`): `_parse_ok` tolerates mojibake (U+FFFD) inside string/bytes literals — cosmetic, not structural. The thin driver re-runs `write_evidence_md` AFTER the Builder so the `web_qa` check audits the ACTUAL page (before the fix it ran pre-build and wrongly reported "index.html not present"). Auditor lane prompt is fed via `_web_qa_summary()` in `backend/main.py`.
- Preview embed contract (`/p/*`): the dashboard frames previews WITHOUT `allow-same-origin` → opaque origin. NEVER send `X-Frame-Options: SAMEORIGIN` on `/p/` (Firefox refuses it against the sandbox origin — user sees a "connection not authorized" browser page). Embed side = CSP `frame-ancestors 'self'` only. Global routes keep `frame-ancestors 'none'` + `X-Frame-Options: DENY`.
- `read_workspace()` must include `.html` (it did not until `3a55d80`), otherwise evidence gate and workspace listing hide the built `index.html`.
- Earlier root-cause fix: JS syntax error (missing comma in I18N object) killed the whole inline script.
- Secrets rotated previously after accidental exposure; treat any printed key as compromised.