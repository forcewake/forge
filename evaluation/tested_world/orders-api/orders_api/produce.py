"""Produce the replay batch on stdout-as-JSON (the executor's arm 3)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orders_api import contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_api.produce")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = {
        "schema": "forge.orders.produced-batch/1",
        "dialect": contract.DIALECT,
        "topic": contract.TOPIC,
        "messages": contract.messages(args.count),
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
