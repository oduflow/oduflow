"""Opt-in deletion of an environment's git branch on origin.

``delete_environment(..., delete_branch=True)`` pushes a ``--delete`` from the
environment's local clone, after the container and database are gone and
before the workspace is removed. Strictly opt-in, and every refusal (unmerged
commits, another environment on the branch, denied push, live-mount, no
container to read the branch from) is a returned warning — the teardown itself
never fails over git.

Real git, no Docker and no network: a local ``file://`` origin plus the same
shallow single-branch clone Oduflow provisions (as in
test_extra_addons_clone.py / test_clone_repo_errors.py); Docker is a mock.
"""

import contextlib
import os
import pathlib
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import env_ops
from oduflow.naming import get_repo_path, get_workspace_path
from oduflow.settings import Settings, TeamSettings

_GIT_ID = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
]

ENV = "dev1"
BRANCH = "feature/x"


@pytest.fixture
def team(tmp_path):
    return TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "data"),
        port_registry_path=str(tmp_path / "data" / "ports.json"),
    )


@pytest.fixture
def settings(team):
    return Settings(routing_mode="traefik", teams={"1": team})


def _src_git(src, *args):
    return subprocess.run(
        ["git", "-C", str(src), *_GIT_ID, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """file:// origin with ``main`` and a merged ``feature/x`` branch."""
    src = tmp_path / "source"
    src.mkdir()
    _src_git(src, "init", "-b", "main")
    (src / "README.md").write_text("hello\n")
    _src_git(src, "add", ".")
    _src_git(src, "commit", "-m", "init")
    # Same tip as main: merged, nothing unmerged to lose.
    _src_git(src, "branch", BRANCH)
    return f"file://{src}"


def _remote_branches(origin_url):
    src = origin_url[len("file://") :]
    out = _src_git(src, "branch", "--format=%(refname:short)")
    return set(out.split())


def _commit_on_origin(origin_url, branch, message, count=1):
    src = origin_url[len("file://") :]
    _src_git(src, "checkout", branch)
    for _ in range(count):
        _src_git(src, "commit", "--allow-empty", "-m", message)
    _src_git(src, "checkout", "main")


def _branch_on_origin(origin_url, branch, start="main"):
    src = origin_url[len("file://") :]
    _src_git(src, "branch", branch, start)


def _merge_on_origin(origin_url, branch, into):
    """A real merge commit of *branch* into *into* (never a fast-forward)."""
    src = origin_url[len("file://") :]
    _src_git(src, "checkout", into)
    _src_git(src, "merge", "--no-ff", "-m", f"Merge {branch}", branch)
    _src_git(src, "checkout", "main")


def _squash_on_origin(origin_url, branch, into):
    src = origin_url[len("file://") :]
    _src_git(src, "checkout", into)
    _src_git(src, "merge", "--squash", branch)
    _src_git(src, "commit", "--allow-empty", "-m", f"Squash {branch}")
    _src_git(src, "checkout", "main")


def _clone_git(repo_path, *args):
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _reject_pushes(origin_url, message):
    src = origin_url[len("file://") :]
    hook = pathlib.Path(src) / ".git" / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n")
    hook.chmod(0o755)


def _provision(team, origin_url, env_name=ENV, branch=BRANCH):
    """The managed checkout delete_environment pushes from: a shallow
    single-branch clone, exactly as create_environment makes it."""
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    os.makedirs(get_workspace_path(env_name, team.workspaces_dir), exist_ok=True)
    env_ops._clone_repo(origin_url, branch, repo_path, team)
    return repo_path


def _labels(origin_url, branch=BRANCH, env_name=ENV, **extra):
    labels = {
        "oduflow.managed": "true",
        "oduflow.team": "1",
        "oduflow.branch": env_name,
        "oduflow.repo": origin_url,
        "oduflow.git_branch": branch,
    }
    labels.update(extra)
    return labels


def _mock_container(labels):
    container = MagicMock()
    container.labels = labels
    return container


def _client(own_labels=None, other_containers=()):
    client = MagicMock()
    if own_labels is None:
        client.containers.get.side_effect = docker.errors.NotFound("nf")
    else:
        client.containers.get.return_value = _mock_container(own_labels)
    client.containers.list.return_value = [
        _mock_container(labels) for labels in other_containers
    ]
    return client


@contextlib.contextmanager
def _teardown_mocks(client):
    """Mock the Docker/PG side of delete_environment; git and files are real."""
    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch.object(env_ops, "_agent_remove_env"),
        patch.object(env_ops, "_exec_sql"),
        patch.object(env_ops, "_drop_pg_role"),
    ):
        yield


class TestDeleteEnvironmentBranch:
    def test_default_leaves_the_branch_on_origin(self, settings, team, origin):
        _provision(team, origin)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(settings, team, ENV)

        assert warnings == []
        assert BRANCH in _remote_branches(origin)
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))

    def test_merged_unused_branch_is_deleted(self, settings, team, origin):
        _provision(team, origin)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert warnings == []
        assert BRANCH not in _remote_branches(origin)
        assert "main" in _remote_branches(origin)
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))

    def test_unmerged_branch_is_kept_with_a_warning(self, settings, team, origin):
        _commit_on_origin(origin, BRANCH, "work in progress")
        _provision(team, origin)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "commits that are not merged into 'main'" in warnings[0]
        # The refusal says how to finish the job by hand.
        assert f"git push origin --delete {BRANCH}" in warnings[0]
        assert BRANCH in _remote_branches(origin)
        # The environment itself is still gone.
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))

    def test_branch_used_by_another_environment_is_kept(self, settings, team, origin):
        _provision(team, origin)
        # A stopped second environment on the same branch and repo blocks the
        # deletion too — stopped containers are part of the scan.
        other = _labels(origin, env_name="dev2")
        client = _client(_labels(origin), other_containers=[other])

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "dev2" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_legacy_environment_without_the_git_branch_label_blocks(
        self, settings, team, origin
    ):
        # Containers older than the oduflow.git_branch label track the branch
        # they are named after — the same fallback this environment gets.
        _branch_on_origin(origin, "hotfix")
        _provision(team, origin, branch="hotfix")
        legacy = _labels(origin, env_name="hotfix")
        del legacy["oduflow.git_branch"]
        # A managed non-environment container (no environment label) is
        # neither a user of the branch nor a reason to fail.
        service = {"oduflow.managed": "true", "oduflow.team": "1"}
        client = _client(
            _labels(origin, branch="hotfix"), other_containers=[service, legacy]
        )

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "still used by environment(s) hotfix" in warnings[0]
        assert "hotfix" in _remote_branches(origin)

    def test_the_environments_own_container_does_not_block(
        self, settings, team, origin
    ):
        _provision(team, origin)
        own = _labels(origin)
        # The scan sees the container being deleted; it must not count as
        # "another environment" using the branch.
        client = _client(own, other_containers=[own])

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert warnings == []
        assert BRANCH not in _remote_branches(origin)

    def test_same_branch_of_another_repo_does_not_block(self, settings, team, origin):
        _provision(team, origin)
        other = _labels("https://github.com/acme/other.git", env_name="dev2")
        client = _client(_labels(origin), other_containers=[other])

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert warnings == []
        assert BRANCH not in _remote_branches(origin)

    def test_denied_push_is_a_warning_not_a_failure(self, settings, team, origin):
        _provision(team, origin)
        _reject_pushes(origin, "ERROR: Permission to acme/repo.git denied to bot.")
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "cannot delete branches" in warnings[0]
        assert BRANCH in _remote_branches(origin)
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))

    def test_live_mounted_environment_is_skipped(self, settings, team, origin):
        os.makedirs(get_workspace_path(ENV, team.workspaces_dir), exist_ok=True)
        client = _client(_labels(origin, **{"oduflow.local_path": "/home/ada/addons"}))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "live-mounted" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_the_default_branch_is_never_deleted(self, settings, team, origin):
        _provision(team, origin, branch="main")
        client = _client(_labels(origin, branch="main"))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "default branch" in warnings[0]
        assert "main" in _remote_branches(origin)

    def test_missing_local_clone_is_a_warning(self, settings, team, origin):
        os.makedirs(get_workspace_path(ENV, team.workspaces_dir), exist_ok=True)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "no local git clone" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_a_missing_container_never_guesses_the_branch_from_the_name(
        self, settings, team, origin
    ):
        # Environment "staging" was created from branch "develop"; its
        # container is gone but the workspace remains. origin also has an
        # unrelated, merged "staging" branch. With no labels the branch would
        # be guessed from the environment name — and the wrong one deleted.
        _branch_on_origin(origin, "develop")
        _branch_on_origin(origin, "staging")
        _provision(team, origin, env_name="staging", branch="develop")
        client = _client(None)

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, "staging", delete_branch=True
            )

        assert len(warnings) == 1
        assert "container is gone" in warnings[0]
        assert {"staging", "develop"} <= _remote_branches(origin)
        client.containers.list.assert_not_called()
        # The teardown of what is left still completes.
        assert not os.path.exists(get_workspace_path("staging", team.workspaces_dir))

    def test_another_teams_container_is_not_a_source_of_labels(
        self, settings, team, origin
    ):
        _provision(team, origin)
        foreign = _labels(origin)
        foreign["oduflow.team"] = "2"
        client = _client(foreign)

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "container is gone" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_a_failed_teardown_leaves_the_branch_alone(self, settings, team, origin):
        # The push-delete is irreversible, so it must come after every step
        # that can still abort the teardown: a branch deleted ahead of a
        # container that then refuses to stop would leave a live environment
        # whose branch is gone.
        _provision(team, origin)
        client = _client(_labels(origin))
        client.containers.get.return_value.stop.side_effect = docker.errors.APIError(
            "cannot stop container"
        )

        with _teardown_mocks(client), pytest.raises(docker.errors.APIError):
            env_ops.delete_environment(settings, team, ENV, delete_branch=True)

        assert BRANCH in _remote_branches(origin)
        assert os.path.isdir(get_repo_path(ENV, team.workspaces_dir))

    def test_the_branch_goes_after_the_container_and_database(
        self, settings, team, origin
    ):
        _provision(team, origin)
        client = _client(_labels(origin))
        container = client.containers.get.return_value
        seen = {}

        def gate(*args, **kwargs):
            seen["container_removed"] = container.remove.called
            seen["database_dropped"] = drop.called
            seen["clone_present"] = os.path.isdir(
                get_repo_path(ENV, team.workspaces_dir)
            )
            return None

        with (
            patch.object(env_ops, "get_client", return_value=client),
            patch.object(env_ops, "_agent_remove_env"),
            patch.object(env_ops, "_exec_sql") as drop,
            patch.object(env_ops, "_drop_pg_role"),
            patch.object(env_ops, "_maybe_delete_env_branch", side_effect=gate),
        ):
            env_ops.delete_environment(settings, team, ENV, delete_branch=True)

        assert seen == {
            "container_removed": True,
            "database_dropped": True,
            "clone_present": True,
        }

    def test_an_unexpected_git_error_is_a_warning_not_a_failure(
        self, settings, team, origin
    ):
        _provision(team, origin)
        client = _client(_labels(origin))

        with (
            _teardown_mocks(client),
            patch.object(
                env_ops, "_maybe_delete_env_branch", side_effect=RuntimeError("boom")
            ),
        ):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "boom" in warnings[0]
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))

    def test_branch_already_gone_on_origin_is_reported(self, settings, team, origin):
        _provision(team, origin)
        src = origin[len("file://") :]
        _src_git(src, "branch", "-D", BRANCH)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "no longer exists" in warnings[0]
        assert not os.path.exists(get_workspace_path(ENV, team.workspaces_dir))


