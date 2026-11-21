"""POST /api/environments/{branch}/delete — the optional delete_branch body.

The endpoint historically took no body at all; it must keep accepting an empty
body (previous behaviour), pass an explicit boolean through to
env_ops.delete_environment, reject non-boolean values, and surface the
teardown's warnings in the result.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    # No ui_password -> the UI mounts without auth.
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app, raise_server_exceptions=False)


def test_empty_body_keeps_the_previous_behaviour(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.delete_environment", return_value=[]) as delete:
        resp = client.post("/api/environments/dev1/delete")

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "result": {"deleted": "dev1", "warnings": []},
    }
    assert delete.call_args.kwargs["delete_branch"] is False


def test_delete_branch_true_reaches_env_ops(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.delete_environment", return_value=[]) as delete:
        resp = client.post(
            "/api/environments/dev1/delete", json={"delete_branch": True}
        )

    assert resp.status_code == 200
    assert delete.call_args.kwargs["delete_branch"] is True


def test_warnings_are_returned_in_the_result(tmp_path):
    client = _client(tmp_path)
    warning = "Branch 'feature/x' has 2 commit(s) not merged into 'main'"
    with patch("oduflow.web_ui.env_ops.delete_environment", return_value=[warning]):
        resp = client.post(
            "/api/environments/dev1/delete", json={"delete_branch": True}
        )

    assert resp.status_code == 200
    assert resp.json()["result"]["warnings"] == [warning]


def test_non_boolean_delete_branch_is_a_400(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.delete_environment") as delete:
        resp = client.post(
            "/api/environments/dev1/delete", json={"delete_branch": "yes"}
        )

    assert resp.status_code == 400
    assert "delete_branch" in resp.json()["error"]
    delete.assert_not_called()


def test_non_object_json_body_is_a_400(tmp_path):
    # Valid JSON that is not an object used to reach .get() and answer 500.
    client = _client(tmp_path)
    for body in (["delete_branch"], "yes", 1, True):
        with patch("oduflow.web_ui.env_ops.delete_environment") as delete:
            resp = client.post("/api/environments/dev1/delete", json=body)

        assert resp.status_code == 400, body
        assert "JSON object" in resp.json()["error"]
        delete.assert_not_called()


def test_json_null_body_is_treated_as_no_body(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.delete_environment", return_value=[]) as delete:
        resp = client.post(
            "/api/environments/dev1/delete",
            content=b"null",
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 200
    assert delete.call_args.kwargs["delete_branch"] is False
