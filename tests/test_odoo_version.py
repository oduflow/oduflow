"""Unit tests for the shared Odoo version detector."""

from unittest.mock import MagicMock

import pytest

from oduflow.odoo_version import (
    detect_odoo_major,
    major_from_image_reference,
    major_from_version_output,
)

LABEL = "oduflow.image"


@pytest.mark.parametrize(
    "image,expected",
    [
        ("odoo:15.0", 15),
        ("odoo:19.0", 19),
        ("odoo:19", 19),
        ("ghcr.io/acme/odoo-16", 16),
        # A versioned repository keeps its version when a non-version tag is
        # pinned on top: the sanitizer reads the label alone, so losing this
        # turns "skip neutralize on 15" into a failing `odoo neutralize`.
        ("acme/odoo-15:latest", 15),
        ("ghcr.io/acme/odoo-16:latest", 16),
        ("acme/odoo_17:nightly", 17),
        ("acme/odoo-19:19.0-custom", 19),
        ("acme/odoo-15:15", 15),
        # Implausible build tags leave the repository version usable.
        ("acme/odoo-19:2.0", 19),
        # Conflicting plausible versions must fall through to a live probe.
        ("acme/odoo-19:15", None),
        ("acme/odoo-15:19", None),
        ("registry.example:5000/acme/odoo-19:15.0-custom", None),
        ("registry.example:5000/acme/odoo-ee:15.0-custom", 15),
        ("ghcr.io/acme/odoo/17.0-custom", 17),
        ("odoo:19.0@sha256:" + "0" * 64, 19),
        ("odoo:saas~19.1", 19),
        # No version to read: the caller must probe or assume the modern flags.
        ("oduist/customer_odoo", None),
        ("ghcr.io/acme/platform:latest", None),
        ("odoo:latest", None),
        # Not Odoo at all — a version-looking tag must not be mistaken for one.
        ("postgres:16", None),
        # A repository that merely contains "odoo" may version itself on its
        # own scheme: reading those as Odoo 2 / Odoo 2024 would pick a removed
        # CLI option instead of falling through to the live probe.
        ("ghcr.io/acme/odoo-stack:2.0", None),
        ("acme/odoo-2024:latest", None),
        ("registry.acme.com/odoo/custom-erp:2024.3", None),
        # "odoo" in the registry host says nothing about the image it serves.
        ("odoo-registry.example/postgres:16", None),
        ("odoo15.example.com/postgres:latest", None),
        ("", None),
    ],
)
def test_major_from_image_reference(image, expected):
    assert major_from_image_reference(image) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Odoo Server 19.0-20260908\n", 19),
        ("Odoo Server 15.0\n", 15),
        ("OpenERP Server 7.0\n", 7),
        # A fork may keep Odoo's banner while printing its own product or
        # calendar version. Reading those as Odoo 2 / Odoo 2024 would select
        # --longpolling-port (removed in 18) or the 19-only `odoo i18n`
        # subcommand, so they are no version at all.
        ("Odoo Server 2.0-20260908\n", None),
        ("Odoo Server 2024.10\n", None),
        # Online releases carry a saas~ prefix; the exporter reads a missing
        # version as pre-19, so failing to parse this picks the --i18n-export
        # option Odoo 19 removed.
        ("Odoo Server saas~19.1-20260908\n", 19),
        # A warning ahead of the banner must not be read as the version: this is
        # what selected the removed --longpolling-port on an Odoo 19 image.
        ("urllib3 v2 only supports OpenSSL 1.1.1+\nOdoo Server 19.0\n", 19),
        # Bare version output is accepted only when it is the whole line.
        ("18.0-20250101\n", 18),
        ("  18.0  \n", 18),
        ("19.0-20260908\n", 19),
        # A prose line that merely *starts* with a plausible version is not the
        # version: reading "7.0.1 shim loaded" as Odoo 7 selects the
        # --longpolling-port option Odoo 18 removed.
        ("7.0.1 shim loaded\n19.0\n", 19),
        ("7.0.1 shim loaded\n", None),
        # ...and only when the number can be an Odoo series at all.
        ("1.1.1+ is required\n", None),
        ('exec: "odoo": executable file not found in $PATH\n', None),
        ("", None),
    ],
)
def test_major_from_version_output(text, expected):
    assert major_from_version_output(text) == expected


class TestDetectOdooMajor:
    @pytest.mark.parametrize("image", ["odoo:19.0", "acme/odoo-19:19.0-custom"])
    def test_label_wins_without_probing_the_container(self, image):
        container = MagicMock()
        container.labels = {LABEL: image}

        assert detect_odoo_major(container, LABEL) == 19
        container.exec_run.assert_not_called()

    def test_unversioned_image_probes_the_binary(self):
        container = MagicMock()
        container.labels = {LABEL: "oduist/customer_odoo"}
        container.exec_run.return_value = (0, b"Odoo Server 19.0-20260908\n")

        assert detect_odoo_major(container, LABEL) == 19
        container.exec_run.assert_called_once_with("odoo --version")

    @pytest.mark.parametrize(
        "image,major",
        [("acme/odoo-19:15", 19), ("acme/odoo-15:19", 15)],
    )
    def test_conflicting_image_versions_probe_the_binary(self, image, major):
        container = MagicMock()
        container.labels = {LABEL: image}
        container.exec_run.return_value = (0, f"Odoo Server {major}.0\n".encode())

        assert detect_odoo_major(container, LABEL) == major
        container.exec_run.assert_called_once_with("odoo --version")

    def test_conflicting_image_versions_remain_unknown_when_probe_fails(self):
        container = MagicMock()
        container.labels = {LABEL: "acme/odoo-19:15"}
        container.exec_run.return_value = (127, b"odoo: not found\n")

        assert detect_odoo_major(container, LABEL) is None
        container.exec_run.assert_called_once_with("odoo --version")

    def test_failed_probe_yields_none(self):
        container = MagicMock()
        container.labels = {LABEL: "oduist/customer_odoo"}
        # Non-zero exit: the output is an error message, not a version banner.
        container.exec_run.return_value = (127, b"odoo: not found 1.0\n")

        assert detect_odoo_major(container, LABEL) is None

    def test_unparseable_probe_yields_none(self):
        container = MagicMock()
        container.labels = {LABEL: "oduist/customer_odoo"}
        container.exec_run.return_value = (0, b"no banner here\n")

        assert detect_odoo_major(container, LABEL) is None

    def test_probe_error_is_swallowed(self):
        container = MagicMock()
        container.labels = {LABEL: "oduist/customer_odoo"}
        container.exec_run.side_effect = RuntimeError("container is gone")

        assert detect_odoo_major(container, LABEL) is None

    def test_missing_or_odd_labels_yield_none(self):
        container = MagicMock()
        container.labels = ["not", "a", "dict"]
        assert detect_odoo_major(container, LABEL) is None
