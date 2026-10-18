"""Retained filestore generations and staged replacement of environment data.

A pin belongs to the workspace, so container updates and daemon restarts keep
it. Only cron sync opts environments into this model; ordinary template
refresh retains its existing remount semantics for unpinned environments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import (
    get_db_name,
    get_filestore_paths,
    get_resource_name,
    get_template_db_name,
    get_workspace_path,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")
PIN = ".template-generation.json"
JOURNAL = ".template-refresh.json"
# The sync snapshot an environment's database was cloned from. The pin's own
# "snapshot" supersedes it once a sync has refreshed the environment.
PROVENANCE = ".template-snapshot.json"
# Template metadata key: the sync snapshot the template currently holds. Every
# import drops it (template_import._load_carried_metadata); a sync records it
# again after a successful import.
TEMPLATE_SNAPSHOT_KEY = "sync_snapshot"


def _marker(team: TeamSettings, env_name: str) -> Path:
    return Path(get_workspace_path(env_name, team.workspaces_dir)) / PIN


def pinned(team: TeamSettings, env_name: str) -> dict[str, Any]:
    try:
        data = json.loads(_marker(team, env_name).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PrerequisiteNotMetError(
            f"Unreadable filestore generation pin for {env_name}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not re.fullmatch(
        r"[a-f0-9]{32}", str(data.get("generation", ""))
    ):
        raise PrerequisiteNotMetError(f"Invalid filestore generation for {env_name}")
    return data


def is_pinned(team: TeamSettings, env_name: str) -> bool:
    """Whether automatic remounts must leave *env_name* alone.

    A corrupt pin counts as pinned: its lower layer is unknown, so moving the
    environment anywhere could pair its database with the wrong attachments.
    """
    try:
        return bool(pinned(team, env_name))
    except PrerequisiteNotMetError as exc:
        logger.error("%s; leaving its filestore alone", exc)
        return True


def has_pin(team: TeamSettings, env_name: str) -> bool:
    return _marker(team, env_name).exists()


def unpin(team: TeamSettings, env_name: str) -> None:
    """Let *env_name* follow the template's live filestore again."""
    _marker(team, env_name).unlink(missing_ok=True)


def record_provenance(team: TeamSettings, env_name: str, template: str) -> None:
    """Remember which sync snapshot a freshly cloned database came from."""
    try:
        metadata = json.loads(
            Path(team.get_template_metadata_path(template)).read_text()
        )
    except (OSError, ValueError):
        return
    snapshot = (
        metadata.get(TEMPLATE_SNAPSHOT_KEY) if isinstance(metadata, dict) else None
    )
    if isinstance(snapshot, str) and snapshot:
        atomic_write_private_json(
            str(Path(get_workspace_path(env_name, team.workspaces_dir)) / PROVENANCE),
            {"snapshot": snapshot},
        )


def data_snapshot(team: TeamSettings, env_name: str) -> str:
    """The sync snapshot *env_name*'s data matches; empty when unknown."""
    snapshot = pinned(team, env_name).get("snapshot")
    if snapshot:
        return str(snapshot)
    path = Path(get_workspace_path(env_name, team.workspaces_dir)) / PROVENANCE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str(data.get("snapshot") or "") if isinstance(data, dict) else ""


def record_template_snapshot(team: TeamSettings, template: str, snapshot: str) -> None:
    path = team.get_template_metadata_path(template)
    with open(path) as f:
        metadata = json.load(f)
    metadata[TEMPLATE_SNAPSHOT_KEY] = snapshot
    atomic_write_private_json(path, metadata)


