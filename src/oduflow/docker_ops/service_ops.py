from __future__ import annotations

import datetime
import json
import logging
import posixpath
import re
from typing import Any

import docker
from oduflow import secret_store
from oduflow.docker_ops import service_presets, volume_ops
from oduflow.docker_ops.client import (
    docker_error_detail,
    docker_operation_error,
    get_client,
)
from oduflow.errors import (
    ConflictError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.locking import keyed_mutex, service_registry_key
from oduflow.naming import get_service_container_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_TRAEFIK_ACME_MOUNT_PATH = "/etc/traefik"
_HTTP_ROUTES_LABEL = "oduflow.http_routes"
# KEY -> "secret:<name>" for env vars whose configured value is a secret
# reference. Lives on the container (not only in the preset) so read surfaces
# keep showing the reference even if the preset is deleted.
_SECRET_ENV_LABEL = "oduflow.secret_env"

_SYSTEM_ENV_KEYS = {
    "PATH",
    "HOME",
    "HOSTNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "GOPATH",
    "JAVA_HOME",
    "SHLVL",
    "PWD",
    "OLDPWD",
    "SHELL",
    "USER",
    "LOGNAME",
    "MAIL",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "LESS",
    "LESSOPEN",
    "LESSCLOSE",
    "LS_COLORS",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_DIRS",
    "XDG_CONFIG_DIRS",
    "XDG_CACHE_HOME",
    "DISPLAY",
    "GPG_AGENT_INFO",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "DBUS_SESSION_BUS_ADDRESS",
}


def _raise_service_start_error(
    name: str,
    port: int | None,
    exc: docker.errors.DockerException,
    *,
    retry_with: str,
) -> None:
    """Translate a failed service start into an actionable FlowError.

    A host-port clash is the one failure an agent can fix on its own, so it
    gets a dedicated ConflictError with the retry path spelled out.
    ``retry_with`` names the tool that can apply a new port: after a failed
    create the name is free again, so ``create_service``; after a failed
    restart the stopped container still holds the name, so ``update_service``.
    Every other daemon explanation describes the caller's own image/port/volume
    parameters and is passed through without the SDK's HTTP wrapper.
    """
    detail = docker_error_detail(exc).lower()
    if "port is already allocated" in detail or (
        "bind for " in detail and "address already in use" in detail
    ):
        port_label = f" {port}" if port is not None else ""
        raise ConflictError(
            f"Could not start service '{name}': host port{port_label} is already "
            f"allocated. Choose a different host port and call {retry_with}."
        ) from exc
    raise docker_operation_error(f"start service '{name}'", exc) from exc


def _pull_service_image(client: Any, image: str) -> Any:
    """Pull a service image and expose only safe, actionable failures."""
    try:
        return client.images.pull(image)
    except docker.errors.NotFound as exc:
        raise NotFoundError(
            f"Docker image '{image}' was not found or is not accessible. "
            "Check the image name, tag, and registry permissions."
        ) from exc
    except docker.errors.DockerException as exc:
        raise PrerequisiteNotMetError(
            f"Could not pull Docker image '{image}'. Check Docker connectivity, "
            "registry availability, and registry credentials."
        ) from exc


def normalize_http_routes(
    routes: list[dict[str, object]] | None,
) -> list[dict[str, object]] | None:
    """Validate and canonicalize restricted HTTP path routes.

    Routes are deliberately self-targeting: they expose another HTTP port of
    the same auxiliary service, never an arbitrary hostname or URL.  This keeps
    the tenant boundary intact even though Traefik is attached to every team
    network.
    """
    if routes is None:
        return None
    if not isinstance(routes, list):
        raise ValueError("routes must be a JSON array of route objects.")

    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, route in enumerate(routes, start=1):
        if not isinstance(route, dict):
            raise ValueError(f"Route #{index} must be a JSON object.")
        unknown_fields = set(route) - {"path", "port", "strip_prefix"}
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(f"Route #{index} contains unsupported fields: {fields}.")

        raw_path = route.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"Route #{index} path must be a non-empty string.")
        if raw_path != raw_path.strip():
            raise ValueError(
                f"Route path '{raw_path}' must not contain outer whitespace."
            )
        if not raw_path.startswith("/"):
            raise ValueError(f"Route path '{raw_path}' must start with '/'.")
        if any(ch in raw_path for ch in ("?", "#", "`")) or any(
            ord(ch) < 32 or ch.isspace() for ch in raw_path
        ):
            raise ValueError(
                f"Route path '{raw_path}' contains unsupported URL characters."
            )
        if "//" in raw_path or any(part in (".", "..") for part in raw_path.split("/")):
            raise ValueError(
                f"Route path '{raw_path}' must not contain empty or dot segments."
            )

        path = raw_path.rstrip("/") or "/"
        if path in seen:
            raise ValueError(f"Duplicate route path '{path}'.")
        seen.add(path)

        raw_port = route.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int):
            raise ValueError(f"Route '{path}' port must be an integer from 1 to 65535.")
        port = raw_port
        if not 1 <= port <= 65535:
            raise ValueError(f"Route '{path}' port must be between 1 and 65535.")

        raw_strip = route.get("strip_prefix", False)
        if not isinstance(raw_strip, bool):
            raise ValueError(f"Route '{path}' strip_prefix must be true or false.")
        normalized.append({"path": path, "port": port, "strip_prefix": raw_strip})
    return normalized


