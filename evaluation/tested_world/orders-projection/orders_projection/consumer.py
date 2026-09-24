"""The idempotent consumer — a real subprocess dialing the broker over TCP.

Three sqlite tables in ONE database make the delivery semantics REAL
transaction boundaries: the projected effect and the idempotency key
commit TOGETHER; the acknowledgement is a separate later write. A
duplicate delivery (injected at the socket by the fake broker) must
land as ``duplicate-no-effect`` — exactly one business effect per
message regardless of delivery count, asserted from the consumer's own
durable rows at exit.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from pathlib import Path

from orders_projection import contract


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_messages"
            " (message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS projected_orders"
            " (message_id TEXT NOT NULL, order_id TEXT NOT NULL,"
            " total INTEGER NOT NULL, region TEXT,"
            " PRIMARY KEY (message_id, order_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS message_acks"
            " (message_id TEXT PRIMARY KEY, acked_at TEXT NOT NULL)"
        )


def handle(conn: sqlite3.Connection, message: dict[str, object]) -> str:
    """Apply ONE delivery idempotently; return what actually happened."""
    accepted, reason = contract.validate(message)
    message_id = str(message.get("id", ""))
    if not accepted:
        return f"rejected:{reason}"
    with conn:  # state persistence: effect + idempotency key, together
        seen = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if seen is None:
            conn.execute(
                "INSERT INTO processed_messages (message_id, processed_at) VALUES (?, 'now')",
                (message_id,),
            )
            conn.execute(
                "INSERT INTO projected_orders (message_id, order_id, total, region)"
                " VALUES (?, ?, ?, ?)",
                (
                    message_id,
                    message_id,
                    int(message.get("total") or 0),  # type: ignore[union-attr]
                    str(message.get("region") or ""),
                ),
            )
    return "effect-applied" if seen is None else "duplicate-no-effect"


def record_ack(conn: sqlite3.Connection, message_id: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO message_acks (message_id, acked_at) VALUES (?, 'now')",
            (message_id,),
        )


def summary(conn: sqlite3.Connection, deliveries: list[dict[str, object]]) -> dict[str, object]:
    ids = sorted({str(entry["id"]) for entry in deliveries})
    per_message = {
        message_id: {
            "deliveries": sum(1 for entry in deliveries if str(entry["id"]) == message_id),
            "effects": int(
                conn.execute(
                    "SELECT count(*) FROM projected_orders WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
            ),
            "acks": int(
                conn.execute(
                    "SELECT count(*) FROM message_acks WHERE message_id = ?", (message_id,)
                ).fetchone()[0]
            ),
        }
        for message_id in ids
    }
    return {
        "schema": "forge.orders.consumer-run/1",
        "dialect": contract.DIALECT,
        "deliveries": len(deliveries),
        "per_message": per_message,
        "exactly_once_all": bool(per_message)
        and all(entry["effects"] == 1 for entry in per_message.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orders_projection.consumer")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    deliveries: list[dict[str, object]] = []
    try:
        ensure_schema(conn)
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
            stream = sock.makefile("rwb")
            stream.write(
                (json.dumps({"op": "sub", "topic": "orders.events.order-created"}) + "\n").encode()
            )
            stream.flush()
            while True:
                line = stream.readline()
                if not line:
                    break
                frame = json.loads(line)
                if frame.get("op") == "delivery":
                    message = dict(frame.get("message", {}))
                    outcome = handle(conn, message)
                    deliveries.append({"id": message.get("id", ""), "outcome": outcome})
                    stream.write(
                        (
                            json.dumps(
                                {
                                    "op": "ack",
                                    "id": message.get("id", ""),
                                    "delivery_seq": frame.get("delivery_seq", 0),
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    stream.flush()
                    if not outcome.startswith("rejected"):
                        # the acknowledgement is a SEPARATE durable write after
                        # the effect — the broker's redelivery window lives here.
                        record_ack(conn, str(message.get("id", "")))
                elif frame.get("op") == "end":
                    break
        report = summary(conn, deliveries)
    finally:
        conn.close()
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["exactly_once_all"] else 1


if __name__ == "__main__":
    sys.exit(main())
