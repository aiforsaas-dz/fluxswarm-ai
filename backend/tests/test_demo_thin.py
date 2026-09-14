"""Thin demo executor tests (Free-tier convergence path).

The free demo surface must converge within its budget: the full Hermes worker
crashes ~40-80s into real work on the 512MB host (measured via recovered
reason=crash), so demo lanes use a bounded thin executor — real board, real
provider completion, real artifact, real completed event — while the paid
path keeps the full agents untouched. These tests lock the thin path's
honesty boundaries and the reaper's ownership split for flux-demo-* boards.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import hermes_client as hc
import demo_llm


# A genuine design-system CSS block that passes the visual QA gate.  Used by
# fixtures whose *content* (not styling) is under test, so visual issues never
# mask the structural/hollow assertions those tests lock.
_CSS_FOUNDATION = (
    "<style>:root{--bg:#0b0f1a;--surface:#151b2b;--text:#eef1fb;--muted:#a7b0c8;"
    "--accent:#6366f1;--accent-2:#22d3ee;--border:#232a3b;--radius:14px;"
    "--shadow:0 8px 24px rgba(0,0,0,.25)}"
    "body{background:var(--bg);color:var(--text);font-family:system-ui;margin:0}"
    "nav{position:sticky;top:0;padding:16px 24px}"
    "h1{font-size:clamp(2rem,5vw,3.2rem)}"
    ".hero{background:linear-gradient(135deg,var(--accent),var(--accent-2));padding:90px 24px}"
    ".btn{padding:12px 22px;border-radius:12px;background:var(--accent);color:#fff;"
    "text-decoration:none;box-shadow:var(--shadow)}"
    "section{padding:90px 24px}.cards{display:grid;gap:20px}"
    ".card{border-radius:var(--radius);background:var(--surface);border:1px solid var(--border);padding:22px}"
    "footer{padding:30px;background:var(--surface)}</style>"
)


def _styled_body(inner: str, brand: str = "Nebula") -> str:
    """A visually-polished page shell around arbitrary body content."""
    return ("<!doctype html><html lang='en'><head><title>" + brand +
            "</title>" + _CSS_FOUNDATION + "</head><body>" + inner +
            "</body></html>")


# A genuinely designed single-file page that must pass the full visual QA gate
# (>=6 CSS rules, >=3 palette colors, border-radius, gradient/shadow, CTA
# buttons, nav/footer/h1, filled sections, closing </html>).
def _polished_page(brand: str = "Nebula") -> str:
    return (
        "<!doctype html><html lang='en'><head><title>" + brand + "</title></head>"
        "<body id='top'><style>"
        ":root{--bg:#0b0f1a;--surface:#151b2b;--text:#eef1fb;--muted:#a7b0c8;"
        "--accent:#6366f1;--accent-2:#22d3ee;--border:#232a3b;--radius:14px;"
        "--shadow:0 8px 24px rgba(0,0,0,.25)}"
        "body{background:var(--bg);color:var(--text);font-family:system-ui;"
        "margin:0;line-height:1.55}"
        "nav{position:sticky;top:0;padding:16px 24px;background:rgba(11,15,26,.8)}"
        "nav a{color:var(--text);text-decoration:none;margin-right:18px;font-weight:600}"
        "h1{font-size:clamp(2.2rem,5vw,3.4rem);line-height:1.1}"
        ".hero{background:linear-gradient(135deg,var(--accent),var(--accent-2));"
        "padding:96px 24px;text-align:center}"
        ".hero h1{color:#fff}"
        ".btn{display:inline-block;padding:12px 22px;border-radius:12px;"
        "background:var(--accent);color:#fff;text-decoration:none;"
        "box-shadow:var(--shadow);border:1px solid var(--border)}"
        ".btn:hover{transform:translateY(-2px)}"
        "section{padding:96px 24px}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}"
        ".card{border-radius:var(--radius);background:var(--surface);"
        "border:1px solid var(--border);padding:22px;box-shadow:var(--shadow)}"
        "footer{padding:32px;background:var(--surface);color:var(--muted);text-align:center}"
        "</style>"
        "<nav><a href='#features'>Features</a><a href='#pricing'>Pricing</a></nav>"
        "<main><section class='hero' id='features'><h1>" + brand +
        "</h1><p>Everything your team needs.</p>"
        "<a href='#pricing' class='btn'>Get started</a>"
        "<a href='#features' class='btn'>Learn more</a>"
        + "<p>" + "x" * 400 + "</p>" * 2 +
        "</section><section id='pricing'><div class='cards'>"
        "<div class='card'><h2>Starter</h2>" + "<p>" + "y" * 300 + "</p>" * 2 +
        "<button class='btn'>Choose Starter</button></div>"
        "<div class='card'><h2>Pro</h2>" + "<p>" + "z" * 300 + "</p>" * 2 +
        "<button class='btn'>Choose Pro</button></div></div></section>"
        "</main><footer>" + brand + " 2026</footer></body></html>"
    )


def _mk_demo_board(tmp_path, monkeypatch, board="bdemo", task_ids=("t1",)):
    """Point HERMES_HOME at a temp sandbox and create a real board DB with
    generic 'ready' tasks (the launch_demo_profile shape minus the graph)."""
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    hc._ensure_demo_board_db(board)
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        for i, tid in enumerate(task_ids):
            c.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (tid, "Task", "ecc-planner", "ready", 1000 + i))
            c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                      "VALUES (?,?,?,?)",
                      (tid, "created", None, 1000 + i))
        c.commit()
    finally:
        c.close()


def _db_events(board, tid):
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        return c.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (tid,)).fetchall()
    finally:
        c.close()


def _db_task(board, tid):
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        row = c.execute(
            "SELECT status, result, completed_at FROM tasks WHERE id=?", (tid,)).fetchone()
        return row
    finally:
        c.close()


def test_demo_llm_gemini_completion_payload(monkeypatch):
    """gemini path builds the generateContent payload and extracts text."""
    seen = {}

    def fake_post(url, payload, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        return {"candidates": [{"content": {"parts": [{"text": "  hello demo  "}]}}]}

    monkeypatch.setattr(demo_llm, "_post_json", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    text = demo_llm.completion("gemini", "gemini-3.5-flash-lite", "do it")
    assert text == "hello demo"
    assert "generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent" in seen["url"]
    assert seen["payload"]["contents"][0]["parts"][0]["text"] == "do it"


def test_demo_llm_openrouter_completion(monkeypatch):
    """openrouter path sends the chat payload with the bearer key."""
    seen = {}

    def fake_post(url, payload, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return {"choices": [{"message": {"content": "  or-answer  "}}]}

    monkeypatch.setattr(demo_llm, "_post_json", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ork-test")
    text = demo_llm.completion("openrouter", "nvidia/nemotron-3.5-lightning:free", "hi")
    assert text == "or-answer"
    assert seen["url"].startswith("https://openrouter.ai/")
    assert seen["headers"]["Authorization"] == "Bearer ork-test"


def test_demo_llm_missing_key_raises(monkeypatch):
    """A keyless thin completion must fail loudly, never fake an answer."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        demo_llm.completion("gemini", "gemini-3.5-flash-lite", "x")
        raise AssertionError("expected DemoLLMError")
    except demo_llm.DemoLLMError as exc:
        assert "GEMINI_API_KEY" in str(exc)


