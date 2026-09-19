"""The dashboard's service editor: prefill, full-replacement edits, delete.

``api_service_config`` answers with exactly the settings an update would keep,
so the Update dialog can never offer a "current" value the update disagrees
with. On the way back, ``volumes`` follows the ``env_vars`` rule — present in
the body means a full replacement — and deleting a service carries the
"save as preset" choice.
"""

from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings


def _client(tmp_path):
    from oduflow.web_ui import mount_web_ui

    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="port", base_data_dir=str(tmp_path), teams={"1": team}
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def _settings(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    return Settings(
        routing_mode="port", base_data_dir=str(tmp_path), teams={"1": team}
    ), team


PRESET = {
    "image": "getmeili/meilisearch:v1.6",
    "port": 7700,
    "hostname": "meili",
    "env_vars": {"MEILI_ENV": "production", "MEILI_MASTER_KEY": "secret:meili_key"},
    "host_mode": False,
    "volumes": [{"volume": "meili-data", "mount_path": "/data", "mode": "rw"}],
    "cap_add": ["NET_ADMIN"],
    "privileged": False,
    "routes": None,
    "command": ["meilisearch", "--http-addr", "0.0.0.0:7700"],
}


def _container():
    container = MagicMock()
    container.labels = {"oduflow.service": "meili"}
    container.image.tags = ["getmeili/meilisearch:v1.6"]
    container.attrs = {"Config": {"Env": ["PATH=/usr/bin"]}}
    return container


def test_config_reports_what_an_update_would_keep(tmp_path):
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    docker_client = MagicMock()
    docker_client.containers.get.return_value = _container()
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=dict(PRESET),
        ),
    ):
        config = service_ops.get_service_config(settings, team, "meili")

    assert config["image"] == "getmeili/meilisearch:v1.6"
    assert config["port"] == 7700
    assert config["hostname"] == "meili"
    # Secret-backed values stay references; the dialog must not show the value.
    assert config["env_vars"]["MEILI_MASTER_KEY"] == "secret:meili_key"
    assert config["volumes"] == PRESET["volumes"]
    assert config["cap_add"] == ["NET_ADMIN"]
    assert config["command"] == PRESET["command"]


def test_config_falls_back_to_the_container_for_a_legacy_service(tmp_path):
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    container = _container()
    container.attrs = {
        "Config": {"Env": ["PATH=/usr/bin", "MEILI_ENV=production"], "Cmd": None},
        "HostConfig": {"CapAdd": None, "Privileged": False},
        "NetworkSettings": {"Ports": {"7700/tcp": [{"HostPort": "7700"}]}},
        "Mounts": [],
    }
    container.image.attrs = {"Config": {"Cmd": ["meilisearch"]}}
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ),
    ):
        config = service_ops.get_service_config(settings, team, "meili")

    assert config["port"] == 7700
    assert config["env_vars"] == {"MEILI_ENV": "production"}


def test_config_does_not_offer_image_defaults_for_a_legacy_service(tmp_path):
    """Vars the image itself sets are not configuration; prefilling them would
    bake them into the preset on the first save and pin them over newer images.
    """
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    container = _container()
    container.attrs = {
        "Config": {
            "Env": ["PATH=/usr/bin", "MEILI_ENV=production", "MEILI_VERSION=1.6"],
            "Cmd": None,
        },
        "HostConfig": {"CapAdd": None, "Privileged": False},
        "NetworkSettings": {"Ports": {"7700/tcp": [{"HostPort": "7700"}]}},
        "Mounts": [],
    }
    container.image.attrs = {
        "Config": {"Cmd": ["meilisearch"], "Env": ["MEILI_VERSION=1.6"]}
    }
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ),
    ):
        config = service_ops.get_service_config(settings, team, "meili")

    assert config["env_vars"] == {"MEILI_ENV": "production"}


def test_config_propagates_an_unreadable_preset(tmp_path):
    """A preset that exists but cannot be read must not fall back to the
    container: the dialog would prefill guessed values and a submit would
    overwrite the real preset with them.
    """
    import pytest

    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    docker_client = MagicMock()
    docker_client.containers.get.return_value = _container()
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=OSError("presets file is unreadable"),
        ),
    ):
        with pytest.raises(OSError):
            service_ops.get_service_config(settings, team, "meili")


def test_api_service_config_returns_the_config(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.get_service_config",
        return_value={"image": "redis:7", "port": 6379},
    ):
        response = client.get("/api/services/redis/config")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "config": {"image": "redis:7", "port": 6379},
    }


