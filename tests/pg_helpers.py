"""Reading the ``psql`` invocations Oduflow makes next to PostgreSQL."""

from __future__ import annotations

from oduflow.docker_ops.system_ops import SUPERUSER_SEARCH_PATH_SQL

# What the superuser's psql carries before the role: no command tags in the
# output, and a failed pin stops the run.
_SUPERUSER_HEAD = ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-U"]


def psql_call(cmd: list[str], superuser: str) -> tuple[str, str, str] | None:
    """``(role, database, sql)`` of a ``system_ops._exec_sql`` command, else None.

    A statement run as *superuser* must come second in its session, after the
    statement that pins ``search_path``: this fails the test when it does not.
    """
    if cmd[:2] == ["psql", "-U"] and cmd[3] == "-d" and cmd[5] == "-tAc":
        assert len(cmd) == 7, cmd
        role, database, sql = cmd[2], cmd[4], cmd[6]
        assert role != superuser, f"superuser statement without a pin: {cmd}"
        return role, database, sql
    if cmd[:5] == _SUPERUSER_HEAD:
        assert len(cmd) == 12, cmd
        role, database = cmd[5], cmd[7]
        assert cmd[6] == "-d", cmd
        assert cmd[8:11] == ["-c", SUPERUSER_SEARCH_PATH_SQL, "-tAc"], cmd
        assert role == superuser, cmd
        return role, database, cmd[11]
    return None
