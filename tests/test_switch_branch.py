"""Reusing an environment for another branch (``switch_environment_branch``).

The environment is the reusable unit: switching the branch must keep the
database, filestore and routing while changing only the code. What these tests
pin down is the part that is easy to get subtly wrong: mutation order, refusing
branches that were never pushed, and handing the branch diff to the normal apply
guardrail.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from oduflow import git_ops
from oduflow.docker_ops import env_ops
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _settings(tmp_path) -> Settings:
    return Settings(teams={"1": _team(tmp_path)})


def _container(labels: dict[str, str]) -> MagicMock:
    container = MagicMock()
    container.labels = labels
    return container


def _labels(**overrides: str) -> dict[str, str]:
    labels = {
        "oduflow.branch": "dev1",
        "oduflow.git_branch": "feature/old",
        "oduflow.repo": "https://github.com/acme/addons.git",
        "oduflow.git_user": "ada",
    }
    labels.update(overrides)
    return labels


# --- git plumbing ---------------------------------------------------------


class TestFetchBranch:
    def test_an_unpushed_branch_is_reported_as_missing_not_as_a_git_failure(
        self, tmp_path
    ):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: couldn't find remote ref refs/heads/feature/new"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(NotFoundError) as excinfo:
                git_ops.fetch_branch(str(tmp_path), "feature/new")

        assert "does not exist on origin" in str(excinfo.value)
        assert "git push -u origin feature/new" in str(excinfo.value)

    def test_any_other_fetch_failure_stays_an_external_command_error(self, tmp_path):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: Authentication failed"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(ExternalCommandError):
                git_ops.fetch_branch(str(tmp_path), "feature/new")

    def test_credentials_are_never_echoed_back_in_the_error(self, tmp_path):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: https://ada:ghp_secret@github.com/acme/addons.git denied"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(ExternalCommandError) as excinfo:
                git_ops.fetch_branch(str(tmp_path), "feature/new")

        assert "ghp_secret" not in str(excinfo.value)

    def test_returns_the_fetched_remote_tip(self, tmp_path):
        with (
            patch("subprocess.run") as run,
            patch.object(git_ops, "rev_parse", return_value="cafe123") as rev_parse,
        ):
            assert git_ops.fetch_branch(str(tmp_path), "feature/new") == "cafe123"

        assert run.call_count == 1
        assert rev_parse.call_args[0][1] == "refs/remotes/origin/feature/new"


class TestCheckoutBranch:
    def test_moves_the_clone_onto_the_branch_and_returns_the_tree_diff(self, tmp_path):
        heads = iter(["old111", "new222"])

        with (
            patch("subprocess.run") as run,
            patch.object(
                git_ops, "rev_parse", side_effect=lambda *_a, **_k: next(heads)
            ),
            patch.object(git_ops, "diff_names", return_value=["sale_x/models/a.py"]),
        ):
            old, new, files = git_ops.checkout_branch(str(tmp_path), "feature/new")

        assert (old, new, files) == ("old111", "new222", ["sale_x/models/a.py"])
        argv = run.call_args[0][0]
        # --force keeps the managed clone's policy identical to pull_repo's
        # reset --hard, and -B pins the local branch on the fetched remote tip.
        assert argv[3:] == [
            "checkout",
            "--force",
            "-B",
            "feature/new",
            "refs/remotes/origin/feature/new",
        ]

    def test_an_identical_tip_reports_no_changed_files(self, tmp_path):
        with (
            patch("subprocess.run"),
            patch.object(git_ops, "rev_parse", return_value="same"),
            patch.object(git_ops, "diff_names") as diff_names,
        ):
            old, new, files = git_ops.checkout_branch(str(tmp_path), "feature/new")

        assert (old, new, files) == ("same", "same", [])
        diff_names.assert_not_called()


# --- switch_environment_branch -------------------------------------------


@pytest.fixture
def switch_env(tmp_path):
    """Patch everything a switch touches; yield the mocks it is asserted on."""
    client = MagicMock()
    client.containers.get.return_value = _container(_labels())

    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch.object(git_ops, "is_git_repository", return_value=True),
        patch.object(git_ops, "fetch_branch", return_value="tip999") as fetch,
        patch.object(
            git_ops,
            "checkout_branch",
            return_value=("old111", "new222", ["sale_x/a.py"]),
        ) as checkout,
        patch.object(env_ops, "update_environment") as update,
        patch.object(
            env_ops,
            "pull_environment",
            return_value={"action": "restart", "message": "Restarted."},
        ) as pull,
        patch.object(env_ops, "_agent_add_env") as agent,
        patch.object(env_ops, "_agent_rename_env") as agent_rename,
        patch.object(env_ops, "_db_exists", return_value=False) as db_exists,
    ):
        yield {
            "client": client,
            "fetch": fetch,
            "checkout": checkout,
            "update": update,
            "pull": pull,
            "agent": agent,
            "agent_rename": agent_rename,
            "db_exists": db_exists,
        }


def _switch(tmp_path, **kwargs):
    return env_ops.switch_environment_branch(
        _settings(tmp_path), _team(tmp_path), "dev1", "feature/new", **kwargs
    )


class TestSwitchEnvironmentBranch:
    def test_the_label_is_flipped_before_the_checkout(self, tmp_path, switch_env):
        calls: list[str] = []
        switch_env["update"].side_effect = lambda *a, **k: calls.append("label")
        switch_env["checkout"].side_effect = lambda *a, **k: (
            calls.append("checkout") or ("old111", "new222", ["sale_x/a.py"])
        )

        _switch(tmp_path)

        # Reversed, a failed label flip would leave a switched tree that the
        # next pull silently resets back to the old branch.
        assert calls == ["label", "checkout"]

    def test_only_the_branch_label_changes_and_no_image_is_pulled(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path)

        kwargs = switch_env["update"].call_args.kwargs
        assert kwargs["label_overrides"] == {"oduflow.git_branch": "feature/new"}
        assert kwargs["pull_image"] is False
        # The checkout is still on the old branch at that point, so a changed
        # requirements.txt belongs to the diff, not to this recreate.
        assert kwargs["install_dependencies"] is False
        assert switch_env["pull"].call_args.kwargs["reinstall_all_deps"] is True

    def test_strict_only_applies_to_the_normal_apply_path(self, tmp_path, switch_env):
        with patch("oduflow.docker_ops.odoo_ops._get_module_states") as module_states:
            _switch(tmp_path, upgrade=["sale_x"], strict=True)

        module_states.assert_not_called()
        kwargs = switch_env["pull"].call_args.kwargs
        assert kwargs["presynced"] == ("old111", ["sale_x/a.py"])
        assert kwargs["upgrade"] == ["sale_x"]
        assert kwargs["strict"] is True

    def test_the_result_records_both_branches_and_the_switch(
        self, tmp_path, switch_env
    ):
        result = _switch(tmp_path)

        assert result["branch"] == "feature/new"
        assert result["previous_branch"] == "feature/old"
        assert result["branch_switched"] is True
        assert result["old_head"] == "old111"
        assert result["new_head"] == "new222"
        assert result["message"].startswith(
            "Switched 'dev1' from 'feature/old' to 'feature/new'."
        )

    def test_two_branches_with_the_same_tree_do_not_report_being_up_to_date(
        self, tmp_path, switch_env
    ):
        switch_env["checkout"].return_value = ("same", "same", [])
        switch_env["pull"].return_value = {
            "action": "none",
            "message": "Already up to date.",
        }

        result = _switch(tmp_path)

        # "Already up to date" is pull vocabulary; after a switch it reads as a
        # contradiction of the switch that just happened.
        assert result["message"] == (
            "Switched 'dev1' from 'feature/old' to 'feature/new'. "
            "No code difference to apply."
        )

    def test_the_agents_own_checkout_follows_the_switch(self, tmp_path, switch_env):
        _switch(tmp_path)

        args = switch_env["agent"].call_args[0]
        assert args[3:] == (
            "dev1",
            "https://github.com/acme/addons.git",
            "feature/new",
            "ada",
        )

    def test_extra_addons_branches_switch_with_the_main_repo(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path, extra_addons={"enterprise": "19.0"})

        overrides = switch_env["update"].call_args.kwargs["label_overrides"]
        assert overrides["oduflow.git_branch"] == "feature/new"
        assert overrides["oduflow.extra_addons"] == '{"enterprise": "19.0"}'

    def test_switching_to_the_branch_it_is_already_on_pulls_instead(
        self, tmp_path, switch_env
    ):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.git_branch": "feature/new"})
        )

        result = _switch(tmp_path)

        assert result["branch_switched"] is False
        switch_env["update"].assert_not_called()
        switch_env["checkout"].assert_not_called()
        switch_env["pull"].assert_called_once()
        assert not switch_env["pull"].call_args.kwargs.get("reinstall_all_deps")
        assert "already on branch 'feature/new'" in result["message"]

    def test_a_live_mounted_environment_is_refused(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.local_path": "/home/ada/addons"})
        )

        with pytest.raises(PrerequisiteNotMetError) as excinfo:
            _switch(tmp_path)

        assert "/home/ada/addons" in str(excinfo.value)
        switch_env["update"].assert_not_called()

    def test_a_production_is_refused_by_name_before_docker_is_touched(
        self, tmp_path, switch_env
    ):
        with pytest.raises(ConflictError) as excinfo:
            env_ops.switch_environment_branch(
                _settings(tmp_path), _team(tmp_path), "prod-erp", "feature/new"
            )

        assert "update_production" in str(excinfo.value)
        switch_env["client"].containers.get.assert_not_called()

    def test_a_production_container_is_refused_by_label_too(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.prod": "true"})
        )

        with pytest.raises(ConflictError):
            _switch(tmp_path)

        switch_env["update"].assert_not_called()

    def test_an_unpushed_branch_leaves_the_environment_untouched(
        self, tmp_path, switch_env
    ):
        switch_env["fetch"].side_effect = NotFoundError("no such branch on origin")

        with pytest.raises(NotFoundError):
            _switch(tmp_path)

        switch_env["update"].assert_not_called()
        switch_env["checkout"].assert_not_called()

    def test_a_failed_checkout_says_the_label_already_moved(self, tmp_path, switch_env):
        switch_env["checkout"].side_effect = ExternalCommandError(
            "git checkout", 1, "index locked"
        )

        with pytest.raises(ExternalCommandError) as excinfo:
            _switch(tmp_path)

        message = str(excinfo.value)
        assert "labelled with branch 'feature/new'" in message
        assert "still on 'feature/old'" in message
        switch_env["pull"].assert_not_called()

    def test_a_missing_environment_is_a_not_found_error(self, tmp_path, switch_env):
        import docker

        switch_env["client"].containers.get.side_effect = docker.errors.NotFound("nope")

        with pytest.raises(NotFoundError):
            _switch(tmp_path)


# --- renaming an environment while switching -----------------------------


def _rename_team(tmp_path) -> TeamSettings:
    """A team whose registries live on disk, so key moves can be asserted."""
    return TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
        hostname_registry_path=str(tmp_path / "hostnames.json"),
    )


class TestSwitchWithRename:
    def test_the_rename_rides_on_the_same_container_recreate(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path, new_name="dev2")

        # A separate recreate for the rename would stop Odoo twice for one
        # operation.
        switch_env["update"].assert_called_once()
        kwargs = switch_env["update"].call_args.kwargs
        assert kwargs["rename_to"] == "dev2"
        assert kwargs["label_overrides"] == {"oduflow.git_branch": "feature/new"}
        # The switch refreshes the agent checkout itself, with the target
        # branch; update_environment must not run clone-env.sh a first time.
        assert kwargs["sync_agent_checkout"] is False

    def test_the_checkout_and_the_apply_run_under_the_new_name(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path, new_name="dev2")

        # The workspace directory moved with the rename, so the old path no
        # longer exists by the time the branch is checked out.
        assert switch_env["checkout"].call_args[0][0].endswith("/dev2/repo")
        assert switch_env["pull"].call_args[0][2] == "dev2"

    def test_the_agent_checkout_is_moved_not_re_cloned(self, tmp_path, switch_env):
        _switch(tmp_path, new_name="dev2")

        # Removing it would delete the coding agent's uncommitted work.
        assert switch_env["agent_rename"].call_args[0][3:] == ("dev1", "dev2")
        assert switch_env["agent"].call_args[0][3] == "dev2"

    def test_the_result_and_message_report_both_names(self, tmp_path, switch_env):
        result = _switch(tmp_path, new_name="dev2")

        assert result["env_name"] == "dev2"
        assert result["renamed_from"] == "dev1"
        assert result["message"].startswith(
            "Switched 'dev1' from 'feature/old' to 'feature/new' and renamed it "
            "to 'dev2'."
        )

    def test_renaming_to_the_current_name_is_a_plain_switch(self, tmp_path, switch_env):
        result = _switch(tmp_path, new_name="dev1")

        assert switch_env["update"].call_args.kwargs["rename_to"] is None
        assert "renamed_from" not in result
        switch_env["agent_rename"].assert_not_called()

    def test_a_rename_alone_is_applied_when_the_branch_is_unchanged(
        self, tmp_path, switch_env
    ):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.git_branch": "feature/new"})
        )

        switch_env["pull"].return_value.update(env_name="dev2", renamed_from="dev1")
        result = _switch(tmp_path, new_name="dev2")
        assert switch_env["pull"].call_args.kwargs["rename_to"] == "dev2"
        switch_env["update"].assert_not_called()
        assert result["branch_switched"] is False
        assert result["env_name"] == "dev2"
        assert result["renamed_from"] == "dev1"

    def test_a_guardrail_refusal_blocks_the_rename_too(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.git_branch": "feature/new"})
        )
        switch_env["pull"].return_value = {
            "action": "blocked",
            "message": "Guardrail (strict) refused.",
        }

        result = _switch(tmp_path, new_name="dev2", strict=True)

        # "Blocked" has to mean nothing changed — the name included.
        switch_env["update"].assert_not_called()
        assert result["env_name"] == "dev1"
        assert "renamed_from" not in result
        assert "was not applied" in result["message"]

    def test_an_invalid_name_is_refused_before_anything_is_mutated(
        self, tmp_path, switch_env
    ):
        with pytest.raises(ValueError):
            _switch(tmp_path, new_name="../escape")

        switch_env["update"].assert_not_called()
        switch_env["fetch"].assert_not_called()

    def test_a_stack_managed_environment_is_refused(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.stack": "demo"})
        )

        with pytest.raises(ConflictError) as excinfo:
            _switch(tmp_path, new_name="dev2")

        assert "demo" in str(excinfo.value)
        switch_env["update"].assert_not_called()

    def test_a_name_taken_by_another_environment_is_refused(self, tmp_path, switch_env):
        taken = _container(_labels())
        own = switch_env["client"].containers.get.return_value

        def by_name(name: str):
            return taken if name.endswith("-dev2-odoo") else own

        switch_env["client"].containers.get.side_effect = by_name

        with pytest.raises(ConflictError) as excinfo:
            _switch(tmp_path, new_name="dev2")

        assert "already exists" in str(excinfo.value)
        switch_env["update"].assert_not_called()


class TestCheckRenameTarget:
    def _check(self, tmp_path, new_name, *, client=None, db_exists=False):
        client = client or MagicMock()
        with patch.object(env_ops, "_db_exists", return_value=db_exists):
            env_ops.check_rename_target(
                client,
                _settings(tmp_path),
                _team(tmp_path),
                "dev1",
                new_name,
                container_id="own",
            )

    def test_a_free_name_passes(self, tmp_path):
        import docker

        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nope")

        self._check(tmp_path, "dev2", client=client)

    def test_the_production_namespace_is_reserved(self, tmp_path):
        import docker

        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nope")

        with pytest.raises(ConflictError):
            self._check(tmp_path, "prod-erp", client=client)

    def test_a_production_environment_cannot_be_renamed(self, tmp_path):
        # Its name is recorded in productions.json; moving the container and
        # database behind that record would strand the deploy log too.
        client = MagicMock()

        with pytest.raises(ConflictError, match="production"):
            env_ops.check_rename_target(
                client,
                _settings(tmp_path),
                _team(tmp_path),
                "prod-erp",
                "erp",
                container_id="own",
            )

        client.containers.get.assert_not_called()

    def test_a_production_container_is_recognised_by_its_label(self, tmp_path):
        client = MagicMock()

        with pytest.raises(ConflictError, match="production"):
            env_ops.check_rename_target(
                client,
                _settings(tmp_path),
                _team(tmp_path),
                "dev1",
                "dev2",
                container_id="own",
                source_labels={"oduflow.prod": "true"},
            )

        client.containers.get.assert_not_called()

    def test_an_existing_workspace_directory_is_a_conflict(self, tmp_path):
        import docker

        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nope")
        (tmp_path / "workspaces" / "dev2").mkdir(parents=True)

        with pytest.raises(ConflictError) as excinfo:
            self._check(tmp_path, "dev2", client=client)

        assert "workspace directory" in str(excinfo.value)

    def test_an_existing_database_is_a_conflict(self, tmp_path):
        import docker

        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nope")

        with pytest.raises(ConflictError) as excinfo:
            self._check(tmp_path, "dev2", client=client, db_exists=True)

        assert "oduflow_1_dev2" in str(excinfo.value)

    def test_a_rename_that_keeps_the_slug_does_not_collide_with_itself(self, tmp_path):
        # feature/x and feature-x derive the same container, path and database;
        # the environment's own resources must not read as someone else's.
        client = MagicMock()
        client.containers.get.return_value = MagicMock(id="own")
        (tmp_path / "workspaces" / "feature-x").mkdir(parents=True)

        with patch.object(env_ops, "_db_exists", return_value=True):
            env_ops.check_rename_target(
                client,
                _settings(tmp_path),
                _team(tmp_path),
                "feature/x",
                "feature-x",
                container_id="own",
            )


class TestRelocateEnvironmentState:
    def _relocate(self, tmp_path, *, sql=None, db_exists=True):
        client = MagicMock()
        with (
            patch.object(env_ops, "_unmount_filestore"),
            patch.object(env_ops, "_db_exists", return_value=db_exists),
            patch.object(env_ops, "_exec_sql", side_effect=sql) as exec_sql,
        ):
            env_ops._relocate_environment_state(
                client, _settings(tmp_path), _rename_team(tmp_path), "dev1", "dev2"
            )
        return exec_sql

    def test_the_workspace_directory_moves_with_its_contents(self, tmp_path):
        old = tmp_path / "workspaces" / "dev1"
        old.mkdir(parents=True)
        (old / "env_credentials.json").write_text('{"pg_user": "u_1_dev1"}')
        (old / ".protected").touch()

        self._relocate(tmp_path)

        new = tmp_path / "workspaces" / "dev2"
        assert not old.exists()
        # Credentials, protection and the note are keyed by the directory, so
        # they follow the environment without any per-file handling.
        assert (new / "env_credentials.json").read_text() == '{"pg_user": "u_1_dev1"}'
        assert (new / ".protected").exists()

    def test_open_connections_are_terminated_before_the_database_is_renamed(
        self, tmp_path
    ):
        (tmp_path / "workspaces" / "dev1").mkdir(parents=True)

        exec_sql = self._relocate(tmp_path)

        statements = [call[0][2] for call in exec_sql.call_args_list]
        assert "pg_terminate_backend" in statements[0]
        assert "oduflow_1_dev1" in statements[0]
        assert statements[1] == (
            'ALTER DATABASE "oduflow_1_dev1" RENAME TO "oduflow_1_dev2";'
        )

    def test_a_failed_database_rename_puts_the_directory_back(self, tmp_path):
        old = tmp_path / "workspaces" / "dev1"
        old.mkdir(parents=True)

        def fail(*_a, **_k):
            raise RuntimeError("database is being accessed by other users")

        with pytest.raises(RuntimeError):
            self._relocate(tmp_path, sql=fail)

        # Half-renamed is the failure this operation must not produce: the
        # environment stays whole under its old name.
        assert old.exists()
        assert not (tmp_path / "workspaces" / "dev2").exists()

    def test_port_hostname_activity_and_chat_sessions_follow_the_name(self, tmp_path):
        from oduflow import activity, agent_sessions, hostname_registry, port_registry

        team = _rename_team(tmp_path)
        (tmp_path / "workspaces" / "dev1").mkdir(parents=True)
        port_registry.allocate_port(team.port_registry_path, "dev1", 50000, 50010)
        hostname_registry.allocate_hostname(
            team.hostname_registry_path, "dev1", 5, hostname_prefix="dev"
        )
        activity.touch(team, "dev1")
        agent_sessions.set_session(team, "dev1", "claude", "session-1")

        self._relocate(tmp_path)

        # The port and the hostname ARE the environment's address; a release +
        # re-allocate could hand back different ones.
        assert port_registry.get_port(team.port_registry_path, "dev1") is None
        assert port_registry.get_port(team.port_registry_path, "dev2") == 50000
        assert hostname_registry.get_hostname(team.hostname_registry_path, "dev2") == (
            "dev1"
        )
        assert "dev1" not in activity.get_all(team)
        assert "dev2" in activity.get_all(team)
        assert agent_sessions.get_session(team, "dev1", "claude") is None
        assert agent_sessions.get_session(team, "dev2", "claude") == "session-1"

    def test_a_rename_that_keeps_the_slug_leaves_the_database_alone(self, tmp_path):
        client = MagicMock()
        (tmp_path / "workspaces" / "feature-x").mkdir(parents=True)

        with (
            patch.object(env_ops, "_unmount_filestore"),
            patch.object(env_ops, "_db_exists", return_value=True),
            patch.object(env_ops, "_exec_sql") as exec_sql,
        ):
            env_ops._relocate_environment_state(
                client,
                _settings(tmp_path),
                _rename_team(tmp_path),
                "feature/x",
                "feature-x",
            )

        # Same slug, same database name — ALTER DATABASE would fail on it.
        exec_sql.assert_not_called()


class TestRebaseVolumes:
    def test_workspace_binds_and_the_filestore_path_follow_the_rename(self):
        volumes = {
            "/data/workspaces/dev1/repo": {"bind": "/mnt/extra-addons", "mode": "rw"},
            "/data/workspaces/dev1/filestore": {
                "bind": "/var/lib/odoo/.local/share/Odoo/filestore/oduflow_1_dev1",
                "mode": "rw",
            },
            "/data/shared_extra_checkouts/enterprise/abc": {
                "bind": "/mnt/extra-addons-enterprise",
                "mode": "ro",
            },
        }

        rebased = env_ops._rebase_volumes(
            volumes,
            "/data/workspaces/dev1",
            "/data/workspaces/dev2",
            "oduflow_1_dev1",
            "oduflow_1_dev2",
        )

        assert rebased["/data/workspaces/dev2/repo"]["bind"] == "/mnt/extra-addons"
        assert rebased["/data/workspaces/dev2/filestore"]["bind"] == (
            "/var/lib/odoo/.local/share/Odoo/filestore/oduflow_1_dev2"
        )
        # Shared checkouts live outside the workspace and are not name-keyed.
        assert "/data/shared_extra_checkouts/enterprise/abc" in rebased


@pytest.fixture
def dependency_switch(tmp_path, monkeypatch):
    """Run switch, recreate, pull and installation; fake only Docker/Git I/O."""
    from types import SimpleNamespace

    team = _team(tmp_path)
    settings = Settings(routing_mode="port", teams={"1": team})
    repo = tmp_path / "workspaces" / "dev1" / "repo"
    repo.mkdir(parents=True)
    extra = tmp_path / "extra-old"
    extra.mkdir()
    target_extra = tmp_path / "extra-new"
    target_extra.mkdir()
    for path, package in (
        (repo, "main-shared"),
        (extra, "extra-old"),
        (target_extra, "extra-new"),
    ):
        (path / "requirements.txt").write_text(package)
        (path / ".oduflow").mkdir()
        (path / ".oduflow" / "apt_packages.txt").write_text("curl\n")

    state = SimpleNamespace(
        files=[],
        target_extra=extra,
        extra_files=[],
        containers=[],
        pip_failure=False,
        apt_failure=False,
    )

    def make_container(labels, volumes):
        container = MagicMock()
        container.labels = labels
        container.image.tags = ["odoo:18.0"]
        container.image.id = "sha256:odoo"
        container.attrs = {
            "Config": {"Cmd": ["odoo"], "Image": "odoo:18.0"},
            "HostConfig": {
                "Binds": [
                    f"{host}:{spec['bind']}:{spec['mode']}"
                    for host, spec in volumes.items()
                ],
                "PortBindings": {"8069/tcp": [{"HostPort": "50000"}]},
            },
        }
        container.packages = []
        container.apt_installs = []

        def execute(command, **kwargs):
            if command.startswith("pip3 install"):
                if state.pip_failure:
                    return 1, b"package unavailable"
                from pathlib import Path

                required = command.split()[-1]
                host = next(
                    host
                    for host, spec in volumes.items()
                    if required.startswith(spec["bind"] + "/")
                )
                relative = required.removeprefix(volumes[host]["bind"] + "/")
                container.packages.append((Path(host) / relative).read_text())
            if command.startswith("apt-get install"):
                if state.apt_failure:
                    return 1, b"apt unavailable"
                container.apt_installs.append(command)
            return 0, b"ok"

        container.exec_run.side_effect = execute
        state.containers.append(container)
        return container

    volumes = {
        str(repo): {"bind": "/mnt/extra-addons", "mode": "rw"},
        str(extra): {"bind": "/mnt/extra-addons-custom", "mode": "ro"},
    }
    state.current = make_container(
        _labels(
            **{
                "oduflow.extra_addons": json.dumps({"custom": "main"}),
                "oduflow.extra_addons_revisions": json.dumps({"custom": "old"}),
            }
        ),
        volumes,
    )
    client = MagicMock()
    client.containers.get.side_effect = lambda *a, **kw: state.current

    def run(**kwargs):
        state.current = make_container(kwargs["labels"], kwargs["volumes"])
        return state.current

    client.containers.run.side_effect = run
    monkeypatch.setattr(env_ops, "get_client", lambda: client)
    monkeypatch.setattr(
        env_ops,
        "load_credentials",
        lambda *a: {
            "pg_user": settings.db_user,
            "pg_password": settings.db_password,
        },
    )
    monkeypatch.setattr(env_ops, "_reapply_odoo_conf", lambda *a: False)
    monkeypatch.setattr(
        env_ops, "_reconcile_environment_hostname_for_update", lambda *a: False
    )
    monkeypatch.setattr(env_ops, "_agent_add_env", lambda *a: None)
    monkeypatch.setattr(git_ops, "is_git_repository", lambda *a: True)
    monkeypatch.setattr(git_ops, "fetch_branch", lambda *a, **kw: "target")
    monkeypatch.setattr(git_ops, "pull_repo", lambda *a, **kw: ("old", state.files))

    def checkout(*args):
        if "requirements.txt" in state.files:
            (repo / "requirements.txt").write_text("main-target")
        return "old", "target", state.files

    monkeypatch.setattr(git_ops, "checkout_branch", checkout)
    monkeypatch.setattr(
        "oduflow.extra_addons.ensure_shared_checkout",
        lambda *a, **kw: {
            "path": str(state.target_extra),
            "revision": "old" if state.target_extra == extra else "new",
            "changed_files": state.extra_files,
        },
    )
    state.switch = lambda **kwargs: env_ops.switch_environment_branch(
        settings, team, "dev1", "feature/new", **kwargs
    )
    state.new_extra = target_extra
    state.client = client
    state.repo = repo
    state.settings = settings
    state.team = team
    return state


@pytest.mark.parametrize("files", [[], ["README.md"], ["requirements.txt"]])
def test_switch_restores_all_repos_from_target_code(dependency_switch, files):
    state = dependency_switch
    state.files = files
    result = state.switch()

    main_package = "main-target" if "requirements.txt" in files else "main-shared"
    assert state.current.packages == [main_package, "extra-old"]
    assert len(state.current.apt_installs) == 2
    state.current.restart.assert_called_once()
    assert result["exit_code"] == 0
    assert result["branch_switched"] is True
    assert "Reinstalled dependencies" in result["message"]
    assert len(state.containers) == 2


@pytest.mark.parametrize("files", [[], ["requirements.txt"]])
def test_extra_mount_recreate_installs_once_in_final_container(
    dependency_switch, files
):
    state = dependency_switch
    state.target_extra = state.new_extra
    state.extra_files = files
    result = state.switch()

    assert len(state.containers) == 3
    assert state.containers[1].packages == []
    assert state.current.packages == ["main-shared", "extra-new"]
    assert len(state.current.apt_installs) == 2
    assert "[PIP]" in result["output"]
    assert "Reinstalled dependencies" in result["message"]


def test_strict_restores_current_mounts_before_refusing_modules(
    dependency_switch, monkeypatch
):
    state = dependency_switch
    state.target_extra = state.new_extra
    state.extra_files = ["sale/security/rules.xml"]
    monkeypatch.setattr(
        "oduflow.git_analysis.guardrail_warnings", lambda *a: ["upgrade required"]
    )
    with patch("oduflow.docker_ops.odoo_ops.upgrade_odoo_modules") as upgrade:
        result = state.switch(restart=True, strict=True)

    assert result["action"] == "blocked"
    assert result["branch_switched"] is True
    assert state.current.packages == ["main-shared", "extra-old"]
    state.current.restart.assert_called_once()
    assert len(state.containers) == 2
    assert "Restored dependencies" in result["message"]
    assert "[PIP]" in result["output"]
    upgrade.assert_not_called()


@pytest.mark.parametrize("failure", ["pip_failure", "apt_failure"])
@pytest.mark.parametrize("strict", [False, True])
def test_failed_restore_stops_module_actions(
    dependency_switch, monkeypatch, failure, strict
):
    state = dependency_switch
    setattr(state, failure, True)
    state.files = ["README.md"]
    monkeypatch.setattr(
        "oduflow.git_analysis.guardrail_warnings", lambda *a: ["incomplete action"]
    )
    with (
        patch("oduflow.docker_ops.odoo_ops.install_odoo_modules") as install,
        patch("oduflow.docker_ops.odoo_ops.upgrade_odoo_modules") as upgrade,
    ):
        result = state.switch(install=["sale"], upgrade=["stock"], strict=strict)

    assert result["action"] == "error"
    assert result["exit_code"] == 1
    assert "FAILED" in result["output"]
    install.assert_not_called()
    upgrade.assert_not_called()
    state.current.restart.assert_not_called()


def test_extra_mount_recreate_dependency_failure_is_not_swallowed(dependency_switch):
    state = dependency_switch
    state.target_extra = state.new_extra
    state.pip_failure = True
    with patch.object(env_ops, "_cleanup_legacy_extra_worktrees") as cleanup:
        result = state.switch()
    assert result["action"] == "error"
    assert result["exit_code"] == 1
    assert "package unavailable" in result["output"]
    cleanup.assert_called_once()


def test_same_branch_without_rename_does_not_reinstall(dependency_switch):
    state = dependency_switch
    state.current.labels["oduflow.git_branch"] = "feature/new"
    result = state.switch()
    assert result["branch_switched"] is False
    assert len(state.containers) == 1
    state.current.exec_run.assert_not_called()
    state.current.restart.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_same_branch_rename_restores_dependencies(
    dependency_switch, monkeypatch, failed
):
    state = dependency_switch
    state.current.labels["oduflow.git_branch"] = "feature/new"
    state.pip_failure = failed
    monkeypatch.setattr(env_ops, "_db_exists", lambda *a: False)

    def relocate(client, settings, team, old, new):
        state.repo.parent.rename(state.repo.parent.with_name(new))

    monkeypatch.setattr(env_ops, "_relocate_environment_state", relocate)
    with (
        patch.object(env_ops, "_agent_rename_env") as move_agent,
        patch.object(env_ops, "_agent_add_env") as sync_agent,
    ):
        result = state.switch(new_name="dev2")
    assert result["renamed_from"] == "dev1"
    assert result["env_name"] == "dev2"
    assert bool(result.get("exit_code")) == failed
    assert "[PIP]" in result["output"]
    if not failed:
        assert state.current.packages == ["main-shared", "extra-old"]
    move_agent.assert_called_once()
    assert move_agent.call_args.args[-2:] == ("dev1", "dev2")
    sync_agent.assert_called_once()
    assert sync_agent.call_args.args[3] == "dev2"
    assert len(state.containers) == 2


@pytest.mark.parametrize("operation", ["switch-branch", "update"])
def test_environment_http_reports_failed_dependency_result(tmp_path, operation):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oduflow.locking import LockManager
    from oduflow.web_ui import mount_web_ui

    app = Starlette()
    settings = _settings(tmp_path)
    mount_web_ui(app, lambda: settings, LockManager())
    result = {
        "action": "error",
        "exit_code": 1,
        "message": "Switched branch. Dependency installation failed.",
        "output": "[PIP] install FAILED: unavailable",
    }
    function = (
        "switch_environment_branch"
        if operation == "switch-branch"
        else "update_environment"
    )
    with patch.object(env_ops, function, return_value=result):
        response = TestClient(app).post(
            f"/api/environments/dev1/{operation}", json={"branch": "feature/new"}
        )
    if operation == "switch-branch":
        # The switch persisted; the failed module/dependency apply is still a
        # completed request, reported via the result like /sync does.
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["result"]["exit_code"] == 1
    else:
        assert response.status_code == 500
        assert response.json()["ok"] is False
    assert response.json()["result"]["output"] == result["output"]


def test_switch_restores_dependencies_before_module_actions(dependency_switch):
    state = dependency_switch
    state.files = ["requirements.txt"]
    applied = []

    def apply_module(*args):
        assert state.current.packages == ["main-target", "extra-old"]
        assert len(state.current.apt_installs) == 2
        applied.append(args[-1])
        return {"exit_code": 0, "output": "module applied"}

    with (
        patch(
            "oduflow.docker_ops.odoo_ops.install_odoo_modules", side_effect=apply_module
        ),
        patch(
            "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules", side_effect=apply_module
        ),
    ):
        result = state.switch(install=["sale"], upgrade=["stock"])

    assert applied == ["sale", "stock"]
    assert result["exit_code"] == 0
    state.current.restart.assert_called_once()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("conf_applied", [False, True])
def test_switch_without_dependency_files_does_not_restart(
    dependency_switch, monkeypatch, strict, conf_applied
):
    state = dependency_switch
    monkeypatch.setattr(env_ops, "_reapply_odoo_conf", lambda *a: conf_applied)
    for root in (state.repo, state.target_extra):
        (root / "requirements.txt").unlink()
        (root / ".oduflow" / "apt_packages.txt").unlink()
    if strict:
        state.files = ["README.md"]
        monkeypatch.setattr(
            "oduflow.git_analysis.guardrail_warnings", lambda *a: ["upgrade required"]
        )
    result = state.switch(upgrade=["sale"] if strict else None, strict=strict)
    assert state.current.restart.call_count == int(conf_applied)
    assert "Dependencies changed" not in result["message"]
    assert "Restored dependencies" not in result["message"]
    assert result["action"] == ("blocked" if strict else "none")
    if not strict:
        assert "No code difference to apply." in result["message"]


def test_auto_dependency_failure_reports_exact_pending_modules(
    dependency_switch, monkeypatch
):
    state = dependency_switch
    state.pip_failure = True
    state.files = ["sale/models/order.py"]
    monkeypatch.setattr(
        "oduflow.git_analysis.recommend",
        lambda *a: {
            "action": "upgrade",
            "modules_to_install": ["new_module"],
            "modules_to_upgrade": ["sale"],
        },
    )
    result = state.switch()
    assert result["pending_actions"]["install"] == ["new_module"]
    assert result["pending_actions"]["upgrade"] == ["sale"]
    # The machine-readable retry is an ordered plan: restore dependencies
    # first (update_environment), then replay the module actions.
    assert [step["tool"] for step in result["retry"]] == [
        "update_environment",
        "pull_and_apply",
    ]
    assert result["retry"][0]["arguments"] == {"env_name": "dev1"}
    assert result["retry"][1]["arguments"]["install"] == "new_module"
    assert result["retry"][1]["arguments"]["upgrade"] == "sale"
    assert "new_module" in result["message"]
    assert "sale" in result["message"]


@pytest.fixture
def rename_switch(dependency_switch, monkeypatch):
    state = dependency_switch
    state.current.labels["oduflow.git_branch"] = "feature/new"
    monkeypatch.setattr(env_ops, "_db_exists", lambda *a: False)
    monkeypatch.setattr(env_ops, "_agent_rename_env", lambda *a: None)
    monkeypatch.setattr(
        env_ops,
        "_relocate_environment_state",
        lambda *a: state.repo.parent.rename(state.repo.parent.with_name(a[-1])),
    )
    return state


@pytest.mark.parametrize("extra_changed", [[], ["requirements.txt"]])
def test_same_branch_rename_and_extra_mounts_recreate_once(
    rename_switch, extra_changed
):
    state = rename_switch
    state.target_extra = state.new_extra
    state.extra_files = extra_changed
    result = state.switch(new_name="dev2")
    assert result["env_name"] == "dev2"
    assert len(state.containers) == 2
    assert state.current.packages == ["main-shared", "extra-new"]
    assert len(state.current.apt_installs) == 2


def test_module_failure_does_not_cancel_same_branch_rename(rename_switch):
    state = rename_switch
    with patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 1, "output": "upgrade traceback"},
    ) as upgrade:
        result = state.switch(new_name="dev2", upgrade=["sale"])
    assert result["env_name"] == "dev2"
    assert result["renamed_from"] == "dev1"
    assert result["exit_code"] == 1
    assert "upgrade traceback" in result["output"]
    assert upgrade.call_args.args[2:] == ("dev2", "sale")


@pytest.mark.parametrize("replace_mounts", [False, True])
def test_auto_plan_can_be_replayed_after_dependency_repair(
    dependency_switch, monkeypatch, replace_mounts
):
    state = dependency_switch
    if replace_mounts:
        state.target_extra = state.new_extra
    state.files = ["sale/models/order.py"]
    state.pip_failure = True
    monkeypatch.setattr(
        "oduflow.git_analysis.recommend",
        lambda *a: {
            "action": "upgrade",
            "modules_to_install": ["new_module"],
            "modules_to_upgrade": ["sale"],
        },
    )
    failed = state.switch()
    assert failed["pending_actions"]["upgrade"] == ["sale"]
    repair, replay = failed["retry"]
    assert repair["tool"] == "update_environment"
    assert replay["tool"] == "pull_and_apply"
    assert replay["arguments"]["install"] == "new_module"

    state.files = []
    state.pip_failure = False
    updated = env_ops.update_environment(
        state.settings, state.team, repair["arguments"]["env_name"], pull_image=False
    )
    assert updated["exit_code"] == 0
    retry = replay["arguments"]
    with (
        patch(
            "oduflow.docker_ops.odoo_ops.install_odoo_modules",
            return_value={"exit_code": 0},
        ) as install,
        patch(
            "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
            return_value={"exit_code": 0},
        ) as upgrade,
    ):
        result = env_ops.pull_environment(
            state.settings,
            state.team,
            retry["env_name"],
            install=retry["install"].split(","),
            upgrade=retry["upgrade"].split(","),
            restart=retry["restart"],
        )
    assert result["exit_code"] == 0
    assert result["changed_files"] == []
    assert install.call_args.args[-1] == "new_module"
    assert upgrade.call_args.args[-1] == "sale"


def test_strict_same_branch_rename_does_not_change_identity_or_mounts(
    rename_switch, monkeypatch
):
    state = rename_switch
    state.target_extra = state.new_extra
    state.extra_files = ["sale/security/rules.xml"]
    monkeypatch.setattr(
        "oduflow.git_analysis.guardrail_warnings", lambda *a: ["upgrade required"]
    )
    with patch.object(env_ops, "_relocate_environment_state") as relocate:
        result = state.switch(new_name="dev2", restart=True, strict=True)
    assert result["action"] == "blocked"
    assert result["env_name"] == "dev1"
    assert not result.get("renamed_from")
    assert len(state.containers) == 1
    relocate.assert_not_called()
