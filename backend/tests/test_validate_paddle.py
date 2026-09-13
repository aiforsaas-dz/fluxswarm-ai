"""Unit tests for validate_paddle.py (configuration checker CLI).

These cover the optional-topup rule introduced with the sandbox go-live:
a missing PADDLE_PRICE_TOPUP must NOT keep the billing gate closed when the
three required price ids are present.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
_VP = BACKEND / "validate_paddle.py"

_valid_demo_env = {
    "FLUXSWARM_PAYMENT_PROVIDER": "paddle",
    "FLUXSWARM_PAYMENTS": "1",
    "PADDLE_API_KEY": "pdl_sdbx_testkey",
    "PADDLE_API_BASE": "https://sandbox-api.paddle.com",
    "PADDLE_WEBHOOK_SECRET": "ntfset_test",
    "PADDLE_CLIENT_TOKEN": "test_ctoken",
    "PADDLE_PRICE_STARTER": "pri_starter",
    "PADDLE_PRICE_PRO": "pri_pro",
    "PADDLE_PRICE_SCALE": "pri_scale",
    "FLUXSWARM_PUBLIC_BASE_URL": "https://example.com",
}


@pytest.fixture
def vp(monkeypatch):
    spec = importlib.util.spec_from_file_location("validate_paddle", _VP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_paddle"] = mod
    spec.loader.exec_module(mod)

    def _run(**overrides):
        env = dict(_valid_demo_env)
        env.update(overrides)
        monkeypatch.setattr(os, "environ", env)
        return mod.main()

    yield _run


def test_ready_without_topup(vp):
    assert vp() == 0


def test_ready_with_topup(vp):
    assert vp(PADDLE_PRICE_TOPUP="pri_topup") == 0


def test_missing_price_closes_gate(vp):
    assert vp(PADDLE_PRICE_PRO="") > 0


def test_missing_client_token_reported(vp):
    assert vp(PADDLE_CLIENT_TOKEN="") > 0


def test_live_base_without_mock_is_fine(vp):
    assert vp(PADDLE_API_BASE="https://api.paddle.com") == 0


def test_mock_with_live_base_is_blocked(vp):
    assert vp(PADDLE_API_BASE="https://api.paddle.com",
              FLUXSWARM_PADDLE_MOCK="1") > 0