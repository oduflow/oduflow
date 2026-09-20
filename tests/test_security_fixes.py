"""Regression tests for the security-audit fixes.

Each test pins a specific vulnerability that was closed so a future refactor
cannot silently reopen it. All are pure/unit — no Docker required.
"""

import pathlib

import pytest

from oduflow.env_credentials import (
    MissingCredentialsError,
    create_credentials,
    load_credentials,
)
from oduflow.naming import validate_env_name


class TestValidateEnvName:
    @pytest.mark.parametrize(
        "name",
        ["19.0", "feature/my-feature", "release/v1.2.3", "client-a", "a.b_c-1"],
    )
    def test_accepts_real_branch_names(self, name):
        assert validate_env_name(name) == name

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/..", "../b", "a/./b"])
    def test_rejects_traversal_and_empty(self, name):
        with pytest.raises(ValueError):
            validate_env_name(name)

    @pytest.mark.parametrize("name", ["a\\b", "a\x00b", "x" * 101])
    def test_rejects_separators_nul_and_oversize(self, name):
        with pytest.raises(ValueError):
            validate_env_name(name)

    @pytest.mark.parametrize("name", ["foo ", " foo", "\tfoo", "foo\n", " 19.0 "])
    def test_rejects_whitespace_padding(self, name):
        # A padded name must not pass: slug strips the space (container/DB) while
        # the workspace path keeps it, so "foo" and "foo " would collide.
        with pytest.raises(ValueError):
            validate_env_name(name)

    def test_returns_name_unchanged(self):
        # No silent canonicalization — the returned value is byte-for-byte input.
        assert validate_env_name("feature/My-Env.1") == "feature/My-Env.1"


class TestAutofillUiPasswords:
    def test_fills_every_empty_password_distinctly(self):
        from oduflow.server import _autofill_ui_passwords

        src = (
            "[team.1]\n"
            'ui_password = ""                  # Web UI password\n'
            "[team.2]\n"
            'ui_password = ""\n'
        )
        out, generated = _autofill_ui_passwords(src)
        assert len(generated) == 2
        assert generated[0] != generated[1]
        assert 'ui_password = ""' not in out
        for pw in generated:
            assert f'ui_password = "{pw}"' in out
        # Untouched lines survive verbatim, including the preserved comment.
        assert "[team.1]" in out and "[team.2]" in out
        assert "# Web UI password" in out

    def test_noop_when_already_set(self):
        from oduflow.server import _autofill_ui_passwords

        src = 'ui_password = "already-set"\n'
        out, generated = _autofill_ui_passwords(src)
        assert generated == []
        assert out.strip() == src.strip()


