"""The consumer's side of the shared order-event contract.

``DIALECT`` is a build property: flipping it builds the OLD consumer
(v1 — no ``region`` demanded) while the module's own tests stay green
(the "separately green pipelines, incompatible system" arm).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: The dialect this build REQUIRES (v2 demands ``region``).
DIALECT = "v2"

#: The field sets per dialect (v2 adds ``region``).
DIALECT_FIELDS: dict[str, tuple[str, ...]] = {
    "v1": ("id", "total"),
    "v2": ("id", "total", "region"),
}

#: The contract document's discriminator.
CONTRACT_SCHEMA = "forge.orders.consumer-contract/1"


def document() -> dict[str, object]:
    """This build's declared contract (what the executor compares
    against the frozen referee)."""
    return {
        "schema": CONTRACT_SCHEMA,
        "service": "orders-projection",
        "dialect": DIALECT,
        "required": list(DIALECT_FIELDS[DIALECT]),
    }


def validate(message: dict[str, object]) -> tuple[bool, str]:
    """Whether *message* speaks THIS build's dialect."""
    version = str(message.get("schema_version", ""))
    if version != DIALECT:
        return False, f"message dialect {version!r} is not the required {DIALECT!r}"
    missing = [field for field in DIALECT_FIELDS[DIALECT] if field not in message]
    if missing:
        return False, f"message lacks required fields {missing}"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_projection.contract")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    args.report.write_text(json.dumps(document(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
