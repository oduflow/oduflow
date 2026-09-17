"""An environment's reported URL must match the route it actually serves.

Traefik labels are frozen when a container is created, and ADR 0064
deliberately performs no migration when a team gains a ``base_domain``: the
existing environments keep their old nested hostname until their next
``update_environment``. Recomputing the URL from current settings therefore
hands out a name Traefik does not route.
"""

from unittest.mock import MagicMock

import pytest

from oduflow.docker_ops.env_ops import (
    ENV_HOSTNAME_LABEL,
    container_route_host,
    get_env_base_url,
)
from oduflow.settings import Settings, TeamSettings

LEGACY_HOST = "feature-x.oduflow.demo.example.com"
ZONE_HOST = "feature-x.demo.example.com"


@pytest.fixture
def team(tmp_path):
    return TeamSettings(
        team_id="1",
        hostname="oduflow.demo.example.com",
        base_domain="demo.example.com",
        data_dir=str(tmp_path / "team"),
    )


@pytest.fixture
def settings(team):
    return Settings(routing_mode="traefik", routing_tls=True, teams={"1": team})


def _container(labels):
    container = MagicMock()
    container.labels = labels
    return container


class TestContainerRouteHost:
    def test_reads_the_host_out_of_the_router_rule(self):
        container = _container(
            {"traefik.http.routers.oduflow-1-feature-x.rule": f"Host(`{LEGACY_HOST}`)"}
        )

        assert container_route_host(container) == LEGACY_HOST

    def test_ignores_non_rule_labels(self):
        container = _container(
            {
                "traefik.enable": "true",
                "oduflow.managed": "true",
                "traefik.http.routers.oduflow-1-feature-x.entrypoints": "websecure",
            }
        )

        assert container_route_host(container) == ""

    def test_no_labels_at_all(self):
        assert container_route_host(_container({})) == ""


class TestGetEnvBaseUrl:
    def test_pre_existing_environment_keeps_its_nested_host(self, settings, team):
        """The team gained base_domain after this container was created."""
        container = _container(
            {
                "traefik.http.routers.oduflow-1-feature-x.rule": (
                    f"Host(`{LEGACY_HOST}`)"
                )
            }
        )

        url, cookie_domain = get_env_base_url(settings, team, "feature-x", container)

        assert url == f"https://{LEGACY_HOST}"
        assert cookie_domain == LEGACY_HOST

    def test_environment_created_in_the_zone_reports_the_zone_host(
        self, settings, team
    ):
        container = _container(
            {"traefik.http.routers.oduflow-1-feature-x.rule": f"Host(`{ZONE_HOST}`)"}
        )

        url, _ = get_env_base_url(settings, team, "feature-x", container)

        assert url == f"https://{ZONE_HOST}"

    def test_falls_back_to_the_computed_name_without_a_rule(self, settings, team):
        """Port-mode leftovers and half-built containers have no router rule."""
        container = _container({ENV_HOSTNAME_LABEL: ""})

        url, _ = get_env_base_url(settings, team, "feature-x", container)

        assert url == f"https://{ZONE_HOST}"

    def test_port_routing_is_untouched(self, tmp_path):
        team = TeamSettings(
            team_id="1", hostname="dev.example.com", data_dir=str(tmp_path / "t")
        )
        settings = Settings(routing_mode="port", routing_tls=False, teams={"1": team})
        container = _container({})
        container.attrs = {
            "NetworkSettings": {"Ports": {"8069/tcp": [{"HostPort": "50001"}]}}
        }

        url, cookie_domain = get_env_base_url(settings, team, "feature-x", container)

        assert url == "http://dev.example.com:50001"
        assert cookie_domain == "dev.example.com"
