"""Extra-addons repos pinned to an exact commit (``repo:branch@<sha40>``).

Real git (no network, no Docker): the source repo is served via a file:// URL,
like in test_extra_addons_clone.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from test_extra_addons_clone import (
    _bare_git,
    _commit_on,
    _make_git_source,
    _no_containers,
    _src_git,
)

from oduflow import server, web_ui
from oduflow.docker_ops import env_ops
from oduflow.errors import ConflictError, NotFoundError
from oduflow.extra_addons import (
    branch_usage,
    checkout_revision,
    create_local_repo,
    create_worktree,
    ensure_shared_checkout,
    pull_extra_worktree,
    register_extra_repo,
    split_extra_addon_spec,
    untrack_branch,
    validate_extra_addon_spec,
)
from oduflow.settings import Settings, TeamSettings

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


# --- spec parsing ------------------------------------------------------------


class TestSpecParsing:
    def test_split_plain_branch(self):
        assert split_extra_addon_spec("19.0") == ("19.0", "")

    def test_split_pinned(self):
        assert split_extra_addon_spec(f"19.0@{SHA}") == ("19.0", SHA)

    def test_split_is_lenient_for_non_sha_suffix(self):
        # A stored value whose suffix is not a full commit id keeps meaning a
        # plain branch name.
        assert split_extra_addon_spec("feat@x") == ("feat@x", "")

    def test_validate_plain_branch_unchanged(self):
        assert validate_extra_addon_spec("oca-web", "19.0") == "19.0"

    def test_validate_normalizes_uppercase_sha(self):
        assert (
            validate_extra_addon_spec("oca-web", f"19.0@{SHA.upper()}") == f"19.0@{SHA}"
        )

    @pytest.mark.parametrize(
        "spec",
        [
            "19.0@abc123",  # short sha
            f"19.0@{SHA}0",  # 41 chars
            f"19.0@{SHA[:-1]}g",  # non-hex
            "19.0@",
        ],
    )
    def test_validate_rejects_bad_sha(self, spec):
        with pytest.raises(ValueError, match="40-character"):
            validate_extra_addon_spec("oca-web", spec)

    def test_validate_rejects_missing_branch(self):
        with pytest.raises(ValueError, match="needs a branch"):
            validate_extra_addon_spec("oca-web", f"@{SHA}")

    @pytest.mark.parametrize(
        "parse", [server._parse_extra_addons, web_ui._parse_extra_addons]
    )
    def test_parse_extra_addons_mixed(self, parse):
        assert parse(f"enterprise:19.0, oca-web:19.0@{SHA}") == {
            "enterprise": "19.0",
            "oca-web": f"19.0@{SHA}",
        }

    @pytest.mark.parametrize(
        "parse", [server._parse_extra_addons, web_ui._parse_extra_addons]
    )
    def test_parse_extra_addons_rejects_bad_pin(self, parse):
        with pytest.raises(ValueError, match="oca-web"):
            parse("oca-web:19.0@deadbeef")

    @pytest.mark.parametrize(
        "parse", [server._parse_extra_addons, web_ui._parse_extra_addons]
    )
    def test_parse_extra_addons_still_requires_branch(self, parse):
        with pytest.raises(ValueError, match="must include a branch"):
            parse("oca-web")


# --- shared (development) checkouts ------------------------------------------


def _pinned_setup(team, tmp_path):
    """Register the repo; return (old 18.0 sha, new 18.0 tip sha)."""
    url = _make_git_source(tmp_path)
    register_extra_repo(team, "enterprise", url)
    old = _src_git(tmp_path, "rev-parse", "18.0")
    new = _commit_on(tmp_path, "18.0", "newer 18.0 commit")
    return old, new


class TestPinnedSharedCheckout:
    def test_checkout_is_made_at_the_pinned_commit_not_the_tip(self, team, tmp_path):
        old, new = _pinned_setup(team, tmp_path)

        checkout = ensure_shared_checkout(team, "enterprise", f"18.0@{old}")

        assert checkout["revision"] == old
        assert checkout["pinned"] is True
        assert checkout["branch"] == "18.0"
        assert checkout["path"].endswith(old)
        assert checkout_revision(checkout["path"]) == old
        # The branch tip was never resolved/downloaded.
        assert _bare_git(team, "rev-parse", "--verify", "refs/heads/18.0").returncode
        assert old != new

    def test_pinned_commit_already_present_needs_no_fetch(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        register_extra_repo(team, "enterprise", url)
        tip = ensure_shared_checkout(team, "enterprise", "18.0")["revision"]

        with patch("oduflow.extra_addons._remote_git_env") as remote:
            pinned = ensure_shared_checkout(team, "enterprise", f"18.0@{tip}")

        remote.assert_not_called()
        assert pinned["revision"] == tip
        assert (
            pinned["path"] == ensure_shared_checkout(team, "enterprise", "18.0")["path"]
        )

    def test_missing_commit_raises_and_never_falls_back(self, team, tmp_path):
        _pinned_setup(team, tmp_path)

        with pytest.raises(NotFoundError, match=f"'{SHA}'.*'enterprise'"):
            ensure_shared_checkout(team, "enterprise", f"18.0@{SHA}")

        cache = os.path.join(team.shared_extra_checkouts_dir, "enterprise")
        assert not os.path.isdir(cache) or os.listdir(cache) == []

    def test_update_does_not_move_a_pinned_repo(self, team, tmp_path):
        old, _new = _pinned_setup(team, tmp_path)
        first = ensure_shared_checkout(team, "enterprise", f"18.0@{old}")
        _commit_on(tmp_path, "18.0", "yet another commit")

        again = ensure_shared_checkout(
            team, "enterprise", f"18.0@{old}", current_revision=first["revision"]
        )

        assert again["revision"] == old
        assert again["path"] == first["path"]
        assert again["changed_files"] == []

    def test_unpinned_still_follows_the_tip(self, team, tmp_path):
        old, new = _pinned_setup(team, tmp_path)
        assert ensure_shared_checkout(team, "enterprise", "18.0")["revision"] == new
        newest = _commit_on(tmp_path, "18.0", "tip moves again")
        moved = ensure_shared_checkout(team, "enterprise", "18.0", current_revision=new)
        assert moved["revision"] == newest
        assert moved["pinned"] is False
        assert old not in (new, newest)

    def test_local_repo_missing_commit_is_not_found(self, team, tmp_path):
        src = tmp_path / "local_src"
        (src / "mod").mkdir(parents=True)
        (src / "mod" / "__manifest__.py").write_text("{'name': 'x'}")
        create_local_repo(team, "localrepo", str(src), "18.0")

        with pytest.raises(NotFoundError, match="local extra repo 'localrepo'"):
            ensure_shared_checkout(team, "localrepo", f"18.0@{SHA}")


class TestPinnedBranchBookkeeping:
    def test_pinned_environment_counts_as_a_branch_user(self, team, tmp_path):
        old, _new = _pinned_setup(team, tmp_path)
        ensure_shared_checkout(team, "enterprise", "18.0")
        labels = {
            "oduflow.branch": "feature-x",
            "oduflow.extra_addons": json.dumps({"enterprise": f"18.0@{old}"}),
            "oduflow.extra_addons_revisions": json.dumps({"enterprise": old}),
        }
        with _no_containers(labels):
            assert branch_usage(Settings(), team, "enterprise") == {
                "18.0": ["feature-x"]
            }
            with pytest.raises(ConflictError, match="feature-x"):
                untrack_branch(Settings(), team, "enterprise", "18.0")


# --- production (mutable per-deployment) worktrees ---------------------------


class TestPinnedProductionWorktree:
    def test_create_and_pull_stay_on_the_pinned_commit(self, team, tmp_path):
        old, _new = _pinned_setup(team, tmp_path)
        wt = str(tmp_path / "prod" / "extra" / "enterprise")

        create_worktree(team, "enterprise", f"18.0@{old}", wt)
        assert checkout_revision(wt) == old

        _commit_on(tmp_path, "18.0", "moves the tip")
        assert pull_extra_worktree(team, "enterprise", f"18.0@{old}", wt) == ("", [])
        assert checkout_revision(wt) == old

    def test_create_with_missing_commit_fails(self, team, tmp_path):
        _pinned_setup(team, tmp_path)
        wt = str(tmp_path / "prod" / "extra" / "enterprise")
        with pytest.raises(NotFoundError, match=SHA):
            create_worktree(team, "enterprise", f"18.0@{SHA}", wt)
        assert not os.path.exists(wt)


# --- pull_and_apply on a pinned development environment ----------------------


def test_pull_environment_keeps_pinned_repo_in_place(tmp_path):
    """End to end through env_ops.pull_environment with real git checkouts:
    the branch moved upstream, but the pinned mount is neither switched nor
    reported as changed."""
    env_team = TeamSettings(team_id="1", data_dir=str(tmp_path / "env_team"))
    settings = Settings(teams={"1": env_team})
    old, _new = _pinned_setup(env_team, tmp_path)
    pinned = ensure_shared_checkout(env_team, "enterprise", f"18.0@{old}")
    _commit_on(tmp_path, "18.0", "upstream keeps moving")

    repo = tmp_path / "env_team" / "workspaces" / "env" / "repo"
    repo.mkdir(parents=True)
    container = MagicMock()
    container.labels = {
        "oduflow.git_branch": "main",
        "oduflow.extra_addons": json.dumps({"enterprise": f"18.0@{old}"}),
        "oduflow.extra_addons_revisions": json.dumps({"enterprise": old}),
    }
    container.attrs = {
        "HostConfig": {
            "Binds": [
                f"{repo}:/mnt/extra-addons:rw",
                f"{pinned['path']}:/mnt/extra-addons-enterprise:ro",
            ]
        }
    }
    client = MagicMock()
    client.containers.get.return_value = container

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch("oduflow.git_ops.pull_repo", return_value=("", [])),
        patch("oduflow.docker_ops.env_ops.update_environment") as update,
    ):
        result = env_ops.pull_environment(settings, env_team, "env")

    update.assert_not_called()
    assert not result.get("extra_addons_cache_migrated")
    assert checkout_revision(pinned["path"]) == old
