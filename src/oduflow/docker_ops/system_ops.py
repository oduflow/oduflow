from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import docker
from docker import DockerClient
from oduflow import pg_hba, service_database_credentials
from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.stats import default_env_limits
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.fsutil import atomic_write_private_json, atomic_write_private_text
from oduflow.naming import (
    get_db_name,
    get_service_database_name,
    get_tablespace_name,
    get_team_network_name,
    get_template_db_name,
    normalize_env_vars,
    validate_template_name,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BUNDLED_PG_CONF = _PACKAGE_ROOT / "templates" / "postgresql.conf"
_BUNDLED_ODOO_CONF = _PACKAGE_ROOT / "templates" / "odoo.conf"
_BUNDLED_SANITIZE_DIR = _PACKAGE_ROOT / "templates"
_PG_RESTORE_HELPER_IMAGE = "postgres:17"
_FILESTORE_HASH_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


def _is_within_directory(directory: str, target: str) -> bool:
    """Return True if ``target`` resolves to a path inside ``directory``.

    Guards archive extraction against path traversal ("zip-slip"): a member
    named ``../../etc/x`` would otherwise be written outside the destination.
    """
    directory = os.path.realpath(directory)
    target = os.path.realpath(target)
    return target == directory or target.startswith(directory + os.sep)


def _get_oduflow_version() -> str:
    """Return the installed package version."""
    try:
        return version("oduflow")
    except PackageNotFoundError:
        return "dev"


def _get_etc_dir() -> pathlib.Path:
    from oduflow.settings import _resolve_etc_dir

    return pathlib.Path(_resolve_etc_dir())


def _file_size_mb(path: str) -> float:
    """Return file size in MB, or 0.0 if file does not exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _update_template_sizes(
    team: TeamSettings,
    settings: Settings,
    template_name: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute filestore/dump sizes and persist them into template metadata."""
    from oduflow.docker_ops.env_ops import _dir_size_mb

    metadata_path = team.get_template_metadata_path(template_name)
    if metadata is None:
        if os.path.isfile(metadata_path):
            with open(metadata_path) as f:
                metadata = json.load(f)
        else:
            metadata = {}

    fs_path = team.get_template_filestore_path(template_name)
    dump_path = team.get_template_sql_path(template_name)
    fs_size = round(_dir_size_mb(fs_path), 1) if os.path.isdir(fs_path) else 0.0
    metadata["filestore_size_mb"] = fs_size
    metadata["dump_size_mb"] = round(_file_size_mb(dump_path), 1)
    if "use_overlay" not in metadata or metadata["use_overlay"] is None:
        metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb

    # Owner-only like the credential stores: metadata records the source
    # environment's env vars, which may hold plaintext values (migration 0007
    # brings pre-existing files forward).
    atomic_write_private_json(metadata_path, metadata, sort_keys=False)
    return metadata


def _normalize_extra_addons(raw_addons: object) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning(
            "Legacy list format for extra_addons (no branch info), skipping: %s",
            raw_addons,
        )
        return {}
    return {}


def _is_filestore_relpath(rel_path: str) -> bool:
    parts = rel_path.strip("/").split("/")
    if len(parts) != 2:
        return False
    chunk, filename = parts
    return (
        len(chunk) == 2
        and all(c in "0123456789abcdefABCDEF" for c in chunk)
        and bool(_FILESTORE_HASH_RE.match(filename))
        and filename[:2].lower() == chunk.lower()
    )


def _clean_archive_member_name(name: str) -> str:
    normalized = name.replace("\\", "/").strip("/")
    if (
        not normalized
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized == ".."
    ):
        return ""
    return normalized


def _detect_filestore_strip_prefix(paths: list[str]) -> str:
    candidates: dict[str, int] = {}
    for path in paths:
        clean = _clean_archive_member_name(path)
        if not clean:
            continue
        parts = clean.split("/")
        for idx in range(len(parts) - 1):
            rel = "/".join(parts[idx : idx + 2])
            if _is_filestore_relpath(rel):
                prefix = "/".join(parts[:idx])
                candidates[prefix] = candidates.get(prefix, 0) + 1
                break
    if not candidates:
        raise PrerequisiteNotMetError(
            "Could not detect an Odoo filestore layout. Expected files like "
            "'60/609e7ca59cc05bf0de7233c6781a381b742a2931'. "
            "Use --strip-prefix if the archive has an unusual wrapper path."
        )
    best_count = max(candidates.values())
    best = sorted(prefix for prefix, count in candidates.items() if count == best_count)
    if len(best) > 1:
        shown = ", ".join(prefix or "<none>" for prefix in best[:5])
        raise PrerequisiteNotMetError(
            "Could not choose a unique filestore prefix; candidates: "
            f"{shown}. Pass --strip-prefix explicitly."
        )
    return best[0]


def _normalize_strip_prefix(strip_prefix: str, paths: list[str]) -> str:
    value = (strip_prefix or "auto").strip().strip("/")
    if value == "auto":
        return _detect_filestore_strip_prefix(paths)
    if value in {"", ".", "none"}:
        return ""
    return value


def _strip_filestore_prefix(path: str, prefix: str) -> str:
    clean = _clean_archive_member_name(path)
    if not clean:
        return ""
    if not prefix:
        return clean
    if clean == prefix:
        return ""
    prefix_slash = prefix.rstrip("/") + "/"
    if not clean.startswith(prefix_slash):
        return ""
    return clean[len(prefix_slash) :]


def _copy_normalized_filestore_tree(
    source_dir: str, dest_dir: str, strip_prefix: str = "auto"
) -> tuple[int, str]:
    file_paths: list[str] = []
    for root, _dirs, files in os.walk(source_dir):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, source_dir).replace(os.sep, "/")
            file_paths.append(rel)
    prefix = _normalize_strip_prefix(strip_prefix, file_paths)
    written = 0
    for rel in file_paths:
        stripped = _strip_filestore_prefix(rel, prefix)
        if not stripped or not _is_filestore_relpath(stripped):
            continue
        src = os.path.join(source_dir, rel.replace("/", os.sep))
        if os.path.islink(src):
            logger.warning("Skipping symlink in filestore source: %s", rel)
            continue
        target = os.path.join(dest_dir, stripped)
        if not _is_within_directory(dest_dir, target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)
        written += 1
    if written == 0:
        raise PrerequisiteNotMetError(
            "No filestore files were copied. Check the source path or pass "
            "--strip-prefix explicitly."
        )
    return written, prefix


def _is_archive_source(source: str) -> bool:
    return source.lower().endswith(_ARCHIVE_SUFFIXES)


def _zip_member_is_symlink(info: Any) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _extract_zip_filestore(
    archive_path: str, dest_dir: str, strip_prefix: str = "auto"
) -> tuple[int, str]:
    import zipfile

    with zipfile.ZipFile(archive_path, "r") as zf:
        file_infos = [
            info
            for info in zf.infolist()
            if not info.is_dir() and not _zip_member_is_symlink(info)
        ]
        prefix = _normalize_strip_prefix(
            strip_prefix, [info.filename for info in file_infos]
        )
        written = 0
        for info in file_infos:
            stripped = _strip_filestore_prefix(info.filename, prefix)
            if not stripped or not _is_filestore_relpath(stripped):
                continue
            target = os.path.join(dest_dir, stripped)
            if not _is_within_directory(dest_dir, target):
                logger.warning(
                    "Skipping unsafe archive member outside filestore: %s",
                    info.filename,
                )
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written += 1
    if written == 0:
        raise PrerequisiteNotMetError(
            "No filestore files were extracted. Check the archive or pass "
            "--strip-prefix explicitly."
        )
    return written, prefix


def _extract_tar_filestore(
    archive_path: str, dest_dir: str, strip_prefix: str = "auto"
) -> tuple[int, str]:
    with tarfile.open(archive_path, "r:*") as tf:
        members = [member for member in tf.getmembers() if member.isfile()]
        prefix = _normalize_strip_prefix(
            strip_prefix, [member.name for member in members]
        )
        written = 0
        for member in members:
            stripped = _strip_filestore_prefix(member.name, prefix)
            if not stripped or not _is_filestore_relpath(stripped):
                continue
            target = os.path.join(dest_dir, stripped)
            if not _is_within_directory(dest_dir, target):
                logger.warning(
                    "Skipping unsafe tar member outside filestore: %s", member.name
                )
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written += 1
    if written == 0:
        raise PrerequisiteNotMetError(
            "No filestore files were extracted. Check the archive or pass "
            "--strip-prefix explicitly."
        )
    return written, prefix


def _extract_archive_filestore(
    archive_path: str, dest_dir: str, strip_prefix: str = "auto"
) -> tuple[int, str]:
    if archive_path.lower().endswith(".zip"):
        return _extract_zip_filestore(archive_path, dest_dir, strip_prefix)
    return _extract_tar_filestore(archive_path, dest_dir, strip_prefix)


def _is_remote_rsync_source(source: str) -> bool:
    if source.startswith("rsync://"):
        return True
    if os.path.exists(source):
        return False
    return bool(re.match(r"^[^/\s:]+:.+", source))


def _run_rsync_source(source: str, dest: str) -> None:
    source_arg = source.rstrip("/") + "/"
    dest_arg = dest.rstrip("/") + "/"
    logger.info("Rsync filestore source: %s -> %s", source_arg, dest_arg)
    subprocess.run(
        ["rsync", "-a", "--delete", source_arg, dest_arg],
        check=True,
        capture_output=True,
    )


def _stage_filestore_source(
    source: str,
    raw_dir: str,
    prepared_dir: str,
    strip_prefix: str = "auto",
) -> tuple[int, str, str]:
    if os.path.isfile(source):
        if not _is_archive_source(source):
            raise PrerequisiteNotMetError(
                "Filestore source file must be a supported archive: "
                ".zip, .tar, .tar.gz, .tgz, .tar.bz2, .tbz2, .tar.xz, or .txz."
            )
        files, prefix = _extract_archive_filestore(source, prepared_dir, strip_prefix)
        return files, prefix, "archive"

    if os.path.isdir(source):
        _run_rsync_source(source, raw_dir)
        files, prefix = _copy_normalized_filestore_tree(
            raw_dir, prepared_dir, strip_prefix
        )
        return files, prefix, "rsync"

    if _is_remote_rsync_source(source):
        _run_rsync_source(source, raw_dir)
        files, prefix = _copy_normalized_filestore_tree(
            raw_dir, prepared_dir, strip_prefix
        )
        return files, prefix, "rsync"

    raise NotFoundError(f"Filestore source not found: {source}")


def _odoo_major_from_module_version(module_version: str) -> str:
    parts = module_version.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return ""


def _read_template_manifest_from_db(
    client: DockerClient, settings: Settings, template_db: str
) -> dict[str, object]:
    """Build Odoo backup-like manifest metadata from a restored database."""
    version = _exec_sql(
        client,
        settings,
        "SELECT COALESCE(latest_version, '') FROM ir_module_module "
        "WHERE name='base' AND state='installed' LIMIT 1;",
        db=template_db,
    ).strip()
    major_version = _odoo_major_from_module_version(version)
    pg_version = _exec_sql(client, settings, "SHOW server_version;", db=template_db)
    modules_raw = _exec_sql(
        client,
        settings,
        "SELECT COALESCE(json_object_agg(name, latest_version), '{}'::json)::text "
        "FROM ir_module_module WHERE state='installed';",
        db=template_db,
    )
    try:
        modules = json.loads(modules_raw) if modules_raw else {}
    except json.JSONDecodeError:
        logger.warning(
            "Could not parse module manifest from %s: %s", template_db, modules_raw
        )
        modules = {}
    return {
        "version": version,
        "major_version": major_version,
        "pg_version": pg_version,
        "modules": modules,
    }


def _resolve_conf(name: str) -> pathlib.Path:
    """Return /etc/oduflow/{name} if present, otherwise the bundled copy."""
    etc_path = _get_etc_dir() / name
    if etc_path.is_file():
        return etc_path
    bundled = _PACKAGE_ROOT / "templates" / name
    return bundled


def _resolve_instance_conf(name: str, data_dir: str) -> pathlib.Path:
    """Return {data_dir}/{name} if present, otherwise the bundled copy."""
    inst_path = pathlib.Path(data_dir) / name
    if inst_path.is_file():
        return inst_path
    return _PACKAGE_ROOT / "templates" / name


def _trusts_upstream_headers(settings: Settings) -> bool:
    """Whether Traefik's ``web`` entrypoint should trust incoming X-Forwarded-*.

    Only true for the "upstream terminates TLS" shape: ``tls = false`` plus a
    public scheme of https, i.e. a Cloudflare-tunnel-style terminator in front
    that must be able to tell us the browser spoke HTTPS. When the deployment is
    plain HTTP end to end (``public_scheme = "http"``) there is no trusted hop,
    so the headers stay untrusted and any client-supplied ones are overwritten.
    """
    return not settings.routing_tls and settings.public_scheme == "https"


def _route_entrypoint(settings: Settings) -> dict[str, Any]:
    """Entrypoint/TLS fragment shared by team and extra-route routers.

    TLS mode routes on ``websecure`` with a Let's Encrypt cert; otherwise (behind
    an upstream TLS terminator such as a Cloudflare tunnel) plain HTTP on ``web``.
    """
    if settings.routing_tls:
        return {"entryPoints": ["websecure"], "tls": {"certResolver": "letsencrypt"}}
    return {"entryPoints": ["web"]}


def _resolve_upstream_url(url: str) -> str:
    """Make a host-loopback upstream reachable from inside the Traefik container.

    A route target of ``http://127.0.0.1:PORT`` (or ``localhost``) means "a
    service listening on the host", but inside the Traefik container 127.0.0.1 is
    the container's own loopback. Rewrite it to ``host.docker.internal`` (mapped
    to host-gateway on the container) so the documented simple case just works.

    Only ``http://`` loopbacks are rewritten. Rewriting an ``https://localhost``
    upstream would make Traefik verify the backend certificate against
    ``host.docker.internal`` while the host's cert is issued for ``localhost`` /
    ``127.0.0.1`` — a guaranteed TLS mismatch (502). A TLS loopback backend is an
    advanced case: point it at the host's real name, or use a drop-in dynamic
    file with a ``serversTransport``. Non-loopback upstreams pass through.
    """
    return re.sub(
        r"^(http://)(127\.0\.0\.1|localhost)(?=[:/]|$)",
        r"\1host.docker.internal",
        url,
    )


def _write_traefik_dynamic_config(settings: Settings, config_path: str) -> None:
    """Generate Traefik dynamic config: route each team hostname to Oduflow, plus
    any static ``[route.*]`` entries to their external upstreams."""
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {
        "oduflow": {
            "loadBalancer": {
                "servers": [{"url": f"http://host.docker.internal:{settings.port}"}]
            }
        }
    }

    for team_id, team in settings.teams.items():
        if not team.hostname:
            continue
        routers[f"oduflow-team-{team_id}"] = {
            "rule": f"Host(`{team.hostname}`)",
            "service": "oduflow",
            **_route_entrypoint(settings),
        }

    # Cross-subdomain Connect As landing: ``/oduflow-connect`` on any env host is
    # routed to Oduflow (not the env's Odoo) so Oduflow can set the session
    # cookie host-only on that host. High priority so it beats the env's docker
    # ``Host(...)`` router for this path only; every other path still hits Odoo.
    if routers:
        routers["oduflow-connect"] = {
            "rule": "PathPrefix(`/oduflow-connect`)",
            "service": "oduflow",
            "priority": 100000,
            **_route_entrypoint(settings),
        }

    # Static extra routes: an external hostname → an arbitrary upstream URL for a
    # service Oduflow does not manage (see [route.*] in oduflow.toml).
    for route in settings.extra_routes:
        name = f"oduflow-route-{route.name}"
        routers[name] = {
            "rule": f"Host(`{route.host}`)",
            "service": name,
            **_route_entrypoint(settings),
        }
        services[name] = {
            "loadBalancer": {"servers": [{"url": _resolve_upstream_url(route.url)}]}
        }

    if not routers:
        return

    config = {"http": {"routers": routers, "services": services}}

    # Written to a ``.yml`` file: Traefik's file provider only accepts
    # .toml/.yaml/.yml (it rejects .json), and JSON is a valid subset of YAML,
    # so json.dump output parses fine. Do not switch the extension back to .json.
    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("Traefik dynamic config written to %s", config_path)


def _ensure_traefik(client: DockerClient, settings: Settings) -> None:
    if settings.routing_mode != "traefik":
        return

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    # ACME/Let's Encrypt only when Traefik terminates TLS itself.
    if settings.routing_tls:
        try:
            client.volumes.get(settings.traefik_acme_volume)
        except docker.errors.NotFound:
            client.volumes.create(settings.traefik_acme_volume, labels=system_labels)
            logger.info("Created volume %s", settings.traefik_acme_volume)

    # Host path for the dynamic config, bind-mounted into the container below.
    # Use the resolved config dir (``/etc/oduflow`` when writable, else
    # ``~/.oduflow/conf``) so it works without root on macOS and lands under a
    # path Docker Desktop shares — same placement as postgresql.conf. We mount a
    # whole directory (not a single file) so operators can drop their own
    # ``*.yml`` dynamic-config files alongside Oduflow's generated ``oduflow.yml``
    # (Traefik watches the directory); Oduflow only ever writes/overwrites
    # ``oduflow.yml`` and never touches the operator's files.
    traefik_dynamic_dir = os.path.join(settings.etc_dir, "traefik-dynamic")
    os.makedirs(traefik_dynamic_dir, exist_ok=True)
    _write_traefik_dynamic_config(
        settings, os.path.join(traefik_dynamic_dir, "oduflow.yml")
    )

    try:
        t = client.containers.get(settings.traefik_container)
        # Self-correcting drift control: _ensure_traefik never rewrites an
        # existing container's args, so config that changed since the container
        # was created would otherwise be ignored until a manual recreate.
        # Recreate on either drift:
        #   - routing tls: the HTTP->HTTPS redirect arg is present only in TLS
        #     mode, so its presence must match routing_tls.
        #   - forwarded headers: trusted only in front of an upstream TLS
        #     terminator, so its presence must match _trusts_upstream_headers
        #     (which public_scheme can flip without touching tls).
        #   - file provider: older containers watch a single file
        #     (--providers.file.filename); we now watch the dynamic directory
        #     (--providers.file.directory), which is what lets operator drop-in
        #     *.yml load. Recreate anything not already on the directory
        #     provider. This supersedes a startup migration: it self-heals even
        #     when the container was left over from a prior traefik-mode setup
        #     while the server ran in port mode.
        cmd = t.attrs.get("Config", {}).get("Cmd") or []
        has_redirect = any("redirections" in str(arg) for arg in cmd)
        has_forwarded = any("forwardedHeaders.insecure" in str(arg) for arg in cmd)
        wants_forwarded = _trusts_upstream_headers(settings)
        on_dir_provider = any(
            str(arg).startswith("--providers.file.directory=") for arg in cmd
        )
        if (
            has_redirect != settings.routing_tls
            or has_forwarded != wants_forwarded
            or not on_dir_provider
        ):
            logger.info(
                "Recreating %s: config drift (tls container=%s wanted=%s, "
                "forwarded_headers container=%s wanted=%s, on_dir_provider=%s)",
                settings.traefik_container,
                has_redirect,
                settings.routing_tls,
                has_forwarded,
                wants_forwarded,
                on_dir_provider,
            )
            t.stop()
            t.remove()
        else:
            if t.status != "running":
                t.start()
            return
    except docker.errors.NotFound:
        pass

    volumes = {
        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"},
        traefik_dynamic_dir: {"bind": "/etc/traefik/dynamic", "mode": "ro"},
    }
    command = [
        "--log.level=INFO",
        "--providers.docker=true",
        "--providers.docker.exposedbydefault=false",
        "--providers.file.directory=/etc/traefik/dynamic",
        "--providers.file.watch=true",
        "--entrypoints.web.address=:80",
    ]
    if settings.routing_tls:
        ports = {"80/tcp": 80, "443/tcp": 443}
        volumes[settings.traefik_acme_volume] = {"bind": "/acme", "mode": "rw"}
        command += [
            "--entrypoints.websecure.address=:443",
            "--entrypoints.web.http.redirections.entryPoint.to=websecure",
            "--entrypoints.web.http.redirections.entryPoint.scheme=https",
            "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
            f"--certificatesresolvers.letsencrypt.acme.email={settings.acme_email}",
            "--certificatesresolvers.letsencrypt.acme.storage=/acme/acme.json",
        ]
    else:
        # Plain HTTP on :80 only — either an upstream (e.g. Cloudflare tunnel)
        # terminates TLS and forwards here over HTTP, or the deployment really
        # is plain HTTP end to end (public_scheme = "http").
        ports = {"80/tcp": 80}
        if _trusts_upstream_headers(settings):
            # Trust the upstream's X-Forwarded-* headers. Without this Traefik
            # overwrites X-Forwarded-Proto with the actual connection scheme
            # (http on this entrypoint), so the tunnel's `X-Forwarded-Proto:
            # https` would be lost and Oduflow would treat the request as
            # insecure (dropping the cookie Secure flag and generating http://
            # links). This entrypoint is only meant to receive traffic from the
            # trusted TLS terminator, so trusting all forwarded headers here is
            # intended. Without such a terminator the entrypoint is directly
            # exposed and anyone could forge the header, so it stays off.
            command.append("--entrypoints.web.forwardedHeaders.insecure=true")

    client.containers.run(
        "traefik:v3",
        name=settings.traefik_container,
        detach=True,
        network=settings.shared_network,
        ports=ports,
        extra_hosts={"host.docker.internal": "host-gateway"},
        volumes=volumes,
        command=command,
        labels=system_labels,
        restart_policy={"Name": "unless-stopped"},
    )
    logger.info("Created container %s", settings.traefik_container)


