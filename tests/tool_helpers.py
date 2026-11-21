"""Shared helpers for calling MCP tools and CLI commands directly."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oduflow.server import mcp  # noqa: E402


def call_tool(name: str, /, **kwargs):
    """Look up a registered MCP tool by name and invoke it with *kwargs*."""
    import asyncio
    import inspect

    tool = mcp._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def list_tools() -> list[str]:
    """Return sorted list of all registered tool names."""
    return sorted(mcp._tool_manager._tools.keys())


def call_cli(command: str, settings=None, **kwargs) -> str:
    """Run a CLI command and return formatted output."""
    from oduflow.docker_ops import system_ops

    if settings is None:
        raise ValueError("settings must be provided")

    if command == "init":
        result = system_ops.init_system(settings)
        return f"System {result['status']}."

    if command == "destroy":
        result = system_ops.destroy_system(settings)
        return f"System {result['status']}.\nRemoved: {result['removed']}"

    raise ValueError(f"Unknown CLI command: {command}")
