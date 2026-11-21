"""The dashboard's service Shell: a ``docker exec -it <container> sh`` terminal."""

from __future__ import annotations

import json
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import docker
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings


def _docker_client(status="running", labels=None):
    """A fake Docker client whose exec socket is one end of a socketpair.

    The exec has already exited by default, so teardown has nothing to end.
    """
    ours, theirs = socket.socketpair()
    client = MagicMock()
    if labels is None:
        labels = {"oduflow.team": "1", "oduflow.service": "redis"}
    client.containers.get.return_value = MagicMock(
        id="cid", status=status, labels=labels
    )
    client.api.exec_create.return_value = {"Id": "exec-1"}
    client.api.exec_start.return_value = MagicMock(_sock=theirs)
    client.api.exec_inspect.return_value = {"Running": False}
    return client, ours


def _kills(docker_client):
    container = docker_client.containers.get.return_value
    return [call.args[0] for call in container.exec_run.call_args_list]


def test_shell_runs_sh_with_a_tty_and_relays_both_ways(tmp_path):
    client, settings = _client(tmp_path)
    docker_client, exec_end = _docker_client()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            # The shell reports its PID first; that line never reaches the
            # terminal.
            exec_end.sendall(b"7\r\n/data # ")
            assert ws.receive_text() == "/data # "

            ws.send_text(json.dumps({"type": "input", "data": "ls\n"}))
            assert exec_end.recv(16) == b"ls\n"

            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            # A later frame proves the resize one was already processed.
            ws.send_text(json.dumps({"type": "input", "data": "x"}))
            assert exec_end.recv(16) == b"x"
    exec_end.close()

    docker_client.containers.get.assert_called_once_with(
        f"{settings.prefix}1-svc-redis"
    )
    args, kwargs = docker_client.api.exec_create.call_args
    # `sh`, wrapped only to report its PID before becoming the shell.
    assert args == ("cid", ["sh", "-c", 'echo $$; exec "$@"', "sh", "sh"])
    assert kwargs["tty"] is True and kwargs["stdin"] is True
    # A bare `docker exec`: the image's default user, not a forced root.
    assert "user" not in kwargs
    docker_client.api.exec_resize.assert_called_once_with(
        "exec-1", height=40, width=120
    )


def test_session_ends_when_the_shell_exits(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            exec_end.sendall(b"7\r\n")
            exec_end.sendall(b"bye\r\n")
            assert ws.receive_text() == "bye\r\n"
            exec_end.close()  # `exit` in the shell: EOF on the exec socket
            assert ws.receive()["type"] == "websocket.close"

    assert _kills(docker_client) == []


def test_closing_the_terminal_hands_the_shell_over_for_cleanup(tmp_path):
    """Docker leaves a TTY exec running when its client disconnects."""
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            exec_end.sendall(b"7\r\n/ # ")
            assert ws.receive_text() == "/ # "
        assert scheduled.wait(5)
    exec_end.close()

    container = docker_client.containers.get.return_value
    # The image's default user started the shell, so it also ends it.
    end_exec.assert_called_once_with(docker_client, container, "exec-1", 7, "")


def test_input_typed_before_the_pid_line_is_held_back(tmp_path):
    """The PTY echoes input at once: an echo ahead of the PID line hides it."""
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "ls\n"}))
            # Frames are handled in order, so once the resize went through
            # the keystrokes before it have been dealt with.
            ws.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
            deadline = time.monotonic() + 5
            while not docker_client.api.exec_resize.called:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            exec_end.settimeout(0.2)
            with pytest.raises(socket.timeout):
                exec_end.recv(16)
            exec_end.settimeout(5)

            exec_end.sendall(b"7\r\n/ # ")
            assert ws.receive_text() == "/ # "
            # Released in order once the PID is known.
            assert exec_end.recv(16) == b"ls\n"
        assert scheduled.wait(5)
    exec_end.close()

    assert end_exec.call_args.args[3] == 7


def test_a_character_split_across_reads_is_not_mangled(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            # "caf\u00e9": the two bytes of its last letter arrive in separate reads.
            exec_end.sendall(b"7\r\ncaf\xc3")
            assert ws.receive_text() == "caf"
            exec_end.sendall(b"\xa9\r\n")
            assert ws.receive_text() == "\u00e9\r\n"
    exec_end.close()


def test_a_character_cut_off_by_the_end_of_the_stream_is_marked(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            exec_end.sendall(b"7\r\ncaf\xc3")
            assert ws.receive_text() == "caf"
            exec_end.close()
            assert ws.receive_text() == "\ufffd"
            assert ws.receive()["type"] == "websocket.close"


def test_no_cleanup_when_the_shell_never_reported_a_pid(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    message = b'OCI runtime exec failed: exec: "sh": executable file not found'

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch("oduflow.web_ui._end_lingering_exec") as end_exec,
    ):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            exec_end.sendall(message)
            exec_end.close()
            # Not a PID line: shown as is instead of being swallowed.
            assert ws.receive_text() == message.decode()
            assert ws.receive()["type"] == "websocket.close"

    end_exec.assert_not_called()


def test_unknown_service_reports_not_found(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    exec_end.close()
    docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            assert "service not found" in ws.receive_text()
            assert ws.receive()["type"] == "websocket.close"

    docker_client.api.exec_create.assert_not_called()


def test_a_container_that_is_not_a_service_is_not_found(tmp_path):
    """An environment named ``svc`` owns ``<prefix><team>-svc-odoo`` too."""
    client, settings = _client(tmp_path)
    docker_client, exec_end = _docker_client(labels={"oduflow.branch": "svc"})
    exec_end.close()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/odoo/shell") as ws:
            assert "service not found" in ws.receive_text()
            assert ws.receive()["type"] == "websocket.close"

    docker_client.containers.get.assert_called_once_with(f"{settings.prefix}1-svc-odoo")
    docker_client.api.exec_create.assert_not_called()


def test_another_teams_service_is_not_found(tmp_path):
    """Team ``a``'s name ``b-svc-x`` is the container of team ``a-svc-b``'s ``x``."""
    team = TeamSettings(team_id="a", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"a": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    docker_client, exec_end = _docker_client(
        labels={"oduflow.team": "a-svc-b", "oduflow.service": "x"}
    )
    exec_end.close()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with TestClient(app).websocket_connect("/api/services/b-svc-x/shell") as ws:
            assert "service not found" in ws.receive_text()
            assert ws.receive()["type"] == "websocket.close"

    docker_client.containers.get.assert_called_once_with(
        f"{settings.prefix}a-svc-b-svc-x"
    )
    docker_client.api.exec_create.assert_not_called()


def test_stopped_service_is_refused(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client(status="exited")
    exec_end.close()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            assert "service is not running" in ws.receive_text()
            assert ws.receive()["type"] == "websocket.close"

    docker_client.api.exec_create.assert_not_called()


def test_invalid_service_name_never_reaches_docker(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    exec_end.close()

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/bad%20name/shell") as ws:
            assert "Invalid service name" in ws.receive_text()
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 1008

    docker_client.containers.get.assert_not_called()


def test_image_without_sh_surfaces_the_exec_error(tmp_path):
    client, _ = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    exec_end.close()
    docker_client.api.exec_start.side_effect = docker.errors.APIError(
        'exec: "sh": executable file not found in $PATH'
    )

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/services/redis/shell") as ws:
            assert "executable file not found" in ws.receive_text()
            assert ws.receive()["type"] == "websocket.close"
