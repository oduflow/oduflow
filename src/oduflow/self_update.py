"""One-command package upgrade: ``oduflow self-update``.

This wraps the documented three-step upgrade (upgrade the package, reconcile
bundled files with ``oduflow upgrade``, restart the service) into one command.
It deliberately does *not* invent a new upgrade mechanism: it detects how the
package was installed and drives that installer's own upgrade path, then runs
the bundled-file reconciliation through the freshly installed binary — a child
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
* **``uvx`` runs.** The environment is an ephemeral cache entry; the next
  ``uvx oduflow`` resolves fresh anyway, so there is nothing durable to update.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution

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
    if f"{sep}uv{sep}tools{sep}" in location or location.startswith(
        os.environ.get("UV_TOOL_DIR") or "\0"
    ):
        uv = shutil.which("uv")
        if not uv:
            return InstallInfo(
                kind="uv-tool",
                reason=(
                    "This is a uv tool install, but `uv` is not on PATH. "
                    "Run `uv tool upgrade oduflow` as the installing user."
                ),
            )
        return InstallInfo(kind="uv-tool", command=[uv, "tool", "upgrade", PACKAGE])
    if f"{sep}.cache{sep}uv{sep}" in location or f"{sep}uv{sep}archive" in location:
        return InstallInfo(
            kind="uvx",
            reason=(
                "Oduflow is running from an ephemeral uvx environment; "
                "there is nothing persistent to update. The next "
                "`uvx oduflow` run resolves the latest release by itself."
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
    if check.status == updates.STATUS_CURRENT:
        print(f"Already up to date (v{check.current}).")
        return 0
    if check.status == updates.STATUS_UPDATE:
        title = f" — {check.release_title}" if check.release_title else ""
        print(f"Upgrading Oduflow v{check.current} → v{check.latest}{title}")
    else:
        # The installer resolves against PyPI either way; a failed GitHub
        # lookup only loses the banner, not the upgrade.
        note = check.error or "versions are not comparable"
        print(f"Release check inconclusive ({note}); asking the installer.")

    print(f"Running: {' '.join(install.command)}")
    if subprocess.run(install.command).returncode != 0:
        print(
            "Error: the package upgrade command failed (see its output above).",
            file=sys.stderr,
        )
        return 1

    new_version = _installed_version()
    version_note = f"v{new_version}" if new_version else "the new version"

    # Reconcile deployed bundled files (odoo.conf, agent guides, sanitize
    # scripts) through the NEW binary so it applies the new bundle. Without
    # --force this prompts, exactly like a manual `oduflow upgrade`.
    oduflow_bin = shutil.which(PACKAGE) or sys.argv[0]
    reconcile = [oduflow_bin, "upgrade"] + (["--force"] if force else [])
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
        print(
            f"Package upgraded to {version_note}. Restart skipped "
            "(--no-restart); restart the server to load it."
        )
        return 0
    if not unit.exists():
        print(
            f"Package upgraded to {version_note}. No systemd unit found — "
            "restart your Oduflow server process to load it."
        )
        return 0
    if os.geteuid() != 0:
        print(
            f"Package upgraded to {version_note}. Restart the service as root:\n"
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
