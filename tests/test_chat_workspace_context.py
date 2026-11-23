"""The dashboard chat hides the workspace's standing instructions.

The shared-environment workspace opens each conversation with a fenced block
of instructions for the agent (frontend/workspace/src/lib/workspaceContext.ts).
An operator can open the same conversation in the dashboard's Agent Chat, which
must show the user's own words only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHAT_JS = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "static" / "chat.js"
)
_CONTEXT_TS = (
    Path(__file__).parents[1]
    / "frontend"
    / "workspace"
    / "src"
    / "lib"
    / "workspaceContext.ts"
)


def _strip(texts: list[str]) -> list[str]:
    source = _CHAT_JS.read_text(encoding="utf-8")
    body, separator, trailer = source.rpartition("})();")
    assert separator, "chat.js IIFE terminator not found"
    harness = (
        "global.window = {};\n"
        + body
        + "\nwindow.__strip = stripWorkspaceContext;\n"
        + separator
        + trailer
        + "\nprocess.stdout.write(JSON.stringify("
        + json.dumps(texts)
        + ".map(window.__strip)));\n"
    )
    result = subprocess.run(
        ["node", "-"], input=harness, text=True, capture_output=True, check=True
    )
    return json.loads(result.stdout)


def test_both_chats_use_the_same_markers():
    workspace = _CONTEXT_TS.read_text(encoding="utf-8")
    chat = _CHAT_JS.read_text(encoding="utf-8")
    for marker in ("<oduflow-workspace-context>", "</oduflow-workspace-context>"):
        assert f"'{marker}'" in workspace
        assert f"'{marker}'" in chat


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_replayed_messages_drop_the_context_block():
    block = "<oduflow-workspace-context>\nrules\n</oduflow-workspace-context>\n\n"
    assert _strip(
        [
            block + "Add a field",
            "<oduflow-workspace-context>\nhalf of the ru",
            "  plain text stays  ",
        ]
    ) == ["Add a field", "", "  plain text stays  "]
