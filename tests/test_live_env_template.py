"""Real Odoo development and snapshot workflows on the enrolled Docker engine."""

import base64
import logging
import re
import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

import docker
from oduflow.docker_ops import env_ops, odoo_ops, odoo_rpc, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError, NotFoundError, ProtectedError

pytestmark = pytest.mark.integration


@pytest.fixture
def local_checkout(live_environment, tmp_path):
    isolated, team = live_environment
    team = replace(team, port_range_start=52200, port_range_end=52299)
    settings = replace(isolated, teams={team.team_id: team}, allow_local_path=True)
    system_ops.init_system(settings)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return settings, team, checkout


def _create(settings, team, name, checkout, template=None):
    env_ops.create_environment(
        settings,
        team,
        branch="main",
        repo_url=str(checkout),
        odoo_image="odoo:19.0",
        env_name=name,
        local_path=str(checkout),
        template_name=template,
    )


def _rpc(settings, team, name, model, method, args, **kwargs):
    result = odoo_rpc.call_kw(settings, team, name, model, method, args, kwargs)
    assert result.ok, result.error_text()
    return result.value


def _attach(settings, team, name, content, partner_id):
    (attachment_id,) = _rpc(
        settings,
        team,
        name,
        "ir.attachment",
        "create",
        [
            [
                {
                    "name": "workflow.bin",
                    "datas": base64.b64encode(content).decode(),
                    "res_model": "res.partner",
                    "res_id": partner_id,
                }
            ]
        ],
    )
    return attachment_id


def _attachment(settings, team, name, attachment_id):
    (record,) = _rpc(
        settings,
        team,
        name,
        "ir.attachment",
        "read",
        [[attachment_id]],
        fields=["datas", "store_fname"],
        context={"bin_size": False},
    )
    assert record["store_fname"], (
        "Expected file-backed attachment, not database storage"
    )
    return base64.b64decode(record["datas"]), record["store_fname"]


def test_create_before_production_volume_exists(local_checkout, caplog):
    isolated, team, checkout = local_checkout
    # Production is enabled for admission control, but only dev PG is provisioned.
    settings = replace(isolated, prod_enabled=True)
    with closing(get_client()) as client, pytest.raises(docker.errors.NotFound):
        client.volumes.get(settings.prod_db_volume)

    env = (settings, team, "first-dev")
    with caplog.at_level(logging.WARNING, logger="oduflow"):
        _create(*env, checkout)
    assert env_ops.wait_for_odoo_ready(*env)
    assert settings.prod_db_volume not in caplog.text


def test_live_mount_install_test_and_upgrade(local_checkout):
    settings, team, checkout = local_checkout
    module = "workflow_probe"
    addon = checkout / module
    (addon / "tests").mkdir(parents=True)
    (addon / "__init__.py").touch()
    (addon / "__manifest__.py").write_text(
        repr(
            {
                "name": "Workflow Probe",
                "version": "19.0.1.0.0",
                "license": "LGPL-3",
                "depends": ["base"],
                "data": ["seed.xml"],
            }
        )
    )
    (addon / "tests" / "__init__.py").write_text("from . import test_probe\n")
    (addon / "tests" / "test_probe.py").write_text(
        "from odoo.tests.common import TransactionCase\n\n"
        "class TestWorkflow(TransactionCase):\n"
        "    def test_seed(self):\n"
        "        self.assertEqual(self.env.ref('workflow_probe.seed').name, 'Before')\n"
    )
    seed = addon / "seed.xml"
    xml = '<odoo><record id="seed" model="res.partner"><field name="name">Before</field></record></odoo>'
    seed.write_text(xml)
    env = (settings, team, "development")
    _create(*env, checkout)

    install = odoo_ops.install_odoo_modules(*env, module)
    assert install["exit_code"] == 0, install["output"]
    assert module in {r["name"] for r in odoo_ops.list_installed_module_records(*env)}
    log = odoo_ops.run_environment_tests(
        *env, module, test_tags="/workflow_probe:TestWorkflow.test_seed"
    )
    assert "TestWorkflow.test_seed" in log, log
    summaries = re.findall(r"(\d+) failed, (\d+) error\(s\) of (\d+) tests", log)
    assert summaries, log
    failures, errors, count = map(int, summaries[-1])
    assert failures == errors == 0 and count > 0, log

    # No git commit or push: host edits appear in the running container, and
    # changes written there are visible to the developer in the checkout.
    new_xml = xml.replace("Before", "After upgrade")
    seed.write_text(new_xml)
    path = f"/mnt/extra-addons/{module}/seed.xml"
    assert odoo_ops.read_file_in_environment(*env, path)["output"] == new_xml
    note = "/mnt/extra-addons/notes.txt"
    odoo_ops.write_file_in_environment(*env, note, "one\ntwo\n")
    assert (checkout / "notes.txt").read_text() == "one\ntwo\n"
    result = odoo_ops.run_command_in_environment(*env, f"tail -n 1 {note}", shell=False)
    assert result["exit_code"] == 0 and result["output"] == "two\n", result

    applied = env_ops.pull_environment(*env, upgrade=[module])
    assert applied["exit_code"] == 0, applied
    assert f"{module}/seed.xml" in applied["changed_files"]
    assert env_ops.wait_for_odoo_ready(*env)
    assert (
        _rpc(*env, "res.partner", "search_count", [[("name", "=", "After upgrade")]])
        == 1
    )
    assert env_ops.pull_environment(*env)["action"] == "none"
    env_ops.delete_environment(*env)
    assert seed.read_text() == new_xml


