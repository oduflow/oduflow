import json
import os
import threading
import time
from unittest.mock import patch

from oduflow.docker_ops import stats
from oduflow.docker_ops.stats import (
    _dir_size_bytes,
    read_storage_cache,
    refresh_env_storage,
    refresh_prod_storage,
    refresh_team_storage,
)
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))


def _make_workspace(team: TeamSettings, env: str, overlay: bool) -> str:
    ws = os.path.join(team.workspaces_dir, env)
    os.makedirs(os.path.join(ws, "repo"))
    with open(os.path.join(ws, "repo", "module.py"), "w") as f:
        f.write("x" * 1000)
    merged = os.path.join(ws, "filestore")
    os.makedirs(merged)
    with open(os.path.join(merged, "blob"), "w") as f:
        f.write("y" * 50_000)
    if overlay:
        upper = os.path.join(ws, "filestore_upper")
        os.makedirs(upper)
        with open(os.path.join(upper, "own-blob"), "w") as f:
            f.write("z" * 2000)
    return ws


def test_read_cache_missing(tmp_path):
    assert read_storage_cache(_team(tmp_path)) == {
        "envs": {},
        "productions": {},
        "team": None,
    }


def test_read_cache_without_productions_key(tmp_path):
    """Cache files written before per-production storage stats still load."""
    team = _team(tmp_path)
    os.makedirs(team.data_dir)
    with open(os.path.join(team.data_dir, "storage_stats.json"), "w") as f:
        json.dump({"envs": {"main": {"db_bytes": 1}}, "team": None}, f)

    cache = read_storage_cache(team)

    assert cache["envs"] == {"main": {"db_bytes": 1}}
    assert cache["productions"] == {}


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="9000")
def test_malformed_cache_sections_read_as_empty(mock_sql, mock_client, tmp_path):
    """A ``null``/non-dict section must not break the next single refresh."""
    team = _team(tmp_path)
    os.makedirs(team.data_dir)
    with open(os.path.join(team.data_dir, "storage_stats.json"), "w") as f:
        json.dump({"envs": [], "productions": None, "team": "x"}, f)

    assert read_storage_cache(team) == {"envs": {}, "productions": {}, "team": None}

    entry = refresh_prod_storage(Settings(), team, "erp")

    assert read_storage_cache(team)["productions"] == {"erp": entry}


def test_directory_size_counts_hardlinked_template_files_once(tmp_path):
    first = tmp_path / "templates" / "base" / "filestore" / "aa" / "blob"
    second = tmp_path / "templates" / "derived" / "filestore" / "aa" / "blob"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"x" * 4096)
    os.link(first, second)

    assert _dir_size_bytes(str(tmp_path)) == 4096


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="12345")
def test_refresh_env_counts_upper_not_merged(mock_sql, mock_client, tmp_path):
    team = _team(tmp_path)
    _make_workspace(team, "main", overlay=True)

    entry = refresh_env_storage(Settings(), team, "main")

    assert entry["db_bytes"] == 12345
    # repo (1000) + upper (2000); the merged overlay view (50000) holds the
    # template's lower layer and duplicates upper — excluded.
    assert entry["disk_bytes"] == 3000
    assert entry["computed_at"]
    # Persisted and readable back.
    assert read_storage_cache(team)["envs"]["main"] == entry


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="500")
def test_refresh_env_plain_filestore_is_counted(mock_sql, mock_client, tmp_path):
    team = _team(tmp_path)
    _make_workspace(team, "plain", overlay=False)

    entry = refresh_env_storage(Settings(), team, "plain")

    # No overlay upper dir → the filestore is a plain copy owned by the env.
    assert entry["disk_bytes"] == 51_000


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="9000")
def test_refresh_prod_storage(mock_sql, mock_client, tmp_path):
    team = _team(tmp_path)
    # Production workspaces are plain directories (no overlay).
    _make_workspace(team, "prod-erp", overlay=False)

    entry = refresh_prod_storage(Settings(), team, "erp")

    assert entry["db_bytes"] == 9000
    assert entry["disk_bytes"] == 51_000
    assert entry["computed_at"]
    # The size query runs against the production cluster, for the prod DB.
    assert mock_sql.call_args.kwargs["container_name"] == Settings().prod_db_container
    assert "oduflow_1_prod-erp" in mock_sql.call_args.args[2]
    # Persisted and readable back, separate from env entries.
    cache = read_storage_cache(team)
    assert cache["productions"]["erp"] == entry
    assert cache["envs"] == {}