class TestEnsureWebUiPassword:
    """`_ensure_web_ui_password` must provision EVERY passwordless team, not
    skip the whole config as soon as one team already has a password."""

    def _settings(self, *ui_passwords, allow_insecure_http=False):
        from oduflow.settings import Settings, TeamSettings

        return Settings(
            allow_insecure_http=allow_insecure_http,
            teams={
                str(i + 1): TeamSettings(team_id=str(i + 1), ui_password=pw)
                for i, pw in enumerate(ui_passwords)
            },
        )

    def test_provisions_partially_configured_multiteam(self, tmp_path, monkeypatch):
        import oduflow.server as server

        cfg = tmp_path / "oduflow.toml"
        cfg.write_text(
            '[team.1]\nui_password = "already-set"\n[team.2]\nui_password = ""\n',
            encoding="utf-8",
        )
        # team 1 has a password, team 2 does not: the old any() guard returned
        # early here and left team 2 locked out.
        settings = self._settings("already-set", "")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
        sentinel = object()
        monkeypatch.setattr(server, "_get_settings", lambda: sentinel)

        result = server._ensure_web_ui_password(settings)

        written = cfg.read_text(encoding="utf-8")
        assert 'ui_password = ""' not in written
        assert 'ui_password = "already-set"' in written
        assert result is sentinel  # reloaded after the write

    def test_skips_when_every_team_has_password(self, monkeypatch):
        import oduflow.server as server

        settings = self._settings("pw-a", "pw-b")
        # find_toml/_get_settings must never be reached.
        monkeypatch.setattr(
            server, "find_toml", lambda: (_ for _ in ()).throw(AssertionError())
        )
        assert server._ensure_web_ui_password(settings) is settings

    def test_skips_when_allow_insecure_http(self, monkeypatch):
        import oduflow.server as server

        settings = self._settings("", "", allow_insecure_http=True)
        monkeypatch.setattr(
            server, "find_toml", lambda: (_ for _ in ()).throw(AssertionError())
        )
        assert server._ensure_web_ui_password(settings) is settings

    def test_never_logs_the_generated_password(self, tmp_path, monkeypatch, caplog):
        """The password goes to oduflow.toml, never to the log: the journal is
        shipped off-host and retained long after the secret is still live."""
        import logging

        import oduflow.server as server

        cfg = tmp_path / "oduflow.toml"
        cfg.write_text('[team.1]\nui_password = ""\n', encoding="utf-8")
        cfg.chmod(0o644)
        settings = self._settings("")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
        monkeypatch.setattr(server, "_get_settings", lambda: settings)

        with caplog.at_level(logging.DEBUG, logger="oduflow"):
            server._ensure_web_ui_password(settings)

        written = cfg.read_text(encoding="utf-8")
        password = written.split('ui_password = "', 1)[1].split('"', 1)[0]
        assert password  # a password really was provisioned
        assert password not in caplog.text
        # ...and the config that now holds it is no longer world-readable.
        assert cfg.stat().st_mode & 0o077 == 0

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o600, 0o400])
    def test_replacement_keeps_secrets_private(self, tmp_path, monkeypatch, mode):
        """Readers of the old inode must never see the generated password."""
        import oduflow.server as server

        cfg = tmp_path / "oduflow.toml"
        original = '[team.1]\nhostname = "a.example.com"\nui_password = ""\n'
        cfg.write_text(original, encoding="utf-8")
        cfg.chmod(mode)
        original_stat = cfg.stat()
        settings = self._settings("")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
        monkeypatch.setattr(server, "_settings", None)
        replace = server.os.replace

        def check_private_before_replace(source, destination):
            staged = pathlib.Path(source)
            assert staged.stat().st_mode & 0o777 == mode & 0o600
            assert 'ui_password = ""' not in staged.read_text(encoding="utf-8")
            assert cfg.read_text(encoding="utf-8") == original
            replace(source, destination)

        monkeypatch.setattr(server.os, "replace", check_private_before_replace)
        with cfg.open(encoding="utf-8") as old_reader:
            result = server._ensure_web_ui_password(settings)
            assert old_reader.read() == original

        assert result.teams["1"].ui_password
        final_stat = cfg.stat()
        assert final_stat.st_mode & 0o777 == mode & 0o600
        assert (final_stat.st_uid, final_stat.st_gid) == (
            original_stat.st_uid,
            original_stat.st_gid,
        )
        assert list(tmp_path.iterdir()) == [cfg]

    def test_preserves_config_symlink(self, tmp_path, monkeypatch):
        import oduflow.server as server

        target = tmp_path / "target.toml"
        target.write_text('[team.1]\nui_password = ""\n', encoding="utf-8")
        cfg = tmp_path / "oduflow.toml"
        cfg.symlink_to(target)
        settings = self._settings("")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))
        monkeypatch.setattr(server, "_get_settings", lambda: settings)

        server._ensure_web_ui_password(settings)

        assert cfg.is_symlink()
        assert 'ui_password = ""' not in target.read_text(encoding="utf-8")
        assert target.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("failure", ["fchmod", "replace"])
    def test_failed_replacement_keeps_original(self, tmp_path, monkeypatch, failure):
        import oduflow.server as server

        cfg = tmp_path / "oduflow.toml"
        original = '[team.1]\nui_password = ""\n'
        cfg.write_text(original, encoding="utf-8")
        cfg.chmod(0o400 if failure == "fchmod" else 0o644)
        settings = self._settings("")
        monkeypatch.setattr(server, "find_toml", lambda: str(cfg))

        def fail(*args):
            raise PermissionError("Read-only config mount")

        monkeypatch.setattr(server.os, failure, fail)

        result = server._ensure_web_ui_password(settings)

        assert result is settings
        assert cfg.read_text(encoding="utf-8") == original
        assert list(tmp_path.iterdir()) == [cfg]