def test_demo_llm_unhandled_provider_raises():
    try:
        demo_llm.completion("anthropic", "claude-x", "x")
        raise AssertionError("expected DemoLLMError")
    except demo_llm.DemoLLMError as exc:
        assert "no completion path" in str(exc)


def test_deliverable_filename_mapping():
    assert demo_llm.deliverable_filename("write a README") == "README.md"
    assert demo_llm.deliverable_filename("landing HTML page") == "index.html"
    assert demo_llm.deliverable_filename("python script") == "app.py"
    # Web-intent goals map to a browsable index.html (served live by /p/<slug>/).
    assert demo_llm.deliverable_filename("build a website for a bakery") == "index.html"
    assert demo_llm.deliverable_filename("a landing page for our startup") == "index.html"
    assert demo_llm.deliverable_filename("create a dashboard UI") == "index.html"
    assert demo_llm.deliverable_filename("a single-page web app") == "index.html"
    assert demo_llm.deliverable_filename("anything else") == "deliverable.md"


def test_web_intent_guidance_in_prompts():
    web = demo_llm.builder_prompt("Build", "a landing page for a startup", "plan")
    assert "index.html" in web
    assert "no external CDNs, fonts, images, or libraries" in web
    assert "Never abbreviate content" in web
    code = demo_llm.builder_prompt("Build", "a FastAPI REST API", "plan")
    assert "index.html" not in code
    assert "single self-contained index.html" not in code


