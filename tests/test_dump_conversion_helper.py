"""The helper that rewrites an unreadable archive as SQL is confined.

An archive written by a newer pg_dump than the cluster's is converted to plain
SQL by ``pg_restore -f`` in a ``postgres:17`` container before it is loaded.
That archive is tenant input, and the helper used to parse it as root with
the directory holding it — the template directory, an import's staging
directory, the team's dump exchange directory — mounted read-write.

These tests pin the confinement against a fake Docker: what the helper is
given (the archive read-only, one fresh scratch directory, no network, no
root, no capabilities), and that what it leaves behind is checked before
anything uses it.
"""

import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings

SQL = b"-- converted\nCREATE TABLE t ();\n"


class _FakeDocker:
    """A Docker whose helper container "runs" *convert* on its output mount."""

    def __init__(self, convert=None, *, exit_code=0, output=b"", status="exited"):
        self.runs: list[dict] = []
        self.events: list[str] = []
        self._convert = convert
        self._exit_code = exit_code
        self._output = output
        self._status = status
        self.container = None
        self.client = MagicMock(name="docker")
        self.client.containers.run.side_effect = self._run

    def _run(self, image, command=None, **kwargs):
        self.runs.append({"image": image, "command": command, **kwargs})
        self.events.append("run")
        out_dir = next(
            host
            for host, mount in kwargs["volumes"].items()
            if mount["bind"] == "/convert/out"
        )
        if self._convert is not None:
            self._convert(out_dir)
        container = MagicMock(name=kwargs.get("name"))
        container.status = self._status
        container.wait.return_value = {"StatusCode": self._exit_code}
        container.logs.return_value = self._output
        container.kill.side_effect = lambda: self.events.append("kill")
        container.remove.side_effect = lambda **kw: self.events.append("remove")
        self.container = container
        return container

    @property
    def run(self):
        (only,) = self.runs
        return only


def _write_sql(out_dir, content=SQL):
    with open(os.path.join(out_dir, "dump.sql"), "wb") as fh:
        fh.write(content)


def _settings(tmp_path):
    team = TeamSettings(team_id="7", data_dir=str(tmp_path / "team_7"))
    return Settings(base_data_dir=str(tmp_path), teams={"7": team}), team


def _archive(team, *, name="dump.pgdump", mode=0o644):
    directory = team.get_template_dir("prod")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(b"PGDMP-archive")
    os.chmod(path, mode)
    return path


def _scratch_root(team):
    return os.path.join(team.data_dir, ".dump_convert")


def _convert(tmp_path, docker_fake, *, name="dump.pgdump", mode=0o644, gz=False):
    settings, team = _settings(tmp_path)
    archive = _archive(team, name=name, mode=mode)
    return (
        system_ops._converted_sql_dump(
            docker_fake.client, settings, team, archive, is_gzipped=gz
        ),
        team,
        archive,
    )


# --------------------------------------------------------------------------
# what the helper is given
# --------------------------------------------------------------------------


