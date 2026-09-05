"""CLI solvers must pass flag values their CLI actually accepts.

The Grok solver shipped `--output-format text`. That value does not exist —
`grok` accepts plain|json|streaming-json|streaming-messages-json and exits 2 —
so every Grok solver died immediately with an unparsed usage error.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SOLVERS = pathlib.Path(__file__).resolve().parents[1] / "backend" / "agents"

# Values each CLI documents for --output-format (from `<cli> --help`).
VALID_OUTPUT_FORMATS = {
    "grok_solver.py": {"plain", "json", "streaming-json", "streaming-messages-json"},
    "antigravity_solver.py": {"text", "json", "stream-json"},
}


@pytest.mark.parametrize(("filename", "allowed"), sorted(VALID_OUTPUT_FORMATS.items()))
def test_output_format_values_are_accepted_by_the_cli(filename, allowed):
    source = (SOLVERS / filename).read_text(encoding="utf-8")
    used = set(re.findall(r"--output-format\s+([a-z-]+)", source))
    assert used, f"{filename} should pass --output-format"
    bad = used - allowed
    assert not bad, (
        f"{filename} passes --output-format {bad}, which its CLI rejects; "
        f"allowed: {sorted(allowed)}"
    )


def test_grok_uses_plain_not_text():
    source = (SOLVERS / "grok_solver.py").read_text(encoding="utf-8")
    assert "--output-format plain" in source
    assert "--output-format text" not in source