def _cluster_sql(prod_rows):
    """Fake ``_exec_sql``: ``prod_rows`` (catalog rows, or an exception to
    raise) on the production cluster, "100" for shared-cluster size queries."""

    def fake(client, settings, sql, db="postgres", *, container_name=None, user=None):
        if container_name == settings.prod_db_container:
            if isinstance(prod_rows, Exception):
                raise prod_rows
            return prod_rows
        return "100"

    return fake


@patch("oduflow.production_registry.list_productions")
@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops.get_team_db_usage_bytes", return_value=777)
@patch("oduflow.docker_ops.system_ops._exec_sql")
@patch("oduflow.docker_ops.env_ops.list_environments")
def test_refresh_team_totals(
    mock_list, mock_sql, mock_team_db, mock_client, mock_prods, tmp_path
):
    mock_sql.side_effect = _cluster_sql("oduflow_1_prod-erp|100\npostgres|7")
    team = _team(tmp_path)
    _make_workspace(team, "main", overlay=True)
    _make_workspace(team, "prod-erp", overlay=False)
    os.makedirs(os.path.join(team.data_dir, "templates", "prod"))
    with open(os.path.join(team.data_dir, "templates", "prod", "dump.sql"), "w") as f:
        f.write("d" * 4000)
    mock_list.return_value = [{"env_name": "main"}]
    mock_prods.return_value = {"erp": {}}

    cache = refresh_team_storage(Settings(), team)

    assert cache["envs"]["main"]["db_bytes"] == 100
    assert cache["envs"]["main"]["disk_bytes"] == 3000
    assert cache["productions"]["erp"]["db_bytes"] == 100
    assert cache["productions"]["erp"]["disk_bytes"] == 51_000
    # The team DB total stays the shared-cluster (quota) figure; production
    # databases are totalled separately.
    assert cache["team"]["db_bytes"] == 777
    assert cache["team"]["prod_db_bytes"] == 100
    # Whole team dir minus the merged overlay view: env workspace (3000) +
    # prod workspace (51000) + template dump (4000); the storage cache file
    # itself is written after measuring, so it is not counted.
    assert cache["team"]["disk_bytes"] == 58_000
    assert read_storage_cache(team)["team"] == cache["team"]


class TestDefaultEnvLimits:
    def _limits(self, monkeypatch, total_mb):
        from oduflow.docker_ops import stats

        monkeypatch.setattr(
            stats, "_read_memory_linux", lambda: (total_mb, 0.0) if total_mb else None
        )
        monkeypatch.setattr(stats, "_read_memory_macos", lambda: None)
        return stats.default_env_limits()

    def test_quarter_of_host(self, monkeypatch):
        assert self._limits(monkeypatch, 16 * 1024)["mem_limit"] == 4 * 1024**3

    def test_floor_2gb(self, monkeypatch):
        assert self._limits(monkeypatch, 4 * 1024)["mem_limit"] == 2 * 1024**3

    def test_cap_8gb(self, monkeypatch):
        assert self._limits(monkeypatch, 128 * 1024)["mem_limit"] == 8 * 1024**3

    def test_unknown_host_fallback(self, monkeypatch):
        limits = self._limits(monkeypatch, 0)
        assert limits["mem_limit"] == 4 * 1024**3
        assert limits["pids_limit"] == 4096


