"""Multi-file build protocol tests.

Complex goals (multi-page sites, API+frontend, auth/database …) can't live in
a single self-contained file. The Builder emits a "file pack" — its primary
file first, then `==== FILE: <relpath>` markers with extra file contents. These
tests lock:

* complex_objective() classification (conservative, not a word-match trap),
* split_artifact() marker parsing + path-safety,
* multi_file_protocol() prompt guidance,
* builder_prompt() emitting the protocol for complex web AND code goals (and
  NOT for simple single-file goals),
* _run_builder() writing extra workspace files (and never clobbering
  index.html / path-traversal),
* the evidence gate's multi_file_links check.
"""
from __future__ import annotations

from pathlib import Path

import demo_llm
from demo_llm import (complex_objective, multi_file_protocol,
                      split_artifact, builder_max_tokens)


def test_complex_objective_detects_real_complexity():
    assert complex_objective("A restaurant website with a menu") is False
    assert complex_objective("Build a landing page") is False
    assert complex_objective("Sell organic soap online") is False
    assert complex_objective("A website") is False


def test_complex_objective_detects_complex():
    assert complex_objective(
        "A multi-page restaurant website with a menu, about and contact pages") is True
    assert complex_objective(
        "A SaaS platform with login and a database") is True
    assert complex_objective(
        "Build a REST API with user accounts") is True
    assert complex_objective(
        "A web app with an admin dashboard") is True
    assert complex_objective(
        "An e-commerce store with multiple pages and payments") is True


def test_deliverable_still_single_file_for_simple():
    assert demo_llm.deliverable_filename("Build a landing page") == "index.html"


def test_builder_max_tokens_complex_boost():
    simple = builder_max_tokens("Build a landing page")
    complex_ = builder_max_tokens(
        "A SaaS platform with login and a database")
    assert complex_ > simple
    assert complex_ <= 12000


def test_multi_file_protocol_instructs_marker_format():
    text = multi_file_protocol()
    assert "==== FILE:" in text
    assert "css/" in text
    assert "js/" in text
    assert "src/" in text


def test_split_artifact_single_file_unchanged():
    body = "<html><body>Hello</body></html>"
    assert split_artifact(body) == {"": body}


def test_split_artifact_pack():
    pack = (
        "<!doctype html><html><body>HOME</body></html>\n"
        "==== FILE: pages/about.html\n"
        "<!doctype html><html><body>ABOUT</body></html>\n"
        "==== FILE: css/app.css\n"
        "body{color:#333}\n"
    )
    files = split_artifact(pack)
    assert files[""] == "<!doctype html><html><body>HOME</body></html>"
    assert files["pages/about.html"] == "<!doctype html><html><body>ABOUT</body></html>"
    assert files["css/app.css"] == "body{color:#333}"


def test_split_artifact_rejects_traversal():
    pack = (
        "PRIMARY\n"
        "==== FILE: ../../../etc/passwd\n"
        "bad\n"
        "==== FILE: safe.txt\n"
        "ok\n"
    )
    files = split_artifact(pack)
    assert "safe.txt" in files
    assert not any(".." in k or k.startswith("/") for k in files if k != "")


def test_split_artifact_empty_input():
    assert split_artifact("") == {"": ""}
    assert split_artifact(None) == {"": ""}


def _mk_ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_builder_prompt_complex_web_has_protocol():
    p = demo_llm.builder_prompt("Build", "A multi-page restaurant website with a menu",
                                "plan")
    assert "==== FILE:" in p
    assert "pages/about.html" in p
    assert "css/app.css" in p


def test_builder_prompt_complex_code_has_protocol():
    p = demo_llm.builder_prompt(
        "Build", "A REST API with user accounts", "plan")
    assert "==== FILE:" in p


def test_builder_prompt_simple_has_no_protocol():
    p = demo_llm.builder_prompt("Build", "Build a landing page", "plan")
    assert "==== FILE:" not in p


