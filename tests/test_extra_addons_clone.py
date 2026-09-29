"""Unit tests for remote extra-addons repos (register_extra_repo & fetching).

These exercise real git (no network, no Docker): a local source repo with two
branches is served via a file:// URL. Registration downloads nothing; each
branch is fetched shallow on first use and incrementally afterwards.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oduflow.extra_addons import (
    create_worktree,
    delete_extra_repo,
    ensure_shared_checkout,
    fetch_extra_repo,
    list_extra_repos,
    list_remote_branches,
    register_extra_repo,
    track_branch,
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
