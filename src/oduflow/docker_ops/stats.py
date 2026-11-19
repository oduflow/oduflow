from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.locking import keyed_mutex, storage_stats_lock_key
from oduflow.naming import get_db_name, get_filestore_paths, get_workspace_path
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _calc_cpu_percent(stats: dict[str, Any]) -> float:
    """Calculate CPU usage % from a single stats snapshot."""
    cpu = stats.get("cpu_stats", {})
    precpu = stats.get("precpu_stats", {})

    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get(
        "cpu_usage", {}
    ).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)

    if system_delta <= 0 or cpu_delta < 0:
        return 0.0

    num_cpus = cpu.get("online_cpus") or len(
        cpu.get("cpu_usage", {}).get("percpu_usage", []) or [1]
    )
    percent: float = round((cpu_delta / system_delta) * num_cpus * 100.0, 1)
    return percent


def _get_one_container_stats(container: Any) -> dict[str, Any] | None:
    """Get stats for a single container. Returns None on error."""
    try:
        if container.status != "running":
            return {
                "name": container.name,
                "cpu_percent": 0.0,
                "mem_usage_mb": 0.0,
                "mem_limit_mb": 0.0,
                "mem_percent": 0.0,
            }
        stats = container.stats(stream=False)
        mem = stats.get("memory_stats", {})
        mem_usage = mem.get("usage", 0)
        # Subtract cache if available (actual process memory)
        cache = mem.get("stats", {}).get("cache", 0) if mem.get("stats") else 0
        actual_mem = mem_usage - cache
        mem_limit = mem.get("limit", 0)
        return {
            "name": container.name,
            "cpu_percent": _calc_cpu_percent(stats),
            "mem_usage_mb": round(actual_mem / (1024 * 1024), 1),
            "mem_limit_mb": round(mem_limit / (1024 * 1024), 1),
            "mem_percent": round((actual_mem / mem_limit) * 100.0, 1)
            if mem_limit > 0
            else 0.0,
        }
    except Exception:
        logger.debug("Failed to get stats for %s", container.name, exc_info=True)
        return None


def get_container_stats(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    """Collect CPU/RAM stats for all managed containers in parallel."""
    client = get_client()
    containers = [
        c
        for c in client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team.team_id}",
                ]
            },
            ignore_removed=True,
        )
        if c.name.startswith(settings.prefix)
    ]
    if not containers:
        return []

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(containers), 8)) as pool:
        futures = {pool.submit(_get_one_container_stats, c): c for c in containers}
        for future in as_completed(futures):
            stat = future.result()
            if stat is not None:
                results.append(stat)

    results.sort(key=lambda s: s["name"])
    return results


def _read_memory_linux() -> tuple[float, float] | None:
    """Return (total_mb, used_mb) from /proc/meminfo, or None if unavailable."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    info[key] = int(parts[1])  # in kB
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        if total <= 0:
            return None
        used = total - available
        return total / 1024, used / 1024
    except Exception:
        logger.debug("Failed to read /proc/meminfo", exc_info=True)
        return None


def _read_memory_macos() -> tuple[float, float] | None:
    """Return (total_mb, used_mb) via vm_stat, or None if unavailable.

    "Used" approximates Activity Monitor: active + wired + compressed pages.
    """
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except Exception:
        logger.debug("Failed to run vm_stat", exc_info=True)
        return None
    try:
        size_match = re.search(r"page size of (\d+) bytes", out)
        page_size = int(size_match.group(1)) if size_match else 4096
        pages: dict[str, int] = {}
        for line in out.splitlines():
            m = re.match(r'^"?([A-Za-z -]+?)"?:\s+(\d+)\.?\s*$', line)
            if m:
                pages[m.group(1)] = int(m.group(2))
        used_pages = (
            pages.get("Pages active", 0)
            + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)
        )
        try:
            total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            total_bytes = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                ).stdout.strip()
            )
        if total_bytes <= 0 or used_pages <= 0:
            return None
        return total_bytes / (1024 * 1024), used_pages * page_size / (1024 * 1024)
    except Exception:
        logger.debug("Failed to parse vm_stat output", exc_info=True)
        return None


def _read_cpu_ticks_linux() -> tuple[float, float] | None:
    """Return cumulative (total, idle) CPU ticks from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            first = f.readline()
        parts = first.split()
        if parts[0] != "cpu" or len(parts) < 5:
            return None
        vals = [float(v) for v in parts[1:9]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
        return total, idle
    except Exception:
        logger.debug("Failed to read /proc/stat", exc_info=True)
        return None


def _read_cpu_ticks_macos() -> tuple[float, float] | None:
    """Return cumulative (total, idle) CPU ticks via mach host_statistics()."""
    try:
        import ctypes

        class _HostCpuLoadInfo(ctypes.Structure):
            # CPU_STATE_USER, CPU_STATE_SYSTEM, CPU_STATE_IDLE, CPU_STATE_NICE
            _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]

        host_cpu_load_info = 3  # HOST_CPU_LOAD_INFO flavor
        libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
        info = _HostCpuLoadInfo()
        count = ctypes.c_uint32(4)
        ret = libsystem.host_statistics(
            libsystem.mach_host_self(),
            host_cpu_load_info,
            ctypes.byref(info),
            ctypes.byref(count),
        )
        if ret != 0:
            return None
        user, system, idle, nice = (float(t) for t in info.cpu_ticks)
        return user + system + idle + nice, idle
    except Exception:
        logger.debug("Failed to read host_statistics", exc_info=True)
        return None


