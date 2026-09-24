"""The projection's schema ladder — the tested artifact's OWN migration
code (the executor's db-upgrade leg runs THIS, not forge's twin).

v1 is the pinned baseline schema (no ``region``); v2 adds ``region``
with a backfill default. Upgrades verify from a SEEDED baseline — an
empty schema proves nothing — under the canary's fingerprint
discipline: per-table row count plus sha256 over the ordered row
identity strings, compared across the upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

#: The schema ladder: version -> the statements that move to it.
MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "1",
        (
            "CREATE TABLE _schema_version (version TEXT PRIMARY KEY)",
            "CREATE TABLE orders (id TEXT PRIMARY KEY, total INTEGER NOT NULL)",
        ),
    ),
    (
        "2",
        ("ALTER TABLE orders ADD COLUMN region TEXT NOT NULL DEFAULT 'eu'",),
    ),
)

#: (table, identity-expression) for the preservation fingerprint — only
#: the columns that must SURVIVE enter the identity (a backfilled
#: default is not data loss).
FINGERPRINTS: tuple[tuple[str, str], ...] = (("orders", "id || '|' || total"),)


def _apply_migration(conn: sqlite3.Connection, version: str, statements: tuple[str, ...]) -> None:
    with conn:  # one transaction per migration: DDL + version stamp land together
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT OR REPLACE INTO _schema_version (version) VALUES (?)", (version,))


def fingerprint(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Per table: ``<table> <count> <sha256>`` over ordered row identities."""
    rows: list[str] = []
    for table, identity in FINGERPRINTS:
        count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        identities = [
            str(row[0]) for row in conn.execute(f"SELECT {identity} AS x FROM {table} ORDER BY x")
        ]
        digest = hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()
        rows.append(f"{table} {count} {digest}")
    return tuple(rows)


def seed_rows(count: int) -> list[tuple[str, int]]:
    """Deterministic synthetic rows at the N-1 schema."""
    return [(f"ord-{index:03d}", 1000 + index * 7) for index in range(1, count + 1)]


def run_upgrade(
    db_path: Path, *, seed: int, baseline: str = "1", target: str = "2"
) -> dict[str, object]:
    """Execute a REAL upgrade between two schema versions on *db_path*.

    Seeds the baseline schema with *seed* rows, fingerprints them,
    applies the ladder, fingerprints again — preservation means counts
    AND digests equal. The schema must genuinely ADVANCE and the new
    schema must accept new-shaped writes.
    """
    seeds = seed_rows(seed)
    ladder = tuple(
        (version, statements) for version, statements in MIGRATIONS if baseline < version <= target
    )
    conn = sqlite3.connect(db_path)
    try:
        base = next((v, s) for v, s in MIGRATIONS if v == baseline)
        _apply_migration(conn, base[0], base[1])
        with conn:
            conn.executemany("INSERT INTO orders (id, total) VALUES (?, ?)", seeds)
        before = fingerprint(conn)
        for version, statements in ladder:
            _apply_migration(conn, version, statements)
        after = fingerprint(conn)
        versions = {str(row[0]) for row in conn.execute("SELECT version FROM _schema_version")}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(orders)")}
        schema_advanced = target in versions and "region" in columns
        post_upgrade_write = True
        try:
            with conn:
                conn.execute("INSERT INTO orders (id, total, region) VALUES ('ord-post', 1, 'us')")
        except sqlite3.Error:
            post_upgrade_write = False
    finally:
        conn.close()
    preserved = before == after
    if not preserved:
        detail = "the upgrade did NOT preserve the seeded rows (fingerprint changed)"
    elif not schema_advanced:
        detail = f"the schema did not advance to {target}"
    elif not post_upgrade_write:
        detail = "the upgraded schema rejected a new-shaped write"
    else:
        detail = (
            f"upgrade {baseline}->{target} preserved every seeded row ({len(seeds)} rows"
            " — counts and sha256 fingerprints equal) and accepts new-shaped writes"
        )
    return {
        "schema": "forge.orders.schema-upgrade/1",
        "baseline_schema": baseline,
        "target_schema": target,
        "seed_rows": len(seeds),
        "baseline_fingerprint": list(before),
        "target_fingerprint": list(after),
        "preserved": preserved,
        "schema_advanced": schema_advanced,
        "post_upgrade_write": post_upgrade_write,
        "detail": detail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_projection.schema")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=25)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    outcome = run_upgrade(args.db, seed=args.seed)
    args.report.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    ok = outcome["preserved"] and outcome["schema_advanced"] and outcome["post_upgrade_write"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
