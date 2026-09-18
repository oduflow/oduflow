import os
import stat
import threading
import time
from unittest.mock import patch

import pytest

from oduflow.docker_ops import service_database_ops
from oduflow.errors import ConflictError, PrerequisiteNotMetError, ProtectedError
from oduflow.service_database_credentials import load
from oduflow.service_database_credentials import save as save_record
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def database_fixture(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="admin",
        db_password="secret",
        shared_db_container="oduflow-db",
        teams={"1": team},
    )
    return settings, team


def test_create_persists_private_credentials_and_returns_connection(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
        patch("oduflow.docker_ops.service_database_ops.check_db_quota"),
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace",
            return_value="oduflow_team_1",
        ),
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
        patch(
            "oduflow.docker_ops.service_database_ops.generate_pg_password",
            return_value="generated-password",
        ),
    ):
        result = service_database_ops.create_database(settings, team, "events")

    assert result["database"] == "oduflow_service_1_events"
    assert result["username"] == "svc_1_events"
    assert result["password"] == "generated-password"
    assert result["url"].startswith("postgresql://svc_1_events:")
    record = load(team, "events")
    assert record["password"] == "generated-password"
    directory = os.path.join(team.data_dir, "service_databases")
    path = os.path.join(directory, "events.json")
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    sql = "\n".join(call.args[2] for call in exec_sql.call_args_list)
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in sql
    assert 'TABLESPACE "oduflow_team_1"' in sql
    assert 'REVOKE ALL ON DATABASE "oduflow_service_1_events" FROM PUBLIC' in sql


def test_quota_admission_is_serialised_across_names(database_fixture):
    """The caller's lock is per database *name*, so it does not serialise two
    concurrent creates for one team. Without the registry mutex both would read
    the same usage figure and jointly overshoot the quota."""
    settings, team = database_fixture
    guard = threading.Lock()
    inside = 0
    peak = 0

    def quota_probe(*_args, **_kwargs):
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.05)  # widen the window a racing caller would slip through
        with guard:
            inside -= 1

    # Patched once, from this thread: unittest.mock.patch restores attributes
    # globally and is not safe to enter concurrently on the same targets.
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.check_db_quota",
            side_effect=quota_probe,
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace",
            return_value="oduflow_team_1",
        ),
        patch("oduflow.docker_ops.service_database_ops._exec_sql"),
    ):
        threads = [
            threading.Thread(
                target=service_database_ops.create_database,
                args=(settings, team, name),
            )
            for name in ("one", "two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert peak == 1


def test_create_refuses_unmanaged_catalog_drift(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
    ):
        with pytest.raises(ConflictError, match="without matching managed credentials"):
            service_database_ops.create_database(settings, team, "events")


def test_create_rolls_role_back_when_database_creation_fails(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
        patch("oduflow.docker_ops.service_database_ops.check_db_quota"),
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace",
            return_value="oduflow_team_1",
        ),
        patch(
            "oduflow.docker_ops.service_database_ops._exec_sql",
            side_effect=["", RuntimeError("create database failed")],
        ),
        patch("oduflow.docker_ops.service_database_ops._drop_pg_role") as drop_role,
    ):
        with pytest.raises(RuntimeError, match="create database failed"):
            service_database_ops.create_database(settings, team, "events")

    drop_role.assert_called_once()
    assert not os.path.exists(
        os.path.join(team.data_dir, "service_databases", "events.json")
    )


def test_get_masks_password_by_default_and_reports_live_state(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "generated-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
        patch(
            "oduflow.docker_ops.service_database_ops._exec_sql",
            side_effect=[str(8 * 1024), "2"],
        ),
    ):
        result = service_database_ops.get_database(settings, team, "events")

    assert result["status"] == "ready"
    assert result["size_bytes"] == 8 * 1024
    assert result["connections"] == 2
    assert "password" not in result
    assert "url" not in result


def test_rotate_rolls_postgres_back_when_credentials_write_fails(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "old-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.generate_pg_password",
            return_value="new-password",
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.save", side_effect=OSError("disk")
        ),
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
    ):
        with pytest.raises(OSError, match="disk"):
            service_database_ops.rotate_password(settings, team, "events")

    sql = [call.args[2] for call in exec_sql.call_args_list]
    assert "new-password" in sql[0]
    assert "old-password" in sql[1]


