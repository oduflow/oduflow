import configparser
import os

from oduflow.extra_addons import (
    generate_odoo_conf,
    resolve_extra_addons_path,
    resolve_main_addons_path,
)


def _read_addons_path(conf_path):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(conf_path)
    return parser.get("options", "addons_path")


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


class TestResolveExtraAddonsPath:
    def test_with_addons_subdir(self, tmp_path):
        os.makedirs(tmp_path / "addons")
        assert (
            resolve_extra_addons_path(str(tmp_path), "acme")
            == "/mnt/extra-addons-acme/addons"
        )

    def test_without_addons_subdir(self, tmp_path):
        assert (
            resolve_extra_addons_path(str(tmp_path), "acme") == "/mnt/extra-addons-acme"
        )

    def test_unknown_host_path_falls_back_to_mount_root(self):
        assert resolve_extra_addons_path("", "acme") == "/mnt/extra-addons-acme"


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

    def test_pins_listen_interface_and_drops_its_alias(self, tmp_path):
        # Odoo 20 binds 127.0.0.1 without http_interface; the legacy alias
        # would override the pin when Odoo reads the file (case-insensitive).
        base = tmp_path / "base.conf"
        base.write_text(
            "[options]\n"
            "addons_path = /mnt/extra-addons\n"
            "http_interface = 0.0.0.0\n"
            "XMLRPC_Interface = 127.0.0.1\n"
        )
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(str(base), str(out), [])

        parser = configparser.RawConfigParser()
        parser.optionxform = str
        parser.read(out)
        options = dict(parser.items("options"))
        assert options["http_interface"] == "0.0.0.0"
        assert not any(k.lower() == "xmlrpc_interface" for k in options)

    def test_pins_interface_when_base_conf_omits_it(self, tmp_path):
        base = _write_base_conf(tmp_path / "base.conf")
        out = tmp_path / "odoo.conf"
        generate_odoo_conf(base, str(out), [], pinned={"http_port": "8069"})

        parser = configparser.RawConfigParser()
        parser.read(out)
        assert parser.get("options", "http_interface") == "0.0.0.0"
        assert parser.get("options", "http_port") == "8069"