def _read_cpu_ticks() -> tuple[float, float] | None:
    if sys.platform == "darwin":
        return _read_cpu_ticks_macos()
    return _read_cpu_ticks_linux()


_cpu_ticks_prev: tuple[float, float] | None = None


def _cpu_percent_interval() -> float | None:
    """Actual CPU utilization % measured between calls.

    The first call takes a short 0.25s sample; afterwards each call measures
    the interval since the previous one (the dashboard's polling period),
    which smooths the value naturally. The 32-bit mach tick counters can
    wrap; a non-positive delta resets the baseline.
    """
    global _cpu_ticks_prev
    now = _read_cpu_ticks()
    if now is None:
        return None
    if _cpu_ticks_prev is None:
        _cpu_ticks_prev = now
        time.sleep(0.25)
        now = _read_cpu_ticks()
        if now is None:
            return None
    d_total = now[0] - _cpu_ticks_prev[0]
    d_idle = now[1] - _cpu_ticks_prev[1]
    _cpu_ticks_prev = now
    if d_total <= 0:
        return None
    return round(min(max((1.0 - d_idle / d_total) * 100.0, 0.0), 100.0), 1)


def get_system_stats() -> dict[str, Any]:
    """Read system CPU and memory stats (Linux via /proc, macOS via mach/vm_stat)."""
    result: dict[str, Any] = {
        "mem_total_mb": 0.0,
        "mem_used_mb": 0.0,
        "mem_percent": 0.0,
        "cpu_percent": 0.0,
        "load_avg": [0.0, 0.0, 0.0],
    }

    mem = _read_memory_linux()
    if mem is None and sys.platform == "darwin":
        mem = _read_memory_macos()
    if mem is not None:
        total_mb, used_mb = mem
        result["mem_total_mb"] = round(total_mb, 1)
        result["mem_used_mb"] = round(used_mb, 1)
        result["mem_percent"] = (
            round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
        )

    # Load average
    try:
        load = os.getloadavg()
        result["load_avg"] = [round(v, 2) for v in load]
        result["cpu_count"] = os.cpu_count() or 1
    except Exception:
        logger.debug("Failed to get load average", exc_info=True)

    # CPU utilization: measured from kernel tick counters; fall back to the
    # old load-average approximation only if tick reading is unavailable.
    cpu = _cpu_percent_interval()
    if cpu is not None:
        result["cpu_percent"] = cpu
    elif result["load_avg"][0] and result.get("cpu_count"):
        result["cpu_percent"] = round(
            (result["load_avg"][0] / result["cpu_count"]) * 100.0, 1
        )

    return result


# ── Storage stats: DB + disk usage per environment and per team ──────────────
#
# Walking a filestore is far too slow for the dashboard polling loop, so these
# numbers are computed only on demand (the per-environment refresh button, or
# the team-wide REST refresh) and cached in team_data_dir/storage_stats.json.
# The same cache is the data source for external billing/quota tooling: an
# operator script can POST /api/usage/refresh per team and read the totals
# without touching Docker or PostgreSQL itself.

_STORAGE_CACHE_FILE = "storage_stats.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _storage_cache_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, _STORAGE_CACHE_FILE)


