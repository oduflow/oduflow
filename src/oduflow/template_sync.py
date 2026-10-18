"""Cron-owned SSH template synchronization; the web process only manages jobs.

The source is a completed backup directory with dump.*, filestore/ and READY
(a unique snapshot ID, published last). Transfer into an inbox, never a live
lower layer. A successful import may reset explicitly opted-in environments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from oduflow.errors import BusyError, ConflictError, PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json, atomic_write_private_text
from oduflow.locking import LockManager, template_lock_key
from oduflow.naming import get_workspace_path, validate_template_name
from oduflow.settings import Settings, TeamSettings, find_toml

logger = logging.getLogger("oduflow")

# How long a worker waits for the team gate after its transfer finished, like
# attach_filestore's swap: the download is already paid for.
TEAM_LOCK_TIMEOUT = 300.0


def job_dir(team: TeamSettings, name: str) -> Path:
    validate_template_name(name)
    return Path(team.data_dir) / "template_sync" / name


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise PrerequisiteNotMetError(f"Invalid state file: {path}")
    return data


def env_policy(team: TeamSettings, name: str) -> dict[str, Any]:
    path = Path(get_workspace_path(name, team.workspaces_dir)) / ".template-sync.json"
    try:
        return _read(path)
    except (OSError, ValueError, PrerequisiteNotMetError) as exc:
        # Fail safe: an unreadable opt-in never authorizes a reset, and one
        # broken workspace must not break listing the whole team.
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return {}


def set_env_policy(team: TeamSettings, name: str, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ValueError("auto_refresh must be a boolean")
    path = Path(get_workspace_path(name, team.workspaces_dir)) / ".template-sync.json"
    if not path.parent.is_dir():
        raise PrerequisiteNotMetError("Environment workspace does not exist")
    atomic_write_private_json(str(path), {"auto_refresh": enabled})


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    enabled = data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    host = str(data.get("host", "")).strip()
    user = str(data.get("user", "")).strip()
    path = str(data.get("path", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("host must be a DNS name or IPv4 address")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user):
        raise ValueError("Invalid SSH user")
    if not path.startswith("/") or any(c in path for c in "\n\r\x00"):
        raise ValueError("path must be an absolute remote directory")
    port = data.get("port", 22)
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    schedule = str(data.get("schedule", "0 3 * * *")).strip()
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("schedule must have five cron fields")
    for field, (low, high) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
        for part in field.split(","):
            if not re.fullmatch(r"(?:\*|\d+(?:-\d+)?)(?:/\d+)?", part):
                raise ValueError("Invalid cron field")
            base, _, step = part.partition("/")
            if step and not 1 <= int(step) <= high + 1:
                raise ValueError("Invalid cron step")
            if base != "*":
                values = [int(v) for v in base.split("-")]
                if any(v < low or v > high for v in values) or values != sorted(values):
                    raise ValueError("Cron value outside allowed range")
    return dict(
        enabled=enabled,
        host=host,
        user=user,
        port=int(port),
        path=path.rstrip("/") or "/",
        schedule=" ".join(fields),
    )


def _cron_path(settings: Settings, team: TeamSettings, name: str) -> Path:
    token = hashlib.sha256(f"{team.data_dir}:{name}".encode()).hexdigest()[:24]
    return Path(settings.template_cron_dir) / ("oduflow-template-" + token)


def _cron_user(settings: Settings) -> str:
    """The user cron runs the job as.

    Natively the worker must be the user running Oduflow: anything it writes —
    lock files, status, pins, imported template files — must stay readable and
    replaceable by the server. Under Docker the host user only runs
    ``docker exec``; the worker inside runs as the container's user anyway.
    """
    user = settings.template_cron_user
    if settings.template_cron_container:
        user = user or "root"
    else:
        import pwd

        own = pwd.getpwuid(os.geteuid()).pw_name
        user = user or own
        if user != own:
            raise ValueError(
                f"template_sync.cron_user must be '{own}', the user running "
                "Oduflow: a worker running as another user writes files the "
                "server cannot read"
            )
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Invalid template_sync.cron_user")
    return user


def cron_text(
    settings: Settings, team: TeamSettings, name: str, config: dict[str, Any]
) -> str:
    user = _cron_user(settings)
    toml = os.path.abspath(find_toml())
    cli = [
        sys.executable,
        "-m",
        "oduflow.server",
        "sync-template",
        name,
        "--team",
        team.team_id,
    ]
    if settings.template_cron_container:
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", settings.template_cron_container
        ):
            raise ValueError("Invalid template_sync.cron_container")
        exec_prefix = ["/usr/bin/docker", "exec", settings.template_cron_container]
        command = [
            "/usr/bin/docker",
            "exec",
            "-e",
            f"ODUFLOW_TOML={toml}",
            settings.template_cron_container,
            *cli,
        ]
    else:
        if Path("/.dockerenv").exists():
            raise PrerequisiteNotMetError(
                "Set [template_sync] cron_container and mount the host cron_dir when running in Docker"
            )
        exec_prefix = []
        command = ["/usr/bin/env", f"ODUFLOW_TOML={toml}", *cli]
    # Probe for a queued request first: the minute tick then costs a `test`
    # (or a `docker exec test`), not a full Python start, while nothing waits.
    probe = [*exec_prefix, "test", "-e", str(job_dir(team, name) / "pending")]
    # Cron interprets '%' before the shell, even inside single quotes.
    cmd = shlex.join(command).replace("%", r"\%")
    queued = shlex.join(probe).replace("%", r"\%")
    if any(c in cmd + queued for c in "\n\r\x00"):
        raise ValueError("Cron command paths must not contain control characters")
    header = "# Managed by Oduflow: template sync. Times use the host cron timezone.\nSHELL=/bin/sh\nPATH=/usr/local/bin:/usr/bin:/bin\n"
    # Manual requests are picked up by cron, never spawned by the web process.
    lines = [f"* * * * * {user} {queued} && {cmd} --pending"]
    if config["enabled"]:
        lines.append(f"{config['schedule']} {user} {cmd}")
    return header + "\n".join(lines) + "\n"


def save_config(
    settings: Settings, team: TeamSettings, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    config = validate_config(data)
    text = cron_text(settings, team, name, config)
    directory = job_dir(team, name)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cron = _cron_path(settings, team, name)
    if not cron.parent.is_dir():
        raise PrerequisiteNotMetError(
            f"Cron directory does not exist: {cron.parent}. Install/enable system cron first."
        )
    # Save configuration before publishing the job; restore it if installation fails.
    previous = _read(directory / "config.json")
    atomic_write_private_json(str(directory / "config.json"), config)
    try:
        atomic_write_private_text(str(cron), text)
    except OSError:
        atomic_write_private_json(str(directory / "config.json"), previous)
        raise
    return config


def remove_config(settings: Settings, team: TeamSettings, name: str) -> None:
    _cron_path(settings, team, name).unlink(missing_ok=True)
    directory = job_dir(team, name)
    (directory / "config.json").unlink(missing_ok=True)
    (directory / "pending").unlink(missing_ok=True)
    # Without a schedule nothing re-syncs from the downloaded backup, and once
    # the template is replaced or deleted its hardlinks are a full copy.
    shutil.rmtree(directory / "inbox", ignore_errors=True)


def remove_job(team: TeamSettings, name: str) -> None:
    """Forget a deleted template's sync state, logs included."""
    shutil.rmtree(job_dir(team, name), ignore_errors=True)


