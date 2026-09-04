"""Category playbook injection."""

from __future__ import annotations

from backend.knowledge import tactics_for
from backend.prompts import ChallengeMeta, build_prompt


def test_known_categories_return_playbooks():
    for cat in ("web", "pwn", "crypto", "rev", "forensics", "misc"):
        block = tactics_for(cat)
        assert block.startswith("## ") and "playbook" in block.lower()


def test_aliases_map_to_canonical():
    assert "Pwn playbook" in tactics_for("Binary Exploitation")
    assert "Rev playbook" in tactics_for("reversing")
    assert "Forensics playbook" in tactics_for("stego")


def test_unknown_category_is_empty():
    assert tactics_for("underwater-basketweaving") == ""
    assert tactics_for("") == ""


def test_tag_fallback_when_category_unknown():
    assert "Crypto playbook" in tactics_for("", tags=["rsa", "crypto"])


def test_build_prompt_includes_playbook():
    meta = ChallengeMeta(name="x", category="web", value=100, description="d")
    prompt = build_prompt(meta, [], "amd64", has_named_tools=True)
    assert "Web playbook" in prompt
