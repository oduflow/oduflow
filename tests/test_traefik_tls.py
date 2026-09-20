import json
import ssl
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import env_ops, odoo_ops, system_ops
from oduflow.docker_ops.env_ops import build_env_traefik_labels
from oduflow.settings import ExtraRoute, Settings, TeamSettings


def _traefik_settings(
    tmp_path, tls, extra_routes=(), public_scheme="", team_public_scheme=""
):
    team = TeamSettings(
        team_id="1",
        hostname="dev.example.com",
        public_scheme_setting=team_public_scheme,
    )
    return Settings(
        routing_mode="traefik",
        routing_tls=tls,
        acme_email="admin@example.com",
        etc_dir=str(tmp_path),
        teams={"1": team},
        extra_routes=tuple(extra_routes),
        public_scheme_setting=public_scheme,
    )


class TestWriteDynamicConfig:
    def test_tls_router_uses_websecure(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, True), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-team-1"]
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}

    def test_no_tls_router_uses_web(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, False), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-team-1"]
        assert router["entryPoints"] == ["web"]
        assert "tls" not in router

    def test_connect_landing_router_high_priority(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, False), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-connect"]
        assert router["rule"] == "PathPrefix(`/oduflow-connect`)"
        assert router["service"] == "oduflow"
        # Must outrank the env's docker Host(...) router for this path.
        assert router["priority"] == 100000
        assert router["entryPoints"] == ["web"]

    def test_connect_landing_router_tls_entrypoint(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, True), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-connect"]
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}

    def test_extra_route_generates_router_and_service(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        route = ExtraRoute(
            name="legacy-api", host="api.example.com", url="https://10.0.0.5:8443"
        )
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, True, [route]), str(cfg)
        )
        http = json.loads(cfg.read_text())["http"]
        router = http["routers"]["oduflow-route-legacy-api"]
        assert router["rule"] == "Host(`api.example.com`)"
        assert router["service"] == "oduflow-route-legacy-api"
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}
        service = http["services"]["oduflow-route-legacy-api"]
        assert service["loadBalancer"]["servers"] == [{"url": "https://10.0.0.5:8443"}]

    def test_extra_route_loopback_rewritten_to_host(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        route = ExtraRoute(
            name="local", host="api.example.com", url="http://127.0.0.1:3000"
        )
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, False, [route]), str(cfg)
        )
        http = json.loads(cfg.read_text())["http"]
        service = http["services"]["oduflow-route-local"]
        assert service["loadBalancer"]["servers"] == [
            {"url": "http://host.docker.internal:3000"}
        ]
        # A non-loopback host is left untouched.
        assert (
            system_ops._resolve_upstream_url("http://192.168.1.9:3000")
            == "http://192.168.1.9:3000"
        )
        # localhost is rewritten too, but a hostname that merely starts with
        # "localhost" (e.g. localhost.example.com) is not.
        assert (
            system_ops._resolve_upstream_url("http://localhost:5000")
            == "http://host.docker.internal:5000"
        )
        assert (
            system_ops._resolve_upstream_url("http://localhost.example.com:5000")
            == "http://localhost.example.com:5000"
        )
        # https:// loopbacks are NOT rewritten: swapping the host would break
        # backend TLS cert verification (cert is for localhost/127.0.0.1).
        assert (
            system_ops._resolve_upstream_url("https://localhost:5000")
            == "https://localhost:5000"
        )
        assert (
            system_ops._resolve_upstream_url("https://127.0.0.1:5000")
            == "https://127.0.0.1:5000"
        )


