import dataclasses
import json
import logging
import os
import re
import tarfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from unittest.mock import call as mock_call
from urllib.parse import urlsplit

import pytest

import docker
from oduflow import agent_uids, po_tools
from oduflow.docker_ops import env_ops, odoo_ops, system_ops
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.settings import DEFAULT_AGENT_IMAGE, Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

# These tests exercise environment/Odoo lifecycle, not the agent; TEST_TEAM
# keeps the default agent_enabled=False so the team agent container stays out
# of it (agent behaviour has its own tests below).
TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    etc_dir="/tmp/flow-test/etc",
    teams={"1": TEST_TEAM},
)


@pytest.fixture(autouse=True)
def _isolated_hostname_registry(tmp_path, monkeypatch):
    # create_environment reserves an environment slot in the team's
    # hostnames.json before provisioning. Keep that registry per-test: the
    # shared /tmp data dir would carry reservations across runs, and several
    # tests below patch ``env_ops.os.makedirs``, which resolves to the shared
    # ``os`` module and so also disables the registry's directory creation.
    monkeypatch.setattr(
        env_ops,
        "_hostname_registry_path",
        lambda team: str(tmp_path / "hostnames.json"),
    )


@pytest.fixture(autouse=True)
def _no_db_quota(monkeypatch):
    # Quota enforcement is covered by tests/test_db_quota.py; here it would
    # only add a psql exec to every mocked create_environment call chain.
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.check_db_quota", lambda *a, **kw: None
    )
    # Disk admission is covered by tests/test_disk_space.py.
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.estimate_new_db_bytes", lambda *a, **kw: 0
    )
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.check_disk_space", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.pg_clone_strategy_clause", lambda *a, **kw: ""
    )
    # Tablespace provisioning is covered by tests/test_tablespaces.py.
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.ensure_team_tablespace",
        lambda *a, **kw: "oduflow_team_1",
    )


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.system_ops.get_client") as sys_mock,
        patch("oduflow.docker_ops.env_ops.get_client") as env_mock,
        patch("oduflow.docker_ops.odoo_ops.get_client") as odoo_mock,
    ):
        client_instance = MagicMock()
        sys_mock.return_value = client_instance
        env_mock.return_value = client_instance
        odoo_mock.return_value = client_instance
        yield client_instance


class TestInitSystem:
    @patch("oduflow.docker_ops.system_ops._reconcile_pg_hba")
    @patch("oduflow.docker_ops.system_ops._copy_file_to_container")
    def test_init_system_fresh(self, mock_copy, mock_hba, mock_docker_client):
        mock_docker_client.networks.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        mock_container = MagicMock()
        mock_docker_client.containers.run.return_value = mock_container

        def get_container(name):
            if name == "oduflow-db":
                c = MagicMock()
                c.status = "running"
                return c
            raise docker.errors.NotFound("nf")

        mock_docker_client.containers.get.side_effect = get_container
        # Readiness probes run detached and are polled (see _exec_exit_code).
        mock_docker_client.api.exec_create.return_value = {"Id": "exec-id"}
        mock_docker_client.api.exec_inspect.return_value = {
            "Running": False,
            "ExitCode": 0,
        }

        result = system_ops.init_system(TEST_SETTINGS)

        assert result["status"] == "initialized"
        # Shared infra network plus one isolated network per team.
        created = [c.args[0] for c in mock_docker_client.networks.create.call_args_list]
        assert created == ["oduflow-net", "oduflow-1-net"]
        mock_docker_client.volumes.create.assert_called_once()
        mock_hba.assert_called_once_with(
            mock_docker_client,
            TEST_SETTINGS,
            container_name=TEST_SETTINGS.shared_db_container,
        )

    @patch("oduflow.docker_ops.system_ops._reconcile_pg_hba")
    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
    def test_init_system_already_initialized(
        self, mock_pg, mock_db_exists, mock_hba, mock_docker_client
    ):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.volumes.get.return_value = MagicMock()
        db_container = MagicMock()
        db_container.status = "running"
        mock_docker_client.containers.get.return_value = db_container

        result = system_ops.init_system(TEST_SETTINGS)

        assert result["status"] == "initialized"
        mock_hba.assert_called_once_with(
            mock_docker_client,
            TEST_SETTINGS,
            container_name=TEST_SETTINGS.shared_db_container,
        )


class TestDestroySystem:
    def test_destroy_with_active_envs(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.branch": "main", "oduflow.managed": "true"}
        container.name = "oduflow-1-main-odoo"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments"):
            system_ops.destroy_system(TEST_SETTINGS)

    def test_destroy_clean(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        db = MagicMock()

        def _get_container(name):
            if name == TEST_SETTINGS.shared_db_container:
                return db
            raise docker.errors.NotFound("not found")

        mock_docker_client.containers.get.side_effect = _get_container
        vol = MagicMock()

        def _get_volume(name):
            if name == TEST_SETTINGS.shared_db_volume:
                return vol
            raise docker.errors.NotFound("not found")

        mock_docker_client.volumes.get.side_effect = _get_volume
        net = MagicMock()
        mock_docker_client.networks.get.return_value = net

        result = system_ops.destroy_system(TEST_SETTINGS)

        assert result["status"] == "destroyed"
        db.stop.assert_called_once()
        db.remove.assert_called_once()
        vol.remove.assert_called_once()
        net.remove.assert_called_once()

    def test_destroy_blocked_by_production(self, mock_docker_client):
        # Productions carry no branch/service label, so they are invisible to the
        # container scan; destroy must still refuse while any exist (P-H4).
        mock_docker_client.containers.list.return_value = []
        with (
            patch(
                "oduflow.production_registry.list_productions",
                return_value={"erp": {"name": "erp"}},
            ),
            pytest.raises(ConflictError, match="Active productions"),
        ):
            system_ops.destroy_system(TEST_SETTINGS)

    def test_destroy_blocked_by_unregistered_production_container(
        self, mock_docker_client
    ):
        # A failed delete_production can remove the registry record while the
        # container survives. The registry guard misses it, so destroy must
        # also refuse on live containers in the reserved prod- name namespace
        # (which dev environments cannot enter) — or the prod PG teardown
        # below would remove the database out from under it.
        stray = MagicMock()
        stray.labels = {"oduflow.managed": "true"}
        stray.name = "oduflow-1-prod-erp-odoo"
        mock_docker_client.containers.list.return_value = [stray]
        with (
            patch("oduflow.production_registry.list_productions", return_value={}),
            pytest.raises(ConflictError, match="oduflow-1-prod-erp-odoo"),
        ):
            system_ops.destroy_system(TEST_SETTINGS)

    def test_destroy_removes_production_pg(self, mock_docker_client):
        # The prod PG container + volume persist past the last production and
        # must be torn down (P-H4), or they leak and keep the shared network busy.
        mock_docker_client.containers.list.return_value = []
        prod_db = MagicMock()

        def _get_container(name):
            if name == TEST_SETTINGS.prod_db_container:
                return prod_db
            raise docker.errors.NotFound("not found")

        mock_docker_client.containers.get.side_effect = _get_container
        prod_vol = MagicMock()

        def _get_volume(name):
            if name == TEST_SETTINGS.prod_db_volume:
                return prod_vol
            raise docker.errors.NotFound("not found")

        mock_docker_client.volumes.get.side_effect = _get_volume
        mock_docker_client.networks.list.return_value = []
        mock_docker_client.networks.get.return_value = MagicMock()

        with patch("oduflow.production_registry.list_productions", return_value={}):
            result = system_ops.destroy_system(TEST_SETTINGS)

        prod_db.stop.assert_called_once()
        prod_db.remove.assert_called_once_with(v=True)
        prod_vol.remove.assert_called_once()
        assert TEST_SETTINGS.prod_db_container in result["removed"]
        assert TEST_SETTINGS.prod_db_volume in result["removed"]

    def test_destroy_survives_network_active_endpoints(self, mock_docker_client):
        # If an endpoint we do not manage still holds the shared network,
        # net.remove() raises APIError; destroy must log and finish, not crash
        # after tearing down half the system (P-H4).
        mock_docker_client.containers.list.return_value = []
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("x")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("x")
        mock_docker_client.networks.list.return_value = []
        net = MagicMock()
        net.remove.side_effect = docker.errors.APIError("has active endpoints")
        mock_docker_client.networks.get.return_value = net

        with patch("oduflow.production_registry.list_productions", return_value={}):
            result = system_ops.destroy_system(TEST_SETTINGS)

        assert result["status"] == "destroyed"
        assert TEST_SETTINGS.shared_network not in result["removed"]


class TestAdoptExistingEnvironment:
    """create_environment answers with the existing environment instead of an
    error, so an agent can call it first without listing environments."""

    @staticmethod
    def _container(status="running", branch="main"):
        container = MagicMock()
        container.status = status
        container.labels = {
            "oduflow.git_branch": branch,
            "oduflow.template": "myproject",
            TEST_SETTINGS.image_label: "odoo:17.0",
            TEST_SETTINGS.team_label: "1",
        }
        container.ports = {"8069/tcp": [{"HostPort": "50000"}]}
        container.attrs = {
            "NetworkSettings": {"Ports": {"8069/tcp": [{"HostPort": "50000"}]}}
        }
        return container

    def test_missing_environment_returns_none(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nope")
        assert (
            env_ops.adopt_existing_environment(TEST_SETTINGS, TEST_TEAM, "main") is None
        )

    def test_running_environment_is_described(self, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._container()

        result = env_ops.adopt_existing_environment(
            TEST_SETTINGS, TEST_TEAM, "main", branch="main"
        )

        assert result is not None
        assert result["url"] == "http://localhost:50000"
        assert result["git_branch"] == "main"
        assert result["odoo_image"] == "odoo:17.0"
        assert result["template_name"] == "myproject"
        assert result["started"] is False

    @patch("oduflow.docker_ops.env_ops.start_environment")
    def test_stopped_environment_is_started(self, mock_start, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._container(
            status="exited"
        )

        result = env_ops.adopt_existing_environment(
            TEST_SETTINGS, TEST_TEAM, "main", branch="main"
        )

        mock_start.assert_called_once_with(TEST_SETTINGS, "main", TEST_TEAM)
        assert result is not None
        assert result["started"] is True

    def test_other_branch_is_refused(self, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._container(branch="main")

        with pytest.raises(ConflictError, match="switch_branch"):
            env_ops.adopt_existing_environment(
                TEST_SETTINGS, TEST_TEAM, "main", branch="feature/x"
            )

    def test_unreachable_docker_defers_to_the_creation_path(self):
        with patch(
            "oduflow.docker_ops.env_ops.get_client",
            side_effect=PrerequisiteNotMetError("no docker"),
        ):
            assert (
                env_ops.adopt_existing_environment(TEST_SETTINGS, TEST_TEAM, "main")
                is None
            )


class TestCreateEnvironment:
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-payments/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-payments", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50000)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_odoo.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.create.return_value.wait.return_value = {
            "StatusCode": 0
        }
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/payments",
            "https://github.com/org/repo.git",
            "odoo:15.0",
        )

        assert result["url"] == "http://localhost:50000"
        assert result["database"] == "oduflow_1_feature-payments"
        assert result["odoo_container"] == "oduflow-1-feature-payments-odoo"
        assert mock_sql.call_count == 2
        mock_creds.assert_called_once()
        mock_role.assert_called_once()
        # Greenfield: an isolated init container (created, so it can receive
        # the generated conf before starting) + the serving container.
        mock_docker_client.containers.create.assert_called_once()
        assert mock_docker_client.containers.run.call_count == 1
        for call in (
            mock_docker_client.containers.create.call_args,
            mock_docker_client.containers.run.call_args,
        ):
            assert call.kwargs["extra_hosts"] == {
                "host.docker.internal": "host-gateway"
            }
        mock_alloc.assert_called_once()

    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._cleanup_old_environment")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-payments/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-payments", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops.reassign_db_ownership")
    @patch("oduflow.docker_ops.env_ops.drop_signaling_sequences")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50000)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_rolls_back_on_run_failure(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_drop_seq,
        mock_reassign,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_cleanup,
        mock_release_port,
        mock_docker_client,
    ):
        # Reach the serving containers.run, then fail it (e.g. bad image / port
        # bind) and assert the partially-created resources are rolled back (#49).
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.side_effect = RuntimeError("bind failed")

        with pytest.raises(RuntimeError, match="bind failed"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "feature/payments",
                "https://github.com/org/repo.git",
                "odoo:15.0",
                template_name="base",
            )

        # Rollback: the allocated port is released and the environment teardown
        # runs (in addition to the pre-create cleanup at the top of the function).
        mock_release_port.assert_called_once_with(
            TEST_TEAM.port_registry_path, "feature/payments"
        )
        assert mock_cleanup.call_count == 2

    @staticmethod
    def _template_create_patches(configure, exec_sql=None):
        """Patches that carry a template create_environment up to the setup
        step, which *configure* replaces."""
        return [
            patch("oduflow.docker_ops.env_ops._db_exists", return_value=True),
            patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
            patch("oduflow.docker_ops.env_ops.ensure_team_network"),
            patch("oduflow.docker_ops.env_ops._cleanup_old_environment"),
            patch("oduflow.docker_ops.env_ops._write_local_snapshot"),
            patch(
                "oduflow.docker_ops.env_ops._template_code_lineage",
                return_value={},
            ),
            patch("oduflow.docker_ops.env_ops._exec_sql", side_effect=exec_sql),
            patch(
                "oduflow.docker_ops.env_ops.create_credentials",
                return_value={"pg_user": "u_1_feature", "pg_password": "pw"},
            ),
            patch("oduflow.docker_ops.env_ops._create_pg_role"),
            patch("oduflow.docker_ops.env_ops.reassign_db_ownership"),
            patch("oduflow.docker_ops.env_ops.drop_signaling_sequences"),
            patch("oduflow.docker_ops.env_ops._mount_filestore"),
            patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set()),
            patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50000),
            patch("oduflow.docker_ops.env_ops.os.makedirs"),
            patch("oduflow.docker_ops.env_ops.os.chmod"),
            patch(
                "oduflow.docker_ops.env_ops.get_odoo_uid_gid",
                return_value="100:101",
            ),
            patch("oduflow.extra_addons.generate_odoo_conf"),
            # What the bundled conf generates for a repo without addons/.
            patch(
                "oduflow.module_versions.addons_path_from_conf",
                return_value=["/mnt/extra-addons"],
            ),
            configure,
        ]

    def test_create_rolls_back_on_required_setup_failure(
        self, mock_docker_client, tmp_path
    ):
        container = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = []
        mock_docker_client.containers.run.return_value = container
        setup_error = PrerequisiteNotMetError("registry not ready")

        # Entered through an ExitStack rather than a parenthesized `with` group:
        # CPython 3.10 caps a function at 20 statically nested blocks, and the
        # patches needed to reach the serving container exceed it.
        provisioning_patches = self._template_create_patches(
            patch(
                "oduflow.docker_ops.env_ops._configure_serving_environment",
                side_effect=setup_error,
            )
        )

        with ExitStack() as stack:
            for provisioning_patch in provisioning_patches:
                stack.enter_context(provisioning_patch)
            mock_rollback = stack.enter_context(
                patch("oduflow.docker_ops.env_ops._rollback_partial_environment")
            )
            with pytest.raises(PrerequisiteNotMetError, match="registry not ready"):
                env_ops.create_environment(
                    TEST_SETTINGS,
                    TEST_TEAM,
                    "feature",
                    "",
                    "odoo:15.0",
                    template_name="base",
                    auto_install_modules=["red_border"],
                    local_path=str(tmp_path),
                )

        mock_rollback.assert_called_once_with(
            mock_docker_client, TEST_SETTINGS, TEST_TEAM, "feature"
        )

    def test_create_hands_modules_newer_than_template_to_setup(
        self, mock_docker_client, tmp_path
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = []
        mock_docker_client.containers.run.return_value = MagicMock()
        for name, version in (
            ("zipfit_delivery_email_rfq", "18.0.1.18.0"),
            ("unchanged", "18.0.1.0.0"),
        ):
            (tmp_path / name).mkdir()
            (tmp_path / name / "__manifest__.py").write_text(
                f"{{'version': '{version}'}}"
            )
        installed = {
            "base": "18.0.1.3",
            "zipfit_delivery_email_rfq": "18.0.1.17.0",
            "unchanged": "18.0.1.0.0",
        }
        queried_dbs = []

        def exec_sql(client, settings, sql, db="postgres", **kwargs):
            if "ir_module_module" in sql:
                queried_dbs.append(db)
                return json.dumps(installed)
            return ""

        with ExitStack() as stack:
            mocks = [
                stack.enter_context(provisioning_patch)
                for provisioning_patch in self._template_create_patches(
                    patch(
                        "oduflow.docker_ops.env_ops._configure_serving_environment",
                        side_effect=PrerequisiteNotMetError("stop here"),
                    ),
                    exec_sql=exec_sql,
                )
            ]
            stack.enter_context(
                patch("oduflow.docker_ops.env_ops._rollback_partial_environment")
            )
            with pytest.raises(PrerequisiteNotMetError, match="stop here"):
                env_ops.create_environment(
                    TEST_SETTINGS,
                    TEST_TEAM,
                    "feature",
                    "",
                    "odoo:18.0",
                    template_name="base",
                    local_path=str(tmp_path),
                )

        # The cloned environment database is compared, not the template.
        assert queried_dbs == ["oduflow_1_feature"]
        mock_configure = mocks[-1]
        assert mock_configure.call_args.args[-1] == {
            "zipfit_delivery_email_rfq": ("18.0.1.17.0", "18.0.1.18.0")
        }

    def test_create_rejects_invalid_auto_install_module_before_provisioning(
        self, mock_docker_client
    ):
        # An unusable module name must fail before the clone, template restore
        # and container start that the setup rollback would otherwise discard.
        with pytest.raises(ValueError, match="Invalid module name"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "feature",
                "https://github.com/org/repo.git",
                "odoo:15.0",
                template_name="base",
                auto_install_modules=["sale-management"],
            )

        mock_docker_client.containers.run.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._cleanup_old_environment")
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_refuses_db_name_collision(
        self, mock_ready, mock_cleanup, mock_docker_client
    ):
        # "Feature/Foo" normalises to the same DB as a running "feature-foo";
        # creation must be refused instead of dropping the live env's DB (#41).
        from oduflow.errors import ConflictError

        other = MagicMock()
        other.name = "oduflow-1-feature-foo-odoo"
        other.labels = {"oduflow.branch": "feature-foo"}
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = [other]

        with pytest.raises(ConflictError, match="normalise to the same database"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "Feature/Foo",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )
        # The destructive cleanup (which drops the DB) must not run.
        mock_cleanup.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_already_exists(
        self, mock_ready, mock_db_exists, mock_docker_client
    ):
        existing = MagicMock()
        existing.status = "running"
        existing.ports = {"8069/tcp": [{"HostPort": "50000"}]}
        mock_docker_client.containers.get.return_value = existing

        with pytest.raises(ConflictError, match="already exists"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )

    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_system_not_ready(
        self, mock_ready, mock_db_exists, mock_docker_client
    ):
        mock_ready.side_effect = PrerequisiteNotMetError(
            "flow-db not found. System not initialized. Restart Oduflow."
        )

        with pytest.raises(PrerequisiteNotMetError, match="not initialized"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )

    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-no-tpl/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-no-tpl", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50001)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_no_template(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_odoo.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.run.return_value = mock_odoo
        init_container = mock_docker_client.containers.create.return_value
        init_container.wait.return_value = {"StatusCode": 0}
        events = MagicMock()
        events.attach_mock(mock_copy_conf, "copy")
        events.attach_mock(init_container.start, "start")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/no-tpl",
            "https://github.com/org/repo.git",
            "odoo:15.0",
            template_name=None,
        )

        assert result["url"] == "http://localhost:50001"
        assert result["database"] == "oduflow_1_feature-no-tpl"
        # Should create empty DB (no TEMPLATE clause)
        create_db_call = mock_sql.call_args_list[0]
        assert "TEMPLATE" not in create_db_call[0][2]
        # Should NOT mount filestore
        mock_mount.assert_not_called()
        # Greenfield init runs `-i base` in a SEPARATE isolated container,
        # before the serving container — so the serving PID1 never races it.
        init_run = mock_docker_client.containers.create.call_args
        assert "-i base" in init_run.kwargs["command"]
        assert "--stop-after-init" in init_run.kwargs["command"]
        assert init_run.kwargs["extra_hosts"] == {
            "host.docker.internal": "host-gateway"
        }
        # The init reads the generated conf (repo .oduflow/odoo.conf, demo
        # data settings included), copied in before the container starts.
        conf = "/tmp/flow-test/workspaces/feature-no-tpl/odoo.conf"
        assert events.mock_calls[:2] == [
            mock_call.copy(
                init_container, conf, "/etc/odoo", archive_name="odoo.conf", mode=0o644
            ),
            mock_call.start(),
        ]
        assert mock_docker_client.containers.run.call_count == 1

    @patch("oduflow.docker_ops.env_ops._rollback_partial_environment")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-no-tpl/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-no-tpl", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50001)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_no_template_failed_init_rolls_back(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_rollback,
        mock_docker_client,
    ):
        # A non-zero `-i base` exit (e.g. a bad repo .oduflow/odoo.conf) leaves
        # an empty/partial database: creation must fail and roll back, not
        # start the serving container and report the environment created.
        init_container = mock_docker_client.containers.create.return_value
        init_container.wait.return_value = {"StatusCode": 2}
        init_container.logs.return_value = b"odoo: unrecognized option"
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(ExternalCommandError, match="-i base"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "feature/no-tpl",
                "https://github.com/org/repo.git",
                "odoo:15.0",
                template_name=None,
            )

        # The DB, role, port and workspace already exist at this point.
        mock_rollback.assert_called_once()
        # The serving container must never start on the broken database.
        mock_docker_client.containers.run.assert_not_called()

    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-local/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-local", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50002)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_local_path_skips_clone(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
        tmp_path,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_odoo.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.run.return_value = mock_odoo
        init_container = mock_docker_client.containers.create.return_value
        init_container.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        local_dir = str(tmp_path)
        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/local",
            "",  # no repo_url in live-mount mode
            "odoo:17.0",
            template_name=None,
            local_path=local_dir,
        )

        # No git clone was invoked.
        for call in mock_run.call_args_list:
            assert "clone" not in (call.args[0] if call.args else [])
            assert "init" not in (call.args[0] if call.args else [])
        # Result and container label point at the live-mount directory.
        abs_local = os.path.abspath(local_dir)
        assert result["local_path"] == abs_local
        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["labels"]["oduflow.local_path"] == abs_local
        # The local dir is bind-mounted to /mnt/extra-addons.
        assert run_kwargs["volumes"][abs_local]["bind"] == "/mnt/extra-addons"

    def test_create_local_path_rejects_when_disabled(self, tmp_path):
        settings = Settings(
            base_data_dir="/tmp/flow-test",
            db_user="odoo",
            db_password="odoo",
            etc_dir="/tmp/flow-test/etc",
            allow_local_path=False,
            teams={"1": TEST_TEAM},
        )

        with (
            patch("oduflow.docker_ops.env_ops.get_client") as mock_get_client,
            patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
            patch("oduflow.docker_ops.env_ops._cleanup_old_environment"),
        ):
            mock_get_client.return_value.containers.get.side_effect = (
                docker.errors.NotFound("nf")
            )
            with pytest.raises(PrerequisiteNotMetError, match="local_path.*disabled"):
                env_ops.create_environment(
                    settings,
                    TEST_TEAM,
                    "feature/local",
                    "",
                    "odoo:17.0",
                    template_name=None,
                    local_path=str(tmp_path),
                )


