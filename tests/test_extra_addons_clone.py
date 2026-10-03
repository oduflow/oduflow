"""Unit tests for remote extra-addons repos (register_extra_repo & fetching).

These exercise real git (no network, no Docker): a local source repo with two
branches is served via a file:// URL. Registration downloads nothing; each
branch is fetched shallow on first use and incrementally afterwards.
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oduflow.extra_addons import (
    branch_usage,
    create_worktree,
    delete_extra_repo,
    ensure_shared_checkout,
    fetch_extra_repo,
    list_extra_repos,
    list_remote_branches,
    refresh_remote_branches,
    register_extra_repo,
    track_branch,
    track_branches,
    untrack_branch,
)
from oduflow.settings import Settings, TeamSettings

_GIT_ID = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
]


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


def _make_git_source(tmp_path):
    """Create a normal git repo with branches ``main`` and ``18.0``.

    Returns a ``file://`` URL — git only honours ``--depth`` for remote-style
    URLs; a plain local path is cloned in full (with a warning).
    """
    src = tmp_path / "source"
    mod = src / "sale_enterprise"
    mod.mkdir(parents=True)

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(src), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init")
    _git("checkout", "-b", "main")
    (mod / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}")
    _git("add", "-A")
    _git(*_GIT_ID, "commit", "-m", "main commit")

    _git("checkout", "-b", "18.0")
    (mod / "views.xml").write_text("<odoo/>")
    _git("add", "-A")
    _git(*_GIT_ID, "commit", "-m", "18.0 commit")

    _git("checkout", "main")
    return f"file://{src}"


def _is_shallow(bare_path):
    out = subprocess.run(
        ["git", "-C", bare_path, "rev-parse", "--is-shallow-repository"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == "true"


def _src_git(tmp_path, *args):
    return subprocess.run(
        ["git", "-C", str(tmp_path / "source"), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bare_git(team, *args):
    return subprocess.run(
        ["git", "-C", os.path.join(team.shared_repos_dir, "enterprise"), *args],
        capture_output=True,
        text=True,
    )


def _commit_on(tmp_path, branch, message):
    _src_git(tmp_path, "checkout", branch)
    _src_git(tmp_path, *_GIT_ID, "commit", "--allow-empty", "-m", message)
    _src_git(tmp_path, "checkout", "main")
    return _src_git(tmp_path, "rev-parse", branch)


def _refspecs(team):
    out = _bare_git(team, "config", "--get-all", "remote.origin.fetch").stdout
    return out.split()


class TestRegisterOnDemand:
    def test_register_downloads_nothing(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        result = register_extra_repo(team, "enterprise", url)

        assert result["branches"] == []
        assert result["available_branches"] == ["18.0", "main"]
        assert _bare_git(team, "for-each-ref").stdout == ""
        assert _refspecs(team) == []
        repo = list_extra_repos(team)[0]
        assert repo["branches"] == []
        assert repo["available_branches"] == ["18.0", "main"]

    def test_first_use_downloads_only_that_branch_shallow(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        _commit_on(tmp_path, "18.0", "more history")
        register_extra_repo(team, "enterprise", url)

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        # views.xml only exists on the 18.0 branch.
        assert (wt / "sale_enterprise" / "views.xml").is_file()
        assert _is_shallow(os.path.join(team.shared_repos_dir, "enterprise"))
        assert _bare_git(team, "rev-list", "--count", "18.0").stdout.strip() == "1"
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]
        # The branch joined the tracked set, so Update keeps refreshing it.
        assert _refspecs(team) == ["+refs/heads/18.0:refs/heads/18.0"]

    def test_update_without_downloaded_branches_does_not_fetch(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        _src_git(tmp_path, "checkout", "-b", "19.0")
        _src_git(tmp_path, "checkout", "main")

        summary = fetch_extra_repo(team, "enterprise")

        assert summary["up_to_date"] is True
        path = os.path.join(team.shared_repos_dir, "enterprise")
        # A refspec-less fetch would have pulled the remote HEAD into FETCH_HEAD,
        # and the legacy repair would have restored the wildcard refspec.
        assert not os.path.exists(os.path.join(path, "FETCH_HEAD"))
        assert _refspecs(team) == []
        assert list_extra_repos(team)[0]["available_branches"] == [
            "18.0",
            "19.0",
            "main",
        ]

    def test_update_refreshes_downloaded_branch_incrementally(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        old = ensure_shared_checkout(team, "enterprise", "18.0")["revision"]
        _commit_on(tmp_path, "18.0", "one")
        new = _commit_on(tmp_path, "18.0", "two")

        summary = fetch_extra_repo(team, "enterprise")

        assert summary["updated_branches"] == [{"branch": "18.0", "new_commits": 2}]
        assert _bare_git(team, "rev-parse", "18.0").stdout.strip() == new
        # The old tip stays an ancestor, so gc keeps the SHA rollback resets to.
        assert _bare_git(team, "merge-base", "--is-ancestor", old, new).returncode == 0
        # Branches never used stay undownloaded.
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]

    def test_checkout_refetch_keeps_old_revision_reachable(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        first = ensure_shared_checkout(team, "enterprise", "18.0")
        new = _commit_on(tmp_path, "18.0", "update")

        second = ensure_shared_checkout(
            team, "enterprise", "18.0", current_revision=first["revision"]
        )

        assert second["revision"] == new
        assert (
            _bare_git(
                team, "merge-base", "--is-ancestor", first["revision"], new
            ).returncode
            == 0
        )

    def test_update_untracks_branch_deleted_on_remote(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        _src_git(tmp_path, "branch", "-D", "18.0")

        # A leftover refspec for the deleted branch would fail every fetch.
        summary = fetch_extra_repo(team, "enterprise")

        assert summary["deleted_branches"] == ["18.0"]
        assert _refspecs(team) == []
        assert list_extra_repos(team)[0]["branches"] == []
        assert Path(checkout["path"], "sale_enterprise", "views.xml").is_file()
        assert fetch_extra_repo(team, "enterprise")["up_to_date"] is True

    def test_missing_branch_on_first_use_is_not_found(self, team, tmp_path):
        from oduflow.errors import NotFoundError

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        with pytest.raises(NotFoundError, match="Branch '17.0'"):
            ensure_shared_checkout(team, "enterprise", "17.0")
        assert _refspecs(team) == []

    def test_register_same_name_concurrently_conflicts(self, team, tmp_path):
        from oduflow.errors import ConflictError

        url = _make_git_source(tmp_path)

        def _register(_index):
            try:
                register_extra_repo(team, "enterprise", url, branches=["18.0"])
                return "ok"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(_register, range(2)))

        assert outcomes == ["conflict", "ok"]
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]


class TestRegisterSelectedBranches:
    def test_register_downloads_selected_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        result = register_extra_repo(team, "enterprise", url, branches=["18.0"])

        assert _is_shallow(result["path"])
        repos = list_extra_repos(team)
        assert set(repos[0]["branches"]) == {"18.0"}

    def test_invalid_branch_name_rejected(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        with pytest.raises(ValueError):
            register_extra_repo(team, "enterprise", url, branches=["--upload-pack=x"])
        # Nothing left behind: the repo can still be added afterwards.
        assert list_extra_repos(team) == []

    def test_branch_missing_on_remote_rejected(self, team, tmp_path):
        from oduflow.errors import NotFoundError

        url = _make_git_source(tmp_path)
        with pytest.raises(NotFoundError, match="17.0"):
            register_extra_repo(team, "enterprise", url, branches=["17.0"])
        assert list_extra_repos(team) == []

    def test_failed_download_leaves_nothing_behind(self, team, tmp_path):
        from oduflow.errors import ExternalCommandError

        url = _make_git_source(tmp_path)
        with patch(
            "oduflow.extra_addons._fetch_branches_unlocked",
            side_effect=ExternalCommandError("git fetch", -1, "Fetch timed out."),
        ):
            with pytest.raises(ExternalCommandError):
                register_extra_repo(team, "enterprise", url, branches=["18.0"])
        assert list_extra_repos(team) == []
        register_extra_repo(team, "enterprise", url, branches=["18.0"])

    def test_update_only_touches_selected_branches(self, team, tmp_path):
        """fetch --all respects the restricted refspec: a branch created
        upstream after registration must not be downloaded."""
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])

        src = tmp_path / "source"
        subprocess.run(
            ["git", "-C", str(src), "checkout", "-b", "19.0"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(src), *_GIT_ID, "commit", "--allow-empty", "-m", "19.0"],
            check=True,
            capture_output=True,
        )

        summary = fetch_extra_repo(team, "enterprise")
        assert summary["new_branches"] == []
        assert set(list_extra_repos(team)[0]["branches"]) == {"18.0"}

    def test_track_branch_adds_branch(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])

        result = track_branch(team, "enterprise", "main")
        assert result["tracked"] is True
        assert set(list_extra_repos(team)[0]["branches"]) == {"18.0", "main"}

        # Tracked branches are updated by later plain fetches too.
        src = tmp_path / "source"
        subprocess.run(
            ["git", "-C", str(src), *_GIT_ID, "commit", "--allow-empty", "-m", "more"],
            check=True,
            capture_output=True,
        )
        summary = fetch_extra_repo(team, "enterprise")
        assert summary["updated_branches"] == [{"branch": "main", "new_commits": 1}]

    def test_worktree_from_selected_branch(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])

        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        assert (Path(checkout["path"]) / "sale_enterprise" / "views.xml").is_file()


class TestLegacyAllBranchesRepo:
    """Repos cloned before on-demand registration keep the wildcard refspec."""

    def _legacy_clone(self, team, url):
        path = os.path.join(team.shared_repos_dir, "enterprise")
        os.makedirs(team.shared_repos_dir, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--bare", "--depth", "1", "--no-single-branch", url, path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                path,
                "config",
                "remote.origin.fetch",
                "+refs/heads/*:refs/heads/*",
            ],
            check=True,
            capture_output=True,
        )

    def test_update_still_fetches_new_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        self._legacy_clone(team, url)
        _src_git(tmp_path, "checkout", "-b", "19.0")
        _src_git(tmp_path, "checkout", "main")

        summary = fetch_extra_repo(team, "enterprise")

        assert summary["new_branches"] == ["19.0"]
        assert _refspecs(team) == ["+refs/heads/*:refs/heads/*"]

    def test_subset_repo_keeps_empty_tracked_set_after_branch_deletion(
        self, team, tmp_path
    ):
        """A pre-marker subset repo whose last branch is deleted upstream must
        not be mistaken for an old clone missing its wildcard refspec."""
        url = _make_git_source(tmp_path)
        path = os.path.join(team.shared_repos_dir, "enterprise")
        os.makedirs(team.shared_repos_dir)
        for args in (
            ["init", "--bare", path],
            ["-C", path, "config", "remote.origin.url", url],
            [
                "-C",
                path,
                "config",
                "remote.origin.fetch",
                "+refs/heads/18.0:refs/heads/18.0",
            ],
            ["-C", path, "fetch", "--depth", "1", "origin"],
        ):
            subprocess.run(["git", *args], check=True, capture_output=True)
        _src_git(tmp_path, "branch", "-D", "18.0")

        assert fetch_extra_repo(team, "enterprise")["deleted_branches"] == ["18.0"]
        assert fetch_extra_repo(team, "enterprise")["new_branches"] == []

        assert _refspecs(team) == []
        assert list_extra_repos(team)[0]["branches"] == []

    def test_first_use_of_branch_does_not_add_refspec(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        self._legacy_clone(team, url)

        ensure_shared_checkout(team, "enterprise", "18.0")

        assert _refspecs(team) == ["+refs/heads/*:refs/heads/*"]

    def test_worktree_fetches_new_commits_on_branch(self, team, tmp_path):
        """create_worktree's targeted single-branch fetch pulls updates."""
        url = _make_git_source(tmp_path)
        self._legacy_clone(team, url)

        src = tmp_path / "source"
        _src_git(tmp_path, "checkout", "18.0")
        (src / "sale_enterprise" / "new.xml").write_text("<odoo/>")
        _src_git(tmp_path, "add", "-A")
        _src_git(tmp_path, *_GIT_ID, "commit", "-m", "18.0 update")
        _src_git(tmp_path, "checkout", "main")

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        # new.xml was committed after the clone → only present if fetched.
        assert (wt / "sale_enterprise" / "new.xml").is_file()