class TestBootstrapConfig:
    """A fresh install writes its generated secrets to disk only."""

    def _bootstrap(self, tmp_path, monkeypatch):
        import oduflow.server as server

        monkeypatch.setattr(
            "oduflow.settings._resolve_etc_dir", lambda: str(tmp_path / "etc")
        )
        return server._bootstrap_config()

    def test_secrets_are_written_to_a_private_file_and_not_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        import os
        import re

        # Pin the module logger's own level: another test in the suite may have
        # raised it, and then caplog would see nothing and pass vacuously.
        with caplog.at_level(logging.DEBUG, logger="oduflow"):
            dest = self._bootstrap(tmp_path, monkeypatch)

        text = pathlib.Path(dest).read_text(encoding="utf-8")
        secrets_found = re.findall(
            r'^\s*(?:password|auth_token|ui_password) = "([^"]+)"', text, re.M
        )
        # DB password, MCP auth_token and web-UI password.
        assert len(secrets_found) == 3
        for secret in secrets_found:
            assert secret not in caplog.text
        assert os.stat(dest).st_mode & 0o777 == 0o600
        # The operator still learns where to look.
        assert dest in caplog.text

    def test_refuses_to_overwrite_an_existing_config(self, tmp_path, monkeypatch):
        """Bootstrap may only ADD a config. find_toml() raises FileNotFoundError
        for a missing ODUFLOW_TOML path *without* falling back, so the caller
        would otherwise truncate a live /etc/oduflow/oduflow.toml and destroy its
        database password, teams and tokens."""
        from oduflow.errors import ConflictError

        dest = self._bootstrap(tmp_path, monkeypatch)
        original = pathlib.Path(dest).read_text(encoding="utf-8")

        with pytest.raises(ConflictError):
            self._bootstrap(tmp_path, monkeypatch)

        assert pathlib.Path(dest).read_text(encoding="utf-8") == original


class TestHttpRequestPathGuard:
    """`http_request_to_odoo` must reject paths that could rewrite the host
    (SSRF) before it ever builds a request."""

    def _call(self, path):
        from oduflow.docker_ops.odoo_ops import http_request_to_odoo

        # The guard raises before touching settings/team/Docker, so None is fine.
        return http_request_to_odoo(None, None, "env", path)

    @pytest.mark.parametrize(
        "path", ["@evil.com/x", "//evil.com/", "http://evil.com", "evil", ""]
    )
    def test_rejects_host_rewriting_paths(self, path):
        with pytest.raises(ValueError):
            self._call(path)


class TestModuleNameValidation:
    def test_accepts_plain_modules(self):
        from oduflow.docker_ops.odoo_ops import _validate_module_names

        _validate_module_names(["sale", "purchase", "stock_account"])

    @pytest.mark.parametrize(
        "mod",
        ["base --load=evil", "a;b", "mod space", "--logfile=/x", "a,b", ""],
    )
    def test_rejects_argument_injection(self, mod):
        from oduflow.docker_ops.odoo_ops import _validate_module_names

        with pytest.raises(ValueError):
            _validate_module_names([mod])


class TestCredentialFallback:
    def test_fallback_returns_shared_when_allowed(self, tmp_path):
        creds = load_credentials(
            "env", str(tmp_path), "odoo", "secret", allow_fallback=True
        )
        assert creds == {"pg_user": "odoo", "pg_password": "secret"}

    def test_no_fallback_raises_when_missing(self, tmp_path):
        with pytest.raises(MissingCredentialsError):
            load_credentials(
                "env", str(tmp_path), "odoo", "secret", allow_fallback=False
            )

    def test_no_fallback_uses_scoped_creds_when_present(self, tmp_path):
        create_credentials("env", "1", str(tmp_path))
        creds = load_credentials(
            "env", str(tmp_path), "odoo", "secret", allow_fallback=False
        )
        assert creds["pg_user"] != "odoo"
        assert creds["pg_password"] != "secret"


class TestSetupRepoAuthSSRF:
    def test_blocks_loopback_before_storing_credentials(self, tmp_path):
        from oduflow.git_ops import setup_repo_auth
        from oduflow.url_safety import BlockedURLError

        cred_file = str(tmp_path / "creds")
        with pytest.raises(BlockedURLError):
            setup_repo_auth("https://user:pat@127.0.0.1/owner/repo.git", cred_file)
        # The PAT must never have been written to disk.
        import os

        assert not os.path.exists(cred_file)


class TestGitErrorRedaction:
    def test_embedded_repo_credentials_are_removed_from_command_output(self):
        from oduflow.docker_ops.env_ops import _redact_repo_urls

        repo_url = "https://user:super-secret@example.com/acme/repo.git"
        output = f"fatal: unable to access '{repo_url}': connection refused"

        redacted = _redact_repo_urls(output, repo_url)

        assert "super-secret" not in redacted
        assert "user@" not in redacted
        assert "https://example.com/acme/repo.git" in redacted


class TestUiPasswordInjection:
    def test_injects_into_bootstrap_config(self):
        from oduflow.server import _inject_ui_password

        src = 'ui_password = ""                  # Web UI password\n'
        out = _inject_ui_password(src, "s3cret")
        assert 'ui_password = "s3cret"' in out
        assert 'ui_password = ""' not in out

    def test_only_first_empty_is_set(self):
        from oduflow.server import _inject_ui_password

        src = 'ui_password = ""\nui_password = ""\n'
        out = _inject_ui_password(src, "s3cret")
        assert out.count('ui_password = "s3cret"') == 1
        assert out.count('ui_password = ""') == 1
