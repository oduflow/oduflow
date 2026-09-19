"""Tests for template code provenance and the lineage check at creation.

A template database is a snapshot of a branch at a moment in time. Without that
anchor an agent cannot tell whether a fresh checkout predates the data it was
handed — the failure then surfaces much later, as an upgrade that dies on
validation against records written by code the branch does not have.
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import patch

import pytest
from tool_helpers import call_tool as _call_tool  # noqa: E402

from oduflow.docker_ops import env_ops, system_ops
from oduflow.settings import Settings, TeamSettings


def _make_env(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        disable_telemetry=True,
        teams={"1": team},
    )
    return settings, team


def _write_template(team: TeamSettings, name: str, metadata: dict) -> None:
    path = team.get_template_metadata_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(metadata, f)


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(path) -> None:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Oduflow Tests")


@pytest.fixture
def tool_env(tmp_path):
    import oduflow.server as server

    settings, team = _make_env(tmp_path)
    old = server._settings
    server._settings = settings
    with patch("oduflow.server._resolve_team", return_value=team):
        yield settings, team
    server._settings = old


# --- recording the snapshot's origin ---------------------------------------


class TestCodeProvenance:
    def test_records_branch_and_commit_of_the_source_checkout(self, tmp_path):
        settings, team = _make_env(tmp_path)
        repo = os.path.join(team.workspaces_dir, "feature-x", "repo")
        os.makedirs(os.path.join(repo, ".git"))

        with (
            patch("oduflow.git_ops.is_git_repository", return_value=True),
            patch("oduflow.git_ops.rev_parse", return_value="c0ffee1234"),
        ):
            provenance = system_ops._code_provenance(
                team, "feature-x", {"oduflow.git_branch": "prod"}
            )

        assert provenance["source_branch"] == "prod"
        assert provenance["source_commit"] == "c0ffee1234"
        assert provenance["snapshot_at"]

    def test_live_mount_checkout_is_used_when_present(self, tmp_path):
        settings, team = _make_env(tmp_path)
        checkout = tmp_path / "local-addons"
        (checkout / ".git").mkdir(parents=True)

        with (
            patch("oduflow.git_ops.is_git_repository", return_value=True),
            patch("oduflow.git_ops.rev_parse", return_value="abc123") as rev,
        ):
            provenance = system_ops._code_provenance(
                team,
                "feature-x",
                {"oduflow.git_branch": "main", "oduflow.local_path": str(checkout)},
            )

        assert rev.call_args[0][0] == str(checkout)
        assert provenance["source_commit"] == "abc123"

    def test_git_worktree_checkout_is_recorded(self, tmp_path):
        settings, team = _make_env(tmp_path)
        source = tmp_path / "source"
        checkout = tmp_path / "feature-worktree"
        _init_repo(source)
        (source / "README.md").write_text("base\n")
        _git(source, "add", "README.md")
        _git(source, "commit", "-m", "base")
        _git(source, "worktree", "add", "-b", "feature", str(checkout))

        assert (checkout / ".git").is_file()
        provenance = system_ops._code_provenance(
            team,
            "feature-x",
            {"oduflow.git_branch": "feature", "oduflow.local_path": str(checkout)},
        )

        assert provenance["source_commit"] == _git(checkout, "rev-parse", "HEAD")

    def test_missing_checkout_yields_timestamp_only(self, tmp_path):
        # No repo on disk (deleted workspace, imported template): record the
        # snapshot time and leave the lineage check with nothing to compare.
        settings, team = _make_env(tmp_path)
        provenance = system_ops._code_provenance(team, "gone", {})
        assert "source_commit" not in provenance
        assert provenance["snapshot_at"]


# --- surfacing it to agents -------------------------------------------------


class TestListTemplatesShowsOrigin:
    @patch("oduflow.docker_ops.system_ops.get_client")
    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=True)
    def test_source_line_includes_branch_and_short_commit(
        self, _db_exists, _client, tool_env, tmp_path
    ):
        settings, team = tool_env
        _write_template(
            team,
            "prod",
            {
                "odoo_image": "odoo:19.0",
                "source_branch": "prod",
                "source_commit": "c0ffee1234567890",
                "snapshot_at": "2026-08-01T10:00:00+00:00",
                "filestore_size_mb": 1.0,
                "dump_size_mb": 1.0,
            },
        )
        output = _call_tool("list_templates")

        assert "Source=prod @ c0ffee12 @ snapshot 2026-08-01" in output


# --- the lineage check ------------------------------------------------------


class TestTemplateCodeLineage:
    def test_no_template_means_no_check(self, tmp_path):
        settings, team = _make_env(tmp_path)
        result = env_ops._template_code_lineage(team, None, str(tmp_path))
        assert result["status"] == "unknown"

    def test_template_without_provenance_is_silent(self, tmp_path):
        # Templates created before provenance existed must not start warning.
        settings, team = _make_env(tmp_path)
        _write_template(team, "old", {"odoo_image": "odoo:19.0"})
        result = env_ops._template_code_lineage(team, "old", str(tmp_path))
        assert result["status"] == "unknown"
        assert result["message"] == ""

    def test_passes_recorded_commit_to_the_comparison(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(
            team, "prod", {"source_commit": "snapshot1", "source_branch": "prod"}
        )
        with patch(
            "oduflow.git_analysis.template_lineage",
            return_value={
                "status": "diverged",
                "message": "Merge prod",
                "modules_to_upgrade": [],
            },
        ) as lineage:
            result = env_ops._template_code_lineage(team, "prod", "/repo")

        assert lineage.call_args[0] == ("/repo", "snapshot1", "prod")
        assert result["status"] == "diverged"

    def test_fetches_missing_history_for_managed_clone(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(
            team, "prod", {"source_commit": "snapshot1", "source_branch": "prod"}
        )
        with (
            patch(
                "oduflow.git_ops.ensure_lineage_history", return_value=True
            ) as ensure,
            patch(
                "oduflow.git_analysis.template_lineage",
                return_value={
                    "status": "aligned",
                    "message": "",
                    "modules_to_install": [],
                    "modules_to_upgrade": [],
                },
            ),
        ):
            env_ops._template_code_lineage(
                team,
                "prod",
                "/repo",
                current_branch="feature",
                fetch_missing_history=True,
            )

        ensure.assert_called_once_with(
            "/repo",
            "snapshot1",
            "feature",
            "prod",
            team.git_credentials_file(),
        )

    def test_depth_one_clone_fetches_both_sides_of_lineage(self, tmp_path):
        from oduflow import git_ops

        source = tmp_path / "source"
        _init_repo(source)
        (source / "module.py").write_text("base = True\n")
        _git(source, "add", "module.py")
        _git(source, "commit", "-m", "base")
        base_commit = _git(source, "rev-parse", "HEAD")
        _git(source, "branch", "feature")
        (source / "module.py").write_text("base = True\nnew = True\n")
        _git(source, "commit", "-am", "template snapshot")
        template_commit = _git(source, "rev-parse", "HEAD")

        ahead = tmp_path / "ahead"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                f"file://{source}",
                str(ahead),
            ],
            check=True,
            capture_output=True,
        )
        assert not git_ops.commit_exists(str(ahead), base_commit)
        assert git_ops.ensure_lineage_history(str(ahead), base_commit, "main", "main")
        assert git_ops.is_ancestor(str(ahead), base_commit)

        behind = tmp_path / "behind"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "feature",
                f"file://{source}",
                str(behind),
            ],
            check=True,
            capture_output=True,
        )
        assert not git_ops.commit_exists(str(behind), template_commit)
        assert git_ops.ensure_lineage_history(
            str(behind), template_commit, "feature", "main"
        )
        assert not git_ops.is_ancestor(str(behind), template_commit)

    def test_a_failing_check_never_breaks_creation(self, tmp_path):
        settings, team = _make_env(tmp_path)
        _write_template(team, "prod", {"source_commit": "snapshot1"})
        with patch(
            "oduflow.git_analysis.template_lineage",
            side_effect=RuntimeError("git gone"),
        ):
            result = env_ops._template_code_lineage(team, "prod", "/repo")
        assert result["status"] == "unknown"


class TestCreateEnvironmentReportsLineage:
    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_diverged_branch_is_reported_with_the_remedy(
        self, mock_create, mock_adopt, tool_env, tmp_path
    ):
        settings, team = tool_env
        _write_template(
            team,
            "prod",
            {"odoo_image": "odoo:19.0", "repo_url": "https://github.com/x/y.git"},
        )
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-t-odoo",
            "database": "oduflow_1_t",
            "workspace": "/tmp/ws",
            "template_lineage": {
                "status": "diverged",
                "message": "Merge prod into this branch before the first pull_and_apply.",
                "modules_to_upgrade": [],
            },
        }

        result = _call_tool("create_environment", branch="t", template_name="prod")

        assert "Code is behind the template database" in result
        assert "Merge prod into this branch" in result

    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_aligned_branch_says_nothing(
        self, mock_create, mock_adopt, tool_env, tmp_path
    ):
        settings, team = tool_env
        _write_template(
            team,
            "prod",
            {"odoo_image": "odoo:19.0", "repo_url": "https://github.com/x/y.git"},
        )
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-t-odoo",
            "database": "oduflow_1_t",
            "workspace": "/tmp/ws",
            "template_lineage": {
                "status": "aligned",
                "message": "",
                "modules_to_upgrade": [],
            },
        }

        result = _call_tool("create_environment", branch="t", template_name="prod")

        assert "template database" not in result

    @patch(
        "oduflow.docker_ops.env_ops.adopt_existing_environment",
        return_value=None,
    )
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_ahead_branch_gets_the_upgrade_list(
        self, mock_create, mock_adopt, tool_env, tmp_path
    ):
        settings, team = tool_env
        _write_template(
            team,
            "prod",
            {"odoo_image": "odoo:19.0", "repo_url": "https://github.com/x/y.git"},
        )
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-t-odoo",
            "database": "oduflow_1_t",
            "workspace": "/tmp/ws",
            "template_lineage": {
                "status": "ahead",
                "message": "schema/data changed in supply, supply_stock. Apply "
                'them explicitly with pull_and_apply(upgrade="supply,supply_stock")',
                "modules_to_upgrade": ["supply", "supply_stock"],
            },
        }

        result = _call_tool("create_environment", branch="t", template_name="prod")

        assert "Code is ahead of the template database" in result
        assert 'upgrade="supply,supply_stock"' in result
