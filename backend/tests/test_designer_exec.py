"""Phase 9 ? the Designer lane (ecc-designer, DESIGN.md) must ship an
EXECUTABLE visual design system, not prose. The Designer blueprint is a
single deterministic json.loads()-able JSON object with:
  * palette (exact hex roles: --bg/--surface/--text/--muted/--accent/
    --accent-2);
  * typography (display/heading/body with explicit clamp/px + weight);
  * spacing + tokens (spacing scale, radius/shadow/color tokens);
  * component_rules, each with a concrete path + responsibility + interfaces
    (in/out types) + data_flow;
  * responsive + interaction_states + accessibility sections;
  * consistency + reproducibility + keys;
  * decisions (each AD-n with id + title + rationale; decisions reference
    concrete D-n components);
  * a web-contract branch mirroring the Architect lane: when the objective is
    web, the Designer keeps the web contract intact and preserves the
    promised ids (index.html#hero, index.html#grid) so the page stays
    buildable; when the objective is CLI / library / API, web-contract
    preservation does not apply and it says so explicitly, with NO web branch.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo_llm
import json

import io

def _bom_safe_read(path):
    with io.open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()

_src = _bom_safe_read(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "demo_llm.py"))
assert "def design_is_executable(" in _src
assert "def design_executable_issues(" in _src
assert '_DESIGN_MAX_TOKENS' in _src
assert 'if name == "design.md":' in _src

_VALID_DESIGN = {
    "palette": [
        {"role": "--bg", "hex": "#0f1115"},
        {"role": "--surface", "hex": "#171a21"},
        {"role": "--text", "hex": "#eef1f7"},
        {"role": "--muted", "hex": "#8b93a7"},
        {"role": "--accent", "hex": "#6366f1"},
        {"role": "--accent-2", "hex": "#22d3ee"},
    ],
    "typography": [
        {"role": "display", "size": "clamp(40px, 6vw, 68px)", "weight": 800},
        {"role": "heading", "size": "clamp(24px, 3.2vw, 34px)", "weight": 700},
        {"role": "body", "size": "17px", "weight": 400},
    ],
    "spacing": [
        {"token": "--space-4", "value": "4px"},
        {"token": "--space-8", "value": "8px"},
        {"token": "--space-16", "value": "16px"},
        {"token": "--space-24", "value": "24px"},
        {"token": "--space-48", "value": "48px"},
        {"token": "--space-96", "value": "96px"},
    ],
    "tokens": [
        {"name": "--radius", "value": "14px"},
        {"name": "--shadow", "value": "0 12px 32px rgba(0,0,0,0.35)"},
    ],
    "component_rules": [
        {"id": "D-1", "name": "hero", "path": "index.html#hero",
         "responsibility": "full app hero band",
         "interfaces": {"in": "title + subtitle strings", "out": "hero band"},
         "data_flow": "title -> hero background gradient"},
        {"id": "D-2", "name": "feature-grid", "path": "index.html#grid",
         "responsibility": "responsive feature cards",
         "interfaces": {"in": "feature list", "out": "card grid"},
         "data_flow": "features -> grid cards"},
    ],
    "responsive": [
        {"id": "D-3", "name": "stack-small", "breakpoint": "640px",
         "responsibility": "single-column stacking",
         "interfaces": {"in": "columns", "out": "stack"},
         "data_flow": "grid -> stacked cards"},
    ],
    "interaction_states": [
        {"id": "D-4", "name": "card-hover", "states": ["hover"],
         "responsibility": "lift + accent border",
         "interfaces": {"in": "state", "out": "accent"},
         "data_flow": "hover -> border color"},
    ],
    "accessibility": [
        {"id": "D-5", "name": "contrast", "responsibility": ">=4.5:1 text",
         "interfaces": {"in": "palette", "out": "ratio"},
         "data_flow": "hexes -> contrast check"},
    ],
    "consistency": [
        "every component rule references palette/typography/token ids",
    ],
    "reproducibility": [
        "exact hexes + clamp values + tokens rebuild the same DESIGN.md",
    ],
    "keys": ["DESIGN.md", "index.html#hero", "index.html#grid"],
    "decisions": [
        {"id": "AD-1", "title": "Dark high-contrast palette",
         "rationale": "professional landing + passes contrast checks"},
        {"id": "AD-2", "title": "Token-driven components",
         "rationale": "no improvisation: rules reference the tokens"},
    ],
}


def _web_design_prompt(objective_web: bool) -> str:
    objective = "a professional dark-themed marketing landing page" \
        if objective_web else "a CLI tool that prints JSON"
    return demo_llm.designer_prompt(
        "Design System",
        objective,
        plan=json.dumps({"design": ["DESIGN.md", "index.html#hero"]}),
    )


def test_designer_prompt_returns_executable_json_blueprint():
    text = _web_design_prompt(objective_web=True)
    obj = json.loads(text)
    assert obj["palette"]
    assert obj["typography"]
    assert obj["spacing"]
    assert obj["tokens"]
    assert len(obj["component_rules"]) >= 2
    assert obj["responsive"]
    assert obj["interaction_states"]
    assert obj["accessibility"]
    assert obj["consistency"]
    assert obj["reproducibility"]
    assert obj["keys"]
    assert any(d["id"].startswith("AD-") for d in obj["decisions"])


def test_design_is_executable_accepts_deterministic_system():
    text = _web_design_prompt(objective_web=True)
    assert demo_llm.design_is_executable(text) is True


def test_design_executable_flags_missing_sections():
    broken = dict(_VALID_DESIGN)
    broken.pop("palette")
    issues = demo_llm.design_executable_issues(json.dumps(broken))
    assert any("palette" in i.lower() for i in issues)


def test_design_executable_flags_undefined_references():
    broken = dict(_VALID_DESIGN)
    broken["component_rules"] = list(broken["component_rules"]) + [
        {"id": "D-9", "name": "ghost", "path": "index.html#ghost",
         "responsibility": "feeds D-9 and AD-9",
         "interfaces": {"in": "x", "out": "y"},
         "data_flow": "D-9 -> AD-9"},
    ]
    issues = demo_llm.design_executable_issues(json.dumps(broken))
    assert any("D-9" in i for i in issues)
    assert any("AD-9" in i for i in issues)


def test_designer_prompt_web_contract_branch_deterministic():
    web = _web_design_prompt(objective_web=True)
    assert "keep the web contract intact" in web
    assert "preserve the promised ids" in web
    assert "index.html#hero" in web

    cli = _web_design_prompt(objective_web=False)
    assert "web contract preservation does not apply" in cli
    assert "keep the web contract intact" not in cli
    assert "index.html#hero" not in cli