def require_unconfigured(team: TeamSettings, name: str) -> None:
    if _read(job_dir(team, name) / "config.json"):
        raise ConflictError(
            "Remove this template's sync configuration before renaming or deleting it."
        )


def request_run(team: TeamSettings, name: str) -> None:
    directory = job_dir(team, name)
    if not _read(directory / "config.json"):
        raise PrerequisiteNotMetError("Save an SSH sync configuration first")
    atomic_write_private_text(str(directory / "pending"), _now())


def inspect_job(settings: Settings, team: TeamSettings, name: str) -> dict[str, Any]:
    directory = job_dir(team, name)
    path = directory / "run.log"
    log = ""
    if path.exists():
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 64000))
            log = handle.read(64000).decode("utf-8", errors="replace")
    state = _read(directory / "status.json")
    if state.get("status") == "running":
        probe = LockManager(os.path.join(settings.base_data_dir, ".locks"))
        try:
            probe.acquire_env(template_lock_key(team.team_id, name))
        except BusyError:
            pass
        else:
            probe.release_env(template_lock_key(team.team_id, name))
            state = {
                **state,
                "status": "interrupted",
                "error": "Worker exited without recording completion; inspect the log before retrying.",
            }
    return {
        "config": _read(directory / "config.json"),
        "state": state,
        "pending": (directory / "pending").exists(),
        "cron_installed": _cron_path(settings, team, name).exists(),
        "log": log,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _transfer(team: TeamSettings, config: dict[str, Any], inbox: Path) -> str:
    from oduflow.git_ops import ssh_key_path

    if not shutil.which("rsync"):
        raise PrerequisiteNotMetError("rsync is required on the Oduflow host")
    ssh = [
        "ssh",
        "-i",
        ssh_key_path(team.ssh_dir()),
        "-p",
        str(config["port"]),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={os.path.join(team.ssh_dir(), 'known_hosts')}",
        "-o",
        "ConnectTimeout=30",
    ]
    remote = f"{config['user']}@{config['host']}"
    ready_path = config["path"].rstrip("/") + "/READY"

    def ready() -> str:
        result = subprocess.run(
            [*ssh, remote, "cat -- " + shlex.quote(ready_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise PrerequisiteNotMetError(
                f"Could not read remote READY ({result.returncode}): {result.stderr[-4000:]}"
            )
        value = result.stdout.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
            raise PrerequisiteNotMetError(
                "READY must contain a unique snapshot ID (1–256 ASCII letters, digits, _ . : -)"
            )
        return value

    snapshot = ready()
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.info("Fetching snapshot %s from %s", snapshot, remote)
    result = subprocess.run(
        [
            "rsync",
            "-rt",
            "--delete",
            "--protect-args",
            "--no-links",
            "--timeout=300",
            "--stats",
            "-e",
            shlex.join(ssh),
            "--",
            remote + ":" + config["path"].rstrip("/") + "/",
            str(inbox) + "/",
        ],
        capture_output=True,
        text=True,
        timeout=6 * 3600,
    )
    logger.info("rsync: %s", result.stdout.strip())
    if result.returncode:
        raise PrerequisiteNotMetError(
            f"rsync failed ({result.returncode}): {result.stderr[-4000:]}"
        )
    if ready() != snapshot or (inbox / "READY").read_text().strip() != snapshot:
        raise PrerequisiteNotMetError(
            "Backup changed during transfer; keep the source immutable until sync completes"
        )
    if not (inbox / "filestore").is_dir():
        raise PrerequisiteNotMetError(
            "Scheduled sync requires dump.* and filestore/ from the same backup"
        )
    return snapshot


def run(
    settings: Settings, team: TeamSettings, name: str, *, pending: bool = False
) -> dict[str, Any]:
    directory = job_dir(team, name)
    if pending and not (directory / "pending").exists():
        return {"status": "idle"}
    locks = LockManager(os.path.join(settings.base_data_dir, ".locks"))
    key = template_lock_key(team.team_id, name)
    try:
        locks.acquire_env(key, operation="sync_template")
    except BusyError:
        # Another worker or a UI/MCP mutation of this template. A queued request
        # stays queued for the next minute tick; a scheduled run is queued the
        # same way rather than lost until the next schedule.
        if pending or not _read(directory / "config.json").get("enabled"):
            return {"status": "busy"}
        atomic_write_private_text(str(directory / "pending"), _now())
        logger.warning("Template '%s' is busy; queued its scheduled sync", name)
        return {"status": "deferred"}
    try:
        return _run_locked(settings, team, name, locks, pending=pending)
    finally:
        locks.release_env(key)


def _run_locked(
    settings: Settings,
    team: TeamSettings,
    name: str,
    locks: LockManager,
    *,
    pending: bool,
) -> dict[str, Any]:
    directory = job_dir(team, name)
    config = _read(directory / "config.json")
    if not config:
        return {"status": "unconfigured"}
    if pending and not (directory / "pending").exists():
        return {"status": "idle"}
    if not pending and not config.get("enabled"):
        return {"status": "disabled"}
    (directory / "pending").unlink(missing_ok=True)
    handler = RotatingFileHandler(
        directory / "run.log", maxBytes=2 * 1024 * 1024, backupCount=3
    )
    os.chmod(directory / "run.log", 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    state: dict[str, Any] = {"status": "running", "started_at": _now()}
    atomic_write_private_json(str(directory / "status.json"), state)
    try:
        snapshot = _transfer(team, validate_config(config), directory / "inbox")
        try:
            locks.acquire_team_blocking(
                team.team_id, TEAM_LOCK_TIMEOUT, operation="sync_template"
            )
        except BusyError as exc:
            # The environments stayed busy. Nothing was published, so the
            # request goes back in the queue instead of being consumed.
            atomic_write_private_text(str(directory / "pending"), _now())
            state.update(
                status="deferred",
                snapshot=snapshot,
                error=f"{exc} Queued again for the next minute tick.",
            )
            logger.warning("Template sync deferred: %s", exc)
            return state
        try:
            state.update(_publish(settings, team, name, snapshot))
        finally:
            locks.release_team(team.team_id)
        logger.info("Sync complete: %s", state)
        return state
    except Exception as exc:
        state.update(status="failed", error=str(exc))
        logger.exception("Template sync failed")
        raise
    finally:
        state["finished_at"] = _now()
        atomic_write_private_json(str(directory / "status.json"), state)
        logger.removeHandler(handler)
        handler.close()


def _publish(
    settings: Settings, team: TeamSettings, name: str, snapshot: str
) -> dict[str, Any]:
    """Import the inbox and refresh opted-in environments; under the team gate."""
    from oduflow import template_generations
    from oduflow.docker_ops import env_ops, system_ops
    from oduflow.docker_ops.client import get_client

    client = get_client()
    template_generations.pin_existing(client, settings, team, name)
    result = system_ops.import_template(
        settings,
        team,
        source=str(job_dir(team, name) / "inbox"),
        template_name=name,
        overwrite=True,
    )
    if result.get("remount_failures"):
        raise PrerequisiteNotMetError(
            f"Filestore remount failed: {result['remount_failures']}"
        )
    template_generations.record_template_snapshot(team, name, snapshot)
    targets: list[str] = []
    failed: dict[str, str] = {}
    skipped: list[str] = []
    for env in env_ops.list_environments(settings, team):
        env_name = env["env_name"]
        if env.get("template_name") != name or not env_policy(team, env_name).get(
            "auto_refresh"
        ):
            continue
        if env_ops.is_protected(settings, team, env_name):
            skipped.append(env_name)
            logger.info("Skipping protected environment %s", env_name)
            continue
        try:
            current = template_generations.data_snapshot(team, env_name) == snapshot
        except PrerequisiteNotMetError as exc:
            failed[env_name] = str(exc)
            continue
        if current:
            logger.info("Environment %s already has snapshot %s", env_name, snapshot)
            continue
        targets.append(env_name)
    updated: list[str] = []
    if targets:
        # Hardlinking the whole filestore is metadata I/O under the team gate:
        # only pay for it when an environment actually moves to this snapshot.
        generation = template_generations.snapshot(team, name)
        for env_name in targets:
            try:
                template_generations.refresh_environment(
                    client, settings, team, env_name, name, generation, snapshot
                )
                updated.append(env_name)
            except Exception as exc:
                logger.exception("Environment %s was not updated", env_name)
                failed[env_name] = str(exc)
    template_generations.collect(team)
    return {
        "status": "partial" if failed or skipped else "success",
        "snapshot": snapshot,
        "updated_envs": updated,
        "failed_envs": failed,
        "skipped_envs": skipped,
    }
