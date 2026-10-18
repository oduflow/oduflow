import io
import json
import tarfile
from unittest.mock import MagicMock

import httpx
import pytest

from oduflow import production_mcp as connector
from oduflow import production_registry
from oduflow.errors import FlowError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def configured(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path), production_token="p" * 40)
    settings = Settings(teams={"1": team}, prod_enabled=True, routing_mode="traefik")
    production_registry.create_production(team, "erp", {"domain": "erp.example.com"})
    return settings, team


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        connector.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_http_preserves_approval_and_target(configured, monkeypatch):
    settings, team = configured
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url == "https://erp.example.com/odumcp/v1/execute"
        assert request.headers["Authorization"] == "Bearer " + team.production_token
        assert json.loads(request.content) == {
            "operation": "changes.preview",
            "params": {"action": "record.update"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "state": "pending",
                    "approval_id": "approval-1",
                    "next_step": "old tool name",
                },
            },
        )

    transport(monkeypatch, handler)
    result = connector.execute(
        settings, team, "erp", "changes.preview", {"action": "record.update"}
    )
    assert result["approval_id"] == "approval-1"
    assert "production_odoo_execute_change" in result["next_step"]
    assert len(calls) == 1


def test_execute_never_retries_timeout(configured, monkeypatch):
    settings, team = configured
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response")

    transport(monkeypatch, handler)
    with pytest.raises(FlowError, match="outcome may be unknown"):
        connector.execute(
            settings, team, "erp", "changes.execute", {"approval_id": "same-plan"}
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,body",
    [
        (403, {"error": {"code": "policy_denied"}}),
        (200, {"ok": False, "error": {"code": "approval_expired"}}),
    ],
)
def test_policy_errors_not_returned_as_success(configured, monkeypatch, status, body):
    transport(monkeypatch, lambda request: httpx.Response(status, json=body))
    with pytest.raises(FlowError, match=body["error"]["code"]):
        connector.execute(*configured, "erp", "changes.execute")


def test_redirect_not_followed(configured, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            307, headers={"Location": "https://untrusted.example.com"}
        )

    transport(monkeypatch, handler)
    with pytest.raises(FlowError, match="redirected"):
        connector.execute(*configured, "erp", "records.search")
    assert len(calls) == 1


def test_other_team_cannot_resolve_target(configured, monkeypatch, tmp_path):
    settings, _ = configured
    other = TeamSettings(
        team_id="2", data_dir=str(tmp_path / "other"), production_token="q" * 40
    )
    with pytest.raises(FlowError, match="not found"):
        connector.execute(settings, other, "erp", "system.info")


def test_shell_secret_private_and_cleaned_on_failure(configured, monkeypatch):
    settings, team = configured
    monkeypatch.setattr(
        "oduflow.env_credentials.load_credentials",
        lambda *a, **kw: {"pg_user": "odoo", "pg_password": "test-db-password"},
    )
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.client.get_odoo_uid_gid", lambda *args: "101:102"
    )
    container = MagicMock(labels={settings.image_label: "odoo:19.0"})
    captured = {}

    def archive(path, stream):
        with tarfile.open(fileobj=io.BytesIO(stream.read())) as tar:
            members = tar.getmembers()
            assert all(
                (item.mode, item.uid, item.gid) == (0o600, 101, 102) for item in members
            )
            captured.update(
                {item.name: tar.extractfile(item).read() for item in members}
            )

    container.put_archive.side_effect = archive
    container.exec_run.return_value = (1, team.production_token.encode())
    with pytest.raises(PrerequisiteNotMetError) as error:
        connector._shell(
            settings,
            team,
            "erp",
            container,
            connector._CONFIGURE,
            {"key": team.production_token},
        )
    assert team.production_token not in str(error.value)
    first = container.exec_run.call_args_list[0]
    assert team.production_token not in str(first)
    assert settings.prod_db_container in str(first)
    assert container.exec_run.call_args_list[-1].args[0][0:2] == ["rm", "-f"]
    assert any(
        team.production_token.encode() in data
        for filename, data in captured.items()
        if filename.endswith(".json")
    )