class TestReloadTemplate:
    @patch("oduflow.docker_ops.system_ops._update_template_sizes")
    @patch("oduflow.docker_ops.system_ops._copy_file_to_container")
    @patch(
        "oduflow.docker_ops.system_ops.ensure_team_tablespace",
        return_value="oduflow_team_1",
    )
    @patch("oduflow.docker_ops.system_ops._is_text_dump", return_value=False)
    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.system_ops._exec_sql", return_value="5")
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
    @patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_restore_uses_no_owner(
        self,
        mock_isfile,
        mock_wait,
        mock_sql,
        mock_db_exists,
        mock_text,
        mock_tablespace,
        mock_copy,
        mock_sizes,
        mock_docker_client,
    ):
        # Custom-format dumps must be restored with --no-owner so the template
        # is not pinned to the source env's per-env role; otherwise deleting the
        # source env leaves an undroppable orphan role. (--no-owner is honored
        # only at restore time for -Fc archives, not at pg_dump time.)
        db_container = MagicMock()
        db_container.exec_run.return_value = (0, b"")
        mock_docker_client.containers.get.return_value = db_container

        system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")
        system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")

        archive_names = [
            call.kwargs["archive_name"] for call in mock_copy.call_args_list
        ]
        assert len(set(archive_names)) == 2

        # exec_run is also used for the post-restore /tmp cleanup, so locate the
        # pg_restore invocation explicitly rather than assuming it is the last.
        restore_cmds = [
            call.args[0]
            for call in db_container.exec_run.call_args_list
            if "pg_restore" in " ".join(call.args[0])
        ]
        assert restore_cmds, "expected a pg_restore invocation"
        assert all("--no-owner" in " ".join(cmd) for cmd in restore_cmds)
        assert {cmd[-1] for cmd in restore_cmds} == {
            f"/tmp/{name}" for name in archive_names
        }

        # The dump copied into the container's /tmp must be removed afterwards so
        # the oduflow-db writable layer does not accumulate full-size dumps.
        cleanup_cmds = [
            call.args[0]
            for call in db_container.exec_run.call_args_list
            if call.args[0][:2] == ["rm", "-f"]
        ]
        assert {cmd[2] for cmd in cleanup_cmds} == {
            f"/tmp/{name}" for name in archive_names
        }

    @patch("oduflow.docker_ops.system_ops._update_template_sizes")
    @patch("oduflow.docker_ops.system_ops._copy_file_to_container")
    @patch(
        "oduflow.docker_ops.system_ops.ensure_team_tablespace",
        return_value="oduflow_team_1",
    )
    @patch("oduflow.docker_ops.system_ops._is_text_dump", return_value=False)
    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.system_ops._exec_sql", return_value="5")
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
    @patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_cleanup_when_initial_copy_fails(
        self,
        mock_isfile,
        mock_wait,
        mock_sql,
        mock_db_exists,
        mock_text,
        mock_tablespace,
        mock_copy,
        mock_sizes,
        mock_docker_client,
    ):
        db_container = MagicMock()
        mock_docker_client.containers.get.return_value = db_container
        mock_copy.side_effect = RuntimeError("upload failed")

        with pytest.raises(RuntimeError, match="upload failed"):
            system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")

        archive_name = mock_copy.call_args.kwargs["archive_name"]
        db_container.exec_run.assert_called_once_with(
            ["rm", "-f", f"/tmp/{archive_name}"]
        )

    def test_restore_retries_unsupported_archive_with_helper(self, mock_docker_client):
        db_container = MagicMock()

        # Route by command so the post-restore /tmp cleanup (extra `rm -f`
        # exec_run calls) does not exhaust a fixed side_effect list: only the
        # initial pg_restore fails with the unsupported-archive error.
        def _exec_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "pg_restore":
                return (
                    1,
                    b"pg_restore: error: unsupported version (1.16) in file header",
                )
            return (0, b"helper sql restored")

        db_container.exec_run.side_effect = _exec_run
        mock_docker_client.containers.get.return_value = db_container

        with (
            patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True),
            patch("oduflow.docker_ops.system_ops._wait_pg_ready"),
            patch("oduflow.docker_ops.system_ops._exec_sql", return_value="5") as sql,
            patch("oduflow.docker_ops.system_ops._db_exists", return_value=False),
            patch(
                "oduflow.docker_ops.system_ops.ensure_team_tablespace",
                return_value="oduflow_team_1",
            ),
            patch("oduflow.docker_ops.system_ops._is_text_dump", return_value=False),
            patch("oduflow.docker_ops.system_ops._copy_file_to_container") as copy_file,
            patch("oduflow.docker_ops.system_ops._update_template_sizes"),
            patch(
                "oduflow.docker_ops.system_ops._convert_custom_dump_to_sql_with_helper",
                return_value=(
                    0,
                    "converted",
                    "/tmp/flow-test/templates/mytpl/dump.sql",
                ),
            ) as helper,
        ):
            result = system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")

        assert result["status"] == "reloaded"
        helper.assert_called_once()
        assert copy_file.call_args_list[-1].args[1].endswith("dump.sql")
        psql_cmds = [
            call.args[0]
            for call in db_container.exec_run.call_args_list
            if call.args[0] and call.args[0][0] == "psql"
        ]
        assert psql_cmds, "expected a psql invocation via the helper path"
        assert any(
            'DROP DATABASE IF EXISTS "oduflow_template_1_mytpl"' in call.args[2]
            for call in sql.call_args_list
        )
        assert any(
            'CREATE DATABASE "oduflow_template_1_mytpl"' in call.args[2]
            for call in sql.call_args_list
        )

    def test_cleanup_when_helper_copy_fails(self, mock_docker_client):
        db_container = MagicMock()

        def _exec_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "pg_restore":
                return (
                    1,
                    b"pg_restore: error: unsupported version (1.16) in file header",
                )
            return (0, b"")

        db_container.exec_run.side_effect = _exec_run
        mock_docker_client.containers.get.return_value = db_container

        with (
            patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True),
            patch("oduflow.docker_ops.system_ops._wait_pg_ready"),
            patch("oduflow.docker_ops.system_ops._exec_sql", return_value="5"),
            patch("oduflow.docker_ops.system_ops._db_exists", return_value=False),
            patch(
                "oduflow.docker_ops.system_ops.ensure_team_tablespace",
                return_value="oduflow_team_1",
            ),
            patch("oduflow.docker_ops.system_ops._is_text_dump", return_value=False),
            patch(
                "oduflow.docker_ops.system_ops._copy_file_to_container",
                side_effect=[None, RuntimeError("helper upload failed")],
            ) as copy_file,
            patch("oduflow.docker_ops.system_ops._update_template_sizes"),
            patch(
                "oduflow.docker_ops.system_ops._convert_custom_dump_to_sql_with_helper",
                return_value=(
                    0,
                    "converted",
                    "/tmp/flow-test/templates/mytpl/dump.sql",
                ),
            ),
        ):
            with pytest.raises(RuntimeError, match="helper upload failed"):
                system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")

        archive_names = [
            call.kwargs["archive_name"] for call in copy_file.call_args_list
        ]
        cleanup_paths = {
            call.args[0][2]
            for call in db_container.exec_run.call_args_list
            if call.args[0][:2] == ["rm", "-f"]
        }
        assert cleanup_paths == {f"/tmp/{name}" for name in archive_names}


class TestLocalSnapshotBasics:
    def test_local_snapshot_detects_added_modified_deleted(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))

        modified = repo / "sale" / "models" / "sale.py"
        deleted = repo / "sale" / "views" / "old.xml"
        unchanged = repo / "sale" / "views" / "same.xml"
        modified.parent.mkdir(parents=True)
        deleted.parent.mkdir(parents=True)
        modified.write_text("old")
        deleted.write_text("<old/>")
        unchanged.write_text("<same/>")

        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = modified.stat()
        modified.write_text("new")
        os.utime(
            modified,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000),
        )
        deleted.unlink()
        added = repo / "sale" / "views" / "new.xml"
        added.write_text("<new/>")

        base_ref, changed = env_ops._detect_local_changes(str(repo), "env", team)

        assert base_ref is None
        assert changed == [
            "sale/models/sale.py",
            "sale/views/new.xml",
            "sale/views/old.xml",
        ]

    def test_local_snapshot_repeated_after_advance_is_clean(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        path = repo / "sale" / "models" / "sale.py"
        path.parent.mkdir(parents=True)
        path.write_text("old")

        env_ops._write_local_snapshot(str(repo), "env", team)
        old_stat = path.stat()
        path.write_text("new")
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/models/sale.py"
        ]
        env_ops._write_local_snapshot(str(repo), "env", team)
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == []


class TestPullEnvironmentLocalAndSharedExtraCheckouts:
    def test_replace_mount_sources_and_persist_revisions(self):
        volumes = {
            "/workspace/repo": {"bind": "/mnt/extra-addons", "mode": "rw"},
            "/workspace/extra/enterprise": {
                "bind": "/mnt/extra-addons-enterprise",
                "mode": "ro",
            },
        }
        labels = {"oduflow.extra_addons": '{"enterprise": "18.0"}'}

        env_ops._replace_extra_checkout_mounts(
            volumes,
            labels,
            {"enterprise": "/cache/enterprise/newsha"},
            {"enterprise": "newsha"},
        )

        assert "/workspace/extra/enterprise" not in volumes
        assert volumes["/cache/enterprise/newsha"] == {
            "bind": "/mnt/extra-addons-enterprise",
            "mode": "ro",
        }
        assert json.loads(labels["oduflow.extra_addons_revisions"]) == {
            "enterprise": "newsha"
        }

    def test_pull_lazily_migrates_legacy_mount_without_code_change(
        self, mock_docker_client, tmp_path
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        legacy = tmp_path / "team" / "workspaces" / "env" / "extra" / "enterprise"
        legacy.mkdir(parents=True)
        shared = tmp_path / "team" / "shared_extra_checkouts" / "enterprise" / "a"
        shared.mkdir(parents=True)

        container = MagicMock()
        container.labels = {
            "oduflow.git_branch": "main",
            "oduflow.extra_addons": json.dumps({"enterprise": "18.0"}),
        }
        container.attrs = {
            "HostConfig": {
                "Binds": [
                    f"{repo}:/mnt/extra-addons:rw",
                    f"{legacy}:/mnt/extra-addons-enterprise:ro",
                ]
            }
        }
        mock_docker_client.containers.get.return_value = container

        with (
            patch("oduflow.git_ops.pull_repo", return_value=("main-old", [])),
            patch("oduflow.extra_addons.checkout_revision", return_value="a" * 40),
            patch(
                "oduflow.extra_addons.ensure_shared_checkout",
                return_value={
                    "path": str(shared),
                    "revision": "a" * 40,
                    "changed_files": [],
                },
            ),
            patch(
                "oduflow.docker_ops.env_ops.update_environment", return_value={}
            ) as update,
            patch("oduflow.docker_ops.env_ops._cleanup_legacy_extra_worktrees"),
        ):
            result = env_ops.pull_environment(settings, team, "env")

        assert result["extra_addons_cache_migrated"] is True
        # install_dependencies is on: the recreate throws away the container's
        # pip --user packages, so they have to be reinstalled from the new
        # mount sources or the environment loses libraries it was running with.
        update.assert_called_once_with(
            settings,
            team,
            "env",
            extra_checkout_overrides={"enterprise": str(shared)},
            extra_revision_overrides={"enterprise": "a" * 40},
            pull_image=False,
            install_dependencies=True,
        )

    def test_revision_bump_reinstalls_dependencies_after_recreate(
        self, mock_docker_client, tmp_path
    ):
        """A pull that only advances an extra repo still recreates the
        container, so deps must be reinstalled even though no dependency
        descriptor is in the diff (dep_units is empty here)."""
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        old_checkout = tmp_path / "team" / "shared_extra_checkouts" / "enterprise" / "a"
        old_checkout.mkdir(parents=True)
        new_checkout = tmp_path / "team" / "shared_extra_checkouts" / "enterprise" / "b"
        new_checkout.mkdir(parents=True)

        container = MagicMock()
        container.labels = {
            "oduflow.git_branch": "main",
            "oduflow.extra_addons": json.dumps({"enterprise": "18.0"}),
            "oduflow.extra_addons_revisions": json.dumps({"enterprise": "a" * 40}),
        }
        container.attrs = {
            "HostConfig": {
                "Binds": [
                    f"{repo}:/mnt/extra-addons:rw",
                    f"{old_checkout}:/mnt/extra-addons-enterprise:ro",
                ]
            }
        }
        mock_docker_client.containers.get.return_value = container

        with (
            patch("oduflow.git_ops.pull_repo", return_value=("main-old", [])),
            patch(
                "oduflow.extra_addons.ensure_shared_checkout",
                return_value={
                    "path": str(new_checkout),
                    "revision": "b" * 40,
                    # Only a Python module changed: no requirements.txt, so
                    # _apply_actions gets an empty dep_units.
                    "changed_files": ["sale/models/sale.py"],
                },
            ),
            patch(
                "oduflow.docker_ops.env_ops.update_environment", return_value={}
            ) as update,
            patch("oduflow.docker_ops.env_ops._cleanup_legacy_extra_worktrees"),
            patch("oduflow.docker_ops.env_ops._apply_actions") as apply_actions,
        ):
            env_ops.pull_environment(settings, team, "env")

        assert update.call_args.kwargs["install_dependencies"] is True
        assert apply_actions.call_args.kwargs["dep_units"] == []

    def test_strict_guardrail_does_not_switch_running_mount(
        self, mock_docker_client, tmp_path
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        shared = tmp_path / "team" / "shared_extra_checkouts" / "enterprise" / "b"
        shared.mkdir(parents=True)

        container = MagicMock()
        container.labels = {
            "oduflow.git_branch": "main",
            "oduflow.extra_addons": json.dumps({"enterprise": "18.0"}),
            "oduflow.extra_addons_revisions": json.dumps({"enterprise": "a" * 40}),
        }
        container.attrs = {
            "HostConfig": {
                "Binds": [
                    f"{repo}:/mnt/extra-addons:rw",
                    f"/cache/enterprise/{'a' * 40}:/mnt/extra-addons-enterprise:ro",
                ]
            }
        }
        mock_docker_client.containers.get.return_value = container

        with (
            patch("oduflow.git_ops.pull_repo", return_value=("main-old", [])),
            patch(
                "oduflow.extra_addons.ensure_shared_checkout",
                return_value={
                    "path": str(shared),
                    "revision": "b" * 40,
                    "changed_files": ["sale/security/rules.xml"],
                },
            ),
            patch(
                "oduflow.git_analysis.merge_recommendations",
                return_value={
                    "action": "upgrade",
                    "modules_to_install": [],
                    "modules_to_upgrade": ["sale"],
                },
            ),
            patch(
                "oduflow.git_analysis.guardrail_warnings",
                return_value=["upgrade sale is required"],
            ),
            patch(
                "oduflow.docker_ops.env_ops.update_environment", return_value={}
            ) as update,
            patch("oduflow.docker_ops.env_ops._apply_actions") as apply,
        ):
            result = env_ops.pull_environment(
                settings, team, "env", restart=True, strict=True
            )

        assert result["action"] == "blocked"
        update.assert_not_called()
        apply.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_auto_uses_path_only_classification(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        py_file = repo / "sale" / "models" / "sale.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text("old")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = py_file.stat()
        py_file.write_text("new")
        os.utime(py_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "restart",
            "changed_files": ["sale/models/sale.py"],
            "message": "Container restarted.",
        }

        result = env_ops.pull_environment(TEST_SETTINGS, team, "env")

        assert result["action"] == "restart"
        assert mock_apply.call_args.kwargs["do_restart"] is True
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == []

    def test_git_markdown_only_needs_no_odoo_action(self, mock_docker_client, tmp_path):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)

        container = MagicMock()
        container.labels = {"oduflow.git_branch": "main"}
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.git_ops.pull_repo", return_value=("old-head", ["README.md"])
        ):
            result = env_ops.pull_environment(settings, team, "env")

        assert result["action"] == "none"
        assert result["changed_files"] == ["README.md"]
        assert result["message"] == (
            "Only Markdown files changed. No Odoo action required."
        )
        container.restart.assert_not_called()

    @pytest.mark.parametrize(
        "pulled", [[], ["README.md"]], ids=["up-to-date", "markdown-only"]
    )
    def test_before_apply_skipped_when_nothing_to_apply(
        self, mock_docker_client, tmp_path, pulled
    ):
        # Issue #309: production's pre-update snapshot hangs off before_apply;
        # a deploy with nothing to apply must not pay for it.
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        container = MagicMock()
        container.labels = {"oduflow.git_branch": "main"}
        mock_docker_client.containers.get.return_value = container
        before_apply = MagicMock()

        with patch("oduflow.git_ops.pull_repo", return_value=("old-head", pulled)):
            result = env_ops.pull_environment(
                settings, team, "env", before_apply=before_apply
            )

        assert result["action"] == "none"
        before_apply.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_before_apply_runs_once_before_an_explicit_action(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        container = MagicMock()
        container.labels = {"oduflow.git_branch": "main"}
        mock_docker_client.containers.get.return_value = container
        order: list[str] = []
        mock_apply.side_effect = lambda *a, **kw: (
            order.append("apply") or {"action": "restart", "exit_code": 0}
        )

        with patch("oduflow.git_ops.pull_repo", return_value=("old-head", [])):
            env_ops.pull_environment(
                settings,
                team,
                "env",
                restart=True,
                before_apply=lambda: order.append("before_apply"),
            )

        assert order == ["before_apply", "apply"]

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_requirements_change_passes_deps_changed(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        req = repo / "requirements.txt"
        req.write_text("phonenumbers\n")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = req.stat()
        req.write_text("phonenumbers\nqrcode\n")
        os.utime(req, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "restart",
            "changed_files": ["requirements.txt"],
            "message": "Reinstalled dependencies. Container restarted.",
        }

        env_ops.pull_environment(TEST_SETTINGS, team, "env")

        assert mock_apply.call_args.kwargs["dep_units"] == [
            (str(repo), "/mnt/extra-addons")
        ]

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_shadowed_root_requirements_change_does_not_reinstall(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        (repo / ".oduflow").mkdir(parents=True)
        (repo / ".oduflow" / "requirements.txt").write_text("preferred\n")
        req = repo / "requirements.txt"
        req.write_text("fallback\n")
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = req.stat()
        req.write_text("fallback\nunused-change\n")
        os.utime(req, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "refresh",
            "changed_files": ["requirements.txt"],
            "message": "Files refreshed.",
        }

        env_ops.pull_environment(TEST_SETTINGS, team, "env")

        assert mock_apply.call_args.kwargs["dep_units"] == []
        assert mock_apply.call_args.kwargs["do_restart"] is False

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_failed_apply_does_not_advance_snapshot(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        xml_file = repo / "sale" / "security" / "rules.xml"
        xml_file.parent.mkdir(parents=True)
        xml_file.write_text("<old/>")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = xml_file.stat()
        xml_file.write_text("<new/>")
        os.utime(xml_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "upgrade",
            "modules_upgraded": ["sale"],
            "exit_code": 1,
            "changed_files": ["sale/security/rules.xml"],
            "message": "Upgraded modules: sale",
        }

        result = env_ops.pull_environment(TEST_SETTINGS, team, "env", upgrade=["sale"])

        assert result["exit_code"] == 1
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/security/rules.xml"
        ]

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_strict_block_does_not_advance_snapshot(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        xml_file = repo / "sale" / "security" / "rules.xml"
        xml_file.parent.mkdir(parents=True)
        xml_file.write_text("<old/>")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = xml_file.stat()
        xml_file.write_text("<new/>")
        os.utime(xml_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container

        result = env_ops.pull_environment(
            TEST_SETTINGS, team, "env", restart=True, strict=True
        )

        assert result["action"] == "blocked"
        mock_apply.assert_not_called()
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/security/rules.xml"
        ]

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_explicit_guardrail_warns(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        # A security XML change recommends -u of 'sale'.
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])
        mock_apply.return_value = {
            "action": "restart",
            "changed_files": ["sale/security/ir_rule.xml"],
            "message": "Container restarted.",
        }

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", restart=True
        )

        assert result["action"] == "restart"
        assert any("sale" in w for w in result.get("warnings", []))
        mock_apply.assert_called_once()
        mock_detect.assert_called_once()
        mock_write.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_strict_blocks(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", restart=True, strict=True
        )

        assert result["action"] == "blocked"
        assert any("sale" in w for w in result["warnings"])
        mock_apply.assert_not_called()
        mock_write.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_explicit_correct_no_warn(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])
        mock_apply.return_value = {
            "action": "upgrade",
            "modules_upgraded": ["sale"],
            "changed_files": ["sale/security/ir_rule.xml"],
            "message": "Upgraded.",
        }

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", upgrade=["sale"]
        )

        assert result["action"] == "upgrade"
        assert not result.get("warnings")
        mock_apply.assert_called_once()
        mock_write.assert_called_once()


class TestAutoPlanNarrowing:
    """Auto mode only acts on modules the database can act on (issue: extra
    repos bundle hundreds of modules; a fast-forward must not fail the deploy
    over the uninstalled ones)."""

    @staticmethod
    def _tree(root, files):
        """Lay out *files* under *root*; ``<mod>/__manifest__.py`` marks a module."""
        for rel in files:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if path.name == "__manifest__.py" else "")

    def _pull_auto(self, mock_docker_client, tmp_path, tree, changed, states):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        settings = Settings(teams={"1": team})
        repo = tmp_path / "team" / "workspaces" / "env" / "repo"
        repo.mkdir(parents=True)
        self._tree(repo, tree)
        container = MagicMock()
        container.labels = {"oduflow.git_branch": "main"}
        mock_docker_client.containers.get.return_value = container
        with (
            # No base ref: the real path-only classifier runs, git-free.
            patch("oduflow.git_ops.pull_repo", return_value=("", changed)),
            patch(
                "oduflow.docker_ops.odoo_ops._get_module_states",
                return_value=states,
            ) as module_states,
            patch(
                "oduflow.docker_ops.env_ops._apply_actions",
                return_value={"action": "none", "changed_files": changed},
            ) as apply,
        ):
            result = env_ops.pull_environment(settings, team, "env")
        return result, apply.call_args.kwargs, module_states

    def test_uninstalled_modules_are_skipped_not_failed(
        self, mock_docker_client, tmp_path
    ):
        manifests = ["moda/__manifest__.py", "modb/__manifest__.py"]
        result, applied, _states = self._pull_auto(
            mock_docker_client,
            tmp_path,
            manifests,
            manifests,
            {"moda": "installed", "modb": "uninstalled"},
        )
        assert applied["to_upgrade"] == ["moda"]
        assert any("modb" in w for w in result["warnings"])

    def test_changes_only_in_uninstalled_modules_are_inert(
        self, mock_docker_client, tmp_path
    ):
        changed = ["modb/__manifest__.py", "modb/models/b.py"]
        result, applied, _states = self._pull_auto(
            mock_docker_client, tmp_path, changed, changed, {"modb": "uninstalled"}
        )
        # Odoo never loads an uninstalled module's code: nothing to upgrade,
        # nothing to restart or refresh for.
        assert applied["to_upgrade"] == []
        assert applied["do_restart"] is False
        assert applied["do_refresh"] is False
        assert "not installed" in result["message"]

    def test_live_python_still_restarts_when_all_upgrades_are_skipped(
        self, mock_docker_client, tmp_path
    ):
        # modb's manifest bump is the only upgrade and modb is uninstalled,
        # but moda (installed) changed a .py — the running server must restart
        # even though the merged action ("upgrade") masked it.
        changed = ["moda/models/a.py", "modb/__manifest__.py"]
        _result, applied, _states = self._pull_auto(
            mock_docker_client,
            tmp_path,
            ["moda/__manifest__.py", *changed],
            changed,
            {"moda": "installed", "modb": "uninstalled"},
        )
        assert applied["to_upgrade"] == []
        assert applied["do_restart"] is True

    def test_views_and_js_in_installed_module_still_refresh(
        self, mock_docker_client, tmp_path
    ):
        changed = [
            "moda/views/a.xml",
            "moda/static/src/a.js",
            "modb/__manifest__.py",
        ]
        _result, applied, _states = self._pull_auto(
            mock_docker_client,
            tmp_path,
            ["moda/__manifest__.py", *changed],
            changed,
            {"moda": "installed", "modb": "uninstalled"},
        )
        assert applied["to_upgrade"] == []
        assert applied["do_refresh"] is True
        assert applied["do_restart"] is False

    def test_python_outside_any_module_restarts(self, mock_docker_client, tmp_path):
        # tools/ has no manifest: its .py is live code (e.g. loaded through a
        # server-wide module), not an uninstalled module named "tools".
        changed = ["tools/patches/monkey.py", "modb/__manifest__.py"]
        _result, applied, module_states = self._pull_auto(
            mock_docker_client, tmp_path, changed, changed, {"modb": "uninstalled"}
        )
        assert applied["to_upgrade"] == []
        assert applied["do_restart"] is True
        assert module_states.call_args.args[3] == ("modb",)

    def test_skipped_pseudo_module_keeps_its_python_restart(
        self, mock_docker_client, tmp_path
    ):
        # The path-only fallback calls tools/data.csv an upgrade of "tools";
        # skipping that pseudo-module must not swallow the live .py beside it.
        changed = ["tools/data.csv", "tools/patch.py"]
        result, applied, _states = self._pull_auto(
            mock_docker_client, tmp_path, changed, changed, {}
        )
        assert applied["to_upgrade"] == []
        assert applied["do_restart"] is True
        assert any("tools" in w for w in result["warnings"])

    def test_unsafe_module_names_never_reach_sql(self, mock_docker_client, tmp_path):
        evil = "x'); DROP TABLE res_users; --"
        changed = [f"{evil}/__manifest__.py", "modb/__manifest__.py"]
        _result, _applied, module_states = self._pull_auto(
            mock_docker_client, tmp_path, changed, changed, {"modb": "installed"}
        )
        assert module_states.call_args.args[3] == ("modb",)

    def _unit(self, tmp_path, files, rec):
        self._tree(tmp_path, files)
        return [((str(tmp_path), "old", files), rec)]

    @staticmethod
    def _rec(action, install=(), upgrade=()):
        return {
            "action": action,
            "modules_to_install": list(install),
            "modules_to_upgrade": list(upgrade),
            "details": {},
        }

    @pytest.mark.parametrize("is_production", [True, False])
    def test_installed_module_classified_as_new_is_upgraded(
        self, tmp_path, is_production
    ):
        # Moved between directories/repos: git finds no old manifest and the
        # classifier says "install", but the database already has it.
        unit_recs = self._unit(
            tmp_path, ["foo/__manifest__.py"], self._rec("install", install=["foo"])
        )
        with patch(
            "oduflow.docker_ops.odoo_ops._get_module_states",
            return_value={"foo": "installed"},
        ):
            plan, warnings = env_ops._narrow_auto_plan(
                TEST_SETTINGS,
                TEST_TEAM,
                "prod-erp",
                unit_recs,
                is_production=is_production,
            )
        assert plan["action"] == "upgrade"
        assert plan["modules_to_install"] == []
        assert plan["modules_to_upgrade"] == ["foo"]
        assert warnings == []

    def test_production_never_auto_installs(self, tmp_path):
        unit_recs = self._unit(
            tmp_path,
            ["brand_new_mod/__manifest__.py"],
            self._rec("install", install=["brand_new_mod"]),
        )
        with patch("oduflow.docker_ops.odoo_ops._get_module_states", return_value={}):
            plan, warnings = env_ops._narrow_auto_plan(
                TEST_SETTINGS, TEST_TEAM, "prod-erp", unit_recs, is_production=True
            )
        assert plan["action"] == "none"
        assert plan["modules_to_install"] == []
        assert any("brand_new_mod" in w for w in warnings)
        assert any("install=" in w for w in warnings)

    def test_dev_environment_still_auto_installs_new_modules(self, tmp_path):
        unit_recs = self._unit(
            tmp_path,
            ["brand_new_mod/__manifest__.py"],
            self._rec("install", install=["brand_new_mod"]),
        )
        with patch("oduflow.docker_ops.odoo_ops._get_module_states", return_value={}):
            plan, warnings = env_ops._narrow_auto_plan(
                TEST_SETTINGS, TEST_TEAM, "env", unit_recs, is_production=False
            )
        assert plan["action"] == "install"
        assert plan["modules_to_install"] == ["brand_new_mod"]
        assert warnings == []

    def test_unreadable_module_states_keep_the_plan_unfiltered(self, tmp_path):
        unit_recs = self._unit(
            tmp_path,
            ["moda/__manifest__.py", "moda/models/a.py"],
            self._rec("upgrade", upgrade=["moda"]),
        )
        with patch(
            "oduflow.docker_ops.odoo_ops._get_module_states",
            side_effect=RuntimeError("db down"),
        ):
            plan, warnings = env_ops._narrow_auto_plan(
                TEST_SETTINGS, TEST_TEAM, "env", unit_recs, is_production=False
            )
        assert plan["action"] == "upgrade"
        assert plan["modules_to_upgrade"] == ["moda"]
        assert warnings == []

    def test_module_state_lookup_rejects_unsafe_names(self):
        from oduflow.docker_ops import odoo_ops

        with patch.object(odoo_ops, "_execute_db_query") as query:
            with pytest.raises(ValueError):
                odoo_ops._get_module_states(
                    TEST_SETTINGS, TEST_TEAM, "env", ("a'); DROP TABLE x; --",)
                )
        query.assert_not_called()


