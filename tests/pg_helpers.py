"""Reading the ``psql`` invocations Oduflow makes next to PostgreSQL."""

from __future__ import annotations

from unittest.mock import MagicMock

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


class PgContainer:
    """A fake PostgreSQL container that logs every ``psql`` it is asked to run.

    ``answers`` and ``failures`` map a fragment of a statement to the output
    or the error of the statements containing it. ``events``, when given, is a
    log shared with other fakes: each statement appends ``sql:<name>``, which
    lets a test place the statements between a container's stop and removal.
    """

    def __init__(
        self, superuser, *, name="dev", events=None, answers=None, failures=None
    ):
        self.calls: list[tuple[str, str, str]] = []
        self.name = name
        self._superuser = superuser
        self._events = events if events is not None else []
        self._answers = answers or {}
        self._failures = failures or {}
        self.container = MagicMock(name=name)
        self.container.status = "running"
        self.container.exec_run.side_effect = self._exec

    def _exec(self, cmd, **kwargs):
        call = psql_call(cmd, self._superuser)
        assert call is not None, cmd
        self.calls.append(call)
        self._events.append(f"sql:{self.name}")
        sql = call[2]
        for marker, error in self._failures.items():
            if marker in sql:
                return 1, error.encode()
        for marker, answer in self._answers.items():
            if marker in sql:
                return 0, answer.encode()
        return 0, b""

    def sql(self, database=None):
        """Statements run so far, optionally only those in *database*."""
        return [sql for _role, db, sql in self.calls if database in (None, db)]
