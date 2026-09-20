from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow import production_registry
from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def settings(tmp_path):
    team_dir = tmp_path / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(tmp_path),
        etc_dir=str(tmp_path / "etc"),
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


def _client_without_prod():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("nf")
    client.volumes.get.side_effect = docker.errors.NotFound("nf")
    return client


def _container(name, status="running"):
    container = MagicMock()
    container.name = name
    container.status = status
    return container


class TestProductionWorkloadReconciliation:
    def test_disabled_stops_odoo_before_postgres_without_removing(self, settings):
        app = _container("oduflow-1-prod-erp-odoo")
        postgres = _container(settings.prod_db_container)
        stopped = []
        app.stop.side_effect = lambda: stopped.append(app.name)
        postgres.stop.side_effect = lambda: stopped.append(postgres.name)
        client = MagicMock()
        client.containers.list.return_value = [postgres, app]

        system_ops.reconcile_prod_workloads(client, settings)

        assert stopped == [app.name, postgres.name]
        client.containers.list.assert_called_once_with(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    "oduflow.prod=true",
                ]
            },
        )
        app.remove.assert_not_called()
        postgres.remove.assert_not_called()

    def test_disabled_ignores_already_stopped_containers(self, settings):
        app = _container("oduflow-1-prod-erp-odoo", status="exited")
        client = MagicMock()
        client.containers.list.return_value = [app]

        system_ops.reconcile_prod_workloads(client, settings)

        app.stop.assert_not_called()

    def test_reenable_ensures_postgres_before_starting_all_odoo(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            etc_dir=settings.etc_dir,
            prod_enabled=True,
            teams=settings.teams,
        )
        app_a = _container("oduflow-1-prod-a-odoo", status="exited")
        app_b = _container("oduflow-1-prod-b-odoo", status="exited")
        postgres = _container(settings.prod_db_container, status="running")
        events = []
        app_a.start.side_effect = lambda: events.append(app_a.name)
        app_b.start.side_effect = lambda: events.append(app_b.name)
        client = MagicMock()
        # PG stopped before ensure_prod_infra is the disable fingerprint that
        # marks this as a genuine re-enable transition.
        client.containers.get.return_value = _container(
            settings.prod_db_container, status="exited"
        )
        client.containers.list.return_value = [app_b, postgres, app_a]

        with patch.object(
            system_ops,
            "ensure_prod_infra",
            side_effect=lambda *_args, **_kwargs: events.append("postgres"),
        ):
            system_ops.reconcile_prod_workloads(client, settings)

        assert events == ["postgres", app_a.name, app_b.name]
        postgres.start.assert_not_called()

    def test_steady_restart_preserves_manual_stops(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            etc_dir=settings.etc_dir,
            prod_enabled=True,
            teams=settings.teams,
        )
        client = MagicMock()
        # PG already running => ordinary restart, not a re-enable: a production
        # stopped via stop_production must stay stopped.
        client.containers.get.return_value = _container(
            settings.prod_db_container, status="running"
        )

        with patch.object(system_ops, "ensure_prod_infra") as ensure:
            system_ops.reconcile_prod_workloads(client, settings)

        ensure.assert_called_once()
        client.containers.list.assert_not_called()


class TestLazyProvisioning:
    def test_noop_without_productions(self, settings):
        client = _client_without_prod()
        assert system_ops.ensure_prod_infra(client, settings) is False
        client.containers.run.assert_not_called()
        client.volumes.create.assert_not_called()

    def test_provisions_when_registry_has_production(self, settings):
        production_registry.create_production(settings.teams["1"], "erp", {})
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba") as reconcile_hba,
            patch("oduflow.walg.apply_archive_command") as apply_cmd,
        ):
            assert system_ops.ensure_prod_infra(client, settings) is True
        client.volumes.create.assert_called_once()
        client.containers.run.assert_called_once()
        # No [backup] configured -> archive command stays disabled.
        assert apply_cmd.call_args[1]["enabled"] is False
        reconcile_hba.assert_called_once_with(
            client, settings, container_name=settings.prod_db_container
        )

    def test_force_provisions_without_productions(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            assert system_ops.ensure_prod_infra(client, settings, force=True) is True
        client.containers.run.assert_called_once()

    def test_walg_download_failure_does_not_block(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg", side_effect=RuntimeError("offline")),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command") as apply_cmd,
        ):
            assert system_ops.ensure_prod_infra(client, settings, force=True) is True
        assert apply_cmd.call_args[1]["enabled"] is False


