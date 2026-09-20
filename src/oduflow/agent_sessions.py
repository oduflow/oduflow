"""Persistent ACP chat session history, per environment and agent.

The ACP chat (dashboard modal, see ``web_ui.ws_agent_acp``) resumes the current
conversation via ACP ``session/load`` and lets the user return to recent
conversations. The underlying agent process is spawned per WebSocket
connection (``docker exec`` of the ACP adapter) and the transcript itself
lives on the agent home volume; here we only persist the session ids and their
display metadata needed to reattach.

A single JSON bag per team at ``<team.data_dir>/agent_sessions.json`` maps
``{branch: {agent_type: {current, history}}}``. Legacy string values are
normalized on read and written in the new form on the next mutation. The
server runs as a single process, so a module-level lock guards the
read-modify-write of the whole bag against concurrent chats. See
specs/0029-agent-console-and-chat.md.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from oduflow.fsutil import atomic_write_private_json
from oduflow.settings import TeamSettings

# The server is single-process; this guards the whole-file read-modify-write so
# concurrent chats (different branches) don't clobber each other's session ids.
_LOCK = threading.Lock()
_HISTORY_LIMIT = 20
_TITLE_MAX = 80

SessionEntry = dict[str, str | None]
SessionState = dict[str, str | list[SessionEntry] | None]
SessionData = dict[str, dict[str, SessionState]]


def _path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "agent_sessions.json")


def _normalize(value: Any) -> SessionState | None:
    """Coerce a legacy or current stored value to the current schema."""
    if isinstance(value, str):
        if not value:
            return None
        return {
            "current": value,
            "history": [
                {
                    "session_id": value,
                    "title": None,
                    "created_at": None,
                    "last_used_at": None,
                }
            ],
        }
    if not isinstance(value, dict):
        return None

    current_value = value.get("current")
    current = str(current_value) if current_value else None
    history: list[SessionEntry] = []
    seen: set[str] = set()
    raw_history = value.get("history")
    if isinstance(raw_history, list):
        for raw_entry in raw_history:
            if not isinstance(raw_entry, dict) or not raw_entry.get("session_id"):
                continue
            session_id = str(raw_entry["session_id"])
            if session_id in seen:
                continue
            seen.add(session_id)
            raw_title = raw_entry.get("title")
            title = str(raw_title)[:_TITLE_MAX] if raw_title else None
            history.append(
                {
                    "session_id": session_id,
                    "title": title,
                    "created_at": (
                        str(raw_entry["created_at"])
                        if raw_entry.get("created_at")
                        else None
                    ),
                    "last_used_at": (
                        str(raw_entry["last_used_at"])
                        if raw_entry.get("last_used_at")
                        else None
                    ),
                }
            )

    if current:
        current_entry = next(
            (entry for entry in history if entry["session_id"] == current), None
        )
        if current_entry is None:
            current_entry = {
                "session_id": current,
                "title": None,
                "created_at": None,
                "last_used_at": None,
            }
        history = [
            current_entry,
            *(entry for entry in history if entry["session_id"] != current),
        ]

    return {"current": current, "history": history[:_HISTORY_LIMIT]}


def _load(team: TeamSettings) -> SessionData:
    path = _path(team)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: SessionData = {}
    for branch, per_agent in data.items():
        if isinstance(per_agent, dict):
            normalized = {
                str(agent_type): state
                for agent_type, value in per_agent.items()
                if (state := _normalize(value)) is not None
            }
            if normalized:
                out[str(branch)] = normalized
    return out


def _save(team: TeamSettings, data: SessionData) -> None:
    os.makedirs(team.data_dir, exist_ok=True)
    atomic_write_private_json(_path(team), data)


def get_session(team: TeamSettings, branch: str, agent_type: str) -> str | None:
    """Return the stored session id for (branch, agent_type), or None."""
    with _LOCK:
        state = _load(team).get(branch, {}).get(agent_type)
        if not state:
            return None
        current = state.get("current")
        return str(current) if current else None


def get_history(team: TeamSettings, branch: str, agent_type: str) -> list[SessionEntry]:
    """Return recent sessions for (branch, agent_type), most recent first."""
    with _LOCK:
        state = _load(team).get(branch, {}).get(agent_type)
        if not state:
            return []
        history = state.get("history")
        if not isinstance(history, list):
            return []
        return [entry.copy() for entry in history]


def set_session(
    team: TeamSettings,
    branch: str,
    agent_type: str,
    session_id: str,
    title: str | None = None,
) -> None:
    """Make a session current, updating its metadata and MRU position."""
    with _LOCK:
        data = _load(team)
        per_agent = data.setdefault(branch, {})
        state = per_agent.get(agent_type) or {"current": None, "history": []}
        history_value = state.get("history")
        history = history_value if isinstance(history_value, list) else []
        existing = next(
            (entry for entry in history if entry["session_id"] == session_id), None
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if existing is None:
            existing = {
                "session_id": session_id,
                "title": None,
                "created_at": now,
                "last_used_at": now,
            }
        else:
            existing["last_used_at"] = now
        if title and not existing.get("title"):
            existing["title"] = title[:_TITLE_MAX]
        state["current"] = session_id
        state["history"] = [
            existing,
            *(entry for entry in history if entry["session_id"] != session_id),
        ][:_HISTORY_LIMIT]
        per_agent[agent_type] = state
        _save(team, data)


def clear_current(team: TeamSettings, branch: str, agent_type: str) -> None:
    """Clear the current session while preserving recent session history."""
    with _LOCK:
        data = _load(team)
        state = data.get(branch, {}).get(agent_type)
        if not state:
            return
        state["current"] = None
        _save(team, data)


def rename(team: TeamSettings, old_name: str, new_name: str) -> None:
    """Move an environment's stored sessions to its new name.

    A renamed environment is the same environment, so the chat conversation
    follows it instead of starting over. Anything already stored under the new
    name (a previous environment of that name) is replaced.
    """
    with _LOCK:
        data = _load(team)
        state = data.pop(old_name, None)
        if state is None:
            return
        data[new_name] = state
        _save(team, data)


def clear_session(
    team: TeamSettings, branch: str, agent_type: str | None = None
) -> None:
    """Forget the stored session(s). Clears one agent, or the whole branch when
    ``agent_type`` is None (used when an environment is deleted)."""
    with _LOCK:
        data = _load(team)
        if branch not in data:
            return
        if agent_type is None:
            data.pop(branch, None)
        else:
            data[branch].pop(agent_type, None)
            if not data[branch]:
                data.pop(branch, None)
        _save(team, data)