def test_provision_reports_failure_and_never_ready(configured, monkeypatch):
    settings, team = configured
    container = MagicMock(status="running")
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.production_ops._get_container", lambda *a: container
    )
    monkeypatch.setattr("oduflow.wal_monitor.assert_writable", lambda *a: None)

    def fail(*args):
        raise PrerequisiteNotMetError("module unavailable")

    monkeypatch.setattr(connector, "_shell", fail)
    with pytest.raises(PrerequisiteNotMetError):
        connector.provision(settings, team, "erp")
    assert (
        production_registry.get_production(team, "erp")["mcp"]["status"]
        == "sync_failed"
    )


def test_existing_addon_does_not_clone(configured, monkeypatch):
    settings, team = configured
    from pathlib import Path

    from oduflow.naming import get_repo_path, prod_env_name

    module = (
        Path(get_repo_path(prod_env_name("erp"), team.workspaces_dir)) / "addons/odumcp"
    )
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    clone.assert_not_called()


def test_sync_all_continues_after_unexpected_target_failure(configured, monkeypatch):
    import asyncio

    from oduflow import server

    settings, team = configured
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx=None: team)
    production_registry.create_production(
        team, "other", {"domain": "other.example.com"}
    )
    reached = []

    def synchronize(settings, team, name, *, refresh=False):
        reached.append(name)
        if name == "erp":
            raise RuntimeError("simulated container failure")
        return {"name": name, "status": "ready"}

    monkeypatch.setattr(connector, "synchronize", synchronize)
    result = json.loads(
        asyncio.run(server.mcp._tool_manager._tools["sync_production_mcp"].fn())
    )
    assert reached == ["erp", "other"]
    assert [row["status"] for row in result["productions"]] == ["failed", "ready"]


def test_managed_addon_is_mounted_and_kept_in_generated_config(configured, monkeypatch):
    from oduflow.docker_ops import production_ops
    from oduflow.naming import get_repo_path, prod_env_name

    settings, team = configured
    module = connector.addon_checkout(team, "erp") / "addons/odumcp"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    record = production_registry.get_production(team, "erp")
    env, volumes, labels = production_ops._container_spec(
        settings, team, "erp", record, {"pg_user": "odoo", "pg_password": "db-test"}, []
    )
    assert volumes[str(module)] == {"bind": connector.MOUNT + "/odumcp", "mode": "ro"}
    assert team.production_token not in json.dumps([env, volumes, labels])
    generate = MagicMock()
    monkeypatch.setattr("oduflow.extra_addons.generate_odoo_conf", generate)
    monkeypatch.setattr(
        "oduflow.pg_tune.detect_resources",
        lambda: {"total_ram_mb": 4096, "cpu_count": 2},
    )
    production_ops._build_prod_odoo_conf(
        settings,
        team,
        "erp",
        get_repo_path(prod_env_name("erp"), team.workspaces_dir),
        [],
    )
    assert connector.MOUNT in generate.call_args.args[2]


def _generated_addons_paths(settings, team, monkeypatch, extra_conf_paths=()):
    """The addons_path list handed to generate_odoo_conf for 'erp'."""
    from oduflow.docker_ops import production_ops
    from oduflow.naming import get_repo_path, prod_env_name

    generate = MagicMock()
    monkeypatch.setattr("oduflow.extra_addons.generate_odoo_conf", generate)
    monkeypatch.setattr(
        "oduflow.pg_tune.detect_resources",
        lambda: {"total_ram_mb": 4096, "cpu_count": 2},
    )
    production_ops._build_prod_odoo_conf(
        settings,
        team,
        "erp",
        get_repo_path(prod_env_name("erp"), team.workspaces_dir),
        list(extra_conf_paths),
    )
    return generate.call_args.args[2]


