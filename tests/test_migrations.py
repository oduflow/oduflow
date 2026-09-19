import json
import os
from unittest.mock import MagicMock

import pytest

from oduflow.errors import PrerequisiteNotMetError
from oduflow.migrations import MIGRATIONS, Migration, run_pending
from oduflow.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(base_data_dir=str(tmp_path))


def _mig(mig_id: str, calls: list[str], fail: bool = False) -> Migration:
    def apply(settings: Settings) -> None:
        if fail:
            raise RuntimeError("boom")
        calls.append(mig_id)

    return Migration(id=mig_id, description=f"test step {mig_id}", apply=apply)


def _state(tmp_path) -> list[str]:
    with open(os.path.join(str(tmp_path), "migrations.json")) as f:
        return json.load(f)["applied"]


def test_fresh_install_stamps_without_running(tmp_path):
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0002-b", calls)]

    ran = run_pending(_settings(tmp_path), registry)

    assert ran == []
    assert calls == []
    assert _state(tmp_path) == ["0001-a", "0002-b"]

    # Second start: still a no-op.
    assert run_pending(_settings(tmp_path), registry) == []
    assert calls == []


def test_legacy_install_runs_all_in_order(tmp_path):
    # A pre-migrations-era install is recognized by existing team data.
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0002-b", calls)]

    ran = run_pending(_settings(tmp_path), registry)

    assert ran == ["0001-a", "0002-b"]
    assert calls == ["0001-a", "0002-b"]
    assert _state(tmp_path) == ["0001-a", "0002-b"]


def test_only_pending_steps_run(tmp_path):
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [_mig("0001-a", calls)]
    run_pending(_settings(tmp_path), registry)
    calls.clear()

    # A new release appends a step: only it runs.
    registry.append(_mig("0002-b", calls))
    ran = run_pending(_settings(tmp_path), registry)

    assert ran == ["0002-b"]
    assert calls == ["0002-b"]
    assert _state(tmp_path) == ["0001-a", "0002-b"]


def test_failure_aborts_and_resumes_at_failed_step(tmp_path):
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [
        _mig("0001-a", calls),
        _mig("0002-b", calls, fail=True),
        _mig("0003-c", calls),
    ]

    with pytest.raises(PrerequisiteNotMetError, match="0002-b"):
        run_pending(_settings(tmp_path), registry)

    # The successful step is recorded; the failed one and its successor are not.
    assert calls == ["0001-a"]
    assert _state(tmp_path) == ["0001-a"]

    # Next start (cause fixed): resumes at the failed step, skips 0001-a.
    fixed = [_mig("0001-a", calls), _mig("0002-b", calls), _mig("0003-c", calls)]
    ran = run_pending(_settings(tmp_path), fixed)

    assert ran == ["0002-b", "0003-c"]
    assert calls == ["0001-a", "0002-b", "0003-c"]
    assert _state(tmp_path) == ["0001-a", "0002-b", "0003-c"]


def test_duplicate_ids_rejected(tmp_path):
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0001-a", calls)]

    with pytest.raises(ValueError, match="Duplicate migration ids"):
        run_pending(_settings(tmp_path), registry)


def test_default_registry_ids_unique_and_sorted():
    # Registry order is execution order; keeping ids sorted keeps the file
    # readable and reviewable as it grows.
    ids = [mig.id for mig in MIGRATIONS]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


class _FakeContainer:
    def __init__(self, name: str, labels: dict[str, str]):
        self.name = name
        self.labels = labels
        self.renamed_to: str | None = None

    def rename(self, new_name: str) -> None:
        self.renamed_to = new_name
        self.name = new_name


class _FakeClient:
    def __init__(self, containers: list[_FakeContainer]):
        self._containers = containers

        class _Containers:
            def __init__(self, outer):
                self._outer = outer

            def list(self, all=True, filters=None):
                wanted = filters["label"] if filters else []
                result = []
                for c in self._outer._containers:
                    ok = True
                    for cond in wanted:
                        key, _, value = cond.partition("=")
                        if c.labels.get(key) != value:
                            ok = False
                            break
                    if ok:
                        result.append(c)
                return result

        self.containers = _Containers(self)


