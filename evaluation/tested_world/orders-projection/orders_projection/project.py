"""The consumer's baseline projection leg.

The PINNED ledger baseline serves rows in its API's shape — ``api/v3``
rows carry ``region``, ``api/v2`` rows never did — and this build's
projection REQUIRES ``region``. The projection replays through a real
sqlite round trip: rows that lack a required column are not projected
(the honest count, never a faked success).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

#: The API dialects the pinned baseline can serve.
BASELINE_APIS: dict[str, tuple[str, ...]] = {
    "api/v2": ("id", "total"),
    "api/v3": ("id", "total", "region"),
}

#: The columns this build's projection requires of a baseline row.
REQUIRED_COLUMNS: tuple[str, ...] = ("id", "total", "region")


def baseline_row(index: int, api: str) -> dict[str, object]:
    values: dict[str, object] = {
        "id": f"led-{index:04d}",
        "total": 500 + index * 3,
        "region": ("eu", "us", "apac")[index % 3],
    }
    return {field: values[field] for field in BASELINE_APIS[api]}


def project_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    """Replay *rows* into a fresh sqlite projection table."""
    conn = sqlite3.connect(":memory:")
    try:
        with conn:
            conn.execute(
                "CREATE TABLE projection (id TEXT PRIMARY KEY, total INTEGER,"
                " region TEXT, projected_from TEXT)"
            )
        projected = 0
        missing: set[str] = set()
        for row in rows:
            absent = [column for column in REQUIRED_COLUMNS if column not in row]
            if absent:
                missing.update(absent)
                continue
            with conn:
                conn.execute(
                    "INSERT INTO projection (id, total, region, projected_from)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        str(row["id"]),
                        int(row["total"]),  # type: ignore[arg-type]
                        str(row["region"]),
                        "ledger-baseline",
                    ),
                )
            projected += 1
        stored = int(conn.execute("SELECT count(*) FROM projection").fetchone()[0])
    finally:
        conn.close()
    return {
        "rows": len(rows),
        "projected": projected,
        "stored": stored,
        "missing_columns": sorted(missing),
        "required_columns": list(REQUIRED_COLUMNS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_projection.project")
    parser.add_argument("--api", choices=sorted(BASELINE_APIS), default="api/v3")
    parser.add_argument("--rows", type=int, default=15)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = [baseline_row(index, args.api) for index in range(1, args.rows + 1)]
    outcome = {
        "schema": "forge.orders.baseline-projection/1",
        "served_api": args.api,
        **project_rows(rows),
    }
    args.report.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    return 0 if outcome["projected"] == outcome["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
