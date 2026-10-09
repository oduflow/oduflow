"""Installed module versions in a database vs. manifest versions in a checkout.

A template database records, per installed module, the manifest version its
schema and data were last loaded with (``ir_module_module.latest_version``). A
branch checkout may carry a newer manifest version for the same module — the
usual sign that the module's schema or data moved on since the snapshot. Odoo
reconciles that only on an explicit ``-u``: a plain start loads the new Python
models against the old tables, and the first query that touches a new field
fails with ``column ... does not exist``.

Everything here reads files only, so the comparison is testable without Docker;
:mod:`oduflow.docker_ops.env_ops` supplies the database side and runs the
upgrade.
"""

from __future__ import annotations

import configparser
import logging
import os
from typing import Any

from oduflow.git_ops import parse_manifest

logger = logging.getLogger("oduflow")

# What Odoo assumes for a manifest without "version" and for a module row whose
# latest_version is empty.
DEFAULT_MODULE_VERSION = "1.0"


def odoo_series(module_version: str) -> str:
    """Odoo series of a stored module version: ``18.0.1.3`` → ``18.0``."""
    parts = module_version.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return ""


def series_version(version: str, series: str) -> str:
    """*version* in the series-prefixed form the database stores.

    Manifests may say ``1.18.0`` or ``18.0.1.18.0``; ``latest_version`` always
    holds the second form. Same rule as Odoo's own ``adapt_version``.
    """
    if version == series or not version.startswith(series + "."):
        return f"{series}.{version}"
    return version


def version_key(version: str) -> tuple[int, ...] | None:
    """Comparable form of a dotted numeric version, ``None`` if not numeric.

    Trailing zeros are insignificant, as in Odoo: ``18.0.1.0`` == ``18.0.1``.
    """
    parts = version.strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def addons_path_from_conf(conf_path: str) -> list[str]:
    """The ``addons_path`` entries of an odoo.conf, in order.

    Non-strict on purpose: a repository conf that spells the key in another
    case (``Addons_Path``) ends up as a duplicate option once configparser
    lowercases it, and strict mode would reject the whole file. Odoo itself
    keeps the last occurrence, so do the same instead of failing the check.
    """
    parser = configparser.RawConfigParser(strict=False)
    parser.read(conf_path)
    raw = parser.get("options", "addons_path", fallback="")
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def host_addons_dirs(addons_path: list[str], mounts: dict[str, str]) -> list[str]:
    """Host directories behind container ``addons_path`` entries, in order.

    *mounts* maps a container mount point to its host path. Entries outside
    every mount — Odoo's own addons inside the image — are dropped: their
    versions move with the image, not with the branch.
    """
    dirs: list[str] = []
    for entry in addons_path:
        entry = entry.rstrip("/")
        binds = [b for b in mounts if entry == b or entry.startswith(b + "/")]
        if binds:
            bind = max(binds, key=len)  # the innermost of nested mounts
            dirs.append(mounts[bind] + entry[len(bind) :])
    return dirs


def checkout_module_versions(addons_dirs: list[str]) -> dict[str, str]:
    """Manifest version of each installable module across *addons_dirs*.

    The first directory holding a module wins, as in Odoo's addons_path
    lookup — including when that copy is not installable, which hides the
    later ones rather than letting them through.
    """
    versions: dict[str, str] = {}
    seen: set[str] = set()
    for addons_dir in addons_dirs:
        try:
            names = sorted(os.listdir(addons_dir))
        except OSError:
            continue
        for name in names:
            if name in seen:
                continue
            manifest_path = os.path.join(addons_dir, name, "__manifest__.py")
            if not os.path.isfile(manifest_path):
                continue
            seen.add(name)
            try:
                manifest = parse_manifest(manifest_path)
            except (OSError, SyntaxError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping unreadable manifest %s: %s", manifest_path, exc
                )
                continue
            if not isinstance(manifest, dict) or not manifest.get("installable", True):
                continue
            versions[name] = str(manifest.get("version") or DEFAULT_MODULE_VERSION)
    return versions


def modules_newer_than_installed(
    installed: dict[str, Any], available: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Installed modules whose checkout manifest version is newer.

    *installed* maps module name → ``latest_version`` (``None`` allowed), and
    must include ``base``: its version supplies the Odoo series used to compare
    short manifest versions. Returns ``{name: (installed, checkout)}`` with both
    versions in series form. A module whose code is *older* than the database
    is not reported — upgrading backwards is never the fix.
    """
    series = odoo_series(str(installed.get("base") or ""))
    if not series:
        return {}
    newer: dict[str, tuple[str, str]] = {}
    for name, checkout_version in sorted(available.items()):
        if name not in installed:
            continue
        db_version = series_version(
            str(installed[name] or DEFAULT_MODULE_VERSION), series
        )
        disk_version = series_version(checkout_version, series)
        db_key, disk_key = version_key(db_version), version_key(disk_version)
        if db_key is None or disk_key is None:
            logger.warning(
                "Cannot compare versions of module %s: installed %s, checkout %s",
                name,
                db_version,
                disk_version,
            )
            continue
        if disk_key > db_key:
            newer[name] = (db_version, disk_version)
    return newer