def test_ls_remote_output_gives_the_default_branch_and_tips():
    default, tips = env_ops._parse_ls_remote(
        "ref: refs/heads/main\tHEAD\n"
        "1111111111111111111111111111111111111111\tHEAD\n"
        "2222222222222222222222222222222222222222\trefs/heads/feature/x\n"
        # A tail match of the pattern, not the branch itself.
        "3333333333333333333333333333333333333333\trefs/pull/7/refs/heads/feature/x\n"
    )

    assert default == "main"
    assert tips == {
        "main": "1111111111111111111111111111111111111111",
        "feature/x": "2222222222222222222222222222222222222222",
    }


def test_ls_remote_output_without_a_head_symref_has_no_default():
    # A detached or unadvertised HEAD: the default branch is unknown, and the
    # caller refuses rather than risk deleting it.
    default, tips = env_ops._parse_ls_remote(
        "1111111111111111111111111111111111111111\tHEAD\n"
        "2222222222222222222222222222222222222222\trefs/heads/feature/x\n"
    )

    assert default == ""
    assert tips == {"feature/x": "2222222222222222222222222222222222222222"}


BASE = "17.0"


@pytest.fixture
def versioned_origin(origin):
    """``origin`` plus a ``17.0`` version branch that has moved on from
    ``main``, and ``feature/x`` restarted from it with one commit of its own —
    the usual Odoo layout, where work is merged into a version branch rather
    than into origin's default branch."""
    src = origin[len("file://") :]
    _branch_on_origin(origin, BASE)
    _commit_on_origin(origin, BASE, "17.0 only")
    _src_git(src, "branch", "-f", BRANCH, BASE)
    _commit_on_origin(origin, BRANCH, "feature work")
    return origin