def _destroy_traefik(
    client: DockerClient, settings: Settings, removed: list[str]
) -> None:
    try:
        t = client.containers.get(settings.traefik_container)
        t.stop()
        t.remove(v=True)
        removed.append(settings.traefik_container)
    except docker.errors.NotFound:
        pass

    try:
        v = client.volumes.get(settings.traefik_acme_volume)
        v.remove()
        removed.append(settings.traefik_acme_volume)
    except docker.errors.NotFound:
        pass


def _exec_exit_code(
    client: DockerClient,
    container: Any,
    cmd: list[str],
    *,
    timeout: float = 10.0,
) -> int:
    """Run *cmd* in *container* and return its exit code within *timeout*.

    ``container.exec_run`` cannot be used where a hang must not be fatal:
    docker-py reads the exec output off a socket whose timeout it deliberately
    disables (``APIClient._disable_socket_timeout``), so an exec that never
    finishes — a container wedged by a daemon restart, for instance — blocks the
    caller forever. Starting the exec detached and polling ``exec_inspect``
    keeps every Docker call under the client's own request timeout and makes the
    wait genuinely bounded.

    Raises ``TimeoutError`` when the command is still running at the deadline;
    Docker API failures propagate as ``docker.errors.APIError``.
    """
    exec_id = client.api.exec_create(container.id, cmd)["Id"]
    client.api.exec_start(exec_id, detach=True)
    deadline = time.monotonic() + timeout
    while True:
        info = client.api.exec_inspect(exec_id)
        if not info.get("Running"):
            exit_code = info.get("ExitCode")
            # A finished exec with no reported code is not a success.
            return 1 if exit_code is None else int(exit_code)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"'{' '.join(cmd)}' in {container.name} did not finish "
                f"within {timeout:.0f}s"
            )
        time.sleep(0.5)


def _wait_pg_ready(
    client: DockerClient,
    settings: Settings,
    timeout: int = 30,
    *,
    container_name: str | None = None,
    exec_timeout: float = 10.0,
) -> None:
    # container_name selects the PostgreSQL instance: the shared dev one by
    # default, or the production cluster (settings.prod_db_container).
    container = client.containers.get(container_name or settings.shared_db_container)
    deadline = time.monotonic() + timeout
    for _ in range(timeout):
        try:
            ready = _exec_exit_code(
                client,
                container,
                ["pg_isready", "-U", settings.db_user],
                timeout=exec_timeout,
            )
            if ready == 0:
                probe = _exec_exit_code(
                    client,
                    container,
                    [
                        "psql",
                        "-U",
                        settings.db_user,
                        "-d",
                        "postgres",
                        "-tAc",
                        "SELECT 1;",
                    ],
                    timeout=exec_timeout,
                )
                if probe == 0:
                    return
        except docker.errors.APIError:
            # The DB container isn't running yet — still starting, or restarting
            # after a crash (e.g. the disk filled up). Docker's exec_create then
            # returns 409. Treat it as "not ready" and retry instead of letting a
            # transient state crash the whole server startup (which systemd would
            # turn into a restart loop).
            pass
        except TimeoutError as exc:
            # A wedged exec is also just "not ready": retry, and let the loop's
            # own deadline decide when to give up.
            logger.warning("PostgreSQL readiness probe timed out: %s", exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    raise PrerequisiteNotMetError(
        f"PostgreSQL ({container.name}) did not become ready within "
        f"{timeout}s. Check its logs: docker logs {container.name}"
    )


def _exec_sql(
    client: DockerClient,
    settings: Settings,
    sql: str,
    db: str = "postgres",
    *,
    container_name: str | None = None,
) -> str:
    container = client.containers.get(container_name or settings.shared_db_container)
    exit_code, output = container.exec_run(
        ["psql", "-U", settings.db_user, "-d", db, "-tAc", sql]
    )
    result = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, result)
    return result.strip()


def get_team_db_usage_bytes(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> int:
    """Combined on-disk size of the team's PostgreSQL databases (environments,
    templates, and auxiliary-service databases).

    One catalog query: ``pg_database_size()`` stats the database's files under
    PGDATA (each database is one directory there) — it does not scan table
    contents, so this is milliseconds, not a table walk. Names are filtered in
    Python to avoid LIKE-pattern escaping of the team id.

    Environment and template databases are matched by prefix, which is what
    their released naming allows. Service databases are matched by exact name
    instead: team ids are unvalidated, so a prefix like
    ``oduflow_service_<team>_`` would also capture the *environment* databases
    of a team literally named ``service_<team>`` and bill them to the wrong
    quota. The credential records are authoritative for which service
    databases a team owns, so the exact set is cheap to derive.
    """
    rows = _exec_sql(
        client,
        settings,
        "SELECT datname, pg_database_size(datname) FROM pg_database "
        "WHERE NOT datistemplate;",
    )
    prefixes = (
        f"oduflow_{team.team_id}_",
        f"oduflow_template_{team.team_id}_",
    )
    service_databases = {
        get_service_database_name(item, team.team_id)
        for item in service_database_credentials.list_names(team)
    }
    total = 0
    for line in rows.splitlines():
        name, _, size = line.partition("|")
        if not size.strip().isdigit():
            continue
        if name.startswith(prefixes) or name in service_databases:
            total += int(size)
    return total


def check_db_quota(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    estimated_new_db_bytes: int = 0,
) -> None:
    """Raise if the team's PostgreSQL usage is at or over its ``db_quota_gb``.

    Called before operations that create a *new* database (environment or
    template); replacement operations (refresh/reload) are not gated so a
    team at its quota can still shrink or refresh what it has. 0 disables
    the quota. ``estimated_new_db_bytes`` (the predicted size of the database
    about to be created) makes the check predictive: a clone that would land
    over the quota is refused before CREATE DATABASE starts, not after it
    fails halfway.
    """
    if team.db_quota_gb <= 0:
        return
    used = get_team_db_usage_bytes(client, settings, team)
    quota = team.db_quota_gb * 1024**3
    projected = used + max(estimated_new_db_bytes, 0)
    if projected >= quota:
        raise PrerequisiteNotMetError(
            f"Team '{team.team_id}' database quota exceeded: "
            f"{used / 1024**3:.1f} GB used plus an estimated "
            f"{max(estimated_new_db_bytes, 0) / 1024**3:.1f} GB for the new "
            f"database exceeds the {team.db_quota_gb} GB quota "
            "(db_quota_gb in oduflow.toml). Delete unused environments, "
            "templates, or service databases, or raise the quota."
        )


# Disk admission control for environment creation: refuse to start when a
# target filesystem would be left without breathing room. Deliberately
# constants, not TOML options — the reserve exists so PostgreSQL never hits
# 0 bytes free (at which point even deleting environments fails).
_DISK_MIN_FREE_GB = 5.0  # absolute floor that must remain free
_DISK_MIN_FREE_PERCENT = 5.0  # relative floor for small disks
_DISK_RESERVE_CAP_GB = 10.0  # cap on the percent leg (large/dev-machine disks)
_DISK_ESTIMATE_MARGIN = 1.2  # safety multiplier on the write estimate
_CLONE_BUDGET_BYTES = 512 * 1024**2  # remote git checkout allowance
_OVERLAY_HEADROOM_BYTES = 512 * 1024**2  # upper/work layer of an overlay
_GREENFIELD_DB_BYTES = 1024**3  # `-i base` init without a template


def estimate_new_db_bytes(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str | None,
) -> int:
    """Predicted on-disk size of the environment database about to be created.

    ``CREATE DATABASE ... TEMPLATE`` physically copies the template, so
    ``pg_database_size()`` of the template DB is the exact answer. Estimation
    failures fall back to the greenfield budget rather than blocking creation.
    """
    if template_name is None:
        return _GREENFIELD_DB_BYTES
    tpl_db = get_template_db_name(template_name, team.team_id)
    try:
        return int(_exec_sql(client, settings, f"SELECT pg_database_size('{tpl_db}');"))
    except Exception:
        logger.warning("Could not measure template DB '%s' size", tpl_db, exc_info=True)
        return _GREENFIELD_DB_BYTES


def _estimate_template_filestore_bytes(
    settings: Settings, team: TeamSettings, template_name: str
) -> int:
    """Bytes the new environment's filestore will occupy on the host.

    Mirrors the mode selection in ``env_ops._mount_filestore``: an overlay
    mount (Linux only) does not copy the lower layer and needs only headroom
    for the upper/work dirs; a plain copy needs the full template size.
    """
    fs_path = team.get_template_filestore_path(template_name)
    if not os.path.isdir(fs_path):
        return 0
    metadata: dict[str, Any] = {}
    metadata_path = team.get_template_metadata_path(template_name)
    if os.path.isfile(metadata_path):
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, OSError):
            metadata = {}
    fs_size_mb = metadata.get("filestore_size_mb")
    if not isinstance(fs_size_mb, (int, float)):
        from oduflow.docker_ops.env_ops import _dir_size_mb

        logger.info(
            "Template '%s' metadata lacks filestore_size_mb; scanning filestore",
            template_name,
        )
        fs_size_mb = _dir_size_mb(fs_path)
    use_overlay = metadata.get("use_overlay")
    if use_overlay is None:
        use_overlay = fs_size_mb >= settings.overlay_threshold_mb
    if use_overlay and sys.platform.startswith("linux"):
        return _OVERLAY_HEADROOM_BYTES
    return int(fs_size_mb * 1024**2)


def _existing_anchor(path: str) -> str | None:
    """Nearest existing ancestor of ``path`` (which may not exist yet)."""
    p = os.path.realpath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    return p or None


def _reserve_bytes(total: int) -> int:
    """max(5 GiB, min(5% of the disk, 10 GiB)) — the percent leg is capped so
    a large disk (e.g. a 1 TB dev machine) is not held to a ~50 GiB reserve."""
    percent_leg = min(
        total * _DISK_MIN_FREE_PERCENT / 100, _DISK_RESERVE_CAP_GB * 1024**3
    )
    return int(max(_DISK_MIN_FREE_GB * 1024**3, percent_leg))


def _pgdata_usage_via_df(
    client: DockerClient, settings: Settings
) -> tuple[int, int] | None:
    """(total, free) bytes of PGDATA measured inside the shared DB container.

    Fallback for hosts where the named volume's mountpoint is not visible
    (macOS / Docker Desktop). Returns None when the container or df is
    unavailable — the check is then skipped, never fatal.
    """
    try:
        container = client.containers.get(settings.shared_db_container)
        exit_code, output = container.exec_run(
            ["df", "-Pk", "/var/lib/postgresql/data"]
        )
        if exit_code != 0:
            return None
        text = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        fields = text.strip().splitlines()[-1].split()
        # POSIX df -Pk: Filesystem 1024-blocks Used Available Capacity Mounted
        return int(fields[1]) * 1024, int(fields[3]) * 1024
    except Exception:
        logger.warning("Could not measure PGDATA disk usage via df", exc_info=True)
        return None


def check_disk_space(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str | None,
    *,
    estimated_db_bytes: int,
    local_mount: bool = False,
    env_name: str = "",
) -> None:
    """Refuse environment creation when a target filesystem lacks room.

    Estimates the bytes each target directory will receive (database
    tablespace, workspace filestore + checkout), groups targets that share a
    device via ``st_dev``, and requires ``free - estimate*margin`` to stay
    above ``max(5 GiB, min(5%, 10 GiB))`` on every device. The PGDATA volume (WAL +
    catalogs live there, not in the tablespace) is held to the same floor.
    Like ``check_db_quota``, only new creation is gated — delete/cleanup paths
    stay available so a full disk can always be recovered.
    """
    fs_bytes = (
        _estimate_template_filestore_bytes(settings, team, template_name)
        if template_name is not None
        else 0
    )
    clone_bytes = 0 if local_mount else _CLONE_BUDGET_BYTES
    targets: list[tuple[str, str, int]] = [
        (
            "database tablespace",
            os.path.join(_pg_tablespaces_host_dir(settings), f"team_{team.team_id}"),
            max(estimated_db_bytes, 0),
        ),
        (
            "workspace (filestore + checkout)",
            team.workspaces_dir,
            fs_bytes + clone_bytes,
        ),
    ]

    pgdata_df: tuple[int, int] | None = None
    mountpoint = ""
    try:
        volume = client.volumes.get(settings.shared_db_volume)
        attrs = volume.attrs if isinstance(volume.attrs, dict) else {}
        raw = attrs.get("Mountpoint", "")
        mountpoint = raw if isinstance(raw, str) else ""
    except Exception:
        logger.warning("Could not inspect shared DB volume", exc_info=True)
    if mountpoint and os.path.isdir(mountpoint):
        targets.append(("PostgreSQL data (WAL)", mountpoint, 0))
    else:
        pgdata_df = _pgdata_usage_via_df(client, settings)

    groups: dict[int, dict[str, Any]] = {}
    for label, path, nbytes in targets:
        anchor = _existing_anchor(path)
        if anchor is None:
            logger.warning("Disk check: no existing ancestor for %s (%s)", path, label)
            continue
        try:
            dev = os.stat(anchor).st_dev
        except OSError:
            logger.warning("Disk check: cannot stat %s (%s)", anchor, label)
            continue
        group = groups.setdefault(dev, {"anchor": anchor, "bytes": 0, "labels": []})
        group["bytes"] += nbytes
        group["labels"].append(label)

    problems: list[str] = []
    checked: list[dict[str, Any]] = []
    for group in groups.values():
        try:
            usage = shutil.disk_usage(group["anchor"])
        except OSError:
            logger.warning("Disk check: disk_usage failed for %s", group["anchor"])
            continue
        required = int(group["bytes"] * _DISK_ESTIMATE_MARGIN)
        reserve = _reserve_bytes(usage.total)
        checked.append(
            {
                "anchor": group["anchor"],
                "components": group["labels"],
                "free_gb": round(usage.free / 1024**3, 1),
                "required_gb": round(required / 1024**3, 1),
                "reserve_gb": round(reserve / 1024**3, 1),
            }
        )
        if usage.free - required < reserve:
            problems.append(
                f"{' + '.join(group['labels'])} on '{group['anchor']}': "
                f"{usage.free / 1024**3:.1f} GiB free, creation needs "
                f"~{required / 1024**3:.1f} GiB and {reserve / 1024**3:.1f} GiB "
                "must stay free"
            )
    if pgdata_df is not None:
        total, free = pgdata_df
        reserve = _reserve_bytes(total)
        checked.append(
            {
                "anchor": "PGDATA (in-container)",
                "components": ["PostgreSQL data (WAL)"],
                "free_gb": round(free / 1024**3, 1),
                "required_gb": 0.0,
                "reserve_gb": round(reserve / 1024**3, 1),
            }
        )
        if free < reserve:
            problems.append(
                f"PostgreSQL data volume '{settings.shared_db_volume}': "
                f"{free / 1024**3:.1f} GiB free, {reserve / 1024**3:.1f} GiB "
                "must stay free"
            )

    logger.info(
        "Disk space check for environment creation",
        extra={
            "env_name": env_name,
            "template": template_name or "none",
            "estimated_db_bytes": estimated_db_bytes,
            "estimated_filestore_bytes": fs_bytes,
            "clone_budget_bytes": clone_bytes,
            "filesystems": checked,
            "problems": len(problems),
        },
    )
    if problems:
        raise PrerequisiteNotMetError(
            f"Not enough free disk space to create environment '{env_name}'. "
            + "; ".join(problems)
            + ". Delete unused environments or templates and retry."
        )


def pg_clone_strategy_clause(client: DockerClient, settings: Settings) -> str:
    """`` STRATEGY FILE_COPY`` when the server supports it (PG 15+), else "".

    PG 15 changed the CREATE DATABASE default to WAL_LOG, which writes the
    whole template clone through WAL — a large template can transiently double
    its disk cost and fill PGDATA mid-clone. FILE_COPY copies at file level
    and keeps WAL small.
    """
    try:
        version = int(_exec_sql(client, settings, "SHOW server_version_num;"))
    except Exception:
        logger.warning("Could not determine PostgreSQL version", exc_info=True)
        return ""
    return " STRATEGY FILE_COPY" if version >= 150000 else ""


def _create_pg_role(
    client: DockerClient,
    settings: Settings,
    username: str,
    password: str,
    db_name: str,
    *,
    container_name: str | None = None,
) -> None:
    safe_pw = password.replace("'", "''")
    _exec_sql(
        client,
        settings,
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{username}') THEN "
        f"CREATE ROLE \"{username}\" LOGIN PASSWORD '{safe_pw}'; "
        f"END IF; "
        f"END $$;",
        container_name=container_name,
    )
    # Always sync the password — the role may already exist with a stale password
    # from a previously deleted environment.
    _exec_sql(
        client,
        settings,
        f"ALTER ROLE \"{username}\" WITH LOGIN PASSWORD '{safe_pw}';",
        container_name=container_name,
    )
    _exec_sql(
        client,
        settings,
        f'ALTER DATABASE "{db_name}" OWNER TO "{username}";',
        container_name=container_name,
    )
    # Ensure the env role is NOT a member of the superuser role. Ownership of
    # template objects (needed for DDL during module upgrades) is handled by the
    # per-object reassignment in create_environment instead; superuser-role
    # membership would let the env role SET ROLE to superuser — a cross-tenant
    # RCE (#40). The REVOKE also de-escalates roles created before this change.
    _exec_sql(
        client,
        settings,
        f'REVOKE "{settings.db_user}" FROM "{username}";',
        container_name=container_name,
    )
    logger.info("Created/ensured PG role '%s' for database '%s'", username, db_name)


def _drop_pg_role(
    client: DockerClient,
    settings: Settings,
    username: str,
    *,
    container_name: str | None = None,
) -> None:
    if username == settings.db_user:
        return
    try:
        _exec_sql(
            client,
            settings,
            f'REVOKE "{settings.db_user}" FROM "{username}";',
            container_name=container_name,
        )
    except Exception:
        pass
    try:
        _exec_sql(
            client,
            settings,
            f'DROP OWNED BY "{username}";',
            container_name=container_name,
        )
    except Exception:
        pass
    _exec_sql(
        client,
        settings,
        f'DROP ROLE IF EXISTS "{username}";',
        container_name=container_name,
    )
    logger.info("Dropped PG role '%s'", username)


