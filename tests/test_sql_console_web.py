"""The dashboard's SQL console: an interactive ``psql`` on the environment DB.

``psql`` is a client: its ``\\!`` is a shell in the container it runs in, and
its ``\\c`` reconnects from there. In the shared PostgreSQL container that is a
root shell beside every team's data and a trust-authenticated socket that lets
any role name in, the cluster superuser included. So the console runs ``psql``
in the environment's own Odoo container instead, as the image's unprivileged
user, with only the scoped role's password.
"""

from __future__ import annotations

import json
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import docker
from oduflow.env_credentials import create_credentials
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_WRAPPER = ["sh", "-c", 'echo $$; exec "$@"', "sh"]
_SQL = "/api/environments/main/sql"


def _client(tmp_path, *, credentials=True):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    creds = (
        create_credentials("main", team.team_id, team.workspaces_dir)
        if credentials
        else None
    )
    return TestClient(app), settings, creds


def _docker_client(settings, *, status="running", labels=None, has_psql=True):
    """A fake Docker client that knows the Odoo and the PostgreSQL container.

    The exec socket is one end of a socketpair; the exec has already exited
    by default, so teardown has nothing to end.
    """
    ours, theirs = socket.socketpair()
    odoo = MagicMock(
        id="odoo-cid",
        status=status,
        labels={"oduflow.team": "1"} if labels is None else labels,
    )
    odoo.exec_run.return_value = (0, b"/usr/bin/psql\n") if has_psql else (127, b"")
    db = MagicMock(id="db-cid", status="running")
    containers = {
        f"{settings.prefix}1-main-odoo": odoo,
        settings.shared_db_container: db,
    }

    def get(name):
        if name not in containers:
            raise docker.errors.NotFound(name)
        return containers[name]

    client = MagicMock()
    client.containers.get.side_effect = get
    client.api.exec_create.return_value = {"Id": "exec-1"}
    client.api.exec_start.return_value = MagicMock(_sock=theirs)
    client.api.exec_inspect.return_value = {"Running": False}
    return client, odoo, db, ours


def _refused(client, docker_client, path=_SQL):
    """Open the console and return the error line it is refused with."""
    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect(path) as ws:
            text = ws.receive_text()
            closed = ws.receive()
    assert closed["type"] == "websocket.close"
    # Refused before a PTY — or anything else — was started for it.
    docker_client.api.exec_create.assert_not_called()
    docker_client.containers.run.assert_not_called()
    return text, closed["code"]


def test_psql_runs_in_the_odoo_container_as_the_scoped_role(tmp_path):
    client, settings, creds = _client(tmp_path)
    docker_client, odoo, db, exec_end = _docker_client(settings)

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect(_SQL) as ws:
            exec_end.sendall(b"7\r\noduflow_1_main=> ")
            assert ws.receive_text() == "oduflow_1_main=> "

            ws.send_text(json.dumps({"type": "input", "data": "select 1;\n"}))
            assert exec_end.recv(32) == b"select 1;\n"

            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            ws.send_text(json.dumps({"type": "input", "data": "x"}))
            assert exec_end.recv(16) == b"x"
    exec_end.close()

    args, kwargs = docker_client.api.exec_create.call_args
    # The environment's own container: the shared PostgreSQL one is neither
    # exec'd into nor even looked up.
    assert args[0] == "odoo-cid"
    docker_client.containers.get.assert_called_once_with(
        f"{settings.prefix}1-main-odoo"
    )
    db.exec_run.assert_not_called()
    # psql over the Docker network, where every role needs its password.
    assert args[1] == [
        *_WRAPPER,
        "psql",
        "-h",
        settings.shared_db_container,
        "-p",
        "5432",
        "-U",
        creds["pg_user"],
        "-d",
        "oduflow_1_main",
    ]
    assert kwargs["tty"] is True and kwargs["stdin"] is True
    # Unprivileged, and the scoped role's password only in the environment.
    assert kwargs["user"] == "odoo"
    assert kwargs["environment"] == {"PGPASSWORD": creds["pg_password"]}
    assert creds["pg_password"] not in " ".join(args[1])
    # Not the cluster superuser, by name or by password.
    assert creds["pg_user"] != settings.db_user
    assert settings.db_password not in kwargs["environment"].values()
    docker_client.api.exec_resize.assert_called_once_with(
        "exec-1", height=40, width=120
    )


def test_closing_the_console_ends_psql_as_the_user_that_started_it(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, odoo, _, exec_end = _docker_client(settings)
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect(_SQL) as ws:
            exec_end.sendall(b"42\r\n=> ")
            assert ws.receive_text() == "=> "
        assert scheduled.wait(5)
    exec_end.close()

    end_exec.assert_called_once_with(docker_client, odoo, "exec-1", 42, "odoo")


def test_environment_without_a_scoped_role_is_refused(tmp_path):
    """A legacy environment's only credential is the cluster superuser's."""
    client, settings, _ = _client(tmp_path, credentials=False)
    docker_client, odoo, db, exec_end = _docker_client(settings)
    exec_end.close()

    text, code = _refused(client, docker_client)

    assert "no scoped database credentials" in text
    assert code == 1011
    odoo.exec_run.assert_not_called()
    db.exec_run.assert_not_called()


def test_stopped_environment_is_refused_without_a_fallback(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, odoo, db, exec_end = _docker_client(settings, status="exited")
    exec_end.close()

    text, code = _refused(client, docker_client)

    assert "is not running" in text
    assert code == 1011
    odoo.exec_run.assert_not_called()
    db.exec_run.assert_not_called()


def test_image_without_psql_is_refused_without_a_fallback(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, odoo, db, exec_end = _docker_client(settings, has_psql=False)
    exec_end.close()

    text, code = _refused(client, docker_client)

    assert "cannot run psql" in text and "postgresql-client" in text
    assert code == 1011
    odoo.exec_run.assert_called_once_with(["sh", "-c", "command -v psql"], user="odoo")
    db.exec_run.assert_not_called()


def test_unknown_environment_is_not_found(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, _, db, exec_end = _docker_client(settings)
    exec_end.close()

    text, code = _refused(client, docker_client, "/api/environments/other/sql")

    assert "does not exist" in text
    assert code == 1011
    db.exec_run.assert_not_called()


def test_another_teams_container_is_not_found(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, odoo, _, exec_end = _docker_client(
        settings, labels={"oduflow.team": "2"}
    )
    exec_end.close()

    text, code = _refused(client, docker_client)

    assert "does not exist" in text
    assert code == 1011
    odoo.exec_run.assert_not_called()


@pytest.mark.parametrize("name", ["prod-erp", "Prod-erp"])
def test_production_is_rejected_before_any_lookup(tmp_path, name):
    """Nothing here may exec inside a production's container."""
    client, settings, _ = _client(tmp_path)
    docker_client, _, _, exec_end = _docker_client(settings)
    exec_end.close()

    text, code = _refused(client, docker_client, f"/api/environments/{name}/sql")

    assert "production environment" in text
    assert code == 1008
    docker_client.containers.get.assert_not_called()


def test_invalid_name_is_rejected_before_any_lookup(tmp_path):
    client, settings, _ = _client(tmp_path)
    docker_client, _, _, exec_end = _docker_client(settings)
    exec_end.close()

    # A backslash: such a name must not reach the credential path lookup.
    text, code = _refused(client, docker_client, "/api/environments/a%5Cb/sql")

    assert "Invalid environment name" in text
    assert code == 1008
    docker_client.containers.get.assert_not_called()
