"""Write-once private historical row evidence with exact native-value digests."""

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, BinaryIO

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from forge.store.schema.legacy import CutoverRefused
from forge.store.schema.signature import inspect_schema

MAX_FILE = 2 * 1024**3


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def private_directory(path: Path) -> None:
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise CutoverRefused("backup directory must be outside a repository")
    if path.is_symlink():
        raise CutoverRefused("backup directory cannot be a symlink")
    path.mkdir(mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CutoverRefused("backup directory is not private")


def new_file(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "wb")


def sync_file(stream: BinaryIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def file_digest(path: Path) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest, length = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise CutoverRefused("archive files must be private regular files")
        while chunk := stream.read(1024 * 1024):
            length += len(chunk)
            if length > MAX_FILE:
                raise CutoverRefused("archive file exceeds the supported bound")
            digest.update(chunk)
    return digest.hexdigest(), length


def cell(value: object) -> object:
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode()]
    raise CutoverRefused("unsupported native archive value; source remains untouched")


async def _rows(
    connection: AsyncConnection, name: str, columns: list[str], target: BinaryIO | None
) -> dict[str, Any]:
    quote = connection.dialect.identifier_preparer.quote
    expressions = [
        quote(column) + ("::text" if connection.dialect.name == "postgresql" else "")
        for column in columns
    ]
    order = [
        value + (' COLLATE "C"' if connection.dialect.name == "postgresql" else "")
        for value in expressions
    ]
    query = (
        "SELECT " + ",".join(expressions) + " FROM " + quote(name) + " ORDER BY " + ",".join(order)
    )
    digest, count, size = hashlib.sha256(), 0, 0
    stream = await connection.stream(text(query))
    async for row in stream:
        encoded = canonical([cell(value) for value in row]) + b"\n"
        count, size = count + 1, size + len(encoded)
        if len(encoded) > 32 * 1024**2 or size > MAX_FILE:
            raise CutoverRefused("historical table exceeds the supported archive bound")
        digest.update(encoded)
        if target is not None:
            target.write(encoded)
    return {"columns": columns, "rows": count, "bytes": size, "sha256": digest.hexdigest()}


async def snapshot(connection: AsyncConnection, directory: Path | None = None) -> dict[str, Any]:
    if connection.dialect.name == "postgresql":
        await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        await connection.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
        await connection.execute(text("SET LOCAL bytea_output = 'hex'"))
        await connection.execute(text("SET LOCAL extra_float_digits = 3"))
    signature = await connection.run_sync(inspect_schema)
    tables = {}
    for name, shape in signature["tables"].items():
        columns = [column["name"] for column in shape["columns"]]
        filename = hashlib.sha256(name.encode()).hexdigest() + ".jsonl"
        if directory is None:
            facts = await _rows(connection, name, columns, None)
        else:
            with new_file(directory / filename) as output:
                facts = await _rows(connection, name, columns, output)
                sync_file(output)
        tables[name] = {**facts, "file": filename}
    sequences = {}
    if connection.dialect.name == "postgresql":
        names = await connection.run_sync(lambda sync: inspect(sync).get_sequence_names())
        quote = connection.dialect.identifier_preparer.quote
        for name in names:
            row = (
                await connection.execute(text("SELECT last_value, is_called FROM " + quote(name)))
            ).one()
            sequences[name] = [cell(value) for value in row]
    return {"schema": signature, "tables": tables, "sequences": sequences}


@dataclass(frozen=True)
class ArchiveReceipt:
    manifest_sha256: str
    backup_sha256: str
    source_snapshot_sha256: str


def seal(
    directory: Path, facts: dict[str, Any], backup: Path, declarations: dict[str, bool]
) -> ArchiveReceipt:
    for table in facts["tables"].values():
        actual, size = file_digest(directory / table["file"])
        if actual != table["sha256"] or size != table["bytes"]:
            raise CutoverRefused("historical export readback differs from its source digest")
    backup_sha, backup_size = file_digest(backup)
    snapshot_sha = hashlib.sha256(canonical(facts)).hexdigest()
    manifest = {
        "format": "forge-history/1",
        "snapshot": facts,
        "snapshot_sha256": snapshot_sha,
        "backup": {"file": backup.name, "sha256": backup_sha, "bytes": backup_size},
        "declarations": declarations,
    }
    raw = canonical(manifest) + b"\n"
    manifest_sha = hashlib.sha256(raw).hexdigest()
    with new_file(directory / "manifest.json") as stream:
        stream.write(raw)
        sync_file(stream)
    with new_file(directory / "manifest.sha256") as stream:
        stream.write((manifest_sha + "\n").encode())
        sync_file(stream)
    for path in directory.iterdir():
        path.chmod(0o400)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ArchiveReceipt(manifest_sha, backup_sha, snapshot_sha)