class TestProdPgContainer:
    def test_container_config(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings, force=True)

        kwargs = client.containers.run.call_args[1]
        assert kwargs["name"] == "oduflow-prod-db"
        volumes = kwargs["volumes"]
        assert volumes[settings.prod_db_volume]["bind"] == ("/var/lib/postgresql/data")
        # wal-g bin + conf dirs are mounted read-only from day one so
        # backups can be enabled later without recreating the container.
        binds = {v["bind"]: v["mode"] for v in volumes.values()}
        assert binds["/opt/oduflow-bin"] == "ro"
        assert binds["/etc/walg"] == "ro"
        # No /tablespaces mount and no published ports.
        assert "/tablespaces" not in binds
        assert "ports" not in kwargs
        assert kwargs["labels"]["oduflow.prod"] == "true"

    def test_prod_image_override(self, settings, tmp_path):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            etc_dir=settings.etc_dir,
            prod_postgres_image="postgres:17",
            teams=settings.teams,
        )
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings, force=True)
        assert client.containers.run.call_args[0][0] == "postgres:17"

    def test_existing_container_started_not_recreated(self, settings):
        client = MagicMock()
        stopped = MagicMock()
        stopped.status = "exited"
        client.containers.get.return_value = stopped
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings)
        stopped.start.assert_called_once()
        client.containers.run.assert_not_called()

    def test_existing_container_still_checks_managed_config(self, settings):
        client = MagicMock()
        running = MagicMock()
        running.status = "running"
        client.containers.get.return_value = running
        conf_path = system_ops._prod_pg_conf_path(settings)
        Path(conf_path).parent.mkdir(parents=True)
        Path(conf_path).write_text("# KEEP\n")

        with patch.object(system_ops, "_warn_stale_prod_pg_conf") as warn_stale:
            system_ops._ensure_prod_pg_container(client, settings, {})

        warn_stale.assert_called_once_with(settings, conf_path)
        client.containers.run.assert_not_called()


class TestProdPgConf:
    def test_generated_once_with_keep_marker(self, settings):
        path = system_ops._ensure_prod_pg_conf(settings)
        content = open(path).read()
        assert content.splitlines()[0] == "# KEEP"
        assert "archive_mode = on" in content
        # Existing file is never rewritten.
        with open(path, "w") as f:
            f.write("# KEEP\ncustom")
        system_ops._ensure_prod_pg_conf(settings)
        assert open(path).read() == "# KEEP\ncustom"


class TestStaleDeployFlags:
    """deploy_in_progress recovery belongs to startup, and only to startup.

    ensure_prod_infra(force=True) is also reached at runtime — create_production,
    start_production, and creating a prod-cluster service database — none of
    which hold the team lock. Clearing the flag there would silently cancel a
    concurrent deploy's status and defeat the "deploy in progress" conflict
    guard that stack_production relies on.
    """

    @staticmethod
    def _deploying(settings):
        team = settings.teams["1"]
        production_registry.create_production(team, "erp", {"deploy_in_progress": True})
        return team

    def test_ensure_prod_infra_leaves_live_deploy_flag_alone(self, settings):
        team = self._deploying(settings)
        client = _client_without_prod()

        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings, force=True)

        record = production_registry.get_production(team, "erp")
        assert record["deploy_in_progress"] is True

    def test_init_system_clears_stale_deploy_flag(self, settings):
        team = self._deploying(settings)

        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(system_ops, "_ensure_iptables_accept"),
            patch.object(system_ops, "_ensure_traefik"),
            patch.object(system_ops, "_ensure_pg_container"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch.object(system_ops, "_reconcile_pg_hba"),
            patch.object(system_ops, "reconcile_prod_workloads"),
            patch("oduflow.docker_ops.env_ops._ensure_agent_container"),
            patch("oduflow.docker_ops.env_ops._remove_agent_container"),
        ):
            system_ops.init_system(settings)

        record = production_registry.get_production(team, "erp")
        assert record["deploy_in_progress"] is False