class TestEnsureTraefik:
    def _client_no_container(self):
        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nf")
        client.volumes.get.side_effect = docker.errors.NotFound("nf")
        return client

    def test_tls_mode_publishes_443_and_acme(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, True))
        kwargs = client.containers.run.call_args[1]
        assert kwargs["ports"] == {"80/tcp": 80, "443/tcp": 443}
        cmd = kwargs["command"]
        assert "--entrypoints.websecure.address=:443" in cmd
        assert any("redirections" in a for a in cmd)
        assert any("certificatesresolvers" in a for a in cmd)
        assert "oduflow-traefik-acme" in kwargs["volumes"]
        # Traefik terminates TLS itself, so no upstream forwarded-header trust.
        assert not any("forwardedHeaders" in a for a in cmd)

    def test_mounts_dynamic_directory_with_file_provider(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, True))
        kwargs = client.containers.run.call_args[1]
        cmd = kwargs["command"]
        # Directory provider (not single-file) so operators can drop in *.yml.
        assert "--providers.file.directory=/etc/traefik/dynamic" in cmd
        assert not any(a.startswith("--providers.file.filename=") for a in cmd)
        dyn = str(tmp_path / "traefik-dynamic")
        assert kwargs["volumes"][dyn] == {"bind": "/etc/traefik/dynamic", "mode": "ro"}
        # Oduflow's generated config lands inside that directory.
        assert (tmp_path / "traefik-dynamic" / "oduflow.yml").is_file()

    def test_no_tls_mode_port_80_only(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        kwargs = client.containers.run.call_args[1]
        assert kwargs["ports"] == {"80/tcp": 80}
        cmd = kwargs["command"]
        assert "--entrypoints.web.address=:80" in cmd
        assert "--entrypoints.websecure.address=:443" not in cmd
        assert not any("redirections" in a for a in cmd)
        assert not any("certificatesresolvers" in a for a in cmd)
        assert "oduflow-traefik-acme" not in kwargs["volumes"]
        # Trust the upstream tunnel's X-Forwarded-* headers so X-Forwarded-Proto:
        # https survives to Oduflow (cookie Secure flag, https:// links).
        assert "--entrypoints.web.forwardedHeaders.insecure=true" in cmd
        # ACME volume is not created when TLS is off.
        client.volumes.create.assert_not_called()

    def test_drift_recreates_on_tls_change(self, tmp_path):
        # Existing container was built in TLS mode, but config now wants
        # tls=false: the stale container is removed and a new one created.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--providers.file.directory=/etc/traefik/dynamic",
                    "--entrypoints.web.http.redirections.entryPoint.to=websecure",
                ]
            }
        }
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        client.containers.run.assert_called_once()
        assert client.containers.run.call_args[1]["ports"] == {"80/tcp": 80}

    def test_drift_recreates_on_old_single_file_provider(self, tmp_path):
        # Existing container watches a single file (older layout); TLS matches
        # but it must be recreated onto the directory provider so operator
        # drop-in *.yml files are honoured.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--entrypoints.web.address=:80",
                    "--providers.file.filename=/etc/traefik/dynamic/oduflow.yml",
                ]
            }
        }
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        client.containers.run.assert_called_once()

    def test_no_drift_when_mode_matches(self, tmp_path):
        # Existing container already in the desired (no-TLS, upstream-trusting,
        # directory) mode: reuse it.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--entrypoints.web.address=:80",
                    "--entrypoints.web.forwardedHeaders.insecure=true",
                    "--providers.file.directory=/etc/traefik/dynamic",
                ]
            }
        }
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()

    def test_plain_http_mode_does_not_trust_forwarded_headers(self, tmp_path):
        # public_scheme = "http": nothing terminates TLS in front, so the :80
        # entrypoint is directly exposed and must not believe a client-supplied
        # X-Forwarded-Proto (which would forge a "secure" request).
        client = self._client_no_container()
        system_ops._ensure_traefik(
            client, _traefik_settings(tmp_path, False, public_scheme="http")
        )
        cmd = client.containers.run.call_args[1]["command"]
        assert client.containers.run.call_args[1]["ports"] == {"80/tcp": 80}
        assert not any("forwardedHeaders" in a for a in cmd)

    def test_team_https_override_trusts_forwarded_headers(self, tmp_path):
        # Mixed deployment: global public_scheme = "http" (LAN team) but one
        # team sits behind a TLS-terminating upstream (Cloudflare tunnel). The
        # shared web entrypoint must trust X-Forwarded-* so the tunnel's
        # X-Forwarded-Proto: https survives for that team.
        client = self._client_no_container()
        system_ops._ensure_traefik(
            client,
            _traefik_settings(
                tmp_path, False, public_scheme="http", team_public_scheme="https"
            ),
        )
        cmd = client.containers.run.call_args[1]["command"]
        assert "--entrypoints.web.forwardedHeaders.insecure=true" in cmd

    def test_drift_recreates_when_public_scheme_drops_tls(self, tmp_path):
        # Container was built for an upstream terminator (forwarded headers
        # trusted); the operator has since set public_scheme = "http".
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--entrypoints.web.address=:80",
                    "--entrypoints.web.forwardedHeaders.insecure=true",
                    "--providers.file.directory=/etc/traefik/dynamic",
                ]
            }
        }
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(
            client, _traefik_settings(tmp_path, False, public_scheme="http")
        )
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        cmd = client.containers.run.call_args[1]["command"]
        assert not any("forwardedHeaders" in a for a in cmd)


def _env_settings(routing_mode, tls):
    team = TeamSettings(team_id="1", hostname="dev.example.com")
    return Settings(
        routing_mode=routing_mode,
        routing_tls=tls,
        teams={"1": team},
    )


