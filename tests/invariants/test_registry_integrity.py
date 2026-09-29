"""The Invariant Suite registry cannot lose its evidence silently."""

from __future__ import annotations

from pathlib import Path

from tests.invariants.registry import INVARIANTS

REPO = Path(__file__).resolve().parents[2]


def test_i1_to_i8_are_all_registered():
    assert {f"I{n}" for n in range(1, 9)} <= {inv.ident for inv in INVARIANTS}


def test_every_invariant_names_evidence():
    assert all(inv.files for inv in INVARIANTS)


def test_every_registered_file_exists():
    missing = [f for inv in INVARIANTS for f in inv.files if not (REPO / f).is_file()]
    assert missing == []