def test_drifted_database_cannot_rotate(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "old-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="drifted"):
            service_database_ops.rotate_password(settings, team, "events")


@pytest.fixture
def prod_fixture(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="admin",
        db_password="secret",
        shared_db_container="oduflow-db",
        prod_enabled=True,
        prod_db_container="oduflow-prod-db",
        teams={"1": team},
    )
    return settings, team


def test_create_on_prod_cluster_targets_prod_pg_without_quota_or_tablespace(
    prod_fixture,
):
    settings, team = prod_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops.ensure_prod_infra") as infra,
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready") as wait_pg,
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ) as catalog,
        patch("oduflow.docker_ops.service_database_ops.check_db_quota") as quota,
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace"
        ) as tablespace,
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
        patch(
            "oduflow.docker_ops.service_database_ops.generate_pg_password",
            return_value="generated-password",
        ),
    ):
        result = service_database_ops.create_database(
            settings, team, "events", cluster="prod"
        )

    infra.assert_called_once()
    quota.assert_not_called()
    tablespace.assert_not_called()
    assert wait_pg.call_args.kwargs["container_name"] == "oduflow-prod-db"
    assert all(
        call.kwargs["container_name"] == "oduflow-prod-db"
        for call in catalog.call_args_list
    )
    sql_calls = exec_sql.call_args_list
    assert all(call.kwargs["container_name"] == "oduflow-prod-db" for call in sql_calls)
    sql = "\n".join(call.args[2] for call in sql_calls)
    assert "TABLESPACE" not in sql
    assert result["cluster"] == "prod"
    assert result["host"] == "oduflow-prod-db"
    assert result["url"].startswith("postgresql://")
    assert "@oduflow-prod-db:5432/" in result["url"]
    assert load(team, "events")["cluster"] == "prod"


def test_create_on_prod_cluster_requires_production_hosting(database_fixture):
    settings, team = database_fixture  # prod_enabled defaults to False
    with pytest.raises(PrerequisiteNotMetError, match="Production hosting is disabled"):
        service_database_ops.create_database(settings, team, "events", cluster="prod")


def test_create_rejects_unknown_cluster(database_fixture):
    settings, team = database_fixture
    with pytest.raises(ValueError, match="Unknown cluster"):
        service_database_ops.create_database(settings, team, "events", cluster="qa")


def test_get_reports_prod_database_unavailable_when_prod_pg_is_down(prod_fixture):
    settings, team = prod_fixture
    save_record(
        team,
        "events",
        {
            "name": "events",
            "database": "oduflow_service_1_events",
            "username": "svc_1_events",
            "password": "generated-password",
            "created_at": "2026-09-18T00:00:00+00:00",
            "cluster": "prod",
        },
    )
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch(
            "oduflow.docker_ops.service_database_ops._prod_pg_running",
            return_value=False,
        ),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready") as wait_pg,
    ):
        result = service_database_ops.get_database(settings, team, "events")

    wait_pg.assert_not_called()
    assert result["status"] == "unavailable"
    assert result["cluster"] == "prod"
    assert result["host"] == "oduflow-prod-db"
    assert "password" not in result


def test_delete_targets_the_record_cluster(prod_fixture):
    settings, team = prod_fixture
    save_record(
        team,
        "events",
        {
            "name": "events",
            "database": "oduflow_service_1_events",
            "username": "svc_1_events",
            "password": "generated-password",
            "created_at": "2026-09-18T00:00:00+00:00",
            "cluster": "prod",
        },
    )
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch(
            "oduflow.docker_ops.service_database_ops._prod_pg_running",
            return_value=True,
        ),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
        patch("oduflow.docker_ops.service_database_ops._drop_pg_role") as drop_role,
    ):
        service_database_ops.delete_database(settings, team, "events")

    assert exec_sql.call_args.kwargs["container_name"] == "oduflow-prod-db"
    assert drop_role.call_args.kwargs["container_name"] == "oduflow-prod-db"


def test_delete_refuses_prod_database_when_prod_pg_is_down(prod_fixture):
    settings, team = prod_fixture
    save_record(
        team,
        "events",
        {
            "name": "events",
            "database": "oduflow_service_1_events",
            "username": "svc_1_events",
            "password": "generated-password",
            "created_at": "2026-09-18T00:00:00+00:00",
            "cluster": "prod",
        },
    )
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch(
            "oduflow.docker_ops.service_database_ops._prod_pg_running",
            return_value=False,
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="not running"):
            service_database_ops.delete_database(settings, team, "events")


def test_protected_database_cannot_be_deleted_until_unprotected(database_fixture):
    settings, team = database_fixture
    save_record(
        team,
        "events",
        {
            "name": "events",
            "database": "oduflow_service_1_events",
            "username": "svc_1_events",
            "password": "generated-password",
            "created_at": "2026-09-18T00:00:00+00:00",
        },
    )
    assert service_database_ops.set_protected(team, "events", True) == {
        "name": "events",
        "protected": True,
    }

    with pytest.raises(ProtectedError, match="protected"):
        service_database_ops.delete_database(settings, team, "events")

    service_database_ops.set_protected(team, "events", False)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops._exec_sql"),
        patch("oduflow.docker_ops.service_database_ops._drop_pg_role"),
    ):
        result = service_database_ops.delete_database(settings, team, "events")
    assert result["name"] == "events"