class TestListRemoteBranches:
    def test_lists_branches_without_cloning(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        assert list_remote_branches(team, url) == ["18.0", "main"]
        assert list_extra_repos(team) == []


class TestSharedCheckoutCache:
    def test_same_revision_reuses_one_checkout(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)

        first = ensure_shared_checkout(team, "enterprise", "18.0")
        second = ensure_shared_checkout(team, "enterprise", "18.0")

        assert first["path"] == second["path"]
        assert first["revision"] == second["revision"]
        assert first["path"].endswith(first["revision"])
        assert (tmp_path / "data" / "shared_extra_checkouts").is_dir()

    def test_branch_update_creates_new_checkout_and_keeps_old_immutable(
        self, team, tmp_path
    ):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        first = ensure_shared_checkout(team, "enterprise", "18.0")

        src = tmp_path / "source"
        subprocess.run(
            ["git", "-C", str(src), "checkout", "18.0"],
            check=True,
            capture_output=True,
        )
        new_file = src / "sale_enterprise" / "new.xml"
        new_file.write_text("<odoo/>")
        subprocess.run(
            ["git", "-C", str(src), "add", "-A"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(src), *_GIT_ID, "commit", "-m", "update"],
            check=True,
            capture_output=True,
        )

        second = ensure_shared_checkout(
            team,
            "enterprise",
            "18.0",
            current_revision=first["revision"],
        )

        assert second["path"] != first["path"]
        assert second["revision"] != first["revision"]
        assert "sale_enterprise/new.xml" in second["changed_files"]
        assert not (Path(first["path"]) / "sale_enterprise" / "new.xml").exists()
        assert (Path(second["path"]) / "sale_enterprise" / "new.xml").is_file()

    def test_concurrent_requests_share_checkout(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: ensure_shared_checkout(team, "enterprise", "18.0"),
                    range(2),
                )
            )

        assert results[0]["path"] == results[1]["path"]

    def test_delete_repo_removes_all_cached_revisions(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        cache_root = tmp_path / "data" / "shared_extra_checkouts" / "enterprise"
        assert cache_root.is_dir()

        client = MagicMock()
        client.containers.list.return_value = []
        with patch("oduflow.extra_addons.get_client", return_value=client):
            delete_extra_repo(Settings(), team, "enterprise")

        assert not cache_root.exists()
        assert not (tmp_path / "data" / "shared_repos" / "enterprise").exists()
        assert not os.path.exists(checkout["path"])


def test_failed_track_branch_does_not_break_subsequent_updates(team, tmp_path):
    from oduflow.errors import NotFoundError

    url = _make_git_source(tmp_path)
    register_extra_repo(team, "enterprise", url, branches=["18.0"])
    with pytest.raises(NotFoundError):
        track_branch(team, "enterprise", "missing")
    fetch_extra_repo(team, "enterprise")
    assert set(list_extra_repos(team)[0]["branches"]) == {"18.0"}


def _age_checkouts(team, name="enterprise", seconds=7 * 3600):
    """Push cached checkouts past the grace period that protects fresh ones."""
    root = os.path.join(team.shared_extra_checkouts_dir, name)
    past = time.time() - seconds
    for entry in os.listdir(root):
        os.utime(os.path.join(root, entry), (past, past))


def _no_containers(*labels_list):
    """A Docker client whose managed containers carry *labels_list*."""
    client = MagicMock()
    containers = []
    for labels in labels_list:
        c = MagicMock()
        c.name = labels.get("oduflow.branch", "container")
        c.labels = labels
        containers.append(c)
    client.containers.list.return_value = containers
    return patch("oduflow.extra_addons.get_client", return_value=client)


class TestTrackBranches:
    def test_downloads_several_branches_at_once(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["main"])

        result = track_branches(team, "enterprise", ["18.0", "main", "18.0"])

        assert result["branches"] == ["18.0", "main"]
        assert result["downloaded"] == ["18.0"]
        assert result["updated"] == ["main"]
        assert set(list_extra_repos(team)[0]["branches"]) == {"18.0", "main"}

    def test_missing_branch_downloads_nothing(self, team, tmp_path):
        from oduflow.errors import NotFoundError

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)

        with pytest.raises(NotFoundError, match="nope"):
            track_branches(team, "enterprise", ["18.0", "nope"])
        assert list_extra_repos(team)[0]["branches"] == []

    def test_refreshes_remote_list_before_checking(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        _src_git(tmp_path, "branch", "19.0", "main")

        track_branches(team, "enterprise", ["19.0"])

        assert "19.0" in list_extra_repos(team)[0]["available_branches"]

    def test_refresh_remote_branches_downloads_nothing(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        _src_git(tmp_path, "branch", "19.0", "main")

        assert refresh_remote_branches(team, "enterprise") == ["18.0", "19.0", "main"]
        assert list_extra_repos(team)[0]["branches"] == []


class TestUntrackBranch:
    def test_removes_branch_and_its_unused_checkout(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0", "main"])
        removed = ensure_shared_checkout(team, "enterprise", "18.0")
        kept = ensure_shared_checkout(team, "enterprise", "main")
        _age_checkouts(team)

        with _no_containers():
            result = untrack_branch(Settings(), team, "enterprise", "18.0")

        assert result["removed_checkouts"] == [removed["revision"]]
        assert result["freed_bytes"] > 0
        assert not os.path.exists(removed["path"])
        assert os.listdir(os.path.dirname(removed["path"])) == [kept["revision"]]
        assert os.path.isdir(kept["path"])
        assert "removed" not in _bare_git(team, "worktree", "list").stdout
        assert list_extra_repos(team)[0]["branches"] == ["main"]
        assert _refspecs(team) == ["+refs/heads/main:refs/heads/main"]
        # Update keeps it gone; first use downloads it again.
        assert fetch_extra_repo(team, "enterprise")["new_branches"] == []
        assert (
            ensure_shared_checkout(team, "enterprise", "18.0")["revision"]
            == removed["revision"]
        )

    def test_refused_while_an_environment_uses_it(self, team, tmp_path):
        from oduflow.errors import ConflictError

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "18.0"}',
        }
        with _no_containers(labels), pytest.raises(ConflictError, match="feature-x"):
            untrack_branch(Settings(), team, "enterprise", "18.0")
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]

    def test_refused_while_a_production_uses_it(self, team, tmp_path):
        from oduflow import production_registry
        from oduflow.errors import ConflictError

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])
        production_registry.create_production(
            team, "erp", {"extra_addons": {"enterprise": "18.0"}}
        )
        with _no_containers(), pytest.raises(ConflictError, match="production erp"):
            untrack_branch(Settings(), team, "enterprise", "18.0")

    def test_other_branch_in_use_does_not_block(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0", "main"])
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "main"}',
        }
        with _no_containers(labels):
            untrack_branch(Settings(), team, "enterprise", "18.0")
        assert list_extra_repos(team)[0]["branches"] == ["main"]

    def test_branch_not_downloaded(self, team, tmp_path):
        from oduflow.errors import NotFoundError

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        with _no_containers(), pytest.raises(NotFoundError, match="not downloaded"):
            untrack_branch(Settings(), team, "enterprise", "18.0")

    def test_legacy_all_branches_repo_switches_to_explicit_list(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        TestLegacyAllBranchesRepo()._legacy_clone(team, url)

        with _no_containers():
            untrack_branch(Settings(), team, "enterprise", "18.0")

        assert _refspecs(team) == ["+refs/heads/main:refs/heads/main"]
        summary = fetch_extra_repo(team, "enterprise")
        assert summary["new_branches"] == []
        assert list_extra_repos(team)[0]["branches"] == ["main"]

    def test_branch_usage_maps_consumers(self, team, tmp_path):
        from oduflow import production_registry

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        production_registry.create_production(
            team, "erp", {"extra_addons": {"enterprise": "18.0"}}
        )
        env = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "18.0", "themes": "main"}',
        }
        prod = {
            "oduflow.prod_name": "erp",
            "oduflow.extra_addons": '{"enterprise": "18.0"}',
        }
        other = {"oduflow.branch": "y", "oduflow.extra_addons": '{"themes": "18.0"}'}
        with _no_containers(env, prod, other):
            usage = branch_usage(Settings(), team, "enterprise")
        assert usage == {"18.0": ["feature-x", "production erp"]}

    def test_keeps_a_checkout_handed_out_recently(self, team, tmp_path):
        # create_environment holds its checkout before its container exists.
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0", "main"])
        checkout = ensure_shared_checkout(team, "enterprise", "18.0")

        with _no_containers():
            result = untrack_branch(Settings(), team, "enterprise", "18.0")

        assert result["removed_checkouts"] == []
        assert os.path.isfile(
            os.path.join(checkout["path"], "sale_enterprise", "views.xml")
        )

    def test_reusing_a_checkout_renews_its_grace_period(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0", "main"])
        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        _age_checkouts(team)
        ensure_shared_checkout(team, "enterprise", "18.0")

        with _no_containers():
            result = untrack_branch(Settings(), team, "enterprise", "18.0")

        assert result["removed_checkouts"] == []
        assert os.path.isdir(checkout["path"])

    def test_pinned_checkout_is_kept(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0", "main"])
        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        _age_checkouts(team)
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": '{"enterprise": "main"}',
            "oduflow.extra_addons_revisions": (
                '{"enterprise": "%s"}' % checkout["revision"]
            ),
        }
        with _no_containers(labels):
            result = untrack_branch(Settings(), team, "enterprise", "18.0")

        assert result["removed_checkouts"] == []
        assert os.path.isdir(checkout["path"])

    def test_refused_for_a_protected_repo(self, team, tmp_path):
        from oduflow.errors import ProtectedError
        from oduflow.extra_addons import protect_extra_repo

        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url, branches=["18.0"])
        protect_extra_repo(team, "enterprise")

        with _no_containers(), pytest.raises(ProtectedError, match="protected"):
            untrack_branch(Settings(), team, "enterprise", "18.0")
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]

    def test_branch_sharing_a_tag_name(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        _src_git(tmp_path, "tag", "18.0", "main")
        TestLegacyAllBranchesRepo()._legacy_clone(team, url)
        _bare_git(team, "fetch", "--tags", "origin")
        assert "18.0" in _bare_git(team, "tag").stdout

        assert set(list_extra_repos(team)[0]["branches"]) == {"18.0", "main"}
        with _no_containers():
            untrack_branch(Settings(), team, "enterprise", "main")

        assert _refspecs(team) == ["+refs/heads/18.0:refs/heads/18.0"]
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]

    def test_interrupted_legacy_conversion_keeps_all_branches(self, team, tmp_path):
        from oduflow import extra_addons

        url = _make_git_source(tmp_path)
        TestLegacyAllBranchesRepo()._legacy_clone(team, url)
        real_run = subprocess.run

        def failing_run(cmd, *args, **kwargs):
            if "--add" in cmd:
                raise subprocess.CalledProcessError(255, cmd, stderr="lock")
            return real_run(cmd, *args, **kwargs)

        with (
            _no_containers(),
            patch.object(extra_addons.subprocess, "run", side_effect=failing_run),
            pytest.raises(subprocess.CalledProcessError),
        ):
            untrack_branch(Settings(), team, "enterprise", "18.0")

        assert "+refs/heads/*:refs/heads/*" in _refspecs(team)
        assert set(list_extra_repos(team)[0]["branches"]) == {"18.0", "main"}
        # A retry completes the conversion.
        with _no_containers():
            untrack_branch(Settings(), team, "enterprise", "18.0")
        assert _refspecs(team) == ["+refs/heads/main:refs/heads/main"]
