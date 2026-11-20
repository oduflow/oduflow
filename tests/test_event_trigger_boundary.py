"""The cluster superuser fires no event trigger of a tenant's database.

An event trigger runs its function with the rights of whoever issues the
statement that fires it: every DDL statement of its database and, since
PostgreSQL 17, every login to it. Only a superuser can create one — which a
dump could, while older versions loaded dumps as the superuser. ``CREATE
DATABASE ... TEMPLATE`` copies it into every clone, where the function behind
it is handed to the clone's tenant. The function is called by OID, so the
pinned ``search_path`` is no help.

Pinned here against a fake Docker:

- a superuser session that enters a database other than the maintenance one
  switches event triggers off with the connection, where the server can
  (PostgreSQL 17+), and a scoped role's session never carries that setting;
- before the superuser runs DDL inside such a database, the enabled event
  triggers there are disabled — on every release, and without any cost but
  one catalog query when there are none;
- the start reports what else only a superuser could have left in a managed
  database, and changes none of it;
- a PostgreSQL upgrade refuses, up front, a database whose own role could not
  recreate an event trigger in the new cluster.
"""

import ast
import json
import logging
import pathlib
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import psql_call

import docker
import oduflow
from oduflow import postgres_migration as pm
from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

SUPERUSER = "odoo"
DEV = "oduflow-db"
PROD = "oduflow-prod-db"
ENV_DB = "oduflow_1_main"
ENV_ROLE = "u_1_main"
QUIET = {"PGOPTIONS": "-c event_triggers=off"}
PG16 = "160004"
PG17 = "170002"
AUDIT = {"name": "audit_ddl", "event": "ddl_command_start", "function": "audit()"}

pytestmark = pytest.mark.usefixtures("real_cluster_questions")


class _Cluster:
    """One PostgreSQL container.

    Logs every ``psql`` session with the environment it was given, answers
    the two questions the superuser asks, and keeps the enabled event
    triggers of each database until the block that disables them runs.
    """

    def __init__(
        self, name=DEV, *, version=PG16, triggers=None, answers=None, stubborn=False
    ):
        self.name = name
        self.version = version
        self.triggers = {db: list(found) for db, found in (triggers or {}).items()}
        self.answers = answers or {}
        self.stubborn = stubborn
        # (role, database, sql, environment)
        self.sessions: list[tuple[str, str, str, dict | None]] = []
        self.other: list[tuple[list[str], dict | None]] = []
        self.scan_output = b""
        self.container = MagicMock(name=name)
        self.container.id = f"id-of-{name}-{id(self)}"
        self.container.status = "running"
        self.container.exec_run.side_effect = self._exec

    def _exec(self, cmd, **kwargs):
        assert set(kwargs) <= {"environment"}, kwargs
        environment = kwargs.get("environment")
        call = psql_call(cmd, SUPERUSER)
        if call is None:
            self.other.append((cmd, environment))
            return 0, self.scan_output
        role, database, sql = call
        self.sessions.append((role, database, sql, environment))
        if sql == "SHOW server_version_num;":
            return 0, self.version.encode()
        if sql == system_ops._ENABLED_EVENT_TRIGGERS_SQL:
            answer = self.answers.get("event triggers")
            if answer is not None:
                return 0, answer.encode()
            return 0, json.dumps(self.triggers.get(database, [])).encode()
        if sql == system_ops._DISABLE_EVENT_TRIGGERS_SQL and not self.stubborn:
            self.triggers[database] = []
        for marker, answer in self.answers.items():
            if marker in sql:
                return 0, answer.encode()
        return 0, b""

    def inside(self, database):
        """Statements run in *database*, in order."""
        return [sql for _role, db, sql, _env in self.sessions if db == database]

    def environments(self, database):
        return [env for _role, db, _sql, env in self.sessions if db == database]


def _client(*clusters):
    by_name = {cluster.name: cluster.container for cluster in clusters}
    client = MagicMock(name="docker")

    def get(name):
        if name in by_name:
            return by_name[name]
        raise docker.errors.NotFound(name)

    client.containers.get.side_effect = get
    return client


def _settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user=SUPERUSER,
        db_password="cluster-superuser-secret",
        teams={"1": team},
    )
    return settings, team


GUARD = system_ops._ENABLED_EVENT_TRIGGERS_SQL
DISABLE = system_ops._DISABLE_EVENT_TRIGGERS_SQL


# --------------------------------------------------------------------------
# the session: PGOPTIONS on a server that has the switch
# --------------------------------------------------------------------------