def test_the_helper_sees_the_archive_read_only_and_one_scratch_directory(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, team, archive = _convert(tmp_path, fake)

    with conversion as sql_path:
        run = fake.run
        mounts = run["volumes"]
        (out_dir,) = [h for h, m in mounts.items() if m["bind"] == "/convert/out"]
        scratch = os.path.dirname(out_dir)

        # Two mounts and no more: the archive itself, read-only, and a
        # directory made for this conversion.
        assert mounts == {
            archive: {"bind": "/convert/in", "mode": "ro"},
            out_dir: {"bind": "/convert/out", "mode": "rw"},
        }
        assert os.path.dirname(scratch) == _scratch_root(team)
        template_dir = team.get_template_dir("prod")
        for host in mounts:
            # Neither the template directory nor anything above it.
            assert not template_dir.startswith(host + os.sep)
            assert host != template_dir
        # Closed to every other account on the host.
        assert stat.S_IMODE(os.stat(scratch).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(_scratch_root(team)).st_mode) == 0o700

        # The SQL was taken out of the directory the helper could write to.
        assert sql_path == os.path.join(scratch, "dump.sql")
        assert os.listdir(out_dir) == []
        with open(sql_path, "rb") as fh:
            assert fh.read() == SQL
        taken = os.lstat(sql_path)
        assert stat.S_ISREG(taken.st_mode)
        assert stat.S_IMODE(taken.st_mode) == 0o644

    # Nothing of the conversion outlives it, next to the archive or elsewhere.
    assert os.listdir(_scratch_root(team)) == []
    assert os.listdir(team.get_template_dir("prod")) == ["dump.pgdump"]
    with open(archive, "rb") as fh:
        assert fh.read() == b"PGDMP-archive"


def test_the_helper_has_no_network_no_root_and_no_capabilities(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, _team, _archive_path = _convert(tmp_path, fake)

    with conversion:
        pass

    run = fake.run
    assert run["image"] == "postgres:17"
    # The client itself, not the image's server entrypoint.
    assert run["entrypoint"] == "pg_restore"
    assert run["command"] == [
        "--no-owner",
        "-f",
        "/convert/out/dump.sql",
        "/convert/in",
    ]
    assert run["network_mode"] == "none"
    assert "network" not in run
    assert run["cap_drop"] == ["ALL"]
    assert "cap_add" not in run
    assert run["security_opt"] == ["no-new-privileges"]
    assert "privileged" not in run
    assert run["read_only"] is True
    assert run["tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=16m"}
    assert run["pids_limit"] == 64
    assert run["mem_limit"] > 0
    assert run["detach"] is True
    # No secret reaches it: it connects to nothing.
    assert run["environment"] == {"HOME": "/tmp"}
    uid, gid = (int(part) for part in run["user"].split(":"))
    assert uid != 0
    assert run["name"].startswith("oduflow-pg-restore-helper-")
    # Removed before its output is looked at, and removed for good.
    assert fake.events == ["run", "remove"]
    fake.container.remove.assert_called_once_with(force=True)
    fake.container.logs.assert_called_once_with(stdout=True, stderr=True, tail=200)


def test_a_gzipped_archive_is_piped_inside_the_helper(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, _team, archive = _convert(
        tmp_path, fake, name="dump.pgdump.gz", gz=True
    )

    with conversion:
        pass

    run = fake.run
    assert run["entrypoint"] == "bash"
    assert run["command"] == [
        "-c",
        "set -o pipefail; gunzip -c /convert/in | "
        "pg_restore --no-owner -f /convert/out/dump.sql",
    ]
    assert run["volumes"][archive] == {"bind": "/convert/in", "mode": "ro"}
    assert run["network_mode"] == "none"
    assert run["read_only"] is True


# --------------------------------------------------------------------------
# who the helper is
# --------------------------------------------------------------------------


def _stat(mode, uid, gid=0):
    return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid)


@pytest.mark.parametrize(
    ("euid", "dump", "expected"),
    [
        # Oduflow as root: nobody reads what anyone may read ...
        (0, _stat(0o644, 0), (65534, 65534, False)),
        (0, _stat(0o644, 999, 999), (65534, 65534, False)),
        # ... a private archive is read as its owner ...
        (0, _stat(0o600, 999, 999), (999, 999, False)),
        # ... and root's own private archive is copied rather than read as root.
        (0, _stat(0o600, 0), (65534, 65534, True)),
        (0, _stat(0o640, 0), (65534, 65534, True)),
        # Oduflow without root cannot give the scratch directory away.
        (1000, _stat(0o644, 1000, 1000), (1000, 1000, False)),
        (1000, _stat(0o600, 1000, 1000), (1000, 1000, False)),
    ],
)
def test_the_helper_account_is_never_root(euid, dump, expected):
    with (
        patch.object(system_ops.os, "geteuid", return_value=euid),
        patch.object(system_ops.os, "getegid", return_value=euid),
        patch.object(system_ops.os, "stat", return_value=dump),
    ):
        account = system_ops._conversion_helper_account("/data/dump.pgdump")

    assert account == expected
    assert account[0] != 0


def test_as_root_the_scratch_directory_is_given_to_the_helper_account(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, _team, archive = _convert(tmp_path, fake)
    own = (os.geteuid(), os.getegid())

    with (
        patch.object(
            system_ops, "_conversion_helper_account", return_value=(65534, 65534, False)
        ),
        patch.object(system_ops.os, "chown") as chown,
    ):
        with conversion:
            run = fake.run

    (out_dir,) = [h for h, m in run["volumes"].items() if m["bind"] == "/convert/out"]
    assert run["user"] == "65534:65534"
    assert run["volumes"][archive]["mode"] == "ro"
    if own != (65534, 65534):
        # Only the directory the helper writes to, and nothing recursive.
        chown.assert_called_once_with(out_dir, 65534, 65534)


def test_roots_private_archive_reaches_the_helper_as_a_copy(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, _team, archive = _convert(tmp_path, fake, mode=0o600)

    with (
        patch.object(
            system_ops, "_conversion_helper_account", return_value=(65534, 65534, True)
        ),
        patch.object(system_ops.os, "chown"),
    ):
        with conversion:
            run = fake.run
            (source,) = [
                h for h, m in run["volumes"].items() if m["bind"] == "/convert/in"
            ]
            (out_dir,) = [
                h for h, m in run["volumes"].items() if m["bind"] == "/convert/out"
            ]
            scratch = os.path.dirname(out_dir)
            # The original is not mounted at all; the copy sits in the scratch
            # directory, which no other account can enter.
            assert archive not in run["volumes"]
            assert source == os.path.join(scratch, "in")
            assert run["volumes"][source] == {"bind": "/convert/in", "mode": "ro"}
            with open(source, "rb") as fh:
                assert fh.read() == b"PGDMP-archive"
            assert stat.S_IMODE(os.stat(source).st_mode) == 0o644
            assert stat.S_IMODE(os.stat(scratch).st_mode) == 0o700

    assert run["user"] == "65534:65534"
    # The original keeps its permissions.
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600


# --------------------------------------------------------------------------
# what the helper leaves behind
# --------------------------------------------------------------------------


def _leave_symlink(target):
    def convert(out_dir):
        os.symlink(target, os.path.join(out_dir, "dump.sql"))

    return convert


def _leave_nothing(out_dir):
    with open(os.path.join(out_dir, "other.sql"), "wb") as fh:
        fh.write(SQL)


def _leave_empty(out_dir):
    _write_sql(out_dir, b"")


def _leave_directory(out_dir):
    os.mkdir(os.path.join(out_dir, "dump.sql"))


def _leave_fifo(out_dir):
    os.mkfifo(os.path.join(out_dir, "dump.sql"))


def _leave_hardlinked(out_dir):
    _write_sql(out_dir)
    os.link(os.path.join(out_dir, "dump.sql"), os.path.join(out_dir, "second-name"))


@pytest.mark.parametrize(
    ("convert", "message"),
    [
        (_leave_nothing, "produced no SQL file"),
        (_leave_empty, "empty SQL file"),
        (_leave_directory, "other than a regular file"),
        (_leave_fifo, "other than a regular file"),
        (_leave_hardlinked, "other than a regular file"),
    ],
)
def test_output_that_is_not_one_regular_file_is_refused(tmp_path, convert, message):
    fake = _FakeDocker(convert)
    conversion, team, _archive_path = _convert(tmp_path, fake)

    with pytest.raises(ExternalCommandError) as failure:
        with conversion:
            pytest.fail("the output must not be handed out")

    assert message in failure.value.output
    assert fake.events == ["run", "remove"]
    assert os.listdir(_scratch_root(team)) == []


def test_a_link_left_as_the_output_is_refused_and_not_followed(tmp_path):
    secret = tmp_path / "host-secret"
    secret.write_bytes(b"the host's own file")
    secret.chmod(0o600)
    fake = _FakeDocker(_leave_symlink(str(secret)))
    conversion, team, _archive_path = _convert(tmp_path, fake)

    with pytest.raises(ExternalCommandError) as failure:
        with conversion:
            pytest.fail("a link must not be handed out as the dump")

    assert "other than a regular file" in failure.value.output
    # Neither read, nor re-owned, nor made readable, nor removed.
    assert secret.read_bytes() == b"the host's own file"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert os.listdir(_scratch_root(team)) == []


# --------------------------------------------------------------------------
# a helper that fails, hangs or fills the disk
# --------------------------------------------------------------------------


def test_a_failed_conversion_reports_the_helper_output(tmp_path):
    fake = _FakeDocker(
        _write_sql, exit_code=1, output=b"pg_restore: error: corrupt tar header"
    )
    conversion, team, archive = _convert(tmp_path, fake)

    with pytest.raises(ExternalCommandError) as failure:
        with conversion:
            pytest.fail("a failed conversion must not hand out its output")

    assert failure.value.exit_code == 1
    assert "corrupt tar header" in failure.value.output
    assert fake.events == ["run", "remove"]
    assert os.listdir(_scratch_root(team)) == []
    assert os.path.isfile(archive)


def test_a_helper_that_cannot_start_leaves_no_scratch_directory(tmp_path):
    fake = _FakeDocker()
    fake.client.containers.run.side_effect = docker.errors.APIError("no such image")
    conversion, team, _archive_path = _convert(tmp_path, fake)

    with pytest.raises(ExternalCommandError) as failure:
        with conversion:
            pytest.fail("nothing was converted")

    assert "could not run" in failure.value.output
    assert os.listdir(_scratch_root(team)) == []


def test_a_helper_past_its_deadline_is_killed(tmp_path):
    fake = _FakeDocker(_write_sql, status="running")
    conversion, team, _archive_path = _convert(tmp_path, fake)

    with (
        patch.object(system_ops, "_CONVERT_TIMEOUT_SECONDS", -1),
        patch.object(system_ops.time, "sleep") as sleep,
    ):
        with pytest.raises(ExternalCommandError) as failure:
            with conversion:
                pytest.fail("a conversion that never finished has no output")

    assert "did not finish" in failure.value.output
    assert fake.events == ["run", "kill", "remove"]
    sleep.assert_not_called()
    assert os.listdir(_scratch_root(team)) == []


def test_a_helper_filling_the_disk_is_killed(tmp_path):
    fake = _FakeDocker(_write_sql, status="running")
    conversion, team, _archive_path = _convert(tmp_path, fake)

    with (
        patch.object(
            system_ops.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=512 * 1024**2),
        ),
        patch.object(system_ops.time, "sleep"),
    ):
        with pytest.raises(ExternalCommandError) as failure:
            with conversion:
                pytest.fail("a stopped conversion has no output")

    assert "disk space" in failure.value.output
    assert fake.events == ["run", "kill", "remove"]
    assert os.listdir(_scratch_root(team)) == []


def test_a_running_helper_is_waited_for(tmp_path):
    fake = _FakeDocker(_write_sql, status="running")
    conversion, _team, _archive_path = _convert(tmp_path, fake)

    def finish(_seconds):
        fake.container.status = "exited"

    with (
        patch.object(
            system_ops.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=100 * 1024**3),
        ),
        patch.object(system_ops.time, "sleep", side_effect=finish) as sleep,
    ):
        with conversion as sql_path:
            assert os.path.isfile(sql_path)

    sleep.assert_called_once_with(system_ops._CONVERT_POLL_SECONDS)
    assert fake.events == ["run", "remove"]


def test_scratch_directories_a_crash_left_behind_are_swept(tmp_path):
    fake = _FakeDocker(_write_sql)
    conversion, team, _archive_path = _convert(tmp_path, fake)
    root = _scratch_root(team)
    stale = os.path.join(root, "0" * 32)
    recent = os.path.join(root, "1" * 32)
    for directory in (stale, recent):
        os.makedirs(os.path.join(directory, "out"))
        _write_sql(os.path.join(directory, "out"))
    two_days_ago = os.stat(stale).st_mtime - 2 * 24 * 3600
    os.utime(stale, (two_days_ago, two_days_ago))

    with conversion:
        pass

    # Another conversion may be running in the recent one.
    assert os.listdir(root) == ["1" * 32]


def test_the_start_sweeps_abandoned_conversions_of_every_team(tmp_path):
    settings, team = _settings(tmp_path)
    root = _scratch_root(team)
    stale = os.path.join(root, "0" * 32)
    recent = os.path.join(root, "1" * 32)
    for directory in (stale, recent):
        os.makedirs(os.path.join(directory, "out"))
    two_days_ago = os.stat(stale).st_mtime - 2 * 24 * 3600
    os.utime(stale, (two_days_ago, two_days_ago))
    # Not a directory of ours: left alone, whatever its age.
    stray = os.path.join(root, "notes.txt")
    with open(stray, "w") as fh:
        fh.write("an operator's file")
    os.utime(stray, (two_days_ago, two_days_ago))

    system_ops.sweep_dump_conversions(settings)

    assert sorted(os.listdir(root)) == ["1" * 32, "notes.txt"]
    # A team that never converted anything has no directory to sweep.
    other = TeamSettings(team_id="8", data_dir=str(tmp_path / "team_8"))
    system_ops.sweep_dump_conversions(
        Settings(base_data_dir=str(tmp_path), teams={"8": other})
    )


# --------------------------------------------------------------------------
# keeping the SQL as the template's dump
# --------------------------------------------------------------------------


def test_the_converted_sql_replaces_the_archive_it_came_from(tmp_path):
    template_dir = tmp_path / "templates" / "prod"
    template_dir.mkdir(parents=True)
    archive = template_dir / "dump.pgdump"
    archive.write_bytes(b"PGDMP-archive")
    sql = tmp_path / "scratch" / "dump.sql"
    sql.parent.mkdir()
    sql.write_bytes(SQL)

    system_ops._keep_converted_dump(str(sql), str(archive))

    assert sorted(os.listdir(template_dir)) == ["dump.sql"]
    assert (template_dir / "dump.sql").read_bytes() == SQL
    assert not sql.exists()


def test_an_archive_named_like_the_sql_is_replaced_not_truncated(tmp_path):
    # Format is detected from content, so a custom archive may be called
    # dump.sql: converting it in place used to truncate it before reading.
    template_dir = tmp_path / "templates" / "prod"
    template_dir.mkdir(parents=True)
    archive = template_dir / "dump.sql"
    archive.write_bytes(b"PGDMP-archive")
    sql = tmp_path / "scratch" / "dump.sql"
    sql.parent.mkdir()
    sql.write_bytes(SQL)

    system_ops._keep_converted_dump(str(sql), str(archive))

    assert os.listdir(template_dir) == ["dump.sql"]
    assert archive.read_bytes() == SQL


def test_an_archive_stays_when_the_sql_cannot_be_kept(tmp_path, caplog):
    template_dir = tmp_path / "templates" / "prod"
    template_dir.mkdir(parents=True)
    archive = template_dir / "dump.pgdump"
    archive.write_bytes(b"PGDMP-archive")
    sql = tmp_path / "scratch" / "dump.sql"
    sql.parent.mkdir()
    sql.write_bytes(SQL)

    with (
        patch.object(system_ops.os, "replace", side_effect=OSError("EXDEV")),
        patch.object(system_ops.shutil, "move", side_effect=OSError("ENOSPC")),
        caplog.at_level("WARNING", logger="oduflow"),
    ):
        system_ops._keep_converted_dump(str(sql), str(archive))

    assert os.listdir(template_dir) == ["dump.pgdump"]
    assert "the archive stays" in caplog.text
