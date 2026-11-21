"""Exercise service storage, routing and recovery on the enrolled test engine."""

import time
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest
from tool_helpers import call_tool

import docker
from oduflow import server
from oduflow.docker_ops import service_database_ops as db
from oduflow.docker_ops import service_ops as svc
from oduflow.docker_ops import system_ops
from oduflow.docker_ops import volume_file_ops as files
from oduflow.docker_ops import volume_ops as volumes
from oduflow.docker_ops.client import get_client, run_for_output
from oduflow.errors import ConflictError, ProtectedError
from oduflow.naming import get_team_network_name

pytestmark = pytest.mark.integration


def _route(path, status, body=None):
    deadline = time.monotonic() + 30
    while True:
        try:
            response = httpx.get(
                f"http://127.0.0.1{path}",
                headers={"Host": "files.qa.test"},
                trust_env=False,
                timeout=2,
            )
        except httpx.TransportError as exc:
            observed = str(exc)
        else:
            if response.status_code == status and (
                body is None or body == response.text
            ):
                return
            observed = f"{response.status_code}/{response.text[:120]}"
        if time.monotonic() >= deadline:
            pytest.fail(f"{path}: expected {status}/{body}, got {observed}")
        time.sleep(0.25)


def test_service_routes_and_volume_survive_restore(live_environment, monkeypatch):
    settings, team = live_environment
    team = replace(team, hostname="qa.test")
    settings = replace(
        settings, routing_mode="traefik", routing_tls=False, teams={team.team_id: team}
    )
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx: team)
    system_ops.init_system(settings)
    with ExitStack() as cleanup:
        volumes.create_volume(settings, team, "assets")
        cleanup.callback(volumes.delete_volume, settings, team, "assets")
        files.write_file_in_volume(
            settings, team, "assets", "proof.txt", "version one\nversion two\n"
        )
        assert (
            files.read_file_in_volume(settings, team, "assets", "proof.txt", "2:2")[
                "output"
            ]
            == "version two\n"
        )
        assert "proof.txt:2:version two" in str(
            files.search_in_volume(
                settings, team, "assets", "version two", glob="*.txt"
            )
        )
        svc.create_service(
            settings,
            team,
            "files",
            "busybox:1.36.1",
            None,
            volumes=[{"volume": "assets", "mount_path": "/www", "mode": "ro"}],
            routes=[{"path": "/assets", "port": 18324, "strip_prefix": True}],
            command=["httpd", "-f", "-p", "18324", "-h", "/www"],
        )

        def remove_service():
            if any(s["name"] == "files" for s in svc.list_services(settings, team)):
                svc.set_service_protected(settings, team, "files", False)
                svc.delete_service(settings, team, "files", save_preset=False)

        cleanup.callback(remove_service)
        _route("/assets/proof.txt", 200, "version one\nversion two\n")
        _route("/proof.txt", 404)
        assert volumes.inspect_volume(settings, team, "assets")["used_by"] == ["files"]
        with pytest.raises(ConflictError):
            volumes.delete_volume(settings, team, "assets")
        svc.set_service_protected(settings, team, "files", True)
        with pytest.raises(ProtectedError):
            svc.delete_service(settings, team, "files")
        svc.set_service_protected(settings, team, "files", False)
        svc.restart_service(settings, team, "files")
        svc.update_service(
            settings,
            team,
            "files",
            host_mode_override=True,
            env_override={"COLOR": "blue"},
        )
        assert (
            svc.run_command_in_service(settings, team, "files", "printenv COLOR")[
                "output"
            ].strip()
            == "blue"
        )
        _route("/assets/proof.txt", 200, "version one\nversion two\n")
        svc.delete_service(settings, team, "files")
        _route("/assets/proof.txt", 404)
        call_tool("restore_service", name="files")
        _route("/assets/proof.txt", 200, "version one\nversion two\n")
        files.delete_file_in_volume(settings, team, "assets", "proof.txt")
        _route("/assets/proof.txt", 404)


def test_service_database_survives_pg_upgrade_and_rotation(live_environment):
    from oduflow import postgres_migration

    settings, team = live_environment
    system_ops.init_system(settings)
    record = db.create_database(settings, team, "records")

    def query(credentials, sql):
        return run_for_output(
            get_client(),
            settings.postgres_image,
            [
                "psql",
                "-w",
                "-h",
                credentials["host"],
                "-U",
                record["username"],
                "-d",
                record["database"],
                "-tAX",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            network=get_team_network_name(team.team_id, settings.prefix),
            environment={"PGPASSWORD": credentials["password"]},
        )

    query(record, "CREATE TABLE sample(k int); INSERT INTO sample VALUES (1463)")
    settings = replace(
        settings, postgres_image="postgres:17", postgres_image_explicit=True
    )
    postgres_migration.preflight(settings)
    postgres_migration.upgrade(settings)
    assert (
        query(
            record,
            "SELECT current_setting('server_version_num')::int/10000,k FROM sample",
        ).strip()
        == b"17|1463"
    )
    rotated = db.rotate_password(settings, team, "records")
    assert query(rotated, "SELECT k FROM sample").strip() == b"1463"
    with pytest.raises(
        docker.errors.ContainerError, match="password authentication failed"
    ):
        query(record, "SELECT k FROM sample")
    db.set_protected(team, "records", True)
    with pytest.raises(ProtectedError):
        db.delete_database(settings, team, "records")
    db.set_protected(team, "records", False)
    db.delete_database(settings, team, "records")
    assert db.list_databases(settings, team) == []
