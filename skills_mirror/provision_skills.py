#!/usr/bin/env python3
"""provision_skills.py — idempotent, non-destructive skill provisioning.

Mirrors the semantics of ``_ensure_verifier_skill`` in hermes_client.py:

  - Target ``SKILL.md`` exists and is non-empty → NO-OP (never overwrite a
    foreign or operator-tuned copy).
  - Target dir is empty (stale from interrupted provisioning) → remove the
    empty dir and re-copy from source.
  - Target dir exists with content but no ``SKILL.md`` → fail loudly and
    refuse to overwrite a possibly-foreign directory.
  - Target missing → copy byte-for-byte via ``shutil.copytree``.
  - Source absent → fail safely with a clear diagnostic.

Usage::

    python provision_skills.py                     # provision all internal skills
    python provision_skills.py --name swarm-ops    # provision a single skill
    python provision_skills.py --verify            # verify installed skills match source
    python provision_skills.py --list              # list skills and their status

Designed to be run offline — no network access required.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

SKILLS_DIR_NAME = "ecc"  # skills/ecc/skills — same as _ECC_SKILLS_DIR_NAME

def _resolve_hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
    return Path.home() / ".local" / "hermes"

HERMES_HOME = _resolve_hermes_home()
ECC_SKILLS_DIR = HERMES_HOME / "skills" / SKILLS_DIR_NAME / "skills"
SOURCE_INTERNAL = Path(__file__).resolve().parent / "internal"
SOURCE_VENDOR = Path(__file__).resolve().parent / "vendor"

def _provision_one(name: str, source: Path, *, dry_run: bool = False) -> str:
    """Provision a single skill directory.  Returns a status string."""
    target = ECC_SKILLS_DIR / name
    skill_source = source / name
    source_skill = skill_source / "SKILL.md"

    if not source_skill.exists():
        return f"SKIP {name}: source SKILL.md not found at {skill_source}"

    if (target / "SKILL.md").exists():
        return f"OK   {name}: target already present — no-op"

    if target.exists():
        if any(target.iterdir()):
            return (
                f"ERR  {name}: target {target} exists with content but no "
                f"SKILL.md; refusing to overwrite a possibly-foreign directory"
            )
        # empty stale dir — remove and re-copy
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        shutil.copytree(skill_source, target)
    return f"INST {name}: provisioned from {skill_source}"

def _verify_one(name: str) -> str:
    """Verify an installed skill matches its source.  Returns a status string."""
    target = ECC_SKILLS_DIR / name
    source_skill = SOURCE_INTERNAL / name / "SKILL.md"
    target_skill = target / "SKILL.md"

    if not target_skill.exists():
        return f"MISS {name}: not installed"
    if not source_skill.exists():
        return f"??   {name}: installed but no source to compare"

    # Compare byte-for-byte
    if filecmp.cmp(str(target_skill), str(source_skill), shallow=False):
        return f"OK   {name}: matches source"
    return f"MIS  {name}: installed copy differs from source"

def _list_all() -> list[str]:
    """List all skills from internal/ and their provision status."""
    results = []
    for skill_dir in sorted(SOURCE_INTERNAL.iterdir()):
        if not skill_dir.is_dir():
            continue
        name = skill_dir.name
        installed = (ECC_SKILLS_DIR / name / "SKILL.md").exists()
        status = "INSTALLED" if installed else "NOT INSTALLED"
        results.append(f"{status:15s} {name}")
    return results

def main():
    parser = argparse.ArgumentParser(description="Fluxswarm skill provisioner")
    parser.add_argument("--name", help="Provision only this skill")
    parser.add_argument("--verify", action="store_true", help="Verify installed skills match source")
    parser.add_argument("--list", action="store_true", help="List skills and their status")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    parser.add_argument("--hermes-home", help="Override HERMES_HOME path")
    args = parser.parse_args()

    if args.hermes_home:
        global HERMES_HOME, ECC_SKILLS_DIR
        HERMES_HOME = Path(args.hermes_home)
        ECC_SKILLS_DIR = HERMES_HOME / "skills" / SKILLS_DIR_NAME / "skills"

    if args.list:
        for line in _list_all():
            print(line)
        return

    if args.verify:
        names = [args.name] if args.name else [d.name for d in SOURCE_INTERNAL.iterdir() if d.is_dir()]
        ok = True
        for name in names:
            result = _verify_one(name)
            print(result)
            if result.startswith(("MISS", "MIS")):
                ok = False
        sys.exit(0 if ok else 1)

    # Provision
    names = [args.name] if args.name else [d.name for d in SOURCE_INTERNAL.iterdir() if d.is_dir()]
    ok = True
    for name in names:
        result = _provision_one(name, SOURCE_INTERNAL, dry_run=args.dry_run)
        print(result)
        if result.startswith("ERR"):
            ok = False
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
