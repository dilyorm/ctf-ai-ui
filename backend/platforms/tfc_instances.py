"""Stable local endpoints for The Few Chosen's per-team challenge instances.

TFC runs dynamic challenges as short-lived Kubernetes deployments behind
``challenge-manager``. Two things make them awkward for an autonomous solver:

1. An instance lives for 15-30 minutes and is then torn down.
2. Re-provisioning mints a *new* random hostname
   (``mid2-20b1fe0c…`` → ``mid2-fa2d8fd3…``), so any address baked into a
   solver's prompt goes stale.

This module hides both. Each challenge image gets one **stable** local
TCP port. Nothing is provisioned until a solver actually connects; on every
inbound connection the broker makes sure a live deployment exists (starting or
replacing it as needed) and pipes the bytes through. The solver's prompt can
therefore say ``nc host.docker.internal 41234`` for the whole run.

Upstream transports differ per challenge and are normalised here:

===============  =======================================  ==================
connection_type  upstream                                 what the solver sees
===============  =======================================  ==================
``netcat``       TLS to ``<dep>.<domain>:1337``            plain TCP
``nodeport``     plain TCP to the reported host/port       plain TCP
``http``         HTTP(S) to ``<dep>.<domain>``             plain HTTP
===============  =======================================  ==================

The ingress routes web challenges by ``Host`` header, so for ``http`` the
broker rewrites the request's ``Host`` line to the current deployment.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Re-provision this long before the platform's stated expiry, so a solver never
# lands mid-teardown.
EXPIRY_MARGIN_S = 45.0
# Cap on the request header block we buffer before rewriting Host.
MAX_HEADER_BYTES = 64 * 1024
# A freshly provisioned pod is listed before it accepts connections, and
# nodeport allocations show up a beat later still — keep retrying for this long
# rather than handing the solver a dead socket.
UPSTREAM_READY_TIMEOUT_S = 120.0
UPSTREAM_CONNECT_TIMEOUT_S = 20.0


def _tls_context() -> ssl.SSLContext:
    """Permissive TLS — challenge instances routinely use throwaway certs."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_expiry(raw: str) -> dt.datetime:
    """Parse challenge-manager's ``2026-09-05T15:15:52Z`` timestamps."""
    try:
        return dt.datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError:
        # Unknown format: treat as already stale so we re-provision.
        return dt.datetime.now(dt.timezone.utc)


def detect_bind_host() -> str:
    """Address the sandbox containers reach us on.

    Sandboxes run on the default bridge with ``host.docker.internal`` mapped to
    the host gateway, so binding the gateway address keeps the proxy off any
    public interface. Falls back to loopback when there is no bridge (tests,
    dev machines).
    """
    import socket
    import subprocess

    candidates: list[str] = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "docker0"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            candidates.append(m.group(1))
    except Exception:
        pass
    candidates += ["172.17.0.1", "127.0.0.1"]

    for host in candidates:
        s = socket.socket()
        try:
            s.bind((host, 0))
            return host
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


@dataclass
class Deployment:
    """A live challenge instance as challenge-manager reports it."""

    name: str
    expires_at: dt.datetime
    connection: dict[str, Any] | None = None
    # Web instances answer at the ingress (TCP connects fine) for a while
    # before the pod behind it serves anything but 503.
    ready: bool = False

    def fresh(self, margin_s: float = EXPIRY_MARGIN_S) -> bool:
        now = dt.datetime.now(dt.timezone.utc)
        return (self.expires_at - now).total_seconds() > margin_s


