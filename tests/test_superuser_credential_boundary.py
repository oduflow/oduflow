"""The cluster superuser's password never enters a container a tenant controls.

A tenant is root in its Odoo container (``run_odoo_command``), so whatever
that container was created with is the tenant's to read: its environment,
its command line, the files copied into it. Every database connection there
is made over the network with a password, which makes the superuser's
password the one thing between a tenant and the whole cluster.

Pinned here against a fake Docker:

- an environment or production without a role of its own gets one when its
  container is recreated, instead of the superuser's login;
- where that role cannot be provisioned, the old container stays and nothing
  new is created;
- the template build runs as a one-off role;
- no code outside a short list reads the superuser's password at all, and the
  credentials API does not return it.
"""

import ast
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from pg_helpers import PgContainer

import docker
import oduflow
from oduflow.docker_ops import env_ops, system_ops
from oduflow.env_credentials import load_credentials
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

SUPERUSER = "odoo"
SECRET = "cluster-superuser-secret"
DEV = "oduflow-db"
PROD = "oduflow-prod-db"
ENV_DB = "oduflow_1_main"
ENV_ROLE = "u_1_main"


def _pg(events, name, **kwargs):
    return PgContainer(SUPERUSER, name=name, events=events, **kwargs)


def _settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team_1"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user=SUPERUSER,
        db_password=SECRET,
        teams={"1": team},
    )
    return settings, team


def _legacy_container(settings, events, *, status="running"):
    """An environment created before per-environment roles existed."""
    container = MagicMock(name="legacy-odoo")
    container.status = status
    container.image.tags = ["odoo:17.0"]
    container.image.id = "sha256:old"
    container.labels = {
        settings.managed_label: "true",
        settings.team_label: "1",
        settings.branch_label: "main",
        settings.image_label: "odoo:17.0",
        "oduflow.template": "none",
    }
    container.attrs = {
        "Config": {
            "Env": [f"HOST={DEV}", f"USER={SUPERUSER}", f"PASSWORD={SECRET}"],
            "Cmd": ["odoo", "-d", ENV_DB, "--dev=xml"],
            "Image": "odoo:17.0",
        },
        "HostConfig": {
            "Binds": ["/host/repo:/mnt/extra-addons:rw"],
            "PortBindings": {"8069/tcp": [{"HostPort": "50000"}]},
        },
    }
    for step in ("stop", "start", "remove"):
        getattr(container, step).side_effect = lambda *a, _step=step, **kw: (
            events.append(_step)
        )
    return container


def _docker(events, containers):
    client = MagicMock(name="docker")

    def get(name):
        if name in containers:
            return containers[name]
        raise docker.errors.NotFound(name)

    def run(*args, **kwargs):
        events.append("run")
        return MagicMock(name="new-container")

    client.containers.get.side_effect = get
    client.containers.run.side_effect = run
    client.containers.list.return_value = []
    return client


def _update(settings, team, client, **kwargs):
    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch.object(env_ops, "_reapply_odoo_conf", return_value=False),
        patch.object(
            env_ops, "_install_repo_dependencies", return_value=(0, False, [])
        ),
    ):
        return env_ops.update_environment(
            settings, team, "main", pull_image=False, **kwargs
        )


def _credentials_file(team, env_name="main"):
    return os.path.join(team.workspaces_dir, env_name, "env_credentials.json")


def _record_role(team, env_name, role, password="scoped-pw"):
    os.makedirs(os.path.dirname(_credentials_file(team, env_name)))
    with open(_credentials_file(team, env_name), "w") as fh:
        json.dump({"pg_user": role, "pg_password": password}, fh)


# --------------------------------------------------------------------------
# update_environment
# --------------------------------------------------------------------------


