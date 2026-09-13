import configparser
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oduflow import pg_tune, production_registry, server
from oduflow.docker_ops import system_ops
from oduflow.naming import get_repo_path, prod_env_name
from oduflow.settings import Settings, TeamSettings


def _settings(tmp_path: Path, *, production: bool = True) -> Settings:
    team_dir = tmp_path / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(tmp_path),
        etc_dir=str(tmp_path / "etc"),
        prod_enabled=production,
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


def _fixed_resources(monkeypatch):
    monkeypatch.setattr(
        pg_tune,
        "detect_resources",
        lambda: {
            "cpu_count": 4,
            "total_ram_mb": 8192.0,
            "source": "test",
        },
    )
    monkeypatch.setattr(server, "_get_version", lambda: "test")


@pytest.mark.parametrize("writer", ["development", "production", "bundled", "retune"])
def test_generated_pg_config_is_readable_with_restrictive_umask(
    tmp_path, monkeypatch, writer
):
    settings = _settings(tmp_path)
    _fixed_resources(monkeypatch)
    config_dir = Path(settings.etc_dir)
    config_dir.mkdir()
    path = config_dir / "postgresql.conf"
    previous_umask = os.umask(0o077)
    try:
        secret = config_dir / "credentials"
        secret.write_text("private test fixture")
        if writer == "development":
            assert server._write_tuned_pg_conf(path, settings=settings)
        elif writer == "production":
            path = Path(system_ops._ensure_prod_pg_conf(settings))
        elif writer == "bundled":
            server._copy_bundled_pg_conf(path)
        else:
            assert server._run_retune_postgres(settings, apply=True, force=False)
            assert (config_dir / "postgresql-prod.conf").stat().st_mode & 0o777 == 0o644
    finally:
        os.umask(previous_umask)
    assert path.stat().st_mode & 0o777 == 0o644
    assert secret.stat().st_mode & 0o777 == 0o600


def test_preview_does_not_write_files(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _fixed_resources(monkeypatch)

    server._run_retune_postgres(settings)

    assert not (tmp_path / "etc" / "postgresql.conf").exists()
    assert not (tmp_path / "etc" / "postgresql-prod.conf").exists()
    output = capsys.readouterr().out
    assert "Preview only" in output
    assert "Production PostgreSQL" in output


def test_apply_writes_both_managed_configs(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _fixed_resources(monkeypatch)

    server._run_retune_postgres(settings, apply=True)

    dev = (tmp_path / "etc" / "postgresql.conf").read_text()
    prod = (tmp_path / "etc" / "postgresql-prod.conf").read_text()
    assert dev.startswith("# KEEP\n# ODUFLOW-TUNE ")
    assert "production=on" in dev.splitlines()[1]
    assert prod.startswith("# KEEP\n# ODUFLOW-TUNE ")
    output = capsys.readouterr().out
    assert "docker restart oduflow-db" in output
    assert "docker restart oduflow-prod-db" in output


def test_matching_fingerprint_avoids_package_version_churn(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, production=False)
    _fixed_resources(monkeypatch)
    server._run_retune_postgres(settings, apply=True)
    dev = tmp_path / "etc" / "postgresql.conf"
    original = dev.read_text()
    capsys.readouterr()
    monkeypatch.setattr(server, "_get_version", lambda: "next-version")

    server._run_retune_postgres(settings, apply=True)

    assert dev.read_text() == original
    assert not list((tmp_path / "etc").glob("postgresql.conf.bak-*"))
    assert "No files changed" in capsys.readouterr().out


def test_apply_refuses_custom_config_without_force(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _fixed_resources(monkeypatch)
    etc = tmp_path / "etc"
    etc.mkdir()
    dev = etc / "postgresql.conf"
    dev.write_text("# KEEP\n# operator config\nshared_buffers = 256MB\n")

    server._run_retune_postgres(settings, apply=True)

    assert "operator config" in dev.read_text()
    assert not (etc / "postgresql-prod.conf").exists()
    assert "Refusing to overwrite custom" in capsys.readouterr().out


def test_force_backs_up_custom_config(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, production=False)
    _fixed_resources(monkeypatch)
    etc = tmp_path / "etc"
    etc.mkdir()
    dev = etc / "postgresql.conf"
    original = "# KEEP\n# operator config\nshared_buffers = 256MB\n"
    dev.write_text(original)

    server._run_retune_postgres(settings, apply=True, force=True)

    backups = list(etc.glob("postgresql.conf.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert "# ODUFLOW-TUNE " in dev.read_text()
    assert "Production is disabled" not in capsys.readouterr().out


def test_retune_stages_existing_production_odoo_config(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _fixed_resources(monkeypatch)
    team = settings.teams["1"]
    production_registry.create_production(team, "erp", {})
    repo = Path(get_repo_path(prod_env_name("erp"), team.workspaces_dir))
    repo.mkdir(parents=True)
    generated = repo.parent / "odoo.conf"
    original = "[options]\nworkers = 8\n"
    generated.write_text(original)

    server._run_retune_postgres(settings)

    assert generated.read_text() == original
    preview = capsys.readouterr().out
    assert "[production Odoo 1/erp]" in preview
    assert "workers = 3" in preview

    container = MagicMock()
    container.name = "oduflow-1-prod-erp-odoo"
    container.labels = {settings.team_label: team.team_id}
    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)

    assert server._run_retune_postgres(settings, apply=True) is True

    parser = configparser.RawConfigParser()
    parser.read(generated)
    assert parser.get("options", "workers") == "3"
    assert list(generated.parent.glob("odoo.conf.bak-*"))
    container.put_archive.assert_called_once()
    output = capsys.readouterr().out
    assert "docker restart oduflow-1-prod-erp-odoo" in output
