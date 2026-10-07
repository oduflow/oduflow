from __future__ import annotations

import datetime
import json
import logging
import os
import posixpath
import re
from typing import Any

import docker
from oduflow import secret_store
from oduflow.docker_ops import service_presets, volume_ops
from oduflow.docker_ops.build_ops import STAGING_REPOSITORY_PREFIX, staging_repository
from oduflow.docker_ops.client import (
    docker_error_detail,
    docker_operation_error,
    get_client,
)
from oduflow.domains import (
    assert_public_hostname_free,
    service_hostname,
    service_parent_domain,
)
from oduflow.errors import (
    ConflictError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.fsutil import atomic_write_private_text
from oduflow.locking import keyed_mutex, service_registry_key
from oduflow.naming import get_service_container_name, validate_domain
from oduflow.service_runtime import (
    RUNTIME_LABEL,
    inspect_runtime,
    normalize_runtime,
    stop_kwargs,
)
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


def _pull_service_image(client: Any, image: str, team: TeamSettings) -> Any:
    """Resolve a service image and expose only safe, actionable failures.

    Registry images are pulled. A staging image produced by
    ``start_image_build`` (``oduflow-build/team-<id>:<build-id>``) exists only
    in the local daemon, so it is looked up there instead — but only the
    calling team's own staging repository: the local daemon is shared, and the
    prefix check is what keeps one team from running another team's build.
    """
    if image.startswith(STAGING_REPOSITORY_PREFIX):
        repository = image.rsplit(":", 1)[0] if ":" in image else image
        if repository != staging_repository(team.team_id):
            raise NotFoundError(
                f"Docker image '{image}' was not found or is not accessible. "
                "Staging builds can only be used by the team that built them; "
                "pass a build's local tag as reported by get_image_build."
            )
        try:
            return client.images.get(image)
        except docker.errors.ImageNotFound as exc:
            raise NotFoundError(
                f"Staging image '{image}' is not in the local Docker daemon: the "
                "build may have failed, been cancelled, or been pruned. Check "
                "get_image_build and run start_image_build again if needed."
            ) from exc
        except docker.errors.DockerException as exc:
            raise PrerequisiteNotMetError(
                f"Could not inspect staging image '{image}'. Check Docker connectivity."
            ) from exc
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
    service configuration. Every service created while the ACME resolver is
    declared (acme_enabled) sees the exact store at ``/etc/traefik``
    read-only. The store may not contain ``acme.json`` yet — it appears only
    after the first certificate issuance — so services must tolerate its
    absence.
    """
    volume_binds: dict[str, dict[str, str]] = volume_ops.resolve_volume_binds(
        team, volumes or []
    )

    if not settings.acme_enabled:
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
    if not settings.acme_enabled:
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
    runtime: dict[str, Any] | None = None,
    stack_labels: dict[str, str] | None = None,
    _resolved_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    runtime = normalize_runtime(runtime)
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
    if runtime:
        labels[RUNTIME_LABEL] = json.dumps(runtime, sort_keys=True)
    if stack_labels:
        labels.update(stack_labels)

    run_kwargs: dict[str, Any] = {
        "image": image,
        "name": container_name,
        "detach": True,
        "labels": labels,
        "restart_policy": {"Name": "unless-stopped"},
    }

    # stop_timeout is not a containers.run() argument in docker-py; it is
    # honored at stop/restart/replacement time via stop_kwargs().
    run_kwargs.update({k: v for k, v in runtime.items() if k != "stop_timeout"})

    if host_mode:
        run_kwargs["network_mode"] = "host"
        labels["oduflow.host_mode"] = "true"
    else:
        run_kwargs["network"] = team_network

    if settings.routing_mode == "traefik":
        # Short names attach to the team zone (base_domain) when configured,
        # else legacy-nest under the team hostname; a dotted value is a full
        # FQDN used as-is. Either way the final name must be free in the
        # global Host() namespace.
        hostname = service_hostname(team, name, hostname)
        # The hostname lands verbatim in a Traefik `Host(...)` router rule.
        # Without this a tenant value like `foo`) || Host(`victim.example.com`
        # would inject a second rule and hijack another team's hostname. Reject
        # anything that is not a plain FQDN (same policy as production domains).
        # update_service recreates via create_service, so this also guards
        # hostname_override.
        hostname = validate_domain(hostname)
        assert_public_hostname_free(
            settings,
            hostname,
            own_team=team.team_id,
            # With `routes` no catch-all router is created, only
            # Host() && PathPrefix() ones, so a path-routed service may share
            # the team dashboard host — that is the documented way to publish
            # a URL prefix next to the dashboard.
            allow_own_team_host=bool(routes),
            exclude_service=name,
            purpose=f"the hostname of service '{name}'",
        )
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
                    labels[f"{router_prefix}.tls"] = "true"
                    if settings.uses_acme:
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
                labels[f"traefik.http.routers.{container_name}.tls"] = "true"
                if settings.uses_acme:
                    labels[
                        f"traefik.http.routers.{container_name}.tls.certresolver"
                    ] = "letsencrypt"
            else:
                # Upstream terminates TLS (e.g. Cloudflare tunnel): plain HTTP on
                # the web entrypoint. The public URL below keeps the upstream's
                # scheme (settings.public_scheme_for(team)), not this entrypoint's.
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
        url = f"{settings.public_scheme_for(team)}://{hostname}"
    else:
        if not host_mode:
            run_kwargs["ports"] = {f"{port}/tcp": port}
        url = f"{settings.public_scheme_for(team)}://{team.hostname}:{port}"

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
    is_staging = image.startswith(STAGING_REPOSITORY_PREFIX)
    if not is_staging:
        _pull_service_image(client, image, team)

    # Registry section: slot admission and the volume binds are both read here
    # and consumed by the `run` below with nothing in between. Concurrent
    # creates therefore cannot overshoot the slot count, and delete_volume —
    # which takes the same key around its in-use check — cannot remove a volume
    # this container is about to mount (Docker would silently re-create it as an
    # empty, unmanaged volume). Staging resolution also stays in this section:
    # build retention takes the same key before inspecting/removing tags.
    # Registry pulls and the preset write stay outside.
    with keyed_mutex(service_registry_key(team.team_id)):
        if is_staging:
            _pull_service_image(client, image, team)
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
            base_hostname=service_parent_domain(team),
            host_mode=host_mode,
            volumes=volumes,
            cap_add=cap_add,
            privileged=privileged,
            routes=routes,
            command=command,
            runtime=runtime,
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
        "routes": _routes_with_urls(routes, hostname, settings.public_scheme_for(team)),
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


def _protection_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "protected_services.json")


def _load_protected_services(team: TeamSettings) -> set[str]:
    path = _protection_path(team)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("Corrupt protected-services file at %s", path)
        return set()
    if not isinstance(data, list):
        return set()
    return {entry for entry in data if isinstance(entry, str)}


def is_service_protected(team: TeamSettings, name: str) -> bool:
    return name in _load_protected_services(team)


def assert_service_not_protected(team: TeamSettings, name: str, action: str) -> None:
    if is_service_protected(team, name):
        raise ProtectedError(
            f"Service '{name}' is protected. Unprotect it in the Oduflow "
            f"dashboard before {action}."
        )


def set_service_protected(
    settings: Settings, team: TeamSettings, name: str, protected: bool
) -> dict[str, Any]:
    """Toggle protection for a service. Protecting requires a live container.

    The flag lives outside the container (a per-team JSON registry) so it
    survives Docker-level drift; it blocks delete, update and restore.
    """
    if protected:
        # Only existing services can be protected; unprotect always works so
        # a stale entry can be cleared even after the container is gone.
        client = get_client()
        container_name = get_service_container_name(name, settings.prefix, team.team_id)
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            raise NotFoundError(f"Service '{name}' not found")
        if not container.labels.get("oduflow.service"):
            raise NotFoundError(f"Service '{name}' not found")
    with keyed_mutex(service_registry_key(team.team_id)):
        names = _load_protected_services(team)
        if protected:
            names.add(name)
        else:
            names.discard(name)
        os.makedirs(team.data_dir, exist_ok=True)
        atomic_write_private_text(
            _protection_path(team), json.dumps(sorted(names), indent=2) + "\n"
        )
    logger.info(
        "Service '%s' for team '%s' %s",
        name,
        team.team_id,
        "protected" if protected else "unprotected",
    )
    return {"name": name, "protected": protected}


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
        container.restart(**stop_kwargs(container))
    except docker.errors.DockerException as exc:
        _raise_service_start_error(
            name, port, exc, retry_with="update_service with a new port"
        )
    logger.info("Restarted service container %s", container_name)

    return {
        "name": name,
        "container_name": container_name,
    }


def backfill_service_preset(
    settings: Settings, team: TeamSettings, name: str, container: Any
) -> bool:
    """Write a preset for *name* if it has none; True when one exists after.

    Every service created or updated through Oduflow already carries a preset
    that is kept current; this covers the legacy ones created before presets
    existed. Run from migration 0008 at startup, so every reader — restore,
    the edit dialog, delete — sees the preset store as the single source of
    truth. A service whose image or port cannot be reconstructed from the
    container is skipped with a warning: a preset ``restore_service`` would
    reject (image "" or port 0) is worse than none at all.
    """
    try:
        service_presets.get_preset(team, name)
        return True
    except NotFoundError:
        pass
    except Exception:
        logger.warning("Failed to read service preset for %s", name, exc_info=True)
        return False

    try:
        config = _effective_service_config(settings, team, name, container)
        image = _container_image_reference(container)
        if not image:
            logger.warning(
                "Skipping preset backfill for %s: cannot determine its image", name
            )
            return False
        if config["port"] is None and not config["routes"]:
            logger.warning(
                "Skipping preset backfill for %s: cannot determine its port", name
            )
            return False
        service_presets.save_preset(
            team,
            name,
            image,
            config["port"],
            hostname=config["hostname"],
            env_vars=config["env_vars"],
            base_hostname=service_parent_domain(team),
            host_mode=config["host_mode"],
            volumes=config["volumes"],
            cap_add=config["cap_add"],
            privileged=config["privileged"],
            routes=config["routes"],
            command=config["command"],
            runtime=inspect_runtime(container),
        )
        return True
    except Exception:
        logger.warning("Failed to save service preset for %s", name, exc_info=True)
        return False


def delete_service(
    settings: Settings, team: TeamSettings, name: str, save_preset: bool = True
) -> dict[str, Any]:
    """Remove a service container, keeping or dropping its saved preset.

    With ``save_preset`` the configuration stays behind for ``restore_service``
    (the default, and what deleting a service has always done); without it the
    preset goes away with the container, so the service leaves nothing behind.
    ``preset_kept`` in the result reports what is actually on disk afterwards,
    not the intent: a legacy service migration 0008 could not backfill has no
    preset to keep, and a failed preset delete leaves one behind.
    """
    assert_service_not_protected(team, name, "deleting")
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    container.stop(**stop_kwargs(container))
    container.remove(v=True)
    logger.info("Deleted service container %s", container_name)

    if save_preset:
        try:
            service_presets.get_preset(team, name)
            preset_kept = True
        except Exception:
            # No preset (a legacy service migration 0008 could not backfill),
            # or the store is unreadable: either way restore cannot be promised.
            preset_kept = False
    else:
        try:
            service_presets.delete_preset(team, name)
            preset_kept = False
        except NotFoundError:
            preset_kept = False
        except Exception:
            # The container is already gone; a stale preset is worth a warning,
            # not an error that makes the delete itself look failed.
            logger.warning(
                "Failed to delete service preset for %s", name, exc_info=True
            )
            preset_kept = True

    return {
        "name": name,
        "container_name": container_name,
        "preset_kept": preset_kept,
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


def _image_env_vars(container: Any) -> dict[str, str]:
    """Env baked into the container's image, minus the keys every image sets.

    Empty when the image record is no longer available (e.g. after a prune).
    """
    try:
        raw_image_env = container.image.attrs.get("Config", {}).get("Env", [])
    except Exception:
        raw_image_env = []
    image_env: dict[str, str] = {}
    if isinstance(raw_image_env, list):
        for entry in raw_image_env:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                if key not in _SYSTEM_ENV_KEYS:
                    image_env[key] = value
    return image_env


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


def _container_image_reference(container: Any) -> str | None:
    """Keep the image reference used at creation, including immutable digests."""
    reference = container.attrs.get("Config", {}).get("Image")
    if isinstance(reference, str) and reference:
        return reference
    try:
        return container.image.tags[0] if container.image.tags else None
    except Exception:
        # The image record can be gone (docker rmi); the caller decides what
        # an unknown image means instead of a raw SDK error surfacing.
        return None


def _describe_service_container(
    settings: Settings, team: TeamSettings, container: Any
) -> dict[str, Any]:
    """Extract a normalized state dict for a managed service container.

    Used by both ``list_services`` and ``get_service_info``.
    """
    svc_name = container.labels.get("oduflow.service")
    container_name = container.name
    image = _container_image_reference(container) or "unknown"
    status = container.status

    env_vars = _container_env_vars(container)
    image_env_vars = _image_env_vars(container)

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
            url = f"{settings.public_scheme_for(team)}://{hostname}"

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
                url = f"{settings.public_scheme_for(team)}://{team.hostname}:{port_num}"
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
                                url = f"{settings.public_scheme_for(team)}://{team.hostname}:{host_port}"
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
        "routes": _routes_with_urls(routes, hostname, settings.public_scheme_for(team)),
        "env_vars": env_vars,
        "image_env_vars": image_env_vars,
        "host_mode": is_host_mode,
        "volumes": svc_volumes,
        "cap_add": svc_cap_add,
        "privileged": svc_privileged,
        "runtime": inspect_runtime(container),
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

    protected_names = _load_protected_services(team)
    result = []
    for container in containers:
        if not container.labels.get("oduflow.service"):
            continue
        info = _describe_service_container(settings, team, container)
        info["protected"] = info["name"] in protected_names
        result.append(info)

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

    info["protected"] = is_service_protected(team, name)
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


def _effective_service_config(
    settings: Settings, team: TeamSettings, name: str, container: Any
) -> dict[str, Any]:
    """The configuration ``update_service`` keeps when nothing overrides it.

    The saved preset is authoritative; only legacy services created before
    presets existed fall back to inspecting the container. ``update_service``
    and the dashboard's edit dialog read through this one function, so the form
    can never offer a different "current" value than the update would keep.

    A preset that exists but cannot be read raises instead of falling back:
    the container inspection is a reconstruction, and silently substituting it
    would let the next update or dialog submit overwrite the real preset with
    guessed values.
    """
    # Read service options from saved preset (authoritative source).
    # Fall back to container inspection for legacy services without a preset.
    preset = None
    try:
        preset = service_presets.get_preset(team, name)
    except NotFoundError:
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
        # Legacy fallback: extract from running container. Values the image
        # itself sets are not service configuration — freezing them into a
        # preset would override a newer image's own defaults later — so only
        # the vars that differ from the image's are kept.
        image_env = _image_env_vars(container)
        env_vars = {
            key: value
            for key, value in _container_env_vars(container).items()
            if image_env.get(key) != value
        }

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

    return {
        "port": port,
        "hostname": hostname,
        "env_vars": env_vars,
        "host_mode": is_host_mode,
        "volumes": old_volumes,
        "cap_add": cap_add,
        "privileged": privileged,
        "routes": routes,
        "command": command,
    }


def get_service_config(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    """Return the current settings of a service in ``update_service`` terms.

    This is what an edit dialog must prefill: every value is the one an update
    would preserve, and env values configured as team secrets come back as
    their ``secret:<name>`` reference, never as the resolved value.
    """
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    if not container.labels.get("oduflow.service"):
        raise NotFoundError(f"Service '{name}' not found")

    config = _effective_service_config(settings, team, name, container)
    config["name"] = name
    config["image"] = _container_image_reference(container) or ""
    return config


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
    runtime_override: dict[str, Any] | None = None,
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
    # An update recreates the container, so protection covers it like delete.
    assert_service_not_protected(team, name, "updating")
    client = get_client()
    container_name = get_service_container_name(name, settings.prefix, team.team_id)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    # Capture current image name
    old_image = _container_image_reference(container)
    if not old_image:
        raise NotFoundError(
            f"Cannot determine image for service '{name}'. "
            "The container has no image tag or Config.Image."
        )

    runtime = inspect_runtime(container)
    candidate_runtime = (
        normalize_runtime(runtime_override) if runtime_override is not None else runtime
    )

    current = _effective_service_config(settings, team, name, container)
    port: int | None = current["port"]
    hostname: str | None = current["hostname"]
    env_vars: dict[str, str] | None = current["env_vars"]
    is_host_mode: bool = current["host_mode"]
    old_volumes: list[dict[str, str]] | None = current["volumes"]
    cap_add: list[str] | None = current["cap_add"]
    privileged: bool = current["privileged"]
    routes: list[dict[str, object]] | None = current["routes"]
    command: list[str] = current["command"]

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
    config_changed = (
        _needs_traefik_acme_mount(settings, container) or candidate_runtime != runtime
    )
    runtime = candidate_runtime
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

    # Validate the candidate hostname before any destructive action.
    # create_service re-validates, but it only runs after the old container is
    # stopped and removed below — a rejected hostname there would leave the
    # service deleted instead of unchanged. Same resolution as create_service.
    if settings.routing_mode == "traefik":
        validate_domain(service_hostname(team, name, hostname))

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
    new_image_obj = _pull_service_image(client, target_image, team)
    new_digest = new_image_obj.id
    image_updated = old_digest != new_digest

    needs_recreate = image_updated or config_changed

    if not needs_recreate:
        logger.info("No changes for service %s: %s", name, new_digest[:19])
        # Compute URL for return
        if settings.routing_mode == "traefik":
            h = service_hostname(team, name, hostname)
            url = f"{settings.public_scheme_for(team)}://{h}"
        else:
            url = f"{settings.public_scheme_for(team)}://{team.hostname}:{port}"
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
                settings.public_scheme_for(team),
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
    # same key for its own admission check. Build retention also takes this
    # key, so it cannot remove a staging tag in the container-free window.
    with keyed_mutex(service_registry_key(team.team_id)):
        if target_image.startswith(STAGING_REPOSITORY_PREFIX):
            # Retention may have run since the initial lookup. Fail before
            # removing the old container if the target is already gone, then
            # hold off pruning until create_service has installed its user.
            _pull_service_image(client, target_image, team)
        # Stop and remove the old container
        container.stop(**stop_kwargs(container))
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
            runtime=runtime,
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
