"""Two equal service names must route to different team networks over TLS."""

import time
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest

from oduflow.docker_ops import service_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.naming import get_team_network_name

pytestmark = pytest.mark.integration


def test_same_name_in_two_teams_routes_to_the_right_network(live_environment, tmp_path):
    isolated, original = live_environment
    teams = {}
    for uid in ("1", "2"):
        root = tmp_path / ("team_" + uid)
        root.mkdir()
        teams[uid] = replace(
            original,
            team_id=uid,
            data_dir=str(root),
            hostname=f"t{uid}.qa.test",
            port_registry_path=str(root / "ports.json"),
            hostname_registry_path=str(root / "hostnames.json"),
        )
    settings = replace(
        isolated,
        teams=teams,
        routing_mode="traefik",
        routing_tls=True,
        routing_tls_auto=False,
    )
    system_ops.init_system(settings)
    client = get_client()
    # tls={} deliberately uses Traefik's generated certificate; do not request
    # a public certificate for the private fixture domains.
    with (
        ExitStack() as cleanup,
        httpx.Client(verify=False, trust_env=False, timeout=2) as web,
    ):
        for uid, team in teams.items():
            service = service_ops.create_service(
                settings,
                team,
                "files",
                "busybox:1.36.1",
                8123,
                env_vars={"TENANT": uid},
                command=[
                    "sh",
                    "-ec",
                    'mkdir /www; echo "$TENANT" >/www/proof; exec httpd -f -p 8123 -h /www',
                ],
            )
            cleanup.callback(
                service_ops.delete_service, settings, team, "files", save_preset=False
            )
            network = get_team_network_name(uid, settings.prefix)
            assert set(
                client.containers.get(service["container_name"]).attrs[
                    "NetworkSettings"
                ]["Networks"]
            ) == {network}
            assert (
                network
                in client.containers.get(settings.traefik_container).attrs[
                    "NetworkSettings"
                ]["Networks"]
            )
            assert service["url"] == f"https://files.{team.hostname}"
        for uid, team in teams.items():
            headers = {"Host": "files." + team.hostname}
            deadline = time.monotonic() + 30
            while True:
                response = web.get("https://127.0.0.1/proof", headers=headers)
                if response.status_code == 200:
                    assert response.text == uid + "\n"
                    break
                assert time.monotonic() < deadline, response.status_code
                time.sleep(0.25)
            redirect = web.get("http://127.0.0.1/proof", headers=headers)
            assert (
                redirect.is_redirect
                and redirect.headers["location"]
                == f"https://files.{team.hostname}/proof"
            )
