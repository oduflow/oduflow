"""Detection and repair of stale ``fuse-overlayfs`` filestore mounts.

When the fuse-overlayfs daemon for an overlay filestore dies, the kernel keeps
the mount table entry and every access to the mountpoint fails with ENOTCONN.
Restarting Oduflow *in Docker* does exactly that to every overlay environment
at once: the daemon lives in the container's PID namespace and dies with it,
while the mount lives in the host's and survives.

The trap these tests pin down is that ``os.path.ismount()`` swallows the
ENOTCONN from its own ``lstat`` and reports False — indistinguishable from "not
mounted" — so every guard written on top of it read a stale overlay as absent
and left it broken.
"""

from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import docker
from oduflow.docker_ops import env_ops
from oduflow.naming import get_filestore_paths
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def stale(monkeypatch):
    """Make chosen paths behave like the mountpoint of a dead FUSE mount."""
    dead: set[str] = set()
    real_stat, real_lstat = os.stat, os.lstat

    def _raise_if_dead(path, real):
        if str(path) in dead:
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
        return real(path)

    monkeypatch.setattr(
        env_ops.os, "stat", lambda p, **kw: _raise_if_dead(p, real_stat)
    )
    monkeypatch.setattr(
        env_ops.os, "lstat", lambda p, **kw: _raise_if_dead(p, real_lstat)
    )
    return dead


def _team(workspaces_dir: str):
    team = MagicMock()
    team.team_id = "1"
    team.workspaces_dir = workspaces_dir
    return team


class TestOverlayMountState:
    def test_absent_when_path_missing(self, tmp_path):
        assert (
            env_ops.overlay_mount_state(str(tmp_path / "nope")) == env_ops.MOUNT_ABSENT
        )

    def test_absent_for_plain_directory(self, tmp_path):
        # Copy-mode filestores are real directories, not mounts.
        (tmp_path / "filestore").mkdir()
        state = env_ops.overlay_mount_state(str(tmp_path / "filestore"))
        assert state == env_ops.MOUNT_ABSENT

    def test_alive_when_mounted(self, tmp_path, monkeypatch):
        (tmp_path / "filestore").mkdir()
        monkeypatch.setattr(env_ops.os.path, "ismount", lambda p: True)
        assert (
            env_ops.overlay_mount_state(str(tmp_path / "filestore"))
            == env_ops.MOUNT_ALIVE
        )

    def test_stale_when_daemon_is_gone(self, tmp_path, stale):
        merged = str(tmp_path / "filestore")
        stale.add(merged)
        assert env_ops.overlay_mount_state(merged) == env_ops.MOUNT_STALE

    def test_unexpected_probe_error_is_not_treated_as_absent(
        self, tmp_path, monkeypatch
    ):
        merged = str(tmp_path / "filestore")
        monkeypatch.setattr(
            env_ops.os,
            "stat",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )

        with pytest.raises(PermissionError, match="denied"):
            env_ops.overlay_mount_state(merged)

    def test_ismount_alone_cannot_tell_stale_from_absent(self, tmp_path, stale):
        """The reason every guard here probes instead of calling ismount()."""
        merged = str(tmp_path / "filestore")
        stale.add(merged)
        assert os.path.ismount(merged) is False
        assert os.path.isdir(merged) is False
        assert env_ops.overlay_mount_state(merged) == env_ops.MOUNT_STALE


class TestUnmountStale:
    def test_detaches_a_stale_mount(self, tmp_path, stale, monkeypatch):
        merged = get_filestore_paths("env1", str(tmp_path))["merged"]
        stale.add(merged)
        calls = []
        monkeypatch.setattr(
            env_ops.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
        )

        env_ops._unmount_filestore("env1", _team(str(tmp_path)))

        # Previously this returned early (isdir/ismount both False) and the
        # stale mount could only be cleared by hand.
        assert calls == [["umount", merged]]


