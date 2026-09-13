"""Production → dev data flow: publish a production as a template, and create
an environment straight from a production.

Two layers are covered here:

* ``system_ops.publish_production_as_template`` — the plumbing that dumps the
  production database out of the production cluster, restores it into the dev
  cluster and snapshots the production filestore. Docker/PostgreSQL access is
  mocked at the ``oduflow.docker_ops.*`` seam, never at the Docker SDK.
* the MCP surface — ``save_production_as_template``,
  ``create_environment(from_production=...)`` and the ``allow_copy_to_dev_mcp``
  flag that gates both for agents (never for the dashboard).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

import docker
from oduflow import production_registry
from oduflow.docker_ops import production_ops, system_ops
from oduflow.errors import ConflictError, ExternalCommandError, NotFoundError
from oduflow.naming import get_template_db_name
from oduflow.settings import Settings, TeamSettings

PROD = "erp"
TPL = "prod-erp"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", hostname="dev.example.com", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(
        routing_mode="traefik",
        acme_email="a@b.co",
        base_data_dir=str(tmp_path),
        etc_dir=str(tmp_path / "etc"),
        prod_enabled=True,
        teams={"1": team},
    )


def _record(team, **overrides):
    """Create a production registry record with sane defaults."""
    values = {
        "domain": "erp.example.com",
        "repo_url": "https://github.com/acme/erp.git",
        "branch": "production",
        "odoo_image": "odoo:18.0",
        "git_user": "acme-bot",
        "extra_addons": {"oca_web": "18.0"},
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    values.update(overrides)
    return production_registry.create_production(team, PROD, values)


def _drop_copy_flag(team):
    """Rewrite the registry as a pre-flag deployment would have left it."""
    path = production_registry.registry_path(team)
    with open(path) as f:
        state = json.load(f)
    state["productions"][PROD].pop("allow_copy_to_dev_mcp", None)
    with open(path, "w") as f:
        json.dump(state, f)


def _db_map(existing):
    """A ``_db_exists`` stand-in that reports only the names in *existing*."""

    def _fn(client, settings, db_name, container_name=None):
        return db_name in existing

    return _fn


class _Remount:
    affected: list[str] = []
    failures: list[tuple[str, str]] = []


@contextmanager
def _fake_remount(*args, **kwargs):
    yield _Remount()


def _prod_container(exit_code=0, output=b""):
    container = MagicMock()
    container.exec_run.return_value = (exit_code, output)
    return container


def _publish_patches(stack, client, existing_dbs, *, reload_side_effect=None):
    """Patch everything publish_production_as_template touches around the logic.

    Returns the dict of started mocks.
    """
    from contextlib import ExitStack

    assert isinstance(stack, ExitStack)
    mocks = {}
    mocks["get_client"] = stack.enter_context(
        patch.object(system_ops, "get_client", return_value=client)
    )
    mocks["_db_exists"] = stack.enter_context(
        patch.object(system_ops, "_db_exists", side_effect=_db_map(existing_dbs))
    )
    mocks["check_db_quota"] = stack.enter_context(
        patch.object(system_ops, "check_db_quota")
    )
    mocks["_wait_pg_ready"] = stack.enter_context(
        patch.object(system_ops, "_wait_pg_ready")
    )
    mocks["_stream_exec_to_file"] = stack.enter_context(
        patch.object(
            system_ops,
            "_stream_exec_to_file",
            side_effect=lambda client, container, cmd, path, *, tool: open(
                path, "wb"
            ).write(b"dump"),
        )
    )
    mocks["reload_template"] = stack.enter_context(
        patch.object(system_ops, "reload_template", side_effect=reload_side_effect)
    )
    mocks["remount"] = stack.enter_context(
        patch(
            "oduflow.docker_ops.env_ops.remount_template_overlays",
            side_effect=_fake_remount,
        )
    )
    mocks["get_odoo_uid_gid"] = stack.enter_context(
        patch.object(system_ops, "get_odoo_uid_gid", return_value="100:101")
    )
    mocks["_chown_filestore"] = stack.enter_context(
        patch.object(system_ops, "_chown_filestore")
    )
    mocks["_baselines_owned_by"] = stack.enter_context(
        patch.object(system_ops, "_baselines_owned_by", return_value=True)
    )
    return mocks


# =============================================================================
# system_ops.publish_production_as_template
# =============================================================================


class TestPublishProductionAsTemplate:
    def test_unknown_production_raises(self, settings, team):
        with patch.object(system_ops, "get_client", return_value=MagicMock()):
            with pytest.raises(NotFoundError, match="ghost"):
                system_ops.publish_production_as_template(
                    settings, team, "ghost", "tpl"
                )

    def test_missing_production_database_raises(self, settings, team):
        _record(team)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_db_exists", side_effect=_db_map(set())),
        ):
            with pytest.raises(NotFoundError, match="production cluster"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

    def test_existing_template_dir_refuses_without_overwrite(self, settings, team):
        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_db_exists", side_effect=_db_map({prod_db})),
        ):
            with pytest.raises(ConflictError, match="already exists"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

    def test_existing_template_db_refuses_without_overwrite(self, settings, team):
        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        tpl_db = get_template_db_name(TPL, team.team_id)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(
                system_ops, "_db_exists", side_effect=_db_map({prod_db, tpl_db})
            ),
        ):
            with pytest.raises(ConflictError, match="already exists"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

    def test_metadata_comes_from_the_registry_record(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            _publish_patches(stack, client, {prod_db})
            stack.enter_context(
                patch("oduflow.git_ops.is_git_repository", return_value=True)
            )
            stack.enter_context(
                patch("oduflow.git_ops.rev_parse", return_value="c0ffee")
            )
            result = system_ops.publish_production_as_template(
                settings, team, PROD, TPL
            )

        assert result["status"] == "promoted"
        assert result["prod_name"] == PROD
        assert result["template_db"] == get_template_db_name(TPL, team.team_id)

        with open(team.get_template_metadata_path(TPL)) as f:
            metadata = json.load(f)
        assert metadata["repo_url"] == "https://github.com/acme/erp.git"
        assert metadata["odoo_image"] == "odoo:18.0"
        assert metadata["git_user"] == "acme-bot"
        assert metadata["extra_addons"] == {"oca_web": "18.0"}
        # Provenance: the production's branch and its checkout's HEAD.
        assert metadata["source_branch"] == "production"
        assert metadata["source_commit"] == "c0ffee"
        assert metadata["snapshot_at"]
        # An empty filestore stays under the overlay threshold.
        assert metadata["use_overlay"] is False
        # The dump is installed under its final name only after the restore.
        assert os.path.isfile(os.path.join(team.get_template_dir(TPL), "dump.pgdump"))

    def test_missing_checkout_only_records_the_branch(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            _publish_patches(stack, client, {prod_db})
            system_ops.publish_production_as_template(settings, team, PROD, TPL)

        with open(team.get_template_metadata_path(TPL)) as f:
            metadata = json.load(f)
        assert metadata["source_branch"] == "production"
        assert "source_commit" not in metadata

    def test_overwrite_republishes_without_charging_the_quota(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        tpl_db = get_template_db_name(TPL, team.team_id)
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            mocks = _publish_patches(stack, client, {prod_db, tpl_db})
            system_ops.publish_production_as_template(
                settings, team, PROD, TPL, overwrite=True
            )

        mocks["check_db_quota"].assert_not_called()
        mocks["reload_template"].assert_called_once()
        assert mocks["reload_template"].call_args.kwargs["persist_dump"] is False

    def test_production_filestore_becomes_the_template_baseline(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        prod_filestore = production_ops.prod_filestore_dir(team, PROD)
        os.makedirs(os.path.join(prod_filestore, "60"), exist_ok=True)
        with open(os.path.join(prod_filestore, "60", "abc"), "w") as f:
            f.write("attachment")
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        def fake_snapshot(src, dst, link_dests=None):
            os.makedirs(os.path.join(dst, "60"), exist_ok=True)
            with open(os.path.join(dst, "60", "abc"), "w") as fh:
                fh.write("attachment")
            return []

        with ExitStack() as stack:
            mocks = _publish_patches(stack, client, {prod_db})
            snapshot = stack.enter_context(
                patch.object(
                    system_ops, "_snapshot_filestore", side_effect=fake_snapshot
                )
            )
            system_ops.publish_production_as_template(settings, team, PROD, TPL)

        snapshot.assert_called_once()
        baseline = team.get_template_filestore_path(TPL)
        assert os.path.isfile(os.path.join(baseline, "60", "abc"))
        # The production stays a plain directory; only the copy is chowned.
        assert os.path.isfile(os.path.join(prod_filestore, "60", "abc"))
        mocks["_chown_filestore"].assert_called_once()

    def test_dump_is_streamed_out_of_the_production_cluster(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        prod_container = _prod_container()
        client = MagicMock()
        client.containers.get.return_value = prod_container

        with ExitStack() as stack:
            mocks = _publish_patches(stack, client, {prod_db})
            system_ops.publish_production_as_template(settings, team, PROD, TPL)

        # Nothing is ever written into the production container's writable
        # layer: pg_dump's stdout goes straight to the staging file.
        client.containers.get.assert_any_call(settings.prod_db_container)
        stream = mocks["_stream_exec_to_file"]
        stream.assert_called_once()
        assert stream.call_args.args[1] is prod_container
        cmd = stream.call_args.args[2]
        assert cmd[:4] == ["pg_dump", "-U", settings.db_user, "-Fc"]
        assert cmd[-1] == prod_db
        assert not any(
            call.args and call.args[0][0] == "pg_dump"
            for call in prod_container.exec_run.call_args_list
        )
        # The staged dump was installed as the template's dump, not copied.
        assert os.path.isfile(os.path.join(team.get_template_dir(TPL), "dump.pgdump"))
        assert not os.path.exists(
            mocks["reload_template"].call_args.kwargs["dump_path"]
        )

    def test_failed_dump_leaves_no_template_directory(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            mocks = _publish_patches(stack, client, {prod_db})
            mocks["_stream_exec_to_file"].side_effect = ExternalCommandError(
                "pg_dump", 1, "pg_dump: fatal"
            )
            with pytest.raises(ExternalCommandError, match="pg_dump"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

        mocks["reload_template"].assert_not_called()
        # Nothing half-published is left behind — not even the directory, which
        # readers would otherwise take for a template.
        assert not os.path.exists(team.get_template_dir(TPL))

    def test_failed_restore_leaves_no_template_directory(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            _publish_patches(
                stack,
                client,
                {prod_db},
                reload_side_effect=ExternalCommandError("pg_restore", 1, "boom"),
            )
            with pytest.raises(ExternalCommandError, match="pg_restore"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

        assert not os.path.exists(team.get_template_dir(TPL))

    def test_failed_republish_keeps_the_existing_template_directory(
        self, settings, team
    ):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        tpl_db = get_template_db_name(TPL, team.team_id)
        template_dir = team.get_template_dir(TPL)
        os.makedirs(template_dir, exist_ok=True)
        with open(os.path.join(template_dir, "dump.pgdump"), "wb") as f:
            f.write(b"previous")
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            _publish_patches(
                stack,
                client,
                {prod_db, tpl_db},
                reload_side_effect=ExternalCommandError("pg_restore", 1, "boom"),
            )
            with pytest.raises(ExternalCommandError, match="pg_restore"):
                system_ops.publish_production_as_template(
                    settings, team, PROD, TPL, overwrite=True
                )

        # A re-baseline that failed leaves the previous template in place.
        with open(os.path.join(template_dir, "dump.pgdump"), "rb") as f:
            assert f.read() == b"previous"

    def test_missing_production_cluster_is_a_prerequisite_error(self, settings, team):
        from oduflow.errors import PrerequisiteNotMetError

        _record(team)
        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nf")
        with patch.object(system_ops, "get_client", return_value=client):
            with pytest.raises(PrerequisiteNotMetError, match="not initialized"):
                system_ops.publish_production_as_template(settings, team, PROD, TPL)

    def test_metadata_marks_the_template_as_production_derived(self, settings, team):
        from contextlib import ExitStack

        _record(team)
        prod_db = production_ops.prod_db_name(team, PROD)
        client = MagicMock()
        client.containers.get.return_value = _prod_container()

        with ExitStack() as stack:
            _publish_patches(stack, client, {prod_db})
            system_ops.publish_production_as_template(settings, team, PROD, TPL)

        with open(team.get_template_metadata_path(TPL)) as f:
            assert json.load(f)["source_production"] == PROD


class TestTemplateIsReady:
    def test_needs_metadata_and_the_template_database(self, settings, team):
        tpl_db = get_template_db_name(TPL, team.team_id)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_db_exists", side_effect=_db_map({tpl_db})),
        ):
            # A bare directory — what a failed publish leaves — is not ready.
            os.makedirs(team.get_template_dir(TPL), exist_ok=True)
            assert system_ops.template_is_ready(settings, team, TPL) is False
            with open(team.get_template_metadata_path(TPL), "w") as f:
                json.dump({"odoo_image": "odoo:18.0"}, f)
            assert system_ops.template_is_ready(settings, team, TPL) is True

    def test_a_stale_database_without_metadata_is_not_ready(self, settings, team):
        tpl_db = get_template_db_name(TPL, team.team_id)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_db_exists", side_effect=_db_map({tpl_db})),
        ):
            assert system_ops.template_is_ready(settings, team, TPL) is False

    def test_metadata_without_the_database_is_not_ready(self, settings, team):
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        with open(team.get_template_metadata_path(TPL), "w") as f:
            json.dump({"odoo_image": "odoo:18.0"}, f)
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_db_exists", side_effect=_db_map(set())),
        ):
            assert system_ops.template_is_ready(settings, team, TPL) is False


# =============================================================================
# MCP layer
# =============================================================================


def _call_tool_fn(tool_name: str, **kwargs):
    """Invoke a registered MCP tool that has its own ``name`` parameter."""
    import asyncio
    import inspect

    from oduflow.server import mcp as mcp_server

    result = mcp_server._tool_manager._tools[tool_name].fn(**kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


@pytest.fixture
def mcp(settings, team):
    """Point the server module at this test's settings and team."""
    import oduflow.server

    oduflow.server._settings = settings
    with patch("oduflow.server._resolve_team", return_value=team):
        from tool_helpers import call_tool

        yield call_tool
    oduflow.server._settings = None