class TestCreateEnvironmentEnvVars:
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-env/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-env", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50002)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_merges_env_vars_and_label(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
    ):
        import json

        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.run.return_value = mock_odoo
        init_container = mock_docker_client.containers.create.return_value
        init_container.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/env",
            "https://github.com/org/repo.git",
            "odoo:15.0",
            template_name=None,
            env_vars={"FOO": "bar", "BAZ": "qux"},
        )

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        environment = run_kwargs["environment"]
        assert environment["FOO"] == "bar"
        assert environment["BAZ"] == "qux"
        assert environment["HOST"] == TEST_SETTINGS.shared_db_container
        assert environment["USER"] == "u_1_feature-env"
        stored = json.loads(run_kwargs["labels"]["oduflow.env_vars"])
        assert stored == {"FOO": "bar", "BAZ": "qux"}


class TestUpdateEnvironment:
    def _make_container(self):
        container = MagicMock()
        container.image.tags = ["odoo:15.0"]
        container.image.id = "sha256:old"
        container.labels = {
            "oduflow.template": "none",
            TEST_SETTINGS.image_label: "odoo:15.0",
            "oduflow.env_vars": '{"OLD": "1"}',
        }
        container.attrs = {
            "Config": {
                "Env": ["HOST=db", "USER=old", "PASSWORD=x", "OLD=1"],
                "Cmd": ["odoo", "-d", "oduflow_1_main", "--dev=xml"],
                "Image": "odoo:15.0",
            },
            "HostConfig": {
                "Binds": ["/host/repo:/mnt/extra-addons:rw"],
                "PortBindings": {"8069/tcp": [{"HostPort": "50000"}]},
            },
        }
        return container

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_update_image_and_env_override(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_reapply_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        import json

        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        new_image = MagicMock()
        new_image.id = "sha256:new"
        mock_docker_client.images.pull.return_value = new_image
        mock_docker_client.containers.run.return_value = MagicMock()

        result = env_ops.update_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            env_override={"FOO": "new"},
            image_override="odoo:17.0",
        )

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["image"] == "odoo:17.0"
        assert run_kwargs["environment"] == {
            "HOST": TEST_SETTINGS.shared_db_container,
            "USER": "u_1_main",
            "PASSWORD": "pw",
            "FOO": "new",
        }
        assert run_kwargs["labels"][TEST_SETTINGS.image_label] == "odoo:17.0"
        assert json.loads(run_kwargs["labels"]["oduflow.env_vars"]) == {"FOO": "new"}
        assert run_kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
        mock_docker_client.images.pull.assert_called_once_with("odoo:17.0")
        assert result["image"] == "odoo:17.0"
        assert result["image_updated"] is True
        assert result["env_vars"] == {"FOO": "new"}

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    @patch("oduflow.docker_ops.env_ops.check_rename_target")
    @patch("oduflow.docker_ops.env_ops._relocate_environment_state")
    def test_rename_recreates_the_container_under_the_new_name(
        self,
        mock_relocate,
        mock_check,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_reapply_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        container = self._make_container()
        container.attrs["HostConfig"]["Binds"] = [
            "/tmp/flow-test/workspaces/main/repo:/mnt/extra-addons:rw",
            "/tmp/flow-test/workspaces/main/filestore:"
            "/var/lib/odoo/.local/share/Odoo/filestore/oduflow_1_main:rw",
        ]
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.run.return_value = MagicMock()

        result = env_ops.update_environment(
            TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, rename_to="next"
        )

        # The name-keyed state moves while the container is down — after it was
        # removed, before the new one is created.
        assert mock_relocate.call_args[0][3:] == ("main", "next")
        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["name"] == "oduflow-1-next-odoo"
        assert run_kwargs["labels"][TEST_SETTINGS.branch_label] == "next"
        assert run_kwargs["command"] == "odoo -d oduflow_1_next --dev=xml"
        assert set(run_kwargs["volumes"]) == {
            "/tmp/flow-test/workspaces/next/repo",
            "/tmp/flow-test/workspaces/next/filestore",
        }
        assert run_kwargs["volumes"]["/tmp/flow-test/workspaces/next/filestore"][
            "bind"
        ] == ("/var/lib/odoo/.local/share/Odoo/filestore/oduflow_1_next")
        assert result["env_name"] == "next"
        assert result["database"] == "oduflow_1_next"

    @patch("oduflow.docker_ops.env_ops.check_rename_target")
    @patch(
        "oduflow.docker_ops.env_ops._relocate_environment_state",
        side_effect=RuntimeError("database is being accessed by other users"),
    )
    def test_a_failed_relocation_reports_the_containerless_state(
        self, mock_relocate, mock_check, mock_docker_client
    ):
        # The old container is removed before the relocation and cannot be
        # brought back; the error must say what the operation left behind
        # instead of surfacing a bare ALTER DATABASE failure.
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(FlowError) as excinfo:
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, rename_to="next"
            )

        message = str(excinfo.value)
        assert "Renaming 'main' to 'next' failed" in message
        assert "database is being accessed by other users" in message
        assert "currently has no container" in message
        mock_docker_client.containers.run.assert_not_called()

    def test_rename_to_the_current_name_is_ignored(self, mock_docker_client):
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container

        with (
            patch("oduflow.docker_ops.env_ops.check_rename_target") as check,
            patch.object(env_ops, "_relocate_environment_state") as relocate,
            patch(
                "oduflow.docker_ops.env_ops.load_credentials",
                return_value={"pg_user": "u_1_main", "pg_password": "pw"},
            ),
            patch("oduflow.docker_ops.env_ops._create_pg_role"),
            patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value=""),
            patch(
                "oduflow.docker_ops.env_ops._install_pip_requirements",
                return_value=(False, ""),
            ),
            patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True),
        ):
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, rename_to="main"
            )

        check.assert_not_called()
        relocate.assert_not_called()

    def _rename(self, mock_docker_client, container, **kwargs):
        """Run a rename through update_environment with the slow parts stubbed."""
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.run.return_value = MagicMock()
        with (
            patch("oduflow.docker_ops.env_ops.check_rename_target"),
            patch.object(env_ops, "_relocate_environment_state"),
            patch(
                "oduflow.docker_ops.env_ops.load_credentials",
                return_value={"pg_user": "u_1_main", "pg_password": "pw"},
            ),
            patch("oduflow.docker_ops.env_ops._create_pg_role"),
            patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False),
            patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False),
            patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value=""),
            patch(
                "oduflow.docker_ops.env_ops._install_pip_requirements",
                return_value=(False, ""),
            ),
            patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True),
        ):
            return env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, **kwargs
            )

    # A bare team domain has no prefix to replace: qa nests under it.
    @pytest.mark.parametrize("team_hostname", ["dev.example.com", "example.com"])
    @pytest.mark.parametrize("old_hostname", ["", "dev1", "previous"])
    def test_update_hostname_routes_and_registry(
        self, mock_docker_client, tmp_path, old_hostname, team_hostname
    ):
        from oduflow.hostname_registry import allocate_hostname, get_hostname

        team = dataclasses.replace(TEST_TEAM, hostname=team_hostname)
        settings = dataclasses.replace(
            TEST_SETTINGS, routing_mode="traefik", teams={"1": team}
        )
        container = self._make_container()
        registry = env_ops._hostname_registry_path(team)
        if old_hostname:
            container.labels[env_ops.ENV_HOSTNAME_LABEL] = old_hostname
            container.labels[env_ops.ENV_HOSTNAME_SOURCE_LABEL] = (
                "slot" if old_hostname == "dev1" else "custom"
            )
            allocate_hostname(
                registry,
                "main",
                0,
                requested_hostname=old_hostname,
                hostname_prefix="dev",
            )
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.list.return_value = []
        with ExitStack() as stack:
            for name in ("_reapply_odoo_conf", "_create_pg_role", "release_port"):
                stack.enter_context(patch.object(env_ops, name))
            stack.enter_context(
                patch.object(
                    env_ops,
                    "load_credentials",
                    return_value={"pg_user": "u", "pg_password": "pw"},
                )
            )
            result = env_ops.update_environment(
                settings,
                team,
                "main",
                hostname_override="qa",
                pull_image=False,
                install_dependencies=False,
            )
            labels = mock_docker_client.containers.run.call_args.kwargs["labels"]
            assert labels[env_ops.ENV_HOSTNAME_LABEL] == "qa"
            assert labels[env_ops.ENV_HOSTNAME_SOURCE_LABEL] == "custom"
            assert any(
                "Host(`qa.example.com`)" in v
                for k, v in labels.items()
                if k.endswith(".rule")
            )
            assert result["url"].endswith("://qa.example.com")
            assert get_hostname(registry, "main") == "qa"
            # An ordinary subsequent update preserves the explicit assignment.
            container.labels = labels
            result = env_ops.update_environment(
                settings, team, "main", pull_image=False, install_dependencies=False
            )
            assert result["hostname"] == "qa"

    def test_hostname_conflict_keeps_container(self, mock_docker_client):
        from oduflow.hostname_registry import allocate_hostname

        team = dataclasses.replace(TEST_TEAM, hostname="dev.example.com")
        settings = dataclasses.replace(
            TEST_SETTINGS, routing_mode="traefik", teams={"1": team}
        )
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.list.return_value = []
        allocate_hostname(
            env_ops._hostname_registry_path(team),
            "other",
            0,
            requested_hostname="qa",
            hostname_prefix="dev",
        )
        with pytest.raises(ConflictError):
            env_ops.update_environment(
                settings, team, "main", hostname_override="qa", pull_image=False
            )
        container.stop.assert_not_called()
        container.remove.assert_not_called()

    @pytest.mark.parametrize("hostname", ["qa", "bad/name"])
    def test_hostname_rejected_before_stop_in_port_mode(
        self, mock_docker_client, hostname
    ):
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        with pytest.raises(ValueError):
            env_ops.update_environment(
                dataclasses.replace(TEST_SETTINGS, routing_mode="port"),
                TEST_TEAM,
                "main",
                hostname_override=hostname,
            )
        container.stop.assert_not_called()

    def test_the_agent_checkout_follows_the_rename(self, mock_docker_client):
        container = self._make_container()
        container.labels.update(
            {
                TEST_SETTINGS.repo_label: "https://example.com/repo.git",
                "oduflow.git_branch": "feature/login",
                "oduflow.git_user": "dev",
            }
        )

        with (
            patch.object(env_ops, "_agent_rename_env") as agent_rename,
            patch.object(env_ops, "_agent_add_env") as agent_add,
        ):
            result = self._rename(mock_docker_client, container, rename_to="next")

        # Moved, never re-cloned: the checkout can hold uncommitted work.
        assert agent_rename.call_args[0][3:] == ("main", "next")
        # And its .mcp.json is rewritten, because the scoped MCP URL carries the
        # environment name in its path.
        assert agent_add.call_args[0][3:] == (
            "next",
            "https://example.com/repo.git",
            "feature/login",
            "dev",
        )
        assert result["renamed_from"] == "main"
        assert result["env_name"] == "next"

    def test_a_live_mounted_environment_has_no_repo_to_clone_from(
        self, mock_docker_client
    ):
        container = self._make_container()
        container.labels.update(
            {
                TEST_SETTINGS.repo_label: "https://example.com/repo.git",
                "oduflow.local_path": "/home/dev/checkout",
            }
        )

        with (
            patch.object(env_ops, "_agent_rename_env"),
            patch.object(env_ops, "_agent_add_env") as agent_add,
        ):
            self._rename(mock_docker_client, container, rename_to="next")

        assert agent_add.call_args[0][4] == ""

    def test_the_caller_can_own_the_agent_sync(self, mock_docker_client):
        # switch_branch refreshes the checkout itself, with the target branch;
        # clone-env.sh is too expensive to run twice for one operation.
        container = self._make_container()

        with (
            patch.object(env_ops, "_agent_rename_env") as agent_rename,
            patch.object(env_ops, "_agent_add_env") as agent_add,
        ):
            self._rename(
                mock_docker_client,
                container,
                rename_to="next",
                sync_agent_checkout=False,
            )

        agent_rename.assert_not_called()
        agent_add.assert_not_called()

    def test_an_update_without_a_rename_leaves_the_agent_checkout_alone(
        self, mock_docker_client
    ):
        container = self._make_container()

        with (
            patch.object(env_ops, "_agent_rename_env") as agent_rename,
            patch.object(env_ops, "_agent_add_env") as agent_add,
        ):
            result = self._rename(mock_docker_client, container)

        agent_rename.assert_not_called()
        agent_add.assert_not_called()
        assert result["renamed_from"] == ""

    def test_a_stack_managed_environment_cannot_be_renamed(self, mock_docker_client):
        # The stack reconciles its environments by name; a rename behind its
        # back would make it provision the old name again.
        container = self._make_container()
        container.labels["oduflow.stack"] = "demo"
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ConflictError, match="demo"):
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, rename_to="next"
            )

        # Refused before the first mutation.
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_a_production_container_cannot_be_renamed(self, mock_docker_client):
        # The REST route has no production fence of its own; the rename must
        # refuse a production before the first mutation.
        container = self._make_container()
        container.labels["oduflow.prod"] = "true"
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ConflictError, match="production"):
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, rename_to="next"
            )

        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_the_reported_database_is_team_scoped(self, mock_docker_client):
        # The database name a team's environment gets is oduflow_<team>_<slug>;
        # reporting team 1's name to any other team is a wrong answer.
        team = dataclasses.replace(TEST_TEAM, team_id="7")
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.run.return_value = MagicMock()

        with (
            patch(
                "oduflow.docker_ops.env_ops.load_credentials",
                return_value={"pg_user": "u_7_main", "pg_password": "pw"},
            ),
            patch("oduflow.docker_ops.env_ops._create_pg_role"),
            patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False),
            patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False),
            patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value=""),
            patch(
                "oduflow.docker_ops.env_ops._install_pip_requirements",
                return_value=(False, ""),
            ),
            patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True),
        ):
            result = env_ops.update_environment(
                TEST_SETTINGS, team, "main", pull_image=False
            )

        assert result["database"] == "oduflow_7_main"

    def test_update_aborts_when_image_unavailable(self, mock_docker_client):
        # Image can't be pulled and isn't local: abort WITHOUT removing the old
        # container, so the env is never left with no container (#49).
        from oduflow.errors import PrerequisiteNotMetError

        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.images.pull.side_effect = RuntimeError("network down")
        mock_docker_client.images.get.side_effect = docker.errors.ImageNotFound("nf")

        with pytest.raises(PrerequisiteNotMetError, match="could not be pulled"):
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", image_override="odoo:99.0"
            )

        # The existing container must be left running.
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_update_no_overrides_keeps_persisted_image_tag_and_env(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_reapply_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        container = self._make_container()
        # A previous digest-pinned recreation can leave the container without
        # image tags and with Config.Image set to the digest. The persisted
        # Oduflow label must remain the source for future image pulls.
        container.image.tags = []
        container.attrs["Config"]["Image"] = "sha256:old"
        mock_docker_client.containers.get.return_value = container
        same_image = MagicMock()
        same_image.id = "sha256:old"
        mock_docker_client.images.pull.return_value = same_image
        mock_docker_client.containers.run.return_value = MagicMock()

        result = env_ops.update_environment(TEST_SETTINGS, TEST_TEAM, "main")

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        # No image override → current image is reused and re-pulled
        assert run_kwargs["image"] == "odoo:15.0"
        mock_docker_client.images.pull.assert_called_once_with("odoo:15.0")
        assert run_kwargs["labels"][TEST_SETTINGS.image_label] == "odoo:15.0"
        # No env override → env restored from the persisted label
        assert run_kwargs["environment"]["OLD"] == "1"
        assert result["image"] == "odoo:15.0"
        assert result["env_vars"] == {"OLD": "1"}
        assert result["image_updated"] is False

    def test_extra_checkout_switch_reuses_exact_image_and_skips_dependencies(
        self, mock_docker_client
    ):
        container = self._make_container()
        container.labels.update(
            {
                "oduflow.extra_addons": json.dumps({"enterprise": "18.0"}),
                "oduflow.extra_addons_revisions": json.dumps({"enterprise": "a" * 40}),
            }
        )
        container.attrs["HostConfig"]["Binds"].append(
            "/old/enterprise:/mnt/extra-addons-enterprise:ro"
        )
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.containers.run.return_value = MagicMock()

        with (
            patch(
                "oduflow.docker_ops.env_ops.load_credentials",
                return_value={"pg_user": "u_1_main", "pg_password": "pw"},
            ),
            patch("oduflow.docker_ops.env_ops._create_pg_role"),
            patch("oduflow.docker_ops.env_ops._reapply_odoo_conf"),
            patch("oduflow.docker_ops.env_ops._install_apt_packages") as apt,
            patch("oduflow.docker_ops.env_ops._install_pip_requirements") as pip,
        ):
            env_ops.update_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                extra_checkout_overrides={"enterprise": "/cache/enterprise/new"},
                extra_revision_overrides={"enterprise": "b" * 40},
                pull_image=False,
                install_dependencies=False,
            )

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["image"] == "sha256:old"
        assert "/old/enterprise" not in run_kwargs["volumes"]
        assert run_kwargs["volumes"]["/cache/enterprise/new"] == {
            "bind": "/mnt/extra-addons-enterprise",
            "mode": "ro",
        }
        assert json.loads(run_kwargs["labels"]["oduflow.extra_addons_revisions"]) == {
            "enterprise": "b" * 40
        }
        mock_docker_client.images.pull.assert_not_called()
        apt.assert_not_called()
        pip.assert_not_called()

    # The recreated container boots Odoo before Oduflow can copy the conf in or
    # install anything, so whatever lands afterwards needs a restart to take
    # effect — and only then.

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_reapplied_conf_is_picked_up_by_a_restart(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        # install_dependencies=False (the pull mount migration and the branch
        # switch): the conf is still reapplied, so the restart must happen or
        # the environment serves on the image's stock addons_path.
        mock_docker_client.containers.get.return_value = self._make_container()
        new_container = MagicMock()
        mock_docker_client.containers.run.return_value = new_container

        env_ops.update_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            pull_image=False,
            install_dependencies=False,
        )

        new_container.restart.assert_called_once()

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_no_restart_when_neither_conf_nor_dependencies_landed(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        # No base conf anywhere and nothing to install: the fresh container is
        # already running exactly what a restart would give it.
        mock_docker_client.containers.get.return_value = self._make_container()
        new_container = MagicMock()
        mock_docker_client.containers.run.return_value = new_container

        env_ops.update_environment(TEST_SETTINGS, TEST_TEAM, "main", pull_image=False)

        new_container.restart.assert_not_called()

    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_xyz", "pg_password": "pw"},
    )
    def test_update_missing_raises(self, mock_creds, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.update_environment(TEST_SETTINGS, TEST_TEAM, "xyz")


class TestAutoInstallReadiness:
    def test_registry_probe_selects_target_database(self):
        probe = env_ops._odoo_registry_probe("oduflow_1_feature")

        assert probe[:2] == ["python3", "-c"]
        assert "/web/login?" in probe[2]
        assert "oduflow_1_feature" in probe[2]
        assert "HTTPCookieProcessor" in probe[2]

    def test_wait_retries_until_target_registry_is_ready(self):
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, b"not ready"),
            docker.errors.APIError("container restarting"),
            (0, b"ready"),
        ]

        with patch("oduflow.docker_ops.env_ops.time.sleep") as mock_sleep:
            result = env_ops._wait_for_container_odoo_ready(
                container, "oduflow_1_feature"
            )

        assert result.ready is True
        assert result.attempts == 3
        assert result.timeout_seconds == 120
        assert container.exec_run.call_count == 3
        expected_probe = env_ops._odoo_registry_probe("oduflow_1_feature")
        assert all(
            call.args[0] == expected_probe for call in container.exec_run.call_args_list
        )
        assert mock_sleep.call_count == 2

    def test_wait_times_out_after_last_failed_probe(self):
        container = MagicMock()
        container.exec_run.return_value = (1, b"not ready")

        with (
            patch(
                "oduflow.docker_ops.env_ops.time.monotonic",
                side_effect=[10.0, 12.0],
            ),
            patch("oduflow.docker_ops.env_ops.time.sleep") as mock_sleep,
        ):
            result = env_ops._wait_for_container_odoo_ready(
                container, "oduflow_1_feature", timeout=1
            )

        assert result.ready is False
        assert result.attempts == 1
        assert result.timeout_seconds == 1
        assert result.last_error == "not ready"
        container.exec_run.assert_called_once_with(
            env_ops._odoo_registry_probe("oduflow_1_feature")
        )
        mock_sleep.assert_not_called()

    @patch("oduflow.docker_ops.odoo_ops._require_installed_modules")
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 2.0, 2, 120),
    )
    def test_auto_install_waits_before_starting_second_odoo(
        self, mock_wait, mock_require_installed
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, b"installed")
        calls = MagicMock()
        calls.attach_mock(mock_wait, "wait")
        calls.attach_mock(container.exec_run, "install")

        result = env_ops._auto_install_modules(
            container,
            TEST_SETTINGS,
            TEST_TEAM,
            "oduflow_1_feature",
            ["red_border"],
            "feature",
        )

        assert [call[0] for call in calls.mock_calls] == ["wait", "install"]
        mock_wait.assert_called_once_with(container, "oduflow_1_feature")
        install_cmd = container.exec_run.call_args.args[0]
        assert "-d oduflow_1_feature -i red_border" in install_cmd
        mock_require_installed.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "feature", ("red_border",)
        )
        assert "completed successfully" in result

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(
            False, 120.0, 60, 120, "URLError: connection refused"
        ),
    )
    def test_auto_install_is_not_attempted_before_readiness(self, mock_wait):
        container = MagicMock()
        container.logs.return_value = b"Failed to load registry\ntraceback"

        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            env_ops._auto_install_modules(
                container,
                TEST_SETTINGS,
                TEST_TEAM,
                "oduflow_1_feature",
                ["red_border"],
                "feature",
            )

        mock_wait.assert_called_once_with(container, "oduflow_1_feature")
        container.exec_run.assert_not_called()
        message = str(exc_info.value)
        assert "was not attempted" in message
        assert "URLError: connection refused" in message
        assert "Failed to load registry" in message

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 2.0, 2, 120),
    )
    def test_auto_install_command_failure_is_fatal(self, mock_wait):
        container = MagicMock()
        container.exec_run.return_value = (1, b"module import failed")

        with pytest.raises(ExternalCommandError, match="module import failed"):
            env_ops._auto_install_modules(
                container,
                TEST_SETTINGS,
                TEST_TEAM,
                "oduflow_1_feature",
                ["red_border"],
                "feature",
            )

    def test_auto_install_validates_module_names_before_exec(self):
        container = MagicMock()

        with pytest.raises(ValueError, match="Invalid module name"):
            env_ops._auto_install_modules(
                container,
                TEST_SETTINGS,
                TEST_TEAM,
                "oduflow_1_feature",
                ["red_border --stop-after-init"],
                "feature",
            )

        container.exec_run.assert_not_called()