class TestMountFilestoreClearsStale:
    def test_stale_mount_is_detached_before_remount(self, tmp_path, stale, monkeypatch):
        template_fs = tmp_path / "template" / "filestore"
        template_fs.mkdir(parents=True)
        metadata = tmp_path / "template" / "metadata.json"
        metadata.write_text(json.dumps({"use_overlay": False}))

        team = _team(str(tmp_path / "workspaces"))
        team.get_template_filestore_path.return_value = str(template_fs)
        team.get_template_metadata_path.return_value = str(metadata)

        merged = get_filestore_paths("env1", team.workspaces_dir)["merged"]
        stale.add(merged)

        order = []
        monkeypatch.setattr(
            env_ops, "_unmount_filestore", lambda *a, **k: order.append("unmount")
        )
        monkeypatch.setattr(
            env_ops, "_wait_unmounted", lambda *a, **k: stale.discard(merged)
        )
        monkeypatch.setattr(env_ops, "get_odoo_uid_gid", lambda *a, **k: "101:101")
        monkeypatch.setattr(env_ops, "chown_recursive", lambda *a, **k: None)
        monkeypatch.setattr(
            env_ops.shutil, "copytree", lambda *a, **k: order.append("copytree")
        )

        volumes: dict = {}
        env_ops._mount_filestore(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "env1",
            "db1",
            "odoo:19.0",
            volumes,
            template_name="tpl",
        )

        # The detach has to happen first: copying into a dead mountpoint would
        # itself fail with ENOTCONN.
        assert order == ["unmount", "copytree"]
        assert merged in volumes

    def test_forced_recovery_ignores_copy_metadata(self, tmp_path, stale, monkeypatch):
        template_fs = tmp_path / "template" / "filestore"
        template_fs.mkdir(parents=True)
        metadata = tmp_path / "template" / "metadata.json"
        metadata.write_text(json.dumps({"use_overlay": False}))

        team = _team(str(tmp_path / "workspaces"))
        team.get_template_filestore_path.return_value = str(template_fs)
        team.get_template_metadata_path.return_value = str(metadata)
        merged = get_filestore_paths("env1", team.workspaces_dir)["merged"]
        os.makedirs(
            get_filestore_paths("env1", team.workspaces_dir)["upper"], exist_ok=True
        )
        stale.add(merged)
        live: set[str] = set()

        monkeypatch.setattr(env_ops, "_unmount_filestore", lambda *a, **k: None)
        monkeypatch.setattr(
            env_ops, "_wait_unmounted", lambda *a, **k: stale.discard(merged)
        )
        monkeypatch.setattr(env_ops, "get_odoo_uid_gid", lambda *a, **k: "101:101")
        monkeypatch.setattr(env_ops, "chown_recursive", lambda *a, **k: None)
        monkeypatch.setattr(env_ops.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(env_ops.os.path, "ismount", lambda path: str(path) in live)
        copytree = MagicMock()
        monkeypatch.setattr(env_ops.shutil, "copytree", copytree)

        def fake_run(*a, **k):
            live.add(merged)
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr(env_ops.subprocess, "run", fake_run)

        env_ops._mount_filestore(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "env1",
            "db1",
            "odoo:19.0",
            {},
            template_name="tpl",
            force_overlay=True,
        )

        copytree.assert_not_called()
        assert merged in live


class TestStaleOverlayPaths:
    def test_finds_stale_mounts_across_teams(self, tmp_path, stale):
        team_dir = tmp_path / "team_1"
        workspaces = team_dir / "workspaces"
        for name in ("broken", "healthy"):
            (workspaces / name).mkdir(parents=True)
            (workspaces / name / "filestore").mkdir()
        broken = str(workspaces / "broken" / "filestore")
        stale.add(broken)

        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
        )

        assert env_ops.stale_overlay_paths(settings) == [broken]

    def test_empty_when_team_never_initialized(self, tmp_path):
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(tmp_path / "gone"))},
        )
        assert env_ops.stale_overlay_paths(settings) == []

    def test_ignores_non_workspace_files(self, tmp_path):
        team_dir = tmp_path / "team_1"
        workspaces = team_dir / "workspaces"
        workspaces.mkdir(parents=True)
        (workspaces / ".DS_Store").write_text("not an environment")
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
        )

        assert env_ops.overlay_mount_issues(settings) == []

    def test_missing_expected_overlay_is_an_issue(self, tmp_path):
        team_dir = tmp_path / "team_1"
        workspaces = team_dir / "workspaces"
        paths = get_filestore_paths("missing", str(workspaces))
        os.makedirs(paths["upper"])
        os.makedirs(paths["work"])
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
        )

        assert env_ops.overlay_mount_issues(settings) == [
            {
                "team_id": "1",
                "env_name": "missing",
                "state": env_ops.MOUNT_ABSENT,
                "path": paths["merged"],
            }
        ]

    def test_unreadable_team_directory_is_reported(self, tmp_path, monkeypatch):
        team_dir = tmp_path / "team_1"
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
        )
        monkeypatch.setattr(
            env_ops.os,
            "scandir",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )

        with pytest.raises(PermissionError, match="denied"):
            env_ops.overlay_mount_issues(settings)


