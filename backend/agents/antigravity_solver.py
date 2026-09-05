"""Antigravity (Google) subscription solver — drives the `agy` CLI headlessly.

A Google-account Antigravity subscription can't be reached over the Gemini API
(the OAuth session is not an API key), so it is only usable through the
Antigravity CLI. We run that CLI **inside the Docker sandbox**, the same way the
Grok solver does, so its autonomous tool calls execute against the challenge in
isolation with the CTF toolchain.

Credential handling differs from Grok in one way: `agy` treats its state
directory as writable (logs, caches, an updater lock, a conversations DB), so
the leased account directory is mounted read-only and copied into a writable
HOME inside the container. `agy` reads HOME *and* the XDG dirs, so all four are
pinned — otherwise one account's session leaks into another's.

Command shape (verified against `agy --help`, v1.1.26):
``agy --print "<prompt>" --dangerously-skip-permissions --output-format text
--model <id>`` runs one prompt to completion, non-interactive, auto-approving
tool calls. The flag is read from the model's final output (``FLAG: <flag>``)
and submitted through the swarm's deduped ``submit_fn``.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import TYPE_CHECKING

from backend.models import model_id_from_spec
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverResult,
)

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)

_FLAG_LINE = re.compile(r"FLAG:\s*([^\s]+)")
_FLAG_FMT = re.compile(r"[A-Za-z0-9_]{2,}\{[^}\n]{1,200}\}")
_QUOTA = re.compile(
    r"429|rate.?limit|quota|too many requests|usage limit|out of credits|insufficient|resource_exhausted",
    re.I,
)
_NOT_SIGNED_IN = re.compile(r"please sign in|not signed in|unauthenticated", re.I)

# Read-only mount of the leased account dir, and the writable HOME copied from it.
_CREDS = "/agy-creds"
_HOME = "/root/agy-home"


class AntigravitySolver:
    def __init__(
        self,
        *,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd,
        cost_tracker,
        settings,
        cancel_event: asyncio.Event,
        no_submit: bool = False,
        submit_fn=None,
        message_bus=None,
        notify_coordinator=None,
        config_dir: str | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event
        self.no_submit = no_submit
        self.submit_fn = submit_fn
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator
        self.config_dir = config_dir
        self.sandbox: DockerSandbox | None = None
        self._insights: list[str] = []
        self._step = 0

    async def start(self) -> None:
        extra = [f"{self.config_dir}:{_CREDS}:ro"] if self.config_dir else []
        self.sandbox = DockerSandbox(
            image=getattr(self.settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=self.challenge_dir,
            memory_limit=getattr(self.settings, "container_memory_limit", "16g"),
            extra_binds=extra,
        )
        await self.sandbox.start()
        await self._ensure_agy()

    async def _ensure_agy(self) -> None:
        """Install `agy` in the container on demand, and stage a writable HOME."""
        if not self.sandbox:
            return
        try:
            check = await self.sandbox.exec(
                "command -v agy >/dev/null 2>&1 && echo yes || echo no", timeout_s=15
            )
            if "yes" not in (check.stdout or ""):
                logger.info("Installing agy CLI into sandbox for %s", self.model_spec)
                await self.sandbox.exec(
                    "curl -fsSL https://antigravity.google/cli/install.sh "
                    "| bash -s -- -d /usr/local/bin || true",
                    timeout_s=300,
                )
            if self.config_dir:
                # `agy` writes logs/caches/locks under its state dirs, so the
                # read-only credential mount can't be used as HOME directly.
                await self.sandbox.exec(
                    f"rm -rf {_HOME} && mkdir -p {_HOME} && cp -a {_CREDS}/. {_HOME}/ 2>/dev/null || true",
                    timeout_s=120,
                )
        except Exception as e:
            logger.warning("agy ensure failed: %s", e)

    def _build_task(self) -> str:
        distfiles = list_distfiles(self.challenge_dir)
        arch = "amd64"
        prompt = build_prompt(self.meta, distfiles, arch, has_named_tools=False)
        prompt += (
            "\n\n## How to report the flag\n"
            "You are running autonomously in a sandbox with the CTF toolchain. Use the shell "
            "freely. When you have the real flag, print it on its own line EXACTLY as:\n"
            "FLAG: <flag>\n"
            "Do not print a placeholder; only print FLAG: once you have verified the real value."
        )
        if self._insights:
            prompt += "\n\n## Notes from teammates / previous attempts\n" + "\n".join(
                self._insights[-5:]
            )
        return prompt

    def _command(self, instruction: str) -> str:
        model_flag = f"--model {shlex.quote(self.model_id)}" if self.model_id else ""
        # HOME plus every XDG dir, or `agy` falls back to the container's real
        # home and loses the leased account's session.
        return (
            "export PATH=/usr/local/bin:/root/.local/bin:$PATH; "
            f"cd /challenge && HOME={_HOME} XDG_CONFIG_HOME={_HOME}/.config "
            f"XDG_DATA_HOME={_HOME}/.local/share XDG_CACHE_HOME={_HOME}/.cache "
            "GEMINI_API_KEY= GOOGLE_API_KEY= "
            f"agy --print {shlex.quote(instruction)} --dangerously-skip-permissions "
            f"--output-format text {model_flag}"
        )

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self.sandbox:
            return SolverResult(
                flag=None, status=ERROR, findings_summary="agy sandbox not started"
            )
        self._step += 1
        task = self._build_task()

        # Stage the task in the workspace rather than passing it as an argument.
        try:
            await self.sandbox.write_file("/challenge/workspace/task.md", task)
        except Exception as e:
            return SolverResult(
                flag=None, status=ERROR, findings_summary=f"agy: could not stage task ({e})"
            )

        instruction = (
            "Read /challenge/workspace/task.md and solve the CTF challenge it describes. "
            "Work in /challenge. When you have the real flag, print it on its own line as: FLAG: <flag>"
        )

        if self.cancel_event.is_set():
            return SolverResult(
                flag=None, status=GAVE_UP, findings_summary="cancelled before start"
            )

        result = await self.sandbox.exec(self._command(instruction), timeout_s=900)
        out = (result.stdout or "") + "\n" + (result.stderr or "")

        if "agy: command not found" in out:
            return SolverResult(
                flag=None,
                status=ERROR,
                findings_summary="Antigravity CLI (agy) is not available in the sandbox image",
            )
        if _NOT_SIGNED_IN.search(out):
            return SolverResult(
                flag=None,
                status=ERROR,
                findings_summary="agy: the leased Google account is not signed in — reconnect it on the Accounts page",
            )
        if _QUOTA.search(out):
            return SolverResult(
                flag=None, status=QUOTA_ERROR, findings_summary="Antigravity usage/rate limit"
            )

        # Explicit FLAG: lines first, then anything shaped like a flag.
        candidates: list[str] = []
        for m in _FLAG_LINE.finditer(out):
            candidates.append(m.group(1).strip().strip("`'\""))
        for m in _FLAG_FMT.finditer(out):
            v = m.group(0)
            if (
                v not in candidates
                and "placeholder" not in v.lower()
                and v.lower() not in ("ctf{flag}", "flag{flag}")
            ):
                candidates.append(v)

        findings = _summarize(out)
        if self.message_bus:
            try:
                await self.message_bus.post(self.model_spec, findings[:1000])
            except Exception:
                pass

        for flag in candidates[:5]:
            if self.no_submit or not self.submit_fn:
                return SolverResult(
                    flag=flag, status=FLAG_FOUND, findings_summary=findings, step_count=self._step
                )
            try:
                display, confirmed = await self.submit_fn(flag)
            except Exception as e:
                logger.warning("agy submit_fn error: %s", e)
                continue
            if confirmed:
                return SolverResult(
                    flag=flag, status=FLAG_FOUND, findings_summary=findings, step_count=self._step
                )

        return SolverResult(
            flag=None, status=GAVE_UP, findings_summary=findings, step_count=self._step
        )

    def bump(self, insights: str) -> None:
        if insights:
            self._insights.append(insights)

    async def stop(self) -> None:
        if self.sandbox:
            try:
                await self.sandbox.stop()
            except Exception:
                pass
            self.sandbox = None


def _summarize(output: str, limit: int = 1200) -> str:
    text = output.strip()
    if len(text) <= limit:
        return text or "agy produced no output"
    return text[-limit:]
