"""A production's data is restored by the production's own role.

What a database contains is its tenant's: a function in a CHECK constraint is
called for every row ``COPY`` loads, with the rights of whoever restores. A
production is seeded from a development database, promoted from one, or put
back from a snapshot of itself, and all three used to run
``pg_restore -U <superuser>`` inside the production PostgreSQL container — so
a development tenant could become superuser of the production cluster, and
``--no-owner`` left every restored view and SECURITY DEFINER function owned by
that superuser.

These tests pin the fix against a fake Docker: the content is restored by the
production's scoped role, into a database that role owns, from a throwaway
client container, and the superuser only prepares and tidies around it.
"""

import hashlib
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import psql_call

from oduflow import backup_ops
from oduflow.docker_ops import production_ops, system_ops
from oduflow.env_credentials import create_credentials
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.naming import prod_env_name
from oduflow.settings import BackupSettings, Settings, TeamSettings

SUPERUSER = "odoo"
SUPERUSER_PASSWORD = "cluster-superuser-secret"
DEV = "oduflow-db"
PROD = "oduflow-prod-db"
DEV_IMAGE = "postgres:16.3"
PROD_IMAGE = "postgres:16.4"
DUMP = b"PGDMP database dump"


class _FakeDocker:
    """Both PostgreSQL containers, the restore clients, and one ordered log of
    everything any of them is asked to do."""

    def __init__(self, *, clients=((0, b""),)):
        self.steps: list[tuple] = []
        self._clients = list(clients)
        # Files the restore client found at its mount: path -> content.
        self.mounted: dict[str, bytes] = {}
        self.pg = {
            DEV: self._pg_container(DEV, DEV_IMAGE),
            PROD: self._pg_container(PROD, PROD_IMAGE),
        }
        self.client = MagicMock(name="docker")
        self.client.containers.get.side_effect = self._get
        self.client.containers.run.side_effect = self._run_container

    def _pg_container(self, name, image):
        container = MagicMock(name=name)
        container.attrs = {"Config": {"Image": image}, "Mounts": []}
        container.exec_run.side_effect = lambda cmd, **kw: self._exec(name, cmd)
        return container

    def _get(self, name):
        import docker

        if name in self.pg:
            return self.pg[name]
        raise docker.errors.NotFound(name)

    def _exec(self, container, cmd):
        call = psql_call(cmd, SUPERUSER)
        if call is not None:
            self.steps.append(("sql", container, *call))
            return 0, b""
        self.steps.append(("exec", container, cmd))
        return 0, b""

    def _run_container(self, image, command=None, **kwargs):
        exit_code, output = self._clients.pop(0)
        for host_path in kwargs.get("volumes", {}):
            with open(host_path, "rb") as fh:
                self.mounted[host_path] = fh.read()
        self.steps.append(("client", {"image": image, "command": command, **kwargs}))
        container = MagicMock(name=kwargs.get("name"))
        container.wait.return_value = {"StatusCode": exit_code}
        container.logs.return_value = output
        container.remove.side_effect = lambda **kw: self.steps.append(
            ("client-removed", kwargs.get("name"))
        )
        return container

    # -- views of the log --------------------------------------------------

    def sql(self, container=None):
        return [
            step[1:]
            for step in self.steps
            if step[0] == "sql" and container in (None, step[1])
        ]

    def statements(self, container=None):
        return [step[3] for step in self.sql(container)]

    def execs(self, container=None):
        return [
            step[2]
            for step in self.steps
            if step[0] == "exec" and container in (None, step[1])
        ]

    def clients(self):
        return [step[1] for step in self.steps if step[0] == "client"]

    def index(self, kind, needle=""):
        for position, step in enumerate(self.steps):
            if step[0] == kind and needle in str(step[1:]):
                return position
        raise AssertionError(f"no {kind} step containing {needle!r}: {self.steps}")