class TestBuildEnvTraefikLabels:
    """A3: single source of an environment's Traefik routing labels."""

    ENV = "18.0"  # slug "180"
    ROUTER = "oduflow-1-180"

    def _labels(self, routing_mode, tls):
        settings = _env_settings(routing_mode, tls)
        return build_env_traefik_labels(settings, settings.teams["1"], self.ENV)

    def test_port_mode_has_no_traefik_labels(self):
        assert self._labels("port", False) == {}
        assert self._labels("port", True) == {}

    def test_no_tls_uses_web_entrypoint(self):
        labels = self._labels("traefik", False)
        assert labels["traefik.enable"] == "true"
        assert (
            labels[f"traefik.http.routers.{self.ROUTER}.rule"]
            == "Host(`180.dev.example.com`)"
        )
        assert (
            labels[f"traefik.http.services.{self.ROUTER}.loadbalancer.server.port"]
            == "8069"
        )
        assert labels["traefik.docker.network"] == "oduflow-1-net"
        assert labels[f"traefik.http.routers.{self.ROUTER}.entrypoints"] == "web"
        assert f"traefik.http.routers.{self.ROUTER}.tls" not in labels

    def test_tls_uses_websecure_and_certresolver(self):
        labels = self._labels("traefik", True)
        assert labels[f"traefik.http.routers.{self.ROUTER}.entrypoints"] == "websecure"
        assert labels[f"traefik.http.routers.{self.ROUTER}.tls"] == "true"
        assert (
            labels[f"traefik.http.routers.{self.ROUTER}.tls.certresolver"]
            == "letsencrypt"
        )

    def test_reusable_short_hostname_replaces_branch_slug(self):
        settings = _env_settings("traefik", True)
        labels = build_env_traefik_labels(
            settings, settings.teams["1"], self.ENV, "dev3"
        )

        assert (
            labels[f"traefik.http.routers.{self.ROUTER}.rule"]
            == "Host(`dev3.example.com`)"
        )


def test_self_signed_dynamic_and_environment_routes(tmp_path):
    settings = _traefik_settings(
        tmp_path,
        True,
        [ExtraRoute(name="extra", host="extra.example.com", url="http://10.0.0.1:80")],
    )
    settings = replace(settings, routing_tls_auto=False)
    cfg = tmp_path / "traefik.yml"
    system_ops._write_traefik_dynamic_config(settings, str(cfg))
    for router in json.loads(cfg.read_text())["http"]["routers"].values():
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {}
    labels = build_env_traefik_labels(settings, settings.teams["1"], "main")
    assert labels["traefik.http.routers.oduflow-1-main.tls"] == "true"
    assert not any("certresolver" in key for key in labels)


# Modes: "acme" = tls = true (auto resolver), "manual" = tls = {} with an
# acme_email (resolver declared, assigned only explicitly), "manual_noacme" =
# tls = {} without acme_email (HTTPS, no ACME at all), "http" = tls = false.
_TLS_MODES = ["acme", "manual", "manual_noacme", "http"]


def _mode_settings(settings, mode):
    return replace(
        settings,
        routing_tls=mode != "http",
        routing_tls_auto=mode == "acme",
        acme_email="" if mode == "manual_noacme" else "admin@example.com",
    )


def _resolver_declared(mode):
    return mode in ("acme", "manual")


@pytest.mark.parametrize("old_mode", _TLS_MODES)
@pytest.mark.parametrize("new_mode", _TLS_MODES)
def test_tls_mode_transition(tmp_path, old_mode, new_mode):
    settings = _mode_settings(_traefik_settings(tmp_path, True), old_mode)
    client = TestEnsureTraefik()._client_no_container()
    system_ops._ensure_traefik(client, settings)
    old_kwargs = client.containers.run.call_args.kwargs
    existing = MagicMock()
    existing.status = "running"
    existing.attrs = {"Config": {"Cmd": old_kwargs["command"]}}
    client.containers.get.side_effect = None
    client.containers.get.return_value = existing
    client.containers.run.reset_mock()
    client.volumes.reset_mock()
    client.volumes.get.side_effect = None
    acme_volume = MagicMock()
    client.volumes.get.return_value = acme_volume
    settings = _mode_settings(settings, new_mode)
    system_ops._ensure_traefik(client, settings)
    # The container is recreated only on command drift: the HTTP->HTTPS
    # redirect (tls on/off) or the declared resolver changed. acme <-> manual
    # share the same command, so the container survives that switch.
    recreate = (old_mode == "http") != (new_mode == "http") or _resolver_declared(
        old_mode
    ) != _resolver_declared(new_mode)
    if recreate:
        existing.remove.assert_called_once()
        kwargs = client.containers.run.call_args.kwargs
    else:
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()
        kwargs = old_kwargs
    assert ("443/tcp" in kwargs["ports"]) == (new_mode != "http")
    assert any("redirections" in arg for arg in kwargs["command"]) == (
        new_mode != "http"
    )
    assert any("certificatesresolvers" in arg for arg in kwargs["command"]) == (
        _resolver_declared(new_mode)
    )
    assert (settings.traefik_acme_volume in kwargs["volumes"]) == _resolver_declared(
        new_mode
    )
    if not _resolver_declared(new_mode):
        # Disabling ACME stops mounting the store but never deletes it: the
        # issued certificates and the account key survive a later re-enable.
        client.volumes.get.assert_not_called()
        client.volumes.create.assert_not_called()
    acme_volume.remove.assert_not_called()
    client.volumes.remove.assert_not_called()
    # Managed routes reference the resolver only in auto mode: in manual mode
    # it is declared but left to explicitly configured (drop-in) routes.
    dynamic = json.loads((tmp_path / "traefik-dynamic" / "oduflow.yml").read_text())
    router = dynamic["http"]["routers"]["oduflow-team-1"]
    if new_mode == "http":
        assert router["entryPoints"] == ["web"]
    else:
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == (
            {"certResolver": "letsencrypt"} if new_mode == "acme" else {}
        )
    env_labels = build_env_traefik_labels(settings, settings.teams["1"], "main")
    assert any("certresolver" in key for key in env_labels) == (new_mode == "acme")


