"""One-command package upgrade: ``oduflow self-update``.

This wraps the documented three-step upgrade (upgrade the package, reconcile
bundled files with ``oduflow upgrade``, restart the service) into one command.
It deliberately does *not* invent a new upgrade mechanism: it detects how the
package was installed and drives that installer's own upgrade path, then runs
the bundled-file reconciliation through the same Python interpreter — a child
process, because this process still executes the old code and its import
machinery predates the upgrade.

Refusals are hard errors, not best-effort attempts:

* **Containers.** ``pip install --upgrade`` inside the ``oduist/oduflow``
  container survives a restart but silently reverts when the container is
  recreated — an upgrade that undoes itself is worse than none. The supported
  path is pulling the new image and recreating the container, which only the
  host can do.
* **Source checkouts and editable installs.** Those are updated with ``git``;
  overwriting them from PyPI would detach the running code from the checkout.
* **``uvx`` runs.** The environment is an ephemeral cache entry; use
  ``uvx oduflow@latest`` to explicitly refresh it.
* **Environments pip cannot upgrade in place.** A virtualenv created without
  pip has no installer to drive, and a site-packages directory this user cannot
  write makes pip "default to user installation" — a second copy in ``~/.local``
  that the running service never loads. Both are reported instead of attempted.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.version import InvalidVersion, Version

from oduflow import updates
from oduflow.systemd import SERVICE_NAME, UNIT_DIR

PACKAGE = "oduflow"

DOCKER_UPGRADE_HINT = (
    "Oduflow is running inside a container. self-update cannot replace the\n"
    "container image from within: a package upgraded in the writable layer\n"
    "reverts as soon as the container is recreated. Upgrade from the host by\n"
    "pulling the new image and recreating the container — see\n"
    "https://docs.oduflow.dev/docker/"
)

# Written by the container runtime at PID-1 setup; nothing on a plain host
# creates them.
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")


def in_container() -> bool:
    """True when this process runs inside a container (Docker or Podman)."""
    if any(os.path.exists(marker) for marker in _CONTAINER_MARKERS):
        return True
    # cgroup v1 hosts name the runtime in every line. cgroup v2 shows only
    # "0::/" inside a container, which the marker files above already cover.
    try:
        with open("/proc/1/cgroup") as f:
            cgroups = f.read()
    except OSError:
        return False
    return any(name in cgroups for name in ("docker", "containerd", "kubepods"))


@dataclass
class InstallInfo:
    """How the running package was installed, and how to upgrade it."""

    kind: str  # "uv-tool" | "pip" | "source" | "editable" | "uvx"
    command: list[str] | None = None  # upgrade command; None means refuse
    reason: str = ""  # shown to the user when command is None


def _under(location: str, env_var: str) -> bool:
    """True when ``location`` sits under the directory named by ``env_var``."""
    root = os.environ.get(env_var)
    if not root:
        return False
    return location.startswith(root.rstrip(os.sep) + os.sep)


def detect_install() -> InstallInfo:
    """Classify this installation and pick its installer's upgrade command."""
    try:
        dist = distribution(PACKAGE)
    except PackageNotFoundError:
        return InstallInfo(
            kind="source",
            reason=(
                "Oduflow is not installed as a package (source checkout). "
                "Update it with `git pull` and reinstall."
            ),
        )

    # PEP 610: a registry install has no direct_url.json; a local-directory,
    # VCS, or editable install records its origin there.
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            direct_url = json.loads(raw)
        except ValueError:
            direct_url = {}
        if direct_url.get("dir_info", {}).get("editable"):
            return InstallInfo(
                kind="editable",
                reason=(
                    "Oduflow is an editable install (pip install -e). "
                    "Update the checkout with `git pull` instead."
                ),
            )
        return InstallInfo(
            kind="source",
            reason=(
                "Oduflow was installed from "
                f"{direct_url.get('url', 'a local source')}, not from PyPI. "
                "Upgrade it the way it was installed."
            ),
        )

    location = str(dist.locate_file(""))
    sep = os.sep
    if f"{sep}uv{sep}tools{sep}" in location or _under(location, "UV_TOOL_DIR"):
        uv = shutil.which("uv")
        if not uv:
            return InstallInfo(
                kind="uv-tool",
                reason=(
                    "This is a uv tool install, but `uv` is not on PATH. "
                    "Run `uv tool upgrade oduflow` as the installing user."
                ),
            )
        # uv selects its tool directory from the caller's user/environment,
        # not from this Python interpreter. Under sudo (or with UV_TOOL_DIR
        # changed), it could otherwise upgrade an unrelated installation.
        try:
            result = subprocess.run(
                [uv, "tool", "dir"], capture_output=True, text=True, timeout=30
            )
            tool_dir = result.stdout.strip()
            matches = (
                result.returncode == 0
                and bool(tool_dir)
                and Path(location)
                .resolve()
                .is_relative_to((Path(tool_dir) / PACKAGE).resolve())
            )
        except (OSError, subprocess.TimeoutExpired):
            matches = False
        if not matches:
            return InstallInfo(
                kind="uv-tool",
                reason=(
                    "uv's tool directory does not match this installation "
                    f"({location}), or could not be determined. Run as the "
                    "installing user with the original UV_TOOL_DIR."
                ),
            )
        return InstallInfo(kind="uv-tool", command=[uv, "tool", "upgrade", PACKAGE])
    if (
        f"{sep}.cache{sep}uv{sep}" in location
        or f"{sep}uv{sep}archive" in location
        or _under(location, "UV_CACHE_DIR")
    ):
        return InstallInfo(
            kind="uvx",
            reason=(
                "Oduflow is running from an ephemeral uvx environment; "
                "there is nothing persistent to update. Run "
                "`uvx oduflow@latest` to refresh the cached version."
            ),
        )
    if importlib.util.find_spec("pip") is None:
        # A `uv venv` (or any --without-pip virtualenv) has no pip to drive.
        return InstallInfo(
            kind="pip",
            reason=(
                f"This environment ({location}) has no pip, so Oduflow cannot "
                "upgrade itself here. Upgrade it with the installer that "
                "created the environment, for example "
                f"`uv pip install --upgrade {PACKAGE}`."
            ),
        )
    if not os.access(location, os.W_OK):
        # pip silently "defaults to user installation" when the target is not
        # writable, which installs a second copy into ~/.local while the
        # running service keeps loading this one.
        return InstallInfo(
            kind="pip",
            reason=(
                f"{location} is not writable by this user, so "
                "`pip install --upgrade` would install a second copy "
                "elsewhere instead of upgrading the running one. Re-run as "
                "the user that owns the installation (or with sudo)."
            ),
        )
    return InstallInfo(
        kind="pip",
        command=[sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE],
    )


