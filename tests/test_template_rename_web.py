"""The dashboard's rename endpoint locks both names — source and destination.

When they are the same name, that is one key, and entering it twice would
answer a legitimate mistake ("rename default to default") with a 409 BusyError
naming a concurrent operation that does not exist, instead of the 400 the real
conflict deserves.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import ConflictError
from oduflow.locking import LockManager, template_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks


def test_renaming_a_template_to_its_own_name_reports_the_conflict(tmp_path):
    client, _ = _client(tmp_path)

    with patch("oduflow.docker_ops.system_ops.rename_template") as mock_rename:
        mock_rename.side_effect = ConflictError(
            "New template name is the same as the current one."
        )
        response = client.post(
            "/api/templates/default/rename", json={"new_name": "default"}
        )

    # 400, the ConflictError status — not the 409 a phantom BusyError would give.
    assert response.status_code == 400
    assert "same as the current one" in response.json()["error"]
    mock_rename.assert_called_once()


def test_rename_still_locks_the_destination(tmp_path):
    client, locks = _client(tmp_path)
    key = template_lock_key("1", "staging")
    locks.acquire_env(key, operation="import_template_from_odoo")
    try:
        with patch("oduflow.docker_ops.system_ops.rename_template") as mock_rename:
            response = client.post(
                "/api/templates/default/rename", json={"new_name": "staging"}
            )
    finally:
        locks.release_env(key)

    assert response.status_code == 409
    assert "import_template_from_odoo" in response.json()["error"]
    mock_rename.assert_not_called()