def reassign_db_ownership(
    client: DockerClient,
    settings: Settings,
    db_name: str,
    new_user: str,
    *,
    container_name: str | None = None,
) -> None:
    """Reassign every object in *db_name* not owned by *new_user* to it.

    A restored/cloned database's objects are owned by whatever role created
    them — normally the superuser, but plain-SQL imports can leave objects
    owned by a source env's role. DDL during module upgrades requires
    ownership, so transfer it per-object instead of granting superuser-role
    membership (which would be a cross-tenant RCE, #40). Linked
    (SERIAL/identity) sequences are skipped: they follow their table's owner
    automatically. System roles (pg_*) are left untouched.
    """
    _exec_sql(
        client,
        settings,
        f'ALTER SCHEMA public OWNER TO "{new_user}";',
        db=db_name,
        container_name=container_name,
    )
    _exec_sql(
        client,
        settings,
        rf"""
        DO $$
        DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT n.nspname FROM pg_namespace n JOIN pg_roles o ON n.nspowner = o.oid
                   WHERE o.rolname <> '{new_user}' AND o.rolname NOT LIKE 'pg\_%'
                     AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
          LOOP EXECUTE format('ALTER SCHEMA %I OWNER TO %I', r.nspname, '{new_user}'); END LOOP;

          FOR r IN SELECT c.relkind AS kind, n.nspname AS sch, c.relname AS rel
                   FROM pg_class c
                   JOIN pg_namespace n ON c.relnamespace = n.oid
                   JOIN pg_roles o ON c.relowner = o.oid
                   WHERE o.rolname <> '{new_user}' AND o.rolname NOT LIKE 'pg\_%'
                     AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
                     AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
                     AND NOT (c.relkind = 'S' AND EXISTS (
                         SELECT 1 FROM pg_depend d
                         WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                           AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))
          LOOP EXECUTE format('ALTER %s %I.%I OWNER TO %I',
               CASE r.kind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                           WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE'
                           ELSE 'TABLE' END, r.sch, r.rel, '{new_user}'); END LOOP;

          FOR r IN SELECT p.oid::regprocedure AS sig
                   FROM pg_proc p
                   JOIN pg_namespace n ON p.pronamespace = n.oid
                   JOIN pg_roles o ON p.proowner = o.oid
                   WHERE o.rolname <> '{new_user}' AND o.rolname NOT LIKE 'pg\_%'
                     AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
          LOOP EXECUTE format('ALTER ROUTINE %s OWNER TO %I', r.sig, '{new_user}'); END LOOP;
        END $$;
        """,
        db=db_name,
        container_name=container_name,
    )


def drop_signaling_sequences(
    client: DockerClient,
    settings: Settings,
    db_name: str,
    *,
    container_name: str | None = None,
) -> None:
    """Drop Odoo signaling sequences carried over from a source database.

    Odoo re-creates them on first startup (CREATE SEQUENCE without IF NOT
    EXISTS), so leftovers cause DuplicateTable errors.
    """
    _exec_sql(
        client,
        settings,
        "DO $$ DECLARE r RECORD; BEGIN "
        "FOR r IN SELECT c.relname FROM pg_class c "
        "WHERE c.relkind = 'S' "
        "AND (c.relname LIKE 'base_registry_signaling%' "
        "OR c.relname LIKE 'base_cache_signaling%') "
        "LOOP EXECUTE 'DROP SEQUENCE IF EXISTS ' || quote_ident(r.relname); "
        "END LOOP; END $$;",
        db=db_name,
        container_name=container_name,
    )


def _db_exists(
    client: DockerClient,
    settings: Settings,
    db_name: str,
    *,
    container_name: str | None = None,
) -> bool:
    result = _exec_sql(
        client,
        settings,
        f"SELECT 1 FROM pg_database WHERE datname='{db_name}';",
        container_name=container_name,
    )
    return result == "1"


def _is_text_dump(path: str) -> bool:
    """Check if dump is text format (SQL) or binary (pgdump). Handles gzip files."""
    try:
        # Handle gzip files by reading header directly
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                header = f.read(5)
        else:
            with open(path, "rb") as f:
                header = f.read(5)
        return header != b"PGDMP"
    except OSError:
        return False


def _copy_file_to_container(
    container: docker.models.containers.Container,
    src_path: str,
    dest_dir: str,
    *,
    archive_name: str | None = None,
) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, mode="w") as tar:
            tar.add(src_path, arcname=archive_name or os.path.basename(src_path))
        with open(tmp_path, "rb") as f:
            container.put_archive(dest_dir, f)
    finally:
        os.remove(tmp_path)


def _convert_custom_dump_to_sql_with_helper(
    client: DockerClient,
    settings: Settings,
    dump_path: str,
    *,
    is_gzipped: bool,
) -> tuple[int, str, str]:
    """Convert a custom dump to plain SQL with a newer pg_restore client."""
    dump_dir = os.path.abspath(os.path.dirname(dump_path))
    dump_name = os.path.basename(dump_path)
    container_path = f"/backup/{dump_name}"
    sql_path = os.path.join(dump_dir, "dump.sql")
    container_sql_path = "/backup/dump.sql"
    if is_gzipped:
        command = [
            "bash",
            "-c",
            "set -o pipefail; "
            f"gunzip -c {shlex.quote(container_path)} | "
            "pg_restore --no-owner "
            f"-f {shlex.quote(container_sql_path)}",
        ]
    else:
        command = [
            "pg_restore",
            "--no-owner",
            "-f",
            container_sql_path,
            container_path,
        ]

    with contextlib.suppress(Exception):
        client.images.pull(_PG_RESTORE_HELPER_IMAGE)

    helper_name = f"{settings.prefix}pg-restore-helper-{time.time_ns()}"
    helper = None
    try:
        helper = client.containers.run(
            _PG_RESTORE_HELPER_IMAGE,
            name=helper_name,
            detach=True,
            volumes={dump_dir: {"bind": "/backup", "mode": "rw"}},
            command=command,
            labels={settings.managed_label: "true", settings.system_label: "true"},
        )
        wait_result = helper.wait()
        exit_code = int(wait_result.get("StatusCode", -1))
        logs = helper.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        return exit_code, logs, sql_path
    except docker.errors.APIError as exc:
        return -1, str(exc), sql_path
    finally:
        if helper is not None:
            with contextlib.suppress(Exception):
                helper.remove(force=True)


def _is_pg_restore_archive_version_error(output: str) -> bool:
    output = output.lower()
    return "unsupported version" in output and "file header" in output


class _ChunkReader(io.RawIOBase):
    """Read-only file object over an iterator of byte chunks.

    ``container.get_archive`` hands back a generator; wrapping it here lets
    ``tarfile`` consume the archive as it arrives instead of spooling the whole
    thing to a temp file first (a full-size extra write + read per dump).
    """

    def __init__(self, chunks: Any) -> None:
        self._chunks = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        size = min(len(buffer), len(self._buf))
        buffer[:size] = self._buf[:size]
        self._buf = self._buf[size:]
        return size


@contextlib.contextmanager
def _container_archive_stream(
    container: docker.models.containers.Container, container_path: str
) -> Any:
    """Open the container's tar stream for sequential reading (``r|``).

    Stream mode has no random access: members must be extracted while the
    iteration is positioned on them, and ``getmembers()`` is unavailable.
    """
    chunks, _ = container.get_archive(container_path)
    reader = io.BufferedReader(_ChunkReader(chunks))
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        yield tar


def _copy_file_from_container(
    container: docker.models.containers.Container, container_path: str, dest_path: str
) -> None:
    with _container_archive_stream(container, container_path) as tar:
        for member in tar:
            if not member.isfile():
                continue
            src = tar.extractfile(member)
            if src is None:
                break
            with open(dest_path, "wb") as out:
                shutil.copyfileobj(src, out)
            return
    raise ExternalCommandError(
        "get_archive", -1, f"Could not extract {container_path} from tar"
    )


def _extract_archive_from_container(
    container: docker.models.containers.Container,
    container_path: str,
    dest_dir: str,
    prefix: str,
) -> int:
    extracted = 0
    with _container_archive_stream(container, container_path) as tar:
        for member in tar:
            if not member.name.startswith(prefix) and member.name != prefix.rstrip("/"):
                continue
            rel = member.name[len(prefix) :]
            if not rel:
                continue
            member.name = rel
            # Reject members that escape dest_dir via traversal or an
            # absolute/symlink target (defence in depth; source is our own
            # template-builder container).
            if not _is_within_directory(dest_dir, os.path.join(dest_dir, rel)):
                logger.warning("Skipping unsafe archive member: %s", rel)
                continue
            if member.issym() or member.islnk():
                link_target = os.path.join(dest_dir, os.path.dirname(rel))
                if not _is_within_directory(
                    dest_dir, os.path.join(link_target, member.linkname)
                ):
                    logger.warning("Skipping unsafe link member: %s", rel)
                    continue
            tar.extract(member, dest_dir)
            if not member.isdir():
                extracted += 1
    return extracted


_EXEC_EXIT_POLL_SECONDS = 5.0


def _wait_exec_exit_code(api: Any, exec_id: str) -> int:
    """Exit code of a finished exec, tolerating a briefly lagging daemon."""
    deadline = time.monotonic() + _EXEC_EXIT_POLL_SECONDS
    while True:
        code = api.exec_inspect(exec_id).get("ExitCode")
        if code is not None:
            return int(code)
        if time.monotonic() >= deadline:
            return -1
        time.sleep(0.05)