class TestReconcileOverlayMounts:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch, stale):
        """Reconciliation with its Docker and mount side effects stubbed out."""
        team_dir = tmp_path / "team_1"
        (team_dir / "workspaces").mkdir(parents=True)
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
        )
        # The lower layer a repair remounts against.
        os.makedirs(
            settings.teams["1"].get_template_filestore_path("tpl"), exist_ok=True
        )
        container = MagicMock()
        container.status = "running"
        container.image.tags = ["odoo:19.0"]
        container.stop.side_effect = lambda **kw: setattr(container, "status", "exited")
        container.start.side_effect = lambda: setattr(container, "status", "running")
        client = MagicMock()
        client.containers.get.return_value = container

        monkeypatch.setattr(env_ops, "get_client", lambda: client)
        unmounted: list[str] = []
        live: set[str] = set()

        def fake_unmount(name, selected_team):
            merged = get_filestore_paths(name, selected_team.workspaces_dir)["merged"]
            unmounted.append(name)
            stale.discard(merged)
            live.discard(merged)

        monkeypatch.setattr(env_ops, "_unmount_filestore", fake_unmount)
        monkeypatch.setattr(env_ops, "_wait_unmounted", lambda *a, **k: None)
        monkeypatch.setattr(env_ops.os.path, "ismount", lambda path: str(path) in live)
        mounted: list = []

        def fake_mount(*a, **k):
            # A successful remount is what stops the mountpoint being stale.
            assert k["force_overlay"] is True
            mounted.append((a[3], k["template_name"]))
            merged = get_filestore_paths(a[3], a[2].workspaces_dir)["merged"]
            os.makedirs(merged, exist_ok=True)
            stale.discard(merged)
            live.add(merged)

        monkeypatch.setattr(env_ops, "_mount_filestore", fake_mount)
        return SimpleNamespace(
            settings=settings,
            client=client,
            container=container,
            mounted=mounted,
            unmounted=unmounted,
            live=live,
        )

    def _envs(self, monkeypatch, *envs):
        def listed(settings, team):
            for env in envs:
                paths = get_filestore_paths(env["env_name"], team.workspaces_dir)
                os.makedirs(paths["upper"], exist_ok=True)
            return list(envs)

        monkeypatch.setattr(env_ops, "list_environments", listed)

    def test_repairs_a_stale_environment(self, wired, stale, monkeypatch):
        settings, container, mounted = wired.settings, wired.container, wired.mounted
        team = settings.teams["1"]
        self._envs(
            monkeypatch,
            {
                "env_name": "wise-moth",
                "template_name": "tpl",
                "odoo_image": "odoo:19.0",
            },
        )
        stale.add(get_filestore_paths("wise-moth", team.workspaces_dir)["merged"])

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result == [
            {"team_id": "1", "env_name": "wise-moth", "repaired": True, "detail": ""}
        ]
        assert mounted == [("wise-moth", "tpl")]
        # The container has to be cycled: its bind mount still points at the
        # dead mount, so a host-side remount alone would not reach it.
        container.stop.assert_called_once()
        container.start.assert_called_once()

    def test_leaves_healthy_environments_alone(self, wired, monkeypatch):
        settings, container, mounted = wired.settings, wired.container, wired.mounted
        self._envs(
            monkeypatch, {"env_name": "fine", "template_name": "tpl", "odoo_image": ""}
        )
        merged = get_filestore_paths("fine", settings.teams["1"].workspaces_dir)[
            "merged"
        ]
        os.makedirs(merged, exist_ok=True)
        wired.live.add(merged)

        assert env_ops.reconcile_overlay_mounts(settings) == []
        assert mounted == []
        container.stop.assert_not_called()

    def test_repairs_an_expected_overlay_that_is_absent(self, wired, monkeypatch):
        settings, mounted = wired.settings, wired.mounted
        team = settings.teams["1"]
        paths = get_filestore_paths("missing", team.workspaces_dir)
        os.makedirs(paths["upper"], exist_ok=True)
        os.makedirs(paths["work"], exist_ok=True)
        self._envs(
            monkeypatch,
            {"env_name": "missing", "template_name": "tpl", "odoo_image": ""},
        )

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is True
        assert mounted == [("missing", "tpl")]

    def test_reports_stale_environment_without_a_template(
        self, wired, stale, monkeypatch
    ):
        settings, container, mounted = wired.settings, wired.container, wired.mounted
        team = settings.teams["1"]
        self._envs(
            monkeypatch,
            {"env_name": "orphan", "template_name": "none", "odoo_image": ""},
        )
        stale.add(get_filestore_paths("orphan", team.workspaces_dir)["merged"])

        result = env_ops.reconcile_overlay_mounts(settings)

        # No lower layer to remount against — flagged for an operator, not
        # silently "repaired".
        assert result == [
            {
                "team_id": "1",
                "env_name": "orphan",
                "repaired": False,
                "detail": "no template to remount against",
            }
        ]
        assert mounted == []
        container.stop.assert_not_called()

    def test_missing_upper_layer_is_not_detached(self, wired, stale, monkeypatch):
        settings = wired.settings
        team = settings.teams["1"]
        monkeypatch.setattr(
            env_ops,
            "list_environments",
            lambda *a: [
                {"env_name": "lost-upper", "template_name": "tpl", "odoo_image": ""}
            ],
        )
        merged = get_filestore_paths("lost-upper", team.workspaces_dir)["merged"]
        stale.add(merged)

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is False
        assert result[0]["detail"] == "overlay upper layer is missing"
        assert wired.unmounted == []
        wired.container.stop.assert_not_called()

    def test_a_failed_repair_does_not_stop_the_pass(self, wired, stale, monkeypatch):
        settings, mounted, live = wired.settings, wired.mounted, wired.live
        team = settings.teams["1"]
        self._envs(
            monkeypatch,
            {"env_name": "a", "template_name": "tpl", "odoo_image": ""},
            {"env_name": "b", "template_name": "tpl", "odoo_image": ""},
        )
        for name in ("a", "b"):
            stale.add(get_filestore_paths(name, team.workspaces_dir)["merged"])

        def _mount(*a, **k):
            if a[3] == "a":
                # The fixture uses one mock object for both names; reset it to
                # model b's distinct running container before continuing.
                wired.container.status = "running"
                raise RuntimeError("mount refused")
            mounted.append(a[3])
            merged = get_filestore_paths(a[3], team.workspaces_dir)["merged"]
            os.makedirs(merged, exist_ok=True)
            stale.discard(merged)
            live.add(merged)

        monkeypatch.setattr(env_ops, "_mount_filestore", _mount)

        result = env_ops.reconcile_overlay_mounts(settings)

        assert [(r["env_name"], r["repaired"]) for r in result] == [
            ("a", False),
            ("b", True),
        ]
        assert result[0]["detail"] == "mount refused"
        assert mounted == ["b"]
        # The failed environment stays stopped rather than starting on an
        # absent filestore.
        assert wired.container.start.call_count == 1

    def test_missing_container_is_not_an_error(self, wired, stale, monkeypatch):
        settings, mounted = wired.settings, wired.mounted
        team = settings.teams["1"]
        wired.client.containers.get.side_effect = docker.errors.NotFound("gone")
        self._envs(
            monkeypatch,
            {"env_name": "c", "template_name": "tpl", "odoo_image": "odoo:19.0"},
        )
        stale.add(get_filestore_paths("c", team.workspaces_dir)["merged"])

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is True
        assert mounted == [("c", "tpl")]

    def test_missing_template_filestore_is_not_touched(self, wired, stale, monkeypatch):
        settings, mounted = wired.settings, wired.mounted
        team = settings.teams["1"]
        self._envs(
            monkeypatch,
            {"env_name": "lost", "template_name": "vanished", "odoo_image": ""},
        )
        stale.add(get_filestore_paths("lost", team.workspaces_dir)["merged"])

        result = env_ops.reconcile_overlay_mounts(settings)

        # Detaching here would trade a stale mount for an empty filestore and
        # still report success, so the environment is left for an operator.
        assert result[0]["repaired"] is False
        assert "template filestore missing" in result[0]["detail"]
        assert wired.unmounted == []
        assert mounted == []

    def test_a_mount_that_stayed_stale_is_not_called_a_repair(
        self, wired, stale, monkeypatch
    ):
        settings = wired.settings
        team = settings.teams["1"]
        self._envs(
            monkeypatch, {"env_name": "stuck", "template_name": "tpl", "odoo_image": ""}
        )
        merged = get_filestore_paths("stuck", team.workspaces_dir)["merged"]
        stale.add(merged)
        mount = MagicMock()
        monkeypatch.setattr(env_ops, "_unmount_filestore", lambda *a, **k: None)
        monkeypatch.setattr(env_ops, "_mount_filestore", mount)

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is False
        assert result[0]["detail"] == "mount did not detach"
        mount.assert_not_called()
        wired.container.start.assert_not_called()

    def test_stop_failure_does_not_touch_the_mount(self, wired, stale, monkeypatch):
        settings = wired.settings
        team = settings.teams["1"]
        self._envs(
            monkeypatch, {"env_name": "busy", "template_name": "tpl", "odoo_image": ""}
        )
        stale.add(get_filestore_paths("busy", team.workspaces_dir)["merged"])
        wired.container.stop.side_effect = RuntimeError("timeout")
        mount = MagicMock()
        monkeypatch.setattr(env_ops, "_mount_filestore", mount)

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is False
        assert result[0]["detail"] == "stop failed: timeout"
        assert wired.unmounted == []
        mount.assert_not_called()
        wired.container.start.assert_not_called()

    def test_restart_failure_is_reported(self, wired, stale, monkeypatch):
        settings = wired.settings
        team = settings.teams["1"]
        self._envs(
            monkeypatch,
            {"env_name": "restart", "template_name": "tpl", "odoo_image": ""},
        )
        stale.add(get_filestore_paths("restart", team.workspaces_dir)["merged"])
        wired.container.start.side_effect = RuntimeError("start refused")

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is False
        assert result[0]["detail"] == "restart failed: start refused"

    def test_uses_team_scoped_database_name(self, tmp_path, monkeypatch, stale):
        team_dir = tmp_path / "team_2"
        (team_dir / "workspaces").mkdir(parents=True)
        team = TeamSettings(team_id="2", data_dir=str(team_dir))
        settings = Settings(base_data_dir=str(tmp_path), teams={"2": team})
        os.makedirs(team.get_template_filestore_path("tpl"), exist_ok=True)
        merged = get_filestore_paths("same-name", team.workspaces_dir)["merged"]
        os.makedirs(get_filestore_paths("same-name", team.workspaces_dir)["upper"])
        stale.add(merged)
        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("gone")
        monkeypatch.setattr(env_ops, "get_client", lambda: client)
        monkeypatch.setattr(
            env_ops,
            "list_environments",
            lambda *a: [
                {"env_name": "same-name", "template_name": "tpl", "odoo_image": ""}
            ],
        )
        monkeypatch.setattr(
            env_ops,
            "_unmount_filestore",
            lambda *a: stale.discard(merged),
        )
        monkeypatch.setattr(env_ops, "_wait_unmounted", lambda *a: None)
        live: set[str] = set()
        monkeypatch.setattr(env_ops.os.path, "ismount", lambda path: str(path) in live)
        captured: dict[str, str] = {}

        def mount(*args, **kwargs):
            captured["db"] = args[4]
            os.makedirs(merged, exist_ok=True)
            live.add(merged)

        monkeypatch.setattr(env_ops, "_mount_filestore", mount)

        result = env_ops.reconcile_overlay_mounts(settings)

        assert result[0]["repaired"] is True
        assert captured["db"] == "oduflow_2_same-name"