def test_api_service_config_reports_missing_service(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.get_service_config",
        side_effect=NotFoundError("Service 'redis' not found"),
    ):
        response = client.get("/api/services/redis/config")

    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_api_service_update_empty_volumes_unmounts_everything(tmp_path):
    """Clearing the volumes field is the only way the form can remove a mount."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/redis/update", json={"volumes": ""})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["volume_override"] == []


def test_api_service_update_without_volumes_keeps_them(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/redis/update", json={"image": "redis:8"})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["volume_override"] is None


def test_api_service_update_switches_routes_back_to_a_port(tmp_path):
    """The dialog sends both halves: the emptied route list and its replacement."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post(
            "/api/services/redis/update", json={"routes": [], "port": 6379}
        )

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["routes_override"] == []
    assert update.call_args.kwargs["port_override"] == 6379


def test_api_service_delete_keeps_the_preset_by_default(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.delete_service", return_value={}
    ) as delete_service:
        response = client.post("/api/services/redis/delete")

    assert response.json()["ok"] is True
    assert delete_service.call_args.kwargs["save_preset"] is True


def test_api_service_delete_can_drop_the_preset(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.delete_service", return_value={}
    ) as delete_service:
        response = client.post(
            "/api/services/redis/delete", json={"save_preset": False}
        )

    assert response.json()["ok"] is True
    assert delete_service.call_args.kwargs["save_preset"] is False


def test_api_service_delete_null_save_preset_keeps_it(tmp_path):
    """An explicit null counts as absent — never as "drop the preset"."""
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.delete_service", return_value={}
    ) as delete_service:
        response = client.post("/api/services/redis/delete", json={"save_preset": None})

    assert response.json()["ok"] is True
    assert delete_service.call_args.kwargs["save_preset"] is True


def test_api_service_delete_tolerates_a_non_object_body(tmp_path):
    """A valid but non-object JSON body must not escape as a bare 500."""
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.delete_service", return_value={}
    ) as delete_service:
        response = client.post(
            "/api/services/redis/delete",
            content="[1]",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert delete_service.call_args.kwargs["save_preset"] is True


def test_api_service_update_takes_an_env_mapping_verbatim(tmp_path):
    """The dashboard sends a mapping so values are never re-split server side."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post(
            "/api/services/meili/update",
            json={"env_vars": {"OPTIONS": "a,b", " MEILI_ENV ": "production"}},
        )

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "OPTIONS": "a,b",
        "MEILI_ENV": "production",
    }


def test_api_service_update_empty_env_mapping_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/meili/update", json={"env_vars": {}})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_service_update_without_env_vars_keeps_them(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/meili/update", json={})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] is None


def test_a_full_round_trip_payload_changes_nothing(tmp_path):
    """Posting the prefill back verbatim must compare equal on every field.

    The dialog itself sends only edited fields, but the API contract this
    rests on is that the prefill representation round-trips: if any field
    below did not, the update would report a config change and recreate the
    container for no reason.
    """
    client = _client(tmp_path)
    container = _container()
    container.image.id = "sha256:same"
    container.attrs = {
        "Config": {"Env": ["PATH=/usr/bin", "MEILI_ENV=production"], "Cmd": None},
        "HostConfig": {"CapAdd": ["NET_ADMIN"], "Privileged": False},
        "Mounts": [],
    }
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    pulled = MagicMock()
    pulled.id = "sha256:same"
    docker_client.images.pull.return_value = pulled
    preset = dict(PRESET, env_vars={"MEILI_ENV": "production"})

    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=docker_client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ),
        # Volume binds are resolved against the live Docker daemon; the mount
        # list itself is compared above, before this call.
        patch(
            "oduflow.docker_ops.service_ops._resolve_service_volume_binds",
            return_value={},
        ),
    ):
        response = client.post(
            "/api/services/meili/update",
            json={
                "image": "getmeili/meilisearch:v1.6",
                "command": "meilisearch --http-addr 0.0.0.0:7700",
                "volumes": "meili-data:/data",
                "host_mode": False,
                "privileged": False,
                "net_admin": True,
                "hostname": "meili",
                "routes": [],
                "port": 7700,
            },
        )

    body = response.json()
    assert body["ok"] is True, body
    assert body["result"]["config_updated"] is False
    assert body["result"]["image_updated"] is False
    container.stop.assert_not_called()
    container.remove.assert_not_called()
