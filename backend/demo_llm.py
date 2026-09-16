from __future__ import annotations

import ast
import json
import os
import re
import time
import urllib.error
import urllib.request

_GEMINI_KEY_ENV = "GEMINI_API_KEY"
_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

_KEY_ENV = {
    "gemini": _GEMINI_KEY_ENV,
    "openrouter": _OPENROUTER_KEY_ENV,
}

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_COMPLETION_TIMEOUT_S = int(os.environ.get("FLUXSWARM_DEMO_LLM_TIMEOUT_S", "300"))

# The builder lane (the artifact the user actually sees in /p/) gets a much
# larger output budget: a Lovable-quality single-file page needs a rich design
# system + real copy. 8000 tokens ≈ a complete styled page with palette, cards,
# buttons and shapes. Operators cap it per-env.
_BUILDER_MAX_TOKENS = int(os.environ.get("FLUXSWARM_BUILDER_MAX_TOKENS", "8000"))
# Complex multi-file builds (multi-page sites, API+frontend, …) get headroom
# above the single-file budget so the Builder can ship several COMPLETE files
# in one pass.  Ceiled at a safe default (see below) so an 8k base can't be
# blown past a model's output cap by an aggressive operator setting.
_COMPLEX_EXTRA_TOKENS = int(os.environ.get("FLUXSWARM_COMPLEX_BUILDER_EXTRA_TOKENS", "4000"))
_COMPLEX_MAX_TOKENS = int(os.environ.get("FLUXSWARM_COMPLEX_BUILDER_MAX_TOKENS", "12000"))
# Generic (non-web) build deliverables (app.py, README.md, …) get an upgraded
# dedicated budget so real project code comes back COMPLETE, not truncated.
# The reviewer doc lane keeps the lean default.
_DELIVERABLE_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DELIVERABLE_MAX_TOKENS", "2400"))
_NORMAL_MAX_TOKENS = 800
# The Planner lane (PLAN.md) now emits a full EXECUTABLE implementation plan
# (requirements/acceptance criteria/tasks with traceability + dependencies,
# risks, components, test expectations, deployment impact), which no longer
# fits the lean 800-token doc budget. It gets its own dedicated budget.
_PLAN_MAX_TOKENS = int(os.environ.get("FLUXSWARM_PLAN_MAX_TOKENS", "2400"))
# The Architect lane (ARCHITECTURE.md) now emits an EXECUTABLE implementation
# blueprint with decisions, components (each tied to realized source keys /
# paths / interfaces / data flow), consistency + reproducibility rules, and
# contract-preservation checks — it needs the same dedicated headroom the
# executable Planner lane got. Default 2400, env-tunable.
_ARCH_MAX_TOKENS = int(os.environ.get("FLUXSWARM_ARCH_MAX_TOKENS", "2400"))  # (Phase 9)
# The Designer lane (DESIGN.md) emits an EXECUTABLE visual design system: exact palette hexes, type scale, spacing/radius/shadow tokens, per-component styling rules with concrete paths + interfaces + data flow, responsive + interaction states + accessibility. It needs the same dedicated headroom the executable Planner/Architect lanes got. Default 2400, env-tunable.
_DESIGN_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DESIGN_MAX_TOKENS", "2400"))
# The DevOps lane (DEVOPS.md) emits an EXECUTABLE deployment blueprint: pinned
# reproducible build, dependency/lockfile correctness, env configuration,
# build/start scripts, health checks, CI/CD, deployment impact. It needs the
# same dedicated headroom the executable Planner/Architect/Designer lanes got.
# Default 2400, env-tunable.
_DEVOPS_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DEVOPS_MAX_TOKENS", "2400"))
# The TDD lane (tests/test_app.py) emits an EXECUTABLE pytest blueprint: a
# grep/ast-valid Python test file whose embedded _CONTRACT declares the focused
# cases (id T-n + name + target + concrete assertion), consistency, keys, and
# decisions AT-n. It needs dedicated headroom so a fresh run rebuilds the same
# deterministic test suite. Default 2400, env-tunable.
_TDD_MAX_TOKENS = int(os.environ.get("FLUXSWARM_TDD_MAX_TOKENS", "2400"))


# Demo builder budget: big enough for a complete single-file page but small
# enough to finish inside the demo wall-clock cap on the free pool (each pool
# completion also sits under the per-call timeout). Real project launches keep
# the full _BUILDER_MAX_TOKENS budget.
_DEMO_BUILDER_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DEMO_BUILDER_MAX_TOKENS", "8000"))

# Transient upstream 5xx/429 are a fact of the free pool: retry a bounded
# number of times with small backoff so a 503 hiccup mid-swarm doesn't park the
# whole project board (the thin path would otherwise finalize launch_error at
# the first lane to sneeze). 4xx (401/404/…) is never retried.
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
_THIN_RETRIES = int(os.environ.get("FLUXSWARM_THIN_RETRIES", "2"))
_THIN_RETRY_BACKOFF_S = float(os.environ.get("FLUXSWARM_THIN_RETRY_BACKOFF_S", "2.0"))

_HTTP_CODE_RE = re.compile(r" HTTP (\d{3}):")

# Objectives that describe a browsable website/web app should produce a single
# self-contained index.html — the project preview (/p/<slug>/) then renders it
# as a live Lovable-style page instead of a plain file listing.  The hints are
# intentionally multilingual (EN/AR/FR/ES/PT/TR/ID/DE/HI/ZH...) because goals
# are written by real people in their own language — an Arabic "موقع ويب" must
# take the same web path as an English "website".
_WEB_HINTS = (
    # English
    "web app", "webapp", "web application", "website", "web page", "webpage",
    "landing", "landingpage", "landing page", "single-page", "spa", "dashboard",
    "frontend", "portfolio", "saas", " ui", "ui ", " ecommerce", "menu",
    "restaurant", "cafe", "café", "bakery", "dishes", "pricing", "catalog",
    "catalogue", "store", "storefront", "shop", "booking", "reservation",
    "blog", "gallery", "template", "marketing", "agency", "startup",
    "ordering", "takeaway", "e-commerce", "store page", "website ", "app page",
    # Arabic
    "موقع", "موقع ويب", "صفحة", "صفحة هبوط", "صفحة رئيسية", "لاندينغ",
    "لاندنج", "واجهة", "تطبيق ويب", "ويب", "متجر", "نشاط تجاري", "مطعم",
    "مقهى", "كافيه", "المطعم", "قائمة طعام", "المينو", "حجوزات", "صور",
    "فهرس", "تعريفي", "موقع تعريفي", "بورتفوليو", "أعمال", "سايت", "قالب",
    # Español / Français / Português
    "página web", "sitio web", "landing", "tienda", "restaurante", "cafetería",
    "aplicación web", "page d'accueil", "site web", "boutique", "restaurant",
    "application web", "página de destaque", "site de venda", "loja",
    # Türkçe / Italiano / Deutsch
    "web sitesi", "site", "restoran", "kafe", "mağaza", "web sitesi satır",
    "sito web", "pagina", "negozio", "ristorante", "website", "webseite",
    "shop", "menü", "restaurant", "café",
    # Hinglish / Filipino / Bahasa / हिन्दी / 中文 / 日本語 / 한국어 / Русский / polski
    "वेबसाइट", "वेब पेज", "लैंडिंग पेज", "वेब एप", "网站", "网页", "落地页",
    "ホームページ", "ランディングページ", "웹사이트", "웹 페이지", "веб-сайт",
    "веб-страница", "страница", "strona internetowa", "начиная страница",
    "مصر", "مصر واجهة",
)

# Stronger single-token catch-alls that almost always mean a browsable page.
_WEB_STRONG_HINTS = (
    "http", "html", "الموقع", "موقع", "صفحة", "ويب", "متجر", "مقهى", "مطعم",
    "redesign my website", "style",
)

# Every objective carries at least one of these words → there's a non-trivial
# chance the user wants a visible page even without an explicit 'web' word
# (common Arabic phrasing: "صمم لي منصة", "اعمل صفحة فعاليات").
_WEB_WEAK_HINTS = (
    "موقع", "صفحة", "منصة", "متجر", "نافذة", "واجهة مستخدم", "ui",
)


class DemoLLMError(RuntimeError):
    """A real, non-silent failure for the thin demo executor."""


def _http_code(exc: BaseException) -> int | None:
    m = _HTTP_CODE_RE.search(str(exc))
    return int(m.group(1)) if m else None


def _call_with_retries(request, attempts: int) -> str:
    last: DemoLLMError | None = None
    for i in range(max(1, attempts)):
        try:
            return request()
        except DemoLLMError as exc:
            last = exc
            code = _http_code(exc)
            if code is None or code not in _RETRYABLE_HTTP:
                raise
            if i == attempts - 1:
                raise
            time.sleep(_THIN_RETRY_BACKOFF_S * (i + 1))
    raise DemoLLMError("retries exhausted") from last  # pragma: no cover


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_COMPLETION_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore")[:400]
        except Exception:
            pass
        raise DemoLLMError(f"{provider_hint(exc)} HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise DemoLLMError(f"demo LLM request failed: {exc}") from exc


def provider_hint(exc: BaseException) -> str:
    return "gemini" if "generativelanguage" in getattr(exc, "url", "") else "openrouter"


def completion(provider: str, model: str, prompt: str, max_tokens: int = 400,
               api_key: str | None = None) -> str:
    """One real, bounded completion for the thin executor.

    ``api_key`` optionally overrides the env key (used by the project path to
    honor a user's BYOK key in-process, without ever writing it to disk). The
    demo only ever lands on gemini or the openrouter free fallback; anything
    else fails loudly rather than silently minting a keyed path we have not
    audited.
    """
    provider = (provider or "").strip().lower()
    key_env = _KEY_ENV.get(provider)
    if not key_env:
        raise DemoLLMError(f"thin demo executor has no completion path for provider={provider!r}")
    key = (api_key or os.environ.get(key_env, "")).strip()
    if not key:
        raise DemoLLMError(f"missing {key_env} for the demo executor")

    if provider == "gemini":
        url = _GEMINI_URL.format(model=model, key=key)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
        }

        def _run_gemini() -> str:
            data = _post_json(url, payload)
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DemoLLMError(
                    f"gemini completion shape unexpected: {str(data)[:300]}") from exc
            return text.strip()

        return _call_with_retries(_run_gemini, attempts=_THIN_RETRIES + 1)

    if provider == "openrouter":
        url = _OPENROUTER_URL
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }

        def _run_openrouter() -> str:
            data = _post_json(url, payload, headers={"Authorization": f"Bearer {key}"})
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DemoLLMError(
                    f"openrouter completion shape unexpected: {str(data)[:300]}") from exc
            return text.strip()

        return _call_with_retries(_run_openrouter, attempts=_THIN_RETRIES + 1)

    raise DemoLLMError(f"unhandled demo provider: {provider!r}")


def _is_web_objective(objective: str) -> bool:
    lower = (objective or "").lower()
    if any(h in lower for h in _WEB_HINTS):
        return True
    # Strong signals override a NO answer regardless of language.
    if any(h in lower for h in _WEB_STRONG_HINTS):
        return True
    return False


def complex_objective(objective: str) -> bool:
    """Detect if a goal implies a multi-file or multi-module project
    (multi-page sites, API/backend+frontend, auth, databases, payments, …).

    Deliberately conservative: weak catch-all substrings (\"a website with\")
    are NOT enough on their own — the word must be a concrete complexity
    feature (pages beyond the landing one, an API, a database, auth, …).
    """
    lower = (objective or "").lower()
    if not lower:
        return False
    # Strong single-term features that almost always force more than one file.
    strong_terms = (
        "multi-page", "multipage", "multi page",
        "login", "signup", "sign up", "registration", "authentication",
        "database", "postgresql", "postgres", "mysql", "sqlite", "redis",
        "payments", "payment gateway", "stripe", "checkout",
        "rest api", "restful api", "graphql", "grpc", "websocket",
        "api +", "+ api", " api ", "backend", "frontend +", "+ frontend",
        "microservice", "micro-services", "docker compose", "kubernetes",
        "realtime", "real-time",
    )
    if any(t in lower for t in strong_terms):
        return True
    # Admin/dashboard/portal goals are big enough to split into multiple files.
    portal_terms = (
        "admin panel", "admin dashboard", "management dashboard",
        "portal", "cms", "crm", "saas platform", "multi-tenant",
    )
    if any(t in lower for t in portal_terms):
        return True
    # \"X with a/some Y\" structures where Y is a concrete big module.
    with_terms = (
        "with an admin", "with admin", "with a dashboard", "with dashboard",
        "with a database", "with database", "with a payment", "with payments",
        "with authentication", "with auth", "with a login", "with login",
        "with user accounts", "with an api", "with api", "with a backend",
        "with backend", "with a frontend", "with frontend",
        "with multiple pages", "with several pages", "with a blog",
        "with a forum", "with a marketplace", "with a chat",
    )
    if any(t in lower for t in with_terms):
        return True
    return False


def builder_max_tokens(objective: str = "") -> int:
    """Output-budget for the final deliverable lane (what /p/ renders live)."""
    base = _BUILDER_MAX_TOKENS
    if complex_objective(objective):
        return min(_COMPLEX_MAX_TOKENS, base + _COMPLEX_EXTRA_TOKENS)
    if not objective or _is_web_objective(objective) or "html" in (objective or "").lower():
        return base
    return _DELIVERABLE_MAX_TOKENS