def _validate_service_exposure(
    settings: Settings,
    port: int | None,
    routes: list[dict[str, object]] | None,
) -> None:
    """Require exactly one supported public exposure model."""
    if routes:
        if settings.routing_mode != "traefik":
            raise ValueError("HTTP path routes require routing.mode = 'traefik'.")
        if port not in (None, 0):
            raise ValueError("Pass either port or routes, not both.")
        return
    if port is None or isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535 when routes are not set.")


def _route_rule(hostname: str, path: str) -> str:
    """Return a segment-safe prefix rule (/api and /api/*, never /apix)."""
    if path == "/":
        path_rule = "PathPrefix(`/`)"
    else:
        path_rule = f"(Path(`{path}`) || PathPrefix(`{path}/`))"
    return f"Host(`{hostname}`) && {path_rule}"


def _routes_from_labels(labels: dict[str, str]) -> list[dict[str, object]] | None:
    raw = labels.get(_HTTP_ROUTES_LABEL)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return normalize_http_routes(parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Ignoring invalid HTTP routes label", exc_info=True)
        return None


def _routes_with_urls(
    routes: list[dict[str, object]] | None, hostname: str | None, scheme: str
) -> list[dict[str, object]]:
    if not routes:
        return []
    result: list[dict[str, object]] = []
    for route in routes:
        item = dict(route)
        if hostname:
            item["url"] = f"{scheme}://{hostname}{route['path']}"
        result.append(item)
    return result


def _resolve_service_volume_binds(
    settings: Settings,
    team: TeamSettings,
    volumes: list[dict[str, str]] | None,
    *,
    client: Any = None,
) -> dict[str, dict[str, str]]:
    """Resolve user mounts and add the implicit Traefik ACME mount.

    The ACME store is platform-owned rather than part of the user-supplied
    service configuration. Every service created while Oduflow terminates TLS
    through Traefik sees the exact store at ``/etc/traefik`` read-only.
    """
    volume_binds: dict[str, dict[str, str]] = volume_ops.resolve_volume_binds(
        team, volumes or []
    )

    if settings.routing_mode != "traefik" or not settings.routing_tls:
        return volume_binds

    for mount in volumes or []:
        mount_path = posixpath.normpath(mount.get("mount_path", ""))
        if mount_path == _TRAEFIK_ACME_MOUNT_PATH or mount_path.startswith(
            f"{_TRAEFIK_ACME_MOUNT_PATH}/"
        ):
            raise ConflictError(
                f"Mount path '{mount.get('mount_path')}' is inside reserved "
                f"'{_TRAEFIK_ACME_MOUNT_PATH}', which Oduflow uses for "
                "the read-only Traefik certificate store."
            )

    docker_client = client or get_client()
    try:
        docker_client.volumes.get(settings.traefik_acme_volume)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Traefik ACME volume '{settings.traefik_acme_volume}' not found. "
            "Run init_system before creating services in Traefik TLS mode."
        )

    volume_binds[settings.traefik_acme_volume] = {
        "bind": _TRAEFIK_ACME_MOUNT_PATH,
        "mode": "ro",
    }
    return volume_binds


def _needs_traefik_acme_mount(settings: Settings, container: Any) -> bool:
    """Whether a Traefik TLS service is missing the implicit ACME mount."""
    if settings.routing_mode != "traefik" or not settings.routing_tls:
        return False

    for mount in container.attrs.get("Mounts", []):
        if (
            mount.get("Type") == "volume"
            and mount.get("Name") == settings.traefik_acme_volume
            and mount.get("Destination") == _TRAEFIK_ACME_MOUNT_PATH
            and not mount.get("RW", True)
        ):
            return False
    return True


def _image_command(container: Any) -> list[str]:
    """The CMD baked into the container's image, or [] when it has none."""
    try:
        cmd = container.image.attrs.get("Config", {}).get("Cmd")
    except Exception:
        return []
    return list(cmd) if isinstance(cmd, list) else []


def _command_override(container: Any) -> list[str]:
    """The container's CMD when it overrides the image default, else [].

    Docker copies the image CMD into ``Config.Cmd`` when the container does not
    set one, so an override is only visible as a difference between the two.
    """
    cmd = container.attrs.get("Config", {}).get("Cmd")
    command = list(cmd) if isinstance(cmd, list) else []
    return [] if command == _image_command(container) else command


