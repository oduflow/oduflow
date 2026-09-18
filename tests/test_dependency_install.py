"""Unit tests for dependency-file resolution in env_ops.

Covers the `.oduflow/`-first lookup for ``apt_packages.txt`` (no repo-root
fallback) and ``requirements.txt`` (falls back to the repo root).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from oduflow.docker_ops import env_ops
from oduflow.errors import PrerequisiteNotMetError


def _write(path: str, content: str = "# pkg\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _mock_container():
    container = MagicMock()
    container.exec_run.return_value = (0, b"ok")
    return container


def _pip_cmd(container) -> str | None:
    """Return the ``pip3 install`` command passed to exec_run, if any."""
    for call in container.exec_run.call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("cmd", "")
        if "pip3 install" in cmd:
            return cmd
    return None


class TestInstallAptPackages:
    def test_reads_from_oduflow_dir(self, tmp_path):
        repo = str(tmp_path)
        _write(os.path.join(repo, ".oduflow", "apt_packages.txt"), "libfoo\nlibbar\n")
        container = _mock_container()

        result = env_ops._install_apt_packages(container, repo)

        assert "[APT] Installed" in result
        assert "libfoo" in result and "libbar" in result

    def test_repo_root_is_ignored(self, tmp_path):
        # apt_packages.txt at the repo root must NOT be picked up anymore.
        repo = str(tmp_path)
        _write(os.path.join(repo, "apt_packages.txt"), "libfoo\n")
        container = _mock_container()

        result = env_ops._install_apt_packages(container, repo)

        assert result == ""
        container.exec_run.assert_not_called()

    def test_missing_returns_empty(self, tmp_path):
        container = _mock_container()
        result = env_ops._install_apt_packages(container, str(tmp_path))
        assert result == ""
        container.exec_run.assert_not_called()


class TestInstallPipRequirements:
    def test_prefers_oduflow_dir(self, tmp_path):
        repo = str(tmp_path)
        _write(os.path.join(repo, ".oduflow", "requirements.txt"), "phonenumbers\n")
        container = _mock_container()

        installed, _ = env_ops._install_pip_requirements(container, repo, restart=False)

        assert installed is True
        assert _pip_cmd(container) is not None
        assert "/mnt/extra-addons/.oduflow/requirements.txt" in _pip_cmd(container)

    def test_falls_back_to_repo_root(self, tmp_path):
        repo = str(tmp_path)
        _write(os.path.join(repo, "requirements.txt"), "phonenumbers\n")
        container = _mock_container()

        installed, _ = env_ops._install_pip_requirements(container, repo, restart=False)

        assert installed is True
        cmd = _pip_cmd(container)
        assert cmd is not None
        assert cmd.endswith("/mnt/extra-addons/requirements.txt")
        assert ".oduflow" not in cmd

    def test_oduflow_wins_over_root(self, tmp_path):
        repo = str(tmp_path)
        _write(os.path.join(repo, ".oduflow", "requirements.txt"), "phonenumbers\n")
        _write(os.path.join(repo, "requirements.txt"), "other\n")
        container = _mock_container()

        env_ops._install_pip_requirements(container, repo, restart=False)

        assert "/mnt/extra-addons/.oduflow/requirements.txt" in _pip_cmd(container)

    def test_missing_returns_false(self, tmp_path):
        container = _mock_container()
        installed, log = env_ops._install_pip_requirements(
            container, str(tmp_path), restart=False
        )
        assert installed is False
        assert log == ""
        assert _pip_cmd(container) is None


def _chown_cmds(container) -> list[str]:
    """All ``chown`` commands passed to exec_run."""
    out = []
    for call in container.exec_run.call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("cmd", "")
        if isinstance(cmd, str) and cmd.strip().startswith("chown"):
            out.append(cmd)
    return out


class TestEnsureUserSitePackages:
    """Regression guard for the overlay copy-up bug.

    ``_ensure_user_site_packages`` makes pip's ``--user`` target odoo-owned, but
    the Odoo filestore is bind-mounted at ``.local/share/Odoo/filestore/<db>``
    from an overlay's read-only lower layer. A recursive ``chown -R`` over the
    whole ``/var/lib/odoo/.local`` descends into the filestore and rewrites every
    file's owner, which forces fuse-overlayfs to copy the entire template
    filestore up into the environment's upper layer — defeating the overlay
    (each env duplicated the ~10 GB filestore). The chown must therefore never
    recurse into ``.local`` / ``.local/share``.
    """

    def test_never_recursively_chowns_all_of_local(self):
        container = _mock_container()
        env_ops._ensure_user_site_packages(container)
        for cmd in _chown_cmds(container):
            assert not (
                "-R" in cmd and cmd.rstrip().endswith("/var/lib/odoo/.local")
            ), f"recursive chown of the whole .local copies the filestore up: {cmd!r}"
            assert not ("-R" in cmd and "/.local/share" in cmd), (
                f"recursive chown into .local/share hits the filestore: {cmd!r}"
            )

    def test_chowns_pip_dirs(self):
        container = _mock_container()
        env_ops._ensure_user_site_packages(container)
        chowns = _chown_cmds(container)
        # .local itself is made odoo-owned, but only non-recursively.
        assert any(
            c.strip() == "chown odoo:odoo /var/lib/odoo/.local" for c in chowns
        ), chowns
        # Both pip --user dirs are chowned (recursively is fine — no filestore).
        assert any("/var/lib/odoo/.local/lib" in c for c in chowns), chowns
        assert any("/var/lib/odoo/.local/bin" in c for c in chowns), chowns


class TestConfigureServingEnvironment:
    """Ordering guard: the serving container boots before Oduflow can exec into
    it, so it always starts with the image's stock conf and without the repo's
    pip packages. Setup must therefore end with exactly one restart that picks
    up both — and it must happen even when the repo ships no requirements.txt,
    which previously left an ``.oduflow/odoo.conf`` unapplied. The restart is
    then gated on a readiness probe, because neutralization execs a second Odoo
    process that must not race the reloading registry.
    """

    def _run(
        self,
        monkeypatch,
        repo_path,
        container,
        *,
        odoo_conf="/tmp/odoo.conf",
        stub_pip=True,
        ready=True,
    ):
        events: list[str] = []
        monkeypatch.setattr(
            env_ops,
            "_copy_file_to_container",
            lambda *a, **kw: events.append("conf"),
        )
        monkeypatch.setattr(
            env_ops,
            "_install_apt_packages",
            lambda *a, **kw: events.append("apt") or "",
        )

        if stub_pip:

            def _pip(_container, _repo, *, restart=True):
                events.append(f"pip(restart={restart})")
                return False, ""

            monkeypatch.setattr(env_ops, "_install_pip_requirements", _pip)

        def _wait(_container, _env_db, timeout=120):
            events.append("wait")
            return env_ops._OdooReadinessResult(
                ready, 1.0, 1, timeout, "" if ready else "URLError: connection refused"
            )

        monkeypatch.setattr(env_ops, "_wait_for_container_odoo_ready", _wait)
        monkeypatch.setattr(
            env_ops, "_recent_container_logs", lambda *a, **kw: "(no logs)"
        )
        container.restart.side_effect = lambda *a, **kw: events.append("restart")

        env_ops._configure_serving_environment(
            MagicMock(),  # client
            MagicMock(),  # settings
            MagicMock(),  # team
            container,
            repo_path,
            "oduflow_1_dev",
            "dev",
            None,  # template_name
            False,  # sanitize
            None,  # auto_install_modules
            odoo_conf,
        )
        return events

    def test_conf_and_deps_land_before_a_single_restart(self, monkeypatch, tmp_path):
        container = _mock_container()
        events = self._run(monkeypatch, str(tmp_path), container)

        assert events == ["conf", "apt", "pip(restart=False)", "restart", "wait"]

    def test_restarts_without_pip_requirements(self, monkeypatch, tmp_path):
        # Repo ships an odoo.conf but no requirements.txt: the conf is useless
        # until the container restarts, so the restart must not be conditional
        # on pip having installed something. The real _install_pip_requirements
        # runs here, so the "nothing to install" branch is the one under test.
        repo = str(tmp_path)
        _write(os.path.join(repo, ".oduflow", "odoo.conf"), "[options]\n")
        container = _mock_container()

        events = self._run(monkeypatch, repo, container, stub_pip=False)

        assert _pip_cmd(container) is None, "there is no requirements.txt to install"
        assert events == ["conf", "apt", "restart", "wait"]

    def test_unready_odoo_after_the_setup_restart_is_fatal(self, monkeypatch, tmp_path):
        # Without this gate, neutralize_environment execs a second Odoo process
        # into a container whose registry is still reloading, and only logs a
        # warning when that collides — handing back an unsanitized environment.
        container = _mock_container()

        with pytest.raises(PrerequisiteNotMetError) as exc_info:
            self._run(monkeypatch, str(tmp_path), container, ready=False)

        assert "Environment setup did not complete" in str(exc_info.value)
