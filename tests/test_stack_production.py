"""Production Stack contracts, using real registry writes and isolated runtime calls."""

from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from oduflow import production_registry, stack_production
from oduflow.errors import BusyError, ConflictError
from oduflow.locking import LockManager, prod_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import resolve_env_values
from oduflow.stack_models import StackManifest, ValueFrom
from oduflow.stack_ops import apply_stack, build_plan, stack_status
from oduflow.stack_production import runtime_drift as inspect_runtime


@pytest.fixture
def setup(tmp_path):
    team = TeamSettings(
        team_id="1", data_dir=str(tmp_path / "team"), hostname="ops.example.org"
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        routing_mode="traefik",
        prod_enabled=True,
    )
    manifest = StackManifest.model_validate(
        {
            "metadata": {"name": "control"},
            "spec": {
                "production": {
                    "name": "control",
                    "domain": "example.org",
                    "repoUrl": "https://github.com/acme/app.git",
                    "branch": "production",
                    "odooImage": "odoo:19.0",
                    "env": {"MODE": "production"},
                }
            },
        }
    )
    with ExitStack() as patches:
        patches.enter_context(patch("oduflow.stack_ops.git_ops.validate_repo_url"))
        patches.enter_context(
            patch("oduflow.extra_addons.list_extra_repos", return_value=[])
        )
        patches.enter_context(
            patch("oduflow.stack_ops.volume_ops.list_volumes", return_value=[])
        )
        patches.enter_context(
            patch("oduflow.stack_ops.service_ops.list_services", return_value=[])
        )
        patches.enter_context(
            patch("oduflow.stack_production.production_ops._assert_domain_free")
        )
        patches.enter_context(
            patch("oduflow.stack_production.runtime_drift", return_value=[])
        )
        yield settings, team, manifest, str(tmp_path / "stack.json")


def register(setup, *, owned=False, **changes):
    settings, team, manifest, _ = setup
    record = stack_production.desired_record(settings, team, manifest, None)
    if owned:
        record["meta"] = {
            "unrelated": "retained",
            "stack": {
                "name": manifest.metadata.name,
                "resource": "production",
                "template": None,
                "appliedHash": stack_production.spec_hash(manifest),
            },
        }
    record.update(changes)
    return production_registry.create_production(team, "control", record)


def test_production_schema_requires_exactly_one_target(setup):
    _, _, manifest, _ = setup
    raw = manifest.model_dump()
    raw["spec"]["production"] = None
    with pytest.raises(ValidationError, match="exactly one"):
        StackManifest.model_validate(raw)
    raw["spec"]["production"] = manifest.spec.production.model_dump()
    raw["spec"]["environment"] = {
        "name": "dev",
        "repo_url": "https://github.com/acme/app",
        "branch": "main",
        "odoo_image": "odoo:19",
    }
    with pytest.raises(ValidationError, match="exactly one"):
        StackManifest.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("domain", "https://example.org"),
        ("name", "../../other"),
        ("odooConf", {"db_password": "secret"}),
        ("odooConf", {"workers": "2\ndb_host = evil"}),
        ("env", {"SELF": {"productionField": "url"}}),
        ("env", {"DB": {"database": "x", "databaseField": "url"}}),
    ],
)
def test_invalid_production_fields_are_rejected(setup, field, value):
    raw = setup[2].model_dump(by_alias=True)
    raw["spec"]["production"][field] = value
    with pytest.raises(ValidationError):
        StackManifest.model_validate(raw)


def test_production_cannot_use_development_token_reference(setup):
    raw = setup[2].model_dump(by_alias=True)
    raw["spec"]["services"] = {
        "consumer": {
            "image": "app:1",
            "port": 8080,
            "env": {"TOKEN": {"environmentField": "token"}},
        }
    }
    with pytest.raises(ValidationError, match="environmentField requires"):
        StackManifest.model_validate(raw)


def test_production_disabled_refuses_all_mutation(setup):
    settings, team, manifest, path = setup
    settings = replace(settings, prod_enabled=False)
    with patch("oduflow.stack_production.production_ops.create_production") as create:
        with pytest.raises(ConflictError, match="production.enabled"):
            apply_stack(settings, team, manifest, path)
        create.assert_not_called()


def test_unowned_production_requires_explicit_adoption(setup):
    register(setup)
    assert build_plan(*setup).has_conflicts


def test_adopt_existing_does_not_create_when_registry_missing(setup):
    setup[2].spec.production.adopt_existing = True
    assert build_plan(*setup).has_conflicts