class TestTeamScopedNamesMigration:
    def _run(self, monkeypatch, containers, teams=("1",)):
        from oduflow.migrations import _migrate_team_scoped_names
        from oduflow.settings import TeamSettings

        client = _FakeClient(containers)
        monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)
        settings = Settings(teams={t: TeamSettings(team_id=t) for t in teams})
        _migrate_team_scoped_names(settings)

    def test_renames_env_and_service_containers(self, monkeypatch):
        env = _FakeContainer(
            "oduflow-feature-x-odoo",
            {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.branch": "feature/x",
            },
        )
        svc = _FakeContainer(
            "oduflow-svc-redis",
            {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.service": "redis",
            },
        )
        self._run(monkeypatch, [env, svc])

        assert env.name == "oduflow-1-feature-x-odoo"
        assert svc.name == "oduflow-1-svc-redis"

    def test_idempotent_on_rerun(self, monkeypatch):
        env = _FakeContainer(
            "oduflow-1-main-odoo",
            {"oduflow.managed": "true", "oduflow.team": "1", "oduflow.branch": "main"},
        )
        svc = _FakeContainer(
            "oduflow-1-svc-redis",
            {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.service": "redis",
            },
        )
        self._run(monkeypatch, [env, svc])

        assert env.renamed_to is None
        assert svc.renamed_to is None

    def test_unmanaged_and_foreign_containers_untouched(self, monkeypatch):
        foreign = _FakeContainer("someone-elses-app", {})
        other_team = _FakeContainer(
            "oduflow-main-odoo",
            {"oduflow.managed": "true", "oduflow.team": "2", "oduflow.branch": "main"},
        )
        self._run(monkeypatch, [foreign, other_team], teams=("1",))

        assert foreign.renamed_to is None
        assert other_team.renamed_to is None


class TestResourceLimitsMigration:
    def test_updates_env_containers_only(self, monkeypatch):
        from unittest.mock import MagicMock

        from oduflow.migrations import _migrate_env_resource_limits
        from oduflow.settings import TeamSettings

        env = MagicMock()
        env.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.branch": "main",
        }
        svc = MagicMock()
        svc.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.service": "redis",
        }
        client = MagicMock()
        client.containers.list.return_value = [env, svc]
        monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)
        monkeypatch.setattr(
            "oduflow.docker_ops.stats.default_env_limits",
            lambda: {"mem_limit": 2**31, "pids_limit": 4096},
        )

        settings = Settings(teams={"1": TeamSettings(team_id="1")})
        _migrate_env_resource_limits(settings)

        env.update.assert_called_once_with(mem_limit=2**31, pids_limit=4096)
        svc.update.assert_not_called()

    def test_update_failure_is_logged_not_fatal(self, monkeypatch):
        from unittest.mock import MagicMock

        from oduflow.migrations import _migrate_env_resource_limits
        from oduflow.settings import TeamSettings

        env = MagicMock()
        env.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.branch": "main",
        }
        env.update.side_effect = RuntimeError("kernel says no")
        client = MagicMock()
        client.containers.list.return_value = [env]
        monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)

        _migrate_env_resource_limits(Settings(teams={"1": TeamSettings(team_id="1")}))


class TestTraefikYmlConfigMigration:
    """Recreate Traefik when it still mounts the rejected ``.json`` config.

    ``_ensure_traefik`` never rewrites an existing container's args, so a
    stale container would keep serving with a dynamic config Traefik refuses
    to load. The migration removes it; system init recreates it.
    """

    _OLD = "--providers.file.filename=/etc/traefik/dynamic/oduflow.json"
    _NEW = "--providers.file.filename=/etc/traefik/dynamic/oduflow.yml"

    def _run(self, monkeypatch, container, routing_mode="traefik"):
        import docker as _docker
        from oduflow.migrations import _migrate_traefik_yml_config

        client = MagicMock()
        if container is None:
            client.containers.get.side_effect = _docker.errors.NotFound("nope")
        else:
            client.containers.get.return_value = container
        monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)
        settings = Settings(routing_mode=routing_mode)
        _migrate_traefik_yml_config(settings)
        return client

    def _container(self, cmd):
        container = MagicMock()
        container.attrs = {"Config": {"Cmd": cmd}}
        return container

    def test_stale_json_container_is_removed(self, monkeypatch):
        container = self._container(["traefik", self._OLD])

        self._run(monkeypatch, container)

        container.stop.assert_called_once()
        container.remove.assert_called_once()

    def test_already_migrated_container_is_left_alone(self, monkeypatch):
        container = self._container(["traefik", self._NEW])

        self._run(monkeypatch, container)

        container.stop.assert_not_called()
        container.remove.assert_not_called()

    def test_container_without_a_cmd_is_left_alone(self, monkeypatch):
        container = MagicMock()
        container.attrs = {}

        self._run(monkeypatch, container)

        container.remove.assert_not_called()

    def test_null_cmd_is_left_alone(self, monkeypatch):
        # Docker reports Cmd as null, not [], for some images.
        container = self._container(None)

        self._run(monkeypatch, container)

        container.remove.assert_not_called()

    def test_port_mode_skips_docker_entirely(self, monkeypatch):
        container = self._container(["traefik", self._OLD])

        client = self._run(monkeypatch, container, routing_mode="port")

        client.containers.get.assert_not_called()
        container.remove.assert_not_called()

    def test_absent_traefik_is_not_an_error(self, monkeypatch):
        self._run(monkeypatch, None)  # must not raise