def test_repository_module_disables_the_managed_mount(configured, monkeypatch):
    """The mount follows repository state, not the checkout's presence: a
    recreate for any reason drops it once the client ships odumcp."""
    from oduflow.docker_ops import production_ops

    settings, team = configured
    module = connector.managed_module(team, "erp")
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    _repository_module(team)
    record = production_registry.get_production(team, "erp")

    _, volumes, _ = production_ops._container_spec(
        settings, team, "erp", record, {"pg_user": "odoo", "pg_password": "db-test"}, []
    )

    assert str(module) not in volumes
    assert not any(v["bind"].startswith(connector.MOUNT) for v in volumes.values())
    assert connector.MOUNT not in _generated_addons_paths(settings, team, monkeypatch)


def test_extra_repository_module_disables_the_managed_mount(configured, monkeypatch):
    """An extra-addon worktree shipping odumcp counts as the client's code."""
    from pathlib import Path

    from oduflow.docker_ops import production_ops
    from oduflow.naming import get_workspace_path, prod_env_name

    settings, team = configured
    module = connector.managed_module(team, "erp")
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    worktree = Path(get_workspace_path(prod_env_name("erp"), team.workspaces_dir))
    worktree = worktree / "extra" / "fleet"
    existing_module(worktree, "addons", "odumcp")
    record = production_registry.update_production(
        team, "erp", {"extra_addons": {"fleet": "main"}}
    )

    _, volumes, _ = production_ops._container_spec(
        settings,
        team,
        "erp",
        record,
        {"pg_user": "odoo", "pg_password": "db-test"},
        [(str(worktree), "/mnt/extra-addons-fleet")],
    )

    assert str(module) not in volumes
    paths = _generated_addons_paths(
        settings, team, monkeypatch, ["/mnt/extra-addons-fleet/addons"]
    )
    assert connector.MOUNT not in paths
    assert "/mnt/extra-addons-fleet/addons" in paths


def test_refresh_requires_an_explicit_production_name(configured, monkeypatch):
    """One refresh re-clones and recreates: never the whole team at once."""
    import asyncio

    from fastmcp.exceptions import ToolError

    from oduflow import server

    settings, team = configured
    monkeypatch.setattr(server, "_get_settings", lambda: settings)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx=None: team)
    synchronize = MagicMock(return_value={"name": "erp", "status": "ready"})
    monkeypatch.setattr(connector, "synchronize", synchronize)
    tool = server.mcp._tool_manager._tools["sync_production_mcp"].fn

    with pytest.raises(ToolError, match="name one production"):
        asyncio.run(tool(refresh=True))
    synchronize.assert_not_called()

    asyncio.run(tool(name="erp", refresh=True))
    synchronize.assert_called_once_with(settings, team, "erp", refresh=True)


def test_missing_addon_uses_configured_source(configured, monkeypatch):
    settings, team = configured

    def clone(url, ref, path, target_team, **kwargs):
        from pathlib import Path

        assert url == settings.prod_odumcp_repo_url
        # The branch follows the production's Odoo version.
        assert ref == "19.0"
        assert target_team == team
        module = Path(path) / "addons/odumcp"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text("{}")

    clone_mock = MagicMock(side_effect=clone)
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone_mock)
    connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    assert clone_mock.call_count == 1


def test_large_production_output_is_bound_to_team(configured, monkeypatch):
    import re

    from oduflow import server
    from oduflow.output_cache import OutputCache

    _, team = configured
    monkeypatch.setattr(server, "_output_cache", OutputCache())

    def resolve(ctx):
        assert ctx is None
        return team

    monkeypatch.setattr(server, "_resolve_team", resolve)
    summary = server._maybe_cache(
        "log line\n" * 2000, "Production logs", "production_logs", "name=erp"
    )
    output_id = re.search(r"id=([0-9a-f]{8})", summary).group(1)
    cached = server._output_cache.get(output_id)
    assert cached.production is True
    assert cached.team_id == team.team_id


