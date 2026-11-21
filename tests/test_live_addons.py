"""Real extra-addon, translation and SSH backup workflows on the enrolled VM."""

import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from oduflow import extra_addons, git_ops, po_tools, template_generations, template_sync
from oduflow.docker_ops import env_ops, odoo_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError
from oduflow.naming import get_team_network_name

pytestmark = pytest.mark.integration


@pytest.fixture
def remote(live_environment, tmp_path, monkeypatch):
    isolated, team = live_environment
    team = replace(team, port_range_start=52300, port_range_end=52399)
    cron = tmp_path / "private-cron"
    cron.mkdir()
    settings = replace(
        isolated, teams={team.team_id: team}, template_cron_dir=str(cron)
    )
    # The generated cron file is inspected, not installed or executed by system cron.
    config = tmp_path / "oduflow.toml"
    config.touch()
    monkeypatch.setenv("ODUFLOW_TOML", str(config))
    system_ops.init_system(settings)
    exports = tmp_path / "exports"
    exports.mkdir()
    git_ops.ensure_ssh_key(team.ssh_dir())
    net = get_team_network_name(team.team_id, settings.prefix)
    server = get_client().containers.run(
        "alpine:latest",
        ["sleep", "infinity"],
        detach=True,
        name=settings.prefix + "backup-origin",
        network=net,
        labels={settings.managed_label: "true"},
        volumes={
            str(exports): {"bind": "/exports", "mode": "ro"},
            git_ops.ssh_public_key_path(team.ssh_dir()): {
                "bind": "/team.pub",
                "mode": "ro",
            },
        },
    )
    try:
        # Unprivileged reader, no password login and no host port. The only key
        # accepted is this fixture's. Never mount a host private key in the peer.
        setup = server.exec_run(
            [
                "sh",
                "-ec",
                r"""
            apk add --no-cache openssh-server git rsync
            adduser -D reader
            passwd -d reader
            install -d -m 700 -o reader /home/reader/.ssh
            install -m 600 -o reader /team.pub /home/reader/.ssh/authorized_keys
            git config --system --add safe.directory /exports/catalog
            ssh-keygen -A
            /usr/sbin/sshd -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o AllowUsers=reader
        """,
            ]
        )
        assert setup.exit_code == 0, setup.output.decode()
        server.reload()
        address = server.attrs["NetworkSettings"]["Networks"][net]["IPAddress"]
        # Trust the key from this container, not an unverified network scan.
        host_key = server.exec_run(["cat", "/etc/ssh/ssh_host_ed25519_key.pub"])
        assert host_key.exit_code == 0
        (Path(team.ssh_dir()) / "known_hosts").write_text(
            f"{address} {host_key.output.decode()}"
        )
        checkout = tmp_path / "main"
        checkout.mkdir()
        yield settings, team, checkout, exports, address
    finally:
        server.remove(force=True)


def _create(settings, team, checkout, name, **options):
    env_ops.create_environment(
        settings,
        team,
        branch="main",
        repo_url=str(checkout),
        local_path=str(checkout),
        odoo_image="odoo:19.0",
        env_name=name,
        **options,
    )


def _git(repo, *args):
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=qa@example.invalid",
            "-c",
            "user.name=Oduflow QA",
            *args,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def test_extra_checkout_pin_and_odoo_translation(remote):
    settings, team, checkout, exports, address = remote
    repo = exports / "catalog"
    module = "qa_catalog"
    addon = repo / "addons" / module
    (addon / "i18n").mkdir(parents=True)
    (addon / "__manifest__.py").write_text(
        repr(
            {
                "name": "QA catalog",
                "version": "19.0.1.0.0",
                "depends": ["base"],
                "data": ["menu.xml"],
                "license": "LGPL-3",
            }
        )
    )
    (addon / "__init__.py").touch()
    term = "Orchard inspection"
    menu = addon / "menu.xml"
    menu.write_text(f'<odoo><menuitem id="inspection" name="{term}"/></odoo>')
    (addon / "i18n" / "fr.po").write_text(
        'msgid ""\nmsgstr ""\n"Language: fr_FR\\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        "#. module: qa_catalog\n#: model:ir.ui.menu,name:qa_catalog.inspection\n"
        f'msgid "{term}"\nmsgstr "Inspection du verger"\n'
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "First catalog")
    original = _git(repo, "rev-parse", "HEAD")
    url = f"ssh://reader@{address}/exports/catalog"
    git_ops.validate_repo_url(url)
    registered = extra_addons.register_extra_repo(team, "catalog", url)
    assert registered["available_branches"] == ["main"] and not registered["branches"]
    env = (settings, team, "translations")
    _create(settings, team, checkout, "translations", extra_addons={"catalog": "main"})
    assert env_ops.get_environment_info(*env)["extra_addons_revisions"] == {
        "catalog": original
    }
    installed = odoo_ops.install_odoo_modules(*env, module)
    assert installed["exit_code"] == 0, installed["output"]
    pot = odoo_ops.export_module_translations(*env, module)
    assert pot["read_only_mount"] and not pot["written_path"]
    assert pot["module_dir"] == f"/mnt/extra-addons-catalog/addons/{module}"
    terms = {entry.msgid: entry for entry in po_tools.parse_po(pot["content"])}
    assert "model:ir.ui.menu,name:qa_catalog.inspection" in terms[term].occurrences

    activated = odoo_ops.run_odoo_shell(*env, "env['res.lang']._activate_lang('fr_FR')")
    assert activated["exit_code"] == 0, activated["output"]
    updated = odoo_ops.upgrade_odoo_modules(*env, module)
    assert updated["exit_code"] == 0, updated["output"]
    fr = odoo_ops.export_module_translations(*env, module, "fr_FR")
    assert {e.msgid: e.msgstr for e in po_tools.parse_po(fr["content"])}[
        term
    ] == "Inspection du verger"
    status = odoo_ops.translation_status(*env, module, langs=["fr_FR"])
    (lang,) = status["langs"]
    assert lang["active"] and lang["database"].translated >= 1
    assert lang["file_path"] == pot["module_dir"] + "/i18n/fr.po"
    assert lang["file"].translated == 1 and lang["import_effective"].no_reference == 0

    # Updating the branch cache alone must not retarget a live bind mount.
    menu.write_text(menu.read_text().replace(term, "Second inspection"))
    _git(repo, "commit", "-am", "Rename inspection")
    revision = _git(repo, "rev-parse", "HEAD")
    extra_addons.fetch_extra_repo(team, "catalog", "main")
    mounted = pot["module_dir"] + "/menu.xml"
    assert term in odoo_ops.read_file_in_environment(*env, mounted)["output"]
    with pytest.raises(ConflictError):
        extra_addons.delete_extra_repo(settings, team, "catalog")
    applied = env_ops.pull_environment(*env, upgrade=[module])
    assert applied["exit_code"] == 0, applied
    assert env_ops.get_environment_info(*env)["extra_addons_revisions"] == {
        "catalog": revision
    }
    assert (
        "Second inspection"
        in odoo_ops.read_file_in_environment(*env, mounted)["output"]
    )
    env_ops.delete_environment(*env)
    extra_addons.delete_extra_repo(settings, team, "catalog")
    assert extra_addons.list_extra_repos(team) == []