def _settings(tmp_path, *, backup=False):
    team = TeamSettings(team_id="7", data_dir=str(tmp_path / "team_7"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user=SUPERUSER,
        db_password=SUPERUSER_PASSWORD,
        backup=BackupSettings(bucket="bucket", access_key="key", secret_key="secret")
        if backup
        else None,
        teams={"7": team},
    )
    return settings, team


def _production_role(team, name="erp"):
    """A production as create_production leaves it: its role is on record."""
    return create_credentials(prod_env_name(name), team.team_id, team.workspaces_dir)


def _assert_no_superuser_restore(docker):
    """No restore tool runs in a PostgreSQL container, and none as superuser."""
    for command in docker.execs():
        assert command[0] not in ("pg_restore", "psql"), command
    for container in docker.pg.values():
        container.put_archive.assert_not_called()
    for client in docker.clients():
        assert client["environment"]["PGUSER"] != SUPERUSER
        assert "-U" not in client["command"]
        assert SUPERUSER_PASSWORD not in repr(client)


# --------------------------------------------------------------------------
# the restore client, pointed at the production cluster
# --------------------------------------------------------------------------


def test_an_archive_is_restored_by_a_role_into_the_named_cluster(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker()
    dump = tmp_path / "seed.pgdump"
    dump.write_bytes(DUMP)
    os.chmod(dump, 0o600)

    system_ops.restore_archive_as_role(
        docker.client,
        settings,
        team,
        str(dump),
        database="oduflow_7_prod-erp",
        role="u_7_prod-erp",
        password="role-pw",
        container_name=PROD,
        jobs=2,
    )

    (client,) = docker.clients()
    owner = os.stat(dump)
    # The production cluster's own image and its name on the team network.
    assert client["image"] == PROD_IMAGE
    assert client["network"] == "oduflow-7-net"
    assert client["environment"] == {
        "PGHOST": PROD,
        "PGUSER": "u_7_prod-erp",
        "PGPASSWORD": "role-pw",
        "PGDATABASE": "oduflow_7_prod-erp",
        "HOME": "/tmp",
    }
    assert client["entrypoint"] == "pg_restore"
    # Tolerant like the restore it replaces: every error is reported and any
    # of them fails the restore, privileges are restored, owners are not.
    assert client["command"] == [
        "--no-owner",
        "-w",
        "-d",
        "oduflow_7_prod-erp",
        "-j",
        "2",
        "/restore/dump",
    ]
    assert client["volumes"] == {str(dump): {"bind": "/restore/dump", "mode": "ro"}}
    assert client["user"] == f"{owner.st_uid}:{owner.st_gid}"
    assert client["cap_drop"] == ["ALL"]
    assert client["security_opt"] == ["no-new-privileges"]
    assert client["name"].startswith("oduflow-db-restore-")
    assert "privileged" not in client
    assert docker.steps[-1][0] == "client-removed"
    # Nothing at all happens inside either PostgreSQL container.
    assert not docker.sql() and not docker.execs()
    _assert_no_superuser_restore(docker)


def test_a_serial_restore_and_the_development_cluster_are_the_defaults(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker()
    dump = tmp_path / "seed.pgdump"
    dump.write_bytes(DUMP)

    system_ops.restore_archive_as_role(
        docker.client, settings, team, str(dump), database="db", role="r", password="p"
    )

    (client,) = docker.clients()
    assert client["image"] == DEV_IMAGE
    assert client["environment"]["PGHOST"] == DEV
    assert client["command"] == ["--no-owner", "-w", "-d", "db", "/restore/dump"]


def test_the_superuser_is_refused_as_the_restoring_role(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker()
    dump = tmp_path / "seed.pgdump"
    dump.write_bytes(DUMP)

    with pytest.raises(PrerequisiteNotMetError, match="as the PostgreSQL superuser"):
        system_ops.restore_archive_as_role(
            docker.client,
            settings,
            team,
            str(dump),
            database="db",
            role=SUPERUSER,
            password=SUPERUSER_PASSWORD,
            container_name=PROD,
        )

    assert docker.steps == []
    docker.client.containers.run.assert_not_called()


def test_a_failed_restore_explains_what_a_scoped_role_cannot_do(tmp_path):
    settings, team = _settings(tmp_path)
    noise = "pg_restore: creating TABLE x\n" * 400
    denied = (
        "pg_restore: error: could not execute query: ERROR:  permission denied "
        'to create extension "postgis"\n'
    )
    docker = _FakeDocker(clients=[(1, (denied + noise).encode())])
    dump = tmp_path / "seed.pgdump"
    dump.write_bytes(DUMP)

    with pytest.raises(ExternalCommandError) as exc_info:
        system_ops.restore_archive_as_role(
            docker.client,
            settings,
            team,
            str(dump),
            database="db",
            role="r",
            password="p",
            container_name=PROD,
            label="pg_restore (template seed)",
        )

    error = exc_info.value
    assert error.cmd == "pg_restore (template seed)"
    assert error.exit_code == 1
    # The cause is found anywhere in the output, though only its tail is kept.
    assert "postgis" not in error.output
    assert "never by the cluster superuser" in error.output
    assert "trusted = true" in error.output
    assert docker.steps[-1][0] == "client-removed"


def test_other_failures_are_reported_as_they_are(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker(clients=[(1, b'pg_restore: error: relation "x" exists')])
    dump = tmp_path / "seed.pgdump"
    dump.write_bytes(DUMP)

    with pytest.raises(ExternalCommandError) as exc_info:
        system_ops.restore_archive_as_role(
            docker.client,
            settings,
            team,
            str(dump),
            database="d",
            role="r",
            password="p",
        )

    assert exc_info.value.output == 'pg_restore: error: relation "x" exists'


def test_a_load_database_is_created_for_its_owner_in_the_named_cluster(tmp_path):
    settings, _team = _settings(tmp_path)
    docker = _FakeDocker()

    system_ops._create_load_db(
        docker.client, settings, "db__restore", "u_7_prod-erp", container_name=PROD
    )

    assert docker.sql() == [
        (
            PROD,
            SUPERUSER,
            "postgres",
            'CREATE DATABASE "db__restore" OWNER "u_7_prod-erp";',
        ),
        # Before the owner loads anything: no other role of the cluster may
        # connect to the staged copy of a production.
        (
            PROD,
            SUPERUSER,
            "postgres",
            'REVOKE ALL ON DATABASE "db__restore" FROM PUBLIC;',
        ),
        (
            PROD,
            SUPERUSER,
            "db__restore",
            'ALTER SCHEMA public OWNER TO "u_7_prod-erp";',
        ),
    ]


# --------------------------------------------------------------------------
# seeding a production from a development database
# --------------------------------------------------------------------------


def _copy(docker, settings, team, creds, **patches):
    def copy_out(container, container_path, host_path):
        docker.steps.append(("copied-out", container_path, host_path))
        with open(host_path, "wb") as fh:
            fh.write(DUMP)

    with patch.object(
        production_ops, "_copy_file_from_container", side_effect=copy_out
    ):
        production_ops._copy_db_into_prod_cluster(
            docker.client,
            settings,
            team,
            "oduflow_7_feature-x",
            "oduflow_7_prod-erp",
            "environment seed",
            creds,
        )


def test_a_development_database_enters_production_as_the_productions_role(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker()
    creds = {"pg_user": "u_7_prod-erp", "pg_password": "role-pw"}

    _copy(docker, settings, team, creds)

    # The dump is taken where the source lives, and removed from there.
    in_dev = "/tmp/oduflow_7_prod-erp.seed.pgdump"
    assert docker.execs(DEV) == [
        ["pg_dump", "-U", SUPERUSER, "-Fc", "-f", in_dev, "oduflow_7_feature-x"],
        ["rm", "-f", in_dev],
    ]
    # Nothing is copied into, or run inside, the production cluster's container.
    assert docker.execs(PROD) == []
    assert docker.sql() == []

    (client,) = docker.clients()
    assert client["image"] == PROD_IMAGE
    assert client["entrypoint"] == "pg_restore"
    assert client["command"] == [
        "--no-owner",
        "-w",
        "-d",
        "oduflow_7_prod-erp",
        "/restore/dump",
    ]
    assert client["environment"]["PGHOST"] == PROD
    assert client["environment"]["PGUSER"] == "u_7_prod-erp"
    assert client["environment"]["PGPASSWORD"] == "role-pw"
    _assert_no_superuser_restore(docker)

    # The client read the dump from under the team's data directory — a path
    # the Docker daemon can mount — and it is gone afterwards.
    ((host_dump, content),) = docker.mounted.items()
    assert content == DUMP
    assert host_dump.startswith(os.path.join(team.data_dir, "tmp") + os.sep)
    assert not os.path.exists(host_dump)
    assert os.listdir(os.path.join(team.data_dir, "tmp")) == []

    order = [
        docker.index("exec", "pg_dump"),
        docker.index("copied-out"),
        docker.index("exec", "rm"),
        docker.index("client"),
        docker.index("client-removed"),
    ]
    assert order == sorted(order)


def test_a_failed_seed_names_its_step_and_leaves_no_dump_behind(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker(clients=[(1, b"pg_restore: error: boom")])

    with pytest.raises(ExternalCommandError) as exc_info:
        _copy(docker, settings, team, {"pg_user": "u", "pg_password": "p"})

    assert exc_info.value.cmd == "pg_restore (environment seed)"
    assert "boom" in exc_info.value.output
    assert os.listdir(os.path.join(team.data_dir, "tmp")) == []
    assert ["rm", "-f", "/tmp/oduflow_7_prod-erp.seed.pgdump"] in docker.execs(DEV)


def test_a_seed_is_never_restored_by_the_superuser(tmp_path):
    settings, team = _settings(tmp_path)
    docker = _FakeDocker()

    with pytest.raises(PrerequisiteNotMetError, match="as the PostgreSQL superuser"):
        _copy(
            docker,
            settings,
            team,
            {"pg_user": SUPERUSER, "pg_password": SUPERUSER_PASSWORD},
        )

    assert docker.clients() == []


@pytest.mark.parametrize(
    ("seed", "source_db", "label"),
    [
        (production_ops._seed_db_from_template, "oduflow_template_7_base", "template"),
        (
            production_ops._seed_db_from_environment,
            "oduflow_7_feature-x",
            "environment",
        ),
    ],
)
def test_both_seeds_hand_the_productions_role_to_the_copy(
    tmp_path, seed, source_db, label
):
    settings, team = _settings(tmp_path)
    creds = {"pg_user": "u_7_prod-erp", "pg_password": "role-pw"}
    client = MagicMock()
    name = "base" if label == "template" else "feature-x"
    with (
        patch.object(production_ops, "_db_exists", return_value=True),
        patch.object(production_ops, "_copy_db_into_prod_cluster") as copy,
    ):
        seed(client, settings, team, name, "oduflow_7_prod-erp", creds)

    copy.assert_called_once_with(
        client, settings, team, source_db, "oduflow_7_prod-erp", f"{label} seed", creds
    )


# --------------------------------------------------------------------------
# the production's credentials
# --------------------------------------------------------------------------


def test_a_productions_role_is_read_from_its_credentials_file(tmp_path):
    settings, team = _settings(tmp_path)
    created = _production_role(team)

    creds = production_ops.production_db_credentials(settings, team, "erp")

    assert creds == created
    assert creds["pg_user"] == "u_7_prod-erp"
    assert creds["pg_user"] != SUPERUSER


def test_a_production_without_credentials_is_not_given_the_superuser(tmp_path):
    settings, team = _settings(tmp_path)

    with pytest.raises(PrerequisiteNotMetError) as exc_info:
        production_ops.production_db_credentials(settings, team, "erp")

    message = str(exc_info.value)
    assert "Production 'erp' has no scoped database credentials" in message
    assert "never by the cluster superuser" in message
    assert SUPERUSER_PASSWORD not in message


# --------------------------------------------------------------------------
# restoring a snapshot
# --------------------------------------------------------------------------


def _snapshot_patches(stack, docker, *, container=None):
    manifest = {
        "id": "snap",
        "db": {
            "bytes": len(DUMP),
            "key": "db/snap.pgdump",
            "sha256": hashlib.sha256(DUMP).hexdigest(),
        },
        "filestore": {"revision": 0},
    }
    s3 = MagicMock()

    def download(_bucket, _key, destination):
        with open(destination, "wb") as fh:
            fh.write(DUMP)

    s3.download_file.side_effect = download
    for patcher in (
        patch.object(backup_ops.production_registry, "get_production", return_value={}),
        patch.object(backup_ops.production_registry, "set_nested"),
        patch.object(backup_ops.production_registry, "update_production"),
        patch.object(backup_ops, "_load_manifest", return_value=manifest),
        patch.object(backup_ops, "get_client", return_value=docker.client),
        patch.object(production_ops, "_get_container", return_value=container),
        patch.object(production_ops, "append_deploy"),
        patch.object(production_ops, "wait_production_healthy", return_value=True),
        patch.object(backup_ops.s3_client, "make_client", return_value=s3),
        patch("oduflow.wal_monitor.assert_writable"),
    ):
        stack.enter_context(patcher)


def test_a_snapshot_is_restored_by_the_productions_role(tmp_path):
    settings, team = _settings(tmp_path, backup=True)
    creds = _production_role(team)
    role = creds["pg_user"]
    docker = _FakeDocker()
    db = production_ops.prod_db_name(team, "erp")
    staged = f"{db}__restore"

    with ExitStack() as stack:
        _snapshot_patches(stack, docker)
        result = backup_ops.restore_production(settings, team, "erp", "snap")

    assert result["snapshot_id"] == "snap"

    # 1. The client: the production cluster's image and host, the production's
    #    role and password, the staged database, two jobs as before.
    (client,) = docker.clients()
    assert client["image"] == PROD_IMAGE
    assert client["entrypoint"] == "pg_restore"
    assert client["command"] == [
        "--no-owner",
        "-w",
        "-d",
        staged,
        "-j",
        "2",
        "/restore/dump",
    ]
    assert client["environment"] == {
        "PGHOST": PROD,
        "PGUSER": role,
        "PGPASSWORD": creds["pg_password"],
        "PGDATABASE": staged,
        "HOME": "/tmp",
    }
    assert client["network"] == "oduflow-7-net"
    ((host_dump, content),) = docker.mounted.items()
    assert content == DUMP
    assert host_dump.startswith(os.path.join(team.data_dir, "tmp") + os.sep)
    assert not os.path.exists(host_dump)

    # 2. Neither PostgreSQL container restores anything or receives the dump,
    #    and the development cluster is not involved at all.
    _assert_no_superuser_restore(docker)
    assert docker.execs() == []
    assert docker.sql(DEV) == []
    assert {user for _container, user, _db, _sql in docker.sql()} == {SUPERUSER}

    # 3. The order: the superuser creates the staged database for the role,
    #    the role fills it, and only then is anything live renamed.
    order = [
        docker.index("sql", f'DROP DATABASE IF EXISTS "{staged}" WITH (FORCE);'),
        docker.index("sql", f'CREATE DATABASE "{staged}" OWNER "{role}";'),
        docker.index("sql", f'ALTER SCHEMA public OWNER TO "{role}";'),
        docker.index("client"),
        docker.index("client-removed"),
        docker.index("sql", "SET LOCAL search_path = pg_catalog;"),
        docker.index("sql", "base_registry_signaling"),
        docker.index("sql", f'ALTER DATABASE "{db}" RENAME TO "{db}__old";'),
        docker.index("sql", f'ALTER DATABASE "{staged}" RENAME TO "{db}";'),
    ]
    assert order == sorted(order)

    # 4. Inside the restored content the superuser runs utility statements and
    #    the two blocks that pin search_path first — no bare query.
    in_staged = [sql for _c, _user, database, sql in docker.sql() if database == staged]
    assert in_staged[:2] == [f'ALTER SCHEMA public OWNER TO "{role}";'] * 2
    assert len(in_staged) == 4
    for block in in_staged[2:]:
        assert "search_path = pg_catalog" in block


def test_a_snapshot_that_needs_a_superuser_fails_before_anything_live_moves(
    tmp_path,
):
    settings, team = _settings(tmp_path, backup=True)
    _production_role(team)
    denied = b'ERROR:  permission denied to create extension "postgis"'
    docker = _FakeDocker(clients=[(1, denied)])
    db = production_ops.prod_db_name(team, "erp")
    container = MagicMock(status="running")

    with ExitStack() as stack:
        _snapshot_patches(stack, docker, container=container)
        with pytest.raises(ExternalCommandError) as exc_info:
            backup_ops.restore_production(settings, team, "erp", "snap")

    assert exc_info.value.cmd == "pg_restore"
    assert "postgis" in exc_info.value.output
    assert "never by the cluster superuser" in exc_info.value.output
    statements = docker.statements(PROD)
    # The live database keeps its name and the production keeps running...
    assert not any("RENAME" in sql for sql in statements)
    container.stop.assert_not_called()
    # ...the staged one is dropped, and nobody retried as the superuser.
    assert statements[-1] == f'DROP DATABASE IF EXISTS "{db}__restore" WITH (FORCE);'
    assert len(docker.clients()) == 1
    _assert_no_superuser_restore(docker)
    assert os.listdir(os.path.join(team.data_dir, "tmp")) == []


def test_a_corrupt_download_is_refused_before_any_database_work(tmp_path):
    settings, team = _settings(tmp_path, backup=True)
    _production_role(team)
    docker = _FakeDocker()

    with ExitStack() as stack:
        _snapshot_patches(stack, docker)
        stack.enter_context(
            patch.object(
                backup_ops,
                "_load_manifest",
                return_value={
                    "db": {"bytes": 1, "key": "k", "sha256": "0" * 64},
                    "filestore": {"revision": 0},
                },
            )
        )
        with pytest.raises(ExternalCommandError, match="sha256"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    assert docker.clients() == []
    assert not any("CREATE DATABASE" in sql for sql in docker.statements())


@pytest.mark.parametrize(
    "restore",
    [
        lambda settings, team: backup_ops.restore_production(
            settings, team, "erp", "snap"
        ),
        lambda settings, team: backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        ),
    ],
    ids=["snapshot", "environment"],
)
def test_a_production_without_a_scoped_role_is_not_restored(tmp_path, restore):
    """load_credentials falls back to the superuser when the file is gone; the
    restore would then run tenant content as it and hand it every object."""
    settings, team = _settings(tmp_path, backup=True)
    docker = _FakeDocker()

    with ExitStack() as stack:
        _snapshot_patches(stack, docker)
        source = stack.enter_context(patch.object(production_ops, "_source_env_info"))
        with pytest.raises(PrerequisiteNotMetError, match="no scoped database"):
            restore(settings, team)

    assert docker.steps == []
    source.assert_not_called()


# --------------------------------------------------------------------------
# replacing a production's data with an environment's
# --------------------------------------------------------------------------


def test_an_environment_is_promoted_as_the_productions_role(tmp_path):
    settings, team = _settings(tmp_path)
    creds = _production_role(team)
    role = creds["pg_user"]
    docker = _FakeDocker()
    db = production_ops.prod_db_name(team, "erp")
    staged = f"{db}__restore"
    source_env = {
        "container": MagicMock(status="exited"),
        "template": "",
        "head_commit": "",
    }

    def copy_out(container, container_path, host_path):
        with open(host_path, "wb") as fh:
            fh.write(DUMP)

    filestore = tmp_path / "env-filestore"
    filestore.mkdir()
    with ExitStack() as stack:
        _snapshot_patches(stack, docker)
        for patcher in (
            patch.object(production_ops, "_source_env_info", return_value=source_env),
            patch.object(production_ops, "_db_exists", return_value=True),
            patch.object(
                production_ops, "_copy_file_from_container", side_effect=copy_out
            ),
            patch.object(
                production_ops,
                "get_filestore_paths",
                return_value={"merged": str(filestore)},
            ),
        ):
            stack.enter_context(patcher)
        result = backup_ops.restore_production_from_environment(
            settings, team, "erp", "feature-x"
        )

    assert result["source_environment"] == "feature-x"

    # Dumped from the development cluster, restored into the production one by
    # the production's role, into a database that role already owns.
    assert docker.execs(DEV)[0][:2] == ["pg_dump", "-U"]
    assert docker.execs(DEV)[0][-1] == "oduflow_7_feature-x"
    assert docker.execs(PROD) == []
    (client,) = docker.clients()
    assert client["image"] == PROD_IMAGE
    assert client["command"] == ["--no-owner", "-w", "-d", staged, "/restore/dump"]
    assert client["environment"]["PGHOST"] == PROD
    assert client["environment"]["PGUSER"] == role
    assert client["environment"]["PGPASSWORD"] == creds["pg_password"]
    assert client["environment"]["PGDATABASE"] == staged
    _assert_no_superuser_restore(docker)

    order = [
        docker.index("sql", f'DROP DATABASE IF EXISTS "{staged}" WITH (FORCE);'),
        docker.index("sql", f'CREATE DATABASE "{staged}" OWNER "{role}";'),
        docker.index("sql", f'ALTER SCHEMA public OWNER TO "{role}";'),
        docker.index("exec", "pg_dump"),
        docker.index("client"),
        docker.index("client-removed"),
        docker.index("sql", "SET LOCAL search_path = pg_catalog;"),
        docker.index("sql", "base_registry_signaling"),
        docker.index("sql", f'ALTER DATABASE "{staged}" RENAME TO "{db}";'),
    ]
    assert order == sorted(order)
