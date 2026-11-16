"""Cold installation checks the packaged schema and never bootstraps on startup."""

from forge.store.schema.__main__ import main as schema_main


def test_check_refuses_an_empty_database_without_creating_it(tmp_path):
    path = tmp_path / "empty.db"
    url = "sqlite+aiosqlite:///" + str(path)
    assert schema_main(["check", "--database-url", url]) == 1
    assert not path.exists()


def test_explicit_init_then_check_round_trips_and_a_second_init_refuses(tmp_path):
    path = tmp_path / "installed.db"
    url = "sqlite+aiosqlite:///" + str(path)
    assert schema_main(["init", "--database-url", url]) == 0
    assert schema_main(["check", "--database-url", url]) == 0
    assert schema_main(["init", "--database-url", url]) == 1


def test_a_historical_revision_is_refused_and_left_unchanged(tmp_path):
    import sqlite3

    path = tmp_path / "historical.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('032')")
        connection.execute("CREATE TABLE historical_evidence (body TEXT NOT NULL)")
        connection.execute("INSERT INTO historical_evidence VALUES ('immutable-old-evidence')")
    url = "sqlite+aiosqlite:///" + str(path)
    assert schema_main(["check", "--database-url", url]) == 1
    assert schema_main(["init", "--database-url", url]) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("032",)
        assert connection.execute("SELECT body FROM historical_evidence").fetchone() == (
            "immutable-old-evidence",
        )