@pytest.mark.parametrize(
    "operation",
    [
        "system.info",
        "records.search",
        "changes.preview",
        "changes.status",
        "changes.execute",
    ],
)
def test_failed_setup_blocks_only_odoo_calls(configured, monkeypatch, operation):
    settings, team = configured
    production_registry.update_production(
        team, "erp", {"mcp": {"status": "sync_failed"}}
    )
    client = MagicMock()
    monkeypatch.setattr(connector.httpx, "Client", client)
    with pytest.raises(PrerequisiteNotMetError, match="sync_production_mcp"):
        connector.execute(settings, team, "erp", operation)
    client.assert_not_called()


def test_successful_sync_restores_odoo_access(configured, monkeypatch):
    settings, team = configured
    production_registry.update_production(
        team, "erp", {"mcp": {"status": "sync_failed"}}
    )
    container = MagicMock(status="running")
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.production_ops._get_container", lambda *a: container
    )
    monkeypatch.setattr("oduflow.wal_monitor.assert_writable", lambda *a: None)
    monkeypatch.setattr(connector, "prepare_addon", lambda *a, **k: _prepared("kept"))
    monkeypatch.setattr(
        connector,
        "_shell",
        MagicMock(
            side_effect=[
                {"installed": True, "module_changed": True},
                {"changed": True},
            ]
        ),
    )
    result = connector.synchronize(settings, team, "erp")
    assert result["status"] == "ready"
    assert result["addon_source"] == "managed"
    assert result["container_recreated"] is False
    container.restart.assert_called_once()
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"ok": True, "data": {"connected": True}}
        ),
    )
    assert connector.execute(settings, team, "erp", "system.info")["connected"]


def test_failed_download_can_be_retried(configured, monkeypatch):
    from pathlib import Path

    settings, team = configured

    def fail(url, ref, path, *args, **kwargs):
        module = Path(path) / "addons/odumcp"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text("{}")
        raise RuntimeError("download interrupted")

    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    assert not connector.addon_checkout(team, "erp").exists()
    assert not list(
        connector.addon_checkout(team, "erp").parent.glob(".odumcp-download-*")
    )

    def succeed(url, ref, path, *args, **kwargs):
        module = Path(path) / "addons/odumcp"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text("{}")

    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", succeed)
    connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    assert (
        connector.addon_checkout(team, "erp") / "addons/odumcp/__manifest__.py"
    ).is_file()


def existing_module(root, *parts):
    from pathlib import Path

    module = Path(root).joinpath(*parts)
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text("{}")
    return module


def test_root_addon_is_used_only_when_odoo_scans_the_root(configured, monkeypatch):
    """The probe must match the one directory resolve_main_addons_path picks."""
    from pathlib import Path

    from oduflow.naming import get_repo_path, prod_env_name

    settings, team = configured
    repo = Path(get_repo_path(prod_env_name("erp"), team.workspaces_dir))
    existing_module(repo, "odumcp")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    clone.assert_not_called()

    # A top-level addons/ moves the generated addons_path off the repo root,
    # so the very same odumcp at the root is no longer visible to Odoo.
    (repo / "addons").mkdir()
    with pytest.raises(PrerequisiteNotMetError):
        connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    clone.assert_called_once()


def test_extra_repo_addon_probe_follows_its_addons_path(
    configured, monkeypatch, tmp_path
):
    settings, team = configured
    extra = tmp_path / "extra" / "fleet"
    existing_module(extra, "addons", "odumcp")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    connector.prepare_addon(
        settings, team, "erp", [(str(extra), "/mnt/extra-addons-fleet")], "odoo:19.0"
    )
    clone.assert_not_called()


