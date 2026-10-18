import itertools
import os
import textwrap

import pytest

from oduflow.git_analysis import (
    _extract_field_lines,
    _extract_view_tag_attrs,
    _get_module_name,
    _is_active_dep_file,
    _is_data_path,
    _is_dep_file,
    _is_security_path,
    _is_translation_file,
    classify_changes,
    merge_recommendations,
    shallow_classify,
)


class TestGetModuleName:
    def test_module_file(self):
        assert _get_module_name("sale/models/sale.py") == "sale"

    def test_root_file(self):
        assert _get_module_name("setup.py") is None

    def test_nested(self):
        assert _get_module_name("crm/views/crm_lead.xml") == "crm"


class TestIsSecurityPath:
    def test_security_dir(self):
        assert _is_security_path("sale/security/ir.model.access.csv") is True

    def test_not_security(self):
        assert _is_security_path("sale/views/sale_order.xml") is False

    def test_security_xml(self):
        assert _is_security_path("crm/security/crm_security.xml") is True


class TestIsDataPath:
    def test_data_dir(self):
        assert _is_data_path("sale/data/sale_data.xml") is True

    def test_not_data(self):
        assert _is_data_path("sale/views/sale_order.xml") is False

    def test_nested_data(self):
        assert _is_data_path("addons_ee/connect_elevenlabs/data/tools.xml") is True


class TestIsDepFile:
    def test_root_requirements(self):
        assert _is_dep_file("requirements.txt") is True

    def test_oduflow_requirements(self):
        assert _is_dep_file(".oduflow/requirements.txt") is True

    def test_oduflow_apt_packages(self):
        assert _is_dep_file(".oduflow/apt_packages.txt") is True

    def test_nested_requirements_is_not_a_dep_file(self):
        # Only the repo-root / .oduflow requirements are installed.
        assert _is_dep_file("sale/requirements.txt") is False

    def test_requirements_dev_is_not_a_dep_file(self):
        assert _is_dep_file("requirements-dev.txt") is False

    def test_root_apt_packages_is_not_a_dep_file(self):
        # apt_packages.txt is only read from .oduflow/, never the repo root.
        assert _is_dep_file("apt_packages.txt") is False

    def test_root_requirements_is_shadowed_by_oduflow_requirements(self, tmp_path):
        (tmp_path / ".oduflow").mkdir()
        (tmp_path / ".oduflow" / "requirements.txt").write_text("preferred\n")
        assert _is_active_dep_file("requirements.txt", str(tmp_path)) is False
        assert _is_active_dep_file(".oduflow/requirements.txt", str(tmp_path)) is True


class TestIsTranslationFile:
    def test_po_catalog(self):
        assert _is_translation_file("sale/i18n/ru.po") is True

    def test_uppercase_extension(self):
        assert _is_translation_file("sale/i18n/RU.PO") is True

    def test_pot_template_is_not_loaded(self):
        # .pot is the translator template; Odoo never loads it into the DB.
        assert _is_translation_file("sale/i18n/sale.pot") is False

    def test_other_file(self):
        assert _is_translation_file("sale/views/sale_order.xml") is False