def _assert_free_service_slot(
    settings: Settings, team: TeamSettings, client: Any
) -> None:
    """Reject a new service when the team has no free slot (0 = unlimited)."""
    if team.service_slots <= 0:
        return
    services = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
                "oduflow.service",
            ]
        },
    )
    if len(services) >= team.service_slots:
        raise FlowError(
            f"No free service slots (configured: {team.service_slots}). "
            "Delete an unused service to free a slot."
        )


def create_service(
    settings: Settings,
    team: TeamSettings,
    name: str,
    image: str,
    port: int | None,
    hostname: str | None = None,
    env_vars: dict[str, str] | None = None,
    host_mode: bool = False,
    volumes: list[dict[str, str]] | None = None,
    cap_add: list[str] | None = None,
    privileged: bool = False,
    routes: list[dict[str, object]] | None = None,
    command: list[str] | None = None,
    stack_labels: dict[str, str] | None = None,
    _resolved_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    container_name = get_service_container_name(name, settings.prefix, team.team_id)
    routes = normalize_http_routes(routes)
    _validate_service_exposure(settings, port, routes)
    # A dangling secret reference must abort before any Docker resource is
    # touched. Only the container environment receives the resolved values;
    # the preset and the label below keep the references. update_service
    # passes the mapping it already resolved (_resolved_env) so its
    # validate-then-recreate window cannot see a concurrently deleted secret
    # after the old container is gone.
    resolved_env = (
        _resolved_env
        if _resolved_env is not None
        else secret_store.resolve_env_secrets(team, env_vars)
    )
    secret_env = secret_store.secret_env_refs(env_vars)
    client = get_client()

    # Check for existing container
    is_new_service = False
    try:
        existing = client.containers.get(container_name)
        if existing.status == "running":
            raise ConflictError(f"Service '{name}' already exists and is running.")
        # Docker would refuse the name with a 409 that quotes the container
        # name and ID; say it in our own words instead.
        raise ConflictError(
            f"Service '{name}' already exists but is not running "
            f"(status: {existing.status}). Use restart_service to start it, "
            "update_service to recreate it with new settings, or delete_service "
            "first."
        )
    except docker.errors.NotFound:
        is_new_service = True

    if is_new_service:
        # Fail before pulling a possibly huge image. Repeated under the registry
        # lock further down, where it is the authoritative admission check.
        _assert_free_service_slot(settings, team, client)

    # Services join the team's isolated network (not needed for host mode).
    team_network = ""
    if not host_mode:
        from oduflow.docker_ops.system_ops import ensure_team_network

        team_network = ensure_team_network(client, settings, team)

    labels = {
        "oduflow.managed": "true",
        "oduflow.team": team.team_id,
        "oduflow.service": name,
        "oduflow.created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if secret_env:
        labels[_SECRET_ENV_LABEL] = json.dumps(secret_env, sort_keys=True)
    if stack_labels:
        labels.update(stack_labels)

    run_kwargs: dict[str, Any] = {
        "image": image,
        "name": container_name,
        "detach": True,
        "labels": labels,
        "restart_policy": {"Name": "unless-stopped"},
    }

    if host_mode:
        run_kwargs["network_mode"] = "host"
        labels["oduflow.host_mode"] = "true"
    else:
        run_kwargs["network"] = team_network

    if settings.routing_mode == "traefik":
        if not hostname:
            hostname = f"{name}.{team.hostname}"
        elif "." not in hostname:
            hostname = f"{hostname}.{team.hostname}"
        labels["traefik.enable"] = "true"
        if routes:
            labels[_HTTP_ROUTES_LABEL] = json.dumps(
                routes, separators=(",", ":"), sort_keys=True
            )
            for index, route in enumerate(routes, start=1):
                route_name = f"{container_name}-route-{index}"
                router_prefix = f"traefik.http.routers.{route_name}"
                service_prefix = f"traefik.http.services.{route_name}"
                labels[f"{router_prefix}.rule"] = _route_rule(
                    hostname, str(route["path"])
                )
                labels[f"{router_prefix}.service"] = route_name
                if settings.routing_tls:
                    labels[f"{router_prefix}.entrypoints"] = "websecure"
                    labels[f"{router_prefix}.tls.certresolver"] = "letsencrypt"
                else:
                    labels[f"{router_prefix}.entrypoints"] = "web"
                if host_mode:
                    labels[f"{service_prefix}.loadbalancer.server.url"] = (
                        f"http://host.docker.internal:{route['port']}"
                    )
                else:
                    labels[f"{service_prefix}.loadbalancer.server.port"] = str(
                        route["port"]
                    )
                if route["strip_prefix"]:
                    middleware_name = f"{route_name}-strip"
                    labels[f"{router_prefix}.middlewares"] = middleware_name
                    labels[
                        f"traefik.http.middlewares.{middleware_name}.stripprefix.prefixes"
                    ] = str(route["path"])
            if not host_mode:
                labels["traefik.docker.network"] = team_network
        else:
            labels[f"traefik.http.routers.{container_name}.rule"] = (
                f"Host(`{hostname}`)"
            )
            if settings.routing_tls:
                labels[f"traefik.http.routers.{container_name}.entrypoints"] = (
                    "websecure"
                )
                labels[f"traefik.http.routers.{container_name}.tls.certresolver"] = (
                    "letsencrypt"
                )
            else:
                # Upstream terminates TLS (e.g. Cloudflare tunnel): plain HTTP on
                # the web entrypoint. The public URL below keeps the upstream's
                # scheme (settings.public_scheme), not this entrypoint's.
                labels[f"traefik.http.routers.{container_name}.entrypoints"] = "web"
            if host_mode:
                labels[
                    f"traefik.http.services.{container_name}.loadbalancer.server.url"
                ] = f"http://host.docker.internal:{port}"
            else:
                labels[
                    f"traefik.http.services.{container_name}.loadbalancer.server.port"
                ] = str(port)
                labels["traefik.docker.network"] = team_network
        url = f"{settings.public_scheme}://{hostname}"
    else:
        if not host_mode:
            run_kwargs["ports"] = {f"{port}/tcp": port}
        url = f"{settings.public_scheme}://{team.hostname}:{port}"

    if resolved_env:
        run_kwargs["environment"] = resolved_env

    if command:
        run_kwargs["command"] = list(command)

    # Reject a missing or reserved volume before the image pull; the binds that
    # are actually used are resolved again under the registry lock below.
    _resolve_service_volume_binds(settings, team, volumes, client=client)

    if privileged:
        run_kwargs["privileged"] = True
    elif cap_add:
        run_kwargs["cap_add"] = list(cap_add)

    logger.info("Pulling image %s for service %s", image, name)
    _pull_service_image(client, image)

    # Registry section: slot admission and the volume binds are both read here
    # and consumed by the `run` below with nothing in between. Concurrent
    # creates therefore cannot overshoot the slot count, and delete_volume —
    # which takes the same key around its in-use check — cannot remove a volume
    # this container is about to mount (Docker would silently re-create it as an
    # empty, unmanaged volume). Held only around these calls; the image pull
    # above and the preset write below stay outside.
    with keyed_mutex(service_registry_key(team.team_id)):
        if is_new_service:
            _assert_free_service_slot(settings, team, client)
        vol_binds = _resolve_service_volume_binds(
            settings, team, volumes, client=client
        )
        if vol_binds:
            run_kwargs["volumes"] = vol_binds
        try:
            client.containers.run(**run_kwargs)
        except docker.errors.DockerException as exc:
            # The SDK creates the container before starting it, so a failed
            # start leaves a `created` container holding the name. Drop it,
            # otherwise the retry this error recommends hits a name conflict.
            _remove_stale_service_container(client, container_name)
            _raise_service_start_error(
                name, port, exc, retry_with="create_service again"
            )
    logger.info("Created service container %s from image %s", container_name, image)

    # Auto-save preset for future restore
    try:
        service_presets.save_preset(
            team,
            name,
            image,
            port,
            hostname=hostname,
            env_vars=env_vars,
            base_hostname=team.hostname,
            host_mode=host_mode,
            volumes=volumes,
            cap_add=cap_add,
            privileged=privileged,
            routes=routes,
            command=command,
        )
    except Exception:
        logger.warning("Failed to save service preset for %s", name, exc_info=True)

    return {
        "name": name,
        "container_name": container_name,
        "image": image,
        "url": url,
        "host_mode": host_mode,
        "command": list(command or []),
        "routes": _routes_with_urls(routes, hostname, settings.public_scheme),
    }


def _remove_stale_service_container(client: Any, container_name: str) -> None:
    """Best-effort removal of a service container that never started.

    Only an Oduflow-managed ``created`` container is touched: that is what the
    SDK leaves behind when ``start`` fails. A stopped (``exited``) container or
    one without our label is someone's service and is left alone, so a name
    clash surfaces instead of deleting it.
    """
    try:
        stale = client.containers.get(container_name)
        if stale.status != "created":
            return
        if (stale.labels or {}).get("oduflow.managed") != "true":
            return
        stale.remove(force=True)
    except docker.errors.NotFound:
        pass
    except docker.errors.DockerException:
        logger.warning(
            "Could not remove failed service container %s",
            container_name,
            exc_info=True,
        )


def restart_service(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, str]:
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    port: int | None = None
    try:
        preset = service_presets.get_preset(team, name)
        port = int(preset["port"]) if preset and preset.get("port") else None
    except Exception:
        pass
    try:
        container.restart()
    except docker.errors.DockerException as exc:
        _raise_service_start_error(
            name, port, exc, retry_with="update_service with a new port"
        )
    logger.info("Restarted service container %s", container_name)

    return {
        "name": name,
        "container_name": container_name,
    }


def delete_service(settings: Settings, team: TeamSettings, name: str) -> dict[str, str]:
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    container.stop()
    container.remove(v=True)
    logger.info("Deleted service container %s", container_name)

    return {
        "name": name,
        "container_name": container_name,
    }


def _traefik_label_by_suffix(labels: dict[str, str], suffix: str) -> str:
    """Value of the traefik label ending in ``suffix``, or "".

    Router/service names embed the container name, which changed with naming
    v2; containers created before the rename migration still carry labels
    keyed by the old name — so labels are matched by suffix, never by exact
    router name.
    """
    for key, value in labels.items():
        if key.startswith("traefik.http.") and key.endswith(suffix):
            return str(value)
    return ""


def _secret_env_refs(container: Any) -> dict[str, str]:
    """KEY -> "secret:<name>" recorded on the container at creation time."""
    raw = (container.labels or {}).get(_SECRET_ENV_LABEL, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_container_env(container: Any) -> dict[str, str]:
    """Config.Env of a container as a dict — the values it actually runs with."""
    env_vars: dict[str, str] = {}
    for entry in container.attrs.get("Config", {}).get("Env", []) or []:
        if "=" in entry:
            key, value = entry.split("=", 1)
            env_vars[key] = value
    return env_vars


def _container_env_vars(container: Any) -> dict[str, str]:
    """Env of a service container without the keys every image sets anyway.

    Keys configured as secret references come back as the reference, not the
    resolved value the container actually runs with: every read surface built
    on container inspection (get_service_info, list_services, the legacy
    no-preset update path) must never expose a secret value.
    """
    env_vars = {
        key: value
        for key, value in _raw_container_env(container).items()
        if key not in _SYSTEM_ENV_KEYS
    }
    for key, ref in _secret_env_refs(container).items():
        if key in env_vars:
            env_vars[key] = ref
    return env_vars


def _describe_service_container(
    settings: Settings, team: TeamSettings, container: Any
) -> dict[str, Any]:
    """Extract a normalized state dict for a managed service container.

    Used by both ``list_services`` and ``get_service_info``.
    """
    svc_name = container.labels.get("oduflow.service")
    container_name = container.name
    image = container.image.tags[0] if container.image.tags else "unknown"
    status = container.status

    env_vars = _container_env_vars(container)

    image_env_vars: dict[str, str] = {}
    try:
        raw_image_env = container.image.attrs.get("Config", {}).get("Env", [])
    except Exception:
        raw_image_env = []
    if isinstance(raw_image_env, list):
        for entry in raw_image_env:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                if key not in _SYSTEM_ENV_KEYS:
                    image_env_vars[key] = value

    port_num: int | None = None
    url: str | None = None
    hostname: str | None = None
    is_host_mode = container.labels.get("oduflow.host_mode") == "true"
    routes = _routes_from_labels(container.labels)

    if settings.routing_mode == "traefik":
        rule_value = _traefik_label_by_suffix(container.labels, ".rule")
        match = re.search(r"Host\(`([^`]+)`\)", rule_value)
        if match:
            hostname = match.group(1)
            url = f"{settings.public_scheme}://{hostname}"

        if routes:
            port_num = None
        elif is_host_mode:
            url_value = _traefik_label_by_suffix(
                container.labels, ".loadbalancer.server.url"
            )
            port_match = re.search(r":(\d+)$", url_value)
            if port_match:
                port_num = int(port_match.group(1))
        else:
            label_port = _traefik_label_by_suffix(
                container.labels, ".loadbalancer.server.port"
            )
            if label_port:
                port_num = int(label_port)
    else:
        if is_host_mode:
            # In host mode without traefik, port is not mapped via Docker.
            # Try to get it from the preset.
            try:
                preset = service_presets.get_preset(team, svc_name)
                port_num = preset.get("port")
            except Exception:
                pass
            if port_num:
                url = f"{settings.public_scheme}://{team.hostname}:{port_num}"
        else:
            ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            if ports_dict:
                for port_key, mappings in ports_dict.items():
                    port_match = re.match(r"(\d+)/", port_key)
                    if port_match:
                        port_num = int(port_match.group(1))
                    if mappings:
                        for mapping in mappings:
                            host_port = mapping.get("HostPort")
                            if host_port:
                                url = f"{settings.public_scheme}://{team.hostname}:{host_port}"
                                break
                    break  # only process first port entry

    svc_volumes: list[dict[str, str]] = []
    mounts = container.attrs.get("Mounts", [])
    for mount in mounts:
        if mount.get("Type") == "volume":
            vol_docker_name = mount.get("Name", "")
            prefix = f"oduflow-vol-{team.team_id}-"
            short_name = (
                vol_docker_name[len(prefix) :]
                if vol_docker_name.startswith(prefix)
                else vol_docker_name
            )
            svc_volumes.append(
                {
                    "volume": short_name,
                    "mount_path": mount.get("Destination", ""),
                    "mode": "ro" if not mount.get("RW", True) else "rw",
                }
            )

    host_config = container.attrs.get("HostConfig", {}) or {}
    svc_cap_add = list(host_config.get("CapAdd") or [])
    svc_privileged = bool(host_config.get("Privileged", False))
    svc_command = _command_override(container)
    svc_image_command = _image_command(container)

    return {
        "name": svc_name,
        "container_name": container_name,
        "image": image,
        "status": status,
        "port": port_num,
        "hostname": hostname,
        "url": url,
        "routes": _routes_with_urls(routes, hostname, settings.public_scheme),
        "env_vars": env_vars,
        "image_env_vars": image_env_vars,
        "host_mode": is_host_mode,
        "volumes": svc_volumes,
        "cap_add": svc_cap_add,
        "privileged": svc_privileged,
        "command": svc_command,
        "image_command": svc_image_command,
        "created_at": container.labels.get("oduflow.created_at", "")
        or container.attrs.get("Created", ""),
        "stack": container.labels.get("oduflow.stack", ""),
        "stack_resource": container.labels.get("oduflow.stack-resource", ""),
        "stack_spec_hash": container.labels.get("oduflow.stack-spec-hash", ""),
    }


def list_services(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    client = get_client()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
    )

    result = []
    for container in containers:
        if not container.labels.get("oduflow.service"):
            continue
        result.append(_describe_service_container(settings, team, container))

    return result


def get_service_info(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    """Return full state and configuration of a single managed service.

    Combines live container state (image, digest, ports, volumes, env, caps,
    runtime status) with preset presence flag. Raises :class:`NotFoundError`
    if the service container is missing.
    """
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    if not container.labels.get("oduflow.service"):
        raise NotFoundError(f"Service '{name}' not found")

    info = _describe_service_container(settings, team, container)

    info["image_digest"] = container.image.id

    state = container.attrs.get("State", {}) or {}
    info["started_at"] = state.get("StartedAt")
    info["restart_count"] = int(container.attrs.get("RestartCount", 0) or 0)

    has_preset = False
    try:
        service_presets.get_preset(team, name)
        has_preset = True
    except NotFoundError:
        pass
    except Exception:
        logger.warning("Failed to check preset for service %s", name, exc_info=True)
    info["has_preset"] = has_preset

    return info


def get_service_env_vars(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, str]:
    """Return the env vars ``update_service`` keeps when nothing overrides them.

    The saved preset is authoritative and is what an update reuses, so it is
    also what an edit dialog must prefill; the container's own environment
    additionally carries the image defaults, which are not part of the service
    configuration. Only legacy services created before presets existed fall
    back to inspecting the container — a preset that exists but cannot be read
    raises instead, because prefilling from the container in that case would
    offer the image defaults for editing and bake them into the preset on the
    first save.
    """
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    if not container.labels.get("oduflow.service"):
        raise NotFoundError(f"Service '{name}' not found")

    try:
        preset = service_presets.get_preset(team, name)
    except NotFoundError:
        return _container_env_vars(container)

    return dict(preset.get("env_vars") or {})


def update_service(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    env_override: dict[str, str] | None = None,
    image_override: str | None = None,
    port_override: int | None = None,
    hostname_override: str | None = None,
    host_mode_override: bool | None = None,
    volume_override: list[dict[str, str]] | None = None,
    cap_add_override: list[str] | None = None,
    privileged_override: bool | None = None,
    routes_override: list[dict[str, object]] | None = None,
    command_override: list[str] | None = None,
    stack_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Pull the latest image for a service and re-create it with the same settings.

    Optional overrides replace the corresponding setting from the saved preset.
    When any override differs from the current config the container is recreated
    even if the image digest has not changed. A rotated team secret also counts
    as a config change: the resolved value of every ``secret:<name>`` reference
    is compared against what the container actually runs with, so the
    documented rotation flow (replace the value, then update) takes effect.
    """
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    # Capture current image name
    old_image = container.image.tags[0] if container.image.tags else None
    if not old_image:
        # Fall back to Config.Image (may be a digest reference)
        old_image = container.attrs.get("Config", {}).get("Image")
    if not old_image:
        raise NotFoundError(
            f"Cannot determine image for service '{name}'. "
            "The container has no image tag or Config.Image."
        )

    # Read service options from saved preset (authoritative source).
    # Fall back to container inspection for legacy services without a preset.
    preset = None
    try:
        preset = service_presets.get_preset(team, name)
    except Exception:
        pass

    # Declared once so the preset and legacy-fallback branches agree on a type.
    port: int | None
    hostname: str | None
    env_vars: dict[str, str] | None
    if preset:
        port = preset.get("port") or None
        hostname = preset.get("hostname") or None
        env_vars = preset.get("env_vars") or None
        is_host_mode = preset.get("host_mode", False)
        old_volumes = preset.get("volumes") or None
        cap_add = preset.get("cap_add") or None
        privileged = preset.get("privileged", False)
        routes = normalize_http_routes(preset.get("routes"))
        command = list(preset.get("command") or [])
    else:
        # Legacy fallback: extract from running container
        env_vars = _container_env_vars(container)

        is_host_mode = container.labels.get("oduflow.host_mode") == "true"
        routes = _routes_from_labels(container.labels)

        host_config = container.attrs.get("HostConfig", {}) or {}
        cap_add = list(host_config.get("CapAdd") or []) or None
        privileged = bool(host_config.get("Privileged", False))
        command = _command_override(container)

        port = None
        hostname = None

        if settings.routing_mode == "traefik":
            rule_value = _traefik_label_by_suffix(container.labels, ".rule")
            match = re.search(r"Host\(`([^`]+)`\)", rule_value)
            if match:
                hostname = match.group(1)

            if routes:
                port = None
            elif is_host_mode:
                url_value = _traefik_label_by_suffix(
                    container.labels, ".loadbalancer.server.url"
                )
                port_match = re.search(r":(\d+)$", url_value)
                if port_match:
                    port = int(port_match.group(1))
            else:
                label_port = _traefik_label_by_suffix(
                    container.labels, ".loadbalancer.server.port"
                )
                if label_port:
                    port = int(label_port)
        else:
            if is_host_mode:
                pass  # Cannot determine port without preset in host mode
            else:
                ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports_dict:
                    for port_key in ports_dict:
                        port_match = re.match(r"(\d+)/", port_key)
                        if port_match:
                            port = int(port_match.group(1))
                            break

        old_volumes = None
        mounts_data = container.attrs.get("Mounts", [])
        vols: list[dict[str, str]] = []
        for mount in mounts_data:
            if mount.get("Type") == "volume":
                vol_docker_name = mount.get("Name", "")
                prefix = f"oduflow-vol-{team.team_id}-"
                if vol_docker_name.startswith(prefix):
                    short_name = vol_docker_name[len(prefix) :]
                    vols.append(
                        {
                            "volume": short_name,
                            "mount_path": mount.get("Destination", ""),
                            "mode": "ro" if not mount.get("RW", True) else "rw",
                        }
                    )
        if vols:
            old_volumes = vols

        env_vars = env_vars or None

    if (
        port is None
        and not routes
        and port_override is None
        and routes_override is None
    ):
        raise NotFoundError(f"Cannot determine port for service '{name}'.")

    # Apply overrides and track whether config changed
    # Services created before the implicit ACME mount was introduced are
    # brought forward by an ordinary update, even when the image digest and
    # user-controlled settings are otherwise unchanged.
    config_changed = _needs_traefik_acme_mount(settings, container)
    persisted_stack_labels = {
        key: value
        for key, value in container.labels.items()
        if key.startswith("oduflow.stack")
    }
    effective_stack_labels = (
        dict(stack_labels) if stack_labels is not None else persisted_stack_labels
    )
    if stack_labels is not None and any(
        container.labels.get(key) != value for key, value in stack_labels.items()
    ):
        config_changed = True
    if env_override is not None and env_override != (env_vars or {}):
        env_vars = env_override or None
        config_changed = True
    if port_override is not None and port_override != port:
        port = port_override
        config_changed = True
    if hostname_override is not None and hostname_override != hostname:
        hostname = hostname_override or None
        config_changed = True
    if host_mode_override is not None and host_mode_override != is_host_mode:
        is_host_mode = host_mode_override
        config_changed = True
    if volume_override is not None and volume_override != (old_volumes or []):
        old_volumes = volume_override or None
        config_changed = True
    if cap_add_override is not None and cap_add_override != (cap_add or []):
        cap_add = cap_add_override or None
        config_changed = True
    if privileged_override is not None and privileged_override != privileged:
        privileged = privileged_override
        config_changed = True
    if command_override is not None and list(command_override) != command:
        command = list(command_override)
        config_changed = True
    if routes_override is not None:
        normalized_override = normalize_http_routes(routes_override)
        if normalized_override:
            if port_override is not None:
                raise ValueError("Pass either port or routes, not both.")
            port = None
            new_routes = normalized_override
        else:
            new_routes = None
            if port_override is None:
                raise ValueError(
                    "Removing routes requires a replacement port in the same update."
                )
        if new_routes != routes:
            routes = new_routes
            config_changed = True

    if routes and port_override is not None and routes_override is None:
        raise ValueError(
            "A route-based service has no catch-all port; replace routes or clear "
            "them with routes=[] and a replacement port."
        )

    _validate_service_exposure(settings, port, routes)

    # Determine the image to pull (override or current)
    target_image = image_override if image_override else old_image
    if image_override and image_override != old_image:
        config_changed = True

    # Validate the complete candidate volume configuration before any
    # destructive action. In particular, a missing/reserved volume override
    # must not stop and remove the currently running service.
    _resolve_service_volume_binds(settings, team, old_volumes or None, client=client)

    # Resolve secret references once, up front: a dangling reference must fail
    # before the (possibly large) image pull and before the destructive
    # stop/remove below, and the same resolved mapping is handed to
    # create_service so a secret deleted concurrently with this update cannot
    # fail the re-create after the old container is already gone.
    resolved_env = secret_store.resolve_env_secrets(team, env_vars)

    # A rotated secret value changes neither the reference-form config nor the
    # image digest, so the documented rotation flow (replace the value in the
    # dashboard, then update_service) hinges on this check: compare the values
    # the container actually runs with against the freshly resolved ones for
    # every reference-configured key.
    if not config_changed:
        live_env = _raw_container_env(container)
        for key in secret_store.secret_env_refs(env_vars):
            if live_env.get(key) != (resolved_env or {}).get(key):
                config_changed = True
                break

    # Capture old image digest
    old_digest = container.image.id  # e.g. sha256:abc...

    # Pull the latest image
    logger.info("Pulling latest image %s for service %s", target_image, name)
    new_image_obj = _pull_service_image(client, target_image)
    new_digest = new_image_obj.id
    image_updated = old_digest != new_digest

    needs_recreate = image_updated or config_changed

    if not needs_recreate:
        logger.info("No changes for service %s: %s", name, new_digest[:19])
        # Compute URL for return
        if settings.routing_mode == "traefik":
            h = hostname or f"{name}.{team.hostname}"
            if "." not in h:
                h = f"{h}.{team.hostname}"
            url = f"{settings.public_scheme}://{h}"
        else:
            url = f"{settings.public_scheme}://{team.hostname}:{port}"
        return {
            "name": name,
            "container_name": container_name,
            "image": target_image,
            "url": url,
            "host_mode": is_host_mode,
            "command": list(command),
            "image_updated": False,
            "config_updated": False,
            "old_digest": old_digest,
            "new_digest": new_digest,
            "routes": _routes_with_urls(
                routes,
                h if settings.routing_mode == "traefik" else None,
                settings.public_scheme,
            ),
        }

    logger.info(
        "Recreating service %s (image_updated=%s, config_changed=%s)",
        name,
        image_updated,
        config_changed,
    )

    # The service is invisible to the registry between remove and re-create, so
    # both happen under the registry key: a delete_volume that looked in that
    # window would find the volume unused and remove it out from under the
    # recreated container. The mutex is re-entrant — create_service takes the
    # same key for its own admission check. Its (already-pulled, cache-warm)
    # image pull is inside this window; splitting the window to exclude it would
    # reopen the very race it exists to close.
    with keyed_mutex(service_registry_key(team.team_id)):
        # Stop and remove the old container
        container.stop()
        container.remove(v=True)
        logger.info("Removed old container %s for update", container_name)

        # Re-create with the (possibly overridden) settings
        result = create_service(
            settings,
            team,
            name=name,
            image=target_image,
            port=port,
            hostname=hostname,
            env_vars=env_vars or None,
            host_mode=is_host_mode,
            volumes=old_volumes or None,
            cap_add=cap_add,
            privileged=privileged,
            routes=routes,
            command=command or None,
            stack_labels=effective_stack_labels,
            _resolved_env=resolved_env,
        )

    result["image_updated"] = image_updated
    result["config_updated"] = config_changed
    result["old_digest"] = old_digest
    result["new_digest"] = new_digest

    logger.info("Service %s updated with image %s", name, target_image)
    return result


def get_service_logs(
    settings: Settings, team: TeamSettings, name: str, lines: int = 100
) -> str:
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    output = container.logs(tail=lines, timestamps=True)
    text: str = output.decode("utf-8")
    return text


def run_command_in_service(
    settings: Settings,
    team: TeamSettings,
    name: str,
    command: str,
    user: str = "root",
    shell: bool = True,
) -> dict[str, object]:
    """Run *command* inside a managed service container.

    Same shell semantics as :func:`odoo_ops.run_command_in_environment`: by
    default the command runs through ``sh -c`` so pipes and redirections work;
    ``shell=False`` execs the argv directly. Service images are minimal but all
    ship a POSIX ``sh``; on one that genuinely has none (scratch/distroless),
    use ``shell=False``.
    """
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    logger.info(
        "Executing command in service",
        extra={"service": name, "command": command, "user": user, "shell": shell},
    )
    exit_code, output = container.exec_run(
        ["sh", "-c", command] if shell else command, user=user
    )
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }
