"""Token-first git credential storage (``store_credential``) and its surfaces."""

import os
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import git_ops
from oduflow.errors import ExternalCommandError
from oduflow.git_ops import (
    DEFAULT_TOKEN_USERNAME,
    InvalidRepoURLError,
    normalize_credential_host,
    store_credential,
)
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _stored_lines(cred_file):
    if not os.path.exists(cred_file):
        return []
    with open(cred_file) as f:
        return [line.strip() for line in f if line.strip()]


class TestNormalizeCredentialHost:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("github.com", "github.com"),
            ("  GitHub.com  ", "github.com"),
            ("git.example.com:8443", "git.example.com:8443"),
            ("https://github.com/owner/repo.git", "github.com"),
            ("https://git.example.com:8443/owner/repo.git", "git.example.com:8443"),
            ("github.com/", "github.com"),
        ],
    )
    def test_accepts_host_port_and_url(self, raw, expected):
        assert normalize_credential_host(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "github.com/owner/repo",
            "ssh://git@github.com",
            "host:notaport",
            "user:pat@github.com",
        ],
    )
    def test_rejects_garbage(self, raw):
        with pytest.raises(InvalidRepoURLError):
            normalize_credential_host(raw)


@pytest.fixture
def allow_any_host():
    """Skip DNS-based SSRF resolution so tests run offline with made-up hosts."""
    with patch("oduflow.url_safety.assert_allowed_host"):
        yield


@pytest.mark.usefixtures("allow_any_host")
class TestStoreCredential:
    def test_stores_token_with_default_username_and_checks_provider(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with patch.object(
            git_ops, "_check_token_with_provider", return_value="valid"
        ) as check:
            result = store_credential("github.com", "ghp_secret", cred_file)

        assert result == {
            "host": "github.com",
            "username": DEFAULT_TOKEN_USERNAME,
            "status": "authenticated",
        }
        check.assert_called_once_with(
            "github.com", DEFAULT_TOKEN_USERNAME, "ghp_secret"
        )
        assert _stored_lines(cred_file) == [
            f"https://{DEFAULT_TOKEN_USERNAME}:ghp_secret@github.com"
        ]
        assert oct(os.stat(cred_file).st_mode & 0o777) == "0o600"

    def test_unknown_host_is_saved_unverified_without_network(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with patch("urllib.request.urlopen") as urlopen:
            result = store_credential(
                "git.example.com:8443", "tok", cred_file, username="deploy"
            )
        urlopen.assert_not_called()
        assert result["status"] == "unverified"
        # git's store helper percent-encodes the port separator in the host.
        assert [unquote(line) for line in _stored_lines(cred_file)] == [
            "https://deploy:tok@git.example.com:8443"
        ]

    def test_provider_rejection_removes_the_token(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with (
            patch.object(git_ops, "_check_token_with_provider", return_value="invalid"),
            pytest.raises(ExternalCommandError, match="rejected the token"),
        ):
            store_credential("github.com", "bad", cred_file)
        assert _stored_lines(cred_file) == []

    def test_verifies_with_ls_remote_when_repo_url_given(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with (
            patch.object(git_ops, "_verify_repo_access") as verify,
            patch.object(git_ops, "_check_token_with_provider") as check,
        ):
            result = store_credential(
                "github.com",
                "ghp_secret",
                cred_file,
                verify_repo_url="https://github.com/owner/repo.git",
            )
        verify.assert_called_once_with("https://github.com/owner/repo.git", cred_file)
        check.assert_not_called()
        assert result["status"] == "authenticated"

    def test_repo_url_on_another_host_is_rejected_before_storing(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with pytest.raises(InvalidRepoURLError, match="does not match"):
            store_credential(
                "github.com",
                "ghp_secret",
                cred_file,
                verify_repo_url="https://gitlab.com/owner/repo.git",
            )
        assert not os.path.exists(cred_file)

    @pytest.mark.parametrize("token", ["", "  ", "a\nb"])
    def test_rejects_empty_or_multiline_token(self, tmp_path, token):
        with pytest.raises(InvalidRepoURLError):
            store_credential("github.com", token, str(tmp_path / "creds"))

    def test_rejects_username_with_url_delimiters(self, tmp_path):
        with pytest.raises(InvalidRepoURLError):
            store_credential(
                "github.com", "tok", str(tmp_path / "creds"), username="a:b"
            )

    def test_same_username_replaces_token_different_keeps_both(self, tmp_path):
        cred_file = str(tmp_path / "creds")
        with patch.object(git_ops, "_check_token_with_provider", return_value="valid"):
            store_credential("github.com", "one", cred_file)
            store_credential("github.com", "two", cred_file)
            store_credential("github.com", "three", cred_file, username="other")
        lines = _stored_lines(cred_file)
        assert f"https://{DEFAULT_TOKEN_USERNAME}:one@github.com" not in lines
        assert f"https://{DEFAULT_TOKEN_USERNAME}:two@github.com" in lines
        assert "https://other:three@github.com" in lines


def test_store_credential_blocks_loopback_before_storing(tmp_path):
    from oduflow.url_safety import BlockedURLError

    cred_file = str(tmp_path / "creds")
    with pytest.raises(BlockedURLError):
        store_credential("127.0.0.1", "pat", cred_file)
    assert not os.path.exists(cred_file)


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), team


class TestCredentialAddRoute:
    def test_token_body_uses_store_credential(self, tmp_path):
        client, team = _client(tmp_path)
        with patch(
            "oduflow.git_ops.store_credential",
            return_value={
                "host": "github.com",
                "username": "x",
                "status": "authenticated",
            },
        ) as store:
            r = client.post(
                "/api/credentials/add",
                json={"token": "ghp_secret", "username": "x", "repo_url": ""},
            )
        assert r.status_code == 200, r.text
        assert r.json()["result"]["status"] == "authenticated"
        store.assert_called_once_with(
            host="github.com",
            token="ghp_secret",
            username="x",
            verify_repo_url="",
            cred_file=team.git_credentials_file(),
        )

    def test_legacy_repo_url_body_still_works(self, tmp_path):
        client, team = _client(tmp_path)
        with patch(
            "oduflow.git_ops.setup_repo_auth",
            return_value={
                "repo_url": "https://github.com/o/r.git",
                "host": "github.com",
                "status": "authenticated",
            },
        ) as setup:
            r = client.post(
                "/api/credentials/add",
                json={"repo_url": "https://user:pat@github.com/o/r.git"},
            )
        assert r.status_code == 200, r.text
        setup.assert_called_once_with(
            "https://user:pat@github.com/o/r.git", cred_file=team.git_credentials_file()
        )

    def test_missing_token_and_url_is_400(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post("/api/credentials/add", json={"host": "github.com"})
        assert r.status_code == 400
        assert "token" in r.json()["error"]

    def test_flow_error_is_400_with_message(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.post(
            "/api/credentials/add",
            json={"token": "tok", "host": "github.com/owner/repo"},
        )
        assert r.status_code == 400
        assert "hostname" in r.json()["error"]