class TestClassifyChanges:
    def test_empty(self):
        result = classify_changes([], "/tmp")
        assert result["action"] == "none"

    def test_only_markdown_needs_no_action(self):
        files = ["README.md", "docs/deployment.MD"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "none"
        assert result["modules_to_upgrade"] == []

    def test_markdown_does_not_hide_refresh_changes(self):
        files = ["README.md", "sale/views/sale_order.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"

    def test_only_xml_not_security(self):
        files = ["sale/views/sale_order.xml", "crm/views/crm_lead.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []

    def test_py_files(self):
        files = ["sale/models/sale.py"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"

    def test_odoo_conf_restart(self):
        result = classify_changes([".oduflow/odoo.conf"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

    def test_odoo_conf_plus_hot_reload_files_restart(self):
        files = [
            ".oduflow/odoo.conf",
            "sale/views/sale_order.xml",
            "web/static/src/js/app.js",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

    def test_lone_requirements_txt_restart(self):
        result = classify_changes(["requirements.txt"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == ["requirements.txt"]
        assert result["modules_to_upgrade"] == []

    def test_oduflow_requirements_restart(self):
        result = classify_changes([".oduflow/requirements.txt"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == [".oduflow/requirements.txt"]

    def test_shadowed_root_requirements_does_not_restart(self, tmp_path):
        (tmp_path / ".oduflow").mkdir()
        (tmp_path / ".oduflow" / "requirements.txt").write_text("preferred\n")
        result = classify_changes(["requirements.txt"], str(tmp_path))
        assert result["action"] == "refresh"
        assert result["details"]["deps_changed"] == []

    def test_apt_packages_restart(self):
        result = classify_changes([".oduflow/apt_packages.txt"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == [".oduflow/apt_packages.txt"]

    def test_requirements_plus_hot_xml_restart(self):
        # A dependency change beats a browser-only refresh.
        files = ["requirements.txt", "sale/views/sale_order.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == ["requirements.txt"]
        assert result["details"]["xml_hot"] == ["sale/views/sale_order.xml"]

    def test_requirements_plus_field_change_stays_upgrade(self, tmp_path):
        # A field change still wins the action, but the dependency file is still
        # recorded so the reinstall trigger survives the mixed case.
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
                customer_code = fields.Char(string='Customer Code')
        """)
        )
        (tmp_path / "requirements.txt").write_text("phonenumbers\n")

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["requirements.txt", "sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]
            assert result["details"]["deps_changed"] == ["requirements.txt"]

    def test_security_csv_triggers_upgrade(self):
        """P-H17: Odoo loads CSV data files (above all ir.model.access.csv)
        only on install/upgrade. Classifying a changed ACL as "refresh"
        silently deployed the environment with stale access rules."""
        files = ["sale/security/ir.model.access.csv"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]
        assert result["details"]["csv_data"] == files

    def test_data_csv_triggers_upgrade(self):
        files = ["sale/data/sale.stage.csv"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]

    def test_csv_outside_a_module_stays_refresh(self):
        # A repo-root CSV has no owning module -> nothing to upgrade.
        files = ["export.csv"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"

    def test_security_xml_file(self):
        files = ["sale/security/sale_security.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]

    def test_data_xml_triggers_upgrade(self):
        files = ["connect_elevenlabs/data/tools.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "connect_elevenlabs" in result["modules_to_upgrade"]

    def test_po_triggers_upgrade(self):
        # Translations only reach the database through a module upgrade.
        files = ["sale/i18n/ru.po"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale"]
        assert result["details"]["i18n_changed"] == ["sale/i18n/ru.po"]

    def test_po_beats_hot_reload_xml(self):
        files = ["sale/views/sale_order.xml", "sale/i18n/ru.po"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale"]
        assert result["details"]["xml_hot"] == ["sale/views/sale_order.xml"]

    def test_pot_only_stays_refresh(self):
        files = ["sale/views/sale_order.xml", "sale/i18n/sale.pot"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []
        assert result["details"]["i18n_changed"] == []

    def test_po_in_new_module_stays_install(self, tmp_path):
        module_dir = tmp_path / "addons_veles" / "customer_code"
        (module_dir / "i18n").mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Customer Code', 'version': '17.0.1.0.0', 'data': []}"
        )
        (module_dir / "i18n" / "ru.po").write_text('msgid ""\nmsgstr ""\n')

        import subprocess
        from unittest.mock import patch

        def mock_subprocess_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd)

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            files = [
                "addons_veles/customer_code/__manifest__.py",
                "addons_veles/customer_code/i18n/ru.po",
            ]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "install"
            assert result["modules_to_install"] == ["customer_code"]
            assert result["modules_to_upgrade"] == []

    def test_js_only(self):
        files = ["web/static/src/js/app.js"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"

    def test_py_plus_xml(self):
        files = ["sale/models/sale.py", "sale/views/sale_order.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"

    def test_upgrade_beats_restart(self):
        files = [
            "sale/models/sale.py",
            "sale/security/sale_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]

    def test_upgrade_beats_odoo_conf_restart(self):
        files = [
            ".oduflow/odoo.conf",
            "sale/security/sale_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

    def test_manifest_version_change(self, tmp_path):
        module_dir = tmp_path / "sale"
        module_dir.mkdir()

        old_manifest = "{'name': 'Sale', 'version': '15.0.1.0.0', 'data': []}"
        new_manifest = "{'name': 'Sale', 'version': '15.0.1.1.0', 'data': []}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        os.makedirs(tmp_path / ".git", exist_ok=True)

        from unittest.mock import patch

        with patch("subprocess.run") as mock_run:
            mock_rev = type("Result", (), {"stdout": old_manifest})()
            mock_run.return_value = mock_rev

            files = ["sale/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_manifest_data_list_change(self, tmp_path):
        module_dir = tmp_path / "crm"
        module_dir.mkdir()

        new_manifest = "{'name': 'CRM', 'version': '15.0.1.0.0', 'data': ['views/crm.xml', 'views/new.xml']}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        from unittest.mock import patch

        old_manifest = (
            "{'name': 'CRM', 'version': '15.0.1.0.0', 'data': ['views/crm.xml']}"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["crm/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_manifest_no_significant_change(self, tmp_path):
        module_dir = tmp_path / "sale"
        module_dir.mkdir()

        new_manifest = "{'name': 'Sale Updated', 'version': '15.0.1.0.0', 'data': []}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        from unittest.mock import patch

        old_manifest = "{'name': 'Sale', 'version': '15.0.1.0.0', 'data': []}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["sale/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "refresh"

    def test_multiple_modules_upgrade(self):
        files = [
            "sale/security/sale_security.xml",
            "crm/security/crm_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert sorted(result["modules_to_upgrade"]) == ["crm", "sale"]

    def test_nested_addon_dir(self, tmp_path):
        module_dir = tmp_path / "addons_veles" / "customer_code"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Customer Code', 'version': '17.0.1.0.0', 'data': []}"
        )

        from unittest.mock import patch

        old_manifest = "{'name': 'Customer Code', 'version': '17.0.0.0.0', 'data': []}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["addons_veles/customer_code/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert result["modules_to_upgrade"] == ["customer_code"]

    def test_new_module_install(self, tmp_path):
        module_dir = tmp_path / "addons_veles" / "customer_code"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Customer Code', 'version': '17.0.1.0.0', 'data': []}"
        )
        models_dir = module_dir / "models"
        models_dir.mkdir()
        (models_dir / "res_partner.py").write_text(
            "from odoo import fields, models\n\n"
            "class ResPartner(models.Model):\n"
            "    customer_code = fields.Char(string='Customer Code')\n"
        )

        import subprocess
        from unittest.mock import patch

        def mock_subprocess_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd)

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            files = [
                "addons_veles/customer_code/__manifest__.py",
                "addons_veles/customer_code/__init__.py",
                "addons_veles/customer_code/models/__init__.py",
                "addons_veles/customer_code/models/res_partner.py",
            ]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "install"
            assert result["modules_to_install"] == ["customer_code"]
            assert result["modules_to_upgrade"] == []

    def test_nested_addon_dir_py_file(self, tmp_path):
        module_dir = tmp_path / "addons_ee" / "sale_ext"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Sale Ext', 'version': '17.0.1.0.0'}"
        )

        files = ["addons_ee/sale_ext/security/ir.model.access.xml"]
        result = classify_changes(files, str(tmp_path))
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale_ext"]

    def test_py_field_added_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
                customer_code = fields.Char(string='Customer Code')
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_py_field_removed_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "crm" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "crm" / "__manifest__.py").write_text(
            "{'name': 'CRM', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "lead.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class CrmLead(models.Model):
                name = fields.Char(string='Name')
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class CrmLead(models.Model):
                name = fields.Char(string='Name')
                priority = fields.Selection(string='Priority')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["crm/models/lead.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_py_field_param_changed_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name', required=True)
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_manifest_assets_change_with_base_ref(self, tmp_path):
        """Manifest change is detected when base_ref points to a commit
        several steps back (multi-commit pull scenario)."""
        module_dir = tmp_path / "web_ext"
        module_dir.mkdir()

        old_manifest = "{'name': 'Web Ext', 'version': '17.0.1.0.0', 'assets': {}}"
        new_manifest = (
            "{'name': 'Web Ext', 'version': '17.0.1.0.0', "
            "'assets': {'web.assets_backend': ['web_ext/static/src/js/app.js']}}"
        )
        (module_dir / "__manifest__.py").write_text(new_manifest)

        from unittest.mock import patch

        fake_base_ref = "abc123"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = [
                "web_ext/__manifest__.py",
                "web_ext/static/src/js/app.js",
            ]
            result = classify_changes(files, str(tmp_path), base_ref=fake_base_ref)
            assert result["action"] == "upgrade"
            assert "web_ext" in result["modules_to_upgrade"]

            # Verify git show used base_ref, not HEAD~1
            git_show_calls = [
                c
                for c in mock_run.call_args_list
                if any(f"{fake_base_ref}:" in str(a) for a in c[0][0])
            ]
            assert len(git_show_calls) == 1

    def test_py_no_field_change_stays_restart(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')

                def action_confirm(self):
                    return True
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')

                def action_confirm(self):
                    pass
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "restart"
            assert result["modules_to_upgrade"] == []


class TestExtractViewTagAttrs:
    def test_tree_with_attrs(self):
        source = '<tree editable="bottom" string="Items">'
        assert _extract_view_tag_attrs(source) == {
            '<tree editable="bottom" string="Items">'
        }

    def test_form(self):
        source = '<form string="Sale Order">'
        assert _extract_view_tag_attrs(source) == {'<form string="Sale Order">'}

    def test_list_tag(self):
        source = '<list editable="top">'
        assert _extract_view_tag_attrs(source) == {'<list editable="top">'}

    def test_no_view_tags(self):
        source = '<field name="partner_id"/>'
        assert _extract_view_tag_attrs(source) == set()

    def test_self_closing(self):
        source = '<tree string="Items"/>'
        assert _extract_view_tag_attrs(source) == {'<tree string="Items"/>'}

    def test_multiple_tags(self):
        source = '<tree editable="bottom">\n</tree>\n<form string="Order">\n</form>'
        result = _extract_view_tag_attrs(source)
        assert result == {'<tree editable="bottom">', '<form string="Order">'}


class TestXmlViewAttrUpgrade:
    def test_tree_attr_change_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale_order.xml").write_text(
            '<odoo><record id="view" model="ir.ui.view"><field name="arch" type="xml">'
            '<tree editable="top"><field name="name"/></tree>'
            "</field></record></odoo>"
        )

        from unittest.mock import patch

        old_xml = (
            '<odoo><record id="view" model="ir.ui.view"><field name="arch" type="xml">'
            '<tree editable="bottom"><field name="name"/></tree>'
            "</field></record></odoo>"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["sale/views/sale_order.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_form_attr_change_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "crm" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "crm" / "__manifest__.py").write_text(
            "{'name': 'CRM', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "crm_lead.xml").write_text('<form string="Lead" create="false">')

        from unittest.mock import patch

        old_xml = '<form string="Lead">'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["crm/views/crm_lead.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_xml_no_view_attr_change_stays_refresh(self, tmp_path):
        module_dir = tmp_path / "sale" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale_order.xml").write_text(
            '<tree editable="bottom"><field name="name"/><field name="date"/></tree>'
        )

        from unittest.mock import patch

        old_xml = '<tree editable="bottom"><field name="name"/></tree>'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["sale/views/sale_order.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "refresh"
            assert result["modules_to_upgrade"] == []


class TestExtractFieldLines:
    def test_basic(self):
        source = "    name = fields.Char(string='Name')\n"
        assert _extract_field_lines(source) == {"name = fields.Char(string='Name')"}

    def test_no_spaces(self):
        source = "customer_code=fields.Char(string='Code')\n"
        assert _extract_field_lines(source) == {
            "customer_code=fields.Char(string='Code')"
        }

    def test_many2one(self):
        source = "    partner_id = fields.Many2one('res.partner')\n"
        assert _extract_field_lines(source) == {
            "partner_id = fields.Many2one('res.partner')"
        }

    def test_no_fields(self):
        source = "def create(self, vals):\n    return super().create(vals)\n"
        assert _extract_field_lines(source) == set()


class TestShallowClassify:
    def test_only_markdown_needs_no_action(self):
        result = shallow_classify(["README.md", "docs/setup.MD"], "/tmp")
        assert result["action"] == "none"
        assert result["modules_to_upgrade"] == []

    def test_lone_requirements_txt_restart(self):
        result = shallow_classify(["requirements.txt"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == ["requirements.txt"]

    def test_apt_packages_restart(self):
        result = shallow_classify([".oduflow/apt_packages.txt"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["deps_changed"] == [".oduflow/apt_packages.txt"]

    def test_shadowed_root_requirements_does_not_restart(self, tmp_path):
        (tmp_path / ".oduflow").mkdir()
        (tmp_path / ".oduflow" / "requirements.txt").write_text("preferred\n")
        result = shallow_classify(["requirements.txt"], str(tmp_path))
        assert result["action"] == "refresh"
        assert result["details"]["deps_changed"] == []

    def test_only_xml_still_refresh(self):
        result = shallow_classify(["sale/views/sale_order.xml"], "/tmp")
        assert result["action"] == "refresh"

    def test_po_triggers_upgrade(self):
        result = shallow_classify(["sale/i18n/ru.po"], "/tmp")
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale"]
        assert result["details"]["i18n_changed"] == ["sale/i18n/ru.po"]

    def test_security_csv_triggers_upgrade(self):
        # P-H17: same rule as classify_changes — CSV data files load only on
        # install/upgrade, so a changed ACL must never stay on "refresh".
        result = shallow_classify(["sale/security/ir.model.access.csv"], "/tmp")
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale"]
        assert result["details"]["csv_data"] == ["sale/security/ir.model.access.csv"]

    def test_pot_only_stays_refresh(self):
        files = ["sale/views/sale_order.xml", "sale/i18n/sale.pot"]
        result = shallow_classify(files, "/tmp")
        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []


class TestMergeRecommendations:
    def test_empty(self):
        from oduflow.git_analysis import merge_recommendations

        assert merge_recommendations([]) == {
            "action": "none",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }

    def test_picks_most_disruptive_action_and_unions_modules(self):
        from oduflow.git_analysis import merge_recommendations

        main = {
            "action": "restart",
            "modules_to_install": [],
            "modules_to_upgrade": [],
            "details": {"restart_required": True},
        }
        extra = {
            "action": "install",
            "modules_to_install": ["enterprise_mod"],
            "modules_to_upgrade": ["other"],
            "details": {"restart_required": False},
        }
        merged = merge_recommendations([main, extra])
        # install (from the extra-addon repo) outranks restart (from the main repo)
        assert merged["action"] == "install"
        assert merged["modules_to_install"] == ["enterprise_mod"]
        assert merged["modules_to_upgrade"] == ["other"]
        assert merged["details"]["restart_required"] is True

    def test_upgrade_in_extra_addon_not_lost(self):
        from oduflow.git_analysis import merge_recommendations

        main = {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
        extra = {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": ["sale_enterprise"],
            "details": {"restart_required": False},
        }
        merged = merge_recommendations([main, extra])
        assert merged["action"] == "upgrade"
        assert merged["modules_to_upgrade"] == ["sale_enterprise"]

    def test_main_repo_dep_change_elevates_merged_action_to_restart(self):
        # A main-repo requirements.txt change elevates its own unit to restart;
        # merge takes the most-disruptive action across units.
        main = classify_changes(["requirements.txt"], "/tmp")
        extra = {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
        merged = merge_recommendations([main, extra])
        assert merged["action"] == "restart"


class TestTemplateLineage:
    """A template DB and a branch checkout drift in both directions; the check
    must name the right remedy for each and stay silent when it cannot tell."""

    def _repo(self, tmp_path):
        os.makedirs(tmp_path / ".git", exist_ok=True)
        return str(tmp_path)

    @pytest.fixture(autouse=True)
    def _recognize_test_repo(self, monkeypatch):
        from oduflow import git_ops

        monkeypatch.setattr(git_ops, "is_git_repository", lambda path: True)

    def test_no_commit_recorded_is_unknown(self, tmp_path):
        from oduflow.git_analysis import template_lineage

        result = template_lineage(self._repo(tmp_path), "")
        assert result["status"] == "unknown"
        assert result["message"] == ""

    def test_commit_absent_from_repo_is_unknown(self, tmp_path, monkeypatch):
        # Deleted source branch / different repo / shallow clone: no lineage
        # information is not an error and must not produce a warning.
        from oduflow import git_ops
        from oduflow.git_analysis import template_lineage

        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: False)
        result = template_lineage(self._repo(tmp_path), "deadbeef", "prod")
        assert result["status"] == "unknown"

    def test_same_commit_is_aligned(self, tmp_path, monkeypatch):
        from oduflow import git_ops
        from oduflow.git_analysis import template_lineage

        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: True)
        monkeypatch.setattr(git_ops, "rev_parse", lambda p, ref="HEAD": "abc123")
        result = template_lineage(self._repo(tmp_path), "abc123", "prod")
        assert result["status"] == "aligned"
        assert result["message"] == ""

    def test_branch_missing_snapshot_commit_asks_for_a_merge(
        self, tmp_path, monkeypatch
    ):
        # The database is newer than the code — upgrading would validate the old
        # code against data written by newer code. Merge first.
        from oduflow import git_ops
        from oduflow.git_analysis import template_lineage

        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: True)
        monkeypatch.setattr(git_ops, "rev_parse", lambda p, ref="HEAD": "feature1")
        monkeypatch.setattr(git_ops, "is_ancestor", lambda p, a, d="HEAD": False)
        result = template_lineage(self._repo(tmp_path), "snapshot1", "prod")
        assert result["status"] == "diverged"
        assert "Merge prod" in result["message"]
        assert result["modules_to_upgrade"] == []

    def test_branch_ahead_names_the_modules_to_upgrade(self, tmp_path, monkeypatch):
        # The code is newer than the database: the changed modules need an
        # explicit -u, including ones whose version was never bumped.
        from oduflow import git_ops
        from oduflow.git_analysis import template_lineage

        module_dir = tmp_path / "supply" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "supply" / "__manifest__.py").write_text(
            "{'name': 'Supply', 'version': '15.0.1.0.0', 'data': []}"
        )
        (module_dir / "supply.py").write_text(
            textwrap.dedent(
                """
                class Supply(models.Model):
                    _name = "supply"
                    new_field = fields.Char()
                """
            )
        )
        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: True)
        monkeypatch.setattr(git_ops, "rev_parse", lambda p, ref="HEAD": "head1")
        monkeypatch.setattr(git_ops, "is_ancestor", lambda p, a, d="HEAD": True)
        monkeypatch.setattr(
            git_ops, "diff_names", lambda p, b, h="HEAD": ["supply/models/supply.py"]
        )

        from unittest.mock import patch

        with patch("subprocess.run") as mock_run:
            # The old revision of the file had no field definition at all.
            mock_run.return_value = type("Result", (), {"stdout": "class Supply:\n"})()
            result = template_lineage(self._repo(tmp_path), "snapshot1", "prod")

        assert result["status"] == "ahead"
        assert result["modules_to_upgrade"] == ["supply"]
        assert 'upgrade="supply"' in result["message"]

    def test_new_modules_are_installed_not_upgraded(self, tmp_path, monkeypatch):
        from oduflow import git_analysis, git_ops
        from oduflow.git_analysis import template_lineage

        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: True)
        monkeypatch.setattr(git_ops, "rev_parse", lambda p, ref="HEAD": "head1")
        monkeypatch.setattr(git_ops, "is_ancestor", lambda p, a, d="HEAD": True)
        monkeypatch.setattr(
            git_ops,
            "diff_names",
            lambda p, b, h="HEAD": [
                "new_module/__manifest__.py",
                "sale/models/order.py",
            ],
        )
        monkeypatch.setattr(
            git_analysis,
            "recommend",
            lambda files, path, base: {
                "modules_to_install": ["new_module"],
                "modules_to_upgrade": ["sale"],
            },
        )

        result = template_lineage(self._repo(tmp_path), "snapshot1", "prod")

        assert result["modules_to_install"] == ["new_module"]
        assert result["modules_to_upgrade"] == ["sale"]
        assert 'install="new_module"' in result["message"]
        assert 'upgrade="sale"' in result["message"]

    def test_ahead_without_db_relevant_changes_stays_quiet(self, tmp_path, monkeypatch):
        # Ahead by a README only: nothing to upgrade, so say nothing.
        from oduflow import git_ops
        from oduflow.git_analysis import template_lineage

        monkeypatch.setattr(git_ops, "commit_exists", lambda p, c: True)
        monkeypatch.setattr(git_ops, "rev_parse", lambda p, ref="HEAD": "head1")
        monkeypatch.setattr(git_ops, "is_ancestor", lambda p, a, d="HEAD": True)
        monkeypatch.setattr(git_ops, "diff_names", lambda p, b, h="HEAD": ["README.md"])
        result = template_lineage(self._repo(tmp_path), "snapshot1", "prod")
        assert result["status"] == "ahead"
        assert result["message"] == ""


class TestModuleScoping:
    """Files that belong to no module must never produce a module action.

    ``_get_module_name`` returns None for a repo-root file. Every classifier
    branch guards on that; dropping the guard would add ``None`` to the module
    lists and send an unusable ``-u None`` to Odoo.
    """

    def test_root_level_py_with_fields_stays_restart(self, tmp_path):
        # A repo-root .py cannot belong to a module even if it defines fields.
        (tmp_path / "conftest.py").write_text("x = fields.Char()\n")

        result = classify_changes(["conftest.py"], str(tmp_path))

        assert result["action"] == "restart"
        assert result["modules_to_upgrade"] == []
        assert result["details"]["py_changed"] is True

    def test_root_level_po_is_ignored(self, tmp_path):
        result = classify_changes(["messages.po"], str(tmp_path))

        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []
        assert result["details"]["i18n_changed"] == []

    def test_root_level_security_xml_is_hot_reload_only(self, tmp_path):
        result = classify_changes(["security.xml"], str(tmp_path))

        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []

    def test_root_level_manifest_is_ignored(self, tmp_path):
        (tmp_path / "__manifest__.py").write_text("{'name': 'x'}")

        result = classify_changes(["__manifest__.py"], str(tmp_path))

        assert result["modules_to_install"] == []
        assert result["modules_to_upgrade"] == []

    def test_shallow_classify_ignores_root_level_files(self):
        assert shallow_classify(["messages.po"], "/tmp")["modules_to_upgrade"] == []
        assert shallow_classify(["security.xml"], "/tmp")["modules_to_upgrade"] == []
        assert shallow_classify(["__manifest__.py"], "/tmp")["modules_to_upgrade"] == []


class TestInstallSupersedesUpgrade:
    """A module being installed is never also listed for upgrade.

    Install already loads everything; listing the same module twice would make
    Odoo run an upgrade right after the install.
    """

    def _new_module(self, tmp_path):
        module = tmp_path / "sale"
        (module / "security").mkdir(parents=True)
        (module / "i18n").mkdir()
        (module / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module / "security" / "groups.xml").write_text("<odoo/>")
        (module / "i18n" / "ru.po").write_text('msgid ""\n')
        return module

    def test_security_xml_in_a_new_module_stays_install_only(self, tmp_path):
        from unittest.mock import patch

        self._new_module(tmp_path)
        files = ["sale/__manifest__.py", "sale/security/groups.xml"]

        # No previous manifest -> the module is new -> install.
        with patch("subprocess.run", side_effect=Exception("no such ref")):
            result = classify_changes(files, str(tmp_path))

        assert result["action"] == "install"
        assert result["modules_to_install"] == ["sale"]
        assert result["modules_to_upgrade"] == []

    def test_order_of_files_does_not_matter(self, tmp_path):
        # The security XML is classified before the manifest here, so the
        # install is only known later; the final subtraction must still win.
        from unittest.mock import patch

        self._new_module(tmp_path)
        files = ["sale/security/groups.xml", "sale/__manifest__.py"]

        with patch("subprocess.run", side_effect=Exception("no such ref")):
            result = classify_changes(files, str(tmp_path))

        assert result["action"] == "install"
        assert result["modules_to_upgrade"] == []


class TestManifestChangeDetection:
    def _module(self, tmp_path, manifest: str):
        module = tmp_path / "sale"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text(manifest)
        return "sale/__manifest__.py"

    def _classify(self, tmp_path, rel_path, old_manifest):
        from unittest.mock import patch

        with patch("subprocess.run") as run:
            run.return_value = type("R", (), {"stdout": old_manifest})()
            return classify_changes([rel_path], str(tmp_path))

    def test_every_file_bearing_key_triggers_an_upgrade(self, tmp_path):
        # data/demo/assets/qweb all list files Odoo loads at upgrade time.
        for key in ("data", "demo", "assets", "qweb"):
            module_root = tmp_path / key
            module_root.mkdir()
            (module_root / "sale").mkdir()
            (module_root / "sale" / "__manifest__.py").write_text(
                f"{{'name': 'Sale', 'version': '1.0', '{key}': ['a.xml', 'b.xml']}}"
            )
            from unittest.mock import patch

            old = f"{{'name': 'Sale', 'version': '1.0', '{key}': ['a.xml']}}"
            with patch("subprocess.run") as run:
                run.return_value = type("R", (), {"stdout": old})()
                result = classify_changes(["sale/__manifest__.py"], str(module_root))

            assert result["action"] == "upgrade", key
            assert result["modules_to_upgrade"] == ["sale"], key

    def test_cosmetic_manifest_edit_needs_no_action(self, tmp_path):
        rel = self._module(
            tmp_path, "{'name': 'Sale', 'version': '1.0', 'author': 'New Author'}"
        )
        result = self._classify(
            tmp_path, rel, "{'name': 'Sale', 'version': '1.0', 'author': 'Old Author'}"
        )

        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []
        assert result["modules_to_install"] == []

    def test_unparsable_new_manifest_falls_back_to_upgrade(self, tmp_path):
        # Better to upgrade needlessly than to skip a real schema change.
        rel = self._module(tmp_path, "{'name': 'Sale', this is not python")
        result = self._classify(tmp_path, rel, "{'name': 'Sale', 'version': '1.0'}")

        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale"]

    def test_manifest_deleted_from_the_worktree_is_skipped(self, tmp_path):
        # The file is in the changed list but no longer on disk (deleted).
        (tmp_path / "sale").mkdir()
        result = self._classify(
            tmp_path, "sale/__manifest__.py", "{'name': 'Sale', 'version': '1.0'}"
        )

        assert result["modules_to_upgrade"] == []
        assert result["modules_to_install"] == []

    def test_version_bump_triggers_upgrade_even_when_lists_match(self, tmp_path):
        rel = self._module(
            tmp_path, "{'name': 'Sale', 'version': '1.1', 'data': ['a.xml']}"
        )
        result = self._classify(
            tmp_path, rel, "{'name': 'Sale', 'version': '1.0', 'data': ['a.xml']}"
        )

        assert result["action"] == "upgrade"


class TestMergeRecommendationPriority:
    def test_full_priority_order(self):
        # none < refresh < restart < upgrade < install
        order = ["none", "refresh", "restart", "upgrade", "install"]
        for lower, higher in itertools.pairwise(order):
            merged = merge_recommendations([{"action": lower}, {"action": higher}])
            assert merged["action"] == higher, (lower, higher)

    def test_unknown_action_ranks_below_every_known_one(self):
        # An unrecognised action maps to 0, below "refresh". Listed first so a
        # wrong default cannot hide behind max()'s first-wins tie-breaking.
        merged = merge_recommendations(
            [{"action": "something-new"}, {"action": "refresh"}]
        )
        assert merged["action"] == "refresh"

    def test_falsy_recommendations_are_dropped(self):
        merged = merge_recommendations([None, {}, {"action": "restart"}])
        assert merged["action"] == "restart"

    def test_detail_lists_are_unioned_and_sorted(self):
        merged = merge_recommendations(
            [
                {"action": "refresh", "details": {"xml_hot": ["b.xml", "a.xml"]}},
                {"action": "refresh", "details": {"xml_hot": ["a.xml", "c.xml"]}},
            ]
        )
        assert merged["details"]["xml_hot"] == ["a.xml", "b.xml", "c.xml"]

    def test_none_detail_lists_do_not_break_the_merge(self):
        merged = merge_recommendations(
            [
                {"action": "refresh", "details": {"xml_hot": None}},
                {"action": "refresh", "details": {"xml_hot": ["a.xml"]}},
            ]
        )
        assert merged["details"]["xml_hot"] == ["a.xml"]

    def test_restart_required_is_ored_across_units(self):
        merged = merge_recommendations(
            [
                {"action": "refresh", "details": {"restart_required": []}},
                {
                    "action": "refresh",
                    "details": {"restart_required": [".oduflow/odoo.conf"]},
                },
            ]
        )
        assert merged["details"]["restart_required"] is True

        neither = merge_recommendations(
            [
                {"action": "refresh", "details": {"restart_required": []}},
                {"action": "refresh", "details": {}},
            ]
        )
        assert neither["details"]["restart_required"] is False
