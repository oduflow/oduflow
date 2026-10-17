"""Unit coverage for persistent Agent Chat attachments."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock

import pytest

from oduflow import agent_uids, agent_uploads
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def team_data_dir(tmp_path):
    return str(tmp_path)


def _settings(data_dir: str = "") -> tuple[Settings, TeamSettings]:
    team = TeamSettings(team_id="7", agent_enabled=True, data_dir=data_dir)
    settings = Settings(prefix="test-", teams={"7": team})
    return settings, team


def _settings_with_uid(data_dir: str, env_name: str):
    settings, team = _settings(data_dir)
    return settings, team, agent_uids.allocate_uid(team, env_name)


def _containers(monkeypatch, *, used_bytes: int = 0):
    environment = MagicMock()
    agent = MagicMock(status="running")

    def exec_run(command, **_kwargs):
        if command[:2] == ["du", "-sb"]:
            return 0, f"{used_bytes}\t{command[2]}\n".encode()
        return 0, b""

    agent.exec_run.side_effect = exec_run
    agent.put_archive.return_value = True
    client = MagicMock()
    client.containers.get.side_effect = lambda name: (
        environment if name.endswith("-odoo") else agent
    )
    monkeypatch.setattr(agent_uploads, "get_client", lambda: client)
    return agent


def test_store_attachment_copies_safe_private_tar(monkeypatch, team_data_dir):
    settings, team, uid = _settings_with_uid(team_data_dir, "feature/x")
    agent = _containers(monkeypatch)

    result = agent_uploads.store_attachment(
        settings,
        team,
        "feature/x",
        "../Q2 invoice.pdf",
        "application/pdf; charset=binary",
        io.BytesIO(b"pdf-data"),
        8,
    )

    assert result["name"] == "Q2 invoice.pdf"
    assert result["path"].startswith("/workspace/.oduflow-uploads/feature-x/")
    assert result["path"].endswith("/Q2_invoice.pdf")
    assert result["uri"].startswith("file:///workspace/.oduflow-uploads/")
    assert result["mimeType"] == "application/pdf"
    assert result["size"] == 8

    root, archive = agent.put_archive.call_args.args
    assert root == "/workspace/.oduflow-uploads/feature-x"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        members = tar.getmembers()
        file_member = next(member for member in members if member.isfile())
        assert file_member.name.endswith("/Q2_invoice.pdf")
        assert file_member.mode == 0o600
        # Owned by the environment's own uid: no other environment's session
        # can read it.
        assert file_member.uid == uid
        assert file_member.gid == uid
        extracted = tar.extractfile(file_member)
        assert extracted is not None
        assert extracted.read() == b"pdf-data"


def test_store_attachment_enforces_environment_quota(monkeypatch, team_data_dir):
    settings, team, _uid = _settings_with_uid(team_data_dir, "main")
    agent = _containers(monkeypatch, used_bytes=agent_uploads.MAX_ENV_BYTES)

    with pytest.raises(agent_uploads.AttachmentError, match="storage.*full") as exc:
        agent_uploads.store_attachment(
            settings,
            team,
            "main",
            "notes.txt",
            "text/plain",
            io.BytesIO(b"x"),
            1,
        )

    assert exc.value.status_code == 413
    agent.put_archive.assert_not_called()


def test_store_attachment_rejects_oversized_file_before_docker(monkeypatch):
    settings, team = _settings()
    get_client = MagicMock()
    monkeypatch.setattr(agent_uploads, "get_client", get_client)

    with pytest.raises(agent_uploads.AttachmentError) as exc:
        agent_uploads.store_attachment(
            settings,
            team,
            "main",
            "large.bin",
            "application/octet-stream",
            io.BytesIO(),
            agent_uploads.MAX_FILE_BYTES + 1,
        )

    assert exc.value.status_code == 413
    get_client.assert_not_called()


def test_delete_attachment_is_scoped_to_environment(monkeypatch, team_data_dir):
    settings, team, uid = _settings_with_uid(team_data_dir, "feature/x")
    agent = _containers(monkeypatch)

    agent_uploads.delete_attachment(settings, team, "feature/x", "a" * 32)

    assert agent.exec_run.call_args.args[0] == [
        "rm",
        "-rf",
        "--",
        "/workspace/.oduflow-uploads/feature-x/" + "a" * 32,
    ]
    assert agent.exec_run.call_args.kwargs["user"] == f"{uid}:{uid}"
    # Same identity as the environment's sessions, not just its uid.
    environment = agent.exec_run.call_args.kwargs["environment"]
    assert environment["HOME"] == f"/home/agent/envs/{uid}"
    assert environment["TMPDIR"] == f"/tmp/user/{uid}"


def test_store_attachment_requires_environment_identity(monkeypatch, team_data_dir):
    settings, team = _settings(team_data_dir)
    agent = _containers(monkeypatch)

    with pytest.raises(agent_uploads.AttachmentError, match="not set up") as exc:
        agent_uploads.store_attachment(
            settings,
            team,
            "main",
            "notes.txt",
            "text/plain",
            io.BytesIO(b"x"),
            1,
        )

    assert exc.value.status_code == 409
    agent.exec_run.assert_not_called()
    agent.put_archive.assert_not_called()


@pytest.mark.parametrize("upload_id", ["../escape", "ABC", "", "a" * 31])
def test_delete_attachment_rejects_invalid_id_before_docker(monkeypatch, upload_id):
    settings, team = _settings()
    get_client = MagicMock()
    monkeypatch.setattr(agent_uploads, "get_client", get_client)

    with pytest.raises(agent_uploads.AttachmentError, match="Invalid attachment id"):
        agent_uploads.delete_attachment(settings, team, "main", upload_id)

    get_client.assert_not_called()