class TestMergedCheck:
    """What counts as merged: the branch tip is contained in origin's default
    branch or in the base branch recorded at creation."""

    def test_merged_into_the_recorded_base_branch_is_deleted(
        self, settings, team, versioned_origin
    ):
        origin = versioned_origin
        _merge_on_origin(origin, BRANCH, into=BASE)
        _provision(team, origin)
        client = _client(_labels(origin, **{"oduflow.git_base_branch": BASE}))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert warnings == []
        assert BRANCH not in _remote_branches(origin)
        assert {BASE, "main"} <= _remote_branches(origin)

    def test_without_a_recorded_base_only_the_default_branch_counts(
        self, settings, team, versioned_origin
    ):
        origin = versioned_origin
        _merge_on_origin(origin, BRANCH, into=BASE)
        _provision(team, origin)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "'main'" in warnings[0]
        assert BASE not in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_unmerged_into_base_and_default_names_both(
        self, settings, team, versioned_origin
    ):
        origin = versioned_origin
        _provision(team, origin)
        client = _client(_labels(origin, **{"oduflow.git_base_branch": BASE}))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "'17.0' or 'main'" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_a_base_branch_gone_from_origin_falls_back_to_the_default(
        self, settings, team, origin
    ):
        _provision(team, origin)
        client = _client(_labels(origin, **{"oduflow.git_base_branch": "gone"}))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        # feature/x sits on main's tip: merged into the default branch.
        assert warnings == []
        assert BRANCH not in _remote_branches(origin)

    def test_a_squash_merge_is_not_recognised_and_says_so(self, settings, team, origin):
        _commit_on_origin(origin, BRANCH, "feature work")
        _squash_on_origin(origin, BRANCH, into="main")
        _provision(team, origin)
        client = _client(_labels(origin))

        with _teardown_mocks(client):
            warnings = env_ops.delete_environment(
                settings, team, ENV, delete_branch=True
            )

        assert len(warnings) == 1
        assert "squash- or rebase-merged" in warnings[0]
        assert BRANCH in _remote_branches(origin)

    def test_a_failed_fetch_is_reported_as_such(self, team, origin):
        _commit_on_origin(origin, BRANCH, "feature work")
        repo_path = _provision(team, origin)
        real_run = subprocess.run

        def failing_fetch(cmd, *args, **kwargs):
            if "fetch" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 128, "", "fatal: unable to access origin"
                )
            return real_run(cmd, *args, **kwargs)

        with patch.object(env_ops.subprocess, "run", side_effect=failing_fetch):
            warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is not None
        assert "git fetch failed: fatal: unable to access origin" in warning
        # Not a verdict on the branch, so no advice about squash merges.
        assert "squash" not in warning
        assert BRANCH in _remote_branches(origin)

    def test_same_tip_as_the_target_needs_no_fetch(self, team, origin):
        # A branch that never got a commit of its own: ls-remote alone proves
        # it is merged, so nothing is downloaded.
        repo_path = _provision(team, origin)
        real_run = subprocess.run
        commands = []

        def spy(cmd, *args, **kwargs):
            commands.append(cmd)
            return real_run(cmd, *args, **kwargs)

        with patch.object(env_ops.subprocess, "run", side_effect=spy):
            warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is None
        assert BRANCH not in _remote_branches(origin)
        assert not any("fetch" in cmd for cmd in commands)

    def test_a_merge_beyond_the_first_depth_is_found_by_deepening(self, team, origin):
        first, last = env_ops._MERGE_CHECK_DEPTHS
        _commit_on_origin(origin, BRANCH, "feature work")
        _merge_on_origin(origin, BRANCH, into="main")
        # main moves on, past what the first (shallow) fetch can see.
        _commit_on_origin(origin, "main", "later work", count=first + 10)
        repo_path = _provision(team, origin)

        warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is None
        assert BRANCH not in _remote_branches(origin)

    def test_a_merge_beyond_the_last_depth_keeps_the_branch(self, team, origin):
        _commit_on_origin(origin, BRANCH, "feature work")
        _merge_on_origin(origin, BRANCH, into="main")
        _commit_on_origin(origin, "main", "later work", count=12)
        repo_path = _provision(team, origin)

        with patch.object(env_ops, "_MERGE_CHECK_DEPTHS", (2, 4)):
            warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is not None
        assert "could not be confirmed as merged into 'main'" in warning
        assert "most recent 4 commits" in warning
        assert BRANCH in _remote_branches(origin)

    @staticmethod
    def _push_lands_before_the_delete(origin):
        """subprocess.run that lets someone push to the branch on origin right
        before Oduflow's delete reaches it — the race the lease closes."""
        real_run = subprocess.run

        def racing(cmd, *args, **kwargs):
            if "push" in cmd and "--delete" in cmd:
                _commit_on_origin(origin, BRANCH, "pushed meanwhile")
            return real_run(cmd, *args, **kwargs)

        return racing

    def test_a_push_during_the_check_keeps_the_branch(self, team, origin):
        _commit_on_origin(origin, BRANCH, "feature work")
        _merge_on_origin(origin, BRANCH, into="main")
        repo_path = _provision(team, origin)
        racing = self._push_lands_before_the_delete(origin)

        with patch.object(env_ops.subprocess, "run", side_effect=racing):
            warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is not None
        assert "received new commits while it was being checked" in warning
        assert BRANCH in _remote_branches(origin)

    def test_a_push_after_the_same_tip_shortcut_keeps_the_branch(self, team, origin):
        # No fetch on this path: the lease is pinned to the ls-remote tip.
        repo_path = _provision(team, origin)
        racing = self._push_lands_before_the_delete(origin)

        with patch.object(env_ops.subprocess, "run", side_effect=racing):
            warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is not None
        assert "received new commits while it was being checked" in warning
        assert BRANCH in _remote_branches(origin)

    def test_the_check_never_downloads_the_full_history(self, team, origin):
        # The managed clone is --depth 1. Comparing against another branch
        # must not turn it into a full clone of that branch: on a large
        # repository that is minutes of download under the environment lock.
        first, _last = env_ops._MERGE_CHECK_DEPTHS
        history = first * 3
        _commit_on_origin(origin, "main", "history", count=history)
        src = origin[len("file://") :]
        _src_git(src, "branch", "-f", BRANCH, "main")
        _commit_on_origin(origin, BRANCH, "unmerged work")
        repo_path = _provision(team, origin)

        warning = env_ops._delete_branch_on_origin(origin, BRANCH, repo_path, team)

        assert warning is not None
        assert "not merged into 'main'" in warning
        assert BRANCH in _remote_branches(origin)
        fetched = int(_clone_git(repo_path, "rev-list", "--count", "--all"))
        assert fetched <= first + 1 < history


