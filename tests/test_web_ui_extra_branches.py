"""Dashboard REST API behind the extra-repo Branches dialog.

The dialog lists downloaded and remote branches with their consumers,
refreshes the remote list, downloads selected branches and removes a
downloaded one. Branch names travel in JSON bodies (they may contain "/").
The git work itself is covered by test_extra_addons_clone.py; here the
extra_addons functions are patched.
"""

import asyncio
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import ConflictError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_branches_lists_downloaded_remote_and_usage(tmp_path):
    client = _client(tmp_path)
    repo = {
        "name": "enterprise",
        "local": False,
        "branches": ["18.0"],
        "available_branches": ["17.0", "18.0"],
        "available_known": True,
        "protected": False,
    }
    with (
        patch("oduflow.extra_addons.get_extra_repo", return_value=repo),
        patch(
            "oduflow.extra_addons.branch_usage",
            return_value={"18.0": ["feature-x"]},
        ),
    ):
        resp = client.get("/api/extra-repos/enterprise/branches")
    assert resp.json() == {
        "ok": True,
        "name": "enterprise",
        "local": False,
        "downloaded": ["18.0"],
        "available": ["17.0", "18.0"],
        "available_known": True,
        "protected": False,
        "usage": {"18.0": ["feature-x"]},
    }


def test_add_branches_passes_the_list(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.extra_addons.track_branches", return_value={"name": "enterprise"}
    ) as track:
        resp = client.post(
            "/api/extra-repos/enterprise/branches",
            json={"branches": ["16.0", "feature/x"]},
        )
    assert resp.json()["ok"] is True
    assert track.call_args.args[1:] == ("enterprise", ["16.0", "feature/x"])


def test_add_branches_rejects_bad_body(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.extra_addons.track_branches") as track:
        for body in ({}, {"branches": []}, {"branches": "16.0"}, {"branches": [1]}):
            resp = client.post("/api/extra-repos/enterprise/branches", json=body)
            assert resp.status_code == 400
    track.assert_not_called()


def test_add_branches_reports_invalid_branch_name(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.extra_addons.track_branches",
        side_effect=ValueError("Invalid branch name '-x'."),
    ):
        resp = client.post(
            "/api/extra-repos/enterprise/branches", json={"branches": ["-x"]}
        )
    assert resp.status_code == 400
    assert "Invalid branch name" in resp.json()["error"]


def test_remove_branch_passes_branch_from_body(tmp_path):
    client = _client(tmp_path)
    result = {"name": "enterprise", "branch": "feature/x", "freed_bytes": 10}
    with patch("oduflow.extra_addons.untrack_branch", return_value=result) as untrack:
        resp = client.post(
            "/api/extra-repos/enterprise/branches/remove",
            json={"branch": " feature/x "},
        )
    assert resp.json() == {"ok": True, "result": result}
    assert untrack.call_args.args[2:] == ("enterprise", "feature/x")


def test_remove_branch_in_use_is_a_conflict(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.extra_addons.untrack_branch",
        side_effect=ConflictError("used by feature-x"),
    ):
        resp = client.post(
            "/api/extra-repos/enterprise/branches/remove", json={"branch": "18.0"}
        )
    assert resp.json()["ok"] is False
    assert "feature-x" in resp.json()["error"]


def test_remove_branch_requires_branch(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.extra_addons.untrack_branch") as untrack:
        resp = client.post("/api/extra-repos/enterprise/branches/remove", json={})
    assert resp.status_code == 400
    untrack.assert_not_called()


def test_refresh_remote_branches(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.extra_addons.refresh_remote_branches", return_value=["17.0", "18.0"]
    ):
        resp = client.post("/api/extra-repos/enterprise/remote-branches")
    assert resp.json() == {"ok": True, "available": ["17.0", "18.0"]}


def _call_update_extra_repo(monkeypatch, add_branch):
    from oduflow import server

    team = TeamSettings(team_id="1")
    monkeypatch.setattr(server, "_resolve_team", lambda ctx=None: team)
    summary = {
        "name": "enterprise",
        "up_to_date": True,
        "new_branches": [],
        "deleted_branches": [],
        "updated_branches": [],
    }
    with (
        patch("oduflow.extra_addons.track_branches") as track,
        patch("oduflow.extra_addons.fetch_extra_repo", return_value=summary),
    ):
        out = server.mcp._tool_manager._tools["update_extra_repo"].fn(
            name="enterprise", add_branch=add_branch
        )
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
    assert "already up to date" in out
    return track


def test_mcp_update_extra_repo_accepts_branch_list(monkeypatch):
    track = _call_update_extra_repo(monkeypatch, ["16.0", " 17.0", ""])
    assert track.call_args.args[1:] == ("enterprise", ["16.0", "17.0"])


def test_mcp_update_extra_repo_keeps_commas_in_a_branch_name(monkeypatch):
    # git allows commas in branch names; a string is always one branch.
    track = _call_update_extra_repo(monkeypatch, " release,2024 ")
    assert track.call_args.args[1:] == ("enterprise", ["release,2024"])


def test_mcp_update_extra_repo_without_branch_only_updates(monkeypatch):
    track = _call_update_extra_repo(monkeypatch, "")
    track.assert_not_called()
