"""The producer's OWN contract tests (the unit pipeline).

Parametric over the declared dialect: the same suite is green for a v1
build and a v2 build — which is the point. Green unit pipelines say
nothing about whether two separately green builds compose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from orders_api import contract

_TESTS: list[Callable[[], object]] = []


def _test(fn: Callable[[], object]) -> Callable[[], object]:
    _TESTS.append(fn)
    return fn


def _other_dialect() -> str:
    return "v1" if contract.DIALECT == "v2" else "v2"


@_test
def emitted_messages_carry_the_declared_dialects_fields() -> None:
    message = contract.emit(1)
    expected = set(contract.DIALECT_FIELDS[contract.DIALECT]) | {"schema_version"}
    assert expected <= set(message), f"{sorted(message)} lacks {sorted(expected - set(message))}"
    assert message["schema_version"] == contract.DIALECT


@_test
def emitted_ids_are_unique_and_totals_positive() -> None:
    batch = contract.messages(16)
    ids = [str(message["id"]) for message in batch]
    assert len(set(ids)) == len(ids), "duplicate order ids in one batch"
    assert all(int(message["total"]) > 0 for message in batch)


@_test
def emission_is_deterministic() -> None:
    assert contract.messages(6) == contract.messages(6)


@_test
def the_declared_document_matches_the_emitted_shape() -> None:
    document = contract.document()
    assert document["dialect"] == contract.DIALECT
    assert tuple(document["fields"]) == contract.DIALECT_FIELDS[contract.DIALECT]  # type: ignore[union-attr]


@_test
def the_other_dialect_is_not_silently_emitted() -> None:
    other = _other_dialect()
    only_other = set(contract.DIALECT_FIELDS[other]) - set(
        contract.DIALECT_FIELDS[contract.DIALECT]
    )
    if only_other:
        message = contract.emit(2)
        assert not (only_other & set(message)), f"this build must not emit {only_other}"


def run_tests() -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for fn in _TESTS:
        try:
            fn()
        except AssertionError as error:
            results.append({"name": fn.__name__, "status": "failed", "detail": str(error)})
        else:
            results.append({"name": fn.__name__, "status": "passed"})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_api.selftest")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    results = run_tests()
    payload = {
        "schema": "forge.orders.selftest/1",
        "package": "orders-api",
        "dialect": contract.DIALECT,
        "tests": results,
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if all(entry["status"] == "passed" for entry in results) else 1


if __name__ == "__main__":
    sys.exit(main())
