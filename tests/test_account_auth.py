"""Regression tests for pool-account credential detection.

The web UI (`ui.server._account_authed`) and the account pool
(`backend.account_pool`) must never disagree on what "authenticated" means —
when they do, a connected account is shown as "pending" forever and its live
CLI sign-in process is never reaped.
"""

from __future__ import annotations

import json
import os

import pytest

from backend.cli_auth import is_authenticated
from backend.db_models import PooledAccount
from ui.server import _account_authed


def _acct(provider: str, config_dir: str) -> PooledAccount:
    return PooledAccount(provider=provider, label=provider, config_dir=config_dir)


@pytest.mark.parametrize(
    ("provider", "relpath"),
    [
        ("claude", ".credentials.json"),
        ("codex", ".codex/auth.json"),
        # Grok CLI writes $GROK_HOME/auth.json (see `grok` user-guide 02-authentication).
        ("grok", "auth.json"),
    ],
)
def test_account_authed_matches_pool(tmp_path, provider, relpath):
    config_dir = str(tmp_path / f"acct-{provider}")
    acct = _acct(provider, config_dir)

    # No credentials yet: both views agree the account is pending.
    assert _account_authed(acct) is False
    assert is_authenticated(provider, config_dir) is False

    path = os.path.join(config_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"access_token": "tok"}, f)

    # Credentials on disk: the UI and the pool must both see them.
    assert is_authenticated(provider, config_dir) is True
    assert _account_authed(acct) is True


def test_token_provider_uses_secret(tmp_path):
    acct = _acct("copilot", "copilot:deadbeef")
    assert _account_authed(acct) is False
    acct.secret_enc = b"sealed"
    assert _account_authed(acct) is True
