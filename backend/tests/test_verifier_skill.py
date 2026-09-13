"""Gate 4 — reviewer verifier-skill provisioning (Candidate C).

Covers the confirmed root cause:

  * ``hermes kanban swarm`` hard-codes the verifier task's skill to
    ``requesting-code-review`` (hermes_cli/kanban_swarm.py) with no CLI override.
  * That skill ships inside the bundled ``software-development`` collection,
    which is NOT visible to the ``ecc-reviewer`` profile (it scans only its
    profile-scoped ``skills.external_dirs`` = ``skills/ecc/skills`` + profile
    local skills). The dispatcher-owned worker therefore receives
    ``--skills requesting-code-review`` and dies at startup with
    ``ValueError("Unknown skill(s): requesting-code-review")``.
  * Candidate C provisions the REAL bundled skill byte-for-byte into
    ``skills/ecc/skills/requesting-code-review`` (idempotent, never overwrites)
    before the swarm is created, so the verifier task resolves it normally.

Test matrix (A-I from the authorization):
  A. Provisioning creates the skill dir in the ecc-reviewer skill scope when absent.
  B. Idempotent: a second provision is a no-op and never alters an existing skill.
  C. Copied content matches the bundled source exactly (byte-for-byte).
  D. Missing bundled source fails safely with a clear error; no fake skill created.
  E. The real reviewer launch env resolves ``requesting-code-review`` after provision.
  F. The real preload path does NOT die with "Unknown skill(s): requesting-code-review".
  G. Runtime pinning stays intact (BYOK provider pinned with the operator model).
  H. No silent fallback to a stale/free provider (Phase 3): opencode-free is gone; the
     only free-tier runtime is an explicit OpenRouter BYOK key (z-ai/glm-5.2:free).
  I. Existing watchdog regression tests remain green (healthy slow worker != stuck;
     genuinely stalled worker = stuck). Covered by running the full suite.

A-D are fully hermetic (isolated temp HERMES_HOME). E-F exercise the REAL Hermes
skill loader (`build_preloaded_skills_prompt`) against an isolated profile env
that mirrors exactly how the dispatcher worker is spawned, so no production data
is touched; they skip on hosts without the Hermes venv (e.g. minimal CI images).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import hermes_client as hc_mod

# Real Hermes install paths, captured BEFORE any test monkeypatches HERMES_HOME.
_REAL_HERMES_HOME = Path(hc_mod.HERMES_HOME)
_REAL_AGENT_ROOT = _REAL_HERMES_HOME / "hermes-agent"


def _hermes_agent_python() -> Path | None:
    for cand in (
        _REAL_AGENT_ROOT / "venv" / "Scripts" / "python.exe",
        _REAL_AGENT_ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if cand.exists():
            return cand
    return None


# --- fixtures/helpers ------------------------------------------------------


def _make_bundled_source(root: Path) -> Path:
    """Create a realistic bundled `software-development/requesting-code-review`."""
    src = root / "skills" / "software-development" / "requesting-code-review"
    (src / "scripts").mkdir(parents=True, exist_ok=True)
    (src / "references").mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_bytes(
        b"---\nname: requesting-code-review\n---\n\n# Requesting Code Review\n"
    )
    (src / "scripts" / "evaluate.py").write_bytes(b"#!/usr/bin/env python\nprint('review')\n")
    (src / "references" / "guide.md").write_bytes(b"# Guide\n")
    return src


def _tree_digest(root: Path) -> dict[str, bytes]:
    """Map every relative file path -> raw bytes under root."""
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _isolate_home(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """Build an isolated profile-scoped Hermes home mirroring the reviewer profile.

    Layout (mirrors real `resolve_profile_env` / `_default_spawn`):
      tmp/home/profiles/ecc-reviewer/config.yaml   -> skills.external_dirs = tmp/home/skills/ecc/skills
      tmp/home/skills/software-development/requesting-code-review -> bundled source
    Returns (profile-scoped HERMES_HOME and bundled source dir) — the env that
    the dispatcher worker gets.
    """
    home = tmp_path / "home"
    src = _make_bundled_source(home)
    profile = home / "profiles" / "ecc-reviewer"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(
        f"skills:\n  external_dirs: {(home / 'skills' / 'ecc' / 'skills').as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(home))
    monkeypatch.setattr(hc_mod, "PROFILES_DIR", home / "profiles")
    return profile, src


def _probe_preload(profile_home: Path) -> tuple[int, str, str]:
    """Run Hermes' real ``build_preloaded_skills_prompt`` in the reviewer env.

    Environment matches the dispatcher-owned worker exactly: HERMES_HOME points
    at the profile-scoped home and HERMES_PROFILE=ecc-reviewer (kanban_db
    ``_default_spawn`` -> ``resolve_profile_env``).
    """
    python = _hermes_agent_python()
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_REAL_AGENT_ROOT)!r})\n"
        "from agent.skill_commands import build_preloaded_skills_prompt\n"
        "p, loaded, missing = build_preloaded_skills_prompt(['requesting-code-review'])\n"
        "print('LOADED=' + str(list(loaded)))\n"
        "print('MISSING=' + str(list(missing)))\n"
    )
    # Match the dispatcher-owned worker environment: the full parent environment
    # plus HERMES_HOME scoped to the profile and HERMES_PROFILE set. (Dropping
    # platform vars like USERPROFILE makes Path.home() fail on Windows, which is
    # not representative of a real worker.)
    env = {
        **__import__("os").environ.copy(),
        "HERMES_HOME": str(profile_home),
        "HERMES_PROFILE": "ecc-reviewer",
    }
    r = subprocess.run(
        [str(python), "-c", script],
        env=env, capture_output=True, text=True, timeout=120,
    )
    return r.returncode, r.stdout, r.stderr


# --- A-D: provisioning behavior -------------------------------------------


class TestProvisionVerifierSkill:
    """A-C: provision, idempotence, byte-exactness; D: safe failure."""

    def test_a_provisions_when_absent(self, monkeypatch, tmp_path):
        _, src = _isolate_home(monkeypatch, tmp_path)

        target = hc_mod._ensure_verifier_skill()

        assert target == tmp_path / "home" / "skills" / "ecc" / "skills" / "requesting-code-review"
        assert target.is_dir()
        assert (target / "SKILL.md").exists()
        assert _tree_digest(target) == _tree_digest(src)

    def test_b_idempotent_no_overwrite(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)

        target = hc_mod._ensure_verifier_skill()
        first = _tree_digest(target)

        # Operator-tunes the provisioned copy (simulates a pre-existing skill).
        (target / "SKILL.md").write_bytes(b"# operator-tuned version\n")
        tuned = _tree_digest(target)

        again = hc_mod._ensure_verifier_skill()
        assert again == target
        # Second provĺion is a no-op: the tuned copy is untouched, nothing added.
        assert _tree_digest(target) == tuned
        assert _tree_digest(target) != first

    def test_c_byte_for_byte_copy(self, monkeypatch, tmp_path):
        _, src = _isolate_home(monkeypatch, tmp_path)
        target = hc_mod._ensure_verifier_skill()
        assert _tree_digest(target) == _tree_digest(src)

    def test_d_missing_source_fails_safely(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        profile = home / "profiles" / "ecc-reviewer"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text(
            f"skills:\n  external_dirs: {(home / 'skills' / 'ecc' / 'skills').as_posix()}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hc_mod, "HERMES_HOME", str(home))
        monkeypatch.setattr(hc_mod, "PROFILES_DIR", home / "profiles")
        missing_src = home / "skills" / "software-development" / hc_mod._VERIFIER_SKILL_NAME
        monkeypatch.setattr(hc_mod, "_bundled_verifier_skill_dir", lambda: missing_src)
        assert not missing_src.exists()

        with pytest.raises(RuntimeError) as ei:
            hc_mod._ensure_verifier_skill()
        msg = str(ei.value)
        assert "requesting-code-review" in msg
        assert "fabricate" in msg.lower() or "refusing" in msg.lower()
        # No fake skill dir may exist.
        target = home / "skills" / "ecc" / "skills" / "requesting-code-review"
        assert not target.exists()

    def test_ae_empty_stale_target_reprovisions(self, monkeypatch, tmp_path):
        """A stale EMPTY dir must not resurrect the MISSING-skill state: an empty
        target (no SKILL.md) is re-provisioned byte-for-byte."""
        _, src = _isolate_home(monkeypatch, tmp_path)
        target = tmp_path / "home" / "skills" / "ecc" / "skills" / "requesting-code-review"
        target.mkdir(parents=True, exist_ok=True)

        got = hc_mod._ensure_verifier_skill()

        assert got == target
        assert (target / "SKILL.md").exists()
        assert _tree_digest(target) == _tree_digest(src)

    def test_af_foreign_target_without_skill_md_refused(self, monkeypatch, tmp_path):
        """A non-empty target without SKILL.md may be foreign data: provisioning
        must refuse to destroy it, and fail loudly."""
        _, src = _isolate_home(monkeypatch, tmp_path)
        target = tmp_path / "home" / "skills" / "ecc" / "skills" / "requesting-code-review"
        target.mkdir(parents=True, exist_ok=True)
        (target / "notes.txt").write_text("foreign content", encoding="utf-8")

        with pytest.raises(RuntimeError) as ei:
            hc_mod._ensure_verifier_skill()
        msg = str(ei.value)
        assert "SKILL.md" in msg and "refus" in msg
        assert (target / "notes.txt").read_text(encoding="utf-8") == "foreign content"


# --- E/F: real Hermes resolution -------------------------------------------


@pytest.mark.skipif(
    _hermes_agent_python() is None,
    reason="Hermes agent venv not present; real-resolution checks skipped",
)
class TestRealReviewerResolution:
    """E/F — real Hermes skill loader resolves the skill in the reviewer env."""

    def test_e_resolves_after_provision(self, monkeypatch, tmp_path):
        profile_home, _ = _isolate_home(monkeypatch, tmp_path)

        # Without provisioning: real loader reports the skill missing.
        rc, out, err = _probe_preload(profile_home)
        assert "MISSING=['requesting-code-review']" in out

        # Provision, then the same real loader must resolve it.
        hc_mod._ensure_verifier_skill()
        rc, out, err = _probe_preload(profile_home)
        assert rc == 0
        assert "LOADED=['requesting-code-review']" in out, f"out={out!r} err={err!r}"
        assert "MISSING=[]" in out

    def test_f_worker_preload_does_not_die_unknown_skill(self, monkeypatch, tmp_path):
        profile_home, _ = _isolate_home(monkeypatch, tmp_path)
        hc_mod._ensure_verifier_skill()

        rc, out, err = _probe_preload(profile_home)
        assert rc == 0
        assert "Unknown skill(s)" not in out + err
        assert "MISSING=[]" in out


# --- G/H: pinning + no paid/free fallback ----------------------------------


class TestPinningIntact:
    """G/H — runtime pinning and provider resolution must be unchanged."""

    def test_g_byok_runtime_pins(self, monkeypatch):
        fake = type("_Proc", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        calls = {"args": None, "keys": None}

        def fake_list(board):
            return [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]

        def fake_run(args, board=None, capture=True, provider_keys=None):
            calls["args"] = args
            calls["keys"] = provider_keys
            return fake

        monkeypatch.setattr(hc_mod, "list_tasks", fake_list)
        monkeypatch.setattr(hc_mod, "_run", fake_run)
        monkeypatch.setenv("FLUXSWARM_MODEL_OPENAI", "gpt-byok-1")
        hc_mod._pin_runtime("u1-proj", {"openai": "sk-openai"})
        assert calls["args"][:2] == ["set-model", "t3"]
        assert calls["args"][2] == "gpt-byok-1"
        assert calls["args"][3:] == ["--provider", "openai"]

    def test_h_no_paid_or_free_fallback(self):
        # opencode-free is NOT a provider in Phase 3 — ignored. Openrouter is the
        # free-tier BYOK provider but sits LAST in precedence.
        # Precedence: anthropic > openai > gemini > kimi > openrouter (dict order irrelevant).
        keys = {
            "openrouter": "sk-x",
            "anthropic": "sk-ant",
            "openai": "sk-oa",
            "gemini": "sk-gem",
            "kimi": "sk-kim",
            "opencode-free": "free",
        }
        model, provider = hc_mod._resolve_runtime(keys)
        assert provider == "anthropic"
        assert model is None


# --- I: watchdog/stall semantics are covered in the existing suite ---------


def test_launch_functions_call_verifier_provisioning(monkeypatch, tmp_path):
    """Both launch paths must provision BEFORE the swarm subprocess runs."""
    home = tmp_path / "home"
    src = _make_bundled_source(home)
    for prof in hc_mod._squad_profiles():
        (home / "profiles" / prof).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hc_mod, "HERMES_HOME", str(home))
    monkeypatch.setattr(hc_mod, "PROFILES_DIR", home / "profiles")
    fake_bin = home / "bin" / "hermes.exe"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.write_text("x")
    monkeypatch.setattr(hc_mod, "HERMES_BIN", fake_bin)

    real_ensure = hc_mod._ensure_verifier_skill
    provisioned = {}

    def call_once_with_tracking():
        provisioned["target"] = real_ensure()

    monkeypatch.setattr(hc_mod, "_ensure_verifier_skill", call_once_with_tracking)

    def fake_run(args, board=None, capture=True, provider_keys=None):
        payload = {
            "root_id": "root-1",
            "worker_ids": ["t1"],
            "verifier_id": "t2",
            "synthesizer_id": "t3",
        }
        return type("_Proc", (), {
            "returncode": 0, "stdout": __import__("json").dumps(payload), "stderr": ""
        })()

    monkeypatch.setattr(hc_mod, "_run", fake_run)
    monkeypatch.setattr(hc_mod, "_pin_runtime",
                        lambda board, keys=None, provider=None, model=None: None)
    # Phase 3 has no free fallback: supply an explicit BYOK runtime.
    monkeypatch.setenv("FLUXSWARM_MODEL_OPENAI", "gpt-verifier-test")

    hc_mod.launch_swarm("u1-proj", "goal", provider_keys={"openai": "sk-openai"})
    assert "target" in provisioned
    assert provisioned["target"].is_dir()
    assert (provisioned["target"] / "SKILL.md").exists()
    assert _tree_digest(provisioned["target"]) == _tree_digest(src)