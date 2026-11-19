"""Unit tests for cleanup of unused shared extra-addons checkouts (issue #347).

Per-SHA checkouts a moving upstream branch leaves behind were never removed:
only `delete_extra_repo` and `untrack_branch` dropped them, and `oduflow
cleanup` did not know the group. These exercise the scan/removal pair in
extra_addons and their wiring into system_ops.cleanup_orphans, with real git
repos (no network, no Docker).
"""

import os
import subprocess
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from oduflow import extra_addons
from oduflow.extra_addons import (
    ensure_shared_checkout,
    list_unused_checkouts,
    register_extra_repo,
    remove_unused_checkouts,
)
from oduflow.settings import Settings, TeamSettings
from tests.test_extra_addons_clone import (
    _age_checkouts,
    _bare_git,
    _commit_on,
    _make_git_source,
    _no_containers,
)


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


def _superseded_checkout(team, tmp_path):
    """An aged checkout of the old 18.0 tip plus the current-tip checkout."""
    url = _make_git_source(tmp_path)
    register_extra_repo(team, "enterprise", url, branches=["18.0"])
    old = ensure_shared_checkout(team, "enterprise", "18.0")
    _commit_on(tmp_path, "18.0", "move the branch")
    new = ensure_shared_checkout(team, "enterprise", "18.0")
    _age_checkouts(team)
    return old, new


def _containers(*specs):
    """A Docker client whose managed containers carry ``(labels, attrs)``."""
    client = MagicMock()
    containers = []
    for labels, attrs in specs:
        c = MagicMock()
        c.name = labels.get("oduflow.branch", "container")
        c.labels = labels
        c.attrs = attrs
        containers.append(c)
    client.containers.list.return_value = containers
    return patch("oduflow.extra_addons.get_client", return_value=client), client


def _git_fails(*_args, **_kwargs):
    raise subprocess.CalledProcessError(128, "git for-each-ref")


