"""Unit tests for the Odoo.sh addon ingest server-side contract (no Docker).

Covers extract_addon_dir (tar → staged addon dir) and _wire_imported_addons
(staged addon → local extra-addons repo + template mapping). Remote addons use
a local file:// git remote, so no network is needed.
"""

import json
import os
import subprocess
import tarfile

import pytest

from oduflow.docker_ops import system_ops
from oduflow.extra_addons import is_local_repo
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _make_repo_dir(tmp_path, top="enterprise_src"):
    src = tmp_path / top
    mod = src / "sale_enterprise"
    mod.mkdir(parents=True)
    (mod / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}")
    return src


def _tar_dir(src_dir, tar_path, arcname):
    """Mirror the script's `tar -C parent -cf - base` (top-level dir = base)."""
    with tarfile.open(tar_path, "w") as tf:
        tf.add(str(src_dir), arcname=arcname)


def _make_git_remote(tmp_path):
    """A file:// remote with branches ``main`` and ``18.0``."""
    src = _make_repo_dir(tmp_path, top="remote-enterprise")
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    for args in (
        ["init", "-b", "main"],
        ["add", "-A"],
        [*ident, "commit", "-m", "init"],
        ["branch", "18.0"],
    ):
        subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True)
    return f"file://{src}"


def _remote_addon_staging(tmp_path, url, branch="18.0"):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "addons.json").write_text(
        json.dumps(
            [
                {
                    "name": "enterprise",
                    "kind": "remote",
                    "origin_url": url,
                    "branch": branch,
                }
            ]
        )
    )
    return staging


class TestExtractAddonDir:
    def test_single_top_dir_renamed_to_name(self, tmp_path):
        src = _make_repo_dir(tmp_path)
        tar_path = tmp_path / "a.tar"
        _tar_dir(src, tar_path, "enterprise_src")
        addons_dir = tmp_path / "staging" / "addons"
        system_ops.extract_addon_dir(str(tar_path), str(addons_dir), "enterprise")
        assert (
            addons_dir / "enterprise" / "sale_enterprise" / "__manifest__.py"
        ).is_file()
        assert not (addons_dir / "enterprise_src").exists()
        assert not (addons_dir / ".incoming_enterprise").exists()

    def test_bare_contents_multiple_top_dirs(self, tmp_path):
        # A tar whose top level is several module dirs (no single wrapper).
        base = tmp_path / "content"
        (base / "mod_a").mkdir(parents=True)
        (base / "mod_a" / "__manifest__.py").write_text("{}")
        (base / "mod_b").mkdir(parents=True)
        (base / "mod_b" / "__manifest__.py").write_text("{}")
        tar_path = tmp_path / "c.tar"
        with tarfile.open(tar_path, "w") as tf:
            tf.add(str(base / "mod_a"), arcname="mod_a")
            tf.add(str(base / "mod_b"), arcname="mod_b")
        addons_dir = tmp_path / "staging" / "addons"
        system_ops.extract_addon_dir(str(tar_path), str(addons_dir), "themes")
        assert (addons_dir / "themes" / "mod_a" / "__manifest__.py").is_file()
        assert (addons_dir / "themes" / "mod_b" / "__manifest__.py").is_file()


