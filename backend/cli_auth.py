"""Shared helpers for locating and validating CLI subscription credentials.

A subscription "account" is an isolated config directory that holds the OAuth
credentials written by a provider's CLI — `claude auth login`, `codex login`,
`grok login`, or `agy`'s Google sign-in. These helpers answer "does this
directory contain usable credentials?"
and are shared by the web sign-in flow (ui.server) and the account pool
(backend.account_pool) so the two never disagree on what "authenticated" means.
"""

from __future__ import annotations

import json
import os

# Per-account config roots. Each connected account gets its own subdirectory
# here so multiple subscriptions never share credentials.
CLAUDE_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".claude-ctf-agents")
CODEX_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".codex-ctf-agents")
GROK_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".grok-ctf-agents")
ANTIGRAVITY_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".agy-ctf-agents")

# The Antigravity CLI (`agy`) keeps its Google session in the OS keyring, with a
# file fallback when no Secret Service is reachable — either way there is no
# documented credentials path to look for. What it *does* offer is a reliable
# oracle: `agy models` exits non-zero with "Please sign in ..." until the account
# is signed in. The connect flow runs that once sign-in finishes and drops this
# marker (holding the model list it reported), so routine authentication checks
# stay a cheap file stat like every other provider.
ANTIGRAVITY_MARKER = ".ctf-agent-authenticated.json"


def claude_is_authenticated(config_dir: str) -> bool:
    """True if a non-empty Claude credentials file exists in *config_dir*."""
    if not config_dir:
        return False
    for name in (".credentials.json", "credentials.json", ".auth.json", "auth.json"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    if json.load(f):
                        return True
            except Exception:
                pass
    return False


def codex_is_authenticated(config_dir: str) -> bool:
    """True if Codex credentials exist in *config_dir* (used as HOME)."""
    if not config_dir:
        return False
    candidates = [
        # Current Codex CLI (>=0.1x) stores creds under HOME/.codex/auth.json.
        os.path.join(config_dir, ".codex", "auth.json"),
        os.path.join(config_dir, ".config", "openai", "credentials.json"),
        os.path.join(config_dir, ".config", "openai", "auth.json"),
        os.path.join(config_dir, ".openai", "credentials.json"),
        os.path.join(config_dir, ".openai", "auth.json"),
        os.path.join(config_dir, ".config", "openai"),  # non-empty dir counts
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            try:
                if any(True for _ in os.scandir(path)):
                    return True
            except Exception:
                pass
        else:
            try:
                with open(path) as f:
                    if json.load(f):
                        return True
            except Exception:
                pass
    return False


def grok_is_authenticated(config_dir: str) -> bool:
    """True if Grok credentials exist in *config_dir* (used as GROK_HOME)."""
    if not config_dir:
        return False
    for name in ("auth.json", "credentials.json", ".auth.json", "session.json"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    if json.load(f):
                        return True
            except Exception:
                # a non-empty non-JSON creds file still counts
                try:
                    if os.path.getsize(path) > 2:
                        return True
                except OSError:
                    pass
    return False


def antigravity_is_authenticated(config_dir: str) -> bool:
    """True if a completed `agy` sign-in was recorded in *config_dir*."""
    if not config_dir:
        return False
    path = os.path.join(config_dir, ANTIGRAVITY_MARKER)
    try:
        with open(path) as f:
            return bool(json.load(f))
    except Exception:
        return False


def is_authenticated(provider: str, config_dir: str) -> bool:
    if provider == "claude":
        return claude_is_authenticated(config_dir)
    if provider == "codex":
        return codex_is_authenticated(config_dir)
    if provider == "grok":
        return grok_is_authenticated(config_dir)
    if provider == "antigravity":
        return antigravity_is_authenticated(config_dir)
    return False