class TestListUnusedCheckouts:
    def test_superseded_checkout_is_unused_and_sized(self, team, tmp_path):
        old, new = _superseded_checkout(team, tmp_path)

        with _no_containers():
            result = list_unused_checkouts(Settings(), team)

        label = f"enterprise/{old['revision']}"
        assert result["checkouts"] == [label]
        assert result["sizes"][label] > 0
        assert f"enterprise/{new['revision']}" not in result["checkouts"]

    def test_pinned_checkout_is_kept(self, team, tmp_path):
        old, _new = _superseded_checkout(team, tmp_path)
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "18.0"}',
            "oduflow.extra_addons_revisions": (
                '{"enterprise": "%s"}' % old["revision"]
            ),
        }

        with _no_containers(labels):
            result = list_unused_checkouts(Settings(), team)

        assert result["checkouts"] == []

    def test_fresh_and_foreign_entries_are_kept(self, team, tmp_path):
        # create_environment holds its checkout before its container (the
        # in-use record) exists: the grace period must protect it.
        old, _new = _superseded_checkout(team, tmp_path)
        os.utime(old["path"])
        os.mkdir(os.path.join(os.path.dirname(old["path"]), "not-a-revision"))

        with _no_containers():
            result = list_unused_checkouts(Settings(), team)

        assert result["checkouts"] == []

    def test_no_cache_dir(self, team):
        with _no_containers():
            assert list_unused_checkouts(Settings(), team) == {
                "checkouts": [],
                "sizes": {},
                "errors": [],
            }

    @pytest.mark.parametrize(
        "labels",
        [
            {},
            {"oduflow.extra_addons_revisions": "{}"},
            {"oduflow.extra_addons_revisions": "not json"},
            {"oduflow.extra_addons_revisions": '["enterprise"]'},
        ],
    )
    @pytest.mark.parametrize("mount_key", ["binds", "mounts"])
    def test_bind_mounted_checkout_is_kept_without_a_usable_label(
        self, team, tmp_path, labels, mount_key
    ):
        # The mount is what a deletion would break: it pins the checkout even
        # when the revisions label is missing, empty or malformed.
        old, _new = _superseded_checkout(team, tmp_path)
        if mount_key == "binds":
            binds = [old["path"] + ":/mnt/extra-addons-enterprise:ro"]
            attrs = {"HostConfig": {"Binds": binds}}
        else:
            attrs = {
                "HostConfig": {"Binds": None},
                "Mounts": [{"Type": "bind", "Source": old["path"]}],
            }
        patcher, _client = _containers((labels, attrs))

        with patcher:
            scan = list_unused_checkouts(Settings(), team)
            result = remove_unused_checkouts(Settings(), team)

        assert scan["checkouts"] == []
        assert result["removed"] == []
        assert os.path.isdir(old["path"])

    def test_unreadable_branches_keep_every_checkout(self, team, tmp_path):
        # Without the branch tips the keep set is unknown: fail closed.
        old, _new = _superseded_checkout(team, tmp_path)

        with (
            _no_containers(),
            patch.object(extra_addons, "_read_branch_refs", _git_fails),
        ):
            result = list_unused_checkouts(Settings(), team)

        assert result["checkouts"] == []
        assert len(result["errors"]) == 1
        assert "enterprise" in result["errors"][0]
        assert os.path.isdir(old["path"])

    def test_one_broken_repo_does_not_hide_the_others(self, team, tmp_path):
        old, _new = _superseded_checkout(team, tmp_path)
        # A second cache directory that cannot be listed.
        os.mkdir(os.path.join(team.shared_extra_checkouts_dir, "broken"))
        real_listdir = os.listdir

        def listdir(path):
            if os.path.basename(path) == "broken":
                raise PermissionError(13, "Permission denied", path)
            return real_listdir(path)

        with _no_containers(), patch("oduflow.extra_addons.os.listdir", listdir):
            result = list_unused_checkouts(Settings(), team)

        assert result["checkouts"] == [f"enterprise/{old['revision']}"]
        assert len(result["errors"]) == 1
        assert "broken" in result["errors"][0]

    def test_docker_failure_is_reported_not_raised(self, team, tmp_path):
        _superseded_checkout(team, tmp_path)
        client = MagicMock()
        client.containers.list.side_effect = RuntimeError("docker is down")

        with patch("oduflow.extra_addons.get_client", return_value=client):
            scan = list_unused_checkouts(Settings(), team)
            result = remove_unused_checkouts(Settings(), team)

        assert scan["checkouts"] == [] and len(scan["errors"]) == 1
        assert result["removed"] == [] and len(result["errors"]) == 1

    def test_containers_are_listed_once(self, team, tmp_path):
        _superseded_checkout(team, tmp_path)
        os.mkdir(os.path.join(team.shared_extra_checkouts_dir, "second"))
        patcher, client = _containers()

        with patcher:
            list_unused_checkouts(Settings(), team)
            assert client.containers.list.call_count == 1
            remove_unused_checkouts(Settings(), team)
            assert client.containers.list.call_count == 2