def _generation_dir(team: TeamSettings, generation: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise ValueError("Invalid filestore generation")
    return Path(team.data_dir) / "template_generations" / generation


def lower_paths(
    team: TeamSettings, env_name: str, template: str, generation: str = ""
) -> tuple[str, str]:
    generation = generation or pinned(team, env_name).get("generation", "")
    if generation:
        root = _generation_dir(team, generation)
        if not (root / "filestore").is_dir():
            raise PrerequisiteNotMetError(
                f"Retained filestore missing for {env_name}: {root}"
            )
        return str(root / "filestore"), str(root / "metadata.json")
    return team.get_template_filestore_path(template), team.get_template_metadata_path(
        template
    )


def snapshot(team: TeamSettings, template: str) -> str:
    generation = uuid.uuid4().hex
    root = _generation_dir(team, generation)
    root.mkdir(parents=True, mode=0o700)
    source = Path(team.get_template_filestore_path(template))
    try:
        if source.is_dir():
            # Odoo attachment names are content hashes. Our imports replace
            # files by rename and never edit these inodes in place.
            shutil.copytree(source, root / "filestore", copy_function=os.link)
        else:
            (root / "filestore").mkdir()
        metadata = Path(team.get_template_metadata_path(template))
        atomic_write_private_json(
            str(root / "metadata.json"),
            json.loads(metadata.read_text()) if metadata.exists() else {},
        )
    except Exception:
        shutil.rmtree(root)
        raise
    return generation


def pin_existing(
    client: Any, settings: Settings, team: TeamSettings, template: str
) -> None:
    from oduflow.docker_ops import env_ops

    candidates = [
        e["env_name"]
        for e in env_ops.list_environments(settings, team)
        if e.get("template_name") == template
        and not is_pinned(team, e["env_name"])
        and (
            env_ops.overlay_mount_state(
                get_filestore_paths(e["env_name"], team.workspaces_dir)["merged"]
            )
            != env_ops.MOUNT_ABSENT
            or os.path.isdir(
                get_filestore_paths(e["env_name"], team.workspaces_dir)["upper"]
            )
        )
    ]
    if not candidates:
        return
    generation = snapshot(team, template)
    with env_ops.remount_template_overlays(client, settings, team, template) as result:
        if result.failures:
            raise PrerequisiteNotMetError(
                f"Could not detach filestores: {result.failures}"
            )
        for name in candidates:
            atomic_write_private_json(
                str(_marker(team, name)), {"generation": generation}
            )
    if result.failures:
        raise PrerequisiteNotMetError(f"Could not pin filestores: {result.failures}")


def collect(team: TeamSettings) -> None:
    """Delete generations no workspace pins; incomplete recovery journals inhibit GC.

    Callers hold the team gate, exclusively or shared: either way no sync runs,
    so no freshly snapshotted generation is still waiting for its pins.
    """
    workspaces = Path(team.workspaces_dir)
    if list(workspaces.glob("*/" + JOURNAL)):
        return
    referenced = set()
    for path in workspaces.glob("*/" + PIN):
        # Corrupt state must retain data rather than risk deleting a live lower.
        data = json.loads(path.read_text())
        referenced.add(data["generation"])
    root = Path(team.data_dir) / "template_generations"
    if root.exists():
        for path in root.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", path.name) and path.name not in referenced:
                # Environment deletions collect under the shared team gate, so
                # two of them may race over the same unreferenced generation.
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    pass


def collect_best_effort(team: TeamSettings) -> None:
    """:func:`collect` after work that must not fail on its cleanup."""
    try:
        collect(team)
    except Exception as exc:  # noqa: BLE001 - corrupt pins retain data; logged
        logger.warning("Could not collect retained filestore generations: %s", exc)


def require_no_recovery(team: TeamSettings, env_name: str) -> None:
    path = Path(get_workspace_path(env_name, team.workspaces_dir)) / JOURNAL
    if path.exists():
        raise PrerequisiteNotMetError(
            f"Interrupted template refresh needs recovery; inspect {path} before starting or updating this environment"
        )


def refresh_environment(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    template: str,
    generation: str,
    snapshot_id: str,
) -> None:
    from oduflow.docker_ops import env_ops, system_ops
    from oduflow.env_credentials import load_credentials
    from oduflow.odoo_version import major_from_container_labels
    from oduflow.sanitizer import neutralize_environment, sanitize_environment

    require_no_recovery(team, env_name)
    old_pin = pinned(team, env_name)
    if data_snapshot(team, env_name) == snapshot_id:
        logger.info("Environment %s already has snapshot %s", env_name, snapshot_id)
        return
    container = client.containers.get(
        get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    )
    container.reload()
    was_running = container.status == "running"
    restart_policy = container.attrs["HostConfig"].get("RestartPolicy", {"Name": "no"})
    image = container.attrs["Config"]["Image"]
    metadata = json.loads(
        (_generation_dir(team, generation) / "metadata.json").read_text()
    )
    expected = str(metadata.get("odoo_version", "")).split(".")[0]
    actual = major_from_container_labels(container, settings.image_label)
    if expected.isdigit() and actual is not None and int(expected) != actual:
        raise PrerequisiteNotMetError(
            f"Odoo version mismatch: template {expected}, environment {actual}"
        )
    paths = get_filestore_paths(env_name, team.workspaces_dir)
    env_db = get_db_name(env_name, team.team_id)
    mount_destination = f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}"
    if not any(
        m.get("Destination") == mount_destination and m.get("Source") == paths["merged"]
        for m in container.attrs.get("Mounts", [])
    ):
        raise PrerequisiteNotMetError(
            "Environment has no managed filestore mount; recreate it from a template with a filestore first"
        )
    creds = load_credentials(
        env_name,
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=False,
    )
    system_ops.check_disk_space(
        client,
        settings,
        team,
        template,
        estimated_db_bytes=system_ops.estimate_new_db_bytes(
            client, settings, team, template
        ),
    )
    token = uuid.uuid4().hex
    staged_db, backup_db = "oduflow_sync_" + token, "oduflow_previous_" + token
    workspace = Path(get_workspace_path(env_name, team.workspaces_dir))
    stage = workspace / (".template-stage-" + token)
    stage.mkdir(mode=0o700)
    stage_paths = {key: str(stage / Path(value).name) for key, value in paths.items()}
    previous_paths = {
        key: str(stage / ("previous-" + Path(value).name))
        for key, value in paths.items()
    }
    journal = workspace / JOURNAL
    runner = None
    swapped_db = False
    moved: list[str] = []
    installed: list[str] = []
    committed = False
    stopped = False
    recovery_failed = False
    original_overlay = (
        env_ops.overlay_mount_state(paths["merged"]) != env_ops.MOUNT_ABSENT
    )

    def sql(statement: str) -> str:
        return system_ops._exec_sql(client, settings, statement)

    def unmount(target: dict[str, str]) -> None:
        env_ops._unmount_filestore(env_name, team, paths_override=target)
        env_ops._wait_unmounted(target["merged"])
        if env_ops.overlay_mount_state(target["merged"]) != env_ops.MOUNT_ABSENT:
            raise PrerequisiteNotMetError(
                f"Filestore is still mounted: {target['merged']}"
            )

    def freeze(db: str) -> None:
        sql(f'ALTER DATABASE "{db}" ALLOW_CONNECTIONS false;')
        sql(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{db}' AND pid <> pg_backend_pid();"
        )

    try:
        # Record enough to recover an OS/process crash between the DB and FS
        # switches. Do not silently start an environment with this marker.
        atomic_write_private_json(
            str(journal),
            dict(
                env_db=env_db,
                staged_db=staged_db,
                backup_db=backup_db,
                generation=generation,
                old_pin=old_pin,
                stage=str(stage),
                was_running=was_running,
                restart_policy=restart_policy,
            ),
        )
        tpl_db = get_template_db_name(template, team.team_id)
        tablespace = system_ops.ensure_team_tablespace(client, settings, team)
        strategy = system_ops.pg_clone_strategy_clause(client, settings)
        sql(
            f'CREATE DATABASE "{staged_db}" TEMPLATE "{tpl_db}"{strategy} TABLESPACE "{tablespace}" OWNER "{creds["pg_user"]}";'
        )
        system_ops.reassign_db_ownership(client, settings, staged_db, creds["pg_user"])
        system_ops.drop_signaling_sequences(client, settings, staged_db)
        volumes: dict[str, Any] = {}
        env_ops._mount_filestore(
            client,
            settings,
            team,
            env_name,
            staged_db,
            image,
            volumes,
            template_name=template,
            generation=generation,
            paths_override=stage_paths,
        )
        staged_overlay = (
            env_ops.overlay_mount_state(stage_paths["merged"]) != env_ops.MOUNT_ABSENT
        )
        # Copy the serving container's code/config mounts into a maintenance
        # container with no Odoo server process, so the new database cannot send
        # mail or run cron before neutralization. Its filestore is staged too.
        for mount in container.attrs.get("Mounts", []):
            if mount["Destination"].startswith("/var/lib/odoo"):
                continue
            volumes[mount["Source"]] = {"bind": mount["Destination"], "mode": "ro"}
        runner = client.containers.run(
            image,
            ["infinity"],
            entrypoint="/bin/sleep",
            detach=True,
            name="oduflow-sanitize-" + token,
            volumes=volumes,
            network=container.attrs["HostConfig"]["NetworkMode"],
            environment=container.attrs["Config"].get("Env", []),
            user=container.attrs["Config"].get("User") or None,
            labels={
                settings.image_label: container.labels.get(settings.image_label, image)
            },
        )
        # Provisioning may have copied odoo.conf into the writable container
        # layer rather than mounting it. Use that exact configuration here too.
        if not any(
            m["Destination"] == "/etc/odoo" for m in container.attrs.get("Mounts", [])
        ):
            archive, _ = container.get_archive("/etc/odoo")
            runner.put_archive("/etc", b"".join(archive))
        dependency_paths = [
            (m["Source"], m["Destination"])
            for m in container.attrs.get("Mounts", [])
            if m["Destination"].startswith("/mnt/extra-addons")
        ]
        dep_code, _, dep_logs = env_ops._install_repo_dependencies(
            runner, dependency_paths
        )
        for line in dep_logs:
            logger.info("%s: %s", env_name, line)
        if dep_code:
            raise PrerequisiteNotMetError(
                "Could not prepare the sanitization container's dependencies"
            )
        logs = neutralize_environment(
            client, settings, team, env_name, runner=runner, database=staged_db
        )
        if any("WARNING:" in line for line in logs):
            raise PrerequisiteNotMetError(
                "Database neutralization failed; keeping the previous environment data"
            )
        modules = container.labels.get("oduflow.auto_install_modules", "")
        command = [
            "/entrypoint.sh",
            "odoo",
            "-d",
            staged_db,
            "--stop-after-init",
            "--no-http",
            "--max-cron-threads=0",
        ]
        if modules:
            from oduflow.docker_ops.odoo_ops import _validate_module_names

            _validate_module_names(modules.split(","))
            command.extend(["-i", modules])
        code, output = runner.exec_run(command)
        if code:
            raise PrerequisiteNotMetError(
                f"Staged Odoo initialization failed: {output[-4000:]!r}"
            )
        # New modules can bring new outbound integrations and cron records.
        logs += neutralize_environment(
            client, settings, team, env_name, runner=runner, database=staged_db
        )
        logs += sanitize_environment(
            client, settings, team, env_name, runner=runner, database=staged_db
        )
        for line in logs:
            logger.info("%s: %s", env_name, line)
        if any("WARNING:" in line for line in logs):
            raise PrerequisiteNotMetError(
                "Sanitization did not complete cleanly; keeping the previous environment data"
            )
        runner.remove(force=True)
        runner = None
        unmount(stage_paths)
        container.update(restart_policy={"Name": "no"})
        stopped = True
        container.stop(timeout=30)
        unmount(paths)
        freeze(env_db)
        freeze(staged_db)
        # One psql command/transaction keeps the pair of renames atomic.
        sql(
            f'ALTER DATABASE "{env_db}" RENAME TO "{backup_db}"; ALTER DATABASE "{staged_db}" RENAME TO "{env_db}";'
        )
        swapped_db = True
        for key, path in paths.items():
            if os.path.exists(path):
                os.rename(path, previous_paths[key])
                moved.append(key)
            if os.path.exists(stage_paths[key]):
                os.rename(stage_paths[key], path)
                installed.append(key)
        atomic_write_private_json(
            str(_marker(team, env_name)),
            {"generation": generation, "snapshot": snapshot_id},
        )
        if staged_overlay:
            env_ops._mount_filestore(
                client,
                settings,
                team,
                env_name,
                env_db,
                image,
                {},
                template_name=template,
                force_overlay=True,
            )
        sql(f'ALTER DATABASE "{env_db}" ALLOW_CONNECTIONS true;')
        if was_running:
            container.start()
            readiness = env_ops._wait_for_container_odoo_ready(container, env_db)
            if not readiness.ready:
                raise PrerequisiteNotMetError(
                    "Refreshed Odoo did not become ready; rolling back"
                )
        container.update(restart_policy=restart_policy)
        committed = True
        logger.info("Refreshed environment %s from snapshot %s", env_name, snapshot_id)
    except Exception:
        if stopped:
            try:
                container.stop(timeout=30)
                unmount(paths)
                if swapped_db:
                    freeze(env_db)
                    sql(
                        f'ALTER DATABASE "{env_db}" RENAME TO "{staged_db}"; ALTER DATABASE "{backup_db}" RENAME TO "{env_db}";'
                    )
                    swapped_db = False
                for key in installed:
                    if os.path.isdir(paths[key]):
                        shutil.rmtree(paths[key])
                for key in moved:
                    os.rename(previous_paths[key], paths[key])
                if old_pin:
                    atomic_write_private_json(str(_marker(team, env_name)), old_pin)
                else:
                    _marker(team, env_name).unlink(missing_ok=True)
                if original_overlay:
                    env_ops._mount_filestore(
                        client,
                        settings,
                        team,
                        env_name,
                        env_db,
                        image,
                        {},
                        template_name=template,
                        force_overlay=True,
                    )
                sql(f'ALTER DATABASE "{env_db}" ALLOW_CONNECTIONS true;')
                if was_running:
                    container.start()
                container.update(restart_policy=restart_policy)
            except Exception:
                recovery_failed = True
                logger.exception("Rollback needs manual recovery: %s", journal)
        raise
    finally:
        if runner is not None:
            runner.remove(force=True)
        if not recovery_failed:
            unmount(stage_paths)
            sql(
                f'DROP DATABASE IF EXISTS "{backup_db if committed else staged_db}" WITH (FORCE);'
            )
            shutil.rmtree(stage)
            journal.unlink(missing_ok=True)