class TestSaveProductionAsTemplateTool:
    def test_happy_path_reports_the_published_template(self, mcp, team, settings):
        _record(team)
        result_payload = {
            "status": "promoted",
            "prod_name": PROD,
            "dump": "/data/templates/prod-erp/dump.pgdump",
            "filestore": "/data/templates/prod-erp/filestore",
            "template_db": "oduflow_tpl_1_prod-erp",
            "affected_envs": [],
            "remount_failures": [],
        }
        with patch.object(
            system_ops, "publish_production_as_template", return_value=result_payload
        ) as publish:
            result = mcp(
                "save_production_as_template", prod_name=PROD, template_name=TPL
            )

        assert publish.call_args.kwargs["template_name"] == TPL
        assert publish.call_args.kwargs["overwrite"] is False
        assert f"Production '{PROD}' saved as template '{TPL}'." in result
        assert "Template DB: oduflow_tpl_1_prod-erp" in result
        assert "UNSANITIZED production data" in result
        assert "No other environments were affected." in result

    def test_affected_environments_are_listed(self, mcp, team):
        _record(team)
        payload = {
            "status": "promoted",
            "prod_name": PROD,
            "dump": "d",
            "filestore": "f",
            "template_db": "t",
            "affected_envs": ["dev1", "dev2"],
            "remount_failures": [("dev3", "busy")],
        }
        with patch.object(
            system_ops, "publish_production_as_template", return_value=payload
        ):
            result = mcp(
                "save_production_as_template",
                prod_name=PROD,
                template_name=TPL,
                reset_env_changes=True,
            )

        assert "Reset filestore overlays for: dev1, dev2" in result
        assert "dev3: busy" in result

    def test_refused_when_copy_to_dev_is_disabled(self, mcp, team):
        _record(team, allow_copy_to_dev_mcp=False)
        with patch.object(system_ops, "publish_production_as_template") as publish:
            with pytest.raises(ToolError, match="disabled"):
                mcp("save_production_as_template", prod_name=PROD, template_name=TPL)
        publish.assert_not_called()

    def test_a_record_without_the_flag_is_allowed(self, mcp, team):
        # Productions created before the flag existed keep working.
        _record(team)
        _drop_copy_flag(team)

        payload = {
            "status": "promoted",
            "prod_name": PROD,
            "dump": "d",
            "filestore": "f",
            "template_db": "t",
            "affected_envs": [],
            "remount_failures": [],
        }
        with patch.object(
            system_ops, "publish_production_as_template", return_value=payload
        ) as publish:
            mcp("save_production_as_template", prod_name=PROD, template_name=TPL)
        publish.assert_called_once()

    def test_holds_the_production_key_while_publishing(self, mcp, team):
        import oduflow.server

        _record(team)
        key = oduflow.server.prod_lock_key(team.team_id, PROD)
        held_during_publish = []

        def _publish(*args, **kwargs):
            try:
                oduflow.server._locks.acquire_env(key)
            except Exception:  # BusyError: the key is taken, as it should be
                held_during_publish.append(True)
            else:
                oduflow.server._locks.release_env(key)
                held_during_publish.append(False)
            return {
                "status": "promoted",
                "prod_name": PROD,
                "dump": "d",
                "filestore": "f",
                "template_db": "t",
                "affected_envs": [],
                "remount_failures": [],
            }

        with patch.object(
            system_ops, "publish_production_as_template", side_effect=_publish
        ):
            mcp("save_production_as_template", prod_name=PROD, template_name=TPL)

        assert held_during_publish == [True]
        # ... and released afterwards.
        oduflow.server._locks.acquire_env(key)
        oduflow.server._locks.release_env(key)

    def test_rejected_when_production_hosting_is_disabled(self, mcp, team, settings):
        import oduflow.server

        oduflow.server._settings = replace(settings, prod_enabled=False)
        _record(team)
        with pytest.raises(ToolError, match="Production hosting is disabled"):
            mcp("save_production_as_template", prod_name=PROD, template_name=TPL)


