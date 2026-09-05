"""SSRF guard for user-supplied URLs.

The connector probe fetches whatever URL an operator types. Without a check,
any authenticated user could aim the server at internal services (its own
:8090, sibling Docker containers, the Postgres box) or the cloud metadata
endpoint (169.254.169.254 / metadata.google.internal). This resolves the
hostname and rejects any target that lands on a non-public address, so a
hostname that *resolves* to a private/loopback/link-local IP is blocked too
(a plain string check on the hostname would miss that).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# Hostnames that commonly front a metadata service or localhost.
BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "localhost",
}


def _addr_blocked(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if a.version == 6 and a.ipv4_mapped is not None:
        return _addr_blocked(str(a.ipv4_mapped))
    return (
        a.is_loopback
        or a.is_link_local  # 169.254.0.0/16 (incl. cloud metadata) + fe80::/10
        or a.is_private  # RFC1918 + ULA fc00::/7
        or a.is_reserved
        or a.is_multicast
        or a.is_unspecified  # 0.0.0.0 / ::
    )


async def assert_public_url(url: str) -> None:
    """Raise ValueError if *url* is not an http(s) URL on a public address."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = p.hostname
    if not host:
        raise ValueError("URL has no host")
    if host.lower() in BLOCKED_HOSTNAMES:
        raise ValueError(f"Refusing to fetch an internal host ({host}).")

    # If it's already a literal IP, check it directly and skip DNS. Parse it
    # explicitly rather than asking _addr_blocked, which reports "blocked" for
    # anything that isn't an IP at all — that made every hostname fail here as
    # "a non-public address" before it was ever resolved.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _addr_blocked(str(literal)):
            raise ValueError(f"Refusing to fetch a non-public address ({host}).")
        return

    port = p.port or (443 if p.scheme == "https" else 80)
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host {host}: {e}") from e
    if not infos:
        raise ValueError(f"Could not resolve host {host}.")
    for info in infos:
        ip = info[4][0]
        if _addr_blocked(ip):
            raise ValueError(f"URL resolves to a non-public address ({ip}).")
