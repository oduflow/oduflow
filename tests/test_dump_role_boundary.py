"""A database is dumped by its own role, not by the cluster superuser.

``pg_dump`` reads a database whose content is its tenant's. It pins
``search_path`` and qualifies every name, so on a current release reading
does not run tenant code. It did until 12.20 / 13.16 / 14.13 / 15.8 / 16.4
(CVE-2024-7348): a session that swapped a sequence for a view between
pg_dump's listing and its read had the view's functions run by whoever
dumped — the cluster superuser, at every dump site Oduflow had. A PostgreSQL
container is never recreated behind the operator's back, so its minor
release is the one it was created with.

These tests pin, against a fake Docker, who dumps at each site, in which
container, and with which flags:

- an environment's and a production's database: the scoped role, next to
  PostgreSQL, where the local socket asks it for no password;
- a template database: the team's template owner;
- the build database of ``init_template``: the one-off builder (pinned in
  test_superuser_credential_boundary);
- a database carried through a PostgreSQL upgrade: the role on record, from
  a helper that holds that role's password and not the superuser's.

The superuser is left with a template an older version loaded as it, and with
what an operator names for the upgrade; there the server is told not to
expand what is not a plain relation.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import psql_call

import oduflow
from oduflow import backup_ops
from oduflow import postgres_migration as pm
from oduflow.docker_ops import production_ops, system_ops
from oduflow.env_credentials import MissingCredentialsError, save_credentials
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.naming import get_template_owner_role, prod_env_name
from oduflow.settings import BackupSettings, Settings, TeamSettings

SUPERUSER = "odoo"
SECRET = "cluster-superuser-secret"
DEV = "oduflow-db"
PROD = "oduflow-prod-db"
GUARD = "-c restrict_nonsystem_relation_kind=view,foreign-table"
PATCHED = "160004"
VULNERABLE = "160003"


def _settings(tmp_path, *, backup=False):
    team = TeamSettings(team_id="7", data_dir=str(tmp_path / "team_7"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user=SUPERUSER,
        db_password=SECRET,
        backup=BackupSettings(bucket="bucket", access_key="key", secret_key="secret")
        if backup
        else None,
        teams={"7": team},
    )
    return settings, team


class _Cluster:
    """A PostgreSQL container: answers psql, logs every other exec."""

    def __init__(self, name, answers=None, *, exec_result=(0, b""), frames=()):
        self.name = name
        self.sql: list[tuple[str, str, str]] = []
        # (argv, the keyword arguments of the exec) for everything but psql.
        self.execs: list[tuple[list[str], dict]] = []
        self._answers = answers or {}
        self._exec_result = exec_result
        self.container = MagicMock(name=name)
        self.container.id = f"{name}-id"
        self.container.attrs = {"Config": {"Image": "postgres:16"}, "Mounts": []}
        self.container.exec_run.side_effect = self._exec
        # The streaming exec API, as backup_ops._dump_stream uses it.
        api = self.container.client.api
        api.exec_create.side_effect = self._exec_create
        api.exec_start.return_value = iter(frames)
        api.exec_inspect.return_value = {"ExitCode": 0}

    def _exec(self, cmd, **kwargs):
        call = psql_call(cmd, SUPERUSER)
        if call is None:
            self.execs.append((cmd, kwargs))
            return self._exec_result
        self.sql.append(call)
        for fragment, answer in self._answers.items():
            if fragment in call[2]:
                return 0, answer.encode()
        return 0, b""

    def _exec_create(self, container_id, cmd, **kwargs):
        assert container_id == self.container.id
        self.execs.append((cmd, kwargs))
        return {"Id": "exec-1"}

    def dumps(self):
        return [(cmd, kw) for cmd, kw in self.execs if cmd[0] == "pg_dump"]


def _docker(*clusters):
    client = MagicMock(name="docker")
    by_name = {cluster.name: cluster.container for cluster in clusters}

    def get(name):
        import docker

        if name in by_name:
            return by_name[name]
        raise docker.errors.NotFound(name)

    client.containers.get.side_effect = get
    return client


# --------------------------------------------------------------------------
# which servers have the fix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "fixed"),
    [
        (120019, False),
        (120020, True),
        (130015, False),
        (130016, True),
        (140012, False),
        (140013, True),
        (150007, False),
        (150008, True),
        (150014, True),
        (160003, False),
        (160004, True),
        (160010, True),
        # 17 was released with it.
        (170000, True),
        (180001, True),
        # Out of support before the fix, and never given it.
        (110022, False),
    ],
)
def test_the_fix_for_the_dump_race_is_known_by_minor_release(version, fixed):
    assert system_ops.pg_dump_guard_supported(version) is fixed


def test_a_superuser_dump_tells_a_fixed_server_what_not_to_expand(tmp_path, caplog):
    settings, _team = _settings(tmp_path)
    prod = _Cluster(PROD, {"SHOW server_version_num": PATCHED})

    with caplog.at_level("WARNING", logger="oduflow"):
        environment = system_ops.superuser_dump_environment(
            _docker(prod), settings, "oduflow_7_x", container_name=PROD
        )

    # With the connection, so it does not depend on the client's release.
    assert environment == {"PGOPTIONS": GUARD}
    assert caplog.records == []
    # Asked of the cluster being dumped, in its maintenance database.
    assert prod.sql == [(SUPERUSER, "postgres", "SHOW server_version_num;")]


def test_a_superuser_dump_on_an_unfixed_server_is_reported(tmp_path, caplog):
    settings, _team = _settings(tmp_path)
    dev = _Cluster(DEV, {"SHOW server_version_num": VULNERABLE})

    with caplog.at_level("WARNING", logger="oduflow"):
        environment = system_ops.superuser_dump_environment(
            _docker(dev), settings, "oduflow_template_7_base"
        )

    # The server does not know the setting: sending it would fail the
    # connection, so the dump goes ahead and the operator is told.
    assert environment == {}
    (record,) = caplog.records
    message = record.getMessage()
    assert "oduflow_template_7_base" in message
    assert "PostgreSQL 16.3" in message and DEV in message
    assert "CVE-2024-7348" in message


# --------------------------------------------------------------------------
# the role of an environment or a production
# --------------------------------------------------------------------------


def _record(team, env_name, role, password="scoped-pw"):
    save_credentials(
        env_name, team.workspaces_dir, {"pg_user": role, "pg_password": password}
    )


def test_the_role_on_record_dumps_and_the_cluster_confirms_it(tmp_path):
    settings, team = _settings(tmp_path)
    _record(team, "feature-x", "u_7_feature-x")
    dev = _Cluster(DEV, {"SELECT rolsuper": "f"})

    role = system_ops.scoped_dump_role(_docker(dev), settings, team, "feature-x")

    assert role == "u_7_feature-x"
    # One catalog read, by the pinned superuser, outside the tenant's database.
    assert dev.sql == [
        (
            SUPERUSER,
            "postgres",
            "SELECT rolsuper FROM pg_roles WHERE rolname = 'u_7_feature-x';",
        )
    ]


def test_a_productions_role_is_looked_up_in_the_production_cluster(tmp_path):
    settings, team = _settings(tmp_path)
    _record(team, prod_env_name("erp"), "u_7_prod-erp")
    dev, prod = _Cluster(DEV), _Cluster(PROD, {"SELECT rolsuper": "f"})

    role = system_ops.scoped_dump_role(
        _docker(dev, prod), settings, team, prod_env_name("erp"), container_name=PROD
    )

    assert role == "u_7_prod-erp"
    assert dev.sql == [] and len(prod.sql) == 1


def test_a_lost_credentials_file_does_not_stop_a_dump(tmp_path):
    # A dump needs the role's name only: next to PostgreSQL no password is
    # asked. The name is derived as it was when the role was created, so a
    # production whose file is gone can still be backed up before it is
    # repaired (reconfigure_production).
    settings, team = _settings(tmp_path)
    prod = _Cluster(PROD, {"SELECT rolsuper": "f"})

    role = system_ops.scoped_dump_role(
        _docker(prod), settings, team, prod_env_name("erp"), container_name=PROD
    )

    assert role == "u_7_prod-erp"
    assert "rolname = 'u_7_prod-erp'" in prod.sql[0][2]


@pytest.mark.parametrize(
    ("env_name", "remedy"),
    [
        ("legacy", "update_environment"),
        (prod_env_name("erp"), "reconfigure_production"),
    ],
)
def test_a_database_that_never_had_a_role_is_not_dumped(tmp_path, env_name, remedy):
    # An environment older than scoped roles: nothing but the superuser could
    # read all of it, and the superuser does not. The remedy keeps the data.
    settings, team = _settings(tmp_path)
    dev = _Cluster(DEV)  # no such role: the query returns nothing

    with pytest.raises(MissingCredentialsError, match=remedy):
        system_ops.scoped_dump_role(_docker(dev), settings, team, env_name)


def test_a_record_naming_the_superuser_is_not_believed(tmp_path):
    # load_credentials' fallback names the superuser, and so might a file
    # somebody edited: the derived name is asked about instead.
    settings, team = _settings(tmp_path)
    _record(team, "feature-x", SUPERUSER, SECRET)
    dev = _Cluster(DEV, {"SELECT rolsuper": "f"})

    role = system_ops.scoped_dump_role(_docker(dev), settings, team, "feature-x")

    assert role == "u_7_feature-x"


def test_a_superuser_under_another_name_is_refused(tmp_path):
    settings, team = _settings(tmp_path)
    _record(team, "feature-x", "postgres")
    dev = _Cluster(DEV, {"SELECT rolsuper": "t"})

    with pytest.raises(PrerequisiteNotMetError, match="is a PostgreSQL superuser"):
        system_ops.scoped_dump_role(_docker(dev), settings, team, "feature-x")


def test_a_recorded_name_cannot_leave_its_sql_literal(tmp_path):
    settings, team = _settings(tmp_path)
    _record(team, "feature-x", "x' OR rolsuper OR 'a'='a")
    dev = _Cluster(DEV)

    with pytest.raises(MissingCredentialsError):
        system_ops.scoped_dump_role(_docker(dev), settings, team, "feature-x")

    assert dev.sql[0][2] == (
        "SELECT rolsuper FROM pg_roles WHERE rolname = 'x'' OR rolsuper OR ''a''=''a';"
    )


# --------------------------------------------------------------------------
# the role of a template database
# --------------------------------------------------------------------------


def test_a_template_is_dumped_by_the_teams_template_owner(tmp_path, caplog):
    settings, team = _settings(tmp_path)
    owner = get_template_owner_role("7")
    dev = _Cluster(DEV, {"d.datdba": owner})

    with caplog.at_level("INFO", logger="oduflow"):
        role = system_ops.template_dump_role(
            _docker(dev), settings, team, "oduflow_template_7_base"
        )

    assert role == owner and role != SUPERUSER
    assert caplog.records == []
    assert "d.datname = 'oduflow_template_7_base'" in dev.sql[0][2]
    assert dev.sql[0][:2] == (SUPERUSER, "postgres")


def test_a_template_loaded_by_an_older_version_is_still_the_superusers(
    tmp_path, caplog
):
    # Nobody else can read all of it, and no tenant role can connect to it.
    settings, team = _settings(tmp_path)
    dev = _Cluster(DEV, {"d.datdba": SUPERUSER})

    with caplog.at_level("INFO", logger="oduflow"):
        role = system_ops.template_dump_role(
            _docker(dev), settings, team, "oduflow_template_7_base"
        )

    assert role == SUPERUSER
    assert "imported or published again" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("owner", ["oduflow_load_0123456789abcdef", "u_7_main", ""])
def test_a_template_owned_by_anyone_else_is_not_dumped(tmp_path, owner):
    # A loader still owning it: the load did not finish.
    settings, team = _settings(tmp_path)
    dev = _Cluster(DEV, {"d.datdba": owner})

    with pytest.raises(PrerequisiteNotMetError, match="its load did not finish"):
        system_ops.template_dump_role(
            _docker(dev), settings, team, "oduflow_template_7_base"
        )


# --------------------------------------------------------------------------
# seeding a production from a development database
# --------------------------------------------------------------------------


def _seed(client, settings, team, source_db, source_role):
    def copy_out(container, container_path, host_path):
        with open(host_path, "wb") as fh:
            fh.write(b"PGDMP")

    with (
        patch.object(production_ops, "_copy_file_from_container", side_effect=copy_out),
        patch.object(production_ops, "restore_archive_as_role") as restore,
    ):
        production_ops._copy_db_into_prod_cluster(
            client,
            settings,
            team,
            source_db,
            "oduflow_7_prod-erp",
            "template seed",
            {"pg_user": "u_7_prod-erp", "pg_password": "role-pw"},
            source_role=source_role,
        )
    return restore


def test_a_template_seed_is_dumped_by_the_template_owner(tmp_path):
    settings, team = _settings(tmp_path)
    owner = get_template_owner_role("7")
    dev, prod = _Cluster(DEV), _Cluster(PROD)

    restore = _seed(
        _docker(dev, prod), settings, team, "oduflow_template_7_base", owner
    )

    in_dev = "/tmp/oduflow_7_prod-erp.seed.pgdump"
    ((command, options),) = dev.dumps()
    assert command == [
        "pg_dump",
        "-U",
        owner,
        "-Fc",
        "-f",
        in_dev,
        "oduflow_template_7_base",
    ]
    # Its own role needs no guard, and the superuser asks the cluster nothing.
    assert options == {"environment": {}}
    assert dev.sql == [] and prod.sql == [] and prod.execs == []
    restore.assert_called_once()


def test_a_legacy_template_seed_is_the_superusers_and_guarded(tmp_path):
    settings, team = _settings(tmp_path)
    dev = _Cluster(DEV, {"SHOW server_version_num": PATCHED})

    _seed(
        _docker(dev, _Cluster(PROD)),
        settings,
        team,
        "oduflow_template_7_old",
        SUPERUSER,
    )

    ((command, options),) = dev.dumps()
    assert command[:3] == ["pg_dump", "-U", SUPERUSER]
    assert options == {"environment": {"PGOPTIONS": GUARD}}
    # The version is read before the dump starts.
    assert dev.sql == [(SUPERUSER, "postgres", "SHOW server_version_num;")]


def test_a_legacy_template_seed_on_an_unfixed_server_is_reported(tmp_path, caplog):
    settings, team = _settings(tmp_path)
    dev = _Cluster(DEV, {"SHOW server_version_num": VULNERABLE})

    with caplog.at_level("WARNING", logger="oduflow"):
        _seed(
            _docker(dev, _Cluster(PROD)),
            settings,
            team,
            "oduflow_template_7_old",
            SUPERUSER,
        )

    ((_command, options),) = dev.dumps()
    assert options == {"environment": {}}
    assert "CVE-2024-7348" in caplog.records[-1].getMessage()


def test_a_dump_its_role_may_not_finish_says_why_and_leaves_nothing(tmp_path):
    settings, team = _settings(tmp_path)
    denied = b"pg_dump: error: query failed: ERROR:  permission denied for table x"
    dev = _Cluster(DEV, exec_result=(1, denied))

    with pytest.raises(ExternalCommandError) as exc_info:
        _seed(
            _docker(dev, _Cluster(PROD)), settings, team, "oduflow_7_main", "u_7_main"
        )

    error = exc_info.value
    assert error.cmd == "pg_dump (template seed)"
    assert "permission denied for table x" in error.output
    assert "never by the cluster superuser" in error.output
    assert "update_environment" in error.output
    # Nobody retried as the superuser, and the partial dump is removed from
    # the PostgreSQL container.
    assert [cmd[:3] for cmd, _kw in dev.dumps()] == [["pg_dump", "-U", "u_7_main"]]
    assert dev.execs[-1][0] == ["rm", "-f", "/tmp/oduflow_7_prod-erp.seed.pgdump"]


@pytest.mark.parametrize(
    ("output", "explained"),
    [
        ("pg_dump: error: permission denied for sequence s", True),
        ("pg_dump: error: permission denied for large object 16402", True),
        (
            "pg_dump: error: query would be affected by row-level security "
            'policy for table "t"',
            True,
        ),
        ('pg_dump: error: FATAL:  permission denied for database "d"', True),
        ("pg_dump: error: connection to server failed", False),
        # The operating system's refusal is not PostgreSQL's.
        (
            'pg_dump: error: could not open output file "/exchange/x": '
            "Permission denied",
            False,
        ),
    ],
)
def test_only_a_refusal_is_explained(output, explained):
    text = system_ops.explain_dump_failure(output)

    assert text.startswith(output)
    assert ("never by the cluster superuser" in text) is explained


# --------------------------------------------------------------------------
# production snapshots
# --------------------------------------------------------------------------


def _snapshot(settings, team, client):
    uploaded: dict[str, object] = {}

    def upload(_s3, bucket, key, frames):
        data = b"".join(frames)
        uploaded.update(bucket=bucket, key=key, data=data)
        return len(data)

    s3 = MagicMock(name="s3")
    with (
        patch.object(backup_ops.production_registry, "get_production", return_value={}),
        patch.object(backup_ops.production_registry, "set_nested"),
        patch.object(backup_ops, "get_client", return_value=client),
        patch.object(backup_ops.s3_client, "make_client", return_value=s3),
        patch.object(
            backup_ops.s3_client, "multipart_upload_stream", side_effect=upload
        ),
        patch("oduflow.wal_monitor.assert_writable"),
    ):
        manifest = backup_ops.snapshot_production(settings, team, "erp")
    return manifest, uploaded, s3


def test_a_snapshot_is_dumped_by_the_productions_role(tmp_path):
    settings, team = _settings(tmp_path, backup=True)
    _record(team, prod_env_name("erp"), "u_7_prod-erp")
    frames = [(b"PGDMP", None), (None, b"pg_dump: reading"), (b" data", None)]
    dev = _Cluster(DEV)
    prod = _Cluster(PROD, {"SELECT rolsuper": "f"}, frames=frames)

    manifest, uploaded, s3 = _snapshot(settings, team, _docker(dev, prod))

    # One pg_dump, in the production cluster's container, as the production's
    # own role over the local socket: no password, and no superuser.
    ((command, options),) = prod.dumps()
    assert command == ["pg_dump", "-U", "u_7_prod-erp", "-Fc", "oduflow_7_prod-erp"]
    assert options == {}
    assert SECRET not in repr(prod.execs)
    assert dev.execs == [] and dev.sql == []
    # The role was confirmed first, in that cluster.
    assert [sql for _role, _db, sql in prod.sql] == [
        "SELECT rolsuper FROM pg_roles WHERE rolname = 'u_7_prod-erp';"
    ]

    # What was streamed is what the manifest describes: same archive format,
    # straight to S3, hashed on the way.
    assert uploaded["data"] == b"PGDMP data"
    assert uploaded["key"] == manifest["db"]["key"]
    assert manifest["db"]["key"].endswith(".pgdump")
    assert manifest["db"]["bytes"] == len(b"PGDMP data")
    assert manifest["db"]["sha256"] == hashlib.sha256(b"PGDMP data").hexdigest()
    assert "seconds" in manifest["db"]
    stored = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert stored["db"] == manifest["db"]


def test_a_production_without_a_role_is_not_snapshotted_by_the_superuser(tmp_path):
    settings, team = _settings(tmp_path, backup=True)
    prod = _Cluster(PROD)  # the role does not exist

    with pytest.raises(MissingCredentialsError, match="reconfigure_production"):
        _snapshot(settings, team, _docker(_Cluster(DEV), prod))

    # Refused before any dump or upload started.
    assert prod.dumps() == []


def test_a_failed_snapshot_dump_carries_pg_dumps_reason():
    container = MagicMock()
    container.id = "cid"
    api = container.client.api
    api.exec_create.return_value = {"Id": "exec-1"}
    api.exec_start.return_value = iter(
        [(b"PGDMP", None), (None, b"pg_dump: error: permission denied for table x\n")]
    )
    api.exec_inspect.return_value = {"ExitCode": 1}

    stream = backup_ops._dump_stream(container, "u_7_prod-erp", "oduflow_7_prod-erp")
    assert next(stream) == b"PGDMP"
    with pytest.raises(ExternalCommandError) as exc_info:
        next(stream)

    assert api.exec_create.call_args.args == (
        "cid",
        ["pg_dump", "-U", "u_7_prod-erp", "-Fc", "oduflow_7_prod-erp"],
    )
    output = exc_info.value.output
    assert "exited with code 1: pg_dump: error: permission denied for table x" in output
    assert "reconfigure_production" in output


# --------------------------------------------------------------------------
# the PostgreSQL upgrade
# --------------------------------------------------------------------------


def _upgrade_dump(
    tmp_path, monkeypatch, *, roles, by_superuser="", fail=None, helpers=None
):
    """Run _dump_cluster over *roles*; return settings, helper runs, SQL.

    ``helpers`` is the list the helper runs are logged to, for a test that
    expects the dump to raise.
    """
    settings, _team = _settings(tmp_path)
    monkeypatch.setenv(pm.SUPERUSER_RESTORE_ENV, by_superuser)
    cluster = {
        "kind": "prod",
        "container": PROD,
        "old_major": 15,
        "databases": sorted(roles),
    }
    state = {"image_id": "sha256:new", "clusters": [cluster]}
    helpers = [] if helpers is None else helpers
    statements: list[str] = []

    def run_helper(
        client, settings_, image, container, command, directory, label, **kw
    ):
        assert (image, container) == ("sha256:new", PROD)
        assert directory == pm._dump_path(settings, "prod")
        helpers.append((command, kw))
        if fail and command[0] == "pg_dump" and fail in command:
            raise ExternalCommandError(
                "pg_dump", 1, "pg_dump: error: permission denied for table x\n"
            )

    def sql(client, settings_, statement, db="postgres", **kwargs):
        assert kwargs == {"container_name": PROD} and db == "postgres"
        statements.append(statement)
        return "150008" if "server_version_num" in statement else ""

    @contextlib.contextmanager
    def started(client, settings_, cluster_):
        container = MagicMock()
        container.name = PROD
        yield container

    with (
        patch.object(pm, "_started", started),
        patch.object(pm, "_run_helper", side_effect=run_helper),
        patch.object(pm, "_carried_roles", return_value=roles),
        patch.object(pm.system_ops, "_exec_sql", side_effect=sql),
    ):
        pm._dump_cluster(MagicMock(), settings, state, cluster)
    return settings, helpers, statements


def test_a_carried_database_is_dumped_by_its_own_role(tmp_path, monkeypatch):
    settings, helpers, statements = _upgrade_dump(
        tmp_path, monkeypatch, roles={"a": ("u_a", "pw-a"), "b": ("u_b", "pw-b")}
    )

    directory = pm._dump_path(settings, "prod")
    assert helpers == [
        # Each database by its role, with that role's password and no other;
        # the archive format and the in-place write are what they were.
        (
            ["pg_dump", "-h", "127.0.0.1", "-U", "u_a", "-Fc"]
            + ["-f", "/dump/a.pgdump", "a"],
            {"progress_path": os.path.join(directory, "a.pgdump"), "password": "pw-a"},
        ),
        # Reading the archive back connects to nothing, so it is given no
        # password at all, and may not write where every dump lies.
        (
            ["pg_restore", "--list", "-f", "/dev/null", "/dump/a.pgdump"],
            {"writable": False},
        ),
        (
            ["pg_dump", "-h", "127.0.0.1", "-U", "u_b", "-Fc"]
            + ["-f", "/dump/b.pgdump", "b"],
            {"progress_path": os.path.join(directory, "b.pgdump"), "password": "pw-b"},
        ),
        (
            ["pg_restore", "--list", "-f", "/dev/null", "/dump/b.pgdump"],
            {"writable": False},
        ),
    ]
    assert not any(kw.get("superuser") for _command, kw in helpers)
    # Roles are still the superuser's to dump, from the cluster's own catalog:
    # two statements in the maintenance database, and no version to check.
    assert statements == [pm._ROLES_SQL, pm._MEMBERSHIPS_SQL]
    assert os.path.isfile(os.path.join(directory, "roles.sql"))


def test_the_operator_may_leave_a_dump_to_the_superuser(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="oduflow"):
        settings, helpers, statements = _upgrade_dump(
            tmp_path,
            monkeypatch,
            roles={"a": ("u_a", "pw-a"), "gis": None},
            by_superuser="gis",
        )

    by_database = {command[-1]: (command, kw) for command, kw in helpers}
    command, kw = by_database["gis"]
    assert command[:5] == ["pg_dump", "-h", "127.0.0.1", "-U", SUPERUSER]
    # It asks for the superuser's password by name, and the old server — 15.8
    # here — is told what not to expand whatever the new image's pg_dump does.
    assert kw["superuser"] is True and "password" not in kw
    assert kw["options"] == GUARD
    assert "SHOW server_version_num;" in statements
    assert "as the PostgreSQL superuser" in caplog.text
    # The other database is no business of the superuser's.
    command, kw = by_database["a"]
    assert command[command.index("-U") + 1] == "u_a"
    assert kw["password"] == "pw-a" and "superuser" not in kw


def test_a_dump_its_role_cannot_take_stops_the_upgrade_with_a_way_out(
    tmp_path, monkeypatch
):
    helpers: list = []
    with pytest.raises(PrerequisiteNotMetError) as exc_info:
        _upgrade_dump(
            tmp_path,
            monkeypatch,
            roles={"a": ("u_a", "pw-a"), "b": ("u_b", "pw-b")},
            fail="a",
            helpers=helpers,
        )

    message = str(exc_info.value)
    assert "Database a could not be dumped by its own role 'u_a'" in message
    assert "permission denied for table x" in message
    assert "ODUFLOW_PG_UPGRADE_SUPERUSER_RESTORE=a" in message
    # Every old cluster is still there (_run cancels on any dump failure).
    assert "Nothing has been changed" in message
    assert "back to 'postgres:15'" in message
    # It stopped there: no superuser retry, and b was not started.
    assert [command[-1] for command, _kw in helpers] == ["a"]


def test_a_database_whose_record_vanished_is_not_dumped_by_the_superuser(
    tmp_path, monkeypatch
):
    helpers: list = []
    with pytest.raises(PrerequisiteNotMetError, match="no scoped role on record"):
        _upgrade_dump(tmp_path, monkeypatch, roles={"a": None}, helpers=helpers)

    assert helpers == []


# --------------------------------------------------------------------------
# where pg_dump is started at all
# --------------------------------------------------------------------------

# function -> who runs the pg_dump it starts. A dump the superuser may run is
# marked: it has to pass system_ops.superuser_dump_environment.
_DUMP_SITES = {
    # An environment's or a production's database, by its scoped role.
    ("docker_ops/system_ops.py", "_staged_db_dump"): "role",
    ("backup_ops.py", "_dump_stream"): "role",
    # init_template's build database, by the one-off builder.
    ("docker_ops/system_ops.py", "init_template"): "role",
    # A development database seeding a production: its role, or the superuser
    # for a template an older version loaded.
    ("docker_ops/production_ops.py", "_copy_db_into_prod_cluster"): "role or superuser",
    # The PostgreSQL upgrade: the role on record, or the superuser for what
    # the operator names.
    ("postgres_migration.py", "_dump_database"): "role or superuser",
}


def _dump_sites():
    """Every function with a ``["pg_dump", ...]`` argv, and what it does with
    the superuser."""
    root = pathlib.Path(oduflow.__file__).parent
    sites = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            argvs = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.List)
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "pg_dump"
            ]
            if not argvs:
                continue
            source = ast.unparse(function)
            # No argv names the superuser unless the function also asks what
            # a superuser's dump has to be started with.
            names_superuser = any(
                "settings.db_user" in ast.unparse(argv) for argv in argvs
            )
            guarded = "superuser_dump_environment" in source
            assert names_superuser <= guarded, (path, function.name)
            sites[(path.relative_to(root).as_posix(), function.name)] = (
                "role or superuser" if guarded else "role"
            )
    return sites


def test_every_dump_site_says_who_dumps():
    # A new pg_dump is a new reader of tenant content. Give it the database's
    # own role (system_ops.scoped_dump_role / template_dump_role); extend the
    # list for the superuser only where no role can read the database, and
    # pass superuser_dump_environment there.
    assert _dump_sites() == _DUMP_SITES


def test_no_dump_falls_back_to_pg_dumpall():
    # pg_dumpall reconnects to every database as the superuser. Roles are
    # read from the catalog instead (postgres_migration._ROLES_SQL).
    root = pathlib.Path(oduflow.__file__).parent
    users = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "pg_dumpall" in path.read_text()
    ]
    assert users == []
