"""Shared environment and diagnostics for the Claude Agent SDK subprocesses.

Both the coordinator and the solver drive the `claude` CLI through the SDK with
``permission_mode="bypassPermissions"``. Two things about that need care, and
both bit us in production, so they live here rather than being repeated.
"""

from __future__ import annotations

import collections
import logging

logger = logging.getLogger(__name__)

# How much CLI stderr to keep for an error message.
_STDERR_LINES = 40


def sdk_env(config_dir: str = "") -> dict[str, str]:
    """Environment for a Claude Agent SDK subprocess.

    ``IS_SANDBOX=1`` is required because the service runs as root. The CLI
    refuses ``--dangerously-skip-permissions`` (what ``bypassPermissions``
    becomes) under uid 0 — "cannot be used with root/sudo privileges for
    security reasons" — and exits 1 before doing anything, which failed every
    run the moment it started.

    Lifting that guard is safe *here* because the permission prompt is not what
    isolates these agents; their PreToolUse hooks are. The coordinator's hook
    denies every tool outside a fixed list of coordinator MCP calls, and the
    solver's rewrites each Bash command to ``docker exec`` into the challenge
    container while denying Read/Write/Edit/Glob/Grep outright. Neither can
    touch the host filesystem whether the prompt is bypassed or not.

    Do not reuse this for an agent without such a hook.
    """
    env = {
        # Prevent nested-session rejection when the solver runs under the
        # coordinator, which is itself a Claude Code session.
        "CLAUDECODE": "",
        "IS_SANDBOX": "1",
    }
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


class StderrCapture:
    """Collect the CLI's stderr so a failure says what actually went wrong.

    On a startup failure the SDK raises ``ProcessError: Command failed with exit
    code 1 / Error output: Check stderr output for details`` — the real reason
    only ever reached the service journal, from a different PID, so the UI
    showed an error nobody could act on.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._lines: collections.deque[str] = collections.deque(maxlen=_STDERR_LINES)

    def __call__(self, line: str) -> None:
        text = (line or "").rstrip()
        if text:
            self._lines.append(text)
            logger.debug("[%s] claude stderr: %s", self.label, text)

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def explain(self, exc: Exception) -> str:
        """Turn an SDK exception into something an operator can act on."""
        detail = self.text
        if not detail:
            return str(exc)
        hint = ""
        low = detail.lower()
        if "root/sudo" in low:
            hint = (
                " — the Claude CLI refuses to bypass permissions as root; "
                "IS_SANDBOX=1 should be set for this subprocess."
            )
        elif "not logged in" in low or "please run /login" in low:
            hint = (
                " — no Claude credentials for this run. Connect a Claude account "
                "on the Accounts page, or set ANTHROPIC_API_KEY."
            )
        return f"{exc}{hint}\nClaude CLI stderr:\n{detail}"
