"""Interactive CLI sign-in manager for connecting pool accounts from the web.

Claude Code and Codex have no clean non-interactive sign-in, so we drive their
real CLIs and surface the OAuth step to the browser:

- **Claude** (`claude auth login --claudeai`): needs a PTY. We allocate a
  pseudo-terminal, scrape the ``https://claude.com/.../oauth/authorize?code=true``
  URL it prints, show it to the user, then write the authorization code they
  paste back into the PTY's stdin. The CLI exchanges it and writes
  ``.credentials.json`` into ``CLAUDE_CONFIG_DIR``.

  Not ``claude setup-token``: that mints a long-lived CI token scoped
  ``user:inference`` only, and drives an Ink TUI that parks on "Press Enter to
  retry" when an exchange fails — which looked, from the browser, like a
  sign-in that never finished.
- **Codex** (`codex login --device-auth`) and **Grok** (`grok login
  --device-auth`): print a device URL + one-time code and poll the auth server
  themselves, writing credentials under their config dir on success. We just
  surface the URL + code and keep the process alive.
- **Antigravity** (bare ``agy``): shows a login menu whose first entry is
  "Google OAuth". We select it, scrape the ``accounts.google.com`` URL it prints
  and take the pasted authorization code. `agy` stores the session in the OS
  keyring rather than a file, so completion is confirmed by asking
  ``agy models`` and recording the answer in ``ANTIGRAVITY_MARKER``.

Sessions are held in-memory keyed by pool account id (the app is a single
process). Completion is detected by the caller polling ``cli_auth`` for
credentials on disk; ``finish()`` reaps the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import re
import struct
import termios
import time

from backend.cli_auth import ANTIGRAVITY_MARKER

logger = logging.getLogger(__name__)

# Strip ANSI escape sequences (CSI + OSC) from TUI output.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB0-2]")

_CLAUDE_URL = re.compile(r"https://[a-zA-Z0-9.]*claude\.[a-z]+/\S*oauth\S*")
_CODEX_URL = re.compile(r"https://\S*device\S*")
_CODEX_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
# Grok device flow: prefer a URL that mentions device/auth/x.ai, else any https URL.
_GROK_URL = re.compile(r"https://\S*(?:device|auth|x\.ai|grok)\S*", re.I)
_AGY_URL = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+")
# `agy` draws its OAuth URL inside a box, so once newlines and padding are
# stripped the match runs straight on into the border glyphs. Cut at the first
# character that cannot appear in a URL.
_URL_TAIL = re.compile(r"[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]")
_ANY_URL = re.compile(r"https://\S+")

# The CLIs report a rejected/expired authorization code on their own stdout and
# then sit at a retry prompt forever. Without scraping these the browser just
# says "finishing sign-in..." indefinitely.
_ERROR_MARKERS = (
    "oauth error",
    "authentication failed",
    "invalid code",
    "invalid_grant",
    "request failed with status code",
    "login failed",
    "sign-in failed",
)
_RETRY_MARKER = "press enter to retry"

_SESSION_TTL = 900  # 15 min


@dataclasses.dataclass
class _Session:
    provider: str
    account_id: int
    config_dir: str
    proc: asyncio.subprocess.Process | None = None
    master_fd: int | None = None  # claude PTY master
    buffer: str = ""
    url: str = ""
    user_code: str = ""
    created_at: float = dataclasses.field(default_factory=time.time)
    exited: bool = False  # the CLI's output stream closed / process ended
    submitted_at: float = 0.0  # when a code was last written to stdin
    # Which URL a PTY session should scrape (claude's OAuth URL by default).
    url_re: re.Pattern = _CLAUDE_URL

    def error(self) -> str:
        """The CLI's own failure line, if it printed one after the last submit.

        Only text produced *after* the code was submitted counts, so the banner
        text a CLI prints at startup can never be mistaken for a failure.
        """
        if not self.submitted_at:
            return ""
        for line in reversed(self.buffer.splitlines()):
            stripped = line.strip()
            low = stripped.lower()
            hits = [at for m in _ERROR_MARKERS if (at := low.find(m)) >= 0]
            if hits:
                # The failure is often echoed onto the prompt line ("Paste code
                # here if prompted > Login failed: ..."), so start at the
                # earliest marker — that keeps the CLI's whole message rather
                # than a fragment of it.
                return stripped[min(hits):][:200]
        return ""

    def at_retry_prompt(self) -> bool:
        return _RETRY_MARKER in self.buffer[-2000:].lower()

    @property
    def expires_at(self) -> float:
        return self.created_at + _SESSION_TTL

    def tail(self, lines: int = 6) -> str:
        """Last few lines of CLI output — shown to the operator when sign-in dies."""
        rows = [ln.strip() for ln in self.buffer.splitlines() if ln.strip()]
        return "\n".join(rows[-lines:])


class ConnectManager:
    def __init__(self) -> None:
        self._sessions: dict[int, _Session] = {}

    # ── public API ───────────────────────────────────────────────────────────

    async def start_claude(self, account_id: int, config_dir: str) -> dict:
        self._prune()
        await self.finish(account_id)  # drop any prior attempt
        os.makedirs(config_dir, exist_ok=True)

        master, slave = os.openpty()
        # Wide terminal so the TUI never line-wraps the OAuth URL.
        with contextlib.suppress(Exception):
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))

        env = {
            **os.environ,
            "CLAUDE_CONFIG_DIR": config_dir,
            "BROWSER": "/bin/false",  # force the URL to print instead of opening
            "DISPLAY": "",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
            "COLUMNS": "400",
            "LINES": "60",
            "CLAUDECODE": "",
        }
        claude_bin = _which("claude")
        try:
            # `auth login --claudeai`, NOT `setup-token`: setup-token mints a
            # long-lived CI token scoped `user:inference` only and drives an Ink
            # TUI whose errors we can't surface; `auth login` is the real
            # subscription sign-in (full scope, plain prompts) and writes
            # `.credentials.json` into CLAUDE_CONFIG_DIR, which is what the pool
            # checks for.
            proc = await asyncio.create_subprocess_exec(
                claude_bin, "auth", "login", "--claudeai",
                stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True, close_fds=True,
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            return {"error": "Claude CLI not found on this server.",
                    "hint": "npm install -g @anthropic-ai/claude-code", "status_code": 404}
        os.close(slave)
        os.set_blocking(master, False)

        sess = _Session(provider="claude", account_id=account_id,
                        config_dir=config_dir, proc=proc, master_fd=master)
        self._sessions[account_id] = sess

        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._on_pty_read, account_id)

        url = await self._await_field(account_id, "url", timeout=25)
        if not url:
            await self.finish(account_id)
            return {"error": "Could not read the Claude sign-in URL.",
                    "hint": "Claude CLI may have changed; try again or sign in via SSH.",
                    "status_code": 502}
        return {"status": "pending", "auth_url": url, "needs_code": True}

    async def start_codex(self, account_id: int, config_dir: str) -> dict:
        self._prune()
        await self.finish(account_id)
        os.makedirs(config_dir, exist_ok=True)

        env = {**os.environ, "HOME": config_dir, "NO_COLOR": "1", "CODEX_DISABLE_TELEMETRY": "1"}
        env.pop("OPENAI_API_KEY", None)  # force subscription auth
        codex_bin = _which("codex")
        try:
            proc = await asyncio.create_subprocess_exec(
                codex_bin, "login", "--device-auth",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, start_new_session=True,
            )
        except FileNotFoundError:
            return {"error": "Codex CLI not found on this server.",
                    "hint": "npm install -g @openai/codex", "status_code": 404}

        sess = _Session(provider="codex", account_id=account_id, config_dir=config_dir, proc=proc)
        self._sessions[account_id] = sess
        asyncio.create_task(self._read_codex(account_id))

        url = await self._await_field(account_id, "url", timeout=25)
        if not url:
            await self.finish(account_id)
            return {"error": "Could not read the Codex device URL.", "status_code": 502}
        return {"status": "device", "auth_url": url,
                "user_code": self._sessions[account_id].user_code, "needs_code": False}

    async def start_grok(self, account_id: int, config_dir: str) -> dict:
        """Sign in a Grok (xAI) subscription via `grok login --device-auth`.

        Prints a URL + one-time code and polls xAI itself, writing credentials
        into GROK_HOME (the isolated config_dir) on success — so multiple Grok
        accounts stay separated and switchable, just like Codex.
        """
        self._prune()
        await self.finish(account_id)
        os.makedirs(config_dir, exist_ok=True)

        env = {**os.environ, "GROK_HOME": config_dir, "NO_COLOR": "1", "BROWSER": "/bin/false", "DISPLAY": ""}
        env.pop("XAI_API_KEY", None)  # force subscription auth
        grok_bin = _which("grok")
        try:
            proc = await asyncio.create_subprocess_exec(
                grok_bin, "login", "--device-auth",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, start_new_session=True,
            )
        except FileNotFoundError:
            return {"error": "Grok CLI not found on this server.",
                    "hint": "Install: curl -fsSL https://x.ai/cli/install.sh | bash", "status_code": 404}

        sess = _Session(provider="grok", account_id=account_id, config_dir=config_dir, proc=proc)
        self._sessions[account_id] = sess
        asyncio.create_task(self._read_device(account_id, _GROK_URL))

        url = await self._await_field(account_id, "url", timeout=25)
        if not url:
            await self.finish(account_id)
            return {"error": "Could not read the Grok device URL.",
                    "hint": "Grok CLI may need updating, or sign in over SSH.", "status_code": 502}
        return {"status": "device", "auth_url": url,
                "user_code": self._sessions[account_id].user_code, "needs_code": False}

    async def start_antigravity(self, account_id: int, config_dir: str) -> dict:
        """Sign in a Google account to Antigravity by driving the `agy` CLI.

        `agy` has no `login` subcommand: run bare, it shows a login menu whose
        first entry is "Google OAuth". Selecting it in an SSH-like environment
        makes it print a Google OAuth URL and wait for the authorization code
        the browser shows — the same shape as the Claude flow.

        All of `agy`'s state lives under HOME/XDG, so pointing those at the
        account's own directory (and hiding the session D-Bus, which would
        otherwise share one keyring across every account) keeps several Google
        accounts isolated and independently poolable.
        """
        self._prune()
        await self.finish(account_id)
        os.makedirs(config_dir, exist_ok=True)

        master, slave = os.openpty()
        with contextlib.suppress(Exception):
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))

        env = {
            **agy_env(config_dir),
            "TERM": "xterm-256color",
            "COLUMNS": "400",
            "LINES": "60",
            # Make `agy` take its out-of-band ("paste the code back") path
            # instead of trying to open a browser on the server.
            "SSH_CONNECTION": "127.0.0.1 22 127.0.0.1 22",
            "SSH_TTY": "/dev/pts/0",
        }
        agy_bin = _which("agy")
        try:
            proc = await asyncio.create_subprocess_exec(
                agy_bin,
                stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True, close_fds=True,
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            return {"error": "Antigravity CLI (agy) not found on this server.",
                    "hint": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                    "status_code": 404}
        os.close(slave)
        os.set_blocking(master, False)

        sess = _Session(provider="antigravity", account_id=account_id,
                        config_dir=config_dir, proc=proc, master_fd=master,
                        url_re=_AGY_URL)
        self._sessions[account_id] = sess

        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._on_pty_read, account_id)

        # Wait for the login menu, then pick its first entry ("Google OAuth").
        for _ in range(60):
            await asyncio.sleep(0.5)
            if "select login method" in sess.buffer.lower():
                break
        if sess.url:
            pass  # already past the menu (a resumed session can skip it)
        else:
            with contextlib.suppress(OSError):
                os.write(master, b"\r")

        url = await self._await_field(account_id, "url", timeout=40)
        if not url:
            tail = sess.tail(8)
            await self.finish(account_id)
            return {"error": "Could not read the Google sign-in URL from the Antigravity CLI.",
                    "hint": tail or "Try again, or run `agy` over SSH once to sign in.",
                    "status_code": 502}
        return {"status": "pending", "auth_url": url, "needs_code": True}

    async def submit_code(self, account_id: int, code: str) -> bool:
        """Write a pasted authorization code into the Claude PTY's stdin.

        If a previous code was rejected the CLI is parked on "Press Enter to
        retry", not on the paste prompt — send the Enter first so a second
        attempt works without restarting the whole sign-in.
        """
        sess = self._sessions.get(account_id)
        if not sess or sess.master_fd is None:
            return False
        try:
            if sess.at_retry_prompt():
                os.write(sess.master_fd, b"\r")
                await asyncio.sleep(0.6)
                sess.buffer = ""  # the old error must not mask the new attempt
            os.write(sess.master_fd, (code.strip() + "\r").encode())
            sess.submitted_at = time.time()
        except OSError:
            return False
        if sess.provider == "antigravity":
            # `agy` keeps its session in the keyring, so there is no credentials
            # file to watch for. Ask the CLI itself and record the answer.
            asyncio.create_task(self._verify_antigravity(account_id))
        return True

    async def _verify_antigravity(self, account_id: int) -> None:
        """Poll `agy models` until the Google sign-in lands, then write the marker."""
        sess = self._sessions.get(account_id)
        if not sess:
            return
        deadline = time.time() + 120
        while time.time() < deadline and self._sessions.get(account_id) is sess:
            await asyncio.sleep(3)
            ok, models, err = await agy_models(sess.config_dir)
            if ok:
                path = os.path.join(sess.config_dir, ANTIGRAVITY_MARKER)
                with contextlib.suppress(OSError):
                    with open(path, "w") as f:
                        json.dump({"models": models, "verified_at": time.time()}, f)
                logger.info("Antigravity account %s signed in (%d models)", account_id, len(models))
                return
            logger.debug("antigravity verify for %s not ready: %s", account_id, err)

    def status(self, account_id: int) -> dict:
        """Liveness of a connect session: is the CLI still waiting, or did it die?

        Returns ``{}`` when no session is tracked (already reaped, or the app
        restarted mid-sign-in).
        """
        self._prune()
        sess = self._sessions.get(account_id)
        if not sess:
            return {}
        dead = sess.exited or (sess.proc is not None and sess.proc.returncode is not None)
        error = sess.error()
        return {
            "alive": not dead,
            "provider": sess.provider,
            "expires_in": max(0, int(sess.expires_at - time.time())),
            # A live CLI parked on a retry prompt has still failed as far as the
            # operator is concerned, so report the error either way.
            "error": error,
            "can_retry": bool(error) and not dead,
            "tail": sess.tail() if (dead or error) else "",
        }

    async def finish(self, account_id: int) -> None:
        """Tear down a session (call after creds are detected, or to cancel)."""
        sess = self._sessions.pop(account_id, None)
        if not sess:
            return
        if sess.master_fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(sess.master_fd)
            with contextlib.suppress(OSError):
                os.close(sess.master_fd)
        if sess.proc and sess.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                sess.proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sess.proc.wait(), timeout=3)
            if sess.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    sess.proc.kill()

    # ── internals ──────────────────────────────────────────────────────────

    def _detach_reader(self, sess: _Session) -> None:
        """Stop watching a PTY whose child has exited, and mark the session dead."""
        sess.exited = True
        if sess.master_fd is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(sess.master_fd)

    def _on_pty_read(self, account_id: int) -> None:
        sess = self._sessions.get(account_id)
        if not sess or sess.master_fd is None:
            return
        try:
            data = os.read(sess.master_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            # The slave side closed (EIO): the CLI is gone. Drop the reader or
            # the fd stays permanently readable and spins the event loop.
            self._detach_reader(sess)
            return
        if not data:
            self._detach_reader(sess)
            return
        sess.buffer += _ANSI.sub("", data.decode("utf-8", errors="replace"))
        if not sess.url:
            # Both CLIs wrap the URL across terminal lines (and pad the
            # continuation), so match against the text with newlines and spaces
            # removed, then cut at the first character a URL can't contain —
            # `agy` draws a box border immediately after its URL.
            m = sess.url_re.search(sess.buffer.replace("\n", "").replace(" ", ""))
            if m:
                url = m.group(0)
                cut = _URL_TAIL.search(url)
                sess.url = url[: cut.start()] if cut else url

    async def _read_codex(self, account_id: int) -> None:
        await self._read_device(account_id, _CODEX_URL)

    async def _read_device(self, account_id: int, url_re: re.Pattern) -> None:
        """Stream a device-flow CLI's stdout, scraping the URL + one-time code."""
        sess = self._sessions.get(account_id)
        if not sess or not sess.proc or not sess.proc.stdout:
            return
        try:
            while True:
                line = await sess.proc.stdout.readline()
                if not line:
                    break
                text = _ANSI.sub("", line.decode("utf-8", errors="replace"))
                sess.buffer += text
                if not sess.url:
                    m = url_re.search(text) or url_re.search(sess.buffer) or _ANY_URL.search(sess.buffer)
                    if m:
                        sess.url = m.group(0)
                if not sess.user_code:
                    m = _CODEX_CODE.search(text)
                    if m:
                        sess.user_code = m.group(0)
        except Exception:
            pass
        finally:
            # stdout closed: the CLI is finished (success writes creds to disk,
            # failure/expiry leaves nothing). Either way stop claiming "waiting".
            sess.exited = True
            if sess.proc:
                with contextlib.suppress(Exception):
                    await sess.proc.wait()

    async def _await_field(self, account_id: int, field: str, timeout: float) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            sess = self._sessions.get(account_id)
            if not sess:
                return ""
            val = getattr(sess, field, "")
            if val:
                return val
            await asyncio.sleep(0.2)
        sess = self._sessions.get(account_id)
        return getattr(sess, field, "") if sess else ""

    def _prune(self) -> None:
        now = time.time()
        stale = [aid for aid, s in self._sessions.items() if now - s.created_at > _SESSION_TTL]
        for aid in stale:
            asyncio.create_task(self.finish(aid))

    async def reap_loop(self, interval: float = 60.0) -> None:
        """Background reaper — drops expired sessions (and their CLI processes).

        ``_prune`` alone only ran when someone started another sign-in, so an
        abandoned connect left ``claude setup-token`` / ``codex login`` /
        ``grok login`` running on the server indefinitely.
        """
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                self._prune()


