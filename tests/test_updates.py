"""Tests for oduflow.updates and the dashboard version endpoint — no network."""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import updates
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _release_payload(tag: str = "v9.0.0") -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "name": "Nine point oh",
            "published_at": "2026-09-19T14:05:12Z",
            "html_url": f"https://github.com/oduflow/oduflow/releases/tag/{tag}",
        }
    ).encode()


class _Response(io.BytesIO):
    """Minimal urlopen context manager over a canned body."""

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _client(tmp_path) -> TestClient:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


class TestCheckForUpdate:
    @patch("oduflow.updates.urlopen")
    def test_newer_release_is_an_update(self, mock_urlopen):
        mock_urlopen.return_value = _Response(_release_payload("v9.0.0"))

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_UPDATE
        assert result.latest == "9.0.0"
        assert result.release_title == "Nine point oh"
        assert result.release_url.endswith("/releases/tag/v9.0.0")
        assert result.error == ""

    @patch("oduflow.updates.urlopen")
    def test_same_release_is_up_to_date(self, mock_urlopen):
        mock_urlopen.return_value = _Response(_release_payload("v1.79.0"))

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_CURRENT
        assert result.latest == "1.79.0"

    @patch("oduflow.updates.urlopen")
    def test_newer_local_build_is_not_an_update(self, mock_urlopen):
        mock_urlopen.return_value = _Response(_release_payload("v1.79.0"))

        result = updates.check_for_update("1.80.0.dev1")

        assert result.status == updates.STATUS_CURRENT

    @patch("oduflow.updates.urlopen")
    def test_source_checkout_is_not_comparable(self, mock_urlopen):
        mock_urlopen.return_value = _Response(_release_payload("v9.0.0"))

        result = updates.check_for_update("dev")

        # "dev" is not a release version: neither "older" nor "newer" is true.
        assert result.status == updates.STATUS_UNKNOWN
        assert result.latest == "9.0.0"

    @patch("oduflow.updates.urlopen")
    def test_request_is_a_plain_get_with_branded_agent(self, mock_urlopen):
        mock_urlopen.return_value = _Response(_release_payload())

        updates.check_for_update("1.79.0")

        request = mock_urlopen.call_args[0][0]
        assert request.get_method() == "GET"
        assert request.data is None  # nothing about this install is sent
        assert request.full_url == updates.LATEST_RELEASE_URL
        assert request.get_header("User-agent", "").startswith("oduflow/")

    @patch("oduflow.updates.urlopen")
    def test_rate_limit_is_reported_as_a_readable_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            updates.LATEST_RELEASE_URL, 403, "rate limited", {}, None
        )

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_ERROR
        assert "rate limit" in result.error
        assert result.current == "1.79.0"
        assert result.release_url == updates.RELEASES_PAGE_URL

    @patch("oduflow.updates.urlopen")
    def test_offline_host_is_reported_not_raised(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("Name or service not known")

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_ERROR
        assert "github.com" in result.error

    @patch("oduflow.updates.urlopen")
    def test_unreadable_reply_is_reported(self, mock_urlopen):
        mock_urlopen.return_value = _Response(b"<html>not json</html>")

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_ERROR

    @patch("oduflow.updates.urlopen")
    def test_missing_tag_is_reported(self, mock_urlopen):
        mock_urlopen.return_value = _Response(json.dumps({"name": "untagged"}).encode())

        result = updates.check_for_update("1.79.0")

        assert result.status == updates.STATUS_ERROR


class TestVersionEndpoint:
    def test_endpoint_returns_the_check_result(self, tmp_path):
        client = _client(tmp_path)
        check = updates.UpdateCheck(
            current="1.79.0",
            status=updates.STATUS_UPDATE,
            latest="9.0.0",
            release_title="Nine point oh",
            release_url="https://github.com/oduflow/oduflow/releases/tag/v9.0.0",
        )

        with patch("oduflow.updates.check_for_update", return_value=check) as checked:
            response = client.get("/api/version")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["version"]["status"] == updates.STATUS_UPDATE
        assert body["version"]["latest"] == "9.0.0"
        checked.assert_called_once()

    def test_failed_check_is_a_200_with_an_error_field(self, tmp_path):
        client = _client(tmp_path)
        check = updates.UpdateCheck(
            current="1.79.0", status=updates.STATUS_ERROR, error="offline"
        )

        with patch("oduflow.updates.check_for_update", return_value=check):
            response = client.get("/api/version")

        # A failed lookup is an answer the dialog shows, not a server error.
        assert response.status_code == 200
        assert response.json()["version"]["error"] == "offline"

    def test_dashboard_never_checks_on_its_own(self, tmp_path):
        client = _client(tmp_path)

        with patch("oduflow.updates.urlopen") as mock_urlopen:
            page = client.get("/")

        assert page.status_code == 200
        mock_urlopen.assert_not_called()
        # The header version is a button that opens the dialog on click.
        assert 'class="brand-version"' in page.text
        assert 'onclick="openVersionModal()"' in page.text
