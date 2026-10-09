"""Unit tests for _clone_repo error translation (env_ops).

These exercise real git (no network, no Docker) against a local ``file://``
repo. The point under test: a missing remote branch must surface as
NotFoundError with an actionable message — the dashboard shows FlowError text
verbatim but hides ExternalCommandError behind a generic "check server logs"
banner, so the translation is what makes the failure diagnosable from the UI.

The same fixtures cover _clone_or_start_branch: a branch missing on origin is
started from a base branch and pushed, so later fetches find it there.
"""

import pathlib
import subprocess

import pytest

from oduflow.docker_ops.env_ops import _clone_or_start_branch, _clone_repo
from oduflow.errors import ExternalCommandError, NotFoundError
from oduflow.git_ops import RepoAuthError
from oduflow.settings import TeamSettings

_GIT_ID = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
]


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


@pytest.fixture
def source_url(tmp_path):
    """A local git repo with a single ``main`` branch, as a file:// URL."""
    src = tmp_path / "source"
    src.mkdir()
    subprocess.run(
        ["git", "-C", str(src), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    (src / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(src), *_GIT_ID, "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(src), *_GIT_ID, "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return f"file://{src}"


def test_missing_branch_raises_not_found(team, tmp_path, source_url):
    with pytest.raises(NotFoundError) as exc_info:
        _clone_repo(source_url, "WaterLab", str(tmp_path / "clone"), team)
    msg = str(exc_info.value)
    assert "Branch 'WaterLab' does not exist on origin" in msg
    assert "git push -u origin WaterLab" in msg


def test_existing_branch_clones(team, tmp_path, source_url):
    dest = tmp_path / "clone"
    _clone_repo(source_url, "main", str(dest), team)
    assert (dest / "README.md").exists()


def test_other_clone_failure_stays_external(team, tmp_path):
    """A non-repo URL is not a branch problem: still ExternalCommandError."""
    with pytest.raises(ExternalCommandError):
        _clone_repo(
            f"file://{tmp_path / 'nowhere'}",
            "main",
            str(tmp_path / "clone"),
            team,
        )


def _source_path(source_url):
    return source_url[len("file://") :]


def _remote_branches(source_url):
    out = subprocess.run(
        ["git", "-C", _source_path(source_url), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def _reject_pushes(source_url, message):
    """Install a pre-receive hook on the origin that refuses every push."""
    hook = pathlib.Path(_source_path(source_url)) / ".git" / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n")
    hook.chmod(0o755)


def test_missing_branch_is_started_from_base_and_pushed(team, tmp_path, source_url):
    dest = tmp_path / "clone"
    created = _clone_or_start_branch(
        source_url, "feature/new", str(dest), team, base_branch="main"
    )
    assert created is True
    assert (dest / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "feature/new"
    # Published on origin, so pull_and_apply / switch_branch / the agent's
    # own clone can fetch it.
    assert "feature/new" in _remote_branches(source_url)


def test_existing_branch_ignores_base(team, tmp_path, source_url):
    dest = tmp_path / "clone"
    created = _clone_or_start_branch(
        source_url, "main", str(dest), team, base_branch="develop"
    )
    assert created is False
    assert (dest / "README.md").exists()
    assert _remote_branches(source_url) == {"main"}


def test_missing_branch_without_base_still_not_found(team, tmp_path, source_url):
    with pytest.raises(NotFoundError, match="Push it first"):
        _clone_or_start_branch(source_url, "feature/new", str(tmp_path / "c"), team)
    assert _remote_branches(source_url) == {"main"}


def test_missing_base_branch_is_reported(team, tmp_path, source_url):
    with pytest.raises(NotFoundError) as exc_info:
        _clone_or_start_branch(
            source_url, "feature/new", str(tmp_path / "c"), team, base_branch="dev"
        )
    assert "neither does its base branch 'dev'" in str(exc_info.value)


def test_denied_push_raises_repo_auth_error(team, tmp_path, source_url):
    _reject_pushes(source_url, "ERROR: Permission to owner/repo.git denied to bot.")
    with pytest.raises(RepoAuthError) as exc_info:
        _clone_or_start_branch(
            source_url, "feature/new", str(tmp_path / "c"), team, base_branch="main"
        )
    msg = str(exc_info.value)
    assert "could not be created from 'main'" in msg
    assert "git push -u origin feature/new" in msg
    assert _remote_branches(source_url) == {"main"}


def test_other_push_failure_stays_external(team, tmp_path, source_url):
    _reject_pushes(source_url, "branch names must start with a ticket id")
    with pytest.raises(ExternalCommandError):
        _clone_or_start_branch(
            source_url, "feature/new", str(tmp_path / "c"), team, base_branch="main"
        )