def test_builder_token_budget_scales_with_web_goals():
    assert demo_llm.builder_max_tokens("a landing page") > 400
    assert demo_llm.builder_max_tokens("build a dashboard UI") > 400
    # Non-web deliverables now get the larger dedicated builder budget (so real
    # project code comes back complete), while doc lanes keep the lean budget.
    assert demo_llm.builder_max_tokens("a REST API") == demo_llm._DELIVERABLE_MAX_TOKENS
    assert demo_llm.lane_max_tokens("a landing page", None) > 400
    assert demo_llm.lane_max_tokens("a REST API", "PLAN.md") == demo_llm._NORMAL_MAX_TOKENS


def test_web_artifact_qa_gate_catches_broken_deliverables():
    good = ("<!doctype html><html lang='en'><head><title>N</title></head><body>"
            + ("<p>" + "x" * 900 + "</p>") * 2 + "</body></html>")
    assert demo_llm.web_artifact_needs_repair(good) is False
    assert demo_llm.web_artifact_needs_repair("<!doctype html><title>cut") is True
    assert demo_llm.web_artifact_needs_repair("tiny") is True
    assert demo_llm.web_artifact_needs_repair("") is True
    fenced = "```html\n<!doctype html><html><body></body></html>\n"
    assert demo_llm.web_artifact_needs_repair(fenced) is True


def test_web_qa_issues_detect_broken_and_sandbox_broken_pages():
    bad = ("<!doctype html><html><body><nav><a href='#missing'>x</a></nav>"
           "<a href='#'>dead</a><script>localStorage.setItem('a',1);"
           "fetch('/x')</script><link rel='stylesheet' href='https://cdn.x/style.css'>"
           + ("<p>lorem ipsum text</p>" * 3) + "</body>")
    issues = demo_llm.web_qa_issues(bad)
    joined = "\n".join(issues)
    assert "no closing </html>" in joined
    assert "broken anchor" in joined
    assert "dead link" in joined
    assert "sandbox-unsafe: uses localStorage" in joined
    assert "fetch(" in joined
    assert "external resource referenced" in joined
    assert "lorem ipsum" in joined
    assert demo_llm.web_qa_should_repair(issues) is True

    good = _polished_page()
    assert demo_llm.web_qa_issues(good) == []


def test_web_qa_catches_invisible_black_page():
    dark_on_dark = ("<!doctype html><html><head><style>"
                    "body{background:#0b0f1a;color:#0b0f1a;font-family:system-ui}"
                    "</style></head><body><nav><a href='#f'>F</a></nav>"
                    + ("<p>'x' * 300</p>") + "garbage</body></html>")
    joined = "\n".join(demo_llm.web_qa_issues(dark_on_dark))
    assert "dark background with no light text color" in joined
    assert demo_llm.web_qa_should_repair(demo_llm.web_qa_issues(dark_on_dark)) is True

    undefined_var = ("<!doctype html><html><head><style>"
                     "body{background:#0b0f1a;color:var(--text)}</style></head>"
                     "<body><nav><a href='#f'>F</a></nav><section id='f'>"
                     + ("<p>" + "x" * 300 + "</p>") * 2 + "</section></body></html>")
    joined = "\n".join(demo_llm.web_qa_issues(undefined_var))
    assert "undefined CSS variable --text" in joined

    hollow = ("<!doctype html><html><head><style>"
              "body{background:#fff;color:#111}</style></head>"
              "<body><nav></nav><h1></h1><section></section><footer></footer>"
              "</body></html>")
    joined = "\n".join(demo_llm.web_qa_issues(hollow))
    assert "almost no readable text" in joined


def test_thin_execute_strips_wrapping_markdown_fence(monkeypatch, tmp_path):
    """A lenient completion that wraps HTML in backticks still lands a clean
    artifact on disk (the builder prompts forbid fences, but never trust the
    model)."""
    import secrets
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    board = "u1-fence-" + secrets.token_hex(3)
    ws = tmp_path / "ws"
    hc._ensure_demo_board_db(board)
    c = sqlite3.connect(str(hc._board_db_path(board)))
    try:
        hc._demo_insert_task(c, board, task_id="t_fix", title="Build",
                             assignee="ecc-build-fixer", status="ready")
        c.commit()
    finally:
        c.close()
    monkeypatch.setattr("hermes_client.demo_llm.completion",
                        lambda p, m, prompt, max_tokens=400, api_key=None:
                        "```html\n<!doctype html><title>X</title>\n```")
    hc.thin_execute(board=board, task_id="t_fix", workspace=str(ws),
                    provider="gemini", model="m", prompt="p",
                    objective="a landing page", artifact_name="index.html")
    out = (ws / "index.html").read_text(encoding="utf-8")
    assert "```" not in out
    assert out.startswith("<!doctype html>")


