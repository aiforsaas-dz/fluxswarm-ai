"""Safe Paddle configuration checker for FluxSwarm.

Prints only presence/format status per variable — never the values — so it can
be run in a shell/CI or by an operator and pasted anywhere. Exit code = number
of problems found (0 == ready).

Usage:
    python validate_paddle.py [--json]
"""
from __future__ import annotations

import json
import os
import sys

from pathlib import Path


def _load_local_env() -> None:
    """Best-effort: load backend/.env next to this script, if present, so the
    checker reflects what a restart of the supervisor would actually use."""
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        k, v = t.split("=", 1)
        k, v = k.strip(), v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def main() -> int:
    _load_local_env()
    checks: list[dict] = []
    problems = 0

    provider = _env("FLUXSWARM_PAYMENT_PROVIDER").lower()
    payments = _env("FLUXSWARM_PAYMENTS").lower() in ("1", "true", "yes")
    checks.append({
        "var": "FLUXSWARM_PAYMENT_PROVIDER",
        "ok": provider in ("paddle", "paddle_sandbox"),
        "detail": provider or "(unset -> stub/402)",
    })

    api_key = _env("PADDLE_API_KEY")
    checks.append({
        "var": "PADDLE_API_KEY",
        "ok": bool(api_key) and len(api_key) >= 8,
        "detail": "set" if api_key else "MISSING",
    })

    base = _env("PADDLE_API_BASE")
    env_name = "sandbox" if "sandbox" in base else ("live" if base else "unset")
    ok_base = bool(base) and ("sandbox-api.paddle.com" in base or "api.paddle.com" in base)
    checks.append({
        "var": "PADDLE_API_BASE",
        "ok": ok_base,
        "detail": env_name,
    })

    if api_key and base and "sandbox" not in base and "api.paddle.com" in base:
        checks.append({
            "var": "live-credentials-present",
            "ok": True,
            "detail": "LIVE api base detected — promotions use these keys",
        })

    secret = _env("PADDLE_WEBHOOK_SECRET")
    mock = _env("FLUXSWARM_PADDLE_MOCK").lower() in ("1", "true", "yes")
    checks.append({
        "var": "PADDLE_WEBHOOK_SECRET",
        "ok": bool(secret),
        "detail": "set" if secret else "MISSING (use the value shown when you "
                                       "created the webhook)",
    })

    ctoken = _env("PADDLE_CLIENT_TOKEN")
    checks.append({
        "var": "PADDLE_CLIENT_TOKEN",
        "ok": bool(ctoken),
        "detail": "set" if ctoken else "MISSING (Dashboard > Developer Tools > "
                                       "Authentication > Client-side tokens)",
    })

    for plan in ("STARTER", "PRO", "SCALE"):
        pid = _env(f"PADDLE_PRICE_{plan}")
        ok = pid.startswith("pri_")
        checks.append({
            "var": f"PADDLE_PRICE_{plan}",
            "ok": ok,
            "detail": "pri_*" if ok else ("MISSING" if not pid else "BAD (must start with pri_)"),
        })

    # Top-up is optional at launch but enables the $9/10 refill pack in checkout.
    pid = _env("PADDLE_PRICE_TOPUP")
    checks.append({
        "var": "PADDLE_PRICE_TOPUP",
        "ok": bool(pid) and pid.startswith("pri_"),
        # True = non-blocking: a missing top-up must NOT keep the billing gate
        # closed (it is optional; three price ids are enough to sell).
        "optional": True,
        "detail": "pri_*" if (bool(pid) and pid.startswith("pri_")) else (
            "MISSING (optional — skip or configure the $9 refill pack)"),
    })

    public_url = _env("FLUXSWARM_PUBLIC_BASE_URL")
    ok_pub = bool(public_url) and public_url.startswith(("http://", "https://"))
    checks.append({
        "var": "FLUXSWARM_PUBLIC_BASE_URL",
        "ok": ok_pub,
        "detail": public_url if ok_pub else ("unset -> http://127.0.0.1:8787 (local)",
                                             "MISSING/BAD")[bool(public_url)],
    })

    # Guard-rail cross-checks (mirrors payments.py:_use_mock)
    if mock and api_key and (not base or base == "https://api.paddle.com"):
        checks.append({
            "var": "MOCK + LIVE conflict",
            "ok": False,
            "detail": "FLUXSWARM_PADDLE_MOCK=1 is set while LIVE Paddle credentials "
                      "are active — mock is forbidden; unset the mock flag.",
        })
    elif mock:
        checks.append({
            "var": "FLUXSWARM_PADDLE_MOCK",
            "ok": True,
            "detail": "local sandbox mode ON (checkout + webhook fully local)",
        })

    if payments and not all(c["ok"] for c in checks
                            if c["var"].startswith(("PADDLE", "FLUXSWARM_PAYMENT"))
                            and not c.get("optional")):
        checks.append({
            "var": "FLUXSWARM_PAYMENTS=1",
            "ok": False,
            "detail": "billing gate would be OPEN while Paddle is not fully configured — "
                      "keep it 0 until the rest is green.",
        })

    for c in checks:
        if not c["ok"] and not c.get("optional"):
            problems += 1

    if "--json" in sys.argv[1:]:
        print(json.dumps({"ready": problems == 0, "problems": problems, "checks": checks}))
    else:
        for c in checks:
            mark = "OK " if c["ok"] else "FAIL"
            print(f"[{mark}] {c['var']}: {c['detail']}")
        print(f"\n{'READY — safe to flip FLUXSWARM_PAYMENTS=1' if problems == 0 else f'{problems} problem(s) to fix'}")

    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())