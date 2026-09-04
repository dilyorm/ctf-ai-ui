"""SSRF guard for connector URLs."""

from __future__ import annotations

import pytest

from backend.net_guard import assert_public_url


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
