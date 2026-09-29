"""Every edge-case register entry still names a real test or file."""

from __future__ import annotations

import re

import pytest

from tests._nodeids import REPO, nodeid_exists

REGISTER = REPO / "docs" / "reference" / "edge-cases.md"
ROW = re.compile(r"^\| (?P<ident>(?:EC|OP)-\d{2}) \|.*\| `(?P<target>[^`]+)` \|$")


def _rows() -> list[tuple[str, str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if match := ROW.match(line):
            rows.append((match["ident"], match["target"]))
    return rows


def test_the_register_is_not_empty():
    assert len([ident for ident, _ in _rows() if ident.startswith("EC-")]) >= 10


@pytest.mark.parametrize(("ident", "target"), _rows(), ids=[i for i, _ in _rows()])
def test_every_entry_points_at_something_real(ident: str, target: str):
    if ident.startswith("EC-"):
        assert nodeid_exists(target), f"{ident}: pinning test {target} is gone"
    else:
        assert (REPO / target).exists(), f"{ident}: reference {target} is gone"


def test_the_node_id_check_rejects_a_missing_test():
    assert not nodeid_exists("tests/test_bot_loop.py::test_there_is_no_such_test")