def test_update_gives_a_legacy_environment_a_role_of_its_own(tmp_path):
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(events, "dev", answers={"FROM pg_database": "1"})
    old = _legacy_container(settings, events)
    client = _docker(events, {DEV: pg.container, "oduflow-1-main-odoo": old})

    result = _update(settings, team, client)

    with open(_credentials_file(team)) as fh:
        on_record = json.load(fh)
    assert on_record["pg_user"] == ENV_ROLE
    assert on_record["pg_password"] not in ("", SECRET)

    # The new container connects as that role, and nothing it is created
    # with carries the superuser's password.
    run_kwargs = client.containers.run.call_args.kwargs
    assert run_kwargs["environment"] == {
        "HOST": DEV,
        "USER": ENV_ROLE,
        "PASSWORD": on_record["pg_password"],
    }
    assert SECRET not in repr(client.containers.run.call_args)

    # The database is closed to PUBLIC and handed over, objects included, by
    # the pinned ownership block.
    maintenance, tenant = pg.sql("postgres"), pg.sql(ENV_DB)
    assert f'REVOKE ALL ON DATABASE "{ENV_DB}" FROM PUBLIC;' in maintenance
    assert f'ALTER DATABASE "{ENV_DB}" OWNER TO "{ENV_ROLE}";' in maintenance
    assert f'REVOKE "{SUPERUSER}" FROM "{ENV_ROLE}";' in maintenance
    assert any(
        f'CREATE ROLE "{ENV_ROLE}" LOGIN PASSWORD' in sql
        and on_record["pg_password"] in sql
        for sql in maintenance
    )
    assert tenant[0] == f'ALTER SCHEMA public OWNER TO "{ENV_ROLE}";'
    assert "SET LOCAL search_path = pg_catalog;" in tenant[1]
    assert "ALTER ROUTINE" in tenant[1]

    # All of it with Odoo stopped and the old container still there.
    assert events[0] == "stop"
    assert events.index("remove") > max(
        i for i, event in enumerate(events) if event == "sql:dev"
    )
    assert events[-1] == "run"
    assert "start" not in events

    assert result["db_role_provisioned"] is True
    assert any(ENV_ROLE in line for line in result["setup_logs"])


@pytest.mark.parametrize("status", ["running", "exited"])
def test_a_role_that_cannot_be_provisioned_leaves_the_old_container(tmp_path, status):
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(
        events,
        "dev",
        answers={"FROM pg_database": "1"},
        failures={"DO $$\n        DECLARE r RECORD": "ERROR: must be owner"},
    )
    old = _legacy_container(settings, events, status=status)
    client = _docker(events, {DEV: pg.container, "oduflow-1-main-odoo": old})

    with pytest.raises(PrerequisiteNotMetError) as excinfo:
        _update(settings, team, client)

    message = str(excinfo.value)
    assert "could not be provisioned" in message
    assert "must be owner" in message
    assert "superuser's credentials" in message
    # No new container, and no role on record that does not own its database.
    client.containers.run.assert_not_called()
    assert "remove" not in events
    assert not os.path.exists(_credentials_file(team))
    # A running environment keeps running in the container it had.
    assert ("start" in events) is (status == "running")


def test_a_legacy_environment_without_a_database_is_refused(tmp_path):
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(events, "dev")  # pg_database has no such row
    old = _legacy_container(settings, events)
    client = _docker(events, {DEV: pg.container, "oduflow-1-main-odoo": old})

    with pytest.raises(PrerequisiteNotMetError, match="does not exist"):
        _update(settings, team, client)

    client.containers.run.assert_not_called()
    assert "remove" not in events
    assert len(pg.calls) == 1


