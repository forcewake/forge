"""Every edge-case register entry still names a real test or file."""

from __future__ import annotations

import re

import pytest

from tests._nodeids import REPO, nodeid_exists

REGISTER = REPO / "docs" / "reference" / "edge-cases.md"
ANY_ROW = re.compile(r"^\|\s*(?:EC|OP)-\d+\s*\|")
ROW = re.compile(r"^\| (?P<ident>(?:EC|OP)-\d{2,}) \|(?P<rest>.*)\|$")
PIN = re.compile(r"`([^`]+)`")


def _rows() -> list[tuple[str, list[str]]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if match := ROW.match(line.rstrip()):
            last_cell = match["rest"].rsplit("|", 1)[-1]
            rows.append((match["ident"], PIN.findall(last_cell)))
    return rows


PINS = [(ident, target) for ident, targets in _rows() for target in targets]


def test_every_register_row_is_parsed():
    lines = [
        line for line in REGISTER.read_text(encoding="utf-8").splitlines() if ANY_ROW.match(line)
    ]
    rows = _rows()
    assert len(rows) == len(lines), "a register row does not have the expected shape"
    assert all(targets for _, targets in rows), "a register row names no pin"
    assert len({ident for ident, _ in rows}) == len(rows), "a register id is used twice"


def test_the_register_is_not_empty():
    assert len([ident for ident, _ in _rows() if ident.startswith("EC-")]) >= 11


@pytest.mark.parametrize(("ident", "target"), PINS, ids=[f"{i}-{t}" for i, t in PINS])
def test_every_pin_points_at_something_real(ident: str, target: str):
    if ident.startswith("EC-"):
        assert "::" in target, f"{ident}: {target} must name a test, not a file"
        assert nodeid_exists(target), f"{ident}: pinning test {target} is gone"
    else:
        assert (REPO / target).exists(), f"{ident}: reference {target} is gone"


def test_the_node_id_check():
    own = "tests/test_edge_case_register.py"
    assert nodeid_exists(f"{own}::test_the_register_is_not_empty")
    assert nodeid_exists(f"{own}::test_every_pin_points_at_something_real[EC-01-x]")
    assert not nodeid_exists(f"{own}::test_there_is_no_such_test")
