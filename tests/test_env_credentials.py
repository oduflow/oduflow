import json
import os
from unittest.mock import patch

from oduflow.env_credentials import (
    create_credentials,
    delete_credentials,
    generate_pg_password,
    generate_pg_username,
    load_credentials,
)


class TestGeneratePgUsername:
    def test_basic(self):
        result = generate_pg_username("feature/payments", "1")
        assert result == "u_1_feature-payments"

    def test_instance_id(self):
        result = generate_pg_username("main", "3")
        assert result == "u_3_main"

    def test_truncated_to_63(self):
        long_branch = "a" * 100
        result = generate_pg_username(long_branch, "1")
        assert len(result) <= 63

    def test_special_chars(self):
        result = generate_pg_username("feature/my.branch@v2", "1")
        assert result == "u_1_feature-mybranchv2"


class TestGeneratePgPassword:
    def test_length(self):
        pw = generate_pg_password()
        assert len(pw) >= 20

    def test_unique(self):
        pw1 = generate_pg_password()
        pw2 = generate_pg_password()
        assert pw1 != pw2

    def test_no_sql_special_chars(self):
        for _ in range(20):
            pw = generate_pg_password()
            assert "'" not in pw
            assert '"' not in pw

    @patch(
        "oduflow.env_credentials.secrets.token_urlsafe",
        side_effect=["-" + "a" * 23, "b" * 24],
    )
    def test_retries_when_password_starts_with_dash(self, mock_token_urlsafe):
        pw = generate_pg_password()

        assert pw == "b" * 24
        assert mock_token_urlsafe.call_count == 2
        mock_token_urlsafe.assert_called_with(18)


class TestCreateLoadDeleteCredentials:
    def test_create_and_load(self, tmp_path):
        workspaces_dir = str(tmp_path / "workspaces")
        os.makedirs(workspaces_dir, exist_ok=True)

        creds = create_credentials("feature/test", "1", workspaces_dir)
        assert "pg_user" in creds
        assert "pg_password" in creds
        assert creds["pg_user"] == "u_1_feature-test"

        loaded = load_credentials("feature/test", workspaces_dir, "odoo")
        assert loaded == creds

    def test_load_fallback(self, tmp_path):
        workspaces_dir = str(tmp_path / "workspaces")
        os.makedirs(workspaces_dir, exist_ok=True)

        # The fallback names the superuser and carries no password.
        loaded = load_credentials("nonexistent", workspaces_dir, "odoo")
        assert loaded == {"pg_user": "odoo", "pg_password": ""}

    def test_delete(self, tmp_path):
        workspaces_dir = str(tmp_path / "workspaces")
        os.makedirs(workspaces_dir, exist_ok=True)

        create_credentials("feature/del", "1", workspaces_dir)
        delete_credentials("feature/del", workspaces_dir)

        loaded = load_credentials("feature/del", workspaces_dir, "odoo")
        assert loaded == {"pg_user": "odoo", "pg_password": ""}

    def test_delete_nonexistent(self, tmp_path):
        workspaces_dir = str(tmp_path / "workspaces")
        os.makedirs(workspaces_dir, exist_ok=True)
        # Should not raise
        delete_credentials("nonexistent", workspaces_dir)

    def test_credentials_file_content(self, tmp_path):
        workspaces_dir = str(tmp_path / "workspaces")
        os.makedirs(workspaces_dir, exist_ok=True)

        creds = create_credentials("main", "2", workspaces_dir)
        creds_path = os.path.join(workspaces_dir, "main", "env_credentials.json")
        assert os.path.isfile(creds_path)
        with open(creds_path) as f:
            stored = json.load(f)
        assert stored == creds


class TestCredentialsFilePermissions:
    def test_create_credentials_is_0600_and_atomic(self, tmp_path):
        ws = str(tmp_path)
        creds = create_credentials("main", "1", ws)
        # Locate the written file via load (round-trips through the same path).
        loaded = load_credentials("main", ws, "fb_user")
        assert loaded == creds
        # Find the credentials file and check perms + no leftover temp file.
        import glob

        files = glob.glob(
            os.path.join(ws, "**", "env_credentials.json"), recursive=True
        )
        assert files, "credentials file not written"
        mode = os.stat(files[0]).st_mode & 0o777
        assert mode == 0o600, oct(mode)
        assert not glob.glob(
            os.path.join(ws, "**", "env_credentials.json.tmp"), recursive=True
        )