def test_thin_execute_drives_board_events_and_artifact(monkeypatch, tmp_path):
    """The thin executor drives REAL board state (claim -> attach -> complete)
    directly in kanban.db and writes the REAL artifact from the provider."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk_demo_board(tmp_path, monkeypatch)
    monkeypatch.setattr(demo_llm, "completion",
                        lambda p, m, prompt, max_tokens=400, api_key=None: "Final deliverable body\nsecond line")

    res = hc.thin_execute("bdemo", "t1", str(ws), "gemini", "gemini-3.5-flash-lite",
                          "PROMPT", objective="write a README")
    assert res["ok"] is True
    assert res["result"] == "Final deliverable body"
    art = ws / "README.md"
    assert art.exists()
    assert "Final deliverable body" in art.read_text(encoding="utf-8")

    kinds = [k for k, _ in _db_events("bdemo", "t1")]
    assert kinds == ["created", "claimed", "heartbeat", "heartbeat", "attached", "completed"]
    status, result, completed_at = _db_task("bdemo", "t1")
    assert status == "done"
    assert result == "Final deliverable body"
    assert completed_at


def test_thin_execute_failure_raises_and_marks_board(monkeypatch, tmp_path):
    """A thin-lane failure must be VISIBLE on the board (error timeline row +
    done-with-error task) before the lane re-raises — never silently stuck."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk_demo_board(tmp_path, monkeypatch)
    monkeypatch.setattr(demo_llm, "completion",
                        lambda p, m, prompt, max_tokens=400, api_key=None: (_ for _ in ()).throw(
                            demo_llm.DemoLLMError("gemini HTTP 401: bad key")))

    try:
        hc.thin_execute("bdemo", "t1", str(ws), "gemini", "g", "P", objective="x")
        raise AssertionError("expected DemoLLMError to propagate")
    except demo_llm.DemoLLMError:
        pass

    kinds = [k for k, _ in _db_events("bdemo", "t1")]
    assert "claimed" in kinds          # the lane really started
    assert "error" in kinds            # the reason is on the board
    status, result, _ = _db_task("bdemo", "t1")
    assert status == "done"            # never left running/stuck
    assert "ERROR" in (result or "")


def test_thin_execute_ignores_provider_absence_only_via_real_error(monkeypatch, tmp_path):
    """A board with no copy of the lane still surfaces the write failure loudly
    (error event) instead of pretending the lane ran."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk_demo_board(tmp_path, monkeypatch, task_ids=("t_other",))
    monkeypatch.setattr(demo_llm, "completion", lambda p, m, prompt, max_tokens=400, api_key=None: "ok")
    try:
        hc.thin_execute("bdemo", "missing", str(ws), "gemini", "g", "P", objective="x")
        raise AssertionError("expected RuntimeError")
    except Exception:
        pass
    kinds = [k for k, _ in _db_events("bdemo", "missing")]
    assert "error" in kinds


def test_reaper_skips_demo_boards(monkeypatch):
    """flux-demo-* boards are owned by the thin driver: the reaper must never
    dispatch a fat worker against them."""
    import main as main_mod
    dispatched = []

    monkeypatch.setattr(main_mod.hc, "list_boards",
                        lambda: [{"slug": "flux-demo-1"}, {"slug": "u1-real"}])
    monkeypatch.setattr(main_mod, "_board_finalized", lambda slug: False)
    monkeypatch.setattr(main_mod.hc, "board_is_sealed", lambda slug: False)
    monkeypatch.setattr(main_mod.hc, "kill_stale_workers", lambda slug: None)
    monkeypatch.setattr(main_mod.hc, "bump_blocked_to_ready", lambda slug: None)
    monkeypatch.setattr(main_mod.hc, "board_has_unfinished_work", lambda slug: True)
    monkeypatch.setattr(main_mod.hc, "dispatch",
                        lambda slug, max_spawn=None, blocking=False: dispatched.append(slug))

    main_mod._reconcile_boards_once()
    assert dispatched == ["u1-real"]


def test_thin_execute_failure_lands_error_event(monkeypatch, tmp_path):
    """A thin-lane failure must be VISIBLE on the board (error timeline row)
    before the lane re-raises — the demo never fails silently."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk_demo_board(tmp_path, monkeypatch)
    monkeypatch.setattr(demo_llm, "completion",
                        lambda p, m, prompt, max_tokens=400, api_key=None: (_ for _ in ()).throw(
                            demo_llm.DemoLLMError("gemini HTTP 401: bad key")))

    try:
        hc.thin_execute("bdemo", "t1", str(ws), "gemini", "g", "P", objective="x")
        raise AssertionError("expected DemoLLMError to propagate")
    except demo_llm.DemoLLMError:
        pass
    err_rows = [p for k, p in _db_events("bdemo", "t1") if k == "error"]
    assert err_rows and "HTTP 401" in (err_rows[0] or "")


