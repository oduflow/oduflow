"""Git plumbing: credential store, pull, and history.

The module had no test file, leaving 402 uncovered mutants. The parts that
matter are the ones handling secrets — the credential file holds plaintext
tokens, so listing must mask them, deletion must remove exactly the matching
line, and validation must not report a token as good when the host says
otherwise — plus ``pull_repo``, whose ``old_head`` return value is what the
change classifier and the production rollback both key off.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from oduflow import git_ops
from oduflow.errors import ExternalCommandError


@pytest.fixture
def cred_file(tmp_path):
    path = tmp_path / "git-credentials"
    path.write_text(
        "https://ada:ghp_abcdef123456@github.com\nhttps://bob:glpat_secret@gitlab.com\n"
    )
    return str(path)


class TestListCredentials:
    def test_lists_host_and_username_per_entry(self, cred_file):
        entries = git_ops.list_credentials(cred_file)

        assert [(e["host"], e["username"]) for e in entries] == [
            ("github.com", "ada"),
            ("gitlab.com", "bob"),
        ]

    def test_the_token_is_masked_to_its_first_four_characters(self, cred_file):
        entries = git_ops.list_credentials(cred_file)

        assert entries[0]["token_masked"] == "ghp_****"
        assert "abcdef123456" not in str(entries)

    def test_a_short_token_is_masked_completely(self, tmp_path):
        path = tmp_path / "creds"
        path.write_text("https://ada:tiny@github.com\n")

        assert git_ops.list_credentials(str(path))[0]["token_masked"] == "****"

    def test_a_four_character_token_is_masked_completely(self, tmp_path):
        # The check is `len > 4`, so a 4-char token reveals nothing.
        path = tmp_path / "creds"
        path.write_text("https://ada:abcd@github.com\n")

        assert git_ops.list_credentials(str(path))[0]["token_masked"] == "****"

    def test_a_five_character_token_reveals_four(self, tmp_path):
        path = tmp_path / "creds"
        path.write_text("https://ada:abcde@github.com\n")

        assert git_ops.list_credentials(str(path))[0]["token_masked"] == "abcd****"

    def test_a_missing_file_lists_nothing(self, tmp_path):
        assert git_ops.list_credentials(str(tmp_path / "nope")) == []

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "creds"
        path.write_text("\n\nhttps://ada:tok12345@github.com\n\n")

        assert len(git_ops.list_credentials(str(path))) == 1

    def test_entries_without_a_host_or_user_are_skipped(self, tmp_path):
        path = tmp_path / "creds"
        path.write_text(
            "not-a-url\nhttps://github.com\nhttps://ada:tok12345@github.com\n"
        )

        entries = git_ops.list_credentials(str(path))
        assert [e["username"] for e in entries] == ["ada"]


class TestDeleteCredential:
    def test_removes_only_the_matching_entry(self, cred_file):
        assert git_ops.delete_credential("github.com", "ada", cred_file) is True

        remaining = git_ops.list_credentials(cred_file)
        assert [(e["host"], e["username"]) for e in remaining] == [
            ("gitlab.com", "bob")
        ]

    def test_the_username_must_match_too(self, cred_file):
        # Same host, different user: deleting one must not take the other.
        assert git_ops.delete_credential("github.com", "eve", cred_file) is False
        assert len(git_ops.list_credentials(cred_file)) == 2

    def test_the_host_must_match_too(self, cred_file):
        assert git_ops.delete_credential("bitbucket.org", "ada", cred_file) is False
        assert len(git_ops.list_credentials(cred_file)) == 2

    def test_an_unknown_entry_reports_nothing_removed(self, cred_file):
        assert git_ops.delete_credential("example.com", "nobody", cred_file) is False

    def test_a_missing_file_reports_nothing_removed(self, tmp_path):
        assert (
            git_ops.delete_credential("github.com", "ada", str(tmp_path / "nope"))
            is False
        )

    def test_the_file_is_left_untouched_when_nothing_matched(self, cred_file):
        with open(cred_file) as f:
            before = f.read()

        git_ops.delete_credential("example.com", "nobody", cred_file)

        with open(cred_file) as f:
            assert f.read() == before

    def test_deleting_the_last_entry_empties_the_file(self, tmp_path):
        path = tmp_path / "creds"
        path.write_text("https://ada:tok12345@github.com\n")

        assert git_ops.delete_credential("github.com", "ada", str(path)) is True
        assert git_ops.list_credentials(str(path)) == []


class TestValidateCredential:
    def _urlopen(self, status=200):
        response = MagicMock()
        response.status = status
        return patch("urllib.request.urlopen", return_value=response)

    def test_a_missing_file_is_invalid(self, tmp_path):
        assert (
            git_ops.validate_credential("github.com", "ada", str(tmp_path / "nope"))
            == "invalid"
        )

    def test_an_absent_entry_is_invalid(self, cred_file):
        assert git_ops.validate_credential("example.com", "ada", cred_file) == (
            "invalid"
        )

    def test_a_matching_entry_with_a_good_token_is_valid(self, cred_file):
        with self._urlopen(200):
            assert git_ops.validate_credential("github.com", "ada", cred_file) == (
                "valid"
            )

    def test_the_username_must_match_the_entry(self, cred_file):
        assert git_ops.validate_credential("github.com", "eve", cred_file) == (
            "invalid"
        )

    def test_an_unknown_host_is_trusted_without_a_probe(self, tmp_path):
        # There is no API to ask, so a stored credential is taken at face
        # value rather than reported broken.
        path = tmp_path / "creds"
        path.write_text("https://ada:tok12345@git.internal\n")

        with patch("urllib.request.urlopen") as urlopen:
            assert git_ops.validate_credential("git.internal", "ada", str(path)) == (
                "valid"
            )
        urlopen.assert_not_called()

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_rejected_token_is_invalid(self, cred_file, code):
        error = HTTPError("u", code, "denied", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            assert git_ops.validate_credential("github.com", "ada", cred_file) == (
                "invalid"
            )

    @pytest.mark.parametrize("code", [500, 502])
    def test_a_server_error_is_unknown_not_invalid(self, cred_file, code):
        # Reporting "invalid" on a GitHub outage would tell the user to
        # re-enter a token that is actually fine.
        error = HTTPError("u", code, "boom", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            assert git_ops.validate_credential("github.com", "ada", cred_file) == (
                "unknown"
            )

    def test_a_network_failure_is_unknown(self, cred_file):
        with patch("urllib.request.urlopen", side_effect=URLError("offline")):
            assert git_ops.validate_credential("github.com", "ada", cred_file) == (
                "unknown"
            )

    def test_a_non_200_response_is_invalid(self, cred_file):
        with self._urlopen(204):
            assert git_ops.validate_credential("github.com", "ada", cred_file) == (
                "invalid"
            )

    def test_github_is_probed_with_a_token_header(self, cred_file):
        with self._urlopen(200) as urlopen:
            git_ops.validate_credential("github.com", "ada", cred_file)

        request = urlopen.call_args.args[0]
        assert request.full_url == "https://api.github.com/user"
        assert request.get_header("Authorization") == "token ghp_abcdef123456"

    def test_gitlab_is_probed_with_its_own_header(self, cred_file):
        with self._urlopen(200) as urlopen:
            git_ops.validate_credential("gitlab.com", "bob", cred_file)

        request = urlopen.call_args.args[0]
        assert request.full_url == "https://gitlab.com/api/v4/user"
        assert request.get_header("Private-token") == "glpat_secret"

    def test_bitbucket_is_probed_with_basic_auth(self, tmp_path):
        import base64

        path = tmp_path / "creds"
        path.write_text("https://ada:tok12345@bitbucket.org\n")

        with self._urlopen(200) as urlopen:
            git_ops.validate_credential("bitbucket.org", "ada", str(path))

        request = urlopen.call_args.args[0]
        expected = base64.b64encode(b"ada:tok12345").decode()
        assert request.get_header("Authorization") == f"Basic {expected}"


class TestPullRepo:
    def _runner(self, heads, changed="a.py\nb.xml\n"):
        """Fake subprocess.run: rev-parse yields heads in order."""
        calls = []
        pending = list(heads)

        def run(argv, **kwargs):
            calls.append(argv)
            out = ""
            if "rev-parse" in argv:
                out = pending.pop(0)
            elif "diff" in argv:
                out = changed
            return MagicMock(stdout=out, returncode=0)

        return run, calls

    def test_returns_the_old_head_and_the_changed_files(self):
        run, _ = self._runner(["old123", "new456"])

        with patch("subprocess.run", side_effect=run):
            old_head, changed = git_ops.pull_repo("/repo", "main")

        assert old_head == "old123"
        assert changed == ["a.py", "b.xml"]

    def test_an_unchanged_head_reports_no_files_and_skips_the_diff(self):
        run, calls = self._runner(["same", "same"])

        with patch("subprocess.run", side_effect=run):
            old_head, changed = git_ops.pull_repo("/repo", "main")

        assert (old_head, changed) == ("same", [])
        assert not any("diff" in argv for argv in calls)

    def test_the_branch_is_fetched_into_its_remote_ref(self):
        run, calls = self._runner(["old", "new"])

        with patch("subprocess.run", side_effect=run):
            git_ops.pull_repo("/repo", "feature/x")

        fetch = next(argv for argv in calls if "fetch" in argv)
        assert "+refs/heads/feature/x:refs/remotes/origin/feature/x" in fetch

    def test_the_checkout_is_reset_to_the_fetched_branch(self):
        run, calls = self._runner(["old", "new"])

        with patch("subprocess.run", side_effect=run):
            git_ops.pull_repo("/repo", "main")

        reset = next(argv for argv in calls if "reset" in argv)
        assert reset[-2:] == ["--hard", "origin/main"]

    def test_the_diff_spans_the_whole_pulled_range(self):
        # Not just the last commit: the classifier needs every file touched
        # between the old and the new head.
        run, calls = self._runner(["old", "new"])

        with patch("subprocess.run", side_effect=run):
            git_ops.pull_repo("/repo", "main")

        diff = next(argv for argv in calls if "diff" in argv)
        assert "old..new" in diff

    def test_blank_diff_lines_are_dropped(self):
        run, _ = self._runner(["old", "new"], changed="a.py\n\n\nb.py\n")

        with patch("subprocess.run", side_effect=run):
            _, changed = git_ops.pull_repo("/repo", "main")

        assert changed == ["a.py", "b.py"]

    def test_a_failed_fetch_raises_with_the_credentials_scrubbed(self):
        # git echoes the full remote, token included, in its stderr.
        error = subprocess.CalledProcessError(
            128, "git", stderr="fatal: https://ada:ghp_secret@github.com/x.git denied"
        )

        def run(argv, **kwargs):
            if "rev-parse" in argv:
                return MagicMock(stdout="old", returncode=0)
            raise error

        with (
            patch("subprocess.run", side_effect=run),
            pytest.raises(ExternalCommandError) as excinfo,
        ):
            git_ops.pull_repo("/repo", "main")

        message = str(excinfo.value)
        assert "ghp_secret" not in message
        assert "***@github.com" in message

    def test_a_fetch_timeout_is_reported_as_such(self):
        def run(argv, **kwargs):
            if "rev-parse" in argv:
                return MagicMock(stdout="old", returncode=0)
            raise subprocess.TimeoutExpired("git", 60)

        with (
            patch("subprocess.run", side_effect=run),
            pytest.raises(ExternalCommandError, match="timed out"),
        ):
            git_ops.pull_repo("/repo", "main")

    def test_the_fetch_is_bounded_by_a_timeout(self):
        run, _ = self._runner(["old", "new"])
        seen = {}

        def wrapped(argv, **kwargs):
            if "fetch" in argv:
                seen["timeout"] = kwargs.get("timeout")
            return run(argv, **kwargs)

        with patch("subprocess.run", side_effect=wrapped):
            git_ops.pull_repo("/repo", "main")

        assert seen["timeout"] == 60

    def test_a_credential_file_switches_the_git_environment(self):
        run, _ = self._runner(["old", "new"])
        envs = []

        def wrapped(argv, **kwargs):
            envs.append(kwargs.get("env"))
            return run(argv, **kwargs)

        with patch("subprocess.run", side_effect=wrapped):
            git_ops.pull_repo("/repo", "main", cred_file="/creds")

        assert envs and all(
            e["GIT_CONFIG_VALUE_0"] == "store --file /creds" for e in envs
        )
        assert all(e["GIT_CONFIG_KEY_0"] == "credential.helper" for e in envs)

    def test_without_a_credential_file_the_base_environment_is_used(self):
        run, _ = self._runner(["old", "new"])
        envs = []

        def wrapped(argv, **kwargs):
            envs.append(kwargs.get("env"))
            return run(argv, **kwargs)

        with patch("subprocess.run", side_effect=wrapped):
            git_ops.pull_repo("/repo", "main")

        assert envs and all("GIT_CONFIG_VALUE_0" not in e for e in envs)

    def test_git_never_prompts_for_credentials(self):
        # An interactive prompt inside a server thread would hang the pull.
        run, _ = self._runner(["old", "new"])
        envs = []

        def wrapped(argv, **kwargs):
            envs.append(kwargs.get("env"))
            return run(argv, **kwargs)

        with patch("subprocess.run", side_effect=wrapped):
            git_ops.pull_repo("/repo", "main")

        assert all(e["GIT_TERMINAL_PROMPT"] == "0" for e in envs)


class TestRevParseAndReset:
    def test_rev_parse_returns_the_stripped_hash(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="abc123\n")):
            assert git_ops.rev_parse("/repo") == "abc123"

    def test_rev_parse_defaults_to_head(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="x")) as run:
            git_ops.rev_parse("/repo")

        assert run.call_args.args[0][-1] == "HEAD"

    def test_rev_parse_failure_raises(self):
        error = subprocess.CalledProcessError(128, "git", stderr="unknown revision")
        with (
            patch("subprocess.run", side_effect=error),
            pytest.raises(ExternalCommandError, match="rev-parse"),
        ):
            git_ops.rev_parse("/repo", "nope")

    def test_reset_hard_targets_the_given_ref(self):
        with patch("subprocess.run") as run:
            git_ops.reset_hard("/repo", "abc123")

        assert run.call_args.args[0] == [
            "git",
            "-C",
            "/repo",
            "reset",
            "--hard",
            "abc123",
        ]

    def test_reset_hard_failure_raises(self):
        error = subprocess.CalledProcessError(1, "git", stderr="cannot reset")
        with (
            patch("subprocess.run", side_effect=error),
            pytest.raises(ExternalCommandError, match="reset"),
        ):
            git_ops.reset_hard("/repo", "abc123")


class TestLogCommits:
    _SEP = "\x1f"

    def _out(self, *rows):
        return "\n".join(self._SEP.join(r) for r in rows)

    def test_parses_every_field(self):
        out = self._out(
            ("full-sha", "short", "the subject", "Ada", "2026-01-02T03:04:05+00:00")
        )
        with patch("subprocess.run", return_value=MagicMock(stdout=out)):
            commits = git_ops.log_commits("/repo")

        assert commits == [
            {
                "sha": "full-sha",
                "short": "short",
                "subject": "the subject",
                "author": "Ada",
                "date": "2026-01-02T03:04:05+00:00",
            }
        ]

    def test_the_default_limit_is_twenty(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="")) as run:
            git_ops.log_commits("/repo")

        assert "-20" in run.call_args.args[0]

    def test_the_limit_is_configurable(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="")) as run:
            git_ops.log_commits("/repo", n=5)

        assert "-5" in run.call_args.args[0]

    def test_malformed_rows_are_skipped(self):
        # A subject containing the separator would otherwise shift every field.
        out = self._out(("a", "b", "c"), ("s", "sh", "subj", "au", "date"))
        with patch("subprocess.run", return_value=MagicMock(stdout=out)):
            commits = git_ops.log_commits("/repo")

        assert [c["sha"] for c in commits] == ["s"]

    def test_an_empty_history_yields_no_commits(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="")):
            assert git_ops.log_commits("/repo") == []

    def test_failure_raises(self):
        error = subprocess.CalledProcessError(128, "git", stderr="not a repository")
        with (
            patch("subprocess.run", side_effect=error),
            pytest.raises(ExternalCommandError, match="git log"),
        ):
            git_ops.log_commits("/repo")


class TestParseManifest:
    def test_reads_the_literal_dict(self, tmp_path):
        path = tmp_path / "__manifest__.py"
        path.write_text("{'name': 'Sale', 'version': '17.0.1.0.0'}")

        assert git_ops.parse_manifest(str(path)) == {
            "name": "Sale",
            "version": "17.0.1.0.0",
        }

    def test_executable_content_is_refused(self, tmp_path):
        # literal_eval, not eval: a manifest is data, never code.
        path = tmp_path / "__manifest__.py"
        path.write_text("__import__('os').system('echo pwned')")

        with pytest.raises(ValueError):
            git_ops.parse_manifest(str(path))


class TestCurrentBranch:
    def _init_repo(self, path, branch="production"):
        path.mkdir(parents=True, exist_ok=True)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        subprocess.run(
            ["git", "init", "-q", "-b", branch, str(path)], check=True, env=env
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.email=t@example.com",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            env=env,
        )
        return path

    def test_reports_the_checked_out_branch(self, tmp_path):
        repo = self._init_repo(tmp_path / "repo", branch="hotfix")
        assert git_ops.current_branch(str(repo)) == "hotfix"

    def test_a_detached_head_reports_the_literal_head(self, tmp_path):
        repo = self._init_repo(tmp_path / "repo")
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True
        )
        assert git_ops.current_branch(str(repo)) == "HEAD"

    def test_a_plain_directory_is_unknown(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert git_ops.current_branch(str(plain)) == ""

    def test_a_directory_nested_inside_a_repo_is_unknown(self, tmp_path):
        # Git discovers repositories upward; a plain subdirectory must not
        # report the enclosing repository's branch as its own.
        repo = self._init_repo(tmp_path / "repo", branch="main-line")
        nested = repo / "workspaces" / "checkout"
        nested.mkdir(parents=True)
        assert git_ops.current_branch(str(nested)) == ""