def test_recreate_environment_never_deletes_the_branch(settings, team, origin):
    """recreate deletes with preserve_access=True and must not forward any
    delete_branch flag — the branch always survives a recreate."""
    client = _client(_labels(origin))

    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch("oduflow.docker_ops.client.get_client", return_value=client),
        patch("oduflow.docker_ops.system_ops.estimate_new_db_bytes", return_value=0),
        patch("oduflow.docker_ops.system_ops.check_disk_space"),
        patch.object(env_ops, "_ensure_system_ready"),
        patch.object(env_ops, "_reconcile_environment_hostname_for_update"),
        patch.object(env_ops, "delete_environment") as delete,
        patch.object(env_ops, "create_environment", return_value={}) as create,
    ):
        env_ops.recreate_environment(settings, team, ENV)

    delete.assert_called_once()
    assert delete.call_args.kwargs == {"preserve_access": True}
    create.assert_called_once()


def test_recreate_carries_the_branch_provenance_labels(settings, team, origin):
    """The branch exists by the time the environment is created again, so the
    created-by-Oduflow hint and the base branch would be lost unless recreate
    hands them over from the replaced container."""
    provenance = {
        "oduflow.git_branch_created": "true",
        "oduflow.git_base_branch": BASE,
    }
    client = _client(_labels(origin, **provenance))

    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch("oduflow.docker_ops.client.get_client", return_value=client),
        patch("oduflow.docker_ops.system_ops.estimate_new_db_bytes", return_value=0),
        patch("oduflow.docker_ops.system_ops.check_disk_space"),
        patch.object(env_ops, "_ensure_system_ready"),
        patch.object(env_ops, "_reconcile_environment_hostname_for_update"),
        patch.object(env_ops, "delete_environment"),
        patch.object(env_ops, "create_environment", return_value={}) as create,
    ):
        env_ops.recreate_environment(settings, team, ENV)

    assert create.call_args.kwargs["branch_provenance"] == provenance
    # Never as base_branch: that would re-create a branch deleted on origin.
    assert "base_branch" not in create.call_args.kwargs