class TestTemplateModuleUpgrade:
    UPGRADES = {"zipfit_delivery_email_rfq": ("18.0.1.17.0", "18.0.1.18.0")}

    def test_relaxed_probe_accepts_http_error_statuses(self):
        strict = env_ops._odoo_registry_probe("oduflow_1_feature")[2]
        relaxed = env_ops._odoo_registry_probe(
            "oduflow_1_feature", accept_http_errors=True
        )[2]

        assert "except urllib.error.HTTPError:" not in strict
        assert "except urllib.error.HTTPError:\n    sys.exit(0)" in relaxed
        # Connection errors still mean "not up yet" in both modes.
        assert relaxed.index("HTTPError") < relaxed.index("except Exception")

    def test_wait_uses_the_relaxed_probe_on_request(self):
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        env_ops._wait_for_container_odoo_ready(
            container, "oduflow_1_feature", accept_http_errors=True
        )

        container.exec_run.assert_called_once_with(
            env_ops._odoo_registry_probe("oduflow_1_feature", accept_http_errors=True)
        )

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 2.0, 1, 120),
    )
    def test_upgrade_runs_after_odoo_answers(self, mock_wait):
        container = MagicMock()
        container.exec_run.return_value = (0, b"upgraded")
        calls = MagicMock()
        calls.attach_mock(mock_wait, "wait")
        calls.attach_mock(container.exec_run, "upgrade")

        log = env_ops._upgrade_template_modules(
            container, "oduflow_1_feature", self.UPGRADES, "feature"
        )

        assert [call[0] for call in calls.mock_calls] == ["wait", "upgrade"]
        mock_wait.assert_called_once_with(
            container, "oduflow_1_feature", accept_http_errors=True
        )
        cmd = container.exec_run.call_args.args[0]
        assert "-d oduflow_1_feature -u zipfit_delivery_email_rfq" in cmd
        assert "--stop-after-init --no-http" in cmd
        assert "18.0.1.17.0 -> 18.0.1.18.0" in log

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(
            False, 120.0, 60, 120, "URLError: connection refused"
        ),
    )
    def test_upgrade_is_not_attempted_when_odoo_never_answers(self, mock_wait):
        container = MagicMock()
        container.logs.return_value = b"traceback"

        with pytest.raises(PrerequisiteNotMetError, match="was not attempted"):
            env_ops._upgrade_template_modules(
                container, "oduflow_1_feature", self.UPGRADES, "feature"
            )

        container.exec_run.assert_not_called()

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 2.0, 1, 120),
    )
    def test_upgrade_failure_is_fatal(self, mock_wait):
        container = MagicMock()
        container.exec_run.return_value = (255, b"ParseError in view")

        with pytest.raises(ExternalCommandError, match="ParseError in view"):
            env_ops._upgrade_template_modules(
                container, "oduflow_1_feature", self.UPGRADES, "feature"
            )

    @patch(
        "oduflow.docker_ops.env_ops._exec_sql",
        side_effect=ExternalCommandError("psql", 1, "relation does not exist"),
    )
    def test_failed_version_check_upgrades_nothing_but_reports_why(self, mock_sql):
        upgrades, error = env_ops._template_module_upgrades(
            MagicMock(),
            TEST_SETTINGS,
            "oduflow_1_feature",
            {"/repo": {"bind": "/mnt/extra-addons", "mode": "rw"}},
            None,
            "feature",
        )

        assert upgrades == {}
        # The reason has to reach the caller: without it a checkout ahead of
        # the template only produces an unexplained readiness timeout.
        assert error is not None
        assert "relation does not exist" in error

    def test_version_check_survives_a_duplicate_addons_path_key(self, tmp_path):
        addons = tmp_path / "repo" / "addons"
        (addons / "shared").mkdir(parents=True)
        (addons / "shared" / "__manifest__.py").write_text("{'version': '1.2'}")
        conf = tmp_path / "odoo.conf"
        # A repository conf spelling the key in another case leaves the
        # generated conf with two options that configparser lowercases alike.
        conf.write_text(
            "[options]\naddons_path = /mnt/extra-addons\n"
            "Addons_Path = /mnt/extra-addons/addons\n"
        )

        with patch(
            "oduflow.docker_ops.env_ops._exec_sql",
            return_value=json.dumps({"base": "18.0.1.3", "shared": "18.0.1.1"}),
        ):
            upgrades, error = env_ops._template_module_upgrades(
                MagicMock(),
                TEST_SETTINGS,
                "oduflow_1_feature",
                {str(tmp_path / "repo"): {"bind": "/mnt/extra-addons", "mode": "rw"}},
                str(conf),
                "feature",
            )

        assert error is None
        assert upgrades == {"shared": ("18.0.1.1", "18.0.1.2")}

    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 2.0, 1, 120),
    )
    def test_upgrade_failure_with_undecodable_output_still_raises(self, mock_wait):
        container = MagicMock()
        container.exec_run.return_value = (255, b"ParseError \xff in view")

        with pytest.raises(ExternalCommandError, match="ParseError"):
            env_ops._upgrade_template_modules(
                container, "oduflow_1_feature", self.UPGRADES, "feature"
            )

    def test_version_check_follows_the_generated_addons_path(self, tmp_path):
        repo, oca = tmp_path / "repo", tmp_path / "oca"
        for addons_dir, version in ((repo / "addons", "1.2"), (oca, "1.9")):
            (addons_dir / "shared").mkdir(parents=True)
            (addons_dir / "shared" / "__manifest__.py").write_text(
                f"{{'version': '{version}'}}"
            )
        conf = tmp_path / "odoo.conf"
        conf.write_text(
            "[options]\naddons_path = /mnt/extra-addons/addons,/mnt/extra-addons-oca\n"
        )

        with patch(
            "oduflow.docker_ops.env_ops._exec_sql",
            return_value=json.dumps({"base": "18.0.1.3", "shared": "18.0.1.1"}),
        ):
            upgrades, error = env_ops._template_module_upgrades(
                MagicMock(),
                TEST_SETTINGS,
                "oduflow_1_feature",
                {
                    str(repo): {"bind": "/mnt/extra-addons", "mode": "rw"},
                    str(oca): {"bind": "/mnt/extra-addons-oca", "mode": "ro"},
                },
                str(conf),
                "feature",
            )

        # The repository copy comes first on the addons_path and shadows OCA's.
        assert error is None
        assert upgrades == {"shared": ("18.0.1.1", "18.0.1.2")}


class TestProvisioningSetup:
    @patch("oduflow.sanitizer.sanitize_environment")
    @patch("oduflow.sanitizer.neutralize_environment")
    @patch(
        "oduflow.docker_ops.env_ops._auto_install_modules",
        side_effect=PrerequisiteNotMetError("registry not ready"),
    )
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 1.0, 1, 120),
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, ""),
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_failed_auto_install_stops_before_sanitization(
        self,
        mock_apt,
        mock_pip,
        mock_wait,
        mock_install,
        mock_neutralize,
        mock_sanitize,
    ):
        container = MagicMock()

        with pytest.raises(PrerequisiteNotMetError, match="registry not ready"):
            env_ops._configure_serving_environment(
                MagicMock(),
                TEST_SETTINGS,
                TEST_TEAM,
                container,
                "/repo",
                "oduflow_1_feature",
                "feature",
                "template",
                True,
                ["red_border"],
                None,
            )

        mock_neutralize.assert_not_called()
        mock_sanitize.assert_not_called()
        # Only the setup restart (conf + dependencies) ran; the post-sanitize
        # one is never reached when the auto-install fails.
        container.restart.assert_called_once()

    @patch("oduflow.sanitizer.sanitize_environment", return_value=["sanitize"])
    @patch("oduflow.sanitizer.neutralize_environment", return_value=["neutralize"])
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 1.0, 1, 120),
    )
    @patch(
        "oduflow.docker_ops.env_ops._auto_install_modules",
        return_value="auto-install",
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, ""),
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_successful_setup_sanitizes_before_final_readiness(
        self,
        mock_apt,
        mock_pip,
        mock_install,
        mock_wait,
        mock_neutralize,
        mock_sanitize,
    ):
        container = MagicMock()
        calls = MagicMock()
        calls.attach_mock(mock_install, "install")
        calls.attach_mock(mock_neutralize, "neutralize")
        calls.attach_mock(mock_sanitize, "sanitize")
        calls.attach_mock(container.restart, "restart")
        calls.attach_mock(mock_wait, "wait")

        result = env_ops._configure_serving_environment(
            MagicMock(),
            TEST_SETTINGS,
            TEST_TEAM,
            container,
            "/repo",
            "oduflow_1_feature",
            "feature",
            "template",
            True,
            ["red_border"],
            None,
        )

        assert [entry[0] for entry in calls.mock_calls] == [
            # Setup restart first: Odoo booted before the generated odoo.conf
            # and the pip packages were in place. Its readiness gate is what
            # keeps neutralization from racing the reloading registry.
            "restart",
            "wait",
            "install",
            "neutralize",
            "sanitize",
            "restart",
            "wait",
        ]
        assert result == ["auto-install", "neutralize", "sanitize"]

    @patch("oduflow.sanitizer.sanitize_environment", return_value=["sanitize"])
    @patch("oduflow.sanitizer.neutralize_environment", return_value=["neutralize"])
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(True, 1.0, 1, 120),
    )
    @patch(
        "oduflow.docker_ops.env_ops._upgrade_template_modules",
        return_value="template-upgrade",
    )
    @patch(
        "oduflow.docker_ops.env_ops._auto_install_modules",
        return_value="auto-install",
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, ""),
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_template_upgrade_runs_before_the_strict_gate(
        self,
        mock_apt,
        mock_pip,
        mock_install,
        mock_upgrade,
        mock_wait,
        mock_neutralize,
        mock_sanitize,
    ):
        container = MagicMock()
        upgrades = {"zipfit_delivery_email_rfq": ("18.0.1.17.0", "18.0.1.18.0")}
        calls = MagicMock()
        calls.attach_mock(mock_upgrade, "upgrade")
        calls.attach_mock(mock_install, "install")
        calls.attach_mock(mock_neutralize, "neutralize")
        calls.attach_mock(container.restart, "restart")
        calls.attach_mock(mock_wait, "wait")

        result = env_ops._configure_serving_environment(
            MagicMock(),
            TEST_SETTINGS,
            TEST_TEAM,
            container,
            "/repo",
            "oduflow_1_feature",
            "feature",
            "template",
            True,
            ["red_border"],
            None,
            None,
            upgrades,
        )

        assert [entry[0] for entry in calls.mock_calls] == [
            # Conf + dependencies are in place before -u imports the modules;
            # the restart after it drops the serving pre-upgrade registry.
            "restart",
            "upgrade",
            "restart",
            "wait",
            "install",
            "neutralize",
            "restart",
            "wait",
        ]
        mock_upgrade.assert_called_once_with(
            container, "oduflow_1_feature", upgrades, "feature"
        )
        assert result == ["template-upgrade", "auto-install", "neutralize", "sanitize"]

    @patch("oduflow.sanitizer.sanitize_environment", return_value=[])
    @patch("oduflow.sanitizer.neutralize_environment", return_value=[])
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        # The setup restart clears its gate; the post-sanitize one does not.
        side_effect=[
            env_ops._OdooReadinessResult(True, 1.0, 1, 120),
            env_ops._OdooReadinessResult(False, 120.0, 60, 120, "HTTPError: HTTP 500"),
        ],
    )
    @patch(
        "oduflow.docker_ops.env_ops._auto_install_modules",
        return_value="auto-install",
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, ""),
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_final_registry_failure_is_fatal(
        self,
        mock_apt,
        mock_pip,
        mock_install,
        mock_wait,
        mock_neutralize,
        mock_sanitize,
    ):
        container = MagicMock()
        container.logs.return_value = b"registry traceback"

        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            env_ops._configure_serving_environment(
                MagicMock(),
                TEST_SETTINGS,
                TEST_TEAM,
                container,
                "/repo",
                "oduflow_1_feature",
                "feature",
                "template",
                True,
                ["red_border"],
                None,
            )

        assert "Environment setup did not complete" in str(exc_info.value)
        assert "HTTPError: HTTP 500" in str(exc_info.value)
        assert "registry traceback" in str(exc_info.value)
        # Sanitization ran: this is the final gate failing, not the setup one.
        mock_sanitize.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._cleanup_old_environment")
    @patch("oduflow.docker_ops.env_ops.release_port")
    def test_partial_environment_rollback_releases_all_resources(
        self, mock_release_port, mock_cleanup
    ):
        env_ops._rollback_partial_environment(
            MagicMock(), TEST_SETTINGS, TEST_TEAM, "feature"
        )

        mock_release_port.assert_called_once_with(
            TEST_TEAM.port_registry_path, "feature"
        )
        mock_cleanup.assert_called_once()