def test_an_environment_with_its_own_role_keeps_it(tmp_path):
    settings, team = _settings(tmp_path)
    _record_role(team, "main", ENV_ROLE)
    events: list[str] = []
    pg = _pg(events, "dev")
    old = _legacy_container(settings, events)
    client = _docker(events, {DEV: pg.container, "oduflow-1-main-odoo": old})

    result = _update(settings, team, client)

    environment = client.containers.run.call_args.kwargs["environment"]
    assert (environment["USER"], environment["PASSWORD"]) == (ENV_ROLE, "scoped-pw")
    # Only the role is ensured, as before: no ownership rewrite of the database.
    assert pg.sql(ENV_DB) == []
    assert not any("REVOKE ALL ON DATABASE" in sql for sql in pg.sql())
    assert result["db_role_provisioned"] is False
    assert result["setup_logs"] == []


def test_a_renamed_legacy_environment_takes_its_role_along(tmp_path):
    # The role is provisioned under the old name, before the state moves; the
    # credentials file travels with the workspace.
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(events, "dev", answers={"FROM pg_database": "1"})
    old = _legacy_container(settings, events)
    client = _docker(events, {DEV: pg.container, "oduflow-1-main-odoo": old})

    def relocate(_client, _settings, _team, old_name, new_name):
        os.rename(
            os.path.join(team.workspaces_dir, old_name),
            os.path.join(team.workspaces_dir, new_name),
        )

    with (
        patch.object(env_ops, "check_rename_target"),
        patch.object(env_ops, "_relocate_environment_state", side_effect=relocate),
        patch.object(env_ops, "_agent_rename_env"),
        patch.object(env_ops, "_agent_add_env"),
    ):
        _update(settings, team, client, rename_to="next")

    with open(_credentials_file(team, "next")) as fh:
        on_record = json.load(fh)
    run_kwargs = client.containers.run.call_args.kwargs
    assert run_kwargs["name"] == "oduflow-1-next-odoo"
    assert run_kwargs["environment"]["USER"] == on_record["pg_user"] == ENV_ROLE
    assert run_kwargs["environment"]["PASSWORD"] == on_record["pg_password"]
    assert SECRET not in repr(client.containers.run.call_args)


def test_recreate_builds_a_legacy_environment_from_its_labels_alone(tmp_path):
    # recreate_environment is delete + create, and create always makes a role
    # (see TestCreateEnvironment). What matters here is that nothing of the
    # old container's environment is carried into the new one.
    settings, team = _settings(tmp_path)
    old = _legacy_container(settings, [])
    old.labels["oduflow.env_vars"] = json.dumps({"WORKERS": "2"})
    client = _docker([], {"oduflow-1-main-odoo": old})

    with (
        patch("oduflow.docker_ops.client.get_client", return_value=client),
        patch.object(system_ops, "estimate_new_db_bytes", return_value=0),
        patch.object(system_ops, "check_disk_space"),
        patch.object(env_ops, "_ensure_system_ready"),
        patch.object(env_ops, "delete_environment") as delete,
        patch.object(env_ops, "create_environment", return_value={}) as create,
    ):
        env_ops.recreate_environment(settings, team, "main")

    delete.assert_called_once()
    assert create.call_args.kwargs["env_vars"] == {"WORKERS": "2"}
    # Past the settings object itself: only what the labels recorded.
    assert SECRET not in repr((create.call_args.args[2:], create.call_args.kwargs))
    assert SUPERUSER not in create.call_args.kwargs.values()


# --------------------------------------------------------------------------
# provision_scoped_role
# --------------------------------------------------------------------------


