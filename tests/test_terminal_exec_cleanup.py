"""Terminals must not leave their process behind in the container.

Docker keeps a TTY exec running after its client disconnects, so the Odoo
shell, the SQL console, the service shell and the Agent CLI all report their
PID up front and are ended once the terminal is gone.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import (
    _end_lingering_exec,
    _finish_cleanups,
    _pid_reporting_argv,
    _run_exec_pty,
    _start_cleanup,
    mount_web_ui,
)

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
    "endpoint,command,user",
    [
        ("terminal", ["/entrypoint.sh", "odoo", "shell"], ""),
        # psql runs in the Odoo container as its unprivileged user, which is
        # therefore also the one that has to end it.
        ("sql", ["psql", "-h", "oduflow-db", "-p", "5432", "-U", "env_role"], "odoo"),
    ],
)
def test_environment_terminals_hand_their_process_over_for_cleanup(
    tmp_path, endpoint, command, user
):
    client = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    container = docker_client.containers.get.return_value
    container.labels = {}
    # The SQL console first checks that the image ships psql.
    container.exec_run.return_value = (0, b"/usr/bin/psql\n")
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.odoo_ops.load_credentials",
            return_value={"pg_user": "env_role", "pg_password": "env-pw"},
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
    end_exec.assert_called_once_with(docker_client, container, "exec-1", 42, user)


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


def test_terminal_closed_before_the_pid_line_still_ends_its_process(tmp_path):
    """Opened and closed at once: the exec is still starting, and will run."""
    client = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    scheduled = threading.Event()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch(
            "oduflow.web_ui._end_lingering_exec",
            side_effect=lambda *a: scheduled.set(),
        ) as end_exec,
    ):
        with client.websocket_connect("/api/environments/main/terminal"):
            pass
        assert not scheduled.wait(0.2)
        exec_end.sendall(b"42\r\n>>> ")
        assert scheduled.wait(5)
        # Only then is the exec socket let go of.
        exec_end.settimeout(5)
        assert exec_end.recv(16) == b""
    exec_end.close()

    container = docker_client.containers.get.return_value
    end_exec.assert_called_once_with(docker_client, container, "exec-1", 42, "")


def test_terminal_closed_early_gives_up_on_a_pid_that_never_comes(tmp_path):
    client = _client(tmp_path)
    docker_client, exec_end = _docker_client()

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=docker_client),
        patch("oduflow.web_ui._PID_LINE_WAIT", 0.05),
        patch("oduflow.web_ui._end_lingering_exec") as end_exec,
    ):
        with client.websocket_connect("/api/environments/main/terminal"):
            pass
        exec_end.settimeout(5)
        assert exec_end.recv(16) == b""  # the socket is closed all the same
    exec_end.close()

    end_exec.assert_not_called()


def test_handler_cancelled_while_the_exec_starts_still_ends_it():
    """The exec is created by then; nothing else would close or end it."""
    docker_client, exec_end = _docker_client()
    container = docker_client.containers.get.return_value
    starting, proceed, scheduled = (threading.Event() for _ in range(3))

    def exec_start(*args, **kwargs):
        starting.set()
        assert proceed.wait(5)
        return MagicMock(_sock=docker_client.sock)

    docker_client.sock = docker_client.api.exec_start.return_value._sock
    docker_client.api.exec_start.side_effect = exec_start

    async def scenario():
        task = asyncio.ensure_future(
            _run_exec_pty(MagicMock(), docker_client, container, ["sh"], user="7:7")
        )
        await asyncio.get_running_loop().run_in_executor(None, starting.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        proceed.set()

    with patch(
        "oduflow.web_ui._end_lingering_exec",
        side_effect=lambda *a: scheduled.set(),
    ) as end_exec:
        asyncio.run(scenario())
        exec_end.sendall(b"42\r\n")
        assert scheduled.wait(5)
    exec_end.close()

    end_exec.assert_called_once_with(docker_client, container, "exec-1", 42, "7:7")


def test_relay_keeps_off_the_shared_default_executor(tmp_path):
    """Each terminal blocks a thread in recv() for as long as it is open."""
    client = _client(tmp_path)
    docker_client, exec_end = _docker_client()
    docker_client.api.exec_inspect.return_value = {"Running": False}
    threads = []
    docker_client.api.exec_create.side_effect = lambda *a, **k: (
        threads.append(threading.current_thread().name) or {"Id": "exec-1"}
    )

    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        with client.websocket_connect("/api/environments/main/terminal") as ws:
            exec_end.sendall(b"42\r\n>>> ")
            assert ws.receive_text() == ">>> "
    exec_end.close()

    assert len(threads) == 1 and threads[0].startswith("oduflow-pty")


def test_interpreter_exit_waits_for_a_cleanup_under_way():
    release, finished = threading.Event(), threading.Event()

    def cleanup():
        assert release.wait(5)
        finished.set()

    _start_cleanup(cleanup)
    threading.Timer(0.05, release.set).start()
    _finish_cleanups()

    assert finished.is_set()


def test_interpreter_exit_does_not_wait_for_a_stuck_cleanup():
    release = threading.Event()
    _start_cleanup(lambda: release.wait(5) and None)

    with patch("oduflow.web_ui._CLEANUP_EXIT_GRACE", 0.05):
        _finish_cleanups()

    release.set()
