"""The producer's side of the shared order-event contract.

The dialect this build EMITS is a build property (``DIALECT``): flipping
it builds the OLD producer (v1 — no ``region``) while the module's own
tests stay green, which is exactly the "separately green pipelines,
incompatible system" combination the verified executor exists to catch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: The dialect this build emits (v2 widens the order event with region).
DIALECT = "v2"

#: The field sets per dialect (v2 adds ``region``).
DIALECT_FIELDS: dict[str, tuple[str, ...]] = {
    "v1": ("id", "total"),
    "v2": ("id", "total", "region"),
}

#: The topic the producer publishes on.
TOPIC = "orders.events.order-created"

#: The contract document's discriminator.
CONTRACT_SCHEMA = "forge.orders.producer-contract/1"

_REGIONS = ("eu", "us", "apac")


def document() -> dict[str, object]:
    """This build's declared contract (what the executor compares
    against the frozen referee)."""
    return {
        "schema": CONTRACT_SCHEMA,
        "service": "orders-api",
        "topic": TOPIC,
        "dialect": DIALECT,
        "fields": list(DIALECT_FIELDS[DIALECT]),
    }


def emit(index: int) -> dict[str, object]:
    """One deterministic order-created event in THIS build's dialect."""
    values: dict[str, object] = {
        "id": f"ord-{index:04d}",
        "total": 1000 + index * 7,
        "region": _REGIONS[index % len(_REGIONS)],
        "schema_version": DIALECT,
    }
    fields = DIALECT_FIELDS[DIALECT]
    return {field: values[field] for field in fields} | {"schema_version": DIALECT}


def messages(count: int) -> list[dict[str, object]]:
    return [emit(index) for index in range(1, count + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_api.contract")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    args.report.write_text(json.dumps(document(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