def _stream_exec_to_file(
    client: DockerClient,
    container: docker.models.containers.Container,
    cmd: list[str],
    dest_path: str,
    *,
    tool: str,
) -> int:
    """Run ``cmd`` in ``container``, streaming its stdout straight to ``dest_path``.

    Replaces the exec-to-/tmp + ``get_archive`` route, which wrote every dump
    three times at full size (container writable layer, host temp tar, final
    file) and left the container copy behind to grow the writable layer. Here
    the payload lands only at its destination.

    ``tty=False`` is required: with a TTY the daemon stops multiplexing the
    streams and mangles binary output. ``demux=True`` then keeps stderr out of
    the payload bytes. The exit code is checked explicitly — without it a
    truncated dump looks like success.
    """
    api = client.api
    exec_id = api.exec_create(container.id, cmd, tty=False, stdout=True, stderr=True)[
        "Id"
    ]
    stderr = bytearray()
    written = 0
    part_path = dest_path + ".part"
    try:
        with open(part_path, "wb") as out:
            for stdout_chunk, stderr_chunk in api.exec_start(
                exec_id, stream=True, demux=True
            ):
                if stdout_chunk:
                    out.write(stdout_chunk)
                    written += len(stdout_chunk)
                if stderr_chunk:
                    stderr.extend(stderr_chunk)
        exit_code = _wait_exec_exit_code(api, exec_id)
        message = stderr.decode("utf-8", errors="replace")
        if exit_code != 0:
            raise ExternalCommandError(tool, exit_code, message)
        os.replace(part_path, dest_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(part_path)
        raise
    if message.strip():
        logger.debug("%s stderr: %s", tool, message.strip())
    return written


def _pg_restore_jobs() -> int:
    """Worker count for parallel ``pg_restore``.

    Derived from the host CPUs rather than configured: the shared PostgreSQL
    container serves every team, so the cap keeps one restore from taking the
    whole cluster.
    """
    return max(1, min(4, os.cpu_count() or 1))


_PG_TABLESPACES_MOUNT = "/tablespaces"
_PG_EXCHANGE_MOUNT = "/exchange"


def _pg_tablespaces_host_dir(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, "pg_tablespaces")


def _pg_exchange_host_dir(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, "pg_exchange")


def _pg_exchange_dirs(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> tuple[str, str] | None:
    """Host and in-container paths of the team's dump exchange dir, if mounted.

    Returns None when the shared PostgreSQL container predates the mount (it is
    only attached at creation time, and an existing container is never recreated
    behind the operator's back). Callers fall back to streaming the dump out
    through the exec API, so nothing has to be migrated: an installation picks
    up the faster path whenever its PostgreSQL container is next recreated.
    """
    try:
        container = client.containers.get(settings.shared_db_container)
    except (docker.errors.NotFound, docker.errors.APIError):
        return None
    attrs = getattr(container, "attrs", None)
    mounts = attrs.get("Mounts") if isinstance(attrs, dict) else None
    if not isinstance(mounts, list):
        return None
    if not any(
        isinstance(m, dict) and m.get("Destination") == _PG_EXCHANGE_MOUNT
        for m in mounts
    ):
        return None
    leaf = f"team_{team.team_id}"
    return (
        os.path.join(_pg_exchange_host_dir(settings), leaf),
        f"{_PG_EXCHANGE_MOUNT}/{leaf}",
    )


@contextlib.contextmanager
def _staged_db_dump(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    db_name: str,
    dest_path: str,
    *,
    container_name: str | None = None,
) -> Any:
    """Dump *db_name* for *dest_path*, as cheaply as this installation allows.

    Yields ``(host_path, container_path)``. ``container_path`` is None unless
    the dump was staged in the pg_exchange mount, where PostgreSQL can restore
    it in place instead of having it copied into its writable layer first.
    Writing through the mount also leaves the archive's TOC offsets intact,
    which a parallel ``pg_restore`` uses; a dump streamed from stdout cannot
    carry them.

    ``container_name`` selects the PostgreSQL instance to dump from: the shared
    dev cluster by default, or ``settings.prod_db_container``. Only the shared
    container has the exchange dir mounted, so a dump from any other cluster is
    streamed out through the exec API — still straight into the exchange dir
    when there is one, so the dev-side restore can read it in place.

    On a clean exit the staged dump is moved to *dest_path*; on failure nothing
    is left behind, and *dest_path* is never half-written.
    """
    source_name = container_name or settings.shared_db_container
    db_container = client.containers.get(source_name)
    dump_in_place = source_name == settings.shared_db_container
    exchange = _pg_exchange_dirs(client, settings, team)
    dump_cmd = ["pg_dump", "-U", settings.db_user, "-Fc"]
    container_path: str | None = None

    if exchange is None:
        host_path = f"{dest_path}.staged"
    else:
        host_dir, container_dir = exchange
        os.makedirs(host_dir, exist_ok=True)
        name = f"{db_name}-{uuid.uuid4().hex}.pgdump"
        host_path = os.path.join(host_dir, name)
        container_path = f"{container_dir}/{name}"

    try:
        if container_path is None or not dump_in_place:
            _stream_exec_to_file(
                client, db_container, [*dump_cmd, db_name], host_path, tool="pg_dump"
            )
        else:
            exit_code, output = db_container.exec_run(
                [*dump_cmd, "-f", container_path, db_name]
            )
            if exit_code != 0:
                msg = (
                    output.decode("utf-8") if isinstance(output, bytes) else str(output)
                )
                raise ExternalCommandError("pg_dump", exit_code, msg)

        yield host_path, container_path
        try:
            os.replace(host_path, dest_path)
        except OSError as exc:
            # Both paths sit under the base data dir, so this is normally a free
            # rename. XFS also refuses it (-EXDEV) when the exchange dir was
            # never stamped with the team's quota project — see quotas.py.
            logger.warning(
                "Could not move the staged dump into place (%s); copying instead",
                exc,
            )
            shutil.move(host_path, dest_path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(host_path)


def _ensure_pg_container(
    client: DockerClient, settings: Settings, system_labels: dict[str, str]
) -> None:
    """Start (or create) the shared PostgreSQL container.

    Mounts only dedicated base-level directories into the container — never the
    whole data dir: PostgreSQL has no business seeing team workspaces or
    credentials. Per-team subdirectories are created inside these mounts, so
    adding a team never requires recreating the container (see
    ensure_team_tablespace).

    pg_tablespaces holds the databases themselves. pg_exchange is where dumps
    are staged: writing one there costs a single write to its final filesystem
    instead of a full-size copy through the container's writable layer, and a
    restore can read it in place. It carries no new exposure — a dump is a
    subset of the cluster this container already serves.
    """
    tablespaces_dir = _pg_tablespaces_host_dir(settings)
    exchange_dir = _pg_exchange_host_dir(settings)
    for mount_parent in (tablespaces_dir, exchange_dir):
        os.makedirs(mount_parent, exist_ok=True)
        # Only shared mount parents are public/searchable. Team directories
        # retain their own ownership and restrictive data permissions. Repair
        # parents created under a restrictive umask even for existing servers.
        os.chmod(mount_parent, 0o755)
    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            db_container.start()
        return
    except docker.errors.NotFound:
        pass

    client.containers.run(
        settings.postgres_image,
        name=settings.shared_db_container,
        detach=True,
        network=settings.shared_network,
        volumes={
            settings.shared_db_volume: {
                "bind": "/var/lib/postgresql/data",
                "mode": "rw",
            },
            str(_resolve_conf("postgresql.conf")): {
                "bind": "/etc/postgresql/postgresql.conf",
                "mode": "ro",
            },
            tablespaces_dir: {"bind": _PG_TABLESPACES_MOUNT, "mode": "rw"},
            exchange_dir: {"bind": _PG_EXCHANGE_MOUNT, "mode": "rw"},
        },
        command=["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"],
        environment={
            "POSTGRES_USER": settings.db_user,
            "POSTGRES_PASSWORD": settings.db_password,
        },
        labels=system_labels,
        restart_policy={"Name": "unless-stopped"},
    )
    logger.info("Created container %s", settings.shared_db_container)


def _prod_pg_conf_path(settings: Settings) -> str:
    return os.path.join(settings.etc_dir, "postgresql-prod.conf")


def _ensure_prod_pg_conf(settings: Settings) -> str:
    """Auto-generate the production postgresql.conf once (``# KEEP`` contract:
    an existing file is never rewritten; use ``retune-postgres`` explicitly)."""
    path = _prod_pg_conf_path(settings)
    if os.path.isfile(path):
        _warn_stale_prod_pg_conf(settings, path)
        return path
    from oduflow import prod_tune
    from oduflow.pg_tune import detect_resources
    from oduflow.resource_plan import build_resource_plan

    res = detect_resources()
    plan = build_resource_plan(
        res["total_ram_mb"],
        res["cpu_count"],
        production_enabled=True,
    )
    content = prod_tune.generate_prod_postgresql_conf(
        res["total_ram_mb"],
        res["cpu_count"],
        source=res["source"],
        oduflow_version=_get_oduflow_version(),
        plan=plan,
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    # This non-secret bind mount must be readable by the PostgreSQL user,
    # including when the Oduflow service uses a restrictive umask.
    os.chmod(path, 0o644)
    logger.info(
        "Config: %s (auto-tuned production profile: %d vCPU, %d MB RAM)",
        path,
        res["cpu_count"],
        int(res["total_ram_mb"]),
    )
    return path


def _warn_stale_prod_pg_conf(settings: Settings, path: str) -> None:
    """Warn when the managed production config predates the current plan."""
    try:
        from oduflow.pg_tune import detect_resources
        from oduflow.resource_plan import (
            build_resource_plan,
            tune_marker,
            tune_status,
        )

        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        res = detect_resources()
        plan = build_resource_plan(
            res["total_ram_mb"],
            res["cpu_count"],
            production_enabled=True,
        )
        status = tune_status(content, tune_marker(plan, "production"))
        if status in {"stale", "legacy"}:
            logger.warning(
                "Auto-generated production PostgreSQL config %s is %s for "
                "the current host resource plan; run `oduflow "
                "retune-postgres` to preview the update",
                path,
                status,
            )
    except Exception:
        logger.debug(
            "Could not check production PostgreSQL tuning fingerprint",
            exc_info=True,
        )


def _ensure_prod_pg_container(
    client: DockerClient, settings: Settings, system_labels: dict[str, str]
) -> None:
    """Start (or create) the production PostgreSQL container.

    Unlike the dev instance there is no /tablespaces mount: everything stays
    inside PGDATA so cluster-level WAL archiving and base backups (WAL-G)
    cover the whole state. The wal-g binary and config directories are
    bind-mounted read-only so backups can be enabled or reconfigured later
    without recreating the container. No ports are published — production
    Odoo containers reach it over the team networks.
    """
    from oduflow import walg

    try:
        db_container = client.containers.get(settings.prod_db_container)
        if db_container.status != "running":
            db_container.start()
        conf_path = _prod_pg_conf_path(settings)
        if os.path.isfile(conf_path):
            _warn_stale_prod_pg_conf(settings, conf_path)
        return
    except docker.errors.NotFound:
        pass

    conf_path = _ensure_prod_pg_conf(settings)
    os.makedirs(walg.bin_host_dir(settings), exist_ok=True)
    os.makedirs(walg.conf_host_dir(settings), exist_ok=True)
    client.containers.run(
        settings.prod_postgres_image or settings.postgres_image,
        name=settings.prod_db_container,
        detach=True,
        network=settings.shared_network,
        volumes={
            settings.prod_db_volume: {
                "bind": "/var/lib/postgresql/data",
                "mode": "rw",
            },
            conf_path: {
                "bind": "/etc/postgresql/postgresql.conf",
                "mode": "ro",
            },
            walg.bin_host_dir(settings): {"bind": walg.BIN_MOUNT, "mode": "ro"},
            walg.conf_host_dir(settings): {"bind": walg.CONF_MOUNT, "mode": "ro"},
        },
        command=["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"],
        environment={
            "POSTGRES_USER": settings.db_user,
            "POSTGRES_PASSWORD": settings.db_password,
        },
        labels={**system_labels, "oduflow.prod": "true"},
        restart_policy={"Name": "unless-stopped"},
    )
    logger.info("Created container %s", settings.prod_db_container)


def prod_infra_exists(client: DockerClient, settings: Settings) -> bool:
    try:
        client.containers.get(settings.prod_db_container)
        return True
    except docker.errors.NotFound:
        return False


def _prod_infra_required(client: DockerClient, settings: Settings) -> bool:
    if prod_infra_exists(client, settings):
        return True
    from oduflow import production_registry

    return any(
        production_registry.list_productions(team) for team in settings.teams.values()
    )


def ensure_prod_infra(
    client: DockerClient, settings: Settings, *, force: bool = False
) -> bool:
    """Provision the production tier (idempotent, lazy).

    Runs on every server start and from create_production (``force=True``).
    Dev-only installs never grow a second PostgreSQL: without ``force`` this
    is a no-op until a production exists or the container is already there.
    Returns True when the production infra is up.
    """
    from oduflow import production_registry, walg

    if not force and not _prod_infra_required(client, settings):
        return False

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.volumes.get(settings.prod_db_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.prod_db_volume, labels=system_labels)
        logger.info("Created volume %s", settings.prod_db_volume)

    # WAL-G binary + config. Best-effort: a github outage must not block
    # server startup or production provisioning — backups just stay off
    # (archive_command remains the no-op) until the next start succeeds.
    walg_ok = False
    try:
        walg.ensure_walg(settings)
        walg_ok = True
    except Exception as exc:
        logger.warning("wal-g unavailable (backups disabled for now): %s", exc)
    walg.write_walg_config(settings)

    _ensure_prod_pg_container(client, settings, system_labels)
    # walg.json must be readable by the container's postgres user (see
    # apply_walg_config_ownership); do it once the PG image is present.
    walg.apply_walg_config_ownership(settings, client)
    _wait_pg_ready(client, settings, container_name=settings.prod_db_container)

    # Attach the (possibly new) prod DB to every team network.
    for team in settings.teams.values():
        ensure_team_network(client, settings, team)
    _reconcile_pg_hba(
        client,
        settings,
        container_name=settings.prod_db_container,
    )

    try:
        walg.apply_archive_command(
            client, settings, enabled=walg_ok and settings.backup is not None
        )
    except Exception as exc:
        logger.warning("Could not set production archive_command: %s", exc)

    # A server that died mid-deploy leaves deploy_in_progress flags behind.
    for team in settings.teams.values():
        production_registry.clear_stale_deploy_flags(team)

    return True


def _prod_pg_running(client: DockerClient, settings: Settings) -> bool:
    """True when the dedicated production PostgreSQL container is up."""
    try:
        container = client.containers.get(settings.prod_db_container)
    except docker.errors.NotFound:
        return False
    return bool(container.status == "running")


def reconcile_prod_workloads(client: DockerClient, settings: Settings) -> None:
    """Apply the global production-hosting switch to managed containers.

    Disabling production is an active shutdown: stop production Odoo
    containers before the dedicated PostgreSQL container, preserving every
    container and volume.  Re-enabling does the inverse: ensure PostgreSQL
    first, then start every managed production Odoo container.  Individual
    container failures are logged so production drift never prevents the
    development tier from starting.

    Because init_system runs on every server start, "start every production"
    must fire only on a genuine disabled->enabled transition, not on an
    ordinary restart -- otherwise a production deliberately stopped via
    stop_production (containers use restart_policy=unless-stopped) would be
    resurrected on the next boot.  The disabled path stops the shared PG last,
    so a stopped production PG is the fingerprint of a prior disable: when it
    is already running this is a steady-state restart and per-container
    running state is left to Docker.
    """

    label_filters = [
        f"{settings.managed_label}=true",
        "oduflow.prod=true",
    ]

    if settings.prod_enabled:
        # Capture the transition signal before ensure_prod_infra starts PG.
        was_disabled = not _prod_pg_running(client, settings)
        ensure_prod_infra(client, settings)
        if not was_disabled:
            return
        containers = client.containers.list(all=True, filters={"label": label_filters})
        for container in sorted(containers, key=lambda item: item.name):
            if container.name == settings.prod_db_container:
                continue
            if container.status == "running":
                continue
            try:
                container.start()
                logger.info("Started production container %s", container.name)
            except Exception:
                logger.exception(
                    "Could not start production container %s", container.name
                )
        return

    containers = client.containers.list(all=True, filters={"label": label_filters})
    # PostgreSQL is deliberately last so applications cannot keep writing while
    # the shared production database is being stopped.
    containers.sort(key=lambda item: item.name == settings.prod_db_container)
    for container in containers:
        if container.status != "running":
            continue
        try:
            container.stop()
            logger.info("Stopped disabled production container %s", container.name)
        except Exception:
            logger.exception(
                "Could not stop disabled production container %s", container.name
            )


def ensure_team_tablespace(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> str:
    """Ensure the team's PostgreSQL tablespace exists; return its name.

    The tablespace lives in base_data_dir/pg_tablespaces/team_{id} on the
    host. Hosting setups assign this directory the same XFS project ID as
    team_{id}/, so one disk quota covers the team's files AND its databases.
    Idempotent and cheap (one catalog query) — called before every
    CREATE DATABASE.
    """
    ts_name = get_tablespace_name(team.team_id)
    exists = _exec_sql(
        client,
        settings,
        f"SELECT 1 FROM pg_tablespace WHERE spcname = '{ts_name}';",
    )
    if exists.strip() == "1":
        return ts_name

    host_dir = os.path.join(_pg_tablespaces_host_dir(settings), f"team_{team.team_id}")
    os.makedirs(host_dir, exist_ok=True)
    target = f"{_PG_TABLESPACES_MOUNT}/team_{team.team_id}"
    container = client.containers.get(settings.shared_db_container)
    # PostgreSQL requires the location to be owned by its OS user (postgres,
    # regardless of POSTGRES_USER) with private permissions.
    for cmd in (["chown", "postgres:postgres", target], ["chmod", "700", target]):
        exit_code, output = container.exec_run(cmd, user="root")
        if exit_code != 0:
            out = (
                output.decode("utf-8", errors="replace")
                if isinstance(output, bytes)
                else str(output)
            )
            raise ExternalCommandError(cmd[0], exit_code, out)
    _exec_sql(
        client,
        settings,
        f"CREATE TABLESPACE \"{ts_name}\" LOCATION '{target}';",
    )
    logger.info("Created tablespace %s at %s", ts_name, target)
    return ts_name


def ensure_team_network(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> str:
    """Ensure the team's isolated network exists and infra is attached to it.

    Environment/service containers join only their team network, so one
    tenant's code can never reach another tenant's containers. The shared
    PostgreSQL containers (password-protected, per-env roles; dev and — when
    provisioned — production) and Traefik (in traefik mode) are attached to
    every team network — they are the only cross-team surface. Idempotent
    and cheap.
    """
    net_name = get_team_network_name(team.team_id, settings.prefix)
    try:
        net = client.networks.get(net_name)
    except docker.errors.NotFound:
        net = client.networks.create(
            net_name,
            labels={
                settings.managed_label: "true",
                settings.system_label: "true",
                settings.team_label: team.team_id,
            },
        )
        logger.info("Created network %s", net_name)
        _ensure_iptables_accept(client, net_name)

    # The production DB is lazily provisioned; the get-or-skip below already
    # tolerates its absence.
    infra = [settings.shared_db_container, settings.prod_db_container]
    if settings.routing_mode == "traefik":
        infra.append(settings.traefik_container)
    for container_name in infra:
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            continue
        try:
            net.connect(container)
        except docker.errors.APIError as exc:
            if "already exists" not in str(exc).lower():
                raise
    return net_name


def _managed_pg_network_cidrs(client: DockerClient, settings: Settings) -> list[str]:
    """Return the real IPAM subnets from every Oduflow-managed client network."""
    network_names = {settings.shared_network}
    network_names.update(
        get_team_network_name(team.team_id, settings.prefix)
        for team in settings.teams.values()
    )

    cidrs: list[str] = []
    for network_name in sorted(network_names):
        try:
            network = client.networks.get(network_name)
        except docker.errors.NotFound:
            continue
        attrs = getattr(network, "attrs", {})
        ipam = attrs.get("IPAM", {}) if isinstance(attrs, dict) else {}
        configs = ipam.get("Config", []) if isinstance(ipam, dict) else []
        if not isinstance(configs, list):
            continue
        for config in configs:
            if isinstance(config, dict) and config.get("Subnet"):
                cidrs.append(str(config["Subnet"]))

    try:
        return pg_hba.normalize_cidrs(cidrs)
    except ValueError as exc:
        raise PrerequisiteNotMetError(
            f"Docker reported an invalid network subnet for PostgreSQL: {exc}"
        ) from exc


def _container_output(output: bytes | str) -> str:
    return (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else str(output)
    )


def _install_pg_hba_content(container: Any, hba_path: str, content: str) -> None:
    """Atomically install *content* at the active HBA path in the PG volume."""
    import tempfile

    destination_dir = os.path.dirname(hba_path)
    staged_name = f".pg_hba.oduflow-{uuid.uuid4().hex}"
    staged_path = os.path.join(destination_dir, staged_name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        _copy_file_to_container(
            container,
            tmp_path,
            destination_dir,
            archive_name=staged_name,
        )
        command = [
            "sh",
            "-c",
            "set -eu; "
            f"chown postgres:postgres {shlex.quote(staged_path)}; "
            f"chmod 600 {shlex.quote(staged_path)}; "
            f"mv -f {shlex.quote(staged_path)} {shlex.quote(hba_path)}",
        ]
        exit_code, output = container.exec_run(command, user="root")
        if exit_code != 0:
            raise ExternalCommandError(
                "install pg_hba.conf", exit_code, _container_output(output)
            )
    finally:
        os.remove(tmp_path)
        with contextlib.suppress(Exception):
            container.exec_run(["rm", "-f", staged_path], user="root")


def _pg_hba_errors(
    client: DockerClient,
    settings: Settings,
    *,
    container_name: str,
) -> str:
    return _exec_sql(
        client,
        settings,
        "SELECT COALESCE(string_agg(line_number::text || ': ' || error, "
        "E'\\n' ORDER BY line_number), '') FROM pg_hba_file_rules "
        "WHERE error IS NOT NULL;",
        container_name=container_name,
    )


def _pg_hba_auth_method(
    client: DockerClient,
    settings: Settings,
    *,
    container_name: str,
) -> str:
    """The HBA auth method every existing role can still authenticate with.

    ``password_encryption`` only governs how *new* passwords are hashed, so a
    data volume initialized by a pre-14 PostgreSQL image can report
    ``scram-sha-256`` while its roles still hold md5 verifiers — and a strict
    ``scram-sha-256`` rule locks those roles out. ``md5`` is the compatible
    choice: PostgreSQL upgrades the exchange to SCRAM whenever the stored
    verifier is SCRAM, so it costs nothing once every role has migrated.
    """
    legacy_md5 = _exec_sql(
        client,
        settings,
        "SELECT EXISTS (SELECT 1 FROM pg_authid WHERE rolpassword LIKE 'md5%');",
        container_name=container_name,
    )
    if legacy_md5.strip().lower() in {"t", "true"}:
        return "md5"
    return _exec_sql(
        client,
        settings,
        "SHOW password_encryption;",
        container_name=container_name,
    )


def _reconcile_pg_hba(
    client: DockerClient,
    settings: Settings,
    *,
    container_name: str,
) -> bool:
    """Allow password-authenticated access from the current managed networks."""
    cidrs = _managed_pg_network_cidrs(client, settings)
    if not cidrs:
        raise PrerequisiteNotMetError(
            "No Docker IPAM subnets were found for Oduflow-managed networks"
        )

    hba_path = _exec_sql(
        client,
        settings,
        "SHOW hba_file;",
        container_name=container_name,
    )
    auth_method = _pg_hba_auth_method(
        client,
        settings,
        container_name=container_name,
    )
    container = client.containers.get(container_name)
    exit_code, output = container.exec_run(["cat", hba_path])
    if exit_code != 0:
        raise ExternalCommandError(
            "cat pg_hba.conf", exit_code, _container_output(output)
        )
    current = _container_output(output)

    try:
        candidate = pg_hba.reconcile_managed_block(current, cidrs, auth_method)
    except ValueError as exc:
        raise PrerequisiteNotMetError(
            f"Could not generate PostgreSQL host authentication rules: {exc}"
        ) from exc
    if candidate == current:
        return False

    existing_errors = _pg_hba_errors(client, settings, container_name=container_name)
    if existing_errors:
        raise PrerequisiteNotMetError(
            "The existing pg_hba.conf contains parse errors; refusing to modify it: "
            f"{existing_errors}"
        )

    try:
        _install_pg_hba_content(container, hba_path, candidate)
        _exec_sql(
            client,
            settings,
            "SELECT pg_reload_conf();",
            container_name=container_name,
        )
        new_errors = _pg_hba_errors(client, settings, container_name=container_name)
        if new_errors:
            raise PrerequisiteNotMetError(
                f"Generated pg_hba.conf contains parse errors: {new_errors}"
            )
    except Exception:
        with contextlib.suppress(Exception):
            _install_pg_hba_content(container, hba_path, current)
            _exec_sql(
                client,
                settings,
                "SELECT pg_reload_conf();",
                container_name=container_name,
            )
        raise

    logger.info(
        "Reconciled %s access for Docker networks: %s",
        container_name,
        ", ".join(cidrs),
    )
    return True


def _ensure_iptables_accept(client: DockerClient, network_name: str) -> None:
    try:
        net = client.networks.get(network_name)
        bridge_iface = "br-" + net.id[:12]
    except docker.errors.NotFound:
        return
    try:
        subprocess.run(
            ["iptables", "-C", "INPUT", "-i", bridge_iface, "-j", "ACCEPT"],
            check=True,
            capture_output=True,
        )
        logger.debug("iptables ACCEPT rule already exists for %s", bridge_iface)
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                ["iptables", "-I", "INPUT", "-i", bridge_iface, "-j", "ACCEPT"],
                check=True,
                capture_output=True,
            )
            logger.info("Added iptables ACCEPT rule for interface %s", bridge_iface)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning("Could not add iptables rule for %s: %s", bridge_iface, exc)


def init_system(
    settings: Settings,
) -> dict[str, str]:
    client = get_client()
    logger.info("Initializing system (version %s)", _get_oduflow_version())

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        client.networks.create(settings.shared_network, labels=system_labels)
        logger.info("Created network %s", settings.shared_network)

    _ensure_iptables_accept(client, settings.shared_network)

    _ensure_traefik(client, settings)

    try:
        client.volumes.get(settings.shared_db_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.shared_db_volume, labels=system_labels)
        logger.info("Created volume %s", settings.shared_db_volume)

    _ensure_pg_container(client, settings, system_labels)

    _wait_pg_ready(client, settings)

    for team in settings.teams.values():
        ensure_team_network(client, settings, team)
    _reconcile_pg_hba(
        client,
        settings,
        container_name=settings.shared_db_container,
    )

    # Production hosting is a strict opt-in. Disabled installations stop all
    # managed production workloads without deleting them; enabled installations
    # ensure PostgreSQL first and then start every production Odoo container.
    # Best-effort so production drift never blocks dev environments from starting.
    try:
        reconcile_prod_workloads(client, settings)
    except Exception:
        logger.exception("Production workload reconciliation failed")

    # Per-team coding-agent containers. init_system runs on every server
    # start, so oduflow.toml is applied here: enabled teams get their container
    # ensured (recreated on config drift), disabled teams get a leftover
    # container removed. Best-effort either way.
    from oduflow.docker_ops.env_ops import (
        _ensure_agent_container,
        _remove_agent_container,
    )

    for team in settings.teams.values():
        if team.agent_enabled:
            _ensure_agent_container(client, settings, team)
        else:
            _remove_agent_container(client, settings, team)

    logger.info("System initialized")
    return {"status": "initialized"}


def reload_template(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    dump_path: str | None = None,
    container_dump_path: str | None = None,
    *,
    persist_dump: bool = True,
) -> dict[str, Any]:
    """Rebuild the template database from its dump.

    ``container_dump_path`` names a dump the PostgreSQL container can already
    read (staged in the pg_exchange mount), skipping the copy into its writable
    layer. The caller owns that file; only dumps copied in here are cleaned up.
    ``persist_dump=False`` lets a caller install its staged file atomically after
    this restore succeeds instead of making a second full-size copy here.
    """
    client = get_client()
    tpl_db = get_template_db_name(template_name, team.team_id)
    resolved_dump = dump_path or team.get_template_sql_path(template_name)

    if not os.path.isfile(resolved_dump):
        raise NotFoundError(f"Dump file not found: {resolved_dump}")

    _wait_pg_ready(client, settings)

    is_gzipped = resolved_dump.endswith(".gz")
    use_psql = _is_text_dump(resolved_dump)

    if _db_exists(client, settings, tpl_db):
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{tpl_db}';",
        )
        _exec_sql(client, settings, f'DROP DATABASE "{tpl_db}" WITH (FORCE);')
        logger.info("Dropped template DB %s", tpl_db)

    ts_name = ensure_team_tablespace(client, settings, team)
    _exec_sql(client, settings, f'CREATE DATABASE "{tpl_db}" TABLESPACE "{ts_name}";')

    db_container = client.containers.get(settings.shared_db_container)
    # Every dump copied into the db container's /tmp for pg_restore/psql is a
    # full-size file in the container's writable layer. Track each copy and
    # delete it in the finally below once the restore is done — otherwise every
    # import/reload/refresh leaves the dump behind and the oduflow-db container
    # grows without bound (mirrors the prod seed cleanup in production_ops).
    container_tmp_files: set[str] = set()

    try:
        # The DB container is shared across teams, whose locks do not contend.
        # Use an opaque per-restore name so one restore cannot overwrite or
        # clean up another restore's input file.
        if container_dump_path is None:
            container_dump_name = f"oduflow-restore-{uuid.uuid4().hex}"
            container_dump_path = f"/tmp/{container_dump_name}"
            container_tmp_files.add(container_dump_path)
            _copy_file_to_container(
                db_container,
                resolved_dump,
                "/tmp",
                archive_name=container_dump_name,
            )

        # pg_restore runs with --no-owner so restored objects are owned by the
        # restoring superuser, not by whatever role the dump recorded. For
        # env-derived templates that role is the source env's per-env role
        # (u_<team>_<env>), which is dropped when the env is deleted — keeping it
        # as owner makes deletion fail with "objects depend on it" and leaks an
        # orphan role. create_environment re-assigns ownership to the new per-env
        # role when provisioning from a template. (--no-owner is meaningful only
        # at restore time for archive formats. The psql path for plain-SQL/
        # external dumps is left untouched.)
        restore_tool = "psql" if use_psql else "pg_restore"
        if is_gzipped:
            if use_psql:
                pipeline = f"gunzip -c {container_dump_path} | psql -U {settings.db_user} -d {tpl_db}"
            else:
                pipeline = (
                    f"gunzip -c {container_dump_path} | "
                    f"pg_restore --no-owner -U {settings.db_user} -d {tpl_db}"
                )
            restore_cmd = ["bash", "-c", f"set -o pipefail; {pipeline}"]
        else:
            if use_psql:
                restore_cmd = [
                    "psql",
                    "-U",
                    settings.db_user,
                    "-d",
                    tpl_db,
                    "-f",
                    container_dump_path,
                ]
            else:
                # Parallel restore only here: -j needs a seekable archive, which
                # rules out the gzip pipeline above (pg_restore reads a pipe) and
                # the plain-SQL psql path. Odoo restores are dominated by index
                # and constraint builds, which is exactly what -j parallelises.
                restore_cmd = [
                    "pg_restore",
                    "--no-owner",
                    "-U",
                    settings.db_user,
                    "-d",
                    tpl_db,
                ]
                jobs = _pg_restore_jobs()
                if jobs > 1:
                    restore_cmd += ["-j", str(jobs)]
                restore_cmd.append(container_dump_path)

        logger.info(
            "DB restore started, template_db=%s, dump=%s", tpl_db, resolved_dump
        )
        restore_start = time.monotonic()

        exit_code, output = db_container.exec_run(restore_cmd)

        restore_elapsed = time.monotonic() - restore_start
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )

        if exit_code != 0:
            if restore_tool == "pg_restore" and _is_pg_restore_archive_version_error(
                output_str
            ):
                logger.warning(
                    "pg_restore in %s cannot read dump archive; retrying with %s helper",
                    settings.shared_db_container,
                    _PG_RESTORE_HELPER_IMAGE,
                )
                _exec_sql(
                    client,
                    settings,
                    f'DROP DATABASE IF EXISTS "{tpl_db}" WITH (FORCE);',
                )
                _exec_sql(
                    client,
                    settings,
                    f'CREATE DATABASE "{tpl_db}" TABLESPACE "{ts_name}";',
                )
                helper_start = time.monotonic()
                helper_exit, helper_output, helper_sql_path = (
                    _convert_custom_dump_to_sql_with_helper(
                        client, settings, resolved_dump, is_gzipped=is_gzipped
                    )
                )
                restore_elapsed += time.monotonic() - helper_start
                if helper_exit != 0:
                    exit_code = helper_exit
                    output_str = helper_output
                else:
                    helper_container_name = f"oduflow-restore-{uuid.uuid4().hex}"
                    helper_container_path = f"/tmp/{helper_container_name}"
                    container_tmp_files.add(helper_container_path)
                    _copy_file_to_container(
                        db_container,
                        helper_sql_path,
                        "/tmp",
                        archive_name=helper_container_name,
                    )
                    psql_cmd = [
                        "psql",
                        "-U",
                        settings.db_user,
                        "-d",
                        tpl_db,
                        "-f",
                        helper_container_path,
                    ]
                    psql_start = time.monotonic()
                    exit_code, output = db_container.exec_run(psql_cmd)
                    restore_elapsed += time.monotonic() - psql_start
                    output_str = (
                        output.decode("utf-8")
                        if isinstance(output, bytes)
                        else str(output)
                    )
                    restore_tool = "psql"
                    template_dir = os.path.abspath(team.get_template_dir(template_name))
                    if (
                        exit_code == 0
                        and dump_path is None
                        and os.path.abspath(resolved_dump).startswith(
                            template_dir + os.sep
                        )
                        and os.path.abspath(helper_sql_path)
                        != os.path.abspath(resolved_dump)
                    ):
                        with contextlib.suppress(OSError):
                            os.remove(resolved_dump)

            if exit_code != 0:
                logger.error(
                    "DB restore failed after %.1fs: %s", restore_elapsed, output_str
                )
                raise ExternalCommandError(restore_tool, exit_code, output_str)

        if output_str.strip():
            logger.info("DB restore output: %s", output_str)

        logger.info("DB restore finished in %.1fs", restore_elapsed)

        table_count = _exec_sql(
            client,
            settings,
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';",
            tpl_db,
        ).strip()

        try:
            num_tables = int(table_count)
        except ValueError:
            num_tables = 0

        if num_tables == 0:
            error_msg = (
                f"Dump restore succeeded but no tables found in database {tpl_db}"
            )
            logger.error(error_msg)
            raise ExternalCommandError("restore", 1, error_msg)

        logger.info("Verified %d tables in restored database %s", num_tables, tpl_db)

        if dump_path and persist_dump:
            tpl_dir = team.get_template_dir(template_name)
            os.makedirs(tpl_dir, exist_ok=True)
            base = "dump.sql" if use_psql else "dump.pgdump"
            if is_gzipped:
                base += ".gz"
            dest_path = os.path.join(tpl_dir, base)
            for old_name in (
                "dump.sql",
                "dump.pgdump",
                "dump.sql.gz",
                "dump.pgdump.gz",
            ):
                old_path = os.path.join(tpl_dir, old_name)
                if old_path == dest_path:
                    continue
                if os.path.isfile(old_path):
                    os.remove(old_path)
                    logger.info("Removed old dump %s", old_path)
            if not os.path.exists(dest_path) or not os.path.samefile(
                dump_path, dest_path
            ):
                shutil.copy2(dump_path, dest_path)
            logger.info("Saved dump to workspace: %s", dest_path)

        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=true WHERE datname='{tpl_db}';",
        )

        _update_template_sizes(team, settings, template_name)
        logger.info(
            "Template DB reloaded, template_db=%s, restore_time=%.1fs, tables=%d",
            tpl_db,
            restore_elapsed,
            num_tables,
        )
        return {
            "status": "reloaded",
            "template_db": tpl_db,
            "restore_seconds": round(restore_elapsed, 1),
            "tables": str(num_tables),
            "message": output_str,
        }
    finally:
        # Reclaim the container's writable layer regardless of how the restore
        # exited (success, restore error, or an unexpected failure mid-flight).
        for _leftover in container_tmp_files:
            with contextlib.suppress(Exception):
                db_container.exec_run(["rm", "-f", _leftover])


def init_template(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    odoo_image: str = "odoo:19.0",
    modules: str = "base",
    force: bool = False,
) -> dict[str, str]:
    template_sql_path = team.get_template_sql_path(template_name)
    template_filestore_path = team.get_template_filestore_path(template_name)

    existing_dump = os.path.exists(template_sql_path)
    existing_filestore = os.path.exists(template_filestore_path) and any(
        True for _ in pathlib.Path(template_filestore_path).rglob("*") if _.is_file()
    )

    if (existing_dump or existing_filestore) and not force:
        parts = []
        if existing_dump:
            parts.append(f"dump.sql ({template_sql_path})")
        if existing_filestore:
            parts.append(f"filestore ({template_filestore_path})")
        raise RuntimeError(
            f"Existing data found: {', '.join(parts)}. Use --force to overwrite."
        )

    if force:
        if existing_dump:
            os.remove(template_sql_path)
            logger.info("Removed existing %s", template_sql_path)
        if existing_filestore:
            shutil.rmtree(template_filestore_path)
            logger.info("Removed existing %s", template_filestore_path)

    client = get_client()
    logger.info(
        "Generating template dump from clean Odoo",
        extra={"image": odoo_image, "modules": modules},
    )

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        client.networks.create(settings.shared_network, labels=system_labels)
        logger.info("Created network %s", settings.shared_network)

    try:
        client.volumes.get(settings.shared_db_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.shared_db_volume, labels=system_labels)
        logger.info("Created volume %s", settings.shared_db_volume)

    _ensure_pg_container(client, settings, system_labels)

    _wait_pg_ready(client, settings)

    build_db = "oduflow_template_build"
    temp_container_name = "flow-template-builder"

    if _db_exists(client, settings, build_db):
        _exec_sql(client, settings, f"DROP DATABASE {build_db} WITH (FORCE);")

    _exec_sql(client, settings, f"CREATE DATABASE {build_db};")
    logger.info("Created temporary database %s", build_db)

    try:
        old = client.containers.get(temp_container_name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    logger.info(
        "Starting Odoo container for base init (image=%s, modules=%s)",
        odoo_image,
        modules,
    )
    init_start = time.monotonic()

    volumes = {}
    odoo_conf = _resolve_instance_conf("odoo.conf", team.data_dir)
    if odoo_conf.exists():
        volumes[str(odoo_conf)] = {"bind": "/etc/odoo/odoo.conf", "mode": "ro"}

    temp_container = client.containers.run(
        odoo_image,
        name=temp_container_name,
        detach=True,
        network=ensure_team_network(client, settings, team),
        **default_env_limits(),
        environment={
            "HOST": settings.shared_db_container,
            "USER": settings.db_user,
            "PASSWORD": settings.db_password,
        },
        volumes=volumes,
        command=f"odoo -d {build_db} -i {modules} --stop-after-init --without-demo=all",
        labels={settings.managed_label: "true"},
    )

    exit_info = temp_container.wait(timeout=600)
    init_elapsed = time.monotonic() - init_start
    exit_code = exit_info.get("StatusCode", -1)

    if exit_code != 0:
        logs = temp_container.logs(tail=50).decode("utf-8", errors="replace")
        temp_container.remove(v=True)
        _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
        raise ExternalCommandError(
            "odoo --stop-after-init",
            exit_code,
            f"Odoo init failed after {init_elapsed:.1f}s.\nLast logs:\n{logs}",
        )

    logger.info("Odoo init completed in %.1fs", init_elapsed)

    os.makedirs(os.path.dirname(template_sql_path), exist_ok=True)

    db_container = client.containers.get(settings.shared_db_container)
    dump_cmd = ["pg_dump", "-U", settings.db_user, "-Fp", build_db]
    try:
        _stream_exec_to_file(
            client, db_container, dump_cmd, template_sql_path, tool="pg_dump"
        )
    except ExternalCommandError:
        temp_container.remove(v=True)
        _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
        raise

    logger.info("Dump saved to %s", template_sql_path)

    if os.path.exists(template_filestore_path):
        shutil.rmtree(template_filestore_path)
    os.makedirs(template_filestore_path, exist_ok=True)

    odoo_data_container_path = "/var/lib/odoo/.local/share/Odoo"
    try:
        src_fs_prefix = f"Odoo/filestore/{build_db}/"
        extracted = _extract_archive_from_container(
            temp_container,
            odoo_data_container_path,
            template_filestore_path,
            src_fs_prefix,
        )
        logger.info(
            "Filestore extracted to %s (%d files)", template_filestore_path, extracted
        )

    except docker.errors.NotFound:
        logger.info(
            "Odoo did not create data dir during init (normal for --stop-after-init). "
            "The template filestore at %s is empty; environments will start with an empty filestore.",
            template_filestore_path,
        )

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    uid_str, gid_str = odoo_uid_gid.split(":")
    uid, gid = int(uid_str), int(gid_str)
    chown_recursive(template_filestore_path, uid, gid, client, odoo_image)

    temp_container.remove(v=True)
    logger.info("Temporary container removed")

    _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
    logger.info("Temporary database dropped")

    logger.info("Template generation complete, loading into template DB")
    result = reload_template(settings, team, template_name=template_name)

    metadata: dict[str, Any] = {
        "odoo_image": odoo_image,
        "snapshot_at": _utc_now_iso(),
    }
    from oduflow.docker_ops.env_ops import _dir_size_mb

    fs_size = _dir_size_mb(template_filestore_path)
    metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb
    metadata = _update_template_sizes(team, settings, template_name, metadata)
    logger.info(
        "Template metadata saved (use_overlay=%s, filestore=%.0f MB)",
        metadata["use_overlay"],
        fs_size,
    )

    result["generated_dump"] = template_sql_path
    result["generated_filestore"] = template_filestore_path
    result["filestore_files"] = sum(
        1 for _ in pathlib.Path(template_filestore_path).rglob("*") if _.is_file()
    )
    return result


def _utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _commit_provenance(branch: str, repo_path: str) -> dict[str, str]:
    """Which code a template's database was snapshotted from.

    A template DB is a snapshot of *branch* at a moment in time; without that
    anchor nothing can tell a later environment whether its checkout predates
    the data it was handed. Recorded best-effort — a missing checkout only means
    the lineage check is skipped later (see git_analysis.template_lineage).
    """
    from oduflow.git_ops import is_git_repository, rev_parse

    provenance: dict[str, str] = {"snapshot_at": _utc_now_iso()}
    if branch:
        provenance["source_branch"] = branch
    if is_git_repository(repo_path):
        try:
            provenance["source_commit"] = rev_parse(repo_path)
        except (ExternalCommandError, OSError) as exc:
            logger.debug(
                "No snapshot commit for template source %s: %s", repo_path, exc
            )
    return provenance


def _code_provenance(
    team: TeamSettings, env_name: str, labels: dict[str, Any]
) -> dict[str, str]:
    """Provenance of an environment's checkout, read from its container labels."""
    from oduflow.naming import get_repo_path

    repo_path = labels.get("oduflow.local_path", "") or get_repo_path(
        env_name, team.workspaces_dir
    )
    return _commit_provenance(labels.get("oduflow.git_branch", ""), repo_path)


def _source_env_metadata(settings: Settings, labels: dict[str, Any]) -> dict[str, Any]:
    """Template metadata describing the source environment's code origin.

    A live-mounted environment (``oduflow.local_path`` label) has no repo URL;
    record the path instead so create-from-template can re-establish the
    live-mount when allow_local_path is enabled.
    """
    metadata: dict[str, object] = {"odoo_image": labels.get(settings.image_label, "")}
    live_path = labels.get("oduflow.local_path", "")
    if live_path:
        metadata["local_path"] = live_path
        metadata["repo_url"] = ""
    else:
        metadata["repo_url"] = labels.get(settings.repo_label, "")
    metadata["git_user"] = labels.get("oduflow.git_user", "")
    raw_extras = labels.get("oduflow.extra_addons", "")
    if raw_extras:
        try:
            parsed = json.loads(raw_extras)
            metadata["extra_addons"] = _normalize_extra_addons(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
    raw_auto = labels.get("oduflow.auto_install_modules", "")
    if raw_auto:
        metadata["auto_install_modules"] = raw_auto
    raw_env = labels.get("oduflow.env_vars", "")
    if raw_env:
        try:
            env_vars = normalize_env_vars(json.loads(raw_env))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Could not record env vars on the template: %s", exc)
        else:
            if env_vars:
                metadata["env_vars"] = env_vars
    return metadata


def _snapshot_filestore(
    source: str, dest: str, *, link_dests: list[str]
) -> list[str] | None:
    """Materialise *source* at *dest*, reusing files from *link_dests*.

    *source* is the environment's merged filestore (for an overlay env, the
    fuse-overlayfs mount: template baseline plus the environment's own upper
    deltas, whiteouts already applied). Copying it wholesale copies the entire
    baseline again, which is almost always the bulk of the data and almost
    always unchanged.

    Each ``--link-dest`` directory lets rsync hardlink instead of copy any file
    that already matches there, so only the environment's actual deltas hit the
    disk. Sharing inodes is safe here: Odoo filestore entries are content
    addressed (the name is the checksum) and never rewritten in place, and the
    baseline being linked against is replaced by this snapshot moments later —
    ``rmtree`` only drops one name, the inode survives through the new link.

    Returns the paths rsync transferred, relative to *dest*, or None when the
    whole tree was copied (in which case every file needs its ownership fixed).
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)

    def _full_copy(reason: str) -> None:
        # Never silent: falling back here costs exactly the full-size copy this
        # function exists to avoid, so it must be visible in the logs.
        logger.warning("Filestore snapshot fell back to a full copy: %s", reason)
        shutil.copytree(source, dest)

    if not shutil.which("rsync"):
        _full_copy("rsync not found on PATH")
        return None

    cmd = ["rsync", "-a", "--delete", "--out-format=%n"]
    for link_dest in link_dests:
        if os.path.isdir(link_dest):
            # Relative paths would be resolved against dest, not the cwd.
            cmd.append(f"--link-dest={os.path.abspath(link_dest)}")
    cmd += [source.rstrip("/") + "/", dest.rstrip("/") + "/"]

    os.makedirs(dest, exist_ok=True)
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 24:
        # "Some files vanished before they could be transferred": routine on a
        # live source (a running production unlinks attachments), and rsync has
        # still copied everything else. Falling back to a full copy of a tree
        # that is changing underneath would only fail the same way, later.
        logger.warning(
            "Filestore snapshot: some source files vanished during rsync; "
            "continuing without them"
        )
    elif result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        _full_copy(
            f"rsync exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
        return None

    return [
        line
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line and not line.startswith("deleting ")
    ]


def _baselines_owned_by(link_dests: list[str], uid: int, gid: int) -> bool:
    """Whether the linkable baselines already carry the target ownership.

    A hardlinked file keeps whatever ownership the publish that created it gave
    it, so chowning only the transferred files is complete only while that
    ownership still matches. It stops matching when a team moves to an Odoo
    image with a different uid — rare, but then the whole tree has to be walked
    or the old files would keep the previous uid forever.

    Checking each baseline's root is enough: a mismatch forces the full walk,
    which leaves root and contents consistent again.
    """
    for path in link_dests:
        if not os.path.isdir(path):
            continue
        info = os.stat(path)
        if info.st_uid != uid or info.st_gid != gid:
            logger.info(
                "Baseline %s is owned by %d:%d, not %d:%d; chowning the whole "
                "template filestore",
                path,
                info.st_uid,
                info.st_gid,
                uid,
                gid,
            )
            return False
    return True


def _chown_filestore(
    root: str,
    rel_paths: list[str] | None,
    uid: int,
    gid: int,
    client: DockerClient,
    image: str,
) -> None:
    """Give *root* to *uid*:*gid*, touching only *rel_paths* when they are known.

    Files hardlinked from the previous baseline already carry the right
    ownership — they are the very inodes a previous publish chowned — so only
    the files rsync actually transferred need fixing. With *rel_paths* None
    (full copy fallback) the whole tree is walked as before.
    """
    if rel_paths is None:
        chown_recursive(root, uid, gid, client, image)
        return
    try:
        os.chown(root, uid, gid)
        for rel in rel_paths:
            target = os.path.join(root, rel)
            if _is_within_directory(root, target) and os.path.lexists(target):
                os.chown(target, uid, gid)
    except PermissionError:
        # macOS and other non-root hosts: fall back to the container-based chown.
        chown_recursive(root, uid, gid, client, image)


def _check_template_publish_allowed(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    tpl_db: str,
    *,
    overwrite: bool,
) -> None:
    """Gate a publish into *template_name*.

    Never silently clobber an existing template: publishing over one is a
    deliberate re-baseline, so it must be requested explicitly (overwrite=True).
    Republishing replaces the template (no net growth), so only a brand-new
    template database is charged against the team's quota.
    """
    tpl_db_exists = _db_exists(client, settings, tpl_db)
    if not overwrite and (
        os.path.exists(team.get_template_dir(template_name)) or tpl_db_exists
    ):
        raise ConflictError(
            f"Template '{template_name}' already exists. Choose a new name "
            f"(or pass overwrite=True to re-baseline it)."
        )
    if not tpl_db_exists:
        check_db_quota(client, settings, team)


@contextlib.contextmanager
def _publishing_template_dir(template_dir: str) -> Iterator[str]:
    """Create *template_dir* for a publish and take it back if the publish fails.

    The directory is the first thing a publish leaves behind and the first thing
    anyone checks for, so a dump or restore that failed halfway must not leave
    an empty one: readers would take it for a template. Only a directory this
    publish created is removed — re-baselining an existing template keeps it.
    """
    created = not os.path.isdir(template_dir)
    os.makedirs(template_dir, exist_ok=True)
    try:
        yield template_dir
    except BaseException:
        if created:
            shutil.rmtree(template_dir, ignore_errors=True)
        raise


def _remove_legacy_dumps(template_dir: str) -> None:
    """Drop the dump formats an older template may carry next to ``dump.pgdump``.

    A publish always produces an uncompressed custom-format archive; the
    previous dump is only removed once the new one has been installed.
    """
    for old_name in ("dump.sql", "dump.sql.gz", "dump.pgdump.gz"):
        old_path = os.path.join(template_dir, old_name)
        if os.path.isfile(old_path):
            os.remove(old_path)
            logger.info("Removed old dump %s", old_path)


def _odoo_uid_gid(client: DockerClient, image: str) -> tuple[int, int]:
    uid_str, gid_str = get_odoo_uid_gid(client, image).split(":")
    return int(uid_str), int(gid_str)


def _install_filestore_snapshot(
    client: DockerClient,
    snapshot_dir: str,
    template_filestore_path: str,
    *,
    transferred: list[str] | None,
    link_dests: list[str],
    uid: int,
    gid: int,
    image: str,
    source_label: str,
) -> None:
    """Make *snapshot_dir* the template's filestore baseline, owned by Odoo.

    Runs while the template's overlay consumers are unmounted (inside
    ``env_ops.remount_template_overlays``): this is where the lower layer is
    swapped.
    """
    if os.path.exists(template_filestore_path):
        shutil.rmtree(template_filestore_path)
    os.makedirs(os.path.dirname(template_filestore_path), exist_ok=True)
    try:
        os.rename(snapshot_dir, template_filestore_path)
    except OSError as exc:
        # Both paths live under the team data dir, so this should be a free
        # rename. Anything else (separate mounts, mismatched XFS project IDs)
        # degrades to a full-size copy — say so loudly instead of quietly
        # paying for it.
        logger.warning(
            "Could not move the filestore snapshot into place (%s); copying %s instead",
            exc,
            snapshot_dir,
        )
        shutil.copytree(snapshot_dir, template_filestore_path)
        shutil.rmtree(snapshot_dir)
    logger.info("Template filestore replaced from %s", source_label)

    _chown_filestore(
        template_filestore_path,
        transferred if _baselines_owned_by(link_dests, uid, gid) else None,
        uid,
        gid,
        client,
        image,
    )
    logger.info("Template filestore chowned to %d:%d", uid, gid)


def _finalize_template_metadata(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Decide the overlay mode from the new baseline's size and persist metadata."""
    from oduflow.docker_ops import env_ops

    template_filestore_path = team.get_template_filestore_path(template_name)
    fs_size = (
        env_ops._dir_size_mb(template_filestore_path)
        if os.path.isdir(template_filestore_path)
        else 0.0
    )
    metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb
    metadata = _update_template_sizes(team, settings, template_name, metadata)
    logger.info(
        "Template metadata saved (use_overlay=%s, filestore=%.0f MB)",
        metadata["use_overlay"],
        fs_size,
    )
    return metadata


def template_is_ready(
    settings: Settings, team: TeamSettings, template_name: str
) -> bool:
    """Whether *template_name* is a complete, usable template.

    Metadata is the last artefact a publish writes and the template database is
    what environments are cloned from, so both must exist. A bare template
    directory is not enough: a publish that failed halfway can leave one behind,
    and a stale database can outlive its directory.
    """
    if not os.path.isfile(team.get_template_metadata_path(template_name)):
        return False
    tpl_db = get_template_db_name(template_name, team.team_id)
    return _db_exists(get_client(), settings, tpl_db)


def publish_env_as_template(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    template_name: str,
    *,
    reset_env_changes: bool = False,
    overwrite: bool = False,
) -> dict[str, object]:
    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_db_name, get_filestore_paths, get_resource_name

    client = get_client()
    tpl_db = get_template_db_name(template_name, team.team_id)
    env_db = get_db_name(env_name, team.team_id)

    if not _db_exists(client, settings, env_db):
        raise NotFoundError(
            f"Database '{env_db}' for environment '{env_name}' not found."
        )
    _check_template_publish_allowed(
        client, settings, team, template_name, tpl_db, overwrite=overwrite
    )
    _wait_pg_ready(client, settings)

    template_dir = team.get_template_dir(template_name)
    dump_path = os.path.join(template_dir, "dump.pgdump")
    with _publishing_template_dir(template_dir):
        # 1-2. pg_dump the branch DB, then rebuild the template DB from it, and
        # only then move the dump into the template dir — a failed restore
        # leaves no half-published template behind. Ownership is stripped at
        # restore time (pg_restore --no-owner in reload_template), NOT here:
        # --no-owner is ignored by pg_dump for the -Fc archive format.
        logger.info("Dumping branch database %s", env_db)
        with _staged_db_dump(client, settings, team, env_db, dump_path) as (
            staged_dump,
            container_dump,
        ):
            logger.info("Branch dump saved to %s", staged_dump)
            reload_template(
                settings,
                team,
                template_name=template_name,
                dump_path=staged_dump,
                container_dump_path=container_dump,
                persist_dump=False,
            )
        _remove_legacy_dumps(template_dir)

        # ------------------------------------------------------------------
        # 3-7. Swap the template's lower filestore non-destructively (issue #2).
        # ------------------------------------------------------------------
        # The SOURCE env's deltas become the new lower layer, so it is always
        # reset. OTHER overlay envs on this template keep their upper deltas by
        # default; reset_env_changes=True resets them to the new baseline.
        env_paths = get_filestore_paths(env_name, team.workspaces_dir)
        branch_merged = env_paths["merged"]
        template_filestore_path = team.get_template_filestore_path(template_name)
        source_container = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )
        source_is_overlay = os.path.isdir(branch_merged) and os.path.ismount(
            branch_merged
        )

        source_template = ""
        try:
            sc = client.containers.get(source_container)
            source_was_running = sc.status == "running"
            source_image = sc.image.tags[0] if sc.image.tags else "odoo:19.0"
            source_template = sc.labels.get("oduflow.template", "") or ""
        except (docker.errors.NotFound, IndexError):
            source_was_running = False
            source_image = "odoo:19.0"

        # Snapshot the source env's merged filestore while it is still mounted.
        # Link against the baseline the env is currently mounted on (where
        # nearly every file matches) and against the template being published
        # into, which differ when an env from one template is published under a
        # new name. An env created without a template carries the literal
        # "none" label (env_ops._env_labels), which is a sentinel, not a
        # template to link against.
        candidates = (
            source_template if source_template != "none" else "",
            template_name,
        )
        link_dests = [
            team.get_template_filestore_path(name)
            for name in dict.fromkeys(filter(None, candidates))
        ]
        snapshot_dir: str | None = None
        transferred: list[str] | None = None
        if os.path.isdir(branch_merged):
            snapshot_dir = branch_merged + "_snapshot"
            transferred = _snapshot_filestore(
                branch_merged, snapshot_dir, link_dests=link_dests
            )
            logger.info("Snapshot of filestore created for env %s", env_name)
        else:
            logger.warning(
                "Branch filestore %s not found, skipping filestore update",
                branch_merged,
            )

        # Resolved before the remount block: a cache miss runs (and may pull) the
        # image, which must not extend the time dependent envs are unmounted.
        uid, gid = _odoo_uid_gid(client, source_image)

        try:
            with env_ops.remount_template_overlays(
                client,
                settings,
                team,
                template_name,
                reset_upper=reset_env_changes,
                exclude_envs=(env_name,),
            ) as remount:
                # Unmount the source overlay (after snapshot) so its lower can
                # change.
                if source_is_overlay:
                    try:
                        client.containers.get(source_container).stop(timeout=10)
                    except docker.errors.NotFound:
                        pass
                    env_ops._unmount_filestore(env_name, team)
                    env_ops._wait_unmounted(branch_merged)

                if snapshot_dir and os.path.isdir(snapshot_dir):
                    _install_filestore_snapshot(
                        client,
                        snapshot_dir,
                        template_filestore_path,
                        transferred=transferred,
                        link_dests=link_dests,
                        uid=uid,
                        gid=gid,
                        image=source_image,
                        source_label=f"env {env_name}",
                    )

                # Remount the source overlay against the new lower (always
                # reset) + restart.
                if source_is_overlay:
                    for key in ("upper", "work"):
                        d = env_paths[key]
                        if os.path.isdir(d):
                            shutil.rmtree(d)
                            os.makedirs(d, mode=0o777, exist_ok=True)
                    env_ops._mount_filestore(
                        client,
                        settings,
                        team,
                        env_name,
                        get_db_name(env_name, team.team_id),
                        source_image,
                        {},
                        template_name=template_name,
                        force_overlay=True,
                    )
                    if source_was_running:
                        try:
                            client.containers.get(source_container).start()
                        except (docker.errors.NotFound, docker.errors.APIError):
                            pass
        finally:
            # A snapshot that never made it into place is a full-size orphan
            # next to the environment's filestore.
            if snapshot_dir and os.path.isdir(snapshot_dir):
                shutil.rmtree(snapshot_dir, ignore_errors=True)

        # Save template metadata from source environment. `source_container` is
        # the team-scoped container name computed above — a hand-built
        # pre-migration name here used to miss the container and silently drop
        # the metadata (env_vars, repo_url, odoo_image, ...) from every
        # published template.
        metadata: dict[str, Any] = {}
        try:
            pc = client.containers.get(source_container)
            metadata = _source_env_metadata(settings, pc.labels)
            metadata.update(_code_provenance(team, env_name, pc.labels))
        except docker.errors.NotFound:
            metadata = {"snapshot_at": _utc_now_iso()}
        _finalize_template_metadata(settings, team, template_name, metadata)

    return {
        "status": "promoted",
        "env_name": env_name,
        "dump": dump_path,
        "filestore": template_filestore_path,
        "template_db": tpl_db,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        "reset_env_changes": reset_env_changes,
    }


def publish_production_as_template(
    settings: Settings,
    team: TeamSettings,
    prod_name: str,
    template_name: str,
    *,
    reset_env_changes: bool = False,
    overwrite: bool = False,
) -> dict[str, object]:
    """Publish a production's database and filestore as a dev template.

    The reverse of seeding a production from a template: the production DB is
    dumped out of the dedicated production cluster and restored into the dev
    cluster as the template database, and the production's plain filestore
    directory becomes the template's lower layer.

    The production keeps running throughout — ``pg_dump`` takes a consistent
    snapshot without stopping it, the same tradeoff ``backup_ops`` already makes
    for scheduled production snapshots.

    This layer deliberately does NOT consult the production's
    ``allow_copy_to_dev_mcp`` flag: that flag gates agent-initiated copies at
    the MCP tool layer only, and the dashboard calls straight in here.
    """
    # production_ops imports this module at import time, so importing it (and
    # the registry, which it owns) back at module level would be a cycle.
    from oduflow import production_registry
    from oduflow.docker_ops import env_ops, production_ops
    from oduflow.naming import get_repo_path, prod_env_name

    client = get_client()
    record = production_registry.get_production(team, prod_name)
    tpl_db = get_template_db_name(template_name, team.team_id)
    prod_db = production_ops.prod_db_name(team, prod_name)

    try:
        client.containers.get(settings.prod_db_container)
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"Production PostgreSQL container '{settings.prod_db_container}' "
            "not found. Production hosting is not initialized on this host."
        ) from None
    if not _db_exists(
        client, settings, prod_db, container_name=settings.prod_db_container
    ):
        raise NotFoundError(
            f"Database '{prod_db}' for production '{prod_name}' not found in "
            f"the production cluster."
        )
    _check_template_publish_allowed(
        client, settings, team, template_name, tpl_db, overwrite=overwrite
    )
    _wait_pg_ready(client, settings)
    _wait_pg_ready(client, settings, container_name=settings.prod_db_container)

    template_dir = team.get_template_dir(template_name)
    dump_path = os.path.join(template_dir, "dump.pgdump")
    with _publishing_template_dir(template_dir):
        # The dump is streamed out of the production cluster straight to its
        # staging file (nothing lands in the production container's writable
        # layer) and restored into the dev cluster; only once that succeeded
        # does it become the template's dump.
        logger.info("Dumping production database %s", prod_db)
        with _staged_db_dump(
            client,
            settings,
            team,
            prod_db,
            dump_path,
            container_name=settings.prod_db_container,
        ) as (staged_dump, container_dump):
            logger.info("Production dump staged at %s", staged_dump)
            reload_template(
                settings,
                team,
                template_name=template_name,
                dump_path=staged_dump,
                container_dump_path=container_dump,
                persist_dump=False,
            )
        _remove_legacy_dumps(template_dir)

        # A production filestore is a plain directory (never an overlay), so
        # there is no source overlay to unmount and remount here — only the
        # template's own consumers need the swap protection.
        template_filestore_path = team.get_template_filestore_path(template_name)
        prod_filestore = production_ops.prod_filestore_dir(team, prod_name)
        link_dests = [template_filestore_path]
        snapshot_dir: str | None = None
        transferred: list[str] | None = None
        if os.path.isdir(prod_filestore):
            snapshot_dir = prod_filestore.rstrip("/") + "_snapshot"
            transferred = _snapshot_filestore(
                prod_filestore, snapshot_dir, link_dests=link_dests
            )
            logger.info("Snapshot of filestore created for production %s", prod_name)
        else:
            logger.warning(
                "Production filestore %s not found, skipping filestore update",
                prod_filestore,
            )

        odoo_image = record.get("odoo_image", "")
        source_image = odoo_image or "odoo:19.0"
        uid, gid = _odoo_uid_gid(client, source_image)

        try:
            with env_ops.remount_template_overlays(
                client,
                settings,
                team,
                template_name,
                reset_upper=reset_env_changes,
            ) as remount:
                if snapshot_dir and os.path.isdir(snapshot_dir):
                    _install_filestore_snapshot(
                        client,
                        snapshot_dir,
                        template_filestore_path,
                        transferred=transferred,
                        link_dests=link_dests,
                        uid=uid,
                        gid=gid,
                        image=source_image,
                        source_label=f"production {prod_name}",
                    )
        finally:
            if snapshot_dir and os.path.isdir(snapshot_dir):
                shutil.rmtree(snapshot_dir, ignore_errors=True)

        # A production carries no oduflow.* code labels: repo, image, git user
        # and extra addons come from the registry record, the commit from the
        # production checkout. source_production marks the template as
        # production-derived for everything downstream.
        metadata: dict[str, Any] = {
            "odoo_image": odoo_image,
            "repo_url": record.get("repo_url", ""),
            "git_user": record.get("git_user", ""),
            "source_production": prod_name,
        }
        extras = _normalize_extra_addons(record.get("extra_addons") or {})
        if extras:
            metadata["extra_addons"] = extras
        metadata.update(
            _commit_provenance(
                record.get("branch", ""),
                get_repo_path(prod_env_name(prod_name), team.workspaces_dir),
            )
        )
        _finalize_template_metadata(settings, team, template_name, metadata)

    return {
        "status": "promoted",
        "prod_name": prod_name,
        "dump": dump_path,
        "filestore": template_filestore_path,
        "template_db": tpl_db,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        "reset_env_changes": reset_env_changes,
    }


def refresh_template(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    *,
    reset_env_changes: bool = False,
) -> dict[str, object]:
    """Re-apply a template's current on-disk filestore to live overlay envs.

    Non-destructive by default: each affected environment is unmounted and
    remounted against the template's current lower layer while keeping its
    ``upper`` deltas. Pass ``reset_env_changes=True`` to discard those deltas
    and reset every affected environment to the template baseline.

    Useful after the template filestore was changed on disk, or to re-sync an
    environment that was busy/skipped during an import or save.
    """
    from oduflow.docker_ops import env_ops

    client = get_client()
    tpl_db = get_template_db_name(template_name, team.team_id)

    with env_ops.remount_template_overlays(
        client,
        settings,
        team,
        template_name,
        reset_upper=reset_env_changes,
    ) as remount:
        # The unmount→remount cycle performed by the context manager is itself
        # the operation; nothing to mutate here.
        pass

    return {
        "status": "refreshed",
        "template_name": template_name,
        "template_db": tpl_db,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        "reset_env_changes": reset_env_changes,
    }


def attach_filestore(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    source: str,
    *,
    reset_env_changes: bool = False,
    strip_prefix: str = "auto",
) -> dict[str, object]:
    """Attach or replace a template filestore from a directory, rsync source, or archive."""
    from oduflow.docker_ops import env_ops

    validate_template_name(template_name)
    client = get_client()
    tpl_dir = team.get_template_dir(template_name)
    tpl_db = get_template_db_name(template_name, team.team_id)
    if not os.path.isdir(tpl_dir) and not _db_exists(client, settings, tpl_db):
        raise NotFoundError(f"Template '{template_name}' not found.")
    os.makedirs(tpl_dir, exist_ok=True)

    staging_root = os.path.join(
        team.data_dir,
        ".attach_filestore",
        template_name.replace("/", "-"),
        str(time.time_ns()),
    )
    raw_dir = os.path.join(staging_root, "raw")
    prepared_dir = os.path.join(staging_root, "prepared")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(prepared_dir, exist_ok=True)

    try:
        try:
            file_count, detected_prefix, source_kind = _stage_filestore_source(
                source, raw_dir, prepared_dir, strip_prefix
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            raise ExternalCommandError("rsync", exc.returncode, stderr or stdout)

        metadata_path = team.get_template_metadata_path(template_name)
        metadata: dict[str, object] = {}
        if os.path.isfile(metadata_path):
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)
            except (OSError, json.JSONDecodeError):
                metadata = {}

        target_filestore = team.get_template_filestore_path(template_name)
        previous_filestore = os.path.join(staging_root, "previous")
        with env_ops.remount_template_overlays(
            client,
            settings,
            team,
            template_name,
            reset_upper=reset_env_changes,
        ) as remount:
            had_previous = os.path.exists(target_filestore)
            os.makedirs(os.path.dirname(target_filestore), exist_ok=True)
            if had_previous:
                # Keep the old lower layer until the prepared replacement is
                # installed. Removing it first turns an otherwise recoverable
                # rename error into a template with no filestore at all.
                os.replace(target_filestore, previous_filestore)
            if os.path.exists(target_filestore):
                shutil.rmtree(target_filestore, ignore_errors=True)
            try:
                os.replace(prepared_dir, target_filestore)
            except BaseException:
                if had_previous and os.path.exists(previous_filestore):
                    os.replace(previous_filestore, target_filestore)
                raise

            odoo_image = str(metadata.get("odoo_image") or "")
            if odoo_image:
                try:
                    uid_gid = get_odoo_uid_gid(client, odoo_image)
                    uid_str, gid_str = uid_gid.split(":")
                    chown_recursive(
                        target_filestore,
                        int(uid_str),
                        int(gid_str),
                        client,
                        odoo_image,
                    )
                    logger.info("Template filestore chowned to %s", uid_gid)
                except Exception as exc:  # noqa: BLE001 - chown is best-effort
                    logger.warning("Could not chown template filestore: %s", exc)

            if os.path.exists(previous_filestore):
                shutil.rmtree(previous_filestore, ignore_errors=True)

        metadata["includes_filestore"] = True
        metadata["use_overlay"] = None
        metadata = _update_template_sizes(team, settings, template_name, metadata)

        return {
            "status": "attached",
            "template_name": template_name,
            "source": source,
            "source_kind": source_kind,
            "strip_prefix": detected_prefix,
            "filestore": target_filestore,
            "filestore_files": file_count,
            "filestore_size_mb": metadata.get("filestore_size_mb", 0.0),
            "use_overlay": metadata.get("use_overlay"),
            "affected_envs": remount.affected,
            "remount_failures": remount.failures,
            "reset_env_changes": reset_env_changes,
        }
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def destroy_system(settings: Settings) -> dict[str, str]:
    client = get_client()
    logger.info("Destroying system")

    filters = {"label": [f"{settings.managed_label}=true"]}
    containers = client.containers.list(all=True, filters=filters)
    system_names = {settings.shared_db_container, settings.traefik_container}
    env_containers = [
        c
        for c in containers
        if c.name.startswith(settings.prefix)
        and c.labels.get(settings.branch_label)
        and c.name not in system_names
    ]
    svc_containers = [
        c
        for c in containers
        if c.name.startswith(settings.prefix)
        and c.labels.get("oduflow.service")
        and c.name not in system_names
    ]
    blocking = env_containers + svc_containers
    if blocking:
        names = [c.name for c in blocking]
        from oduflow.errors import ConflictError

        if svc_containers and not env_containers:
            raise ConflictError(
                f"Active environments/services exist: {', '.join(names)}. Delete them first."
            )
        elif env_containers and not svc_containers:
            raise ConflictError(
                f"Active environments exist: {', '.join(names)}. Delete them first."
            )
        else:
            raise ConflictError(
                f"Active environments/services exist: {', '.join(names)}. Delete them first."
            )

    removed: list[str] = []

    # Per-team agent containers and their volumes. They must go before the
    # team networks (a running agent container keeps its network busy); destroy
    # is full teardown, so the home/workspace volumes go too.
    from oduflow.naming import (
        get_agent_container_name,
        get_agent_home_volume_name,
        get_agent_workspace_volume_name,
    )

    for team in settings.teams.values():
        agent_name = get_agent_container_name(team.team_id, settings.prefix)
        try:
            client.containers.get(agent_name).remove(force=True)
            removed.append(agent_name)
        except docker.errors.NotFound:
            pass
        for volume_name in (
            get_agent_home_volume_name(team.team_id, settings.prefix),
            get_agent_workspace_volume_name(team.team_id, settings.prefix),
        ):
            try:
                client.volumes.get(volume_name).remove()
                removed.append(volume_name)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError:
                logger.warning("Could not remove volume %s", volume_name)

    _destroy_traefik(client, settings, removed)

    try:
        db = client.containers.get(settings.shared_db_container)
        db.stop()
        db.remove(v=True)
        removed.append(settings.shared_db_container)
    except docker.errors.NotFound:
        pass

    try:
        vol = client.volumes.get(settings.shared_db_volume)
        vol.remove()
        removed.append(settings.shared_db_volume)
    except docker.errors.NotFound:
        pass

    try:
        for extra_net in client.networks.list(
            filters={"label": f"{settings.managed_label}=true"}
        ):
            if extra_net.name == settings.shared_network:
                continue
            try:
                extra_net.remove()
                removed.append(extra_net.name)
            except docker.errors.APIError:
                logger.warning("Could not remove network %s", extra_net.name)
        net = client.networks.get(settings.shared_network)
        net.remove()
        removed.append(settings.shared_network)
    except docker.errors.NotFound:
        pass

    logger.info("System destroyed, removed=%s", removed)
    return {"status": "destroyed", "removed": ", ".join(removed)}


def import_from_odoo(
    settings: Settings,
    team: TeamSettings,
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "",
    without_filestore: bool = False,
) -> dict[str, object]:
    """Import a template from a running Odoo instance via its database manager API.

    Downloads a full ZIP backup or DB-only custom dump, saves metadata.json,
    and loads the dump into PostgreSQL as a template DB.
    """
    import urllib.request
    import zipfile

    from oduflow.url_safety import assert_allowed_url

    base = odoo_url.rstrip("/")
    validate_template_name(template_name)
    template_dir = team.get_template_dir(template_name)
    tpl_db = get_template_db_name(template_name, team.team_id)

    # SSRF guard: block loopback and the cloud metadata endpoint. Internal
    # RFC1918 hosts stay allowed (allow_private=True) because operator-managed
    # Odoo instances commonly live on a private LAN.
    # HTTP is allowed for operator-managed Odoo instances, although it sends the
    # master password without transport encryption.
    assert_allowed_url(base, require_https=False, allow_private=True)

    if os.path.exists(template_dir):
        raise ConflictError(f"Template directory already exists: {template_dir}")

    client = get_client()
    if _db_exists(client, settings, tpl_db):
        raise ConflictError(f"Template database already exists: {tpl_db}")

    # Gate on the DB quota before downloading a potentially huge backup.
    check_db_quota(client, settings, team)

    # 1. Resolve database name
    if not db_name:
        req = urllib.request.Request(
            f"{base}/web/database/list",
            data=json.dumps(
                {"jsonrpc": "2.0", "method": "call", "params": {}}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        databases = body.get("result", [])
        if not databases:
            raise NotFoundError(f"No databases found on {base}")
        if len(databases) > 1:
            raise PrerequisiteNotMetError(
                f"Multiple databases found: {', '.join(databases)}. "
                f"Specify db_name explicitly."
            )
        db_name = databases[0]
        logger.info("Auto-detected database: %s", db_name)

    # 2. Download backup
    logger.info("Downloading backup from %s (db=%s)...", base, db_name)
    boundary = "----OduflowBoundary"
    backup_format = "dump" if without_filestore else "zip"
    parts = []
    for field_name, field_value in [
        ("master_pwd", master_pwd),
        ("name", db_name),
        ("backup_format", backup_format),
    ]:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
            f"{field_value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    multipart_body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/web/database/backup",
        data=multipart_body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    download_start = time.monotonic()
    tmp_backup = os.path.join(team.data_dir, f"tmp_odoo_backup.{backup_format}")
    os.makedirs(team.data_dir, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "zip" not in content_type and "octet" not in content_type:
                body = resp.read(2000).decode("utf-8", errors="replace")
                raise ExternalCommandError(
                    "odoo backup",
                    -1,
                    f"Unexpected response (Content-Type: {content_type}): {body}",
                )
            with open(tmp_backup, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        body = e.read(2000).decode("utf-8", errors="replace")
        raise ExternalCommandError("odoo backup", e.code, f"HTTP {e.code}: {body}")

    download_elapsed = time.monotonic() - download_start
    backup_size_mb = os.path.getsize(tmp_backup) / (1024 * 1024)
    logger.info(
        "Backup downloaded in %.1fs (%.1f MB)", download_elapsed, backup_size_mb
    )

    # 3. Stage dump/filestore files
    from oduflow.docker_ops import env_ops

    template_sql_path = os.path.join(
        template_dir, "dump.pgdump" if without_filestore else "dump.sql"
    )
    template_filestore_path = team.get_template_filestore_path(template_name)

    os.makedirs(template_dir, exist_ok=True)

    manifest = {}
    affected_envs: list[str] = []
    remount_failures: list[tuple[str, str]] = []
    try:
        # Swap the template's filestore (the overlay lower layer) non-destructively:
        # live overlay envs are unmounted (keeping their upper deltas) and remounted
        # against the new lower on exit. See issue #2.
        with env_ops.remount_template_overlays(
            client, settings, team, template_name
        ) as remount:
            if without_filestore:
                shutil.copy2(tmp_backup, template_sql_path)
                logger.info("Saved DB-only dump to %s", template_sql_path)
            else:
                with zipfile.ZipFile(tmp_backup, "r") as zf:
                    # Extract manifest.json
                    if "manifest.json" in zf.namelist():
                        with zf.open("manifest.json") as mf:
                            manifest = json.load(mf)

                    # Extract dump.sql
                    with (
                        zf.open("dump.sql") as src,
                        open(template_sql_path, "wb") as dst,
                    ):
                        shutil.copyfileobj(src, dst)
                    logger.info("Extracted dump.sql to %s", template_sql_path)

                    # Extract filestore
                    if os.path.exists(template_filestore_path):
                        shutil.rmtree(template_filestore_path)
                    os.makedirs(template_filestore_path, exist_ok=True)

                    fs_prefix = "filestore/"
                    for member in zf.namelist():
                        if not member.startswith(fs_prefix):
                            continue
                        rel = member[len(fs_prefix) :]
                        if not rel:
                            continue
                        # Skip checklist/ symlink-like entries
                        if rel.startswith("checklist/"):
                            continue
                        target = os.path.join(template_filestore_path, rel)
                        # Reject any member that escapes the filestore dir (zip-slip).
                        if not _is_within_directory(template_filestore_path, target):
                            logger.warning(
                                "Skipping unsafe archive member outside filestore: %s",
                                member,
                            )
                            continue
                        if member.endswith("/"):
                            os.makedirs(target, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)

                    fs_count = sum(
                        1
                        for f in pathlib.Path(template_filestore_path).rglob("*")
                        if f.is_file()
                    )
                    logger.info(
                        "Extracted filestore to %s (%d files)",
                        template_filestore_path,
                        fs_count,
                    )

            # chown the new lower layer so the odoo user can read it through the
            # overlay once it is remounted.
            major = manifest.get("major_version", "")
            if major:
                try:
                    uid_gid = get_odoo_uid_gid(client, f"odoo:{major}")
                    uid_str, gid_str = uid_gid.split(":")
                    chown_recursive(
                        template_filestore_path,
                        int(uid_str),
                        int(gid_str),
                        client,
                        f"odoo:{major}",
                    )
                    logger.info("Template filestore chowned to %s", uid_gid)
                except Exception as exc:  # noqa: BLE001 - chown is best-effort
                    logger.warning("Could not chown template filestore: %s", exc)
        affected_envs = remount.affected
        remount_failures = remount.failures
    finally:
        if os.path.exists(tmp_backup):
            os.remove(tmp_backup)

    # 4. Determine Odoo image from manifest
    major_version = manifest.get("major_version", "")
    odoo_image = f"odoo:{major_version}" if major_version else ""

    # 5. Save metadata.json
    metadata = {
        "odoo_image": odoo_image,
        "repo_url": "",
        "source_url": base,
        "source_db": db_name,
        "odoo_version": manifest.get("version", ""),
        "pg_version": manifest.get("pg_version", ""),
        "modules": manifest.get("modules", {}),
        "includes_filestore": not without_filestore,
        # No source_commit: an imported database has no branch of ours behind
        # it, so the lineage check has nothing to compare against and is skipped.
        "snapshot_at": _utc_now_iso(),
    }
    _update_template_sizes(team, settings, template_name, metadata)
    logger.info("Metadata saved for template %s", template_name)

    # 6. Load dump into PostgreSQL
    result = reload_template(settings, team, template_name=template_name)
    if without_filestore:
        manifest = _read_template_manifest_from_db(
            client, settings, str(result["template_db"])
        )
        major_version = manifest.get("major_version", "")
        odoo_image = f"odoo:{major_version}" if major_version else ""
        metadata.update(
            {
                "odoo_image": odoo_image,
                "odoo_version": manifest.get("version", ""),
                "pg_version": manifest.get("pg_version", ""),
                "modules": manifest.get("modules", {}),
            }
        )
        _update_template_sizes(team, settings, template_name, metadata)
        logger.info(
            "Metadata updated from restored template DB %s", result["template_db"]
        )

    return {
        "status": "imported",
        "template_name": template_name,
        "source_url": base,
        "source_db": db_name,
        "odoo_image": odoo_image,
        "odoo_version": manifest.get("version", ""),
        "template_db": result["template_db"],
        "restore_seconds": result.get("restore_seconds", 0),
        "zip_size_mb": round(backup_size_mb, 1),
        "includes_filestore": not without_filestore,
        "affected_envs": affected_envs,
        "remount_failures": remount_failures,
    }


def extract_filestore_tar(tar_path: str, dest_dir: str) -> int:
    """Extract a tar stream into ``dest_dir``.

    Members that would escape the destination (zip-slip) or are not regular
    files/dirs are skipped. Returns the number of files written.
    """
    os.makedirs(dest_dir, exist_ok=True)
    written = 0
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf:
            target = os.path.join(dest_dir, member.name)
            if not _is_within_directory(dest_dir, target):
                logger.warning(
                    "Skipping unsafe tar member outside filestore: %s", member.name
                )
                continue
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written += 1
            # symlinks/devices/etc. are intentionally ignored — Odoo filestores
            # contain only regular files and directories.
    return written


def extract_filestore_chunk(tar_path: str, filestore_dir: str, chunk: str) -> int:
    """Atomically extract one filestore hash-dir tar (``<chunk>/...``) into
    ``filestore_dir``.

    The tar is unpacked into a temporary sibling directory first and the chunk
    is moved into place with a rename only once extraction completed, so a
    truncated/corrupt upload never leaves a half-extracted ``<chunk>/`` behind —
    resume (which treats a present chunk dir as complete) stays truthful.
    Returns the number of files written.
    """
    os.makedirs(filestore_dir, exist_ok=True)
    tmp_root = os.path.join(filestore_dir, f".incoming_{chunk}")
    if os.path.exists(tmp_root):
        shutil.rmtree(tmp_root)
    try:
        written = extract_filestore_tar(tar_path, tmp_root)
        extracted = os.path.join(tmp_root, chunk)
        if not os.path.isdir(extracted):
            raise ExternalCommandError(
                "import filestore",
                1,
                f"Archive does not contain the expected '{chunk}/' directory",
            )
        final = os.path.join(filestore_dir, chunk)
        if os.path.exists(final):
            shutil.rmtree(final)
        os.rename(extracted, final)
    finally:
        if os.path.exists(tmp_root):
            shutil.rmtree(tmp_root)
    return written


def extract_addon_dir(tar_path: str, addons_dir: str, name: str) -> int:
    """Atomically extract one addon-repo tar (``<name>/...``) into ``addons_dir``.

    Mirrors :func:`extract_filestore_chunk`: unpack into a temporary sibling,
    verify the expected top-level ``<name>/`` directory is present, then rename
    into place, so a truncated upload never leaves a half-extracted addon behind
    (resume treats a present addon dir as complete). Returns files written.
    """
    os.makedirs(addons_dir, exist_ok=True)
    tmp_root = os.path.join(addons_dir, f".incoming_{name}")
    if os.path.exists(tmp_root):
        shutil.rmtree(tmp_root)
    cleanup: str | None = tmp_root
    try:
        written = extract_filestore_tar(tar_path, tmp_root)
        entries = [e for e in os.listdir(tmp_root) if not e.startswith(".")]
        dirs = [e for e in entries if os.path.isdir(os.path.join(tmp_root, e))]
        # The client tars the repo directory (top-level "<repodir>/..."), so a
        # single top-level dir is the addon root; otherwise (a tar of the repo's
        # bare contents) tmp_root itself is the root. Either way it lands as
        # addons/<name>.
        if len(entries) == 1 and len(dirs) == 1:
            src = os.path.join(tmp_root, dirs[0])
        else:
            src = tmp_root
        final = os.path.join(addons_dir, name)
        if os.path.exists(final):
            shutil.rmtree(final)
        os.rename(src, final)
        if src == tmp_root:
            cleanup = None  # renamed away, nothing to clean
    finally:
        if cleanup and os.path.exists(cleanup):
            shutil.rmtree(cleanup)
    return written


def _wire_imported_addons(
    team: TeamSettings,
    staging_dir: str,
    major_version: str,
    *,
    addon_error_policy: str = "strict",
    addon_warnings: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """Turn staged Odoo.sh addons into Oduflow extra-addons repos.

    Reads ``addons.json`` (a list of ``{name, kind, branch, origin_url}``) from
    the staging dir. For each entry: ``kind == "remote"`` is cloned from its
    origin (updatable via update_extra_repo); everything else is seeded as a
    local (remote-less) repo from ``addons/<name>/``. A repo that already exists
    is reused only when the requested branch exists. Every declared addon must
    be usable in ``strict`` mode. ``best_effort`` uses a staged local fallback
    when possible and otherwise skips only the failing addon, recording every
    fallback or skip in ``addon_warnings``.
    """
    from oduflow import extra_addons

    if addon_error_policy not in {"strict", "best_effort"}:
        raise ValueError("addon_error_policy must be 'strict' or 'best_effort'.")
    best_effort = addon_error_policy == "best_effort"
    warnings = addon_warnings if addon_warnings is not None else []

    def record_warning(name: str, action: str, reason: str) -> None:
        warnings.append({"name": name, "action": action, "reason": reason})
        logger.warning(
            "Imported addon '%s': %s (%s)",
            name,
            action.replace("_", " "),
            reason,
        )

    manifest = os.path.join(staging_dir, "addons.json")
    if not os.path.isfile(manifest):
        return {}
    try:
        with open(manifest) as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            raise PrerequisiteNotMetError(
                "The staged addons manifest must contain a JSON list."
            )
    except (OSError, ValueError):
        raise PrerequisiteNotMetError(
            "Could not read the staged addons manifest."
        ) from None

    addons_src = os.path.join(staging_dir, "addons")
    wired: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        kind = str(entry.get("kind") or "local").strip()
        branch = str(entry.get("branch") or "").strip()
        if branch in ("", "HEAD"):
            branch = major_version or "import"
        origin_url = str(entry.get("origin_url") or "").strip()
        repo_path = os.path.join(team.shared_repos_dir, name)

        # Already registered (e.g. a re-run, or the user added it manually):
        # reference it only if the requested branch is genuinely available.
        if os.path.isdir(repo_path):
            try:
                extra_addons._resolve_branch_revision(repo_path, name, branch)
            except Exception as exc:
                if not best_effort:
                    raise
                record_warning(name, "skipped", str(exc))
                continue
            wired[name] = branch
            logger.info("Extra repo '%s' already exists; referencing it", name)
            continue

        src_dir = os.path.join(addons_src, name)
        local_fallback_reason = ""
        if kind == "remote" and origin_url:
            try:
                extra_addons.clone_extra_repo(team, name, origin_url)
            except Exception as exc:
                # A failed clone may leave a partial bare repo. It belongs to
                # this import attempt, so remove it before a retry or fallback.
                shutil.rmtree(repo_path, ignore_errors=True)
                if not os.path.isdir(src_dir):
                    if best_effort:
                        record_warning(name, "skipped", str(exc))
                        continue
                    raise
                local_fallback_reason = str(exc)
                logger.warning(
                    "Clone of extra repo '%s' failed; using uploaded files",
                    name,
                )
            else:
                try:
                    extra_addons._resolve_branch_revision(repo_path, name, branch)
                except Exception as exc:
                    # Clone succeeded but the requested branch is missing on
                    # the remote: the bare repo cannot serve this branch, so
                    # discard the repo created by this import attempt.
                    shutil.rmtree(repo_path, ignore_errors=True)
                    if not best_effort:
                        raise
                    reason = str(exc)
                    if not os.path.isdir(src_dir):
                        record_warning(name, "skipped", reason)
                        continue
                    local_fallback_reason = reason
                else:
                    wired[name] = branch
                    continue
        if not os.path.isdir(src_dir):
            error = PrerequisiteNotMetError(
                f"Addon '{name}' has no uploaded files and no usable origin."
            )
            if best_effort:
                record_warning(name, "skipped", str(error))
                continue
            raise error
        try:
            extra_addons.create_local_repo(team, name, src_dir, branch)
            extra_addons._resolve_branch_revision(repo_path, name, branch)
        except Exception as exc:
            if not best_effort:
                raise
            # No repo existed before this entry, so any partial target belongs
            # to this import attempt and must not poison a later retry.
            shutil.rmtree(repo_path, ignore_errors=True)
            record_warning(name, "skipped", str(exc))
            continue
        wired[name] = branch
        if local_fallback_reason:
            record_warning(name, "used_local_copy", local_fallback_reason)

    return wired


def finalize_imported_template(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    staging_dir: str,
    *,
    addon_error_policy: str = "strict",
) -> dict[str, object]:
    """Promote a fully-staged push import into the live template and load it.

    The push-based Odoo.sh import uploads into ``staging_dir`` (metadata.json,
    dump.sql.gz, filestore/); nothing touches the live template until now.
    This mirrors the tail of :func:`import_from_odoo`: within the overlay
    remount guard (live envs keep their upper deltas), swap the staged
    filestore/dump/metadata into the template directory, chown the filestore
    for the odoo user, then refresh sizes and restore the dump into the
    template DB. The staging directory is removed on success.
    """
    from oduflow.docker_ops import env_ops

    os.makedirs(staging_dir, exist_ok=True)
    promoted_marker = os.path.join(staging_dir, ".promoted")
    staged_meta = os.path.join(staging_dir, "metadata.json")
    staged_dump = os.path.join(staging_dir, "dump.sql.gz")
    staged_fs = os.path.join(staging_dir, "filestore")
    tpl_dir = team.get_template_dir(template_name)
    live_meta = team.get_template_metadata_path(template_name)
    live_dump = os.path.join(tpl_dir, "dump.sql.gz")
    promoted = os.path.isfile(promoted_marker)
    if not (
        os.path.isfile(staged_meta) or (promoted and os.path.isfile(live_meta))
    ) or not (os.path.isfile(staged_dump) or (promoted and os.path.isfile(live_dump))):
        raise PrerequisiteNotMetError(
            "Manifest and SQL dump must be uploaded before finalize."
        )
    metadata_source = staged_meta if os.path.isfile(staged_meta) else live_meta
    try:
        with open(metadata_source) as f:
            major_version = str(json.load(f).get("odoo_version") or "")
    except (OSError, ValueError):
        major_version = ""

    client = get_client()
    template_filestore_path = team.get_template_filestore_path(template_name)

    # Written before promotion so a crash after any individual rename can be
    # retried from the mix of remaining staged and already-live artifacts.
    open(promoted_marker, "a").close()
    with env_ops.remount_template_overlays(
        client, settings, team, template_name
    ) as remount:
        os.makedirs(tpl_dir, exist_ok=True)

        # Swap filestore (the overlay lower layer) while envs are unmounted.
        if os.path.isdir(staged_fs):
            if os.path.exists(template_filestore_path):
                shutil.rmtree(template_filestore_path)
            os.rename(staged_fs, template_filestore_path)
        elif not os.path.isdir(template_filestore_path):
            os.makedirs(template_filestore_path, exist_ok=True)

        # Swap dump: drop stale dumps under other names so
        # get_template_sql_path resolves to the new gzip'd SQL dump.
        if os.path.isfile(staged_dump):
            for stale in (
                "dump.pgdump",
                "dump.sql",
                "dump.pgdump.gz",
                "dump.sql.gz",
            ):
                stale_path = os.path.join(tpl_dir, stale)
                if os.path.isfile(stale_path):
                    os.remove(stale_path)
            os.rename(staged_dump, live_dump)
        if os.path.isfile(staged_meta):
            os.replace(staged_meta, live_meta)

        if major_version:
            try:
                uid_gid = get_odoo_uid_gid(client, f"odoo:{major_version}")
                uid_str, gid_str = uid_gid.split(":")
                chown_recursive(
                    template_filestore_path,
                    int(uid_str),
                    int(gid_str),
                    client,
                    f"odoo:{major_version}",
                )
                logger.info("Template filestore chowned to %s", uid_gid)
            except Exception as exc:  # noqa: BLE001 - chown is best-effort
                logger.warning("Could not chown template filestore: %s", exc)

    _update_template_sizes(team, settings, template_name)
    result = reload_template(settings, team, template_name=template_name)

    # Turn any uploaded/announced addons (Enterprise, Themes, extra repos) into
    # Oduflow extra-addons repos and record them on the template so environments
    # created from it mount the same addons-path Odoo.sh ran with. Done after the
    # DB restore and before staging cleanup. Declared addons are part of the
    # template contract, so a missing repo/branch keeps the import retryable.
    addon_warnings: list[dict[str, str]] = []
    wired = _wire_imported_addons(
        team,
        staging_dir,
        major_version,
        addon_error_policy=addon_error_policy,
        addon_warnings=addon_warnings,
    )
    if wired:
        meta_path = team.get_template_metadata_path(template_name)
        try:
            with open(meta_path) as f:
                md = json.load(f)
            existing = _normalize_extra_addons(md.get("extra_addons", {}))
            md["extra_addons"] = {**existing, **wired}
            atomic_write_private_json(meta_path, md, sort_keys=False)
        except (OSError, ValueError) as exc:
            raise PrerequisiteNotMetError(
                f"Could not record imported addons on the template: {exc}"
            ) from exc

    shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "status": "imported",
        "template_name": template_name,
        "template_db": result["template_db"],
        "restore_seconds": result.get("restore_seconds", 0),
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        "extra_addons": wired,
        "addon_warnings": addon_warnings,
    }


def cleanup_orphans(
    settings: Settings, team: TeamSettings, dry_run: bool = True
) -> dict[str, Any]:
    """Find and remove orphaned databases, workspaces, and port registry entries.

    An orphan is a resource whose branch has no corresponding Docker container.
    Template databases (oduflow_template_*) are always excluded.

    Returns a dict with keys: orphan_databases, orphan_workspaces, orphan_ports,
    and orphan_roles, each a list of removed (or would-be-removed) names.
    """
    from oduflow.port_registry import _load_registry, _save_registry

    client = get_client()

    # 1. Collect branches that have live containers
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    live_branches: set[str] = set()
    for c in client.containers.list(all=True, filters=filters):
        if not c.name.startswith(settings.prefix):
            continue
        branch = c.labels.get(settings.branch_label)
        if branch:
            live_branches.add(branch)

    db_prefix = f"oduflow_{team.team_id}_"

    # 2. Orphan databases
    rows = _exec_sql(
        client,
        settings,
        "SELECT datname FROM pg_database WHERE datistemplate=false AND datname NOT IN ('postgres','template0','template1');",
    )
    all_dbs = [r for r in rows.splitlines() if r]

    orphan_dbs: list[str] = []
    for db_name in all_dbs:
        if not db_name.startswith(db_prefix):
            continue
        # Reverse-map: strip prefix to get the slug, then check if any live branch produces this db name
        matched = any(get_db_name(b, team.team_id) == db_name for b in live_branches)
        if not matched:
            orphan_dbs.append(db_name)

    # 3. Orphan workspace directories
    orphan_workspaces: list[str] = []
    if os.path.isdir(team.workspaces_dir):
        for entry in os.listdir(team.workspaces_dir):
            entry_path = os.path.join(team.workspaces_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            # Protected workspaces are never cleaned up
            if os.path.exists(os.path.join(entry_path, ".protected")):
                continue
            matched = any(entry == b.replace("/", "-") for b in live_branches)
            if not matched:
                orphan_workspaces.append(entry)

    # 4. Orphan port registry entries
    orphan_ports: list[str] = []
    registry = _load_registry(team.port_registry_path)
    for branch in list(registry.keys()):
        if branch not in live_branches:
            orphan_ports.append(branch)

    # 5. Orphan PG roles
    from oduflow.env_credentials import generate_pg_username, load_credentials

    role_prefix = f"u_{team.team_id}_"
    roles_raw = _exec_sql(
        client, settings, "SELECT rolname FROM pg_roles WHERE rolcanlogin=true;"
    )
    all_roles = [r for r in roles_raw.splitlines() if r.startswith(role_prefix)]
    live_roles = {
        generate_pg_username(branch, team.team_id) for branch in live_branches
    }
    for branch in live_branches:
        credentials = load_credentials(
            branch,
            team.workspaces_dir,
            settings.db_user,
            settings.db_password,
        )
        live_roles.add(credentials["pg_user"])
    orphan_roles = [role for role in all_roles if role not in live_roles]

    if dry_run:
        logger.info(
            "Cleanup dry-run: %d orphan DBs, %d orphan workspaces, %d orphan ports, %d orphan roles",
            len(orphan_dbs),
            len(orphan_workspaces),
            len(orphan_ports),
            len(orphan_roles),
        )
        return {
            "dry_run": True,
            "orphan_databases": orphan_dbs,
            "orphan_workspaces": orphan_workspaces,
            "orphan_ports": orphan_ports,
            "orphan_roles": orphan_roles,
        }

    # --- Actually remove ---
    removed_dbs: list[str] = []
    for db_name in orphan_dbs:
        try:
            _exec_sql(
                client, settings, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);'
            )
            removed_dbs.append(db_name)
            logger.info("Dropped orphan database %s", db_name)
        except Exception as exc:
            logger.warning("Failed to drop orphan database %s: %s", db_name, exc)

    removed_workspaces: list[str] = []
    for entry in orphan_workspaces:
        entry_path = os.path.join(team.workspaces_dir, entry)
        try:
            # Unmount any overlay before removing. _unmount_filestore needs the
            # TeamSettings (it reads team.workspaces_dir); passing the global
            # Settings here raised AttributeError on every orphan, so cleanup
            # silently removed nothing.
            from oduflow.docker_ops.env_ops import _unmount_filestore

            _unmount_filestore(entry, team)
            shutil.rmtree(entry_path)
            removed_workspaces.append(entry)
            logger.info("Removed orphan workspace %s", entry_path)
        except Exception as exc:
            logger.warning("Failed to remove orphan workspace %s: %s", entry_path, exc)

    removed_ports: list[str] = []
    for branch in orphan_ports:
        registry.pop(branch, None)
        removed_ports.append(branch)
        logger.info("Released orphan port for branch '%s'", branch)
    if removed_ports:
        _save_registry(team.port_registry_path, registry)

    removed_roles: list[str] = []
    for role in orphan_roles:
        try:
            _drop_pg_role(client, settings, role)
            removed_roles.append(role)
        except Exception as exc:
            logger.warning("Failed to drop orphan PG role %s: %s", role, exc)

    return {
        "dry_run": False,
        "orphan_databases": removed_dbs,
        "orphan_workspaces": removed_workspaces,
        "orphan_ports": removed_ports,
        "orphan_roles": removed_roles,
    }


def delete_template(
    settings: Settings, team: TeamSettings, template_name: str
) -> dict[str, str]:
    client = get_client()

    # Refuse if any environment was created from this template: its filestore is
    # the overlay lower layer for those envs (see env_ops._mount_filestore), so
    # deleting it would yank the base out from under a live overlay and break it.
    # Mirrors the same guard in rename_template.
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    dependent: list[str] = []
    for c in client.containers.list(all=True, filters=filters):
        if c.labels.get("oduflow.template", "none") == template_name:
            dependent.append(c.labels.get(settings.branch_label, c.name))
    if dependent:
        raise ConflictError(
            f"Cannot delete template '{template_name}': used by environments: "
            f"{', '.join(dependent)}. Delete those environments first."
        )

    tpl_db = get_template_db_name(template_name, team.team_id)
    db_exists = _db_exists(client, settings, tpl_db)
    template_dir_path = team.get_template_dir(template_name)
    dir_exists = os.path.isdir(template_dir_path)

    if not db_exists and not dir_exists:
        raise NotFoundError(f"Template '{template_name}' not found.")

    if db_exists:
        _wait_pg_ready(client, settings)
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{tpl_db}';",
        )
        _exec_sql(client, settings, f'DROP DATABASE IF EXISTS "{tpl_db}" WITH (FORCE);')
        logger.info("Dropped template DB %s", tpl_db)

    if dir_exists:
        shutil.rmtree(template_dir_path)
        logger.info("Removed template directory %s", template_dir_path)

    return {"status": "dropped", "template_name": template_name, "template_db": tpl_db}


def rename_template(
    settings: Settings, team: TeamSettings, template_name: str, new_name: str
) -> dict[str, str]:
    """Rename a template's directory and (if loaded) its PostgreSQL template DB.

    Blocked when any environment was created from the template: the
    ``oduflow.template`` label is immutable on a running container and
    :func:`env_ops.remount_template_overlays` matches environments by it, so
    renaming out from under them would orphan them (mirrors how
    ``delete_extra_repo`` refuses in-use repos).

    The DB is renamed first (reversible with a second ``ALTER``), then the
    directory; if the directory rename fails, the DB name is rolled back so the
    two never diverge.
    """
    validate_template_name(template_name)
    validate_template_name(new_name)
    if new_name == template_name:
        raise ConflictError("New template name is the same as the current one.")

    old_dir = team.get_template_dir(template_name)
    new_dir = team.get_template_dir(new_name)
    if not os.path.isdir(old_dir):
        raise NotFoundError(f"Template '{template_name}' not found.")
    if os.path.isdir(new_dir):
        raise ConflictError(f"Template '{new_name}' already exists.")

    client = get_client()
    old_db = get_template_db_name(template_name, team.team_id)
    new_db = get_template_db_name(new_name, team.team_id)

    # Refuse if any environment references this template (immutable label).
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    dependent: list[str] = []
    for c in client.containers.list(all=True, filters=filters):
        if c.labels.get("oduflow.template", "none") == template_name:
            dependent.append(c.labels.get(settings.branch_label, c.name))
    if dependent:
        raise ConflictError(
            f"Cannot rename template '{template_name}': used by environments: "
            f"{', '.join(dependent)}. Delete those environments first."
        )

    db_renamed = False
    if _db_exists(client, settings, old_db):
        if _db_exists(client, settings, new_db):
            raise ConflictError(f"A template database named '{new_db}' already exists.")
        _wait_pg_ready(client, settings)
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{old_db}';",
        )
        # ALTER DATABASE ... RENAME fails if the database has open sessions.
        _exec_sql(
            client,
            settings,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{old_db}' AND pid<>pg_backend_pid();",
        )
        try:
            _exec_sql(
                client, settings, f'ALTER DATABASE "{old_db}" RENAME TO "{new_db}";'
            )
            db_renamed = True
        finally:
            # Re-mark whichever name is live as a template.
            live_db = new_db if db_renamed else old_db
            _exec_sql(
                client,
                settings,
                f"UPDATE pg_database SET datistemplate=true WHERE datname='{live_db}';",
            )
        logger.info("Renamed template DB %s -> %s", old_db, new_db)

    try:
        os.makedirs(os.path.dirname(new_dir), exist_ok=True)
        os.rename(old_dir, new_dir)
    except OSError as exc:
        if db_renamed:
            try:
                _exec_sql(
                    client,
                    settings,
                    f"UPDATE pg_database SET datistemplate=false "
                    f"WHERE datname='{new_db}';",
                )
                _exec_sql(
                    client, settings, f'ALTER DATABASE "{new_db}" RENAME TO "{old_db}";'
                )
                _exec_sql(
                    client,
                    settings,
                    f"UPDATE pg_database SET datistemplate=true "
                    f"WHERE datname='{old_db}';",
                )
                logger.warning("Rolled back template DB rename after directory failure")
            except Exception:  # noqa: BLE001 - rollback is best-effort
                logger.error(
                    "Template DB renamed to %s but directory rename failed and the "
                    "DB rollback also failed; manual reconciliation needed.",
                    new_db,
                )
        raise ExternalCommandError("rename template directory", 1, str(exc))

    logger.info("Renamed template '%s' -> '%s'", template_name, new_name)
    return {
        "status": "renamed",
        "template_name": new_name,
        "old_name": template_name,
        "template_db": new_db,
    }


def get_template_metadata(team: TeamSettings, template_name: str) -> dict[str, str]:
    """Return a template's raw metadata file and its optimistic revision.

    Invalid JSON is deliberately returned unchanged so the dashboard editor can
    be used to repair a file that was broken by an earlier filesystem edit.
    """
    validate_template_name(template_name)
    template_dir = team.get_template_dir(template_name)
    if not os.path.isdir(template_dir):
        raise NotFoundError(f"Template '{template_name}' not found.")

    metadata_path = team.get_template_metadata_path(template_name)
    try:
        with open(metadata_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = b""

    return {
        "content": raw.decode("utf-8") if raw else "{}\n",
        "revision": hashlib.sha256(raw).hexdigest(),
    }


def update_template_metadata(
    team: TeamSettings,
    template_name: str,
    content: str,
    expected_revision: str,
) -> dict[str, str]:
    """Validate and atomically replace a template's ``metadata.json`` file."""
    validate_template_name(template_name)
    template_dir = team.get_template_dir(template_name)
    if not os.path.isdir(template_dir):
        raise NotFoundError(f"Template '{template_name}' not found.")

    metadata_path = team.get_template_metadata_path(template_name)
    try:
        with open(metadata_path, "rb") as f:
            current_raw = f.read()
    except FileNotFoundError:
        current_raw = b""

    current_revision = hashlib.sha256(current_raw).hexdigest()
    if expected_revision != current_revision:
        raise ConflictError(
            f"Template '{template_name}' metadata changed after it was opened. "
            "Reload the settings and apply your changes again."
        )

    try:
        metadata = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(metadata, dict):
        raise TypeError("Template metadata must be a JSON object.")
    if metadata.get("env_vars") is not None:
        # Rejected here rather than at environment creation: a name Docker
        # cannot export is worth surfacing while the editor is still open.
        metadata["env_vars"] = normalize_env_vars(metadata["env_vars"])

    normalized = json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    # Always owner-only, even for files that predate migration 0007: the
    # editor is exactly where env vars (possibly plaintext values) get added.
    atomic_write_private_text(metadata_path, normalized)

    logger.info("Updated metadata for template %s", template_name)
    return {
        "content": normalized,
        "revision": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _template_env_vars(metadata: dict[str, Any], template_name: str) -> dict[str, str]:
    """Env vars recorded on a template, or {} if the entry is unusable.

    Never raises: a template whose metadata was hand-edited into an invalid
    env_vars block must still list and still provision — the variables are
    dropped with a warning instead of taking the environment down with them.
    """
    raw = metadata.get("env_vars")
    if not raw:
        return {}
    try:
        return normalize_env_vars(raw)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Ignoring invalid env_vars in template %s metadata: %s",
            template_name,
            exc,
        )
        return {}


def list_templates(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    client = get_client()
    templates = team.list_templates()
    result = []
    for template_name in templates:
        tpl_db = get_template_db_name(template_name, team.team_id)
        has_sql = os.path.isfile(team.get_template_sql_path(template_name))
        has_filestore = os.path.isdir(team.get_template_filestore_path(template_name))
        db_loaded = _db_exists(client, settings, tpl_db)
        metadata: dict[str, Any] = {}
        metadata_valid = True
        metadata_path = team.get_template_metadata_path(template_name)
        if os.path.isfile(metadata_path):
            try:
                with open(metadata_path) as f:
                    loaded_metadata = json.load(f)
                if not isinstance(loaded_metadata, dict):
                    raise TypeError("metadata must be a JSON object")
                metadata = loaded_metadata
            except (OSError, TypeError, UnicodeError, ValueError) as exc:
                metadata_valid = False
                logger.warning(
                    "Could not read metadata for template %s: %s",
                    template_name,
                    exc,
                )
        if metadata_valid and (
            "filestore_size_mb" not in metadata or "dump_size_mb" not in metadata
        ):
            metadata = _update_template_sizes(team, settings, template_name, metadata)
        result.append(
            {
                "template_name": template_name,
                "template_db": tpl_db,
                "has_sql": has_sql,
                "has_filestore": has_filestore,
                "db_loaded": db_loaded,
                "metadata_valid": metadata_valid,
                "odoo_image": metadata.get("odoo_image", ""),
                "repo_url": metadata.get("repo_url", ""),
                "git_user": metadata.get("git_user", ""),
                "extra_addons": _normalize_extra_addons(
                    metadata.get("extra_addons", {}),
                ),
                "use_overlay": metadata.get("use_overlay"),
                "filestore_size_mb": metadata.get("filestore_size_mb"),
                "dump_size_mb": metadata.get("dump_size_mb"),
                "auto_install_modules": metadata.get("auto_install_modules", ""),
                "env_vars": _template_env_vars(metadata, template_name),
                # Code the snapshot was taken from — the anchor for the lineage
                # check run at environment creation.
                "source_branch": metadata.get("source_branch", ""),
                "source_commit": metadata.get("source_commit", ""),
                "snapshot_at": metadata.get("snapshot_at", ""),
            }
        )
    return result
