import json
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import service_ops, system_ops
from oduflow.errors import (
    ConflictError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEST_TEAM},
)


class TestRoutesWithUrls:
    ROUTES = [{"path": "/api", "port": 3000}]

    def _url(self, scheme):
        routes = service_ops._routes_with_urls(self.ROUTES, "svc.example.com", scheme)
        return routes[0]["url"]

    def test_uses_given_scheme(self):
        assert self._url("https") == "https://svc.example.com/api"
        assert self._url("http") == "http://svc.example.com/api"

    def test_no_hostname_means_no_url(self):
        assert "url" not in service_ops._routes_with_urls(self.ROUTES, None, "https")[0]


@pytest.fixture(autouse=True)
def _fake_team_network(monkeypatch):
    # Network provisioning is covered by tests/test_team_networks.py; here it
    # would only consume the mocked docker client's side_effect iterators.
    monkeypatch.setattr(
        "oduflow.docker_ops.system_ops.ensure_team_network",
        lambda client, settings, team: f"oduflow-{team.team_id}-net",
    )


TRAEFIK_TEAM = TeamSettings(
    team_id="1",
    hostname="example.com",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TRAEFIK_SETTINGS = Settings(
    routing_mode="traefik",
    acme_email="admin@example.com",
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TRAEFIK_TEAM},
)


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.service_ops.get_client") as svc_mock,
        patch("oduflow.docker_ops.system_ops.get_client") as sys_mock,
        patch("oduflow.docker_ops.volume_ops.get_client") as vol_mock,
    ):
        client_instance = MagicMock()
        svc_mock.return_value = client_instance
        sys_mock.return_value = client_instance
        vol_mock.return_value = client_instance
        yield client_instance


