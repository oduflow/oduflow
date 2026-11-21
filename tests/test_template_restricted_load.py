"""A template dump is tenant input and is loaded without superuser rights.

An import pulls its dump from a URL, bucket or path the caller names, and a
published environment's dump carries whatever its tenant defined. Loaded the
old way — ``psql -U <superuser> -f`` inside the shared PostgreSQL container —
any of them could ``COPY ... FROM PROGRAM``, make its own role a superuser, or
simply ``\\!`` into a root shell next to every team's data files.

These tests pin the two halves of the fix against a fake Docker: the dump's
content runs as a one-off role that owns only the database being loaded, and
it is read by a client in a throwaway container, never by a process in the
PostgreSQL container.
"""

import contextlib
import os
import re
import stat
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import psql_call

from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings

SUPERUSER = "odoo"
SUPERUSER_PASSWORD = "cluster-superuser-secret"
TPL_DB = "oduflow_template_7_prod"
OWNER = "oduflow_tplown_7"
TABLESPACE = "oduflow_team_7"
PG_IMAGE = "postgres:16.4"


class _FakeDocker:
    """The shared PostgreSQL container, the restore clients, and one ordered
    log of everything either of them is asked to do."""

    def __init__(self, *, clients=((0, b""),), database_owner=None, converted=None):
        self.steps: list[tuple] = []
        self._clients = list(clients)
        # What the conversion helper writes as SQL, when one is expected.
        self._converted = converted
        # The template database, when it already exists: its owner's name.
        self._databases = {TPL_DB: database_owner} if database_owner else {}
        self.fail_sql: dict[str, str] = {}

        self.db = MagicMock(name="oduflow-db")
        self.db.attrs = {"Config": {"Image": PG_IMAGE}, "Mounts": []}
        self.db.exec_run.side_effect = self._exec_in_db

        self.client = MagicMock(name="docker")
        self.client.containers.get.return_value = self.db
        self.client.containers.run.side_effect = self._run_container

    # -- the PostgreSQL container ------------------------------------------

    def _exec_in_db(self, cmd, **kwargs):
        call = psql_call(cmd, SUPERUSER)
        if call is None:
            self.steps.append(("db-exec", cmd))
            return 0, b""
        role, database, sql = call
        self.steps.append(("sql", role, database, sql))
        for marker, error in self.fail_sql.items():
            if marker in sql:
                return 1, error.encode()
        return 0, self._answer(sql).encode()

    def _answer(self, sql: str) -> str:
        if sql.startswith("SELECT 1 FROM pg_database"):
            return "1" if TPL_DB in self._databases else ""
        if sql.startswith("SELECT 1 FROM pg_tablespace"):
            return "1"
        if "d.datdba" in sql:
            return self._databases.get(TPL_DB) or ""
        if sql.startswith("SELECT COUNT(*) FROM information_schema.tables"):
            return "412"
        created = re.match(r'CREATE DATABASE "([^"]+)" OWNER "([^"]+)"', sql)
        if created:
            self._databases[created.group(1)] = created.group(2)
        dropped = re.match(r'DROP DATABASE (?:IF EXISTS )?"([^"]+)"', sql)
        if dropped:
            self._databases.pop(dropped.group(1), None)
        return ""

    # -- restore clients ---------------------------------------------------

    def _run_container(self, image, command=None, **kwargs):
        if kwargs.get("network_mode") == "none":
            return self._run_conversion_helper(image, command, **kwargs)
        exit_code, output = self._clients.pop(0)
        self.steps.append(("client", {"image": image, "command": command, **kwargs}))
        container = MagicMock(name=kwargs.get("name"))
        container.wait.return_value = {"StatusCode": exit_code}
        container.logs.return_value = output
        container.remove.side_effect = lambda **kw: self.steps.append(
            ("client-removed", kwargs.get("name"))
        )
        return container

    def _run_conversion_helper(self, image, command, **kwargs):
        assert self._converted is not None, "no conversion was expected"
        self.steps.append(("helper", {"image": image, "command": command, **kwargs}))
        (out_dir,) = [
            host
            for host, mount in kwargs["volumes"].items()
            if mount["bind"] == "/convert/out"
        ]
        with open(os.path.join(out_dir, "dump.sql"), "wb") as fh:
            fh.write(self._converted)
        container = MagicMock(name=kwargs.get("name"))
        container.status = "exited"
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = b""
        container.remove.side_effect = lambda **kw: self.steps.append(
            ("helper-removed", kwargs.get("name"))
        )
        return container

    # -- views of the log --------------------------------------------------

    def sql(self, role=None):
        return [
            step[1:]
            for step in self.steps
            if step[0] == "sql" and role in (None, step[1])
        ]

    def clients(self):
        return [step[1] for step in self.steps if step[0] == "client"]

    def index(self, kind, needle=""):
        for position, step in enumerate(self.steps):
            if step[0] == kind and needle in str(step[1:]):
                return position
        raise AssertionError(f"no {kind} step containing {needle!r}: {self.steps}")


