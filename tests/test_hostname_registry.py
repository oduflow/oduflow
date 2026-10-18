from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import env_ops
from oduflow.errors import ConflictError, FlowError
from oduflow.hostname_registry import (
    CAPACITY_RESERVATION,
    allocate_hostname,
    clear_hostname_assignment,
    get_hostname,
    release_hostname,
    reserve_environment_slot,
)
from oduflow.settings import Settings, TeamSettings


def _path(tmp_path):
    return str(tmp_path / "hostnames.json")


def test_automatic_slots_are_stable_and_reusable(tmp_path):
    path = _path(tmp_path)

    assert allocate_hostname(path, "feature-a", 2, hostname_prefix="dev") == "dev1"
    assert allocate_hostname(path, "feature-b", 2, hostname_prefix="dev") == "dev2"
    assert allocate_hostname(path, "feature-a", 2, hostname_prefix="dev") == "dev1"

    release_hostname(path, "feature-a")
    assert get_hostname(path, "feature-a") is None
    assert allocate_hostname(path, "feature-c", 2, hostname_prefix="dev") == "dev1"


def test_explicit_hostname_consumes_capacity_and_cannot_collide(tmp_path):
    path = _path(tmp_path)

    assert (
        allocate_hostname(
            path,
            "feature-a",
            2,
            requested_hostname="preview",
            hostname_prefix="dev",
        )
        == "preview"
    )
    with pytest.raises(ConflictError, match="already used"):
        allocate_hostname(
            path,
            "feature-b",
            2,
            requested_hostname="preview",
            hostname_prefix="dev",
        )

    assert allocate_hostname(path, "feature-b", 2, hostname_prefix="dev") == "dev1"
    with pytest.raises(FlowError, match="No free environment slots"):
        allocate_hostname(path, "feature-c", 2, hostname_prefix="dev")


def test_active_legacy_environments_count_toward_capacity(tmp_path):
    with pytest.raises(FlowError, match="configured: 2"):
        allocate_hostname(
            _path(tmp_path),
            "feature-c",
            2,
            active_envs={"feature-a", "feature-b"},
            used_hostnames={"feature-a", "feature-b"},
            hostname_prefix="dev",
        )


def test_parallel_allocations_never_share_a_slot(tmp_path):
    path = _path(tmp_path)
    envs = [f"feature-{number}" for number in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        hostnames = list(
            pool.map(
                lambda env: allocate_hostname(
                    path, env, len(envs), hostname_prefix="dev"
                ),
                envs,
            )
        )

    assert len(set(hostnames)) == len(envs)
    assert set(hostnames) == {f"dev{number}" for number in range(1, 21)}


def test_capacity_reservations_are_concurrency_safe_without_hostname_changes(tmp_path):
    path = _path(tmp_path)
    envs = [f"feature-{number}" for number in range(10)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda env: reserve_environment_slot(path, env, len(envs)),
                envs,
            )
        )

    assert {get_hostname(path, env) for env in envs} == {CAPACITY_RESERVATION}
    with pytest.raises(FlowError, match="No free environment slots"):
        reserve_environment_slot(path, "overflow", len(envs))


def test_hostname_assignment_can_return_to_capacity_only(tmp_path):
    path = _path(tmp_path)
    reserve_environment_slot(path, "feature-a", 2)
    assert allocate_hostname(path, "feature-a", 2, hostname_prefix="dev") == "dev1"

    clear_hostname_assignment(path, "feature-a", retain_slot=True)

    assert get_hostname(path, "feature-a") == CAPACITY_RESERVATION


def _traefik_settings(
    tmp_path, slots=2, hostname_mode="slots", hostname="dev.example.com"
):
    data_dir = tmp_path / "team_1"
    team = TeamSettings(
        team_id="1",
        hostname=hostname,
        data_dir=str(data_dir),
        hostname_registry_path=str(data_dir / "hostnames.json"),
        environment_slots=slots,
        environment_hostname_mode=hostname_mode,
    )
    return Settings(
        routing_mode="traefik",
        routing_tls=False,
        teams={"1": team},
    ), team


def test_create_wrapper_assigns_short_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://dev1.example.com"},
        ) as provision,
    ):
        result = env_ops.create_environment(
            settings, team, "feature-a", "repo", "odoo:19.0"
        )

    assert result["url"] == "https://dev1.example.com"
    assert provision.call_args.kwargs["hostname"] == "dev1"
    assert provision.call_args.kwargs["hostname_source"] == "slot"
    assert get_hostname(team.hostname_registry_path, "feature-a") == "dev1"
    # Capacity and hostname usage come from one container listing.
    assert client.containers.list.call_count == 1


def test_create_wrapper_accepts_explicit_parent_domain_prefix(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://qa.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(
            settings,
            team,
            "feature-a",
            "repo",
            "odoo:19.0",
            hostname="qa",
        )

    assert provision.call_args.kwargs["hostname"] == "qa"
    assert provision.call_args.kwargs["hostname_source"] == "custom"
    assert get_hostname(team.hostname_registry_path, "feature-a") == "qa"


def test_create_wrapper_accepts_explicit_hostname_on_bare_team_domain(tmp_path):
    # example.com has no prefix to split off; an explicit name needs none.
    settings, team = _traefik_settings(
        tmp_path, slots=0, hostname_mode="branch", hostname="example.com"
    )
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://qa.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(
            settings, team, "feature-a", "repo", "odoo:19.0", hostname="qa"
        )

    assert provision.call_args.kwargs["hostname"] == "qa"
    assert get_hostname(team.hostname_registry_path, "feature-a") == "qa"


