from __future__ import annotations

import sys
from unittest.mock import patch


def test_client_cli_dispatches_without_loading_settings() -> None:
    from oduflow import server

    with (
        patch.object(
            sys,
            "argv",
            ["oduflow", "client", "--url", "https://host/mcp", "list"],
        ),
        patch("oduflow.client.run_client", return_value=0) as run_client,
        patch.object(
            server, "find_toml", side_effect=AssertionError("settings loaded")
        ),
    ):
        server._run_cli()

    run_client.assert_called_once_with(["--url", "https://host/mcp", "list"])


def test_client_cli_propagates_nonzero_exit_code() -> None:
    from oduflow import server

    with (
        patch.object(sys, "argv", ["oduflow", "client", "list"]),
        patch("oduflow.client.run_client", return_value=2),
        patch.object(
            server, "find_toml", side_effect=AssertionError("settings loaded")
        ),
        patch("builtins.print"),
    ):
        try:
            server._run_cli()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("SystemExit was not raised")


def test_client_cli_dispatches_after_a_global_option() -> None:
    from oduflow import server

    with (
        patch.object(
            sys, "argv", ["oduflow", "--no-telemetry", "client", "--json", "list"]
        ),
        patch("oduflow.client.run_client", return_value=0) as run_client,
        patch.object(
            server, "find_toml", side_effect=AssertionError("settings loaded")
        ),
    ):
        server._run_cli()

    run_client.assert_called_once_with(["--json", "list"])