def test_a_session_in_a_tenant_database_fires_no_event_trigger_on_17(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=PG17)

    system_ops._exec_sql(_client(cluster), settings, "SELECT 1;", db=ENV_DB)

    # The release is asked in the maintenance database, where the session
    # cannot carry the option yet. The session that enters the tenant's
    # database is given it with the connection: an ON login trigger fires
    # before any statement could switch it off.
    assert cluster.sessions == [
        (SUPERUSER, "postgres", "SHOW server_version_num;", None),
        (SUPERUSER, ENV_DB, "SELECT 1;", QUIET),
    ]


@pytest.mark.parametrize("version", ["150014", PG16, "169999"])
def test_an_older_server_is_not_sent_a_setting_it_does_not_know(tmp_path, version):
    # It would refuse the connection over it. It has no login triggers, and
    # DDL is covered by disabling the triggers themselves.
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=version)

    system_ops._exec_sql(_client(cluster), settings, "SELECT 1;", db=ENV_DB)

    assert cluster.sessions[-1] == (SUPERUSER, ENV_DB, "SELECT 1;", None)
    # No ``environment`` argument at all, as before.
    assert cluster.container.exec_run.call_args.kwargs == {}


@pytest.mark.parametrize(
    ("version", "supported"),
    [(150014, False), (160010, False), (170000, True), (170002, True), (180001, True)],
)
def test_the_switch_exists_from_postgresql_17(version, supported):
    assert system_ops.event_trigger_switch_supported(version) is supported


def test_the_release_is_asked_once_per_container(tmp_path):
    settings, _team = _settings(tmp_path)
    dev, prod = _Cluster(version=PG17), _Cluster(PROD, version=PG16)
    client = _client(dev, prod)

    for _ in range(3):
        system_ops._exec_sql(client, settings, "SELECT 1;", db=ENV_DB)
        system_ops._exec_sql(
            client, settings, "SELECT 1;", db=ENV_DB, container_name=PROD
        )

    # A container never changes its image; another container is another
    # cluster, with a release of its own.
    assert len(dev.inside("postgres")) == 1 and len(prod.inside("postgres")) == 1
    assert dev.environments(ENV_DB) == [QUIET] * 3
    assert prod.environments(ENV_DB) == [None] * 3


def test_the_maintenance_database_and_scoped_roles_are_left_alone(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=PG17)
    client = _client(cluster)

    system_ops._exec_sql(client, settings, "CREATE DATABASE x;")
    system_ops._exec_sql(client, settings, "SELECT 1;", db=ENV_DB, user=ENV_ROLE)

    # Nothing of a tenant's is in the maintenance database. And the setting
    # is a superuser's to set: a role's session sent it would not connect.
    assert cluster.sessions == [
        (SUPERUSER, "postgres", "CREATE DATABASE x;", None),
        (ENV_ROLE, ENV_DB, "SELECT 1;", None),
    ]


def test_the_superuser_named_as_a_role_is_a_superuser_session(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=PG17)

    system_ops._exec_sql(
        _client(cluster), settings, "SELECT 1;", db=ENV_DB, user=SUPERUSER
    )

    assert cluster.sessions[-1] == (SUPERUSER, ENV_DB, "SELECT 1;", QUIET)


@pytest.mark.parametrize("answer", ["", "PostgreSQL 17", "17.2"])
def test_an_unreadable_release_stops_the_session(tmp_path, answer):
    # Guessing would either refuse every connection or leave a login trigger
    # its chance.
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=answer)

    with pytest.raises(ExternalCommandError, match="server_version_num"):
        system_ops._exec_sql(_client(cluster), settings, "SELECT 1;", db=ENV_DB)

    assert cluster.inside(ENV_DB) == []


# --------------------------------------------------------------------------
# DDL: the triggers are disabled first
# --------------------------------------------------------------------------


def _reassign(client, settings, cluster=None):
    system_ops.reassign_db_ownership(
        client, settings, ENV_DB, ENV_ROLE, container_name=cluster
    )


def _drop_sequences(client, settings, cluster=None):
    system_ops.drop_signaling_sequences(
        client, settings, ENV_DB, container_name=cluster
    )


def _own_public(client, settings, cluster=None):
    system_ops._own_public_schema(
        client, settings, ENV_DB, ENV_ROLE, container_name=cluster
    )