def _installed_version() -> str:
    """Read the now-installed version in a fresh interpreter.

    This process still runs the pre-upgrade code, so its importlib metadata
    caches are stale; only a child process sees what is actually on disk.
    """
    script = "from importlib.metadata import version; print(version('oduflow'))"
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def run(*, force: bool = False, restart: bool = True) -> int:
    """Upgrade the package, reconcile bundled files, restart the service."""
    if in_container():
        print(f"Error: {DOCKER_UPGRADE_HINT}", file=sys.stderr)
        return 1

    install = detect_install()
    if install.command is None:
        print(f"Error: {install.reason}", file=sys.stderr)
        return 1

    check = updates.check_for_update()
    already_current = check.status == updates.STATUS_CURRENT
    if already_current and not force:
        print(f"Already up to date (v{check.current}).")
        return 0
    if already_current:
        # --force also means "finish the job": a previous run may have
        # installed the package and then stopped on a bundle conflict, so the
        # reconciliation and restart below still have work to do.
        print(
            f"Package is already v{check.current}; --force: reconciling "
            "bundled files and restarting anyway."
        )
    elif check.status == updates.STATUS_UPDATE:
        title = f" — {check.release_title}" if check.release_title else ""
        print(f"Upgrading Oduflow v{check.current} → v{check.latest}{title}")
    else:
        # The installer resolves against PyPI either way; a failed GitHub
        # lookup only loses the banner, not the upgrade.
        note = check.error or "versions are not comparable"
        print(f"Release check inconclusive ({note}); asking the installer.")

    if not already_current:
        print(f"Running: {' '.join(install.command)}")
        if subprocess.run(install.command).returncode != 0:
            print(
                "Error: the package upgrade command failed (see its output above).",
                file=sys.stderr,
            )
            return 1

    new_version = check.current if already_current else _installed_version()
    if not already_current:
        # Installer success need not mean the requested release was installed:
        # uv retains version pins, and a package index can lag GitHub releases.
        # Never reconcile/restart or claim success without checking this env.
        try:
            installed = Version(new_version)
        except InvalidVersion:
            print(
                "Error: could not verify the installed Oduflow version. "
                "Reconciliation and restart skipped.",
                file=sys.stderr,
            )
            return 1
        expected = (
            check.latest if check.status == updates.STATUS_UPDATE else check.current
        )
        try:
            minimum = Version(expected)
        except InvalidVersion:
            minimum = installed
        if installed < minimum:
            print(
                f"Error: installer finished, but this environment has v{new_version}; "
                f"expected at least v{expected}. Check installer version constraints "
                "and package index availability, then retry. "
                "Reconciliation and restart skipped.",
                file=sys.stderr,
            )
            return 1
    version_note = f"v{new_version}" if new_version else "the new version"
    done = (
        f"Bundled files reconciled for {version_note}."
        if already_current
        else f"Package upgraded to {version_note}."
    )

    # Launch fresh code through the interpreter whose version we just checked.
    # Console scripts beside a system Python or on PATH can belong to another
    # install (notably with pip --user), so neither is a safe fallback.
    reconcile = [sys.executable, "-m", "oduflow.server", "upgrade"] + (
        ["--force"] if force else []
    )
    print(f"Reconciling bundled files: {' '.join(reconcile)}")
    if subprocess.run(reconcile).returncode != 0:
        print(
            "Error: bundled-file reconciliation needs attention (see above).\n"
            "Resolve it with `oduflow upgrade`, then restart the service:\n"
            f"  systemctl restart {SERVICE_NAME}",
            file=sys.stderr,
        )
        return 1

    unit = UNIT_DIR / SERVICE_NAME
    if not restart:
        print(f"{done} Restart skipped (--no-restart); restart the server to load it.")
        return 0
    if not unit.exists():
        print(
            f"{done} No systemd unit found — "
            "restart your Oduflow server process to load it."
        )
        return 0
    if os.geteuid() != 0:
        print(
            f"{done} Restart the service as root:\n"
            f"  sudo systemctl restart {SERVICE_NAME}"
        )
        return 0
    if subprocess.run(["systemctl", "restart", SERVICE_NAME]).returncode != 0:
        print(
            f"Error: `systemctl restart {SERVICE_NAME}` failed. Check "
            f"`systemctl status {SERVICE_NAME}` and `journalctl -u oduflow`.",
            file=sys.stderr,
        )
        return 1
    print(f"Service {SERVICE_NAME} restarted; now running {version_note}.")
    return 0