class TestEnsureOverlayMounted:
    """P-H5: a start-time remount closes the gap the startup reconciliation
    (reconcile_overlay_mounts, run only at server start) does not cover — an
    overlay that went absent/stale mid-session, then start_environment is called.
    """

    def _team_ws(self, tmp_path):
        ws = tmp_path / "workspaces"
        return _team(str(ws)), str(ws)

    def test_remounts_when_mount_absent_and_upper_exists(self, tmp_path, monkeypatch):
        team, ws = self._team_ws(tmp_path)
        os.makedirs(get_filestore_paths("feature-x", ws)["upper"])  # overlay marker
        seen: dict = {}
        monkeypatch.setattr(
            env_ops,
            "_mount_filestore",
            lambda *a, **k: seen.update(
                force=k.get("force_overlay"), template=k.get("template_name")
            ),
        )

        result = env_ops.ensure_overlay_mounted(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "feature-x",
            {"oduflow.template": "base"},
            "odoo:19.0",
        )

        assert result is True
        assert seen == {"force": True, "template": "base"}

    def test_noop_when_mount_alive(self, tmp_path, monkeypatch):
        team, ws = self._team_ws(tmp_path)
        os.makedirs(get_filestore_paths("feature-x", ws)["upper"])
        monkeypatch.setattr(
            env_ops, "overlay_mount_state", lambda p: env_ops.MOUNT_ALIVE
        )
        called: list = []
        monkeypatch.setattr(
            env_ops, "_mount_filestore", lambda *a, **k: called.append(1)
        )

        result = env_ops.ensure_overlay_mounted(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "feature-x",
            {"oduflow.template": "base"},
            "odoo:19.0",
        )

        assert result is False
        assert called == []

    def test_noop_for_copy_mode_without_upper(self, tmp_path, monkeypatch):
        team, _ = self._team_ws(tmp_path)
        called: list = []
        monkeypatch.setattr(
            env_ops, "_mount_filestore", lambda *a, **k: called.append(1)
        )

        result = env_ops.ensure_overlay_mounted(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "feature-x",
            {"oduflow.template": "base"},
            "odoo:19.0",
        )

        assert result is False
        assert called == []

    def test_failed_remount_aborts_start(self, tmp_path, monkeypatch):
        # start_environment must not start Odoo over an unmounted merged dir:
        # attachments would be missing and new writes hidden by a later
        # successful remount. A failed remount is a prerequisite failure.
        from oduflow.errors import PrerequisiteNotMetError

        team, _ = self._team_ws(tmp_path)
        settings = Settings(base_data_dir=str(tmp_path))
        db = MagicMock(status="running")
        odoo = MagicMock()
        odoo.labels = {"oduflow.team": "1", "oduflow.template": "base"}
        client = MagicMock()
        client.containers.get.side_effect = lambda name: (
            db if name == settings.shared_db_container else odoo
        )
        monkeypatch.setattr(env_ops, "get_client", lambda: client)
        monkeypatch.setattr(env_ops, "_assert_team_owns", lambda *a, **k: None)

        def boom(*a, **k):
            raise RuntimeError("fuse-overlayfs unavailable")

        monkeypatch.setattr(env_ops, "ensure_overlay_mounted", boom)

        with pytest.raises(PrerequisiteNotMetError, match="overlay"):
            env_ops.start_environment(settings, "feature-x", team)

        odoo.start.assert_not_called()

    def test_noop_when_env_has_no_template(self, tmp_path, monkeypatch):
        team, ws = self._team_ws(tmp_path)
        os.makedirs(get_filestore_paths("feature-x", ws)["upper"])
        called: list = []
        monkeypatch.setattr(
            env_ops, "_mount_filestore", lambda *a, **k: called.append(1)
        )

        result = env_ops.ensure_overlay_mounted(
            MagicMock(),
            Settings(base_data_dir=str(tmp_path)),
            team,
            "feature-x",
            {"oduflow.template": "none"},
            "odoo:19.0",
        )

        assert result is False
        assert called == []
