"""Unit tests for _clone_repo error translation (env_ops).

These exercise real git (no network, no Docker) against a local ``file://``
repo. The point under test: a missing remote branch must surface as
NotFoundError with an actionable message — the dashboard shows FlowError text
verbatim but hides ExternalCommandError behind a generic "check server logs"
banner, so the translation is what makes the failure diagnosable from the UI.
"""

import subprocess

import pytest

from oduflow.docker_ops.env_ops import _clone_repo
from oduflow.errors import ExternalCommandError, NotFoundError
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
