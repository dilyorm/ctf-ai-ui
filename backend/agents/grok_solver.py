"""Grok (xAI) subscription solver — drives the `grok` CLI headlessly.

Unlike the API-key providers (which the Pydantic-AI ``Solver`` runs), a Grok
*subscription* can only be used through the `grok` CLI. We run that CLI **inside
the Docker sandbox** so its autonomous tool calls execute against the challenge
in isolation with the CTF toolchain, and mount the leased ``GROK_HOME`` (the
account's isolated credentials) read-only.

Verified against grok 1.0.13 on the host: ``grok -p "<prompt>" --always-approve
--output-format plain -m <model>`` runs one prompt to completion,
non-interactive, auto-approving tool calls. The flag is read from the model's final output
(``FLAG: <flag>``) and submitted through the swarm's deduped ``submit_fn``.

Requires the `grok` binary inside the sandbox image (see sandbox/Dockerfile.sandbox).
Auth comes entirely from the mounted GROK_HOME; no API key is used.
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
_QUOTA = re.compile(r"429|rate.?limit|quota|too many requests|usage limit|out of credits|insufficient", re.I)
_CREDS = "/grok-creds"


class GrokSolver:
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
        await self._ensure_grok()

    async def _ensure_grok(self) -> None:
        """Make sure the grok CLI is available in the container (install on demand)."""
        if not self.sandbox:
            return
        try:
            check = await self.sandbox.exec("command -v grok >/dev/null 2>&1 && echo yes || echo no", timeout_s=15)
            if "yes" in (check.stdout or ""):
                return
            logger.info("Installing grok CLI into sandbox for %s", self.model_spec)
            await self.sandbox.exec(
                "curl -fsSL https://x.ai/cli/install.sh | bash "
                "|| npm install -g @vibe-kit/grok-cli 2>/dev/null || true",
                timeout_s=180,
            )
        except Exception as e:
            logger.warning("grok ensure failed: %s", e)

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
            prompt += "\n\n## Notes from teammates / previous attempts\n" + "\n".join(self._insights[-5:])
        return prompt

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self.sandbox:
            return SolverResult(flag=None, status=ERROR, findings_summary="grok sandbox not started")
        self._step += 1
        task = self._build_task()

        # Write the task to the workspace and let grok read it (avoids arg-length limits).
        try:
            await self.sandbox.write_file("/challenge/workspace/task.md", task)
        except Exception as e:
            return SolverResult(flag=None, status=ERROR, findings_summary=f"grok: could not stage task ({e})")

        model_flag = f"-m {shlex.quote(self.model_id)}" if self.model_id else ""
        instruction = (
            "Read /challenge/workspace/task.md and solve the CTF challenge it describes. "
            "Work in /challenge. When you have the real flag, print it on its own line as: FLAG: <flag>"
        )
        cmd = (
            "export PATH=/root/.grok/bin:/root/.local/bin:$PATH; "
            f"cd /challenge && GROK_HOME={_CREDS} XAI_API_KEY= "
            # `--output-format` takes plain|json|streaming-json|streaming-messages-json.
            # "text" is rejected with exit code 2, which failed every Grok solver.
            f"grok -p {shlex.quote(instruction)} --always-approve --output-format plain {model_flag}"
        )

        if self.cancel_event.is_set():
            return SolverResult(flag=None, status=GAVE_UP, findings_summary="cancelled before start")

        result = await self.sandbox.exec(cmd, timeout_s=900)
        out = (result.stdout or "") + "\n" + (result.stderr or "")

        if "grok: command not found" in out or "not found" in (result.stderr or "").lower() and "grok" in (result.stderr or "").lower():
            return SolverResult(flag=None, status=ERROR,
                                findings_summary="grok CLI is not installed in the sandbox image")
        if _QUOTA.search(out):
            return SolverResult(flag=None, status=QUOTA_ERROR, findings_summary="grok subscription rate/usage limit")

        # Extract candidate flags: explicit FLAG: lines first, then flag-format tokens.
        candidates: list[str] = []
        for m in _FLAG_LINE.finditer(out):
            candidates.append(m.group(1).strip().strip("`'\""))
        for m in _FLAG_FMT.finditer(out):
            v = m.group(0)
            if v not in candidates and "placeholder" not in v.lower() and v.lower() not in ("ctf{flag}", "flag{flag}"):
                candidates.append(v)

        findings = _summarize(out)
        if self.message_bus:
            try:
                await self.message_bus.post(self.model_spec, findings[:1000])
            except Exception:
                pass

        for flag in candidates[:5]:
            if self.no_submit or not self.submit_fn:
                return SolverResult(flag=flag, status=FLAG_FOUND, findings_summary=findings,
                                    step_count=self._step)
            try:
                display, confirmed = await self.submit_fn(flag)
            except Exception as e:
                logger.warning("grok submit_fn error: %s", e)
                continue
            if confirmed:
                return SolverResult(flag=flag, status=FLAG_FOUND, findings_summary=findings,
                                    step_count=self._step)

        return SolverResult(flag=None, status=GAVE_UP, findings_summary=findings, step_count=self._step)

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
        return text or "grok produced no output"
    return text[-limit:]
