"""Explicit schema commands exercise real databases and redact connection failures."""

from argparse import Namespace
from contextlib import closing
from pathlib import Path
import sqlite3

from alembic import command
import pytest

from forge.store.schema.__main__ import engine, execute, main
from forge.store.schema.migration import config
from tests.schema_fixture import databases as databases
from tests.schema_fixture import legacy


def test_fresh_cli_initialization_and_check_are_explicit_and_refuse_a_second_install(tmp_path):
    path = tmp_path / "new.db"
    url = "sqlite+aiosqlite:///" + str(path)
    assert main(["check", "--database-url", url]) == 1
    assert not path.exists()
    assert main(["init", "--database-url", url]) == 0
    assert main(["check", "--database-url", url]) == 0
    assert main(["init", "--database-url", url]) == 1


def test_cli_refusal_never_echoes_passwords_or_unknown_connection_details(tmp_path, capsys):
    assert main(["check", "--database-url", "bad://owner:private-password@host/secret-db"]) == 1
    assert main(["init", "--database-url", ""]) == 1
    message = capsys.readouterr().err
    assert "private-password" not in message and "secret-db" not in message
    assert "refused" in message and "required" in message


@pytest.mark.parametrize("kind", ("export-history", "migrate"))
async def test_cli_offline_commands_emit_only_archive_digests(databases, kind, capsys):
    await legacy(databases.source)
    args = Namespace(
        command=kind,
        source_url=databases.source.url.render_as_string(hide_password=False),
        verification_url=databases.verification.url.render_as_string(hide_password=False),
        database_url=databases.destination.url.render_as_string(hide_password=False),
        output=databases.directory / "archive",
        old_service_stopped=True,
        executions_stopped=True,
    )
    await execute(args)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    for line, prefix in zip(lines, ("archive_sha256=", "backup_sha256="), strict=True):
        assert line.startswith(prefix) and len(line.removeprefix(prefix)) == 64


def test_packaged_single_alembic_baseline_installs_fresh_and_never_downgrades(tmp_path):
    path = tmp_path / "literal%name.db"
    url = "sqlite+aiosqlite:///" + str(path)
    command.upgrade(config(url), "head")
    assert main(["check", "--database-url", url]) == 0
    with pytest.raises(RuntimeError, match="backup"):
        command.downgrade(config(url), "base")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "10000001",
        )


async def test_cli_engine_does_not_read_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "bad://ambient-secret")
    selected = engine("sqlite+aiosqlite:///" + str(Path(tmp_path) / "explicit.db"))
    try:
        assert selected.url.database.endswith("explicit.db")
    finally:
        await selected.dispose()


def test_packaged_alembic_refuses_non_table_owner_objects_before_ddl(tmp_path):
    path = tmp_path / "owner.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE VIEW owner_view AS SELECT 7 AS id")
        connection.commit()
    with pytest.raises(RuntimeError, match="empty"):
        command.upgrade(config("sqlite+aiosqlite:///" + str(path)), "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("owner_view",)]
