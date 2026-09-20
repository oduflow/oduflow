"""PostgreSQL databases managed as persistent storage for sidecar services."""

from __future__ import annotations

import datetime
import logging
from typing import Any
from urllib.parse import quote

from oduflow.docker_ops.client import get_client
from oduflow.docker_ops.system_ops import (
    _drop_pg_role,
    _exec_sql,
    _prod_pg_running,
    _wait_pg_ready,
    check_db_quota,
    ensure_prod_infra,
    ensure_team_network,
    ensure_team_tablespace,
)
from oduflow.env_credentials import generate_pg_password
from oduflow.errors import ConflictError, PrerequisiteNotMetError, ProtectedError
from oduflow.locking import keyed_mutex, service_database_registry_key
from oduflow.naming import (
    get_service_database_name,
    get_service_database_role,
    validate_service_database_name,
)
from oduflow.service_database_credentials import (
    delete as delete_credentials,
)
from oduflow.service_database_credentials import (
    exists as credentials_exist,
)
from oduflow.service_database_credentials import (
    list_names,
    load,
    save,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

CLUSTERS = ("dev", "prod")

_PRODUCTION_DISABLED_MESSAGE = (
    "Production hosting is disabled. Set enabled = true in the [production] "
    "section of oduflow.toml and restart Oduflow."
)


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def record_cluster(record: dict[str, Any]) -> str:
    """Cluster a credential record belongs to; records predate the field."""
    return record.get("cluster") or "dev"


def _cluster_container(settings: Settings, cluster: str) -> str:
    return (
        settings.prod_db_container
        if cluster == "prod"
        else settings.shared_db_container
    )


def _require_prod_pg(client: Any, settings: Settings, name: str, action: str) -> None:
    if not _prod_pg_running(client, settings):
        raise PrerequisiteNotMetError(
            f"Service database '{name}' lives on the production PostgreSQL "
            f"cluster, which is not running; cannot {action}. Enable and start "
            "production hosting first."
        )


def _catalog_exists(
    client: Any,
    settings: Settings,
    catalog: str,
    name: str,
    *,
    container_name: str | None = None,
) -> bool:
    safe = _sql_literal(name)
    query = {
        "database": f"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname='{safe}');",
        "role": f"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{safe}');",
    }[catalog]
    return _exec_sql(
        client, settings, query, container_name=container_name
    ).strip().lower() in {"t", "true"}


def _validate_record(team: TeamSettings, name: str, record: dict[str, Any]) -> None:
    expected_database = get_service_database_name(name, team.team_id)
    expected_role = get_service_database_role(name, team.team_id)
    if record["database"] != expected_database or record["username"] != expected_role:
        raise PrerequisiteNotMetError(
            f"Credentials for service database '{name}' do not match its managed identifiers."
        )


def _connection_fields(
    settings: Settings, record: dict[str, Any], *, reveal_password: bool
) -> dict[str, Any]:
    host = _cluster_container(settings, record_cluster(record))
    result: dict[str, Any] = {
        "cluster": record_cluster(record),
        "protected": bool(record.get("protected", False)),
        "host": host,
        "port": 5432,
        "database": record["database"],
        "username": record["username"],
    }
    if reveal_password:
        password = record["password"]
        result["password"] = password
        result["url"] = (
            "postgresql://"
            f"{quote(record['username'], safe='')}:{quote(password, safe='')}@"
            f"{host}:5432/{quote(record['database'], safe='')}"
        )
    return result


def create_database(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    cluster: str = "dev",
    stack_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an empty team-scoped database and its least-privilege owner.

    ``cluster`` selects the PostgreSQL instance: ``"dev"`` (default) is the
    shared development cluster, ``"prod"`` the dedicated production cluster
    (requires production hosting to be enabled). Production-cluster databases
    take part in the cluster-wide WAL-G backups but not in the development
    disk quota or per-production snapshots.
    """
    validate_service_database_name(name)
    if cluster not in CLUSTERS:
        raise ValueError(
            f"Unknown cluster '{cluster}'. Use one of: {', '.join(CLUSTERS)}."
        )
    if cluster == "prod" and not settings.prod_enabled:
        raise PrerequisiteNotMetError(_PRODUCTION_DISABLED_MESSAGE)
    if credentials_exist(team, name):
        raise ConflictError(f"Service database '{name}' already exists.")

    database = get_service_database_name(name, team.team_id)
    username = get_service_database_role(name, team.team_id)
    password = generate_pg_password()
    client = get_client()
    if cluster == "prod":
        # Provision the production PG container on demand so a prod-cluster
        # service database can exist before the first production. This also
        # attaches the container to every team network.
        ensure_prod_infra(client, settings, force=True)
    container_name = _cluster_container(settings, cluster)
    _wait_pg_ready(client, settings, container_name=container_name)
    ensure_team_network(client, settings, team)

    if _catalog_exists(
        client, settings, "database", database, container_name=container_name
    ) or _catalog_exists(
        client, settings, "role", username, container_name=container_name
    ):
        raise ConflictError(
            f"PostgreSQL resources for service database '{name}' already exist "
            "without matching managed credentials; resolve the drift before retrying."
        )

    # Registry section: the quota is read here and consumed by the CREATE
    # DATABASE below with nothing in between. The per-name lock the caller
    # holds does not serialise *different* names, so without this two
    # concurrent creates for one team would both pass admission against the
    # same stale usage figure and jointly overshoot db_quota_gb.
    with keyed_mutex(service_database_registry_key(team.team_id)):
        # The production cluster has no team tablespaces and is not under the
        # development disk quota (mirroring production Odoo databases).
        if cluster == "prod":
            create_database_sql = f'CREATE DATABASE "{database}" OWNER "{username}";'
        else:
            check_db_quota(client, settings, team)
            tablespace = ensure_team_tablespace(client, settings, team)
            create_database_sql = (
                f'CREATE DATABASE "{database}" OWNER "{username}" '
                f'TABLESPACE "{tablespace}";'
            )
        safe_password = _sql_literal(password)
        safe_comment = _sql_literal(
            f"Oduflow service database team={team.team_id} name={name}"
        )
        role_created = False
        database_created = False
        try:
            _exec_sql(
                client,
                settings,
                f'CREATE ROLE "{username}" WITH LOGIN NOSUPERUSER NOCREATEDB '
                f"NOCREATEROLE NOREPLICATION PASSWORD '{safe_password}';",
                container_name=container_name,
            )
            role_created = True
            _exec_sql(
                client,
                settings,
                create_database_sql,
                container_name=container_name,
            )
            database_created = True
            _exec_sql(
                client,
                settings,
                f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC; '
                f'GRANT CONNECT, TEMPORARY ON DATABASE "{database}" TO "{username}"; '
                f"COMMENT ON DATABASE \"{database}\" IS '{safe_comment}';",
                container_name=container_name,
            )
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record = {
                "name": name,
                "database": database,
                "username": username,
                "password": password,
                "created_at": created_at,
                "cluster": cluster,
            }
            if stack_labels:
                record.update(
                    {
                        "stack": stack_labels.get("oduflow.stack", ""),
                        "stack_resource": stack_labels.get(
                            "oduflow.stack-resource", ""
                        ),
                        "stack_spec_hash": stack_labels.get(
                            "oduflow.stack-spec-hash", ""
                        ),
                    }
                )
            save(team, name, record)
        except Exception:
            if database_created:
                try:
                    _exec_sql(
                        client,
                        settings,
                        f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE);',
                        container_name=container_name,
                    )
                except Exception:
                    logger.exception(
                        "Could not roll back service database %s", database
                    )
            if role_created:
                try:
                    _drop_pg_role(
                        client, settings, username, container_name=container_name
                    )
                except Exception:
                    logger.exception(
                        "Could not roll back service database role %s", username
                    )
            raise

    logger.info(
        "Created service database '%s' for team '%s' on the %s cluster",
        name,
        team.team_id,
        cluster,
    )
    return {
        "name": name,
        "created_at": created_at,
        "status": "ready",
        **_connection_fields(settings, record, reveal_password=True),
    }


def get_database(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    reveal_password: bool = False,
) -> dict[str, Any]:
    validate_service_database_name(name)
    record = load(team, name)
    _validate_record(team, name, record)
    client = get_client()
    cluster = record_cluster(record)
    container_name = _cluster_container(settings, cluster)
    if cluster == "prod" and not _prod_pg_running(client, settings):
        # The production cluster is stopped (or hosting is disabled): report
        # the record without probing PostgreSQL instead of stalling on a
        # readiness wait that cannot succeed.
        return {
            "name": name,
            "created_at": record["created_at"],
            "status": "unavailable",
            "database_exists": False,
            "role_exists": False,
            "size_bytes": 0,
            "connections": 0,
            "stack": record.get("stack", ""),
            "stack_resource": record.get("stack_resource", ""),
            "stack_spec_hash": record.get("stack_spec_hash", ""),
            **_connection_fields(settings, record, reveal_password=reveal_password),
        }
    _wait_pg_ready(client, settings, container_name=container_name)
    database_exists = _catalog_exists(
        client, settings, "database", record["database"], container_name=container_name
    )
    role_exists = _catalog_exists(
        client, settings, "role", record["username"], container_name=container_name
    )
    status = "ready" if database_exists and role_exists else "drifted"
    size_bytes = 0
    connections = 0
    if database_exists:
        safe_database = _sql_literal(record["database"])
        size_raw = _exec_sql(
            client,
            settings,
            f"SELECT pg_database_size('{safe_database}');",
            container_name=container_name,
        )
        connections_raw = _exec_sql(
            client,
            settings,
            f"SELECT count(*) FROM pg_stat_activity WHERE datname='{safe_database}';",
            container_name=container_name,
        )
        if size_raw.strip().isdigit():
            size_bytes = int(size_raw.strip())
        if connections_raw.strip().isdigit():
            connections = int(connections_raw.strip())
    return {
        "name": name,
        "created_at": record["created_at"],
        "status": status,
        "database_exists": database_exists,
        "role_exists": role_exists,
        "size_bytes": size_bytes,
        "connections": connections,
        "stack": record.get("stack", ""),
        "stack_resource": record.get("stack_resource", ""),
        "stack_spec_hash": record.get("stack_spec_hash", ""),
        **_connection_fields(settings, record, reveal_password=reveal_password),
    }


def list_databases(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in list_names(team):
        try:
            result.append(get_database(settings, team, name))
        except PrerequisiteNotMetError:
            # Ownership is unknowable when the record cannot be read. Report
            # the keys anyway so consumers distinguish this row by its status
            # instead of misreading a missing label as foreign ownership.
            result.append(
                {
                    "name": name,
                    "status": "credentials-error",
                    "database_exists": False,
                    "role_exists": False,
                    "size_bytes": 0,
                    "connections": 0,
                    "stack": "",
                    "stack_resource": "",
                    "stack_spec_hash": "",
                    "cluster": "dev",
                    "protected": False,
                }
            )
    return result


def rotate_password(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    validate_service_database_name(name)
    record = load(team, name)
    _validate_record(team, name, record)
    client = get_client()
    cluster = record_cluster(record)
    container_name = _cluster_container(settings, cluster)
    if cluster == "prod":
        _require_prod_pg(client, settings, name, "rotate its credentials")
    _wait_pg_ready(client, settings, container_name=container_name)
    if not _catalog_exists(
        client, settings, "database", record["database"], container_name=container_name
    ) or not _catalog_exists(
        client, settings, "role", record["username"], container_name=container_name
    ):
        raise PrerequisiteNotMetError(
            f"Service database '{name}' is drifted; restore its PostgreSQL resources before rotating credentials."
        )

    old_password = record["password"]
    new_password = generate_pg_password()
    safe_new = _sql_literal(new_password)
    _exec_sql(
        client,
        settings,
        f"ALTER ROLE \"{record['username']}\" WITH PASSWORD '{safe_new}';",
        container_name=container_name,
    )
    updated = {**record, "password": new_password}
    try:
        save(team, name, updated, overwrite=True)
    except Exception:
        safe_old = _sql_literal(old_password)
        try:
            _exec_sql(
                client,
                settings,
                f"ALTER ROLE \"{record['username']}\" WITH PASSWORD '{safe_old}';",
                container_name=container_name,
            )
        except Exception:
            logger.exception(
                "Could not roll back password rotation for service database %s", name
            )
        raise
    logger.info(
        "Rotated credentials for service database '%s' in team '%s'",
        name,
        team.team_id,
    )
    return {
        "name": name,
        "created_at": record["created_at"],
        "status": "ready",
        **_connection_fields(settings, updated, reveal_password=True),
    }


def delete_database(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, str]:
    validate_service_database_name(name)
    record = load(team, name)
    _validate_record(team, name, record)
    if record.get("protected"):
        raise ProtectedError(
            f"Service database '{name}' is protected. Unprotect it in the "
            "Oduflow dashboard before deleting."
        )
    client = get_client()
    cluster = record_cluster(record)
    container_name = _cluster_container(settings, cluster)
    if cluster == "prod":
        _require_prod_pg(client, settings, name, "delete it")
    _wait_pg_ready(client, settings, container_name=container_name)
    _exec_sql(
        client,
        settings,
        f'DROP DATABASE IF EXISTS "{record["database"]}" WITH (FORCE);',
        container_name=container_name,
    )
    _drop_pg_role(client, settings, record["username"], container_name=container_name)
    delete_credentials(team, name)
    logger.info("Deleted service database '%s' for team '%s'", name, team.team_id)
    return {
        "name": name,
        "database": record["database"],
        "username": record["username"],
    }


def set_protected(team: TeamSettings, name: str, protected: bool) -> dict[str, Any]:
    """Toggle deletion protection on a service database credential record.

    Pure metadata: no PostgreSQL access, so a database on a stopped production
    cluster can still be protected or unprotected.
    """
    validate_service_database_name(name)
    record = load(team, name)
    _validate_record(team, name, record)
    save(team, name, {**record, "protected": bool(protected)}, overwrite=True)
    logger.info(
        "Service database '%s' for team '%s' %s",
        name,
        team.team_id,
        "protected" if protected else "unprotected",
    )
    return {"name": name, "protected": bool(protected)}