def test_provisioning_can_be_repeated_and_selects_the_cluster(tmp_path):
    settings, team = _settings(tmp_path)
    events: list[str] = []
    dev = _pg(events, "dev")
    prod = _pg(events, "prod", answers={"FROM pg_database": "1"})
    client = _docker(events, {DEV: dev.container, PROD: prod.container})
    kept = {"pg_user": "u_1_prod-erp", "pg_password": "the-one-in-use"}

    for _ in range(2):
        creds = system_ops.provision_scoped_role(
            client, settings, team, "prod-erp", creds=kept, container_name=PROD
        )
        assert creds == kept

    assert dev.calls == []
    # The password handed in is the one set and recorded: a container still
    # running with it is not locked out.
    assert any("PASSWORD 'the-one-in-use'" in sql for sql in prod.sql("postgres"))
    assert load_credentials("prod-erp", team.workspaces_dir, SUPERUSER) == kept
    first, second = (
        prod.calls[: len(prod.calls) // 2],
        prod.calls[len(prod.calls) // 2 :],
    )
    assert first == second


def test_the_superuser_is_never_recorded_as_a_databases_role(tmp_path):
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(events, "dev", answers={"FROM pg_database": "1"})
    client = _docker(events, {DEV: pg.container})

    with pytest.raises(PrerequisiteNotMetError, match="superuser"):
        system_ops.provision_scoped_role(
            client,
            settings,
            team,
            "main",
            creds={"pg_user": SUPERUSER, "pg_password": SECRET},
        )

    assert not os.path.exists(_credentials_file(team))
    assert len(pg.calls) == 1  # the existence check


# --------------------------------------------------------------------------
# containers that still hold it
# --------------------------------------------------------------------------


def _listed(settings, name, labels, env):
    container = MagicMock()
    container.name = name
    container.labels = {settings.managed_label: "true", **labels}
    container.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}}
    return container


def test_start_names_the_containers_created_with_the_superusers_login(tmp_path, caplog):
    settings, _team = _settings(tmp_path)
    superuser_env = {"HOST": DEV, "USER": SUPERUSER, "PASSWORD": SECRET}
    client = MagicMock()
    client.containers.list.return_value = [
        _listed(
            settings, "env-legacy", {settings.branch_label: "legacy"}, superuser_env
        ),
        _listed(settings, "prod-legacy", {"oduflow.prod": "true"}, superuser_env),
        _listed(
            settings,
            "env-scoped",
            {settings.branch_label: "main"},
            {"HOST": DEV, "USER": ENV_ROLE, "PASSWORD": "scoped-pw"},
        ),
        # A tenant may set USER itself; without the password it holds nothing.
        _listed(
            settings,
            "env-named",
            {settings.branch_label: "named"},
            {"USER": SUPERUSER, "PASSWORD": "a-guess"},
        ),
        # The clusters hold it by definition, and a service's variables are
        # its tenant's own choice.
        _listed(
            settings,
            "the-cluster",
            {settings.system_label: "true", "oduflow.prod": "true"},
            {"POSTGRES_USER": SUPERUSER, "POSTGRES_PASSWORD": SECRET, **superuser_env},
        ),
        _listed(settings, "a-service", {"oduflow.service": "true"}, superuser_env),
    ]

    assert system_ops.superuser_credential_holders(client, settings) == [
        "env-legacy",
        "prod-legacy",
    ]

    with caplog.at_level("WARNING", logger="oduflow"):
        system_ops._warn_superuser_credential_holders(client, settings)
    warning = caplog.records[-1].getMessage()
    assert "env-legacy, prod-legacy" in warning
    assert "update_environment" in warning and "reconfigure_production" in warning
    assert SECRET not in warning


def test_the_check_never_fails_a_start(tmp_path, caplog):
    settings, _team = _settings(tmp_path)
    client = MagicMock()
    client.containers.list.side_effect = docker.errors.APIError("daemon busy")

    with caplog.at_level("WARNING", logger="oduflow"):
        system_ops._warn_superuser_credential_holders(client, settings)

    assert "Could not check containers" in caplog.records[-1].getMessage()


# --------------------------------------------------------------------------
# init_template
# --------------------------------------------------------------------------


