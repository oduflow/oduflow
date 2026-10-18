"""OCA catalogue pagination, limits, caching and dashboard access."""

import threading
from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import oca_catalog
from oduflow.errors import PrerequisiteNotMetError
from oduflow.extra_addons import validate_extra_repo_name
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


@pytest.fixture(autouse=True)
def empty_catalogue(monkeypatch):
    monkeypatch.setattr(oca_catalog, "_cached", [])
    monkeypatch.setattr(oca_catalog, "_expires", 0)
    monkeypatch.setattr(oca_catalog, "_error", "")
    monkeypatch.setattr(oca_catalog, "_error_until", 0)


def _repo(name, **kwargs):
    return {"name": name, "owner": {"login": "OCA"}, **kwargs}


def _row(name):
    return {
        "name": name,
        "extra_name": "oca-" + name,
        "repo_url": f"https://github.com/OCA/{name}.git",
        "description": "",
        "stars": 0,
        "archived": False,
    }


def _mock_github(monkeypatch, handler):
    requests = []

    def recording(request):
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    monkeypatch.setattr(oca_catalog.httpx, "Client", lambda **kwargs: client)
    return requests


def test_all_pages_and_cache_do_not_share_mutable_team_state(monkeypatch):
    def handler(request):
        assert request.url.host == "api.github.com"
        page = int(request.url.params["page"])
        items = (
            [_repo(f"repo-{i}") for i in range(100)]
            if page == 1
            else [
                _repo("server-tools", description="Tools", stargazers_count=123),
                _repo("old", archived=True),
                _repo("dot.name", clone_url="https://untrusted.invalid/repo"),
                _repo("../escape"),
                _repo("foreign", owner={"login": "other"}),
            ]
        )
        return httpx.Response(200, json=items)

    requests = _mock_github(monkeypatch, handler)
    catalogue = oca_catalog.list_oca_repositories()
    rows = catalogue["repos"]
    assert catalogue["stale"] is False
    assert len(rows) == 103
    assert rows[100] == {
        "name": "server-tools",
        "extra_name": "oca-server-tools",
        "repo_url": "https://github.com/OCA/server-tools.git",
        "description": "Tools",
        "stars": 123,
        "archived": False,
    }
    assert rows[101]["archived"] is True
    assert rows[102]["extra_name"] == "oca-dot-name"
    assert rows[102]["repo_url"] == "https://github.com/OCA/dot.name.git"
    rows[0]["name"] = "changed-by-caller"
    assert oca_catalog.list_oca_repositories()["repos"][0]["name"] == "repo-0"
    assert len(requests) == 2


@pytest.mark.parametrize("status", [403, 429, 500])
def test_failed_page_is_not_cached_and_not_retried_at_once(monkeypatch, status):
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[_repo(f"repo-{i}") for i in range(100)])
        return httpx.Response(status)

    requests = _mock_github(monkeypatch, handler)
    with pytest.raises(PrerequisiteNotMetError) as first:
        oca_catalog.list_oca_repositories()
    assert oca_catalog._cached == []
    assert oca_catalog._expires == 0
    # Other dialogs opened during the error window get the same answer, without
    # spending more of the shared unauthenticated GitHub quota.
    with pytest.raises(PrerequisiteNotMetError, match=str(first.value)):
        oca_catalog.list_oca_repositories()
    assert len(requests) == 2


def test_refresh_failure_serves_the_stale_catalogue(monkeypatch):
    monkeypatch.setattr(oca_catalog, "_cached", [_row("stale")])
    requests = _mock_github(monkeypatch, lambda request: httpx.Response(500))
    for _ in range(2):
        catalogue = oca_catalog.list_oca_repositories()
        assert catalogue == {"repos": [_row("stale")], "stale": True}
    assert len(requests) == 1


def test_expired_cache_refreshes(monkeypatch):
    monkeypatch.setattr(oca_catalog, "_cached", [_row("stale")])
    _mock_github(monkeypatch, lambda request: httpx.Response(200, json=[_repo("new")]))
    catalogue = oca_catalog.list_oca_repositories()
    assert [row["name"] for row in catalogue["repos"]] == ["new"]
    assert catalogue["stale"] is False


def test_malformed_items_are_skipped_not_fatal(monkeypatch):
    items = [
        None,
        {"name": 17},
        _repo("no-owner", owner=None),
        _repo("no-login", owner={"login": None}),
        _repo("ok", stargazers_count="many"),
    ]
    _mock_github(monkeypatch, lambda request: httpx.Response(200, json=items))
    rows = oca_catalog.list_oca_repositories()["repos"]
    assert [(row["name"], row["stars"]) for row in rows] == [("ok", 0)]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"message": "bad"}),
        httpx.Response(200, content=b"<html>"),
    ],
)
def test_invalid_response_is_actionable(monkeypatch, response):
    _mock_github(monkeypatch, lambda request: response)
    with pytest.raises(PrerequisiteNotMetError, match="invalid OCA catalogue"):
        oca_catalog.list_oca_repositories()


def test_empty_catalogue_is_an_error_not_a_cached_result(monkeypatch):
    _mock_github(monkeypatch, lambda request: httpx.Response(200, json=[]))
    with pytest.raises(PrerequisiteNotMetError, match="empty OCA catalogue"):
        oca_catalog.list_oca_repositories()
    assert oca_catalog._cached == []
    assert oca_catalog._expires == 0


