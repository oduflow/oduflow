from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
from collections.abc import Iterable
from typing import Any

from oduflow import settings

logger = logging.getLogger("oduflow")


def _trace(msg: str, *args: object) -> None:
    if settings.TRACE:
        logger.info("[TRACE] " + msg, *args)


MANIFEST_KEYS_WITH_FILES = ("data", "demo", "assets", "qweb")
RESTART_REQUIRED_PATHS = {".oduflow/odoo.conf"}

# Dependency descriptors Oduflow can read into the container. A change to the
# active one means Python/apt deps changed → reinstall + restart. The
# .oduflow/requirements.txt file shadows the root fallback when both exist.
# Only these exact repo-root-relative paths are installed (see
# env_ops._install_pip_requirements / _install_apt_packages); a nested
# ``foo/requirements.txt`` or a repo-root ``apt_packages.txt`` is intentionally
# NOT a dependency descriptor.
DEP_FILE_PATHS = {
    "requirements.txt",
    ".oduflow/requirements.txt",
    ".oduflow/apt_packages.txt",
}

_FIELD_RE = re.compile(r"^\s*\w+\s*=\s*fields\..*", re.MULTILINE)
_VIEW_TAG_RE = re.compile(r"<(tree|list|form)\b([^>]*)/?>")


def _parse_manifest(path: str) -> dict[str, Any]:
    with open(path, "r") as f:
        manifest: dict[str, Any] = ast.literal_eval(f.read())
        return manifest


def _get_module_name(file_path: str, repo_path: str = "") -> str | None:
    """Extract Odoo module name from a relative file path.

    Walks up from the file's directory to find the nearest ancestor
    that contains a ``__manifest__.py``, which marks an Odoo module.
    Falls back to the first directory component when *repo_path* is
    not provided or no manifest is found.
    """
    parts = file_path.split("/")
    if len(parts) < 2:
        return None

    if repo_path:
        dir_parts = parts[:-1]

        for i in range(len(dir_parts), 0, -1):
            candidate = "/".join(dir_parts[:i])
            manifest = os.path.join(repo_path, candidate, "__manifest__.py")
            if os.path.isfile(manifest):
                return dir_parts[i - 1]

    return parts[0]


def _extract_field_lines(source: str) -> set[str]:
    """Return the set of normalised field-definition lines from Python source."""
    lines = set()
    for match in _FIELD_RE.finditer(source):
        line = match.group(0)
        lines.add(re.sub(r"\s+", " ", line).strip())
    return lines


