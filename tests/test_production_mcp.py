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
    production_registry.create_production(
        team, "erp", {"domain": "erp.example.com", "branch": "main"}
    )
    return settings, team


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        connector.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def git(*args, cwd=None):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def production_repo(team, tmp_path, *, nested=False):
    """A production checkout of a bare origin, as create_production leaves it."""
    from pathlib import Path

    from oduflow.naming import get_repo_path, prod_env_name

    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin))
    repo = Path(get_repo_path(prod_env_name("erp"), team.workspaces_dir))
    repo.parent.mkdir(parents=True, exist_ok=True)
    git("clone", str(origin), str(repo))
    seed = repo / ("addons/sale_ext/__manifest__.py" if nested else "README.md")
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("{}")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed", cwd=repo)
    git("push", "origin", "HEAD:refs/heads/main", cwd=repo)
    return origin, repo


def connector_clone(url, ref, path, *args, **kwargs):
    from pathlib import Path

    module = Path(path) / "addons/odumcp"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    (Path(path) / "addons/other").mkdir()


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
    connector.prepare_addon(settings, team, "erp", [])
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

    def synchronize(settings, team, name):
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


@pytest.mark.parametrize("nested", [False, True])
def test_missing_addon_is_vendored_into_production_repo(
    configured, monkeypatch, tmp_path, nested
):
    settings, team = configured
    origin, repo = production_repo(team, tmp_path, nested=nested)

    def clone(url, ref, path, target_team, **kwargs):
        assert url == settings.prod_odumcp_repo_url
        assert ref == settings.prod_odumcp_ref
        assert target_team == team
        connector_clone(url, ref, path)

    clone_mock = MagicMock(side_effect=clone)
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone_mock)
    connector.prepare_addon(settings, team, "erp", [])
    connector.prepare_addon(settings, team, "erp", [])
    assert clone_mock.call_count == 1

    # Only odumcp lands in the directory Odoo scans, and origin has the commit.
    module = "addons/odumcp" if nested else "odumcp"
    assert (repo / module / "__manifest__.py").is_file()
    assert not (repo / "addons/other").exists()
    assert git("status", "--porcelain", cwd=repo) == ""
    assert git("rev-parse", "HEAD", cwd=repo) == git("rev-parse", "main", cwd=origin)
    assert module + "/__manifest__.py" in git(
        "show", "--name-only", "--format=%an", "main", cwd=origin
    )


def test_rejected_push_leaves_no_local_commit(configured, monkeypatch, tmp_path):
    settings, team = configured
    origin, repo = production_repo(team, tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo)
    hook = origin / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", connector_clone)
    with pytest.raises(PrerequisiteNotMetError, match="push access"):
        connector.prepare_addon(settings, team, "erp", [])
    assert git("rev-parse", "HEAD", cwd=repo) == head
    assert not (repo / "odumcp").exists()
    assert git("status", "--porcelain", cwd=repo) == ""


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
    monkeypatch.setattr(connector, "prepare_addon", lambda *a: None)
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
    assert connector.synchronize(settings, team, "erp")["status"] == "ready"
    container.restart.assert_called_once()
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"ok": True, "data": {"connected": True}}
        ),
    )
    assert connector.execute(settings, team, "erp", "system.info")["connected"]


def test_failed_download_can_be_retried(configured, monkeypatch, tmp_path):
    settings, team = configured
    _, repo = production_repo(team, tmp_path)

    def fail(url, ref, path, *args, **kwargs):
        connector_clone(url, ref, path)
        raise RuntimeError("download interrupted")

    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", fail)
    with pytest.raises(PrerequisiteNotMetError, match="interrupted"):
        connector.prepare_addon(settings, team, "erp", [])
    assert not (repo / "odumcp").exists()

    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", connector_clone)
    connector.prepare_addon(settings, team, "erp", [])
    assert (repo / "odumcp/__manifest__.py").is_file()


def existing_module(root, *parts):
    from pathlib import Path

    module = Path(root).joinpath(*parts)
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text("{}")
    return module


def test_root_addon_is_used_only_when_odoo_scans_the_root(
    configured, monkeypatch, tmp_path
):
    """The probe must match the one directory resolve_main_addons_path picks."""
    settings, team = configured
    _, repo = production_repo(team, tmp_path)
    existing_module(repo, "odumcp")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    connector.prepare_addon(settings, team, "erp", [])
    clone.assert_not_called()

    # A top-level addons/ moves the generated addons_path off the repo root,
    # so the very same odumcp at the root is no longer visible to Odoo.
    (repo / "addons").mkdir()
    with pytest.raises(PrerequisiteNotMetError):
        connector.prepare_addon(settings, team, "erp", [])
    clone.assert_called_once()
    assert not (repo / "addons/odumcp").exists()


def test_extra_repo_addon_probe_follows_its_addons_path(
    configured, monkeypatch, tmp_path
):
    settings, team = configured
    extra = tmp_path / "extra" / "fleet"
    existing_module(extra, "addons", "odumcp")
    clone = MagicMock()
    monkeypatch.setattr("oduflow.docker_ops.env_ops._clone_repo", clone)
    connector.prepare_addon(
        settings, team, "erp", [(str(extra), "/mnt/extra-addons-fleet")]
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