@pytest.mark.parametrize(
    "changes",
    [
        {"domain": "other.example.org"},
        {"env_vars": {"MODE": "wrong"}},
        {"auto_update": True},
        {"odoo_conf": {"workers": "3"}},
        {"meta": {"stack": {"name": "another", "resource": "production"}}},
        {"deploy_in_progress": True},
        {"unhealthy": True},
    ],
)
def test_adoption_refuses_drift_and_foreign_ownership(setup, changes):
    register(setup, **changes)
    setup[2].spec.production.adopt_existing = True
    with pytest.raises(ConflictError):
        apply_stack(*setup)
    assert (
        not production_registry.get_production(setup[1], "control")
        .get("meta", {})
        .get("stack", {})
        .get("appliedHash")
    )


@pytest.mark.parametrize(
    "drift",
    [
        ["container missing"],
        ["container stopped"],
        ["container ownership"],
        ["container env"],
    ],
)
def test_adoption_checks_runtime_not_just_registry(setup, drift):
    register(setup)
    setup[2].spec.production.adopt_existing = True
    with patch("oduflow.stack_production.runtime_drift", return_value=drift):
        assert build_plan(*setup).has_conflicts


def test_adoption_and_reapply_preserve_runtime_and_metadata(setup):
    register(setup, meta={"unrelated": "kept"})
    settings, team, manifest, path = setup
    manifest.spec.production.adopt_existing = True
    with (
        patch(
            "oduflow.stack_production.production_ops.reconfigure_production"
        ) as update,
        patch("oduflow.stack_ops.env_ops.create_environment") as dev,
    ):
        assert apply_stack(*setup).actions[0].operation == "adopt"
        assert not apply_stack(*setup).actions
        manifest.spec.production.adopt_existing = False
        assert not build_plan(*setup).actions
        update.assert_not_called()
        dev.assert_not_called()
    assert (
        production_registry.get_production(team, "control")["meta"]["unrelated"]
        == "kept"
    )
    status = stack_status(*setup)
    assert status["inSync"]
    assert status["state"]["resources"]["production"] == "control"
    assert "environment" not in status["state"]["resources"]


def test_creation_uses_production_lifecycle_and_reapply_is_noop(setup):
    settings, team, manifest, _ = setup

    def create(_settings, _team, name, **kwargs):
        metadata = kwargs.pop("stack_metadata")
        kwargs.pop("template_name")
        return production_registry.create_production(
            team, name, {**kwargs, "meta": {"stack": metadata}}
        )

    with (
        patch(
            "oduflow.stack_production.production_ops.create_production",
            side_effect=create,
        ) as production,
        patch("oduflow.stack_ops.env_ops.create_environment") as dev,
        patch("oduflow.stack_production.get_client"),
        patch(
            "oduflow.stack_production.production_ops.wait_production_healthy",
            return_value=True,
        ),
    ):
        assert apply_stack(*setup).actions[0].operation == "create"
        assert not apply_stack(*setup).actions
        assert production.call_count == 1
        dev.assert_not_called()


def test_failed_reconfiguration_leaves_pending_state_and_retries(setup):
    register(setup, owned=True)
    settings, team, manifest, _ = setup
    manifest.spec.production.env = {"MODE": "updated"}

    def fail(_settings, _team, name, **kwargs):
        production_registry.update_production(team, name, kwargs)
        raise RuntimeError("container create failed")

    with patch(
        "oduflow.stack_production.production_ops.reconfigure_production",
        side_effect=fail,
    ):
        with pytest.raises(RuntimeError, match="container create failed"):
            apply_stack(*setup)
    record = production_registry.get_production(team, "control")
    assert record["env_vars"] == {"MODE": "updated"}
    assert record["meta"]["stack"]["appliedHash"] == ""
    assert build_plan(*setup).has_changes
    with patch(
        "oduflow.stack_production.production_ops.reconfigure_production",
        return_value={"healthy": True},
    ) as retry:
        apply_stack(*setup)
        retry.assert_called_once()
    assert not build_plan(*setup).actions


def test_auto_update_toggle_does_not_restart_production(setup):
    register(setup, owned=True)
    setup[2].spec.production.auto_update = True
    with patch(
        "oduflow.stack_production.production_ops.reconfigure_production"
    ) as reconfigure:
        apply_stack(*setup)
        reconfigure.assert_not_called()
    assert (
        production_registry.get_production(setup[1], "control")["auto_update"] is True
    )
    assert not build_plan(*setup).actions


