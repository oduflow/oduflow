"""The cluster superuser resolves no name a tenant controls.

A tenant owns the content of its database: tables and views, functions and
operators in ``public``, and the ``search_path`` every session starts with
(``ALTER DATABASE ... SET``). Even fixed SQL becomes tenant code run as the
superuser once it resolves one of those — ``res_users`` swapped for a view
over a function, ``public.!~~(name, name)`` beating the built-in ``LIKE``.

Three rules are pinned here against a fake Docker:

- queries over a tenant's own tables run as that database's scoped role, and
  an environment without one is refused instead of served by the superuser;
- whatever the superuser still runs, it runs with ``search_path`` pinned by a
  statement of its own, after the connection has applied the tenant's;
- no role but its owner may connect to a managed database.
"""

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import psql_call

from oduflow import postgres_migration as pm
from oduflow.docker_ops import odoo_ops, system_ops
from oduflow.env_credentials import MissingCredentialsError, recorded_roles
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings

SUPERUSER = "odoo"
DEV = "oduflow-db"
PROD = "oduflow-prod-db"
PIN = "SET search_path = pg_catalog, pg_temp"


class _Cluster:
    """One PostgreSQL container: logs every psql it is asked to run."""

    def __init__(self, answers=None, failures=None):
        self.calls: list[tuple[str, str, str]] = []
        self.other: list[list[str]] = []
        self._answers = answers or {}
        self._failures = failures or {}
        self.container = MagicMock()
        self.container.status = "running"
        self.container.exec_run.side_effect = self._exec

    def _exec(self, cmd, **kwargs):
        call = psql_call(cmd, SUPERUSER)
        if call is None:
            self.other.append(cmd)
            return 0, b""
        self.calls.append(call)
        sql = call[2]
        for marker, error in self._failures.items():
            if marker in sql:
                return 1, error.encode()
        for marker, answer in self._answers.items():
            if marker in sql:
                return 0, answer.encode()
        return 0, b""

    def sql(self, database=None):
        return [sql for _role, db, sql in self.calls if database in (None, db)]


def _client(**clusters):
    client = MagicMock(name="docker")
    by_name = {name.replace("_", "-"): cluster for name, cluster in clusters.items()}

    def get(name):
        import docker

        if name in by_name:
            return by_name[name].container
        raise docker.errors.NotFound(name)

    client.containers.get.side_effect = get
    return client


def _settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    return (
        Settings(
            base_data_dir=str(tmp_path),
            db_user=SUPERUSER,
            db_password="cluster-superuser-secret",
            teams={"1": team},
        ),
        team,
    )


def _record_role(team, env_name, role):
    workspace = os.path.join(team.workspaces_dir, env_name)
    os.makedirs(workspace)
    with open(os.path.join(workspace, "env_credentials.json"), "w") as fh:
        json.dump({"pg_user": role, "pg_password": "scoped-pw"}, fh)


# --------------------------------------------------------------------------
# the superuser's own session
# --------------------------------------------------------------------------


@pytest.mark.parametrize("database", ["postgres", "oduflow_1_main"])
def test_every_superuser_statement_follows_a_search_path_pin(tmp_path, database):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()

    system_ops._exec_sql(
        _client(oduflow_db=cluster), settings, "SELECT 1;", db=database
    )

    # The pin is its own -c, sent after the connection is established: ALTER
    # DATABASE ... SET search_path is applied while connecting. A failed pin
    # stops psql before the statement, and -q keeps its tag out of the result.
    assert cluster.container.exec_run.call_args.args[0] == [
        "psql",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        SUPERUSER,
        "-d",
        database,
        "-c",
        PIN,
        "-tAc",
        "SELECT 1;",
    ]


def test_a_scoped_role_keeps_its_own_search_path(tmp_path):
    # Its statements are the tenant's (sanitize scripts, reads of its tables)
    # and expect the tenant's schemas.
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()

    system_ops._exec_sql(
        _client(oduflow_db=cluster),
        settings,
        "SELECT 1;",
        db="oduflow_1_main",
        user="u_1_main",
    )

    assert cluster.container.exec_run.call_args.args[0] == [
        "psql",
        "-U",
        "u_1_main",
        "-d",
        "oduflow_1_main",
        "-tAc",
        "SELECT 1;",
    ]