class TestCreateEnvironmentFromProduction:
    ENV_RESULT = {
        "url": "http://localhost:50000",
        "hostname": "dev1",
        "odoo_container": "oduflow-1-dev-odoo",
        "database": "oduflow_1_dev",
        "workspace": "/tmp/ws",
    }

    @staticmethod
    def _env_patches(stack, *, template_ready=False):
        from contextlib import ExitStack

        assert isinstance(stack, ExitStack)
        stack.enter_context(
            patch(
                "oduflow.docker_ops.env_ops.adopt_existing_environment",
                return_value=None,
            )
        )
        stack.enter_context(
            patch.object(system_ops, "template_is_ready", return_value=template_ready)
        )
        return stack.enter_context(
            patch(
                "oduflow.docker_ops.env_ops.create_environment",
                return_value=TestCreateEnvironmentFromProduction.ENV_RESULT,
            )
        )

    @pytest.mark.parametrize("conflicting", ["template_name", "local_path"])
    def test_mutually_exclusive_with_other_code_sources(self, mcp, team, conflicting):
        _record(team)
        with pytest.raises(ToolError, match="cannot be combined"):
            mcp(
                "create_environment",
                branch="dev",
                from_production=PROD,
                **{conflicting: "something"},
            )

    def test_unknown_production_raises(self, mcp, team):
        from contextlib import ExitStack

        with ExitStack() as stack:
            self._env_patches(stack)
            with pytest.raises(ToolError, match="ghost"):
                mcp("create_environment", branch="dev", from_production="ghost")

    def test_refused_when_copy_to_dev_is_disabled(self, mcp, team):
        from contextlib import ExitStack

        _record(team, allow_copy_to_dev_mcp=False)
        with ExitStack() as stack:
            create = self._env_patches(stack)
            publish = stack.enter_context(
                patch.object(system_ops, "publish_production_as_template")
            )
            with pytest.raises(ToolError, match="disabled"):
                mcp("create_environment", branch="dev", from_production=PROD)
        publish.assert_not_called()
        create.assert_not_called()

    def test_publishes_the_managed_template_on_first_use(self, mcp, team):
        from contextlib import ExitStack

        _record(team)
        with ExitStack() as stack:
            create = self._env_patches(stack)
            publish = stack.enter_context(
                patch.object(system_ops, "publish_production_as_template")
            )
            result = mcp(
                "create_environment",
                branch="dev",
                from_production=PROD,
                repo_url="https://github.com/acme/erp.git",
                odoo_image="odoo:18.0",
            )

        publish.assert_called_once()
        args, kwargs = publish.call_args
        assert args[2:] == (PROD, TPL)
        assert kwargs["overwrite"] is True
        assert create.call_args.kwargs["template_name"] == TPL
        assert f"Source production: {PROD}" in result
        assert f"Published production '{PROD}' as template '{TPL}'" in result
        assert "sanitized on creation" in result

    def test_existing_template_is_reused_with_a_refresh_hint(self, mcp, team):
        from contextlib import ExitStack

        _record(team)
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        with open(team.get_template_metadata_path(TPL), "w") as f:
            json.dump(
                {
                    "repo_url": "https://github.com/acme/erp.git",
                    "odoo_image": "odoo:18.0",
                    "snapshot_at": "2026-09-10T10:00:00+00:00",
                },
                f,
            )

        with ExitStack() as stack:
            create = self._env_patches(stack, template_ready=True)
            publish = stack.enter_context(
                patch.object(system_ops, "publish_production_as_template")
            )
            result = mcp("create_environment", branch="dev", from_production=PROD)

        publish.assert_not_called()
        # Code source and image come from the managed template's metadata.
        assert create.call_args.kwargs["template_name"] == TPL
        assert create.call_args.args[4] == "odoo:18.0"
        assert f"Reused the existing template '{TPL}'" in result
        assert "snapshot taken 2026-09-10T10:00:00+00:00" in result
        assert (
            f"save_production_as_template('{PROD}', '{TPL}', overwrite=True)" in result
        )

    def test_a_half_published_template_is_republished(self, mcp, team):
        from contextlib import ExitStack

        _record(team)
        # What a failed first publish used to leave behind: a directory (even
        # with metadata) but no template database. Directory existence must not
        # count as "published".
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        with ExitStack() as stack:
            self._env_patches(stack, template_ready=False)
            publish = stack.enter_context(
                patch.object(system_ops, "publish_production_as_template")
            )
            result = mcp(
                "create_environment",
                branch="dev",
                from_production=PROD,
                repo_url="https://github.com/acme/erp.git",
                odoo_image="odoo:18.0",
            )

        publish.assert_called_once()
        assert publish.call_args.kwargs["overwrite"] is True
        assert f"Published production '{PROD}' as template '{TPL}'" in result

    def test_existing_environment_reports_the_production_template_mismatch(
        self, mcp, team
    ):
        _record(team)
        existing = {
            "env_name": "dev",
            "url": "http://localhost:50000",
            "git_branch": "dev",
            "hostname": "dev",
            "odoo_container": "c",
            "database": "d",
            "workspace": "w",
            "odoo_image": "odoo:18.0",
            "template_name": "staging",
        }
        with (
            patch(
                "oduflow.docker_ops.env_ops.adopt_existing_environment",
                return_value=existing,
            ),
            patch.object(system_ops, "publish_production_as_template") as publish,
        ):
            result = mcp("create_environment", branch="dev", from_production=PROD)

        publish.assert_not_called()
        assert f"you asked for template '{TPL}'" in result
        assert "created from 'staging'" in result

    def test_unsanitized_use_of_a_production_template_is_gated(self, mcp, team):
        from contextlib import ExitStack

        _record(team, allow_copy_to_dev_mcp=False)
        os.makedirs(team.get_template_dir(TPL), exist_ok=True)
        with open(team.get_template_metadata_path(TPL), "w") as f:
            json.dump(
                {
                    "repo_url": "https://github.com/acme/erp.git",
                    "odoo_image": "odoo:18.0",
                    "source_production": PROD,
                },
                f,
            )

        # Sanitized use of the already-published template is fine ...
        with ExitStack() as stack:
            create = self._env_patches(stack, template_ready=True)
            mcp("create_environment", branch="dev", template_name=TPL)
        assert create.call_args.kwargs["sanitize"] is True

        # ... skipping sanitization is exactly what the flag refuses.
        with ExitStack() as stack:
            create = self._env_patches(stack, template_ready=True)
            with pytest.raises(ToolError, match="sanitize=True"):
                mcp(
                    "create_environment",
                    branch="dev",
                    template_name=TPL,
                    sanitize=False,
                )
        create.assert_not_called()

    def test_the_production_lock_is_released_after_publishing(self, mcp, team):
        from contextlib import ExitStack

        import oduflow.server

        _record(team)
        with ExitStack() as stack:
            self._env_patches(stack)
            stack.enter_context(
                patch.object(system_ops, "publish_production_as_template")
            )
            mcp(
                "create_environment",
                branch="dev",
                from_production=PROD,
                repo_url="https://github.com/acme/erp.git",
                odoo_image="odoo:18.0",
            )

        key = oduflow.server.prod_lock_key(team.team_id, PROD)
        oduflow.server._locks.acquire_env(key)
        oduflow.server._locks.release_env(key)