def agy_env(config_dir: str) -> dict[str, str]:
    """Environment that pins every bit of `agy` state to *config_dir*.

    HOME alone is not enough: `agy` also honours the XDG dirs, and it reaches a
    shared OS keyring over the session D-Bus — either of which would let one
    connected Google account overwrite another's session.
    """
    env = {
        **os.environ,
        "HOME": config_dir,
        "XDG_CONFIG_HOME": os.path.join(config_dir, ".config"),
        "XDG_DATA_HOME": os.path.join(config_dir, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(config_dir, ".cache"),
        "NO_COLOR": "1",
        "DISPLAY": "",
        "BROWSER": "/bin/false",
    }
    for k in ("DBUS_SESSION_BUS_ADDRESS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(k, None)
    return env


async def agy_models(config_dir: str, timeout: float = 60.0) -> tuple[bool, list[str], str]:
    """Ask `agy models` whether *config_dir* holds a signed-in Google account.

    Returns ``(signed_in, model_ids, message)``. The CLI exits non-zero with
    "Please sign in to view available models" until the account is connected,
    which makes this a reliable oracle — `agy` stores no credentials file we
    could stat instead.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _which("agy"), "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=agy_env(config_dir), start_new_session=True,
        )
    except FileNotFoundError:
        return False, [], "Antigravity CLI (agy) not found on this server."
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False, [], "agy models timed out"
    text = _ANSI.sub("", (out or b"").decode("utf-8", "replace"))
    if proc.returncode != 0:
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return False, [], line or f"agy models exited {proc.returncode}"
    models = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        # Model rows are the bare ids; skip headings and decoration.
        if line and " " not in line and "/" not in line and not line.endswith(":"):
            models.append(line)
    return True, sorted(set(models)), ""


def _which(binary: str) -> str:
    import shutil
    return shutil.which(binary) or binary


_mgr: ConnectManager | None = None


def get_connect_manager() -> ConnectManager:
    global _mgr
    if _mgr is None:
        _mgr = ConnectManager()
    return _mgr
