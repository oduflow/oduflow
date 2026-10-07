"""PG15 → PG16 cluster replacement: archive cleanup, restore resume, heartbeat."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from oduflow import postgres_migration
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings, TeamSettings


def _settings(tmp_path, *, backup: bool = True) -> Settings:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    return Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        backup=BackupSettings(bucket="b", access_key="k", secret_key="s")
        if backup
        else None,
    )


def _s3(pages, errors=None):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = pages
    client.delete_objects.return_value = {"Errors": errors} if errors else {}
    return client


class TestDeleteWalgArchive:
    def test_deletes_only_the_walg_prefix_page_by_page(self, tmp_path):
        settings = _settings(tmp_path)
        s3 = _s3(
            [
                {"Contents": [{"Key": "oduflow/walg/basebackups_005/a"}]},
                {},
                {"Contents": [{"Key": "oduflow/walg/wal_005/b"}]},
            ]
        )
        with patch.object(postgres_migration.s3_client, "make_client", return_value=s3):
            postgres_migration._delete_walg_archive(settings)

        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="b", Prefix="oduflow/walg/"
        )
        deleted = [
            call.kwargs["Delete"]["Objects"]
            for call in s3.delete_objects.call_args_list
        ]
        assert deleted == [
            [{"Key": "oduflow/walg/basebackups_005/a"}],
            [{"Key": "oduflow/walg/wal_005/b"}],
        ]

    def test_partial_failure_is_an_error(self, tmp_path):
        settings = _settings(tmp_path)
        s3 = _s3(
            [{"Contents": [{"Key": "oduflow/walg/x"}]}],
            errors=[{"Key": "oduflow/walg/x", "Code": "AccessDenied", "Message": "no"}],
        )
        with patch.object(postgres_migration.s3_client, "make_client", return_value=s3):
            with pytest.raises(PrerequisiteNotMetError, match="AccessDenied"):
                postgres_migration._delete_walg_archive(settings)


class TestRemoveOld:
    def _cluster(self, kind):
        return {
            "kind": kind,
            "container": "c",
            "volume": "v",
            "old_container_id": "old",
            "old_volume_created": "t",
            "image_id": "img",
        }

    def _client(self):
        client = MagicMock()
        client.containers.get.return_value.id = "old"
        client.volumes.get.return_value.attrs = {"CreatedAt": "t"}
        return client

    @pytest.mark.parametrize(
        ("kind", "backup", "expected"),
        [("prod", True, 1), ("prod", False, 0), ("dev", True, 0)],
    )
    def test_archive_deleted_only_for_backed_up_prod(
        self, tmp_path, kind, backup, expected
    ):
        settings = _settings(tmp_path, backup=backup)
        with (
            patch.object(postgres_migration, "_delete_walg_archive") as delete,
            patch.object(
                postgres_migration.system_ops,
                "_pg_tablespaces_host_dir",
                return_value=str(tmp_path / "missing"),
            ),
        ):
            postgres_migration._remove_old(
                self._client(), settings, self._cluster(kind)
            )
        assert delete.call_count == expected

    def test_s3_failure_leaves_the_stopped_cluster_intact(self, tmp_path):
        settings = _settings(tmp_path)
        client = self._client()
        with patch.object(
            postgres_migration,
            "_delete_walg_archive",
            side_effect=PrerequisiteNotMetError("AccessDenied"),
        ):
            with pytest.raises(PrerequisiteNotMetError):
                postgres_migration._remove_old(client, settings, self._cluster("prod"))
        client.containers.get.return_value.stop.assert_called_once()
        client.containers.get.return_value.remove.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()


def _journal(settings, templates):
    path = os.path.join(settings.base_data_dir, postgres_migration._JOURNAL)
    with open(path, "w") as f:
        json.dump({"clusters": [], "templates": templates, "restored": []}, f)
    return path


def _write_dump(team, name, filename="dump.pgdump"):
    tpl_dir = team.get_template_dir(name)
    os.makedirs(tpl_dir, exist_ok=True)
    with open(os.path.join(tpl_dir, filename), "wb") as f:
        f.write(b"PGDMP")


@pytest.fixture
def no_docker():
    with (
        patch.object(postgres_migration, "validate_configuration"),
        patch.object(postgres_migration, "get_client"),
    ):
        yield


@pytest.mark.usefixtures("no_docker")
class TestTemplateRestore:
    def test_removed_template_and_team_are_skipped(self, tmp_path):
        settings = _settings(tmp_path)
        team = settings.teams["1"]
        _write_dump(team, "kept", filename="dump.sql")
        path = _journal(
            settings,
            [
                {"team": "1", "name": "gone"},
                {"team": "1", "name": "kept"},
                {"team": "9", "name": "orphan"},
            ],
        )
        with patch.object(postgres_migration.system_ops, "reload_template") as reload:
            postgres_migration.migrate(settings)

        # The dump is resolved again, so a fixed file under another name is used.
        reload.assert_called_once()
        assert reload.call_args.kwargs["dump_path"] == os.path.join(
            team.get_template_dir("kept"), "dump.sql"
        )
        state = json.loads(open(path).read())
        assert state["restored"] == [["1", "kept"]]
        assert state["skipped"] == [["1", "gone"], ["9", "orphan"]]

    def test_restore_failure_explains_how_to_skip(self, tmp_path):
        settings = _settings(tmp_path)
        _write_dump(settings.teams["1"], "broken")
        path = _journal(settings, [{"team": "1", "name": "broken"}])
        with patch.object(
            postgres_migration.system_ops,
            "reload_template",
            side_effect=ExternalCommandError("pg_restore", 1, "bad archive"),
        ):
            with pytest.raises(
                PrerequisiteNotMetError,
                match="oduflow delete-template broken --team 1",
            ):
                postgres_migration.migrate(settings)
        assert json.loads(open(path).read())["restored"] == []


class TestRestoreHeartbeat:
    def test_beats_only_after_a_successful_probe(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        probed = threading.Event()
        calls = []

        def probe(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 1:
                raise RuntimeError("docker hiccup")
            if len(calls) == 3:
                # The second probe's beat was logged before this one started.
                probed.set()
            return "COPY public.mail_message"

        with (
            patch.object(postgres_migration, "_HEARTBEAT_SECONDS", 0.01),
            patch.object(postgres_migration, "get_client"),
            patch.object(postgres_migration.system_ops, "_exec_sql", side_effect=probe),
            caplog.at_level(logging.INFO, logger="oduflow"),
        ):
            with postgres_migration._restore_heartbeat(
                settings, "1/base", "oduflow_1_template_base"
            ):
                assert probed.wait(5)

        assert "datname = 'oduflow_1_template_base'" in calls[0]
        beats = [r for r in caplog.records if "Restoring template 1/base" in r.message]
        assert beats and "COPY public.mail_message" in beats[0].message


class TestStrictRestoreCommands:
    """Archives get --no-acl/--exit-on-error; plain SQL stays tolerant, because
    pg_dump/pg_restore 17 emit ``SET transaction_timeout`` that PG16 rejects."""

    @pytest.mark.parametrize("text_dump", [False, True])
    def test_strict_only_hardens_archive_restores(self, tmp_path, text_dump):
        settings = _settings(tmp_path)
        team = settings.teams["1"]
        _write_dump(team, "base")
        db_container = MagicMock()
        db_container.exec_run.return_value = (0, b"")
        client = MagicMock()
        client.containers.get.return_value = db_container
        system_ops = postgres_migration.system_ops

        with (
            patch.object(system_ops, "get_client", return_value=client),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "_exec_sql", return_value="5"),
            patch.object(system_ops, "_db_exists", return_value=False),
            patch.object(system_ops, "_is_text_dump", return_value=text_dump),
            patch.object(system_ops, "ensure_team_tablespace", return_value="ts"),
            patch.object(system_ops, "_update_template_sizes"),
            patch.object(system_ops, "_copy_file_to_container"),
        ):
            system_ops.reload_template(
                settings,
                team,
                "base",
                dump_path=team.get_template_sql_path("base"),
                persist_dump=False,
                strict=True,
            )

        cmds = [call.args[0] for call in db_container.exec_run.call_args_list]
        restore = next(cmd for cmd in cmds if cmd[0] in ("psql", "pg_restore"))
        if text_dump:
            assert restore[0] == "psql"
            assert "ON_ERROR_STOP=1" not in restore
        else:
            assert restore[0] == "pg_restore"
            assert {"--no-acl", "--exit-on-error"} <= set(restore)


class TestImageReferences:
    @pytest.mark.parametrize(
        ("image", "repository"),
        [
            ("postgres:15", "postgres"),
            ("library/postgres:15", "postgres"),
            ("docker.io/postgres:15", "postgres"),
            ("docker.io/library/postgres:15", "postgres"),
            ("index.docker.io/library/postgres@sha256:abc", "postgres"),
            ("oduist/oduflow-postgres:15-bookworm-1", "oduist/oduflow-postgres"),
            (
                "mirror.local:5000/library/postgres",
                "mirror.local:5000/library/postgres",
            ),
        ],
    )
    def test_repository_normalizes_docker_hub_forms(self, image, repository):
        assert postgres_migration._repository(image) == repository

    @pytest.mark.parametrize(
        ("image", "major"),
        [
            ("postgres:16", 16),
            ("docker.io/library/postgres:17.2-bookworm", 17),
            ("postgres:latest", None),
            # Other repositories put their own version first.
            ("pgvector/pgvector:0.8.0-pg16", None),
            ("timescale/timescaledb:2.17.2-pg16", None),
        ],
    )
    def test_tag_is_trusted_only_for_official_images(self, image, major):
        assert postgres_migration._image_major(image) == major


def _docker(images_present=True, containers=None):
    client = MagicMock()
    if not images_present:
        client.images.get.side_effect = [
            postgres_migration.docker.errors.ImageNotFound("missing"),
            MagicMock(id="sha256:new"),
        ]
    else:
        client.images.get.return_value = MagicMock(id="sha256:local")
    containers = containers or {}

    def get(name):
        if name not in containers:
            raise postgres_migration.docker.errors.NotFound(name)
        return containers[name]

    client.containers.get.side_effect = get
    return client


class TestValidateConfiguration:
    def _validate(self, tmp_path, image, client, data_majors=None):
        settings = replace(_settings(tmp_path), postgres_image=image)
        with (
            patch.object(postgres_migration, "get_client", return_value=client),
            patch.object(postgres_migration, "_binary_major", return_value=16),
            patch.object(
                postgres_migration,
                "_data_major",
                side_effect=lambda c: (data_majors or {})[c.name],
            ),
        ):
            postgres_migration.validate_configuration(settings)

    def test_extension_image_is_checked_by_binary(self, tmp_path):
        self._validate(tmp_path, "pgvector/pgvector:0.8.0-pg16", _docker())

    @pytest.mark.parametrize("image", ["postgres:15", "postgres:18"])
    def test_unsupported_majors_are_refused(self, tmp_path, image):
        with pytest.raises(
            PrerequisiteNotMetError, match="supports PostgreSQL 16 and 17"
        ):
            self._validate(tmp_path, image, _docker())

    def test_existing_cluster_must_keep_its_major(self, tmp_path):
        db = MagicMock()
        db.name = "oduflow-db"
        client = _docker(containers={"oduflow-db": db})
        with pytest.raises(PrerequisiteNotMetError, match="holds PostgreSQL 16 data"):
            self._validate(tmp_path, "postgres:17", client, {"oduflow-db": 16})

    def test_old_cluster_is_left_to_the_migration(self, tmp_path):
        db = MagicMock()
        db.name = "oduflow-db"
        client = _docker(containers={"oduflow-db": db})
        self._validate(tmp_path, "postgres:16", client, {"oduflow-db": 15})


class TestPullTarget:
    def test_local_image_is_used_without_pulling(self):
        client = _docker()
        with (
            patch.object(postgres_migration, "_download_image") as download,
            patch.object(postgres_migration, "_binary_major", return_value=16),
        ):
            assert postgres_migration._pull_target(client, "acme/pg16:latest") == (
                "sha256:local",
                16,
            )
        download.assert_not_called()

    def test_missing_image_is_pulled(self):
        client = _docker(images_present=False)
        with (
            patch.object(postgres_migration, "_download_image") as download,
            patch.object(postgres_migration, "_binary_major", return_value=17),
        ):
            assert postgres_migration._pull_target(client, "postgres:17") == (
                "sha256:new",
                17,
            )
        download.assert_called_once()


class TestTemplateDiscovery:
    def test_nested_names_optional_metadata_and_empty_dirs(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        team = settings.teams["1"]
        _write_dump(team, "base")  # no metadata.json
        _write_dump(team, "customer/prod")
        os.makedirs(team.get_template_dir("empty"))
        os.makedirs(team.get_template_dir("dumpless"))
        with open(team.get_template_metadata_path("dumpless"), "w") as f:
            f.write("{}")

        with caplog.at_level(logging.WARNING, logger="oduflow"):
            templates = postgres_migration._templates(settings)

        assert templates == [
            {"team": "1", "name": "base"},
            {"team": "1", "name": "customer/prod"},
        ]
        assert "'dumpless'" in caplog.text
        assert "'empty'" not in caplog.text


class TestWorkloads:
    def test_all_blockers_are_listed_at_once(self, tmp_path):
        settings = _settings(tmp_path)
        envs = []
        for name in ("a", "b"):
            env = MagicMock(labels={settings.branch_label: name})
            env.name = f"{settings.prefix}1-{name}-odoo"
            envs.append(env)
        client = MagicMock()
        client.containers.list.return_value = envs
        with (
            patch.object(
                postgres_migration.production_registry,
                "list_productions",
                return_value={"shop": {}},
            ),
            patch.object(
                postgres_migration.service_database_credentials,
                "list_names",
                return_value=[],
            ),
        ):
            with pytest.raises(PrerequisiteNotMetError) as exc_info:
                postgres_migration._check_workloads(client, settings, {"dev", "prod"})
        message = str(exc_info.value)
        assert envs[0].name in message and envs[1].name in message
        assert "production 1/shop" in message


class TestDatabaseCheck:
    def _cluster_container(self, status):
        container = MagicMock(status=status)
        container.name = "oduflow-prod-db"
        client = MagicMock()
        client.containers.get.return_value = container
        return client, container

    def test_cluster_started_for_inspection_is_stopped_after_refusal(self, tmp_path):
        client, container = self._cluster_container("exited")
        with (
            patch.object(postgres_migration.system_ops, "_wait_pg_ready"),
            patch.object(
                postgres_migration.system_ops, "_exec_sql", return_value="leftover"
            ),
        ):
            with pytest.raises(PrerequisiteNotMetError, match="leftover"):
                postgres_migration._check_databases(
                    client,
                    _settings(tmp_path),
                    {"kind": "prod", "container": "oduflow-prod-db"},
                    [],
                )
        container.start.assert_called_once()
        container.stop.assert_called_once()

    def test_running_cluster_is_left_running(self, tmp_path):
        client, container = self._cluster_container("running")
        with (
            patch.object(postgres_migration.system_ops, "_wait_pg_ready"),
            patch.object(
                postgres_migration.system_ops,
                "_exec_sql",
                side_effect=["postgres", "0"],
            ),
        ):
            postgres_migration._check_databases(
                client,
                _settings(tmp_path),
                {"kind": "prod", "container": "oduflow-prod-db"},
                [],
            )
        container.start.assert_not_called()
        container.stop.assert_not_called()


class TestPreflight:
    def _old_cluster(self, settings, status, image="docker.io/library/postgres:15"):
        container = MagicMock(status=status, id="old")
        container.name = settings.shared_db_container
        container.labels = {settings.managed_label: "true"}
        container.attrs = {
            "Config": {"Image": image},
            "Mounts": [
                {
                    "Destination": "/var/lib/postgresql/data",
                    "Type": "volume",
                    "Name": settings.shared_db_volume,
                }
            ],
        }
        volume = MagicMock(attrs={"CreatedAt": "t"})
        client = MagicMock()

        def get_container(name):
            if name == settings.shared_db_container:
                return container
            raise postgres_migration.docker.errors.NotFound(name)

        def get_volume(name):
            if name == settings.shared_db_volume:
                return volume
            raise postgres_migration.docker.errors.NotFound(name)

        client.containers.get.side_effect = get_container
        client.volumes.get.side_effect = get_volume
        client.containers.list.return_value = []
        return client, container

    def _run(self, settings, client):
        with (
            patch.object(postgres_migration, "validate_configuration"),
            patch.object(postgres_migration, "get_client", return_value=client),
            patch.object(postgres_migration, "_data_major", return_value=15),
            patch.object(
                postgres_migration.system_ops,
                "_pg_tablespaces_host_dir",
                return_value=str(settings.base_data_dir),
            ),
            patch.object(postgres_migration, "_check_databases") as check_databases,
            patch.object(postgres_migration, "_pull_target") as pull,
        ):
            postgres_migration.preflight(settings)
        pull.assert_not_called()
        return check_databases

    def test_stopped_cluster_is_not_started(self, tmp_path):
        settings = _settings(tmp_path)
        client, container = self._old_cluster(settings, "exited")
        assert not self._run(settings, client).called
        container.start.assert_not_called()

    def test_running_cluster_databases_are_checked(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(settings, "running")
        assert self._run(settings, client).called

    def test_custom_image_cluster_is_refused(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(
            settings, "running", image="pgvector/pgvector:0.8.0-pg15"
        )
        with pytest.raises(PrerequisiteNotMetError, match="Cannot automatically reset"):
            self._run(settings, client)

    def test_migration_under_way_is_not_rechecked(self, tmp_path):
        settings = _settings(tmp_path)
        _journal(settings, [])
        with patch.object(postgres_migration, "validate_configuration") as validate:
            postgres_migration.preflight(settings)
        validate.assert_not_called()