class TestServicePresetsPermissions:
    def _settings(self, tmp_path):
        from oduflow.settings import TeamSettings

        team_dir = tmp_path / "team_1"
        os.makedirs(team_dir, exist_ok=True)
        team = TeamSettings(team_id="1", data_dir=str(team_dir))
        return Settings(base_data_dir=str(tmp_path), teams={"1": team}), team_dir

    def test_restricts_existing_presets_file(self, tmp_path):
        from oduflow.migrations import _migrate_service_presets_permissions

        settings, team_dir = self._settings(tmp_path)
        presets = team_dir / "service_presets.json"
        presets.write_text("{}")
        os.chmod(presets, 0o644)

        _migrate_service_presets_permissions(settings)
        assert oct(os.stat(presets).st_mode & 0o777) == "0o600"

        # Idempotent on rerun and with no file at all.
        _migrate_service_presets_permissions(settings)
        os.remove(presets)
        _migrate_service_presets_permissions(settings)

    def test_unchmodable_file_is_logged_not_fatal(self, tmp_path):
        """Permission hardening must not block startup: a file the server user
        cannot chmod (e.g. root-owned after a backup restore) is only warned
        about — this migration re-runs on every failed start otherwise."""
        from unittest.mock import patch

        from oduflow.migrations import _migrate_service_presets_permissions

        settings, team_dir = self._settings(tmp_path)
        presets = team_dir / "service_presets.json"
        presets.write_text("{}")

        with patch("os.chmod", side_effect=PermissionError("not the owner")):
            _migrate_service_presets_permissions(settings)


class TestTemplateMetadataPermissions:
    def test_restricts_metadata_including_nested_templates(self, tmp_path):
        from oduflow.migrations import _migrate_template_metadata_permissions
        from oduflow.settings import TeamSettings

        team_dir = tmp_path / "team_1"
        team = TeamSettings(team_id="1", data_dir=str(team_dir))
        settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
        for name in ("base", "customer/prod"):
            tpl_dir = team_dir / "templates" / name
            os.makedirs(tpl_dir, exist_ok=True)
            meta = tpl_dir / "metadata.json"
            meta.write_text("{}")
            os.chmod(meta, 0o644)

        _migrate_template_metadata_permissions(settings)

        for name in ("base", "customer/prod"):
            meta = team_dir / "templates" / name / "metadata.json"
            assert oct(os.stat(meta).st_mode & 0o777) == "0o600"

        # Idempotent on rerun and with no templates dir at all.
        _migrate_template_metadata_permissions(settings)
        empty = Settings(
            base_data_dir=str(tmp_path),
            teams={"2": TeamSettings(team_id="2", data_dir=str(tmp_path / "team_2"))},
        )
        _migrate_template_metadata_permissions(empty)


class TestBackfillServicePresetsMigration:
    def test_backfills_each_labelled_service(self, monkeypatch):
        from unittest.mock import patch

        from oduflow.migrations import _migrate_backfill_service_presets
        from oduflow.settings import TeamSettings

        svc = MagicMock()
        svc.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.service": "redis",
        }
        unlabeled = MagicMock()
        unlabeled.labels = {}
        client = MagicMock()
        client.containers.list.return_value = [svc, unlabeled]
        monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: client)
        team = TeamSettings(team_id="1")
        settings = Settings(teams={"1": team})

        with patch(
            "oduflow.docker_ops.service_ops.backfill_service_preset"
        ) as backfill:
            _migrate_backfill_service_presets(settings)

        # One call per service-labelled container; the unlabeled one is skipped.
        backfill.assert_called_once_with(settings, team, "redis", svc)
        filters = client.containers.list.call_args.kwargs["filters"]
        assert "oduflow.service" in filters["label"]
