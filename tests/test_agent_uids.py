"""Per-environment uid registry for the team's coding-agent container."""

from __future__ import annotations

import json
import os

import pytest

from oduflow import agent_uids
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _registry(team: TeamSettings) -> dict:
    with open(os.path.join(team.data_dir, "agent_uids.json")) as f:
        return json.load(f)


def test_missing_registry_is_empty(team):
    assert agent_uids.get_uid(team, "main") is None
    assert agent_uids.pending_removals(team) == {}


def test_allocation_is_stable_and_distinct(team):
    main = agent_uids.allocate_uid(team, "main")
    feature = agent_uids.allocate_uid(team, "feature/x")

    assert main == agent_uids.UID_BASE
    assert feature == main + 1
    assert agent_uids.allocate_uid(team, "main") == main
    assert agent_uids.get_uid(team, "feature/x") == feature


def test_retired_uid_is_never_reused(team):
    first = agent_uids.allocate_uid(team, "main")
    assert agent_uids.retire_uid(team, "main") == first

    assert agent_uids.get_uid(team, "main") is None
    # Files the deleted environment left behind must not become readable by
    # a later environment, even one recreated under the same name.
    assert agent_uids.allocate_uid(team, "main") == first + 1
    assert agent_uids.allocate_uid(team, "other") == first + 2


def test_retired_uid_stays_pending_until_its_files_are_removed(team):
    uid = agent_uids.allocate_uid(team, "feature/x")
    agent_uids.retire_uid(team, "feature/x")

    # The slug is what the agent container's cleanup needs.
    assert agent_uids.pending_removals(team) == {uid: "feature-x"}
    assert _registry(team)["pending_removal"] == {str(uid): "feature-x"}

    agent_uids.forget_removed(team, uid)

    assert agent_uids.pending_removals(team) == {}
    assert "pending_removal" not in _registry(team)
    # Still never handed out again once the cleanup is done.
    assert agent_uids.allocate_uid(team, "next") == uid + 1


def test_pending_uid_counts_for_the_counter(team):
    path = os.path.join(team.data_dir, "agent_uids.json")
    with open(path, "w") as f:
        json.dump(
            {"next_uid": 20000, "envs": {}, "pending_removal": {"20007": "old"}}, f
        )

    assert agent_uids.allocate_uid(team, "main") == 20008


def test_rename_keeps_the_uid(team):
    uid = agent_uids.allocate_uid(team, "old")
    stale = agent_uids.allocate_uid(team, "new")

    agent_uids.rename_env(team, "old", "new")

    assert agent_uids.get_uid(team, "new") == uid
    assert agent_uids.get_uid(team, "old") is None
    # The replaced stale uid is retired, not handed out again.
    assert agent_uids.allocate_uid(team, "next") == stale + 1


def test_rename_retire_and_forget_of_unknown_env_are_noops(team):
    agent_uids.rename_env(team, "ghost", "new")
    assert agent_uids.retire_uid(team, "ghost") is None
    agent_uids.forget_removed(team, 20000)

    assert not os.path.exists(os.path.join(team.data_dir, "agent_uids.json"))


def test_registry_is_private(team):
    agent_uids.allocate_uid(team, "main")

    path = os.path.join(team.data_dir, "agent_uids.json")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert _registry(team) == {
        "next_uid": agent_uids.UID_BASE + 1,
        "envs": {"main": agent_uids.UID_BASE},
    }


def test_counter_never_goes_below_assigned_uids(team):
    path = os.path.join(team.data_dir, "agent_uids.json")
    with open(path, "w") as f:
        json.dump({"next_uid": 10, "envs": {"main": 20005}}, f)

    assert agent_uids.allocate_uid(team, "other") == 20006


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "null",
        '{"envs": {}}',
        '{"next_uid": 20000, "envs": {"a": "x"}}',
        '{"next_uid": 20000, "envs": {}, "pending_removal": []}',
    ],
)
def test_unreadable_registry_is_an_error_not_a_reset(team, content):
    path = os.path.join(team.data_dir, "agent_uids.json")
    with open(path, "w") as f:
        f.write(content)

    # Starting over would hand an existing environment's uid (its checkout,
    # HOME and login) to a different environment.
    with pytest.raises(agent_uids.AgentUidRegistryError):
        agent_uids.allocate_uid(team, "main")
    with open(path) as f:
        assert f.read() == content
