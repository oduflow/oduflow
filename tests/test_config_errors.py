"""A broken oduflow.toml must read as one line, not as a Python traceback.

Settings failures are operator mistakes (a typo, a duplicate token), so the
settings boundary converts them into ConfigError and the CLI entry point prints
a single sentence. Before this, a stray ValueError from validate() escaped
main() and systemd recorded ~15 lines of stack for one sentence of feedback.
"""

import pytest

import oduflow.server as server
from oduflow.errors import ConfigError


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    monkeypatch.setattr(server, "_settings", None)


def _use_config(monkeypatch, tmp_path, text):
    cfg = tmp_path / "oduflow.toml"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
    return cfg


def test_validation_failure_becomes_config_error(monkeypatch, tmp_path):
    cfg = _use_config(
        monkeypatch,
        tmp_path,
        '[team.1]\nhostname = "a.example.com"\n[production]\nworkers_cap = 0\n',
    )

    with pytest.raises(ConfigError) as exc:
        server._get_settings()

    message = str(exc.value)
    assert str(cfg) in message  # which file to edit
    assert "workers_cap must be >= 1" in message  # and what is wrong with it


def test_toml_syntax_error_becomes_config_error(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path, "[team.1\nui_password = \n")

    with pytest.raises(ConfigError) as exc:
        server._get_settings()

    assert str(cfg) in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        '[server]\nport = []\n[team.1]\nhostname = "a.example.com"\n',
        '[server]\nport = inf\n[team.1]\nhostname = "a.example.com"\n',
        "routing = false\n",
        "team = true\n",
        "[team]\n1 = false\n",
        "[production]\nwal = false\n",
    ],
)
def test_invalid_types_exit_without_traceback(monkeypatch, tmp_path, capsys, text):
    import sys

    cfg = _use_config(monkeypatch, tmp_path, text)
    monkeypatch.setattr(sys, "argv", ["oduflow"])

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Invalid configuration" in err
    assert str(cfg) in err
    assert "Traceback" not in err
    assert len([line for line in err.splitlines() if line.strip()]) == 1
    assert server._settings is None


def test_missing_config_becomes_config_error(monkeypatch):
    def _missing():
        raise FileNotFoundError("oduflow.toml not found. Searched: ...")

    monkeypatch.setattr(server, "find_toml", _missing)

    with pytest.raises(ConfigError) as exc:
        server._get_settings()

    assert "oduflow.toml not found" in str(exc.value)


def test_invalid_config_is_not_cached_as_valid(monkeypatch, tmp_path):
    """A failed load must leave the cache empty: caching a half-built Settings
    would make every later call succeed with an unvalidated object."""
    _use_config(
        monkeypatch,
        tmp_path,
        '[team.1]\nhostname = "a.example.com"\n[production]\nworkers_cap = 0\n',
    )

    with pytest.raises(ConfigError):
        server._get_settings()

    assert server._settings is None


def test_main_prints_one_line_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        server,
        "_run_cli",
        lambda: (_ for _ in ()).throw(
            ConfigError(
                "Invalid configuration in /etc/oduflow/oduflow.toml: "
                "production_token must be distinct from all dev and production tokens."
            )
        ),
    )

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "production_token must be distinct" in err
    assert len([line for line in err.splitlines() if line.strip()]) == 1
