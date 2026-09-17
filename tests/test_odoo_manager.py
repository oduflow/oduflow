import dataclasses
import json
import os
import re
import tarfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from unittest.mock import call as mock_call
from urllib.parse import urlsplit

import pytest

import docker
from oduflow import po_tools
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
    @patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_init_system_fresh(
        self, mock_isfile, mock_copy, mock_hba, mock_docker_client
    ):
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
        # Greenfield: isolated init container + serving container = 2 run calls.
        assert mock_docker_client.containers.run.call_count == 2
        for run_call in mock_docker_client.containers.run.call_args_list:
            assert run_call.kwargs["extra_hosts"] == {
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

    def test_create_rolls_back_on_required_setup_failure(
        self, mock_docker_client, tmp_path
    ):
        container = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = []
        mock_docker_client.containers.run.return_value = container
        instance_conf = MagicMock()
        instance_conf.exists.return_value = False
        setup_error = PrerequisiteNotMetError("registry not ready")

        # Entered through an ExitStack rather than a parenthesized `with` group:
        # CPython 3.10 caps a function at 20 statically nested blocks, and the
        # patches needed to reach the serving container exceed it.
        provisioning_patches = [
            patch("oduflow.docker_ops.env_ops._db_exists", return_value=True),
            patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
            patch("oduflow.docker_ops.env_ops.ensure_team_network"),
            patch("oduflow.docker_ops.env_ops._cleanup_old_environment"),
            patch("oduflow.docker_ops.env_ops._write_local_snapshot"),
            patch(
                "oduflow.docker_ops.env_ops._template_code_lineage",
                return_value={},
            ),
            patch("oduflow.docker_ops.env_ops._exec_sql"),
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
            patch(
                "oduflow.docker_ops.env_ops._resolve_instance_conf",
                return_value=instance_conf,
            ),
            patch(
                "oduflow.docker_ops.env_ops._configure_serving_environment",
                side_effect=setup_error,
            ),
        ]

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
            "flow-db not found. System not initialized. Restart oduflow."
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
        # Greenfield init runs `-i base` in a SEPARATE isolated container
        # (the FIRST containers.run), before the serving container — so the
        # serving PID1 never races it. containers.run is called twice.
        assert mock_docker_client.containers.run.call_count == 2
        init_run = mock_docker_client.containers.run.call_args_list[0]
        assert "-i base" in init_run.kwargs["command"]
        assert "--stop-after-init" in init_run.kwargs["command"]
        assert init_run.kwargs["extra_hosts"] == {
            "host.docker.internal": "host-gateway"
        }

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
            patch("oduflow.docker_ops.env_ops.update_environment") as update,
            patch("oduflow.docker_ops.env_ops._cleanup_legacy_extra_worktrees"),
        ):
            result = env_ops.pull_environment(settings, team, "env")

        assert result["extra_addons_cache_migrated"] is True
        update.assert_called_once_with(
            settings,
            team,
            "env",
            extra_checkout_overrides={"enterprise": str(shared)},
            extra_revision_overrides={"enterprise": "a" * 40},
            pull_image=False,
            install_dependencies=False,
        )

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
            patch("oduflow.docker_ops.env_ops.update_environment") as update,
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

        assert mock_apply.call_args.kwargs["deps_changed"] is True
        assert mock_apply.call_args.kwargs["repo_path"] == str(repo)

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

        assert mock_apply.call_args.kwargs["deps_changed"] is False
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
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
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
        mock_resolve_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        import json

        mock_resolve_conf.return_value.exists.return_value = False
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
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
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
        mock_resolve_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        mock_resolve_conf.return_value.exists.return_value = False
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
            patch("oduflow.docker_ops.env_ops._resolve_instance_conf") as conf,
        ):
            conf.return_value.exists.return_value = False
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
            patch("oduflow.docker_ops.env_ops._resolve_instance_conf") as conf,
        ):
            conf.return_value.exists.return_value = False
            return env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", pull_image=False, **kwargs
            )

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
            patch("oduflow.docker_ops.env_ops._resolve_instance_conf") as conf,
        ):
            conf.return_value.exists.return_value = False
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
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
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
        mock_resolve_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        mock_resolve_conf.return_value.exists.return_value = False
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