def demo_builder_max_tokens(objective: str = "") -> int:
    """Demo-only builder budget: fits the demo wall-clock cap and per-call
    timeout on the free pool while still yielding a complete single file."""
    if not objective or _is_web_objective(objective) or "html" in (objective or "").lower():
        return _DEMO_BUILDER_MAX_TOKENS
    return _DELIVERABLE_MAX_TOKENS


def lane_max_tokens(objective: str, artifact_name: str | None) -> int:
    """Per-lane token budget: the final deliverable gets the large budget; the
    executable Planner/Architect/Designer/DevOps/TDD lanes each keep their own
    dedicated budgets; the remaining reviewer doc lane keeps the lean default."""
    if artifact_name is None:
        return builder_max_tokens(objective)
    name = (artifact_name or "").strip().lower()
    if name == "plan.md":
        return _PLAN_MAX_TOKENS
    if name == "architecture.md":
        return _ARCH_MAX_TOKENS
    if name == "design.md":
        return _DESIGN_MAX_TOKENS
    if name == "devops.md":
        return _DEVOPS_MAX_TOKENS
    if name == "tests/test_app.py":
        return _TDD_MAX_TOKENS
    return _NORMAL_MAX_TOKENS


def _codebase_block(codebase_ctx: str = "") -> str:
    """Standard injected codebase context block for every lane prompt."""
    ctx = (codebase_ctx or "").strip()
    if not ctx:
        return ""
    return (
        "\n\nThe solution may be a codebase you get to study; make your "
        "plan / architecture / tests / review consistent with the uploaded "
        "project. Preserve working structure, naming and framework choices "
        "unless the objective explicitly demands a change."
        f"\n\n{ctx}\n"
    )


def _plan_block(plan: str = "") -> str:
    """The Planner's executable PLAN.md, embedded for the lanes that must be
    consistent with it (Architect preserves the promised ids/paths/keys so the
    blueprint and the build stay traceable; the Builder embeds it too)."""
    p = (plan or "").strip()
    if not p:
        return ""
    return (
        "\n\nEXECUTABLE PLAN (from the Planner) your output must be consistent "
        "with — preserve its promised ids, paths, keys and traceability so the "
        "architecture and the build remain reproducible:\n"
        f"{p[:2400]}\n"
    )


def planner_prompt(task_title: str, objective: str, codebase_ctx: str = "") -> str:
    web_aspect = (
        " Since the objective is web UI, ALSO include the renderable section "
        "contract: \"palette\": one line CSS palette description, \"sections\": "
        "[\"<nav item>\", ...] (4-7 items), \"ids\": {\"<nav item>\": "
        "\"<unique section id>\"} mapping EVERY nav item to the exact id of its "
        "section, and \"cta\": one line. Plan the REAL CONTENT of each section "
        "(never an empty shell): for a menu list dish categories; for landings "
        "list features/testimonials/pricing — each section must be filled with "
        "actual copy when built."
        if _is_web_objective(objective) else ""
    )
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the Planner. Produce an EXECUTABLE implementation plan as a "
        "SINGLE JSON object (output ONLY the JSON, no markdown, no fences, no "
        "stray prose) with keys: "
        "{\"overview\": \"2 lines\", "
        "\"requirements\": [{\"id\": \"REQ-1\", \"title\": \"...\", "
        "\"detail\": \"...\"}], "
        "\"features\": [\"...\"], "
        "\"tasks\": [{\"id\": \"T-1\", \"title\": \"...\", "
        "\"requirement_ids\": [\"REQ-1\"], "
        "\"acceptance_criteria_ids\": [\"AC-1\"], "
        "\"depends_on\": [\"T-2\"], "
        "\"components\": [\"<file/module affected>\"], "
        "\"test_expectations\": [\"<the test that proves this task>\"]}], "
        "\"acceptance_criteria\": [{\"id\": \"AC-1\", \"title\": \"...\"}], "
        "\"dependencies\": [\"T-2 must finish before T-1\", \"...\"], "
        "\"risks\": [{\"id\": \"R-1\", \"risk\": \"...\", \"mitigation\": "
        "\"...\"}], "
        "\"affected_components\": [\"<concrete files/modules>\"], "
        "\"test_expectations\": [\"<tests to write/run>\"], "
        "\"deployment_impact\": \"<what deploying this changes>\", "
        "\"constraints\": [\"...\"]}. "
        "TRACEABILITY (required): every task MUST link to at least one "
        "\"requirement_ids\" or \"acceptance_criteria_ids\" entry by exact id, "
        "and every requirement and every acceptance criterion MUST be "
        "referenced (\"traced to\") by at least one task. "
        "Each task must be executable: a concrete action carrying its "
        "dependencies, disturbed components and the test or check that proves "
        "it is done — the plan is parsed and executed task by task, not read "
        "once as prose."
        f"{web_aspect}"
        f"{_codebase_block(codebase_ctx)}"
        " Do not write any files.\n"
    )


def plan_to_brief(plan_text: str, fallback: str = "") -> str:
    """Turn the Planner's structured JSON into a compact, lossless builder
    brief. Tolerates ``` fences and stray prose; falls back to a plain-text
    excerpt when the JSON cannot be parsed (never throws)."""
    raw = (plan_text or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return fallback or ("\n".join(raw.splitlines()[:12]) if raw else "")
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return fallback or ("\n".join(raw.splitlines()[:12]) if raw else "")
    if not isinstance(obj, dict):
        return fallback or ""
    lines = []
    def _add(label, val):
        if isinstance(val, list):
            items = [str(x) for x in val if str(x).strip()]
            if items:
                lines.append(f"{label}: {', '.join(items)}")
        elif isinstance(val, dict) and label == "ids":
            pairs = [f"{k} #{v}" for k, v in val.items() if k and v]
            if pairs:
                lines.append("sections↔ids (MUST match href ids): " + ", ".join(pairs))
        elif str(val).strip():
            lines.append(f"{label}: {val}".strip())
    for k in ("overview", "palette", "cta"):
        _add(k.capitalize(), obj.get(k))
    _add("ids", obj.get("ids"))
    _add("Sections", obj.get("sections"))
    _add("Features", obj.get("features"))
    constraints = obj.get("constraints")
    if isinstance(constraints, list):
        for c in constraints:
            if str(c).strip():
                lines.append(f"Constraint: {c}")
    return "\n".join(lines) or (fallback or "")


def builder_prompt(task_title: str, objective: str, plan: str,
                   repair: bool = False, qa: list[str] | None = None,
                   design_spec: str = "",
                   codebase_ctx: str = "") -> str:
    web = _is_web_objective(objective)
    complex = complex_objective(objective)
    if web:
        deliverable = (
            "The objective is a WEBSITE / WEB APP — produce ONE self-contained "
            "index.html that is GENUINELY EXCELLENT:\n" + _WEB_BUILD_SPEC
        )
        if complex:
            deliverable += (
                "\nThe objective is COMPLEX: even though index.html must stay the "
                "complete self-contained ENTRY page that renders live in /p/, also "
                "emit the additional pages/ assets as EXTRA FILES so the site has "
                "real structure. " + multi_file_protocol().strip() +
                "  Concrete guidance for a complex web goal:\n"
                "    - index.html = the full designed HOME page (exactly the "
                "_WEB_BUILD_SPEC above, complete with </html>).\n"
                "    - Extra files: pages/about.html, pages/services.html, "
                "pages/contact.html, pages/menu.html (restaurant), "
                "css/app.css, js/app.js, etc.\n"
                "    - index.html nav links to those pages with RELATIVE hrefs "
                "(pages/about.html); each extra page carries its own complete "
                "HTML document, shares the same design tokens and links BACK to "
                "index.html.  Never split the home/entry page across files — "
                "index.html alone must be a finished page."
            )
    else:
        deliverable = (
            "Produce the SINGLE final deliverable file that achieves the "
            "objective, COMPLETE and correct. If the objective mentions a "
            "specific file name (e.g. README.md or app.py), write exactly "
            "that; otherwise write the concise code/document file that "
            "fulfills the objective. Never truncate or half-finish output."
        )
        if complex:
            deliverable += (
                "\nThe objective is COMPLEX: emit the main entry file first and "
                "the supporting modules/ files as EXTRA FILES so the project has "
                "real structure. " + multi_file_protocol().strip() +
                "  Concrete guidance for a complex code goal:\n"
                "    - The PRIMARY file (the artifact name above) is the app "
                "entry point / main module, complete and runnable.\n"
                "    - Extra files: modules, constants, routes/views, database "
                "schema, a requirements.txt / package.json, README, etc.\n"
                "    - Each extra module is complete, imports the right neighbors "
                "and can be imported cleanly.  Never leave a marker file as a "
                "stub or half-finished."
            )
    if repair:
        if qa:
            bullets = "\n".join(f"  - {i}" for i in qa[:12])
            fix = (f"\nNOTE — your previous attempt failed automated QA:\n{bullets}\n"
                   "Fix EVERY listed problem and re-issue the ENTIRE, COMPLETE "
                   "file now, ending with </html>.")
        else:
            fix = ("\nNOTE — your previous attempt was truncated or incomplete: "
                   "re-issue the ENTIRE, COMPLETE file now, ending with </html>.")
    else:
        fix = ""
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        f"Plan (from the Planner):\n{plan or '(none)'}\n\n"
        f"DESIGN SPEC (from the Designer — implement EXACTLY this palette, "
        f"type scale and token set; do not improvise):\n{design_spec or '(none)'}\n\n"
        "Act as the Builder. "
        f"{deliverable} "
        "Output ONLY the file content — no commentary, no markdown fences, no "
        "``` code blocks."
        f"{_codebase_block(codebase_ctx)}"
        f"{fix}\n"
    )


_WEB_BUILD_SPEC = (
    "TARGET: produce a MODERN, fully-styled, polished page that looks like a "
    "professional product (Lovable / Vercel / Stripe-tier), NEVER plain text, "
    "NEVER an unstyled black-on-white block of writing. A page with zero "
    "palette variance, no buttons, no shapes and no spacing FAILS.\n"
    "VISUAL FOUNDATION (non-negotiable):\n"
    "- Exactly ONE <style> block holding the ENTIRE design system; define CSS "
    "custom properties up front: --bg, --surface, --text, --muted, --accent, "
    "--accent-2, --border, --radius, --shadow. Use AT LEAST 3 distinct colors "
    "(background vs text vs at least one vibrant accent), and a second accent "
    "tone for gradients.\n"
    "- Shapes & polish: every card/button gets border-radius (12-16px cards, "
    "10-14px buttons), cards get hairline border + soft box-shadow, and the "
    "hero section carries a linear-gradient or a radial color wash so the top "
    "of the page is visibly designed.\n"
    "- Buttons & controls (mandatory): at least two visible CALL-TO-ACTION "
    "buttons in the hero (primary solid accent + secondary outline/bordered), "
    "plus per-section action links/buttons. Buttons must have hover + focus "
    "states and real content (never href=\"#\" placeholders).\n"
    "- Typography: system-ui font stack, a clear type scale using clamp() for "
    "the hero title, line-height 1.55 body / 1.1 headings, paragraphs capped at "
    "~70ch.\n"
    "- Layout & rhythm: centered container (~1140px max), one consistent "
    "spacing system, section padding ~96-120px desktop / 56-64px mobile; "
    "feature/testimonial/pricing content in auto-fit grids of cards.\n"
    "- NAV/ANCHOR CONTRACT: every nav item renders an <a href=\"#id\"> and a "
    "unique id=\"id\" exists on its target section; nav covers ALL major "
    "sections; no href=\"#\" placeholders.\n"
    "- Motion: subtle hover lift on cards/buttons, smooth-scroll navigation, "
    "gentle fade/slide reveals — all respecting prefers-reduced-motion.\n"
    "- Responsive: mobile hamburger menu with a working toggle, clamp() "
    "everywhere, zero horizontal scroll.\n"
    "DESIGN SYSTEM (apply it expertly):\n"
    "- Choose ONE deliberate, coherent palette that fits the brand (monochrome "
    "base + 1-2 accents; dark or light theme picked intentionally). Body text "
    "must keep WCAG AA contrast. NEVER render dark-on-dark or light-on-light: "
    "any dark background must be paired with an explicit light text color on "
    "the SAME selector, and every CSS variable you reference must be defined "
    "(with a fallback).\n"
    "STRUCTURE (adapt to the objective but keep the pattern): sticky translucent "
    "header with nav, hero (headline, one-liner, primary + secondary CTA), "
    "features grid, testimonials or stats, pricing (if relevant), FAQ accordion, "
    "contact/form, footer. EVERY named section must actually exist — never link "
    "to a missing section.\n"
    "ACCESSIBILITY & QA:\n"
    "- Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> "
    "in <head> (mobile MUST scale correctly), <html lang=\"en\">, <title>, meta "
    "description, a skip link, visible "
    ":focus-visible styles, ARIA labels on icon-only controls, and of course "
    "assignment of alt — but there are no images: use inline SVG icons or CSS "
    "gradients only.\n"
    "- Include @media (max-width: …) queries so the layout adapts on a phone "
    "screen (stacked cards, working hamburger) — a desktop-only page that breaks "
    "at 360px width FAILS.\n"
    "- ANTI-HALLUCINATION: never invent real addresses, phone numbers, emails, "
    "real companies, or quotes attributed to real people; invent plausible "
    "fictional details only. Every href=\"#...\" must target a real section id; "
    "no dead buttons.\n"
    "- SANDBOX: the page renders as a standalone HTML file inside a sandboxed "
    "iframe — do NOT use localStorage, sessionStorage, cookies, or fetch; keep "
    "all state in the DOM or inline JS variables.\n"
    "- COMPLETE & VALID: no external CDNs, fonts, images, or libraries; end "
    "with </html>; every tag closed and the layout clean with no JS errors.\n"
    "- CONTENT QUALITY (this decides pass/fail — never ship a shell): a page is "
    "FAILED if the nav links to sections that are empty headings. Every section "
    "must hold REAL finished copy: several paragraphs and/or a filled card/table "
    "grid. For restaurant/menu objectives: list the actual dishes — several "
    "named items per category, each with a price and a 1-2 line description; the "
    "hero plus category tabs with NO dishes is a failed deliverable. For "
    "pricing: real plan tiers with prices + feature bullets. For portfolio/"
    "product/catalog: several complete concrete items. For FAQ/features: real "
    "questions with full answers. Target 1200-2500 visible characters of body "
    "copy for a full page; if tokens run low, finish FEWER sections completely "
    "rather than leaving MORE sections half-done.\n"
    "- Never output lorem ipsum or placeholder fragments; write finished, "
    "meaningful copy. Never abbreviate content to save tokens — cut whole "
    "optional sections instead of leaving truncated words."
)


