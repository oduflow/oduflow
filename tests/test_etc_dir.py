"""The configurable config directory (``[storage].etc_dir``, specs/0083).

The config dir holds files that containers bind-mount — ``postgresql.conf``,
``postgresql-prod.conf``, ``traefik-dynamic/`` — and the Docker daemon resolves
a bind source on the *host*. When Oduflow itself runs in a container the dir
therefore has to be a path the daemon sees too, exactly like ``data_dir``.
These tests pin how the dir is resolved (explicit > loaded TOML's directory >
first-run probe), that it is a locked key, that startup refuses an unusable
dir, that every container mount follows the Settings value, and the
Docker-out-of-Docker self-mount diagnostic.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow import config_schema, health
from oduflow import settings as settings_mod
from oduflow.docker_ops import system_ops
from oduflow.errors import ConfigError
from oduflow.settings import Settings, TeamSettings

MINIMAL_TOML = '[team.1]\nhostname = "a.example.com"\n'


@pytest.fixture(autouse=True)
def _reset_default_probe_cache():
    settings_mod._cached_etc_dir = None
    yield
    settings_mod._cached_etc_dir = None


@pytest.fixture
def settings(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    team_dir = data / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(data),
        etc_dir=str(etc),
        routing_mode="traefik",
        routing_tls=False,
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


class TestResolveEtcDir:
    @pytest.mark.parametrize(
        ("toml_path", "expected"),
        [
            # The two find_toml() locations: byte-for-byte the old probe result,
            # so existing installations see no change.
            ("/etc/oduflow/oduflow.toml", "/etc/oduflow"),
            ("/home/me/.oduflow/conf/oduflow.toml", "/home/me/.oduflow/conf"),
            # ODUFLOW_TOML pointing anywhere relocates the config dir on its own.
            ("/srv/oduflow/etc/oduflow.toml", "/srv/oduflow/etc"),
        ],
    )
    def test_defaults_to_the_loaded_tomls_directory(self, toml_path, expected):
        assert settings_mod.resolve_etc_dir("", toml_path) == expected

    def test_explicit_override_wins(self):
        assert (
            settings_mod.resolve_etc_dir("/host/conf", "/etc/oduflow/oduflow.toml")
            == "/host/conf"
        )

    def test_default_never_consults_the_first_run_probe(self, monkeypatch):
        # The probe answers for a *missing* config; once one exists, where it
        # lives is the only thing that matters.
        monkeypatch.setattr(settings_mod.os, "access", lambda *a: False)
        monkeypatch.setattr(settings_mod.os.path, "expanduser", lambda p: "/home/me")
        assert (
            settings_mod.resolve_etc_dir("", "/etc/oduflow/oduflow.toml")
            == "/etc/oduflow"
        )
        assert settings_mod._cached_etc_dir is None

    def test_without_a_toml_path_falls_back_to_the_first_run_probe(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "_resolve_etc_dir", lambda: "/probe")
        assert settings_mod.resolve_etc_dir("", "") == "/probe"

    def test_from_raw_plumbs_the_storage_key(self, tmp_path):
        raw = {
            "team": {"1": {"hostname": "a.example.com"}},
            "storage": {"etc_dir": str(tmp_path / "conf")},
        }
        s = Settings.from_raw(raw, "/etc/oduflow/oduflow.toml")
        assert s.etc_dir == str(tmp_path / "conf")

    def test_from_raw_defaults_to_the_toml_directory(self, tmp_path):
        raw = {"team": {"1": {"hostname": "a.example.com"}}}
        cfg = tmp_path / "somewhere" / "oduflow.toml"
        s = Settings.from_raw(raw, str(cfg))
        assert s.etc_dir == str(cfg.parent)


class TestFirstRunBootstrap:
    def test_bootstrap_uses_the_probe_and_the_loaded_config_agrees(
        self, tmp_path, monkeypatch
    ):
        """First run: nothing is loaded yet, so the config goes where the
        writability probe says (``/etc/oduflow`` else ``~/.oduflow/conf``) —
        and once it is there, resolving from the loaded file gives the same
        directory. Bootstrap behaviour is unchanged by the new default."""
        import oduflow.server as server

        probe_dir = tmp_path / "probe"
        monkeypatch.setattr("oduflow.settings._resolve_etc_dir", lambda: str(probe_dir))

        dest = server._bootstrap_config()

        assert Path(dest) == probe_dir / "oduflow.toml"
        loaded = Settings.from_toml(dest)
        assert loaded.etc_dir == str(probe_dir)


# ---------------------------------------------------------------------------
# schema + startup validation
# ---------------------------------------------------------------------------


class TestSchemaAndValidation:
    def test_registered_as_locked_like_data_dir(self):
        found = config_schema.find_field(("storage", "etc_dir"))
        assert found is not None
        group, field = found
        assert group.id == "storage"
        assert field.apply == config_schema.LOCKED
        data_dir = config_schema.find_field(("storage", "data_dir"))
        assert data_dir is not None and data_dir[1].apply == field.apply

    def _server_with_config(self, monkeypatch, tmp_path, text):
        import oduflow.server as server

        cfg = tmp_path / "oduflow.toml"
        cfg.write_text(text, encoding="utf-8")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
        monkeypatch.setattr(server, "_settings", None)
        return server

    def test_missing_dir_is_one_clear_line(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope"
        server = self._server_with_config(
            monkeypatch,
            tmp_path,
            MINIMAL_TOML + f'[storage]\netc_dir = "{missing}"\n',
        )
        with pytest.raises(ConfigError) as exc:
            server._get_settings()
        message = str(exc.value)
        assert str(missing) in message
        assert "does not exist" in message
        assert "[storage].etc_dir" in message

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
    def test_read_only_dir_is_one_clear_line(self, monkeypatch, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            server = self._server_with_config(
                monkeypatch,
                tmp_path,
                MINIMAL_TOML + f'[storage]\netc_dir = "{ro}"\n',
            )
            with pytest.raises(ConfigError, match="not writable"):
                server._get_settings()
        finally:
            ro.chmod(0o700)

    def test_default_dir_passes(self, monkeypatch, tmp_path):
        server = self._server_with_config(monkeypatch, tmp_path, MINIMAL_TOML)
        assert server._get_settings().etc_dir == str(tmp_path)

    def test_console_refuses_a_dir_that_would_brick_startup(self, tmp_path):
        """The console dry-runs the boot parser, so it must refuse what boot
        refuses — otherwise Raw TOML can save a config the next start rejects."""
        from oduflow import config_store

        missing = tmp_path / "nope"
        with pytest.raises(ConfigError, match="does not exist"):
            config_store.validate_text(
                MINIMAL_TOML + f'[storage]\netc_dir = "{missing}"\n',
                str(tmp_path / "oduflow.toml"),
                http=False,
            )
        missing.mkdir()
        config_store.validate_text(
            MINIMAL_TOML + f'[storage]\netc_dir = "{missing}"\n',
            str(tmp_path / "oduflow.toml"),
            http=False,
        )


class TestLegacyConfigDirWarning:
    """The config dir used to be the probe's answer regardless of where the
    TOML lived, so an ODUFLOW_TOML outside the two standard locations moves it
    on upgrade — and would silently orphan license.key, branding and tuning."""

    def test_names_the_files_left_behind(self, tmp_path, monkeypatch, caplog):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "license.key").write_text("x")
        (legacy / "traefik-dynamic").mkdir()
        new = tmp_path / "new"
        new.mkdir()
        monkeypatch.setattr(settings_mod, "_cached_etc_dir", str(legacy))
        with caplog.at_level("WARNING", logger="oduflow"):
            orphaned = settings_mod.warn_if_legacy_etc_dir(str(new))
        assert orphaned == ["license.key", "traefik-dynamic"]
        assert str(legacy) in caplog.text
        assert "[storage].etc_dir" in caplog.text

    def test_silent_when_the_dir_did_not_move(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "license.key").write_text("x")
        monkeypatch.setattr(settings_mod, "_cached_etc_dir", str(tmp_path))
        with caplog.at_level("WARNING", logger="oduflow"):
            assert settings_mod.warn_if_legacy_etc_dir(str(tmp_path)) == []
        assert not caplog.records

    def test_silent_when_the_new_dir_already_has_the_file(
        self, tmp_path, monkeypatch, caplog
    ):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "license.key").write_text("old")
        new = tmp_path / "new"
        new.mkdir()
        (new / "license.key").write_text("moved")
        monkeypatch.setattr(settings_mod, "_cached_etc_dir", str(legacy))
        with caplog.at_level("WARNING", logger="oduflow"):
            assert settings_mod.warn_if_legacy_etc_dir(str(new)) == []
        assert not caplog.records


# ---------------------------------------------------------------------------
# every container mount follows settings.etc_dir
# ---------------------------------------------------------------------------


def _absent_client():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("absent")
    client.volumes.get.side_effect = docker.errors.NotFound("absent")
    return client


class TestContainerMounts:
    @pytest.fixture(autouse=True)
    def _poison_module_global(self):
        # If any consumer still asked the module-level probe instead of the
        # Settings value, this is the path it would get.
        settings_mod._cached_etc_dir = "/definitely/not/this"
        yield

    def test_dev_pg_mounts_postgresql_conf_from_etc_dir(self, settings):
        conf = Path(settings.etc_dir) / "postgresql.conf"
        conf.write_text("# KEEP\n")
        client = _absent_client()
        system_ops._ensure_pg_container(client, settings, {})
        volumes = client.containers.run.call_args.kwargs["volumes"]
        assert volumes[str(conf)] == {
            "bind": "/etc/postgresql/postgresql.conf",
            "mode": "ro",
        }
        assert not any(h.startswith("/definitely") for h in volumes)

    def test_dev_pg_falls_back_to_the_bundled_conf_when_etc_has_none(self, settings):
        client = _absent_client()
        system_ops._ensure_pg_container(client, settings, {})
        volumes = client.containers.run.call_args.kwargs["volumes"]
        (source,) = [
            h for h, v in volumes.items() if v["bind"].endswith("postgresql.conf")
        ]
        assert source == str(system_ops._BUNDLED_PG_CONF)

    def test_prod_pg_mounts_postgresql_prod_conf_from_etc_dir(self, settings):
        client = _absent_client()
        with patch("oduflow.wal_monitor.assert_writable"):
            system_ops._ensure_prod_pg_container(client, settings, {})
        volumes = client.containers.run.call_args.kwargs["volumes"]
        expected = str(Path(settings.etc_dir) / "postgresql-prod.conf")
        assert volumes[expected]["bind"] == "/etc/postgresql/postgresql.conf"
        assert Path(expected).is_file()

    def test_traefik_mounts_the_dynamic_dir_from_etc_dir(self, settings):
        client = _absent_client()
        system_ops._ensure_traefik(client, settings)
        volumes = client.containers.run.call_args.kwargs["volumes"]
        dyn = str(Path(settings.etc_dir) / "traefik-dynamic")
        assert volumes[dyn] == {"bind": "/etc/traefik/dynamic", "mode": "ro"}
        assert (Path(dyn) / "oduflow.yml").is_file()

    def test_resolve_conf_has_no_module_level_fallback(self):
        assert not hasattr(system_ops, "_get_etc_dir")


# ---------------------------------------------------------------------------
# Docker-out-of-Docker reachability diagnostic
# ---------------------------------------------------------------------------


def _self_client(mounts, hostname="abc123"):
    me = MagicMock()
    me.attrs = {"Mounts": mounts}
    client = MagicMock()

    def _get(name):
        if name == hostname:
            return me
        raise docker.errors.NotFound("nf")

    client.containers.get.side_effect = _get
    return client


def _same_path_bind(path):
    return {"Type": "bind", "Source": path, "Destination": path}


class TestSelfMountCheck:
    @pytest.fixture(autouse=True)
    def _in_container(self, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "abc123")
        with (
            patch("oduflow.self_update.in_container", return_value=True),
            patch.object(health, "_own_container_id", return_value=""),
        ):
            yield

    def test_off_on_a_plain_host(self, settings):
        with patch("oduflow.self_update.in_container", return_value=False):
            result = health._check_self_mounts(MagicMock(), settings)
        assert result["status"] == "off"

    def test_ok_when_both_dirs_are_bound_from_the_same_host_path(self, settings):
        client = _self_client(
            [_same_path_bind(settings.base_data_dir), _same_path_bind(settings.etc_dir)]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "ok"
        assert "etc_dir" in result["detail"]

    def test_ok_when_both_dirs_are_inside_one_same_path_bind(self, settings):
        parent = os.path.dirname(settings.base_data_dir)
        client = _self_client([_same_path_bind(parent)])
        assert health._check_self_mounts(client, settings)["status"] == "ok"

    def test_warns_on_a_named_volume_for_data_dir(self, settings):
        # The docs' old compose example: `oduflow_data:/srv/oduflow`. The
        # daemon resolves /srv/oduflow/... on the host, where it does not exist.
        client = _self_client(
            [
                {
                    "Type": "volume",
                    "Name": "oduflow_data",
                    "Source": "/var/lib/docker/volumes/oduflow_data/_data",
                    "Destination": settings.base_data_dir,
                },
                _same_path_bind(settings.etc_dir),
            ]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "data_dir is on a Docker volume" in result["detail"]
        assert "etc_dir" not in result["detail"]
        # /healthz is public: no host paths in the public detail.
        assert settings.base_data_dir not in result["detail"]

    def test_warns_on_a_named_volume_for_etc_dir(self, settings):
        # The docs' old compose example: `oduflow_etc:/etc/oduflow` — exactly
        # the trap that made oduflow-db parse a directory as postgresql.conf.
        client = _self_client(
            [
                _same_path_bind(settings.base_data_dir),
                {
                    "Type": "volume",
                    "Name": "oduflow_etc",
                    "Source": "/var/lib/docker/volumes/oduflow_etc/_data",
                    "Destination": settings.etc_dir,
                },
            ]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "etc_dir is on a Docker volume" in result["detail"]
        assert "data_dir" not in result["detail"]
        assert settings.etc_dir not in result["detail"]

    def test_warns_when_etc_dir_host_path_differs(self, settings):
        client = _self_client(
            [
                _same_path_bind(settings.base_data_dir),
                {
                    "Type": "bind",
                    "Source": "/Users/me/oduflow-conf",
                    "Destination": settings.etc_dir,
                },
            ]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "etc_dir is bound from a different host path" in result["detail"]
        assert "/Users/me" not in result["detail"]

    def test_warns_when_etc_dir_is_in_the_writable_layer(self, settings):
        # The docs' old minimal example mounted the data dir only: oduflow.toml
        # and the generated postgresql.conf lived in the container's own layer.
        client = _self_client([_same_path_bind(settings.base_data_dir)])
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert (
            "etc_dir is inside the Oduflow container's writable layer"
            in (result["detail"])
        )

    def test_reports_both_dirs_when_both_are_wrong(self, settings):
        client = _self_client([])
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "data_dir" in result["detail"] and "etc_dir" in result["detail"]

    def test_identifies_itself_by_container_id_when_the_hostname_does_not(
        self, settings, monkeypatch
    ):
        # Compose sets the hostname to the *service* name and names the
        # container <project>-oduflow-1, so neither resolves to a container.
        # The ID read from /proc does, which is what keeps the check from
        # silently going "off" on the deployment the docs recommend.
        cid = "a" * 64
        monkeypatch.setenv("HOSTNAME", "oduflow")
        client = _self_client(
            [
                _same_path_bind(settings.base_data_dir),
                _same_path_bind(settings.etc_dir),
            ],
            hostname=cid,
        )
        with patch.object(health, "_own_container_id", return_value=cid):
            assert health._check_self_mounts(client, settings)["status"] == "ok"

    def test_off_when_own_container_is_not_found(self, settings):
        client = _self_client([], hostname="someone-else")
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "off"
        assert "not verified" in result["detail"]

    def test_off_when_inspecting_own_container_raises(self, settings):
        client = MagicMock()
        client.containers.get.side_effect = RuntimeError("daemon gone")
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "off"

    def test_startup_warning_names_host_paths_and_both_fixes(self, settings, caplog):
        client = _self_client(
            [
                {
                    "Type": "bind",
                    "Source": "/Users/me/oduflow",
                    "Destination": settings.base_data_dir,
                },
                {
                    "Type": "volume",
                    "Name": "oduflow_etc",
                    "Source": "/var/lib/docker/volumes/oduflow_etc/_data",
                    "Destination": settings.etc_dir,
                },
            ]
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            caplog.at_level("WARNING", logger="oduflow"),
        ):
            assert health.warn_if_dirs_unreachable(settings) == "warn"
        message = "\n".join(r.getMessage() for r in caplog.records)
        assert "Docker-out-of-Docker mount check" in message
        assert "/Users/me/oduflow" in message
        assert f"-v {settings.base_data_dir}:{settings.base_data_dir}" in message
        assert f"-v {settings.etc_dir}:{settings.etc_dir}" in message
        assert "[storage].etc_dir" in message

    def test_startup_check_is_silent_when_ok(self, settings, caplog):
        client = _self_client(
            [_same_path_bind(settings.base_data_dir), _same_path_bind(settings.etc_dir)]
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            caplog.at_level("WARNING", logger="oduflow"),
        ):
            assert health.warn_if_dirs_unreachable(settings) == "ok"
        assert not caplog.records

    def test_startup_check_never_raises(self, settings):
        with patch(
            "oduflow.docker_ops.client.get_client",
            side_effect=RuntimeError("no docker"),
        ):
            assert health.warn_if_dirs_unreachable(settings) is None

    def test_startup_check_runs_before_infrastructure_is_created(self, settings):
        import oduflow.server as server

        order = []
        with (
            patch.object(server, "_apply_agent_feedback"),
            patch.object(server, "_apply_branding"),
            patch.object(server, "_copy_bundled_configs"),
            patch("oduflow.prereqs.ensure_fuse_overlayfs"),
            patch("oduflow.prereqs.ensure_rsync"),
            patch.object(
                health,
                "warn_if_dirs_unreachable",
                side_effect=lambda s: order.append("check"),
            ),
            patch.object(
                server.system_ops,
                "init_system",
                side_effect=lambda s: order.append("init"),
            ),
            patch("oduflow.backup_ops.register_pre_update_hook"),
            patch.object(server.git_ops, "ensure_ssh_key", return_value=False),
            patch.object(server, "_managed_bundled_files", return_value=[]),
            patch.object(server.env_ops, "reconcile_overlay_mounts", return_value=[]),
            patch("oduflow.telemetry.record_startup", return_value="id"),
        ):
            server._ensure_initialized(settings)
        assert order == ["check", "init"]

    def test_collect_health_reports_but_does_not_degrade(self, settings):
        health._cache["result"] = None
        client = _self_client(
            [
                {
                    "Type": "volume",
                    "Name": "oduflow_etc",
                    "Source": "/var/lib/docker/volumes/oduflow_etc/_data",
                    "Destination": settings.etc_dir,
                },
                _same_path_bind(settings.base_data_dir),
            ]
        )
        running = MagicMock()
        running.status = "running"
        running.exec_run.return_value = (0, b"")
        get = client.containers.get.side_effect

        def _get(name):
            if name in (settings.shared_db_container, settings.traefik_container):
                return running
            return get(name)

        client.containers.get.side_effect = _get
        client.containers.list.return_value = []
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch.object(health, "_check_overlays", return_value={"status": "ok"}),
        ):
            result = health.collect_health(settings, force=True)
        health._cache["result"] = None
        assert result["checks"]["self_mounts"]["status"] == "warn"
        assert result["ok"] is True


def test_container_id_is_read_from_mountinfo(tmp_path, monkeypatch):
    cid = "b" * 64
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 2 0:1 / / rw - overlay overlay rw\n"
        f"3 1 8:1 /var/lib/docker/containers/{cid}/resolv.conf /etc/resolv.conf "
        "rw - ext4 /dev/sda1 rw\n"
    )
    real_open = open

    def _open(path, *a, **kw):
        if path == "/proc/self/mountinfo":
            return real_open(mountinfo, *a, **kw)
        raise OSError("no such file")

    monkeypatch.setattr("builtins.open", _open)
    assert health._own_container_id() == cid


def test_dashboard_renders_the_mounts_chip():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/oduflow/templates/dashboard.html").read_text(encoding="utf-8")
    assert "self_mounts: 'MOUNTS'" in html


@pytest.mark.skipif(sys.platform != "linux", reason="shape of /proc is Linux-only")
def test_own_container_id_does_not_raise_outside_a_container():
    # Either "" or a real ID; the point is it never raises.
    assert isinstance(health._own_container_id(), str)