class TestProvisioningSetup:
    @patch("oduflow.sanitizer.sanitize_environment")
    @patch("oduflow.sanitizer.neutralize_environment")
    @patch(
        "oduflow.docker_ops.env_ops._auto_install_modules",
        side_effect=PrerequisiteNotMetError("registry not ready"),
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
        container.restart.assert_not_called()

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
            "install",
            "neutralize",
            "sanitize",
            "restart",
            "wait",
        ]
        assert result == ["auto-install", "neutralize", "sanitize"]

    @patch("oduflow.sanitizer.sanitize_environment", return_value=[])
    @patch("oduflow.sanitizer.neutralize_environment", return_value=[])
    @patch(
        "oduflow.docker_ops.env_ops._wait_for_container_odoo_ready",
        return_value=env_ops._OdooReadinessResult(
            False, 120.0, 60, 120, "HTTPError: HTTP 500"
        ),
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

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 2, "output": "upgrade traceback"},
    )
    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 0, "output": "installed"},
    )
    def test_failed_upgrade_is_reported(self, mock_install, mock_up):
        assert self._apply()["exit_code"] == 2

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 0, "output": "upgraded"},
    )
    @patch(
        "oduflow.docker_ops.odoo_ops.install_odoo_modules",
        return_value={"exit_code": 0, "output": "installed"},
    )
    def test_all_successful_reports_zero(self, mock_install, mock_up):
        assert self._apply()["exit_code"] == 0


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
            deps_changed=True,
            repo_path="/repo",
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
            deps_changed=True,
            repo_path="/repo",
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
            deps_changed=True,
            repo_path="/repo",
        )
        assert result["action"] == "restart"
        assert "FAILED" in result["output"]
        assert result["exit_code"] == 1
        container.restart.assert_called_once()

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
            deps_changed=False,
            repo_path="/repo",
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
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    def test_returns_false_when_no_base_conf(
        self, mock_isfile, mock_resolve, mock_copy
    ):
        inst = MagicMock()
        inst.exists.return_value = False
        mock_resolve.return_value = inst
        container = MagicMock()
        container.labels = {}
        applied = env_ops._reapply_odoo_conf(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        assert applied is False
        mock_copy.assert_not_called()


class TestAgentContainer:
    """The agent is a SINGLE per-team container with persistent HOME/workspace
    volumes; per-env checkouts are added/removed via `docker exec`. It is
    opt-in per team (a hosting feature) and configured statically in
    oduflow.toml. See specs/0029-agent-console-and-chat.md."""

    def _team(self, **kw):
        base = dict(
            team_id="1",
            data_dir="/tmp/flow-test",
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
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        env_ops.refresh_agent_mcp_config(container, settings, team, "feature/x")

        cmd = container.exec_run.call_args.args[0]
        assert cmd[:2] == ["sh", "-c"]
        assert cmd[4] == "/workspace/feature-x/.mcp.json"
        assert cmd[5] == "https://mirageflow.ca/mcp/feature/x"
        assert "ODUFLOW_MCP_TOKEN" in cmd[2]
        assert container.exec_run.call_args.kwargs["user"] == "agent"

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
        self, monkeypatch, mock_docker_client
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

        env_ops._ensure_agent_container(mock_docker_client, new_settings, team)

        mock_docker_client.images.pull.assert_called_once_with(DEFAULT_AGENT_IMAGE)
        existing.remove.assert_called_once_with(force=True)
        assert mock_docker_client.containers.run.call_args.kwargs["image"] == (
            DEFAULT_AGENT_IMAGE
        )

    def test_failed_image_pull_preserves_existing_container(
        self, monkeypatch, mock_docker_client
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
        mock_docker_client.images.pull.side_effect = RuntimeError("registry down")

        env_ops._ensure_agent_container(mock_docker_client, settings, team)

        existing.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

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

    def test_add_env_execs_clone_script(self, monkeypatch, mock_docker_client):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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

        container.exec_run.assert_called_once()
        cmd = container.exec_run.call_args.args[0]
        assert cmd[0] == "/usr/local/bin/clone-env.sh"
        assert cmd[1] == "https://x/r.git"
        assert cmd[2] == "feature/x"
        assert cmd[3] == "feature-x"  # slugified env -> checkout dir
        # The SCOPED per-env endpoint; the team auth_token is never passed.
        assert cmd[4] == "http://host.docker.internal:8000/mcp/feature/x"
        assert cmd[5] == "alice"
        assert "tok" not in cmd
        assert container.exec_run.call_args.kwargs["user"] == "agent"

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

    def test_ensure_env_checkout_clones_from_labels(
        self, monkeypatch, mock_docker_client
    ):
        # Opening a console for a pre-existing env reads repo/branch/user from the
        # Odoo container labels and clones on demand.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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

        agent.exec_run.assert_called_once()
        cmd = agent.exec_run.call_args.args[0]
        assert cmd[0] == "/usr/local/bin/clone-env.sh"
        assert cmd[1] == "https://x/r.git"
        assert cmd[2] == "19.0"  # git_branch label (may differ from env name)
        assert cmd[3] == "prod"  # slug from env name -> checkout dir
        assert cmd[4] == "http://host.docker.internal:8000/mcp/prod"
        assert cmd[5] == "alice"
        assert agent.exec_run.call_args.kwargs["user"] == "agent"

    def test_ensure_env_checkout_skips_repo_for_live_mount(
        self, monkeypatch, mock_docker_client
    ):
        # A live-mount env has no repo to clone; the hook passes an empty URL
        # (clone-env.sh then skips) instead of cloning a stale repo label.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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

    def test_remove_env_removes_checkout_and_chat_attachments(self, mock_docker_client):
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
