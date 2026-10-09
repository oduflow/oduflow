import pytest

from oduflow import module_versions as mv


def _module(addons_dir, name, manifest):
    module_dir = addons_dir / name
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text(manifest)


class TestVersionNormalization:
    @pytest.mark.parametrize(
        "version, expected",
        [
            ("1.18.0", "18.0.1.18.0"),
            ("18.0.1.18.0", "18.0.1.18.0"),
            ("18.0", "18.0.18.0"),
            ("1.0", "18.0.1.0"),
        ],
    )
    def test_series_version_matches_stored_form(self, version, expected):
        assert mv.series_version(version, "18.0") == expected

    def test_trailing_zeros_are_insignificant(self):
        assert mv.version_key("18.0.1.0") == mv.version_key("18.0.1")
        assert mv.version_key("18.0.1.0.0") == mv.version_key("18.0.1")

    def test_components_compare_numerically(self):
        assert mv.version_key("18.0.1.10.0") > mv.version_key("18.0.1.9.0")

    def test_non_numeric_version_is_not_comparable(self):
        assert mv.version_key("18.0.1.0-beta") is None

    def test_series_from_stored_version(self):
        assert mv.odoo_series("18.0.1.3") == "18.0"
        assert mv.odoo_series("") == ""


class TestModulesNewerThanInstalled:
    INSTALLED = {
        "base": "18.0.1.3",
        "zipfit_delivery_email_rfq": "18.0.1.17.0",
        "same": "18.0.1.0.0",
        "older_in_code": "18.0.2.0.0",
        "no_version": None,
    }

    def test_reports_only_installed_modules_bumped_in_the_checkout(self):
        newer = mv.modules_newer_than_installed(
            self.INSTALLED,
            {
                "zipfit_delivery_email_rfq": "18.0.1.18.0",
                "same": "1.0.0",
                "older_in_code": "18.0.1.5.0",
                "not_installed": "18.0.9.0.0",
            },
        )

        assert newer == {
            "zipfit_delivery_email_rfq": ("18.0.1.17.0", "18.0.1.18.0"),
        }

    def test_short_manifest_version_is_adapted_before_comparing(self):
        newer = mv.modules_newer_than_installed(
            self.INSTALLED, {"zipfit_delivery_email_rfq": "1.18.0"}
        )

        assert newer == {
            "zipfit_delivery_email_rfq": ("18.0.1.17.0", "18.0.1.18.0"),
        }

    def test_empty_installed_version_counts_as_default(self):
        newer = mv.modules_newer_than_installed(self.INSTALLED, {"no_version": "1.1"})

        assert newer == {"no_version": ("18.0.1.0", "18.0.1.1")}

    def test_uncomparable_versions_are_skipped(self):
        newer = mv.modules_newer_than_installed(
            self.INSTALLED, {"zipfit_delivery_email_rfq": "18.0.1.18.0-beta"}
        )

        assert newer == {}

    def test_nothing_is_compared_without_base(self):
        installed = {"zipfit_delivery_email_rfq": "18.0.1.17.0"}

        assert (
            mv.modules_newer_than_installed(
                installed, {"zipfit_delivery_email_rfq": "18.0.1.18.0"}
            )
            == {}
        )


class TestCheckoutScan:
    def test_addons_path_entries_map_to_host_mounts(self):
        mounts = {
            "/mnt/extra-addons": "/ws/repo",
            "/mnt/extra-addons-oca": "/shared/oca",
        }

        dirs = mv.host_addons_dirs(
            [
                "/mnt/extra-addons/addons",
                "/mnt/extra-addons-oca",
                "/usr/lib/python3/dist-packages/odoo/addons",
            ],
            mounts,
        )

        assert dirs == ["/ws/repo/addons", "/shared/oca"]

    def test_first_addons_dir_wins(self, tmp_path):
        first, second = tmp_path / "first", tmp_path / "second"
        _module(first, "shared", "{'version': '18.0.1.1.0'}")
        _module(second, "shared", "{'version': '18.0.9.0.0'}")
        _module(second, "other", "{'name': 'Other'}")

        versions = mv.checkout_module_versions([str(first), str(second)])

        assert versions == {"shared": "18.0.1.1.0", "other": "1.0"}

    def test_non_installable_copy_hides_later_ones(self, tmp_path):
        first, second = tmp_path / "first", tmp_path / "second"
        _module(first, "shared", "{'version': '18.0.1.1.0', 'installable': False}")
        _module(second, "shared", "{'version': '18.0.9.0.0'}")

        assert mv.checkout_module_versions([str(first), str(second)]) == {}

    def test_unreadable_manifest_and_missing_dir_are_skipped(self, tmp_path):
        addons = tmp_path / "addons"
        _module(addons, "broken", "{'version': ")
        _module(addons, "good", "{'version': '18.0.1.0.1'}")
        (addons / "not_a_module").mkdir()

        versions = mv.checkout_module_versions([str(tmp_path / "missing"), str(addons)])

        assert versions == {"good": "18.0.1.0.1"}

    def test_addons_path_from_conf(self, tmp_path):
        conf = tmp_path / "odoo.conf"
        conf.write_text(
            "[options]\naddons_path = /mnt/extra-addons/addons, /mnt/extra-addons-oca\n"
        )

        assert mv.addons_path_from_conf(str(conf)) == [
            "/mnt/extra-addons/addons",
            "/mnt/extra-addons-oca",
        ]

    def test_duplicate_addons_path_key_keeps_the_last_one(self, tmp_path):
        conf = tmp_path / "odoo.conf"
        # configparser lowercases keys, so a conf mixing cases holds the same
        # option twice; Odoo keeps the last occurrence rather than failing.
        conf.write_text(
            "[options]\naddons_path = /mnt/extra-addons\n"
            "Addons_Path = /mnt/extra-addons/addons\n"
        )

        assert mv.addons_path_from_conf(str(conf)) == ["/mnt/extra-addons/addons"]
