"""Data refresh stages first, preserves old data on failure, and pins lowers."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from oduflow import template_generations as gen
from oduflow.docker_ops import env_ops, system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import get_db_name, get_filestore_paths, get_workspace_path
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    source = Path(team.get_template_filestore_path("prod"))
    source.mkdir(parents=True)
    (source / "new").write_text("new attachment")
    atomic_write_private_json(
        team.get_template_metadata_path("prod"),
        {"odoo_version": "19.0", "use_overlay": False},
    )
    paths = get_filestore_paths("dev", team.workspaces_dir)
    Path(paths["merged"]).mkdir(parents=True)
    Path(paths["merged"], "old").write_text("old attachment")
    generation = gen.snapshot(team, "prod")
    container = Mock(status="running", labels={settings.image_label: "odoo:19.0"})
    container.attrs = {
        "Config": {"Image": "odoo:19.0", "Env": []},
        "HostConfig": {
            "NetworkMode": "net",
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "Mounts": [
            {
                "Source": paths["merged"],
                "Destination": f"/var/lib/odoo/.local/share/Odoo/filestore/{get_db_name('dev', '1')}",
            }
        ],
    }
    container.get_archive.return_value = ([b"archive"], {})
    runner = Mock()
    runner.exec_run.return_value = (0, b"OK")
    client = Mock()
    client.containers.get.return_value = container
    client.containers.run.return_value = runner
    sql = Mock(return_value="")
    monkeypatch.setattr(system_ops, "_exec_sql", sql)
    for name in [
        "check_disk_space",
        "reassign_db_ownership",
        "drop_signaling_sequences",
    ]:
        monkeypatch.setattr(system_ops, name, Mock())
    monkeypatch.setattr(system_ops, "estimate_new_db_bytes", Mock(return_value=10))
    monkeypatch.setattr(system_ops, "ensure_team_tablespace", Mock(return_value="ts"))
    monkeypatch.setattr(system_ops, "pg_clone_strategy_clause", Mock(return_value=""))
    monkeypatch.setattr(
        "oduflow.env_credentials.load_credentials",
        Mock(return_value={"pg_user": "u_1_dev", "pg_password": "test"}),
    )
    monkeypatch.setattr(
        env_ops, "_install_repo_dependencies", Mock(return_value=(0, False, []))
    )
    monkeypatch.setattr(
        env_ops,
        "_wait_for_container_odoo_ready",
        Mock(return_value=SimpleNamespace(ready=True)),
    )
    monkeypatch.setattr(
        env_ops, "overlay_mount_state", lambda path: env_ops.MOUNT_ABSENT
    )
    monkeypatch.setattr(env_ops, "_unmount_filestore", Mock())
    monkeypatch.setattr(env_ops, "_wait_unmounted", Mock())

    def mount(client, settings, team, name, db, image, volumes, **kwargs):
        lower, _ = gen.lower_paths(
            team, name, kwargs["template_name"], kwargs.get("generation", "")
        )
        dest = kwargs.get("paths_override", paths)["merged"]
        shutil.copytree(lower, dest)
        volumes[dest] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{db}",
            "mode": "rw",
        }

    monkeypatch.setattr(env_ops, "_mount_filestore", mount)
    neutral = Mock(return_value=[])
    sanitize = Mock(return_value=[])
    monkeypatch.setattr("oduflow.sanitizer.neutralize_environment", neutral)
    monkeypatch.setattr("oduflow.sanitizer.sanitize_environment", sanitize)
    return SimpleNamespace(
        settings=settings,
        team=team,
        client=client,
        container=container,
        runner=runner,
        sql=sql,
        paths=paths,
        generation=generation,
        neutral=neutral,
        sanitize=sanitize,
    )


def refresh(p):
    gen.refresh_environment(
        p.client, p.settings, p.team, "dev", "prod", p.generation, "snapshot-1"
    )


def test_copy_refresh_preserves_container_and_promotes_sanitized_data(prepared):
    p = prepared
    refresh(p)
    assert Path(p.paths["merged"], "new").read_text() == "new attachment"
    assert not Path(p.paths["merged"], "old").exists()
    assert gen.pinned(p.team, "dev")["snapshot"] == "snapshot-1"
    assert p.sanitize.call_args.kwargs["database"].startswith("oduflow_sync_")
    assert p.sanitize.call_args.kwargs["runner"] is p.runner
    p.container.remove.assert_not_called()
    p.container.start.assert_called_once()
    assert not (
        Path(get_workspace_path("dev", p.team.workspaces_dir)) / gen.JOURNAL
    ).exists()
    # Re-running the same backup must not erase the developer's new work.
    Path(p.paths["merged"], "local").write_text("local")
    refresh(p)
    assert Path(p.paths["merged"], "local").exists()
    assert p.sanitize.call_count == 1


def test_stopped_environment_stays_stopped(prepared):
    p = prepared
    p.container.status = "exited"
    refresh(p)
    p.container.start.assert_not_called()


def test_failed_sanitization_never_stops_or_renames_old_database(prepared):
    p = prepared
    p.sanitize.return_value = ["[SANITIZE] WARNING: script failed"]
    with pytest.raises(PrerequisiteNotMetError, match="Sanitization"):
        refresh(p)
    assert Path(p.paths["merged"], "old").read_text() == "old attachment"
    p.container.stop.assert_not_called()
    assert all("RENAME TO" not in c.args[2] for c in p.sql.call_args_list)
    assert not gen.pinned(p.team, "dev")


def test_legacy_sanitize_path_notice_does_not_block_promotion(prepared):
    p = prepared
    p.sanitize.return_value = [
        "[SANITIZE:repo] NOTICE: Project sanitize scripts moved to "
        ".oduflow/odoo_sanitize; move scripts from .odoo_sanitize there.",
        "[SANITIZE:repo-legacy] Executed 10_mail.sql",
    ]
    refresh(p)
    assert gen.pinned(p.team, "dev")["snapshot"] == "snapshot-1"


def test_start_failure_rolls_back_database_and_filestore(prepared):
    p = prepared
    p.container.start.side_effect = [RuntimeError("start failed"), None]
    with pytest.raises(RuntimeError, match="start failed"):
        refresh(p)
    assert Path(p.paths["merged"], "old").read_text() == "old attachment"
    assert not Path(p.paths["merged"], "new").exists()
    renames = [c.args[2] for c in p.sql.call_args_list if "RENAME TO" in c.args[2]]
    assert len(renames) == 2
    assert '"oduflow_previous_' in renames[1]
    assert not gen.pinned(p.team, "dev")


def test_rollback_failure_keeps_recovery_evidence_and_blocks_start(prepared):
    p = prepared
    p.container.start.side_effect = RuntimeError("cannot start")
    with pytest.raises(RuntimeError):
        refresh(p)
    journal = Path(get_workspace_path("dev", p.team.workspaces_dir)) / gen.JOURNAL
    assert journal.exists()
    assert Path(json.loads(journal.read_text())["stage"]).exists()
    with pytest.raises(PrerequisiteNotMetError, match="recovery"):
        gen.require_no_recovery(p.team, "dev")
    with pytest.raises(PrerequisiteNotMetError, match="recovery"):
        env_ops.start_environment(p.settings, "dev", p.team)


def test_gc_preserves_pins_and_does_not_follow_current_template(prepared):
    p = prepared
    refresh(p)
    unrelated = gen.snapshot(p.team, "prod")
    gen.collect(p.team)
    assert gen._generation_dir(p.team, p.generation).exists()
    assert not gen._generation_dir(p.team, unrelated).exists()
    # Simulate rsync's atomic replacement, including a deletion in the source.
    source = Path(p.team.get_template_filestore_path("prod"))
    (source / "new").unlink()
    assert Path(gen.lower_paths(p.team, "dev", "prod")[0], "new").exists()


def test_pinned_overlay_is_not_remounted_by_ordinary_refresh(prepared, monkeypatch):
    p = prepared
    atomic_write_private_json(
        str(gen._marker(p.team, "dev")), {"generation": p.generation}
    )
    monkeypatch.setattr(
        env_ops,
        "list_environments",
        lambda *a: [{"env_name": "dev", "template_name": "prod"}],
    )
    monkeypatch.setattr(env_ops, "overlay_mount_state", lambda p: env_ops.MOUNT_ALIVE)
    with env_ops.remount_template_overlays(
        p.client, p.settings, p.team, "prod"
    ) as result:
        assert not result.affected
    # Reported, not silently skipped: callers tell the operator.
    assert result.pinned == ["dev"]
    assert not result.failures
    p.container.stop.assert_not_called()


def test_unreadable_pin_is_left_alone_not_fatal(prepared, monkeypatch):
    p = prepared
    gen._marker(p.team, "dev").write_text('{"generation": "abc')
    with pytest.raises(PrerequisiteNotMetError, match="Unreadable"):
        gen.pinned(p.team, "dev")
    monkeypatch.setattr(
        env_ops,
        "list_environments",
        lambda *a: [{"env_name": "dev", "template_name": "prod"}],
    )
    monkeypatch.setattr(env_ops, "overlay_mount_state", lambda p: env_ops.MOUNT_ALIVE)
    with env_ops.remount_template_overlays(
        p.client, p.settings, p.team, "prod"
    ) as result:
        pass
    # Its lower layer is unknown: moving it could pair the database with the
    # wrong attachments, and one bad workspace must not fail the whole refresh.
    assert result.pinned == ["dev"]
    p.container.stop.assert_not_called()


def test_database_cloned_from_the_current_snapshot_is_not_reset(prepared):
    p = prepared
    gen.record_template_snapshot(p.team, "prod", "snapshot-1")
    gen.record_provenance(p.team, "dev", "prod")
    # pin_existing pins it before the sync's import, without a snapshot.
    atomic_write_private_json(
        str(gen._marker(p.team, "dev")), {"generation": p.generation}
    )
    assert gen.data_snapshot(p.team, "dev") == "snapshot-1"
    refresh(p)
    p.sql.assert_not_called()
    p.sanitize.assert_not_called()
    assert Path(p.paths["merged"], "old").exists()


def test_provenance_needs_a_template_written_by_sync(prepared):
    from oduflow import template_import

    p = prepared
    gen.record_provenance(p.team, "dev", "prod")
    assert gen.data_snapshot(p.team, "dev") == ""
    gen.record_template_snapshot(p.team, "prod", "snapshot-1")
    # Any import replaces the data, so it forgets the snapshot it carried.
    carried = template_import._load_carried_metadata(p.team, "prod")
    assert gen.TEMPLATE_SNAPSHOT_KEY not in carried
    assert carried["odoo_version"] == "19.0"


def test_version_mismatch_leaves_data_intact(prepared):
    p = prepared
    p.container.labels[p.settings.image_label] = "odoo:18.0"
    with pytest.raises(PrerequisiteNotMetError, match="version mismatch"):
        refresh(p)
    p.sql.assert_not_called()


def test_cleanup_failure_retains_journal_and_new_generation(prepared, monkeypatch):
    p = prepared

    def sql(*args, **kwargs):
        if args[2].startswith('DROP DATABASE IF EXISTS "oduflow_previous_'):
            raise RuntimeError("cleanup failed")
        return ""

    p.sql.side_effect = sql
    with pytest.raises(RuntimeError, match="cleanup failed"):
        refresh(p)
    assert Path(p.paths["merged"], "new").exists()
    assert (
        Path(get_workspace_path("dev", p.team.workspaces_dir)) / gen.JOURNAL
    ).exists()
    gen.collect(p.team)
    assert gen._generation_dir(p.team, p.generation).exists()
    with pytest.raises(PrerequisiteNotMetError, match="recovery"):
        env_ops.delete_environment(p.settings, p.team, "dev")


@pytest.mark.parametrize("fail_remount", [False, True])
def test_switch_to_overlay_and_rollback_keep_matching_data(
    prepared, monkeypatch, fail_remount
):
    p = prepared
    mounted = set()

    def state(path):
        return env_ops.MOUNT_ALIVE if path in mounted else env_ops.MOUNT_ABSENT

    def unmount(name, team, *, paths_override=None):
        target = (paths_override or p.paths)["merged"]
        if target in mounted:
            shutil.rmtree(target)
            Path(target).mkdir()
            mounted.remove(target)

    def mount(client, settings, team, name, db, image, volumes, **kwargs):
        paths = kwargs.get("paths_override", p.paths)
        if paths is p.paths and fail_remount:
            raise RuntimeError("remount failed")
        lower, _ = gen.lower_paths(team, name, "prod", kwargs.get("generation", ""))
        for target in paths.values():
            Path(target).mkdir(parents=True, exist_ok=True)
        shutil.copytree(lower, paths["merged"], dirs_exist_ok=True)
        mounted.add(paths["merged"])
        volumes[paths["merged"]] = {"bind": "/filestore", "mode": "rw"}

    monkeypatch.setattr(env_ops, "overlay_mount_state", state)
    monkeypatch.setattr(env_ops, "_unmount_filestore", unmount)
    monkeypatch.setattr(env_ops, "_mount_filestore", mount)
    if fail_remount:
        with pytest.raises(RuntimeError, match="remount failed"):
            refresh(p)
        assert Path(p.paths["merged"], "old").exists()
        assert p.paths["merged"] not in mounted
    else:
        refresh(p)
        assert Path(p.paths["merged"], "new").exists()
        assert p.paths["merged"] in mounted
        assert Path(p.paths["upper"]).is_dir()
    assert not (
        Path(get_workspace_path("dev", p.team.workspaces_dir)) / gen.JOURNAL
    ).exists()


def test_deleting_a_pinned_environment_frees_its_generation(tmp_path, monkeypatch):
    import docker as _docker

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    Path(team.get_template_filestore_path("prod")).mkdir(parents=True)
    kept, freed = gen.snapshot(team, "prod"), gen.snapshot(team, "prod")
    for name, generation in (("keep", kept), ("gone", freed)):
        Path(get_workspace_path(name, team.workspaces_dir)).mkdir(parents=True)
        atomic_write_private_json(
            str(gen._marker(team, name)), {"generation": generation}
        )

    client = Mock()
    client.containers.get.side_effect = _docker.errors.NotFound("gone")
    monkeypatch.setattr(env_ops, "is_protected", lambda *a, **k: False)
    monkeypatch.setattr(env_ops, "get_client", lambda: client)
    monkeypatch.setattr(env_ops, "_agent_remove_env", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "release_port", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_exec_sql", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "load_credentials", lambda *a, **k: {"pg_user": "x"})
    monkeypatch.setattr(env_ops, "_drop_pg_role", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_unmount_filestore", lambda *a, **k: None)

    env_ops.delete_environment(settings, team, "gone")

    assert not gen._generation_dir(team, freed).exists()
    assert gen._generation_dir(team, kept).exists()