class TestDeleteEnvironment:
    @patch("oduflow.docker_ops.env_ops._drop_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_feature-payments", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops.shutil.rmtree")
    @patch(
        "oduflow.docker_ops.env_ops.os.path.exists",
        side_effect=lambda p: ".protected" not in p,
    )
    def test_delete(
        self,
        mock_exists,
        mock_rmtree,
        mock_sql,
        mock_release,
        mock_load_creds,
        mock_drop_role,
        mock_docker_client,
    ):
        container = MagicMock()
        container.labels = {"oduflow.team": "1"}
        mock_docker_client.containers.get.return_value = container

        with patch("oduflow.docker_ops.env_ops.env_share.remove") as remove_share:
            env_ops.delete_environment(TEST_SETTINGS, TEST_TEAM, "feature/payments")

        container.stop.assert_called_once()
        container.remove.assert_called_once()
        mock_drop_role.assert_called_once_with(
            mock_docker_client, TEST_SETTINGS, "u_1_feature-payments"
        )
        mock_sql.assert_called_once()
        mock_rmtree.assert_called_once()
        mock_release.assert_called_once_with(
            TEST_TEAM.port_registry_path, "feature/payments"
        )
        remove_share.assert_called_once_with(TEST_TEAM, "feature/payments")

    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_delete_missing_raises_not_found(
        self,
        mock_exists,
        mock_sql,
        mock_release,
        mock_docker_client,
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.delete_environment(TEST_SETTINGS, TEST_TEAM, "ghost")

        mock_release.assert_not_called()
        mock_sql.assert_not_called()


class TestRestartEnvironment:
    def test_restart(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.team": "1"}
        mock_docker_client.containers.get.return_value = container

        result = env_ops.restart_environment(TEST_SETTINGS, "main", TEST_TEAM)

        assert result["odoo_container"] == "oduflow-1-main-odoo"
        container.restart.assert_called_once()

    def test_restart_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.restart_environment(TEST_SETTINGS, "main", TEST_TEAM)


class TestStopEnvironment:
    def test_stop(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.team": "1"}
        mock_docker_client.containers.get.return_value = container

        result = env_ops.stop_environment(TEST_SETTINGS, TEST_TEAM, "main")

        assert "oduflow-1-main-odoo" in result["stopped"]
        container.stop.assert_called_once()

    def test_stop_rejects_other_team_container(self, mock_docker_client):
        # Container names are not team-namespaced; a caller scoped to team 1
        # must not stop a container labelled for team 2 (issue #39).
        container = MagicMock()
        container.labels = {"oduflow.team": "2"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.stop_environment(TEST_SETTINGS, TEST_TEAM, "main")
        container.stop.assert_not_called()


class TestStartEnvironment:
    def test_start(self, mock_docker_client):
        db = MagicMock()
        db.status = "running"
        odoo = MagicMock()
        odoo.labels = {"oduflow.team": "1"}

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.start_environment(TEST_SETTINGS, "main", TEST_TEAM)

        assert "oduflow-1-main-odoo" in result["started"]
        odoo.start.assert_called_once()


class TestGetEnvironmentInfo:
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_all_running(self, mock_load_creds, mock_docker_client):
        odoo = MagicMock()
        odoo.status = "running"
        odoo.labels = {
            "oduflow.template": "default",
            "oduflow.repo": "https://github.com/example/repo.git",
            "oduflow.image": "odoo:17.0",
            "oduflow.extra_addons": "{}",
        }
        odoo.attrs = {"NetworkSettings": {"Ports": {}}}
        db = MagicMock()
        db.status = "running"

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        with (
            patch.object(
                env_ops.activity,
                "get_all",
                return_value={
                    "main": {
                        "last_activity": "2026-08-27T10:00:00+00:00",
                        "stopped_at": "2026-08-27T11:00:00+00:00",
                        "stopped_by": "manual",
                    }
                },
            ),
            patch.object(env_ops, "is_protected", return_value=True),
        ):
            result = env_ops.get_environment_info(TEST_SETTINGS, TEST_TEAM, "main")

        assert result["all_running"] is True
        assert result["db"]["name"] == "oduflow-db"
        assert result["db_name"] == "oduflow_1_main"
        assert result["db_user"] == "u_1_main"
        assert result["repo_url"] == "https://github.com/example/repo.git"
        assert result["local_path"] == ""
        assert result["odoo_image"] == "odoo:17.0"
        assert result["template_name"] == "default"
        assert result["last_activity"] == "2026-08-27T10:00:00+00:00"
        assert result["stopped_at"] == "2026-08-27T11:00:00+00:00"
        assert result["stopped_by"] == "manual"
        assert result["protected"] is True

    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_live_mount_path(self, mock_load_creds, mock_docker_client):
        odoo = MagicMock()
        odoo.status = "running"
        odoo.labels = {
            "oduflow.template": "default",
            "oduflow.repo": "/Users/dev/addons",
            "oduflow.local_path": "/Users/dev/addons",
            "oduflow.image": "odoo:17.0",
            "oduflow.extra_addons": "{}",
        }
        odoo.attrs = {"NetworkSettings": {"Ports": {}}}
        db = MagicMock()
        db.status = "running"

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.get_environment_info(TEST_SETTINGS, TEST_TEAM, "main")

        assert result["local_path"] == "/Users/dev/addons"

    def test_unknown_env_raises_not_found(self, tmp_path, mock_docker_client):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path))
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "missing"
        )

        with pytest.raises(NotFoundError, match="'ghost' does not exist"):
            env_ops.get_environment_info(TEST_SETTINGS, team, "ghost")

    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_half", "pg_password": "test-pw"},
    )
    def test_workspace_without_container_still_reported(
        self, mock_load_creds, tmp_path, mock_docker_client
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path))
        os.makedirs(os.path.join(team.workspaces_dir, "half"))
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "missing"
        )

        with (
            patch.object(env_ops.activity, "get_all", return_value={}),
            patch.object(env_ops, "is_protected", return_value=False),
        ):
            result = env_ops.get_environment_info(TEST_SETTINGS, team, "half")

        assert result["odoo"]["status"] == "not found"
        assert result["all_running"] is False


class TestRunDbQuery:
    def test_arbitrary_query_rejects_legacy_db_credentials(
        self, tmp_path, mock_docker_client
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path))
        db_container = MagicMock()
        mock_docker_client.containers.get.return_value = db_container

        with pytest.raises(
            PrerequisiteNotMetError, match="no scoped database credentials"
        ):
            odoo_ops.run_db_query(TEST_SETTINGS, team, "main", "SELECT 1")

        db_container.exec_run.assert_not_called()

    def test_fixed_module_listing_supports_legacy_credentials(
        self, tmp_path, mock_docker_client
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path))
        db_container = MagicMock()
        db_container.exec_run.return_value = (
            0,
            b'name,latest_version\nbase,18.0.1.3\nsale,"18.0,custom"\n',
        )
        mock_docker_client.containers.get.return_value = db_container

        result = odoo_ops.list_installed_module_records(TEST_SETTINGS, team, "main")

        assert result == [
            {"name": "base", "version": "18.0.1.3"},
            {"name": "sale", "version": "18.0,custom"},
        ]
        command = db_container.exec_run.call_args.args[0]
        assert command[:6] == [
            "psql",
            "-U",
            TEST_SETTINGS.db_user,
            "-d",
            "oduflow_1_main",
            "--csv",
        ]
        assert "state = 'installed'" in command[-1]

    @pytest.mark.parametrize("env_name", ["prod-erp", "Prod-erp", "pr.od-erp"])
    def test_fixed_module_listing_rejects_production_aliases(
        self, env_name, mock_docker_client
    ):
        with pytest.raises(ValueError, match="production environment"):
            odoo_ops.list_installed_module_records(TEST_SETTINGS, TEST_TEAM, env_name)

        mock_docker_client.containers.get.assert_not_called()


class TestInstallModules:
    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={
            "exit_code": 0,
            "output": "name,state\nsale,installed\ncrm,installed\n",
        },
    )
    def test_install(self, mock_query, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.install_odoo_modules(
            TEST_SETTINGS, TEST_TEAM, "main", "sale", "crm"
        )

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_1_main" in args
        assert "-i sale,crm" in args

    def test_install_verification_allows_legacy_db_credentials(
        self, tmp_path, mock_docker_client
    ):
        team = TeamSettings(team_id="1", data_dir=str(tmp_path))
        odoo_container = MagicMock()
        odoo_container.exec_run.return_value = (0, b"OK")
        db_container = MagicMock()
        db_container.exec_run.return_value = (
            0,
            b"name,state\nsale,installed\n",
        )

        def get_container(name):
            if name == TEST_SETTINGS.shared_db_container:
                return db_container
            return odoo_container

        mock_docker_client.containers.get.side_effect = get_container

        result = odoo_ops.install_odoo_modules(TEST_SETTINGS, team, "main", "sale")

        assert result["exit_code"] == 0
        query_cmd = db_container.exec_run.call_args.args[0]
        assert query_cmd[:6] == [
            "psql",
            "-U",
            TEST_SETTINGS.db_user,
            "-d",
            "oduflow_1_main",
            "--csv",
        ]

    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={"exit_code": 0, "output": "name,state\n"},
    )
    def test_unknown_module_after_success_exit_is_refused(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (
            0,
            b"WARNING invalid module names, ignored: prt_email_from",
        )
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="not found.*successful installation"):
            odoo_ops.install_odoo_modules(
                TEST_SETTINGS, TEST_TEAM, "main", "prt_email_from"
            )

    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={
            "exit_code": 0,
            "output": "name,state\nwebsite,uninstalled\n",
        },
    )
    def test_uninstalled_module_after_success_exit_is_refused(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, b"install command completed")
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            PrerequisiteNotMetError,
            match=r"website \(uninstalled\).*successful installation",
        ):
            odoo_ops.install_odoo_modules(TEST_SETTINGS, TEST_TEAM, "main", "website")

    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={
            "exit_code": 0,
            "output": "name,state\nsale,installed\ncrm,uninstalled\n",
        },
    )
    def test_partial_install_after_success_exit_is_refused(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, b"install command completed")
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(PrerequisiteNotMetError, match=r"crm \(uninstalled\)"):
            odoo_ops.install_odoo_modules(
                TEST_SETTINGS, TEST_TEAM, "main", "sale", "crm"
            )

    @patch("oduflow.docker_ops.odoo_ops._execute_db_query")
    def test_failed_command_is_not_masked_by_state_verification(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (1, b"manifest parse error")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.install_odoo_modules(
            TEST_SETTINGS, TEST_TEAM, "main", "broken_module"
        )

        assert result["exit_code"] == 1
        assert result["output"] == "manifest parse error"
        mock_query.assert_not_called()


class TestUpgradeModules:
    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={"exit_code": 0, "output": "name,state\nsale,installed\n"},
    )
    def test_upgrade(self, mock_query, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, TEST_TEAM, "main", "sale")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_1_main" in args
        assert "-u sale" in args

    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={"exit_code": 0, "output": "name,state\nwebsite,uninstalled\n"},
    )
    def test_uninstalled_module_is_refused(self, mock_query, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            PrerequisiteNotMetError, match="not installed.*install_odoo_modules"
        ):
            odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, TEST_TEAM, "main", "website")

        container.exec_run.assert_not_called()

    @patch(
        "oduflow.docker_ops.odoo_ops._execute_db_query",
        return_value={"exit_code": 0, "output": "name,state\n"},
    )
    def test_unknown_module_is_refused(self, mock_query, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="unknown.*install_odoo_modules"):
            odoo_ops.upgrade_odoo_modules(
                TEST_SETTINGS, TEST_TEAM, "main", "not_a_module"
            )

        container.exec_run.assert_not_called()

    @patch("oduflow.docker_ops.odoo_ops._execute_db_query")
    def test_all_upgrades_every_installed_module(self, mock_query, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, TEST_TEAM, "main", "all")

        assert result["exit_code"] == 0
        assert result["modules"] == ["all"]
        assert "-u all" in container.exec_run.call_args[0][0]
        # "all" is Odoo's keyword, not a module name: ir_module_module holds no
        # such row, so a state check would reject a legitimate upgrade.
        mock_query.assert_not_called()

    @patch("oduflow.docker_ops.odoo_ops._execute_db_query")
    def test_all_is_refused_next_to_module_names(self, mock_query, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ValueError, match="cannot be combined"):
            odoo_ops.upgrade_odoo_modules(
                TEST_SETTINGS, TEST_TEAM, "main", "all", "sale"
            )

        container.exec_run.assert_not_called()
        mock_query.assert_not_called()


class TestRunEnvironmentTests:
    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run(self, mock_load_creds, mock_docker_client):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.run_environment_tests(
            TEST_SETTINGS, TEST_TEAM, "main", "base"
        )

        assert "All tests passed" in output
        args = container.exec_run.call_args[0][0]
        assert "--db_host=oduflow-db" in args
        assert "--database=oduflow_1_main" in args
        assert "-r u_1_main" in args
        # Use -u (upgrade), not -i: the module is already installed, and -i would be
        # a no-op that never runs the test phase ("0 of 0 tests").
        assert "-u base" in args
        # Tests run via `docker exec` inside the already-running odoo container,
        # which already holds 8069/8072. --no-http is ignored under --test-enable
        # (tests need a live HTTP server), so the test server's ports are moved off
        # the defaults to avoid the bind conflict.
        assert "--http-port 8089" in args
        # Odoo 17 → modern --gevent-port flag.
        assert "--gevent-port 8090" in args
        assert "--workers 0" in args
        # Issue #84: the per-env DB password must never be interpolated onto the
        # odoo CLI (it would show up in the container's `ps`); it is passed via
        # the PGPASSWORD env var instead. The username (-r) is not secret.
        assert "test-pw" not in args
        assert "-w" not in args.split()
        assert container.exec_run.call_args.kwargs["environment"] == {
            "PGPASSWORD": "test-pw"
        }

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_odoo_15_uses_longpolling_port(
        self, mock_load_creds, mock_docker_client
    ):
        # Odoo 15.0 has no --gevent-port (introduced in 16.0); it must get the
        # legacy --longpolling-port instead, parsed from the image tag.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:15.0"}
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        args = container.exec_run.call_args[0][0]
        assert "--longpolling-port 8090" in args
        assert "--gevent-port" not in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_odoo_19_uses_gevent_port(self, mock_load_creds, mock_docker_client):
        # Odoo 18.0 removed --longpolling-port outright (16.0/17.0 still took it
        # as a deprecated alias), so on 18+ the legacy flag is a hard failure:
        # "server: error: no such option: --longpolling-port".
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:19.0"}
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        args = container.exec_run.call_args[0][0]
        assert "--gevent-port 8090" in args
        assert "--longpolling-port" not in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_custom_repository_reads_version_from_the_tag(
        self, mock_load_creds, mock_docker_client
    ):
        # A private registry with a port and a custom repository name still
        # carries the version in the tag; no live probe should be needed.
        container = MagicMock()
        container.labels = {
            TEST_SETTINGS.image_label: "registry.example:5000/acme/odoo-ee:19.0-custom"
        }
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        assert container.exec_run.call_count == 1  # no `odoo --version` probe
        args = container.exec_run.call_args[0][0]
        assert "--gevent-port 8090" in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_noisy_version_probe_does_not_downgrade_the_flag(
        self, mock_load_creds, mock_docker_client
    ):
        # exec_run merges stderr into stdout, so a custom image that warns before
        # printing the banner must not be read as Odoo 1 (which would select the
        # removed --longpolling-port on a 19.0 environment).
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "oduist/customer_odoo"}
        container.exec_run.side_effect = [
            (
                0,
                b"urllib3 v2 only supports OpenSSL 1.1.1+\nOdoo Server 19.0-20260908\n",
            ),
            (0, b"All tests passed"),
        ]
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        test_cmd = container.exec_run.call_args_list[1][0][0]
        assert "--gevent-port 8090" in test_cmd
        assert "--longpolling-port" not in test_cmd

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_failed_version_probe_falls_back_to_gevent_port(
        self, mock_load_creds, mock_docker_client
    ):
        # A probe that errors out prints a runtime message, not a version banner.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "oduist/customer_odoo"}
        container.exec_run.side_effect = [
            (127, b'exec: "odoo": executable file not found in $PATH: 1.0\n'),
            (0, b"All tests passed"),
        ]
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        test_cmd = container.exec_run.call_args_list[1][0][0]
        assert "--gevent-port 8090" in test_cmd
        assert "--longpolling-port" not in test_cmd

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_custom_image_detects_version_via_binary(
        self, mock_load_creds, mock_docker_client
    ):
        # Custom-tagged image carries no version in the tag, so the major version
        # is resolved from `odoo --version` on the live container.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "oduist/customer_odoo"}
        container.exec_run.side_effect = [
            (0, b"Odoo Server 15.0\n"),  # odoo --version probe
            (0, b"All tests passed"),  # actual test run
        ]
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        assert container.exec_run.call_args_list[0][0][0] == "odoo --version"
        test_cmd = container.exec_run.call_args_list[1][0][0]
        assert "--longpolling-port 8090" in test_cmd
        assert "--gevent-port" not in test_cmd

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_test_tags_narrow_the_run(self, mock_load_creds, mock_docker_client):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        container.exec_run.return_value = (0, b"1 test")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            "sale",
            test_tags="/sale:TestInvoice.test_total",
        )

        args = container.exec_run.call_args[0][0]
        # --test-tags=VALUE, not a separate token: an exclusion expression like
        # '-slow' would otherwise be parsed as a CLI flag.
        assert "--test-tags=/sale:TestInvoice.test_total" in args
        assert "-u sale" in args  # upgrade still on by default

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_upgrade_false_drops_u_and_scopes_by_module(
        self, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        container.exec_run.return_value = (0, b"ok")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(
            TEST_SETTINGS, TEST_TEAM, "main", "sale,crm", upgrade=False
        )

        args = container.exec_run.call_args[0][0]
        assert "-u " not in args
        # Without -u, Odoo would otherwise sweep every installed module's
        # post-install tests; the module filter keeps the run scoped.
        assert "--test-tags=/sale,/crm" in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_explicit_tags_win_over_derived_ones(
        self, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        container.exec_run.return_value = (0, b"ok")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            "sale",
            test_tags="/sale:TestX",
            upgrade=False,
        )

        args = container.exec_run.call_args[0][0]
        assert "--test-tags=/sale:TestX" in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_upgrade_false_scopes_exclusion_to_requested_modules(
        self, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        container.exec_run.return_value = (0, b"ok")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            "sale,crm",
            test_tags="-slow",
            upgrade=False,
        )

        args = container.exec_run.call_args[0][0]
        assert "--test-tags=/sale,/crm,-slow" in args

    def test_upgrade_false_rejects_unscoped_positive_tag(self, mock_docker_client):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ValueError, match="must include a module scope"):
            odoo_ops.run_environment_tests(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "sale",
                test_tags="slow",
                upgrade=False,
            )

    def test_upgrade_false_rejects_tag_for_another_module(self, mock_docker_client):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ValueError, match="outside the requested modules"):
            odoo_ops.run_environment_tests(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "sale",
                test_tags="/stock:TestMove",
                upgrade=False,
            )

    def test_empty_modules_rejected(self, mock_docker_client):
        # A bare `-u` (no value) with upgrade=True, and an unscoped sweep over
        # every installed module's post-install tests with upgrade=False.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ValueError, match="modules is required"):
            odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "")

    def test_test_tags_with_whitespace_rejected(self, mock_docker_client):
        # Same argument-injection guard as module names: exec_run splits the
        # command with shlex, so a space would smuggle a second CLI flag.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(ValueError, match="Invalid test_tags"):
            odoo_ops.run_environment_tests(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "sale",
                test_tags="/sale --load=web,base",
            )