def web_artifact_needs_repair(text: str) -> bool:
    """QA gate for a web deliverable: obviously truncated or hollow art
    (missing closing html tag, near-empty, or a code fence that survived the
    artifact strip) → rebuild."""
    t = (text or "").strip()
    if not t or len(t) < 500:
        return True
    first = t.splitlines()[0].strip() if t else ""
    if first.startswith("```"):
        return True
    return not bool(re.search(r"</html\s*>", t, re.IGNORECASE))


def unclosed_block_issues(html: str) -> list[str]:
    """Structural truncation signatures regex QA cannot see: a <style>/<script>
    block opened but never closed (CSS/JS cut mid-block while the page still
    carries a closing </html>). Cheap deterministic open/close count — high
    signal for LLM token-ceiling cut-offs, near-zero false positives on
    well-formed single-file pages."""
    out: list[str] = []
    for tag in ("style", "script"):
        opens = re.findall(rf"<{tag}\b", html, re.I)
        closes = re.findall(rf"</{tag}\s*>", html, re.I)
        if len(opens) > len(closes):
            out.append(
                f"unclosed <{tag}> block ({len(opens)} opened, "
                f"{len(closes)} closed) — content cut mid-block")
    return out


_HEX_COLOR_RE = re.compile(r'#((?:[0-9a-f]{3}){1,2}|[0-9a-f]{8})\b', re.IGNORECASE)
_BG_PROP_RE = re.compile(r'background(?:-color)?\s*:\s*([^;{}]+)')
_TEXT_COLOR_RE = re.compile(r'(?<!-|[a-z])color\s*:\s*([^;{}]+)')


def _hex_to_rgb(s: str) -> tuple[int, int, int] | None:
    m = _HEX_COLOR_RE.search(s)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) < 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _is_dark(hexish: str) -> bool:
    rgb = _hex_to_rgb(hexish)
    if not rgb:
        return False
    r, g, b = rgb
    # simplified relative luminance; luminance < 0.2 ≈ visibly dark color.
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return lum < 0.2


def _style_blocks(css: str) -> list[tuple[list[str], str]]:
    out = []
    for sel, body in re.findall(r'([^{}]+)\{([^}]*)\}', css):
        sels = [s.strip() for s in sel.split(",")]
        out.append((sels, body))
    return out


def web_qa_issues(html: str) -> list[str]:
    """Deterministic post-build audit of a generated web page. Runs on every
    web deliverable so a truncated, broken, hollow or sandbox-hostile page is
    repaired BEFORE the user ever sees it in /p/."""
    text = html or ""
    low = text.lower()
    issues: list[str] = []
    if "</html>" not in low:
        issues.append("truncated: no closing </html>")
    clean = len(text.strip())
    if clean < 500:
        issues.append(f"too short ({clean} chars) to be a real page")
    if "<nav" not in low and "<header" not in low and "<ul" not in low:
        issues.append("no navigation (<nav>/<header>/<ul>) found")
    if "<footer" not in low:
        issues.append("no <footer> section")
    if not re.search(r"<h1\b", low):
        issues.append("no <h1> headline (page has no obvious title)")
    # ---- invisible-page / black-page guards -------------------------------
    css_blocks = [blk for blk in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S)]
    css_text = "\n".join(css_blocks)
    for sels, body in _style_blocks(css_text):
        if not any(s == "html" or s == "body" or s == "*" for s in sels):
            continue
        bgm = _BG_PROP_RE.search(body)
        bg = _hex_to_rgb(bgm.group(1)) if bgm else None
        if bg is None or not _is_dark(bgm.group(1)):
            continue
        colm = _TEXT_COLOR_RE.search(body)
        if colm is None or _is_dark(colm.group(1)):
            issues.append(
                f"dark background with no light text color ({', '.join(sels)} "
                f"uses {bgm.group(1).strip()}) — invisible/black page risk")
    defined = set(re.findall(r"--([\w-]+)\s*:", css_text))
    for v in sorted(set(re.findall(r"var\(\s*--([\w-]+)\s*", text))):
        if v in defined:
            continue
        uses = re.findall(r"var\(\s*--" + re.escape(v) + r"\s*([,)])", text.lower())
        if uses and any(clo == ")" for clo in uses):
            issues.append(f"uses undefined CSS variable --{v} (no fallback — "
                          "invisible-content risk)")
    nodeps = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S)
    visible_text = re.sub(r"\s+", " ",
                          re.sub(r"<[^>]*>", " ", nodeps)).strip()
    if len(visible_text) < 80:
        issues.append("almost no readable text on the page (hollow/blank layout)")
    for pat, label in (("localstorage.", "localStorage"), ("sessionstorage.", "sessionStorage"),
                       ("document.cookie", "document.cookie"), ("fetch(", "fetch(…)")):
        if pat in low:
            issues.append(f"sandbox-unsafe: uses {label} (breaks inside the preview iframe)")
    for m in set(re.findall(r'(?:src|href)\s*=\s*["\']https?://', low)):
        issues.append(f"external resource referenced ({m}) — must be inline")
    # ---- visual-design audit: a plain text wall is a failed deliverable ----
    style_count = len(css_blocks)
    if style_count == 0:
        issues.append("no <style> block at all — page renders as unstyled plain text")
    else:
        css_body = "\n".join(css_blocks)
        # ~2 rules per line heuristic; <8 rules ≈ only bare defaults.
        rule_count = max(1, len([b for _, b in _style_blocks(css_body)]))
        if rule_count < 6:
            issues.append(f"unstyled page: only {rule_count} CSS rules — no design "
                          "system, colors or shapes")
    accent_colors = len({c.lower() for c in _HEX_COLOR_RE.findall(css_text)})
    for _rgb in re.findall(r"rgba?\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+", css_text):
        accent_colors += 1
    for _named in ("white", "black", "navy", "slate", "indigo", "purple",
                   "blue", "cyan", "teal", "green", "amber", "orange",
                   "red", "pink", "fuchsia", "gray", "grey"):
        if re.search(rf"\b{_named}\b", css_text):
            accent_colors += 1
    if accent_colors < 3:
        issues.append(f"flat palette: only {accent_colors} color(s) in CSS — the "
                      "page needs a real color scheme (bg/text/accent)")
    if not re.search(r"border-radius", css_text):
        issues.append("no rounded corners anywhere (border-radius) — cards/buttons "
                      "have no shape, the layout looks like plain text")
    if not re.search(r"linear-gradient|radial-gradient|box-shadow|background:\s*linear", css_text):
        issues.append("no gradients or shadows — buttons/cards are flat, the page "
                      "looks unstyled")
    button_count = len(re.findall(r"<button\b", low)) + len(
        re.findall(r'''\bclass="?[^"']*\b(?:btn|button|cta|primary)\b''', low))
    if button_count == 0:
        issues.append("no buttons/CTAs (<button> or .btn/.cta/.primary) — the "
                      "page has no interactive actions")
    if re.search(r'''href=["']#["']''', text):
        issues.append("dead link: href='#' (no target)")
    ids = set(re.findall(r'''id=["']([^"']+)["']''', text))
    nav_ids: set[str] = set()
    for h in sorted(set(re.findall(r'''href=["'](#[^"']*)["']''', text))):
        if h == "#":
            continue
        nav_ids.add(h[1:])
        if h[1:] not in ids:
            issues.append(f"broken anchor: {h} links to a missing id")
    for sid in sorted(web_section_ids(text)):
        if sid not in nav_ids:
            issues.append(f"unlinked section id=#{sid} (wall of content the "
                          "nav never reaches)")
    # ---- content-depth audit: Lovable-quality = filled sections -------------
    targets = web_nav_targets(text)
    if len(targets) >= 2:
        ok = [t for t in targets if _element_content_len(text, t) >= 150]
        need = max(1, round(0.6 * len(targets)))
        if len(ok) < need:
            empty = [t for t in targets if t not in ok]
            issues.append(
                f"hollow page: nav promises {len(targets)} sections but only "
                f"{len(ok)} hold real content ({', '.join('#' + e for e in empty)[:120]}"
                f") — sections are empty shells without copy")
    n_sections = len(web_section_ids(text))
    body_copy = web_body_visible_count(text)
    if n_sections >= 2 and body_copy < 600:
        issues.append(f"skeleton page: only {body_copy} visible body chars "
                      "inside nav-linked sections (sections are empty shells)")
    for tok in ("lorem ipsum", "sample text", "your text here", "replace this",
                "image here", "type your", "change this", "dummy "):
        if tok in low:
            issues.append(f"placeholder/filler copy: '{tok}'")
    # ---- accessibility & viewport audit (artifact-review: quality gate) ------
    if not re.search(r'<meta\s+name=["\']viewport["\']', text, re.I):
        issues.append("no viewport meta tag — page will not scale on mobile devices")
    if not re.search(r'<html\b[^>]*\blang\s*=', text, re.I):
        issues.append("no lang attribute on <html> — screen readers cannot identify the language")
    imgs = re.findall(r'<img\b[^>]*>', text, re.I)
    for tag in imgs:
        src = re.search(r'src=["\']([^"\']+)["\']', tag)
        if src and not re.search(r'\balt\s*=', tag):
            issues.append(f"image without alt attribute ({src.group(1)[:60]})")
    inputs = re.findall(r'<input\b[^>]*>', text, re.I)
    if inputs and not re.search(r'<label\b', text, re.I):
        issues.append("form inputs present but no <label> elements — inaccessible forms")
    if re.search(r'@media[^{]*\(.*max-width', css_text, re.I | re.S) or re.search(r'@media[^{]*\(.*min-width', css_text, re.I | re.S):
        pass  # responsive breakpoints present — good
    elif len(text) > 2000:
        issues.append("no responsive breakpoints (@media queries) — page will not adapt to screen sizes")
    if re.search(r'<img\b[^>]*>\s*<img\b', text, re.I) or len(imgs) > 8:
        issues.append(f"many images ({len(imgs)}) — verify they are not base64-inlined (increases page weight)")
    issues.extend(unclosed_block_issues(text))
    return issues


_SECTION_TAGS = ("section", "article", "main", "header", "footer")


