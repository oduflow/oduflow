"""Staging database dumps through the pg_exchange mount.

A dump written straight into a directory the PostgreSQL container also sees
costs one write to its final filesystem, and a restore can read it in place
instead of having a full-size copy pushed into the container's writable layer.
The mount is only attached when the shared container is created, so an existing
installation must keep working unchanged — detection is by inspection, never by
recreating the container behind the operator's back.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError
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


# --------------------------------------------------------------------------
# _staged_db_dump
# --------------------------------------------------------------------------


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
        client, settings, team, "oduflow_7_main", str(dest)
    ) as (host_path, container_path):
        assert container_path.startswith("/exchange/team_7/")
        assert os.path.dirname(host_path) == str(host_dir)
        assert os.path.exists(host_path)

    assert dest.read_text() == "PGDMP staged"
    assert list(host_dir.iterdir()) == []
    cmd = container.exec_run.call_args.args[0]
    assert cmd[:4] == ["pg_dump", "-U", settings.db_user, "-Fc"]
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
            client, settings, team, "oduflow_7_main", str(dest)
        ) as (host_path, container_path):
            assert container_path is None
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
            client, settings, team, "oduflow_7_main", str(dest)
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
            client, settings, team, "oduflow_7_main", str(dest)
        ):
            pytest.fail("body must not run after pg_dump fails")

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
            client, settings, team, "oduflow_7_main", str(dest)
        ):
            pytest.fail("body must not run after Docker exec fails")

    assert not dest.exists()
    assert list(host_dir.iterdir()) == []


# --------------------------------------------------------------------------
# reload_template
# --------------------------------------------------------------------------


def test_reload_skips_the_container_copy_for_an_already_readable_dump(tmp_path):
    team = _team(tmp_path)
    os.makedirs(team.get_template_dir("prod"), exist_ok=True)
    with open(team.get_template_sql_path("prod"), "wb") as fh:
        fh.write(b"PGDMP")

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
        patch.object(system_ops, "_copy_file_to_container") as copy_in,
    ):
        system_ops.reload_template(
            Settings(),
            team,
            template_name="prod",
            container_dump_path="/exchange/team_7/staged.pgdump",
        )

    copy_in.assert_not_called()
    cmds = [call.args[0] for call in db_container.exec_run.call_args_list]
    restore = next(cmd for cmd in cmds if cmd[0] == "pg_restore")
    assert restore[-1] == "/exchange/team_7/staged.pgdump"
    # The staged dump belongs to the caller; only copies made here are removed.
    assert not [cmd for cmd in cmds if cmd[:2] == ["rm", "-f"]]


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
        patch.object(system_ops.shutil, "copy2") as copy_dump,
    ):
        system_ops.reload_template(
            Settings(),
            team,
            template_name="prod",
            dump_path=str(staged),
            container_dump_path="/exchange/team_7/staged.pgdump",
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
            container_name=settings.prod_db_container,
        ) as (host_path, container_path):
            assert container_path.startswith("/exchange/team_7/")
            assert os.path.dirname(host_path) == str(host_dir)

    assert dest.read_text() == "PGDMP streamed from prod"
    assert list(host_dir.iterdir()) == []
    assert streamed == [
        (
            prod_container,
            ["pg_dump", "-U", settings.db_user, "-Fc", "oduflow_7_prod-erp"],
        )
    ]
    prod_container.exec_run.assert_not_called()
