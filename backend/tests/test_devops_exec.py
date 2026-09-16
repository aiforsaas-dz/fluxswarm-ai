"""Phase 10 — the DevOps lane (ecc-devops, DEVOPS.md) must ship an EXECUTABLE
deployment blueprint, not a prose Dockerfile. The DevOps blueprint is a single
deterministic json.loads()-able JSON object with:
  * components (container / dependencies / environment / scripts / health-check
    / ci-cd / deployment), each with a concrete path + responsibility +
    interfaces (in/out types) + data_flow;
  * build steps (pinned base image, install from a lockfile, exposed port);
  * health_check (a probeable endpoint answering 2xx);
  * ci_cd (CI installs locked deps + runs tests + builds the container; CD
    deploys the built image);
  * deployment_target (a web / CLI-API applicability branch: when the objective
    is web, the container must keep the built web assets same-origin with the
    API; otherwise no web-asset step is required);
  * consistency + reproducibility + keys;
  * decisions (each DD-n with id + title + rationale; decisions reference
    concrete DEV-n components).
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
assert "def devops_is_executable(" in _src
assert "def devops_executable_issues(" in _src
assert '_DEVOPS_MAX_TOKENS' in _src
assert 'if name == "devops.md":' in _src

_VALID_DEVOPS = {
    "components": [
        {"id": "DEV-1", "name": "container", "path": "Dockerfile",
         "responsibility": "pinned base image + locked deps",
         "interfaces": {"in": "requirements.lock", "out": "runnable image"},
         "data_flow": "lockfile -> pip install -> image"},
        {"id": "DEV-2", "name": "dependencies", "path": "requirements.lock",
         "responsibility": "pinned dependency versions",
         "interfaces": {"in": "requirements.in", "out": "locked versions"},
         "data_flow": "tested versions -> lock -> fresh install"},
    ],
    "build": [
        "pinned base image (python:3.11-slim, version-pinned)",
        "install from requirements.lock with --no-cache-dir",
    ],
    "health_check": ["GET /health returns 2xx with health state"],
    "ci_cd": [
        "CI installs the locked deps and runs the full test suite",
        "CD deploys the built image on the declared target",
    ],
    "consistency": [
        "Dockerfile, lockfile, CI and deploy config agree on the same "
        "Python/runtime version and the same port",
    ],
    "reproducibility": [
        "fresh clone + docker build uses only the lockfile",
        "fresh install, container build, container start, and /health probe "
        "all pass from a clean tree",
    ],
    "keys": ["DEVOPS.md", "Dockerfile", "requirements.lock"],
    "decisions": [
        {"id": "DD-1", "title": "Reproducible, pinned container",
         "rationale": "pinned base + lockfile so the same artifact rebuilds"},
        {"id": "DD-2", "title": "Health-checked deployment",
         "rationale": "orchestration probes /health for readiness"},
    ],
}


def _devops_prompt(objective_web: bool) -> str:
    objective = "a professional dark-themed marketing landing page" \
        if objective_web else "a CLI tool that prints JSON"
    return demo_llm.devops_prompt(
        "Deploy Blueprint",
        objective,
        plan=json.dumps({"deploy": ["DEVOPS.md", "Dockerfile"]}),
    )


def test_devops_prompt_returns_executable_json_blueprint():
    text = _devops_prompt(objective_web=True)
    obj = json.loads(text)
    assert len(obj["components"]) >= 7
    assert obj["build"]
    assert obj["health_check"]
    assert obj["ci_cd"]
    assert obj["deployment_target"]
    assert obj["consistency"]
    assert obj["reproducibility"]
    assert obj["keys"]
    assert any(d["id"].startswith("DD-") for d in obj["decisions"])


def test_devops_is_executable_accepts_deterministic_blueprint():
    text = _devops_prompt(objective_web=True)
    assert demo_llm.devops_is_executable(text) is True


def test_devops_executable_flags_missing_sections():
    broken = dict(_VALID_DEVOPS)
    broken.pop("build")
    issues = demo_llm.devops_executable_issues(json.dumps(broken))
    assert any("build" in i.lower() for i in issues)

    broken2 = dict(_VALID_DEVOPS)
    broken2.pop("health_check")
    issues2 = demo_llm.devops_executable_issues(json.dumps(broken2))
    assert any("health" in i.lower() for i in issues2)


def test_devops_executable_flags_undefined_references():
    broken = dict(_VALID_DEVOPS)
    broken["components"] = list(broken["components"]) + [
        {"id": "DEV-9", "name": "ghost", "path": "deploy/ghost.yml",
         "responsibility": "DEPENDS_ON DEV-99 and DD-9",
         "interfaces": {"in": "x", "out": "y"},
         "data_flow": "DEV-99 -> DD-9"},
    ]
    issues = demo_llm.devops_executable_issues(json.dumps(broken))
    assert any("DEV-99" in i for i in issues)
    assert any("DD-9" in i for i in issues)


def test_devops_prompt_deployment_branch_deterministic():
    web = _devops_prompt(objective_web=True)
    assert "same-origin web app + API" in web
    assert "built web assets" in web

    cli = _devops_prompt(objective_web=False)
    assert "no web-asset step" in cli
    assert "same-origin web app + API" not in cli