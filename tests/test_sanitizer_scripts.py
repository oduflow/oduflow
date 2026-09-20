"""Execution semantics of the sanitize script runner.

``_run_scripts_from_dir`` executes operator-supplied ``.sql`` (against the env
database) and ``.py`` (inside the serving container) scripts. Every other
sanitizer test patches it out to assert *which* directories are visited; this
module covers what the runner itself does — ordering, the SQL/DB it hands to
``_exec_sql``, the environment handed to the container, and the rule that a
failing script degrades to a warning instead of aborting provisioning.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import docker as _docker
from oduflow import sanitizer
from oduflow.env_credentials import MissingCredentialsError


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.prefix = "oduflow"
    settings.shared_db_container = "oduflow-db"
    settings.db_user = "odoo"
    settings.db_password = "pw"
    return settings


def _team(tmp_path) -> MagicMock:
    team = MagicMock()
    team.team_id = "1"
    team.data_dir = str(tmp_path / "team")
    team.workspaces_dir = str(tmp_path / "workspaces")
    return team


def _client(container: MagicMock | None = None) -> MagicMock:
    client = MagicMock()
    if container is None:
        client.containers.get.side_effect = _docker.errors.NotFound("nope")
    else:
        client.containers.get.return_value = container
    return client


def _run(tmp_path, scripts_dir, *, container=None, label="system"):
    return sanitizer._run_scripts_from_dir(
        str(scripts_dir),
        label,
        _client(container),
        _settings(),
        _team(tmp_path),
        "oduflow_1_main",
        "main",
    )


# The environment's scoped, non-superuser PostgreSQL role. Sanitize scripts run
# as this role, never as settings.db_user (the shared-cluster superuser).
_CREDS = {"pg_user": "u1", "pg_password": "s3cret"}


def _patch_creds():
    return patch.object(sanitizer, "load_credentials", return_value=dict(_CREDS))


class TestMissingDirectory:
    def test_absent_directory_is_a_no_op(self, tmp_path):
        with patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, tmp_path / "nope")

        assert logs == []
        exec_sql.assert_not_called()

    def test_empty_directory_is_a_no_op(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()

        with patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        assert logs == []
        exec_sql.assert_not_called()


class TestSqlScripts:
    def test_runs_sql_against_the_environment_database(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_wipe.sql").write_text("DELETE FROM res_partner;\n")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        assert exec_sql.call_count == 1
        assert exec_sql.call_args.args[2] == "DELETE FROM res_partner;"
        assert exec_sql.call_args.kwargs["db"] == "oduflow_1_main"
        # Runs as the scoped role, never the shared-cluster superuser (P-H9).
        assert exec_sql.call_args.kwargs["user"] == "u1"
        assert logs == ["[SANITIZE:system] Executed 10_wipe.sql"]

    def test_scripts_run_in_alphabetical_order(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        for name in ("30_c.sql", "10_a.sql", "20_b.sql"):
            (scripts / name).write_text(f"SELECT '{name}';")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        executed = [call.args[2] for call in exec_sql.call_args_list]
        assert executed == [
            "SELECT '10_a.sql';",
            "SELECT '20_b.sql';",
            "SELECT '30_c.sql';",
        ]
        assert logs == [
            "[SANITIZE:system] Executed 10_a.sql",
            "[SANITIZE:system] Executed 20_b.sql",
            "[SANITIZE:system] Executed 30_c.sql",
        ]

    def test_psql_metacommands_are_rejected(self, tmp_path):
        # The scoped role confines SQL, but psql interprets `\! cmd` client-side
        # and runs a shell inside the shared PostgreSQL container — a script
        # with any backslash-metacommand line must be skipped, not executed.
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_evil.sql").write_text("\\! id\n")
        (scripts / "20_mixed.sql").write_text("DELETE FROM res_partner;\n  \\! id\n")
        (scripts / "30_ok.sql").write_text("DELETE FROM res_users_log;\n")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        assert exec_sql.call_count == 1
        assert exec_sql.call_args.args[2] == "DELETE FROM res_users_log;"
        assert logs == [
            "[SANITIZE:system] WARNING: 10_evil.sql skipped: psql backslash "
            "metacommands are not allowed in sanitize scripts",
            "[SANITIZE:system] WARNING: 20_mixed.sql skipped: psql backslash "
            "metacommands are not allowed in sanitize scripts",
            "[SANITIZE:system] Executed 30_ok.sql",
        ]

    def test_blank_script_is_skipped_without_a_log_line(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "empty.sql").write_text("   \n\n")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        exec_sql.assert_not_called()
        assert logs == []

    def test_failing_sql_warns_and_the_run_continues(self, tmp_path, caplog):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_bad.sql").write_text("BOOM;")
        (scripts / "20_good.sql").write_text("SELECT 1;")

        def _exec(client, settings, sql, db=None, user=None):
            if "BOOM" in sql:
                raise RuntimeError("syntax error")

        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            _patch_creds(),
            patch.object(sanitizer, "_exec_sql", side_effect=_exec),
        ):
            logs = _run(tmp_path, scripts)

        assert logs == [
            "[SANITIZE:system] WARNING: 10_bad.sql failed: syntax error",
            "[SANITIZE:system] Executed 20_good.sql",
        ]
        assert "10_bad.sql" in caplog.text

    def test_non_sql_non_py_files_are_ignored(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "README.md").write_text("not a script")
        (scripts / "notes.txt").write_text("also not a script")

        with patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts)

        exec_sql.assert_not_called()
        assert logs == []


class TestPyScripts:
    def test_runs_python_in_the_container_with_db_credentials(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "anonymize.py").write_text("print('hi')")
        container = MagicMock()
        container.exec_run.return_value = (0, b"done")

        with patch.object(
            sanitizer,
            "load_credentials",
            return_value={"pg_user": "u1", "pg_password": "s3cret"},
        ):
            logs = _run(tmp_path, scripts, container=container)

        cmd, kwargs = (
            container.exec_run.call_args.args[0],
            container.exec_run.call_args.kwargs,
        )
        assert cmd == ["python3", "-c", "print('hi')"]
        assert kwargs["environment"] == {
            "ODOO_DB": "oduflow_1_main",
            "DB_HOST": "oduflow-db",
            "DB_USER": "u1",
            "DB_PASSWORD": "s3cret",
        }
        assert logs == ["[SANITIZE:system] Executed anonymize.py"]

    def test_nonzero_exit_warns_and_the_run_continues(self, tmp_path, caplog):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_bad.py").write_text("raise SystemExit(3)")
        (scripts / "20_good.py").write_text("pass")
        container = MagicMock()
        container.exec_run.side_effect = [(3, b"traceback"), (0, b"")]

        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            patch.object(
                sanitizer,
                "load_credentials",
                return_value={"pg_user": "u", "pg_password": "p"},
            ),
        ):
            logs = _run(tmp_path, scripts, container=container)

        assert logs == [
            "[SANITIZE:system] WARNING: 10_bad.py failed (exit 3)",
            "[SANITIZE:system] Executed 20_good.py",
        ]
        assert "10_bad.py" in caplog.text

    def test_exec_exception_is_contained(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "boom.py").write_text("pass")
        container = MagicMock()
        container.exec_run.side_effect = RuntimeError("docker exploded")

        with patch.object(
            sanitizer,
            "load_credentials",
            return_value={"pg_user": "u", "pg_password": "p"},
        ):
            logs = _run(tmp_path, scripts, container=container)

        assert logs == ["[SANITIZE:system] WARNING: boom.py failed: docker exploded"]

    def test_missing_container_skips_py_but_keeps_sql_results(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_first.sql").write_text("SELECT 1;")
        (scripts / "20_second.py").write_text("pass")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql") as exec_sql:
            logs = _run(tmp_path, scripts, container=None)

        assert exec_sql.call_count == 1
        assert logs == [
            "[SANITIZE:system] Executed 10_first.sql",
            "[SANITIZE:system] WARNING: container not found, skipping .py scripts",
        ]

    def test_sql_runs_before_py(self, tmp_path):
        # Ordering matters: .sql wipes rows, .py scripts then work on what is
        # left. The name below sorts before the .sql file to prove the tiers,
        # not the filenames, decide the order.
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "00_first.py").write_text("pass")
        (scripts / "99_last.sql").write_text("SELECT 1;")
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        with (
            patch.object(sanitizer, "_exec_sql"),
            patch.object(
                sanitizer,
                "load_credentials",
                return_value={"pg_user": "u", "pg_password": "p"},
            ),
        ):
            logs = _run(tmp_path, scripts, container=container)

        assert logs == [
            "[SANITIZE:system] Executed 99_last.sql",
            "[SANITIZE:system] Executed 00_first.py",
        ]

    def test_label_appears_in_every_log_line(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "a.sql").write_text("SELECT 1;")

        with _patch_creds(), patch.object(sanitizer, "_exec_sql"):
            logs = _run(tmp_path, scripts, label="repo-legacy")

        assert logs == ["[SANITIZE:repo-legacy] Executed a.sql"]


class TestScopedCredentials:
    """P-H9: sanitize scripts must never run as the shared-cluster superuser."""

    def test_missing_scoped_creds_skips_all_scripts(self, tmp_path, caplog):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_wipe.sql").write_text("DELETE FROM res_partner;")
        (scripts / "20_anon.py").write_text("pass")
        container = MagicMock()

        # No scoped credentials file -> load_credentials(allow_fallback=False)
        # raises. The runner must skip everything, not fall back to the superuser.
        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            patch.object(
                sanitizer,
                "load_credentials",
                side_effect=MissingCredentialsError("no creds"),
            ),
            patch.object(sanitizer, "_exec_sql") as exec_sql,
        ):
            logs = _run(tmp_path, scripts, container=container)

        exec_sql.assert_not_called()
        container.exec_run.assert_not_called()
        assert len(logs) == 1
        assert "no scoped database credentials" in logs[0]
        assert "skipping sanitize scripts" in logs[0]

    def test_scoped_creds_requested_without_superuser_fallback(self, tmp_path):
        scripts = tmp_path / "sanitize"
        scripts.mkdir()
        (scripts / "10_wipe.sql").write_text("SELECT 1;")

        with (
            patch.object(
                sanitizer, "load_credentials", return_value=dict(_CREDS)
            ) as load_creds,
            patch.object(sanitizer, "_exec_sql"),
        ):
            _run(tmp_path, scripts)

        # allow_fallback=False is what denies the superuser to legacy envs.
        assert load_creds.call_args.kwargs.get("allow_fallback") is False


class TestDetectOdooMajor:
    def test_reads_major_from_the_image_label(self):
        container = MagicMock()
        container.labels = {"oduflow.image": "odoo:18.0"}
        assert (
            sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
            == 18
        )

    def test_handles_registry_style_image_references(self):
        container = MagicMock()
        container.labels = {"oduflow.image": "ghcr.io/acme/odoo/17.0-custom"}
        assert (
            sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
            == 17
        )

    def test_unknown_or_missing_label_yields_none(self):
        container = MagicMock()
        container.labels = {"other": "odoo:18.0"}
        assert (
            sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
            is None
        )

        container.labels = {"oduflow.image": "postgres:16"}
        assert (
            sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
            is None
        )

    def test_non_dict_labels_yield_none(self):
        container = MagicMock()
        container.labels = ["not", "a", "dict"]
        assert (
            sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
            is None
        )
