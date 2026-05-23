"""Pure-unit checks for sql_query's SELECT-only validator and user_data
identifier helpers. DB-backed end-to-end tests for create_table / sql_query
live in test_tool_sql_db.py."""

from __future__ import annotations

from uuid import UUID

import pytest

from wolfpaw.toolbox.tools.sql_query import _validate_select
from wolfpaw.toolbox.registry import ToolError
from wolfpaw.toolbox.user_data import (
    quote_ident,
    user_data_schema,
    validate_identifier,
)


def test_accepts_select():
    _validate_select("SELECT 1")
    _validate_select("select * from foo")
    _validate_select("   -- a comment\nSELECT 1")
    _validate_select("/* block */ WITH a AS (SELECT 1) SELECT * FROM a")


def test_rejects_dml_and_ddl():
    for bad in [
        "INSERT INTO foo VALUES (1)",
        "DELETE FROM foo",
        "UPDATE foo SET x = 1",
        "DROP TABLE foo",
        "CREATE TABLE bar (id INT)",
    ]:
        with pytest.raises(ToolError):
            _validate_select(bad)


def test_user_data_schema_uses_hex_form():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    assert user_data_schema(uid) == "user_data_12345678123456781234567812345678"


def test_validate_identifier_accepts_safe_names():
    for name in ["foo", "foo_bar", "x123", "_private"]:
        assert validate_identifier(name) == name


def test_validate_identifier_rejects_unsafe_names():
    for bad in ["Foo", "1col", "with-dash", "name space", '"quoted"', "drop;"]:
        with pytest.raises(ValueError):
            validate_identifier(bad)


def test_quote_ident_escapes_inner_quotes():
    # Caller validates names first; this just confirms the quoter behaves.
    assert quote_ident('weird"name') == '"weird""name"'
