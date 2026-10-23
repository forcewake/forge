"""Historical findings migrations remain unchanged after feature retirement."""


class TestMigration010:
    def test_security_findings_table_matches_model(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                spec = importlib.util.spec_from_file_location(
                    "migration_010",
                    Path(__file__).resolve().parent.parent
                    / "alembic"
                    / "versions"
                    / "010_security_findings.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                module.upgrade()

            inspector = inspect(conn)
            names = {column["name"] for column in inspector.get_columns("security_findings")}
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
        assert names == {
            "id",
            "run_id",
            "provider",
            "scope",
            "source",
            "fingerprint",
            "severity",
            "title",
            "path",
            "line",
            "identifiers",
            "status",
            "triage_note",
            "ref",
            "sha",
            "first_seen",
            "last_seen",
        }
        # The dedupe key IS the unique index.
        assert "uq_finding_per_source_scope_fingerprint" in index_names
        engine.dispose()

    def test_upgrade_downgrade_round_trip(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                spec = importlib.util.spec_from_file_location(
                    "migration_010_rt",
                    Path(__file__).resolve().parent.parent
                    / "alembic"
                    / "versions"
                    / "010_security_findings.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                module.upgrade()
                assert "security_findings" in inspect(conn).get_table_names()
                module.downgrade()
                assert "security_findings" not in inspect(conn).get_table_names()
        engine.dispose()


class TestMigration016:
    def _run(self, conn, filename: str, direction: str):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            spec = importlib.util.spec_from_file_location(
                filename,
                Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename,
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            getattr(module, direction)()

    def test_upgrade_adds_governance_columns_and_tables(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            # The findings table starts at its 010 shape.
            self._run(conn, "010_security_findings.py", "upgrade")
            self._run(conn, "016_findings_triage_governance.py", "upgrade")

            inspector = inspect(conn)
            columns = {column["name"] for column in inspector.get_columns("security_findings")}
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
            tables = inspector.get_table_names()
            unique_key = next(
                index
                for index in inspector.get_indexes("security_findings")
                if index["name"] == "uq_finding_per_connection_source_scope_fingerprint"
            )
            assert unique_key["column_names"] == ["connection_id", "source", "scope", "fingerprint"]
            assert unique_key["unique"]
        assert {"suggested_verdict", "suggested_at", "suggested_by", "version"} <= columns
        assert {"connection_id", "absent_in_last_scan"} <= columns
        # The dedupe key is connection-led now (R26).
        assert "uq_finding_per_source_scope_fingerprint" not in index_names
        assert "uq_finding_per_connection_source_scope_fingerprint" in index_names
        assert "security_scan_executions" in tables
        assert "security_finding_actions" in tables
        engine.dispose()

    def test_upgrade_downgrade_round_trip(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            self._run(conn, "010_security_findings.py", "upgrade")
            self._run(conn, "016_findings_triage_governance.py", "upgrade")
            assert "security_finding_actions" in inspect(conn).get_table_names()
            self._run(conn, "016_findings_triage_governance.py", "downgrade")
            inspector = inspect(conn)
            assert "security_finding_actions" not in inspector.get_table_names()
            assert "security_scan_executions" not in inspector.get_table_names()
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
            assert "uq_finding_per_source_scope_fingerprint" in index_names
            columns = {column["name"] for column in inspector.get_columns("security_findings")}
            assert "suggested_verdict" not in columns and "connection_id" not in columns
        engine.dispose()


async def test_fresh_sqlite_keeps_the_historical_findings_tables(tmp_path):
    from sqlalchemy import inspect
    from forge.database import init_db, get_engine, dispose_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/historical.db"
    try:
        await init_db(url)
        async with get_engine(url).connect() as connection:
            tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        assert {
            "security_findings",
            "security_scan_executions",
            "security_finding_actions",
        } <= tables
    finally:
        await dispose_engine()
