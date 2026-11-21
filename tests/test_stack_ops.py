from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from oduflow import stack_production
from oduflow.errors import ConflictError, ExternalCommandError
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import (
    StackValidationError,
    load_stack,
    manifest_hash,
    resolve_env_values,
)
from oduflow.stack_models import (
    ServiceDatabase,
    ServiceRoute,
    StackManifest,
    ValueFrom,
)
from oduflow.stack_ops import (
    STACK_SPEC_HASH_LABEL,
    PlanAction,
    StackPlan,
    _environment_hash_with_sanitize,
    _environment_labels,
    _file_matches,
    _resource_hash,
    _service_kwargs,
    _service_order,
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
            "oduflow.extra_addons.register_extra_repo",
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


def test_stack_update_dependency_failure_preserves_actionable_error(stack_fixture):
    settings, team, manifest, path = stack_fixture
    actions = StackPlan(
        "acme",
        (
            PlanAction("update", "environment"),
            PlanAction("create", "services.fs"),
            PlanAction("install", "modules", "acme_base"),
        ),
    )
    with (
        patch("oduflow.stack_ops.build_plan", return_value=actions),
        patch(
            "oduflow.stack_ops.env_ops.update_environment",
            return_value={
                "exit_code": 1,
                "env_name": "acme-dev",
                "setup_logs": ["[PIP] failed"],
            },
        ),
        patch("oduflow.stack_ops.service_ops.create_service") as service,
        patch("oduflow.stack_ops.odoo_ops.install_odoo_modules") as install,
        patch("oduflow.stack_ops.save_state") as save,
    ):
        with pytest.raises(ExternalCommandError, match="was recreated") as exc:
            apply_stack(settings, team, manifest, path)
    assert "acme-dev" in str(exc.value)
    assert "[PIP] failed" in str(exc.value)
    assert "update_environment" in str(exc.value)
    assert exc.value.exit_code == 1
    service.assert_not_called()
    install.assert_not_called()
    save.assert_not_called()


# --- Sibling service references -------------------------------------------

REFERENCES_MANIFEST = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: inbox
spec:
  environment:
    name: inbox-dev
    branch: main
    repoUrl: https://github.com/acme/addons.git
    odooImage: odoo:18.0
  services:
    app:
      image: example/app:1
      port: 3000
      env:
        UPSTASH_REDIS_URL:
          service: redis-http
          serviceField: containerName
          format: "http://{value}:80"
    mailer:
      image: example/mailer:1
      port: 2525
    redis-http:
      image: hiett/serverless-redis-http:0.0.10
      port: 8888
      env:
        REDIS_HOST:
          service: redis
          serviceField: containerName
    redis:
      image: redis:7
      port: 6379
    docs:
      image: example/docs:1
      port: 8000
"""

# Targets first, unrelated services in manifest order (mailer before docs).
REFERENCE_ORDER = ["mailer", "redis", "redis-http", "app", "docs"]


@pytest.fixture
def references(stack_fixture, tmp_path):
    settings, team, _manifest, _path = stack_fixture
    path = tmp_path / "references" / "oduflow.yaml"
    path.parent.mkdir()
    path.write_text(REFERENCES_MANIFEST, encoding="utf-8")
    return settings, team, load_stack(path), str(path)


def _live_services(settings, team, manifest):
    """What create_service records for each service of an applied manifest."""
    live = []
    for name, desired in manifest.spec.services.items():
        kwargs = _service_kwargs(settings, team, manifest, name, desired, None)
        live.append(
            {
                "name": name,
                "image": kwargs["image"],
                "port": kwargs["port"],
                "hostname": kwargs["hostname"],
                "env_vars": dict(kwargs["env_vars"] or {}),
                "image_env_vars": {},
                "host_mode": kwargs["host_mode"],
                "volumes": kwargs["volumes"] or [],
                "cap_add": kwargs["cap_add"] or [],
                "privileged": kwargs["privileged"],
                "routes": kwargs["routes"] or [],
                "command": kwargs["command"] or [],
                "runtime": kwargs["runtime"],
                "stack": manifest.metadata.name,
                "stack_resource": f"services.{name}",
                "stack_spec_hash": kwargs["stack_labels"][STACK_SPEC_HASH_LABEL],
            }
        )
    return live


def _plan_against(settings, team, manifest, path, live_services):
    """Plan with the Odoo environment already applied and the given services."""
    environment = {
        "env_name": "inbox-dev",
        "stack": "inbox",
        "stack_resource": "environment",
        "repo_url": "https://github.com/acme/addons.git",
        "git_branch": "main",
        "template_name": None,
        "extra_addons": {},
        "odoo_image": "odoo:18.0",
        "stack_sanitize": "true",
    }
    with ExitStack() as patches:
        for target, value in (
            ("oduflow.extra_addons.list_extra_repos", []),
            ("oduflow.stack_ops.volume_ops.list_volumes", []),
            ("oduflow.stack_ops.env_ops.list_environments", [environment]),
            ("oduflow.stack_ops.env_ops.get_environment_info", {"env_vars": {}}),
            ("oduflow.stack_ops.service_ops.list_services", live_services),
        ):
            patches.enter_context(patch(target, return_value=value))
        return build_plan(settings, team, manifest, path)


@pytest.mark.parametrize(
    "prefix,team_id,service,expected",
    [
        ("oduflow-", "1", "redis", "oduflow-1-svc-redis"),
        ("oduflow-", "2", "redis", "oduflow-2-svc-redis"),
        ("acme-", "7", "redis", "acme-7-svc-redis"),
        ("oduflow-", "2", "inbox.cache_1", "oduflow-2-svc-inbox.cache_1"),
    ],
)
def test_service_reference_resolves_to_the_team_container_name(
    stack_fixture, prefix, team_id, service, expected
):
    settings, team, _manifest, _path = stack_fixture
    value = ValueFrom(service=service, serviceField="containerName")

    # Pure naming: the sibling need not exist, and Docker is never asked.
    with patch(
        "oduflow.docker_ops.client.get_client", side_effect=AssertionError("docker")
    ):
        resolved = resolve_env_values(
            {"REDIS_HOST": value},
            settings=replace(settings, prefix=prefix),
            team=replace(team, team_id=team_id),
        )

    assert resolved == {"REDIS_HOST": expected}


@pytest.mark.parametrize(
    "template,expected",
    [
        ("redis://{value}:6379", "redis://oduflow-1-svc-redis:6379"),
        ("http://{value}:80", "http://oduflow-1-svc-redis:80"),
    ],
)
def test_service_reference_format_wraps_the_name_once(
    stack_fixture, template, expected
):
    settings, team, _manifest, _path = stack_fixture
    value = ValueFrom(service="redis", serviceField="containerName", format=template)

    resolved = resolve_env_values({"URL": value}, settings=settings, team=team)

    assert resolved == {"URL": expected}


def test_service_reference_needs_the_server_context():
    value = ValueFrom(service="redis", serviceField="containerName")

    with pytest.raises(StackValidationError, match="service 'redis'"):
        resolve_env_values({"REDIS_HOST": value}, environ={})


def test_service_order_puts_targets_first_and_keeps_manifest_order(references):
    _settings, _team, manifest, _path = references

    assert list(manifest.spec.services) == [
        "app",
        "mailer",
        "redis-http",
        "redis",
        "docs",
    ]
    assert _service_order(manifest.spec.services) == REFERENCE_ORDER


@patch("oduflow.extra_addons.list_extra_repos", return_value=[])
@patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
@patch("oduflow.stack_ops.env_ops.list_environments", return_value=[])
@patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
def test_plan_creates_referenced_services_before_consumers(
    _services, _envs, _volumes, _repos, references
):
    settings, team, manifest, path = references

    plan = build_plan(settings, team, manifest, path)

    assert [(item.operation, item.resource) for item in plan.actions] == [
        ("create", "environment"),
        *(("create", f"services.{name}") for name in REFERENCE_ORDER),
    ]


@pytest.mark.parametrize(
    "prefix,team_id",
    [("oduflow-", "1"), ("oduflow-", "2"), ("acme-", "1")],
)
def test_apply_creates_services_in_reference_order(references, prefix, team_id):
    settings, team, manifest, path = references
    settings = replace(settings, prefix=prefix)
    team = replace(team, team_id=team_id)
    # Deliberately in YAML order: apply must not depend on the plan's order.
    plan = StackPlan(
        "inbox",
        (
            PlanAction("create", "environment"),
            *(
                PlanAction("create", f"services.{name}")
                for name in manifest.spec.services
            ),
        ),
    )
    created = []

    with (
        patch("oduflow.stack_ops.build_plan", return_value=plan),
        patch("oduflow.stack_ops.env_ops.create_environment"),
        patch(
            "oduflow.stack_ops.service_ops.create_service",
            side_effect=lambda _s, _t, name, **kwargs: created.append((name, kwargs)),
        ),
    ):
        apply_stack(settings, team, manifest, path)

    assert [name for name, _ in created] == REFERENCE_ORDER
    env_by_service = {name: kwargs["env_vars"] for name, kwargs in created}
    assert env_by_service["redis-http"] == {
        "REDIS_HOST": f"{prefix}{team_id}-svc-redis"
    }
    assert env_by_service["app"] == {
        "UPSTASH_REDIS_URL": f"http://{prefix}{team_id}-svc-redis-http:80"
    }
    # Ownership hashes and the state record keep the declarative reference,
    # so the same manifest carries the same hashes on every team and server.
    for name, kwargs in created:
        assert kwargs["stack_labels"][STACK_SPEC_HASH_LABEL] == _resource_hash(
            manifest.spec.services[name]
        )
    state = (Path(team.data_dir) / "stacks/inbox.json").read_text(encoding="utf-8")
    assert "-svc-" not in state


@pytest.mark.parametrize(
    "prefix,team_id",
    [("oduflow-", "1"), ("oduflow-", "2"), ("acme-", "1")],
)
def test_applied_service_references_report_no_drift(references, prefix, team_id):
    settings, team, manifest, path = references
    settings = replace(settings, prefix=prefix)
    team = replace(team, team_id=team_id)
    live = _live_services(settings, team, manifest)

    plan = _plan_against(settings, team, manifest, path, live)

    assert plan.actions == ()


def test_prefix_change_updates_only_the_consumers(references):
    settings, team, manifest, path = references
    live = _live_services(settings, team, manifest)

    plan = _plan_against(replace(settings, prefix="acme-"), team, manifest, path, live)

    assert [(a.operation, a.resource, a.detail) for a in plan.actions] == [
        ("update", "services.redis-http", "env (REDIS_HOST)"),
        ("update", "services.app", "env (UPSTASH_REDIS_URL)"),
    ]
    assert not any("acme-" in action.detail for action in plan.actions)


def test_unreadable_live_env_is_reported_as_bare_env_drift(references):
    settings, team, manifest, path = references
    live = _live_services(settings, team, manifest)
    # A degraded inspect result: the live env cannot be read at all. That is
    # drift with no attributable key, rendered as plain "env" in the plan.
    next(item for item in live if item["name"] == "redis")["env_vars"] = None

    plan = _plan_against(settings, team, manifest, path, live)

    assert [(a.operation, a.resource, a.detail) for a in plan.actions] == [
        ("update", "services.redis", "env"),
    ]


def test_replacing_a_referenced_service_leaves_consumers_alone(references):
    settings, team, manifest, path = references
    live = _live_services(settings, team, manifest)
    manifest.spec.services["redis"].image = "redis:8"

    plan = _plan_against(settings, team, manifest, path, live)

    assert [(a.operation, a.resource, a.detail) for a in plan.actions] == [
        ("update", "services.redis", "image"),
    ]


def test_unorderable_references_fail_before_any_mutation(references):
    settings, team, manifest, path = references
    # Validation rejects cycles at load; a spec mutated afterwards must still
    # be refused before apply touches anything.
    manifest.spec.services["redis"].env["UPSTREAM"] = ValueFrom(
        service="app", serviceField="containerName"
    )

    with (
        patch("oduflow.extra_addons.list_extra_repos", return_value=[]),
        patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[]),
        patch("oduflow.stack_ops.env_ops.list_environments", return_value=[]),
        patch("oduflow.stack_ops.service_ops.list_services", return_value=[]),
        patch("oduflow.stack_ops.env_ops.create_environment") as create_environment,
        patch("oduflow.stack_ops.service_ops.create_service") as create_service,
    ):
        with pytest.raises(
            StackValidationError,
            match=(
                "cannot be ordered: services.app -> services.redis-http "
                "-> services.redis -> services.app"
            ),
        ):
            apply_stack(settings, team, manifest, path)

    create_environment.assert_not_called()
    create_service.assert_not_called()
    assert not (Path(team.data_dir) / "stacks/inbox.json").exists()


def test_existing_manifests_keep_their_hashes():
    """Digests computed before service references existed. Adding optional
    ValueFrom keys must not move any of them, or every existing Stack would
    report drift (and productions would be recreated)."""
    dev = StackManifest.model_validate(
        {
            "metadata": {"name": "legacy"},
            "spec": {
                "environment": {
                    "name": "legacy-dev",
                    "branch": "main",
                    "repoUrl": "https://github.com/acme/addons.git",
                    "odooImage": "odoo:18.0",
                    "env": {
                        "LOG_LEVEL": "info",
                        "API_KEY": {"fromEnv": "ACME_API_KEY"},
                    },
                },
                "databases": {"events": {}},
                "services": {
                    "worker": {
                        "image": "example/worker:1",
                        "port": 8080,
                        "env": {
                            "ODOO_URL": {"environmentField": "url"},
                            "DATABASE_URL": {
                                "database": "events",
                                "databaseField": "url",
                            },
                            "SECRET": {"fromEnv": "WORKER_SECRET"},
                        },
                    }
                },
            },
        }
    )
    prod = StackManifest.model_validate(
        {
            "metadata": {"name": "legacy-prod"},
            "spec": {
                "production": {
                    "name": "control",
                    "domain": "example.org",
                    "repoUrl": "https://github.com/acme/app.git",
                    "branch": "production",
                    "odooImage": "odoo:19.0",
                    "env": {"MODE": "production", "KEY": {"fromEnv": "CONTROL_KEY"}},
                },
                "services": {
                    "gateway": {
                        "image": "acme/gateway:1",
                        "port": 8080,
                        "env": {"ODOO_HOST": {"productionField": "containerName"}},
                    }
                },
            },
        }
    )

    assert manifest_hash(dev) == (
        "4541a1fac67f25faa0cd36245e5ce1236734c41a7d77b275431354c38d25e7c4"
    )
    assert _environment_labels("legacy", dev)[STACK_SPEC_HASH_LABEL] == (
        "7811f30e643626803931a5d98eacaf9cf140a84508a37ca6bf642830f1600e20"
    )
    assert _resource_hash(dev.spec.services["worker"]) == (
        "f1c9111d4557a21353c16c051bc23bf668b2bd3ab927b4f6b64dd060758c28d7"
    )
    assert manifest_hash(prod) == (
        "7a401b202d301305d0ad88a3589772ac05eb2329487aad7585d02b712e2da7a5"
    )
    assert stack_production.spec_hash(prod) == (
        "576e04b45bb8f8c8674d0edee6ccac0da82cc9e3b244fc836e7ec39f1860711e"
    )
    assert _resource_hash(prod.spec.services["gateway"]) == (
        "b5a0d064f8eb45fcdb163d75b7d24a3410bcaed8cb42941d368b636d51737593"
    )
