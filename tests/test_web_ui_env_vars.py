"""Env-var handling in the dashboard REST API.

api_update treats an ``env_vars`` key present in the body as a full
replacement (an empty string or an empty mapping clears every user-supplied
variable), while an absent key keeps the current ones. A mapping is taken
verbatim; the legacy string form is parsed for existing REST clients. api_env_vars returns the persisted
``oduflow.env_vars`` label for a single environment so the update dialog can
prefill the current values. The same endpoint carries the dialog's rename, so
``new_name`` is covered here too.
"""

from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import docker
from oduflow.errors import BusyError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path, locks=None):
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks or LockManager())
    return TestClient(app)


def test_api_update_parses_env_vars(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "WORKERS=2\nLIMIT_TIME_CPU=600"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "WORKERS": "2",
        "LIMIT_TIME_CPU": "600",
    }


def test_api_update_accepts_mapping_verbatim(tmp_path):
    """The dashboard sends a mapping so values are never re-split server side."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": {"OPTIONS": "a,b", "WEIRD": "x,B=c", " WORKERS ": "2"}},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "OPTIONS": "a,b",
        "WEIRD": "x,B=c",
        "WORKERS": "2",
    }


def test_api_update_empty_mapping_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"env_vars": {}})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_update_keeps_commas_inside_values(tmp_path):
    """A stack file can set a value holding commas; editing must not truncate it."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "CONNECT_MCP_TOOL_GROUPS=write,collaboration\nWORKERS=2"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "CONNECT_MCP_TOOL_GROUPS": "write,collaboration",
        "WORKERS": "2",
    }


def test_api_update_comma_separated_pairs_still_split(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "WORKERS=2,LIMIT_TIME_CPU=600"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "WORKERS": "2",
        "LIMIT_TIME_CPU": "600",
    }


def test_api_update_empty_env_vars_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"env_vars": ""})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_update_absent_env_vars_keeps_current(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] is None


def test_api_update_renames_with_new_name(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"new_name": "next"})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["rename_to"] == "next"


def test_api_update_new_name_equal_to_the_current_one_is_not_a_rename(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"new_name": "main"})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["rename_to"] is None


def test_api_update_holds_the_target_name_and_releases_it(tmp_path):
    """A concurrent create must not claim the name mid-rename."""
    locks = LockManager()
    client = _client(tmp_path, locks)

    def _check(*args, **kwargs):
        with pytest.raises(BusyError):
            locks.acquire_env("next", "1")
        return {}

    with patch("oduflow.web_ui.env_ops.update_environment", side_effect=_check):
        resp = client.post("/api/environments/main/update", json={"new_name": "next"})

    assert resp.json()["ok"] is True
    locks.acquire_env("next", "1")
    locks.release_env("next")


def test_api_update_reports_a_busy_target_name_without_touching_the_env(tmp_path):
    locks = LockManager()
    client = _client(tmp_path, locks)
    locks.acquire_env("next", "1")
    try:
        with patch("oduflow.web_ui.env_ops.update_environment") as update:
            resp = client.post(
                "/api/environments/main/update", json={"new_name": "next"}
            )
    finally:
        locks.release_env("next")

    assert resp.json()["ok"] is False
    update.assert_not_called()
    # The source environment's own lock is released again, not leaked.
    locks.acquire_env("main", "1")
    locks.release_env("main")


def test_api_create_accepts_mapping(tmp_path):
    """The create dialog posts a mapping too, so both dialogs share a contract."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.create_environment", return_value={}) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "main",
                "repo_url": "https://example.invalid/repo.git",
                "odoo_image": "odoo:19.0",
                "env_vars": {"OPTIONS": "a,b"},
            },
        )
    assert resp.json()["ok"] is True, resp.json()
    assert create.call_args.kwargs["env_vars"] == {"OPTIONS": "a,b"}


def test_api_env_vars_returns_label(tmp_path):
    client = _client(tmp_path)
    container = MagicMock()
    container.labels = {"oduflow.env_vars": '{"WORKERS": "2"}'}
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        resp = client.get("/api/environments/main/env-vars")
    data = resp.json()
    assert data["ok"] is True
    assert data["env_vars"] == {"WORKERS": "2"}


def test_api_env_vars_missing_env_is_404(tmp_path):
    client = _client(tmp_path)
    docker_client = MagicMock()
    docker_client.containers.get.side_effect = docker.errors.NotFound("gone")
    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        resp = client.get("/api/environments/main/env-vars")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_api_update_reports_an_invalid_new_name_as_a_bad_request(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.env_ops.update_environment",
        side_effect=ValueError("Invalid environment name: '../escape'"),
    ):
        resp = client.post(
            "/api/environments/main/update", json={"new_name": "../escape"}
        )

    # Not an internal error: the message is written for the operator.
    assert resp.status_code == 400
    assert "../escape" in resp.json()["error"]


@pytest.mark.parametrize("hostname, expected", [(" qa ", "qa"), ("", None)])
def test_api_update_hostname(tmp_path, hostname, expected):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        response = client.post(
            "/api/environments/main/update", json={"hostname": hostname}
        )
    assert response.json()["ok"] is True
    assert update.call_args.kwargs["hostname_override"] == expected