class TestRunCommandInEnvironment:
    """Docker exec has no shell — pipes and redirections would reach the program
    as literal argv entries. Commands go through `sh -c` unless asked otherwise."""

    def test_shell_by_default(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"2\n")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_command_in_environment(
            TEST_SETTINGS, TEST_TEAM, "main", "ls /mnt/extra-addons | wc -l"
        )

        assert container.exec_run.call_args[0][0] == [
            "sh",
            "-c",
            "ls /mnt/extra-addons | wc -l",
        ]
        assert container.exec_run.call_args.kwargs["user"] == "odoo"

    def test_shell_false_passes_the_raw_command(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_command_in_environment(
            TEST_SETTINGS, TEST_TEAM, "main", "odoo --version", shell=False
        )

        assert container.exec_run.call_args[0][0] == "odoo --version"


class TestRunOdooShell:
    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_password_passed_via_pgpassword_env(
        self, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, b"shell output")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.run_odoo_shell(TEST_SETTINGS, TEST_TEAM, "main", "print(1)")

        assert result["exit_code"] == 0
        # First exec_run is the `sh -c "odoo shell ..."` invocation.
        shell_call = container.exec_run.call_args_list[0]
        sh_argv = shell_call[0][0]
        assert sh_argv[:2] == ["sh", "-c"]
        cmd = sh_argv[2]
        assert "-r u_1_main" in cmd
        assert "--database=oduflow_1_main" in cmd
        # Issue #84: password goes through PGPASSWORD, never onto the odoo CLI.
        assert "test-pw" not in cmd
        assert "-w" not in cmd.split()
        assert shell_call.kwargs["user"] == "odoo"
        assert shell_call.kwargs["environment"] == {"PGPASSWORD": "test-pw"}


class TestConnectAsUserScript:
    def test_numeric_user_lookup_preserves_active_filter(self):
        script = odoo_ops._build_connect_as_user_script("7")

        assert "Users.search([('id', '=', int(_sel))], limit=1)" in script
        assert "Users.browse(int(_sel)).exists()" not in script

    def test_session_context_uses_target_user_env(self):
        script = odoo_ops._build_connect_as_user_script("jane@acme.com")

        assert "user_env = env(user=u.id)" in script
        assert (
            "user_context = dict(user_env['res.users'].context_get() or {})" in script
        )
        assert "user_context['uid'] = user.id" in script
        assert "'context': user_context" in script
        assert "'context': dict(u.context_get())" not in script


class TestConnectAsUser:
    _SHELL_OUTPUT = (
        b"__ODUFLOW_SID__session-id__END__\n"
        b"__ODUFLOW_LOGIN__jane@acme.com__END__\n"
        b"__ODUFLOW_UID__7__END__\n"
    )

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    @patch(
        "oduflow.docker_ops.env_ops.get_env_base_url",
        return_value=("https://main.example.com", "main.example.com"),
    )
    def test_each_invocation_uses_one_distinct_path_everywhere(
        self, mock_base_url, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, self._SHELL_OUTPUT)
        mock_docker_client.containers.get.return_value = container

        archive_members = []

        def capture_archive(_path, stream):
            with tarfile.open(fileobj=stream, mode="r") as archive:
                archive_members.append(archive.getnames())

        container.put_archive.side_effect = capture_archive

        odoo_ops.connect_as_user(TEST_SETTINGS, TEST_TEAM, "main", "jane@acme.com")
        odoo_ops.connect_as_user(TEST_SETTINGS, TEST_TEAM, "main", "jane@acme.com")

        shell_calls = [
            call
            for call in container.exec_run.call_args_list
            if call.args[0][:2] == ["sh", "-c"]
        ]
        shell_paths = [call.args[0][2].rsplit("< ", 1)[1] for call in shell_calls]
        cleanup_paths = [
            call.args[0][2]
            for call in container.exec_run.call_args_list
            if call.args[0][:2] == ["rm", "-f"]
        ]

        assert len(set(shell_paths)) == 2
        assert archive_members == [
            [shell_paths[0].removeprefix("/tmp/")],
            [shell_paths[1].removeprefix("/tmp/")],
        ]
        assert cleanup_paths == shell_paths
        assert all(call.kwargs["user"] == "odoo" for call in shell_calls)

    @patch("oduflow.docker_ops.odoo_ops.secrets.token_hex", return_value="a1b2c3")
    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_cleanup_targets_invocation_file_after_exec_raises(
        self, mock_load_creds, mock_token_hex, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.side_effect = [RuntimeError("exec failed"), (0, b"")]
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(RuntimeError, match="exec failed"):
            odoo_ops.connect_as_user(TEST_SETTINGS, TEST_TEAM, "main", "jane@acme.com")

        assert container.exec_run.call_args_list[0].args[0][0:2] == ["sh", "-c"]
        assert container.exec_run.call_args_list[1].args[0] == [
            "rm",
            "-f",
            "/tmp/_oduflow_connect_script_a1b2c3.py",
        ]
        assert container.exec_run.call_count == 2


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.team": "1"}
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.get_environment_logs(
            TEST_SETTINGS, "main", 50, team=TEST_TEAM
        )

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)


class TestApplyActionsConf:
    """A changed ``.oduflow/odoo.conf`` must be reconstructed before a restart.

    PR #69 only triggered a plain restart, which reuses the stale
    ``/etc/odoo/odoo.conf`` copy and never picks up the new config.
    """

    @staticmethod
    def _client_with_container():
        client = MagicMock()
        container = MagicMock()
        client.containers.get.return_value = container
        return client, container

    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    def test_restart_reapplies_conf_when_config_changed(self, mock_reapply):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=True,
            changed_files=[".oduflow/odoo.conf"],
            config_changed=True,
        )
        mock_reapply.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        container.restart.assert_called_once()
        assert result["action"] == "restart"
        assert "odoo.conf" in result["message"]

    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf")
    def test_restart_skips_conf_when_only_python(self, mock_reapply):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=True,
            changed_files=["sale/models/sale.py"],
            config_changed=False,
        )
        mock_reapply.assert_not_called()
        container.restart.assert_called_once()
        assert result["message"] == "Container restarted."

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 0, "output": "upgraded"},
    )
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    def test_upgrade_also_reapplies_conf(self, mock_reapply, mock_upgrade):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=["sale"],
            do_restart=False,
            changed_files=[".oduflow/odoo.conf", "sale/security/groups.xml"],
            config_changed=True,
        )
        mock_upgrade.assert_called_once()
        mock_reapply.assert_called_once()
        container.restart.assert_called_once()
        assert result["action"] == "upgrade"
        assert "Reapplied odoo.conf." in result["message"]

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        side_effect=PrerequisiteNotMetError("module is not installed"),
    )
    def test_rejected_upgrade_does_not_restart(self, mock_upgrade):
        client, container = self._client_with_container()

        with pytest.raises(PrerequisiteNotMetError, match="not installed"):
            env_ops._apply_actions(
                client,
                TEST_SETTINGS,
                TEST_TEAM,
                "feature/x",
                "oduflow-1-feature-x-odoo",
                to_install=[],
                to_upgrade=["website"],
                do_restart=False,
                changed_files=[],
            )

        container.restart.assert_not_called()

    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        side_effect=NotFoundError("module was not found after installation"),
    )
    def test_rejected_install_does_not_restart(self, mock_install):
        client, container = self._client_with_container()

        with pytest.raises(NotFoundError, match="not found after installation"):
            env_ops._apply_actions(
                client,
                TEST_SETTINGS,
                TEST_TEAM,
                "feature/x",
                "oduflow-1-feature-x-odoo",
                to_install=["missing_module"],
                to_upgrade=[],
                do_restart=False,
                changed_files=[],
            )

        container.restart.assert_not_called()


class TestApplyActionsExitCode:
    """A failed install must not be masked by a later successful upgrade.

    ``exit_code`` is the whole apply's status: the compact ``pull_and_apply``
    response derives "Status: ok/failed" from it and keeps the log server-side,
    and the live-mount snapshot is only written when it is 0.
    """

    @staticmethod
    def _client_with_container():
        client = MagicMock()
        container = MagicMock()
        client.containers.get.return_value = container
        return client, container

    def _apply(self):
        client, _ = self._client_with_container()
        return env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=["new_module"],
            to_upgrade=["sale"],
            do_restart=False,
            changed_files=["new_module/__manifest__.py", "sale/models/sale.py"],
        )

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 0, "output": "upgraded"},
    )
    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 1, "output": "install traceback"},
    )
    def test_failed_install_survives_successful_upgrade(self, mock_install, mock_up):
        result = self._apply()

        assert result["exit_code"] == 1
        assert "install traceback" in result["output"]
        # The step behind exit_code, with its own output, not the upgrade's.
        assert result["failed_step"] == "install"
        assert result["failed_step_output"] == "install traceback"

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 2, "output": "upgrade traceback"},
    )
    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 0, "output": "installed"},
    )
    def test_failed_upgrade_is_reported(self, mock_install, mock_up):
        result = self._apply()

        assert result["exit_code"] == 2
        assert result["failed_step"] == "upgrade"
        assert result["failed_step_output"] == "upgrade traceback"

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 0, "output": "upgraded"},
    )
    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 0, "output": "installed"},
    )
    def test_all_successful_reports_zero(self, mock_install, mock_up):
        result = self._apply()

        assert result["exit_code"] == 0
        assert "failed_step" not in result
        assert "dependencies_exit_code" not in result


class TestApplyActionsDeps:
    """A changed dependency descriptor must reinstall apt/pip deps into the
    running container and restart it — not fall through to an XML/JS refresh."""

    @staticmethod
    def _client_with_container():
        client = MagicMock()
        container = MagicMock()
        client.containers.get.return_value = container
        return client, container

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(True, "[PIP] Requirements installed successfully:\nok"),
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_apt_packages",
        return_value="",
    )
    def test_deps_only_reinstalls_and_restarts(self, mock_apt, mock_pip):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=False,
            changed_files=["requirements.txt"],
            dep_units=[("/repo", "/mnt/extra-addons")],
        )
        # pip must run without restarting itself; the single restart is below.
        mock_pip.assert_called_once()
        assert mock_pip.call_args.kwargs["restart"] is False
        container.restart.assert_called_once()
        assert result["action"] == "restart"
        assert "[PIP]" in result["output"]
        assert "Reinstalled dependencies." in result["message"]
        assert result["exit_code"] == 0

    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 0, "output": "installed"},
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(True, "[PIP] Requirements installed successfully:\nok"),
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_apt_packages",
        return_value="",
    )
    def test_deps_installed_before_module_install(
        self, mock_apt, mock_pip, mock_install
    ):
        client, container = self._client_with_container()
        # Route both patched calls through one parent so ordering is observable.
        parent = MagicMock()
        parent.attach_mock(mock_pip, "pip")
        parent.attach_mock(mock_install, "install")

        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=["sale"],
            to_upgrade=[],
            do_restart=False,
            changed_files=["requirements.txt", "sale/__manifest__.py"],
            dep_units=[("/repo", "/mnt/extra-addons")],
        )
        order = [name for name, _, _ in parent.mock_calls]
        assert order.index("pip") < order.index("install")
        container.restart.assert_called_once()
        assert result["action"] == "install"
        assert "[PIP]" in result["output"]
        assert "installed" in result["output"]
        assert result["message"].startswith("Reinstalled dependencies.")

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, "[PIP] install FAILED (exit 1):\nboom"),
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_apt_packages",
        return_value="",
    )
    def test_deps_pip_failure_surfaces_without_crashing(self, mock_apt, mock_pip):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=False,
            changed_files=["requirements.txt"],
            dep_units=[("/repo", "/mnt/extra-addons")],
        )
        assert result["action"] == "error"
        assert "FAILED" in result["output"]
        assert result["exit_code"] == 1
        assert result["failed_step"] == "dependencies"
        assert result["dependencies_exit_code"] == 1
        assert "boom" in result["failed_step_output"]
        container.restart.assert_not_called()

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 255, "output": "ModuleNotFoundError: boto3"},
    )
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        side_effect=[
            (True, "[PIP] Requirements installed successfully:\nfine"),
            (True, "[PIP] Requirements installed successfully:\nalso fine"),
        ],
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_upgrade_failure_keeps_the_dependency_code_visible(
        self, mock_apt, mock_pip, mock_upgrade
    ):
        # The upgrade's code is the apply's status; the reinstall that ran
        # before it stays visible next to it, so a failed module command can
        # be told apart from one that failed because a dependency did not
        # install.
        client, _ = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=["sale"],
            do_restart=False,
            changed_files=["requirements.txt", "sale/models/sale.py"],
            dep_units=[("/repo", "/mnt/extra-addons"), ("/x", "/mnt/extra-addons-x")],
        )
        assert result["exit_code"] == 255
        assert result["failed_step"] == "upgrade"
        assert result["failed_step_output"] == "ModuleNotFoundError: boto3"
        assert result["dependencies_exit_code"] == 0
        assert "Module upgrade FAILED (exit 255): sale." in result["message"]

    @patch("oduflow.docker_ops.odoo_ops.upgrade_odoo_modules")
    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        return_value=(False, "[PIP] install FAILED (exit 1):\nno boto3 for you"),
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_dependency_failure_stops_module_actions(
        self, mock_apt, mock_pip, mock_upgrade
    ):
        # A failed reinstall stops the apply before any module command: the
        # pending plan is returned for a retry instead of running an upgrade
        # against a container that is missing its dependencies.
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=["sale"],
            do_restart=False,
            changed_files=["requirements.txt", "sale/models/sale.py"],
            dep_units=[("/repo", "/mnt/extra-addons")],
        )
        mock_upgrade.assert_not_called()
        container.restart.assert_not_called()
        assert result["action"] == "error"
        assert result["exit_code"] == 1
        assert result["failed_step"] == "dependencies"
        assert result["pending_actions"]["upgrade"] == ["sale"]

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements",
        side_effect=[
            (True, "[PIP] Requirements installed successfully:\nfine"),
            (False, "[PIP] install FAILED (exit 1):\nERROR: No matching dist"),
        ],
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    def test_dependency_failure_output_is_the_failed_install_only(
        self, mock_apt, mock_pip
    ):
        client, _ = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=False,
            changed_files=["requirements.txt"],
            dep_units=[("/repo", "/mnt/extra-addons"), ("/x", "/mnt/extra-addons-x")],
        )
        assert result["failed_step"] == "dependencies"
        assert "No matching dist" in result["failed_step_output"]
        assert "fine" not in result["failed_step_output"]

    @patch("oduflow.docker_ops.env_ops._install_pip_requirements")
    @patch("oduflow.docker_ops.env_ops._install_apt_packages")
    def test_deps_not_changed_skips_reinstall(self, mock_apt, mock_pip):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-1-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=False,
            changed_files=["sale/views/sale_order.xml"],
            do_refresh=True,
            dep_units=[],
        )
        mock_apt.assert_not_called()
        mock_pip.assert_not_called()
        container.restart.assert_not_called()
        assert result["action"] == "refresh"