class TestWireImportedAddons:
    def test_no_manifest_returns_empty(self, team, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        assert system_ops._wire_imported_addons(team, str(staging), "18.0") == {}

    def test_local_addon_becomes_local_repo(self, team, tmp_path):
        staging = tmp_path / "staging"
        addons = staging / "addons" / "enterprise"
        (addons / "sale_enterprise").mkdir(parents=True)
        (addons / "sale_enterprise" / "__manifest__.py").write_text("{}")
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")
        assert wired == {"enterprise": "18.0"}
        assert is_local_repo(team, "enterprise")

    def test_branch_falls_back_to_major_version(self, team, tmp_path):
        staging = tmp_path / "staging"
        addons = staging / "addons" / "themes"
        addons.mkdir(parents=True)
        (addons / "readme.txt").write_text("x")
        (staging / "addons.json").write_text(
            json.dumps([{"name": "themes", "kind": "local", "branch": "HEAD"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "17.0")
        assert wired == {"themes": "17.0"}

    def test_existing_repo_is_referenced_not_recreated(self, team, tmp_path):
        source = _make_repo_dir(tmp_path, top="existing-enterprise")
        from oduflow.extra_addons import create_local_repo

        create_local_repo(team, "enterprise", str(source), "18.0")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")
        assert wired == {"enterprise": "18.0"}

    def test_existing_repo_without_requested_branch_fails(self, team, tmp_path):
        source = _make_repo_dir(tmp_path, top="existing-enterprise")
        from oduflow.errors import NotFoundError
        from oduflow.extra_addons import create_local_repo

        create_local_repo(team, "enterprise", str(source), "17.0")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        with pytest.raises(NotFoundError, match="Branch '18.0'"):
            system_ops._wire_imported_addons(team, str(staging), "18.0")

    def test_declared_addon_without_remote_or_files_fails(self, team, tmp_path):
        from oduflow.errors import PrerequisiteNotMetError

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        with pytest.raises(PrerequisiteNotMetError, match="no uploaded files"):
            system_ops._wire_imported_addons(team, str(staging), "18.0")

    def test_registered_repo_downloads_requested_branch(self, team, tmp_path):
        from oduflow.extra_addons import list_extra_repos, register_extra_repo

        url = _make_git_remote(tmp_path)
        register_extra_repo(team, "enterprise", url)
        staging = _remote_addon_staging(tmp_path, url)

        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")

        assert wired == {"enterprise": "18.0"}
        assert list_extra_repos(team)[0]["branches"] == ["18.0"]

    def test_new_remote_addon_downloads_only_requested_branch(self, team, tmp_path):
        from oduflow.extra_addons import list_extra_repos

        url = _make_git_remote(tmp_path)
        staging = _remote_addon_staging(tmp_path, url)

        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")

        assert wired == {"enterprise": "18.0"}
        repo = list_extra_repos(team)[0]
        assert repo["branches"] == ["18.0"]
        assert repo["available_branches"] == ["18.0", "main"]

    def test_strict_missing_remote_branch_rejects_uploaded_copy(self, team, tmp_path):
        """A remote that lacks the branch must fail a strict import even when
        uploaded files exist; a local copy could never receive remote updates."""
        from oduflow.errors import NotFoundError

        url = _make_git_remote(tmp_path)
        staging = _remote_addon_staging(tmp_path, url, branch="17.0")
        local_src = staging / "addons" / "enterprise"
        local_src.mkdir(parents=True)
        (local_src / "__manifest__.py").write_text("{}")

        with pytest.raises(NotFoundError, match="17.0"):
            system_ops._wire_imported_addons(team, str(staging), "17.0")

        assert not os.path.exists(os.path.join(team.shared_repos_dir, "enterprise"))

    def test_best_effort_missing_remote_branch_uses_uploaded_copy(self, team, tmp_path):
        url = _make_git_remote(tmp_path)
        staging = _remote_addon_staging(tmp_path, url, branch="17.0")
        local_src = staging / "addons" / "enterprise"
        local_src.mkdir(parents=True)
        (local_src / "__manifest__.py").write_text("{}")
        warnings = []

        wired = system_ops._wire_imported_addons(
            team,
            str(staging),
            "17.0",
            addon_error_policy="best_effort",
            addon_warnings=warnings,
        )

        assert wired == {"enterprise": "17.0"}
        assert is_local_repo(team, "enterprise")
        assert [w["action"] for w in warnings] == ["used_local_copy"]
        assert "17.0" in warnings[0]["reason"]

    def test_clone_success_with_wrong_branch_fails_loud(self, team, tmp_path):
        """A successful clone whose branch is missing on the remote must NOT
        silently fall back in strict mode. The bare repo is removed and the
        branch error surfaces."""
        from unittest.mock import patch

        from oduflow import extra_addons
        from oduflow.errors import NotFoundError

        staging = tmp_path / "staging"
        staging.mkdir()
        # Provision BOTH local files and a remote URL: a too-broad except
        # used to silently use the local files when the branch check failed.
        local_src = staging / "addons" / "enterprise"
        local_src.mkdir(parents=True)
        (local_src / "__manifest__.py").write_text("{}")
        (staging / "addons.json").write_text(
            json.dumps(
                [
                    {
                        "name": "enterprise",
                        "kind": "remote",
                        "origin_url": "https://example.com/enterprise.git",
                        "branch": "18.0",
                    }
                ]
            )
        )

        def fake_clone(*_args, **_kwargs):
            os.makedirs(os.path.join(team.shared_repos_dir, "enterprise"))
            return {"name": "enterprise"}

        with (
            patch.object(
                extra_addons,
                "register_extra_repo",
                side_effect=fake_clone,
            ),
            patch.object(
                extra_addons,
                "_resolve_branch_revision",
                side_effect=NotFoundError("Branch '18.0' not found."),
            ),
        ):
            with pytest.raises(NotFoundError, match="Branch '18.0'"):
                system_ops._wire_imported_addons(team, str(staging), "18.0")

        # The bare repo the clone created must be cleaned up so it cannot be
        # referenced as if it were a fresh wiring of the correct branch.
        assert not os.path.exists(os.path.join(team.shared_repos_dir, "enterprise"))

    def test_best_effort_wrong_remote_branch_uses_local_copy(self, team, tmp_path):
        from unittest.mock import patch

        from oduflow import extra_addons
        from oduflow.errors import NotFoundError

        staging = tmp_path / "staging"
        local_src = staging / "addons" / "enterprise"
        local_src.mkdir(parents=True)
        (local_src / "__manifest__.py").write_text("{}")
        (staging / "addons.json").write_text(
            json.dumps(
                [
                    {
                        "name": "enterprise",
                        "kind": "remote",
                        "origin_url": "https://example.com/enterprise.git",
                        "branch": "18.0",
                    }
                ]
            )
        )

        def fake_clone(*_args, **_kwargs):
            os.makedirs(os.path.join(team.shared_repos_dir, "enterprise"))
            return {"name": "enterprise"}

        warnings = []
        with (
            patch.object(extra_addons, "register_extra_repo", side_effect=fake_clone),
            patch.object(
                extra_addons,
                "_resolve_branch_revision",
                side_effect=[
                    NotFoundError("Branch '18.0' not found."),
                    "a" * 40,
                ],
            ),
        ):
            wired = system_ops._wire_imported_addons(
                team,
                str(staging),
                "18.0",
                addon_error_policy="best_effort",
                addon_warnings=warnings,
            )

        assert wired == {"enterprise": "18.0"}
        assert is_local_repo(team, "enterprise")
        assert warnings == [
            {
                "name": "enterprise",
                "action": "used_local_copy",
                "reason": "Branch '18.0' not found.",
            }
        ]

    def test_best_effort_missing_addon_is_skipped(self, team, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "missing", "kind": "local", "branch": "18.0"}])
        )
        warnings = []

        wired = system_ops._wire_imported_addons(
            team,
            str(staging),
            "18.0",
            addon_error_policy="best_effort",
            addon_warnings=warnings,
        )

        assert wired == {}
        assert warnings == [
            {
                "name": "missing",
                "action": "skipped",
                "reason": "Addon 'missing' has no uploaded files and no usable origin.",
            }
        ]

    def test_strict_clone_failure_can_use_uploaded_copy_with_warning(
        self, team, tmp_path
    ):
        from unittest.mock import patch

        from oduflow import extra_addons

        staging = tmp_path / "staging"
        local_src = staging / "addons" / "oca"
        local_src.mkdir(parents=True)
        (local_src / "__manifest__.py").write_text("{}")
        (staging / "addons.json").write_text(
            json.dumps(
                [
                    {
                        "name": "oca",
                        "kind": "remote",
                        "origin_url": "https://example.com/oca.git",
                        "branch": "18.0",
                    }
                ]
            )
        )
        warnings = []

        with patch.object(
            extra_addons,
            "register_extra_repo",
            side_effect=RuntimeError("remote unavailable"),
        ):
            wired = system_ops._wire_imported_addons(
                team,
                str(staging),
                "18.0",
                addon_warnings=warnings,
            )

        assert wired == {"oca": "18.0"}
        assert warnings == [
            {
                "name": "oca",
                "action": "used_local_copy",
                "reason": "remote unavailable",
            }
        ]
