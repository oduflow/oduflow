"""System health checks: dev PG, prod PG, Traefik, S3, disk, overlays, productions,
and (in Docker-out-of-Docker) whether the daemon can see data_dir and etc_dir.

Backs the public ``GET /healthz`` endpoint (uptime-monitor friendly:
200 all-ok / 503 degraded) and the dashboard's status-bar chips. Results
are cached briefly so a polling dashboard doesn't hammer Docker/S3.

Check states: ``ok`` | ``warn`` | ``error`` | ``off`` (not applicable /
not configured). Only ``error`` on a critical check degrades /healthz.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import threading
import time
from typing import Any

from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_CACHE_TTL_SECONDS = 15
DISK_WARN_PERCENT = 85
# Container ID as it appears in this process' own /proc (see _own_container_id).
_MOUNTINFO_ID_RE = re.compile(r"/containers/([0-9a-f]{64})")
_CGROUP_ID_RE = re.compile(r"\b([0-9a-f]{64})\b")

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "result": None}


def _check_pg(client: Any, settings: Settings, container_name: str) -> dict[str, Any]:
    import docker

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        return {"status": "off", "detail": "not provisioned"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if container.status != "running":
        return {"status": "error", "detail": f"container {container.status}"}
    try:
        exit_code, _ = container.exec_run(["pg_isready", "-U", settings.db_user])
        if exit_code == 0:
            return {"status": "ok", "detail": ""}
        return {"status": "error", "detail": "pg_isready failed"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


def _check_traefik(client: Any, settings: Settings) -> dict[str, Any]:
    import docker

    if settings.routing_mode != "traefik":
        return {"status": "off", "detail": "port routing mode"}
    try:
        container = client.containers.get(settings.traefik_container)
    except docker.errors.NotFound:
        return {"status": "error", "detail": "container missing"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if container.status != "running":
        return {"status": "error", "detail": f"container {container.status}"}
    return {"status": "ok", "detail": ""}


def _check_s3(settings: Settings) -> dict[str, Any]:
    if not settings.prod_enabled:
        return {"status": "off", "detail": "production hosting disabled"}
    if settings.backup is None:
        return {"status": "off", "detail": "backups not configured"}
    from oduflow.s3_client import check_s3

    probe = check_s3(settings.backup)
    if probe["ok"]:
        return {"status": "ok", "detail": ""}
    return {"status": "error", "detail": probe["error"][:200]}


def _check_disk(settings: Settings) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(settings.base_data_dir or "/")
    except OSError as exc:
        return {"status": "error", "detail": str(exc)[:200], "percent": 0}
    percent = round(usage.used / usage.total * 100)
    status = "warn" if percent >= DISK_WARN_PERCENT else "ok"
    return {
        "status": status,
        "detail": f"{percent}% used",
        "percent": percent,
        "free_gb": round(usage.free / 1024**3, 1),
    }


def _check_walg(
    client: Any, settings: Settings, pg_status: dict[str, Any]
) -> dict[str, Any]:
    if not settings.prod_enabled or settings.backup is None:
        return {"status": "off", "detail": "production backups not configured"}
    if pg_status.get("status") == "off":
        return {"status": "off", "detail": "production PostgreSQL not provisioned"}
    if pg_status.get("status") != "ok":
        return {"status": "error", "detail": "production PostgreSQL unavailable"}
    from oduflow import wal_monitor

    result = wal_monitor.status(settings)
    return {"status": result["status"], "detail": result["detail"]}


def _check_overlays(settings: Settings) -> dict[str, Any]:
    """Filestore overlays that are stale, missing, or cannot be inspected.

    A stale overlay is invisible from the outside — the environment's container
    is up and the dashboard shows it green — while every filestore read inside
    it fails with ENOTCONN. Surfacing it here is what turns that into something
    an operator sees before a user does.
    """
    from oduflow.docker_ops.env_ops import overlay_mount_issues

    try:
        issues = overlay_mount_issues(settings)
    except Exception:  # noqa: BLE001 - health must answer, but never false-green
        logger.exception("Could not inspect filestore overlays")
        return {
            "status": "error",
            "detail": "filestore overlay scan failed; check server logs",
            "count": 0,
            "affected": [],
        }
    if not issues:
        return {"status": "ok", "detail": "", "count": 0, "affected": []}

    # /healthz is public. Report bounded logical identifiers here and keep host
    # filesystem paths in authenticated operator logs only.
    public = [
        {
            "team_id": issue["team_id"],
            "env_name": issue["env_name"],
            "state": issue["state"],
        }
        for issue in issues[:20]
    ]
    identifiers = [f"{item['team_id']}/{item['env_name']}" for item in public]
    suffix = (
        f" (+{len(issues) - len(public)} more)" if len(issues) > len(public) else ""
    )
    return {
        "status": "error",
        "detail": "filestore overlay needs attention: "
        + ", ".join(identifiers)
        + suffix,
        "count": len(issues),
        "affected": public,
    }


def _own_container_id() -> str:
    """This container's full ID read from ``/proc``, or "" when not found.

    The hostname is only the container ID when nothing overrode it, and
    Compose always overrides it with the *service* name — which is not the
    container's name either (``<project>-<service>-1``). So the ID is taken
    from the runtime's own bookkeeping: the daemon bind-mounts
    ``/var/lib/docker/containers/<id>/{hostname,hosts,resolv.conf}`` into every
    container, which shows up in ``/proc/self/mountinfo``; cgroup v1 hosts name
    the ID in ``/proc/self/cgroup``.
    """
    for path, pattern in (
        ("/proc/self/mountinfo", _MOUNTINFO_ID_RE),
        ("/proc/self/cgroup", _CGROUP_ID_RE),
    ):
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _own_container(client: Any) -> Any | None:
    """The container this process runs in, or None when it cannot be identified."""
    import docker

    seen: set[str] = set()
    for name in (
        _own_container_id(),
        os.environ.get("HOSTNAME", ""),
        socket.gethostname(),
    ):
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            return client.containers.get(name)
        except docker.errors.NotFound:
            continue
    return None


# (label, setting key) for the two directories whose children Oduflow hands
# to the daemon as bind-mount sources.
_DAEMON_VISIBLE_DIRS: tuple[tuple[str, str], ...] = (
    ("data_dir", "base_data_dir"),
    ("etc_dir", "etc_dir"),
)


def _dir_mount_verdict(
    label: str, path: str, mounts: list[Any]
) -> tuple[str, str, str]:
    """``(status, public_detail, operator_detail)`` for one directory.

    The directory passes when it sits under a bind mount of our own container
    whose host ``Source`` equals its container ``Destination``: then every
    path Oduflow derives from it names the same file on both sides.
    """
    path = os.path.normpath(path or "/")
    best: dict[str, Any] | None = None
    best_dest = ""
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        dest = os.path.normpath(str(mount.get("Destination") or ""))
        if dest in ("", "/"):
            continue
        if (path == dest or path.startswith(dest + "/")) and len(dest) > len(best_dest):
            best, best_dest = mount, dest

    fix = (
        f"mount a host directory at the same path (-v {path}:{path}) "
        f"and point [storage].{label} at it"
    )
    if best is None:
        return (
            "warn",
            f"{label} is inside the Oduflow container's writable layer; the "
            "Docker daemon cannot see it, so every bind mount Oduflow creates "
            "from it resolves to an empty host directory",
            f"{label} {path} is not under any mount of this container; {fix}",
        )
    if best.get("Type") != "bind":
        return (
            "warn",
            f"{label} is on a Docker {best.get('Type')}, not a host bind mount; "
            "the Docker daemon resolves Oduflow's mount sources on the host, "
            "where that path does not exist",
            f"{label} {path} is on {best.get('Type')} "
            f"{best.get('Name') or best.get('Source')} at {best_dest}; {fix}",
        )
    source = os.path.normpath(str(best.get("Source") or ""))
    if source != best_dest:
        return (
            "warn",
            f"{label} is bound from a different host path; the Docker daemon "
            "resolves Oduflow's mount sources on the host, where the "
            "container-side path does not exist",
            f"{label} {path} is bound from host path {source} at {best_dest}; {fix}",
        )
    return "ok", f"{label} is bound from the same host path", ""


def _self_mount_verdict(client: Any, settings: Settings) -> tuple[str, str, str]:
    """``(status, public_detail, operator_detail)`` for the self-mount check.

    Every bind mount Oduflow creates names a *host* path that the Docker daemon
    resolves, not the Oduflow container. A daemon that has no such path
    silently auto-creates an empty directory — PostgreSQL then parses a
    directory as its config and crash-loops, Traefik watches an empty dynamic
    directory, filestores land in the void. So when Oduflow itself runs in a
    container, both ``base_data_dir`` (filestores, tablespaces, exchange dirs)
    and ``etc_dir`` (``postgresql.conf``, ``postgresql-prod.conf``,
    ``traefik-dynamic/``) must sit under a bind mount whose host ``Source``
    equals its container ``Destination``; a named volume or a differing host
    path breaks every mount, invisibly. This check makes it visible. It is a
    warning, never an outage verdict: the server can still serve what it
    already has. The public detail names no host paths (``/healthz`` is
    unauthenticated); the operator detail does.
    """
    from oduflow.self_update import in_container

    if not in_container():
        return "off", "not running in a container", ""
    try:
        me = _own_container(client)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        return "off", "cannot inspect own container", str(exc)[:200]
    if me is None:
        return "off", "own container could not be identified; mounts not verified", ""
    attrs = getattr(me, "attrs", None) or {}
    mounts = attrs.get("Mounts") if isinstance(attrs, dict) else None
    if not isinstance(mounts, list):
        return "off", "own container mounts unreadable", ""

    verdicts = [
        _dir_mount_verdict(label, getattr(settings, attr), mounts)
        for label, attr in _DAEMON_VISIBLE_DIRS
    ]
    failing = [v for v in verdicts if v[0] != "ok"]
    if not failing:
        return "ok", "data_dir and etc_dir are bound from the same host path", ""
    return (
        "warn",
        "; ".join(v[1] for v in failing),
        "; ".join(v[2] for v in failing),
    )


def _check_self_mounts(client: Any, settings: Settings) -> dict[str, Any]:
    status, detail, _ = _self_mount_verdict(client, settings)
    return {"status": status, "detail": detail}


def warn_if_dirs_unreachable(settings: Settings) -> str | None:
    """Log the self-mount check loudly at startup; never raises.

    Returns the check status, or None when the check could not run.
    """
    try:
        from oduflow.docker_ops.client import get_client

        status, detail, operator_detail = _self_mount_verdict(get_client(), settings)
    except Exception:  # noqa: BLE001 - a diagnostic must not block startup
        logger.debug("Could not run the self-mount check", exc_info=True)
        return None
    if status == "warn":
        logger.warning(
            "Docker-out-of-Docker mount check: %s (%s). The PostgreSQL, Traefik "
            "and environment containers cannot work until this is fixed.",
            detail,
            operator_detail,
        )
    return status


def _unhealthy_productions(settings: Settings) -> list[str]:
    from oduflow import production_registry

    names = []
    for team in settings.teams.values():
        try:
            for name, record in production_registry.list_productions(team).items():
                if record.get("unhealthy"):
                    names.append(f"{team.team_id}/{name}")
        except Exception:
            continue
    return names


def _disabled_production_status(client: Any, settings: Settings) -> dict[str, Any]:
    """Confirm disabled production workloads are actually stopped."""
    try:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    "oduflow.prod=true",
                ]
            },
        )
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "running": []}
    running = sorted(c.name for c in containers if c.status == "running")
    if running:
        return {
            "status": "error",
            "detail": "production disabled but containers still running: "
            + ", ".join(running),
            "running": running,
        }
    return {
        "status": "off",
        "detail": "production hosting disabled",
        "running": [],
    }


def collect_health(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """All checks with a short cache. ``ok`` is the /healthz verdict."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["result"]
        if not force and cached is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
            return cached  # type: ignore[no-any-return]

    checks: dict[str, Any] = {}
    try:
        from oduflow.docker_ops.client import get_client

        client = get_client()
    except Exception as exc:
        client = None
        checks["docker"] = {"status": "error", "detail": str(exc)[:200]}

    if client is not None:
        checks["dev_pg"] = _check_pg(client, settings, settings.shared_db_container)
        checks["prod_pg"] = (
            _check_pg(client, settings, settings.prod_db_container)
            if settings.prod_enabled
            else {"status": "off", "detail": "production hosting disabled"}
        )
        checks["traefik"] = _check_traefik(client, settings)
        checks["self_mounts"] = _check_self_mounts(client, settings)
        checks["walg"] = _check_walg(client, settings, checks["prod_pg"])
        if settings.prod_enabled and checks["prod_pg"]["status"] != "off":
            from oduflow import wal_monitor

            result = wal_monitor.status(settings)
            checks["prod_wal_disk"] = {
                "status": result["status"],
                "detail": result["detail"],
                "free_bytes": result.get("disk_free_bytes"),
            }
    checks["s3"] = _check_s3(settings)
    checks["disk"] = _check_disk(settings)
    checks["overlays"] = _check_overlays(settings)

    if not settings.prod_enabled and client is not None:
        checks["productions"] = _disabled_production_status(client, settings)
    else:
        unhealthy = _unhealthy_productions(settings) if settings.prod_enabled else []
        checks["productions"] = {
            "status": "error" if unhealthy else "ok",
            "detail": ", ".join(unhealthy),
            "unhealthy": unhealthy,
        }

    # Critical checks: dev PG always; prod PG only when provisioned; traefik
    # when in traefik mode; S3 when configured; unhealthy productions; broken
    # filestore overlays (stale, unexpectedly absent, or unreadable).
    # self_mounts only ever warns: it explains *why* dev_pg/traefik fail in a
    # mis-mounted Docker-out-of-Docker setup, and those already degrade.
    critical = [
        "dev_pg",
        "prod_pg",
        "traefik",
        "s3",
        "walg",
        "prod_wal_disk",
        "productions",
        "docker",
        "overlays",
    ]
    ok = all(
        checks.get(name, {}).get("status") in ("ok", "warn", "off", None)
        for name in critical
    )
    result = {"ok": ok, "checks": checks}
    with _cache_lock:
        _cache["at"] = now
        _cache["result"] = result
    return result
