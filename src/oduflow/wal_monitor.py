"""Local WAL progress monitoring and a persistent production disk circuit breaker.

Sampling and protection run independently of backup jobs and HTTP requests.
Emergency shutdown deliberately does not wait for deployment/backup locks:
those operations may themselves be stuck while the disk fills.
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import docker
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")
INTERVAL = 15
STALE_AFTER = 60
GIB = 1024**3
_STATE_SIZE = 16384
_emergency: set[str] = set()
_monitors: dict[str, Monitor] = {}
_policies: dict[str, dict[str, Any]] = {}
_registry_lock = threading.Lock()


def _path(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, "wal_guard.json")


def state(settings: Settings) -> dict[str, Any]:
    try:
        with open(_path(settings)) as f:
            value = json.load(f)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid guard state")
    except FileNotFoundError:
        value = {"version": 1, "latched": False, "paused": False}
    except (OSError, ValueError):
        value = {"version": 1, "latched": True, "reason": "WAL guard state unreadable"}
    if settings.base_data_dir in _emergency:
        value["latched"] = True
    return value


def _save(settings: Settings, value: dict[str, Any]) -> None:
    """Reserve space early; retain an in-place fallback when disk is full.

    An interrupted in-place write is unreadable and therefore fails closed on
    the next boot. A memory latch also prevents starts if both writes fail.
    """
    os.makedirs(settings.base_data_dir, exist_ok=True)
    payload = json.dumps(value, sort_keys=True).encode().ljust(_STATE_SIZE, b" ")
    tmp = ""
    try:
        try:
            fd, tmp = tempfile.mkstemp(dir=settings.base_data_dir, prefix="wal-guard.")
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _path(settings))
            directory = os.open(settings.base_data_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Existing fixed-size blocks can be overwritten without allocating
            # a new file on ext4. Never truncate the reservation first.
            with open(_path(settings), "r+b") as f:
                if os.fstat(f.fileno()).st_size < len(payload):
                    raise
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def assert_writable(settings: Settings) -> None:
    if state(settings).get("latched"):
        raise PrerequisiteNotMetError(
            "Production disk protection is active. Recover WAL archiving and "
            "release the cluster protection before starting production work."
        )


def _decode(output: Any) -> str:
    return (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else str(output)
    )


def parse_sample(output: str, now: float) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in output.splitlines():
        if line in {"DISK", "WAL", "READY", "CURRENT", "LAST"}:
            current = line
            sections[current] = []
        elif current and line:
            sections[current].append(line)
    disk = sections["DISK"][-1].split()
    sizes = {
        name: int(size) for name, size in (line.split() for line in sections["WAL"])
    }
    ready = [line.split() for line in sections["READY"]]
    oldest = min((float(parts[1]) for parts in ready), default=now)
    result: dict[str, Any] = {
        "sampled_at": now,
        "disk_total_bytes": int(disk[-5]),
        "disk_free_bytes": int(disk[-3]),  # df Available excludes root's reserve
        "wal_bytes": sum(sizes.values()),
        "ready_count": len(ready),
        "ready_bytes": sum(
            sizes.get(parts[0].removesuffix(".ready"), 0) for parts in ready
        ),
        "oldest_ready_seconds": max(0, now - oldest) if ready else 0,
    }
    if sections.get("CURRENT"):
        pid, started, segment = sections["CURRENT"][0].split()
        result["attempt"] = {
            "pid": int(pid),
            "started_at": float(started),
            "segment": segment,
            "seconds": max(0, now - float(started)),
        }
    if sections.get("LAST"):
        started, finished, code, segment = sections["LAST"][0].split()
        result["last_attempt"] = {
            "started_at": float(started),
            "finished_at": float(finished),
            "exit_code": int(code),
            "segment": segment,
        }
    return result


def sample_local(client: Any, settings: Settings) -> dict[str, Any]:
    """Inspect the actual WAL filesystem, including when PostgreSQL is stopped."""
    pg = client.containers.get(settings.prod_db_container)
    script = (Path(__file__).parent / "templates" / "wal-sample.sh").read_text()
    if pg.status == "running":
        code, output = pg.exec_run(
            ["timeout", "10s", "sh", "-c", script], user="postgres"
        )
    else:
        # Docker's volume may live on another host/filesystem. Inspect it from
        # the daemon side, read-only, using the same postgres identity.
        helper = client.containers.run(
            pg.attrs.get("Image") or settings.production_pg_image,
            entrypoint=["timeout", "10s", "sh", "-c", script],
            detach=True,
            network_disabled=True,
            user="postgres",
            volumes={
                settings.prod_db_volume: {
                    "bind": "/var/lib/postgresql/data",
                    "mode": "ro",
                }
            },
        )
        try:
            code = helper.wait(timeout=15)["StatusCode"]
            output = helper.logs()
        finally:
            helper.remove(force=True, v=True)
    if code:
        raise PrerequisiteNotMetError("Cannot inspect production WAL filesystem")
    result = parse_sample(_decode(output), time.time())
    result["pg_status"] = pg.status
    result["restart_count"] = pg.attrs.get("RestartCount", 0)
    if pg.status == "running":
        query = """SELECT json_build_object(
          'archived_count', archived_count, 'last_archived_wal', last_archived_wal,
          'last_archived_time', last_archived_time, 'failed_count', failed_count,
          'last_failed_wal', last_failed_wal, 'last_failed_time', last_failed_time,
          'stats_reset', stats_reset, 'archive_mode', current_setting('archive_mode'),
          'archive_command', current_setting('archive_command'),
          'hba_file', current_setting('hba_file')) FROM pg_stat_archiver"""
        try:
            code, output = pg.exec_run(
                [
                    "timeout",
                    "5s",
                    "psql",
                    "-U",
                    settings.db_user,
                    "-d",
                    "postgres",
                    "-Atc",
                    query,
                ],
                user="postgres",
            )
        except Exception:
            code, output = 1, b""
        if code == 0:
            try:
                result["archiver"] = json.loads(_decode(output))
            except ValueError:
                result["archiver_error"] = "Invalid PostgreSQL archiver statistics"
        else:
            result["archiver_error"] = "PostgreSQL archiver statistics unavailable"
    return result


def assess(
    settings: Settings,
    sample: dict[str, Any],
    previous: dict[str, Any] | None,
    history: list[dict[str, Any]],
    guard: dict[str, Any],
) -> dict[str, Any]:
    """Derive progress/risk without contacting Docker or S3."""
    now = sample["sampled_at"]
    free = sample["disk_free_bytes"]
    reserve = settings.wal_stop_free_gb * GIB
    rate = 0.0
    if history and now - history[0]["sampled_at"] >= 30:
        rate = max(
            0.0,
            (history[0]["disk_free_bytes"] - free) / (now - history[0]["sampled_at"]),
        )
    recent_rate = 0.0
    if history and now - history[-1]["sampled_at"] >= 5:
        # A sudden burst must not be hidden by an earlier cleanup or a flat
        # five-minute average. Use the more conservative observed slope.
        recent_rate = max(
            0.0,
            (history[-1]["disk_free_bytes"] - free) / (now - history[-1]["sampled_at"]),
        )
        rate = max(rate, recent_rate)
    until_reserve = max(0, (free - reserve) / rate) if rate else None
    # df covers the whole filesystem, so an unrelated burst (filestore copy,
    # image pull) can project a breach that never happens. Two guards keep
    # that burst from latching the cluster-wide breaker: only the current
    # slope may project (the five-minute average keeps projecting long after
    # a burst has finished), and the projection must hold across two
    # consecutive samples. Actually reaching the reserve still latches now.
    until_recent = max(0, (free - reserve) / recent_rate) if recent_rate else None
    projected_breach = (
        until_recent is not None and until_recent <= settings.wal_stop_within
    )
    critical_disk = free <= reserve or (
        projected_breach and bool((previous or {}).get("projected_breach"))
    )
    critical_queue = sample["ready_bytes"] >= settings.wal_stop_queue_gb * GIB
    archiver = sample.get("archiver", {})
    old_archiver = (previous or {}).get("archiver", {})
    progressed = bool(previous) and (
        (
            archiver.get("last_archived_time") is not None
            and archiver.get("last_archived_time")
            != old_archiver.get("last_archived_time")
        )
        or (
            archiver.get("stats_reset") == old_archiver.get("stats_reset")
            and archiver.get("archived_count", 0)
            > old_archiver.get("archived_count", 0)
        )
    )
    last_progress = (
        now if progressed or not previous else previous.get("last_progress_at", now)
    )
    if not sample["ready_count"]:
        last_progress = now
    stalled_for = min(max(0, now - last_progress), sample["oldest_ready_seconds"])
    status, detail = (
        "ok",
        "Archiving is idle" if not sample["ready_count"] else "Archiving WAL",
    )
    if settings.backup is None:
        status, detail = "off", "WAL archiving is not configured"
    elif guard.get("paused"):
        status, detail = "warn", "Archiving paused; WAL continues to accumulate"
    elif sample.get("pg_status") != "running" or sample.get("archiver_error"):
        status, detail = "error", "PostgreSQL archiver unavailable"
    elif archiver.get("archive_mode") not in {
        "on",
        "always",
    } or "wal-archive.sh" not in archiver.get("archive_command", ""):
        status, detail = "error", "Managed WAL archive command is not active"
    elif sample["ready_count"] and stalled_for >= settings.wal_stall_after:
        status, detail = "error", "WAL archiving has stopped making progress"
    elif sample["ready_count"] and (
        stalled_for >= settings.wal_warn_after
        or sample["oldest_ready_seconds"] >= settings.wal_stall_after
    ):
        status, detail = "warn", "WAL archive backlog needs attention"
    if free / max(1, sample["disk_total_bytes"]) < 0.15 and status in {"ok", "off"}:
        status, detail = "warn", "Production WAL filesystem has less than 15% available"
    if sample["ready_bytes"] >= settings.wal_warn_queue_gb * GIB and status in {
        "ok",
        "off",
        "warn",
    }:
        status, detail = "warn", "WAL archive queue exceeded its warning size"
    if critical_queue:
        status, detail = "error", "WAL archive queue reached its safety size limit"
    if critical_disk:
        status, detail = "error", "Production WAL disk reached its safety reserve"
    if guard.get("latched"):
        status, detail = (
            "error",
            guard.get("reason", "Production disk protection is active"),
        )
    return {
        **sample,
        "status": status,
        "detail": detail,
        "last_progress_at": last_progress,
        "no_progress_seconds": stalled_for,
        "disk_growth_bytes_per_second": round(rate, 1),
        "seconds_to_reserve": until_reserve,
        "projected_breach": projected_breach,
        "critical_disk": critical_disk,
        "critical_queue": critical_queue,
        "warn_queue_bytes": settings.wal_warn_queue_gb * GIB,
        "stop_queue_bytes": settings.wal_stop_queue_gb * GIB,
        "reserve_bytes": reserve,
        "resume_bytes": settings.wal_resume_free_gb * GIB,
    }


def cancel_attempt(client: Any, settings: Settings) -> None:
    """Signal only wal-push, including legacy attempts predating the wrapper."""
    script = r"""
