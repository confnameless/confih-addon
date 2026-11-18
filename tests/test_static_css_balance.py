"""A stylesheet with unbalanced braces is silently half-applied.

Slicing a block out of `confih.css` once left two stray `}` behind, which cost
`.nav-indicator` its whole rule and made the nav look broken without any test
noticing. Braces live inside comments rarely enough that counting them is a
fair guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


@pytest.mark.parametrize("css_file", sorted(STATIC.glob("*.css")), ids=lambda p: p.name)
def test_stylesheet_braces_balance(css_file: Path) -> None:
    text = _strip_comments(css_file.read_text(encoding="utf-8"))
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            assert depth >= 0, f"{css_file.name}: stray closing brace near offset {index}"
    assert depth == 0, f"{css_file.name}: {depth} unclosed block(s)"


def test_nav_indicator_rule_still_styles_the_pill() -> None:
    """The sliding pill must keep its own paint properties."""
    css = (STATIC / "confih.css").read_text(encoding="utf-8")
    match = re.search(r"\.nav-indicator\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert match, ".nav-indicator rule disappeared from confih.css"
    body = match.group(1)
    for prop in ("position", "background", "border", "box-shadow", "transition"):
        assert prop in body, f".nav-indicator lost its {prop}"