# =============================================================================
# The allow_copy_to_dev_mcp flag on the production record
# =============================================================================


class TestAllowCopyToDevFlag:
    def test_create_production_stores_the_flag(self, settings, team):
        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nf")
        real_create = production_registry.create_production
        spy = MagicMock(side_effect=real_create)

        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "ensure_prod_infra"),
            patch.object(production_ops, "_db_exists", return_value=False),
            patch("oduflow.production_registry.create_production", spy),
            patch.object(
                production_ops,
                "ensure_team_network",
                side_effect=RuntimeError("stop here"),
            ),
            patch.object(production_ops, "_cleanup_partial_production"),
        ):
            with pytest.raises(RuntimeError, match="stop here"):
                production_ops.create_production(
                    settings,
                    team,
                    PROD,
                    "https://github.com/acme/erp.git",
                    "production",
                    "erp.example.com",
                    "odoo:18.0",
                    allow_copy_to_dev_mcp=False,
                )

        assert spy.call_args.args[2]["allow_copy_to_dev_mcp"] is False

    def test_the_mcp_tool_passes_the_flag_through(self, mcp, team):
        with patch.object(
            production_ops,
            "create_production",
            return_value={
                "name": PROD,
                "url": "https://erp.example.com",
                "elapsed_seconds": 12,
                "database": "oduflow_1_prod-erp",
                "commit": "c0ffee1234",
                "odoo_container": "oduflow-1-prod-erp-odoo",
            },
        ) as create:
            # Called directly: this tool has its own `name` parameter.
            _call_tool_fn(
                "create_production",
                name=PROD,
                repo_url="https://github.com/acme/erp.git",
                branch="production",
                domain="erp.example.com",
                odoo_image="odoo:18.0",
                allow_copy_to_dev_mcp=False,
            )
        assert create.call_args.kwargs["allow_copy_to_dev_mcp"] is False

    def test_no_mcp_tool_can_toggle_the_flag(self):
        """The flag is deliberately write-once over MCP: only the dashboard
        (an administrator) can re-enable or disable it afterwards."""
        from oduflow.server import mcp as mcp_server

        setters = [
            name
            for name, tool in mcp_server._tool_manager._tools.items()
            if "allow_copy_to_dev_mcp" in (tool.fn.__doc__ or "")
            and name != "create_production"
        ]
        assert setters == []

    def test_list_and_info_surface_the_flag(self, settings, team):
        _record(team, allow_copy_to_dev_mcp=False)
        client = MagicMock()
        with patch.object(production_ops, "get_client", return_value=client):
            listed = production_ops.list_productions(settings, team)
            info = production_ops.get_production_info(settings, team, PROD)
        assert listed[0]["allow_copy_to_dev_mcp"] is False
        assert info["allow_copy_to_dev_mcp"] is False

    def test_a_record_without_the_flag_reads_as_allowed(self, settings, team):
        _record(team)
        _drop_copy_flag(team)

        client = MagicMock()
        with patch.object(production_ops, "get_client", return_value=client):
            listed = production_ops.list_productions(settings, team)
            info = production_ops.get_production_info(settings, team, PROD)
        assert listed[0]["allow_copy_to_dev_mcp"] is True
        assert info["allow_copy_to_dev_mcp"] is True