_DDL = pytest.mark.parametrize(
    ("run", "statements"),
    [(_reassign, 2), (_drop_sequences, 1), (_own_public, 1)],
    ids=["reassign_db_ownership", "drop_signaling_sequences", "_own_public_schema"],
)


@_DDL
def test_a_database_without_event_triggers_costs_one_catalog_query(
    tmp_path, run, statements, caplog
):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()

    with caplog.at_level(logging.WARNING, logger="oduflow"):
        run(_client(cluster), settings)

    inside = cluster.inside(ENV_DB)
    # One read of pg_event_trigger, then the statements as they always were.
    assert inside[0] == GUARD
    assert len(inside) == 1 + statements
    assert DISABLE not in inside and GUARD not in inside[1:]
    assert caplog.records == []


@_DDL
def test_enabled_event_triggers_are_disabled_before_any_ddl(
    tmp_path, run, statements, caplog
):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(triggers={ENV_DB: [AUDIT]})

    with caplog.at_level(logging.WARNING, logger="oduflow"):
        run(_client(cluster), settings)

    inside = cluster.inside(ENV_DB)
    # Found, disabled, found gone — and only then the DDL that would have
    # run audit() with superuser rights.
    assert inside[:3] == [GUARD, DISABLE, GUARD]
    assert len(inside) == 3 + statements
    assert not any(sql in (GUARD, DISABLE) for sql in inside[3:])
    (record,) = caplog.records
    message = record.getMessage()
    assert "Disabled 1 event trigger(s)" in message
    assert ENV_DB in message and DEV in message
    assert "audit_ddl (on ddl_command_start, runs audit())" in message
    assert "rebuild the template" in message


@_DDL
def test_the_guard_runs_in_the_cluster_the_ddl_runs_in(tmp_path, run, statements):
    settings, _team = _settings(tmp_path)
    dev, prod = _Cluster(), _Cluster(PROD, triggers={ENV_DB: [AUDIT]})

    run(_client(dev, prod), settings, PROD)

    assert dev.sessions == []
    assert prod.inside(ENV_DB)[:3] == [GUARD, DISABLE, GUARD]


@_DDL
def test_on_17_every_one_of_those_sessions_is_quiet_as_well(tmp_path, run, statements):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=PG17, triggers={ENV_DB: [AUDIT]})

    run(_client(cluster), settings)

    # Reading the catalog, disabling and the DDL each log in to the tenant's
    # database: none of them may fire an ON login trigger.
    environments = cluster.environments(ENV_DB)
    assert len(environments) == 3 + statements
    assert environments == [QUIET] * len(environments)


@_DDL
def test_no_ddl_runs_on_an_answer_that_cannot_be_read(tmp_path, run, statements):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(answers={"event triggers": "5"})

    with pytest.raises(ExternalCommandError, match="event triggers of database"):
        run(_client(cluster), settings)

    assert cluster.inside(ENV_DB) == [GUARD]


@_DDL
def test_no_ddl_runs_while_an_event_trigger_stays_enabled(tmp_path, run, statements):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(triggers={ENV_DB: [AUDIT]}, stubborn=True)

    with pytest.raises(PrerequisiteNotMetError, match="DROP EVENT TRIGGER"):
        run(_client(cluster), settings)

    assert cluster.inside(ENV_DB) == [GUARD, DISABLE, GUARD]


def test_the_answer_is_found_among_what_psql_says_besides(tmp_path):
    # stderr arrives in the same stream as the row.
    settings, _team = _settings(tmp_path)
    noisy = (
        'WARNING:  database "oduflow_1_main" has a collation version mismatch\n'
        "DETAIL:  The database was created using collation version 2.31.\n"
        + json.dumps([AUDIT])
    )
    cluster = _Cluster(answers={"event triggers": noisy})

    found = system_ops.enabled_event_triggers(_client(cluster), settings, ENV_DB)

    assert found == [AUDIT]


def test_trigger_names_never_become_sql_or_log_lines_of_their_own(tmp_path, caplog):
    # A name is whatever a dump said.
    settings, _team = _settings(tmp_path)
    hostile = {
        "name": 'x"; ALTER ROLE u_1_main SUPERUSER; --\nWARNING forged line',
        "event": "ddl_command_start",
        "function": "public.f()",
    }
    cluster = _Cluster(triggers={ENV_DB: [hostile]})

    with caplog.at_level(logging.WARNING, logger="oduflow"):
        disabled = system_ops.disable_event_triggers(_client(cluster), settings, ENV_DB)

    assert disabled == [hostile["name"]]
    (record,) = caplog.records
    assert "\n" not in record.getMessage()
    assert "--?WARNING forged line (on ddl_command_start" in record.getMessage()
    # The statement that disables is a constant: the server quotes each name
    # it reads from its own catalog.
    assert "SUPERUSER" not in "".join(cluster.inside(ENV_DB))
    assert "format('ALTER EVENT TRIGGER %I DISABLE', r.evtname)" in DISABLE
    assert DISABLE.startswith("DO $$ DECLARE r RECORD; BEGIN SET LOCAL search_path")


