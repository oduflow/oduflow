"""Unit tests for the per-environment MCP token registry and resolution."""

from __future__ import annotations

import json
import os
import stat

import pytest

from oduflow import env_tokens
from oduflow.settings import Settings, TeamSettings


def _settings(tmp_path) -> Settings:
    return Settings(
        teams={
            "1": TeamSettings(
                team_id="1",
                auth_token="team-tok-1",
                data_dir=str(tmp_path / "team_1"),
                port_range_start=50000,
                port_range_end=50100,
            ),
            "2": TeamSettings(
                team_id="2",
                auth_token="team-tok-2",
                data_dir=str(tmp_path / "team_2"),
                port_range_start=50100,
                port_range_end=50200,
            ),
        }
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    env_tokens.invalidate_cache()
    yield
    env_tokens.invalidate_cache()


def test_generate_token_unique():
    a = env_tokens.generate_token()
    b = env_tokens.generate_token()
    assert a != b
    assert len(a) >= 20


def test_resolve_team_token(tmp_path):
    s = _settings(tmp_path)
    assert env_tokens.resolve_token(s, "team-tok-1") == ("1", None)
    assert env_tokens.resolve_token(s, "team-tok-2") == ("2", None)


def test_create_or_get_mints_once(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["2"]
    token = env_tokens.create_or_get(team, "feature/x")
    assert env_tokens.create_or_get(team, "feature/x") == token
    assert env_tokens.get(team, "feature/x") == token


def test_registry_file_is_owner_only(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["1"]
    env_tokens.create_or_get(team, "main")
    mode = stat.S_IMODE(os.stat(env_tokens.tokens_path(team)).st_mode)
    assert mode == 0o600


def test_resolve_env_token(tmp_path):
    s = _settings(tmp_path)
    token = env_tokens.create_or_get(s.teams["2"], "feature/x")
    assert env_tokens.resolve_token(s, token) == ("2", "feature/x")


def test_resolve_unknown(tmp_path):
    s = _settings(tmp_path)
    assert env_tokens.resolve_token(s, "nope") is None
    assert env_tokens.resolve_token(s, "") is None


def test_revoke_stops_resolution(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["1"]
    token = env_tokens.create_or_get(team, "main")
    assert env_tokens.resolve_token(s, token) == ("1", "main")
    assert env_tokens.revoke(team, "main") is True
    assert env_tokens.resolve_token(s, token) is None
    assert env_tokens.get(team, "main") is None
    assert env_tokens.revoke(team, "main") is False


def test_rename_carries_token(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["1"]
    token = env_tokens.create_or_get(team, "old-name")
    env_tokens.rename(team, "old-name", "new-name")
    assert env_tokens.get(team, "old-name") is None
    assert env_tokens.get(team, "new-name") == token
    assert env_tokens.resolve_token(s, token) == ("1", "new-name")


def test_recreate_keeps_token(tmp_path):
    """create_or_get after a preserved delete returns the original token."""
    s = _settings(tmp_path)
    team = s.teams["1"]
    token = env_tokens.create_or_get(team, "main")
    # delete_environment(preserve_access=True) does not touch the registry,
    # so the next create finds and returns the same credential.
    assert env_tokens.create_or_get(team, "main") == token
    assert env_tokens.resolve_token(s, token) == ("1", "main")


def test_adopt_keeps_existing_record(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["1"]
    env_tokens.adopt(team, "main", "label-token")
    assert env_tokens.get(team, "main") == "label-token"
    env_tokens.adopt(team, "main", "other-token")
    assert env_tokens.get(team, "main") == "label-token"


def test_cache_picks_up_external_file_change(tmp_path):
    """A registry written by a sibling process resolves without invalidation."""
    s = _settings(tmp_path)
    team = s.teams["1"]
    first = env_tokens.create_or_get(team, "main")
    assert env_tokens.resolve_token(s, first) == ("1", "main")
    # Simulate another process replacing the file (new inode via _save).
    env_tokens._save(
        env_tokens.tokens_path(team),
        {"other": {"token": "ext-token", "created_at": "2026-01-01T00:00:00+00:00"}},
    )
    assert env_tokens.resolve_token(s, "ext-token") == ("1", "other")
    assert env_tokens.resolve_token(s, first) is None


def test_team_token_wins_over_registry(tmp_path):
    s = _settings(tmp_path)
    env_tokens.adopt(s.teams["2"], "evil-env", "team-tok-1")
    assert env_tokens.resolve_token(s, "team-tok-1") == ("1", None)


def test_malformed_registry_fails_closed(tmp_path):
    s = _settings(tmp_path)
    team = s.teams["1"]
    os.makedirs(team.data_dir, exist_ok=True)
    with open(env_tokens.tokens_path(team), "w") as f:
        json.dump(None, f)
    assert env_tokens.get(team, "main") is None
    assert env_tokens.resolve_token(s, "anything") is None


def test_no_data_dir_is_tokenless(tmp_path):
    team = TeamSettings(team_id="9")
    assert env_tokens.get(team, "main") is None
    with pytest.raises(ValueError):
        env_tokens.create_or_get(team, "main")
