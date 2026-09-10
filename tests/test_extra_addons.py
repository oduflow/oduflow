import configparser
import os

import pytest

from oduflow.extra_addons import generate_odoo_conf, resolve_main_addons_path
from oduflow.naming import odoo_major_from_image


def _read_option(conf_path, option):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(conf_path)
    return parser.get("options", option, fallback=None)


def _read_addons_path(conf_path):
    return _read_option(conf_path, "addons_path")


def _write_base_conf(path, addons_path="/mnt/extra-addons"):
    path.write_text(f"[options]\naddons_path = {addons_path}\nlist_db = False\n")
    return str(path)


class TestResolveMainAddonsPath:
    def test_with_addons_subdir(self, tmp_path):
        os.makedirs(tmp_path / "addons")
        assert resolve_main_addons_path(str(tmp_path)) == "/mnt/extra-addons/addons"

    def test_without_addons_subdir(self, tmp_path):
        assert resolve_main_addons_path(str(tmp_path)) == "/mnt/extra-addons"

    def test_addons_is_a_file_not_dir(self, tmp_path):
        (tmp_path / "addons").write_text("not a directory")
        assert resolve_main_addons_path(str(tmp_path)) == "/mnt/extra-addons"


class TestGenerateOdooConf:
    def test_replaces_root_with_addons_subdir(self, tmp_path):
        base = _write_base_conf(tmp_path / "base.conf")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(base, str(out), [], "/mnt/extra-addons/addons")

        assert _read_addons_path(out) == "/mnt/extra-addons/addons"

    def test_default_keeps_root(self, tmp_path):
        base = _write_base_conf(tmp_path / "base.conf")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(base, str(out), [])

        assert _read_addons_path(out) == "/mnt/extra-addons"

    def test_extra_paths_appended_after_subdir(self, tmp_path):
        base = _write_base_conf(tmp_path / "base.conf")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(
            base,
            str(out),
            ["/mnt/extra-addons-foo"],
            "/mnt/extra-addons/addons",
        )

        parts = _read_addons_path(out).split(",")
        assert parts == ["/mnt/extra-addons/addons", "/mnt/extra-addons-foo"]
        assert "/mnt/extra-addons" not in parts

    def test_custom_base_addons_path_untouched(self, tmp_path):
        # A user-provided conf with a custom path (no exact /mnt/extra-addons
        # element) is left alone; the detected subdir is prepended.
        base = _write_base_conf(tmp_path / "base.conf", addons_path="/opt/custom")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(base, str(out), [], "/mnt/extra-addons/addons")

        parts = _read_addons_path(out).split(",")
        assert "/opt/custom" in parts
        assert "/mnt/extra-addons/addons" in parts


class TestOdooMajorFromImage:
    @pytest.mark.parametrize(
        ("image", "major"),
        [
            ("odoo:19.0", 19),
            ("odoo:18", 18),
            ("registry:5000/acme/odoo:19.0", 19),
            ("acme/odoo:19.0-custom", 19),
            ("odoo:latest", None),
            ("odoo@sha256:98fabc0123", None),
            ("odoo:19.0@sha256:98fabc0123", 19),
            ("oduist/customer_odoo", None),
            ("registry:5000/odoo", None),
            ("", None),
        ],
    )
    def test_parse(self, image, major):
        assert odoo_major_from_image(image) == major


class TestWithoutDemoNormalization:
    def _generate(self, tmp_path, value, odoo_image):
        base = tmp_path / "base.conf"
        base.write_text(
            f"[options]\naddons_path = /mnt/extra-addons\nwithout_demo = {value}\n"
        )
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(str(base), str(out), [], odoo_image=odoo_image)
        return _read_option(out, "without_demo")

    def test_all_rewritten_to_true_on_19(self, tmp_path):
        assert self._generate(tmp_path, "all", "odoo:19.0") == "True"

    def test_module_list_rewritten_to_true_on_19(self, tmp_path):
        assert self._generate(tmp_path, "sale,crm", "odoo:19.0") == "True"

    def test_empty_rewritten_to_false_on_19(self, tmp_path):
        # Pre-19 an empty value means "install demo"; keep that intent.
        assert self._generate(tmp_path, "", "odoo:19.0") == "False"

    def test_boolean_value_kept_on_19(self, tmp_path):
        assert self._generate(tmp_path, "False", "odoo:19.0") == "False"

    def test_all_kept_pre_19(self, tmp_path):
        assert self._generate(tmp_path, "all", "odoo:18.0") == "all"

    def test_all_kept_when_version_unknown(self, tmp_path):
        assert self._generate(tmp_path, "all", "acme/custom:latest") == "all"

    def test_absent_option_stays_absent_on_19(self, tmp_path):
        base = tmp_path / "base.conf"
        base.write_text("[options]\naddons_path = /mnt/extra-addons\n")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(str(base), str(out), [], odoo_image="odoo:19.0")
        assert _read_option(out, "without_demo") is None
