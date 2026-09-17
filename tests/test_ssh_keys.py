"""Team SSH deploy keys: generation, git wiring, and SSH URL acceptance.

The private key is a team-wide secret with real repository access, so the
tests pin what matters: file permissions, the exact GIT_SSH_COMMAND options
that keep git from ever prompting interactively (the reason SSH URLs used to
be rejected), and that URL sanitizers no longer mangle SSH remotes — an SSH
URL's ``git@`` user is protocol, not a credential.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest

from oduflow import git_ops
from oduflow.git_ops import InvalidRepoURLError
from oduflow.naming import sanitize_repo_url


@pytest.fixture
def ssh_dir(tmp_path):
    return str(tmp_path / "ssh")


class TestEnsureSshKey:
    def test_generates_keypair_with_tight_permissions(self, ssh_dir):
        assert git_ops.ensure_ssh_key(ssh_dir, comment="oduflow-1") is True

        key = git_ops.ssh_key_path(ssh_dir)
        pub = git_ops.ssh_public_key_path(ssh_dir)
        assert os.path.isfile(key) and os.path.isfile(pub)
        assert stat.S_IMODE(os.stat(ssh_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
        public_key = git_ops.get_ssh_public_key(ssh_dir)
        assert public_key.startswith("ssh-ed25519 ")
        assert public_key.endswith("oduflow-1")

    def test_second_call_keeps_existing_key(self, ssh_dir):
        git_ops.ensure_ssh_key(ssh_dir)
        before = git_ops.get_ssh_public_key(ssh_dir)

        assert git_ops.ensure_ssh_key(ssh_dir) is False
        assert git_ops.get_ssh_public_key(ssh_dir) == before

    def test_stale_half_pair_is_replaced(self, ssh_dir):
        # A crash between the two writes leaves only one file; ensure must
        # recover instead of failing on ssh-keygen's overwrite prompt.
        os.makedirs(ssh_dir, mode=0o700)
        with open(git_ops.ssh_key_path(ssh_dir), "w") as f:
            f.write("garbage")

        assert git_ops.ensure_ssh_key(ssh_dir) is True
        assert git_ops.get_ssh_public_key(ssh_dir).startswith("ssh-ed25519 ")

    def test_regenerate_replaces_key(self, ssh_dir):
        git_ops.ensure_ssh_key(ssh_dir)
        before = git_ops.get_ssh_public_key(ssh_dir)

        new_pub = git_ops.regenerate_ssh_key(ssh_dir)

        assert new_pub != before
        assert new_pub == git_ops.get_ssh_public_key(ssh_dir)

    def test_failed_regenerate_keeps_the_old_key(self, ssh_dir):
        # The old key is still registered with git hosts; a failed
        # regeneration must not destroy it.
        git_ops.ensure_ssh_key(ssh_dir)
        before = git_ops.get_ssh_public_key(ssh_dir)

        with patch("oduflow.git_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(Exception):
                git_ops.regenerate_ssh_key(ssh_dir)

        assert git_ops.get_ssh_public_key(ssh_dir) == before

    def test_fingerprint_matches_ssh_keygen(self, ssh_dir):
        # The fingerprint is computed in-process; it must stay byte-identical
        # to what `ssh-keygen -lf` prints (users compare against GitHub's UI).
        import subprocess

        git_ops.ensure_ssh_key(ssh_dir)
        fingerprint = git_ops.ssh_key_fingerprint(ssh_dir)
        assert fingerprint.startswith("SHA256:")
        out = subprocess.run(
            ["ssh-keygen", "-lf", git_ops.ssh_public_key_path(ssh_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert fingerprint == out.stdout.split()[1]

    def test_fingerprint_and_public_key_empty_without_key(self, ssh_dir):
        assert git_ops.get_ssh_public_key(ssh_dir) == ""
        assert git_ops.ssh_key_fingerprint(ssh_dir) == ""
        assert git_ops.ssh_key_info(ssh_dir) == ("", "")


class TestGitEnvSshCommand:
    def test_env_carries_ssh_command_when_key_exists(self, tmp_path):
        git_ops.ensure_ssh_key(str(tmp_path / "ssh"))
        env = git_ops.git_env_for_team(str(tmp_path / ".git-credentials"))

        cmd = env["GIT_SSH_COMMAND"]
        assert git_ops.ssh_key_path(str(tmp_path / "ssh")) in cmd
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert str(tmp_path / "ssh" / "known_hosts") in cmd

    def test_env_pins_ssh_command_even_without_key(self, tmp_path):
        # No key must mean a deterministic auth failure, never a fallback to
        # the host user's ambient ssh identities or an interactive prompt.
        env = git_ops.git_env_for_team(str(tmp_path / ".git-credentials"))
        cmd = env["GIT_SSH_COMMAND"]
        assert "BatchMode=yes" in cmd
        assert "IdentitiesOnly=yes" in cmd

    def test_explicit_ssh_dir_overrides_derivation(self, tmp_path):
        ssh_dir = str(tmp_path / "elsewhere")
        git_ops.ensure_ssh_key(ssh_dir)
        env = git_ops.git_env_for_team(str(tmp_path / ".git-credentials"), ssh_dir)
        assert git_ops.ssh_key_path(ssh_dir) in env["GIT_SSH_COMMAND"]


class TestValidateRepoUrlSsh:
    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "ssh://git@git.example.com:2222/owner/repo.git",
            "https://github.com/owner/repo.git",
        ],
    )
    def test_accepts_https_and_ssh_forms(self, url):
        with patch("oduflow.url_safety.assert_allowed_host"):
            git_ops.validate_repo_url(url)

    @pytest.mark.parametrize("url", ["ftp://host/repo.git", "not a url"])
    def test_rejects_other_schemes(self, url):
        with pytest.raises(InvalidRepoURLError):
            git_ops.validate_repo_url(url)

    def test_ssrf_guard_applies_to_scp_host(self):
        with pytest.raises(Exception):
            git_ops.validate_repo_url("git@127.0.0.1:owner/repo.git")

    def test_rejects_option_shaped_scp_host(self):
        # A leading "-" in user or host would reach ssh as an option
        # (CVE-2017-1000117 shape); the URL gate must refuse it.
        with pytest.raises(InvalidRepoURLError):
            git_ops.validate_repo_url("git@-ohost.example.com:owner/repo.git")
        with pytest.raises(InvalidRepoURLError):
            git_ops.validate_repo_url("-user@github.com:owner/repo.git")

    def test_rejects_ssh_url_with_password(self):
        with pytest.raises(InvalidRepoURLError):
            git_ops.validate_repo_url("ssh://git:secret@git.example.com/owner/repo.git")

    def test_is_ssh_url(self):
        assert git_ops.is_ssh_url("git@github.com:owner/repo.git")
        assert git_ops.is_ssh_url("ssh://git@github.com/owner/repo.git")
        assert not git_ops.is_ssh_url("https://github.com/owner/repo.git")


class TestSshUrlSanitizers:
    def test_sanitize_keeps_ssh_protocol_user(self):
        url = "ssh://git@github.com/owner/repo.git"
        assert sanitize_repo_url(url) == url

    def test_sanitize_still_strips_https_credentials(self):
        assert (
            sanitize_repo_url("https://user:pat@github.com/owner/repo.git")
            == "https://github.com/owner/repo.git"
        )

    def test_sanitize_strips_ssh_embedded_password(self):
        # An ssh:// password is a secret; it must never survive into stored
        # or displayed URLs (labels, environment info, dashboard).
        assert (
            sanitize_repo_url("ssh://git:secret@git.example.com/owner/repo.git")
            == "ssh://git@git.example.com/owner/repo.git"
        )

    def test_inline_credential_extraction_leaves_ssh_urls_alone(self, tmp_path):
        cred_file = str(tmp_path / ".git-credentials")
        url = "ssh://git@github.com/owner/repo.git"

        assert git_ops.extract_and_store_inline_credentials(url, cred_file) == (
            url,
            "",
        )
        assert not os.path.exists(cred_file)

    def test_store_credential_rejects_ssh_verify_url(self, tmp_path):
        # An SSH verify URL would authenticate with the deploy key, falsely
        # marking a dead token "authenticated".
        with pytest.raises(InvalidRepoURLError):
            git_ops.store_credential(
                "github.com",
                "ghp_dead",
                str(tmp_path / ".git-credentials"),
                verify_repo_url="ssh://git@github.com/owner/repo.git",
            )


def _client(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oduflow.locking import LockManager
    from oduflow.settings import Settings, TeamSettings
    from oduflow.web_ui import mount_web_ui

    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), team


class TestSshKeyRoutes:
    def test_get_without_key_reports_empty(self, tmp_path):
        client, _ = _client(tmp_path)
        r = client.get("/api/ssh-key")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "public_key": "", "fingerprint": ""}

    def test_generate_then_get_round_trip(self, tmp_path):
        client, team = _client(tmp_path)
        r = client.post("/api/ssh-key/generate", json={})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["public_key"].startswith("ssh-ed25519 ")
        assert created["fingerprint"].startswith("SHA256:")

        r = client.get("/api/ssh-key")
        assert r.json()["public_key"] == created["public_key"]
        # The private key must never appear in any response.
        assert "PRIVATE KEY" not in r.text

    def test_generate_without_force_is_idempotent(self, tmp_path):
        client, _ = _client(tmp_path)
        first = client.post("/api/ssh-key/generate", json={}).json()
        second = client.post("/api/ssh-key/generate", json={}).json()
        assert second["public_key"] == first["public_key"]

    def test_force_replaces_the_key(self, tmp_path):
        client, _ = _client(tmp_path)
        first = client.post("/api/ssh-key/generate", json={}).json()
        second = client.post("/api/ssh-key/generate", json={"force": True}).json()
        assert second["public_key"] != first["public_key"]