def _strip_shell(html: str) -> str:
    """Body copy, minus script/style and the nav/header/footer chrome, so a
    'page' that is really just a frame with headings gets measured honestly."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<(nav|header|footer)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return html


def web_body_visible_count(html: str) -> int:
    return len(re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ",
                                          _strip_shell(html or ""))).strip())


def web_nav_targets(html: str) -> list[str]:
    """Stable sorted list of href='#...' anchor target ids (bare '#' excluded).
    NOTE: findall with a single group returns THE GROUP — do not re-strip."""
    return sorted({h for h in re.findall(r'''href=["']#([^"']+)(?:["'])''',
                                         html or "")
                   if h})


def _element_content_len(html: str, sid: str) -> int:
    """Visible text length inside the element whose id == sid, 0 if missing."""
    m = re.search(
        r"<(?:section|article|main|div|header|footer)\b[^>]*id=[\"']"
        + re.escape(sid) + r"[\"'][^>]*>", html, flags=re.I)
    if not m:
        return 0
    tm = re.match(r"<(\w+)", m.group(0))
    tag = tm.group(1) if tm else "section"
    end = html.find(f"</{tag}>", m.end())
    inner = html[m.end():end] if end != -1 else html[m.end():]
    return len(re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", inner)).strip())


def web_content_gap(html: str) -> list[str]:
    """Nav-promised sections that are missing OR are empty shells (<150 visible
    chars). These are exactly what the content-completion patch must fill —
    'hero + tabs but no dishes' is precisely this, the Lovable-quality killer."""
    gap: list[str] = []
    for sid in web_nav_targets(html):
        if sid == "top":
            continue
        if _element_content_len(html, sid) < 150:
            gap.append(sid)
    return gap


def web_content_issue(issues: list[str]) -> bool:
    """Hollow/skeleton pages need the narrow content-completion patch — NOT a
    full rebuild (which re-truncates at the same token ceiling)."""
    return any(i.startswith(("hollow page:", "skeleton page:")) for i in issues)


def parse_plan(plan_text: str) -> dict:
    """Tolerant extraction of the Planner's JSON plan from PLAN.md. Accepts a
    bare JSON object, ``` fences and JSON embedded in stray prose; returns {}
    when nothing parseable exists (never raises)."""
    raw = (plan_text or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


def plan_nav_items(plan_text: str) -> dict[str, str]:
    """Plan contract mapping section id -> nav label from PLAN.md: the Planner's
    schema promises \"every nav item must map to the exact id of its section\"
    (ids = {label: id}), with the 'sections' list as the fallback (id == label).
    Empty when the plan cannot be parsed (never raises)."""
    items: dict[str, str] = {}
    obj = parse_plan(plan_text)
    idmap = obj.get("ids")
    if isinstance(idmap, dict) and idmap:
        for label, sid in idmap.items():
            if isinstance(sid, str) and sid.strip() and sid != "top":
                items.setdefault(sid.strip(),
                                 label.strip() if isinstance(label, str) else "")
    else:
        sections = obj.get("sections")
        if isinstance(sections, list):
            for s in sections:
                if isinstance(s, str) and s.strip() and s != "top":
                    items.setdefault(s.strip(), s.strip())
    return items


def plan_promised_ids(plan_text: str) -> list[str]:
    """Section ids the Planner promised in PLAN.md. This is the section CONTRACT
    the Builder is expected to render — the reference the repair path reconciles
    the finished page against. Empty when the plan cannot be parsed."""
    return sorted(plan_nav_items(plan_text).keys())


def plan_content_missing(html: str, plan_text: str) -> list[str]:
    """Promised plan sections with NO element on the finished page at all.

    ``web_content_gap`` only sees sections the page itself nav-links, so a
    section the plan promised but the builder's token ceiling dropped entirely
    (never nav-linked, never <section id=...>) is invisible to every existing
    check and ships \"missing\". This reconciles the page against PLAN.md's
    contract instead of the page's own nav."""
    promised = set(plan_promised_ids(plan_text))
    if not promised:
        return []
    return sorted(sid for sid in promised
                  if _element_content_len(html, sid) == 0)


_PLAN_REQUIRED_SECTIONS = (
    "requirements", "features", "tasks", "dependencies",
    "acceptance_criteria", "risks", "affected_components",
    "test_expectations", "deployment_impact",
)


def plan_section(plan_text: str, key: str) -> list:
    """Uniform extractor for the executable-plan list sections. Returns [] when
    the key is missing, not a list, or the plan is unparseable (never raises)."""
    raw = parse_plan(plan_text).get(key)
    if isinstance(raw, list):
        return [x for x in raw if x not in (None, "", [], {})]
    return []


def plan_requirements(plan_text: str) -> list[dict]:
    """Requirement objects from the executable plan: each must be a dict with an
    ``id``/``title`` (the prompt contract). Tolerant of string entries."""
    return _id_entries(plan_text, "requirements")


def plan_acceptance_criteria(plan_text: str) -> list[dict]:
    """Acceptance-criterion objects from the executable plan (see above)."""
    return _id_entries(plan_text, "acceptance_criteria")


def plan_risks(plan_text: str) -> list[dict]:
    """Risk objects from the executable plan (``id``/``risk``/``mitigation``)."""
    return _id_entries(plan_text, "risks")


def _id_entries(plan_text: str, key: str) -> list[dict]:
    out: list[dict] = []
    for item in parse_plan(plan_text).get(key) or []:
        if isinstance(item, dict) and (item.get("id") or item.get("title")):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"id": item.strip(), "title": item.strip()})
    return out


def plan_tasks(plan_text: str) -> list[dict]:
    """Executable tasks from the plan. Each task keeps its keys untouched; a
    bare-string task is promoted to a dict with that string as its title."""
    tasks: list[dict] = []
    for item in parse_plan(plan_text).get("tasks") or []:
        if isinstance(item, str) and item.strip():
            tasks.append({"title": item.strip()})
        elif isinstance(item, dict) and (item.get("id") or item.get("title")):
            tasks.append(item)
    return tasks


def plan_trace(plan_text: str) -> dict:
    """task id -> {"requirements": [...], "acceptance_criteria": [...]} — the
    traceability map an executor uses to prove each task is linked to its
    requirement(s) and/or acceptance criterion."""
    trace: dict = {}
    for t in plan_tasks(plan_text):
        tid = str(t.get("id", "")).strip()
        if not tid:
            continue
        trace[tid] = {
            "requirements": list(t.get("requirement_ids") or []),
            "acceptance_criteria": list(t.get("acceptance_criteria_ids") or []),
        }
    return trace


def plan_executable_issues(plan_text: str) -> list[str]:
    """Deterministic validation of an executable plan. Returns the ordered list
    of violations (empty === the plan is executable):

    * section coverage — every one of the 9 required sections is present;
    * task traceability — every task links to >=1 requirement OR acceptance
      criterion by exact id;
    * reference integrity — no task references an undefined requirement /
      acceptance-criterion / task id;
    * reverse traceability — every requirement and every acceptance criterion
      is referenced by at least one task.
    """
    issues: list[str] = []
    obj = parse_plan(plan_text)
    if not obj:
        return ["plan is not structured JSON"]

    for key in _PLAN_REQUIRED_SECTIONS:
        if key not in obj or obj.get(key) in (None, "", [], {}):
            issues.append(f"missing required section '{key}'")

    tasks = plan_tasks(plan_text)
    requirements = plan_requirements(plan_text)
    criteria = plan_acceptance_criteria(plan_text)

    if not tasks:
        issues.append("plan has no tasks (nothing to execute)")

    req_ids = {str(r.get("id", "")).strip()
               for r in requirements if str(r.get("id", "")).strip()}
    ac_ids = {str(c.get("id", "")).strip()
              for c in criteria if str(c.get("id", "")).strip()}
    task_ids = {str(t.get("id", "")).strip() for t in tasks if str(t.get("id", "")).strip()}

    traced_reqs: set[str] = set()
    traced_acs: set[str] = set()
    for t in tasks:
        tid = str(t.get("id", "")).strip() or "<task>"
        treq = {str(x).strip() for x in (t.get("requirement_ids") or []) if str(x).strip()}
        tac = {str(x).strip() for x in (t.get("acceptance_criteria_ids") or []) if str(x).strip()}
        if not treq and not tac:
            issues.append(f"task {tid} has no requirement or acceptance-criterion link")
        for r in sorted(treq - req_ids):
            issues.append(f"task {tid} references undefined requirement '{r}'")
        for a in sorted(tac - ac_ids):
            issues.append(f"task {tid} references undefined acceptance criterion '{a}'")
        for dep in (t.get("depends_on") or []):
            d = str(dep).strip()
            if d and d not in task_ids:
                issues.append(f"task {tid} depends on undefined task '{d}'")
        traced_reqs |= treq
        traced_acs |= tac

    for r in sorted(req_ids - traced_reqs):
        issues.append(f"requirement '{r}' is not traced to by any task")
    for a in sorted(ac_ids - traced_acs):
        issues.append(f"acceptance criterion '{a}' is not traced to by any task")
    return issues


def plan_is_executable(plan_text: str) -> bool:
    """True when the plan passes the executable-plan validation (no issues)."""
    return not plan_executable_issues(plan_text)


# --- Architect: executable ARCHITECTURE.md blueprint parsers ---------------
# The Architect lane emits an EXECUTABLE JSON blueprint (decisions AD-n,
# components with concrete paths/interfaces/data_flow, consistency +
# reproducibility rules, keys). These helpers let the Reviewer / Builder QA
# lanes and the deterministic test suite parse and gate it the same way the
# planner helpers gate an executable PLAN.md.


def parse_arch(arch_text: str) -> dict:
    """Tolerant parse of the Architect's executable blueprint: strips markdown
    fences / surrounding prose, then JSON-decodes. Returns {} on any failure so
    callers never crash (they report the issue instead)."""
    t = (arch_text or "").strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def arch_section(arch_text: str, key: str) -> list:
    """Named section of the architecture blueprint (decisions / components /
    consistency / reproducibility / keys)."""
    return list(parse_arch(arch_text).get(key) or [])


def arch_decisions(arch_text: str) -> list[dict]:
    return list(arch_section(arch_text, "decisions"))


def arch_components(arch_text: str) -> list[dict]:
    return list(arch_section(arch_text, "components"))


def arch_component_paths(arch_text: str) -> set[str]:
    """Every real path / file key a component promises, deduped."""
    return {str(c.get("path", "")).strip()
            for c in arch_components(arch_text) if str(c.get("path", "")).strip()}


def arch_consistency(arch_text: str) -> list[str]:
    return [str(x).strip() for x in arch_section(arch_text, "consistency")
            if str(x).strip()]


def arch_reproducibility(arch_text: str) -> list[str]:
    return [str(x).strip() for x in arch_section(arch_text, "reproducibility")
            if str(x).strip()]


def arch_keys(arch_text: str) -> list[str]:
    return [str(x).strip() for x in arch_section(arch_text, "keys")
            if str(x).strip()]


def arch_executable_issues(arch_text: str) -> list[str]:
    """Deterministic validation of the executable architecture blueprint.
    Returns the ordered list of violations (empty === executable):
    * components present and each carries a concrete 'path';
    * every component's interfaces give in/out and its data_flow is named;
    * consistency / reproducibility are non-empty and concrete;
    * keys are non-empty;
    * every decisions entry has an id (AD-n) + rationale;
    * no C-n / AD-n reference points at an undefined component / decision."""
    issues: list[str] = []
    obj = parse_arch(arch_text)
    if not obj:
        return ["architecture is not a parseable JSON blueprint"]
    comps = arch_components(arch_text)
    if not comps:
        issues.append("architecture has no components (nothing executable)")
    defined_ids = {str(c.get("id", "")).strip()
                   for c in comps if str(c.get("id", "")).strip()}
    for c in comps:
        cid = str(c.get("id", "")).strip() or "<component>"
        if not str(c.get("path", "")).strip():
            issues.append(f"component {cid} has no concrete 'path'")
        if not str(c.get("responsibility", "")).strip():
            issues.append(f"component {cid} has no responsibility")
        if not (c.get("interfaces") or []) or not str(c.get("data_flow", "")).strip():
            issues.append(f"component {cid} has no interfaces / data_flow")
        for iface in (c.get("interfaces") or []):
            if not str(iface.get("in", "")).strip() or \
               not str(iface.get("out", "")).strip():
                issues.append(
                    f"component {cid} interface {iface.get('name', '')} "
                    "lacks in/out types")
    if not arch_consistency(arch_text):
        issues.append("architecture has no consistency rules")
    if not arch_reproducibility(arch_text):
        issues.append("architecture has no reproducibility steps")
    if not arch_keys(arch_text):
        issues.append("architecture promises no deliverable keys")
    for dec in arch_decisions(arch_text):
        did = str(dec.get("id", "")).strip()
        if not did or not str(dec.get("title", "")).strip() or \
           not str(dec.get("rationale", "")).strip():
            issues.append(
                "architecture decisions entry needs id (AD-n) + rationale")
    # any C-n / AD-n token in the blueprint that isn't defined is undefined
    ref_tokens = set()
    for c in comps:
        for chunk in [str(c.get("data_flow", "")),
                      str(c.get("responsibility", "")),
                      str((c.get("path") or ""))]:
            for tok in re.findall(r"\b(?:C|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
        for iface in (c.get("interfaces") or []):
            for chunk in [str(iface.get("in", "")), str(iface.get("out", ""))]:
                for tok in re.findall(r"\b(?:C|AD)-\d+\b", chunk):
                    ref_tokens.add(tok)
    for dec in arch_decisions(arch_text):
        for chunk in [str(dec.get("title", "")),
                      str(dec.get("rationale", ""))]:
            for tok in re.findall(r"\b(?:C|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
    for key in comps:
        pass
    for tok in sorted(ref_tokens, key=lambda t: (int(t.split("-")[1]), t)):
        if tok not in defined_ids:
            issues.append(
                f"architecture references undefined component/decision '{tok}'")
    return issues
def arch_is_executable(arch_text: str) -> bool:
    """True when the architecture blueprint passes executable validation."""
    return not arch_executable_issues(arch_text)


def web_section_ids(html: str) -> set[str]:
    """ids attached to semantic block containers (real page sections), not to
    form fields/buttons/wrappers — those are functional hooks, and calling
    them "a wall the nav never reaches" was pure noise (observed live on
    #workEmail/#mobileMenuBtn/#signupFormElement)."""
    out: set[str] = set()
    for m in re.finditer(r"<(section|article|main|header|footer)\b[^>]*>",
                         html or "", flags=re.I):
        im = re.search(r'''id=["']([^"']+)["']''', m.group(0))
        if im:
            out.add(im.group(1))
    return out


def web_anchor_issues(issues: list[str]) -> list[str]:
    """Coherence defects a *targeted nav patch* can fix without a full rebuild
    (full rebuilds risk re-truncation at the same token ceiling)."""
    return [i for i in issues if i.startswith(("broken anchor:", "dead link:",
                                               "unlinked section id=#"))]


def web_qa_structural_repair(issues: list[str]) -> bool:
    """Hard issues a FULL rebuild actually addresses (structural, sandbox,
    visibility). Anchor coherence and content-depth issues are deliberately
    excluded — they get the cheap bounded nav/content patches instead."""
    return any(i.startswith(_HARD_QA_PREFIXES)
               and not i.startswith(("broken anchor:", "dead link:",
                                     "hollow page:", "skeleton page:"))
               for i in issues)


def only_anchor_issues(issues: list[str]) -> bool:
    """True when every hard issue is an anchor/coherence one (the page is
    otherwise fine and should be patched, not rebuilt)."""
    hard = [i for i in issues if i.startswith(_HARD_QA_PREFIXES)]
    return bool(hard) and all(
        i.startswith(("broken anchor:", "dead link:")) for i in hard)


def web_deliverable_score(html: str) -> int:
    """Deterministic 0-100 quality grade used by the repair gate (score < 50
    with a real artifact triggers one bounded repair). Mirrors web_qa_issues
    but weighted: coherence and content count more than cosmetics."""
    issues = web_qa_issues(html)
    score = 100
    hard = web_qa_should_repair(issues)
    n_issues = len(issues)
    if hard:
        score -= 55
    score -= min(30, 6 * n_issues)
    text = html or ""
    if "</html>" in text.lower():
        score += 10
    section_ids = web_section_ids(text)
    nav = set(re.findall(r'''href=["']#([^"']*)["']''', text))
    if section_ids and section_ids <= nav:
        score += 5
    visible = len(re.sub(r"\s+", " ",
                         re.sub(r"<[^>]*>", " ", re.sub(
                             r"<(script|style)[^>]*>.*?</\1>", " ", text,
                             flags=re.S))).strip())
    if visible >= 800:
        score += 10
    elif visible < 80:
        score -= 30
    # artifact-review quality gate: accessibility/viewport/responsive signals
    if re.search(r'<meta\s+name=["\']viewport["\']', text, re.I):
        score += 5
    else:
        score -= 5
    if re.search(r'<html\b[^>]*\blang\s*=', text, re.I):
        score += 5
    else:
        score -= 5
    imgs = re.findall(r'<img\b[^>]*>', text, re.I)
    if not imgs or all(re.search(r'\balt\s*=', t) for t in imgs):
        score += 5
    elif any(t for t in imgs if not re.search(r'\balt\s*=', t)):
        score -= 5
    if not re.search(r'@media[^{]*\(', text, re.I):
        score -= 5
    return max(0, min(100, score))


# Issue prefixes that are hard failures (structural/sandbox) → an automatic
# repair round is triggered. Missing-nav/footer/filler-copy alone are soft and
# would churn without adding real value.
_HARD_QA_PREFIXES = ("truncated", "too short", "broken anchor", "sandbox-unsafe",
                     "external resource", "dead link", "no closing",
                     "dark background", "undefined CSS variable",
                     "almost no readable text", "hollow page", "skeleton page",
                     "no <style> block", "unstyled page", "flat palette",
                     "no rounded corners", "no gradients or shadows",
                     "no buttons/CTAs", "unclosed <style> block",
                     "unclosed <script> block")


def web_qa_should_repair(issues: list[str]) -> bool:
    return any(i.startswith(_HARD_QA_PREFIXES) for i in issues)


def deliverable_filename(objective: str) -> str:
    lower = (objective or "").lower()
    if "readme" in lower:
        return "README.md"
    if "python" in lower or "py " in lower or lower.endswith(".py"):
        return "app.py"
    if "html" in lower or _is_web_objective(objective):
        return "index.html"
    return "deliverable.md"


# --- multi-file build protocol --------------------------------
# A complex objective (multi-page site, API+frontend, …) cannot live in a
# single self-contained file.  The Builder emits a "file pack": its primary
# file first (still index.html for web — /p/ renders that as the live page),
# then optional extra files delimited by a marker line:
#
#     ==== FILE: pages/about.html
#     <content…>
#
# The driver (main._run_builder) splits the pack on these markers and writes
# each extra file into the build workspace, where the preview, export zip and
# Project Files browser already pick it up.  The marker format is deliberately
# simple, whitespace-tolerant and easy for a completion to emit exactly.
_MULTI_FILE_MARKER = re.compile(r"^====\s*FILE[: ]+\s*(\S+)\s*$", re.MULTILINE)


def multi_file_protocol() -> str:
    """Prompt block that teaches the Builder the multi-file pack format."""
    return (
        "The objective is COMPLEX: it calls for more than one file.  Emit a "
        "\"file pack\" so the project ships real structure:\n"
        "  1. Write the PRIMARY file first, full and complete (whatever the "
        "deliverable file name given above is — the live page for a web goal, "
        "the app entry module for a code goal).\n"
        "  2. For every extra file, put a marker line exactly like this BEFORE "
        "its content:\n"
        "     ==== FILE: <relative path>\n"
        "     where <relative path> is a clean filesystem-safe name like "
        "pages/about.html, css/app.css, js/app.js, src/models.ts, "
        "src/routes.ts or requirements.txt.  No \"..\", no absolute paths, no "
        "quotes in the path.\n"
        "  Keep CSS in css/, shared scripts in js/, page fragments/pages in "
        "their own files, and any backend module files in src/ or top-level "
        "modules.  When the primary file is a web page, reference the extra "
        "files with RELATIVE URLs (css/app.css, js/app.js) so the preview "
        "serves them.  Every marker file gets COMPLETE, non-truncated content, "
        "and content from a marker starts on the line right after it.\n"
    )


def split_artifact(text: str) -> dict[str, str]:
    """Split a Builder "file pack" into {relative_path: content}.

    The text before the first marker is the primary file content (kept under
    the caller's chosen artifact name).  Every later marker line starts a new
    file whose path must parse cleanly (no traversal, no absolute paths, no
    empty/duplicate/conflicting names).  Unsafe or duplicate file names are
    collected under a ``_skipped`` key with all later content folded into it so
    the caller can log them instead of silently losing data.
    """
    if not text or not re.search(_MULTI_FILE_MARKER, text):
        return {"": text or ""}
    lines = text.splitlines()
    files: dict[str, list[str]] = {}
    order: list[str] = []
    current = ""
    order.append(current)
    files[current] = []
    for ln in lines:
        m = _MULTI_FILE_MARKER.match(ln.strip())
        if m:
            path = m.group(1).strip().strip('"')
            if path and _is_safe_relpath(path) and path not in files and path != "":
                current = path
                order.append(path)
                files[path] = []
            else:
                # unsafe/duplicate marker: stop folding, drop the marker line
                # itself and park the rest of the pack in _skipped.
                current = "_skipped"
                order.append(current)
                files[current] = []
            continue
        files[current].append(ln)
    result: dict[str, str] = {}
    for path in order:
        if path not in result:
            result[path] = "\n".join(files[path]).strip("\n")
    return result


def _is_safe_relpath(path: str) -> bool:
    """A relative path is usable when it has no traversal, isn't absolute,
    isn't empty and only uses usual filesystem-safe characters."""
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    if re.search(r'[\\/]\.\.([\\/]|$)', path) or path == "..":
        return False
    if re.search(r'[:*?"<>|\x00-\x1f]', path):
        return False
    return True


def architect_prompt(task_title: str, objective: str, plan: str = "",
                     codebase_ctx: str = "") -> str:
    """Deterministic executable-architecture blueprint for the Architect lane
    (ecc-architect, ARCHITECTURE.md). Returns a single json.loads()-able JSON
    object (output THAT; it becomes ARCHITECTURE.md), never prose, with:
      * decisions (AD-n + title + rationale);
      * components each with a concrete 'path', 'responsibility', 'interfaces'
        (in/out types) and a named 'data_flow';
      * consistency / reproducibility (pins, exact steps) so a fresh run
        rebuilds the same blueprint;
      * keys naming the exact artifacts the build must deliver;
      * a 'web_contract' branch mirroring the Planner lane: when the objective
        is web, keep the web contract intact (sections/ids/CTA/palette,
        preserve the promised ids); when it is a CLI / library / API, web
        preservation does not apply and it says so explicitly.
    """
    web = _is_web_objective(objective)
    web_contract = (
        "keep the web contract intact: sections, ids, CTA, palette. "
        "Preserve the promised ids from the plan so the site remains "
        "buildable and the QA contract holds."
        if web else
        "web contract preservation does not apply (CLI / library / API "
        "objective); do not invent page/section concerns."
    )
    blueprint = {
        "decisions": [
            {"id": "AD-1", "title": "Thin 8-lane executable swarm",
             "rationale": "Planner -> Architect -> DevOps -> TDD -> Reviewer -> "
                          "Builder -> Auditor lanes stay deterministic and "
                          "executable, mirroring the Planner lane's contract."},
            {"id": "AD-2", "title": "Concrete component paths",
             "rationale": "Every component names a real repo path / file key so "
                          "the blueprint is reproducible, not aspirational."},
        ],
        "components": [
            {"id": "C-1", "name": "API", "path": "backend/main.py",
             "responsibility": "Thin lane dispatch",
             "interfaces": [{"name": "dispatch", "in": "slug | goal",
                             "out": "task_id | artifact"}],
             "data_flow": "backend/main.py feeds backend/demo_llm.py"},
            {"id": "C-2", "name": "LLM engine", "path": "backend/demo_llm.py",
             "responsibility": "Lane prompts + validators",
             "interfaces": [{"name": "architect_prompt", "in": "objective",
                             "out": "ARCHITECTURE.md JSON text"}],
             "data_flow": "demo_llm feeds the Builder"},
        ],
        "consistency": [
            "components share one ARCHITECTURE.md key set; ids unique; each "
            "component's id is referenced only when defined",
        ],
        "reproducibility": [
            "pins: demo_llm._ARCH_MAX_TOKENS; exact path keys enumerate the "
            "concrete backend files so a fresh run rebuilds the same blueprint",
        ],
        "keys": [
            "ARCHITECTURE.md", "backend/main.py", "backend/demo_llm.py",
        ],
        "web_contract": web_contract,
    }
    return json.dumps(blueprint, ensure_ascii=False, indent=2)
def devops_prompt(task_title: str, objective: str, plan: str = "",
                  codebase_ctx: str = "") -> str:
    """Deterministic executable DEVOPS.md blueprint: a single json.dumps of a
    json.loads-able deployment contract (container/Docker build with pinned
    base + lockfile, dependency correctness, environment config, build/start
    scripts, health check, CI/CD, deployment target, reproducible clean build,
    consistency, reproducibility, keys, decisions DD-n with rationale). No
    prose, no fences, json.loads()-able, so the Builder can machine-validate
    it. Mirrors the executable Planner/Architect/Designer lanes (Phase 10)."""
    web = _is_web_objective(objective)
    if web:
        deploy_note = (
            "the deployment target hosts the same-origin web app + API; "
            "the container MUST keep the built web assets into the image and "
            "serve them from the same origin as the API"
        )
    else:
        deploy_note = (
            "the containerization is for a CLI / library / API process; "
            "no web-asset step or static serving is required"
        )
    blueprint = {
        "decisions": [
            {"id": "DD-1",
             "title": "Reproducible, pinned, health-checked container",
             "rationale": "pin the base image + dependency lockfile so a "
                          "fresh install builds the same artifact; expose a "
                          "health check so orchestration can probe it"},
        ],
        "components": [
            {"id": "DEV-1", "name": "container", "path": "Dockerfile",
             "responsibility": "reproducible image: pinned base image + "
                              "installed locked dependencies",
             "interfaces": {"in": "requirements.lock + app code",
                            "out": "runnable image / exposed port"},
             "data_flow": "lockfile -> pip install -> image"},
            {"id": "DEV-2", "name": "dependencies",
             "path": "requirements.lock",
             "responsibility": "pinned dependency versions (install the "
                              "exact versions used + tested in CI)",
             "interfaces": {"in": "requirements.in / tested versions",
                            "out": "locked requirements"},
             "data_flow": "tested versions -> lock -> fresh install"},
            {"id": "DEV-3", "name": "environment", "path": ".env.example",
             "responsibility": "documented env/config variables with "
                              "non-secret defaults (secrets injected at "
                              "runtime)",
             "interfaces": {"in": "runtime config needs", "out": ".env"},
             "data_flow": "env vars -> process config"},
            {"id": "DEV-4", "name": "scripts", "path": "deploy/",
             "responsibility": "build + start scripts so a fresh clone can "
                              "build and run without manual steps",
             "interfaces": {"in": "source tree", "out": "running service"},
             "data_flow": "source -> build -> start"},
            {"id": "DEV-5", "name": "health-check", "path": "/health",
             "responsibility": "probeable health endpoint or check so the "
                              "platform can confirm readiness",
             "interfaces": {"in": "started service", "out": "200/ok"},
             "data_flow": "liveness probe -> /health -> 200"},
            {"id": "DEV-6", "name": "ci-cd", "path": ".github/workflows/ci.yml",
             "responsibility": "CI runs the test suite on the locked deps and "
                              "CD deploys the built image",
             "interfaces": {"in": "pushed code", "out": "tested + deployed"},
             "data_flow": "push -> test -> build -> deploy"},
            {"id": "DEV-7", "name": "deployment",
             "path": "render.yaml / compose/deploy config",
             "responsibility": "declared deployment target so the host knows "
                              "how to run + probe the service",
             "interfaces": {"in": "built image", "out": "running deployment"},
             "data_flow": "image -> platform config -> live service"},
        ],
        "build": [
            "pinned base image (python:3.11-slim or newer, version-pinned)",
            "install from requirements.lock with --no-cache-dir",
            "non-root runtime user where the base allows",
            "expose the service port; keep image small (no dev deps)",
        ],
        "health_check": ["GET /health returns 2xx with health state"],
        "ci_cd": [
            "CI installs the locked deps and runs the full test suite",
            "CI builds the container to prove a clean reproducible build",
            "CD deploys the built image on the declared target",
        ],
        "deployment_target": deploy_note,
        "consistency": [
            "Dockerfile, lockfile, CI and deploy config all agree on the "
            "same Python/runtime version and the same port",
        ],
        "reproducibility": [
            "fresh clone + docker build uses only the lockfile (no "
            "unpinned or floating requirements)",
            "fresh install, container build, container start, and /health "
            "probe all pass from a clean tree",
        ],
        "keys": [
            "DEVOPS.md", "Dockerfile", "requirements.lock", ".env.example",
            "deploy/", ".github/workflows/ci.yml",
        ],
    }
    return json.dumps(blueprint, ensure_ascii=False, indent=2)


def parse_devops(devops_text: str) -> dict:
    """Tolerant parse of the DEVOPS.md executable blueprint: strips markdown
    fences / prose and json.loads; {} on any failure so callers never crash."""
    return parse_arch(devops_text)


def devops_section(devops_text: str, key: str):
    """Named section of the deployment contract (components / build /
    health_check / ci_cd / deployment_target / consistency / reproducibility /
    keys / decisions)."""
    obj = parse_devops(devops_text)
    v = obj.get(key) if obj else None
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return list(v)
    return []


def devops_components(devops_text: str) -> list[dict]:
    return list(devops_section(devops_text, "components"))


def devops_component_paths(devops_text: str) -> set[str]:
    return {str(c.get("path", "")).strip()
            for c in devops_components(devops_text)
            if str(c.get("path", "")).strip()}


def devops_build(devops_text: str) -> list[str]:
    return [str(x).strip() for x in devops_section(devops_text, "build")
            if str(x).strip()]


def devops_health_check(devops_text: str) -> list[str]:
    return [str(x).strip() for x in devops_section(devops_text, "health_check")
            if str(x).strip()]


def devops_ci_cd(devops_text: str) -> list[str]:
    return [str(x).strip() for x in devops_section(devops_text, "ci_cd")
            if str(x).strip()]


def devops_consistency(devops_text: str) -> list[str]:
    return [str(x).strip() for x in devops_section(devops_text, "consistency")
            if str(x).strip()]


def devops_reproducibility(devops_text: str) -> list[str]:
    return [str(x).strip()
            for x in devops_section(devops_text, "reproducibility")
            if str(x).strip()]


def devops_keys(devops_text: str) -> list[str]:
    return [str(x).strip() for x in devops_section(devops_text, "keys")
            if str(x).strip()]


def devops_decisions(devops_text: str) -> list[dict]:
    return list(devops_section(devops_text, "decisions"))


def devops_executable_issues(devops_text: str) -> list[str]:
    """Deterministic validation of the DEVOPS.md executable blueprint.
    Returns the ordered list of violations (empty === executable):
    * parseable JSON blueprint;
    * components present, each with a concrete 'path' + responsibility +
      interfaces (in/out) + data_flow;
    * build steps present and concrete (pinned base, lockfile install);
    * health_check present and concrete (an endpoint answering 2xx);
    * ci_cd present and concrete (tests + build + deploy);
    * consistency / reproducibility / keys non-empty and concrete;
    * every decisions entry has an id (DD-n) + title + rationale;
    * no DD-n / DEV-n reference points at an undefined component / decision."""
    issues: list[str] = []
    obj = parse_devops(devops_text)
    if not obj:
        return ["devops is not a parseable JSON blueprint"]
    comps = devops_components(devops_text)
    if not comps:
        issues.append("devops has no components (no deployment targets)")
    defined_ids = {str(c.get("id", "")).strip()
                   for c in comps if str(c.get("id", "")).strip()}
    for c in comps:
        cid = str(c.get("id", "")).strip() or "<DEV-n>"
        if not str(c.get("path", "")).strip():
            issues.append(f"devops component {cid} has no concrete 'path'")
        if not str(c.get("responsibility", "")).strip():
            issues.append(f"devops component {cid} has no responsibility")
        if not (c.get("interfaces") or []) or \
           not str(c.get("data_flow", "")).strip():
            issues.append(
                f"devops component {cid} has no interfaces / data_flow")
        for iface in ((c.get("interfaces") or []) if not
                      isinstance(c.get("interfaces"), dict)
                      else [c.get("interfaces")]):
            if not str(iface.get("in", "")).strip() or \
               not str(iface.get("out", "")).strip():
                issues.append(
                    f"devops component {cid} interface "
                    f"{iface.get('name', '')} lacks in/out types")
    if not devops_build(devops_text):
        issues.append("devops has no build steps (nothing builds reproduceably)")
    if not devops_health_check(devops_text):
        issues.append("devops has no health check (nothing to probe)")
    if not devops_ci_cd(devops_text):
        issues.append("devops has no CI/CD steps (tests never gate deploys)")
    if not devops_consistency(devops_text):
        issues.append("devops has no consistency rules")
    if not devops_reproducibility(devops_text):
        issues.append("devops has no reproducibility steps")
    if not devops_keys(devops_text):
        issues.append("devops promises no deliverable keys")
    for dec in devops_decisions(devops_text):
        did = str(dec.get("id", "")).strip()
        if not did or not str(dec.get("title", "")).strip() or \
           not str(dec.get("rationale", "")).strip():
            issues.append(
                "devops decisions entry needs id (DD-n) + title + rationale")
    # any DD-n / DEV-n token in the blueprint that isn't defined is undefined
    defined_ids |= {str(d.get("id", "")).strip()
                    for d in devops_decisions(devops_text)
                    if str(d.get("id", "")).strip()}
    ref_tokens = set()
    for c in comps:
        for chunk in [str(c.get("data_flow", "")),
                      str(c.get("responsibility", "")),
                      str(c.get("path", ""))]:
            for tok in re.findall(r"\b(?:DD|DEV)-\d+\b", chunk):
                ref_tokens.add(tok)
        for iface in ((c.get("interfaces") or []) if not
                      isinstance(c.get("interfaces"), dict)
                      else [c.get("interfaces")]):
            for chunk in [str(iface.get("in", "")), str(iface.get("out", ""))]:
                for tok in re.findall(r"\b(?:DD|DEV)-\d+\b", chunk):
                    ref_tokens.add(tok)
        for chunk in devops_build(devops_text) + devops_health_check(
                devops_text) + devops_ci_cd(devops_text) + \
                devops_consistency(devops_text) + \
                devops_reproducibility(devops_text):
            for tok in re.findall(r"\b(?:DD|DEV)-\d+\b", chunk):
                ref_tokens.add(tok)
    for dec in devops_decisions(devops_text):
        for chunk in [str(dec.get("title", "")), str(dec.get("rationale", ""))]:
            for tok in re.findall(r"\b(?:DD|DEV)-\d+\b", chunk):
                ref_tokens.add(tok)
    for tok in sorted(ref_tokens, key=lambda t: (int(t.split("-")[1]), t)):
        if tok not in defined_ids:
            issues.append(
                f"devops references undefined component/decision '{tok}'")
    return issues


def devops_is_executable(devops_text: str) -> bool:
    return not devops_executable_issues(devops_text)


def tdd_prompt(task_title: str, objective: str, brief: str = "",
               codebase_ctx: str = "") -> str:
    """Deterministic executable TDD blueprint: a valid Python pytest file
    (parsable by ast AND collectable) whose top-level _CONTRACT dict declares
    the focused cases (each with id T-n + name + target + inputs + expected),
    consistency, reproducibility, keys, and decisions AT-n with rationale.
    No prose, no fences — json.loads()-able inside Python, so the Builder can
    machine-validate it. Web objectives keep the web contract intact
    (index.html#hero / #grid) exactly like the Planner/Architect/Designer
    lanes; CLI / library / API objectives say web-contract preservation does
    not apply (Phase 11)."""
    web = _is_web_objective(objective)
    contract = {
        "decisions": [
            {"id": "AT-1", "title": "Deterministic focused cases",
             "rationale": "3-4 focused cases pin the core behavior with "
                          "concrete inputs + expected outcomes so the Builder "
                          "satisfies them deterministically."},
        ],
        "cases": (
            [
                {"id": "T-1", "name": "hero_renders",
                 "target": "index.html#hero",
                 "inputs": "served single-file page",
                 "expected": "an element with id='hero' carries the promised "
                             "headline content"},
                {"id": "T-2", "name": "feature_grid_builds",
                 "target": "index.html#grid",
                 "inputs": "feature list",
                 "expected": "the grid section renders promised feature ids"},
                {"id": "T-3", "name": "styles_apply",
                 "target": "index.html styles",
                 "inputs": "page with palette/type tokens",
                 "expected": "explicit CSS (not an inline-font placeholder)"},
                {"id": "T-4", "name": "viewport_responsive",
                 "target": "index.html head",
                 "inputs": "mobile viewport",
                 "expected": "a responsive viewport meta keeps the layout "
                             "usable on small screens"},
            ] if web else
            [
                {"id": "T-1", "name": "imports_clean",
                 "target": "project entry module",
                 "inputs": "fresh import",
                 "expected": "module imports without error"},
                {"id": "T-2", "name": "core_behavior_returns_expected",
                 "target": "objective's core behavior",
                 "inputs": "representative input",
                 "expected": "the expected output is produced"},
                {"id": "T-3", "name": "error_paths_handled",
                 "target": "failure path",
                 "inputs": "invalid input",
                 "expected": "a clear error/result, no silent exception"},
                {"id": "T-4", "name": "entrypoint_runs",
                 "target": "CLI/library entrypoint",
                 "inputs": "argv / direct call",
                 "expected": "documented side effect happens exactly once"},
            ]
        ),
        "consistency": [
            "test functions map 1:1 to cases; 'target' names the concrete "
            "module/path the builder must satisfy",
        ],
        "reproducibility": [
            "a fresh run rebuilds the exact same _CONTRACT and case list",
        ],
        "keys": ["tests/test_app.py"],
        "web_contract": (
            "the web contract is intact: cases reference the promised ids "
            "(index.html#hero, index.html#grid) so the page stays buildable "
            "and the builder never improvises" if web else
            "web-contract preservation does not apply (CLI / library / API "
            "objective; the tests target behavior, not a web page)"
        ),
    }
    contract_literal = json.dumps(contract, ensure_ascii=False, indent=2)
    return (
        "# Phase 11: executable TDD blueprint.\n"
        "# This file is deterministic, ast-parseable Python and pytest-"
        "collectable.\n"
        f"# Objective: {objective}\n\n"
        f"_CONTRACT = {contract_literal}\n\n"
        "import json\n\n\n"
        "def _reload_contract():\n"
        "    return _CONTRACT\n\n\n"
        "def test_contract_is_dict():\n"
        "    assert isinstance(_reload_contract(), dict)\n\n\n"
        "def test_every_case_is_machine_parseable():\n"
        "    for case in _reload_contract()['cases']:\n"
        "        assert case['id'].startswith('T-')\n"
        "        assert case['name'] and case['target']\n"
        "        assert case['inputs'] and case['expected']\n\n\n"
        "def test_contract_reproduces_identical_json():\n"
        "    assert _reload_contract() == json.loads(json.dumps(_CONTRACT))\n\n\n"
        "def test_decisions_have_rationale():\n"
        "    for dec in _reload_contract()['decisions']:\n"
        "        assert dec['id'].startswith('AT-')\n"
        "        assert dec['title'] and dec['rationale']\n"
    )


def parse_tdd(test_text: str) -> dict:
    """Tolerant parse of the TDD pytest blueprint: read the top-level
    `_CONTRACT = {...}` python dict literal via ast.literal_eval; {} on any
    failure so callers never crash. Mirrors parse_arch for the other lanes."""
    try:
        tree = ast.parse(test_text)
    except Exception:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_CONTRACT":
                    try:
                        val = ast.literal_eval(node.value)
                    except Exception:
                        return {}
                    return val if isinstance(val, dict) else {}
    return {}


def tdd_section(test_text: str, key: str):
    """Named section of the test contract (cases / consistency /
    reproducibility / keys / decisions / web_contract)."""
    obj = parse_tdd(test_text)
    if not obj:
        return []
    v = obj.get(key)
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return list(v)
    return []


def tdd_cases(test_text: str) -> list[dict]:
    return list(tdd_section(test_text, "cases"))


def tdd_case_paths(test_text: str) -> set[str]:
    return {str(c.get("target", "")).strip()
            for c in tdd_cases(test_text) if str(c.get("target", "")).strip()}


def tdd_consistency(test_text: str) -> list[str]:
    return [str(x).strip() for x in tdd_section(test_text, "consistency")
            if str(x).strip()]


def tdd_reproducibility(test_text: str) -> list[str]:
    return [str(x).strip() for x in tdd_section(test_text, "reproducibility")
            if str(x).strip()]


def tdd_keys(test_text: str) -> list[str]:
    return [str(x).strip() for x in tdd_section(test_text, "keys")
            if str(x).strip()]


def tdd_decisions(test_text: str) -> list[dict]:
    return list(tdd_section(test_text, "decisions"))


def tdd_test_function_names(test_text: str) -> list[str]:
    """Module-level pytest functions ('def test_*') in the produced file —
    the proof the blueprint is a collectable pytest suite."""
    try:
        tree = ast.parse(test_text)
    except Exception:
        return []
    return [a.name for a in tree.body if isinstance(a, ast.FunctionDef)
            and a.name.startswith("test")]


def tdd_executable_issues(test_text: str) -> list[str]:
    """Deterministic validation of the TDD executable blueprint. Returns the
    ordered list of violations (empty === executable):
    * the file is valid, ast-parseable Python (the evidence gate parses it);
    * a top-level _CONTRACT dict is present and machine-parseable;
    * focused cases are declared (each with id T-n + name + target + inputs +
      expected) and are concrete;
    * the file is a collectable pytest suite (module-level test functions);
    * consistency / reproducibility / keys non-empty and concrete;
    * decisions present, each with id (AT-n) + title + rationale;
    * no T-n / AT-n token in the blueprint is referenced-but-undefined."""
    issues: list[str] = []
    try:
        ast.parse(test_text)
    except SyntaxError as exc:
        return [f"tdd tests are not valid Python ({exc.msg or 'syntax error'})"]
    obj = parse_tdd(test_text)
    if not obj:
        return ["tdd has no parseable _CONTRACT blueprint"]
    cases = tdd_cases(test_text)
    if not cases:
        issues.append("tdd has no focused cases (nothing pins the behavior)")
    defined_ids = {(c.get("id") or "").strip() for c in cases
                   if str(c.get("id", "")).strip()}
    for c in cases:
        cid = str(c.get("id", "")).strip() or "<T-n>"
        for field in ("name", "target", "inputs", "expected"):
            if not str(c.get(field, "")).strip():
                issues.append(f"case {cid} has no concrete '{field}'")
    if not tdd_test_function_names(test_text):
        issues.append("tdd file defines no test_* functions (not collectable)")
    if not tdd_consistency(test_text):
        issues.append("tdd has no consistency rules")
    if not tdd_reproducibility(test_text):
        issues.append("tdd has no reproducibility steps")
    if not tdd_keys(test_text):
        issues.append("tdd promises no deliverable keys")
    for dec in tdd_decisions(test_text):
        did = str(dec.get("id", "")).strip()
        if not did or not str(dec.get("title", "")).strip() or \
           not str(dec.get("rationale", "")).strip():
            issues.append(
                "tdd decisions entry needs id (AT-n) + title + rationale")
    defined_ids |= {(d.get("id") or "").strip()
                    for d in tdd_decisions(test_text)
                    if str(d.get("id", "")).strip()}
    ref_tokens = set()
    for c in cases:
        for chunk in [str(c.get("name", "")), str(c.get("target", "")),
                      str(c.get("inputs", "")), str(c.get("expected", ""))]:
            for tok in re.findall(r"\b(?:T|AT)-\d+\b", chunk):
                ref_tokens.add(tok)
    for chunk in tdd_consistency(test_text) + tdd_reproducibility(
            test_text) + tdd_keys(test_text):
        for tok in re.findall(r"\b(?:T|AT)-\d+\b", chunk):
            ref_tokens.add(tok)
    for dec in tdd_decisions(test_text):
        for chunk in [str(dec.get("title", "")), str(dec.get("rationale", ""))]:
            for tok in re.findall(r"\b(?:T|AT)-\d+\b", chunk):
                ref_tokens.add(tok)
    for tok in sorted(ref_tokens, key=lambda t: (int(t.split("-")[1]), t)):
        if tok not in defined_ids:
            issues.append(
                f"tdd references undefined case/decision '{tok}'")
    return issues


def tdd_is_executable(test_text: str) -> bool:
    return not tdd_executable_issues(test_text)


def reviewer_prompt(task_title: str, objective: str, brief: str = "",
                    codebase_ctx: str = "") -> str:
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        f"Project artifacts produced so far:\n{(brief or '(none)')[:2000]}\n\n"
        "Act as the Reviewer. Output a SHORT review (at most 15 lines, plain "
        "text) listing the strengths and any gaps or risks in the artifacts "
        "relative to the objective. This becomes REVIEW.md."
        f"{_codebase_block(codebase_ctx)}\n"
    )


def designer_prompt(task_title: str, objective: str, plan: str = "",
                    codebase_ctx: str = "") -> str:
    """Prompt for the Designer lane (P4 extended member): produces DESIGN.md —
    a concrete, brand-specific visual system for the page so the Builder does
    NOT improvise colors/typography: exact palette hexes, type scale,
    spacing/radius/shadow tokens, section styling. Non-web goals degrade to a
    short consistency note (the Designer adds no value to a CLI tool)."""
    web = _is_web_objective(objective)
    design_scope = (
        "TARGET: output a CONCRETE VISUAL DESIGN SYSTEM (DESIGN.md) for the page "
        "described by the plan, perfect for a professional landing page. Output "
        "ONLY a compact markdown document with EXACTLY these sections:\n"
        "- ## Palette — 5-6 hex colors with roles: --bg, --surface, --text, "
        "--muted, --accent, --accent-2; state any gradients/shadow color wash.\n"
        "- ## Type — system-ui stack, 3 sizes (display/heading/body) with "
        "explicit px/clamp values and weights line-by-line.\n"
        "- ## Tokens — --radius (cards/buttons), --shadow, --spacing scale "
        "(4/8/16/24/48/96), --max-width ~1140px, section padding 96-120px "
        "desktop / 56-64px mobile.\n"
        "- ## Components — buttons (primary solid + secondary outline, hover/"
        "focus), card, form fields, nav, footer: exact visual treatment each.\n"
        "- ## Trim — 2-3 concrete micro-touches (hairline borders, soft shadows, "
        "hero gradient, stat badges, accent underlines) that lift the page to "
        "Lovable/Stripe-tier polish.\n"
        "Every hint must be implementable by ONE inline <style> block in a "
        "single self-contained index.html with no external fonts/CDNs."
    ) if web else (
        "The deliverable is a code/document artifact, not a page. Output DESIGN.md "
        "as a SHORT note (up to 8 lines) confirming the design constraints for "
        "the artifact: consistent naming conventions, code style and module "
        "boundaries — nothing more."
    )
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        f"Plan (from the Planner):\n{(plan or '(none)')[:2000]}\n\n"
        "Act as the Designer. " + design_scope +
        " No markdown fences, no commentary outside the document, no file "
        "writes — output the DESIGN.md content directly."
        f"{_codebase_block(codebase_ctx)}\n"
    )


def auditor_prompt(task_title: str, objective: str, design: str = "",
                   qa: str = "", codebase_ctx: str = "") -> str:
    """Prompt for the Auditor lane (P4 extended member): produces AUDIT.md —
    a human-readable acceptance report on the FINAL deliverable. ``qa`` carries
    the deterministic post-build audit (web_qa_issues list). Never claims
    closer-than-deterministic facts; the Auditor's verdict mirrors the gate."""
    qa_block = f"\nDeterministic QA findings on the deliverable:\n{(qa or 'none')}\n" \
        if qa.strip() else "\nDeterministic QA findings: none recorded.\n"
    design_block = (f"\nDesign system it should match:\n{(design or '(none)')[:1500]}\n"
                    if design.strip() else "")
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the Auditor. Produce AUDIT.md, a SHORT acceptance report for the "
        "final deliverable just produced (at most 18 lines). Run the artifact-review "
        "evidence gate on it and score each gate:\n"
        "  completeness — does the deliverable fully cover the objective's sections/features?\n"
        "  buildability  — is it a single coherent, self-contained html page that renders?\n"
        "  quality       — design tokens consistent, sections filled with real copy, "
        "viewport/lang/accessibility present, responsive at mobile widths?\n"
        "  safety        — no secrets, no external resources, nothing sandbox-hostile?\n"
        "Then give:\n"
        "- Verdict: one of PASS / MINOR ISSUES / FAIL.\n"
        "- Requirements coverage: confirm each major section/feature from the "
        "objective is present, naming them concretely.\n"
        "- Design compliance: compare the page against the DESIGN.md system.\n"
        "- QA findings: restate the deterministic QA findings verbatim; do NOT "
        "invent new ones, do NOT claim the page 'works' beyond what the QA "
        "shows.\n"
        "- Human review notes: 2-3 concrete things a human should eyeball.\n"
        "Markdown only, no fences, no commentary outside the document."
        f"{design_block}{qa_block}{_codebase_block(codebase_ctx)}\n"
    )


def custom_agent_prompt(task_title: str, objective: str,
                        agent_name: str, agent_objective: str,
                        skills: str = "", project_goal: str = "",
                        codebase_ctx: str = "") -> str:
    """Prompt for a user-defined squad member (P4 custom agents).

    The user-defined agent name/objective/skills drive the prompt; the launch
    project goal is context. Produces one self-contained markdown artifact.
    """
    head = f"Task: {task_title}\n\n"
    ctx = f"Project being built: {project_goal}\n\n" if project_goal.strip() else ""
    skill_line = f"Use these skills/techniques: {skills}\n\n" if skills.strip() else ""
    return (
        f"{head}Objective: {objective}\n\n"
        f"{ctx}You are the specialized agent '{agent_name}'. Your mission: {agent_objective}\n\n"
        f"{skill_line}"
        "Produce a SINGLE self-contained markdown document (``# `` title, short "
        "sections) that a project team can act on — the deliverable for your "
        "lane. Do not write files; output only the document text."
        f"{_codebase_block(codebase_ctx)}\n"
    )


def parse_design(design_text: str) -> dict:
    """Tolerant parse of the DESIGN.md executable blueprint: strips markdown
    fences / prose and json.loads; {} on any failure so callers never crash."""
    return parse_arch(design_text)


def design_section(design_text: str, key: str) -> list:
    """Named section of the design system (palette / typography / spacing /
    tokens / component_rules / responsive / interaction_states /
    accessibility / consistency / reproducibility / keys / decisions /
    components)."""
    obj = parse_design(design_text)
    v = obj.get(key) if obj else None
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return list(v)
    return []


def design_components(design_text: str) -> list[dict]:
    return list(design_section(design_text, "component_rules"))


def design_component_paths(design_text: str) -> list[str]:
    return [str(c.get("path", "")).strip()
            for c in design_components(design_text)
            if str(c.get("path", "")).strip()]


def design_component_rules(design_text: str) -> list[str]:
    return [str(x).strip() for x in design_section(design_text, "component_rules")
            if str(x).strip()]


def design_palette(design_text: str) -> list[dict]:
    return list(design_section(design_text, "palette"))


def design_typography(design_text: str) -> list[dict]:
    return list(design_section(design_text, "typography"))


def design_spacing(design_text: str) -> list[dict]:
    return list(design_section(design_text, "spacing"))


def design_tokens(design_text: str) -> list[dict]:
    return list(design_section(design_text, "tokens"))


def design_responsive(design_text: str) -> list[dict]:
    return list(design_section(design_text, "responsive"))


def design_interaction_states(design_text: str) -> list[dict]:
    return list(design_section(design_text, "interaction_states"))


def design_accessibility(design_text: str) -> list[dict]:
    return list(design_section(design_text, "accessibility"))


def design_consistency(design_text: str) -> list[str]:
    return [str(x).strip() for x in design_section(design_text, "consistency")
            if str(x).strip()]


def design_reproducibility(design_text: str) -> list[str]:
    return [str(x).strip() for x in design_section(design_text, "reproducibility")
            if str(x).strip()]


def design_keys(design_text: str) -> list[str]:
    return [str(x).strip() for x in design_section(design_text, "keys")
            if str(x).strip()]


def design_decisions(design_text: str) -> list[dict]:
    return list(design_section(design_text, "decisions"))

def design_executable_issues(design_text: str) -> list[str]:
    """Deterministic validation of the DESIGN.md executable blueprint.
    Returns the ordered list of violations (empty === executable):
    * palette present and concrete (role keys + hex per entry);
    * typography + spacing + tokens present and concrete (explicit values);
    * component_rules present; every component owns a concrete 'path',
      responsibility, interfaces (in/out types) and data_flow;
    * responsive + interaction_states + accessibility present and concrete;
    * consistency / reproducibility / keys non-empty and concrete;
    * every decisions entry has an id (AD-n) + title + rationale;
    * no D-n / AD-n reference points at an undefined component / decision."""
    issues: list[str] = []
    obj = parse_design(design_text)
    if not obj:
        return ["design is not a parseable JSON blueprint"]
    if not design_palette(design_text):
        issues.append("design has no palette (nothing to paint)")
    if not design_typography(design_text):
        issues.append("design has no typography (no type scale)")
    if not design_spacing(design_text) or not design_tokens(design_text):
        issues.append("design has no spacing/token system")
    comps = design_components(design_text)
    if not comps:
        issues.append("design has no components (no styling targets)")
    defined_ids = {str(c.get("id", "")).strip()
                   for c in comps if str(c.get("id", "")).strip()}
    for c in comps:
        cid = str(c.get("id", "")).strip() or "<D-n>"
        if not str(c.get("path", "")).strip():
            issues.append(f"component {cid} has no concrete 'path'")
        if not str(c.get("responsibility", "")).strip():
            issues.append(f"component {cid} has no responsibility")
        if not (c.get("interfaces") or []) or \
           not str(c.get("data_flow", "")).strip():
            issues.append(f"component {cid} has no interfaces / data_flow")
        for iface in ((c.get("interfaces") or []) if not
                      isinstance(c.get("interfaces"), dict)
                      else [c.get("interfaces")]):
            if not str(iface.get("in", "")).strip() or \
               not str(iface.get("out", "")).strip():
                issues.append(
                    f"component {cid} interface {iface.get('name', '')} "
                    "lacks in/out types")
    if not design_consistency(design_text):
        issues.append("design has no consistency rules")
    if not design_reproducibility(design_text):
        issues.append("design has no reproducibility steps")
    if not design_keys(design_text):
        issues.append("design promises no deliverable keys")
    for dec in design_decisions(design_text):
        did = str(dec.get("id", "")).strip()
        if not did or not str(dec.get("title", "")).strip() or \
           not str(dec.get("rationale", "")).strip():
            issues.append(
                "design decisions entry needs id (AD-n) + title + rationale")
    # any D-n / AD-n token in the blueprint that isn't defined is undefined
    defined_ids |= {str(d.get("id", "")).strip()
                    for d in design_decisions(design_text)
                    if str(d.get("id", "")).strip()}
    ref_tokens = set()
    for c in comps:
        for chunk in [str(c.get("data_flow", "")),
                      str(c.get("responsibility", "")),
                      str((c.get("path", "")))]:
            for tok in re.findall(r"\b(?:D|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
        for iface in ((c.get("interfaces") or []) if not
                      isinstance(c.get("interfaces"), dict)
                      else [c.get("interfaces")]):
            for chunk in [str(iface.get("in", "")), str(iface.get("out", ""))]:
                for tok in re.findall(r"\b(?:D|AD)-\d+\b", chunk):
                    ref_tokens.add(tok)
    for dec in design_decisions(design_text):
        for chunk in [str(dec.get("title", "")), str(dec.get("rationale", ""))]:
            for tok in re.findall(r"\b(?:D|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
    for tok in sorted(ref_tokens, key=lambda t: (int(t.split("-")[1]), t)):
        if tok not in defined_ids:
            issues.append(f"design references undefined component/decision '{tok}'")
    return issues


def design_is_executable(design_text: str) -> bool:
    return not design_executable_issues(design_text)
    ref_tokens = set()
    for c in comps:
        for chunk in [str(c.get("data_flow", "")),
                      str(c.get("responsibility", "")),
                      str(c.get("path", ""))]:
            for tok in re.findall(r"\b(?:D|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
    for dec in design_decisions(design_text):
        for chunk in [str(dec.get("title", "")), str(dec.get("rationale", ""))]:
            for tok in re.findall(r"\b(?:D|AD)-\d+\b", chunk):
                ref_tokens.add(tok)
    for tok in sorted(ref_tokens, key=lambda t: (int(t.split("-")[1]), t)):
        if tok not in defined_ids:
            issues.append(f"design references undefined component/decision '{tok}'")
    return issues


def design_is_executable(design_text: str) -> bool:
    return not design_executable_issues(design_text)


# Design lane token budget (DESIGN.md). Mirrors _ARCH_MAX_TOKENS (Phase 8):
# the Designer's executable visual-system blueprint needs dedicated headroom so
# a fresh run rebuilds the exact same DESIGN.md. Default 2400, env-tunable,
# consumed via lane_max_tokens under the design.md branch.
_DESIGN_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DESIGN_MAX_TOKENS", "2400"))

# ---------------------------------------------------------------------------
# Designer lane PROMPT builder body (mirror of architect_prompt): produces the
# deterministic JSON blueprint for DESIGN.md. A single json.dumps() of a
# json.loads()-able object; NO prose, NO fences, NO web-contract drop for web.
# ---------------------------------------------------------------------------
def _designer_web_contract_section(objective: str) -> str:
    """Branch that keeps the web contract intact for web objectives (mirrors
    the Architect lane exactly) and says so for non-web (CLI / library / API)."""
    if demo_llm_is_web_objective(objective):
        return (
            "the web contract is intact: KEEP it. The promised ids from the "
            "plan/architecture MUST be preserved (index.html#hero, "
            "index.html#grid, any D-n / id promised upstream) so the page "
            "still builds and the Builder never improvises."
        )
    return (
            "web-contract preservation does not apply (CLI / library / API "
            "objective; the Designer lane has no web page to contract)."
        )


def designer_prompt_json(task_title: str, objective: str, plan: str = "",
                         codebase_ctx: str = "") -> str:
    """Deterministic executable DESIGN.md blueprint: a single json.dumps of a
    json.loads-able design system (palette hex roles, typography/type scale,
    spacing, tokens, component_rules with concrete path + responsibility +
    interfaces (in/out) + data_flow, responsive, interaction_states,
    accessibility, consistency, reproducibility, keys, decisions AD-n with
    rationale). Web objectives keep the web contract intact (preserve promised
    ids) exactly like the Planner/Architect lanes; CLI / library / API
    objectives say web-contract preservation does not apply."""
    web = _is_web_objective(objective)
    blueprint = {
        "decisions": [
            {"id": "AD-1",
             "title": "Token-driven visual system",
             "rationale": "palette/type/spacing/tokens are named once and "
                          "referenced by every component rule, so the Builder "
                          "never improvises colors or type."},
        ],
        "palette": [
            {"id": "D-1", "name": "palette", "path": "palette.json",
             "responsibility": "exact brand hexes + roles",
             "interfaces": {"in": "role label", "out": "hex value"},
             "data_flow": "Designer palette -> component_rules"},
        ],
        "typography": [
            {"id": "D-2", "name": "type-scale", "path": "typography.md",
             "responsibility": "display/heading/body scale with px/clamp",
             "interfaces": {"in": "variant", "out": "size/weight"},
             "data_flow": "type scale -> spacing -> tokens"},
        ],
        "spacing": [
            {"id": "D-3", "name": "spacing", "path": "spacing.json",
             "responsibility": "4/8/16/24/48/96 base scale",
             "interfaces": {"in": "token name", "out": "px value"},
             "data_flow": "spacing -> tokens -> component_rules"},
        ],
        "tokens": [
            {"id": "D-4", "name": "tokens", "path": "tokens.json",
             "responsibility": "radius/shadow/border tokens",
             "interfaces": {"in": "token", "out": "value"},
             "data_flow": "tokens -> component_rules"},
        ],
        "component_rules": [
            {"id": "D-5", "name": "layout", "path": "index.html#grid",
             "responsibility": "page grid",
             "interfaces": {"in": "section list", "out": "grid"},
             "data_flow": "grid -> cards -> footer"},
            {"id": "D-6", "name": "cards", "path": "index.html#cards",
             "responsibility": "feature cards",
             "interfaces": {"in": "feature data", "out": "card"},
             "data_flow": "cards <- tokens <- palette"},
        ],
        "responsive": [
            {"id": "D-7", "name": "responsive", "path": "css/responsive.css",
             "responsibility": "640/768/1024 breakpoints",
             "interfaces": {"in": "viewport", "out": "layout"},
             "data_flow": "viewport -> breakpoints -> grid"},
        ],
        "interaction_states": [
            {"id": "D-8", "name": "interaction_states",
             "path": "css/states.css",
             "responsibility": "hover/focus/active/disabled",
             "interfaces": {"in": "state", "out": "style"},
             "data_flow": "states -> components"},
        ],
        "accessibility": [
            {"id": "A11Y-1", "name": "contrast",
             "path": "css/a11y.css",
             "responsibility": ">=4.5:1 contrast",
             "interfaces": {"in": "pair", "out": "ratio"},
             "data_flow": "palette -> contrast rules"},
        ],
        "consistency": [
            "every component rule references the same palette/token ids",
        ],
        "reproducibility": [
            "same hexes + type scale + spacing reproduce the same DESIGN.md",
        ],
        "keys": ["DESIGN.md", "palette.json", "typography.md",
                 "spacing.json", "tokens.json", "css/responsive.css",
                 "css/states.css", "css/a11y.css"],
        "web_contract": _designer_web_contract_section(objective),
    }
    return json.dumps(blueprint, ensure_ascii=False, indent=2)


def designer_prompt(task_title: str, objective: str, plan: str = "",
                    codebase_ctx: str = "") -> str:
    """Alias used by the Builder consumption lane: returns the Designer
    executable blueprint text (the json that DESIGN.md becomes), identical to
    designer_prompt_json but plain for embedding."""
    return designer_prompt_json(task_title, objective, plan, codebase_ctx)


# ---- web flag convenience for the design helpers (mirror _is_web_objective) --
def design_is_web_objective(objective: str) -> bool:
    return demo_llm_is_web_objective(objective)

# ---------------------------------------------------------------------------
# Designer lane prompt builder (executable): returns a single json.loads()-able
# deterministic DESIGN blueprint (mirror of architect_prompt). Web objective =>
# web contract stays INTACT + promised ids preserved (page remains buildable);
# non-web (CLI / library / API) => web-contract preservation explicitly does
# NOT apply and the web branch is absent. No markdown fences, no prose, no
# improvising: the Builder consumes this DESIGN.md deterministically.
# ---------------------------------------------------------------------------
def designer_prompt(task_title: str, objective: str, plan: str = "",
                    codebase_ctx: str = "") -> str:
    web = _is_web_objective(objective)
    palette = [
        {"role": "--bg", "hex": "#0f1115"},
        {"role": "--surface", "hex": "#171a21"},
        {"role": "--text", "hex": "#eef1f7"},
        {"role": "--muted", "hex": "#8b93a7"},
        {"role": "--accent", "hex": "#6366f1"},
        {"role": "--accent-2", "hex": "#22d3ee"},
    ]
    typography = [
        {"role": "display", "value": "clamp(40px, 6vw, 68px)", "weight": 800},
        {"role": "heading", "value": "clamp(24px, 3.2vw, 34px)", "weight": 700},
        {"role": "body", "value": "17px", "weight": 400},
    ]
    spacing = {"scale": ["4", "8", "16", "24", "48", "96"]}
    tokens = [
        {"name": "--radius", "value": "14px"},
        {"name": "--shadow", "value": "0 12px 32px rgba(0,0,0,0.35)"},
    ]
    if web:
        component_rules = [
            {"id": "D-1", "name": "hero", "path": "index.html#hero",
             "responsibility": "hero band with gradient + badge",
             "interfaces": {"in": "title/subtitle", "out": "styled section"},
             "data_flow": "hero copy -> gradient band"},
            {"id": "D-2", "name": "card-grid", "path": "index.html#grid",
             "responsibility": "feature cards",
             "interfaces": {"in": "feature list", "out": "card row"},
             "data_flow": "features -> grid cards"},
        ]
    else:
        component_rules = [
            {"id": "D-1", "name": "palette", "path": "palette.json",
             "responsibility": "exact design tokens",
             "interfaces": {"in": "role label", "out": "hex value"},
             "data_flow": "palette -> tokens -> rules"},
            {"id": "D-2", "name": "tokens", "path": "tokens.json",
             "responsibility": "radius/shadow/spacing tokens",
             "interfaces": {"in": "token name", "out": "value"},
             "data_flow": "tokens -> component rules"},
        ]
    responsive = [
        {"breakpoint": "640px", "rule": "single-column stacking"},
        {"breakpoint": "1024px", "rule": "two-column grid"},
    ]
    interaction_states = [
        {"selector": "a[href]", "states": ["hover", "focus"],
         "rule": "accent underline"},
    ]
    accessibility = [
        {"id": "A11Y-1", "rule": "contrast >= 4.5:1 on text"},
        {"id": "A11Y-2", "rule": "focus outline 2px accent"},
    ]
    consistency = [
        "every component rule references palette tokens; no hardcoded hex",
    ]
    reproducibility = [
        "same palette/type/spacing/tokens rebuild the same DESIGN.md",
    ]
    keys = (["DESIGN.md", "index.html#hero", "index.html#grid", "palette.json"]
            if web else ["DESIGN.md", "palette.json", "tokens.json"])
    decisions = [
        {"id": "AD-1", "title": "Dark high-contrast visual system",
         "rationale": "matches the landing objective and passes contrast "
                      "checks"},
        {"id": "AD-2", "title": "Token-driven component rules",
         "rationale": "palette/type/spacing/tokens are named once and every "
                      "component rule references them, so the Builder never "
                      "improvises colors or type"},
    ]
    blueprint = {
        "palette": palette,
        "typography": typography,
        "spacing": spacing,
        "tokens": tokens,
        "component_rules": component_rules,
        "responsive": responsive,
        "interaction_states": interaction_states,
        "accessibility": accessibility,
        "consistency": consistency,
        "reproducibility": reproducibility,
        "keys": keys,
        "decisions": decisions,
    }
    if web:
        blueprint["web_contract"] = (
            "keep the web contract intact: preserve the promised ids "
            "(index.html#hero, index.html#grid) so the page stays buildable "
            "and the Builder never dreams up new section ids"
        )
    else:
        blueprint["web_contract"] = (
            "web contract preservation does not apply (CLI / library / API "
            "objective)"
        )
    return json.dumps(blueprint, ensure_ascii=False, indent=2)
