"""Unit tests for cloning remote extra-addons repos (clone_extra_repo).

These exercise real git (no network, no Docker): a local source repo with two
branches is cloned via a file:// URL, and we assert the clone is shallow yet
keeps every branch (--depth 1 --no-single-branch), so one bare repo still serves
worktrees for any Odoo version.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oduflow.extra_addons import (
    clone_extra_repo,
    create_worktree,
    delete_extra_repo,
    ensure_shared_checkout,
    fetch_extra_repo,
    list_extra_repos,
    list_remote_branches,
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


class TestCloneExtraRepoShallow:
    def test_clone_is_shallow(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        result = clone_extra_repo(team, "enterprise", url)

        assert _is_shallow(result["path"])

    def test_clone_keeps_all_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

        repos = list_extra_repos(team)
        assert len(repos) == 1
        assert set(repos[0]["branches"]) == {"main", "18.0"}

    def test_worktree_from_non_default_branch(self, team, tmp_path):
        """A shallow, all-branches clone still checks out any branch's tip."""
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        # views.xml only exists on the 18.0 branch.
        assert (wt / "sale_enterprise" / "views.xml").is_file()

    def test_worktree_fetches_new_commits_on_branch(self, team, tmp_path):
        """create_worktree's targeted single-branch fetch pulls updates.

        A commit added to 18.0 *after* the clone must land in the worktree,
        proving the per-branch fetch runs instead of relying on stale clone
        data (and without a slow ``--all`` over every branch).
        """
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

        src = tmp_path / "source"

        def _git(*args):
            subprocess.run(
                ["git", "-C", str(src), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        _git("checkout", "18.0")
        (src / "sale_enterprise" / "new.xml").write_text("<odoo/>")
        _git("add", "-A")
        _git(*_GIT_ID, "commit", "-m", "18.0 update")
        _git("checkout", "main")

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        # new.xml was committed after the clone → only present if fetched.
        assert (wt / "sale_enterprise" / "new.xml").is_file()


class TestCloneSelectedBranches:
    def test_clone_keeps_only_selected_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        result = clone_extra_repo(team, "enterprise", url, branches=["18.0"])

        assert _is_shallow(result["path"])
        repos = list_extra_repos(team)
        assert set(repos[0]["branches"]) == {"18.0"}

    def test_invalid_branch_name_rejected(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        with pytest.raises(ValueError):
            clone_extra_repo(team, "enterprise", url, branches=["--upload-pack=x"])
        # Nothing left behind: the repo can still be added afterwards.
        assert list_extra_repos(team) == []

    def test_update_only_touches_selected_branches(self, team, tmp_path):
        """fetch --all respects the restricted refspec: a branch created
        upstream after the clone must not appear."""
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url, branches=["18.0"])

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
        clone_extra_repo(team, "enterprise", url, branches=["18.0"])

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
        assert [b["branch"] for b in summary["updated_branches"]] == ["main"]

    def test_worktree_from_selected_branch(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url, branches=["18.0"])

        checkout = ensure_shared_checkout(team, "enterprise", "18.0")
        assert (Path(checkout["path"]) / "sale_enterprise" / "views.xml").is_file()

    def test_empty_branch_list_means_all_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url, branches=[])

        assert set(list_extra_repos(team)[0]["branches"]) == {"main", "18.0"}


class TestListRemoteBranches:
    def test_lists_branches_without_cloning(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        assert list_remote_branches(team, url) == ["18.0", "main"]
        assert list_extra_repos(team) == []


class TestSharedCheckoutCache:
    def test_same_revision_reuses_one_checkout(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

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
        clone_extra_repo(team, "enterprise", url)
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
        clone_extra_repo(team, "enterprise", url)

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
        clone_extra_repo(team, "enterprise", url)
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
    from oduflow.errors import ExternalCommandError

    url = _make_git_source(tmp_path)
    clone_extra_repo(team, "enterprise", url, branches=["18.0"])
    with pytest.raises(ExternalCommandError):
        track_branch(team, "enterprise", "missing")
    fetch_extra_repo(team, "enterprise")
    assert set(list_extra_repos(team)[0]["branches"]) == {"18.0"}