def test_the_guard_reads_one_catalog_table_by_its_full_name():
    # It is the superuser inside a tenant's database: nothing in the query
    # may resolve through the tenant's schemas, pinned path or not.
    assert "FROM pg_catalog.pg_event_trigger t" in GUARD
    assert "OPERATOR(pg_catalog.<>) 'D'" in GUARD
    assert " = " not in GUARD and " <> " not in GUARD
    # Names go into JSON as text: no cast of the dump's is looked up for a
    # built-in type.
    assert "t.evtname::pg_catalog.text" in GUARD


def test_a_template_load_disables_before_it_hands_over(tmp_path):
    # REASSIGN OWNED and DROP OWNED fire ddl_command_start too. The loader
    # cannot create an event trigger, but an extension the operator's image
    # marks trusted can: its script runs as the bootstrap superuser.
    settings, _team = _settings(tmp_path)
    tpl = "oduflow_template_1_base"
    cluster = _Cluster(triggers={tpl: [AUDIT]}, answers={"FROM pg_database": "1"})

    system_ops._retire_template_loader(
        _client(cluster),
        settings,
        "oduflow_load_0123456789abcdef",
        handover=(tpl, "oduflow_tplown_1"),
    )

    inside = cluster.inside(tpl)
    assert inside[:3] == [GUARD, DISABLE, GUARD]
    assert inside[3].startswith('REASSIGN OWNED BY "oduflow_load_0123456789abcdef"')
    assert "DROP OWNED BY" in inside[3]
    assert len(inside) == 4


def test_a_role_is_provisioned_with_the_triggers_disabled_first(tmp_path):
    # update_environment / reconfigure_production on a database that never
    # had a role: an older clone of a template with an event trigger.
    settings, team = _settings(tmp_path)
    cluster = _Cluster(triggers={ENV_DB: [AUDIT]}, answers={"FROM pg_database": "1"})

    creds = system_ops.provision_scoped_role(_client(cluster), settings, team, "main")

    inside = cluster.inside(ENV_DB)
    assert inside[:3] == [GUARD, DISABLE, GUARD]
    assert inside[3] == f'ALTER SCHEMA public OWNER TO "{creds["pg_user"]}";'
    assert "ALTER ROUTINE" in inside[4]
    # ALTER DATABASE ... OWNER and the role itself are statements of the
    # maintenance database, about shared objects: no event trigger fires
    # for those, and none of the tenant's is there.
    assert f'ALTER DATABASE "{ENV_DB}" OWNER TO "{creds["pg_user"]}";' in (
        cluster.inside("postgres")
    )


def test_a_clone_is_fixed_up_as_before_when_its_template_is_clean(tmp_path):
    # The whole post-clone sequence of create_environment, statement by
    # statement: two catalog reads are all that is new.
    settings, _team = _settings(tmp_path)
    cluster = _Cluster()
    client = _client(cluster)

    system_ops.reassign_db_ownership(client, settings, ENV_DB, ENV_ROLE)
    system_ops.drop_signaling_sequences(client, settings, ENV_DB)

    inside = cluster.inside(ENV_DB)
    assert [sql == GUARD for sql in inside] == [True, False, False, True, False]
    assert inside[1] == f'ALTER SCHEMA public OWNER TO "{ENV_ROLE}";'
    assert "ALTER ROUTINE" in inside[2] and "DROP SEQUENCE IF EXISTS" in inside[4]
    # PostgreSQL 16: nothing but the release was asked of the cluster, once.
    assert cluster.inside("postgres") == ["SHOW server_version_num;"]
    assert cluster.environments(ENV_DB) == [None] * 5


# --------------------------------------------------------------------------
# every superuser statement inside a database is accounted for
# --------------------------------------------------------------------------

