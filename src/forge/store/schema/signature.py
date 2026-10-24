"""Physical schema inspection includes native constraints, indexes and authority."""

from typing import Any

from sqlalchemy import Connection, inspect, text

from forge.store.schema.expressions import canonical_sql


def _plain(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): canonical_sql(str(item))
            if key in ("sqltext", "postgresql_where")
            else _plain(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _rows(connection: Connection, query: str) -> list[list[Any]]:
    return [[_plain(value) for value in row] for row in connection.execute(text(query))]


def _sqlite_details(connection: Connection) -> dict[str, Any]:
    return {
        "table_options": sorted(
            [row[1], row[4], row[5]]
            for row in connection.execute(text("PRAGMA table_list"))
            if row[0] == "main" and not row[1].startswith("sqlite_")
        ),
        "triggers": _rows(
            connection, "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ),
    }


def _postgres_details(connection: Connection) -> dict[str, Any]:
    constraints = _rows(
        connection,
        "SELECT c.relname, x.conname, x.contype, x.condeferrable, x.condeferred, "
        "x.convalidated, x.connoinherit, pg_get_constraintdef(x.oid) "
        "FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' ORDER BY c.relname,x.conname",
    )
    return {
        "constraint_options": [[*row[:-1], canonical_sql(row[-1])] for row in constraints],
        "table_options": _rows(
            connection,
            "SELECT c.relname,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname",
        ),
        "index_options": _rows(
            connection,
            "SELECT c.relname,i.indisvalid,i.indisready,i.indislive,i.indisunique,"
            "i.indnullsnotdistinct FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' ORDER BY c.relname",
        ),
        "policies": _rows(
            connection,
            "SELECT tablename,policyname,permissive,roles::text,cmd,qual,with_check "
            "FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname",
        ),
        "materialized_views": inspect(connection).get_materialized_view_names(),
        "triggers": _rows(
            connection,
            "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY t.tgname",
        ),
        "sequences": _rows(
            connection,
            "SELECT sequencename,data_type::text,start_value,min_value,max_value,increment_by,cycle "
            "FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename",
        ),
        **_postgres_objects(connection),
    }


def _postgres_objects(connection: Connection) -> dict[str, Any]:
    return {
        "schemas": _rows(
            connection,
            "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
            "AND nspname <> 'information_schema' ORDER BY nspname",
        ),
        "extensions": _rows(
            connection, "SELECT extname,extversion FROM pg_extension ORDER BY extname"
        ),
        "view_definitions": _rows(
            connection,
            "SELECT c.relname,c.relkind,pg_get_viewdef(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('v','m') ORDER BY c.relname",
        ),
        "routines": _rows(
            connection,
            "SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.prokind IN ('f','p') "
            "ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
        ),
    }


def inspect_schema(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    result: dict[str, Any] = {
        "tables": {},
        "views": inspector.get_view_names(),
        "temporary_tables": inspector.get_temp_table_names(),
    }
    for name in sorted(inspector.get_table_names()):
        result["tables"][name] = {
            "columns": [_plain(dict(column)) for column in inspector.get_columns(name)],
            "primary_key": _plain(inspector.get_pk_constraint(name)),
            "foreign_keys": sorted(
                [_plain(item) for item in inspector.get_foreign_keys(name)], key=str
            ),
            "unique": sorted(
                [_plain(item) for item in inspector.get_unique_constraints(name)], key=str
            ),
            "checks": sorted(
                [_plain(item) for item in inspector.get_check_constraints(name)], key=str
            ),
            "indexes": sorted([_plain(item) for item in inspector.get_indexes(name)], key=str),
        }
    if connection.dialect.name == "sqlite":
        result.update(_sqlite_details(connection))
    elif connection.dialect.name == "postgresql":
        result.update(_postgres_details(connection))
    else:
        raise ValueError("unsupported database dialect")
    return result


def is_empty(connection: Connection) -> bool:
    shape = inspect_schema(connection)
    allowed = {"schemas": [["public"]], "extensions": [["plpgsql", "1.0"]]}
    return all(
        value == allowed.get(key, {} if key == "tables" else []) for key, value in shape.items()
    )