def read_storage_cache(team: TeamSettings) -> dict[str, Any]:
    """Return the cached storage stats:
    {"envs": {name: entry}, "productions": {name: entry}, "team": entry|None}."""
    try:
        with open(_storage_cache_path(team)) as f:
            data = json.load(f)
        if isinstance(data, dict):
            # A section of the wrong shape (``null`` from a hand-edit or a
            # foreign writer) reads as empty, so callers can index into it.
            envs = data.get("envs")
            productions = data.get("productions")
            team_entry = data.get("team")
            return {
                "envs": envs if isinstance(envs, dict) else {},
                "productions": productions if isinstance(productions, dict) else {},
                "team": team_entry if isinstance(team_entry, dict) else None,
            }
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("Unreadable storage stats cache", exc_info=True)
    return {"envs": {}, "productions": {}, "team": None}


def _write_storage_cache(team: TeamSettings, cache: dict[str, Any]) -> None:
    """Atomically replace the cache file. Callers hold the team's
    ``storage_stats_lock_key`` mutex.

    The temp file name is unique per write (same reason as ports.json): a
    fixed ``.tmp`` path lets two writers rename each other's file away.
    """
    path = _storage_cache_path(team)
    os.makedirs(team.data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="storage_stats.", suffix=".tmp", dir=team.data_dir
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _store_storage_entry(
    team: TeamSettings, section: str, name: str, entry: dict[str, Any]
) -> None:
    """Merge one freshly measured entry into the cache.

    Refresh handlers run on parallel threadpool threads, so the
    read-modify-write is serialised per team; the (slow) measuring itself
    stays outside the mutex.
    """
    with keyed_mutex(storage_stats_lock_key(team.team_id)):
        cache = read_storage_cache(team)
        cache[section][name] = entry
        _write_storage_cache(team, cache)


def _dir_size_bytes(path: str, skip_dirs: set[str] | None = None) -> int:
    """Sum file sizes under ``path``, counting each hardlinked inode once."""
    skip = skip_dirs or set()
    seen: set[tuple[int, int]] = set()
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in skip]
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if info.st_nlink > 1:
                inode = (info.st_dev, info.st_ino)
                if inode in seen:
                    continue
                seen.add(inode)
            total += info.st_size
    return total


def _env_merged_filestore_skip(env_name: str, team: TeamSettings) -> set[str]:
    """The overlay's merged view includes the template's read-only lower layer;
    only the upper layer is data this environment actually adds, so the merged
    mountpoint is excluded from disk sizing (it would also double-count the
    upper layer)."""
    paths = get_filestore_paths(env_name, team.workspaces_dir)
    if os.path.isdir(paths["upper"]):
        return {paths["merged"]}
    return set()


def _db_size_bytes(
    settings: Settings, db_name: str, container_name: str | None = None
) -> int:
    from oduflow.docker_ops.system_ops import _exec_sql

    out = _exec_sql(
        get_client(),
        settings,
        "SELECT COALESCE((SELECT pg_database_size(datname) FROM pg_database "
        f"WHERE datname = '{db_name}'), 0);",
        container_name=container_name,
    )
    return int(out) if out.strip().isdigit() else 0


def _env_db_size_bytes(settings: Settings, team: TeamSettings, env_name: str) -> int:
    return _db_size_bytes(settings, get_db_name(env_name, team.team_id))


