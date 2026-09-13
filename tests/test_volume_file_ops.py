import tarfile
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import volume_file_ops
from oduflow.docker_ops.volume_file_ops import _MOUNT_POINT, _safe_path
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEST_TEAM},
)


@pytest.fixture
def mock_docker_client():
    with patch("oduflow.docker_ops.volume_file_ops.get_client") as mock:
        client_instance = MagicMock()
        mock.return_value = client_instance
        yield client_instance


# ---------------------------------------------------------------------------
# _safe_path
# ---------------------------------------------------------------------------


class TestSafePath:
    def test_normal_path(self):
        assert _safe_path("data/file.txt") == f"{_MOUNT_POINT}/data/file.txt"

    def test_leading_slash(self):
        assert _safe_path("/data/file.txt") == f"{_MOUNT_POINT}/data/file.txt"

    def test_empty_path(self):
        assert _safe_path("") == _MOUNT_POINT

    def test_root_slash(self):
        assert _safe_path("/") == _MOUNT_POINT

    def test_path_traversal_blocked(self):
        with pytest.raises(ConflictError):
            _safe_path("../../etc/passwd")

    def test_path_traversal_via_middle(self):
        with pytest.raises(ConflictError):
            _safe_path("data/../../etc/passwd")

    def test_nested_path(self):
        assert _safe_path("a/b/c/d.txt") == f"{_MOUNT_POINT}/a/b/c/d.txt"

    def test_dot_path(self):
        assert _safe_path(".") == _MOUNT_POINT


# ---------------------------------------------------------------------------
# read_file_in_volume
# ---------------------------------------------------------------------------


class TestReadFileInVolume:
    def test_read_file(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = (
            b"TYPE:FILE\nSIZE:42\nCONTENT_START\nhello world\n"
        )

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "file.txt"
        )

        assert result["type"] == "file"
        assert "hello world" in result["output"]
        assert result["size"] == 42

    def test_read_directory(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = (
            b"TYPE:DIR\ntotal 4\ndrwxr-xr-x 2 root root 4096 data\n"
        )

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "/"
        )

        assert result["type"] == "directory"
        assert "data" in result["output"]

    def test_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = b"TYPE:NOTFOUND\n"

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "missing.txt"
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_binary_file(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = b"TYPE:BINARY\nSIZE:1024\n"

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "data.bin"
        )

        assert "error" in result
        assert "binary" in result["error"].lower()

    def test_file_too_large(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = b"TYPE:TOOLARGE\nSIZE:200000\n"

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "big.log"
        )

        assert "error" in result
        assert "too large" in result["error"].lower()

    def test_read_range(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = (
            b"TYPE:FILE\nSIZE:5000\nCONTENT_START\nline 10\nline 11\n"
        )

        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "file.txt", read_range="10:11"
        )

        assert result["type"] == "file"
        assert result["range"] == "10:11"

    def test_invalid_range_format(self, mock_docker_client):
        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "file.txt", read_range="bad"
        )
        assert "error" in result

    def test_invalid_range_values(self, mock_docker_client):
        result = volume_file_ops.read_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "file.txt", read_range="a:b"
        )
        assert "error" in result

    def test_volume_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError):
            volume_file_ops.read_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "nonexistent", "file.txt"
            )


# ---------------------------------------------------------------------------
# write_file_in_volume
# ---------------------------------------------------------------------------