class TestReapplyOdooConf:
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-x/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True)
    def test_uses_repo_conf_and_copies(self, mock_isfile, mock_gen, mock_copy):
        container = MagicMock()
        container.labels = {}
        applied = env_ops._reapply_odoo_conf(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        assert applied is True
        base_arg, _generated, extra_arg, main_addons = mock_gen.call_args[0]
        assert base_arg.endswith("/.oduflow/odoo.conf")
        assert extra_arg == []
        assert main_addons == "/mnt/extra-addons"
        mock_copy.assert_called_once()
        assert mock_copy.call_args[0][0] is container
        assert mock_copy.call_args[0][2] == "/etc/odoo"

    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-x/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True)
    def test_merges_extra_addons_paths(self, mock_isfile, mock_gen, mock_copy):
        container = MagicMock()
        container.labels = {"oduflow.extra_addons": json.dumps({"enterprise": "17.0"})}
        env_ops._reapply_odoo_conf(TEST_SETTINGS, TEST_TEAM, "feature/x", container)
        _base, _generated, extra_arg, _main = mock_gen.call_args[0]
        assert extra_arg == ["/mnt/extra-addons-enterprise"]

    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    def test_pins_listen_interface_over_repo_conf(self, mock_copy, tmp_path):
        # Odoo 20 binds 127.0.0.1 when http_interface is unset; a loopback
        # value from the repo conf is just as unreachable for Traefik.
        repo = tmp_path / "repo"
        (repo / ".oduflow").mkdir(parents=True)
        (repo / ".oduflow" / "odoo.conf").write_text(
            "[options]\naddons_path = /mnt/extra-addons\nhttp_interface = 127.0.0.1\n"
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        with patch(
            "oduflow.docker_ops.env_ops.get_workspace_path",
            return_value=str(workspace),
        ):
            assert env_ops._reapply_odoo_conf(
                TEST_SETTINGS, TEST_TEAM, "feature/x", container
            )
        import configparser

        cp = configparser.RawConfigParser()
        cp.read(workspace / "odoo.conf")
        assert cp.get("options", "http_interface") == "0.0.0.0"
        mock_copy.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    def test_falls_back_to_the_bundled_conf(self, mock_copy, tmp_path):
        # Neither a repo nor a team conf: the bundled odoo.conf is the base,
        # so a conf (with the pinned interface) is always applied.
        team = dataclasses.replace(TEST_TEAM, data_dir=str(tmp_path / "team"))
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        container = MagicMock()
        container.labels = {"oduflow.local_path": str(tmp_path / "repo")}
        with patch(
            "oduflow.docker_ops.env_ops.get_workspace_path",
            return_value=str(workspace),
        ):
            assert env_ops._reapply_odoo_conf(
                TEST_SETTINGS, team, "feature/x", container
            )
        import configparser

        cp = configparser.RawConfigParser()
        cp.read(workspace / "odoo.conf")
        assert cp.get("options", "max_cron_threads") == "0"
        assert cp.get("options", "http_interface") == "0.0.0.0"
        mock_copy.assert_called_once()


class TestAgentContainer:
    """The agent is a SINGLE per-team container with persistent HOME/workspace
    volumes; per-env checkouts are added/removed via `docker exec`. It is
    opt-in per team (a hosting feature) and configured statically in
    oduflow.toml. Every environment runs as its own uid inside it. See
    specs/0029-agent-console-and-chat.md and
    specs/0079-per-environment-agent-identities.md."""

    @pytest.fixture(autouse=True)
    def _isolated_data_dir(self, tmp_path):
        # The per-environment uid registry lives in the team data dir.
        self._data_dir = str(tmp_path)

    def _team(self, **kw):
        base = dict(
            team_id="1",
            data_dir=self._data_dir,
            auth_token="tok",
            agent_enabled=True,
        )
        base.update(kw)
        return TeamSettings(**base)

    def _settings(self, team=None, **kw):
        team = team or self._team()
        base = dict(
            base_data_dir="/tmp/flow-test",
            etc_dir="/tmp/flow-test/etc",
            port=8000,
            agent_image=DEFAULT_AGENT_IMAGE,
            teams={team.team_id: team},
        )
        base.update(kw)
        return Settings(**base)

    def test_agent_mcp_url_uses_public_team_hostname_in_traefik_mode(self):
        team = self._team(hostname="mirageflow.ca")
        settings = self._settings(team=team, routing_mode="traefik")

        assert (
            env_ops.get_agent_mcp_url(settings, team, "feature/x")
            == "https://mirageflow.ca/mcp/feature/x"
        )

    def test_agent_mcp_url_falls_back_to_host_gateway_in_local_port_mode(self):
        team = self._team()
        settings = self._settings(team=team)

        assert (
            env_ops.get_agent_mcp_url(settings, team, "feature/x")
            == "http://host.docker.internal:8000/mcp/feature/x"
        )

    def test_refresh_agent_mcp_config_uses_current_public_url(self):
        team = self._team(hostname="mirageflow.ca")
        settings = self._settings(team=team, routing_mode="traefik")
        uid = agent_uids.allocate_uid(team, "feature/x")
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        env_ops.refresh_agent_mcp_config(container, settings, team, "feature/x", uid)

        cmd = container.exec_run.call_args.args[0]
        assert cmd[:2] == ["sh", "-c"]
        assert cmd[4] == "/workspace/feature-x/.mcp.json"
        assert cmd[5] == "https://mirageflow.ca/mcp/feature/x"
        assert "ODUFLOW_MCP_TOKEN" in cmd[2]
        assert "agent_browser" in cmd[2]
        # Only the environment's own uid can write its checkout.
        kwargs = container.exec_run.call_args.kwargs
        assert kwargs["user"] == f"{uid}:{uid}"
        assert kwargs["environment"]["HOME"] == f"/home/agent/envs/{uid}"

    def test_exec_identity_is_the_environments_own_uid_home_and_tmpdir(self):
        user, env = env_ops.agent_exec_identity(20001)

        assert user == "20001:20001"
        assert env == {
            "HOME": "/home/agent/envs/20001",
            "CLAUDE_CONFIG_DIR": "/home/agent/envs/20001/.claude",
            "CODEX_HOME": "/home/agent/envs/20001/.codex",
            "TMPDIR": "/tmp/user/20001",
            "DISABLE_AUTOUPDATER": "1",
        }

    def test_session_command_runs_with_a_private_umask(self):
        assert env_ops.agent_session_command(["claude", "--model", "x y"]) == [
            "sh",
            "-c",
            'umask 077; exec "$@"',
            "sh",
            "claude",
            "--model",
            "x y",
        ]

    def test_env_uid_checks_account_home_tmpdir_and_checkout_as_the_env(self):
        team = self._team()
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        # No uid yet (an environment from before per-environment identities).
        assert env_ops.agent_env_uid(container, team, "feature/x") is None
        container.exec_run.assert_not_called()

        uid = agent_uids.allocate_uid(team, "feature/x")
        assert env_ops.agent_env_uid(container, team, "feature/x") == uid
        cmd = container.exec_run.call_args.args[0]
        assert cmd[-1] == "/workspace/feature-x"
        assert "getent passwd" in cmd[2]
        assert '[ -w "$TMPDIR" ]' in cmd[2]
        assert container.exec_run.call_args.kwargs["user"] == f"{uid}:{uid}"
        assert (
            container.exec_run.call_args.kwargs["environment"]["TMPDIR"]
            == f"/tmp/user/{uid}"
        )

        container.exec_run.return_value = (1, b"")
        assert env_ops.agent_env_uid(container, team, "feature/x") is None

    def test_refresh_agent_mcp_config_renames_server_and_follows_brand(self, tmp_path):
        import json
        import shutil
        import subprocess

        if not (shutil.which("jq") and shutil.which("git")):
            pytest.skip("jq and git are required to run the refresh script")
        team = self._team(hostname="mirageflow.ca")
        settings = self._settings(team=team, routing_mode="traefik")
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        env_ops.refresh_agent_mcp_config(container, settings, team, "feature/x", 20000)
        cmd = container.exec_run.call_args.args[0]
        assert cmd[6:] == ["oduflow", "development", "oduflow"]

        checkout = tmp_path / "feature-x"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "config", "user.email", "dev@oduflow.local"],
            check=True,
        )
        config = checkout / ".mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "oduflow": {"type": "http", "url": "http://old"},
                        "custom": {"command": "custom-mcp"},
                    }
                }
            )
        )
        script_args = [*cmd[:4], str(config), cmd[5], "acmeflow", *cmd[7:]]
        subprocess.run(script_args, check=True)

        servers = json.loads(config.read_text())["mcpServers"]
        # The legacy entry is dropped; Agent Browser is (re)added alongside.
        assert set(servers) == {"agent_browser", "custom", "development"}
        assert servers["development"]["url"] == "https://mirageflow.ca/mcp/feature/x"
        email = subprocess.run(
            ["git", "-C", str(checkout), "config", "user.email"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert email == "dev@acmeflow.local"

    def test_disabled_creates_nothing(self, mock_docker_client):
        team = self._team(agent_enabled=False)
        env_ops._ensure_agent_container(
            mock_docker_client, self._settings(team=team), team
        )
        mock_docker_client.containers.run.assert_not_called()
        mock_docker_client.volumes.create.assert_not_called()

    def test_claude_auth_mode_and_credential_normalization(self, monkeypatch):
        for key in env_ops._AGENT_PROVIDER_CREDENTIALS:
            monkeypatch.delenv(key, raising=False)

        interactive_team = self._team()
        interactive_settings = self._settings(team=interactive_team)
        assert (
            env_ops._claude_auth_mode(interactive_settings, interactive_team)
            == "interactive"
        )

        api_team = self._team(
            agent_env={
                "ANTHROPIC_API_KEY": "  sk-ant-api  ",
                "OPENCODE_API_KEY": "  sk-open  ",
                "MY_FLAG": "  keep  ",
            }
        )
        api_settings = self._settings(team=api_team)
        api_env = env_ops._agent_env_vars(api_settings, api_team)
        assert api_env["ANTHROPIC_API_KEY"] == "sk-ant-api"
        assert api_env["OPENCODE_API_KEY"] == "sk-open"
        assert api_env["MY_FLAG"] == "  keep  "
        assert env_ops._claude_auth_mode(api_settings, api_team) == "api_key"

        token_team = self._team(
            agent_env={
                "CLAUDE_CODE_OAUTH_TOKEN": "  sk-ant-oat  ",
                "ANTHROPIC_API_KEY": "  ignored-key  ",
            }
        )
        token_settings = self._settings(team=token_team)
        token_env = env_ops._agent_env_vars(token_settings, token_team)
        assert token_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat"
        assert "ANTHROPIC_API_KEY" not in token_env
        assert env_ops._claude_auth_mode(token_settings, token_team) == "setup_token"

    def test_blank_team_credential_masks_server_value(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "server-token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team(
            agent_env={
                "CLAUDE_CODE_OAUTH_TOKEN": "  ",
                "ANTHROPIC_API_KEY": " team-key ",
            }
        )
        settings = self._settings(team=team)

        env = env_ops._agent_env_vars(settings, team)

        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["ANTHROPIC_API_KEY"] == "team-key"
        assert env_ops._claude_auth_mode(settings, team) == "api_key"

    def test_multi_team_ignores_server_claude_credentials(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
        team1 = self._team()
        team2 = TeamSettings(team_id="2", data_dir="/tmp/flow-test-2")
        settings = Settings(
            base_data_dir="/tmp/flow-test",
            teams={"1": team1, "2": team2},
        )

        assert env_ops._agent_env_vars(settings, team1) == {}
        assert env_ops._claude_auth_mode(settings, team1) == "interactive"

    def test_codex_auth_mode(self, monkeypatch):
        for key in env_ops._AGENT_PROVIDER_CREDENTIALS:
            monkeypatch.delenv(key, raising=False)

        team = self._team()
        assert env_ops._codex_auth_mode(self._settings(team=team), team) == (
            "interactive"
        )

        keyed = self._team(agent_env={"OPENAI_API_KEY": "  sk-oai  "})
        assert env_ops._codex_auth_mode(self._settings(team=keyed), keyed) == (
            "api_key"
        )

        # A blank team value masks a server key, as for the container env.
        monkeypatch.setenv("OPENAI_API_KEY", "server-key")
        assert env_ops._codex_auth_mode(self._settings(team=team), team) == "api_key"
        blank = self._team(agent_env={"OPENAI_API_KEY": "  "})
        assert env_ops._codex_auth_mode(self._settings(team=blank), blank) == (
            "interactive"
        )

    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True)
    def test_ensure_creates_single_container_with_volumes(
        self, mock_isfile, monkeypatch, mock_docker_client
    ):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
        # No existing volumes, no existing container.
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        team = self._team()
        s = self._settings(team=team)
        created_container = mock_docker_client.containers.run.return_value
        created_container.exec_run.return_value = (0, b"")
        env_ops._ensure_agent_container(mock_docker_client, s, team)

        # Both persistent volumes created.
        created = {c.args[0] for c in mock_docker_client.volumes.create.call_args_list}
        assert "oduflow-1-agent-home" in created
        assert "oduflow-1-agent-workspace" in created

        # A short-lived root init migrates/copies persistent data; only the
        # second call is the long-lived, unprivileged agent container.
        assert mock_docker_client.containers.run.call_count == 2
        init_kwargs = mock_docker_client.containers.run.call_args_list[0].kwargs
        assert init_kwargs["user"] == "root"
        assert init_kwargs["network_disabled"] is True
        assert init_kwargs["remove"] is True
        assert "missing required user 'agent'" in init_kwargs["command"][2]
        assert init_kwargs["volumes"]["oduflow-1-agent-home"]["bind"] == ("/home/agent")
        assert any(
            v["bind"] == "/run/oduflow/git-credentials"
            for v in init_kwargs["volumes"].values()
        )

        kwargs = mock_docker_client.containers.run.call_args_list[1].kwargs
        assert kwargs["name"] == "oduflow-1-agent"
        assert kwargs["user"] == "agent"
        # Tenant isolation: the agent joins the team network, not a shared one.
        assert kwargs["network"] == "oduflow-1-net"
        assert kwargs["labels"]["oduflow.agent"] == "true"
        assert kwargs["labels"][s.team_label] == "1"
        # Config-as-source-of-truth: the injected config is fingerprinted so a
        # later ensure can detect drift and recreate.
        assert kwargs["labels"]["oduflow.agent_config_hash"]
        # Team-wide: no per-env branch label.
        assert s.branch_label not in kwargs["labels"]
        assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
        assert kwargs["shm_size"] == "1g"
        # A real PID 1 reaps ended sessions and killed environments' processes.
        assert kwargs["init"] is True
        # Accounts live in the container layer: a fresh container gets every
        # environment's identity back before any session starts (as root).
        created_container.exec_run.assert_called_once()
        assert created_container.exec_run.call_args.args[0][:2] == [
            "/usr/local/bin/env-user.sh",
            "sync",
        ]
        assert created_container.exec_run.call_args.kwargs["user"] == "root"
        # An image without per-environment identities is refused up front.
        assert "env-user.sh" in init_kwargs["command"][2]
        vols = kwargs["volumes"]
        assert vols["oduflow-1-agent-home"]["bind"] == "/home/agent"
        assert vols["oduflow-1-agent-workspace"]["bind"] == "/workspace"
        assert not any(
            v["bind"] == "/run/oduflow/git-credentials" for v in vols.values()
        )
        env = kwargs["environment"]
        # The team auth_token must never enter the container: any console
        # session could read it. Sessions get SCOPED per-env tokens via their
        # own exec env instead (ADR 0028).
        assert "ODUFLOW_MCP_TOKEN" not in env
        assert "ODUFLOW_MCP_URL" not in env
        # Subscription wins; the API key must not be set alongside it.
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sub-token"
        assert "ANTHROPIC_API_KEY" not in env
        assert env["OPENAI_API_KEY"] == "oai-key"

    def _existing_container(self, settings, team, monkeypatch):
        """A mocked running container carrying the CURRENT config hash."""
        env = dict(env_ops._agent_env_vars(settings, team))
        existing = MagicMock()
        existing.status = "running"
        existing.labels = {
            "oduflow.agent_config_hash": env_ops._agent_config_hash(
                env_ops._agent_container_config(settings, team, env),
                os.path.isfile(team.git_credentials_file()),
            )
        }
        return existing

    def test_ensure_idempotent_when_config_unchanged(
        self, monkeypatch, mock_docker_client
    ):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team()
        s = self._settings(team=team)
        mock_docker_client.volumes.get.return_value = MagicMock()  # volumes exist
        existing = self._existing_container(s, team, monkeypatch)
        mock_docker_client.containers.get.return_value = existing

        env_ops._ensure_agent_container(mock_docker_client, s, team)

        mock_docker_client.containers.run.assert_not_called()
        existing.remove.assert_not_called()
        existing.start.assert_not_called()

    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    def test_ensure_recreates_on_config_drift(
        self, mock_isfile, monkeypatch, mock_docker_client
    ):
        # The container was created with different env (stale hash label) —
        # e.g. the operator edited [team.X.agent_env] and restarted the server.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team(agent_env={"OPENAI_API_KEY": "new-key"})
        s = self._settings(team=team)
        mock_docker_client.volumes.get.return_value = MagicMock()
        existing = MagicMock()
        existing.status = "running"
        existing.labels = {"oduflow.agent_config_hash": "stale"}
        mock_docker_client.containers.get.return_value = existing

        env_ops._ensure_agent_container(mock_docker_client, s, team)

        existing.remove.assert_called_once_with(force=True)
        assert mock_docker_client.containers.run.call_count == 2
        env = mock_docker_client.containers.run.call_args.kwargs["environment"]
        assert env["OPENAI_API_KEY"] == "new-key"

    def test_ensure_recreates_when_git_credentials_appear(
        self, monkeypatch, mock_docker_client
    ):
        # Credentials are copied into HOME at creation: a container created
        # BEFORE setup_repo_auth has no copy, so the file appearing later must
        # change the fingerprint and trigger a recreate.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team()
        s = self._settings(team=team)
        env = dict(env_ops._agent_env_vars(s, team))
        env.update(
            {
                "ODUFLOW_MCP_URL": "http://host.docker.internal:8000/mcp",
                "ODUFLOW_MCP_TOKEN": team.auth_token,
            }
        )
        existing = MagicMock()
        existing.status = "running"
        existing.labels = {
            "oduflow.agent_config_hash": env_ops._agent_config_hash(
                env_ops._agent_container_config(s, team, env), False
            )
        }
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.return_value = existing

        with patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True):
            env_ops._ensure_agent_container(mock_docker_client, s, team)

        existing.remove.assert_called_once_with(force=True)
        vols = mock_docker_client.containers.run.call_args_list[0].kwargs["volumes"]
        assert any(v["bind"] == "/run/oduflow/git-credentials" for v in vols.values())

    def test_ensure_recreates_when_pinned_image_changes(
        self, monkeypatch, mock_docker_client, caplog
    ):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team()
        old_settings = self._settings(
            team=team, agent_image="oduist/oduflow-coder:0.2.1"
        )
        new_settings = self._settings(team=team)
        existing = self._existing_container(old_settings, team, monkeypatch)
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.return_value = existing
        mock_docker_client.api.pull.return_value = [
            {"status": "Pulling from oduist/oduflow-coder", "id": "0.3.4"}
        ]

        with caplog.at_level(logging.INFO, logger="oduflow"):
            env_ops._ensure_agent_container(mock_docker_client, new_settings, team)

        mock_docker_client.api.pull.assert_called_once_with(
            DEFAULT_AGENT_IMAGE, stream=True, decode=True
        )
        # The pull can block startup for a long time: the reason, the daemon's
        # progress and the duration are all logged.
        messages = [record.getMessage() for record in caplog.records]
        assert (
            f"Agent config changed; pulling agent image {DEFAULT_AGENT_IMAGE}"
            in messages
        )
        assert "Agent image: Pulling from oduist/oduflow-coder 0.3.4" in messages
        assert any(
            m.startswith(f"Pulled agent image {DEFAULT_AGENT_IMAGE} in ")
            for m in messages
        )
        existing.remove.assert_called_once_with(force=True)
        assert mock_docker_client.containers.run.call_args.kwargs["image"] == (
            DEFAULT_AGENT_IMAGE
        )

    @pytest.mark.parametrize(
        "pull",
        [
            # The request itself fails (registry unreachable, unknown tag).
            {"side_effect": RuntimeError("registry down")},
            # The daemon reports a failure mid-stream instead of raising.
            {"return_value": [{"error": "unexpected EOF"}]},
        ],
        ids=["request-error", "stream-error"],
    )
    def test_failed_image_pull_preserves_existing_container(
        self, monkeypatch, mock_docker_client, caplog, pull
    ):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        team = self._team(agent_env={"OPENAI_API_KEY": "new-key"})
        settings = self._settings(team=team)
        existing = MagicMock()
        existing.status = "running"
        existing.labels = {"oduflow.agent_config_hash": "stale"}
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.return_value = existing
        mock_docker_client.api.pull.configure_mock(**pull)

        with caplog.at_level(logging.WARNING, logger="oduflow"):
            env_ops._ensure_agent_container(mock_docker_client, settings, team)

        existing.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()
        assert any(
            re.fullmatch(
                rf"Could not pull agent image {re.escape(DEFAULT_AGENT_IMAGE)} "
                r"after \d+\.\ds; keeping existing container",
                record.getMessage(),
            )
            for record in caplog.records
        )

    def test_api_key_when_no_subscription(self, monkeypatch, mock_docker_client):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        team = self._team()
        with patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False):
            env_ops._ensure_agent_container(
                mock_docker_client, self._settings(team=team), team
            )

        env = mock_docker_client.containers.run.call_args.kwargs["environment"]
        assert env["ANTHROPIC_API_KEY"] == "sk-ant"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_server_env_not_inherited_with_multiple_teams(
        self, monkeypatch, mock_docker_client
    ):
        # With several teams, a server-level provider key must NOT leak into a
        # tenant's agent container; only the team's own config applies.
        monkeypatch.setenv("OPENAI_API_KEY", "operator-key")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        team1 = self._team()
        team2 = TeamSettings(
            team_id="2", data_dir="/tmp/flow-test-2", auth_token="tok2"
        )
        s = Settings(
            base_data_dir="/tmp/flow-test",
            etc_dir="/tmp/flow-test/etc",
            port=8000,
            teams={"1": team1, "2": team2},
        )
        with patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False):
            env_ops._ensure_agent_container(mock_docker_client, s, team1)

        env = mock_docker_client.containers.run.call_args.kwargs["environment"]
        assert "OPENAI_API_KEY" not in env

    def test_team_agent_env_injected_and_overrides_server(
        self, monkeypatch, mock_docker_client
    ):
        # Variables from [team.X.agent_env] reach the container and override
        # the server environment; wiring vars always win over user vars.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "from-server")
        team = self._team(agent_env={"OPENAI_API_KEY": "from-config", "MY_FLAG": "1"})
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False):
            env_ops._ensure_agent_container(
                mock_docker_client, self._settings(team=team), team
            )

        env = mock_docker_client.containers.run.call_args.kwargs["environment"]
        assert env["OPENAI_API_KEY"] == "from-config"  # config wins
        assert env["MY_FLAG"] == "1"

    def _no_provider_keys(self, monkeypatch):
        for key in env_ops._AGENT_PROVIDER_CREDENTIALS:
            monkeypatch.delenv(key, raising=False)

    def test_sync_gives_every_existing_environment_an_identity(
        self, mock_docker_client
    ):
        # Environments that predate per-environment identities get a uid on
        # the next container (re)creation; known ones keep theirs. Deletions
        # the container could not carry out are finished first.
        team = self._team()
        s = self._settings(team=team)
        known = agent_uids.allocate_uid(team, "main")
        deleted = agent_uids.allocate_uid(team, "gone")
        agent_uids.retire_uid(team, "gone")

        def _labelled(name, env_name):
            c = MagicMock()
            c.name = name
            c.labels = {s.branch_label: env_name} if env_name else {}
            return c

        mock_docker_client.containers.list.return_value = [
            _labelled("oduflow-1-main-odoo", "main"),
            _labelled("oduflow-1-main-db", "main"),
            _labelled("oduflow-1-feature-x-odoo", "feature/x"),
            _labelled("oduflow-1-agent", None),
            _labelled("unrelated", "other"),
        ]
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        env_ops._agent_sync_identities(mock_docker_client, s, team, container)

        new = agent_uids.get_uid(team, "feature/x")
        assert new is not None and new not in (known, deleted)
        assert agent_uids.get_uid(team, "other") is None
        remove, sync = container.exec_run.call_args_list
        assert remove.args[0] == [
            "/usr/local/bin/env-user.sh",
            "remove",
            "gone",
            str(deleted),
        ]
        assert sync.args[0] == [
            "/usr/local/bin/env-user.sh",
            "sync",
            f"feature-x:{new}",
            f"main:{known}",
        ]
        assert all(
            c.kwargs["user"] == "root" for c in container.exec_run.call_args_list
        )
        assert agent_uids.pending_removals(team) == {}

    def test_add_env_provisions_identity_then_clones_as_the_env(
        self, monkeypatch, mock_docker_client
    ):
        self._no_provider_keys(monkeypatch)
        team = self._team()
        s = self._settings(team=team)
        container = self._existing_container(s, team, monkeypatch)
        container.exec_run.return_value = (0, b"")
        mock_docker_client.volumes.get.return_value = MagicMock()  # ensure noop
        mock_docker_client.containers.get.return_value = container

        env_ops._agent_add_env(
            mock_docker_client,
            s,
            team,
            "feature/x",
            "https://x/r.git",
            "feature/x",
            "alice",
        )

        uid = agent_uids.get_uid(team, "feature/x")
        assert uid is not None
        provision, clone = container.exec_run.call_args_list
        assert provision.args[0] == [
            "/usr/local/bin/env-user.sh",
            "provision",
            "feature-x",
            str(uid),
        ]
        assert provision.kwargs["user"] == "root"
        cmd = clone.args[0]
        assert cmd[0] == "/usr/local/bin/clone-env.sh"
        assert cmd[1] == "https://x/r.git"
        assert cmd[2] == "feature/x"
        assert cmd[3] == "feature-x"  # slugified env -> checkout dir
        # The SCOPED per-env endpoint; the team auth_token is never passed.
        assert cmd[4] == "http://host.docker.internal:8000/mcp/feature/x"
        assert cmd[5] == "alice"
        assert "tok" not in cmd
        # The checkout belongs to the environment's own uid, cloned with the
        # git credentials seeded into its own HOME.
        assert clone.kwargs["user"] == f"{uid}:{uid}"
        assert clone.kwargs["environment"]["HOME"] == f"/home/agent/envs/{uid}"

    def test_add_env_finishes_pending_removals_before_provisioning(
        self, monkeypatch, mock_docker_client
    ):
        # A new environment that reuses a deleted one's slug must never meet
        # the old checkout: the deferred cleanup runs before provisioning.
        self._no_provider_keys(monkeypatch)
        team = self._team()
        s = self._settings(team=team)
        old = agent_uids.allocate_uid(team, "feature/x")
        agent_uids.retire_uid(team, "feature/x")
        container = self._existing_container(s, team, monkeypatch)
        container.exec_run.return_value = (0, b"")
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.return_value = container

        env_ops._agent_add_env(
            mock_docker_client, s, team, "feature/x", "https://x/r.git", "x", ""
        )

        new = agent_uids.get_uid(team, "feature/x")
        cmds = [c.args[0] for c in container.exec_run.call_args_list]
        assert cmds[0] == [
            "/usr/local/bin/env-user.sh",
            "remove",
            "feature-x",
            str(old),
        ]
        assert cmds[1] == [
            "/usr/local/bin/env-user.sh",
            "provision",
            "feature-x",
            str(new),
        ]
        assert new != old
        assert agent_uids.pending_removals(team) == {}

    def test_add_env_does_not_clone_when_provisioning_fails(
        self, monkeypatch, mock_docker_client
    ):
        self._no_provider_keys(monkeypatch)
        team = self._team()
        s = self._settings(team=team)
        container = self._existing_container(s, team, monkeypatch)
        container.exec_run.return_value = (1, b"useradd failed")
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.return_value = container

        env_ops._agent_add_env(
            mock_docker_client, s, team, "main", "https://x/r.git", "main", ""
        )

        container.exec_run.assert_called_once()
        assert container.exec_run.call_args.args[0][1] == "provision"

    def test_add_env_skipped_when_disabled(self, mock_docker_client):
        team = self._team(agent_enabled=False)
        env_ops._agent_add_env(
            mock_docker_client,
            self._settings(team=team),
            team,
            "feature/x",
            "https://x/r.git",
            "feature/x",
            "alice",
        )
        mock_docker_client.containers.get.assert_not_called()
        assert agent_uids.get_uid(team, "feature/x") is None

    def test_ensure_env_checkout_clones_from_labels(
        self, monkeypatch, mock_docker_client
    ):
        # Opening a console for a pre-existing env reads repo/branch/user from the
        # Odoo container labels and clones on demand.
        self._no_provider_keys(monkeypatch)
        team = self._team()
        s = self._settings(team=team)
        odoo = MagicMock()
        odoo.labels = {
            "oduflow.repo": "https://x/r.git",
            "oduflow.git_branch": "19.0",
            "oduflow.git_user": "alice",
        }
        agent = self._existing_container(s, team, monkeypatch)
        agent.exec_run.return_value = (0, b"")
        mock_docker_client.volumes.get.return_value = MagicMock()

        def _get(name):
            return odoo if name.endswith("-odoo") else agent

        mock_docker_client.containers.get.side_effect = _get

        env_ops.ensure_agent_env_checkout(s, team, "prod")

        uid = agent_uids.get_uid(team, "prod")
        cmd = agent.exec_run.call_args.args[0]
        assert cmd[0] == "/usr/local/bin/clone-env.sh"
        assert cmd[1] == "https://x/r.git"
        assert cmd[2] == "19.0"  # git_branch label (may differ from env name)
        assert cmd[3] == "prod"  # slug from env name -> checkout dir
        assert cmd[4] == "http://host.docker.internal:8000/mcp/prod"
        assert cmd[5] == "alice"
        assert agent.exec_run.call_args.kwargs["user"] == f"{uid}:{uid}"

    def test_ensure_env_checkout_skips_repo_for_live_mount(
        self, monkeypatch, mock_docker_client
    ):
        # A live-mount env has no repo to clone; the hook passes an empty URL
        # (clone-env.sh then skips) instead of cloning a stale repo label.
        self._no_provider_keys(monkeypatch)
        team = self._team()
        s = self._settings(team=team)
        odoo = MagicMock()
        odoo.labels = {
            "oduflow.repo": "https://x/r.git",
            "oduflow.local_path": "/home/dev/checkout",
            "oduflow.git_branch": "19.0",
        }
        agent = self._existing_container(s, team, monkeypatch)
        agent.exec_run.return_value = (0, b"")
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = lambda name: (
            odoo if name.endswith("-odoo") else agent
        )

        env_ops.ensure_agent_env_checkout(s, team, "prod")

        cmd = agent.exec_run.call_args.args[0]
        assert cmd[1] == ""  # no repo URL for live-mount envs

    def test_remove_env_guards_empty_slug(self, mock_docker_client):
        # "..." slugifies to "" -> checkout dir would be /workspace/ itself;
        # the removal must refuse rather than rm -rf the whole shared volume.
        team = self._team()
        agent = MagicMock()
        mock_docker_client.containers.get.return_value = agent

        env_ops._agent_remove_env(
            mock_docker_client, self._settings(team=team), team, "..."
        )

        agent.exec_run.assert_not_called()

    def test_remove_env_drops_identity_and_retires_uid(self, mock_docker_client):
        team = self._team()
        uid = agent_uids.allocate_uid(team, "feature/x")
        agent = MagicMock()
        agent.exec_run.return_value = (0, b"")
        mock_docker_client.containers.get.return_value = agent

        env_ops._agent_remove_env(
            mock_docker_client, self._settings(team=team), team, "feature/x"
        )

        # Kills its sessions and deletes checkout, attachments and HOME.
        assert agent.exec_run.call_args.args[0] == [
            "/usr/local/bin/env-user.sh",
            "remove",
            "feature-x",
            str(uid),
        ]
        assert agent.exec_run.call_args.kwargs["user"] == "root"
        assert agent_uids.get_uid(team, "feature/x") is None
        assert agent_uids.pending_removals(team) == {}
        # Never handed out again, not even to an environment of the same name.
        assert agent_uids.allocate_uid(team, "feature/x") > uid

    def test_remove_env_keeps_cleanup_pending_when_it_cannot_run(
        self, mock_docker_client
    ):
        # Container gone (agent disabled, or stopped and removed): the files
        # stay on the volumes, so the uid waits for a later cleanup.
        team = self._team()
        uid = agent_uids.allocate_uid(team, "feature/x")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        env_ops._agent_remove_env(
            mock_docker_client, self._settings(team=team), team, "feature/x"
        )

        assert agent_uids.get_uid(team, "feature/x") is None
        assert agent_uids.pending_removals(team) == {uid: "feature-x"}

    def test_remove_env_keeps_cleanup_pending_when_it_fails(self, mock_docker_client):
        team = self._team()
        uid = agent_uids.allocate_uid(team, "feature/x")
        agent = MagicMock()
        agent.exec_run.return_value = (1, b"files of environment remain")
        mock_docker_client.containers.get.return_value = agent

        env_ops._agent_remove_env(
            mock_docker_client, self._settings(team=team), team, "feature/x"
        )

        assert agent_uids.pending_removals(team) == {uid: "feature-x"}

        # A stopped container makes the exec raise; still pending, no crash.
        agent.exec_run.side_effect = docker.errors.APIError("not running")
        env_ops._agent_purge_removed(agent, team)
        assert agent_uids.pending_removals(team) == {uid: "feature-x"}

    def test_remove_env_without_identity_removes_checkout_and_chat_attachments(
        self, mock_docker_client
    ):
        team = self._team()
        agent = MagicMock()
        mock_docker_client.containers.get.return_value = agent

        env_ops._agent_remove_env(
            mock_docker_client, self._settings(team=team), team, "feature/x"
        )

        assert agent.exec_run.call_args.args[0] == [
            "rm",
            "-rf",
            "--",
            "/workspace/feature-x",
            "/workspace/.oduflow-uploads/feature-x",
        ]
        assert agent.exec_run.call_args.kwargs["user"] == "root"

    def test_rename_env_keeps_the_uid_even_without_a_container(
        self, mock_docker_client
    ):
        team = self._team()
        uid = agent_uids.allocate_uid(team, "old")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        env_ops._agent_rename_env(
            mock_docker_client, self._settings(team=team), team, "old", "new"
        )

        assert agent_uids.get_uid(team, "new") == uid
        assert agent_uids.get_uid(team, "old") is None

    def test_rename_env_moves_checkout_and_attachments_as_root(
        self, mock_docker_client
    ):
        team = self._team()
        agent = MagicMock()
        mock_docker_client.containers.get.return_value = agent

        env_ops._agent_rename_env(
            mock_docker_client, self._settings(team=team), team, "old", "new"
        )

        moves = [c.args[0][2] for c in agent.exec_run.call_args_list]
        assert 'mv -- "/workspace/old" "/workspace/new"' in moves[0]
        assert (
            'mv -- "/workspace/.oduflow-uploads/old" "/workspace/.oduflow-uploads/new"'
        ) in moves[1]
        assert all(c.kwargs["user"] == "root" for c in agent.exec_run.call_args_list)