class TestRemoveUnusedCheckouts:
    def test_removes_superseded_checkout_and_keeps_tip(self, team, tmp_path):
        old, new = _superseded_checkout(team, tmp_path)

        with _no_containers():
            result = remove_unused_checkouts(Settings(), team)

        assert result["removed"] == [f"enterprise/{old['revision']}"]
        assert result["freed_bytes"] > 0
        assert result["errors"] == []
        assert not os.path.exists(old["path"])
        assert os.path.isdir(new["path"])
        assert old["revision"] not in _bare_git(team, "worktree", "list").stdout
        # The pinned-by-nothing tip is reusable afterwards.
        assert (
            ensure_shared_checkout(team, "enterprise", "18.0")["revision"]
            == new["revision"]
        )

    def test_only_limits_removal_to_reviewed_names(self, team, tmp_path):
        old, _new = _superseded_checkout(team, tmp_path)

        with _no_containers():
            none = remove_unused_checkouts(Settings(), team, only=[])
            assert none["removed"] == []
            assert os.path.isdir(old["path"])
            some = remove_unused_checkouts(
                Settings(), team, only=[f"enterprise/{old['revision']}"]
            )

        assert some["removed"] == [f"enterprise/{old['revision']}"]
        assert not os.path.exists(old["path"])

    def test_checkout_pinned_after_scan_survives(self, team, tmp_path):
        # The unused set is recomputed under the repo lock: a container that
        # appeared between scan and confirm keeps its checkout alive even when
        # the reviewed names still list it.
        old, _new = _superseded_checkout(team, tmp_path)
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "18.0"}',
            "oduflow.extra_addons_revisions": (
                '{"enterprise": "%s"}' % old["revision"]
            ),
        }

        with _no_containers(labels):
            result = remove_unused_checkouts(
                Settings(), team, only=[f"enterprise/{old['revision']}"]
            )

        assert result["removed"] == []
        assert os.path.isdir(old["path"])

    def test_unreadable_branches_remove_nothing(self, team, tmp_path):
        old, _new = _superseded_checkout(team, tmp_path)

        with (
            _no_containers(),
            patch.object(extra_addons, "_read_branch_refs", _git_fails),
        ):
            result = remove_unused_checkouts(Settings(), team)

        assert result["removed"] == []
        assert len(result["errors"]) == 1
        assert os.path.isdir(old["path"])

    def test_busy_repo_is_skipped_instead_of_waited_for(self, team, tmp_path):
        # add/update_extra_repo can hold the repo lock through an hour-long
        # fetch; cleanup (under the team lock) must not queue behind it.
        old, _new = _superseded_checkout(team, tmp_path)
        held = threading.Event()
        release = threading.Event()

        def hold_repo_lock():
            with extra_addons._repo_operation_lock(team, "enterprise"):
                held.set()
                release.wait(30)

        holder = threading.Thread(target=hold_repo_lock)
        holder.start()
        try:
            assert held.wait(10)
            started = time.monotonic()
            with (
                _no_containers(),
                patch.object(extra_addons, "_CLEANUP_LOCK_WAIT_SECONDS", 0.05),
            ):
                result = remove_unused_checkouts(Settings(), team)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            holder.join(10)

        assert elapsed < 5
        assert result["removed"] == []
        assert len(result["errors"]) == 1
        assert "another operation" in result["errors"][0]
        assert os.path.isdir(old["path"])
        # Once the repo is free the same call removes the checkout.
        with _no_containers():
            assert remove_unused_checkouts(Settings(), team)["removed"] == [
                f"enterprise/{old['revision']}"
            ]

    def test_staging_failure_keeps_what_was_already_moved(self, team, tmp_path):
        # Two superseded checkouts; renaming the second one fails. The first
        # has already left the cache, so it is still deleted, reported and
        # pruned from the bare repo; only the second is reported as failed.
        first, second = _superseded_checkout(team, tmp_path)
        _commit_on(tmp_path, "18.0", "move the branch again")
        ensure_shared_checkout(team, "enterprise", "18.0")
        _age_checkouts(team)
        cache_dir = os.path.dirname(first["path"])
        superseded = sorted([first["revision"], second["revision"]])
        real_rename = os.rename

        def rename(src, dst):
            if os.path.basename(src) == superseded[1]:
                raise OSError(16, "Device or resource busy", src)
            return real_rename(src, dst)

        with _no_containers(), patch("oduflow.extra_addons.os.rename", rename):
            result = remove_unused_checkouts(Settings(), team)

        assert result["removed"] == [f"enterprise/{superseded[0]}"]
        assert result["freed_bytes"] > 0
        assert len(result["errors"]) == 1
        assert superseded[1] in result["errors"][0]
        assert not os.path.exists(os.path.join(cache_dir, superseded[0]))
        assert os.path.isdir(os.path.join(cache_dir, superseded[1]))
        # No empty holder is left behind for the checkout that stayed.
        assert not [e for e in os.listdir(cache_dir) if e.startswith(".removing-")]
        worktrees = _bare_git(team, "worktree", "list").stdout
        assert superseded[0] not in worktrees
        assert superseded[1] in worktrees

    def test_each_removed_checkout_is_sized_once(self, team, tmp_path):
        _superseded_checkout(team, tmp_path)

        with (
            _no_containers(),
            patch.object(
                extra_addons, "_dir_size_bytes", wraps=extra_addons._dir_size_bytes
            ) as size,
        ):
            result = remove_unused_checkouts(Settings(), team)

        assert len(result["removed"]) == 1
        assert size.call_count == 1

    def test_stale_removal_leftover_is_collected(self, team, tmp_path):
        old, _new = _superseded_checkout(team, tmp_path)
        cache_dir = os.path.dirname(old["path"])
        holder = tempfile.mkdtemp(prefix=".removing-", dir=cache_dir)
        past = time.time() - 7 * 3600
        os.utime(holder, (past, past))
        label = f"enterprise/{os.path.basename(holder)}"

        with _no_containers():
            scan = list_unused_checkouts(Settings(), team)
            result = remove_unused_checkouts(Settings(), team, only=[label])

        assert label in scan["checkouts"]
        assert result["removed"] == [label]
        assert not os.path.exists(holder)