class TestWriteFileInVolume:
    def test_replacement_advances_filesystem_mtime_within_one_second(
        self, mock_docker_client, tmp_path
    ):
        container = MagicMock()
        mock_docker_client.containers.run.return_value = container

        def extract_archive(_parent, stream):
            with tarfile.open(fileobj=stream, mode="r") as archive:
                member = archive.getmember("app.conf")
                assert member.isfile()
                archive.extract(member, tmp_path)

        container.put_archive.side_effect = extract_archive
        timestamps = [1_789_253_700.125, 1_789_253_700.875]
        observed = []
        with patch.object(volume_file_ops.time, "time", side_effect=timestamps):
            for content in ("version=1", "version=2"):
                volume_file_ops.write_file_in_volume(
                    TEST_SETTINGS, TEST_TEAM, "mydata", "app.conf", content
                )
                target = tmp_path / "app.conf"
                assert target.read_text() == content
                observed.append(target.stat().st_mtime)

        assert observed == timestamps
        assert int(observed[0]) == int(observed[1])
        assert observed[0] != observed[1]

    def test_write_file(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_container = MagicMock()
        mock_docker_client.containers.run.return_value = mock_container

        result = volume_file_ops.write_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "config/app.conf", "key=value"
        )

        assert result["path"] == "config/app.conf"
        assert result["size"] == len("key=value".encode("utf-8"))
        mock_container.exec_run.assert_called_once()
        mock_container.put_archive.assert_called_once()
        mock_container.stop.assert_called_once()
        mock_container.remove.assert_called_once()

    def test_content_too_large(self, mock_docker_client):
        with pytest.raises(ConflictError):
            volume_file_ops.write_file_in_volume(
                TEST_SETTINGS,
                TEST_TEAM,
                "mydata",
                "big.txt",
                "x" * 2_000_000,
            )

    def test_volume_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError):
            volume_file_ops.write_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "nonexistent", "file.txt", "content"
            )

    def test_path_traversal_blocked(self, mock_docker_client):
        with pytest.raises(ConflictError):
            volume_file_ops.write_file_in_volume(
                TEST_SETTINGS,
                TEST_TEAM,
                "mydata",
                "../../etc/passwd",
                "malicious",
            )

    def test_cleanup_on_error(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = RuntimeError("container error")
        mock_docker_client.containers.run.return_value = mock_container

        with pytest.raises(RuntimeError):
            volume_file_ops.write_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "mydata", "file.txt", "content"
            )

        # Container should still be cleaned up
        mock_container.stop.assert_called_once()
        mock_container.remove.assert_called_once()


# ---------------------------------------------------------------------------
# search_in_volume
# ---------------------------------------------------------------------------


class TestSearchInVolume:
    def test_matches_found(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = (
            b"/mnt/volume/config/app.conf:3:key=value\n"
            b"/mnt/volume/config/app.conf:7:key=other\n"
        )

        result = volume_file_ops.search_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "key=", glob="*.conf"
        )

        assert result["matches"] == 2
        assert "config/app.conf" in result["output"]
        assert not result["truncated"]
        # Mount point prefix should be stripped
        assert "/mnt/volume/" not in result["output"]

    def test_no_matches(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.side_effect = docker.errors.ContainerError(
            container=MagicMock(),
            exit_status=1,
            command="grep",
            image="alpine",
            stderr=b"",
        )

        result = volume_file_ops.search_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "nonexistent"
        )

        assert result["matches"] == 0
        assert result["output"] == ""

    def test_truncation(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        lines = "\n".join(f"/mnt/volume/file.txt:{i}:match" for i in range(100))
        mock_docker_client.containers.run.return_value = lines.encode()

        result = volume_file_ops.search_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "match", max_results=10
        )

        assert result["matches"] == 10
        assert result["truncated"] is True

    def test_volume_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError):
            volume_file_ops.search_in_volume(
                TEST_SETTINGS, TEST_TEAM, "nonexistent", "pattern"
            )

    def test_non_exit1_error_reraised(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.side_effect = docker.errors.ContainerError(
            container=MagicMock(),
            exit_status=2,
            command="grep",
            image="alpine",
            stderr=b"grep: invalid option",
        )

        with pytest.raises(docker.errors.ContainerError):
            volume_file_ops.search_in_volume(
                TEST_SETTINGS, TEST_TEAM, "mydata", "pattern"
            )

    def test_search_with_path(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = (
            b"/mnt/volume/subdir/file.txt:1:found\n"
        )

        result = volume_file_ops.search_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "found", path="subdir"
        )

        assert result["matches"] == 1


# ---------------------------------------------------------------------------
# delete_file_in_volume
# ---------------------------------------------------------------------------


class TestDeleteFileInVolume:
    def test_delete_file(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = b"DELETED\n"

        result = volume_file_ops.delete_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "old/data.txt"
        )

        assert result["status"] == "deleted"
        assert result["path"] == "old/data.txt"

    def test_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = b"NOTFOUND\n"

        result = volume_file_ops.delete_file_in_volume(
            TEST_SETTINGS, TEST_TEAM, "mydata", "missing.txt"
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_cannot_delete_volume_root(self, mock_docker_client):
        with pytest.raises(ConflictError):
            volume_file_ops.delete_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "mydata", "/"
            )

    def test_cannot_delete_empty_path(self, mock_docker_client):
        with pytest.raises(ConflictError):
            volume_file_ops.delete_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "mydata", ""
            )

    def test_volume_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError):
            volume_file_ops.delete_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "nonexistent", "file.txt"
            )

    def test_path_traversal_blocked(self, mock_docker_client):
        with pytest.raises(ConflictError):
            volume_file_ops.delete_file_in_volume(
                TEST_SETTINGS, TEST_TEAM, "mydata", "../../etc/passwd"
            )
