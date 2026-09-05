"""Regression tests for scraping a sign-in URL out of CLI/TUI output.

Two failure modes, both seen against the real CLIs:

* Matching a single line truncates a URL that the terminal hard-wrapped, so the
  browser gets a request missing half its scopes and the exchange 400s.
* Joining every line (stripping newlines and spaces) fixes the wrap but glues on
  whatever the CLI printed next — a box border, or the "Paste code here" prompt.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fcntl", reason="connect_manager is POSIX-only (PTY sign-in)")

from backend.connect_manager import _AGY_URL, _CLAUDE_URL, scrape_url  # noqa: E402

# Real `agy` output: the URL is drawn inside a box, wrapped across two lines,
# with the continuation padded and a border row immediately after.
AGY_OUTPUT = """
 Signing in...Open the URL below in your browser:
 ────────────────────────────────────────────────────────
 https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-x.apps.googleusercontent.com&code_challenge=ABC&code_challenge_method=S256&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+https%
 3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email+openid&state=STATEVALUE
 ────────────────────────────────────────────────────────

 After authenticating, copy the code displayed in the browser and paste it below:
"""

# Real `claude auth login` output: URL on one line, prompt on a later line.
CLAUDE_OUTPUT = """Opening browser to sign in…
If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&scope=user%3Ainference&state=5VL6j4BiOCQ7j0

Paste code here if prompted >
"""


def test_agy_url_rejoins_the_wrap_and_stops_at_the_border():
    url = scrape_url(AGY_OUTPUT, _AGY_URL)
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    # The continuation line must be absorbed: without it the scope list is cut
    # in half and `state` is missing entirely.
    assert url.endswith("&state=STATEVALUE")
    assert "userinfo.email" in url
    assert "openid" in url
    # …and the box border must not be.
    assert "─" not in url


def test_claude_url_does_not_absorb_the_following_prompt():
    url = scrape_url(CLAUDE_OUTPUT, _CLAUDE_URL)
    assert url.endswith("&state=5VL6j4BiOCQ7j0")
    assert "Paste" not in url and "paste" not in url


def test_no_url_returns_empty():
    assert scrape_url("Welcome to the CLI. You are not signed in.", _AGY_URL) == ""


def test_partial_output_yields_only_what_arrived():
    """A half-received URL still scrapes — the caller debounces before using it."""
    partial = AGY_OUTPUT[: AGY_OUTPUT.index("&scope=")]
    url = scrape_url(partial, _AGY_URL)
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "&scope=" not in url
