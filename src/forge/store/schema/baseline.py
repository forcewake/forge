"""One frozen baseline shared by explicit initialization and Alembic."""

import hashlib
from importlib.resources import files
import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Connection, DateTime, bindparam, text

REVISION = "10000001"


def document() -> dict[str, Any]:
    raw = files("forge.store.schema").joinpath("baseline.json").read_bytes()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or parsed.get("revision") != REVISION:
        raise RuntimeError("packaged baseline is invalid")
    return cast(dict[str, Any], parsed)


def digest() -> str:
    return hashlib.sha256(
        files("forge.store.schema").joinpath("baseline.json").read_bytes()
    ).hexdigest()


def apply(
    connection: Connection, *, archive_sha256: str | None = None, backup_sha256: str | None = None
) -> None:
    dialect = connection.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError("unsupported database dialect")
    for statement in document()["dialects"][dialect]["ddl"]:
        connection.exec_driver_sql(statement)
    connection.execute(
        text(
            "INSERT INTO schema_installation (id, installation_id, baseline_sha256, archive_sha256, backup_sha256, installed_at) VALUES (1, :identity, :baseline, :archive, :backup, :now)"
        ).bindparams(bindparam("now", type_=DateTime(timezone=True))),
        {
            "identity": uuid4().hex,
            "baseline": digest(),
            "archive": archive_sha256,
            "backup": backup_sha256,
            "now": datetime.now(timezone.utc),
        },
    )
