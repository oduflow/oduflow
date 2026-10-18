"""Server-side OCA batch import: validation, cloning and dashboard API."""

import threading
import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import oca_import
from oduflow.errors import BusyError, ExternalCommandError, NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_NAMES = ["web", "tools", "sale", "stock", "account", "hr"]


def _row(name):
    return {
        "name": name,
        "extra_name": "oca-" + name,
        "repo_url": f"https://github.com/OCA/{name}.git",
        "description": "",
        "stars": 0,
        "archived": False,
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(oca_import, "_jobs", {})
    catalogue = {"repos": [_row(name) for name in _NAMES], "stale": False}
    with (
        patch("oduflow.oca_catalog.list_oca_repositories", return_value=catalogue),
        patch("oduflow.extra_addons.list_extra_repos", return_value=[]) as existing,
    ):
        yield existing


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


def _public(exc):
    return "Command failed." if isinstance(exc, ExternalCommandError) else str(exc)


def _wait(team):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = oca_import.get_job(team)
        if job["status"] == "finished":
            return job
        time.sleep(0.01)
    raise AssertionError("OCA import did not finish")


def _statuses(job):
    return {item["name"]: (item["status"], item["error"]) for item in job["items"]}


@pytest.mark.parametrize(
    "selections",
    [
        None,
        [],
        ["web"],
        [{"name": "unknown", "branches": ["18.0"]}],
        [{"name": 17}],
        [{"name": "web", "branches": "18.0"}],
        [{"name": "web", "branches": [18]}],
        [{"name": "web"}, {"name": "web", "branches": ["18.0"]}],
        [{"name": "web"}] * (oca_import.MAX_REPOS + 1),
    ],
)
def test_invalid_batches_are_rejected_before_cloning(team, selections):
    with patch("oduflow.extra_addons.register_extra_repo") as clone:
        with pytest.raises(ValueError):
            oca_import.start_import(team, selections, _public)
    clone.assert_not_called()
    assert oca_import.get_job(team) is None


def test_clone_targets_come_from_the_catalogue(team):
    selections = [
        {
            "name": "web",
            "branches": ["18.0", "18.0", "19.0"],
            "repo_url": "https://attacker.invalid/x.git",
            "extra_name": "elsewhere",
        },
        {"name": "tools", "branches": [" "]},
    ]
    with patch("oduflow.extra_addons.register_extra_repo") as clone:
        oca_import.start_import(team, selections, _public)
        job = _wait(team)
    calls = sorted(
        call.args[1:] + (call.kwargs["branches"],) for call in clone.call_args_list
    )
    assert calls == [
        ("oca-tools", "https://github.com/OCA/tools.git", []),
        ("oca-web", "https://github.com/OCA/web.git", ["18.0", "19.0"]),
    ]
    assert _statuses(job) == {"web": ("done", ""), "tools": ("done", "")}


def test_existing_origin_is_skipped_and_taken_name_fails(team, isolated):
    isolated.return_value = [
        {"name": "custom-web", "repo_url": "git@github.com:OCA/web.git"},
        {"name": "oca-tools", "repo_url": "https://example.com/other.git"},
    ]
    selections = [
        {"name": name, "branches": ["18.0"]} for name in ("web", "tools", "sale")
    ]
    with patch("oduflow.extra_addons.register_extra_repo") as clone:
        oca_import.start_import(team, selections, _public)
        job = _wait(team)
    assert [call.args[1] for call in clone.call_args_list] == ["oca-sale"]
    assert _statuses(job) == {
        "web": ("skipped", "Already added as custom-web"),
        "tools": ("error", "Name already in use: oca-tools"),
        "sale": ("done", ""),
    }


def test_three_clones_at_most_and_failures_stay_per_repository(team):
    active, peak = 0, 0
    guard = threading.Lock()

    def clone(team, name, repo_url, branches):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        if name == "oca-tools":
            raise ExternalCommandError("git clone", 128, "fatal: /srv/secret path")
        if name == "oca-sale":
            raise ValueError("Invalid branch name 'x'.")
        if name == "oca-stock":
            raise RuntimeError("boom")
        if name == "oca-account":
            raise NotFoundError("Branch(es) not found: 20.0.")

    selections = [{"name": name, "branches": ["18.0"]} for name in _NAMES]
    with patch("oduflow.extra_addons.register_extra_repo", side_effect=clone):
        oca_import.start_import(team, selections, _public)
        job = _wait(team)
    assert peak == 3
    assert _statuses(job) == {
        "web": ("done", ""),
        "tools": ("error", "Command failed."),
        "sale": ("error", "Invalid branch name 'x'."),
        "stock": ("error", "Internal error."),
        "account": ("error", "Branch(es) not found: 20.0."),
        "hr": ("done", ""),
    }
    assert job["finished_at"]


def test_one_running_import_per_team(team, tmp_path):
    other = TeamSettings(team_id="2", data_dir=str(tmp_path / "other"))
    release = threading.Event()
    selection = [{"name": "web", "branches": ["18.0"]}]
    with patch(
        "oduflow.extra_addons.register_extra_repo",
        side_effect=lambda *a, **kw: release.wait(5),
    ):
        oca_import.start_import(team, selection, _public)
        with pytest.raises(BusyError):
            oca_import.start_import(team, selection, _public)
        oca_import.start_import(other, selection, _public)
        assert oca_import.get_job(team)["status"] == "running"
        release.set()
        _wait(team)
        _wait(other)
        oca_import.start_import(team, selection, _public)
        _wait(team)


def _client(tmp_path, password=""):
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", ui_password=password)},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings.teams["1"]


def test_import_api_starts_and_reports_the_job(tmp_path):
    client, team = _client(tmp_path)
    assert client.get("/api/extra-repos/oca/import").json() == {"ok": True, "job": None}
    with patch(
        "oduflow.extra_addons.register_extra_repo",
        side_effect=ExternalCommandError("git clone", 128, "fatal: /srv/secret path"),
    ):
        response = client.post(
            "/api/extra-repos/oca/import",
            json={"repos": [{"name": "web", "branches": ["18.0"]}]},
        )
        assert response.status_code == 202
        assert response.json()["job"]["items"][0]["name"] == "web"
        _wait(team)
    job = client.get("/api/extra-repos/oca/import").json()["job"]
    assert job["status"] == "finished"
    assert job["items"][0]["status"] == "error"
    assert "secret" not in job["items"][0]["error"]


def test_import_api_rejects_invalid_and_concurrent_batches(tmp_path):
    client, team = _client(tmp_path)
    bad = client.post("/api/extra-repos/oca/import", json={"repos": [{"name": "x"}]})
    assert bad.status_code == 400
    assert "not in the OCA catalogue" in bad.json()["error"]
    release = threading.Event()
    with patch(
        "oduflow.extra_addons.register_extra_repo",
        side_effect=lambda *a, **kw: release.wait(5),
    ):
        body = {"repos": [{"name": "web"}]}
        assert client.post("/api/extra-repos/oca/import", json=body).status_code == 202
        busy = client.post("/api/extra-repos/oca/import", json=body)
        release.set()
        _wait(team)
    assert busy.status_code == 409


def test_import_api_requires_dashboard_auth(tmp_path):
    client, _ = _client(tmp_path, password="test-password")
    with patch("oduflow.extra_addons.register_extra_repo") as clone:
        posted = client.post(
            "/api/extra-repos/oca/import",
            json={"repos": [{"name": "web"}]},
        )
        polled = client.get("/api/extra-repos/oca/import")
    assert (posted.status_code, polled.status_code) == (401, 401)
    clone.assert_not_called()