def test_lifecycle_and_rename_preserve_committed_data(local_checkout):
    settings, team, checkout = local_checkout
    env = (settings, team, "lifecycle")
    _create(*env, checkout)
    (partner_id,) = _rpc(
        *env, "res.partner", "create", [[{"name": "Lifecycle Partner"}]]
    )
    attachment_id = _attach(*env, b"Before rename\x00", partner_id)
    sql = f"SELECT name FROM res_partner WHERE id = {int(partner_id)}"
    assert odoo_ops.run_db_query(*env, sql)["output"].splitlines() == [
        "name",
        "Lifecycle Partner",
    ]

    env_ops.protect_environment(*env)
    with pytest.raises(ProtectedError):
        env_ops.stop_environment(*env)
    with pytest.raises(ProtectedError):
        env_ops.delete_environment(*env)
    env_ops.unprotect_environment(*env)
    env_ops.stop_environment(*env)
    assert env_ops.list_environments(settings, team)[0]["status"] == "stopped"
    env_ops.start_environment(settings, env_name=env[2], team=team)
    assert env_ops.wait_for_odoo_ready(*env)
    env_ops.restart_environment(settings, env_name=env[2], team=team)
    assert env_ops.wait_for_odoo_ready(*env)

    updated = env_ops.update_environment(*env, rename_to="renamed", pull_image=False)
    assert updated["exit_code"] == 0, updated["output"]
    with pytest.raises(NotFoundError):
        env_ops.get_environment_info(*env)
    renamed = (settings, team, "renamed")
    assert env_ops.wait_for_odoo_ready(*renamed)
    info = env_ops.get_environment_info(*renamed)
    assert info["local_path"] == str(checkout) and info["git_branch"] == "main"
    assert odoo_ops.run_db_query(*renamed, sql)["output"].splitlines() == [
        "name",
        "Lifecycle Partner",
    ]
    assert _attachment(*renamed, attachment_id)[0] == b"Before rename\x00"
    env_ops.delete_environment(*renamed)
    assert env_ops.list_environments(settings, team) == []
    assert not Path(info["workspace"]).exists()
    assert checkout.is_dir()


@pytest.mark.parametrize("overlay", [False, True], ids=["copy", "overlay"])
def test_snapshot_import_refresh_and_recreate(local_checkout, overlay):
    isolated, team, checkout = local_checkout
    if overlay:
        assert Path("/dev/fuse").exists() and shutil.which("fuse-overlayfs"), (
            "The full Docker suite requires Linux FUSE and fuse-overlayfs"
        )
    settings = replace(isolated, overlay_threshold_mb=0 if overlay else 1024)
    source = (settings, team, "source")
    clone = (settings, team, "clone")
    _create(*source, checkout)
    (partner_id,) = _rpc(
        *source, "res.partner", "create", [[{"name": "Snapshot Partner"}]]
    )
    baseline = b"Snapshot content\x00"
    attachment_id = _attach(*source, baseline, partner_id)
    original, store_name = _attachment(*source, attachment_id)
    assert original == baseline

    saved = system_ops.publish_env_as_template(*source, "saved")
    assert not saved["remount_failures"], saved
    assert (
        Path(team.get_template_filestore_path("saved")) / store_name
    ).read_bytes() == baseline
    imported = system_ops.import_template(
        settings, team, source=team.get_template_dir("saved"), template_name="imported"
    )
    assert imported["dump_reloaded"] and imported["includes_filestore"], imported
    profiles = {
        p["template_name"]: p for p in system_ops.list_templates(settings, team)
    }
    assert profiles["saved"]["db_loaded"] and profiles["imported"]["db_loaded"]
    assert profiles["imported"]["use_overlay"] is overlay
    _create(*clone, checkout, template="imported")
    store = Path(env_ops.get_environment_info(*clone)["workspace"]) / "filestore"
    assert store.is_mount() is overlay
    token = env_ops.get_env_token(*clone)
    assert token
    assert _attachment(*clone, attachment_id) == (baseline, store_name)

    changed = b"Private to clone\x00"
    assert _rpc(
        *clone,
        "ir.attachment",
        "write",
        [[attachment_id], {"datas": base64.b64encode(changed).decode()}],
    )
    refreshed = system_ops.refresh_template(settings, team, "imported")
    assert not refreshed["remount_failures"], refreshed
    assert refreshed["affected_envs"] == (["clone"] if overlay else [])
    assert store.is_mount() is overlay
    assert env_ops.wait_for_odoo_ready(*clone)
    content, private_store_name = _attachment(*clone, attachment_id)
    assert content == changed
    assert _attachment(*source, attachment_id) == (baseline, store_name)
    template_files = Path(team.get_template_filestore_path("imported"))
    assert (template_files / store_name).read_bytes() == baseline
    assert not (template_files / private_store_name).exists()

    with pytest.raises(ConflictError):
        system_ops.delete_template(settings, team, "imported")
    env_ops.recreate_environment(*clone)
    assert env_ops.get_env_token(*clone) == token
    assert _attachment(*clone, attachment_id) == (baseline, store_name)
    for env in (clone, source):
        env_ops.delete_environment(*env)
    assert env_ops.list_environments(settings, team) == []
    for name in ("imported", "saved"):
        system_ops.delete_template(settings, team, name)
        assert not Path(team.get_template_dir(name)).exists()
    assert not {"imported", "saved"}.intersection(
        p["template_name"] for p in system_ops.list_templates(settings, team)
    )
