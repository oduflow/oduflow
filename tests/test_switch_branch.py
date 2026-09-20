"""Reusing an environment for another branch (``switch_environment_branch``).

The environment is the reusable unit: switching the branch must keep the
database, filestore and routing while changing only the code. What these tests
pin down is the part that is easy to get subtly wrong: mutation order, refusing
branches that were never pushed, and handing the branch diff to the normal apply
guardrail.
"""

from __future__ import annotations

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

        result = _switch(tmp_path, new_name="dev2")

        assert switch_env["update"].call_args.kwargs["rename_to"] == "dev2"
        # Same as for a real switch: one clone-env.sh run, owned by the caller.
        assert switch_env["update"].call_args.kwargs["sync_agent_checkout"] is False
        assert result["branch_switched"] is False
        assert result["env_name"] == "dev2"
        assert result["renamed_from"] == "dev1"
        # The pull that stands in for the switch runs first, under the old name,
        # so a strict refusal can still leave the name untouched.
        assert switch_env["pull"].call_args[0][2] == "dev1"
        assert switch_env["agent"].call_args[0][3] == "dev2"

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
