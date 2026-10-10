import json
import os
import stat

from oduflow import agent_sessions
from oduflow.settings import OpenCodeProviderSettings, Settings, TeamSettings
from oduflow.web_ui import (
    _acp_adapter_cmd,
    _codex_cli_cmd,
    _confirm_before_start,
    _opencode_cli_cmd,
    _opencode_config,
    _opencode_provider_env_warning,
    _wire_client_acp_mcp,
)


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def test_get_none_when_unset(tmp_path):
    assert agent_sessions.get_session(_team(tmp_path), "feature/x", "claude") is None


def test_set_get_roundtrip_per_branch_and_agent(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "feature/x", "claude", "sid-1")
    agent_sessions.set_session(t, "feature/x", "codex", "sid-2")
    agent_sessions.set_session(t, "feature/x", "opencode", "sid-4")
    agent_sessions.set_session(t, "main", "claude", "sid-3")
    assert agent_sessions.get_session(t, "feature/x", "claude") == "sid-1"
    assert agent_sessions.get_session(t, "feature/x", "codex") == "sid-2"
    assert agent_sessions.get_session(t, "feature/x", "opencode") == "sid-4"
    assert agent_sessions.get_session(t, "main", "claude") == "sid-3"


def test_set_overwrites(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "old")
    agent_sessions.set_session(t, "b", "claude", "new")
    assert agent_sessions.get_session(t, "b", "claude") == "new"


def test_legacy_string_is_normalized_and_preserved_on_write(tmp_path):
    t = _team(tmp_path)
    with open(agent_sessions._path(t), "w", encoding="utf-8") as f:
        json.dump({"b": {"claude": "sid-1"}}, f)

    assert agent_sessions.get_session(t, "b", "claude") == "sid-1"
    assert agent_sessions.get_history(t, "b", "claude") == [
        {
            "session_id": "sid-1",
            "title": None,
            "created_at": None,
            "last_used_at": None,
        }
    ]

    agent_sessions.set_session(t, "b", "claude", "sid-2")
    assert agent_sessions.get_session(t, "b", "claude") == "sid-2"
    assert [
        entry["session_id"] for entry in agent_sessions.get_history(t, "b", "claude")
    ] == [
        "sid-2",
        "sid-1",
    ]


def test_existing_session_moves_to_front_without_duplicates(tmp_path):
    t = _team(tmp_path)
    old = "2026-01-01T00:00:00+00:00"
    stored = {
        "current": "sid-2",
        "history": [
            {
                "session_id": "sid-2",
                "title": "Second",
                "created_at": old,
                "last_used_at": old,
            },
            {
                "session_id": "sid-1",
                "title": "First",
                "created_at": old,
                "last_used_at": old,
            },
        ],
    }
    with open(agent_sessions._path(t), "w", encoding="utf-8") as f:
        json.dump({"b": {"claude": stored}}, f)

    agent_sessions.set_session(t, "b", "claude", "sid-1")

    history = agent_sessions.get_history(t, "b", "claude")
    assert [entry["session_id"] for entry in history] == ["sid-1", "sid-2"]
    assert history[0]["created_at"] == old
    assert history[0]["last_used_at"] != old


def test_title_is_first_write_wins_and_truncated(tmp_path):
    t = _team(tmp_path)
    title = "x" * 100
    agent_sessions.set_session(t, "b", "claude", "sid-1")
    agent_sessions.set_session(t, "b", "claude", "sid-1", title=title)
    agent_sessions.set_session(t, "b", "claude", "sid-1", title="replacement")

    history = agent_sessions.get_history(t, "b", "claude")
    assert history[0]["title"] == "x" * 80


def test_history_is_capped_with_current_at_front(tmp_path):
    t = _team(tmp_path)
    for index in range(21):
        agent_sessions.set_session(t, "b", "claude", f"sid-{index}")

    history = agent_sessions.get_history(t, "b", "claude")
    assert len(history) == 20
    assert history[0]["session_id"] == "sid-20"
    assert agent_sessions.get_session(t, "b", "claude") == "sid-20"
    assert "sid-0" not in {entry["session_id"] for entry in history}