# --- _run_builder integration ------------------------------------------------

def _full_page(inner: str = "", href: str = "") -> str:
    """A structurally-complete, QA-passing page (>500 chars, </html>, styled)."""
    link = "" if not href else f"<a href='{href}'>Link</a>"
    return (
        "<!doctype html><html lang='en'><head><meta name='viewport' "
        "content='width=device-width, initial-scale=1'><title>Multi</title></head>"
        "<body id='top'><style>"
        ":root{--bg:#0b0f1a;--surface:#151b2b;--text:#eef1fb;--muted:#a7b0c8;"
        "--accent:#6366f1;--accent-2:#22d3ee;--border:#232a3b;--radius:14px;"
        "--shadow:0 8px 24px rgba(0,0,0,.25)}"
        "@media(max-width:640px){section{padding:48px 16px}.cards{grid-template-columns:1fr}}"
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
        "section{padding:96px 24px}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}"
        ".card{border-radius:var(--radius);background:var(--surface);"
        "border:1px solid var(--border);padding:22px;box-shadow:var(--shadow)}"
        "footer{padding:32px;background:var(--surface);color:var(--muted);text-align:center}"
        "</style>"
        "<nav><a href='#features'>Features</a><a href='#pricing'>Pricing</a></nav>"
        "<main><section class='hero' id='features'><h1>Multi</h1>"
        "<p>Everything your team needs.</p>"
        f"{link}"
        "<a href='#pricing' class='btn'>Get started</a>"
        "<a href='#features' class='btn'>Learn more</a>"
        + "<p>" + "x" * 400 + "</p>" * 2 +
        "</section><section id='pricing'><div class='cards'>"
        "<div class='card'><h2>Starter</h2>" + "<p>" + "y" * 300 + "</p>" * 2 +
        "<button class='btn'>Choose Starter</button></div>"
        "<div class='card'><h2>Pro</h2>" + "<p>" + "z" * 300 + "</p>" * 2 +
        "<button class='btn'>Choose Pro</button></div></div></section>"
        "</main><footer>Multi 2026</footer></body></html>"
    )


def _poly_pack(href: str = "pages/about.html") -> str:
    """A valid multi-file pack whose primary part passes the web QA gate."""
    return (
        _full_page(href=href) + "\n"
        "==== FILE: pages/about.html\n"
        "<!doctype html><html lang='en'><head><title>About</title></head>"
        "<body>About us</body></html>\n"
        "==== FILE: css/app.css\n"
        "body{color:#333}\n"
    )


def _fake_execute_pack():
    def fake_execute(board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None,
                     max_tokens=400):
        (Path(workspace) / artifact_name).write_text(_poly_pack(), encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}
    return fake_execute


def test_run_builder_writes_pack_files(tmp_path, monkeypatch):
    """A builder pack split must write the extra files into the workspace and
    keep index.html as the (stripped) primary file."""
    import main as main_mod
    import hermes_client as hc

    ws = _mk_ws(tmp_path)
    monkeypatch.setattr(main_mod.hc, "thin_execute", _fake_execute_pack())

    out = main_mod._run_builder(
        slug="flux-demo-mf", task_id="tb", workspace=str(ws),
        provider="gemini", model="g",
        objective="A multi-page restaurant website with a menu",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens(
            "A multi-page restaurant website with a menu"))

    assert out.get("ok") is True
    assert out.get("files_built") == ["pages/about.html", "css/app.css"]
    assert "==== FILE:" not in (ws / "index.html").read_text(encoding="utf-8")
    assert (ws / "pages" / "about.html").read_text(encoding="utf-8").startswith(
        "<!doctype html>")
    assert (ws / "css" / "app.css").read_text(encoding="utf-8") == "body{color:#333}"


