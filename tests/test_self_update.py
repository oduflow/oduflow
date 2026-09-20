"""Tests for oduflow.self_update — no network, no real subprocesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oduflow import self_update
from oduflow.updates import (
    STATUS_CURRENT,
    STATUS_ERROR,
    STATUS_UPDATE,
    UpdateCheck,
)


class _FakeDistribution:
    """Just enough of importlib.metadata.Distribution for detect_install."""

    def __init__(self, location: str, direct_url: dict | None = None) -> None:
        self._location = location
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        if name == "direct_url.json" and self._direct_url is not None:
            return json.dumps(self._direct_url)
        return None

    def locate_file(self, path: str) -> str:
        return self._location + path


class _Run:
    """Record subprocess.run invocations and answer with canned exit codes."""

    def __init__(self, returncodes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return SimpleNamespace(returncode=self._match(cmd), stdout="9.0.0\n")

    def _match(self, cmd) -> int:
        for marker, code in self._returncodes.items():
            if any(marker in part for part in cmd):
                return code
        return 0


class TestInContainer:
    def test_marker_file_means_container(self, tmp_path):
        marker = tmp_path / ".dockerenv"
        marker.touch()
        with patch.object(self_update, "_CONTAINER_MARKERS", (str(marker),)):
            assert self_update.in_container() is True

    def test_plain_host(self, tmp_path):
        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/init.scope\n")
        with (
            patch.object(self_update, "_CONTAINER_MARKERS", ()),
            patch("builtins.open", lambda *a, **k: cgroup.open()),
        ):
            assert self_update.in_container() is False

    def test_docker_cgroup(self, tmp_path):
        cgroup = tmp_path / "cgroup"
        cgroup.write_text("12:pids:/docker/abcdef\n")
        with (
            patch.object(self_update, "_CONTAINER_MARKERS", ()),
            patch("builtins.open", lambda *a, **k: cgroup.open()),
        ):
            assert self_update.in_container() is True


class TestDetectInstall:
    def test_source_checkout_refused(self):
        with patch.object(
            self_update, "distribution", side_effect=PackageNotFoundError
        ):
            info = self_update.detect_install()
        assert info.kind == "source"
        assert info.command is None

    def test_editable_install_refused(self):
        dist = _FakeDistribution(
            "/repo/src/", {"url": "file:///repo", "dir_info": {"editable": True}}
        )
        with patch.object(self_update, "distribution", return_value=dist):
            info = self_update.detect_install()
        assert info.kind == "editable"
        assert info.command is None

    def test_local_dir_install_refused(self):
        dist = _FakeDistribution(
            "/usr/lib/python3/site-packages/", {"url": "file:///repo", "dir_info": {}}
        )
        with patch.object(self_update, "distribution", return_value=dist):
            info = self_update.detect_install()
        assert info.kind == "source"
        assert info.command is None

    def test_uv_tool_install_upgrades_via_uv(self):
        dist = _FakeDistribution(
            "/root/.local/share/uv/tools/oduflow/lib/python3.12/site-packages/"
        )
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(
                self_update.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0, stdout="/root/.local/share/uv/tools\n"
                ),
            ),
        ):
            info = self_update.detect_install()
        assert info.kind == "uv-tool"
        assert info.command == ["/usr/bin/uv", "tool", "upgrade", "oduflow"]

    def test_uv_tool_without_uv_on_path_refused(self):
        dist = _FakeDistribution(
            "/root/.local/share/uv/tools/oduflow/lib/python3.12/site-packages/"
        )
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.shutil, "which", return_value=None),
        ):
            info = self_update.detect_install()
        assert info.kind == "uv-tool"
        assert info.command is None

    def test_uvx_environment_refused(self):
        dist = _FakeDistribution(
            "/root/.cache/uv/archive-v0/AbC/lib/python3.12/site-packages/"
        )
        with patch.object(self_update, "distribution", return_value=dist):
            info = self_update.detect_install()
        assert info.kind == "uvx"
        assert info.command is None

    def test_uv_tool_dir_env_var_is_honoured(self, tmp_path):
        dist = _FakeDistribution(
            f"{tmp_path}{os.sep}oduflow{os.sep}site-packages{os.sep}"
        )
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.dict(os.environ, {"UV_TOOL_DIR": str(tmp_path)}),
            patch.object(self_update.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(
                self_update.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=str(tmp_path)),
            ),
        ):
            info = self_update.detect_install()
        assert info.kind == "uv-tool"
        assert info.command == ["/usr/bin/uv", "tool", "upgrade", "oduflow"]

    def test_uv_cache_dir_env_var_is_honoured(self, tmp_path):
        # A custom UV_CACHE_DIR has none of the default path markers; without
        # reading the env var this uvx run would be mistaken for a pip install.
        dist = _FakeDistribution(f"{tmp_path}{os.sep}archive-v0{os.sep}AbC{os.sep}")
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.dict(os.environ, {"UV_CACHE_DIR": str(tmp_path)}),
        ):
            info = self_update.detect_install()
        assert info.kind == "uvx"
        assert info.command is None

    @pytest.mark.parametrize(
        "tool_dir, returncode",
        [
            ("/root/.local/share/uv/tools", 0),
            ("", 0),
            ("/home/alice/.local/share/uv/tools", 1),
        ],
    )
    def test_uv_tool_directory_mismatch_or_failure_refused(self, tool_dir, returncode):
        dist = _FakeDistribution(
            "/home/alice/.local/share/uv/tools/oduflow/lib/python3.12/site-packages"
        )
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(
                self_update.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=returncode, stdout=tool_dir),
            ) as run,
        ):
            info = self_update.detect_install()
        assert info.command is None
        assert "installing user" in info.reason
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "dir"], capture_output=True, text=True, timeout=30
        )

    @pytest.mark.parametrize(
        "error", [OSError("missing"), subprocess.TimeoutExpired("uv", 30)]
    )
    def test_uv_tool_directory_probe_exception_refused(self, error):
        dist = _FakeDistribution("/root/.local/share/uv/tools/oduflow/site-packages")
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(self_update.subprocess, "run", side_effect=error),
        ):
            assert self_update.detect_install().command is None

    def test_environment_without_pip_refused(self, tmp_path):
        dist = _FakeDistribution(f"{tmp_path}{os.sep}site-packages{os.sep}")
        (tmp_path / "site-packages").mkdir()
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.importlib.util, "find_spec", return_value=None),
        ):
            info = self_update.detect_install()
        assert info.kind == "pip"
        assert info.command is None
        assert "no pip" in info.reason

    def test_unwritable_site_packages_refused(self, tmp_path):
        # pip would quietly "default to user installation" here, upgrading a
        # copy the running service never loads.
        target = tmp_path / "site-packages"
        target.mkdir()
        dist = _FakeDistribution(f"{target}{os.sep}")
        with (
            patch.object(self_update, "distribution", return_value=dist),
            patch.object(self_update.os, "access", return_value=False),
        ):
            info = self_update.detect_install()
        assert info.kind == "pip"
        assert info.command is None
        assert "not writable" in info.reason

    def test_plain_pip_install_upgrades_via_pip(self, tmp_path):
        dist = _FakeDistribution(f"{tmp_path}{os.sep}site-packages{os.sep}")
        (tmp_path / "site-packages").mkdir()
        with patch.object(self_update, "distribution", return_value=dist):
            info = self_update.detect_install()
        assert info.kind == "pip"
        assert info.command == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "oduflow",
        ]


def _pip_install() -> self_update.InstallInfo:
    return self_update.InstallInfo(
        kind="pip", command=["/opt/venv/bin/python", "-m", "pip", "install", "x"]
    )


def _update_available() -> UpdateCheck:
    return UpdateCheck(current="1.79.0", status=STATUS_UPDATE, latest="9.0.0")


class TestRun:
    @pytest.mark.parametrize("version", ["1.79.0", "8.0.0", "", "invalid"])
    def test_unsuccessful_version_verification_stops_before_reconcile(
        self, tmp_path, capsys, version
    ):
        (tmp_path / "oduflow.service").touch()
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update, "_installed_version", return_value=version),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=0),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 1
        assert run.calls == [_pip_install().command]
        assert "Reconciliation and restart skipped" in capsys.readouterr().err

    def test_container_is_a_hard_error(self):
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=True),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 1
        assert run.calls == []

    def test_unsupported_install_is_a_hard_error(self):
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(
                self_update,
                "detect_install",
                return_value=self_update.InstallInfo(kind="editable", reason="no"),
            ),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 1
        assert run.calls == []

    def test_up_to_date_does_nothing(self):
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=UpdateCheck(current="1.79.0", status=STATUS_CURRENT),
            ),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 0
        assert run.calls == []

    def test_upgrade_reconcile_and_root_restart(self, tmp_path):
        unit = tmp_path / "oduflow.service"
        unit.touch()
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update, "_installed_version", return_value="9.0.0"),
            patch.object(
                self_update.shutil, "which", return_value="/other/bin/oduflow"
            ) as which,
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=0),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run(force=True) == 0
        which.assert_not_called()
        assert run.calls == [
            ["/opt/venv/bin/python", "-m", "pip", "install", "x"],
            [sys.executable, "-m", "oduflow.server", "upgrade", "--force"],
            ["systemctl", "restart", "oduflow.service"],
        ]

    def test_failed_release_check_still_upgrades(self, tmp_path):
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=UpdateCheck(
                    current="1.79.0", status=STATUS_ERROR, error="offline"
                ),
            ),
            patch.object(self_update, "_installed_version", return_value="9.0.0"),
            patch.object(self_update, "UNIT_DIR", tmp_path),  # no unit file
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 0
        assert [sys.executable, "-m", "oduflow.server", "upgrade"] in run.calls

    def test_failed_package_upgrade_stops(self, tmp_path):
        run = _Run(returncodes={"pip": 1})
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 1
        assert len(run.calls) == 1

    def test_failed_reconcile_stops_before_restart(self, tmp_path):
        unit = tmp_path / "oduflow.service"
        unit.touch()
        run = _Run(returncodes={"upgrade": 1})
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update, "_installed_version", return_value="9.0.0"),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=0),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 1
        assert ["systemctl", "restart", "oduflow.service"] not in run.calls

    def test_non_root_gets_restart_hint_instead_of_restart(self, tmp_path):
        unit = tmp_path / "oduflow.service"
        unit.touch()
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update, "_installed_version", return_value="9.0.0"),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=1000),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run() == 0
        assert ["systemctl", "restart", "oduflow.service"] not in run.calls

    def test_no_restart_flag_skips_systemctl(self, tmp_path):
        unit = tmp_path / "oduflow.service"
        unit.touch()
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=_update_available(),
            ),
            patch.object(self_update, "_installed_version", return_value="9.0.0"),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=0),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run(restart=False) == 0
        assert ["systemctl", "restart", "oduflow.service"] not in run.calls


class TestForcedReconcile:
    """`--force` on an already-current package still finishes the job."""

    def test_force_reconciles_and_restarts_without_reinstalling(self, tmp_path):
        unit = tmp_path / "oduflow.service"
        unit.touch()
        run = _Run()
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=UpdateCheck(current="9.0.0", status=STATUS_CURRENT),
            ),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.os, "geteuid", return_value=0),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run(force=True) == 0
        assert run.calls == [
            [sys.executable, "-m", "oduflow.server", "upgrade", "--force"],
            ["systemctl", "restart", "oduflow.service"],
        ]

    def test_force_reports_the_reconcile_failure(self, tmp_path):
        run = _Run(returncodes={"upgrade": 1})
        with (
            patch.object(self_update, "in_container", return_value=False),
            patch.object(self_update, "detect_install", return_value=_pip_install()),
            patch.object(
                self_update.updates,
                "check_for_update",
                return_value=UpdateCheck(current="9.0.0", status=STATUS_CURRENT),
            ),
            patch.object(self_update, "UNIT_DIR", tmp_path),
            patch.object(self_update.subprocess, "run", run),
        ):
            assert self_update.run(force=True) == 1
        assert run.calls == [
            [sys.executable, "-m", "oduflow.server", "upgrade", "--force"]
        ]