def test_clear_current_preserves_history_and_clear_session_removes_it(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "sid-1")
    agent_sessions.clear_current(t, "b", "claude")

    assert agent_sessions.get_session(t, "b", "claude") is None
    assert [
        entry["session_id"] for entry in agent_sessions.get_history(t, "b", "claude")
    ] == ["sid-1"]

    agent_sessions.clear_session(t, "b", "claude")
    assert agent_sessions.get_history(t, "b", "claude") == []


def test_clear_one_agent_keeps_the_other(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    agent_sessions.set_session(t, "b", "codex", "c2")
    agent_sessions.clear_session(t, "b", "claude")
    assert agent_sessions.get_session(t, "b", "claude") is None
    assert agent_sessions.get_session(t, "b", "codex") == "c2"


def test_clear_whole_branch(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    agent_sessions.set_session(t, "b", "codex", "c2")
    agent_sessions.clear_session(t, "b")
    assert agent_sessions.get_session(t, "b", "claude") is None
    assert agent_sessions.get_session(t, "b", "codex") is None


def test_clear_missing_is_noop(tmp_path):
    t = _team(tmp_path)
    agent_sessions.clear_session(t, "nope")
    agent_sessions.clear_session(t, "nope", "claude")


def test_per_team_isolation(tmp_path):
    t1 = TeamSettings(team_id="1", data_dir=str(tmp_path / "t1"))
    t2 = TeamSettings(team_id="2", data_dir=str(tmp_path / "t2"))
    agent_sessions.set_session(t1, "b", "claude", "sid-1")
    assert agent_sessions.get_session(t2, "b", "claude") is None


def test_file_permissions_are_restrictive(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    mode = stat.S_IMODE(os.stat(agent_sessions._path(t)).st_mode)
    assert mode == 0o600


def test_acp_adapter_cmd():
    assert _acp_adapter_cmd("claude") == ["claude-agent-acp"]
    assert _acp_adapter_cmd("codex") == ["codex-acp"]
    assert _acp_adapter_cmd("opencode") == ["opencode", "acp"]
    # Unknown agent falls back to Claude (the configured default agent).
    assert _acp_adapter_cmd("something-else") == ["claude-agent-acp"]


def test_codex_cli_cmd_uses_docker_as_the_sandbox():
    assert _codex_cli_cmd("http://scoped/mcp/env", "gpt-test") == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'mcp_servers.oduflow.url="http://scoped/mcp/env"',
        "-c",
        'mcp_servers.oduflow.bearer_token_env_var="ODUFLOW_MCP_TOKEN"',
        "--model",
        "gpt-test",
    ]


def test_opencode_cli_and_session_config():
    assert _opencode_cli_cmd("anthropic/test-model") == [
        "opencode",
        "--auto",
        "--model",
        "anthropic/test-model",
    ]
    config = json.loads(
        _opencode_config(
            "http://scoped/mcp/env",
            include_oduflow=True,
            model="anthropic/test-model",
        )
    )
    assert config["autoupdate"] is False
    assert config["permission"] == "allow"
    assert config["model"] == "anthropic/test-model"
    assert config["mcp"]["agent_browser"]["command"] == [
        "agent-browser",
        "mcp",
        "--tools",
        "all",
    ]
    assert config["mcp"]["oduflow"] == {
        "type": "remote",
        "url": "http://scoped/mcp/env",
        "enabled": True,
        "oauth": False,
        "headers": {
            "Authorization": "Bearer {env:ODUFLOW_MCP_TOKEN}",
        },
    }
    assert "scoped-token" not in json.dumps(config)


def test_opencode_acp_config_omits_mcp_servers():
    config = json.loads(
        _opencode_config(
            include_browser=False,
            model="openai/test-model",
        )
    )
    assert config == {
        "autoupdate": False,
        "permission": "allow",
        "model": "openai/test-model",
    }


def _provider() -> OpenCodeProviderSettings:
    return OpenCodeProviderSettings(
        id="litellm",
        name="LiteLLM",
        models=("glm-5.3", "openai/gpt-4o"),
    )


def test_opencode_config_declares_chat_completions_provider_by_reference():
    config = json.loads(
        _opencode_config(
            include_browser=False,
            model="litellm/glm-5.3",
            provider=_provider(),
        )
    )
    assert config["model"] == "litellm/glm-5.3"
    assert config["provider"] == {
        "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiteLLM",
            # Pinned so an id from OpenCode's catalog gains no built-in
            # models or variables.
            "env": [],
            "whitelist": ["glm-5.3", "openai/gpt-4o"],
            "options": {
                "baseURL": "{env:OPENCODE_PROVIDER_BASE_URL}",
                "apiKey": "{env:OPENCODE_PROVIDER_API_KEY}",
            },
            "models": {
                "glm-5.3": {"name": "glm-5.3"},
                "openai/gpt-4o": {"name": "openai/gpt-4o"},
            },
        }
    }


def test_opencode_config_without_provider_is_unchanged():
    assert _opencode_config(
        "http://scoped/mcp/env", include_oduflow=True, model="anthropic/x"
    ) == _opencode_config(
        "http://scoped/mcp/env",
        include_oduflow=True,
        model="anthropic/x",
        provider=None,
    )
    assert "provider" not in json.loads(_opencode_config(include_browser=False))


def test_opencode_provider_env_warning(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        agent_env={
            "OPENCODE_PROVIDER_BASE_URL": "http://gw/v1",
            "OPENCODE_PROVIDER_API_KEY": "sk-x",
        },
    )
    assert _opencode_provider_env_warning(Settings(teams={"1": team}), team) == ""
    configured = Settings(
        teams={"1": team},
        agent_opencode_model="litellm/glm-5.3",
        agent_opencode_provider=_provider(),
    )
    assert _opencode_provider_env_warning(configured, team) == ""

    bare = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        agent_env={"OPENCODE_PROVIDER_API_KEY": " "},
    )
    warning = _opencode_provider_env_warning(
        Settings(
            teams={"1": bare},
            agent_opencode_model="litellm/glm-5.3",
            agent_opencode_provider=_provider(),
        ),
        bare,
    )
    assert "litellm/glm-5.3" in warning
    assert "OPENCODE_PROVIDER_BASE_URL, OPENCODE_PROVIDER_API_KEY" in warning
    assert "[team.1.agent_env]" in warning
    assert "server environment" in warning