def test_endless_pagination_stops_at_the_page_cap(monkeypatch):
    full = [_repo(f"repo-{i}") for i in range(100)]
    requests = _mock_github(monkeypatch, lambda request: httpx.Response(200, json=full))
    with pytest.raises(PrerequisiteNotMetError, match="invalid OCA catalogue"):
        oca_catalog.list_oca_repositories()
    assert len(requests) == oca_catalog._MAX_PAGES
    assert oca_catalog._cached == []


def test_oversized_page_is_rejected(monkeypatch):
    monkeypatch.setattr(oca_catalog, "_MAX_PAGE_BYTES", 64)
    body = [_repo("tools", description="x" * 200)]
    _mock_github(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(PrerequisiteNotMetError, match="invalid OCA catalogue"):
        oca_catalog.list_oca_repositories()


def test_slow_github_hits_the_deadline(monkeypatch):
    monkeypatch.setattr(oca_catalog, "_DEADLINE", 0)
    _mock_github(monkeypatch, lambda request: httpx.Response(200, json=[_repo("a")]))
    with pytest.raises(PrerequisiteNotMetError, match="too slowly"):
        oca_catalog.list_oca_repositories()


def test_concurrent_callers_share_one_fetch(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def handler(request):
        entered.set()
        release.wait(5)
        return httpx.Response(200, json=[_repo("tools")])

    requests = _mock_github(monkeypatch, handler)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(oca_catalog.list_oca_repositories())
        )
        for _ in range(3)
    ]
    threads[0].start()
    assert entered.wait(5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(requests) == 1
    assert [len(result["repos"]) for result in results] == [1, 1, 1]


def test_import_names_are_unique_and_valid(monkeypatch):
    names = ["foo.bar", "foo-bar", "x" * 61, "x" * 60, "x" * 59]
    _mock_github(
        monkeypatch,
        lambda request: httpx.Response(200, json=[_repo(name) for name in names]),
    )
    rows = {
        row["name"]: row["extra_name"]
        for row in oca_catalog.list_oca_repositories()["repos"]
    }
    # Names that need no change keep the plain form; the others get suffixes.
    assert rows["foo-bar"] == "oca-foo-bar"
    assert rows["foo.bar"] == "oca-foo-bar-2"
    assert rows["x" * 59] == "oca-" + "x" * 59
    assert rows["x" * 60] == "oca-" + "x" * 57 + "-2"
    assert rows["x" * 61] == "oca-" + "x" * 57 + "-3"
    assert len(set(rows.values())) == len(names)
    for extra_name in rows.values():
        validate_extra_repo_name(extra_name)


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/OCA/server-tools.git",
        "https://github.com/oca/server-tools",
        "https://www.github.com/OCA/server-tools/",
        "https://user@github.com/OCA/server-tools.git",
        "git@github.com:OCA/server-tools.git",
        "ssh://git@github.com/OCA/server-tools.git",
    ],
)
def test_existing_repositories_are_matched_by_origin(origin):
    rows = oca_catalog.annotate_existing(
        [_row("server-tools")], [{"name": "tools", "repo_url": origin}]
    )
    assert rows[0]["existing_as"] == "tools"
    assert rows[0]["name_taken"] is False


def test_unrelated_repository_with_the_import_name_is_reported():
    rows = oca_catalog.annotate_existing(
        [_row("sale"), _row("web")],
        [
            {"name": "oca-sale", "repo_url": "https://github.com/elsewhere/sale.git"},
            {"name": "web-fork", "repo_url": "https://evilgithub.com/OCA/web.git"},
        ],
    )
    assert (rows[0]["existing_as"], rows[0]["name_taken"]) == ("", True)
    assert (rows[1]["existing_as"], rows[1]["name_taken"]) == ("", False)


def _client(tmp_path, password=""):
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", ui_password=password)},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings.teams["1"]


def test_catalogue_api_annotates_current_team_repositories(tmp_path):
    client, team = _client(tmp_path)
    existing = [{"name": "tools", "repo_url": "git@github.com:OCA/server-tools.git"}]
    with (
        patch(
            "oduflow.oca_catalog.list_oca_repositories",
            return_value={"repos": [_row("server-tools")], "stale": True},
        ) as catalogue,
        patch("oduflow.extra_addons.list_extra_repos", return_value=existing) as listed,
    ):
        response = client.get("/api/extra-repos/oca")
    data = response.json()
    assert data["ok"] is True
    assert data["stale"] is True
    assert data["job"] is None
    assert data["repos"][0]["existing_as"] == "tools"
    assert "existing" not in data
    catalogue.assert_called_once_with()
    listed.assert_called_once_with(team)


def test_catalogue_api_requires_dashboard_auth(tmp_path):
    client, _ = _client(tmp_path, password="test-password")
    with patch("oduflow.oca_catalog.list_oca_repositories") as catalogue:
        response = client.get("/api/extra-repos/oca")
    assert response.status_code == 401
    catalogue.assert_not_called()


def test_catalogue_api_reports_upstream_failure(tmp_path):
    client, _ = _client(tmp_path)
    with (
        patch(
            "oduflow.oca_catalog.list_oca_repositories",
            side_effect=PrerequisiteNotMetError(
                "GitHub has limited catalogue requests."
            ),
        ),
        patch("oduflow.extra_addons.list_extra_repos", return_value=[]),
    ):
        response = client.get("/api/extra-repos/oca")
    assert response.json() == {
        "ok": False,
        "error": "GitHub has limited catalogue requests.",
    }