def test_internal_recreate_preserves_slot_hostname_source(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://dev1.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(
            settings,
            team,
            "feature-a",
            "repo",
            "odoo:19.0",
            hostname="dev1",
            hostname_source="slot",
        )

    assert provision.call_args.kwargs["hostname_source"] == "slot"


def test_automatic_allocation_skips_hostname_used_by_service(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    service = MagicMock()
    service.labels = {
        "oduflow.managed": "true",
        "oduflow.team": "1",
        "traefik.http.routers.oduflow-1-service-dev1.rule": (
            "Host(`dev1.example.com`)"
        ),
    }
    client.containers.list.return_value = [service]

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://dev2.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(settings, team, "feature-a", "repo", "odoo:19.0")

    assert provision.call_args.kwargs["hostname"] == "dev2"


def test_failed_create_releases_hostname_before_container_exists(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        env_ops.create_environment(settings, team, "feature-a", "repo", "odoo:19.0")

    assert get_hostname(team.hostname_registry_path, "feature-a") is None


def test_branch_mode_keeps_legacy_hostname_while_enforcing_capacity(tmp_path):
    settings, team = _traefik_settings(tmp_path, slots=1, hostname_mode="branch")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://feature-a.dev.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(settings, team, "feature-a", "repo", "odoo:19.0")

    assert provision.call_args.kwargs["hostname"] == ""
    assert get_hostname(team.hostname_registry_path, "feature-a") == (
        CAPACITY_RESERVATION
    )

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        pytest.raises(FlowError, match="No free environment slots"),
    ):
        env_ops.create_environment(settings, team, "feature-b", "repo", "odoo:19.0")


def test_port_mode_enforces_environment_capacity(tmp_path):
    data_dir = tmp_path / "team_1"
    team = TeamSettings(
        team_id="1",
        data_dir=str(data_dir),
        hostname_registry_path=str(data_dir / "hostnames.json"),
        environment_slots=1,
    )
    settings = Settings(routing_mode="port", teams={"1": team})
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    existing = MagicMock()
    existing.labels = {
        settings.managed_label: "true",
        settings.team_label: "1",
        settings.branch_label: "feature-a",
    }
    client.containers.list.return_value = [existing]

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        pytest.raises(FlowError, match="No free environment slots"),
    ):
        env_ops.create_environment(settings, team, "feature-b", "repo", "odoo:19.0")


def test_update_keeps_unlabeled_legacy_hostname_in_branch_mode(tmp_path):
    settings, team = _traefik_settings(tmp_path, hostname_mode="branch")
    labels = {}

    clear_after_update = env_ops._reconcile_environment_hostname_for_update(
        MagicMock(), settings, team, "feature-a", labels
    )

    assert clear_after_update is False
    assert env_ops.ENV_HOSTNAME_LABEL not in labels
    assert env_ops.ENV_HOSTNAME_SOURCE_LABEL not in labels


def test_update_clears_stale_registry_assignment_for_unlabeled_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path, hostname_mode="branch")
    reserve_environment_slot(team.hostname_registry_path, "feature-a", 2)
    allocate_hostname(
        team.hostname_registry_path, "feature-a", 2, hostname_prefix="dev"
    )
    labels = {}

    clear_after_update = env_ops._reconcile_environment_hostname_for_update(
        MagicMock(), settings, team, "feature-a", labels
    )

    assert clear_after_update is True


def test_update_returns_legacy_automatic_slot_to_branch_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path, hostname_mode="branch")
    reserve_environment_slot(team.hostname_registry_path, "feature-a", 2)
    allocate_hostname(
        team.hostname_registry_path, "feature-a", 2, hostname_prefix="dev"
    )
    labels = {env_ops.ENV_HOSTNAME_LABEL: "dev1"}

    clear_after_update = env_ops._reconcile_environment_hostname_for_update(
        MagicMock(), settings, team, "feature-a", labels
    )

    assert clear_after_update is True
    assert env_ops.ENV_HOSTNAME_LABEL not in labels
    assert env_ops.ENV_HOSTNAME_SOURCE_LABEL not in labels


def test_update_preserves_legacy_custom_hostname_in_branch_mode(tmp_path):
    settings, team = _traefik_settings(tmp_path, hostname_mode="branch")
    labels = {env_ops.ENV_HOSTNAME_LABEL: "qa"}

    clear_after_update = env_ops._reconcile_environment_hostname_for_update(
        MagicMock(), settings, team, "feature-a", labels
    )

    assert clear_after_update is False
    assert labels[env_ops.ENV_HOSTNAME_LABEL] == "qa"
    assert labels[env_ops.ENV_HOSTNAME_SOURCE_LABEL] == "custom"


def test_update_assigns_slot_only_when_slot_mode_is_explicit(tmp_path):
    settings, team = _traefik_settings(tmp_path, hostname_mode="slots")
    client = MagicMock()
    client.containers.list.return_value = []
    labels = {}

    clear_after_update = env_ops._reconcile_environment_hostname_for_update(
        client, settings, team, "feature-a", labels
    )

    assert clear_after_update is False
    assert labels[env_ops.ENV_HOSTNAME_LABEL] == "dev1"
    assert labels[env_ops.ENV_HOSTNAME_SOURCE_LABEL] == "slot"


def test_base_url_reads_persisted_short_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    container = MagicMock()
    container.labels = {env_ops.ENV_HOSTNAME_LABEL: "dev2"}

    assert env_ops.get_env_base_url(settings, team, "feature-a", container) == (
        "https://dev2.example.com",
        "dev2.example.com",
    )