# function -> why its superuser statements inside a named database fire no
# event trigger. A new one has to disable them first or belong here.
_READS_ONLY = {
    # The guard itself: one catalog read, and ALTER EVENT TRIGGER, which
    # fires no event trigger on any release.
    ("docker_ops/system_ops.py", "enabled_event_triggers"),
    ("docker_ops/system_ops.py", "disable_event_triggers"),
    # The upgrade plan: catalog reads of every database it carries over.
    ("postgres_migration.py", "_inventory"),
}


def _superuser_sessions_in_a_named_database():
    """(file, function, disables first) of each ``_exec_sql`` naming a database
    and no role."""
    root = pathlib.Path(oduflow.__file__).parent
    found = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                node
                for node in ast.walk(scope)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                in ("_exec_sql", "disable_event_triggers")
            ]
            guards = [
                call.lineno
                for call in calls
                if getattr(call.func, "id", getattr(call.func, "attr", ""))
                == "disable_event_triggers"
            ]
            for call in calls:
                name = getattr(call.func, "id", getattr(call.func, "attr", ""))
                keywords = {keyword.arg for keyword in call.keywords}
                if name != "_exec_sql" or "user" in keywords:
                    continue
                if "db" not in keywords and len(call.args) < 4:
                    continue
                found.add(
                    (
                        path.relative_to(root).as_posix(),
                        scope.name,
                        any(line < call.lineno for line in guards),
                    )
                )
    return found


def test_every_superuser_statement_inside_a_database_is_guarded_or_a_read():
    sessions = _superuser_sessions_in_a_named_database()

    unguarded = {
        (file, function) for file, function, guarded in sessions if not guarded
    }
    assert unguarded == _READS_ONLY
    # The four places the superuser runs DDL over a tenant's content.
    assert {(file, function) for file, function, guarded in sessions if guarded} == {
        ("docker_ops/system_ops.py", "reassign_db_ownership"),
        ("docker_ops/system_ops.py", "drop_signaling_sequences"),
        ("docker_ops/system_ops.py", "_own_public_schema"),
        ("docker_ops/system_ops.py", "_retire_template_loader"),
    }


# --------------------------------------------------------------------------
# the dumps the superuser still takes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "options"),
    [
        # 17 has both settings; a login trigger fires for pg_dump's own login.
        (
            PG17,
            "-c restrict_nonsystem_relation_kind=view,foreign-table "
            "-c event_triggers=off",
        ),
        # A patched 16 knows the first only, and has no login triggers.
        (PG16, "-c restrict_nonsystem_relation_kind=view,foreign-table"),
    ],
)
def test_a_superuser_dump_logs_in_quietly_where_logins_can_fire(
    tmp_path, version, options
):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version=version)

    environment = system_ops.superuser_dump_environment(
        _client(cluster), settings, "oduflow_template_1_base"
    )

    assert environment == {"PGOPTIONS": options}