def test_status_bookkeeping_never_masks_the_provisioning_error(configured, monkeypatch):
    """A production deleted mid-flight must not hide why provisioning failed."""
    settings, team = configured
    container = MagicMock(status="running")
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.production_ops._get_container", lambda *a: container
    )
    monkeypatch.setattr("oduflow.wal_monitor.assert_writable", lambda *a: None)

    def fail(*args):
        raise PrerequisiteNotMetError("module unavailable")

    def vanished(*args, **kwargs):
        raise NotFoundError("Production 'erp' not found.")

    monkeypatch.setattr(connector, "_shell", fail)
    monkeypatch.setattr(production_registry, "update_production", vanished)
    with pytest.raises(PrerequisiteNotMetError, match="module unavailable"):
        connector.provision(settings, team, "erp")


def test_list_productions_reports_mcp_status(configured, monkeypatch):
    """The dashboard flags a failed OduMCP setup on the production card."""
    from oduflow.docker_ops import production_ops

    settings, team = configured
    production_registry.create_production(team, "crm", {"domain": "crm.example.com"})
    production_registry.update_production(
        team, "erp", {"mcp": {"status": "sync_failed"}}
    )
    monkeypatch.setattr(production_ops, "get_client", MagicMock)

    listed = {
        prod["name"]: prod["mcp_status"]
        for prod in production_ops.list_productions(settings, team)
    }

    # A record from before OduMCP provisioning carries no status at all.
    assert listed == {"crm": "", "erp": "sync_failed"}


def _fake_clone(version):
    """A _clone_repo stand-in that publishes odumcp at *version*."""

    def clone(url, ref, path, *args, **kwargs):
        from pathlib import Path

        module = Path(path) / "addons/odumcp"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text(repr({"version": version}))

    return MagicMock(side_effect=clone)


@pytest.mark.parametrize("major", [16, 17, 18, 19, 20])
def test_addon_branch_follows_the_production_odoo_version(
    configured, monkeypatch, major
):
    settings, team = configured
    clone = _fake_clone(f"{major}.0.1.4.0")
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    prepared = connector.prepare_addon(settings, team, "erp", [], f"odoo:{major}.0")

    assert (prepared.managed, prepared.action, prepared.major) == (
        True,
        "cloned",
        major,
    )
    assert prepared.recreate
    assert clone.call_args.args[1] == f"{major}.0"


def test_odoo_before_16_has_no_managed_addon(configured, monkeypatch):
    settings, team = configured
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    with pytest.raises(PrerequisiteNotMetError, match="Odoo 16 and later"):
        connector.prepare_addon(settings, team, "erp", [], "odoo:15.0")
    clone.assert_not_called()


def test_upgraded_production_gets_its_new_version_addon(configured, monkeypatch):
    """A checkout made for Odoo 19 must not be mounted into Odoo 20."""
    settings, team = configured
    checkout = connector.addon_checkout(team, "erp")
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops._clone_repo", _fake_clone("19.0.1.4.2")
    )
    assert connector.prepare_addon(settings, team, "erp", [], "odoo:19.0").recreate

    same = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", same)
    kept = connector.prepare_addon(settings, team, "erp", [], "odoo:19.0")
    assert (kept.action, kept.recreate) == ("kept", False)
    same.assert_not_called()

    upgrade = _fake_clone("20.0.1.4.0")
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", upgrade)
    replaced = connector.prepare_addon(settings, team, "erp", [], "odoo:20.0")
    assert (replaced.action, replaced.major, replaced.recreate) == (
        "replaced",
        20,
        True,
    )
    assert upgrade.call_args.args[1] == "20.0"
    manifest = (checkout / "addons/odumcp/__manifest__.py").read_text()
    assert "20.0.1.4.0" in manifest
    assert not list(checkout.parent.glob(".odumcp-download-*"))