def compute_env_storage(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Measure one environment: database size (one catalog query) and workspace
    disk size (full walk — seconds on large filestores, hence the cache)."""
    workspace = get_workspace_path(env_name, team.workspaces_dir)
    disk = 0
    if os.path.isdir(workspace):
        disk = _dir_size_bytes(workspace, _env_merged_filestore_skip(env_name, team))
    return {
        "db_bytes": _env_db_size_bytes(settings, team, env_name),
        "disk_bytes": disk,
        "computed_at": _utcnow_iso(),
    }


def refresh_env_storage(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Recompute one environment's storage entry and persist it in the cache."""
    entry = compute_env_storage(settings, team, env_name)
    _store_storage_entry(team, "envs", env_name, entry)
    return entry


def _prod_disk_bytes(team: TeamSettings, name: str) -> int:
    from oduflow.docker_ops.production_ops import _workspace

    workspace = _workspace(team, name)
    return _dir_size_bytes(workspace) if os.path.isdir(workspace) else 0


def _prod_db_sizes(
    settings: Settings, team: TeamSettings, names: list[str]
) -> dict[str, int] | None:
    """Database size of each named production, from one catalog query on the
    production cluster (same query the shared-cluster team total uses).

    Returns None when the cluster cannot be queried (stopped, recreating,
    under PITR): the sizes are then unknown, and that must not take the rest
    of a team refresh down with it.
    """
    from oduflow.docker_ops.production_ops import prod_db_name
    from oduflow.docker_ops.system_ops import _exec_sql

    try:
        rows = _exec_sql(
            get_client(),
            settings,
            "SELECT datname, pg_database_size(datname) FROM pg_database "
            "WHERE NOT datistemplate;",
            container_name=settings.prod_db_container,
        )
    except Exception:
        logger.warning(
            "Production cluster unavailable; production DB sizes not measured",
            exc_info=True,
        )
        return None
    by_db: dict[str, int] = {}
    for line in rows.splitlines():
        db, _, size = line.partition("|")
        if size.strip().isdigit():
            by_db[db] = int(size)
    return {name: by_db.get(prod_db_name(team, name), 0) for name in names}


def compute_prod_storage(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    """Measure one production: database size (one catalog query on the
    production cluster) and workspace disk size (filestore, repo, sessions —
    a full walk, hence the cache). Productions use a plain filestore
    directory, so there is no overlay merged view to skip."""
    from oduflow.docker_ops.production_ops import prod_db_name

    return {
        "db_bytes": _db_size_bytes(
            settings, prod_db_name(team, name), settings.prod_db_container
        ),
        "disk_bytes": _prod_disk_bytes(team, name),
        "computed_at": _utcnow_iso(),
    }


def refresh_prod_storage(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    """Recompute one production's storage entry and persist it in the cache."""
    entry = compute_prod_storage(settings, team, name)
    _store_storage_entry(team, "productions", name, entry)
    return entry


def refresh_team_storage(settings: Settings, team: TeamSettings) -> dict[str, Any]:
    """Recompute storage for every environment and production plus team totals.

    The team disk total covers the whole team data dir (workspaces — production
    ones included — templates, shared repos, dumps) minus overlay merged views;
    the team DB total is the same single pg_database catalog query the quota
    check uses, so it covers the shared dev cluster only. Production databases
    live on the production cluster, outside that quota, and are totalled
    separately as ``prod_db_bytes``. This is the entry point external billing
    tooling calls per team.

    A production cluster that cannot be queried leaves the production DB sizes
    (per production and the total) as None instead of failing the refresh.
    """
    from oduflow import production_registry
    from oduflow.docker_ops import env_ops
    from oduflow.docker_ops.system_ops import get_team_db_usage_bytes

    env_names = [e["env_name"] for e in env_ops.list_environments(settings, team)]
    envs = {name: compute_env_storage(settings, team, name) for name in env_names}
    prod_names = list(production_registry.list_productions(team))
    # No productions, no query: such a team may have no production cluster.
    prod_db = _prod_db_sizes(settings, team, prod_names) if prod_names else {}
    prods = {
        name: {
            "db_bytes": None if prod_db is None else prod_db[name],
            "disk_bytes": _prod_disk_bytes(team, name),
            "computed_at": _utcnow_iso(),
        }
        for name in prod_names
    }

    skip: set[str] = set()
    for name in env_names:
        skip |= _env_merged_filestore_skip(name, team)
    cache = {
        "envs": envs,
        "productions": prods,
        "team": {
            "db_bytes": get_team_db_usage_bytes(get_client(), settings, team),
            "prod_db_bytes": None if prod_db is None else sum(prod_db.values()),
            "disk_bytes": _dir_size_bytes(team.data_dir, skip),
            "computed_at": _utcnow_iso(),
        },
    }
    with keyed_mutex(storage_stats_lock_key(team.team_id)):
        _write_storage_cache(team, cache)
    return cache


# ── Default resource limits for environment containers ───────────────────────

_GB = 1024**3


def default_env_limits() -> dict[str, Any]:
    """Resource limits applied to every environment container.

    Auto-derived from host size — no config knobs: memory is a quarter of
    host RAM clamped to [2 GB, 8 GB] (Odoo installs and test runs are
    memory-hungry; the cap keeps one tenant's environment from taking the
    machine), plus a pids ceiling against fork bombs. CPU gets no hard
    quota: it is compressible, the kernel's fair scheduler already splits it
    evenly under contention, and a cap would only slow work on an idle host.
    """
    mem = _read_memory_linux()
    if mem is None and sys.platform == "darwin":
        mem = _read_memory_macos()
    total_bytes = int(mem[0] * 1024 * 1024) if mem else 0
    if total_bytes:
        mem_limit = min(8 * _GB, max(2 * _GB, total_bytes // 4))
    else:
        mem_limit = 4 * _GB
    return {"mem_limit": mem_limit, "pids_limit": 4096}