def test_an_unpatched_15_gets_neither_setting(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(version="150007")

    assert (
        system_ops.superuser_dump_environment(
            _client(cluster), settings, "oduflow_template_1_base"
        )
        == {}
    )


# --------------------------------------------------------------------------
# the report on start
# --------------------------------------------------------------------------

_LISTING = "\n".join(
    [
        "postgres|f|t",
        "odoo|f|t",
        "oduflow_template_1_base|t|t",
        "oduflow_template_1_clean|t|f",
        "oduflow_1_main|f|f",
        "oduflow_1_other|f|f",
        "oduflow_1_gone|f|f",
        # Not a name Oduflow gives a database: never handed to a shell.
        "oduflow_1_x; rm -rf /|f|f",
        "operators_own|f|f",
    ]
)


def _row(database, **objects):
    return json.dumps({"database": database, **objects})


def _scan(tmp_path, *, version=PG16, rows=(), name=DEV):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(name, version=version, answers={"d.datallowconn": _LISTING})
    cluster.scan_output = "\n".join(rows).encode()
    return settings, cluster, _client(cluster)


def test_the_scan_is_one_exec_over_the_managed_databases(tmp_path):
    settings, cluster, client = _scan(tmp_path)

    system_ops.superuser_only_objects(client, settings)

    ((argv, environment),) = cluster.other
    assert argv[:5] == ["timeout", "120", "sh", "-c", system_ops._SCAN_SCRIPT]
    # The names are arguments of the shell, never its text.
    assert argv[5:] == [
        "sh",
        "oduflow_1_gone",
        "oduflow_1_main",
        "oduflow_1_other",
        "oduflow_template_1_base",
        "oduflow_template_1_clean",
    ]
    # So are the role, the pin and the query: environment, not text.
    assert environment == {
        "ODUFLOW_SCAN_ROLE": SUPERUSER,
        "ODUFLOW_SCAN_PIN": system_ops.SUPERUSER_SEARCH_PATH_SQL,
        "ODUFLOW_SCAN_SQL": system_ops._SUPERUSER_ONLY_OBJECTS_SQL,
    }
    script = system_ops._SCAN_SCRIPT
    assert 'for db in "$@"' in script and '-d "$db"' in script
    # The pin comes first and a failed pin stops that psql.
    assert '-v ON_ERROR_STOP=1 -U "$ODUFLOW_SCAN_ROLE"' in script
    assert script.index('-c "$ODUFLOW_SCAN_PIN"') < script.index(
        '-c "$ODUFLOW_SCAN_SQL"'
    )
    # A database being copied makes its session wait; the others go on.
    assert "timeout 10 psql -X -q -tA" in script


def test_the_scan_logs_in_quietly_on_17(tmp_path):
    settings, cluster, client = _scan(tmp_path, version=PG17)

    system_ops.superuser_only_objects(client, settings)

    ((_argv, environment),) = cluster.other
    assert environment["PGOPTIONS"] == "-c event_triggers=off"


def test_the_scan_reads_each_databases_row_and_marks_the_rest(tmp_path):
    rows = [
        'psql: error: connection to server failed: FATAL:  database "x" is gone',
        "oduflow-scan-failed oduflow_1_gone",
        _row(
            "oduflow_template_1_base",
            event_triggers=[{**AUDIT, "enabled": True, "function_owner": "odoo"}],
            untrusted_functions=["public.run(text) in plpython3u"],
            definer_functions=[],
            user_mappings=0,
        ),
        _row("oduflow_1_main", event_triggers=[], user_mappings=0),
        _row("oduflow_template_1_clean", user_mappings=0),
        # A row for a database that was not asked about is not taken.
        _row("oduflow_1_elsewhere", foreign_servers=["loop"]),
    ]
    settings, _cluster, client = _scan(tmp_path, rows=rows)

    found = system_ops.superuser_only_objects(client, settings)

    assert set(found) == {
        "oduflow_1_gone",
        "oduflow_1_main",
        "oduflow_1_other",
        "oduflow_template_1_base",
        "oduflow_template_1_clean",
    }
    assert found["oduflow_template_1_base"] == {
        "template": True,
        "superuser_owned": True,
        "checked": True,
        "objects": {
            "event_triggers": [{**AUDIT, "enabled": True, "function_owner": "odoo"}],
            "untrusted_functions": ["public.run(text) in plpython3u"],
        },
    }
    assert found["oduflow_1_main"] == {
        "template": False,
        "superuser_owned": False,
        "checked": True,
        "objects": {},
    }
    assert found["oduflow_template_1_clean"]["superuser_owned"] is False
    assert {name for name, entry in found.items() if not entry["checked"]} == {
        "oduflow_1_gone",
        "oduflow_1_other",
    }


def test_a_cluster_without_managed_databases_is_one_query(tmp_path):
    settings, _team = _settings(tmp_path)
    cluster = _Cluster(answers={"d.datallowconn": "postgres|f|t\nodoo|f|t"})

    assert system_ops.superuser_only_objects(_client(cluster), settings) == {}
    assert cluster.other == []
    assert cluster.inside("postgres") == [system_ops._SCAN_DATABASES_SQL]


def test_the_scan_query_is_catalog_only_and_well_formed():
    sql = system_ops._SUPERUSER_ONLY_OBJECTS_SQL
    # One statement, one row, no quoting the shell or psql could trip over.
    assert sql.count(";") == 1 and sql.endswith(")::text;")
    assert '"' not in sql and "\\" not in sql and "\n" not in sql
    depth = 0
    for char in sql:
        depth += {"(": 1, ")": -1}.get(char, 0)
        assert depth >= 0
    assert depth == 0
    assert sql.count("'") % 2 == 0
    # Every kind the report knows is a key of the row, and nothing is read
    # but the system catalogs.
    for kind in system_ops._OBJECT_KINDS:
        assert f"'{kind}', " in sql
    tables = {
        word.rstrip(",)")
        for clause in ("FROM ", "JOIN ")
        for word in (part.split()[0] for part in sql.split(clause)[1:])
        if not word.startswith("(")
    } - {"LATERAL"}
    assert tables == {
        "pg_available_extension_versions",
        "pg_cast",
        "pg_class",
        "pg_database",
        "pg_db_role_setting",
        "pg_depend",
        "pg_event_trigger",
        "pg_extension",
        "pg_foreign_server",
        "pg_language",
        "pg_opclass",
        "pg_operator",
        "pg_proc",
        "pg_publication",
        "pg_rewrite",
        "pg_roles",
        "pg_subscription",
        "pg_user_mapping",
    }


def _report(tmp_path, caplog, rows, *, name=DEV):
    settings, cluster, client = _scan(tmp_path, rows=rows, name=name)
    with caplog.at_level(logging.INFO, logger="oduflow"):
        system_ops._report_superuser_only_objects(client, settings, name)
    return cluster, {
        level: [r.getMessage() for r in caplog.records if r.levelno == level]
        for level in (logging.WARNING, logging.INFO)
    }


def test_the_report_warns_about_what_can_hand_out_rights(tmp_path, caplog):
    rows = [
        _row(
            "oduflow_template_1_base",
            event_triggers=[{**AUDIT, "enabled": True, "function_owner": "odoo"}],
            untrusted_functions=["public.run(text) in plpython3u"],
            # In a template the superuser still owns, everything is the
            # superuser's: these say nothing more than that.
            superuser_views=["public.report_a", "public.report_b"],
            definer_functions=["public.f()"],
        ),
        _row(
            "oduflow_1_main",
            definer_functions=["public.as_superuser(text)"],
            foreign_servers=["loop"],
            user_mappings=2,
        ),
        _row("oduflow_1_other"),
        _row("oduflow_1_gone"),
        _row("oduflow_template_1_clean"),
    ]

    cluster, logged = _report(tmp_path, caplog, rows)

    environment, template = logged[logging.WARNING]
    assert "Database oduflow_template_1_base in oduflow-db holds" in template
    assert (
        "event triggers: audit_ddl (on ddl_command_start, runs audit() of odoo, "
        "enabled)" in template
    )
    assert "functions in an untrusted language: public.run(text) in plpython3u" in (
        template
    )
    assert "report_a" not in template and "SECURITY DEFINER" not in template
    assert "rebuild the template" in template
    assert "Database oduflow_1_main in oduflow-db holds" in environment
    assert "rebuild the template" not in environment
    assert "SECURITY DEFINER functions owned by a superuser: public.as_superuser" in (
        environment
    )
    assert "foreign servers: loop; user mappings: 2" in environment
    assert logged[logging.INFO] == []
    # Reported, and nothing else: no statement was run in any database.
    assert all(db == "postgres" for _role, db, _sql, _env in cluster.sessions)
    assert len(cluster.other) == 1


def test_the_report_only_mentions_what_a_rebuild_cannot_recreate(tmp_path, caplog):
    rows = [
        # Loaded by an older version, nothing else to say about it.
        _row("oduflow_template_1_base", superuser_views=["public.report_a"]),
        # Disabled: it fires for nobody, and only a superuser enables it.
        _row(
            "oduflow_1_main",
            event_triggers=[{**AUDIT, "enabled": False, "function_owner": ENV_ROLE}],
            untrusted_extensions=["postgis"],
        ),
        _row("oduflow_1_other"),
        _row("oduflow_template_1_clean"),
    ]

    _cluster, logged = _report(tmp_path, caplog, rows)

    assert logged[logging.WARNING] == []
    mentioned, legacy, unchecked = logged[logging.INFO]
    assert mentioned.startswith("1 database(s) in oduflow-db hold what a rebuild")
    assert "oduflow_1_main (event triggers: audit_ddl" in mentioned
    assert "disabled)" in mentioned
    assert "extensions the image does not mark trusted: postgis" in mentioned
    assert "1 template database(s) in oduflow-db were loaded by an older" in legacy
    assert "oduflow_template_1_base" in legacy and "--overwrite" in legacy
    assert "oduflow_template_1_clean" not in legacy
    assert "1 database(s) in oduflow-db could not be checked" in unchecked
    assert "oduflow_1_gone" in unchecked


def test_a_clean_cluster_is_reported_as_nothing(tmp_path, caplog):
    rows = [
        _row(name)
        for name in (
            "oduflow_1_gone",
            "oduflow_1_main",
            "oduflow_1_other",
            "oduflow_template_1_clean",
        )
    ]
    # The one template an older version loaded is the only thing to say.
    rows.append(_row("oduflow_template_1_base"))

    _cluster, logged = _report(tmp_path, caplog, rows)

    assert logged[logging.WARNING] == []
    (legacy,) = logged[logging.INFO]
    assert "oduflow_template_1_base" in legacy


def test_names_from_a_tenants_catalog_stay_on_their_line(tmp_path, caplog):
    rows = [
        _row(
            "oduflow_1_main",
            foreign_servers=["loop\nWARNING forged line", "x" * 500],
        )
    ]

    _cluster, logged = _report(tmp_path, caplog, rows)

    (message,) = logged[logging.WARNING]
    assert "\n" not in message
    assert "loop?WARNING forged line" in message
    assert "x" * 121 not in message


def test_a_scan_that_fails_never_fails_the_start(tmp_path, caplog):
    settings, _team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("gone")

    with caplog.at_level(logging.WARNING, logger="oduflow"):
        system_ops._report_superuser_only_objects(client, settings, DEV)

    (record,) = caplog.records
    assert "Could not check the databases of oduflow-db" in record.getMessage()


def test_a_row_of_an_unexpected_shape_never_fails_the_start_either(tmp_path, caplog):
    rows = [_row("oduflow_1_main", event_triggers=5, foreign_servers={"a": 1})]
    settings, _cluster, client = _scan(tmp_path, rows=rows)

    with caplog.at_level(logging.WARNING, logger="oduflow"):
        system_ops._report_superuser_only_objects(client, settings, DEV)

    (record,) = caplog.records
    assert "Could not check the databases of oduflow-db" in record.getMessage()


def test_a_scan_that_answers_for_no_database_is_an_error(tmp_path):
    settings, cluster, client = _scan(tmp_path, rows=["sh: psql: not found"])
    cluster.container.exec_run.side_effect = lambda cmd, **kwargs: (
        (127, b"sh: timeout: not found") if cmd[0] == "timeout" else cluster._exec(cmd)
    )

    with pytest.raises(ExternalCommandError, match="timeout: not found"):
        system_ops.superuser_only_objects(client, settings)


@pytest.mark.parametrize(
    ("guard", "reported"),
    [({"latched": False}, [DEV, PROD]), ({"latched": True}, [DEV])],
)
def test_init_system_reports_on_the_clusters_it_may_touch(tmp_path, guard, reported):
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
            "_restrict_cluster_databases",
        ):
            stack.enter_context(patch.object(system_ops, name))
        stack.enter_context(patch.object(system_ops, "get_client", return_value=client))
        report = stack.enter_context(
            patch.object(system_ops, "_report_superuser_only_objects")
        )
        stack.enter_context(patch("oduflow.wal_monitor.state", return_value=guard))
        stack.enter_context(
            patch(
                "oduflow.production_registry.clear_stale_deploy_flags",
                side_effect=RuntimeError("stop here"),
            )
        )
        with pytest.raises(RuntimeError, match="stop here"):
            system_ops.init_system(settings)

    assert [call.args[2] for call in report.call_args_list] == reported