def test_unversioned_image_is_asked_for_its_odoo_version(configured, monkeypatch):
    settings, team = configured
    run = MagicMock(return_value=b"Odoo Server 18.0-20260901\n")
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", MagicMock)
    monkeypatch.setattr("oduflow.docker_ops.client.run_for_output", run)

    assert connector.odoo_major(settings, "acme/customer-erp:latest") == 18
    assert run.call_args.kwargs["entrypoint"] == "odoo"

    run.side_effect = RuntimeError("no odoo binary")
    with pytest.raises(PrerequisiteNotMetError, match="Cannot determine"):
        connector.odoo_major(settings, "acme/customer-erp:latest")


def _synced_container(monkeypatch, manifest):
    """A running container with the managed mount; *manifest* is what it sees."""
    container = MagicMock(status="running")
    container.attrs = {"Mounts": [{"Destination": connector.MOUNT + "/odumcp"}]}
    container.exec_run.return_value = (
        (0, repr(manifest).encode()) if manifest is not None else (1, b"")
    )
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: None)
    monkeypatch.setattr(
        "oduflow.docker_ops.production_ops._get_container", lambda *a: container
    )
    reconfigure = MagicMock()
    monkeypatch.setattr(
        "oduflow.docker_ops.production_ops.reconfigure_production", reconfigure
    )
    provision = MagicMock(return_value={"status": "ready"})
    monkeypatch.setattr(connector, "provision", provision)
    container.provision = provision
    return container, reconfigure


def _managed_checkout(team, version):
    module = connector.managed_module(team, "erp")
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text(repr({"version": version}))


def _prepared(action, major=19):
    managed = action not in ("repository", "removed")
    return connector.AddonPreparation(
        managed=managed, action=action, major=major if managed else None
    )


def test_sync_recreates_the_container_for_a_replaced_addon(configured, monkeypatch):
    """The old bind mount still shows the previous checkout to the container."""
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    _, reconfigure = _synced_container(monkeypatch, {"version": "19.0.1.4.2"})

    monkeypatch.setattr(connector, "prepare_addon", lambda *a, **k: _prepared("kept"))
    connector.synchronize(settings, team, "erp")
    reconfigure.assert_not_called()

    monkeypatch.setattr(
        connector, "prepare_addon", lambda *a, **k: _prepared("replaced")
    )
    connector.synchronize(settings, team, "erp")
    reconfigure.assert_called_once_with(settings, team, "erp", force_recreate=True)


@pytest.mark.parametrize("seen", [None, {"version": "18.0.1.4.0"}])
def test_retried_sync_repairs_a_failed_recreation(configured, monkeypatch, seen):
    """The checkout was already replaced by a sync whose recreation failed:
    the container sees a removed directory (None) or the old series."""
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    _, reconfigure = _synced_container(monkeypatch, seen)
    monkeypatch.setattr(connector, "prepare_addon", lambda *a, **k: _prepared("kept"))

    connector.synchronize(settings, team, "erp")

    reconfigure.assert_called_once_with(settings, team, "erp", force_recreate=True)


def _repository_module(team):
    """odumcp committed into the production's own repository."""
    from pathlib import Path

    from oduflow.naming import get_repo_path, prod_env_name

    repo = Path(get_repo_path(prod_env_name("erp"), team.workspaces_dir))
    return existing_module(repo, "addons", "odumcp")


def test_client_takeover_removes_the_managed_checkout(configured, monkeypatch):
    """Once the client repository ships odumcp, Oduflow's copy must not linger
    in the addons path, and the running container must lose its mount."""
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    _repository_module(team)
    _, reconfigure = _synced_container(monkeypatch, {"version": "19.0.1.4.2"})
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    result = connector.synchronize(settings, team, "erp")

    checkout = connector.addon_checkout(team, "erp")
    assert not checkout.exists()
    assert not list(checkout.parent.glob(".odumcp-retired-*"))
    clone.assert_not_called()
    reconfigure.assert_called_once_with(settings, team, "erp", force_recreate=True)
    assert result["addon_source"] == "repository"
    assert result["checkout"] == "removed"
    assert result["container_recreated"] is True
    assert "connector_branch" not in result