def test_opencode_provider_env_warning_only_when_the_model_selects_it(tmp_path):
    bare = TeamSettings(team_id="1", data_dir=str(tmp_path))
    for model in ("", "anthropic/claude-sonnet-4-6", "litellmx/glm-5.3"):
        settings = Settings(
            teams={"1": bare},
            agent_opencode_model=model,
            agent_opencode_provider=_provider(),
        )
        assert _opencode_provider_env_warning(settings, bare) == "", model


def test_confirm_before_start_passes_the_warning_as_an_argument():
    cmd = _confirm_before_start(["opencode", "--auto"], "it's $(id)")
    assert cmd[:2] == ["sh", "-c"]
    assert "$(id)" not in cmd[2]
    assert cmd[3:] == ["sh", "it's $(id)", "opencode", "--auto"]
    assert cmd[2].endswith('read -r _; shift; exec "$@"')


def test_wire_client_acp_mcp_adds_scoped_server_and_preserves_others():
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/new",
            "params": {
                "cwd": "/workspace/x",
                "mcpServers": [
                    {"name": "custom", "command": "custom-mcp", "args": [], "env": []},
                    {"name": "agent_browser", "command": "wrong-browser"},
                    {
                        "type": "http",
                        "name": "oduflow",
                        "url": "http://wrong",
                        "headers": [],
                    },
                ],
            },
        }
    )
    wired = json.loads(
        _wire_client_acp_mcp(
            frame, "http://scoped/mcp/env", "scoped-token", "environment-x"
        )
    )
    assert wired["params"]["mcpServers"] == [
        {"name": "custom", "command": "custom-mcp", "args": [], "env": []},
        {
            "name": "agent_browser",
            "command": "agent-browser",
            "args": ["mcp", "--tools", "all"],
            "env": [
                {"name": "AGENT_BROWSER_SESSION", "value": "environment-x"},
                {
                    "name": "AGENT_BROWSER_EXECUTABLE_PATH",
                    "value": "/usr/bin/chromium",
                },
            ],
        },
        {
            "type": "http",
            "name": "oduflow",
            "url": "http://scoped/mcp/env",
            "headers": [{"name": "Authorization", "value": "Bearer scoped-token"}],
        },
    ]


