"""Best-effort detection of the Odoo major version behind a container.

Several call sites need to know which Odoo generation they are talking to
before they build a CLI invocation: the test runner picks
``--longpolling-port`` vs ``--gevent-port``, the translation exporter picks the
``--i18n-*`` server options vs the ``odoo i18n`` subcommand, and the sanitizer
decides whether ``odoo neutralize`` exists at all. Getting it wrong is not a
cosmetic problem — Odoo aborts on an unknown option — so the helpers here are
deliberately conservative: they return ``None`` rather than a guess whenever
the evidence is ambiguous. What ``None`` then means is the caller's decision,
because the safe default differs per flag: the test runner and the sanitizer
assume the modern behaviour (every image Oduflow can still be pointed at is
16+), while the exporter keeps the ``--i18n-*`` options, which work on
everything up to 18, rather than betting on the 19-only subcommand.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("oduflow")

# `odoo --version` prints a single banner line, e.g. "Odoo Server 19.0-20260908"
# (Odoo 9 and earlier: "OpenERP Server 7.0"; online releases: "Odoo Server
# saas~19.1-20260908"). Anchor on that banner instead of scanning for the first
# "N.M" anywhere in the output: exec_run merges stderr into stdout, so an
# unrelated warning from a custom image ("urllib3 v2 only supports OpenSSL
# 1.1.1+") would otherwise be read as Odoo 1.
_VERSION_BANNER_RE = re.compile(
    r"(?:Odoo|OpenERP) Server\s+(?:saas[-~])?(\d+)\.\d+", re.I
)
# Fallback for images whose binary prints the bare version: a line that is
# *nothing but* the version (plus an optional build suffix), not a number buried
# in prose. Matching a whole line is what makes scanning every line safe -- the
# version need not be on the first one, since a custom image may emit a warning
# ahead of it, but "7.0.1 shim loaded" must not be read as Odoo 7.
_BARE_VERSION_RE = re.compile(
    r"^\s*(?:saas[-~])?v?(\d+)\.\d+(?:\.\d+)?(?:[-+]\S*)?\s*$", re.M
)

# Odoo series that may plausibly appear in an image reference. The number lifted
# out of a tag is a heuristic, not a statement by Odoo itself: a repository that
# merely contains "odoo" may well carry an independent product version
# (`acme/odoo-stack:2.0`) or a calendar tag (`odoo/custom-erp:2024.3`). Reading
# those as Odoo 2 / Odoo 2024 is worse than not reading them at all, because it
# silently selects a removed CLI option instead of falling through to the live
# `odoo --version` probe.
_MIN_PLAUSIBLE_MAJOR = 6
_MAX_PLAUSIBLE_MAJOR = 30


def _plausible(major: int | None) -> int | None:
    """*major* if it can be an Odoo series, else ``None``."""
    if major is None or not (_MIN_PLAUSIBLE_MAJOR <= major <= _MAX_PLAUSIBLE_MAJOR):
        return None
    return major


def _image_path(reference: str) -> str:
    """Repository path without the registry host.

    ``registry.example:5000/acme/odoo-ee`` → ``acme/odoo-ee``. The host is
    dropped before looking for "odoo" so that a vendor's own registry
    (``odoo-registry.example/postgres``) does not make every image it serves
    look like an Odoo image.
    """
    segments = reference.split("/")
    if len(segments) > 1 and (
        "." in segments[0] or ":" in segments[0] or segments[0] == "localhost"
    ):
        return "/".join(segments[1:])
    return reference


def major_from_image_reference(image: str) -> int | None:
    """Major Odoo version encoded in a Docker image reference, if any.

    Handles official tags (``odoo:15.0``), custom repositories with a version
    tag (``registry:5000/acme/odoo-ee:15.0-custom``) and versioned repository
    names (``ghcr.io/acme/odoo-16``). Returns ``None`` for references that
    carry no Odoo version (``ghcr.io/acme/platform:latest``), that are not
    Odoo at all (``postgres:16``) or whose tag is some other versioning scheme
    (``ghcr.io/acme/odoo-stack:2.0``). Conflicting versions in the tag and
    repository name also yield ``None`` so callers can probe the live binary.
    """
    if not isinstance(image, str) or not image:
        return None

    # Parse the Docker tag: this handles official and custom repositories,
    # including registries with ports (registry:5000/acme/odoo-ee:15.0).
    reference = image.split("@", 1)[0]
    leaf = reference.rsplit("/", 1)[-1]
    has_tag = ":" in leaf
    tag = leaf.rsplit(":", 1)[1] if has_tag else ""
    repository = reference.rsplit(":", 1)[0] if has_tag else reference
    is_odoo_image = (
        re.search(r"(?:^|[/_-])odoo(?:$|[/_-])", _image_path(repository), re.I)
        is not None
    )
    match = (
        re.match(r"(?:saas[-~])?(\d+)(?:\.\d+)?(?:$|[-_])", tag)
        if is_odoo_image
        else None
    )
    tag_major = _plausible(int(match.group(1))) if match else None
    # Independently check versioned repository names such as acme/odoo-15.
    # Keep the tag detached so acme/odoo-15:latest still yields version 15.
    match = re.search(
        r"odoo[-_:/]?(\d+)(?:\.\d+)?(?:$|[-_])", _image_path(repository), re.I
    )
    repository_major = _plausible(int(match.group(1))) if match else None
    if (
        tag_major is not None
        and repository_major is not None
        and tag_major != repository_major
    ):
        # Either number could be a custom build version. Let the caller probe
        # instead of selecting incompatible CLI options from an arbitrary one.
        return None
    return tag_major if tag_major is not None else repository_major


def major_from_version_output(text: str) -> int | None:
    """Major Odoo version reported by ``odoo --version`` output."""
    if not isinstance(text, str) or not text:
        return None
    match = _VERSION_BANNER_RE.search(text)
    if match:
        # Plausibility-checked like every other source: a fork that keeps the
        # Odoo banner but prints its own product version ("Odoo Server 2.0") or
        # a calendar one ("Odoo Server 2024.10") would otherwise hand back 2 /
        # 2024 and select a CLI option the underlying Odoo does not have.
        return _plausible(int(match.group(1)))
    match = _BARE_VERSION_RE.search(text)
    return _plausible(int(match.group(1))) if match else None


def major_from_container_labels(container: Any, image_label: str) -> int | None:
    """Major Odoo version from the container's ``oduflow.image`` label."""
    labels = getattr(container, "labels", {}) or {}
    if not isinstance(labels, dict) or not isinstance(image_label, str):
        return None
    return major_from_image_reference(labels.get(image_label, ""))


def detect_odoo_major(container: Any, image_label: str) -> int | None:
    """Major version of the Odoo running in *container*, or ``None``.

    Fast path: the image reference recorded in the ``oduflow.image`` label.
    Custom-tagged images that carry no version (``oduist/customer_odoo``) fall
    back to asking the already-running binary via ``odoo --version`` —
    authoritative and independent of the image name. A probe that fails, or
    whose output has no recognisable version banner, yields ``None``.
    """
    major = major_from_container_labels(container, image_label)
    if major is not None:
        return major

    try:
        exit_code, out = container.exec_run("odoo --version")
    except Exception as exc:  # noqa: BLE001 - version detection is best-effort
        logger.warning("Could not detect Odoo version from container: %s", exc)
        return None

    text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    if exit_code not in (0, None):
        # A failed probe prints a shell/runtime error, not a version banner;
        # parsing it would hand back a number from an unrelated message.
        logger.warning(
            "`odoo --version` failed (exit %s): %s", exit_code, text.strip()[:200]
        )
        return None
    major = major_from_version_output(text)
    if major is None:
        logger.warning(
            "Could not parse an Odoo version from `odoo --version`: %s",
            text.strip()[:200],
        )
    return major
