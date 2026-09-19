"""Local enrollment, durable replay/rate limits, and CLI recovery."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pyotp
import pytest

from oduflow import ui_totp
from oduflow.errors import FlowError
from oduflow.settings import Settings, TeamSettings

NOW = 1_800_000_000
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def config(tmp_path, monkeypatch):
    team = TeamSettings(
        team_id="1", ui_password="password", data_dir=str(tmp_path / "team_1")
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    monkeypatch.setattr(ui_totp.time, "time", lambda: NOW)
    return settings, team


def activate(config):
    settings, team = config
    ui_totp.enroll(settings, team, SECRET, pyotp.TOTP(SECRET).at(NOW - 30))


def test_setup_requires_confirmation_and_preserves_existing_factor(config):
    settings, team = config
    with pytest.raises(FlowError, match="Invalid authenticator"):
        ui_totp.enroll(settings, team, SECRET, "invalid")
    assert not ui_totp.enabled(settings, team)
    activate(config)
    generation = ui_totp.version(settings, team)
    with pytest.raises(FlowError, match="already enabled"):
        activate(config)
    assert ui_totp.version(settings, team) == generation
    assert os.stat(ui_totp._path(settings, team)).st_mode & 0o777 == 0o600


def test_setup_without_password_is_rejected(config):
    settings, team = config
    from dataclasses import replace

    with pytest.raises(FlowError, match="ui_password"):
        ui_totp.enroll(
            settings,
            replace(team, ui_password=""),
            SECRET,
            pyotp.TOTP(SECRET).at(ui_totp.time.time()),
        )


def test_replay_rejected_after_reload_and_reset_revokes_generation(config):
    settings, team = config
    activate(config)
    code = pyotp.TOTP(SECRET).at(ui_totp.time.time())
    generation = ui_totp.verify(settings, team, code)
    assert generation
    # No cache: this also covers a new process reading the on-disk last step.
    assert ui_totp.verify(settings, team, code) is None
    ui_totp.reset(settings, team)
    assert not ui_totp.enabled(settings, team)
    assert ui_totp.verify(settings, team, "") not in (None, generation)


def test_concurrent_code_is_accepted_only_once(config):
    settings, team = config
    activate(config)
    code = pyotp.TOTP(SECRET).at(ui_totp.time.time())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: ui_totp.verify(settings, team, code), range(6))
        )
    assert sum(result is not None for result in results) == 1


def test_setup_code_is_consumed(config):
    settings, team = config
    code = pyotp.TOTP(SECRET).at(ui_totp.time.time())
    ui_totp.enroll(settings, team, SECRET, code)
    assert ui_totp.verify(settings, team, code) is None


@pytest.mark.parametrize(
    "offset,accepted", [(-90, False), (-30, True), (0, True), (30, True), (90, False)]
)
def test_clock_window(config, offset, accepted, monkeypatch):
    settings, team = config
    activate(config)
    monkeypatch.setattr(ui_totp.time, "time", lambda: NOW + 120)
    result = ui_totp.verify(settings, team, pyotp.TOTP(SECRET).at(NOW + 120 + offset))
    assert (result is not None) is accepted


def test_persistent_team_throttle_expires(config, monkeypatch):
    settings, team = config
    activate(config)
    for _ in range(10):
        assert ui_totp.verify(settings, team, "bad") is None
    with pytest.raises(ui_totp.TOTPThrottled):
        ui_totp.verify(settings, team, pyotp.TOTP(SECRET).at(ui_totp.time.time()))
    monkeypatch.setattr(ui_totp.time, "time", lambda: NOW + 301)
    assert ui_totp.verify(settings, team, pyotp.TOTP(SECRET).at(ui_totp.time.time()))


@pytest.mark.parametrize("contents", [b"{", b"null", b"{}", b"\xff", b'{"version":42}'])
def test_corrupt_state_fails_closed_and_cli_reset_recovers(config, contents):
    settings, team = config
    activate(config)
    ui_totp._path(settings, team).write_bytes(contents)
    with pytest.raises(ui_totp.TOTPStateError):
        ui_totp.verify(settings, team, "")
    ui_totp.reset(settings, team)
    assert not ui_totp.enabled(settings, team)


def test_failed_persistence_does_not_authenticate(config, monkeypatch):
    settings, team = config
    activate(config)
    monkeypatch.setattr(
        ui_totp, "_save", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        ui_totp.verify(settings, team, pyotp.TOTP(SECRET).at(ui_totp.time.time()))


def test_cli_setup_prints_local_qr_and_confirms_code(config, capsys):
    settings, team = config
    with (
        patch("pyotp.random_base32", return_value=SECRET),
        patch(
            "oduflow.ui_totp.getpass.getpass",
            return_value=pyotp.TOTP(SECRET).at(ui_totp.time.time()),
        ),
    ):
        ui_totp.run_cli(settings, team, "setup")
    output = capsys.readouterr().out
    assert "Scan this QR code" in output
    assert SECRET in output
    assert "UI 2FA enabled" in output
    assert ui_totp.enabled(settings, team)


def test_cli_setup_cancel_does_not_enable(config):
    settings, team = config
    with (
        patch("oduflow.ui_totp.getpass.getpass", side_effect=EOFError),
        pytest.raises(FlowError, match="Cancelled"),
    ):
        ui_totp.run_cli(settings, team, "setup")
    assert not ui_totp.enabled(settings, team)


def test_cli_reset_requires_confirmation(config):
    settings, team = config
    activate(config)
    with patch("builtins.input", return_value="n"):
        ui_totp.run_cli(settings, team, "reset")
    assert ui_totp.enabled(settings, team)
    with patch("builtins.input", return_value="y"):
        ui_totp.run_cli(settings, team, "reset")
    assert not ui_totp.enabled(settings, team)


@pytest.mark.parametrize("action", ["setup", "reset"])
def test_cli_dispatch_uses_existing_settings_without_docker(config, action):
    from oduflow import server

    settings, team = config
    with (
        patch.object(sys, "argv", ["oduflow", "ui-2fa", action, "--team", "1"]),
        patch.object(server, "_get_settings", return_value=settings),
        patch.object(
            server, "find_toml", side_effect=AssertionError("bootstrap attempted")
        ),
        patch("oduflow.ui_totp.run_cli") as run,
    ):
        server._run_cli()
    run.assert_called_once_with(settings, team, action)