def test_the_template_build_runs_as_a_one_off_role(tmp_path):
    # The image is the operator's choice, but it may be one a tenant built,
    # and the build is root in its container.
    settings, team = _settings(tmp_path)
    events: list[str] = []
    pg = _pg(events, "dev")
    builder = MagicMock(name="builder")
    builder.wait.return_value = {"StatusCode": 0}
    client = _docker(events, {DEV: pg.container})
    client.containers.run.side_effect = None
    client.containers.run.return_value = builder

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(system_ops, "_ensure_pg_container"),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "require_postgres_for_odoo"),
        patch.object(system_ops, "ensure_team_network", return_value="net"),
        patch.object(system_ops, "_stream_exec_to_file") as dump,
        patch.object(system_ops, "_extract_archive_from_container", return_value=0),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="100:101"),
        patch.object(system_ops, "chown_recursive"),
        patch.object(system_ops, "reload_template", return_value={}),
        patch.object(system_ops, "_update_template_sizes", side_effect=lambda *a: a[3]),
    ):
        system_ops.init_template(settings, team, "base", odoo_image="odoo:17.0")

    environment = client.containers.run.call_args.kwargs["environment"]
    role = environment["USER"]
    assert role.startswith("oduflow_load_") and role != SUPERUSER
    assert environment["PASSWORD"] not in ("", SECRET)
    assert SECRET not in repr(client.containers.run.call_args)

    # What the image put into the build database is dumped by that role too,
    # next to PostgreSQL, before the role is dropped.
    dump.assert_called_once()
    assert dump.call_args.args[1] is pg.container
    assert dump.call_args.args[2] == [
        "pg_dump",
        "-U",
        role,
        "-Fp",
        "--no-owner",
        "oduflow_template_build",
    ]

    statements = pg.sql()
    create_role = next(sql for sql in statements if sql.startswith("CREATE ROLE"))
    assert f'"{role}"' in create_role and "NOSUPERUSER" in create_role
    assert f'CREATE DATABASE oduflow_template_build OWNER "{role}";' in statements
    assert 'REVOKE ALL ON DATABASE "oduflow_template_build" FROM PUBLIC;' in statements
    # Nothing of the build outlives it.
    assert statements.index(
        "DROP DATABASE IF EXISTS oduflow_template_build WITH (FORCE);"
    ) < statements.index(f'DROP ROLE IF EXISTS "{role}";')


# --------------------------------------------------------------------------
# where the password may be read at all
# --------------------------------------------------------------------------

# function -> why it reads Settings.db_password. Everything else connects
# through the local socket of a PostgreSQL container, which asks for none.
_PASSWORD_READERS = {
    # The clusters are initialised with it (POSTGRES_PASSWORD).
    ("docker_ops/system_ops.py", "_ensure_pg_container"),
    ("docker_ops/system_ops.py", "_ensure_prod_pg_container"),
    # Compared with what existing containers hold, to report them.
    ("docker_ops/system_ops.py", "superuser_credential_holders"),
    # The PostgreSQL upgrade's helper: the cluster's own image, in the
    # cluster's network namespace, running a fixed pg_dump / pg_restore — and
    # only for the steps the superuser itself takes (superuser=True).
    ("postgres_migration.py", "_run_helper"),
}


def _password_readers():
    root = pathlib.Path(oduflow.__file__).parent
    readers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "db_password"):
                continue
            scope = node
            while scope in parents and not isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                scope = parents[scope]
            readers.add(
                (
                    path.relative_to(root).as_posix(),
                    getattr(scope, "name", "<module>"),
                )
            )
    return readers


def test_only_the_clusters_own_setup_reads_the_superusers_password():
    # A new reader is a new way for the password to reach a container, a
    # file or a command line. Give the tenant-facing code a scoped role
    # instead (system_ops.provision_scoped_role); extend the list only for
    # something no tenant can reach, and say why.
    assert _password_readers() == _PASSWORD_READERS


def test_the_credentials_api_does_not_hand_out_the_superusers_password(tmp_path):
    settings, team = _settings(tmp_path)

    creds = load_credentials("main", team.workspaces_dir, settings.db_user)

    assert creds == {"pg_user": SUPERUSER, "pg_password": ""}