# --------------------------------------------------------------------------
# the PostgreSQL upgrade
# --------------------------------------------------------------------------


def test_an_event_trigger_is_among_what_a_role_cannot_carry_over():
    # Its dump includes it, and then only a superuser could create it again:
    # the restore would stop with the old cluster already gone.
    sql = pm._ROLE_DUMP_OMISSIONS_SQL
    assert "SELECT 'an event trigger' WHERE EXISTS (" in sql
    assert "FROM pg_catalog.pg_event_trigger" in sql


def test_a_whole_archive_the_superuser_restores_fires_no_trigger_on_17(
    tmp_path, monkeypatch
):
    settings, _team = _settings(tmp_path)
    monkeypatch.setenv(pm.SUPERUSER_RESTORE_ENV, "oduflow_1_prod-shop")
    new = _Cluster(PROD, version=PG17)
    helpers = []

    def run_helper(client, settings_, image, container, command, *args, **kwargs):
        helpers.append((command, kwargs))

    with (
        patch.object(pm, "_run_helper", side_effect=run_helper),
        patch.object(pm.system_ops, "_pg_restore_jobs", return_value=2),
    ):
        pm._restore_database(
            _client(new),
            settings,
            {"image_id": "sha256:new"},
            {"container": PROD, "kind": "prod"},
            "oduflow_1_prod-shop",
        )

    # The archive may create event triggers, and what follows them in it
    # would fire them for the superuser.
    ((command, kwargs),) = helpers
    assert command[:5] == ["pg_restore", "-h", "127.0.0.1", "-U", SUPERUSER]
    assert kwargs["superuser"] is True
    assert kwargs["options"] == "-c event_triggers=off"
