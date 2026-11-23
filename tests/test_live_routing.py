"""End-to-end routing on the enrolled Docker host."""

import time
from contextlib import ExitStack, closing
from dataclasses import replace

import httpx
import pytest

from oduflow.docker_ops import env_ops, odoo_ops, service_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.naming import get_team_network_name

pytestmark = pytest.mark.integration


def test_each_environment_serves_its_database_on_both_addresses(
    live_environment, tmp_path
):
    isolated, original = live_environment
    team = replace(original, hostname="dev.qa.test")
    settings = replace(
        isolated,
        teams={team.team_id: team},
        routing_mode="traefik",
        routing_tls=False,
        public_scheme_setting="http",
        allow_local_path=True,
    )
    system_ops.init_system(settings)
    ports = set()
    with (
        closing(get_client()) as client,
        httpx.Client(trust_env=False, timeout=10) as web,
    ):
        for name in ("first", "second"):
            checkout = tmp_path / name
            checkout.mkdir()
            result = env_ops.create_environment(
                settings,
                team,
                "main",
                "",
                "odoo:19.0",
                env_name=name,
                local_path=str(checkout),
            )
            port = result["local_port"]
            assert port not in ports
            ports.add(port)
            assert result["local_url"] == f"http://127.0.0.1:{port}"
            host = f"{name}.{team.hostname}"
            assert result["url"] == f"http://{host}"
            container = client.containers.get(result["odoo_container"])
            assert container.ports["8069/tcp"] == [
                {"HostIp": "127.0.0.1", "HostPort": str(port)}
            ]
            session = odoo_ops.connect_as_user(settings, team, name, "admin")
            for origin, headers in (
                (result["local_url"], {}),
                ("http://127.0.0.1:80", {"Host": host}),
            ):
                response = web.post(
                    origin + "/web/session/get_session_info",
                    headers={**headers, "Cookie": f"session_id={session['sid']}"},
                    json={"jsonrpc": "2.0", "method": "call", "params": {}, "id": 1},
                )
                response.raise_for_status()
                assert response.json()["result"]["db"] == result["database"]


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
                try:
                    response = web.get("https://127.0.0.1/proof", headers=headers)
                except (httpx.ConnectError, httpx.TimeoutException):
                    if time.monotonic() >= deadline:
                        raise
                else:
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