def _check_field_changes(
    py_rel_path: str, repo_path: str, base_ref: str = "HEAD~1"
) -> bool:
    """Return True if any ``fields.`` definition was added, removed or modified."""
    abs_path = os.path.join(repo_path, py_rel_path)

    try:
        new_source = open(abs_path).read() if os.path.isfile(abs_path) else ""
    except Exception:
        new_source = ""

    try:
        old_source = subprocess.run(
            ["git", "-C", repo_path, "show", f"{base_ref}:{py_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        old_source = ""

    old_fields = _extract_field_lines(old_source)
    new_fields = _extract_field_lines(new_source)

    if old_fields != new_fields:
        diff = (new_fields - old_fields) | (old_fields - new_fields)
        logger.info("Field definitions changed in %s: %s", py_rel_path, diff)
        _trace("_check_field_changes(%s) -> CHANGED: %s", py_rel_path, diff)
        return True
    _trace("_check_field_changes(%s) -> no change", py_rel_path)
    return False


def _extract_view_tag_attrs(source: str) -> set[str]:
    """Return normalised ``<tree …>``, ``<list …>``, ``<form …>`` opening tags."""
    tags = set()
    for m in _VIEW_TAG_RE.finditer(source):
        tag = re.sub(r"\s+", " ", m.group(0)).strip()
        tags.add(tag)
    return tags


def _check_xml_view_attr_changes(
    xml_rel_path: str, repo_path: str, base_ref: str = "HEAD~1"
) -> bool:
    """Return True if attributes on ``<tree>``, ``<list>``, or ``<form>`` tags changed."""
    abs_path = os.path.join(repo_path, xml_rel_path)

    try:
        new_source = open(abs_path).read() if os.path.isfile(abs_path) else ""
    except Exception:
        new_source = ""

    try:
        old_source = subprocess.run(
            ["git", "-C", repo_path, "show", f"{base_ref}:{xml_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        old_source = ""

    old_tags = _extract_view_tag_attrs(old_source)
    new_tags = _extract_view_tag_attrs(new_source)

    if old_tags != new_tags:
        diff = (new_tags - old_tags) | (old_tags - new_tags)
        logger.info("View tag attributes changed in %s: %s", xml_rel_path, diff)
        _trace("_check_xml_view_attr_changes(%s) -> CHANGED: %s", xml_rel_path, diff)
        return True
    _trace("_check_xml_view_attr_changes(%s) -> no change", xml_rel_path)
    return False


def _is_security_path(file_path: str) -> bool:
    return "/security/" in f"/{file_path}/" or file_path.startswith("security/")


def _is_data_path(file_path: str) -> bool:
    return "/data/" in f"/{file_path}/" or file_path.startswith("data/")


def _requires_restart_path(file_path: str) -> bool:
    return file_path.replace(os.sep, "/") in RESTART_REQUIRED_PATHS


def _is_dep_file(file_path: str) -> bool:
    return file_path.replace(os.sep, "/") in DEP_FILE_PATHS


def _is_translation_file(file_path: str) -> bool:
    """Return True for a translation catalog Odoo loads into the database.

    Only ``.po`` files count: Odoo loads them on module install/upgrade, so a
    changed catalog needs an upgrade to reach the database. A ``.pot`` is the
    translator template and is never loaded, so it is not upgrade-worthy.
    """
    return os.path.splitext(file_path)[1].lower() == ".po"


def _is_markdown_file(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() == ".md"


def _is_active_dep_file(file_path: str, repo_path: str) -> bool:
    """Whether a changed dependency file is the one Oduflow actually reads."""
    normalized = file_path.replace(os.sep, "/")
    if normalized == "requirements.txt" and os.path.isfile(
        os.path.join(repo_path, ".oduflow", "requirements.txt")
    ):
        return False
    return normalized in DEP_FILE_PATHS


def classify_changes(
    changed_files: list[str], repo_path: str, base_ref: str = "HEAD~1"
) -> dict[str, Any]:
    """
    Classify changed files and determine required Odoo actions.

    Returns:
        {
            "action": "none" | "refresh" | "restart" | "upgrade" | "install",
            "modules_to_upgrade": [...],
            "modules_to_install": [...],
            "details": {
                "py_changed": bool,
                "xml_hot": [...],        # xml not in security/
                "xml_security": [...],   # xml in security/
                "manifest_upgrade": [...], # modules needing upgrade due to manifest
                "js_changed": [...],
                "i18n_changed": [...],   # .po catalogs (module upgrade)
                "restart_required": [...],
                "deps_changed": [...],   # dependency descriptors (requirements/apt)
            }
        }
    """
    _trace("classify_changes: %d files to classify", len(changed_files))

    if not changed_files:
        _trace("classify_changes: no changed files -> action=none")
        return {
            "action": "none",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": {},
        }

    if all(_is_markdown_file(f) for f in changed_files):
        _trace("classify_changes: only Markdown files changed -> action=none")
        return {
            "action": "none",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": {},
        }

    py_changed = False
    xml_hot = []
    xml_security = []
    js_changed = []
    i18n_changed: list[str] = []
    restart_required = []
    deps_changed: list[str] = []
    modules_to_upgrade: set[str] = set()
    modules_to_install: set[str] = set()

    for f in changed_files:
        ext = os.path.splitext(f)[1].lower()
        module = _get_module_name(f, repo_path)

        if _requires_restart_path(f):
            restart_required.append(f)
            _trace(
                "  file=%s ext=%s module=%s -> restart-required config",
                f,
                ext,
                module,
            )
            continue

        if _is_active_dep_file(f, repo_path):
            deps_changed.append(f)
            _trace(
                "  file=%s -> dependency descriptor, reinstall + restart",
                f,
            )
            continue

        if os.path.basename(f) == "__manifest__.py" and module:
            manifest_action = _check_manifest_changes(f, module, repo_path, base_ref)
            _trace(
                "  file=%s ext=manifest module=%s -> manifest_action=%s",
                f,
                module,
                manifest_action,
            )
            if manifest_action == "install":
                modules_to_install.add(module)
            elif manifest_action == "upgrade":
                modules_to_upgrade.add(module)
            continue

        if ext == ".py":
            py_changed = True
            field_changed = False
            if module and module not in modules_to_install:
                field_changed = _check_field_changes(f, repo_path, base_ref)
                if field_changed:
                    modules_to_upgrade.add(module)
            _trace(
                "  file=%s ext=.py module=%s field_changed=%s", f, module, field_changed
            )
            continue

        if ext == ".xml":
            if (_is_security_path(f) or _is_data_path(f)) and module:
                xml_security.append(f)
                if module not in modules_to_install:
                    modules_to_upgrade.add(module)
                _trace(
                    "  file=%s ext=.xml module=%s -> security/data XML, UPGRADE",
                    f,
                    module,
                )
            else:
                if (
                    module
                    and module not in modules_to_install
                    and _check_xml_view_attr_changes(f, repo_path, base_ref)
                ):
                    modules_to_upgrade.add(module)
                    _trace(
                        "  file=%s ext=.xml module=%s -> view attr changed, UPGRADE",
                        f,
                        module,
                    )
                else:
                    xml_hot.append(f)
                    _trace("  file=%s ext=.xml module=%s -> hot-reload XML", f, module)
            continue

        if ext == ".js":
            js_changed.append(f)
            _trace("  file=%s ext=.js module=%s -> hot-reload JS", f, module)
            continue

        if _is_translation_file(f) and module:
            i18n_changed.append(f)
            if module not in modules_to_install:
                modules_to_upgrade.add(module)
            _trace("  file=%s ext=.po module=%s -> translations, UPGRADE", f, module)
            continue

        _trace("  file=%s ext=%s module=%s -> ignored", f, ext, module)

    modules_to_upgrade -= modules_to_install

    details = {
        "py_changed": py_changed,
        "xml_hot": xml_hot,
        "xml_security": xml_security,
        "manifest_upgrade": sorted(modules_to_upgrade),
        "manifest_install": sorted(modules_to_install),
        "js_changed": js_changed,
        "i18n_changed": i18n_changed,
        "restart_required": restart_required,
        "deps_changed": deps_changed,
    }

    if modules_to_install or modules_to_upgrade:
        action = "install" if modules_to_install else "upgrade"
        _trace(
            "classify_changes RESULT: action=%s install=%s upgrade=%s",
            action,
            sorted(modules_to_install),
            sorted(modules_to_upgrade),
        )
        return {
            "action": action,
            "modules_to_install": sorted(modules_to_install),
            "modules_to_upgrade": sorted(modules_to_upgrade),
            "details": details,
        }

    if py_changed or restart_required or deps_changed:
        _trace(
            "classify_changes RESULT: action=restart "
            "(Python, restart-required config, or dependencies changed)"
        )
        return {
            "action": "restart",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": details,
        }

    _trace("classify_changes RESULT: action=refresh (only XML/JS hot-reload)")
    return {
        "action": "refresh",
        "modules_to_upgrade": [],
        "modules_to_install": [],
        "details": details,
    }


def _check_manifest_changes(
    manifest_rel_path: str, module: str, repo_path: str, base_ref: str = "HEAD~1"
) -> str | None:
    """
    Check if __manifest__.py changes require a module install or upgrade.
    Uses git to compare *base_ref* vs current manifest content.
    Returns ``"install"`` for a new module, ``"upgrade"`` for significant
    changes to an existing module, or ``None`` if no action is needed.
    """
    manifest_abs = os.path.join(repo_path, manifest_rel_path)
    if not os.path.isfile(manifest_abs):
        _trace("_check_manifest(%s) -> file missing, skip", module)
        return None

    try:
        new_manifest = _parse_manifest(manifest_abs)
    except Exception:
        logger.warning("Cannot parse new manifest for %s", module)
        _trace("_check_manifest(%s) -> parse error, assume upgrade", module)
        return "upgrade"

    try:
        old_content = subprocess.run(
            ["git", "-C", repo_path, "show", f"{base_ref}:{manifest_rel_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        old_manifest = ast.literal_eval(old_content)
    except Exception:
        logger.info("No previous manifest for %s, new module detected", module)
        _trace("_check_manifest(%s) -> new module, INSTALL", module)
        return "install"

    if old_manifest.get("version") != new_manifest.get("version"):
        logger.info("Module %s: version changed", module)
        _trace(
            "_check_manifest(%s) -> version changed %s -> %s, UPGRADE",
            module,
            old_manifest.get("version"),
            new_manifest.get("version"),
        )
        return "upgrade"

    for key in MANIFEST_KEYS_WITH_FILES:
        old_val = old_manifest.get(key, [])
        new_val = new_manifest.get(key, [])
        if old_val != new_val:
            logger.info("Module %s: '%s' list changed", module, key)
            _trace("_check_manifest(%s) -> '%s' changed, UPGRADE", module, key)
            return "upgrade"

    _trace("_check_manifest(%s) -> no significant changes", module)
    return None


def shallow_classify(changed_files: list[str], repo_path: str = "") -> dict[str, Any]:
    """Path-only classification used when no git *base_ref* is available
    (a non-git live-mount).

    Coarser than :func:`classify_changes`: with no access to old file content
    it cannot tell a field change from a method change in a ``.py`` (treated as
    *restart*), nor a new module from a changed one (a changed
    ``__manifest__.py`` is treated as *upgrade*). Returns the same dict shape.
    """
    if not changed_files:
        return {
            "action": "none",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": {},
        }

    if all(_is_markdown_file(f) for f in changed_files):
        return {
            "action": "none",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": {},
        }

    py_changed = False
    xml_hot: list[str] = []
    xml_security: list[str] = []
    js_changed: list[str] = []
    i18n_changed: list[str] = []
    restart_required: list[str] = []
    deps_changed: list[str] = []
    modules_to_upgrade: set[str] = set()

    for f in changed_files:
        ext = os.path.splitext(f)[1].lower()
        module = _get_module_name(f, repo_path)

        if _requires_restart_path(f):
            restart_required.append(f)
            continue
        if _is_active_dep_file(f, repo_path):
            deps_changed.append(f)
            continue
        if os.path.basename(f) == "__manifest__.py" and module:
            modules_to_upgrade.add(module)
            continue
        if ext == ".py":
            py_changed = True
            continue
        if ext == ".xml":
            if (_is_security_path(f) or _is_data_path(f)) and module:
                xml_security.append(f)
                modules_to_upgrade.add(module)
            else:
                xml_hot.append(f)
            continue
        if ext == ".js":
            js_changed.append(f)
            continue
        if _is_translation_file(f) and module:
            i18n_changed.append(f)
            modules_to_upgrade.add(module)

    details = {
        "py_changed": py_changed,
        "xml_hot": xml_hot,
        "xml_security": xml_security,
        "manifest_upgrade": sorted(modules_to_upgrade),
        "manifest_install": [],
        "js_changed": js_changed,
        "i18n_changed": i18n_changed,
        "restart_required": restart_required,
        "deps_changed": deps_changed,
    }

    if modules_to_upgrade:
        return {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": sorted(modules_to_upgrade),
            "details": details,
        }
    if py_changed or restart_required or deps_changed:
        return {
            "action": "restart",
            "modules_to_upgrade": [],
            "modules_to_install": [],
            "details": details,
        }
    return {
        "action": "refresh",
        "modules_to_upgrade": [],
        "modules_to_install": [],
        "details": details,
    }


def recommend(
    changed_files: list[str], repo_path: str, base_ref: str | None
) -> dict[str, Any]:
    """Recommended Odoo action for *changed_files*.

    Full :func:`classify_changes` (git-based deep checks) when a *base_ref* is
    available, else path-only :func:`shallow_classify`.
    """
    if base_ref:
        return classify_changes(changed_files, repo_path, base_ref=base_ref)
    return shallow_classify(changed_files, repo_path)


# Most → least disruptive. Used to pick the overall action when merging the
# per-repo recommendations of the main repo and each extra-addon worktree.
_ACTION_PRIORITY = {"none": 0, "refresh": 1, "restart": 2, "upgrade": 3, "install": 4}

_DETAIL_LIST_KEYS = (
    "xml_hot",
    "xml_security",
    "manifest_upgrade",
    "manifest_install",
    "js_changed",
    "i18n_changed",
)


def merge_recommendations(recs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine several :func:`recommend` results (one per repo/worktree).

    The main repo and each extra-addon worktree are classified against their own
    tree, so their recommendations must be merged: the overall action is the
    most disruptive one, module lists are unioned, and ``restart_required`` is
    OR-ed across all details (issue #51).
    """
    recs = [r for r in recs if r]
    if not recs:
        return {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}

    action = max(
        (r.get("action", "none") for r in recs),
        key=lambda a: _ACTION_PRIORITY.get(a, 0),
    )
    install = sorted({m for r in recs for m in r.get("modules_to_install", []) or []})
    upgrade = sorted({m for r in recs for m in r.get("modules_to_upgrade", []) or []})

    details: dict[str, Any] = {}
    for key in _DETAIL_LIST_KEYS:
        merged: list[str] = []
        for r in recs:
            d = r.get("details")
            if isinstance(d, dict):
                merged.extend(d.get(key, []) or [])
        if merged:
            details[key] = sorted(set(merged))
    details["restart_required"] = any(
        isinstance(r.get("details"), dict) and r["details"].get("restart_required")
        for r in recs
    )

    return {
        "action": action,
        "modules_to_install": install,
        "modules_to_upgrade": upgrade,
        "details": details,
    }


def template_lineage(
    repo_path: str,
    template_commit: str,
    template_branch: str = "",
) -> dict[str, Any]:
    """Compare a checkout against the commit its template database came from.

    A template database and a branch checkout are two snapshots of one lineage,
    and they diverge in both directions:

    * the checkout is **ahead** — the database predates the code, so existing
      modules whose schema/data changed need an explicit ``-u`` and newly added
      modules need ``-i``. The classic symptom is ``column ... does not exist``
      for a field that exists in the code but not yet in the template's database.
    * the checkout is **behind or diverged** — the database already holds views
      and records written by newer code, and upgrading against the older branch
      fails validation. The fix is to merge the template's source branch first,
      never to recreate the environment (the template would be the same).

    Returns ``{status, message, modules_to_install, modules_to_upgrade}`` where
    status is one of ``unknown`` (no usable provenance — always safe to ignore),
    ``aligned``, ``ahead`` or ``diverged``.
    """
    from oduflow import git_ops

    unknown: dict[str, Any] = {
        "status": "unknown",
        "message": "",
        "modules_to_install": [],
        "modules_to_upgrade": [],
    }
    if not template_commit or not git_ops.is_git_repository(repo_path):
        return unknown
    if not git_ops.commit_exists(repo_path, template_commit):
        # Deleted source branch, different repository, or unavailable fetch —
        # the comparison is simply unavailable, which is not a problem to report.
        return unknown

    short = template_commit[:8]
    branch_hint = f" (branch '{template_branch}')" if template_branch else ""

    try:
        head = git_ops.rev_parse(repo_path)
    except Exception:  # noqa: BLE001 - lineage info is advisory, never fatal
        return unknown

    if head == template_commit:
        return {
            "status": "aligned",
            "message": "",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }

    if not git_ops.is_ancestor(repo_path, template_commit, head):
        merge_hint = template_branch or "the template's source branch"
        return {
            "status": "diverged",
            "message": (
                f"This branch does not contain the template's snapshot commit "
                f"{short}{branch_hint}. The database is newer than the code, so "
                f"upgrades can fail against data written by code this branch does "
                f"not have. Merge {merge_hint} into this branch before the first "
                f"pull_and_apply."
            ),
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }

    try:
        changed = git_ops.diff_names(repo_path, template_commit, head)
    except Exception:  # noqa: BLE001 - advisory only
        return unknown
    if not changed:
        return {
            "status": "aligned",
            "message": "",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }

    rec = recommend(changed, repo_path, template_commit)
    to_install = sorted(set(rec.get("modules_to_install", [])))
    to_upgrade = sorted(set(rec.get("modules_to_upgrade", [])))
    modules = sorted(set(to_install) | set(to_upgrade))
    if not modules:
        return {
            "status": "ahead",
            "message": "",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }
    action_args = []
    if to_install:
        action_args.append(f'install="{",".join(to_install)}"')
    if to_upgrade:
        action_args.append(f'upgrade="{",".join(to_upgrade)}"')
    return {
        "status": "ahead",
        "message": (
            f"This branch is ahead of the template snapshot {short}{branch_hint}: "
            f"schema/data changed in {', '.join(modules)}. Apply them explicitly "
            f"with pull_and_apply({', '.join(action_args)}) — list "
            "dependencies before dependents; modules whose version was not bumped "
            "are not upgraded automatically."
        ),
        "modules_to_install": to_install,
        "modules_to_upgrade": to_upgrade,
    }


def guardrail_warnings(
    recommended: dict[str, Any],
    to_install: list[str],
    to_upgrade: list[str],
    do_restart: bool,
) -> list[str]:
    """Non-blocking warnings comparing the agent's requested action against
    what the changed files suggest.

    Flags only likely *under*-actions (a needed install/upgrade/restart that
    appears to be missing) — the agent stays in charge and may legitimately
    request more than recommended. Returns an empty list when the request
    covers everything (e.g. in auto mode, where requested == recommended).
    """
    warnings: list[str] = []
    rec_install = set(recommended.get("modules_to_install", []))
    rec_upgrade = set(recommended.get("modules_to_upgrade", []))
    requested = set(to_install) | set(to_upgrade)

    for m in sorted(rec_install - set(to_install)):
        warnings.append(
            f"Module '{m}' looks new (manifest added) — consider install='{m}' "
            "(-i); a restart alone won't pick it up."
        )
    for m in sorted(rec_upgrade - requested):
        warnings.append(
            f"Module '{m}' has data/schema changes (manifest, security/data XML, "
            f"translations, or a changed field) — consider upgrade='{m}' (-u); a "
            "restart won't load them into the database."
        )
    if recommended.get("action") == "restart" and not do_restart and not requested:
        warnings.append(
            "Python code or runtime config changed — a restart is recommended; "
            "an XML/JS-only refresh won't reload it."
        )
    return warnings
