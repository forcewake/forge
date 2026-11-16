"""Every ADR that ADR-0035 supersedes or amends says so at the top."""

from __future__ import annotations

from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[1] / "docs" / "adr"
LINK = "[ADR-0035](0035-simplification-program.md)"
EXPECTED = {
    "Superseded by": ("0019", "0021", "0027", "0029", "0030", "0031", "0033", "0034"),
    "Amended by": ("0002", "0013", "0015", "0020", "0022", "0023", "0025", "0028", "0032"),
    "Superseded on main by": ("0024",),
}
CASES = [(verb, number) for verb, numbers in EXPECTED.items() for number in numbers]


def _adr(number: str) -> Path:
    (path,) = ADR_DIR.glob(f"{number}-*.md")
    return path


@pytest.mark.parametrize(("verb", "number"), CASES, ids=[n for _, n in CASES])
def test_affected_adr_carries_its_banner(verb: str, number: str):
    head = "\n".join(_adr(number).read_text(encoding="utf-8").splitlines()[:6])
    blockquote = f"> **{verb} {LINK}** (2026-09-29)."
    assert blockquote in head or (verb.lower() in head.lower() and LINK in head)