class TestFinalizeShellScript:
    """`odoo shell` rolls back at the end, so auto_commit must append a commit."""

    def test_captures_cursor_and_commits_when_enabled(self):
        out = odoo_ops._finalize_shell_script("x = 1", auto_commit=True)
        lines = out.splitlines()
        assert lines[0] == "__oduflow_cr__ = env.cr"
        assert lines[-1] == "__oduflow_cr__.commit()"
        assert "x = 1" in lines

    def test_no_commit_when_disabled(self):
        out = odoo_ops._finalize_shell_script("x = 1\n", auto_commit=False)
        assert "commit" not in out
        assert out == "x = 1\n"

    def test_commit_survives_env_rebind(self):
        # The cursor is captured before user code, so a script that rebinds
        # `env` still commits through the private handle (not the rebound env).
        out = odoo_ops._finalize_shell_script("env = object()", auto_commit=True)
        lines = out.splitlines()
        assert lines[0] == "__oduflow_cr__ = env.cr"
        assert lines[-1] == "__oduflow_cr__.commit()"

    def test_trailing_newlines_normalized_before_commit(self):
        # A dangling block or trailing blank lines must not push the commit
        # off the top level or duplicate blank lines.
        out = odoo_ops._finalize_shell_script(
            "for i in range(3):\n    x = i\n\n\n", True
        )
        assert out == (
            "__oduflow_cr__ = env.cr\n"
            "for i in range(3):\n    x = i\n"
            "__oduflow_cr__.commit()\n"
        )


# --- Translations (i18n) ----------------------------------------------------

_POT = """msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

#. module: mymod
#: model:ir.model.fields,field_description:mymod.field_mymod_deal__max_price
msgid "Budget Ceiling"
msgstr ""

#. module: mymod
#: model_terms:ir.ui.view,arch_db:mymod.view_deal_form
msgid "Publish"
msgstr ""

#. module: mymod
#: code:addons/mymod/models/deal.py:0
msgid "Only a dispatch manager may accept deals."
msgstr ""
"""

_INSTALLED = {"exit_code": 0, "output": "state\ninstalled\n"}
_ACTIVE_LANGS = {"exit_code": 0, "output": "code\nen_US\npl_PL\n"}


def _export_exec_results(po_text=_POT, manifest="/mnt/extra-addons/mymod"):
    """exec_run results for one _export_po run, then the module-dir lookup."""
    return [
        (0, b"odoo: module mymod: exporting translations"),  # the export itself
        (0, str(len(po_text.encode())).encode()),  # stat -c %s
        (0, po_text.encode()),  # cat
        (0, b""),  # rm -f
        (0, f"{manifest}/__manifest__.py\n".encode()),  # find
    ]


class TestExportModuleTranslations:
    def test_translation_write_override_accepts_catalogue_above_one_mb(
        self, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        mock_docker_client.containers.get.return_value = container
        content = "x" * 1_500_000

        result = odoo_ops.write_file_in_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            "/mnt/extra-addons/mymod/i18n/pl.po",
            content,
            max_bytes=odoo_ops._PO_EXPORT_LIMIT,
        )

        assert result["size"] == len(content)
        container.put_archive.assert_called_once()

    def test_write_creates_missing_parent_as_root_without_chowning_checkout(
        self, mock_docker_client
    ):
        container = MagicMock()
        parent = "/mnt/extra-addons/mymod/i18n"
        path = f"{parent}/pl.po"
        container.exec_run.side_effect = [
            (1, b"Permission denied"),  # mkdir as odoo
            (1, b""),  # i18n does not exist
            (0, b""),  # module directory exists
            (0, b""),  # mkdir as root
            (0, b""),  # chown new i18n directory
            (0, b""),  # chown written file
        ]
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.write_file_in_environment(
            TEST_SETTINGS, TEST_TEAM, "main", path, 'msgid ""\n'
        )

        assert result == {"path": path, "size": 9}
        assert container.exec_run.call_args_list == [
            mock_call(["mkdir", "-p", parent], user="odoo"),
            mock_call(["test", "-d", parent], user="root"),
            mock_call(["test", "-d", "/mnt/extra-addons/mymod"], user="root"),
            mock_call(["mkdir", "-p", parent], user="root"),
            mock_call(["chown", "-R", "odoo", parent], user="root"),
            mock_call(["chown", "odoo", path], user="root"),
        ]
        container.put_archive.assert_called_once()

    def test_write_reports_root_mkdir_failure(self, mock_docker_client):
        container = MagicMock()
        parent = "/mnt/extra-addons/mymod/i18n"
        container.exec_run.side_effect = [
            (1, b"Permission denied"),
            (1, b""),
            (0, b""),
            (1, b"Read-only file system"),
        ]
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            odoo_ops.ExternalCommandError, match="Read-only file system"
        ):
            odoo_ops.write_file_in_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                f"{parent}/pl.po",
                "content",
            )

        container.put_archive.assert_not_called()

    def test_write_wraps_docker_archive_error(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        container.put_archive.side_effect = docker.errors.NotFound(
            "parent directory not found"
        )
        mock_docker_client.containers.get.return_value = container
        path = "/mnt/extra-addons/mymod/i18n/pl.po"

        with pytest.raises(
            odoo_ops.ExternalCommandError,
            match=r"Could not write '.*/i18n/pl\.po'.*parent directory",
        ):
            odoo_ops.write_file_in_environment(
                TEST_SETTINGS, TEST_TEAM, "main", path, "content"
            )

    def test_write_reports_chown_failure(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, b""),
            (1, b"Operation not permitted"),
        ]
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            odoo_ops.ExternalCommandError, match="Operation not permitted"
        ):
            odoo_ops.write_file_in_environment(
                TEST_SETTINGS, TEST_TEAM, "main", "/tmp/output.txt", "content"
            )

    def test_public_write_limit_remains_one_mb(self, mock_docker_client):
        with pytest.raises(odoo_ops.ExternalCommandError, match="1000000 byte limit"):
            odoo_ops.write_file_in_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "/tmp/large.txt",
                "x" * 1_000_001,
            )

    @patch("oduflow.docker_ops.odoo_ops.write_file_in_environment")
    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_exports_template_through_entrypoint(
        self, mock_query, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {
            "oduflow.local_path": "/home/dev/veles",
            TEST_SETTINGS.image_label: "odoo:18.0",
        }
        container.exec_run.side_effect = _export_exec_results()
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.export_module_translations(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod"
        )

        cmd = container.exec_run.call_args_list[0][0][0]
        # Going through /entrypoint.sh is what gives the exporter the full
        # addons_path; without it the module's own .py files resolve to no
        # known module and every `_()` term is silently dropped.
        assert cmd.startswith("/entrypoint.sh odoo -d oduflow_1_main")
        assert "--modules=mymod" in cmd
        # The extension is what tells Odoo which writer to use.
        assert re.search(r"--i18n-export=/tmp/oduflow-i18n-\w+\.pot(\s|$)", cmd)
        assert " -l " not in cmd

        assert result["filename"] == "mymod.pot"
        assert result["summary"].by_type == {
            "model": 1,
            "model_terms": 1,
            "code": 1,
        }

    @patch("oduflow.docker_ops.odoo_ops.write_file_in_environment")
    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_writes_into_the_module_i18n_dir_and_reports_host_path(
        self, mock_query, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {
            "oduflow.local_path": "/home/dev/veles",
            TEST_SETTINGS.image_label: "odoo:18.0",
        }
        container.exec_run.side_effect = _export_exec_results()
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.export_module_translations(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod"
        )

        assert result["written_path"] == "/mnt/extra-addons/mymod/i18n/mymod.pot"
        mock_write.assert_called_once_with(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            "/mnt/extra-addons/mymod/i18n/mymod.pot",
            _POT,
            max_bytes=5 * 1024 * 1024,
        )
        # Live-mount: the container path maps onto the developer's own checkout,
        # so an agent on this machine can open the file without a download.
        assert result["host_path"] == "/home/dev/veles/mymod/i18n/mymod.pot"

    @patch("oduflow.docker_ops.odoo_ops.write_file_in_environment")
    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_lang_export_uses_odoos_own_i18n_filename(
        self, mock_query, mock_write, mock_docker_client
    ):
        # Module loading looks for i18n/<get_iso_codes(lang)>.po, so pl_PL must
        # be written as pl.po — pl_PL.po would be a file Odoo never reads.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = _export_exec_results()
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.export_module_translations(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", "pl_PL"
        )

        cmd = container.exec_run.call_args_list[0][0][0]
        assert "-l pl_PL" in cmd
        assert re.search(r"--i18n-export=/tmp/oduflow-i18n-\w+\.po(\s|$)", cmd)
        assert result["filename"] == "pl.po"
        assert result["written_path"] == "/mnt/extra-addons/mymod/i18n/pl.po"

    def test_i18n_basename_keeps_locales_that_differ_from_their_language(self):
        # pt_BR is a distinct locale from pt, so it does not collapse.
        assert odoo_ops._i18n_basename("pl_PL") == "pl"
        assert odoo_ops._i18n_basename("pt_BR") == "pt_BR"
        assert odoo_ops._i18n_basename("fr") == "fr"

    def test_odoo_locale_modifiers_are_accepted(self):
        odoo_ops._validate_lang_codes(["sr@latin", "sr@Cyrl", "sr_RS@latin"])

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.odoo_ops.write_file_in_environment")
    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_odoo_19_uses_the_i18n_subcommand(
        self, mock_query, mock_write, mock_creds, mock_docker_client
    ):
        # Odoo 19 dropped the --i18n-* server options. Its `odoo i18n` subcommand
        # parses argv strictly, so it rejects the --db_* arguments the image
        # entrypoint appends and takes the connection from a config file instead.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:19.0"}
        container.exec_run.side_effect = _export_exec_results()
        mock_docker_client.containers.get.return_value = container

        odoo_ops.export_module_translations(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", "pl_PL"
        )

        call = container.exec_run.call_args_list[0]
        argv = call[0][0]
        assert argv[:2] == ["sh", "-c"]
        script = argv[2]
        assert "/entrypoint.sh" not in script
        assert "odoo i18n export -c /tmp/oduflow-i18n-" in script
        assert "-d oduflow_1_main mymod -l pl_PL -o /tmp/oduflow-i18n-" in script
        assert 'echo "db_host = oduflow-db"' in script
        assert 'echo "db_user = u_1_main"' in script
        # The password reaches libpq through the environment, never the config
        # file on disk or the container's process argv.
        assert "test-pw" not in script
        assert call.kwargs["environment"] == {"PGPASSWORD": "test-pw"}
        # Both the catalogue and the generated config are cleaned up.
        cleanup = container.exec_run.call_args_list[3][0][0]
        assert cleanup[:2] == ["rm", "-f"]
        assert len(cleanup) == 4

    @patch("oduflow.docker_ops.odoo_ops.write_file_in_environment")
    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_extra_addons_repo_is_not_written_to(
        self, mock_query, mock_write, mock_docker_client
    ):
        # Extra-addons checkouts are shared between environments and mounted
        # read-only, so the file can only leave via the download link.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = _export_exec_results(
            manifest="/mnt/extra-addons-enterprise/mymod"
        )
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.export_module_translations(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod"
        )

        assert result["read_only_mount"] is True
        assert result["written_path"] == ""
        mock_write.assert_not_called()

    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_failed_export_raises_with_the_odoo_log(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = [
            (1, b"odoo: error: database not initialized"),
            (1, b""),  # stat: no file was produced
            (0, b""),  # rm -f
        ]
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            odoo_ops.ExternalCommandError, match="database not initialized"
        ):
            odoo_ops.export_module_translations(
                TEST_SETTINGS, TEST_TEAM, "main", "mymod"
            )

    @patch("oduflow.docker_ops.odoo_ops.run_db_query", return_value=_INSTALLED)
    def test_failed_export_rejects_a_partial_output_file(
        self, mock_query, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = [
            (1, b"odoo: export aborted halfway"),
            (0, b""),  # cleanup
        ]
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(
            odoo_ops.ExternalCommandError, match="export aborted halfway"
        ):
            odoo_ops.export_module_translations(
                TEST_SETTINGS, TEST_TEAM, "main", "mymod"
            )
        assert not any(
            call.args[0][:2] == ["stat", "-c"]
            for call in container.exec_run.call_args_list
        )

    @patch(
        "oduflow.docker_ops.odoo_ops.run_db_query",
        return_value={"exit_code": 0, "output": "state\nuninstalled\n"},
    )
    def test_uninstalled_module_is_refused(self, mock_query, mock_docker_client):
        # Odoo's exporter filters on installed modules, so this would otherwise
        # succeed and return an empty catalogue.
        with pytest.raises(PrerequisiteNotMetError, match="not installed"):
            odoo_ops.export_module_translations(
                TEST_SETTINGS, TEST_TEAM, "main", "mymod"
            )

    @patch(
        "oduflow.docker_ops.odoo_ops.run_db_query",
        return_value={"exit_code": 0, "output": "state\n"},
    )
    def test_unknown_module_is_refused(self, mock_query, mock_docker_client):
        with pytest.raises(NotFoundError, match="unknown"):
            odoo_ops.export_module_translations(
                TEST_SETTINGS, TEST_TEAM, "main", "nosuch"
            )

    def test_bad_lang_code_is_rejected(self, mock_docker_client):
        # The value lands on the `odoo -l <lang>` command line, which exec_run
        # tokenizes — an unvalidated token could smuggle a separate CLI flag.
        with pytest.raises(ValueError, match="Invalid language code"):
            odoo_ops.export_module_translations(
                TEST_SETTINGS, TEST_TEAM, "main", "mymod", "pl --load=web"
            )


class TestTranslationStatus:
    @patch("oduflow.docker_ops.odoo_ops.run_db_query")
    def test_flags_a_po_file_that_imports_to_nothing(
        self, mock_query, mock_docker_client
    ):
        mock_query.side_effect = [_INSTALLED, _ACTIVE_LANGS]
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        po_text = (
            'msgid ""\nmsgstr ""\n\n'
            "#. module: mymod\n"
            'msgid "Budget Ceiling"\nmsgstr "Limit budżetu"\n'
        )
        # Fresh template export + module lookup, no sibling POT, database
        # export, then the committed language file.
        container.exec_run.side_effect = _export_exec_results() + [
            (1, b""),  # test -f mymod.pot
            (0, b"odoo: exporting"),
            (0, b"1"),
            (0, b""),  # database holds nothing for pl_PL
            (0, b""),
            (0, b""),  # test -f pl.po
            (0, str(len(po_text.encode())).encode()),
            (0, po_text.encode()),
        ]
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.translation_status(TEST_SETTINGS, TEST_TEAM, "main", "mymod")

        # en_US is skipped; only the real target language is reported.
        assert [e["lang"] for e in result["langs"]] == ["pl_PL"]
        entry = result["langs"][0]
        assert entry["active"] is True
        assert entry["database"].entries == 0
        assert entry["file"].entries == 1
        assert entry["file"].no_reference == 1
        assert [e.msgid for e in entry["diff"]["missing"]] == [
            "Only a dispatch manager may accept deals.",
            "Publish",
        ]
        assert result["template"].entries == 3
        # The backend reaches the verdict, so every caller reads the same one.
        assert entry["status"].code == po_tools.IMPORT_DROPPED
        assert entry["status"].covered_terms == 1

    @patch("oduflow.docker_ops.odoo_ops.run_db_query")
    def test_inactive_language_is_reported_not_silently_empty(
        self, mock_query, mock_docker_client
    ):
        mock_query.side_effect = [_INSTALLED, _ACTIVE_LANGS]
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = _export_exec_results() + [
            (1, b""),  # sibling POT missing
            (1, b""),  # language PO missing
        ]
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.translation_status(
            TEST_SETTINGS, TEST_TEAM, "main", "mymod", ["ru_RU"]
        )

        entry = result["langs"][0]
        assert entry["active"] is False
        assert "database" not in entry
        assert entry["file_path"] == ""

    def test_translation_reader_accepts_catalogues_above_generic_read_limit(self):
        content = "x" * (200 * 1024)
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, b""),
            (0, str(len(content)).encode()),
            (0, content.encode()),
        ]

        assert odoo_ops._read_translation_catalog(container, "/x/pl.po") == content

    def test_translation_reader_does_not_treat_read_failure_as_missing(self):
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, b""),
            (0, b"123"),
            (1, b"permission denied"),
        ]

        with pytest.raises(odoo_ops.ExternalCommandError, match="permission denied"):
            odoo_ops._read_translation_catalog(container, "/x/pl.po")

    @patch("oduflow.docker_ops.odoo_ops.run_db_query")
    def test_sibling_pot_supplies_metadata_used_by_odoo_import(
        self, mock_query, mock_docker_client
    ):
        mock_query.side_effect = [_INSTALLED, _ACTIVE_LANGS]
        po_text = 'msgid "Budget Ceiling"\nmsgstr "Limit budżetu"\n'
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:18.0"}
        container.exec_run.side_effect = _export_exec_results() + [
            (0, b""),
            (0, str(len(_POT.encode())).encode()),
            (0, _POT.encode()),
            (0, b"odoo: exporting"),
            (0, b"1"),
            (0, b""),
            (0, b""),
            (0, b""),
            (0, str(len(po_text.encode())).encode()),
            (0, po_text.encode()),
        ]
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.translation_status(TEST_SETTINGS, TEST_TEAM, "main", "mymod")
        entry = result["langs"][0]

        assert entry["file"].no_reference == 1
        assert entry["import_effective"].no_reference == 0
        assert entry["import_effective"].no_module_comment == 0
        assert entry["metadata_template_path"].endswith("/i18n/mymod.pot")


class TestOdooUrlComposition:
    """The environment base URL must carry no path of its own.

    `get_environment_info()["url"]` ends in "/web?debug=1"; composing a request
    path onto it buried the path in the query string, so every call hit "/web".
    """

    @staticmethod
    def _response(status=200, body=b"{}"):
        response = MagicMock()
        response.status = status
        response.headers = {}
        response.read.return_value = body
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *exc: False
        return response

    @patch(
        "oduflow.docker_ops.env_ops.get_env_base_url",
        return_value=("http://localhost:50000", "localhost"),
    )
    @patch("urllib.request.urlopen")
    def test_http_request_hits_the_requested_path(self, mock_open, mock_base):
        mock_open.return_value = self._response()

        odoo_ops.http_request_to_odoo(
            TEST_SETTINGS, TEST_TEAM, "main", "/jsonrpc", method="POST", body="{}"
        )

        request = mock_open.call_args[0][0]
        assert urlsplit(request.full_url).path == "/jsonrpc"
        assert urlsplit(request.full_url).query == ""

    @patch(
        "oduflow.docker_ops.env_ops.get_env_base_url",
        return_value=("http://localhost:50000", "localhost"),
    )
    @patch("urllib.request.urlopen")
    def test_readiness_probes_web_health(self, mock_open, mock_base):
        mock_open.return_value = self._response()

        assert env_ops.wait_for_odoo_ready(TEST_SETTINGS, TEST_TEAM, "main") is True

        url = mock_open.call_args[0][0]
        assert urlsplit(url).path == "/web/health"


class TestConnectAsUserCredentials:
    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    @patch(
        "oduflow.docker_ops.env_ops.get_env_base_url",
        return_value=("https://main.example.com", "main.example.com"),
    )
    def test_password_passed_via_pgpassword_env(
        self, mock_base_url, mock_load_creds, mock_docker_client
    ):
        container = MagicMock()
        container.exec_run.return_value = (0, TestConnectAsUser._SHELL_OUTPUT)
        mock_docker_client.containers.get.return_value = container

        odoo_ops.connect_as_user(TEST_SETTINGS, TEST_TEAM, "main", "jane@acme.com")

        shell_call = next(
            call
            for call in container.exec_run.call_args_list
            if call.args[0][:2] == ["sh", "-c"]
        )
        cmd = shell_call.args[0][2]
        assert "-r u_1_main" in cmd
        # The session mint is a hot path for the odoo_* tools: the password must
        # not land in the container's process argv.
        assert "test-pw" not in cmd
        assert "-w" not in cmd.split()
        assert shell_call.kwargs["environment"] == {"PGPASSWORD": "test-pw"}
