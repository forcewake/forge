"""The consumer's OWN tests (the unit pipeline).

Parametric over the declared dialect: green for a v1 build and a v2
build alike — the separately-green-pipelines trap the verified executor
exists to catch. Includes the idempotency unit (duplicate delivery ->
no second effect) and the schema-upgrade unit (seeded data preserved).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from orders_projection import contract, project, schema

_TESTS: list[Callable[[], object]] = []


def _test(fn: Callable[[], object]) -> Callable[[], object]:
    _TESTS.append(fn)
    return fn


def _message(dialect: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "ord-9001",
        "total": 1204,
        "region": "eu",
        "schema_version": dialect,
    }
    fields = contract.DIALECT_FIELDS[dialect]
    message = {field: values[field] for field in fields} | {"schema_version": dialect}
    message.update(overrides)
    return message


@_test
def the_declared_dialects_messages_are_accepted() -> None:
    accepted, reason = contract.validate(_message(contract.DIALECT))
    assert accepted, reason


@_test
def the_other_dialects_messages_are_rejected() -> None:
    other = "v1" if contract.DIALECT == "v2" else "v2"
    accepted, _reason = contract.validate(_message(other))
    assert not accepted, f"this {contract.DIALECT} build must reject a {other} message"


@_test
def messages_missing_required_fields_are_rejected() -> None:
    message = _message(contract.DIALECT)
    message.pop(contract.DIALECT_FIELDS[contract.DIALECT][-1])
    accepted, reason = contract.validate(message)
    assert not accepted, reason


@_test
def a_duplicate_delivery_applies_no_second_effect() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        consumer_schema(conn)
        from orders_projection import consumer

        message = _message(contract.DIALECT)
        first = consumer.handle(conn, message)
        duplicate = consumer.handle(conn, dict(message))
        assert first == "effect-applied", first
        assert duplicate == "duplicate-no-effect", duplicate
        effects = int(conn.execute("SELECT count(*) FROM projected_orders").fetchone()[0])
        assert effects == 1, effects
    finally:
        conn.close()


@_test
def the_upgrade_preserves_seeded_rows() -> None:
    with tempfile.TemporaryDirectory() as directory:
        outcome = schema.run_upgrade(Path(directory) / "upgrade.db", seed=5)
        assert outcome["preserved"], outcome["detail"]
        assert outcome["schema_advanced"], "the ladder must advance"
        assert outcome["post_upgrade_write"], "new-shaped writes must land"
        assert outcome["seed_rows"] == 5


@_test
def the_projection_requires_the_new_baseline_shape() -> None:
    v3 = project.project_rows([project.baseline_row(index, "api/v3") for index in range(1, 4)])
    assert v3["projected"] == v3["rows"] == 3, v3
    v2 = project.project_rows([project.baseline_row(index, "api/v2") for index in range(1, 4)])
    assert v2["projected"] == 0, "api/v2 rows must not silently project"


def consumer_schema(conn: sqlite3.Connection) -> None:
    from orders_projection import consumer

    consumer.ensure_schema(conn)


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
    parser = argparse.ArgumentParser(prog="orders_projection.selftest")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    results = run_tests()
    payload = {
        "schema": "forge.orders.selftest/1",
        "package": "orders-projection",
        "dialect": contract.DIALECT,
        "tests": results,
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if all(entry["status"] == "passed" for entry in results) else 1


if __name__ == "__main__":
    sys.exit(main())