def test_run_builder_ignores_single_file(tmp_path, monkeypatch):
    """A simple single-file output must NOT be split/clobbered."""
    import main as main_mod
    import hermes_client as hc

    ws = _mk_ws(tmp_path)
    page = _full_page()

    def fake_execute(board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None,
                     max_tokens=400):
        (Path(workspace) / artifact_name).write_text(page, encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    out = main_mod._run_builder(
        slug="flux-demo-sf", task_id="tb", workspace=str(ws),
        provider="gemini", model="g",
        objective="Build a landing page",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens("Build a landing page"))

    assert out.get("ok") is True
    assert "files_built" not in out
    assert (ws / "index.html").read_text(encoding="utf-8") == page


def test_run_builder_ignores_pack_traversal(tmp_path, monkeypatch):
    """Path-traversal marker names must not be written outside the workspace."""
    import main as main_mod
    import hermes_client as hc

    ws = _mk_ws(tmp_path)

    def fake_execute(board, task_id, workspace, provider, model, prompt,
                     objective="", artifact_name=None, api_key=None,
                     max_tokens=400):
        (Path(workspace) / artifact_name).write_text(
            _full_page(href="inside.txt") + "\n"
            "==== FILE: ../../../escape.txt\n"
            "bad\n"
            "==== FILE: inside.txt\n"
            "good\n",
            encoding="utf-8")
        return {"ok": True, "elapsed_s": 1}

    monkeypatch.setattr(main_mod.hc, "thin_execute", fake_execute)
    out = main_mod._run_builder(
        slug="flux-demo-trav", task_id="tb", workspace=str(ws),
        provider="gemini", model="g",
        objective="A multi-page restaurant website with a menu",
        brief="plan", task_title=hc.DEMO_BUILDER_TITLE,
        max_tokens=demo_llm.demo_builder_max_tokens(
            "A multi-page restaurant website with a menu"))

    assert "files_built" in out
    assert not (ws.parent / "escape.txt").exists()
    assert (ws / "inside.txt").read_text(encoding="utf-8") == "good"


# --- evidence gate -----------------------------------------------------------

def test_evidence_gate_multi_file_check(tmp_path):
    from evidence_gate import collect_evidence

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "PLAN.md").write_text("# plan", encoding="utf-8")
    (ws / "ARCHITECTURE.md").write_text("# arch", encoding="utf-8")
    (ws / "REVIEW.md").write_text("# review", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (ws / "index.html").write_text(
        "<!doctype html><html><body>"
        "<a href='pages/about.html'>About</a>"
        "</body></html>",
        encoding="utf-8")
    (ws / "pages").mkdir()
    (ws / "pages" / "about.html").write_text(
        "<!doctype html><html><body>About</body></html>", encoding="utf-8")

    ev = collect_evidence(str(ws), goal="A multi-page website")
    names = {c["name"]: c for c in ev["checks"]}
    assert "multi_file_links" in names
    assert names["multi_file_links"]["status"] == "pass"
    assert "extra workspace file(s)" in names["multi_file_links"]["detail"]


def test_evidence_gate_multi_file_dangling_link(tmp_path):
    """A relative href with no backing file must surface as a WARN."""
    from evidence_gate import collect_evidence

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "PLAN.md").write_text("# plan", encoding="utf-8")
    (ws / "ARCHITECTURE.md").write_text("# arch", encoding="utf-8")
    (ws / "REVIEW.md").write_text("# review", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (ws / "index.html").write_text(
        "<!doctype html><html><head><title>X</title></head><body>"
        "<a href='pages/missing.html'>Oops</a>"
        "<style>body{background:#0b0f1a;color:#eef1fb;"
        "font-family:system-ui;margin:0}</style>"
        "<h1>X</h1><p>" + ("x" * 300) + "</p>"
        "</body></html>",
        encoding="utf-8")

    ev = collect_evidence(str(ws), goal="A multi-page website")
    names = {c["name"]: c for c in ev["checks"]}
    assert names["multi_file_links"]["status"] == "warn"
    assert "pages/missing.html" in names["multi_file_links"]["detail"]