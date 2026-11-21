"""Regression tests for the `oduflow call` CLI argument coercion (P-H1).

server.py uses `from __future__ import annotations`, so a parameter's raw
annotation is the *string* 'bool'/'int', not the class. The old coercion did
`annotation is bool`, which never matched, so every positional value was passed
through as a string: `oduflow call delete_production erp erp false` reached
`drop_database='false'` — a truthy string — and dropped the database despite the
explicit `false`. This module reproduces that PEP 563 condition (note the
`from __future__` import below) and pins the corrected behaviour.
"""

from __future__ import annotations

import types as _types
from typing import Any

import pytest

from oduflow import server

# --- _coerce_cli_value (pure) -------------------------------------------------


def test_coerce_bool_variants():
    assert server._coerce_cli_value("false", bool, False) is False
    assert server._coerce_cli_value("False", bool, False) is False
    assert server._coerce_cli_value("0", bool, False) is False
    assert server._coerce_cli_value("no", bool, False) is False
    assert server._coerce_cli_value("true", bool, False) is True
    assert server._coerce_cli_value("1", bool, False) is True
    assert server._coerce_cli_value("yes", bool, False) is True


def test_coerce_int_and_float():
    assert server._coerce_cli_value("50", int, 100) == 50
    assert isinstance(server._coerce_cli_value("50", int, 100), int)
    assert server._coerce_cli_value("1.5", float, 0.0) == 1.5


def test_coerce_optional_is_unwrapped():
    # `X | None` resolves via get_type_hints to a types.UnionType; the helper
    # must unwrap it to the inner type and coerce accordingly.
    assert server._coerce_cli_value("7", int | None, None) == 7
    assert server._coerce_cli_value("false", bool | None, None) is False


def test_coerce_falls_back_to_default_type_when_hint_missing():
    # Simulates typing.get_type_hints() failing: hint=None, coerce off default.
    assert server._coerce_cli_value("false", None, False) is False
    assert server._coerce_cli_value("50", None, 100) == 50
    # bool is a subclass of int; a bool default must not take the int branch.
    assert server._coerce_cli_value("true", None, False) is True


def test_coerce_str_passthrough():
    assert server._coerce_cli_value("csv", str, "csv") == "csv"


# --- _run_call integration (fake tools, nothing real executes) ----------------


def _register_tool(monkeypatch, name, fn):
    monkeypatch.setitem(
        server.mcp._tool_manager._tools, name, _types.SimpleNamespace(fn=fn)
    )


def test_run_call_bool_positional_false_stays_false(monkeypatch):
    captured = {}

    def fake_delete_production(
        name: str, confirm: str = "", drop_database: bool = False, ctx: Any = None
    ):
        captured["drop_database"] = drop_database
        return "ok"

    _register_tool(monkeypatch, "fake_delete_production", fake_delete_production)
    server._run_call(["fake_delete_production", "erp", "erp", "false"])

    # The whole point of P-H1: an explicit `false` must arrive as bool False.
    assert captured["drop_database"] is False


def test_run_call_int_positional_is_int(monkeypatch):
    captured = {}

    def fake_run_db_query(
        env_name: str,
        query: str,
        output_format: str = "csv",
        max_rows: int = 100,
        ctx: Any = None,
    ):
        captured["max_rows"] = max_rows
        return "ok"

    _register_tool(monkeypatch, "fake_run_db_query", fake_run_db_query)
    server._run_call(["fake_run_db_query", "env", "select 1", "csv", "50"])

    assert captured["max_rows"] == 50
    assert isinstance(captured["max_rows"], int)


def test_run_call_does_not_echo_credentials(monkeypatch, capsys):
    secret = "fixture-only-token"

    def configure(token: str):
        assert token == secret
        return "configured"

    _register_tool(monkeypatch, "setup_repo_auth", configure)
    for args in ([secret, secret], ['{"token":"fixture-only-token"}']):
        server._run_call(["setup_repo_auth", *args])
        captured = capsys.readouterr()
        assert "configured" in captured.out
        assert secret not in captured.out + captured.err


def test_run_call_invalid_int_exits_cleanly(monkeypatch, capsys):
    def fake_tool(env_name: str, max_rows: int = 100, ctx: Any = None):
        return "ok"

    _register_tool(monkeypatch, "fake_tool_bad_int", fake_tool)
    with pytest.raises(SystemExit):
        server._run_call(["fake_tool_bad_int", "env", "notanint"])
    error = capsys.readouterr().err
    assert "max_rows" in error and "int" in error
    assert "notanint" not in error


def test_run_call_missing_required_argument(capsys):
    with pytest.raises(SystemExit) as error:
        server._run_call(["create_environment"])
    assert error.value.code == 2
    assert "Usage: oduflow call create_environment <branch>" in capsys.readouterr().err
