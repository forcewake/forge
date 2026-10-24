"""Explicit schema installation and an exact-baseline startup check without DDL."""

from forge.store.schema.gate import SchemaMismatch, check_schema, initialize

__all__ = ["SchemaMismatch", "check_schema", "initialize"]
