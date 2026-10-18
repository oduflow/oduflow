"""Unit tests for recreate_environment's handling of name-keyed credentials
(the scoped MCP token and the dashboard share secret)."""

from __future__ import annotations

import pytest

from oduflow import env_share, env_tokens
from oduflow.docker_ops import env_ops, system_ops
from oduflow.settings import Settings, TeamSettings


class _FakeContainer:
    def __init__(self, labels: dict[str, str]):
        self.labels = labels


class _FakeContainers:
    def __init__(self, container: _FakeContainer):
        self._container = container

    def get(self, name: str) -> _FakeContainer:
        return self._container


class _FakeClient:
    def __init__(self, container: _FakeContainer):
        self.containers = _FakeContainers(container)


def _settings(tmp_path) -> tuple[Settings, TeamSettings]:
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team_1"),
        port_range_start=50000,
        port_range_end=50100,
    )
    return Settings(teams={"1": team}), team


@pytest.fixture(autouse=True)
def _clear_cache():
    env_tokens.invalidate_cache()
    yield
    env_tokens.invalidate_cache()


def _stub_recreate_prechecks(monkeypatch, container: _FakeContainer) -> None:
    """Neutralize everything recreate_environment does before delete/create."""
    import oduflow.docker_ops.client as docker_client

    monkeypatch.setattr(docker_client, "get_client", lambda: _FakeClient(container))
    monkeypatch.setattr(
        env_ops,
        "_reconcile_environment_hostname_for_update",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(system_ops, "estimate_new_db_bytes", lambda *a, **k: 0)
    monkeypatch.setattr(system_ops, "check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(env_ops.secret_store, "resolve_env_secrets", lambda *a, **k: {})
    monkeypatch.setattr(env_ops, "_ensure_system_ready", lambda *a, **k: None)


def test_recreate_adopts_label_only_token(tmp_path, monkeypatch):
    """A token living only in the legacy container label (pre-registry backup
    restore) survives recreate instead of being rotated with the container."""
    s, team = _settings(tmp_path)
    container = _FakeContainer({env_tokens.MCP_TOKEN_LABEL: "label-tok"})
    _stub_recreate_prechecks(monkeypatch, container)

    deleted: dict[str, bool] = {}

    def fake_delete(settings, team, env_name, *, preserve_access=False):
        deleted["preserve_access"] = preserve_access
        return []

    monkeypatch.setattr(env_ops, "delete_environment", fake_delete)
    monkeypatch.setattr(
        env_ops, "create_environment", lambda *a, **k: {"env_name": "main"}
    )

    env_ops.recreate_environment(s, team, "main")

    assert deleted["preserve_access"] is True
    assert env_tokens.get(team, "main") == "label-tok"


def test_failed_recreate_revokes_preserved_credentials(tmp_path, monkeypatch):
    """When create fails after the preserved delete, the orphaned token and
    share secret are revoked so a future unrelated environment under this
    name cannot inherit them."""
    s, team = _settings(tmp_path)
    container = _FakeContainer({})
    _stub_recreate_prechecks(monkeypatch, container)

    token = env_tokens.create_or_get(team, "main")
    env_share.create_or_get(team, "main")

    monkeypatch.setattr(env_ops, "delete_environment", lambda *a, **k: [])

    def failing_create(*a, **k):
        raise RuntimeError("no space left")

    monkeypatch.setattr(env_ops, "create_environment", failing_create)

    with pytest.raises(RuntimeError, match="no space left"):
        env_ops.recreate_environment(s, team, "main")

    assert env_tokens.get(team, "main") is None
    assert env_share.get(team, "main") is None
    assert env_tokens.resolve_token(s, token) is None