@pytest.mark.parametrize(
    "mode,expected",
    [("acme", True), ("manual", True), ("manual_noacme", False), ("http", False)],
)
def test_acme_enabled_property(tmp_path, mode, expected):
    settings = _mode_settings(_traefik_settings(tmp_path, True), mode)
    assert settings.acme_enabled is expected
    # Port mode never declares a resolver, whatever the email says.
    assert replace(settings, routing_mode="port").acme_enabled is False


# ---------------------------------------------------------------------------
# Internal probes of Oduflow's own public URLs
# ---------------------------------------------------------------------------


def _probe_settings(tmp_path, mode):
    """Settings for each routing/TLS combination the probes must cope with."""
    if mode == "port":
        return replace(_traefik_settings(tmp_path, True), routing_mode="port")
    settings = _traefik_settings(tmp_path, mode != "http")
    return replace(settings, routing_tls_auto=mode == "acme")


@pytest.mark.parametrize(
    "mode,expected",
    [("acme", False), ("self_signed", True), ("http", False), ("port", False)],
)
def test_uses_default_tls_cert(tmp_path, mode, expected):
    assert _probe_settings(tmp_path, mode).uses_default_tls_cert is expected


@pytest.mark.parametrize("mode", ["acme", "http", "port"])
def test_probe_context_verifies_by_default(tmp_path, mode):
    """Anything with a real trust anchor keeps full certificate verification."""
    assert env_ops.public_url_ssl_context(_probe_settings(tmp_path, mode)) is None


def test_probe_context_skips_verification_for_default_cert(tmp_path):
    """`tls = {}` serves a self-signed certificate: nothing to verify against,
    so a verifying probe would fail the handshake on every call."""
    context = env_ops.public_url_ssl_context(_probe_settings(tmp_path, "self_signed"))
    assert context is not None
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def _https_response():
    response = MagicMock()
    response.status = 200
    response.headers = {}
    response.read.return_value = b"{}"
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *exc: False
    return response


@pytest.mark.parametrize("mode,verifies", [("self_signed", False), ("acme", True)])
def test_http_request_to_odoo_probe_context(tmp_path, mode, verifies):
    settings = _probe_settings(tmp_path, mode)
    team = settings.teams["1"]
    with (
        patch(
            "oduflow.docker_ops.env_ops.get_env_base_url",
            return_value=("https://main.dev.example.com", "main.dev.example.com"),
        ),
        patch("urllib.request.urlopen", return_value=_https_response()) as mock_open,
    ):
        result = odoo_ops.http_request_to_odoo(settings, team, "main", "/web/health")

    assert result["status_code"] == 200
    context = mock_open.call_args.kwargs["context"]
    assert (context is None) is verifies


@pytest.mark.parametrize("mode,verifies", [("self_signed", False), ("acme", True)])
def test_wait_for_odoo_ready_probe_context(tmp_path, mode, verifies):
    settings = _probe_settings(tmp_path, mode)
    team = settings.teams["1"]
    with (
        patch(
            "oduflow.docker_ops.env_ops.get_env_base_url",
            return_value=("https://main.dev.example.com", "main.dev.example.com"),
        ),
        patch("urllib.request.urlopen", return_value=_https_response()) as mock_open,
    ):
        assert env_ops.wait_for_odoo_ready(settings, team, "main") is True

    context = mock_open.call_args.kwargs["context"]
    assert (context is None) is verifies
