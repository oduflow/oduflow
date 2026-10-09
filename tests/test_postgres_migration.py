"""PostgreSQL major upgrades: trigger, plan, dump/restore, resume and finish."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from unittest.mock import MagicMock, call, patch

import pytest

from oduflow import postgres_migration
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings, TeamSettings

pm = postgres_migration


def _settings(tmp_path, *, backup: bool = True, **kwargs) -> Settings:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    return Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        backup=BackupSettings(bucket="b", access_key="k", secret_key="s")
        if backup
        else None,
        **kwargs,
    )


def _cluster(kind="dev", **overrides):
    cluster = {
        "kind": kind,
        "container": "oduflow-db" if kind == "dev" else "oduflow-prod-db",
        "volume": "v",
        "old_container_id": "old",
        "old_image": "postgres:15",
        "old_volume_created": "t",
        "old_major": 15,
        "was_running": True,
        "databases": [],
        "restored_databases": [],
        "roles_restored": False,
        "phase": "dump",
    }
    cluster.update(overrides)
    return cluster


def _state(clusters=(), **overrides):
    state = {
        "version": 2,
        "image": "postgres:16",
        "image_id": "sha256:new",
        "major": 16,
        "stopped": [],
        "clusters": list(clusters),
        "templates": [],
        "restored": [],
        "skipped": [],
    }
    state.update(overrides)
    return state


def _journal(settings):
    path = os.path.join(settings.base_data_dir, pm._JOURNAL)
    with open(path) as f:
        return json.load(f)


def _s3(pages, errors=None):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = pages
    client.delete_objects.return_value = {"Errors": errors} if errors else {}
    return client


def _docker(images_present=True, containers=None):
    client = MagicMock()
    if not images_present:
        client.images.get.side_effect = [
            pm.docker.errors.ImageNotFound("missing"),
            MagicMock(id="sha256:new"),
        ]
    else:
        client.images.get.return_value = MagicMock(id="sha256:local")
    containers = containers or {}

    def get(name):
        if name not in containers:
            raise pm.docker.errors.NotFound(name)
        return containers[name]

    client.containers.get.side_effect = get
    return client


def _pg_container(name, *, status="running", labels=None):
    container = MagicMock(status=status, id="old")
    container.name = name
    container.labels = labels if labels is not None else {"oduflow.managed": "true"}
    return container


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
        with patch.object(pm.s3_client, "make_client", return_value=s3):
            pm._delete_walg_archive(settings)

        s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="b", Prefix="oduflow/walg/"
        )
        deleted = [
            c.kwargs["Delete"]["Objects"] for c in s3.delete_objects.call_args_list
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
        with patch.object(pm.s3_client, "make_client", return_value=s3):
            with pytest.raises(PrerequisiteNotMetError, match="AccessDenied"):
                pm._delete_walg_archive(settings)


class TestRemoveOld:
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
            patch.object(pm, "_delete_walg_archive") as delete,
            patch.object(
                pm.system_ops,
                "_pg_tablespaces_host_dir",
                return_value=str(tmp_path / "missing"),
            ),
        ):
            pm._remove_old(self._client(), settings, _state(), _cluster(kind))
        assert delete.call_count == expected

    def test_s3_failure_leaves_the_stopped_cluster_intact(self, tmp_path):
        settings = _settings(tmp_path)
        client = self._client()
        with patch.object(
            pm,
            "_delete_walg_archive",
            side_effect=PrerequisiteNotMetError("AccessDenied"),
        ):
            with pytest.raises(PrerequisiteNotMetError):
                pm._remove_old(client, settings, _state(), _cluster("prod"))
        client.containers.get.return_value.stop.assert_called_once()
        client.containers.get.return_value.remove.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()

    def test_only_the_old_majors_tablespace_files_are_removed(self, tmp_path):
        settings = _settings(tmp_path)
        tablespaces = tmp_path / "pg_tablespaces"
        tablespaces.mkdir()
        with (
            patch.object(
                pm.system_ops,
                "_pg_tablespaces_host_dir",
                return_value=str(tablespaces),
            ),
            patch.object(pm, "run_for_output") as run,
        ):
            pm._remove_old(
                self._client(), settings, _state(), _cluster("dev", old_major=16)
            )
        assert "-name 'PG_16_*'" in run.call_args.args[2][1]


def _write_dump(team, name, filename="dump.pgdump"):
    tpl_dir = team.get_template_dir(name)
    os.makedirs(tpl_dir, exist_ok=True)
    with open(os.path.join(tpl_dir, filename), "wb") as f:
        f.write(b"PGDMP")


class TestTemplateRestore:
    def test_removed_template_and_team_are_skipped(self, tmp_path):
        settings = _settings(tmp_path)
        team = settings.teams["1"]
        _write_dump(team, "kept", filename="dump.sql")
        state = _state(
            templates=[
                {"team": "1", "name": "gone"},
                {"team": "1", "name": "kept"},
                {"team": "9", "name": "orphan"},
            ]
        )
        with patch.object(pm.system_ops, "reload_template") as reload:
            pm._restore_templates(settings, state)

        # The dump is resolved again, so a fixed file under another name is used.
        reload.assert_called_once()
        assert reload.call_args.kwargs["dump_path"] == os.path.join(
            team.get_template_dir("kept"), "dump.sql"
        )
        assert reload.call_args.kwargs["strict"] is True
        journal = _journal(settings)
        assert journal["restored"] == [["1", "kept"]]
        assert journal["skipped"] == [["1", "gone"], ["9", "orphan"]]

    def test_restore_failure_explains_how_to_skip(self, tmp_path):
        settings = _settings(tmp_path)
        _write_dump(settings.teams["1"], "broken")
        state = _state(templates=[{"team": "1", "name": "broken"}])
        with patch.object(
            pm.system_ops,
            "reload_template",
            side_effect=ExternalCommandError("pg_restore", 1, "bad archive"),
        ):
            with pytest.raises(
                PrerequisiteNotMetError,
                match="oduflow delete-template broken --team 1",
            ):
                pm._restore_templates(settings, state)
        assert state["restored"] == []


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
            patch.object(pm, "_HEARTBEAT_SECONDS", 0.01),
            patch.object(pm, "get_client"),
            patch.object(pm.system_ops, "_exec_sql", side_effect=probe),
            caplog.at_level(logging.INFO, logger="oduflow"),
        ):
            with pm._restore_heartbeat(settings, "1/base", "oduflow_1_template_base"):
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
        system_ops = pm.system_ops

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

        cmds = [c.args[0] for c in db_container.exec_run.call_args_list]
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
        assert pm._repository(image) == repository

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
        assert pm._image_major(image) == major


class TestValidateConfiguration:
    def _validate(self, tmp_path, image, client, data_majors=None, explicit=True):
        settings = replace(
            _settings(tmp_path),
            postgres_image=image,
            postgres_image_explicit=explicit,
        )
        with (
            patch.object(pm, "get_client", return_value=client),
            patch.object(pm, "_binary_major", return_value=16),
            patch.object(
                pm,
                "_data_major",
                side_effect=lambda c: (data_majors or {})[c.name],
            ),
        ):
            pm.validate_configuration(settings)

    def _with_dev(self, data_major):
        db = _pg_container("oduflow-db")
        return _docker(containers={"oduflow-db": db}), {"oduflow-db": data_major}

    def test_extension_image_is_checked_by_binary(self, tmp_path):
        self._validate(tmp_path, "pgvector/pgvector:0.8.0-pg16", _docker())

    @pytest.mark.parametrize("image", ["postgres:15", "postgres:16", "postgres:17"])
    def test_supported_majors_on_a_fresh_install(self, tmp_path, image):
        self._validate(tmp_path, image, _docker())

    @pytest.mark.parametrize("image", ["postgres:14", "postgres:18"])
    def test_unsupported_majors_are_refused(self, tmp_path, image):
        with pytest.raises(
            PrerequisiteNotMetError, match="supports PostgreSQL 15 to 17"
        ):
            self._validate(tmp_path, image, _docker())

    def test_cluster_stays_on_its_major_by_default(self, tmp_path):
        client, majors = self._with_dev(15)
        self._validate(tmp_path, "postgres:15", client, majors)

    def test_explicit_newer_image_is_left_to_the_upgrade(self, tmp_path):
        client, majors = self._with_dev(15)
        self._validate(tmp_path, "postgres:16", client, majors)

    def test_new_default_never_upgrades_a_cluster(self, tmp_path):
        client, majors = self._with_dev(15)
        with pytest.raises(
            PrerequisiteNotMetError, match="Set it to 'postgres:15' to keep"
        ):
            self._validate(tmp_path, "postgres:16", client, majors, explicit=False)

    def test_downgrade_is_refused(self, tmp_path):
        client, majors = self._with_dev(17)
        with pytest.raises(PrerequisiteNotMetError, match="cannot be downgraded"):
            self._validate(tmp_path, "postgres:16", client, majors)


class TestPullTarget:
    def test_local_image_is_used_without_pulling(self):
        client = _docker()
        with (
            patch.object(pm, "_download_image") as download,
            patch.object(pm, "_binary_major", return_value=16),
        ):
            assert pm._pull_target(client, "acme/pg16:latest") == ("sha256:local", 16)
        download.assert_not_called()

    def test_missing_image_is_pulled(self):
        client = _docker(images_present=False)
        with (
            patch.object(pm, "_download_image") as download,
            patch.object(pm, "_binary_major", return_value=17),
        ):
            assert pm._pull_target(client, "postgres:17") == ("sha256:new", 17)
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
            templates = pm._templates(settings)

        assert templates == [
            {"team": "1", "name": "base"},
            {"team": "1", "name": "customer/prod"},
        ]
        assert "'dumpless'" in caplog.text
        assert "'empty'" not in caplog.text


class TestEnvironments:
    def test_all_environments_are_listed_and_productions_ignored(self, tmp_path):
        settings = _settings(tmp_path)
        containers = []
        for name, labels in (
            ("a", {settings.branch_label: "a"}),
            ("b", {settings.branch_label: "b"}),
            ("prod-shop", {settings.branch_label: "x", "oduflow.prod": "true"}),
        ):
            container = MagicMock(labels=labels)
            container.name = f"{settings.prefix}1-{name}-odoo"
            containers.append(container)
        client = MagicMock()
        client.containers.list.return_value = containers
        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            pm._check_environments(client, settings, 15)
        message = str(exc_info.value)
        assert containers[0].name in message and containers[1].name in message
        assert "prod-shop" not in message
        assert "back to 'postgres:15'" in message


class TestInventory:
    def _run(self, settings, kind, present, *, templates=(), sizes=None):
        def sql(client, settings_, query, db="postgres", **kwargs):
            if query.startswith("SELECT datname"):
                return "\n".join(present)
            if query.startswith("SELECT count(*)"):
                return "0"
            if query.startswith("SELECT extname"):
                return {
                    "oduflow_1_prod-shop": "pg_trgm",
                    "oduflow_template_1_base": "unaccent",
                }.get(db, "")
            return str((sizes or {}).get(query, 10))

        records = {
            "n8n": {"database": "oduflow_service_1_n8n", "cluster": "dev"},
            "pay": {"database": "oduflow_service_1_pay", "cluster": "prod"},
        }
        with (
            patch.object(pm.system_ops, "_exec_sql", side_effect=sql),
            patch.object(
                pm.service_database_credentials, "list_names", return_value=records
            ),
            patch.object(
                pm.service_database_credentials,
                "load",
                side_effect=lambda team, name: records[name],
            ),
            patch.object(
                pm.production_registry, "list_productions", return_value={"shop": {}}
            ),
        ):
            return pm._inventory(MagicMock(), settings, _cluster(kind), list(templates))

    def test_dev_carries_service_databases_and_allows_templates(self, tmp_path):
        settings = _settings(tmp_path)
        databases, extensions, size = self._run(
            settings,
            "dev",
            ["postgres", "odoo", "oduflow_service_1_n8n", "oduflow_template_1_base"],
            templates=[{"team": "1", "name": "base"}],
        )
        assert databases == ["oduflow_service_1_n8n"]
        # Templates are not dumped, but the new image must still serve them.
        assert extensions == {"unaccent"}
        assert size == 10

    def test_prod_carries_registered_and_kept_productions(self, tmp_path):
        settings = _settings(tmp_path)
        # A production deleted with its database kept still has its workspace.
        os.makedirs(os.path.join(settings.teams["1"].workspaces_dir, "prod-old"))
        databases, extensions, _ = self._run(
            settings,
            "prod",
            [
                "postgres",
                "oduflow_1_prod-shop",
                "oduflow_1_prod-old",
                "oduflow_service_1_pay",
            ],
        )
        assert databases == [
            "oduflow_1_prod-old",
            "oduflow_1_prod-shop",
            "oduflow_service_1_pay",
        ]
        assert extensions == {"pg_trgm"}

    def test_unknown_databases_are_refused(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            self._run(
                settings,
                "dev",
                ["oduflow_1_feature-x", "oduflow_service_1_n8n", "manual"],
            )
        message = str(exc_info.value)
        assert "manual, oduflow_1_feature-x" in message
        assert "oduflow cleanup --force" in message
        assert "back to 'postgres:15'" in message


class TestCheckExtensions:
    def _check(self, listing, needed):
        with patch.object(pm, "run_for_output", return_value=listing) as run:
            pm._check_extensions(MagicMock(), "postgres:16", "sha256:new", needed)
        return run

    def test_nothing_needed_runs_nothing(self):
        assert not self._check(b"", set()).called

    def test_available_extensions_pass(self):
        self._check(
            b"/usr/share/postgresql/16/extension/plpgsql.control\n"
            b"/usr/share/postgresql/16/extension/pg_trgm.control\n",
            {"pg_trgm"},
        )

    def test_missing_extension_is_refused(self):
        with pytest.raises(PrerequisiteNotMetError, match="lacks extensions.*vector"):
            self._check(b"/x/extension/plpgsql.control\n", {"vector"})

    def test_empty_listing_means_the_probe_failed(self):
        with pytest.raises(PrerequisiteNotMetError, match="Cannot list"):
            self._check(b"", {"pg_trgm"})


class TestPlan:
    def _old_cluster(self, settings, status, *, data_major=15):
        container = _pg_container(settings.shared_db_container, status=status)
        container.attrs = {
            "Config": {"Image": "postgres:15"},
            "Mounts": [
                {
                    "Destination": "/var/lib/postgresql/data",
                    "Type": "volume",
                    "Name": settings.shared_db_volume,
                }
            ],
        }
        volume = MagicMock(attrs={"CreatedAt": "t"})
        client = _docker(containers={settings.shared_db_container: container})

        def get_volume(name):
            if name == settings.shared_db_volume:
                return volume
            raise pm.docker.errors.NotFound(name)

        client.volumes.get.side_effect = get_volume
        client.containers.list.return_value = []
        return client, container

    def _plan(self, settings, client, *, preflight, data_major=15):
        with (
            patch.object(pm, "_configured_major", return_value=16),
            patch.object(pm, "_data_major", return_value=data_major),
            patch.object(pm, "_templates", return_value=[]),
            patch.object(
                pm.system_ops,
                "_pg_tablespaces_host_dir",
                return_value=str(settings.base_data_dir),
            ),
            patch.object(pm.system_ops, "_wait_pg_ready"),
            patch.object(
                pm, "_inventory", return_value=(["db"], {"pg_trgm"}, 5)
            ) as inventory,
            patch.object(pm, "_pull_target", return_value=("sha256:new", 16)) as pull,
            patch.object(pm, "_check_extensions") as extensions,
            patch.object(pm, "_check_disk_space") as disk,
        ):
            state = pm._plan(client, settings, preflight=preflight)
        return state, inventory, pull, extensions, disk

    def test_cluster_on_the_configured_major_needs_nothing(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(settings, "running")
        state, *_ = self._plan(settings, client, preflight=False, data_major=16)
        assert state is None

    def test_upgrade_pins_the_image_and_checks_before_removal(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(settings, "running")
        state, _, pull, extensions, disk = self._plan(settings, client, preflight=False)
        (cluster,) = state["clusters"]
        assert (cluster["kind"], cluster["phase"], cluster["databases"]) == (
            "dev",
            "dump",
            ["db"],
        )
        assert (state["image_id"], state["major"]) == ("sha256:new", 16)
        extensions.assert_called_once_with(
            client, settings.postgres_image, "sha256:new", {"pg_trgm"}
        )
        disk.assert_called_once_with(settings, 5)

    def test_stopped_cluster_is_inspected_and_stopped_again(self, tmp_path):
        settings = _settings(tmp_path)
        client, container = self._old_cluster(settings, "exited")
        state, inventory, *_ = self._plan(settings, client, preflight=False)
        assert inventory.called
        container.start.assert_called_once()
        container.stop.assert_called_once()
        assert state["clusters"][0]["was_running"] is False

    def test_preflight_neither_starts_clusters_nor_pulls(self, tmp_path):
        settings = _settings(tmp_path)
        client, container = self._old_cluster(settings, "exited")
        _, inventory, pull, *_ = self._plan(settings, client, preflight=True)
        assert not inventory.called
        container.start.assert_not_called()
        pull.assert_not_called()

    def test_preflight_inspects_running_clusters(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(settings, "running")
        _, inventory, pull, *_ = self._plan(settings, client, preflight=True)
        assert inventory.called
        pull.assert_not_called()

    def test_environments_refuse_the_upgrade(self, tmp_path):
        settings = _settings(tmp_path)
        client, _ = self._old_cluster(settings, "running")
        env = MagicMock(labels={settings.branch_label: "a"})
        env.name = f"{settings.prefix}1-a-odoo"
        client.containers.list.return_value = [env]
        with pytest.raises(PrerequisiteNotMetError, match="Delete these environments"):
            self._plan(settings, client, preflight=False)


class TestStopApplications:
    def test_services_and_production_odoo_are_recorded_then_stopped(self, tmp_path):
        settings = _settings(tmp_path)
        running = []
        for name, labels in (
            ("oduflow-svc", {"oduflow.service": "fs"}),
            ("oduflow-shop", {"oduflow.prod": "true"}),
            (settings.prod_db_container, {"oduflow.prod": "true"}),
            ("oduflow-agent", {}),
            # Another Oduflow instance on the same Docker daemon.
            ("other-shop", {"oduflow.prod": "true"}),
        ):
            container = MagicMock(labels=labels)
            container.name = name
            running.append(container)
        client = MagicMock()
        client.containers.list.return_value = running
        state = _state([_cluster("prod")])
        recorded = []
        for container in running:
            # Journaled before the stop, so a crash still restarts it.
            container.stop.side_effect = lambda name=container.name: recorded.append(
                name in _journal(settings)["stopped"]
            )

        pm._stop_applications(client, settings, state)

        assert state["stopped"] == ["oduflow-shop", "oduflow-svc"]
        assert recorded == [True, True]
        assert not any(c.stop.called for c in running[2:])


class TestRestoreCluster:
    def _setup(self, tmp_path, kind="prod"):
        settings = _settings(tmp_path)
        directory = pm._dump_path(settings, kind)
        os.makedirs(directory)
        with open(os.path.join(directory, "roles.sql"), "w") as f:
            f.write("ALTER ROLE x;\n")
        cluster = _cluster(kind, phase="restore", databases=["a", "b"])
        return settings, cluster, _state([cluster])

    def test_roles_once_then_each_database_dropped_and_restored(self, tmp_path):
        settings, cluster, state = self._setup(tmp_path)
        cluster["restored_databases"] = ["a"]
        with (
            patch.object(pm.system_ops, "_exec_sql") as sql,
            patch.object(pm, "_run_helper") as helper,
        ):
            pm._restore_cluster(MagicMock(), settings, state, cluster)
            pm._restore_cluster(MagicMock(), settings, state, cluster)

        statements = [c.args[2] for c in sql.call_args_list]
        assert statements.count("ALTER ROLE x;") == 1
        assert statements.count('DROP DATABASE IF EXISTS "b" WITH (FORCE);') == 1
        (command,) = [c.args[4] for c in helper.call_args_list]
        assert command[0] == "pg_restore" and "--create" in command
        assert command[-1] == "/dump/b.pgdump"
        assert _journal(settings)["clusters"][0]["restored_databases"] == ["a", "b"]

    def test_dev_tablespaces_exist_before_restoring(self, tmp_path):
        settings, cluster, state = self._setup(tmp_path, kind="dev")
        order = []
        with (
            patch.object(
                pm.system_ops,
                "ensure_team_tablespace",
                side_effect=lambda *a: order.append("tablespace"),
            ),
            patch.object(pm.system_ops, "_exec_sql"),
            patch.object(
                pm, "_run_helper", side_effect=lambda *a, **k: order.append("restore")
            ),
        ):
            pm._restore_cluster(MagicMock(), settings, state, cluster)
        assert order == ["tablespace", "restore", "restore"]

    def test_failure_names_the_kept_dump(self, tmp_path):
        settings, cluster, state = self._setup(tmp_path)
        with (
            patch.object(pm.system_ops, "_exec_sql"),
            patch.object(
                pm,
                "_run_helper",
                side_effect=ExternalCommandError("pg_restore", 1, "boom"),
            ),
        ):
            with pytest.raises(PrerequisiteNotMetError, match=r"boom.*a\.pgdump"):
                pm._restore_cluster(MagicMock(), settings, state, cluster)
        assert cluster["restored_databases"] == []


class TestRunHelper:
    def _client(self, exit_code, stale=None):
        client = MagicMock()
        helper = MagicMock(status="exited", attrs={"State": {"ExitCode": exit_code}})
        helper.logs.return_value = b"pg_dump: error: connection refused"
        client.containers.run.return_value = helper
        client.containers.get.side_effect = (
            (lambda name: stale) if stale else pm.docker.errors.NotFound("x")
        )
        return client, helper

    def test_runs_in_the_cluster_namespace_with_the_dump_mount(self, tmp_path):
        settings = _settings(tmp_path)
        stale = MagicMock()
        client, helper = self._client(0, stale=stale)
        pm._run_helper(
            client, settings, "sha256:new", "oduflow-db", ["pg_dump", "x"], "/d", "L"
        )
        stale.remove.assert_called_once_with(force=True)
        kwargs = client.containers.run.call_args.kwargs
        assert client.containers.run.call_args.args == ("sha256:new", ["x"])
        assert kwargs["entrypoint"] == "pg_dump"
        assert kwargs["network_mode"] == "container:oduflow-db"
        assert kwargs["volumes"] == {"/d": {"bind": "/dump", "mode": "rw"}}
        helper.remove.assert_called_once_with(force=True)

    def test_failure_carries_the_tool_output(self, tmp_path):
        settings = _settings(tmp_path)
        client, helper = self._client(1)
        with pytest.raises(ExternalCommandError, match="connection refused"):
            pm._run_helper(
                client, settings, "img", "oduflow-db", ["pg_dump", "x"], "/d", "L"
            )
        helper.remove.assert_called_once_with(force=True)


class TestRun:
    def test_dump_failure_restores_everything_as_it_was(self, tmp_path):
        settings = _settings(tmp_path)
        state = _state([_cluster("dev")])
        os.makedirs(pm._dump_path(settings, "dev"))
        pm._save(settings, state)

        def stop(client, settings_, state_):
            state_["stopped"].append("svc")

        client = MagicMock()
        with (
            patch.object(pm, "_stop_applications", side_effect=stop),
            patch.object(
                pm, "_dump_cluster", side_effect=ExternalCommandError("pg_dump", 1, "x")
            ),
            patch.object(pm, "_remove_old") as remove,
        ):
            with pytest.raises(ExternalCommandError):
                pm._run(client, settings, state)
        remove.assert_not_called()
        client.containers.get.assert_called_once_with("svc")
        client.containers.get.return_value.start.assert_called_once()
        assert not os.path.exists(os.path.join(settings.base_data_dir, pm._JOURNAL))
        assert not os.path.exists(pm._dump_path(settings))

    def test_phases_run_in_order_and_resume_after_restore_failure(self, tmp_path):
        settings = _settings(tmp_path)
        state = _state([_cluster("dev", databases=["a"])])
        new = MagicMock(id="new", status="running")
        new.name = "oduflow-db"
        client = MagicMock()
        client.containers.get.return_value = new
        calls = []
        patches = (
            patch.object(pm, "_stop_applications"),
            patch.object(
                pm, "_dump_cluster", side_effect=lambda *a: calls.append("dump")
            ),
            patch.object(
                pm, "_remove_old", side_effect=lambda *a: calls.append("remove")
            ),
            patch.object(
                pm, "_create_new", side_effect=lambda *a: calls.append("create")
            ),
            patch.object(pm.system_ops, "_wait_pg_ready"),
            patch.object(pm, "_restore_templates"),
            patch.object(pm, "_finish"),
        )
        for p in patches:
            p.start()
        try:
            with patch.object(
                pm, "_restore_cluster", side_effect=PrerequisiteNotMetError("x")
            ):
                with pytest.raises(PrerequisiteNotMetError):
                    pm._run(client, settings, state)
            assert calls == ["dump", "remove", "create"]
            assert _journal(settings)["clusters"][0]["phase"] == "restore"

            resumed = _journal(settings)
            with patch.object(pm, "_restore_cluster") as restore:
                pm._run(client, settings, resumed)
            restore.assert_called_once()
            assert calls == ["dump", "remove", "create"]  # nothing redone
            assert resumed["clusters"][0]["phase"] == "done"
        finally:
            for p in patches:
                p.stop()

    def test_replaced_cluster_must_still_be_the_one_created(self, tmp_path):
        settings = _settings(tmp_path)
        cluster = _cluster("dev", phase="restore", new_container_id="new")
        other = MagicMock(id="other")
        client = MagicMock()
        client.containers.get.return_value = other
        with pytest.raises(PrerequisiteNotMetError, match="changed or disappeared"):
            pm._run(client, settings, _state([cluster]))


class TestResume:
    def test_journal_before_any_removal_is_cancelled_and_replanned(self, tmp_path):
        settings = _settings(tmp_path)
        pm._save(settings, _state([_cluster("dev")], stopped=["svc"]))
        client = MagicMock()
        assert pm._resume(client, settings) is None
        client.containers.get.return_value.start.assert_called_once()
        assert not os.path.exists(os.path.join(settings.base_data_dir, pm._JOURNAL))

    def test_image_must_stay_after_a_removal(self, tmp_path):
        settings = replace(_settings(tmp_path), postgres_image="postgres:15")
        pm._save(settings, _state([_cluster("dev", phase="create_new")]))
        with pytest.raises(PrerequisiteNotMetError, match="Keep \\[database\\].image"):
            pm._resume(MagicMock(), settings)


class TestLegacyJournal:
    def _write(self, settings, legacy):
        path = os.path.join(settings.base_data_dir, pm._LEGACY_JOURNAL)
        with open(path, "w") as f:
            json.dump(legacy, f)
        return path

    def _legacy_cluster(self, phase):
        return {
            "kind": "dev",
            "container": "oduflow-db",
            "volume": "oduflow-db-data",
            "old_container_id": "old",
            "old_image": "postgres:15",
            "old_volume_created": "t",
            "image": "postgres:16",
            "image_id": "sha256:new",
            "major": 16,
            "phase": phase,
            "new_container_id": "new",
        }

    def test_finished_replacement_is_dropped(self, tmp_path):
        settings = _settings(tmp_path)
        path = self._write(
            settings,
            {
                "clusters": [self._legacy_cluster("restore")],
                "templates": [{"team": "1", "name": "base"}],
                "restored": [["1", "base"]],
            },
        )
        assert not pm._legacy_pending(settings)
        assert pm._resume(MagicMock(), settings) is None
        assert not os.path.exists(path)

    def test_untouched_replacement_is_dropped(self, tmp_path):
        settings = _settings(tmp_path)
        path = self._write(
            settings,
            {"clusters": [self._legacy_cluster("remove_old")], "templates": []},
        )
        container = MagicMock(id="old")
        volume = MagicMock(attrs={"CreatedAt": "t"})
        client = MagicMock()
        client.containers.get.return_value = container
        client.volumes.get.return_value = volume
        assert pm._resume(client, settings) is None
        assert not os.path.exists(path)

    def test_started_replacement_is_taken_over(self, tmp_path):
        settings = _settings(tmp_path)
        path = self._write(
            settings,
            {
                "clusters": [self._legacy_cluster("restore")],
                "templates": [{"team": "1", "name": "base"}],
                "restored": [],
            },
        )
        assert pm._legacy_pending(settings)
        state = pm._resume(MagicMock(), settings)
        assert not os.path.exists(path)
        (cluster,) = state["clusters"]
        assert (cluster["phase"], cluster["databases"], cluster["roles_restored"]) == (
            "restore",
            [],
            True,
        )
        assert (state["image_id"], state["templates"]) == (
            "sha256:new",
            [{"team": "1", "name": "base"}],
        )


class TestFinish:
    def _finish(self, settings, cluster, **state):
        os.makedirs(pm._dump_path(settings, cluster["kind"]))
        full = _state([cluster], **state)
        pm._save(settings, full)
        client = MagicMock()
        with (
            patch.object(pm.system_ops, "ensure_team_network"),
            patch.object(pm.system_ops, "_reconcile_pg_hba"),
            patch.object(pm.system_ops, "ensure_prod_infra") as infra,
            patch("oduflow.backup_scheduler.request_base_backup") as base_backup,
        ):
            pm._finish(client, settings, full)
        assert not os.path.exists(pm._dump_path(settings))
        assert not os.path.exists(os.path.join(settings.base_data_dir, pm._JOURNAL))
        return client, infra, base_backup

    def test_running_production_is_verified_before_its_odoo_starts(self, tmp_path):
        settings = _settings(tmp_path, prod_enabled=True)
        client, infra, base_backup = self._finish(
            settings, _cluster("prod", phase="done"), stopped=["shop"]
        )
        infra.assert_called_once_with(client, settings, force=True)
        base_backup.assert_called_once_with(settings)
        assert call("shop") in client.containers.get.call_args_list

    def test_stopped_production_cluster_stays_stopped(self, tmp_path):
        settings = _settings(tmp_path, prod_enabled=False, backup=False)
        client, infra, base_backup = self._finish(
            settings, _cluster("prod", phase="done", was_running=False)
        )
        infra.assert_not_called()
        base_backup.assert_not_called()
        client.containers.get.assert_called_with(settings.prod_db_container)
        client.containers.get.return_value.stop.assert_called_once()


class TestPreflight:
    def test_upgrade_under_way_is_not_rechecked(self, tmp_path):
        settings = _settings(tmp_path)
        pm._save(settings, _state([_cluster("dev", phase="restore")]))
        with patch.object(pm, "validate_configuration") as validate:
            pm.preflight(settings)
        validate.assert_not_called()

    def test_checks_run_read_only(self, tmp_path):
        settings = _settings(tmp_path)
        with (
            patch.object(pm, "validate_configuration") as validate,
            patch.object(pm, "get_client"),
            patch.object(pm, "_plan") as plan,
        ):
            pm.preflight(settings)
        validate.assert_called_once_with(settings)
        assert plan.call_args.kwargs == {"preflight": True}


class TestUpgrade:
    def test_nothing_requested_changes_nothing(self, tmp_path):
        settings = _settings(tmp_path)
        with (
            patch.object(pm, "get_client"),
            patch.object(pm, "_plan", return_value=None),
            patch.object(pm, "_run") as run,
        ):
            pm.upgrade(settings)
        run.assert_not_called()
        assert not os.path.exists(os.path.join(settings.base_data_dir, pm._JOURNAL))

    def test_plan_is_journaled_before_it_runs(self, tmp_path):
        settings = _settings(tmp_path)
        state = _state([_cluster("dev")])

        def run(client, settings_, state_):
            assert _journal(settings) == state

        with (
            patch.object(pm, "get_client"),
            patch.object(pm, "_plan", return_value=state),
            patch.object(pm, "_run", side_effect=run) as runner,
        ):
            pm.upgrade(settings)
        runner.assert_called_once()


class TestOdooRequirement:
    def _check(self, image, version):
        with patch.object(pm.system_ops, "_exec_sql", return_value=version) as sql:
            pm.system_ops.require_postgres_for_odoo(MagicMock(), Settings(), image)
        return sql

    @pytest.mark.parametrize("image", ["odoo:19.0", "acme/platform:latest"])
    def test_older_or_unversioned_odoo_is_not_checked(self, image):
        assert not self._check(image, "150004").called

    def test_odoo_20_runs_on_postgresql_16(self):
        self._check("odoo:20.0", "160004")

    def test_odoo_20_is_refused_on_postgresql_15(self):
        with pytest.raises(
            PrerequisiteNotMetError, match="Odoo 20 needs PostgreSQL 16"
        ):
            self._check("odoo:20.0", "150004")
