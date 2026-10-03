"""The global --no-telemetry flag disables telemetry for a single run."""

import sys
from unittest.mock import patch

import pytest

from oduflow import server
from oduflow import settings as settings_module
from oduflow.settings import Settings, TeamSettings


def _settings(tmp_path) -> Settings:
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    return Settings(
        base_data_dir=str(tmp_path),
        db_password="password",
        teams={"1": team},
    )


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """Make the real _get_settings load a fixed Settings, restoring globals after."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_no_telemetry", False)
    monkeypatch.setattr(settings_module, "TRANSPORT", settings_module.TRANSPORT)
    monkeypatch.setattr(server, "find_toml", lambda: str(tmp_path / "oduflow.toml"))
    monkeypatch.setattr(Settings, "from_toml", classmethod(lambda cls, path: settings))
    monkeypatch.setattr(Settings, "validate", lambda self: None)
    return settings


def test_override_survives_settings_reload(loaded, monkeypatch):
    monkeypatch.setattr(server, "_no_telemetry", True)

    assert server._get_settings().disable_telemetry is True
    # _ensure_web_ui_password and similar paths drop the cache and reload.
    monkeypatch.setattr(server, "_settings", None)
    assert server._get_settings().disable_telemetry is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["oduflow", "--transport", "http"], False),
        (["oduflow", "--no-telemetry", "--transport", "http"], True),
    ],
)
def test_no_telemetry_flag_applies_for_whole_run(loaded, argv, expected):
    seen = {}

    def reload_like_start_http():
        server._settings = None
        seen["after_reload"] = server._get_settings().disable_telemetry

    with (
        patch.object(sys, "argv", argv),
        patch("oduflow.docker_ops.client.wait_for_docker"),
        patch("oduflow.postgres_migration.validate_configuration"),
        patch.object(server.migrations, "run_pending"),
        patch.object(
            server,
            "_ensure_initialized",
            side_effect=lambda s: seen.setdefault("startup", s.disable_telemetry),
        ),
        patch.object(server.quotas, "apply_all"),
        patch.object(server, "_start_http", side_effect=reload_like_start_http),
    ):
        server._run_cli()

    assert seen == {"startup": expected, "after_reload": expected}
