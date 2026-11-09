"""The daemon-visible mirror of bind-mounted config files (specs/0083).

The operator edits ``<etc_dir>/postgresql.conf``, ``postgresql-prod.conf`` and
``traefik-dynamic/``; the containers mount a derived copy under
``base_data_dir``, the one directory the Docker daemon is guaranteed to see
even when Oduflow itself runs in a container. These tests pin the mirror's
content and permissions, that every container mounts the mirror and not the
source, how containers created against the old mount source are healed, and
the startup/health diagnostic for a data dir the daemon cannot reach.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow import health
from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings

PG_MOUNT = "/etc/postgresql/postgresql.conf"
TRAEFIK_MOUNT = "/etc/traefik/dynamic"


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


def _dev_conf(settings, content="# KEEP\nshared_buffers = 1GB\n"):
    path = os.path.join(settings.etc_dir, "postgresql.conf")
    with open(path, "w") as f:
        f.write(content)
    return path


def _existing(mounts, image="postgres:16", status="running"):
    container = MagicMock()
    container.status = status
    container.attrs = {"Mounts": mounts, "Config": {"Image": image}}
    return container


def _absent_client():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("absent")
    client.volumes.get.side_effect = docker.errors.NotFound("absent")
    return client


# ---------------------------------------------------------------------------
# mirror content
# ---------------------------------------------------------------------------


class TestPgConfMirror:
    def test_dev_mirror_is_a_marked_copy_under_data_dir(self, settings):
        source = _dev_conf(settings)
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            mirror = system_ops.sync_dev_pg_conf_mirror(settings)

        assert mirror == os.path.join(
            settings.base_data_dir, "conf-mirror", "postgresql.conf"
        )
        text = open(mirror).read()
        # Self-identifying: says it is a copy and names the file to edit.
        assert text.startswith("# GENERATED COPY - DO NOT EDIT.")
        assert source in text
        assert settings.shared_db_container in text
        # The operator's content follows the header verbatim.
        assert text.endswith("# KEEP\nshared_buffers = 1GB\n")
        # The source keeps its own first line, so `# KEEP` detection on it is
        # unaffected.
        assert open(source).read().splitlines()[0] == "# KEEP"

    def test_mirror_is_world_readable(self, settings):
        source = _dev_conf(settings)
        previous = os.umask(0o077)
        try:
            with patch.object(system_ops, "_resolve_conf", return_value=source):
                mirror = system_ops.sync_dev_pg_conf_mirror(settings)
        finally:
            os.umask(previous)
        assert os.stat(mirror).st_mode & 0o777 == 0o644
        assert os.stat(os.path.dirname(mirror)).st_mode & 0o777 == 0o755

    def test_refresh_rewrites_in_place(self, settings):
        source = _dev_conf(settings, "# KEEP\nwork_mem = 4MB\n")
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            mirror = system_ops.sync_dev_pg_conf_mirror(settings)
            inode = os.stat(mirror).st_ino
            _dev_conf(settings, "# KEEP\nwork_mem = 64MB\n")
            system_ops.sync_dev_pg_conf_mirror(settings)
        assert "work_mem = 64MB" in open(mirror).read()
        assert "work_mem = 4MB" not in open(mirror).read()
        # Same inode: a container that already bind-mounts the file keeps
        # seeing the current content.
        assert os.stat(mirror).st_ino == inode

    def test_prod_mirror_copies_the_generated_prod_conf(self, settings):
        source = system_ops._ensure_prod_pg_conf(settings)
        mirror = system_ops.sync_prod_pg_conf_mirror(settings)
        assert mirror == os.path.join(
            settings.base_data_dir, "conf-mirror", "postgresql-prod.conf"
        )
        text = open(mirror).read()
        assert source in text
        assert "archive_mode = on" in text
        # The source is untouched (the `# KEEP` contract lives there).
        assert open(source).read().splitlines()[0] == "# KEEP"


class TestTraefikDynamicMirror:
    def test_mirrors_generated_and_drop_in_files_and_prunes_stale(self, settings):
        source_dir = os.path.join(settings.etc_dir, "traefik-dynamic")
        os.makedirs(source_dir)
        with open(os.path.join(source_dir, "oduflow.yml"), "w") as f:
            f.write("http: {}\n")
        with open(os.path.join(source_dir, "custom.yml"), "w") as f:
            f.write("http:\n  routers: {}\n")
        os.mkdir(os.path.join(source_dir, "notes"))  # directories are skipped

        mirror_dir = system_ops.sync_traefik_dynamic_mirror(settings)

        assert mirror_dir == os.path.join(
            settings.base_data_dir, "conf-mirror", "traefik-dynamic"
        )
        assert sorted(os.listdir(mirror_dir)) == ["custom.yml", "oduflow.yml"]
        assert open(os.path.join(mirror_dir, "custom.yml")).read() == (
            "http:\n  routers: {}\n"
        )
        assert os.stat(os.path.join(mirror_dir, "custom.yml")).st_mode & 0o777 == (
            0o644
        )

        # An operator drop-in removed from the source disappears from the
        # mirror on the next sync; nothing lingers to keep routing a deleted
        # host.
        os.remove(os.path.join(source_dir, "custom.yml"))
        system_ops.sync_traefik_dynamic_mirror(settings)
        assert os.listdir(mirror_dir) == ["oduflow.yml"]


# ---------------------------------------------------------------------------
# containers mount the mirror, never the config dir
# ---------------------------------------------------------------------------


class TestDevPgContainer:
    def test_new_container_mounts_the_mirror(self, settings):
        source = _dev_conf(settings)
        client = _absent_client()
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            system_ops._ensure_pg_container(client, settings, {})

        kwargs = client.containers.run.call_args.kwargs
        mirror = os.path.join(settings.base_data_dir, "conf-mirror", "postgresql.conf")
        assert kwargs["volumes"][mirror] == {"bind": PG_MOUNT, "mode": "ro"}
        assert source not in kwargs["volumes"]
        assert kwargs["command"] == ["postgres", "-c", f"config_file={PG_MOUNT}"]
        assert os.path.isfile(mirror)

    def test_stale_mount_source_recreates_keeping_the_image(self, settings):
        # Created by an older Oduflow: binds /etc/oduflow/postgresql.conf, a
        # path the daemon cannot see in Docker-out-of-Docker. Its state lives
        # in the data volume and the tablespaces mount, so a recreate is only
        # a restart — but it must come back on the image it was running, never
        # as an accidental major upgrade.
        source = _dev_conf(settings)
        old = _existing(
            [
                {"Destination": "/var/lib/postgresql/data", "Source": "vol"},
                {"Destination": PG_MOUNT, "Source": "/etc/oduflow/postgresql.conf"},
            ],
            image="postgres:15",
        )
        client = MagicMock()
        client.containers.get.return_value = old
        pinned = Settings(
            base_data_dir=settings.base_data_dir,
            etc_dir=settings.etc_dir,
            postgres_image="postgres:17",
            teams=settings.teams,
        )
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            system_ops._ensure_pg_container(client, pinned, {})

        old.stop.assert_called_once()
        old.remove.assert_called_once()
        args, kwargs = client.containers.run.call_args
        assert args[0] == "postgres:15"
        mirror = os.path.join(settings.base_data_dir, "conf-mirror", "postgresql.conf")
        assert kwargs["volumes"][mirror] == {"bind": PG_MOUNT, "mode": "ro"}
        assert kwargs["volumes"][pinned.shared_db_volume]["bind"] == (
            "/var/lib/postgresql/data"
        )

    def test_container_on_the_mirror_is_kept(self, settings):
        source = _dev_conf(settings)
        mirror = os.path.join(settings.base_data_dir, "conf-mirror", "postgresql.conf")
        existing = _existing(
            [{"Destination": PG_MOUNT, "Source": mirror}], status="exited"
        )
        client = MagicMock()
        client.containers.get.return_value = existing
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            system_ops._ensure_pg_container(client, settings, {})

        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()
        existing.start.assert_called_once()
        # The mirror is refreshed on every start path, so an operator edit to
        # the source followed by `docker restart` takes effect.
        assert "shared_buffers = 1GB" in open(mirror).read()

    def test_unreadable_mounts_never_trigger_a_recreate(self, settings):
        source = _dev_conf(settings)
        existing = MagicMock()
        existing.status = "running"
        existing.attrs = {}  # truncated inspect: no Mounts at all
        client = MagicMock()
        client.containers.get.return_value = existing
        with patch.object(system_ops, "_resolve_conf", return_value=source):
            system_ops._ensure_pg_container(client, settings, {})
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()


class TestProdPgContainer:
    def test_new_container_mounts_the_mirror(self, settings):
        client = _absent_client()
        with patch("oduflow.wal_monitor.assert_writable"):
            system_ops._ensure_prod_pg_container(client, settings, {})

        kwargs = client.containers.run.call_args.kwargs
        mirror = os.path.join(
            settings.base_data_dir, "conf-mirror", "postgresql-prod.conf"
        )
        source = system_ops._prod_pg_conf_path(settings)
        assert kwargs["volumes"][mirror] == {"bind": PG_MOUNT, "mode": "ro"}
        assert source not in kwargs["volumes"]
        assert os.path.isfile(source)
        assert os.path.isfile(mirror)

    def test_stale_mount_source_warns_and_does_not_recreate(self, settings, caplog):
        # Production is never bounced behind the operator's back: the stale
        # mount is reported with the exact step to run instead.
        system_ops._ensure_prod_pg_conf(settings)
        old = _existing(
            [{"Destination": PG_MOUNT, "Source": "/etc/oduflow/postgresql-prod.conf"}]
        )
        client = MagicMock()
        client.containers.get.return_value = old
        with (
            patch("oduflow.wal_monitor.assert_writable"),
            caplog.at_level("WARNING", logger="oduflow"),
        ):
            system_ops._ensure_prod_pg_container(client, settings, {})

        old.stop.assert_not_called()
        old.remove.assert_not_called()
        client.containers.run.assert_not_called()
        warnings = [r for r in caplog.records if "cannot read" in r.getMessage()]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "/etc/oduflow/postgresql-prod.conf" in message
        assert (
            f"docker stop {settings.prod_db_container} && "
            f"docker rm {settings.prod_db_container}"
        ) in message
        # The mirror is still kept current so the operator's recreate finds it.
        assert os.path.isfile(
            os.path.join(settings.base_data_dir, "conf-mirror", "postgresql-prod.conf")
        )

    def test_container_on_the_mirror_is_silent(self, settings, caplog):
        system_ops._ensure_prod_pg_conf(settings)
        mirror = os.path.join(
            settings.base_data_dir, "conf-mirror", "postgresql-prod.conf"
        )
        existing = _existing([{"Destination": PG_MOUNT, "Source": mirror}])
        client = MagicMock()
        client.containers.get.return_value = existing
        with (
            patch("oduflow.wal_monitor.assert_writable"),
            caplog.at_level("WARNING", logger="oduflow"),
        ):
            system_ops._ensure_prod_pg_container(client, settings, {})
        assert not [r for r in caplog.records if "cannot read" in r.getMessage()]
        client.containers.run.assert_not_called()


class TestTraefikContainer:
    def test_stale_dynamic_dir_mount_recreates(self, settings):
        # Same command as today, but the dynamic dir is still bound from the
        # config dir: recreate onto the mirror, like any other drift.
        existing = _existing(
            [
                {
                    "Destination": TRAEFIK_MOUNT,
                    "Source": os.path.join(settings.etc_dir, "traefik-dynamic"),
                }
            ]
        )
        existing.attrs["Config"] = {
            "Cmd": [
                "--entrypoints.web.address=:80",
                "--entrypoints.web.forwardedHeaders.insecure=true",
                f"--providers.file.directory={TRAEFIK_MOUNT}",
            ]
        }
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, settings)

        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        kwargs = client.containers.run.call_args.kwargs
        mirror = os.path.join(settings.base_data_dir, "conf-mirror", "traefik-dynamic")
        assert kwargs["volumes"][mirror] == {"bind": TRAEFIK_MOUNT, "mode": "ro"}
        assert os.path.isfile(os.path.join(mirror, "oduflow.yml"))

    def test_container_on_the_mirror_is_kept(self, settings):
        mirror = os.path.join(settings.base_data_dir, "conf-mirror", "traefik-dynamic")
        existing = _existing([{"Destination": TRAEFIK_MOUNT, "Source": mirror}])
        existing.attrs["Config"] = {
            "Cmd": [
                "--entrypoints.web.address=:80",
                "--entrypoints.web.forwardedHeaders.insecure=true",
                f"--providers.file.directory={TRAEFIK_MOUNT}",
            ]
        }
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, settings)
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()


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


class TestSelfMountCheck:
    @pytest.fixture(autouse=True)
    def _in_container(self, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "abc123")
        with patch("oduflow.self_update.in_container", return_value=True):
            yield

    def test_off_on_a_plain_host(self, settings):
        with patch("oduflow.self_update.in_container", return_value=False):
            result = health._check_self_mounts(MagicMock(), settings)
        assert result["status"] == "off"

    def test_ok_when_data_dir_is_bound_from_the_same_host_path(self, settings):
        client = _self_client(
            [
                {
                    "Type": "bind",
                    "Source": settings.base_data_dir,
                    "Destination": settings.base_data_dir,
                }
            ]
        )
        assert health._check_self_mounts(client, settings)["status"] == "ok"

    def test_ok_when_data_dir_is_inside_a_same_path_bind(self, settings):
        parent = os.path.dirname(settings.base_data_dir)
        client = _self_client(
            [{"Type": "bind", "Source": parent, "Destination": parent}]
        )
        assert health._check_self_mounts(client, settings)["status"] == "ok"

    def test_warns_on_a_named_volume(self, settings):
        # The docs' old compose example: `oduflow_data:/srv/oduflow`. The
        # daemon resolves /srv/oduflow/... on the host, where it does not exist.
        client = _self_client(
            [
                {
                    "Type": "volume",
                    "Name": "oduflow_data",
                    "Source": "/var/lib/docker/volumes/oduflow_data/_data",
                    "Destination": settings.base_data_dir,
                }
            ]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "volume" in result["detail"]
        # /healthz is public: no host paths in the public detail.
        assert settings.base_data_dir not in result["detail"]

    def test_warns_when_host_path_differs(self, settings):
        client = _self_client(
            [
                {
                    "Type": "bind",
                    "Source": "/Users/me/oduflow",
                    "Destination": settings.base_data_dir,
                }
            ]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "different host path" in result["detail"]

    def test_warns_when_data_dir_is_in_the_writable_layer(self, settings):
        client = _self_client(
            [{"Type": "bind", "Source": "/run/docker.sock", "Destination": "/run/x"}]
        )
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "warn"
        assert "writable layer" in result["detail"]

    def test_off_when_own_container_is_not_found(self, settings):
        client = _self_client([], hostname="someone-else")
        result = health._check_self_mounts(client, settings)
        assert result["status"] == "off"
        assert "not verified" in result["detail"]

    def test_startup_warning_names_host_paths(self, settings, caplog):
        client = _self_client(
            [
                {
                    "Type": "bind",
                    "Source": "/Users/me/oduflow",
                    "Destination": settings.base_data_dir,
                }
            ]
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            caplog.at_level("WARNING", logger="oduflow"),
        ):
            assert health.warn_if_data_dir_unreachable(settings) == "warn"
        message = "\n".join(r.getMessage() for r in caplog.records)
        assert "Docker-out-of-Docker mount check" in message
        assert "/Users/me/oduflow" in message
        assert f"-v {settings.base_data_dir}:{settings.base_data_dir}" in message

    def test_startup_check_never_raises(self, settings):
        with patch(
            "oduflow.docker_ops.client.get_client",
            side_effect=RuntimeError("no docker"),
        ):
            assert health.warn_if_data_dir_unreachable(settings) is None

    def test_collect_health_reports_but_does_not_degrade(self, settings):
        health._cache["result"] = None
        client = _self_client(
            [
                {
                    "Type": "volume",
                    "Name": "oduflow_data",
                    "Source": "/var/lib/docker/volumes/oduflow_data/_data",
                    "Destination": settings.base_data_dir,
                }
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
