"""Scheduled production backups (snapshots, base backups, retention).

A background daemon thread (mirroring :mod:`oduflow.reaper`) ticks every
minute and fires three job kinds when their schedule slot arrives:

- per-production **snapshots** — daily at ``[backup] snapshot_time``
  (per-production override in the registry: ``backup.schedule`` =
  ``"HH:MM"`` or ``"off"``);
- cluster **WAL-G base backup** — daily at ``basebackup_time``, followed
  by ``wal-g delete retain FULL n``;
- **retention/prune** — weekly (Sunday 04:30): snapshot manifests + dumps
  thinned by the ``keep`` policy, chunkstore revisions kept in lockstep.

The fire rule ``last_success < fire_time <= now`` gives catch-up for free:
a server that was down at 02:00 runs the snapshot once on the first tick
after startup, while a restart right after a successful run does not
double-fire. Failures are retried on subsequent ticks (still due), capped
per slot, and surfaced in the registry / backup log.

Jobs run inline, sequentially, in the scheduler thread — one pg_dump / one
chunkstore upload at a time keeps I/O predictable, and it serializes
backup against prune for the chunkstore's safety assumptions.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from oduflow import production_registry
from oduflow.errors import BusyError
from oduflow.locking import LockManager, prod_backups_lock_key
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

TICK_SECONDS = 60
_MAX_ATTEMPTS_PER_SLOT = 3

_LOG_MAX_BYTES = 5 * 1024 * 1024

# Weekly prune slot: Sunday 04:30 server-local time.
_PRUNE_WEEKDAY = 6
_PRUNE_TIME = "04:30"

# Cluster-level job state lives in a small JSON beside the registry files.
_CLUSTER_STATE_FILENAME = "backup_scheduler.json"


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


def last_fire_time(
    now: datetime.datetime,
    hhmm: str,
    *,
    weekday: int | None = None,
) -> datetime.datetime:
    """The most recent scheduled occurrence of ``hhmm`` at/before *now*
    (optionally constrained to a weekday, for the weekly prune)."""
    hour, minute = _parse_hhmm(hhmm)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        if candidate > now:
            candidate -= datetime.timedelta(days=1)
        return candidate
    while candidate.weekday() != weekday or candidate > now:
        candidate -= datetime.timedelta(days=1)
    return candidate


def is_due(
    now: datetime.datetime,
    fire_time: datetime.datetime,
    last_success: datetime.datetime | None,
    last_attempt: datetime.datetime | None,
    attempts: int,
) -> bool:
    """The single fire rule: due when the slot arrived after the last
    success, not exhausted by failed attempts for this slot, and not
    attempted within the current tick already."""
    if fire_time > now:
        return False
    if last_success is not None and last_success >= fire_time:
        return False
    if last_attempt is not None and last_attempt >= fire_time:
        # Same slot was already attempted: retry only while under the cap.
        if attempts >= _MAX_ATTEMPTS_PER_SLOT:
            return False
    return True


def _parse_ts(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _append_log(team: TeamSettings, record: dict[str, Any]) -> None:
    """One JSON line per job into {team.data_dir}/production/backup.log
    (size-rotated)."""
    log_dir = os.path.join(team.data_dir, "production")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "backup.log")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        logger.debug("Could not write backup log", exc_info=True)


def _cluster_state_path(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, _CLUSTER_STATE_FILENAME)


def _load_cluster_state(settings: Settings) -> dict[str, Any]:
    path = _cluster_state_path(settings)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cluster_state(settings: Settings, state: dict[str, Any]) -> None:
    path = _cluster_state_path(settings)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        logger.debug("Could not persist scheduler state", exc_info=True)


def _local_now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------


def _run_snapshot_job(
    settings: Settings,
    team: TeamSettings,
    locks: LockManager,
    name: str,
    now: datetime.datetime,
    fire: datetime.datetime,
) -> None:
    from oduflow import backup_ops
    from oduflow.locking import prod_lock_key

    key = prod_lock_key(team.team_id, name)
    backups_key = prod_backups_lock_key(team.team_id)
    try:
        locks.acquire_env(key, operation="scheduled backup")
    except BusyError:
        return  # deploy/restore in flight; still due next tick
    try:
        # Same order as the MCP tool: the production first, then the team's
        # backup store, so a prune cannot run mid-snapshot.
        locks.acquire_env(backups_key, operation="scheduled backup")
    except BusyError:
        locks.release_env(key)
        return  # prune/restore is using the store; still due next tick
    started = time.time()
    backup_state: dict[str, Any] = {
        "last_attempt_at": now.isoformat(),
    }
    try:
        record = production_registry.get_production(team, name)
        prev_backup = record.get("backup", {}) or {}
        # A new schedule slot resets the retry budget: the counter only ever
        # cleared on success before, so a slot that exhausted its attempts left
        # the counter high and starved subsequent slots of retries.
        prev_attempt = _parse_ts(str(prev_backup.get("last_attempt_at", "")))
        attempts = (
            0
            if prev_attempt is None or prev_attempt < fire
            else int(prev_backup.get("slot_attempts", 0) or 0)
        )
        backup_state["slot_attempts"] = attempts + 1
        production_registry.set_nested(team, name, "backup", backup_state)
        manifest = backup_ops.snapshot_production(
            settings, team, name, trigger="schedule"
        )
        production_registry.set_nested(team, name, "backup", {"slot_attempts": 0})
        _append_log(
            team,
            {
                "job": "snapshot",
                "production": name,
                "snapshot_id": manifest["id"],
                "ok": True,
                "seconds": round(time.time() - started, 1),
                "at": now.isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("Scheduled snapshot of production '%s' failed: %s", name, exc)
        try:
            production_registry.set_nested(
                team,
                name,
                "backup",
                {"last_result": "error", "last_error": str(exc)[:500]},
            )
        except Exception:
            pass
        _append_log(
            team,
            {
                "job": "snapshot",
                "production": name,
                "ok": False,
                "error": str(exc)[:500],
                "at": now.isoformat(),
            },
        )
    finally:
        locks.release_env(backups_key)
        locks.release_env(key)


def _run_basebackup_job(
    settings: Settings,
    locks: LockManager,
    now: datetime.datetime,
    fire: datetime.datetime,
) -> None:
    from oduflow import walg
    from oduflow.docker_ops.client import get_client

    state = _load_cluster_state(settings)
    base = state.setdefault("basebackup", {})
    prev_attempt = _parse_ts(str(base.get("last_attempt_at", "")))
    attempts = (
        0
        if prev_attempt is None or prev_attempt < fire
        else int(base.get("slot_attempts", 0) or 0)
    )
    base["last_attempt_at"] = now.isoformat()
    base["slot_attempts"] = attempts + 1
    _save_cluster_state(settings, state)
    try:
        locks.acquire_env("prod:__cluster__", operation="scheduled base backup")
    except BusyError:
        return
    try:
        client = get_client()
        walg.backup_push(client, settings)
        if settings.backup is not None:
            walg.delete_retain(client, settings, settings.backup.walg_keep_full)
        base["last_success_at"] = now.isoformat()
        base["last_error"] = ""
        base["slot_attempts"] = 0
        logger.info("WAL-G base backup completed")
    except Exception as exc:
        base["last_error"] = str(exc)[:500]
        logger.warning("WAL-G base backup failed: %s", exc)
    finally:
        locks.release_env("prod:__cluster__")
        _save_cluster_state(settings, state)


def _run_prune_job(
    settings: Settings,
    locks: LockManager,
    now: datetime.datetime,
    fire: datetime.datetime,
) -> None:
    from oduflow import backup_ops

    state = _load_cluster_state(settings)
    prune_state = state.setdefault("prune", {})
    prev_attempt = _parse_ts(str(prune_state.get("last_attempt_at", "")))
    attempts = (
        0
        if prev_attempt is None or prev_attempt < fire
        else int(prune_state.get("slot_attempts", 0) or 0)
    )
    prune_state["last_attempt_at"] = now.isoformat()
    prune_state["slot_attempts"] = attempts + 1
    _save_cluster_state(settings, state)
    ok = True
    for team in settings.teams.values():
        backups_key = prod_backups_lock_key(team.team_id)
        try:
            locks.acquire_env(backups_key, operation="scheduled backup prune")
        except BusyError:
            ok = False
            continue
        try:
            result = backup_ops.prune_backups(settings, team)
            _append_log(
                team,
                {
                    "job": "prune",
                    "ok": True,
                    "deleted_snapshots": result["deleted_snapshots"],
                    "at": now.isoformat(),
                },
            )
        except Exception as exc:
            ok = False
            logger.warning("Backup prune failed for team %s: %s", team.team_id, exc)
            _append_log(
                team,
                {
                    "job": "prune",
                    "ok": False,
                    "error": str(exc)[:500],
                    "at": now.isoformat(),
                },
            )
        finally:
            locks.release_env(backups_key)
    if ok:
        prune_state["last_success_at"] = now.isoformat()
        prune_state["slot_attempts"] = 0
    _save_cluster_state(settings, state)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def tick(settings: Settings, locks: LockManager) -> None:
    """One scheduler pass. Cheap when nothing is due."""
    if not settings.prod_enabled:
        return
    backup = settings.backup
    if backup is None:
        return
    now = _local_now()

    # Per-production snapshots.
    for team in settings.teams.values():
        try:
            productions = production_registry.list_productions(team)
        except Exception:
            logger.debug(
                "Scheduler could not read registry for team %s",
                team.team_id,
                exc_info=True,
            )
            continue
        for name, record in productions.items():
            backup_state = record.get("backup", {}) or {}
            schedule = str(backup_state.get("schedule") or backup.snapshot_time)
            if schedule == "off":
                continue
            try:
                fire = last_fire_time(now, schedule)
            except ValueError:
                logger.warning(
                    "Production '%s' has an invalid backup schedule %r",
                    name,
                    schedule,
                )
                continue
            due = is_due(
                now,
                fire,
                _parse_ts(str(backup_state.get("last_snapshot_at", ""))),
                _parse_ts(str(backup_state.get("last_attempt_at", ""))),
                int(backup_state.get("slot_attempts", 0) or 0),
            )
            if due:
                _run_snapshot_job(settings, team, locks, name, now, fire)

    # Cluster jobs only make sense once the production tier exists.
    from oduflow.docker_ops.client import get_client
    from oduflow.docker_ops.system_ops import prod_infra_exists

    try:
        if not prod_infra_exists(get_client(), settings):
            return
    except Exception:
        return

    state = _load_cluster_state(settings)
    base = state.get("basebackup", {})
    fire = last_fire_time(now, backup.basebackup_time)
    if is_due(
        now,
        fire,
        _parse_ts(str(base.get("last_success_at", ""))),
        _parse_ts(str(base.get("last_attempt_at", ""))),
        int(base.get("slot_attempts", 0) or 0),
    ):
        _run_basebackup_job(settings, locks, now, fire)

    prune_state = state.get("prune", {})
    fire = last_fire_time(now, _PRUNE_TIME, weekday=_PRUNE_WEEKDAY)
    if is_due(
        now,
        fire,
        _parse_ts(str(prune_state.get("last_success_at", ""))),
        _parse_ts(str(prune_state.get("last_attempt_at", ""))),
        int(prune_state.get("slot_attempts", 0) or 0),
    ):
        _run_prune_job(settings, locks, now, fire)


def start_backup_scheduler(
    get_settings: Callable[[], Settings],
    locks: LockManager,
    interval: float = TICK_SECONDS,
) -> threading.Thread | None:
    """Start the scheduler thread when production and backups are enabled."""
    settings = get_settings()
    if not settings.prod_enabled:
        logger.info("Backup scheduler disabled (production hosting disabled)")
        return None
    if settings.backup is None:
        logger.info("Backup scheduler disabled ([backup] not configured)")
        return None

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                tick(get_settings(), locks)
            except Exception:
                logger.warning("Backup scheduler tick failed", exc_info=True)

    thread = threading.Thread(target=loop, name="oduflow-backup-scheduler", daemon=True)
    thread.start()
    logger.info(
        "Backup scheduler started (snapshots %s, base backups %s, prune weekly Sun %s)",
        settings.backup.snapshot_time,
        settings.backup.basebackup_time,
        _PRUNE_TIME,
    )
    return thread