for proc in /proc/[0-9]*; do
    test -r "$proc/cmdline" || continue
    cmd=$(tr '\000' ' ' < "$proc/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      '/opt/oduflow-bin/wal-g --config /etc/walg/walg.json wal-push '*)
        started=$(awk '{print $22}' "$proc/stat" 2>/dev/null) || continue
        kill -TERM "${proc##*/}" 2>/dev/null || true
        sleep 1
        current=$(awk '{print $22}' "$proc/stat" 2>/dev/null) || continue
        # PID reuse must never turn retry-upload into killing another process.
        if test "$started" = "$current"; then
            kill -KILL "${proc##*/}" 2>/dev/null || true
        fi ;;
    esac
done
"""
    pg = client.containers.get(settings.prod_db_container)
    code, _ = pg.exec_run(["timeout", "5s", "sh", "-c", script], user="postgres")
    if code:
        raise PrerequisiteNotMetError("Could not interrupt the current WAL upload")


def _stop_container(container: Any) -> str | None:
    try:
        if container.status == "paused":
            container.unpause()
        if container.status in {"running", "restarting", "paused"}:
            container.stop(timeout=30)
        return None
    except Exception:
        logger.exception("WAL protection could not stop %s", container.name)
        return str(container.name)


def recovery_fence(client: Any, settings: Settings, *, enabled: bool) -> None:
    """Offline HBA fence: recovery admits local maintenance, never app writers.

    Keep networking intact so private S3 endpoints still work. The original
    HBA is retained in PGDATA and survives both Docker and Oduflow restarts.
    """
    common = "set -eu; cd /var/lib/postgresql/data; original=.oduflow-wal-pg_hba.conf; "
    if enabled:
        script = common + (
            'test -e "$original" || cp -p pg_hba.conf "$original"; '
            "printf 'local all all trust\\nhost all all 0.0.0.0/0 reject\\nhost all all ::/0 reject\\n' > pg_hba.conf.wal-tmp; "
            "chmod 600 pg_hba.conf.wal-tmp; mv pg_hba.conf.wal-tmp pg_hba.conf"
        )
        pg = client.containers.get(settings.prod_db_container)
        helper = client.containers.run(
            pg.attrs.get("Image") or settings.production_pg_image,
            entrypoint=["timeout", "10s", "sh", "-c", script],
            detach=True,
            network_disabled=True,
            user="postgres",
            volumes={
                settings.prod_db_volume: {
                    "bind": "/var/lib/postgresql/data",
                    "mode": "rw",
                }
            },
        )
        try:
            code = helper.wait(timeout=15)["StatusCode"]
        finally:
            helper.remove(force=True, v=True)
    else:
        script = (
            common
            + 'cp -p "$original" pg_hba.conf.wal-tmp; mv pg_hba.conf.wal-tmp pg_hba.conf'
        )
        pg = client.containers.get(settings.prod_db_container)
        code, _ = pg.exec_run(["timeout", "5s", "sh", "-c", script], user="postgres")
        if not code:
            code, _ = pg.exec_run(
                [
                    "timeout",
                    "5s",
                    "psql",
                    "-U",
                    settings.db_user,
                    "-d",
                    "postgres",
                    "-Atc",
                    "SELECT pg_reload_conf()",
                ],
                user="postgres",
            )
        if not code:
            code, _ = pg.exec_run(
                ["rm", "-f", "/var/lib/postgresql/data/.oduflow-wal-pg_hba.conf"],
                user="postgres",
            )
    if code:
        raise PrerequisiteNotMetError(
            "Could not update the production recovery connection fence"
        )


def enforce_stop(client: Any, settings: Settings, guard: dict[str, Any]) -> None:
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{settings.managed_label}=true", "oduflow.prod=true"]},
    )
    # The configured PG container is authoritative even if an old container
    # predates our production labels.
    try:
        pg = client.containers.get(settings.prod_db_container)
        if all(container.id != pg.id for container in containers):
            containers.append(pg)
    except docker.errors.NotFound:
        pass
    policies = guard.setdefault("restart_policies", {})
    # A _save that failed on a full disk leaves the guard file without the
    # policies we already disabled, so recover them from this process first.
    remembered = _policies.setdefault(settings.base_data_dir, {})
    for container_id, policy in remembered.items():
        policies.setdefault(container_id, policy)
    for container in containers:
        if container.id in policies:
            continue
        observed = container.attrs.get("HostConfig", {}).get("RestartPolicy") or {}
        if observed.get("Name") in {"", "no"}:
            # Either a previous pass already disabled restarts or the policy is
            # unreadable. Every managed production container is created with
            # unless-stopped, so recording "no" here would make release strip
            # the restart policy permanently.
            observed = {"Name": "unless-stopped"}
        policies[container.id] = observed
    remembered.update(policies)
    try:
        _save(settings, guard)
    except OSError:
        _emergency.add(settings.base_data_dir)
        logger.exception("Could not persist WAL protection; disabling Docker restarts")
    for container in containers:
        try:
            container.update(restart_policy={"Name": "no"})
        except Exception:
            logger.exception("Could not disable restart for %s", container.name)
    apps = [c for c in containers if c.name != settings.prod_db_container]
    with ThreadPoolExecutor(max_workers=8) as pool:
        errors = [error for error in pool.map(_stop_container, apps) if error]
    if not guard.get("recovering"):
        for pg in containers:
            if pg.name == settings.prod_db_container:
                error = _stop_container(pg)
                if error:
                    errors.append(error)
    if errors:
        raise PrerequisiteNotMetError(
            "Protection could not stop all production containers; will retry"
        )


class Monitor:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.latest: dict[str, Any] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=20)
        self.thread: threading.Thread | None = None

    def tick(self, settings: Settings, client: Any) -> None:
        with self.lock:
            guard = state(settings)
            if not os.path.exists(_path(settings)):
                try:
                    _save(settings, guard)
                except OSError:
                    logger.exception(
                        "Could not reserve WAL guard state; still checking disk safety"
                    )
            try:
                sample = sample_local(client, settings)
            except docker.errors.NotFound:
                self.latest = {
                    "status": "off",
                    "detail": "Production PostgreSQL not provisioned",
                    "sampled_at": time.time(),
                }
                return
            except Exception:
                self.latest = {
                    **self.latest,
                    "status": "error",
                    "detail": "WAL sampling failed; data may be stale",
                }
                if guard.get("latched"):
                    enforce_stop(client, settings, guard)
                raise
            result = assess(
                settings, sample, self.latest or None, list(self.history), guard
            )
            self.history.append(sample)
            self.latest = result
            # Recovery must be allowed to drain an oversized queue with apps
            # fenced. Disk pressure still stops PostgreSQL during recovery.
            if result["critical_disk"] or (
                result["critical_queue"] and not guard.get("recovering")
            ):
                if not guard.get("latched"):
                    guard["protected_at"] = time.time()
                    guard.pop("recovery_started_at", None)
                guard.update(latched=True, recovering=False, reason=result["detail"])
                guard.setdefault("protected_at", time.time())
                guard.setdefault(
                    "hba_file",
                    sample.get("archiver", {}).get(
                        "hba_file", "/var/lib/postgresql/data/pg_hba.conf"
                    ),
                )
                _emergency.add(settings.base_data_dir)
            if guard.get("latched"):
                enforce_stop(client, settings, guard)
                result = {
                    **result,
                    "status": "error",
                    "detail": guard.get(
                        "reason", "Production disk protection is active"
                    ),
                }
                self.latest = result
            # Remote failure cannot delay the local safety decision.
            if (
                settings.backup is not None
                and sample["pg_status"] == "running"
                and not (guard.get("latched") and not guard.get("recovering"))
            ):
                from oduflow import walg

                storage = walg.storage_status(client, settings)
                result = {**result, "storage": storage}
                if storage["status"] == "error" and result["status"] != "error":
                    result.update(status="error", detail=storage["detail"])
                self.latest = result
                # Live evidence supersedes the last activation attempt: without
                # this, one transient preflight failure would keep /healthz and
                # the dashboard red forever (nothing else clears the record).
                if result["status"] == "ok" and storage["status"] == "ok":
                    walg.clear_readiness(settings)


def _monitor(settings: Settings) -> Monitor:
    with _registry_lock:
        return _monitors.setdefault(settings.base_data_dir, Monitor())


def status(settings: Settings) -> dict[str, Any]:
    monitor = _monitor(settings)
    # Readers never wait for sampling, S3, shutdown, or a recovery action.
    result = copy.deepcopy(monitor.latest)
    guard = state(settings)
    result["guard"] = {
        key: value for key, value in guard.items() if key != "restart_policies"
    }
    result["configured"] = settings.backup is not None
    from oduflow import walg

    result["preflight"] = walg.readiness_status(settings)
    if not result.get("sampled_at") or time.time() - result["sampled_at"] > STALE_AFTER:
        result.update(
            status="error", detail="WAL monitor has no fresh sample", stale=True
        )
    else:
        result["stale"] = False
    preflight = result["preflight"]
    checked_at = preflight.get("checked_at", 0)
    if (
        settings.backup is not None
        and preflight.get("status") in {"error", "pending"}
        # A record, not a readiness cache: it stops driving the overall status
        # once a newer live sample has superseded it. The short grace window
        # keeps a failure that lands between two ticks visible.
        and (
            checked_at >= result.get("sampled_at", 0)
            or time.time() - checked_at <= STALE_AFTER
        )
    ):
        result.update(status="error", detail=preflight["detail"])
    if guard.get("latched"):
        result.update(
            status="error",
            detail=guard.get("reason", "Production disk protection is active"),
        )
    elif guard.get("paused") and result.get("status") in {"ok", "off"}:
        result.update(
            status="warn", detail="Archiving paused; WAL continues to accumulate"
        )
    return result


def control(
    settings: Settings, client: Any, action: str, confirm: str
) -> dict[str, Any]:
    if confirm != "ALL-PRODUCTIONS":
        raise PrerequisiteNotMetError(
            "Confirm ALL-PRODUCTIONS: this action affects the shared cluster"
        )
    monitor = _monitor(settings)
    with monitor.lock:
        guard = state(settings)
        from oduflow import walg

        if action in {"pause", "resume", "retry"}:
            if settings.backup is None:
                raise PrerequisiteNotMetError("WAL archiving is not configured")
            marker = Path(walg.conf_host_dir(settings)) / "archive-paused"
            if action == "pause":
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch(mode=0o644)
                guard["paused"] = True
                _save(settings, guard)
            elif action == "resume":
                walg.write_walg_config(settings)
                walg.apply_walg_config_ownership(settings, client)
                running = (
                    client.containers.get(settings.prod_db_container).status
                    == "running"
                )
                if running:
                    walg.storage_preflight(client, settings)
                marker.unlink(missing_ok=True)
                guard["paused"] = False
                _save(settings, guard)
                if running:
                    walg.apply_archive_command(client, settings, enabled=True)
            elif guard.get("paused"):
                raise PrerequisiteNotMetError("Resume archiving before retrying")
            if client.containers.get(settings.prod_db_container).status == "running":
                cancel_attempt(client, settings)
            elif action == "retry":
                raise PrerequisiteNotMetError("Production PostgreSQL is not running")
        elif action in {"recover", "release"}:
            if not guard.get("latched"):
                raise PrerequisiteNotMetError(
                    "Production disk protection is not active"
                )
            sample = sample_local(client, settings)
            if sample["disk_free_bytes"] < settings.wal_resume_free_gb * GIB:
                raise PrerequisiteNotMetError(
                    "Free more space before recovering production"
                )
            if assess(
                settings, sample, monitor.latest or None, list(monitor.history), guard
            )["critical_disk"]:
                raise PrerequisiteNotMetError(
                    "Disk space is still being consumed too quickly for safe recovery"
                )
            if action == "recover":
                if guard.get("paused"):
                    raise PrerequisiteNotMetError(
                        "Resume WAL archiving before recovery"
                    )
                guard.update(recovering=False)
                enforce_stop(client, settings, guard)
                if (
                    guard.get("hba_file", "/var/lib/postgresql/data/pg_hba.conf")
                    != "/var/lib/postgresql/data/pg_hba.conf"
                ):
                    raise PrerequisiteNotMetError(
                        "Recovery fencing requires the managed PostgreSQL HBA path"
                    )
                recovery_fence(client, settings, enabled=True)
                if settings.backup is not None:
                    walg.write_walg_config(settings)
                    walg.apply_walg_config_ownership(settings, client)
                client.containers.get(settings.prod_db_container).start()
                from oduflow.docker_ops.system_ops import _wait_pg_ready

                _wait_pg_ready(
                    client,
                    settings,
                    timeout=30,
                    container_name=settings.prod_db_container,
                )
                if settings.backup is not None:
                    walg.storage_preflight(client, settings)
                    walg.apply_archive_command(client, settings, enabled=True)
                guard.update(recovering=True, recovery_started_at=time.time())
                _save(settings, guard)
                if settings.backup is not None:
                    pg = client.containers.get(settings.prod_db_container)
                    code, _ = pg.exec_run(
                        [
                            "timeout",
                            "5s",
                            "psql",
                            "-U",
                            settings.db_user,
                            "-d",
                            "postgres",
                            "-Atc",
                            "SELECT pg_switch_wal()",
                        ],
                        user="postgres",
                    )
                    if code:
                        raise PrerequisiteNotMetError(
                            "Could not request a WAL archive verification segment"
                        )
            else:
                if not guard.get("recovering") or sample.get("pg_status") != "running":
                    raise PrerequisiteNotMetError(
                        "Start PostgreSQL recovery before releasing protection"
                    )
                if sample["ready_bytes"] >= settings.wal_warn_queue_gb * GIB:
                    raise PrerequisiteNotMetError(
                        "Wait for the WAL queue to fall below warn_queue_gb before releasing protection"
                    )
                archived_at = sample.get("archiver", {}).get("last_archived_time")
                archived_time = (
                    datetime.datetime.fromisoformat(archived_at).timestamp()
                    if archived_at
                    else 0
                )
                archiver = sample.get("archiver", {})
                if settings.backup is not None and (
                    guard.get("paused")
                    or sample.get("archiver_error")
                    or "wal-archive.sh" not in archiver.get("archive_command", "")
                    or archiver.get("archive_mode") not in {"on", "always"}
                    or archived_time <= guard.get("recovery_started_at", 0)
                ):
                    raise PrerequisiteNotMetError(
                        "Wait for confirmed WAL archiving progress before releasing protection"
                    )
                if (
                    settings.backup is not None
                    and walg.storage_status(client, settings)["status"] != "ok"
                ):
                    raise PrerequisiteNotMetError("WAL-G storage is still unavailable")
                restore = {
                    **_policies.get(settings.base_data_dir, {}),
                    **guard.get("restart_policies", {}),
                }
                for container_id, policy in restore.items():
                    try:
                        client.containers.get(container_id).update(
                            restart_policy=policy
                        )
                    except docker.errors.NotFound:
                        continue
                released = {
                    **guard,
                    "latched": False,
                    "recovering": False,
                    "reason": "",
                    "restart_policies": {},
                }
                try:
                    recovery_fence(client, settings, enabled=False)
                    _save(settings, released)
                except Exception:
                    _emergency.add(settings.base_data_dir)
                    guard["recovering"] = False
                    enforce_stop(client, settings, guard)
                    raise
                _emergency.discard(settings.base_data_dir)
                _policies.pop(settings.base_data_dir, None)
                # Applications remain stopped; operators start the desired ones.
        else:
            raise PrerequisiteNotMetError("Unknown WAL control action")
    return status(settings)


def start_monitor(get_settings: Callable[[], Settings]) -> threading.Thread | None:
    settings = get_settings()
    if not settings.prod_enabled:
        return None
    monitor = _monitor(settings)
    with monitor.lock:
        if monitor.thread is not None and monitor.thread.is_alive():
            return monitor.thread

        def loop() -> None:
            from oduflow.docker_ops.client import get_client

            while True:
                try:
                    current = get_settings()
                    if current.prod_enabled:
                        monitor.tick(current, get_client())
                except Exception:
                    logger.exception("WAL monitor tick failed")
                time.sleep(INTERVAL)

        monitor.thread = threading.Thread(
            target=loop, name="oduflow-wal-monitor", daemon=True
        )
        monitor.thread.start()
        return monitor.thread