def test_wire_client_acp_mcp_handles_load_but_not_other_frames():
    load = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/load",
            "params": {"sessionId": "sid", "cwd": "/workspace", "mcpServers": []},
        }
    )
    assert (
        json.loads(_wire_client_acp_mcp(load, "http://scoped", "token", "env"))[
            "params"
        ]["mcpServers"][0]["name"]
        == "agent_browser"
    )
    load_servers = json.loads(
        _wire_client_acp_mcp(load, "http://scoped", "token", "env")
    )["params"]["mcpServers"]
    assert [server["name"] for server in load_servers] == [
        "agent_browser",
        "oduflow",
    ]

    prompt = '{"jsonrpc":"2.0","method":"session/prompt","params":{}}'
    assert _wire_client_acp_mcp(prompt, "http://scoped", "token", "env") == prompt
    no_token = json.loads(_wire_client_acp_mcp(load, "http://scoped", "", "env"))
    assert [server["name"] for server in no_token["params"]["mcpServers"]] == [
        "agent_browser"
    ]
    assert (
        _wire_client_acp_mcp("not-json", "http://scoped", "token", "env") == "not-json"
    )


def test_delete_environment_clears_chat_sessions(tmp_path, monkeypatch):
    """A deleted environment must not leave a stale session that a later env of
    the same name would resume."""
    import docker as _docker
    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_workspace_path

    data = tmp_path / "data"
    data.mkdir()
    team = TeamSettings(
        team_id="1",
        data_dir=str(data),
        port_registry_path=str(data / "ports.json"),
    )
    settings = Settings(base_data_dir=str(data), teams={"1": team})

    agent_sessions.set_session(team, "feature/x", "claude", "sid-old")
    agent_sessions.set_session(team, "feature/x", "codex", "sid-old-2")
    # A second env's sessions must survive the first env's deletion.
    agent_sessions.set_session(team, "other", "claude", "keep-me")

    # Make the env look present on disk with no live container.
    os.makedirs(get_workspace_path("feature/x", team.workspaces_dir), exist_ok=True)

    class _FakeContainers:
        def get(self, name):
            raise _docker.errors.NotFound(name)

    class _FakeClient:
        containers = _FakeContainers()

    monkeypatch.setattr(env_ops, "is_protected", lambda *a, **k: False)
    monkeypatch.setattr(env_ops, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(env_ops, "_agent_remove_env", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "release_port", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_exec_sql", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "load_credentials", lambda *a, **k: {"pg_user": "x"})
    monkeypatch.setattr(env_ops, "_drop_pg_role", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_unmount_filestore", lambda *a, **k: None)

    env_ops.delete_environment(settings, team, "feature/x")

    assert agent_sessions.get_session(team, "feature/x", "claude") is None
    assert agent_sessions.get_session(team, "feature/x", "codex") is None
    assert agent_sessions.get_session(team, "other", "claude") == "keep-me"


def _normalize_history(entries):
    state = agent_sessions._normalize({"current": None, "history": entries})
    return [e["session_id"] for e in (state or {}).get("history", [])]


def test_history_entries_must_be_dicts_with_a_session_id():
    # Both halves of the guard matter: a bare string entry has no .get, and a
    # dict without session_id would land in history as a KeyError waiting to
    # happen.
    assert _normalize_history(
        [
            {"session_id": "keep"},
            "a bare string",
            {"title": "no id"},
            {"session_id": ""},
            None,
            {"session_id": "keep2"},
        ]
    ) == ["keep", "keep2"]


def test_duplicate_session_ids_are_collapsed():
    assert _normalize_history(
        [{"session_id": "a"}, {"session_id": "a"}, {"session_id": "b"}]
    ) == ["a", "b"]


def test_a_non_list_history_yields_no_entries():
    assert _normalize_history("not a list") == []


def test_a_non_dict_non_str_value_normalizes_to_nothing():
    assert agent_sessions._normalize(42) is None
    assert agent_sessions._normalize(None) is None


def test_an_empty_legacy_string_normalizes_to_nothing():
    assert agent_sessions._normalize("") is None