class TestCleanupOrphansWiring:
    """cleanup_orphans reports and removes the new group."""

    def _run(self, team, settings, dry_run, only=None):
        from oduflow.docker_ops import system_ops

        with (
            patch.object(system_ops, "get_client", return_value=_FakeClient()),
            patch.object(system_ops, "_exec_sql", return_value=""),
            patch("oduflow.port_registry._load_registry", return_value={}),
            patch("oduflow.port_registry._save_registry"),
            _no_containers(),
        ):
            return system_ops.cleanup_orphans(
                settings, team, dry_run=dry_run, only=only
            )

    def _team_and_settings(self, tmp_path):
        team = TeamSettings(
            team_id="1",
            data_dir=str(tmp_path / "data"),
            port_registry_path=str(tmp_path / "ports.json"),
        )
        return team, Settings(base_data_dir=str(tmp_path), teams={"1": team})

    def test_dry_run_lists_unused_checkouts_with_sizes(self, tmp_path):
        team, settings = self._team_and_settings(tmp_path)
        old, _new = _superseded_checkout(team, tmp_path)
        label = f"enterprise/{old['revision']}"

        result = self._run(team, settings, dry_run=True)

        assert result["orphan_extra_checkouts"] == [label]
        assert result["orphan_extra_checkout_sizes"][label] > 0
        assert os.path.isdir(old["path"])

    def test_force_removes_and_reports_freed_bytes(self, tmp_path):
        team, settings = self._team_and_settings(tmp_path)
        old, new = _superseded_checkout(team, tmp_path)

        result = self._run(team, settings, dry_run=False)

        assert result["orphan_extra_checkouts"] == [f"enterprise/{old['revision']}"]
        assert result["extra_checkouts_freed_bytes"] > 0
        assert not os.path.exists(old["path"])
        assert os.path.isdir(new["path"])

    def test_force_does_not_scan_and_size_first(self, tmp_path):
        team, settings = self._team_and_settings(tmp_path)
        _superseded_checkout(team, tmp_path)

        with patch.object(extra_addons, "list_unused_checkouts") as scan:
            result = self._run(team, settings, dry_run=False)

        scan.assert_not_called()
        assert len(result["orphan_extra_checkouts"]) == 1

    def test_checkout_scan_problem_keeps_the_rest_of_the_report(self, tmp_path):
        team, settings = self._team_and_settings(tmp_path)
        _superseded_checkout(team, tmp_path)
        os.makedirs(os.path.join(team.workspaces_dir, "orphan-env"))

        with patch.object(extra_addons, "_read_branch_refs", _git_fails):
            result = self._run(team, settings, dry_run=True)

        assert result["orphan_workspaces"] == ["orphan-env"]
        assert result["orphan_extra_checkouts"] == []
        assert len(result["errors"]) == 1

    def test_only_filter_applies_to_checkouts(self, tmp_path):
        team, settings = self._team_and_settings(tmp_path)
        old, _new = _superseded_checkout(team, tmp_path)

        result = self._run(
            team, settings, dry_run=False, only={"orphan_extra_checkouts": []}
        )

        assert result["orphan_extra_checkouts"] == []
        assert os.path.isdir(old["path"])


class _FakeContainers:
    def list(self, all=False, filters=None):  # noqa: A002 - matches docker SDK
        return []


class _FakeClient:
    containers = _FakeContainers()
