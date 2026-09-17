from pathlib import Path
from unittest.mock import patch

import pytest

from oduflow.errors import ConflictError
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import StackValidationError, load_stack, resolve_env_values
from oduflow.stack_models import ServiceDatabase, ServiceRoute, ValueFrom
from oduflow.stack_ops import (
    PlanAction,
    StackPlan,
    _environment_hash_with_sanitize,
    _file_matches,
    _resource_hash,
    apply_stack,
    build_plan,
)

MANIFEST = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: acme
spec:
  environment:
    name: acme-dev
    branch: main
    repoUrl: https://github.com/acme/addons.git
    odooImage: odoo:18.0
    template: default
    env:
      LOG_LEVEL: info
    modules:
      install: [acme_base]
  extraRepositories:
    enterprise:
      repoUrl: https://github.com/odoo/enterprise.git
      branch: "18.0"
  volumes:
    fs-data:
      description: FreeSWITCH data
  files:
    - source: files/fs.conf
      volume: fs-data
      path: config/fs.conf
  services:
    fs:
      image: example/fs:1
      port: 8080
      env:
        ODOO_URL:
          environmentField: url
      volumes:
        - source: fs-data
          target: /data
"""


@pytest.fixture(autouse=True)
def allow_example_repo_urls():
    with (
        patch("oduflow.stack_ops.git_ops.validate_repo_url"),
        patch(
            "oduflow.stack_ops.system_ops.list_templates",
            return_value=[{"template_name": "default", "db_loaded": True}],
        ),
    ):
        yield


@pytest.fixture
def stack_fixture(tmp_path):
    data_dir = tmp_path / "team"
    team = TeamSettings(
        team_id="1",
        data_dir=str(data_dir),
        port_registry_path=str(data_dir / "ports.json"),
        port_range_start=50000,
        port_range_end=50100,
        hostname="localhost",
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="odoo",
        teams={"1": team},
    )
    path = tmp_path / "oduflow.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    (files / "fs.conf").write_text("profile=internal\n", encoding="utf-8")
    return settings, team, load_stack(path), str(path)


def test_host_environment_value_is_required():
    value = ValueFrom(fromEnv="MISSING_SECRET")

    with pytest.raises(StackValidationError, match="MISSING_SECRET"):
        resolve_env_values({"TOKEN": value}, environ={})


def test_database_value_is_resolved_without_persisting_reference_details(stack_fixture):
    settings, team, _manifest, _path = stack_fixture
    value = ValueFrom(database="events", databaseField="url")
    with patch(
        "oduflow.docker_ops.service_database_ops.get_database",
        return_value={"status": "ready", "url": "postgresql://secret"},
    ):
        resolved = resolve_env_values(
            {"DATABASE_URL": value}, settings=settings, team=team
        )

    assert resolved == {"DATABASE_URL": "postgresql://secret"}


def test_database_is_looked_up_once_per_resource_not_once_per_variable(stack_fixture):
    """Every lookup costs several round trips into the PostgreSQL container,
    and a full PG* set points six variables at the same database."""
    settings, team, _manifest, _path = stack_fixture
    values = {
        key: ValueFrom(database="events", databaseField=field)
        for key, field in (
            ("PGHOST", "host"),
            ("PGPORT", "port"),
            ("PGDATABASE", "database"),
            ("PGUSER", "username"),
            ("PGPASSWORD", "password"),
            ("DATABASE_URL", "url"),
        )
    }
    live = {
        "status": "ready",
        "host": "oduflow-db",
        "port": 5432,
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "secret",
        "url": "postgresql://svc_1_events:secret@oduflow-db:5432/oduflow_service_1_events",
    }
    with patch(
        "oduflow.docker_ops.service_database_ops.get_database", return_value=live
    ) as get_database:
        resolved = resolve_env_values(values, settings=settings, team=team)

    assert get_database.call_count == 1
    assert resolved["PGPORT"] == "5432"
    assert resolved["DATABASE_URL"] == live["url"]


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_new_stack_has_dependency_ordered_actions(
    _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture

    plan = build_plan(settings, team, manifest, path)

    assert [(item.operation, item.resource) for item in plan.actions] == [
        ("create", "extraRepositories.enterprise"),
        ("create", "volumes.fs-data"),
        ("create", "environment"),
        ("write", "files.fs-data:config/fs.conf"),
        ("create", "services.fs"),
        ("install", "modules"),
    ]


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.service_database_ops.list_databases", return_value=[])
def test_plan_creates_database_before_its_consumers(
    _databases, _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.databases = {"events": ServiceDatabase()}
    manifest.spec.services["fs"].env["DATABASE_URL"] = ValueFrom(
        database="events", databaseField="url"
    )

    plan = build_plan(settings, team, manifest, path)
    resources = [item.resource for item in plan.actions]

    assert resources.index("databases.events") < resources.index("environment")
    assert resources.index("databases.events") < resources.index("services.fs")


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_database_ops.list_databases", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_updates_a_service_whose_database_is_being_recreated(
    envs, env_info, _databases, _volumes, _repos, stack_fixture
):
    """A database deleted out of band is recreated with a *fresh* password. The
    consuming container still holds the superseded one, so the plan must say so
    — otherwise apply recreates the database, skips the service, and reports
    success while the sidecar can no longer connect."""
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.environment.modules.install = []
    manifest.spec.databases = {"events": ServiceDatabase()}
    service = manifest.spec.services["fs"]
    service.env = {"DATABASE_URL": ValueFrom(database="events", databaseField="url")}
    service.volumes = []
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
            "stack_sanitize": "true",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}
    actual_service = {
        "name": "fs",
        "image": "example/fs:1",
        "port": 8080,
        "hostname": None,
        "env_vars": {"DATABASE_URL": "postgresql://svc:superseded@oduflow-db/events"},
        "image_env_vars": {},
        "host_mode": False,
        "volumes": [],
        "cap_add": [],
        "privileged": False,
        "routes": [],
        "stack": "acme",
        "stack_resource": "services.fs",
        # Unchanged: the manifest did not move, only the database vanished.
        "stack_spec_hash": _resource_hash(service),
    }

    with patch(
        "oduflow.stack_ops.service_ops.list_services", return_value=[actual_service]
    ):
        plan = build_plan(settings, team, manifest, path)

    operations = {item.resource: item.operation for item in plan.actions}
    assert operations["databases.events"] == "create"
    assert operations["services.fs"] == "update"


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
def test_plan_reports_unreadable_credentials_as_such(
    _envs, _services, _volumes, _repos, stack_fixture
):
    """A row degraded by unreadable credentials carries no ownership labels;
    reporting it as foreign ownership would send the operator hunting for a
    conflict that does not exist."""
    settings, team, manifest, path = stack_fixture
    manifest.spec.databases = {"events": ServiceDatabase()}
    degraded = [
        {
            "name": "events",
            "status": "credentials-error",
            "stack": "",
            "stack_resource": "",
            "stack_spec_hash": "",
        }
    ]

    with patch(
        "oduflow.stack_ops.service_database_ops.list_databases", return_value=degraded
    ):
        plan = build_plan(settings, team, manifest, path)

    action = next(item for item in plan.actions if item.resource == "databases.events")
    assert action.operation == "conflict"
    assert action.detail == "stored credentials for this database cannot be read"


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_rejects_unavailable_template_before_apply(
    _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture

    with patch("oduflow.stack_ops.system_ops.list_templates", return_value=[]):
        plan = build_plan(settings, team, manifest, path)

    assert (
        PlanAction("conflict", "environment", "template 'default' is not available")
        in plan.actions
    )


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_refuses_to_adopt_existing_environment(
    envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    envs.return_value = [{"env_name": "acme-dev", "stack": "", "stack_resource": ""}]

    with (
        patch("oduflow.stack_ops.service_ops.list_services", return_value=[]),
        patch(
            "oduflow.stack_ops.env_ops.get_environment_info",
            return_value={"env_vars": {}},
        ),
        patch("oduflow.stack_ops._installed_modules", return_value=set()),
    ):
        plan = build_plan(settings, team, manifest, path)

    conflict = next(item for item in plan.actions if item.resource == "environment")
    assert conflict.operation == "conflict"
    assert "not owned" in conflict.detail


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_is_empty_when_owned_environment_matches(
    envs, env_info, _services, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.services = {}
    manifest.spec.environment.modules.install = []
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
            "stack_sanitize": "true",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}

    plan = build_plan(settings, team, manifest, path)

    assert plan.actions == ()


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_treats_sanitize_change_as_immutable(
    envs, env_info, _services, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.services = {}
    manifest.spec.environment.modules.install = []
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
            "stack_sanitize": "false",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}

    plan = build_plan(settings, team, manifest, path)

    conflict = next(item for item in plan.actions if item.resource == "environment")
    assert conflict.operation == "conflict"
    assert "sanitize" in conflict.detail


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_refuses_ambiguous_legacy_sanitize_drift(
    envs, env_info, _services, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.services = {}
    manifest.spec.environment.modules.install = []
    legacy = manifest.model_copy(deep=True)
    legacy.spec.environment.odoo_image = "odoo:17.0"
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "stack_spec_hash": _environment_hash_with_sanitize(legacy, True),
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:17.0",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}

    plan = build_plan(settings, team, manifest, path)

    conflict = next(item for item in plan.actions if item.resource == "environment")
    assert conflict.operation == "conflict"
    assert "sanitize" in conflict.detail


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_ignores_service_image_environment_defaults(
    envs, env_info, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.environment.modules.install = []
    service = manifest.spec.services["fs"]
    service.env = {"CACHE_SIZE": "256"}
    service.volumes = []
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
            "stack_sanitize": "true",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}
    actual_service = {
        "name": "fs",
        "image": "example/fs:1",
        "port": 8080,
        "hostname": None,
        "env_vars": {"CACHE_SIZE": "256", "IMAGE_VERSION": "1.0"},
        "image_env_vars": {"IMAGE_VERSION": "1.0"},
        "host_mode": False,
        "volumes": [],
        "cap_add": [],
        "privileged": False,
        "routes": [],
        "stack": "acme",
        "stack_resource": "services.fs",
        "stack_spec_hash": _resource_hash(service),
    }

    with patch(
        "oduflow.stack_ops.service_ops.list_services", return_value=[actual_service]
    ):
        plan = build_plan(settings, team, manifest, path)

    assert plan.actions == ()


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.get_environment_info")
@patch("oduflow.stack_ops.env_ops.list_environments")
def test_plan_reports_service_command_drift(
    envs, env_info, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.environment.modules.install = []
    service = manifest.spec.services["fs"]
    service.env = {}
    service.volumes = []
    service.command = ["server", "/data"]
    envs.return_value = [
        {
            "env_name": "acme-dev",
            "stack": "acme",
            "stack_resource": "environment",
            "repo_url": "https://github.com/acme/addons.git",
            "git_branch": "main",
            "template_name": "default",
            "extra_addons": {},
            "odoo_image": "odoo:18.0",
            "stack_sanitize": "true",
        }
    ]
    env_info.return_value = {"env_vars": {"LOG_LEVEL": "info"}}
    actual_service = {
        "name": "fs",
        "image": "example/fs:1",
        "port": 8080,
        "hostname": None,
        "env_vars": {},
        "image_env_vars": {},
        "host_mode": False,
        "volumes": [],
        "cap_add": [],
        "privileged": False,
        "command": [],
        "routes": [],
        "stack": "acme",
        "stack_resource": "services.fs",
        "stack_spec_hash": _resource_hash(service),
    }

    with patch(
        "oduflow.stack_ops.service_ops.list_services", return_value=[actual_service]
    ):
        plan = build_plan(settings, team, manifest, path)

    action = next(item for item in plan.actions if item.resource == "services.fs")
    assert action.operation == "update"
    assert "command" in action.detail

    # An explicit desired command that happens to equal the image CMD is
    # already converged. Docker inspect cannot distinguish those two sources,
    # but their effective argv is identical.
    actual_service["image_command"] = ["server", "/data"]
    with patch(
        "oduflow.stack_ops.service_ops.list_services", return_value=[actual_service]
    ):
        converged_plan = build_plan(settings, team, manifest, path)

    assert converged_plan.actions == ()


def test_stack_file_comparison_uses_manifest_size_limit(stack_fixture):
    settings, team, _manifest, _path = stack_fixture
    content = "x" * 200_000

    with patch(
        "oduflow.stack_ops.volume_file_ops.read_file_in_volume",
        return_value={"type": "file", "output": content},
    ) as read_file:
        assert _file_matches(settings, team, "fs-data", "large.conf", content)

    read_file.assert_called_once_with(
        settings, team, "fs-data", "large.conf", max_bytes=1_000_000
    )


def test_apply_preflights_conflicts_before_mutation(stack_fixture):
    settings, team, manifest, path = stack_fixture
    conflict = StackPlan("acme", (PlanAction("conflict", "environment", "owned"),))

    with (
        patch("oduflow.stack_ops.build_plan", return_value=conflict),
        patch("oduflow.stack_ops.volume_ops.create_volume") as create_volume,
    ):
        with pytest.raises(ConflictError, match="apply refused"):
            apply_stack(settings, team, manifest, path)

    create_volume.assert_not_called()


def test_apply_creates_resources_and_persists_non_secret_state(stack_fixture):
    settings, team, manifest, path = stack_fixture
    actions = StackPlan(
        "acme",
        (
            PlanAction("create", "extraRepositories.enterprise"),
            PlanAction("create", "volumes.fs-data"),
            PlanAction("create", "environment"),
            PlanAction("write", "files.fs-data:config/fs.conf"),
            PlanAction("create", "services.fs"),
            PlanAction("install", "modules", "acme_base"),
        ),
    )
    events = []

    with (
        patch("oduflow.stack_ops.build_plan", return_value=actions),
        patch(
            "oduflow.extra_addons.clone_extra_repo",
            side_effect=lambda *a, **k: events.append("repo"),
        ),
        patch(
            "oduflow.stack_ops.volume_ops.create_volume",
            side_effect=lambda *a, **k: events.append("volume"),
        ),
        patch(
            "oduflow.stack_ops.env_ops.create_environment",
            side_effect=lambda *a, **k: events.append("environment"),
        ),
        patch(
            "oduflow.stack_ops.volume_file_ops.write_file_in_volume",
            side_effect=lambda *a, **k: events.append("file"),
        ),
        patch(
            "oduflow.stack_ops.env_ops.get_env_base_url",
            return_value=("http://odoo", "localhost"),
        ),
        patch(
            "oduflow.stack_ops.service_ops.create_service",
            side_effect=lambda *a, **k: events.append("service"),
        ) as create_service,
        patch(
            "oduflow.stack_ops.odoo_ops.install_odoo_modules",
            side_effect=lambda *a, **k: events.append("modules") or {"exit_code": 0},
        ),
        patch(
            "oduflow.stack_ops.env_ops.restart_environment",
            side_effect=lambda *a, **k: events.append("restart"),
        ),
    ):
        result = apply_stack(settings, team, manifest, path)

    assert result == actions
    assert events == [
        "repo",
        "volume",
        "environment",
        "file",
        "service",
        "modules",
        "restart",
    ]
    assert create_service.call_args.kwargs["env_vars"] == {"ODOO_URL": "http://odoo"}
    state = Path(team.data_dir) / "stacks/acme.json"
    assert state.is_file()
    assert "http://odoo" not in state.read_text(encoding="utf-8")


def test_apply_no_changes_is_idempotent(stack_fixture):
    settings, team, manifest, path = stack_fixture
    empty = StackPlan("acme", ())

    with (
        patch("oduflow.stack_ops.build_plan", return_value=empty),
        patch("oduflow.stack_ops.env_ops.create_environment") as create_environment,
    ):
        result = apply_stack(settings, team, manifest, path)

    assert result == empty
    assert (Path(team.data_dir) / "stacks/acme.json").is_file()
    create_environment.assert_not_called()


def test_apply_wires_generated_database_url_without_saving_it_in_state(stack_fixture):
    settings, team, manifest, path = stack_fixture
    manifest.spec.extra_repositories = {}
    manifest.spec.volumes = {}
    manifest.spec.files = []
    manifest.spec.environment.modules.install = []
    manifest.spec.databases = {"events": ServiceDatabase()}
    manifest.spec.services["fs"].env = {
        "DATABASE_URL": ValueFrom(database="events", databaseField="url")
    }
    actions = StackPlan(
        "acme",
        (
            PlanAction("create", "databases.events"),
            PlanAction("create", "environment"),
            PlanAction("create", "services.fs"),
        ),
    )
    generated = {
        "status": "ready",
        "url": "postgresql://svc:secret@oduflow-db/events",
    }

    with (
        patch("oduflow.stack_ops.build_plan", return_value=actions),
        patch("oduflow.stack_ops.service_database_ops.create_database"),
        patch(
            "oduflow.docker_ops.service_database_ops.get_database",
            return_value=generated,
        ),
        patch("oduflow.stack_ops.env_ops.create_environment"),
        patch("oduflow.stack_ops.service_ops.create_service") as create_service,
    ):
        apply_stack(settings, team, manifest, path)

    assert create_service.call_args.kwargs["env_vars"] == {
        "DATABASE_URL": generated["url"]
    }
    state = Path(team.data_dir) / "stacks/acme.json"
    text = state.read_text(encoding="utf-8")
    assert "events" in text
    assert "secret" not in text


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_reports_route_mode_conflict_before_apply(
    _services, _envs, _volumes, _repos, stack_fixture
):
    settings, team, manifest, path = stack_fixture
    manifest.spec.services["fs"].port = None
    manifest.spec.services["fs"].routes = [ServiceRoute(path="/events", port=8080)]

    plan = build_plan(settings, team, manifest, path)

    assert (
        PlanAction(
            "conflict",
            "services.fs",
            "HTTP routes require routing.mode = 'traefik'",
        )
        in plan.actions
    )


class TestDesiredServiceHostname:
    """Stack drift must compare against what create_service actually assigns.

    When this helper spelled the nesting rule out itself it kept using the team
    hostname after base_domain landed, so in a zone team every plan reported
    hostname drift and every apply — including the `--stack` reconciliation on
    each server restart — recreated the container.
    """

    @staticmethod
    def _team(tmp_path, **kw):
        return TeamSettings(
            team_id="1",
            hostname=kw.pop("hostname", "oduflow.demo.example.com"),
            data_dir=str(tmp_path / "team"),
            **kw,
        )

    def test_zone_team_matches_create_service(self, tmp_path):
        from oduflow.domains import service_hostname
        from oduflow.stack_ops import _desired_hostname

        team = self._team(tmp_path, base_domain="demo.example.com")
        settings = Settings(routing_mode="traefik", teams={"1": team})

        assert (
            _desired_hostname(settings, team, "meilisearch", None)
            == "meilisearch.demo.example.com"
        )
        assert _desired_hostname(settings, team, "meilisearch", None) == (
            service_hostname(team, "meilisearch", None)
        )

    def test_legacy_team_layout_is_unchanged(self, tmp_path):
        from oduflow.stack_ops import _desired_hostname

        team = self._team(tmp_path, hostname="dev.example.com")
        settings = Settings(routing_mode="traefik", teams={"1": team})

        assert (
            _desired_hostname(settings, team, "meilisearch", None)
            == "meilisearch.dev.example.com"
        )

    def test_explicit_fqdn_is_left_alone(self, tmp_path):
        from oduflow.stack_ops import _desired_hostname

        team = self._team(tmp_path, base_domain="demo.example.com")
        settings = Settings(routing_mode="traefik", teams={"1": team})

        assert (
            _desired_hostname(settings, team, "meilisearch", "search.customer.com")
            == "search.customer.com"
        )

    def test_port_routing_keeps_the_raw_value(self, tmp_path):
        from oduflow.stack_ops import _desired_hostname

        team = self._team(tmp_path, base_domain="demo.example.com")
        settings = Settings(routing_mode="port", teams={"1": team})

        assert _desired_hostname(settings, team, "meilisearch", None) is None