def test_client_module_without_a_checkout_leaves_the_container_alone(
    configured, monkeypatch
):
    settings, team = configured
    _repository_module(team)
    container, reconfigure = _synced_container(monkeypatch, None)
    container.attrs = {"Mounts": []}

    result = connector.synchronize(settings, team, "erp")

    reconfigure.assert_not_called()
    assert (result["addon_source"], result["checkout"]) == ("repository", "repository")
    assert result["container_recreated"] is False


def test_takeover_of_an_unmounted_checkout_skips_the_recreate(configured, monkeypatch):
    """The checkout exists but an earlier recreation failed before the
    container ever mounted it: removing it needs no downtime."""
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    _repository_module(team)
    container, reconfigure = _synced_container(monkeypatch, None)
    container.attrs = {"Mounts": []}

    result = connector.synchronize(settings, team, "erp")

    assert not connector.addon_checkout(team, "erp").exists()
    reconfigure.assert_not_called()
    assert (result["checkout"], result["container_recreated"]) == ("removed", False)


def test_lingering_managed_mount_is_dropped_after_a_takeover(configured, monkeypatch):
    """The checkout was removed by a sync whose recreation failed: the
    container still mounts it and must be recreated."""
    settings, team = configured
    _repository_module(team)
    _, reconfigure = _synced_container(monkeypatch, None)

    result = connector.synchronize(settings, team, "erp")

    reconfigure.assert_called_once_with(settings, team, "erp", force_recreate=True)
    assert result["container_recreated"] is True


def test_default_sync_never_refreshes_a_same_series_checkout(configured, monkeypatch):
    settings, team = configured
    production_registry.update_production(team, "erp", {"odoo_image": "odoo:19.0"})
    _managed_checkout(team, "19.0.1.4.2")
    container, reconfigure = _synced_container(monkeypatch, {"version": "19.0.1.4.2"})
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    result = connector.synchronize(settings, team, "erp")

    clone.assert_not_called()
    reconfigure.assert_not_called()
    assert (result["checkout"], result["refreshed"]) == ("kept", False)
    assert result["connector_branch"] == "19.0"
    assert result["container_recreated"] is False
    assert container.provision.call_args.kwargs == {"upgrade": False}


def test_refresh_reclones_the_same_series_and_recreates(configured, monkeypatch):
    settings, team = configured
    production_registry.update_production(team, "erp", {"odoo_image": "odoo:19.0"})
    _managed_checkout(team, "19.0.1.4.2")
    container, reconfigure = _synced_container(monkeypatch, {"version": "19.0.1.4.2"})
    clone = _fake_clone("19.0.1.5.0")
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    result = connector.synchronize(settings, team, "erp", refresh=True)

    # Re-cloned code of the same series may change data or views without a
    # manifest bump: the module upgrade is forced.
    assert container.provision.call_args.kwargs == {"upgrade": True}

    assert clone.call_args.args[0] == settings.prod_odumcp_repo_url
    assert clone.call_args.args[1] == "19.0"
    checkout = connector.addon_checkout(team, "erp")
    assert "19.0.1.5.0" in (checkout / "addons/odumcp/__manifest__.py").read_text()
    assert not list(checkout.parent.glob(".odumcp-download-*"))
    reconfigure.assert_called_once_with(settings, team, "erp", force_recreate=True)
    assert (result["checkout"], result["refreshed"]) == ("refreshed", True)
    assert result["connector_branch"] == "19.0"
    assert result["container_recreated"] is True