@dataclass
class _Instance:
    """One challenge image and the local port standing in for it."""

    image: str
    connection_type: str
    http_only: bool
    port: int = 0
    server: asyncio.base_events.Server | None = None
    deployment: Deployment | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InstanceBroker:
    """Keeps stable local ports pointed at TFC's rotating challenge instances."""

    def __init__(
        self,
        manager_url: str,
        challenge_domain: str,
        auth_headers: Callable[[], Awaitable[dict[str, str]]],
        client: httpx.AsyncClient | None = None,
        bind_host: str = "",
        advertise_host: str = "host.docker.internal",
    ) -> None:
        self.manager_url = manager_url.rstrip("/")
        self.challenge_domain = challenge_domain
        self._auth_headers = auth_headers
        self._client = client
        self._owns_client = client is None
        self.bind_host = bind_host or detect_bind_host()
        self.advertise_host = advertise_host
        self._instances: dict[str, _Instance] = {}

    # ---------------------------------------------------------------- HTTP

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.manager_url, timeout=60.0)
        return self._client

    async def _manager(self, method: str, path: str, **kw: Any) -> httpx.Response:
        client = await self._http()
        headers = {**(await self._auth_headers()), **kw.pop("headers", {})}
        return await client.request(method, path, headers=headers, **kw)

    async def list_deployments(self) -> list[Deployment]:
        resp = await self._manager("GET", "/isolated")
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data") if isinstance(body, dict) else body
        out: list[Deployment] = []
        for item in items or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(
                    Deployment(
                        name=item["name"],
                        expires_at=_parse_expiry(item.get("expiresAt", "")),
                        connection=item.get("connection"),
                    )
                )
        return out

    async def _adopt(self, image: str) -> Deployment | None:
        """Find a deployment already running for *image* (names are ``<image>-<hex>``)."""
        for dep in await self.list_deployments():
            if dep.name.rsplit("-", 1)[0] == image:
                return dep
        return None

    async def _start(self, image: str) -> Deployment:
        resp = await self._manager("POST", "/isolated", json={"name": image})
        if resp.status_code == 409:
            # Already running for this team — adopt whatever is live.
            adopted = await self._adopt(image)
            if adopted:
                return adopted
        resp.raise_for_status()
        name = (resp.json() or {}).get("deploymentName") or ""
        if not name:
            raise RuntimeError(f"challenge-manager did not name the deployment for {image}")
        # The create response has no expiry/connection; read it back.
        for _ in range(10):
            adopted = await self._adopt(image)
            if adopted and adopted.name == name:
                return adopted
            await asyncio.sleep(1.0)
        # Listing lagged; assume the platform's shortest advertised lifetime.
        return Deployment(
            name=name,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15),
        )

    async def _stop(self, name: str) -> None:
        with contextlib.suppress(Exception):
            await self._manager("DELETE", f"/isolated/{name}")

    async def ensure_deployment(self, inst: _Instance) -> Deployment:
        """Return a live deployment for *inst*, provisioning or replacing as needed."""
        async with inst.lock:
            if inst.deployment and inst.deployment.fresh():
                return inst.deployment

            adopted = await self._adopt(inst.image)
            if adopted and adopted.fresh():
                inst.deployment = adopted
                return adopted

            if adopted:
                # Expiring or expired: tear down so the next start succeeds.
                logger.info("tfc: replacing stale instance %s", adopted.name)
                await self._stop(adopted.name)

            inst.deployment = await self._start(inst.image)
            logger.info(
                "tfc: instance %s live until %s", inst.deployment.name, inst.deployment.expires_at
            )
            return inst.deployment

    # ------------------------------------------------------------- upstream

    def _upstream(self, inst: _Instance, dep: Deployment) -> tuple[str, int, bool, str]:
        """Return ``(host, port, use_tls, sni)`` for the current deployment."""
        vhost = f"{dep.name}.{self.challenge_domain}"
        if inst.connection_type == "nodeport":
            conn = dep.connection or {}
            host = conn.get("host") or conn.get("ip") or vhost
            ports = conn.get("ports") or []
            port = 0
            if ports and isinstance(ports[0], dict):
                port = int(ports[0].get("tcpPort") or ports[0].get("port") or 0)
            if not port:
                raise RuntimeError(f"no nodeport allocated yet for {inst.image}")
            return (str(host), port, False, "")
        if inst.connection_type == "http":
            return (vhost, 80, False, "") if inst.http_only else (vhost, 443, True, vhost)
        # netcat (and anything unrecognised) is TLS on 1337, per the platform UI.
        return (vhost, 1337, True, vhost)

    async def _connect_upstream(
        self, inst: _Instance
    ) -> tuple[Deployment, asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a socket to the live instance, waiting for it to come up.

        The platform reports a deployment as soon as it is scheduled, so the
        first connection after provisioning routinely races the pod's start-up.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPSTREAM_READY_TIMEOUT_S
        delay = 2.0
        last: Exception | None = None
        while True:
            dep = await self.ensure_deployment(inst)
            try:
                host, port, use_tls, sni = self._upstream(inst, dep)
                if inst.connection_type == "http" and not dep.ready:
                    if not await self._http_ready(inst, dep):
                        raise RuntimeError("web instance is still returning 503")
                    dep.ready = True
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host,
                        port,
                        ssl=_tls_context() if use_tls else None,
                        server_hostname=sni if use_tls else None,
                    ),
                    timeout=UPSTREAM_CONNECT_TIMEOUT_S,
                )
                return dep, reader, writer
            except Exception as e:
                last = e
                if loop.time() >= deadline:
                    raise RuntimeError(f"{inst.image} never became reachable: {e}") from last
                # A nodeport's ports (and readiness generally) land on the
                # deployment record after it first appears — re-read it.
                async with inst.lock:
                    refreshed = await self._adopt(inst.image)
                    if refreshed and refreshed.name == dep.name:
                        inst.deployment = refreshed
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 8.0)
                if loop.time() >= deadline and inst.connection_type == "http":
                    # Out of patience: hand the solver whatever the app says
                    # rather than nothing at all.
                    dep.ready = True

    async def _http_ready(self, inst: _Instance, dep: Deployment) -> bool:
        """True once the web instance serves something other than a gateway error."""
        scheme = "http" if inst.http_only else "https"
        url = f"{scheme}://{dep.name}.{self.challenge_domain}/"
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=15.0, follow_redirects=False
            ) as probe:
                resp = await probe.get(url)
            return resp.status_code not in (502, 503, 504)
        except Exception:
            return False

    # -------------------------------------------------------------- proxying

    async def _rewrite_http_head(
        self, reader: asyncio.StreamReader, vhost: str
    ) -> bytes:
        """Read the first request's header block and point ``Host`` at *vhost*."""
        head = b""
        while b"\r\n\r\n" not in head and len(head) < MAX_HEADER_BYTES:
            chunk = await reader.read(4096)
            if not chunk:
                break
            head += chunk
        if b"\r\n\r\n" not in head:
            return head
        block, rest = head.split(b"\r\n\r\n", 1)
        lines = block.split(b"\r\n")
        out = [lines[0]] if lines else []
        seen_host = False
        upgrade = any(ln.lower().startswith(b"upgrade:") for ln in lines[1:])
        for line in lines[1:]:
            low = line.lower()
            if low.startswith(b"host:"):
                out.append(f"Host: {vhost}".encode())
                seen_host = True
            elif low.startswith(b"connection:") and not upgrade:
                # Force one request per connection so every Host gets rewritten;
                # a keep-alive stream would smuggle later requests past us.
                out.append(b"Connection: close")
            else:
                out.append(line)
        if not seen_host:
            out.insert(1, f"Host: {vhost}".encode())
        if not upgrade and not any(ln.lower().startswith(b"connection:") for ln in out[1:]):
            out.append(b"Connection: close")
        return b"\r\n".join(out) + b"\r\n\r\n" + rest

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.close()

    def _handler(self, inst: _Instance) -> Callable[..., Awaitable[None]]:
        async def handle(
            client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
        ) -> None:
            up_writer: asyncio.StreamWriter | None = None
            try:
                dep, up_reader, up_writer = await self._connect_upstream(inst)
                if inst.connection_type == "http":
                    head = await self._rewrite_http_head(
                        client_reader, f"{dep.name}.{self.challenge_domain}"
                    )
                    if head:
                        up_writer.write(head)
                        await up_writer.drain()
                await asyncio.gather(
                    self._pipe(client_reader, up_writer),
                    self._pipe(up_reader, client_writer),
                )
            except Exception as e:
                logger.warning("tfc proxy %s: %s", inst.image, e)
                with contextlib.suppress(Exception):
                    client_writer.close()
            finally:
                for w in (up_writer, client_writer):
                    if w is not None:
                        with contextlib.suppress(Exception):
                            w.close()

        return handle

    # ------------------------------------------------------------------ API

    async def endpoint(self, image: str, connection_type: str, http_only: bool) -> str:
        """Return a stable connection string a solver can use for the whole run.

        Binds a local port on first call; the challenge instance itself is only
        provisioned when something actually connects to it.
        """
        inst = self._instances.get(image)
        if inst is None:
            inst = _Instance(
                image=image,
                connection_type=(connection_type or "netcat").lower(),
                http_only=bool(http_only),
            )
            inst.server = await asyncio.start_server(
                self._handler(inst), host=self.bind_host, port=0
            )
            inst.port = inst.server.sockets[0].getsockname()[1]
            self._instances[image] = inst
            logger.info(
                "tfc: %s proxied at %s:%d (%s)",
                image, self.bind_host, inst.port, inst.connection_type,
            )
        if inst.connection_type == "http":
            return f"http://{self.advertise_host}:{inst.port}"
        return f"nc {self.advertise_host} {inst.port}"

    async def close(self) -> None:
        for inst in self._instances.values():
            if inst.server is not None:
                inst.server.close()
                with contextlib.suppress(Exception):
                    await inst.server.wait_closed()
        self._instances.clear()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


__all__ = ["Deployment", "InstanceBroker", "detect_bind_host"]