def test_the_superuser_named_as_a_role_is_pinned_all_the_same(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()

    system_ops._exec_sql(
        _client(oduflow_db=cluster),
        settings,
        "SELECT 1;",
        db="oduflow_1_main",
        user=SUPERUSER,
    )

    assert cluster.calls == [(SUPERUSER, "oduflow_1_main", "SELECT 1;")]
    assert PIN in cluster.container.exec_run.call_args.args[0]


def test_post_clone_fixups_in_a_tenant_database_are_pinned(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()
    client = _client(oduflow_db=cluster)

    system_ops.reassign_db_ownership(client, settings, "oduflow_1_main", "u_1_main")
    system_ops.drop_signaling_sequences(client, settings, "oduflow_1_main")
    system_ops._own_public_schema(client, settings, "oduflow_1_main", "u_1_main")

    # psql_call refuses a superuser command without the pin, so reaching
    # this line means all four statements had it.
    assert [(role, db) for role, db, _sql in cluster.calls] == [
        (SUPERUSER, "oduflow_1_main")
    ] * 4
    assert cluster.other == []


def test_upgrade_plan_reads_tenant_databases_with_a_pinned_path(tmp_path):
    settings, team = _settings(tmp_path)
    _record_role(team, "prod-shop", "u_1_prod-shop")
    cluster = _Cluster(
        answers={
            "SELECT datname FROM pg_database": "postgres\nodoo\noduflow_1_prod-shop",
            "SELECT count(*)": "0",
            "SELECT extname": "pg_trgm",
            "pg_database_size": "10",
        }
    )
    client = _client(oduflow_prod_db=cluster)

    with (
        patch.object(
            pm.production_registry, "list_productions", return_value={"shop": {}}
        ),
        patch.object(pm.service_database_credentials, "list_names", return_value={}),
    ):
        databases, extensions, _restricted, _size = pm._inventory(
            client,
            settings,
            {"container": PROD, "kind": "prod", "old_major": 15},
            [],
        )

    assert databases == ["oduflow_1_prod-shop"]
    assert extensions == {"pg_trgm"}
    # Inside databases other than the maintenance one: the two bootstrap
    # databases are counted, the tenant's extensions are listed. All of it as
    # the superuser, none of it without the pin (psql_call checks each).
    inside = [(role, db) for role, db, _sql in cluster.calls if db != "postgres"]
    assert inside == [(SUPERUSER, "odoo"), (SUPERUSER, "oduflow_1_prod-shop")]
    assert cluster.other == []
    # The tenant's database is asked for a catalog table by its full name.
    (extension_query,) = cluster.sql("oduflow_1_prod-shop")
    assert "FROM pg_catalog.pg_extension" in extension_query


# --------------------------------------------------------------------------
# the "Connect as" picker
# --------------------------------------------------------------------------

_USERS = [
    {"login": "admin", "name": "Mitchell Admin", "share": False, "portal": False},
    {"login": "portal", "name": "Joel Willis", "share": True, "portal": True},
]


def _list_users(tmp_path, output, *, with_role=True):
    settings, team = _settings(tmp_path)
    if with_role:
        _record_role(team, "main", "u_1_main")
    database = MagicMock()
    database.exec_run.return_value = (0, output)
    client = MagicMock()
    client.containers.get.return_value = database
    with patch.object(odoo_ops, "get_client", return_value=client):
        return odoo_ops.list_env_users(settings, team, "main"), database


def test_users_are_listed_by_the_environments_own_role(tmp_path):
    users, database = _list_users(tmp_path, json.dumps(_USERS).encode() + b"\n")

    assert users == _USERS
    database.exec_run.assert_called_once()
    argv = database.exec_run.call_args.args[0]
    # res_users, res_partner and lower() are whatever the tenant made them:
    # they are resolved with the tenant's rights, not the superuser's.
    assert argv[:6] == ["psql", "-U", "u_1_main", "-d", "oduflow_1_main", "-tA"]
    assert argv[6] == "-c" and "FROM res_users u" in argv[7]
    assert SUPERUSER not in argv


def test_a_legacy_environment_gets_no_user_list_from_the_superuser(tmp_path):
    # No credentials file: the picker reports the error (connect_as_user
    # refuses such an environment too) and nothing is run.
    with pytest.raises(MissingCredentialsError, match="no scoped database"):
        _list_users(tmp_path, b"[]", with_role=False)


def test_a_legacy_environment_runs_no_query(tmp_path):
    settings, team = _settings(tmp_path)
    database = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = database
    with (
        patch.object(odoo_ops, "get_client", return_value=client),
        pytest.raises(MissingCredentialsError),
    ):
        odoo_ops.list_env_users(settings, team, "main")

    database.exec_run.assert_not_called()


@pytest.mark.parametrize(
    "output, expected",
    [
        (b"", []),
        (b"not json", []),
        # The answer is the tenant's to shape: anything but a list of users
        # with a login is dropped, and the flags are booleans or false.
        (b'{"login": "admin"}', []),
        (b'["admin", 7, null]', []),
        (
            b'[{"login": "a", "name": {"x": 1}, "share": "yes", "portal": 1},'
            b' {"login": 5}, {"name": "nobody"}]',
            [{"login": "a", "name": "", "share": False, "portal": False}],
        ),
    ],
)
def test_the_tenants_answer_is_taken_as_data(tmp_path, output, expected):
    users, _database = _list_users(tmp_path, output)

    assert users == expected


# --------------------------------------------------------------------------
# CONNECT on managed databases
# --------------------------------------------------------------------------


def test_a_new_database_is_closed_to_public(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()

    system_ops.restrict_database_access(
        _client(oduflow_prod_db=cluster),
        settings,
        "oduflow_1_main",
        container_name=PROD,
    )

    # From the maintenance database: nothing of the new one is touched.
    assert cluster.calls == [
        (SUPERUSER, "postgres", 'REVOKE ALL ON DATABASE "oduflow_1_main" FROM PUBLIC;')
    ]


_PUBLIC_ACCESS_MARKER = "pg_catalog.aclexplode"


def _legacy_cluster(databases, roles, **kwargs):
    return _Cluster(
        answers={
            _PUBLIC_ACCESS_MARKER: "\n".join(databases),
            "FROM pg_catalog.pg_roles": "\n".join(roles),
        },
        **kwargs,
    )


def test_existing_databases_lose_public_access_at_startup(tmp_path):
    settings, team = _settings(tmp_path)
    _record_role(team, "main", "u_1_main")
    _record_role(team, "prod-erp", "u_1_prod-erp")
    cluster = _legacy_cluster(
        [
            "postgres",
            "template1",
            "odoo",
            "manual_reporting",
            "oduflow_1_main",
            "oduflow_1_prod-erp",
            "oduflow_template_1_base",
        ],
        ["odoo", "u_1_main", "u_1_prod-erp", "oduflow_tplown_1"],
    )

    restricted = system_ops.restrict_managed_databases(
        _client(oduflow_db=cluster), settings
    )

    # Only Oduflow's own namespace: the maintenance databases stay reachable
    # (Odoo lists databases through "postgres") and an operator's database is
    # not Oduflow's to close.
    assert restricted == [
        "oduflow_1_main",
        "oduflow_1_prod-erp",
        "oduflow_template_1_base",
    ]
    statements = [sql for sql in cluster.sql("postgres") if "REVOKE" in sql]
    assert statements == [
        # The role on record keeps its access even where it is not the owner
        # (a production an older version restored is owned by the superuser):
        # granted and revoked in one transaction.
        'GRANT CONNECT, TEMPORARY ON DATABASE "oduflow_1_main" TO "u_1_main"; '
        'REVOKE ALL ON DATABASE "oduflow_1_main" FROM PUBLIC;',
        'GRANT CONNECT, TEMPORARY ON DATABASE "oduflow_1_prod-erp" '
        'TO "u_1_prod-erp"; '
        'REVOKE ALL ON DATABASE "oduflow_1_prod-erp" FROM PUBLIC;',
        # A template has no role on record; its owner's rights are its own.
        'REVOKE ALL ON DATABASE "oduflow_template_1_base" FROM PUBLIC;',
    ]
    # Everything from the maintenance database, as the pinned superuser.
    assert {(role, db) for role, db, _sql in cluster.calls} == {(SUPERUSER, "postgres")}


def test_the_lookup_reads_the_default_privileges_of_untouched_databases(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _legacy_cluster([], [])

    assert (
        system_ops.restrict_managed_databases(_client(oduflow_db=cluster), settings)
        == []
    )

    # One catalog query and nothing else once every database is closed. A
    # database nobody ran GRANT or REVOKE on has a NULL ACL: its defaults,
    # which include PUBLIC, are what counts.
    (query,) = cluster.sql()
    assert "pg_catalog.acldefault('d', d.datdba)" in query
    assert "a.grantee OPERATOR(pg_catalog.=) 0" in query


def test_a_role_missing_from_the_cluster_is_not_granted(tmp_path):
    # A development environment's credentials describe a role of the
    # development cluster; GRANT to an unknown role would fail the revoke too.
    settings, team = _settings(tmp_path)
    _record_role(team, "main", "u_1_main")
    cluster = _legacy_cluster(["oduflow_1_main"], ["odoo"])

    system_ops.restrict_managed_databases(_client(oduflow_db=cluster), settings)

    assert cluster.sql()[-1] == 'REVOKE ALL ON DATABASE "oduflow_1_main" FROM PUBLIC;'


def test_one_failing_database_does_not_stop_the_others(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _legacy_cluster(
        ["oduflow_1_a", "oduflow_1_b"],
        ["odoo"],
        failures={'"oduflow_1_a" FROM PUBLIC': "ERROR: tuple concurrently updated"},
    )

    restricted = system_ops.restrict_managed_databases(
        _client(oduflow_db=cluster), settings
    )

    assert restricted == ["oduflow_1_b"]


def test_startup_checks_both_clusters_and_survives_a_failure(tmp_path):
    settings, _team = _settings(tmp_path)
    client = MagicMock()
    with patch.object(
        system_ops,
        "restrict_managed_databases",
        side_effect=[ExternalCommandError("psql", 1, "boom"), []],
    ) as restrict:
        system_ops._restrict_cluster_databases(client, settings, DEV)
        system_ops._restrict_cluster_databases(client, settings, PROD)

    assert [call.kwargs for call in restrict.call_args_list] == [
        {"container_name": DEV},
        {"container_name": PROD},
    ]


def _clusters_checked_by_init_system(tmp_path, *, guard):
    """Cluster containers init_system checks, with both of them running."""
    settings, _team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.return_value.status = "running"
    with ExitStack() as stack:
        for name in (
            "_ensure_iptables_accept",
            "_ensure_traefik",
            "_ensure_pg_container",
            "_wait_pg_ready",
            "ensure_team_network",
            "_reconcile_pg_hba",
            "reconcile_prod_workloads",
        ):
            stack.enter_context(patch.object(system_ops, name))
        stack.enter_context(patch.object(system_ops, "get_client", return_value=client))
        restrict = stack.enter_context(
            patch.object(system_ops, "_restrict_cluster_databases")
        )
        stack.enter_context(patch("oduflow.wal_monitor.state", return_value=guard))
        # What follows the database steps is not these tests' subject.
        stack.enter_context(
            patch(
                "oduflow.production_registry.clear_stale_deploy_flags",
                side_effect=RuntimeError("stop here"),
            )
        )
        with pytest.raises(RuntimeError, match="stop here"):
            system_ops.init_system(settings)
    return settings, [call.args[2] for call in restrict.call_args_list]


def test_init_system_closes_databases_of_both_running_clusters(tmp_path):
    settings, checked = _clusters_checked_by_init_system(
        tmp_path, guard={"latched": False}
    )

    assert checked == [settings.shared_db_container, settings.prod_db_container]


def test_a_latched_wal_guard_keeps_the_production_cluster_untouched(tmp_path):
    # The guard holds a cluster that must not be written to.
    settings, checked = _clusters_checked_by_init_system(
        tmp_path, guard={"latched": True}
    )

    assert checked == [settings.shared_db_container]


def test_roles_on_record_are_read_per_workspace(tmp_path):
    _settings_, team = _settings(tmp_path)
    _record_role(team, "main", "u_1_main")
    _record_role(team, "feature-x", "u_1_feature-x")
    os.makedirs(os.path.join(team.workspaces_dir, "legacy"))
    broken = os.path.join(team.workspaces_dir, "broken")
    os.makedirs(broken)
    with open(os.path.join(broken, "env_credentials.json"), "w") as fh:
        fh.write("{not json")

    assert recorded_roles(team.workspaces_dir) == {
        "main": "u_1_main",
        "feature-x": "u_1_feature-x",
    }
    assert recorded_roles(str(tmp_path / "missing")) == {}