def test_failed_health_does_not_mark_success(setup):
    register(setup, owned=True)
    setup[2].spec.production.odoo_image = "odoo:19.1"
    with patch(
        "oduflow.stack_production.production_ops.reconfigure_production",
        return_value={"healthy": False},
    ):
        with pytest.raises(ConflictError, match="healthy"):
            apply_stack(*setup)
    assert (
        production_registry.get_production(setup[1], "control")["meta"]["stack"][
            "appliedHash"
        ]
        == ""
    )


def test_source_change_requires_explicit_deploy_workflow(setup):
    register(setup, owned=True)
    setup[2].spec.production.branch = "another"
    assert build_plan(*setup).has_conflicts


def test_production_lock_conflict_releases_team_lock(setup):
    settings, team, manifest, _ = setup
    locks = LockManager()
    key = prod_lock_key(team.team_id, "control")
    locks.acquire_env(key, operation="deploy")
    with pytest.raises(BusyError):
        apply_stack(*setup, lock_manager=locks)
    locks.acquire_team(team.team_id)
    locks.release_team(team.team_id)
    locks.release_env(key)


def test_production_references_use_registry_identity(setup):
    register(setup)
    settings, team, _, _ = setup
    values = {
        key: ValueFrom(productionField=key)
        for key in ["url", "containerName", "database"]
    }
    assert resolve_env_values(
        values, settings=settings, team=team, production_name="control"
    ) == {
        "url": "https://example.org",
        "containerName": "oduflow-1-prod-control-odoo",
        "database": "oduflow_1_prod-control",
    }


def test_configuration_replacement_removes_omitted_overrides(setup):
    register(setup, owned=True, odoo_conf={"workers": "4", "limit_time_real": "90"})
    setup[2].spec.production.odoo_conf = {"workers": "2"}
    with (
        patch(
            "oduflow.stack_production.production_ops.set_production_odoo_conf"
        ) as conf,
        patch(
            "oduflow.stack_production.production_ops.reconfigure_production",
            return_value={"healthy": True},
        ),
        patch("oduflow.stack_production.get_client"),
        patch(
            "oduflow.stack_production.production_ops.wait_production_healthy",
            return_value=True,
        ),
    ):
        apply_stack(*setup)
    assert conf.call_args.kwargs == {
        "set_options": {"workers": "2"},
        "unset_options": ["limit_time_real"],
        "restart": False,
    }


def test_missing_secret_fails_before_production_creation(setup):
    setup[2].spec.production.env = {"API_TOKEN": "secret:does-not-exist"}
    with patch("oduflow.stack_production.production_ops.create_production") as create:
        with pytest.raises(Exception, match="does-not-exist"):
            apply_stack(*setup)
        create.assert_not_called()


def test_production_field_is_deferred_until_creation(setup):
    from oduflow.stack_models import Service

    settings, team, manifest, _ = setup
    manifest.spec.services["consumer"] = Service(
        image="app:1", port=8080, env={"ORIGIN": ValueFrom(productionField="url")}
    )
    actions = build_plan(*setup).actions
    assert [(a.operation, a.resource) for a in actions] == [
        ("create", "production"),
        ("create", "services.consumer"),
    ]


def test_runtime_inspection_detects_effective_drift(setup):
    # Exercise the actual inspector, rather than the orchestration fixture's stub.
    from unittest.mock import Mock

    settings, team, _, _ = setup
    record = register(setup)
    labels = {
        settings.managed_label: "true",
        settings.team_label: "1",
        "oduflow.prod": "true",
        "oduflow.prod_name": "control",
        settings.repo_label: record["repo_url"],
        "oduflow.domain": "example.org",
        "oduflow.git_branch": "production",
        "oduflow.env_vars": '{"MODE":"production"}',
        "traefik.http.routers.oduflow-1-prod-control.rule": "Host(`example.org`)",
    }
    container = Mock(
        status="running",
        attrs={
            "Config": {
                "Image": "odoo:19.0",
                "Labels": labels,
                "Env": ["MODE=production", "HOST=oduflow-prod-db"],
            }
        },
    )
    with (
        patch("oduflow.stack_production.get_client"),
        patch(
            "oduflow.stack_production.production_ops._get_container",
            return_value=container,
        ),
    ):
        assert inspect_runtime(settings, team, record) == []
        container.attrs["Config"]["Env"] = ["MODE=wrong", "HOST=oduflow-db"]
        assert inspect_runtime(settings, team, record) == [
            "container database host",
            "container env",
        ]
        labels[settings.team_label] = "another"
        assert inspect_runtime(settings, team, record) == ["container ownership"]
