"""Terminals must not leave their process behind in the container.

Docker keeps a TTY exec running after its client disconnects, so the Odoo
shell, the SQL console, the service shell and the Agent CLI all report their
PID up front and are ended once the terminal is gone.
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _end_lingering_exec, _pid_reporting_argv, mount_web_ui

_WRAPPER = ["sh", "-c", 'echo $$; exec "$@"', "sh"]


def _client(tmp_path, **team_options):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"), **team_options)
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def _docker_client():
    ours, theirs = socket.socketpair()
    client = MagicMock()
    client.containers.get.return_value = MagicMock(id="cid", status="running")
    client.api.exec_create.return_value = {"Id": "exec-1"}
    client.api.exec_start.return_value = MagicMock(_sock=theirs)
    return client, ours


def _kills(container):
    return [call.args[0] for call in container.exec_run.call_args_list]


def test_wrapper_keeps_the_command_and_its_arguments_intact():
    argv = ["psql", "-U", "role with space", "-d", "db"]
    assert _pid_reporting_argv(argv) == [*_WRAPPER, *argv]


@pytest.mark.parametrize(
    "endpoint,command",
    [
        ("terminal", ["/entrypoint.sh", "odoo", "shell"]),
        ("sql", ["psql", "-U", "env_role"]),
    ],
)
def test_environment_terminals_hand_their_process_over_for_cleanup(
    tmp_path, endpoint, command
):
    client = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.env_credentials.load_credentials",
            return_value={"pg_user": "env_role"},
        ),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect(f"/api/environments/main/{endpoint}") as ws:
            # The PID line is consumed; the terminal sees only what follows.
            exec_end.sendall(b"42\r\nprompt> ")
            assert ws.receive_text() == "prompt> "
        assert scheduled.wait(5)
    exec_end.close()

    argv = docker_client.api.exec_create.call_args.args[1]
    assert argv[: len(_WRAPPER)] == _WRAPPER
    assert argv[len(_WRAPPER) :][: len(command)] == command
    container = docker_client.containers.get.return_value
    end_exec.assert_called_once_with(docker_client, container, "exec-1", 42, "")


def test_agent_cli_hands_its_process_over_for_cleanup(tmp_path):
    """The agent container is long-lived: a closed console must not leave its
    agent — and the scoped MCP token in its env — running there."""
    client = _client(tmp_path, agent_enabled=True)
    docker_client, exec_end = _docker_client()
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch("oduflow.web_ui.env_ops.agent_env_uid", return_value=20001),
        patch("oduflow.web_ui.env_ops.refresh_agent_mcp_config"),
        patch("oduflow.web_ui.env_ops.get_env_token", return_value="env-token"),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect("/api/environments/main/agent?type=claude") as ws:
            exec_end.sendall(b"42\r\nclaude> ")
            assert ws.receive_text() == "claude> "
        assert scheduled.wait(5)
    exec_end.close()

    args, kwargs = docker_client.api.exec_create.call_args
    assert args[1][: len(_WRAPPER)] == _WRAPPER
    assert "claude" in args[1]
    assert kwargs["tty"] is True and kwargs["stdin"] is True
    assert kwargs["user"] == "20001:20001"
    assert kwargs["environment"]["ODUFLOW_MCP_TOKEN"] == "env-token"
    # Ended as the environment's own uid: the container's default user could
    # not signal that process.
    container = docker_client.containers.get.return_value
    end_exec.assert_called_once_with(
        docker_client, container, "exec-1", 42, "20001:20001"
    )


def test_cleanup_hangs_up_a_process_that_is_still_running():
    client = MagicMock()
    client.api.exec_inspect.side_effect = [{"Running": True}, {"Running": False}]
    container = MagicMock()

    with patch("oduflow.web_ui.time.sleep"):
        _end_lingering_exec(client, container, "exec-1", 7)

    assert _kills(container) == [["sh", "-c", "kill -HUP 7"]]


def test_cleanup_signals_as_the_user_the_process_runs_as():
    client = MagicMock()
    client.api.exec_inspect.return_value = {"Running": True}
    container = MagicMock()

    with patch("oduflow.web_ui.time.sleep"):
        _end_lingering_exec(client, container, "exec-1", 7, "20001:20001")

    assert [call.kwargs for call in container.exec_run.call_args_list] == [
        {"user": "20001:20001"},
        {"user": "20001:20001"},
    ]


def test_cleanup_kills_a_process_that_ignores_the_hangup():
    client = MagicMock()
    client.api.exec_inspect.return_value = {"Running": True}
    container = MagicMock()

    with patch("oduflow.web_ui.time.sleep"):
        _end_lingering_exec(client, container, "exec-1", 7)

    assert _kills(container) == [
        ["sh", "-c", "kill -HUP 7"],
        ["sh", "-c", "kill -KILL 7"],
    ]


def test_cleanup_leaves_an_exited_process_alone():
    """Its PID may belong to another process by now."""
    client = MagicMock()
    client.api.exec_inspect.return_value = {"Running": False}
    container = MagicMock()

    _end_lingering_exec(client, container, "exec-1", 7)

    assert _kills(container) == []