class TestCreateService:
    def test_service_slot_limit_rejects_new_service(self, mock_docker_client):
        team = TeamSettings(team_id="1", service_slots=2)
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = [MagicMock(), MagicMock()]

        with pytest.raises(FlowError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS,
                team,
                "third",
                "redis:7",
                6379,
            )

        assert str(exc_info.value) == (
            "No free service slots (configured: 2). "
            "Delete an unused service to free a slot."
        )
        mock_docker_client.containers.list.assert_called_once_with(
            all=True,
            filters={
                "label": [
                    "oduflow.managed=true",
                    "oduflow.team=1",
                    "oduflow.service",
                ]
            },
        )
        mock_docker_client.images.pull.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_zero_service_slots_disables_limit(self, mock_docker_client):
        team = TeamSettings(team_id="1", service_slots=0)
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(
            TEST_SETTINGS,
            team,
            "redis",
            "redis:7",
            6379,
        )

        mock_docker_client.containers.list.assert_not_called()

    def test_missing_image_returns_safe_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.images.pull.side_effect = docker.errors.NotFound(
            "404 for http+docker://localhost/v1.53/images/create?secret=token"
        )

        with pytest.raises(NotFoundError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "missing",
                "example/missing:0.7.2",
                8080,
            )

        assert str(exc_info.value) == (
            "Docker image 'example/missing:0.7.2' was not found or is not "
            "accessible. Check the image name, tag, and registry permissions."
        )
        assert "http+docker" not in str(exc_info.value)
        assert "secret" not in str(exc_info.value)
        mock_docker_client.containers.run.assert_not_called()

    def test_other_pull_error_returns_safe_prerequisite_error(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.images.pull.side_effect = docker.errors.APIError(
            "registry failure with secret credentials"
        )

        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                "registry.example.com/redis:7",
                6379,
            )

        assert str(exc_info.value) == (
            "Could not pull Docker image 'registry.example.com/redis:7'. Check "
            "Docker connectivity, registry availability, and registry credentials."
        )
        assert "secret" not in str(exc_info.value)
        mock_docker_client.containers.run.assert_not_called()

    def test_port_conflict_is_actionable_flow_error(self, mock_docker_client):
        # First lookup: no service yet. Second lookup: the container the SDK
        # created before the failed start, which must be removed for the retry.
        stale = MagicMock()
        stale.status = "created"
        stale.labels = {"oduflow.managed": "true"}
        mock_docker_client.containers.get.side_effect = [
            docker.errors.NotFound("nf"),
            stale,
        ]
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/start",
            explanation=(
                "failed to set up container networking: Bind for "
                "0.0.0.0:8080 failed: port is already allocated"
            ),
        )

        with pytest.raises(ConflictError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "hindsight-bankname",
                "oduist/streams-hindsight-sidecar:0.5.0",
                8080,
            )

        message = str(exc_info.value)
        assert "host port 8080 is already allocated" in message
        assert "call create_service again" in message
        assert "http+docker" not in message
        stale.remove.assert_called_once_with(force=True)

    def test_stopped_service_with_same_name_is_authored_conflict(
        self, mock_docker_client
    ):
        """A name clash with an exited service is reported in our own words.

        Docker's 409 would quote the container name and ID; the stopped
        service must also survive the attempt untouched.
        """
        stopped = MagicMock()
        stopped.status = "exited"
        mock_docker_client.containers.get.return_value = stopped

        with pytest.raises(ConflictError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        message = str(exc_info.value)
        assert "already exists but is not running" in message
        assert "status: exited" in message
        assert "update_service" in message
        mock_docker_client.containers.run.assert_not_called()
        stopped.remove.assert_not_called()

    def test_stale_created_container_is_removed_only_when_never_started(
        self, mock_docker_client
    ):
        """_remove_stale_service_container leaves non-`created` containers alone."""
        stopped = MagicMock()
        stopped.status = "exited"
        mock_docker_client.containers.get.return_value = stopped
        service_ops._remove_stale_service_container(mock_docker_client, "c")
        stopped.remove.assert_not_called()

        foreign = MagicMock()
        foreign.status = "created"
        foreign.labels = {}
        mock_docker_client.containers.get.return_value = foreign
        service_ops._remove_stale_service_container(mock_docker_client, "c")
        foreign.remove.assert_not_called()

        created = MagicMock()
        created.status = "created"
        created.labels = {"oduflow.managed": "true"}
        mock_docker_client.containers.get.return_value = created
        service_ops._remove_stale_service_container(mock_docker_client, "c")
        created.remove.assert_called_once_with(force=True)

    def test_start_failure_scrubs_container_ids(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/start",
            explanation=(
                "cannot join network of a non running container: "
                "3f9a1c2b4d5e6f708192a3b4c5d6e7f8"
            ),
        )

        with pytest.raises(FlowError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        assert "3f9a1c2b4d5e" not in str(exc_info.value)
        assert "<id>" in str(exc_info.value)

    def test_start_failure_scrubs_host_paths_and_container_names(
        self, mock_docker_client
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/start",
            explanation=(
                "error while mounting volume "
                "'/var/lib/docker/volumes/oduflow-vol-t1-data/_data': "
                'failed to mount local volume: no such file; container "/oduflow-t1-redis"'
            ),
        )

        with pytest.raises(FlowError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        message = str(exc_info.value)
        assert "/var/lib/docker" not in message
        assert "oduflow-t1-redis" not in message
        assert "failed to mount local volume: no such file" in message
        assert message.startswith("Docker failed to start service 'redis': ")

    def test_other_start_failure_is_flow_error(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/start",
            explanation="invalid mount config for type bind: source path is missing",
        )

        with pytest.raises(FlowError) as exc_info:
            service_ops.create_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                "redis:7",
                6379,
            )

        assert str(exc_info.value) == (
            "Docker failed to start service 'redis': invalid mount config for "
            "type bind: source path is missing"
        )
        assert "http+docker" not in str(exc_info.value)

    def test_create_port_mode(self, mock_docker_client):
        # Network exists
        mock_docker_client.networks.get.return_value = MagicMock()
        # No existing container
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
        )

        assert result["name"] == "redis"
        assert result["container_name"] == "oduflow-1-svc-redis"
        assert result["url"] == "http://localhost:6379"
        assert result["image"] == "redis:7"

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["name"] == "oduflow-1-svc-redis"
        assert run_kwargs[1]["image"] == "redis:7"
        assert run_kwargs[1]["network"] == "oduflow-1-net"
        assert run_kwargs[1]["ports"] == {"6379/tcp": 6379}
        assert run_kwargs[1]["labels"]["oduflow.managed"] == "true"
        assert run_kwargs[1]["labels"]["oduflow.service"] == "redis"

        mock_docker_client.images.pull.assert_called_once_with("redis:7")

    def test_create_traefik_mode(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "meilisearch",
            "getmeili/meilisearch:v1.6",
            7700,
        )

        assert result["url"] == "https://meilisearch.example.com"

        run_kwargs = mock_docker_client.containers.run.call_args
        labels = run_kwargs[1]["labels"]
        assert labels["traefik.enable"] == "true"
        assert (
            labels["traefik.http.routers.oduflow-1-svc-meilisearch.rule"]
            == "Host(`meilisearch.example.com`)"
        )
        assert (
            labels[
                "traefik.http.services.oduflow-1-svc-meilisearch.loadbalancer.server.port"
            ]
            == "7700"
        )
        # No port mapping in traefik mode
        assert "ports" not in run_kwargs[1]
        # TLS mode: router uses websecure with Let's Encrypt.
        assert (
            labels["traefik.http.routers.oduflow-1-svc-meilisearch.entrypoints"]
            == "websecure"
        )
        assert (
            labels["traefik.http.routers.oduflow-1-svc-meilisearch.tls.certresolver"]
            == "letsencrypt"
        )
        assert run_kwargs[1]["volumes"] == {
            "oduflow-traefik-acme": {"bind": "/etc/traefik", "mode": "ro"}
        }

    def test_create_traefik_bridge_with_restricted_routes(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "gateway",
            "example/gateway:1",
            None,
            routes=[
                {"path": "/RPC2", "port": 8080},
                {"path": "/admin/", "port": 8081, "strip_prefix": True},
            ],
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        labels = run_kwargs["labels"]
        assert run_kwargs["network"] == "oduflow-1-net"
        assert "traefik.http.routers.oduflow-1-svc-gateway.rule" not in labels
        assert labels["traefik.http.routers.oduflow-1-svc-gateway-route-1.rule"] == (
            "Host(`gateway.example.com`) && (Path(`/RPC2`) || PathPrefix(`/RPC2/`))"
        )
        assert (
            labels[
                "traefik.http.services.oduflow-1-svc-gateway-route-1.loadbalancer.server.port"
            ]
            == "8080"
        )
        assert labels[
            "traefik.http.routers.oduflow-1-svc-gateway-route-2.middlewares"
        ] == ("oduflow-1-svc-gateway-route-2-strip")
        assert (
            labels[
                "traefik.http.middlewares.oduflow-1-svc-gateway-route-2-strip.stripprefix.prefixes"
            ]
            == "/admin"
        )
        assert result["routes"][0]["url"] == "https://gateway.example.com/RPC2"

    def test_create_traefik_host_mode_routes_use_host_gateway(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "fs",
            "oduist/freeswitch:latest",
            None,
            host_mode=True,
            routes=[{"path": "/RPC2", "port": 8080}],
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["network_mode"] == "host"
        assert (
            run_kwargs["labels"][
                "traefik.http.services.oduflow-1-svc-fs-route-1.loadbalancer.server.url"
            ]
            == "http://host.docker.internal:8080"
        )

    @pytest.mark.parametrize(
        ("settings", "port", "routes", "message"),
        [
            (TEST_SETTINGS, None, [{"path": "/api", "port": 8080}], "traefik"),
            (
                TRAEFIK_SETTINGS,
                8080,
                [{"path": "/api", "port": 8080}],
                "either port or routes",
            ),
            (
                TRAEFIK_SETTINGS,
                None,
                [{"path": "/api", "port": 8080}, {"path": "/api/", "port": 8081}],
                "Duplicate route",
            ),
            (
                TRAEFIK_SETTINGS,
                None,
                [{"path": "/api", "port": 8080, "url": "http://other:80"}],
                "unsupported fields: url",
            ),
        ],
    )
    def test_create_rejects_invalid_route_exposure(
        self, mock_docker_client, settings, port, routes, message
    ):
        with pytest.raises(ValueError, match=message):
            service_ops.create_service(
                settings, TRAEFIK_TEAM, "svc", "example/svc:1", port, routes=routes
            )
        mock_docker_client.images.pull.assert_not_called()

    def test_create_traefik_mounts_acme_with_user_volumes(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "meilisearch",
            "getmeili/meilisearch:v1.6",
            7700,
            volumes=[{"volume": "data", "mount_path": "/data", "mode": "rw"}],
        )

        assert mock_docker_client.containers.run.call_args[1]["volumes"] == {
            "oduflow-vol-1-data": {"bind": "/data", "mode": "rw"},
            "oduflow-traefik-acme": {"bind": "/etc/traefik", "mode": "ro"},
        }

    def test_create_traefik_host_mode_mounts_acme(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "fs",
            "oduist/freeswitch:latest",
            8080,
            host_mode=True,
            volumes=[
                {
                    "volume": "fs-sounds",
                    "mount_path": "/usr/share/freeswitch/sounds",
                    "mode": "rw",
                }
            ],
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["network_mode"] == "host"
        assert run_kwargs["volumes"] == {
            "oduflow-vol-1-fs-sounds": {
                "bind": "/usr/share/freeswitch/sounds",
                "mode": "rw",
            },
            "oduflow-traefik-acme": {"bind": "/etc/traefik", "mode": "ro"},
        }

    def test_create_traefik_rejects_user_mount_at_reserved_path(
        self, mock_docker_client
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(ConflictError, match="inside reserved '/etc/traefik'"):
            service_ops.create_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "meilisearch",
                "getmeili/meilisearch:v1.6",
                7700,
                volumes=[
                    {
                        "volume": "config",
                        "mount_path": "/etc/traefik",
                        "mode": "rw",
                    }
                ],
            )

        mock_docker_client.images.pull.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_create_traefik_requires_acme_volume(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Traefik ACME volume.*not found"):
            service_ops.create_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "meilisearch",
                "getmeili/meilisearch:v1.6",
                7700,
            )

        mock_docker_client.images.pull.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_create_traefik_no_tls(self, mock_docker_client):
        # tls=false (e.g. behind a Cloudflare tunnel): router on the plain-HTTP
        # web entrypoint, no certresolver — but the public URL stays https://.
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            base_data_dir="/tmp/flow-test",
            db_user="odoo",
            db_password="odoo",
            teams={"1": TRAEFIK_TEAM},
        )
        result = service_ops.create_service(
            settings, TRAEFIK_TEAM, "meilisearch", "getmeili/meilisearch:v1.6", 7700
        )

        assert result["url"] == "https://meilisearch.example.com"
        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-1-svc-meilisearch.entrypoints"]
            == "web"
        )
        assert (
            "traefik.http.routers.oduflow-1-svc-meilisearch.tls.certresolver"
            not in labels
        )
        assert "volumes" not in mock_docker_client.containers.run.call_args[1]

    def test_create_traefik_custom_hostname(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "redis",
            "redis:7",
            6379,
            hostname="my-redis.example.com",
        )

        assert result["url"] == "https://my-redis.example.com"
        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-1-svc-redis.rule"]
            == "Host(`my-redis.example.com`)"
        )

    def test_create_with_env_vars(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        env = {"MEILI_MASTER_KEY": "abc123", "MEILI_ENV": "production"}
        service_ops.create_service(
            TEST_SETTINGS,
            TEST_TEAM,
            "meili",
            "getmeili/meilisearch:v1.6",
            7700,
            env_vars=env,
        )

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == env

    def test_create_with_command(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.save_preset"
        ) as mock_save:
            result = service_ops.create_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "minio",
                "minio/minio:latest",
                9000,
                command=["server", "/data"],
            )

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["command"] == ["server", "/data"]
        assert result["command"] == ["server", "/data"]
        assert mock_save.call_args[1]["command"] == ["server", "/data"]

    def test_create_without_command(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "command" not in run_kwargs[1]

    def test_create_without_env_vars(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "environment" not in run_kwargs[1]

    def test_create_conflict(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        existing = MagicMock()
        existing.status = "running"
        mock_docker_client.containers.get.return_value = existing

        with pytest.raises(ConflictError, match="already exists"):
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        mock_docker_client.containers.run.assert_not_called()


class TestDeleteService:
    def test_delete(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = service_ops.delete_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["container_name"] == "oduflow-1-svc-redis"
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

    def test_delete_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.delete_service(TEST_SETTINGS, TEST_TEAM, "redis")


class TestListServices:
    def test_list_with_services_port_mode(self, mock_docker_client):
        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.service": "redis",
        }
        container.name = "oduflow-1-svc-redis"
        container.status = "running"
        container.image.tags = ["redis:7"]
        container.image.attrs = {
            "Config": {"Env": ["REDIS_VERSION=7.4", "PATH=/usr/local/bin"]}
        }
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
            "Config": {"Env": ["REDIS_PASSWORD=secret", "PATH=/usr/bin", "HOME=/root"]},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert len(result) == 1
        svc = result[0]
        assert svc["name"] == "redis"
        assert svc["container_name"] == "oduflow-1-svc-redis"
        assert svc["image"] == "redis:7"
        assert svc["status"] == "running"
        assert svc["port"] == 6379
        assert svc["url"] == "http://localhost:6379"
        # Env vars: system vars filtered out, only custom remain
        assert svc["env_vars"] == {"REDIS_PASSWORD": "secret"}
        assert svc["image_env_vars"] == {"REDIS_VERSION": "7.4"}

    def test_list_with_services_traefik_mode(self, mock_docker_client):
        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.service": "meili",
            "traefik.enable": "true",
            "traefik.http.routers.oduflow-1-svc-meili.rule": "Host(`meili.example.com`)",
            "traefik.http.services.oduflow-1-svc-meili.loadbalancer.server.port": "7700",
        }
        container.name = "oduflow-1-svc-meili"
        container.status = "running"
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.attrs = {"Config": {"Env": []}}
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TRAEFIK_SETTINGS, TRAEFIK_TEAM)

        assert len(result) == 1
        svc = result[0]
        assert svc["url"] == "https://meili.example.com"
        assert svc["port"] == 7700

    def test_list_reports_command_override(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "minio"}
        container.name = "oduflow-1-svc-minio"
        container.status = "running"
        container.image.tags = ["minio/minio:latest"]
        container.image.attrs = {"Config": {"Cmd": ["minio"], "Env": []}}
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {"Cmd": ["server", "/data"], "Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        svc = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)[0]

        assert svc["command"] == ["server", "/data"]
        assert svc["image_command"] == ["minio"]

    def test_list_image_default_command_is_not_an_override(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.name = "oduflow-1-svc-redis"
        container.status = "running"
        container.image.tags = ["redis:7"]
        container.image.attrs = {"Config": {"Cmd": ["redis-server"], "Env": []}}
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {"Cmd": ["redis-server"], "Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        svc = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)[0]

        assert svc["command"] == []
        assert svc["image_command"] == ["redis-server"]

    def test_list_empty(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result == []

    def test_list_skips_non_service_containers(self, mock_docker_client):
        # Container with managed label but no service label (e.g. an odoo env)
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.branch": "main"}
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result == []

    def test_list_env_var_filtering(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "meili"}
        container.name = "oduflow-1-svc-meili"
        container.status = "running"
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {
                "Env": [
                    "MEILI_MASTER_KEY=abc",
                    "MEILI_ENV=production",
                    "PATH=/usr/local/bin:/usr/bin",
                    "HOME=/root",
                    "HOSTNAME=abc123",
                    "TERM=xterm",
                    "LANG=en_US.UTF-8",
                    "LC_ALL=en_US.UTF-8",
                ]
            },
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        env = result[0]["env_vars"]
        assert env == {"MEILI_MASTER_KEY": "abc", "MEILI_ENV": "production"}
        # System vars must be excluded
        for sys_key in ("PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LC_ALL"):
            assert sys_key not in env

    def test_list_image_fallback(self, mock_docker_client):
        """When image.tags is empty, fall back to Config.Image."""
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.name = "oduflow-1-svc-redis"
        container.status = "running"
        container.image.tags = []
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {"Image": "redis:7-alpine", "Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result[0]["image"] == "unknown"

    def test_list_port_no_mappings(self, mock_docker_client):
        """Port key exists but no host mappings."""
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.name = "oduflow-1-svc-redis"
        container.status = "exited"
        container.image.tags = ["redis:7"]
        container.attrs = {
            "NetworkSettings": {"Ports": {"6379/tcp": None}},
            "Config": {"Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        svc = result[0]
        assert svc["port"] == 6379
        assert svc["url"] is None


class TestRestartService:
    def _conflict(self, port: int) -> docker.errors.APIError:
        return docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/restart",
            explanation=(
                "driver failed programming external connectivity: Bind for "
                f"0.0.0.0:{port} failed: port is already allocated"
            ),
        )

    def test_restart_port_conflict_names_preset_port(self, mock_docker_client):
        container = MagicMock()
        container.restart.side_effect = self._conflict(6379)
        mock_docker_client.containers.get.return_value = container

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value={"name": "redis", "port": 6379},
            ),
            pytest.raises(ConflictError) as exc_info,
        ):
            service_ops.restart_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert "host port 6379 is already allocated" in str(exc_info.value)
        # The stopped container still holds the name, so create_service would
        # fail; update_service is the path that can apply a new port.
        assert "call update_service with a new port" in str(exc_info.value)
        assert "create_service" not in str(exc_info.value)
        assert "http+docker" not in str(exc_info.value)

    def test_restart_without_preset_still_reports_conflict(self, mock_docker_client):
        from oduflow.errors import NotFoundError

        container = MagicMock()
        container.restart.side_effect = self._conflict(6379)
        mock_docker_client.containers.get.return_value = container

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                side_effect=NotFoundError("no preset"),
            ),
            pytest.raises(ConflictError) as exc_info,
        ):
            service_ops.restart_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert "host port is already allocated" in str(exc_info.value)

    def test_restart_other_failure_is_flow_error(self, mock_docker_client):
        container = MagicMock()
        container.restart.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/restart",
            explanation='OCI runtime create failed: exec: "serve": not found',
        )
        mock_docker_client.containers.get.return_value = container

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value={"name": "redis", "port": 6379},
            ),
            pytest.raises(FlowError) as exc_info,
        ):
            service_ops.restart_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert str(exc_info.value) == (
            "Docker failed to start service 'redis': OCI runtime create failed: "
            'exec: "serve": not found'
        )

    def test_restart_not_found(self, mock_docker_client):
        from oduflow.errors import NotFoundError

        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        with pytest.raises(NotFoundError):
            service_ops.restart_service(TEST_SETTINGS, TEST_TEAM, "redis")


class TestUpdateService:
    def _make_container(self, image_tags, labels, attrs):
        container = MagicMock()
        container.image.tags = image_tags
        container.labels = labels
        container.attrs = attrs
        return container

    def test_pull_failure_keeps_existing_container(self, mock_docker_client):
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:old"
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.images.pull.side_effect = docker.errors.APIError(
            "registry unavailable"
        )
        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            pytest.raises(PrerequisiteNotMetError),
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_update_uses_preset(self, mock_docker_client):
        """When a preset exists, update_service reads options from it (not the container)."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"REDIS_PASSWORD": "secret"},
            "volumes": [
                {"volume": "oduflow-traefik-acme", "mount_path": "/acme", "mode": "ro"}
            ],
        }

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            patch(
                "oduflow.docker_ops.service_ops.volume_ops.resolve_volume_binds",
                return_value={"vol1": {"bind": "/acme", "mode": "ro"}},
            ),
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["image"] == "redis:7"

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == {"REDIS_PASSWORD": "secret"}

    def test_update_preset_preserves_volumes(self, mock_docker_client):
        """Volumes from the preset (including external) are preserved through update."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        volumes = [
            {"volume": "oduflow-traefik-acme", "mount_path": "/acme", "mode": "ro"},
            {"volume": "data", "mount_path": "/data", "mode": "rw"},
        ]
        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "volumes": volumes,
        }

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            patch(
                "oduflow.docker_ops.service_ops.volume_ops.resolve_volume_binds",
                return_value={"vol1": {"bind": "/acme", "mode": "ro"}},
            ) as mock_resolve,
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")
            # update pre-validates, then create both pre-validates (before the
            # image pull) and re-resolves under the service-registry lock.
            assert mock_resolve.call_count == 3
            mock_resolve.assert_any_call(TEST_TEAM, volumes)

    def test_update_preserves_preset_command(self, mock_docker_client):
        """An update with no command override keeps the preset's command."""
        container = self._make_container(
            image_tags=["minio/minio:latest"],
            labels={"oduflow.managed": "true", "oduflow.service": "minio"},
            attrs={"Config": {"Env": []}},
        )
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "minio",
            "image": "minio/minio:latest",
            "port": 9000,
            "hostname": "",
            "env_vars": {},
            "command": ["server", "/data"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "minio")

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["command"] == ["server", "/data"]

    def test_update_preserves_runtime_and_shutdown_timeout(self, mock_docker_client):
        """Replacement must retain runtime settings and drain the old service."""
        runtime = {
            "stop_timeout": 180,
            "stop_signal": "SIGRTMIN+3",
            "cgroupns": "private",
        }
        container = self._make_container(
            image_tags=["minio/minio:latest"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "minio",
                "oduflow.runtime": json.dumps(runtime),
            },
            attrs={"Config": {"Env": []}},
        )
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "minio",
            "image": "minio/minio:latest",
            "port": 9000,
            "hostname": "",
            "env_vars": {},
            "command": ["server", "/data"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "minio")

        run_kwargs = mock_docker_client.containers.run.call_args
        container.stop.assert_called_once_with(timeout=180)
        assert run_kwargs[1]["stop_signal"] == "SIGRTMIN+3"
        assert run_kwargs[1]["cgroupns"] == "private"
        # docker-py's containers.run() rejects stop_timeout; it is preserved
        # in the label and applied when the container is stopped/restarted.
        assert "stop_timeout" not in run_kwargs[1]
        assert json.loads(run_kwargs[1]["labels"]["oduflow.runtime"]) == runtime

    def test_update_command_override_recreates_container(self, mock_docker_client):
        """A changed command recreates the container even on an unchanged digest."""
        container = self._make_container(
            image_tags=["minio/minio:latest"],
            labels={"oduflow.managed": "true", "oduflow.service": "minio"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:same"
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        new_image = MagicMock()
        new_image.id = "sha256:same"
        mock_docker_client.images.pull.return_value = new_image
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "minio",
            "image": "minio/minio:latest",
            "port": 9000,
            "hostname": "",
            "env_vars": {},
            "command": ["server", "/data"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "minio",
                command_override=["server", "/data", "--console-address", ":9001"],
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["command"] == [
            "server",
            "/data",
            "--console-address",
            ":9001",
        ]

    def test_update_empty_command_override_clears_it(self, mock_docker_client):
        """An empty override drops the command back to the image default."""
        container = self._make_container(
            image_tags=["minio/minio:latest"],
            labels={"oduflow.managed": "true", "oduflow.service": "minio"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:same"
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        new_image = MagicMock()
        new_image.id = "sha256:same"
        mock_docker_client.images.pull.return_value = new_image
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "minio",
            "image": "minio/minio:latest",
            "port": 9000,
            "hostname": "",
            "env_vars": {},
            "command": ["server", "/data"],
        }

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            patch(
                "oduflow.docker_ops.service_ops.service_presets.save_preset"
            ) as mock_save,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "minio", command_override=[]
            )

        assert result["config_updated"] is True
        run_kwargs = mock_docker_client.containers.run.call_args
        assert "command" not in run_kwargs[1]
        assert mock_save.call_args[1]["command"] is None

    def test_update_legacy_no_preset_keeps_command_override(self, mock_docker_client):
        """Without a preset the override is read back from the container's Cmd."""
        container = self._make_container(
            image_tags=["minio/minio:latest"],
            labels={"oduflow.managed": "true", "oduflow.service": "minio"},
            attrs={
                "Config": {"Env": [], "Cmd": ["server", "/data"]},
                "NetworkSettings": {"Ports": {"9000/tcp": []}},
            },
        )
        container.image.attrs = {"Config": {"Cmd": ["minio"], "Env": []}}
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "minio")

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["command"] == ["server", "/data"]

    def test_update_port_mode_legacy_no_preset(self, mock_docker_client):
        """Legacy fallback: extract settings from container when no preset exists."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={
                "NetworkSettings": {
                    "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
                },
                "Config": {
                    "Env": ["REDIS_PASSWORD=secret", "PATH=/usr/bin", "HOME=/root"]
                },
                "Mounts": [],
            },
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("not found"),
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["image"] == "redis:7"
        assert result["url"] == "http://localhost:6379"

        mock_docker_client.images.pull.assert_any_call("redis:7")
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == {"REDIS_PASSWORD": "secret"}

    def test_update_traefik_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "meili",
                "traefik.http.routers.oduflow-1-svc-meili.rule": "Host(`meili.example.com`)",
                "traefik.http.services.oduflow-1-svc-meili.loadbalancer.server.port": "7700",
            },
            attrs={"Config": {"Env": ["MEILI_MASTER_KEY=abc"]}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "meili",
            "image": "getmeili/meilisearch:v1.6",
            "port": 7700,
            "hostname": "meili",
            "env_vars": {"MEILI_MASTER_KEY": "abc"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili")

        assert result["url"] == "https://meili.example.com"
        mock_docker_client.images.pull.assert_any_call("getmeili/meilisearch:v1.6")

        run_kwargs = mock_docker_client.containers.run.call_args
        labels = run_kwargs[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-1-svc-meili.rule"]
            == "Host(`meili.example.com`)"
        )

    def test_update_replaces_legacy_port_with_routes(self, mock_docker_client):
        container = self._make_container(
            image_tags=["example/app:1"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "app",
                "traefik.http.routers.oduflow-1-svc-app.rule": "Host(`app.example.com`)",
                "traefik.http.services.oduflow-1-svc-app.loadbalancer.server.port": "8080",
            },
            attrs={"Config": {"Env": []}, "Mounts": []},
        )
        container.image.id = "sha256:old"
        pulled = MagicMock(id="sha256:old")
        mock_docker_client.images.pull.return_value = pulled
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        preset = {
            "name": "app",
            "image": "example/app:1",
            "port": 8080,
            "hostname": "app",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "app",
                routes_override=[{"path": "/api", "port": 8081}],
            )

        assert result["config_updated"] is True
        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert "traefik.http.routers.oduflow-1-svc-app.rule" not in labels
        assert (
            labels[
                "traefik.http.services.oduflow-1-svc-app-route-1.loadbalancer.server.port"
            ]
            == "8081"
        )

    def test_update_clear_routes_requires_replacement_port(self, mock_docker_client):
        container = self._make_container(
            image_tags=["example/app:1"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "app",
                "oduflow.http_routes": '[{"path":"/api","port":8080,"strip_prefix":false}]',
            },
            attrs={"Config": {"Env": []}, "Mounts": []},
        )
        preset = {
            "name": "app",
            "image": "example/app:1",
            "port": 0,
            "hostname": "app",
            "env_vars": {},
            "routes": [{"path": "/api", "port": 8080, "strip_prefix": False}],
        }
        mock_docker_client.containers.get.return_value = container

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            pytest.raises(ValueError, match="replacement port"),
        ):
            service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "app", routes_override=[]
            )
        container.stop.assert_not_called()

    def test_update_no_env_vars(self, mock_docker_client):
        """When the preset has no custom env vars, env_vars=None is passed to create."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "environment" not in run_kwargs[1]

    def test_update_image_unchanged_returns_url_port_mode(self, mock_docker_client):
        """When image digest is unchanged, return early with a valid URL (port mode)."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:same"

        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()
        pulled = MagicMock()
        pulled.id = "sha256:same"
        mock_docker_client.images.pull.return_value = pulled

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image_updated"] is False
        assert result["url"] == "http://localhost:6379"
        assert result["name"] == "redis"
        assert result["image"] == "redis:7"
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_update_image_unchanged_returns_url_traefik(self, mock_docker_client):
        """When image digest is unchanged in traefik mode, URL is built from hostname."""
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            labels={"oduflow.managed": "true", "oduflow.service": "meili"},
            attrs={
                "Config": {"Env": []},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-traefik-acme",
                        "Destination": "/etc/traefik",
                        "RW": False,
                    }
                ],
            },
        )
        container.image.id = "sha256:same"

        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()
        pulled = MagicMock()
        pulled.id = "sha256:same"
        mock_docker_client.images.pull.return_value = pulled

        preset = {
            "name": "meili",
            "image": "getmeili/meilisearch:v1.6",
            "port": 7700,
            "hostname": "meili",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili")

        assert result["image_updated"] is False
        assert result["url"] == "https://meili.example.com"
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_update_adds_missing_implicit_traefik_acme_mount(self, mock_docker_client):
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            labels={"oduflow.managed": "true", "oduflow.service": "meili"},
            attrs={"Config": {"Env": []}, "Mounts": []},
        )
        container.image.id = "sha256:same"
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        pulled = MagicMock()
        pulled.id = "sha256:same"
        mock_docker_client.images.pull.return_value = pulled
        preset = {
            "name": "meili",
            "image": "getmeili/meilisearch:v1.6",
            "port": 7700,
            "hostname": "meili",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili")

        assert result["image_updated"] is False
        assert result["config_updated"] is True
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)
        assert mock_docker_client.containers.run.call_args[1]["volumes"] == {
            "oduflow-traefik-acme": {"bind": "/etc/traefik", "mode": "ro"}
        }

    def test_update_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

    def test_update_reserved_volume_preflight_does_not_remove_running_service(
        self, mock_docker_client
    ):
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        mock_docker_client.containers.get.return_value = container
        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            with pytest.raises(NotFoundError, match="reserved"):
                service_ops.update_service(
                    TEST_SETTINGS,
                    TEST_TEAM,
                    "redis",
                    volume_override=[
                        {
                            "volume": "oduflow-traefik-acme",
                            "mount_path": "/data",
                            "mode": "rw",
                        }
                    ],
                )

        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.images.pull.assert_not_called()

    def test_update_image_fallback_to_config(self, mock_docker_client):
        """When image.tags is empty, fall back to Config.Image."""
        container = self._make_container(
            image_tags=[],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Image": "redis:7-alpine", "Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7-alpine",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image"] == "redis:7-alpine"
        mock_docker_client.images.pull.assert_any_call("redis:7-alpine")

    def test_update_env_override_replaces_env_vars(self, mock_docker_client):
        """env_override fully replaces preset env_vars and forces recreation."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        # Force same digest so only config change triggers recreate
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"OLD_KEY": "old", "KEEP_ME": "1"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                env_override={"NEW_KEY": "new"},
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

        run_kwargs = mock_docker_client.containers.run.call_args
        # Full replace: only NEW_KEY remains
        assert run_kwargs[1]["environment"] == {"NEW_KEY": "new"}

    def test_update_no_changes_no_recreate(self, mock_docker_client):
        """When image digest and all overrides match, no recreation happens."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.return_value = container

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"A": "1"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image_updated"] is False
        assert result["config_updated"] is False
        assert result["url"] == "http://localhost:6379"
        assert result["host_mode"] is False
        container.stop.assert_not_called()
        container.remove.assert_not_called()

    def test_update_host_mode_no_changes_preserves_mode(self, mock_docker_client):
        """A no-op update still reports the service's current network mode."""
        container = self._make_container(
            image_tags=["freeswitch:latest"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "fs",
                "oduflow.host_mode": "true",
            },
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.return_value = container

        preset = {
            "name": "fs",
            "image": "freeswitch:latest",
            "port": 8080,
            "hostname": "",
            "env_vars": {},
            "host_mode": True,
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "fs")

        assert result["host_mode"] is True
        assert result["config_updated"] is False
        container.stop.assert_not_called()
        container.remove.assert_not_called()

    def test_update_image_override(self, mock_docker_client):
        """image_override pulls a new image tag and recreates."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", image_override="redis:8"
            )

        assert result["image"] == "redis:8"
        assert result["config_updated"] is True
        mock_docker_client.images.pull.assert_any_call("redis:8")

    def test_update_port_override(self, mock_docker_client):
        """port_override changes port and recreates even if image digest unchanged."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", port_override=6380
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["ports"] == {"6380/tcp": 6380}

    def test_update_port_conflict_points_at_create_service(self, mock_docker_client):
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "500 Server Error for http+docker://localhost/containers/id/start",
            explanation=(
                "failed to set up container networking: Bind for "
                "0.0.0.0:6380 failed: port is already allocated"
            ),
        )
        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value=preset,
            ),
            pytest.raises(ConflictError) as exc_info,
        ):
            service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", port_override=6380
            )

        # The old container is already gone, so the retry path is create_service.
        assert "host port 6380 is already allocated" in str(exc_info.value)
        assert "call create_service again" in str(exc_info.value)
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

    def test_update_port_override_repairs_legacy_host_mode(self, mock_docker_client):
        """port_override repairs a legacy host-mode service whose port cannot be inferred.

        In non-traefik host mode without a preset the port is unknowable, so the
        guard would normally raise. Supplying port_override must bypass the guard
        and let the service be recreated with the given port.
        """
        container = self._make_container(
            image_tags=["redis:7"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "redis",
                "oduflow.host_mode": "true",
            },
            attrs={"Config": {"Env": []}, "HostConfig": {}, "Mounts": []},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", port_override=7700
            )

        assert result["config_updated"] is True
        assert result["url"] == "http://localhost:7700"
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

    def test_update_net_admin_override_adds_cap(self, mock_docker_client):
        """cap_add_override adds NET_ADMIN and recreates the container."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", cap_add_override=["NET_ADMIN"]
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["cap_add"] == ["NET_ADMIN"]

    def test_update_clear_net_admin(self, mock_docker_client):
        """cap_add_override=[] removes a previously set capability."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "cap_add": ["NET_ADMIN"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", cap_add_override=[]
            )

        assert result["config_updated"] is True
        run_kwargs = mock_docker_client.containers.run.call_args
        assert "cap_add" not in run_kwargs[1]

    def test_update_privileged_override(self, mock_docker_client):
        """privileged_override switches the service to privileged mode and recreates."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", privileged_override=True
            )

        assert result["config_updated"] is True
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["privileged"] is True

    def test_update_caps_unchanged_no_recreate(self, mock_docker_client):
        """Capability overrides equal to current values do not trigger a recreate."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "cap_add": ["NET_ADMIN"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                cap_add_override=["NET_ADMIN"],
                privileged_override=False,
            )

        assert result["config_updated"] is False
        assert result["image_updated"] is False
        container.stop.assert_not_called()


