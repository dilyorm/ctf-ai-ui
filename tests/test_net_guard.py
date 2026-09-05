"""SSRF guard for connector URLs."""

from __future__ import annotations

import socket

import pytest

from backend import net_guard
from backend.net_guard import assert_public_url


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin DNS so these tests never depend on the machine's resolver."""

    def _install(*ips: str, error: Exception | None = None):
        calls: list[tuple[str, int]] = []

        async def fake_getaddrinfo(host, port, **kwargs):
            calls.append((host, port))
            if error is not None:
                raise error
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips
            ]

        class _Loop:
            getaddrinfo = staticmethod(fake_getaddrinfo)

        monkeypatch.setattr(net_guard.asyncio, "get_event_loop", lambda: _Loop())
        return calls

    return _install


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8090/",
    "http://localhost/x",
    "https://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://metadata.google.internal/",
    "http://10.0.0.5/api",
    "http://192.168.1.1/",
    "http://172.16.0.9/",
    "http://[::1]/",
    "http://0.0.0.0/",
    "ftp://example.com/",                          # bad scheme
    "http:///nohost",
])
async def test_blocks_internal_and_bad(url):
    with pytest.raises(ValueError):
        await assert_public_url(url)


@pytest.mark.parametrize("url", [
    "https://8.8.8.8/",          # public IP literal, no DNS needed
    "http://1.1.1.1/challenges",
])
async def test_allows_public_ip(url):
    await assert_public_url(url)  # should not raise


@pytest.mark.parametrize("url", [
    "https://ctf.uz",
    "https://ctf.dilyor.dev/",
    "http://play.example.org:8080/challenges",
    "https://sub.domain.example.co.uk/api/v1",
])
async def test_allows_public_hostname(url, resolves_to):
    """A hostname must be resolved, not rejected out of hand.

    `_addr_blocked` returns True when its argument isn't a literal IP, so using
    it to test the hostname made *every* domain fail as "a non-public address"
    before DNS was ever consulted — no operator could connect any platform.
    """
    calls = resolves_to("93.184.216.34")
    await assert_public_url(url)
    assert calls, "the hostname should have been resolved"


async def test_hostname_resolving_to_private_ip_is_blocked(resolves_to):
    """The actual protection: DNS pointing at an internal address is refused."""
    resolves_to("10.1.2.3")
    with pytest.raises(ValueError, match="non-public"):
        await assert_public_url("https://internal.example.com/")


async def test_blocks_when_any_answer_is_private(resolves_to):
    """One public answer must not launder a private one."""
    resolves_to("93.184.216.34", "127.0.0.1")
    with pytest.raises(ValueError, match="non-public"):
        await assert_public_url("https://mixed.example.com/")


async def test_unresolvable_host_reports_dns_failure(resolves_to):
    resolves_to(error=socket.gaierror("Name or service not known"))
    with pytest.raises(ValueError, match="[Cc]ould not resolve"):
        await assert_public_url("https://nope.invalid/")
