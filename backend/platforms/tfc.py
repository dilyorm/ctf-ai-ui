"""The Few Chosen (``ctf.thefewchosen.com``) platform client.

TFC runs a bespoke API rather than CTFd/rCTF. The shapes below were read off
the site's own frontend bundle:

- ``POST {api}/auth/login`` — ``{username_or_email, password}`` →
  ``{access_token, refresh_token}``. The access token is a 10-minute JWT.
- ``POST {api}/auth/refresh`` — ``{username, refresh_token}`` → a new pair.
- ``GET  {api}/challenge`` — one call returns everything: ``challenges``,
  ``categories``, ``difficulties``. Category/difficulty are UUID references
  into those lists.
- ``POST {api}/challenge/submit`` — ``{challenge_id, flag_id, flag}``.
  HTTP 2xx means the flag was right; a wrong flag is ``400 invalid_flag``.

A challenge carries a *list* of flags (each with its own points and solved
state), so "solved" here means every flag on it is solved.

Dynamic challenges run as short-lived per-team instances; those are handled by
:class:`~backend.platforms.tfc_instances.InstanceBroker`, which hands out a
stable local address that survives the platform's instance rotation.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.ctfd import SubmitResult

logger = logging.getLogger(__name__)

DEFAULT_SITE = "https://ctf.thefewchosen.com"
DEFAULT_API = "https://api.ctf.thefewchosen.com"
DEFAULT_MANAGER = "https://challenge-manager.management.ctf.thefewchosen.com"
DEFAULT_CHALLENGE_DOMAIN = "challs.ctf.thefewchosen.com"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Access tokens are 10-minute JWTs; renew with room to spare.
TOKEN_MARGIN_S = 90.0


def derive_endpoints(base_url: str) -> tuple[str, str, str]:
    """Map a TFC site URL onto its ``(api, manager, challenge_domain)``.

    ``https://ctf.example.com`` → ``https://api.ctf.example.com``,
    ``https://challenge-manager.management.ctf.example.com``,
    ``challs.ctf.example.com`` — the naming the platform uses. Passing the API
    host directly also works.
    """
    url = (base_url or DEFAULT_SITE).strip().rstrip("/")
    if not url:
        return (DEFAULT_API, DEFAULT_MANAGER, DEFAULT_CHALLENGE_DOMAIN)
    scheme, _, rest = url.partition("://")
    if not rest:
        scheme, rest = "https", url
    host = rest.split("/", 1)[0]
    if host.startswith("api."):
        host = host[len("api.") :]
    return (
        f"{scheme}://api.{host}",
        f"{scheme}://challenge-manager.management.{host}",
        f"challs.{host}",
    )


@dataclass
class TFCClient:
    """The Few Chosen platform client — duck-typed to match ``CTFdClient``."""

    base_url: str = DEFAULT_SITE
    # Username/password are the real credentials; ``token`` accepts an
    # already-minted access token (used by the connector probe).
    username: str = ""
    password: str = ""
    token: str = ""
    # Provision instances for dynamic challenges and proxy them to a stable
    # local port. Off in tests and for read-only syncs.
    manage_instances: bool = True

    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _broker: Any = field(default=None, repr=False)
    _refresh_token: str = field(default="", repr=False)
    _token_expiry: dt.datetime | None = field(default=None, repr=False)
    _challenges: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.api_url, self.manager_url, self.challenge_domain = derive_endpoints(self.base_url)

    # --------------------------------------------------------------- session

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Credentials cross this connection, so certificate verification
            # stays on (unlike the throwaway certs on challenge instances).
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                follow_redirects=True,
                timeout=30.0,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    @staticmethod
    def _jwt_expiry(token: str) -> dt.datetime | None:
        """Read ``exp`` out of a JWT without verifying it (we only need timing)."""
        import base64
        import json

        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
            if exp:
                return dt.datetime.fromtimestamp(int(exp), dt.timezone.utc)
        except Exception:
            pass
        return None

    def _store_tokens(self, body: dict[str, Any]) -> None:
        self.token = body.get("access_token") or ""
        self._refresh_token = body.get("refresh_token") or self._refresh_token
        self._token_expiry = self._jwt_expiry(self.token)

    async def _login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError(
                "The Few Chosen needs a username and password "
                "(the access token it issues expires every 10 minutes)."
            )
        client = await self._ensure_client()
        resp = await client.post(
            "/auth/login",
            json={"username_or_email": self.username, "password": self.password},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"TFC login failed ({resp.status_code}): {resp.text[:200]}")
        self._store_tokens(resp.json())
        logger.info("tfc: logged in as %s", self.username)

    async def _refresh(self) -> bool:
        if not self._refresh_token or not self.username:
            return False
        client = await self._ensure_client()
        try:
            resp = await client.post(
                "/auth/refresh",
                json={"username": self.username, "refresh_token": self._refresh_token},
            )
            if resp.status_code >= 400:
                return False
            self._store_tokens(resp.json())
            return True
        except Exception:
            return False

    async def _access_token(self) -> str:
        """A token that is valid now — refreshing or re-logging in as needed."""
        if self.token and self._token_expiry is None:
            self._token_expiry = self._jwt_expiry(self.token)
        stale = (
            not self.token
            or self._token_expiry is None
            or (self._token_expiry - dt.datetime.now(dt.timezone.utc)).total_seconds()
            < TOKEN_MARGIN_S
        )
        if stale and not await self._refresh():
            await self._login()
        return self.token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}"}

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Authenticated request that re-authenticates once on a 401."""
        client = await self._ensure_client()
        resp = await client.request(method, path, headers=await self._auth_headers(), **kw)
        if resp.status_code == 401:
            self.token = ""
            self._token_expiry = None
            resp = await client.request(method, path, headers=await self._auth_headers(), **kw)
        return resp

    def _get_broker(self) -> Any:
        if self._broker is None:
            from backend.platforms.tfc_instances import InstanceBroker

            self._broker = InstanceBroker(
                manager_url=self.manager_url,
                challenge_domain=self.challenge_domain,
                auth_headers=self._auth_headers,
            )
        return self._broker

    # ------------------------------------------------------------ challenges

    async def _fetch_raw(self) -> dict[str, Any]:
        resp = await self._request("GET", "/challenge")
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _points(ch: dict[str, Any]) -> int:
        return sum(int(f.get("flag_points") or 0) for f in (ch.get("flags") or []))

    @staticmethod
    def _is_solved(ch: dict[str, Any]) -> bool:
        flags = ch.get("flags") or []
        return bool(flags) and all(f.get("is_solved") for f in flags)

    def _normalize(
        self, ch: dict[str, Any], categories: dict[str, str], difficulties: dict[str, str]
    ) -> dict[str, Any]:
        """Map a TFC challenge onto the CTFd-like dict the rest of the app uses."""
        files: list[str] = []
        for f in ch.get("files") or []:
            if isinstance(f, dict) and f.get("file_url"):
                files.append(f["file_url"])
            elif isinstance(f, str):
                files.append(f)
        tags: list[str] = []
        difficulty = difficulties.get(ch.get("difficulty_id") or "", "")
        if difficulty:
            tags.append(difficulty)
        return {
            "id": ch.get("challenge_id") or ch.get("challenge_name"),
            "name": ch.get("challenge_name") or "",
            "category": categories.get(ch.get("category_id") or "", ""),
            "value": self._points(ch),
            "description": ch.get("description") or "",
            "files": files,
            "tags": tags,
            "connection_info": "",
            "solves": int(ch.get("amount_solves") or 0),
            "type": "dynamic" if ch.get("is_dynamic") else "standard",
            "hints": [],
            "author": ch.get("challenge_author") or "",
            # TFC-specific, consumed by submit_flag and the instance broker.
            "flags": ch.get("flags") or [],
            "is_dynamic": bool(ch.get("is_dynamic")),
            "image_name": ch.get("image_name") or "",
            "connection_type": ch.get("connection_type") or "",
            "http_only": bool(ch.get("http_only")),
        }

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        raw = await self._fetch_raw()
        categories = {c["id"]: c.get("name", "") for c in raw.get("categories") or [] if c.get("id")}
        difficulties = {
            d["id"]: d.get("name", "") for d in raw.get("difficulties") or [] if d.get("id")
        }
        out: list[dict[str, Any]] = []
        for ch in raw.get("challenges") or []:
            norm = self._normalize(ch, categories, difficulties)
            if norm["name"]:
                self._challenges[norm["name"]] = norm
                out.append(norm)
        return out

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        # One call already returns full detail, so there is nothing extra to pull.
        return await self.fetch_challenge_stubs()

    async def fetch_solved_names(self) -> set[str]:
        try:
            raw = await self._fetch_raw()
        except Exception:
            logger.warning("tfc: could not fetch solved challenges", exc_info=True)
            return set()
        return {
            ch.get("challenge_name", "")
            for ch in raw.get("challenges") or []
            if self._is_solved(ch) and ch.get("challenge_name")
        }

    async def get_challenge_id(self, name: str) -> str:
        if name not in self._challenges:
            await self.fetch_challenge_stubs()
        if name not in self._challenges:
            raise RuntimeError(f'Challenge "{name}" not found on The Few Chosen')
        return str(self._challenges[name]["id"])

    # ---------------------------------------------------------------- submit

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        await self.get_challenge_id(challenge_name)
        ch = self._challenges[challenge_name]
        flags = ch.get("flags") or []
        if not flags:
            return SubmitResult(
                "unknown", "no flag slot", f'"{challenge_name}" has no flag slot to submit to.'
            )

        # A challenge can carry several flags; try the unsolved ones in order so
        # a multi-part challenge still accepts a flag for whichever part it is.
        targets = [f for f in flags if not f.get("is_solved")] or flags
        last: tuple[str, str] = ("unknown", "")
        for slot in targets:
            resp = await self._request(
                "POST",
                "/challenge/submit",
                json={
                    "challenge_id": ch["id"],
                    "flag_id": slot.get("flag_id"),
                    "flag": flag,
                },
            )
            try:
                body = resp.json()
            except Exception:
                body = {}
            code = str((body or {}).get("code") or "")
            message = str((body or {}).get("error") or (body or {}).get("message") or "").strip()

            if resp.status_code < 400:
                slot["is_solved"] = True
                return SubmitResult(
                    "correct",
                    message or "accepted",
                    f'CORRECT — "{flag}" accepted for {challenge_name}.',
                )
            if "already" in code.lower() or "already" in message.lower():
                return SubmitResult(
                    "already_solved",
                    message or code,
                    f'ALREADY SOLVED — "{flag}" was previously accepted. {message}'.strip(),
                )
            if resp.status_code == 429:
                return SubmitResult(
                    "unknown",
                    message or "rate limited",
                    f"RATE LIMITED — the platform rejected the submission: {message}",
                )
            last = (code or f"http_{resp.status_code}", message)

        code, message = last
        return SubmitResult(
            "incorrect",
            message or code,
            f'INCORRECT — "{flag}" rejected for {challenge_name}. {message}'.strip(),
        )

    # ------------------------------------------------------------------ pull

    async def instance_connection(self, challenge: dict[str, Any]) -> str:
        """Stable address for a dynamic challenge's per-team instance."""
        if not self.manage_instances or not challenge.get("is_dynamic"):
            return ""
        image = challenge.get("image_name") or ""
        if not image:
            return ""
        try:
            return await self._get_broker().endpoint(
                image,
                challenge.get("connection_type") or "netcat",
                bool(challenge.get("http_only")),
            )
        except Exception as e:
            logger.warning("tfc: could not proxy instance for %s: %s", image, e)
            return ""

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        """Download attachments and write metadata.yml — mirrors ``CTFdClient``."""
        from pathlib import Path
        from urllib.parse import urlparse

        import yaml
        from markdownify import markdownify as html2md

        name = challenge.get("name", f"tfc-{challenge.get('id')}")
        slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-") or "challenge"

        ch_dir = Path(output_dir) / slug
        ch_dir.mkdir(parents=True, exist_ok=True)

        client = await self._ensure_client()
        for raw_url in challenge.get("files") or []:
            dist_dir = ch_dir / "distfiles"
            dist_dir.mkdir(exist_ok=True)
            url = (
                raw_url
                if raw_url.startswith("http")
                else f"{self.api_url.rstrip('/')}/{raw_url.lstrip('/')}"
            )
            fname = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "file"
            dest = dist_dir / fname
            if dest.exists():
                continue
            try:
                # Attachments are served unauthenticated, but only send the
                # bearer token to the platform's own host regardless.
                headers = (
                    await self._auth_headers()
                    if urlparse(url).hostname == urlparse(self.api_url).hostname
                    else {}
                )
                resp = await client.get(
                    url, headers=headers, follow_redirects=True, timeout=120.0
                )
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                logger.info("Downloaded %s (%d bytes)", fname, len(resp.content))
            except Exception as e:
                logger.warning("Failed to download %s: %s", url, e)

        desc = challenge.get("description") or ""
        try:
            desc = html2md(desc, heading_style="atx", escape_asterisks=False)
        except Exception:
            pass

        connection_info = challenge.get("connection_info") or await self.instance_connection(
            challenge
        )
        meta = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": desc.strip(),
            "value": challenge.get("value", 0),
            "connection_info": connection_info,
            "tags": challenge.get("tags") or [],
            "solves": challenge.get("solves", 0),
        }
        (ch_dir / "metadata.yml").write_text(
            yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return str(ch_dir)

    async def close(self) -> None:
        if self._broker is not None:
            await self._broker.close()
            self._broker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["TFCClient", "derive_endpoints"]