def test_error_kind_renders_in_activity_log(monkeypatch, tmp_path):
    """The UI timeline renders a 'Demo error: …' row for kind=error events."""
    monkeypatch.setattr(hc, "HERMES_HOME", str(tmp_path))
    board = "flux-demo-render"
    db = hc._board_db_path(board)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, "
                "kind TEXT, payload TEXT, created_at REAL)")
    con.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("t1", "error", json.dumps({"message": "gemini HTTP 429: rate limited"}), 1000))
    con.commit()
    con.close()
    ev = hc._task_activity_events(board, "t1")
    assert ev and ev[0]["kind"] == "error"
    assert "HTTP 429" in ev[0]["label"]


def test_demo_drive_builder_lane_runs(monkeypatch, tmp_path):
    """Regression: the demo builder lane must actually RUN and write index.html.

    _demo_drive used to call _run_builder with a stale signature (demo=True,
    no task_title) → TypeError → builder stayed pending forever → /p/ only
    served the dark fallback card (the 'black page'). This drives the real
    _run_builder wiring synchronously and proves the artifact is produced."""
    import main as main_mod

    class SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(main_mod.threading, "Thread", SyncThread)

    ws = tmp_path / "ws"
    ws.mkdir()

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        if artifact_name == "index.html":
            (Path(workspace) / artifact_name).write_text(
                _styled_body(
                    "<nav><a href='#f'>F</a></nav>"
                    "<section id='f'><h1>Nebula</h1>"
                    + ("<p>" + "x" * 300 + "</p>") * 2 +
                    "</section><footer>Nebula 2026</footer>"),
                encoding="utf-8")
        else:
            (Path(workspace) / artifact_name).write_text(
                "plan: build the landing page", encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    main_mod._demo_drive(
        slug="flux-demo-regr", goal="Build a landing page for Nebula",
        planner_id="tp", builder_id="tb", workspace=str(ws),
        provider="gemini", model="g")

    assert (ws / "index.html").exists()
    assert "Nebula" in (ws / "index.html").read_text(encoding="utf-8")


def test_run_builder_accepts_demo_call_shape(monkeypatch, tmp_path):
    """The exact call shape _demo_drive uses must not TypeError."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "ws2"
    ws.mkdir()
    calls = {}

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        calls["max_tokens"] = max_tokens
        (Path(workspace) / artifact_name).write_text(
            _styled_body(
                "<nav><a href='#f'>F</a></nav>"
                "<section id='f'><h1>Nebula</h1>"
                + ("<p>" + "x" * 300 + "</p>") * 2 +
                "</section><footer>Nebula 2026</footer>"),
            encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    out = main_mod._run_builder(
        slug="flux-demo-regr", task_id="tb", workspace=str(ws),
        provider="gemini", model="g", objective="Build a landing page for Nebula",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page for Nebula"))

    assert out["ok"]
    assert calls["max_tokens"] == demo_llm.demo_builder_max_tokens(
        "Build a landing page for Nebula")


def test_demo_builder_token_budget_is_web_scaled():
    assert demo_llm.demo_builder_max_tokens("Build a landing page") \
        >= demo_llm.builder_max_tokens("Build a landing page")
    # Non-web builders use the upgraded deliverable budget (was 800).
    assert demo_llm.demo_builder_max_tokens("make a CLI tool") \
        == demo_llm.builder_max_tokens("make a CLI tool") \
        == demo_llm._DELIVERABLE_MAX_TOKENS


def test_planner_prompt_asks_for_structured_json_plan():
    p = demo_llm.planner_prompt("Plan the demo deliverable", "landing page")
    assert "JSON" in p
    assert '"sections"' in p
    assert '"ids"' in p


def test_plan_to_brief_parses_json_with_and_without_fences():
    plan = ('{"overview": "Nebula landing", "palette": "dark + indigo/cyan", '
            '"sections": ["Features", "Pricing"], '
            '"ids": {"Features": "features", "Pricing": "pricing"}, '
            '"features": ["auth", "insights"], '
            '"cta": "Start free", "constraints": ["no external resources"]}')
    for wrapped in (plan, "```json\n" + plan + "\n```"):
        brief = demo_llm.plan_to_brief(wrapped)
        assert "Features #features" in brief
        assert "Pricing #pricing" in brief
        assert "Start free" in brief
        assert "no external resources" in brief
    # plain-text fallback never throws
    assert demo_llm.plan_to_brief("plain lines\nhere") \
        and "plain lines" in demo_llm.plan_to_brief("plain lines\nhere")


def test_web_qa_unlinked_sections_and_score(monkeypatch, tmp_path):
    coherent = _polished_page()
    assert not any("unlinked" in i for i in demo_llm.web_qa_issues(coherent))
    assert demo_llm.web_deliverable_score(coherent) >= 70

    orphan = ("<!doctype html><html><head><style>body{background:#0b0f1a;"
              "color:#eef1fb}</style></head><body><nav></nav>"
              "<section id='orphan'>" + ("<p>" + "x" * 300 + "</p>") * 2 +
              "</section></body>")  # truncated (no </html>)
    joined = "\n".join(demo_llm.web_qa_issues(orphan))
    assert "unlinked section id=#orphan" in joined
    assert demo_llm.web_qa_should_repair(demo_llm.web_qa_issues(orphan)) is True
    assert demo_llm.web_deliverable_score(orphan) < 50


def test_lane_model_override_respects_env(monkeypatch):
    import main as main_mod

    monkeypatch.delenv("FLUXSWARM_BUILDER_MODEL", raising=False)
    assert main_mod._lane_model("BUILDER", "base-model") == "base-model"
    monkeypatch.setenv("FLUXSWARM_BUILDER_MODEL", "paid-gpt")
    assert main_mod._lane_model("BUILDER", "base-model") == "paid-gpt"
    # default fork still falls back when role differs
    assert main_mod._lane_model("PLANNER", "base-model") == "base-model"


def test_run_builder_repairs_then_reaudits_broken_deliverable(monkeypatch, tmp_path):
    """A truncated deliverable is repaired ONCE and the repaired artifact is
    re-audited — a repair that comes back good must ship the good page."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "ws3"
    ws.mkdir()
    calls = []

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        n = len(calls)
        calls.append(prompt)
        if artifact_name == "index.html":
            if n == 0:
                (Path(workspace) / artifact_name).write_text(
                    "<!doctype html><html><head><style>.hero p { max-width: 650px;",
                    encoding="utf-8")  # first build is truncated
            else:
                (Path(workspace) / artifact_name).write_text(
                        _styled_body(
                            "<nav><a href='#f'>F</a></nav>"
                            "<section id='f'><h1>Nebula</h1>"
                            + ("<p>" + "x" * 300 + "</p>") * 2 +
                            "</section><footer>Nebula 2026 "
                            "<button class='btn'>Join</button></footer>"),
                        encoding="utf-8")
        else:
            (Path(workspace) / artifact_name).write_text("plan", encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    out = main_mod._run_builder(
        slug="flux-demo-regr", task_id="tb", workspace=str(ws),
        provider="gemini", model="g", objective="Build a landing page for Nebula",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page for Nebula"))

    assert out["ok"]
    assert out.get("retried") is True
    # 1 initial build + 1 repair (good page ships)
    assert len(calls) == 2
    assert "</html>" in (ws / "index.html").read_text(encoding="utf-8")


def test_run_builder_bounded_repair_stops_after_tail_salvage(monkeypatch, tmp_path):
    """A repair that is ITSELF still truncated must not loop forever: at most
    1 build + 2 repair passes, then the deterministic tail-completion appends
    the missing closure so a valid page ships."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "ws4"
    ws.mkdir()
    calls = []

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        calls.append(artifact_name)
        if artifact_name == "index-tail.html":
            (Path(workspace) / artifact_name).write_text(
                "</body></html>", encoding="utf-8")
        else:
            (Path(workspace) / artifact_name).write_text(
                ("<!doctype html><html><head><style>.hero p { max-width: 650px;\n"
                 + ".features { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; }\n"
                 + ".pricing { margin-top: 48px; }\n" + "/*pad*/" * 60
                 + "\n</style>"),
                encoding="utf-8")  # always truncated (mid-CSS, no </html>)
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    out = main_mod._run_builder(
        slug="flux-demo-regr", task_id="tb", workspace=str(ws),
        provider="gemini", model="g", objective="Build a landing page for Nebula",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page for Nebula"))

    # build + 2 repairs + 1 tail-salvage
    assert calls.count("index.html") == 3
    assert calls.count("index-tail.html") == 1
    assert out.get("retried") is True
    assert out.get("tail_completed") is True
    final = (ws / "index.html").read_text(encoding="utf-8")
    assert final.endswith("</body></html>")


def test_web_qa_unlinked_only_semantic_sections():
    """Form-field ids (#email etc.) are functional hooks, NOT 'walls the nav
    never reaches' — the unlinked-section check must stay semantic-only."""
    html = ("<!doctype html><html><head><style>body{background:#0b0f1a;"
            "color:#eef1fb}</style></head><body><nav>"
            "<a href='#sec'>S</a></nav><section id='sec'><h2>S</h2>"
            + ("<p>" + "x" * 300 + "</p>") +
            "</section><form><input id='email'></form>"
            "<footer>N 2026</footer></body></html>")
    joined = "\n".join(demo_llm.web_qa_issues(html))
    assert "unlinked section id=#email" not in joined
    assert not any("unlinked" in i for i in demo_llm.web_qa_issues(html))


def test_anchor_repair_helpers():
    issues = ["dead link: href='#' (no target)",
              "broken anchor: #about links to a missing id",
              "no <footer> section"]
    assert demo_llm.web_qa_structural_repair(issues) is False
    assert demo_llm.only_anchor_issues(issues) is True
    anchors = demo_llm.web_anchor_issues(issues)
    assert any("broken anchor" in i for i in anchors)
    assert any("dead link" in i for i in anchors)
    assert not any("unlinked" in i for i in anchors)
    issues2 = ["sandbox-unsafe: uses fetch(…)", "dead link: href='#' (no target)"]
    assert demo_llm.web_qa_structural_repair(issues2) is True
    assert demo_llm.only_anchor_issues(issues2) is False


def test_run_builder_anchor_patch_fixes_dead_link_without_rebuild(monkeypatch, tmp_path):
    """A complete page whose ONLY defect is a dead href='#' must NOT burn a
    full rebuild round: the deterministic corner-patch retargets it to #top
    (and pins id='top' on <body>) so a single build call ships."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "wsA"
    ws.mkdir()
    calls = []

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        calls.append(artifact_name)
        (Path(workspace) / artifact_name).write_text(
            _styled_body(
                "<nav><a href='#'>Home</a><a href='#features'>F</a></nav>"
                "<section id='features'><h1>Nebula</h1>"
                + ("<p>" + "x" * 300 + "</p>") * 2 +
                "</section><footer>N 2026 <button class='btn'>Go</button></footer>"),
            encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    out = main_mod._run_builder(
        slug="flux-demo-regr", task_id="tb", workspace=str(ws),
        provider="gemini", model="g", objective="Build a landing page for Nebula",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page for Nebula"))

    assert calls.count("index.html") == 1          # no rebuild round
    assert out.get("anchor_patched") is True
    final = (ws / "index.html").read_text(encoding="utf-8")
    assert 'href="#top"' in final
    assert 'id="top"' in final


def test_web_content_gap_detects_empty_promised_sections():
    shell = ("<!doctype html><html><head><style>body{background:#0b0f1a;"
             "color:#eef1fb}</style></head><body><nav>"
             "<a href='#starters'>S</a><a href='#mains'>M</a>"
             "<a href='#desserts'>D</a></nav>"
             "<section id='starters'><h1>Starters</h1></section>"
             "<section id='mains'></section><section id='desserts'></section>"
             "<footer>N 2026</footer></body></html>")
    gap = demo_llm.web_content_gap(shell)
    assert all(g in gap for g in ("starters", "mains", "desserts"))
    joined = "\n".join(demo_llm.web_qa_issues(shell))
    assert "hollow page" in joined
    # starters with real content must NOT be in gap
    filled = ("<!doctype html><html><head><style>body{background:#0b0f1a;"
              "color:#eef1fb}</style></head><body><nav>"
              "<a href='#starters'>S</a><a href='#mains'>M</a>"
              "<a href='#desserts'>D</a></nav>"
              "<section id='starters'><h1>Starters</h1>"
              + ("<p>" + "x" * 300 + "</p>") * 3 +
              "</section>"
              "<section id='mains'><h2>Mains</h2>"
              + ("<p>" + "y" * 300 + "</p>") * 2 +
              "</section><section id='desserts'><h2>Desserts</h2>"
              + ("<p>" + "z" * 300 + "</p>") * 2 +
              "</section><footer>N 2026</footer></body></html>")
    assert demo_llm.web_content_gap(filled) == []
    assert not any("hollow" in i or "skeleton" in i
                   for i in demo_llm.web_qa_issues(filled))


def test_web_qa_skeleton_shell_vs_real_page():
    # Skeleton: >500 chars, has nav + h1 + footer, but sections hold only
    # brief headings (not enough body text).  "too short" and "no readable
    # text" must NOT fire — only hollow/skeleton should appear.  The shell is
    # visually designed (styled body) so the content gap is the ONLY issue.
    shell = _styled_body(
        "<nav><a href='#a'>A</a><a href='#b'>B</a></nav>"
        "<h1>Title</h1>"
        "<section id='a'><h2>Section A</h2>" + "x" * 60 +
        "</section><section id='b'><h2>Section B</h2>" + "y" * 60 +
        "</section><footer>N 2026 <button class='btn'>Go</button></footer>")
    joined = "\n".join(demo_llm.web_qa_issues(shell))
    assert "skeleton page" in joined
    assert demo_llm.web_qa_structural_repair(demo_llm.web_qa_issues(shell)) is False
    filled = shell.replace("x" * 60, "x" * 400).replace("y" * 60, "y" * 400)
    assert not any("skeleton" in i or "hollow" in i
                   for i in demo_llm.web_qa_issues(filled))


def test_run_builder_content_patch_fills_missing_sections(monkeypatch, tmp_path):
    """A page that is structurally complete but hollow (nav promises sections
    that hold no content) must NOT burn rebuild rounds: the narrow
    content-completion patch fills the empty sections."""
    import main as main_mod

    monkeypatch.setattr(main_mod.hc, "HERMES_HOME", str(tmp_path))
    ws = tmp_path / "wsC"
    ws.mkdir()
    calls = []
    # starters is genuinely filled (builder wrote that section fully);
    # mains and desserts are empty shells (heading only).
    shell = _styled_body(
        "<nav><a href='#starters'>S</a><a href='#mains'>M</a>"
        "<a href='#desserts'>D</a></nav>"
        "<section id='starters'><h1>Menu</h1>"
        + ("<p>" + "x" * 300 + "</p>") * 3 +
        "</section><section id='mains'><h2>Mains</h2></section>"
        "<section id='desserts'><h2>Desserts</h2></section>"
        "<footer>N 2026 <button class='btn'>Order</button></footer>")

    def fake_execute(*, board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None, max_tokens=None):
        calls.append(artifact_name)
        if artifact_name == "index-content.html":
            (Path(workspace) / artifact_name).write_text(
                ("<section id='mains'><h2>Main Courses</h2>"
                 + ("<p>" + "y" * 300 + "</p>") * 2 +
                 "</section><section id='desserts'><h2>Desserts</h2>"
                 + ("<p>" + "z" * 300 + "</p>") * 2),
                encoding="utf-8")
        else:
            (Path(workspace) / artifact_name).write_text(shell, encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)

    out = main_mod._run_builder(
        slug="flux-demo-regr", task_id="tb", workspace=str(ws),
        provider="gemini", model="g", objective="Build a menu page for the restaurant",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a menu page"))

    assert calls.count("index.html") == 1          # shell rebuilt ZERO times
    assert out.get("content_patched") is True
    final = (ws / "index.html").read_text(encoding="utf-8")
    assert "<section id='mains'" in final and "<section id='desserts'" in final
    assert demo_llm.web_content_gap(final) == []