class TestGetServiceInfo:
    def _make_container(self, image_tags, image_id, labels, attrs, status="running"):
        container = MagicMock()
        container.name = labels.get("__name") or "oduflow-1-svc-redis"
        container.image.tags = image_tags
        container.image.id = image_id
        container.labels = {k: v for k, v in labels.items() if k != "__name"}
        container.attrs = attrs
        container.status = status
        return container

    def test_get_service_info_port_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["redis:7"],
            image_id="sha256:abc123def456",
            labels={
                "__name": "oduflow-1-svc-redis",
                "oduflow.managed": "true",
                "oduflow.service": "redis",
            },
            attrs={
                "Config": {
                    "Env": [
                        "REDIS_PASSWORD=secret",
                        "PATH=/usr/bin",
                    ]
                },
                "NetworkSettings": {
                    "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-vol-1-data",
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "HostConfig": {"CapAdd": ["NET_ADMIN"], "Privileged": False},
                "State": {"StartedAt": "2026-05-26T13:00:00Z"},
                "RestartCount": 2,
            },
        )
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "redis"},
        ):
            info = service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")

        assert info["name"] == "redis"
        assert info["container_name"] == "oduflow-1-svc-redis"
        assert info["image"] == "redis:7"
        assert info["image_digest"] == "sha256:abc123def456"
        assert info["status"] == "running"
        assert info["port"] == 6379
        assert info["url"] == "http://localhost:6379"
        assert info["env_vars"] == {"REDIS_PASSWORD": "secret"}
        assert info["host_mode"] is False
        assert info["volumes"] == [
            {"volume": "data", "mount_path": "/data", "mode": "rw"}
        ]
        assert info["cap_add"] == ["NET_ADMIN"]
        assert info["privileged"] is False
        assert info["restart_count"] == 2
        assert info["started_at"] == "2026-05-26T13:00:00Z"
        assert info["has_preset"] is True

    def test_get_service_info_traefik_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            image_id="sha256:meili123",
            labels={
                "__name": "oduflow-1-svc-meili",
                "oduflow.managed": "true",
                "oduflow.service": "meili",
                "traefik.http.routers.oduflow-1-svc-meili.rule": "Host(`meili.example.com`)",
                "traefik.http.services.oduflow-1-svc-meili.loadbalancer.server.port": "7700",
            },
            attrs={
                "Config": {"Env": ["MEILI_MASTER_KEY=abc"]},
                "Mounts": [],
                "HostConfig": {"CapAdd": [], "Privileged": False},
                "State": {"StartedAt": "2026-05-26T13:00:00Z"},
                "RestartCount": 0,
            },
        )
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            info = service_ops.get_service_info(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili")

        assert info["hostname"] == "meili.example.com"
        assert info["url"] == "https://meili.example.com"
        assert info["port"] == 7700
        assert info["has_preset"] is False

    def test_get_service_info_reports_http_routes(self, mock_docker_client):
        container = self._make_container(
            image_tags=["example/app:1"],
            image_id="sha256:app",
            labels={
                "__name": "oduflow-1-svc-app",
                "oduflow.managed": "true",
                "oduflow.service": "app",
                "oduflow.http_routes": '[{"path":"/api","port":8080,"strip_prefix":false}]',
                "traefik.http.routers.oduflow-1-svc-app-route-1.rule": (
                    "Host(`app.example.com`) && (Path(`/api`) || PathPrefix(`/api/`))"
                ),
            },
            attrs={
                "Config": {"Env": []},
                "Mounts": [],
                "HostConfig": {},
                "State": {},
            },
        )
        mock_docker_client.containers.get.return_value = container
        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            info = service_ops.get_service_info(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "app")

        assert info["port"] is None
        assert info["routes"] == [
            {
                "path": "/api",
                "port": 8080,
                "strip_prefix": False,
                "url": "https://app.example.com/api",
            }
        ]

    def test_get_service_info_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")

    def test_get_service_info_not_managed(self, mock_docker_client):
        """A container with name oduflow-1-svc-X but no oduflow.service label is rejected."""
        container = self._make_container(
            image_tags=["redis:7"],
            image_id="sha256:abc",
            labels={"some_other_label": "foo"},
            attrs={
                "Config": {"Env": []},
                "Mounts": [],
                "HostConfig": {},
                "State": {},
            },
        )
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")


class TestGetServiceLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = (
            b"2025-01-01T00:00:00Z log line 1\n2025-01-01T00:00:01Z log line 2"
        )
        mock_docker_client.containers.get.return_value = container

        output = service_ops.get_service_logs(TEST_SETTINGS, TEST_TEAM, "redis", 50)

        assert "log line 1" in output
        assert "log line 2" in output
        container.logs.assert_called_with(tail=50, timestamps=True)

    def test_logs_default_lines(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"line"
        mock_docker_client.containers.get.return_value = container

        service_ops.get_service_logs(TEST_SETTINGS, TEST_TEAM, "redis")

        container.logs.assert_called_with(tail=100, timestamps=True)

    def test_logs_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_logs(TEST_SETTINGS, TEST_TEAM, "redis")


class TestDestroyBlockedByServices:
    def test_destroy_with_active_services(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.service": "redis", "oduflow.managed": "true"}
        container.name = "oduflow-1-svc-redis"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments/services exist"):
            system_ops.destroy_system(TEST_SETTINGS)


class TestServiceSecrets:
    """secret:<name> references resolve only into the container environment;
    every persisted or displayed shape keeps the reference."""

    def _team(self, tmp_path):
        return TeamSettings(team_id="1", data_dir=str(tmp_path))

    def test_create_resolves_refs_but_persists_references(
        self, mock_docker_client, tmp_path
    ):
        from oduflow import secret_store
        from oduflow.docker_ops import service_presets

        team = self._team(tmp_path)
        secret_store.set_secret(team, "master-key", "hunter2")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(
            TEST_SETTINGS,
            team,
            "meili",
            "getmeili/meilisearch:v1.6",
            7700,
            env_vars={"MEILI_MASTER_KEY": "secret:master-key", "MEILI_ENV": "dev"},
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["environment"] == {
            "MEILI_MASTER_KEY": "hunter2",
            "MEILI_ENV": "dev",
        }
        assert json.loads(run_kwargs["labels"]["oduflow.secret_env"]) == {
            "MEILI_MASTER_KEY": "secret:master-key"
        }
        preset = service_presets.get_preset(team, "meili")
        assert preset["env_vars"] == {
            "MEILI_MASTER_KEY": "secret:master-key",
            "MEILI_ENV": "dev",
        }

    def test_create_with_dangling_ref_touches_nothing(
        self, mock_docker_client, tmp_path
    ):
        team = self._team(tmp_path)

        with pytest.raises(PrerequisiteNotMetError, match="gone"):
            service_ops.create_service(
                TEST_SETTINGS,
                team,
                "meili",
                "getmeili/meilisearch:v1.6",
                7700,
                env_vars={"KEY": "secret:gone"},
            )

        mock_docker_client.images.pull.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_container_env_shows_reference_not_value(self):
        container = MagicMock()
        container.labels = {
            "oduflow.secret_env": json.dumps({"KEY": "secret:master-key"})
        }
        container.attrs = {"Config": {"Env": ["KEY=hunter2", "OTHER=x"]}}

        assert service_ops._container_env_vars(container) == {
            "KEY": "secret:master-key",
            "OTHER": "x",
        }

    def test_update_with_dangling_ref_keeps_the_container(
        self, mock_docker_client, tmp_path
    ):
        team = self._team(tmp_path)
        container = MagicMock()
        container.labels = {"oduflow.service": "meili"}
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        mock_docker_client.containers.get.return_value = container

        with (
            patch(
                "oduflow.docker_ops.service_ops.service_presets.get_preset",
                return_value={
                    "name": "meili",
                    "image": "getmeili/meilisearch:v1.6",
                    "port": 7700,
                    "env_vars": {"KEY": "secret:gone"},
                },
            ),
            pytest.raises(PrerequisiteNotMetError, match="gone"),
        ):
            service_ops.update_service(TEST_SETTINGS, team, "meili")

        container.stop.assert_not_called()
        container.remove.assert_not_called()
        # The reference is validated before the image pull, so a dangling one
        # costs neither a registry round-trip nor a download.
        mock_docker_client.images.pull.assert_not_called()

    def test_update_rotated_secret_value_recreates_container(
        self, mock_docker_client, tmp_path
    ):
        """The documented rotation flow (replace the value, then update_service)
        must recreate the container even though neither the reference-form
        config nor the image digest changed."""
        from oduflow import secret_store

        team = self._team(tmp_path)
        secret_store.set_secret(team, "master-key", "new-value")
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "meili"}
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.image.id = "sha256:same"
        container.attrs = {"Config": {"Env": ["KEY=old-value"]}}
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        new_image = MagicMock()
        new_image.id = "sha256:same"
        mock_docker_client.images.pull.return_value = new_image
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "meili",
                "image": "getmeili/meilisearch:v1.6",
                "port": 7700,
                "hostname": "",
                "env_vars": {"KEY": "secret:master-key"},
            },
        ):
            result = service_ops.update_service(TEST_SETTINGS, team, "meili")

        assert result["config_updated"] is True
        container.stop.assert_called_once()
        container.remove.assert_called_once()
        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["environment"] == {"KEY": "new-value"}

    def test_update_unrotated_secret_stays_a_noop(self, mock_docker_client, tmp_path):
        """An unchanged secret value must not force a recreation."""
        from oduflow import secret_store

        team = self._team(tmp_path)
        secret_store.set_secret(team, "master-key", "same-value")
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "meili"}
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.image.id = "sha256:same"
        container.attrs = {"Config": {"Env": ["KEY=same-value"]}}
        mock_docker_client.containers.get.return_value = container
        new_image = MagicMock()
        new_image.id = "sha256:same"
        mock_docker_client.images.pull.return_value = new_image

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "meili",
                "image": "getmeili/meilisearch:v1.6",
                "port": 7700,
                "hostname": "",
                "env_vars": {"KEY": "secret:master-key"},
            },
        ):
            result = service_ops.update_service(TEST_SETTINGS, team, "meili")

        assert result["config_updated"] is False
        assert result["image_updated"] is False
        container.stop.assert_not_called()
        container.remove.assert_not_called()
