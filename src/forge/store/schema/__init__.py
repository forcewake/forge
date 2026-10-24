"""Explicit schema installation and a zero-DDL startup compatibility gate."""

from forge.store.schema.gate import SchemaMismatch, check_schema, initialize

__all__ = ["SchemaMismatch", "check_schema", "initialize"]