def test_refresh_reports_the_connector_commit(configured, monkeypatch):
    import subprocess

    settings, team = configured
    production_registry.update_production(team, "erp", {"odoo_image": "odoo:19.0"})
    _managed_checkout(team, "19.0.1.4.2")
    _, reconfigure = _synced_container(monkeypatch, {"version": "19.0.1.4.2"})
    heads = []

    def clone(url, ref, path, *args, **kwargs):
        from pathlib import Path

        module = Path(path) / "addons/odumcp"
        module.mkdir(parents=True)
        (module / "__manifest__.py").write_text(repr({"version": "19.0.1.5.0"}))
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", path]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "release"], check=True)
        heads.append(
            subprocess.run(
                [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
        )

    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    result = connector.synchronize(settings, team, "erp", refresh=True)

    assert result["connector_commit"] == heads[0]
    assert len(result["connector_commit"]) == 40


def test_refresh_is_a_no_op_for_a_client_shipped_module(configured, monkeypatch):
    settings, team = configured
    _repository_module(team)
    container, reconfigure = _synced_container(monkeypatch, None)
    container.attrs = {"Mounts": []}
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)

    result = connector.synchronize(settings, team, "erp", refresh=True)

    clone.assert_not_called()
    reconfigure.assert_not_called()
    assert not connector.addon_checkout(team, "erp").exists()
    assert result["addon_source"] == "repository"
    assert result["refreshed"] is False
    assert "does not touch" in result["note"]


def test_refresh_without_a_known_odoo_version_is_refused(configured, monkeypatch):
    """An explicit refresh cannot pick a connector branch blindly."""
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", MagicMock)
    monkeypatch.setattr(
        "oduflow.docker_ops.client.run_for_output",
        MagicMock(side_effect=RuntimeError("no odoo binary")),
    )

    with pytest.raises(PrerequisiteNotMetError, match="Cannot determine"):
        connector.prepare_addon(
            settings, team, "erp", [], "acme/erp:latest", refresh=True
        )
    clone.assert_not_called()
    assert (connector.addon_checkout(team, "erp") / "addons/odumcp").is_dir()


def test_unsupported_odoo_is_not_reported_as_a_failed_sync(configured, monkeypatch):
    settings, team = configured
    production_registry.update_production(team, "erp", {"odoo_image": "odoo:15.0"})
    _synced_container(monkeypatch, None)

    with pytest.raises(connector.UnsupportedOdooVersion):
        connector.synchronize(settings, team, "erp")

    record = production_registry.get_production(team, "erp")
    assert record["mcp"]["status"] == "unsupported"
    with pytest.raises(PrerequisiteNotMetError, match="Odoo 16 and later") as exc:
        connector.execute(settings, team, "erp", "system.info")
    assert "sync_production_mcp" not in str(exc.value)


def test_provision_refuses_an_addon_of_another_odoo_series(configured, monkeypatch):
    settings, team = configured
    provision = connector.provision
    container, _ = _synced_container(monkeypatch, {"version": "18.0.1.4.0"})
    container.labels = {settings.image_label: "odoo:19.0"}
    monkeypatch.setattr("oduflow.wal_monitor.assert_writable", lambda *a: None)
    shell = MagicMock()
    monkeypatch.setattr(connector, "_shell", shell)

    with pytest.raises(PrerequisiteNotMetError, match="for Odoo 18, but"):
        provision(settings, team, "erp")

    shell.assert_not_called()
    record = production_registry.get_production(team, "erp")
    assert record["mcp"]["status"] == "sync_failed"


def test_unknown_version_keeps_an_existing_checkout(configured, monkeypatch):
    settings, team = configured
    _managed_checkout(team, "19.0.1.4.2")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", MagicMock)
    monkeypatch.setattr(
        "oduflow.docker_ops.client.run_for_output",
        MagicMock(side_effect=RuntimeError("no odoo binary")),
    )

    kept = connector.prepare_addon(settings, team, "erp", [], "acme/erp:latest")
    assert (kept.managed, kept.action, kept.major) == (True, "kept", 19)
    clone.assert_not_called()

    connector.addon_checkout(team, "erp").rename(
        connector.addon_checkout(team, "erp").with_name("gone")
    )
    with pytest.raises(PrerequisiteNotMetError, match="Cannot determine"):
        connector.prepare_addon(settings, team, "erp", [], "acme/erp:latest")