@patch("oduflow.production_registry.list_productions")
@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops.get_team_db_usage_bytes", return_value=777)
@patch("oduflow.docker_ops.system_ops._exec_sql")
@patch("oduflow.docker_ops.env_ops.list_environments", return_value=[])
def test_refresh_team_sizes_all_production_dbs_in_one_query(
    mock_list, mock_sql, mock_team_db, mock_client, mock_prods, tmp_path
):
    team = _team(tmp_path)
    mock_prods.return_value = {"erp": {}, "shop": {}, "new": {}}
    mock_sql.side_effect = _cluster_sql(
        "oduflow_1_prod-erp|5000\n"
        "oduflow_1_prod-shop|300\n"
        # Another team's production and system databases are not this team's.
        "oduflow_2_prod-erp|99999\n"
        "postgres|7"
    )

    cache = refresh_team_storage(Settings(), team)

    prod_calls = [
        c
        for c in mock_sql.call_args_list
        if c.kwargs.get("container_name") == Settings().prod_db_container
    ]
    assert len(prod_calls) == 1
    assert cache["productions"]["erp"]["db_bytes"] == 5000
    assert cache["productions"]["shop"]["db_bytes"] == 300
    # Registered, but its database does not exist (yet): 0, like an env.
    assert cache["productions"]["new"]["db_bytes"] == 0
    assert cache["team"]["prod_db_bytes"] == 5300


@patch("oduflow.production_registry.list_productions", return_value={})
@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops.get_team_db_usage_bytes", return_value=777)
@patch("oduflow.docker_ops.system_ops._exec_sql")
@patch("oduflow.docker_ops.env_ops.list_environments", return_value=[])
def test_refresh_team_without_productions_skips_production_cluster(
    mock_list, mock_sql, mock_team_db, mock_client, mock_prods, tmp_path
):
    """A team with no productions may have no production cluster at all."""
    cache = refresh_team_storage(Settings(), _team(tmp_path))

    mock_sql.assert_not_called()
    assert cache["productions"] == {}
    assert cache["team"]["prod_db_bytes"] == 0


@patch("oduflow.production_registry.list_productions")
@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops.get_team_db_usage_bytes", return_value=777)
@patch("oduflow.docker_ops.system_ops._exec_sql")
@patch("oduflow.docker_ops.env_ops.list_environments")
def test_refresh_team_survives_unreachable_production_cluster(
    mock_list, mock_sql, mock_team_db, mock_client, mock_prods, tmp_path
):
    """A stopped/recreating production cluster must not cost billing the
    environment and team totals: only the production DB sizes go unknown."""
    team = _team(tmp_path)
    _make_workspace(team, "main", overlay=True)
    _make_workspace(team, "prod-erp", overlay=False)
    mock_list.return_value = [{"env_name": "main"}]
    mock_prods.return_value = {"erp": {}}
    mock_sql.side_effect = _cluster_sql(
        ExternalCommandError("psql", 2, "connection refused")
    )

    cache = refresh_team_storage(Settings(), team)

    assert cache["envs"]["main"]["db_bytes"] == 100
    assert cache["productions"]["erp"]["db_bytes"] is None
    assert cache["productions"]["erp"]["disk_bytes"] == 51_000
    assert cache["team"]["db_bytes"] == 777
    assert cache["team"]["prod_db_bytes"] is None
    assert cache["team"]["disk_bytes"] == 54_000
    # The cache is still written.
    assert read_storage_cache(team) == cache


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="1")
def test_concurrent_single_refreshes_keep_each_others_entries(
    mock_sql, mock_client, tmp_path
):
    """Refresh handlers run on parallel threads; an unserialised
    read-modify-write of the cache file would drop one of the entries."""
    team = _team(tmp_path)
    real_read = stats.read_storage_cache

    def slow_read(t):
        cache = real_read(t)
        time.sleep(0.05)  # hold the stale snapshot across the other writer
        return cache

    errors: list[BaseException] = []

    def run(fn, name):
        try:
            fn(Settings(), team, name)
        except BaseException as exc:  # surfaced below, not lost in the thread
            errors.append(exc)

    with patch("oduflow.docker_ops.stats.read_storage_cache", side_effect=slow_read):
        threads = [
            threading.Thread(target=run, args=(refresh_env_storage, f"env{i}"))
            for i in range(3)
        ] + [
            threading.Thread(target=run, args=(refresh_prod_storage, f"prod{i}"))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    cache = read_storage_cache(team)
    assert sorted(cache["envs"]) == ["env0", "env1", "env2"]
    assert sorted(cache["productions"]) == ["prod0", "prod1", "prod2"]
    # Unique temp files, all renamed into place or cleaned up.
    assert os.listdir(team.data_dir) == ["storage_stats.json"]