def _attachment(env, code):
    result = odoo_ops.run_odoo_shell(*env, code)
    assert result["exit_code"] == 0, result["output"]
    return result["output"].splitlines()


def test_attach_rename_and_scheduled_ssh_refresh(remote):
    settings, team, checkout, exports, address = remote
    source = (settings, team, "seed")
    target = (settings, team, "receiver")
    _create(settings, team, checkout, "seed")
    _attachment(
        source,
        "env['ir.attachment'].create({'name': 'inspection-note', 'raw': b'orchard snapshot'})",
    )
    system_ops.publish_env_as_template(*source, "origin")
    origin = Path(team.get_template_dir("origin"))
    imported = system_ops.import_template(
        settings, team, source=str(origin / "dump.pgdump"), template_name="dbonly"
    )
    assert imported["dump_reloaded"] and not imported["includes_filestore"]
    archive = checkout.parent / "backup.zip"
    with zipfile.ZipFile(archive, "w") as out:
        for path in (origin / "filestore").rglob("*"):
            if path.is_file():
                out.write(
                    path,
                    "original-db/" + path.relative_to(origin / "filestore").as_posix(),
                )
    attached = system_ops.attach_filestore(settings, team, "dbonly", str(archive))
    assert attached["filestore_files"] > 0 and not attached["remount_failures"]
    system_ops.rename_template(settings, team, "dbonly", "weekly")
    profiles = {
        r["template_name"]: r for r in system_ops.list_templates(settings, team)
    }
    assert "dbonly" not in profiles and profiles["weekly"]["db_loaded"]
    _create(settings, team, checkout, "receiver", template_name="weekly")
    lookup = "a = env['ir.attachment'].search([('name', '=', 'inspection-note')]).ensure_one()\n"
    show = lookup + "print('CONTENT:', a.raw.decode())"
    assert "CONTENT: orchard snapshot" in _attachment(target, show)
    with pytest.raises(ConflictError):
        system_ops.rename_template(settings, team, "weekly", "unexpected")

    # Publish a second, complete backup. READY is written last; none of the
    # private key, configuration, or live workspaces is exported by the peer.
    _attachment(source, lookup + "a.write({'raw': b'next orchard snapshot'})")
    system_ops.publish_env_as_template(*source, "next")
    backup = exports / "completed"
    backup.mkdir()
    shutil.copyfile(team.get_template_sql_path("next"), backup / "dump.pgdump")
    shutil.copytree(
        team.get_template_filestore_path("next"),
        backup / "filestore",
        copy_function=shutil.copyfile,
    )
    (backup / "READY").write_text("orchard-02\n")
    template_sync.save_config(
        settings,
        team,
        "weekly",
        {
            "enabled": True,
            "schedule": "5 3 * * 1",
            "user": "reader",
            "host": address,
            "path": "/exports/completed",
        },
    )
    assert template_sync.inspect_job(settings, team, "weekly")["cron_installed"]
    template_sync.set_env_policy(team, "receiver", True)
    try:
        run = template_sync.run(settings, team, "weekly")
        assert run["status"] == "success" and run["updated_envs"] == ["receiver"], run
        assert template_generations.data_snapshot(team, "receiver") == "orchard-02"
        assert "CONTENT: next orchard snapshot" in _attachment(target, show)
        _attachment(target, lookup + "a.write({'raw': b'local edit after sync'})")
        template_sync.request_run(team, "weekly")
        same = template_sync.run(settings, team, "weekly", pending=True)
        assert same["status"] == "success" and same["updated_envs"] == [], same
        assert "CONTENT: local edit after sync" in _attachment(target, show)
        assert not template_sync.inspect_job(settings, team, "weekly")["pending"]
    finally:
        template_sync.remove_config(settings, team, "weekly")
    assert not list(Path(settings.template_cron_dir).iterdir())
    for env in (target, source):
        env_ops.delete_environment(*env)
    for name in ("weekly", "origin", "next"):
        system_ops.delete_template(settings, team, name)
