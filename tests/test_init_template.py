"""init_template cleanup: the builder container and build DB never leak."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import system_ops as so
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings


def _settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    return Settings(base_data_dir=str(tmp_path), teams={"1": team}), team


def _run_init(tmp_path, builder, sql):
    """Run init_template with Docker mocked out, recording SQL into ``sql``."""
    settings, team = _settings(tmp_path)
    client = MagicMock()
    client.containers.run.return_value = builder

    with ExitStack() as stack:
        stack.enter_context(patch.object(so, "get_client", return_value=client))
        stack.enter_context(patch.object(so, "_ensure_pg_container"))
        stack.enter_context(patch.object(so, "_wait_pg_ready"))
        stack.enter_context(patch.object(so, "require_postgres_for_odoo"))
        stack.enter_context(patch.object(so, "ensure_team_network", return_value="n"))
        stack.enter_context(patch.object(so, "_db_exists", return_value=False))
        stack.enter_context(
            patch.object(
                so, "_exec_sql", side_effect=lambda c, s, q, *a, **kw: sql.append(q)
            )
        )
        so.init_template(settings, team, "base", "odoo:19.0", force=True)


class TestInitTemplateCleanup:
    def test_wait_timeout_removes_builder_and_drops_build_db(self, tmp_path):
        # A hung init (requests timeout from wait()) is an unexpected
        # exception, not the exit_code!=0 branch: the builder may still be
        # running and must be force-removed, and the build DB dropped.
        builder = MagicMock()
        builder.wait.side_effect = ConnectionError("read timed out")
        sql: list[str] = []

        with pytest.raises(ConnectionError, match="read timed out"):
            _run_init(tmp_path, builder, sql)

        builder.remove.assert_called_once_with(v=True, force=True)
        assert any(
            q.startswith("DROP DATABASE IF EXISTS oduflow_template_build") for q in sql
        )

    def test_failed_init_exit_code_still_cleans_up(self, tmp_path):
        builder = MagicMock()
        builder.wait.return_value = {"StatusCode": 1}
        builder.logs.return_value = b"boom"
        sql: list[str] = []

        with pytest.raises(ExternalCommandError, match="Odoo init failed"):
            _run_init(tmp_path, builder, sql)

        builder.remove.assert_called_once_with(v=True, force=True)
        assert any(
            q.startswith("DROP DATABASE IF EXISTS oduflow_template_build") for q in sql
        )
