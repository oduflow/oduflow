import logging
import sys
from unittest.mock import patch

import pytest

from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import StackValidationError
from oduflow.stack_ops import StackPlan

_MANIFEST = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {name: demo}
spec:
  environment:
    name: demo
    branch: main
    repoUrl: https://github.com/acme/demo.git
    odooImage: odoo:18.0
"""


def test_stack_validate_cli(tmp_path, capsys):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(
        """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {name: demo}
spec:
  environment:
    name: demo
    branch: main
    repoUrl: https://github.com/acme/demo.git
    odooImage: odoo:18.0
""",
        encoding="utf-8",
    )
    from oduflow import server

    with patch.object(sys, "argv", ["oduflow", "stack", "validate", str(manifest)]):
        server._run_cli()

    assert "Stack 'demo' is valid" in capsys.readouterr().out


def test_stack_validate_cli_checks_referenced_files(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(
        """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {name: demo}
spec:
  environment:
    name: demo
    branch: main
    repoUrl: https://github.com/acme/demo.git
    odooImage: odoo:18.0
  volumes:
    config: {}
  files:
    - source: missing.conf
      volume: config
      path: app.conf
""",
        encoding="utf-8",
    )
    from oduflow import server

    with (
        patch.object(sys, "argv", ["oduflow", "stack", "validate", str(manifest)]),
        pytest.raises(StackValidationError, match="cannot read file source"),
    ):
        server._run_cli()


def test_stack_plan_cli_overlays_adjacent_env_file(tmp_path, capsys):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(
        """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {name: demo}
spec:
  environment:
    name: demo
    branch: main
    repoUrl: https://github.com/acme/demo.git
    odooImage: odoo:18.0
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("FROM_DOTENV=s3cret\n", encoding="utf-8")

    from oduflow import server

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_password="password",
        teams={"1": team},
    )
    captured = {}

    def fake_build_plan(*args, **kwargs):
        captured["environ"] = kwargs["environ"]
        return StackPlan("demo", ())

    with (
        patch.object(sys, "argv", ["oduflow", "stack", "plan", str(manifest)]),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch("oduflow.stack_ops.build_plan", side_effect=fake_build_plan),
    ):
        server._run_cli()

    assert captured["environ"]["FROM_DOTENV"] == "s3cret"
    # The live process environment is overlaid on top of the file values.
    assert "PATH" in captured["environ"]


def test_log_level_flag_quiets_stack_commands(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    from oduflow import server

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path), db_password="password", teams={"1": team}
    )

    argv = ["oduflow", "--log-level", "warning", "stack", "plan", str(manifest)]
    with (
        patch.object(sys, "argv", argv),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch("oduflow.stack_ops.build_plan", return_value=StackPlan("demo", ())),
        patch.object(logging, "basicConfig") as basic_config,
    ):
        server._run_cli()

    assert basic_config.call_args.kwargs["level"] == logging.WARNING


def test_cli_input_error_is_one_line_without_traceback(tmp_path, capsys):
    # A plain ValueError under a stack command (here: an invalid environment
    # hostname reached through the manifest) is manifest input validation,
    # reported like a FlowError. Outside the stack commands a bare ValueError
    # is just as likely a programming bug and keeps its traceback.
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    from oduflow import server

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path), db_password="password", teams={"1": team}
    )

    with (
        patch.object(sys, "argv", ["oduflow", "stack", "plan", str(manifest)]),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch(
            "oduflow.stack_ops.build_plan",
            side_effect=ValueError("Invalid environment hostname 'Bad_Name'"),
        ),
        pytest.raises(SystemExit) as exc,
    ):
        server.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert [line for line in err.splitlines() if line.strip()] == [
        "❌ Invalid environment hostname 'Bad_Name'"
    ]


def test_startup_stack_is_applied_before_server(tmp_path):
    from oduflow import server

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_password="password",
        teams={"1": team},
    )
    manifest = object()
    events = []

    with (
        patch.object(
            sys,
            "argv",
            [
                "oduflow",
                "--no-telemetry",
                "--stack",
                "/stack/oduflow.yaml",
                "--transport",
                "http",
            ],
        ),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch("oduflow.postgres_migration.validate_configuration"),
        patch.object(server.migrations, "run_pending"),
        patch("oduflow.postgres_migration.upgrade"),
        patch.object(server, "_ensure_initialized"),
        patch.object(server.quotas, "apply_all"),
        patch(
            "oduflow.docker_ops.client.wait_for_docker",
            side_effect=lambda: events.append("docker"),
        ),
        patch("oduflow.stack_loader.load_stack", return_value=manifest),
        patch(
            "oduflow.stack_ops.apply_stack",
            side_effect=lambda *a, **k: events.append("stack") or StackPlan("demo", ()),
        ),
        patch.object(
            server, "_start_http", side_effect=lambda: events.append("server")
        ),
    ):
        server._run_cli()

    assert events == ["docker", "stack", "server"]
