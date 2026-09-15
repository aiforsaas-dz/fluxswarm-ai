from __future__ import annotations

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
# The short plan/arch/devops/tdd/review lanes keep the lean default.
_DELIVERABLE_MAX_TOKENS = int(os.environ.get("FLUXSWARM_DELIVERABLE_MAX_TOKENS", "2400"))
_NORMAL_MAX_TOKENS = 800

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
    smaller plan/doc/lint lanes keep the normal cap."""
    if artifact_name is None:
        return builder_max_tokens(objective)
    return _NORMAL_MAX_TOKENS


def _codebase_block(codebase_ctx: str = "") -> str:
    """Standard injected codebase context block for every lane prompt."""
    ctx = (codebase_ctx or "").strip()
    if not ctx:
        return ""
    return (
        "\n\nREFERENCE CODEBASE (an uploaded project you are improving):\n"
        f"{ctx}\n"
        "STUDY the file tree and config above, and make your plan / architecture / "
        "tests / review consistent with it. Preserve working structure, naming and "
        "framework choices unless the objective explicitly demands a change.\n"
    )


def planner_prompt(task_title: str, objective: str, codebase_ctx: str = "") -> str:
    prefix = " Deliver the site as ONE self-contained index.html." if _is_web_objective(objective) else ""
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the Planner. Produce a SHORT but STRUCTURED plan as a SINGLE "
        "JSON object (output ONLY the JSON, no markdown, no fences) with keys: "
        "{\"overview\": \"2 lines\", \"palette\": \"one line CSS palette "
        "description\", \"sections\": [\"<nav item>\", ...], \"ids\": {"
        "\"<nav item>\": \"<unique section id>\"}, \"features\": [\"...\"], "
        "\"cta\": \"one line\", \"constraints\": [\"...\"]}. List 4-7 "
        "sections; every nav item must map to the exact id of its section. "
        "Plan the REAL CONTENT of each section (never an empty shell): for a "
        "menu list dish categories; for landings list features/testimonials/"
        "pricing — each section must be filled with actual copy when built."
        f"{prefix}"
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
    "- Add <html lang>, <title>, meta description, a skip link, visible "
    ":focus-visible styles, ARIA labels on icon-only controls, and of course "
    "assignment of alt — but there are no images: use inline SVG icons or CSS "
    "gradients only.\n"
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
                     "no buttons/CTAs")


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


def architect_prompt(task_title: str, objective: str, codebase_ctx: str = "") -> str:
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the Architect. Produce a SHORT architecture document (at most "
        "20 lines, plain text, no markdown fences) covering components, data "
        "flow, and the key interfaces of the solution. This becomes "
        "ARCHITECTURE.md. Output only the document text."
        f"{_codebase_block(codebase_ctx)}\n"
    )


def devops_prompt(task_title: str, objective: str, plan: str = "",
                  codebase_ctx: str = "") -> str:
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the DevOps engineer. Output ONLY a production-ready Dockerfile "
        "(plain text, no markdown fences, no commentary) that would containerize "
        "this project as a simple Python or static web service."
        f"{_codebase_block(codebase_ctx)}\n"
    )


def tdd_prompt(task_title: str, objective: str, brief: str = "",
               codebase_ctx: str = "") -> str:
    return (
        f"Task: {task_title}\n\n"
        f"Objective: {objective}\n\n"
        "Act as the TDD specialist. Output ONLY the Python source of a pytest "
        "test suite (plain text, no markdown fences) with 3-6 focused tests for "
        "the core behavior described in the objective. No commentary outside "
        "the code."
        f"{_codebase_block(codebase_ctx)}\n"
    )


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
        "final deliverable just produced (at most 18 lines):\n"
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