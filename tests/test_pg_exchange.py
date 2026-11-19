"""Staging database dumps through the pg_exchange mount.

A dump written straight into a directory the PostgreSQL container also sees
costs one write to its final filesystem instead of a full-size copy through the
container's writable layer. Restores do not go through the mount: the restore
client reads the staged file from the host.
The mount is only attached when the shared container is created, so an existing
installation must keep working unchanged — detection is by inspection, never by
recreating the container behind the operator's back.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


def _settings(tmp_path):
    return Settings(base_data_dir=str(tmp_path))


def _team(tmp_path):
    return TeamSettings(team_id="7", data_dir=str(tmp_path / "team_7"))


def _client(mounts):
    container = MagicMock()
    container.attrs = {"Mounts": mounts} if mounts is not None else MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def test_exchange_dirs_when_mounted(tmp_path):
    client, _ = _client([{"Destination": "/tablespaces"}, {"Destination": "/exchange"}])

    dirs = system_ops._pg_exchange_dirs(client, _settings(tmp_path), _team(tmp_path))

    assert dirs == (str(tmp_path / "pg_exchange" / "team_7"), "/exchange/team_7")


def test_no_exchange_on_a_container_predating_the_mount(tmp_path):
    # Existing installations keep the streaming path until their PostgreSQL
    # container happens to be recreated; nothing is migrated for them.
    client, _ = _client([{"Destination": "/tablespaces"}])

    assert (
        system_ops._pg_exchange_dirs(client, _settings(tmp_path), _team(tmp_path))
        is None
    )


def test_no_exchange_when_mounts_are_not_a_list(tmp_path):
    client, _ = _client(None)

    assert (
        system_ops._pg_exchange_dirs(client, _settings(tmp_path), _team(tmp_path))
        is None
    )


def test_no_exchange_without_a_postgres_container(tmp_path):
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("gone")

    assert (
        system_ops._pg_exchange_dirs(client, _settings(tmp_path), _team(tmp_path))
        is None
    )


def test_pg_container_gets_the_exchange_mount(tmp_path):
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("absent")
    settings = _settings(tmp_path)

    with patch.object(system_ops, "_resolve_conf", return_value=tmp_path / "pg.conf"):
        system_ops._ensure_pg_container(client, settings, {})

    volumes = client.containers.run.call_args.kwargs["volumes"]
    assert volumes[str(tmp_path / "pg_exchange")] == {
        "bind": "/exchange",
        "mode": "rw",
    }
    # The team's data dir stays out: PostgreSQL has no business seeing
    # workspaces or credentials.
    assert not any(str(tmp_path / "team_7") in host for host in volumes)
    assert (tmp_path / "pg_exchange").is_dir()


@pytest.mark.parametrize("existing_container", [False, True])
def test_pg_mount_parents_are_searchable_without_opening_team_data(
    tmp_path, existing_container
):
    client = MagicMock()
    if existing_container:
        client.containers.get.return_value.status = "running"
    else:
        client.containers.get.side_effect = docker.errors.NotFound("absent")
    previous_umask = os.umask(0o077)
    try:
        children = []
        for name in ("pg_tablespaces", "pg_exchange"):
            child = tmp_path / name / "team_7"
            child.mkdir(parents=True)
            (child / "private").write_text("private test fixture")
            children.append(child)
        with patch.object(
            system_ops, "_resolve_conf", return_value=tmp_path / "pg.conf"
        ):
            system_ops._ensure_pg_container(client, _settings(tmp_path), {})
    finally:
        os.umask(previous_umask)

    for child in children:
        assert child.parent.stat().st_mode & 0o777 == 0o755
        assert child.stat().st_mode & 0o777 == 0o700
        assert (child / "private").stat().st_mode & 0o777 == 0o600
    if existing_container:
        client.containers.run.assert_not_called()


# --------------------------------------------------------------------------
# _staged_db_dump
# --------------------------------------------------------------------------


# The scoped role of the database being dumped.
ROLE = "u_7_main"


def _dump_into_exchange(host_dir):
    """exec_run stand-in: write the file pg_dump was told to write."""

    def _run(cmd, **kwargs):
        target = cmd[cmd.index("-f") + 1]
        with open(os.path.join(host_dir, os.path.basename(target)), "w") as fh:
            fh.write("PGDMP staged")
        return 0, b""

    return _run


def test_staged_dump_writes_through_the_mount_and_moves_into_place(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    host_dir = tmp_path / "pg_exchange" / "team_7"
    host_dir.mkdir(parents=True)
    client, container = _client([{"Destination": "/exchange"}])
    container.exec_run.side_effect = _dump_into_exchange(str(host_dir))
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    with system_ops._staged_db_dump(
        client, settings, team, "oduflow_7_main", str(dest), role=ROLE
    ) as host_path:
        assert os.path.dirname(host_path) == str(host_dir)
        assert os.path.exists(host_path)

    assert dest.read_text() == "PGDMP staged"
    assert list(host_dir.iterdir()) == []
    cmd = container.exec_run.call_args.args[0]
    # The database's own role dumps it, not the cluster superuser.
    assert cmd[:4] == ["pg_dump", "-U", ROLE, "-Fc"]
    assert settings.db_user not in cmd
    # pg_dump writes the archive itself, through the mount.
    target = cmd[cmd.index("-f") + 1]
    assert target.startswith("/exchange/team_7/")
    assert cmd[-1] == "oduflow_7_main"


def test_staged_dump_streams_when_the_mount_is_absent(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    client, _ = _client([])
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    def _stream(client_, container_, cmd, path, *, tool):
        with open(path, "w") as fh:
            fh.write("PGDMP streamed")

    with patch.object(system_ops, "_stream_exec_to_file", side_effect=_stream):
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=ROLE
        ) as host_path:
            assert host_path == str(dest) + ".staged"

    assert dest.read_text() == "PGDMP streamed"
    assert not os.path.exists(str(dest) + ".staged")


def test_staged_dump_leaves_nothing_behind_when_the_restore_fails(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    host_dir = tmp_path / "pg_exchange" / "team_7"
    host_dir.mkdir(parents=True)
    client, container = _client([{"Destination": "/exchange"}])
    container.exec_run.side_effect = _dump_into_exchange(str(host_dir))
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    with pytest.raises(RuntimeError):
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=ROLE
        ):
            raise RuntimeError("restore blew up")

    # A failed publish must not leave a dump for a template that was never built.
    assert not dest.exists()
    assert list(host_dir.iterdir()) == []


def test_staged_dump_reports_pg_dump_failure(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    client, container = _client([{"Destination": "/exchange"}])
    container.exec_run.return_value = (1, b"pg_dump: error: no such database")
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    with pytest.raises(ExternalCommandError, match="no such database"):
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=ROLE
        ):
            pytest.fail("body must not run after pg_dump fails")

    assert not dest.exists()


def test_staged_dump_explains_what_the_role_may_not_read(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    client, container = _client([{"Destination": "/exchange"}])
    container.exec_run.return_value = (
        1,
        b"pg_dump: error: query failed: ERROR:  permission denied for table x",
    )
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    with pytest.raises(ExternalCommandError) as exc_info:
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=ROLE
        ):
            pytest.fail("body must not run after pg_dump fails")

    # The reason pg_dump gave, and what to do about it.
    assert "permission denied for table x" in exc_info.value.output
    assert "never by the cluster superuser" in exc_info.value.output
    assert "update_environment" in exc_info.value.output
    assert "reconfigure_production" in exc_info.value.output


def test_staged_dump_is_never_taken_by_the_superuser(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    client, container = _client([{"Destination": "/exchange"}])
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    with pytest.raises(PrerequisiteNotMetError, match="as the PostgreSQL superuser"):
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=settings.db_user
        ):
            pytest.fail("body must not run")

    container.exec_run.assert_not_called()
    assert not dest.exists()


def test_staged_dump_cleans_partial_file_when_docker_exec_raises(tmp_path):
    settings, team = _settings(tmp_path), _team(tmp_path)
    host_dir = tmp_path / "pg_exchange" / "team_7"
    host_dir.mkdir(parents=True)
    client, container = _client([{"Destination": "/exchange"}])
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)

    def _disconnect_after_writing(cmd, **kwargs):
        target = cmd[cmd.index("-f") + 1]
        with open(host_dir / os.path.basename(target), "w") as fh:
            fh.write("partial dump")
        raise docker.errors.APIError("daemon connection lost")

    container.exec_run.side_effect = _disconnect_after_writing

    with pytest.raises(docker.errors.APIError, match="connection lost"):
        with system_ops._staged_db_dump(
            client, settings, team, "oduflow_7_main", str(dest), role=ROLE
        ):
            pytest.fail("body must not run after Docker exec fails")

    assert not dest.exists()
    assert list(host_dir.iterdir()) == []


# --------------------------------------------------------------------------
# reload_template
# --------------------------------------------------------------------------


def test_reload_restores_a_staged_dump_from_its_host_path(tmp_path):
    # A dump is tenant input: it is neither linked nor copied into the shared
    # PostgreSQL container. The restore client reads the staged file itself.
    settings, team = _settings(tmp_path), _team(tmp_path)
    source = tmp_path / "pg_exchange" / "team_7" / "staged.pgdump"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"PGDMP imported from S3")

    db_container = MagicMock()
    db_container.attrs = {"Mounts": [{"Destination": "/exchange"}]}
    db_container.exec_run.return_value = (0, b"")
    client = MagicMock()
    client.containers.get.return_value = db_container

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_exec_sql", return_value="5"),
        patch.object(system_ops, "_db_exists", return_value=False),
        patch.object(system_ops, "_is_text_dump", return_value=False),
        patch.object(
            system_ops, "ensure_team_tablespace", return_value="oduflow_team_7"
        ),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(system_ops, "_copy_file_to_container") as copy_in,
        patch.object(
            system_ops, "_run_restore_client", return_value=(0, "")
        ) as restore,
    ):
        system_ops.reload_template(
            settings,
            team,
            template_name="prod",
            dump_path=str(source),
            persist_dump=False,
        )

    restore.assert_called_once()
    assert restore.call_args.kwargs["dump_path"] == str(source)
    copy_in.assert_not_called()
    db_container.exec_run.assert_not_called()
    assert [entry.name for entry in source.parent.iterdir()] == ["staged.pgdump"]
    assert source.read_bytes() == b"PGDMP imported from S3"


# --------------------------------------------------------------------------
# stale staging sweep
# --------------------------------------------------------------------------


def test_sweep_removes_only_abandoned_staging_files(tmp_path):
    team_dir = tmp_path / "pg_exchange" / "team_7"
    team_dir.mkdir(parents=True)
    hexid = "0123456789abcdef0123456789abcdef"
    staged = [
        team_dir / f"oduflow-restore-{hexid}",
        team_dir / f"oduflow_7_main-{hexid}.pgdump",
        team_dir / f"oduflow_7_prod-{hexid}.pgdump.part",
    ]
    unrelated = [
        team_dir / "operator-notes.pgdump",
        team_dir / "operator-notes.pgdump.part",
    ]
    for path in (*staged, *unrelated):
        path.write_bytes(b"x")
    # Hardlinks carry the source's mtime, which says nothing about the link.
    os.utime(staged[0], (0, 0))

    # A restore started moments ago may still be running (e.g. a CLI
    # ``stack apply`` next to a starting server): leave it alone.
    assert system_ops.sweep_pg_exchange(_settings(tmp_path)) == 0
    assert set(team_dir.iterdir()) == set(staged + unrelated)

    later = system_ops.time.time() + system_ops._PG_EXCHANGE_STALE_SECONDS + 60
    with patch.object(system_ops.time, "time", return_value=later):
        assert system_ops.sweep_pg_exchange(_settings(tmp_path)) == len(staged)

    assert set(team_dir.iterdir()) == set(unrelated)


def test_sweep_tolerates_a_missing_exchange_dir(tmp_path):
    assert system_ops.sweep_pg_exchange(_settings(tmp_path)) == 0


def test_startup_reclaims_abandoned_dumps_before_quota_setup(tmp_path):
    from oduflow import server

    settings = _settings(tmp_path)
    team_dir = tmp_path / "pg_exchange" / "team_7"
    team_dir.mkdir(parents=True)
    staged = team_dir / f"oduflow-restore-{'a' * 32}"
    staged.write_bytes(b"abandoned dump")
    later = staged.stat().st_ctime + system_ops._PG_EXCHANGE_STALE_SECONDS + 60

    def apply_quotas(_settings):
        # Real XFS project setup refreshes ctime on every file it visits. The
        # abandoned dump must already be gone when quota setup reaches it.
        assert not staged.exists()

    with (
        patch.object(sys, "argv", ["oduflow", "--no-telemetry", "-t", "http"]),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch("oduflow.docker_ops.client.wait_for_docker"),
        patch("oduflow.postgres_migration.validate_configuration"),
        patch.object(server.migrations, "run_pending"),
        patch("oduflow.postgres_migration.upgrade"),
        patch.object(server, "_ensure_initialized"),
        patch.object(system_ops.time, "time", return_value=later),
        patch.object(server.quotas, "apply_all", side_effect=apply_quotas) as quotas,
        patch.object(server, "_start_http") as start_http,
    ):
        server._run_cli()

    quotas.assert_called_once_with(settings)
    start_http.assert_called_once_with()


def test_reload_does_not_persist_a_dump_owned_by_the_staging_context(tmp_path):
    team = _team(tmp_path)
    staged = tmp_path / "pg_exchange" / "team_7" / "staged.pgdump"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"PGDMP")

    db_container = MagicMock()
    db_container.exec_run.return_value = (0, b"")
    client = MagicMock()
    client.containers.get.return_value = db_container

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_exec_sql", return_value="5"),
        patch.object(system_ops, "_db_exists", return_value=False),
        patch.object(system_ops, "_is_text_dump", return_value=False),
        patch.object(
            system_ops, "ensure_team_tablespace", return_value="oduflow_team_7"
        ),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(system_ops, "_run_restore_client", return_value=(0, "")),
        patch.object(system_ops.shutil, "copy2") as copy_dump,
    ):
        system_ops.reload_template(
            Settings(),
            team,
            template_name="prod",
            dump_path=str(staged),
            persist_dump=False,
        )

    copy_dump.assert_not_called()
    assert not os.path.exists(team.get_template_sql_path("prod"))


def test_staged_dump_from_another_cluster_streams_into_the_exchange_dir(tmp_path):
    # The production cluster has no exchange mount, so its dump is streamed out
    # through the exec API — but still lands in the dev cluster's exchange dir,
    # where the restore reads it in place. One full-size write, nothing in
    # either container's writable layer.
    settings, team = _settings(tmp_path), _team(tmp_path)
    host_dir = tmp_path / "pg_exchange" / "team_7"
    host_dir.mkdir(parents=True)
    dev_container = MagicMock()
    dev_container.attrs = {"Mounts": [{"Destination": "/exchange"}]}
    prod_container = MagicMock()
    client = MagicMock()
    client.containers.get.side_effect = lambda name: (
        prod_container if name == settings.prod_db_container else dev_container
    )
    dest = tmp_path / "templates" / "prod" / "dump.pgdump"
    dest.parent.mkdir(parents=True)
    streamed = []

    def _stream(client_, container_, cmd, path, *, tool):
        streamed.append((container_, cmd))
        with open(path, "w") as fh:
            fh.write("PGDMP streamed from prod")

    with patch.object(system_ops, "_stream_exec_to_file", side_effect=_stream):
        with system_ops._staged_db_dump(
            client,
            settings,
            team,
            "oduflow_7_prod-erp",
            str(dest),
            role="u_7_prod-erp",
            container_name=settings.prod_db_container,
        ) as host_path:
            assert os.path.dirname(host_path) == str(host_dir)

    assert dest.read_text() == "PGDMP streamed from prod"
    assert list(host_dir.iterdir()) == []
    assert streamed == [
        (
            prod_container,
            # The production's role, in the production cluster.
            ["pg_dump", "-U", "u_7_prod-erp", "-Fc", "oduflow_7_prod-erp"],
        )
    ]
    prod_container.exec_run.assert_not_called()