def _settings(tmp_path):
    team = TeamSettings(team_id="7", data_dir=str(tmp_path / "team_7"))
    return (
        Settings(
            base_data_dir=str(tmp_path),
            db_user=SUPERUSER,
            db_password=SUPERUSER_PASSWORD,
            teams={"7": team},
        ),
        team,
    )


def _dump(team, name, content, *, mode=0o600):
    directory = team.get_template_dir("prod")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(content)
    os.chmod(path, mode)
    return path


def _reload(tmp_path, docker, dump_name, content, *, mode=0o600, **kwargs):
    settings, team = _settings(tmp_path)
    dump = _dump(team, dump_name, content, mode=mode)
    with (
        patch.object(system_ops, "get_client", return_value=docker.client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(system_ops, "_pg_restore_jobs", return_value=4),
    ):
        result = system_ops.reload_template(
            settings, team, "prod", dump_path=dump, persist_dump=False, **kwargs
        )
    return result, dump


def _loader(docker):
    created = [
        sql
        for _role, _db, sql in docker.sql()
        if sql.startswith('CREATE ROLE "oduflow_load_')
    ]
    assert len(created) == 1
    match = re.fullmatch(
        r'CREATE ROLE "(oduflow_load_[0-9a-f]{16})" WITH LOGIN NOSUPERUSER '
        r"NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
        r"PASSWORD '([A-Za-z0-9_-]{32})';",
        created[0],
    )
    assert match, created[0]
    return match.group(1), match.group(2)


# --------------------------------------------------------------------------
# who runs the dump, and where
# --------------------------------------------------------------------------


def test_an_archive_is_restored_by_the_loader_from_a_client_container(tmp_path):
    docker = _FakeDocker()

    result, dump = _reload(tmp_path, docker, "dump.pgdump", b"PGDMP...")

    loader, password = _loader(docker)
    assert result["status"] == "reloaded"
    assert result["tables"] == "412"

    # 1. The client: the cluster's own image, on the team network, as the
    #    loader — and holding nothing of the superuser's.
    (client,) = docker.clients()
    owner = os.stat(dump)
    assert client["image"] == PG_IMAGE
    assert client["entrypoint"] == "pg_restore"
    assert client["command"] == [
        "--no-owner",
        "-w",
        "-d",
        TPL_DB,
        "-j",
        "4",
        "/restore/dump",
    ]
    assert client["network"] == "oduflow-7-net"
    assert client["environment"] == {
        "PGHOST": "oduflow-db",
        "PGUSER": loader,
        "PGPASSWORD": password,
        "PGDATABASE": TPL_DB,
        "HOME": "/tmp",
    }
    assert client["volumes"] == {dump: {"bind": "/restore/dump", "mode": "ro"}}
    # A private dump is read as its owner: without capabilities nobody else can.
    assert client["user"] == f"{owner.st_uid}:{owner.st_gid}"
    assert client["cap_drop"] == ["ALL"]
    assert client["security_opt"] == ["no-new-privileges"]
    assert client["detach"] is True
    assert "privileged" not in client
    assert SUPERUSER_PASSWORD not in repr(client)
    assert password != SUPERUSER_PASSWORD

    # 2. The PostgreSQL container: server-built statements only. Nothing there
    #    reads the dump, and no restore tool runs in it.
    assert not [step for step in docker.steps if step[0] == "db-exec"]
    docker.db.put_archive.assert_not_called()
    for _role, _database, sql in docker.sql():
        assert "/restore" not in sql
        assert dump not in sql
        assert not sql.lstrip().startswith("\\")

    # 3. The order: the superuser prepares, the loader restores, the superuser
    #    hands the result to the template owner, and only then is it a template.
    order = [
        docker.index("sql", f'CREATE ROLE "{OWNER}"'),
        docker.index("sql", f'CREATE ROLE "{loader}"'),
        docker.index("sql", f'CREATE DATABASE "{TPL_DB}" OWNER "{loader}" TABLESPACE'),
        docker.index("sql", f'ALTER SCHEMA public OWNER TO "{loader}"'),
        docker.index("client"),
        docker.index("client-removed"),
        docker.index("sql", f'ALTER ROLE "{loader}" PASSWORD NULL'),
        docker.index("sql", "pg_terminate_backend"),
        docker.index("sql", f'REASSIGN OWNED BY "{loader}" TO "{OWNER}"'),
        docker.index("sql", f'DROP ROLE IF EXISTS "{loader}"'),
        docker.index("sql", "information_schema.tables"),
        docker.index("sql", "datistemplate=true"),
    ]
    assert order == sorted(order)


def test_the_superuser_only_prepares_and_hands_over(tmp_path):
    docker = _FakeDocker()

    _reload(tmp_path, docker, "dump.pgdump", b"PGDMP...")

    loader, _password = _loader(docker)
    in_template_db = [sql for _role, db, sql in docker.sql(SUPERUSER) if db == TPL_DB]
    # Inside the database a dump filled, the superuser issues two utility
    # statements and no query: a query there would resolve the dump's own
    # operators and functions with superuser rights.
    assert in_template_db == [
        f'ALTER SCHEMA public OWNER TO "{loader}";',
        f'REASSIGN OWNED BY "{loader}" TO "{OWNER}"; DROP OWNED BY "{loader}";',
    ]
    # ...and the first of them runs before the dump does.
    assert docker.index("sql", "ALTER SCHEMA public") < docker.index("client")

    # Reads of the restored database go through the template owner.
    assert docker.sql(OWNER) == [
        (
            OWNER,
            TPL_DB,
            "SELECT COUNT(*) FROM information_schema.tables WHERE "
            "table_schema='public' AND table_type='BASE TABLE';",
        )
    ]
    assert {role for role, _db, _sql in docker.sql()} == {SUPERUSER, OWNER}


def test_the_template_owner_is_an_ordinary_role_without_a_password(tmp_path):
    docker = _FakeDocker()
    settings, team = _settings(tmp_path)

    assert system_ops.ensure_template_owner_role(docker.client, settings, team) == OWNER

    ((role, database, sql),) = docker.sql()
    assert (role, database) == (SUPERUSER, "postgres")
    assert f'CREATE ROLE "{OWNER}" WITH LOGIN NOSUPERUSER NOCREATEDB' in sql
    assert "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS" in sql
    # It cannot authenticate over the network: there is no password, and one
    # set later (a role may change its own) would already be expired.
    assert "PASSWORD" not in sql
    assert "VALID UNTIL 'epoch'" in sql
    # Two loads of one team may create it at the same moment.
    assert "duplicate_object OR unique_violation" in sql


def test_plain_sql_is_run_by_psql_in_the_client(tmp_path):
    docker = _FakeDocker()

    _reload(tmp_path, docker, "dump.sql", b"\\! id > /pwned\n", mode=0o644)

    (client,) = docker.clients()
    assert client["entrypoint"] == "psql"
    assert client["command"] == ["-X", "-q", "-w", "-f", "/restore/dump"]
    # A world-readable dump does not even need its owner's uid.
    assert client["user"] == "65534:65534"
    assert client["environment"]["PGUSER"] == _loader(docker)[0]
    assert not [step for step in docker.steps if step[0] == "db-exec"]


@pytest.mark.parametrize(
    ("name", "content", "pipeline"),
    [
        (
            "dump.sql.gz",
            b"-- sql",
            "set -o pipefail; gunzip -c /restore/dump | psql -X -q -w -f -",
        ),
        (
            "dump.pgdump.gz",
            b"PGDMP",
            "set -o pipefail; gunzip -c /restore/dump | "
            f"pg_restore --no-owner -w -d {TPL_DB}",
        ),
    ],
)
def test_gzipped_dumps_are_piped_inside_the_client(tmp_path, name, content, pipeline):
    import gzip

    docker = _FakeDocker()

    _reload(tmp_path, docker, name, gzip.compress(content))

    (client,) = docker.clients()
    assert client["entrypoint"] == "bash"
    assert client["command"] == ["-c", pipeline]
    assert client["environment"]["PGUSER"] == _loader(docker)[0]


def test_strict_restores_stay_restricted(tmp_path):
    docker = _FakeDocker()

    _reload(tmp_path, docker, "dump.pgdump", b"PGDMP", strict=True)

    (client,) = docker.clients()
    assert client["entrypoint"] == "pg_restore"
    assert {"--no-owner", "--no-acl", "--exit-on-error"} <= set(client["command"])
    assert client["environment"]["PGUSER"] == _loader(docker)[0]


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------


def test_a_failed_restore_still_retires_the_loader(tmp_path):
    docker = _FakeDocker(clients=[(1, b"pg_restore: error: could not execute query")])

    with pytest.raises(ExternalCommandError, match="could not execute query"):
        _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    loader, _password = _loader(docker)
    statements = [sql for _role, _db, sql in docker.sql(SUPERUSER)]
    assert f'ALTER ROLE "{loader}" PASSWORD NULL;' in statements
    assert (
        f'REASSIGN OWNED BY "{loader}" TO "{OWNER}"; DROP OWNED BY "{loader}";'
        in statements
    )
    assert f'DROP ROLE IF EXISTS "{loader}";' in statements
    assert docker.index("client-removed") < docker.index("sql", "PASSWORD NULL")
    # A half-restored database is never published as a template.
    assert not any("datistemplate=true" in sql for sql in statements)


def test_a_client_that_cannot_start_still_retires_the_loader(tmp_path):
    docker = _FakeDocker()
    docker.client.containers.run.side_effect = RuntimeError("no such image")

    with pytest.raises(RuntimeError, match="no such image"):
        _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    loader, _password = _loader(docker)
    assert (SUPERUSER, "postgres", f'DROP ROLE IF EXISTS "{loader}";') in docker.sql()


def test_a_dump_needing_superuser_rights_fails_with_the_reason(tmp_path):
    docker = _FakeDocker(
        clients=[
            (
                1,
                b"pg_restore: error: could not execute query: ERROR:  "
                b'permission denied to create extension "postgis"\n'
                b"HINT:  Must be superuser to create this extension.\n",
            )
        ]
    )

    with pytest.raises(ExternalCommandError) as failure:
        _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    assert 'permission denied to create extension "postgis"' in failure.value.output
    assert "restricted PostgreSQL role" in failure.value.output
    assert "trusted" in failure.value.output


@pytest.mark.parametrize(
    ("dump", "source"), [("dump.sql", "/restore/dump"), ("dump.sql.gz", "<stdin>")]
)
def test_sql_statement_failure_is_not_a_successful_load(tmp_path, dump, source):
    import gzip

    warnings = (
        f'psql:{source}:1: ERROR:  unrecognized configuration parameter "transaction_timeout"\n'
        f'psql:{source}:2: ERROR:  must be able to SET ROLE "odoo"\n'
        f"psql:{source}:3: ERROR:  must be owner of extension plpgsql\n"
    )
    denied = 'permission denied to create extension "pg_stat_statements"'
    content = gzip.compress(b"-- sql") if dump.endswith(".gz") else b"-- sql"

    result, _ = _reload(
        tmp_path, _FakeDocker(clients=[(0, warnings.encode())]), dump, content
    )
    assert result["status"] == "reloaded"

    failed = _FakeDocker(
        clients=[(0, f"{warnings}psql:{source}:4: ERROR:  {denied}\n".encode())]
    )
    with pytest.raises(ExternalCommandError) as failure:
        _reload(tmp_path, failed, dump, content)
    assert denied in failure.value.output
    assert "restricted PostgreSQL role" in failure.value.output


def test_an_unreadable_archive_is_converted_and_loaded_by_the_same_loader(tmp_path):
    docker = _FakeDocker(
        clients=[
            (1, b"pg_restore: error: unsupported version (1.16) in file header"),
            (0, b""),
        ]
    )
    settings, team = _settings(tmp_path)
    converted = _dump(team, "dump.sql", b"-- converted", mode=0o644)

    with patch.object(
        system_ops,
        "_converted_sql_dump",
        return_value=contextlib.nullcontext(converted),
    ):
        result, dump = _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    assert result["status"] == "reloaded"
    loader, _password = _loader(docker)
    first, second = docker.clients()
    assert first["entrypoint"] == "pg_restore"
    assert second["entrypoint"] == "psql"
    assert second["command"] == ["-X", "-q", "-w", "-f", "/restore/dump"]
    assert second["volumes"] == {converted: {"bind": "/restore/dump", "mode": "ro"}}
    assert first["environment"]["PGUSER"] == second["environment"]["PGUSER"] == loader
    # The database the failed attempt touched is recreated for the same loader.
    created = [
        sql
        for _role, _db, sql in docker.sql(SUPERUSER)
        if sql.startswith(f'CREATE DATABASE "{TPL_DB}"')
    ]
    assert (
        created
        == [f'CREATE DATABASE "{TPL_DB}" OWNER "{loader}" TABLESPACE "{TABLESPACE}";']
        * 2
    )
    assert not [step for step in docker.steps if step[0] == "db-exec"]


UNREADABLE = (1, b"pg_restore: error: unsupported version (1.16) in file header")


def test_the_conversion_helper_gets_neither_the_template_directory_nor_a_role(
    tmp_path,
):
    docker = _FakeDocker(clients=[UNREADABLE, (0, b"")], converted=b"-- converted")

    result, dump = _reload(tmp_path, docker, "dump.pgdump", b"PGDMP", mode=0o644)

    assert result["status"] == "reloaded"
    loader, loader_password = _loader(docker)
    (helper,) = [step[1] for step in docker.steps if step[0] == "helper"]
    template_dir = os.path.dirname(dump)
    scratch_root = str(tmp_path / "team_7" / ".dump_convert")
    # The archive, read-only, and a directory of its own — not the directory
    # the archive lies in, which holds the template's other files.
    assert helper["volumes"][dump] == {"bind": "/convert/in", "mode": "ro"}
    (out_dir,) = [h for h in helper["volumes"] if h != dump]
    assert helper["volumes"][out_dir] == {"bind": "/convert/out", "mode": "rw"}
    assert out_dir.startswith(scratch_root + os.sep)
    assert template_dir not in helper["volumes"]
    # It connects to nothing: no network, and no role's password to leak.
    assert helper["network_mode"] == "none"
    assert "network" not in helper
    assert loader_password not in str(helper)
    assert SUPERUSER_PASSWORD not in str(helper)
    assert helper["cap_drop"] == ["ALL"]
    assert helper["security_opt"] == ["no-new-privileges"]
    assert helper["read_only"] is True
    assert not helper["user"].startswith("0:")

    # Its SQL is loaded like any other dump: by the loader, from a client
    # that sees that one file read-only.
    first, second = docker.clients()
    assert second["entrypoint"] == "psql"
    assert second["environment"]["PGUSER"] == loader
    ((sql_path, mount),) = second["volumes"].items()
    assert mount == {"bind": "/restore/dump", "mode": "ro"}
    assert sql_path == os.path.join(os.path.dirname(out_dir), "dump.sql")
    # The helper is gone before its output is used.
    assert docker.index("helper-removed") < docker.index("client", "psql")

    # A dump handed in by a caller stays that caller's: nothing is written
    # next to it, and the scratch directory is gone.
    assert os.listdir(template_dir) == ["dump.pgdump"]
    assert os.listdir(scratch_root) == []


def test_a_template_reloaded_from_its_own_archive_keeps_the_sql_instead(tmp_path):
    docker = _FakeDocker(clients=[UNREADABLE, (0, b"")], converted=b"-- converted")
    settings, team = _settings(tmp_path)
    archive = _dump(team, "dump.pgdump", b"PGDMP", mode=0o644)

    with (
        patch.object(system_ops, "get_client", return_value=docker.client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_update_template_sizes"),
    ):
        result = system_ops.reload_template(settings, team, "prod")

    assert result["status"] == "reloaded"
    template_dir = os.path.dirname(archive)
    # The cluster cannot read the archive, so the SQL takes its place.
    assert os.listdir(template_dir) == ["dump.sql"]
    with open(os.path.join(template_dir, "dump.sql"), "rb") as fh:
        assert fh.read() == b"-- converted"
    assert team.get_template_sql_path("prod") == os.path.join(template_dir, "dump.sql")
    assert os.listdir(tmp_path / "team_7" / ".dump_convert") == []


def test_sql_that_fails_to_load_is_not_kept(tmp_path):
    docker = _FakeDocker(
        clients=[
            UNREADABLE,
            (0, b'psql:/restore/dump:42: ERROR:  relation "x" does not exist'),
        ],
        converted=b"-- converted",
    )
    settings, team = _settings(tmp_path)
    archive = _dump(team, "dump.pgdump", b"PGDMP", mode=0o644)

    with (
        patch.object(system_ops, "get_client", return_value=docker.client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_update_template_sizes"),
        pytest.raises(ExternalCommandError) as failure,
    ):
        system_ops.reload_template(settings, team, "prod")

    assert failure.value.cmd == "psql"
    assert os.listdir(os.path.dirname(archive)) == ["dump.pgdump"]
    assert os.listdir(tmp_path / "team_7" / ".dump_convert") == []
    # The loader of the failed load is retired all the same.
    loader, _password = _loader(docker)
    assert (SUPERUSER, "postgres", f'DROP ROLE IF EXISTS "{loader}";') in docker.sql()


def test_reload_drops_the_loader_a_crashed_load_left_behind(tmp_path):
    stale = "oduflow_load_0123456789abcdef"
    docker = _FakeDocker(database_owner=stale)

    _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    loader, _password = _loader(docker)
    assert loader != stale
    dropped_db = docker.index("sql", f'DROP DATABASE "{TPL_DB}"')
    assert dropped_db < docker.index("sql", f'ALTER ROLE "{stale}" PASSWORD NULL')
    assert dropped_db < docker.index("sql", f'DROP ROLE IF EXISTS "{stale}"')


def test_reload_leaves_the_owner_of_an_existing_template_alone(tmp_path):
    docker = _FakeDocker(database_owner=OWNER)

    _reload(tmp_path, docker, "dump.pgdump", b"PGDMP")

    assert not any(
        sql.startswith(("DROP ROLE", "ALTER ROLE")) and f'"{OWNER}"' in sql
        for _role, _db, sql in docker.sql()
    )


def test_a_loader_that_cannot_be_dropped_is_disabled(tmp_path, caplog):
    docker = _FakeDocker()
    docker.fail_sql["DROP ROLE"] = (
        'ERROR:  role "oduflow_load_0123456789abcdef" cannot be dropped because '
        "some objects depend on it"
    )
    settings, _team = _settings(tmp_path)

    system_ops._retire_template_loader(
        docker.client, settings, "oduflow_load_0123456789abcdef"
    )

    assert [sql for _role, _db, sql in docker.sql()][-1] == (
        'ALTER ROLE "oduflow_load_0123456789abcdef" NOLOGIN;'
    )
    assert "could not be dropped" in caplog.text


# --------------------------------------------------------------------------
# small parts
# --------------------------------------------------------------------------


def test_restore_client_user_follows_the_dump_permissions(tmp_path):
    dump = tmp_path / "dump.sql"
    dump.write_text("-- sql")

    dump.chmod(0o640)
    owner = dump.stat()
    assert not owner.st_mode & stat.S_IROTH
    assert (
        system_ops._restore_client_user(str(dump)) == f"{owner.st_uid}:{owner.st_gid}"
    )

    dump.chmod(0o644)
    assert system_ops._restore_client_user(str(dump)) == "65534:65534"


def test_restore_client_image_falls_back_to_the_configured_one():
    container = MagicMock()
    container.attrs = {"Config": {"Image": "postgres:17"}}
    assert system_ops._container_image(container, "postgres:16") == "postgres:17"
    assert system_ops._container_image(MagicMock(), "postgres:16") == "postgres:16"


def test_skipped_ownership_statements_are_folded_into_one_line():
    output = "\n".join(
        [
            'psql:/restore/dump:41: ERROR:  must be able to SET ROLE "odoo"',
            'psql:/restore/dump:57: ERROR:  must be member of role "odoo"',
            'psql:/restore/dump:90: ERROR:  relation "x" does not exist',
        ]
    )

    summary = system_ops._summarize_restore_output(output)

    assert summary.splitlines() == [
        'psql:/restore/dump:90: ERROR:  relation "x" does not exist',
        "2 ownership statement(s) in the dump were not applied: "
        "the template keeps one owner.",
    ]
    assert system_ops._summarize_restore_output("") == ""


def test_the_manifest_is_read_as_the_template_owner(tmp_path):
    docker = _FakeDocker()
    settings, team = _settings(tmp_path)

    system_ops._read_template_manifest_from_db(docker.client, settings, team, TPL_DB)

    assert [(role, db) for role, db, _sql in docker.sql()] == [
        (OWNER, TPL_DB),
        (SUPERUSER, "postgres"),
        (OWNER, TPL_DB),
    ]
    # The one superuser statement does not touch the restored database.
    assert docker.sql(SUPERUSER) == [(SUPERUSER, "postgres", "SHOW server_version;")]


# --------------------------------------------------------------------------
# after the clone: the superuser inside a database a template filled
# --------------------------------------------------------------------------


def test_post_clone_superuser_blocks_pin_their_search_path(tmp_path):
    # With "public" on the path, an operator such as public.!~~(name, name)
    # from a template dump is a better match for `rolname NOT LIKE '...'` than
    # the built-in one, and would run with superuser rights.
    docker = _FakeDocker()
    settings, _team = _settings(tmp_path)

    system_ops.reassign_db_ownership(
        docker.client, settings, "oduflow_7_feature", "u_7_feature"
    )
    system_ops.drop_signaling_sequences(docker.client, settings, "oduflow_7_feature")

    blocks = [sql for _role, _db, sql in docker.sql() if "DO $$" in sql]
    assert len(blocks) == 2
    for block in blocks:
        body = block.split("BEGIN", 1)[1].lstrip()
        assert body.startswith("SET LOCAL search_path = pg_catalog;")
    # Nothing is dropped or re-owned by a name that depends on the path.
    assert "quote_ident" not in "".join(blocks)
    assert "format('DROP SEQUENCE IF EXISTS %I.%I', r.nspname, r.relname)" in blocks[1]
    assert "n.nspname = 'public'" in blocks[1]
    # The only other statement there is a utility one.
    assert [sql for _role, _db, sql in docker.sql() if "DO $$" not in sql] == [
        'ALTER SCHEMA public OWNER TO "u_7_feature";'
    ]


def test_module_versions_of_a_clone_are_read_as_the_environment_role(tmp_path):
    from oduflow.docker_ops import env_ops

    docker = _FakeDocker()
    settings, _team = _settings(tmp_path)

    with patch.object(env_ops, "_exec_sql", return_value="{}") as sql:
        upgrades, error = env_ops._template_module_upgrades(
            docker.client,
            settings,
            "oduflow_7_feature",
            {},
            None,
            "feature",
            db_user="u_7_feature",
        )

    assert (upgrades, error) == ({}, None)
    sql.assert_called_once()
    assert sql.call_args.kwargs == {"db": "oduflow_7_feature", "user": "u_7_feature"}
