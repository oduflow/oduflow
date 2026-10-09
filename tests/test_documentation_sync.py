"""Contracts that keep public interfaces represented in the documentation."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from scripts.build_llms_full import SOURCES

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# This opt-in feedback channel is intentionally absent from public docs. Keep
# the exceptions exact so every other MCP tool and setting remains covered by
# the central references.
HIDDEN_MCP_TOOLS = {"submit_agent_feedback"}
HIDDEN_SETTINGS = {"[server].agent_feedback"}


def _tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def _is_mcp_tool(function: ast.FunctionDef) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def test_every_mcp_tool_is_in_the_central_reference():
    tree = _tree("src/oduflow/server.py")
    tools = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and _is_mcp_tool(node)
    }
    reference = (DOCS / "mcp-tools.md").read_text(encoding="utf-8")

    missing = sorted(
        tool for tool in tools - HIDDEN_MCP_TOOLS if f"`{tool}`" not in reference
    )
    assert not missing, f"MCP tools missing from docs/mcp-tools.md: {missing}"


def test_every_cli_subcommand_is_in_the_cli_reference():
    tree = _tree("src/oduflow/server.py")
    commands = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_parser" or not node.args:
            continue
        name = node.args[0]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            commands.add(name.value)

    reference = (DOCS / "cli.md").read_text(encoding="utf-8")
    missing = sorted(
        command for command in commands if f"oduflow {command}" not in reference
    )
    assert not missing, f"CLI commands missing from docs/cli.md: {missing}"


def test_every_dashboard_api_route_is_in_the_web_reference():
    tree = _tree("src/oduflow/web_ui.py")
    routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"Route", "WebSocketRoute"} or not node.args:
            continue
        path = node.args[0]
        if not isinstance(path, ast.Constant) or not isinstance(path.value, str):
            continue
        if path.value.startswith("/api/") or path.value == "/healthz":
            routes.add(path.value.replace("{branch:path}", "{branch}"))

    reference = (DOCS / "web-api.md").read_text(encoding="utf-8")
    missing = sorted(path for path in routes if path not in reference)
    assert not missing, f"Dashboard API routes missing from docs/web-api.md: {missing}"


def _get_keys(function: ast.FunctionDef, receivers: set[str]) -> set[str]:
    keys = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            node.func.attr == "get"
            and isinstance(owner, ast.Name)
            and owner.id in receivers
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def test_every_toml_setting_is_in_the_installation_reference():
    tree = _tree("src/oduflow/settings.py")
    settings_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    from_toml = next(
        node
        for node in settings_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "from_raw"
    )
    parse_backup = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_backup_section"
    )
    reference = (DOCS / "installation.md").read_text(encoding="utf-8")

    sections = {
        "admin": {"admin"},
        "server": {"server"},
        "routing": {"routing"},
        "database": {"database"},
        "storage": {"storage"},
        "lifecycle": {"lifecycle"},
        "agent": {"agent"},
        "oauth": {"oauth"},
        "production": {"production"},
        "backup": {"backup_raw"},
    }
    missing = []
    for section, receivers in sections.items():
        function = parse_backup if section == "backup" else from_toml
        for key in _get_keys(function, receivers):
            qualified = f"[{section}].{key}"
            if qualified not in HIDDEN_SETTINGS and f"`{qualified}`" not in reference:
                missing.append(qualified)

    team_keys = _get_keys(from_toml, {"team_cfg"})
    for key in team_keys:
        if f"`{key}`" not in reference and f"`[team.X.{key}]`" not in reference:
            missing.append(f"[team.*].{key}")

    assert not sorted(missing), (
        f"TOML settings missing from docs/installation.md: {sorted(missing)}"
    )


def test_retired_init_command_is_not_shown_as_runnable():
    command = re.compile(r"^(?:uvx\s+)?oduflow\s+init(?!-)(?:\s|$)")
    checked = [path for path in DOCS.glob("*.md") if path.name != "changelog.md"]
    checked.append(DOCS / "llms.txt")
    offenders = []
    for path in checked:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if command.match(line.strip().lstrip("$ ")):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"Retired `oduflow init` command shown at: {offenders}"


def test_direct_tool_examples_do_not_use_unsupported_named_flags():
    # `oduflow call` accepts positional values or one JSON object. It is not an
    # argparse layer and therefore does not understand `--parameter` options.
    command = re.compile(r"^oduflow\s+call\s+.*\s--[a-zA-Z0-9_-]+(?:\s|$)")
    offenders = []
    for path in DOCS.glob("*.md"):
        if path.name == "changelog.md":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if command.match(line.strip().lstrip("$ ")):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "`oduflow call` examples use unsupported flags:\n" + "\n".join(offenders)
    )


def test_llms_full_is_generated_from_current_docs():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_llms_full.py"), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout or result.stderr


def test_llms_full_source_list_covers_the_documentation_navigation():
    mkdocs = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    nav_pages = set(re.findall(r"\b([a-z0-9][a-z0-9-]*\.md)\s*$", mkdocs, re.MULTILINE))
    nav_pages.discard("changelog.md")  # release history, not the current manual
    source_pages = set(SOURCES) - {"llms.txt"}
    assert source_pages == nav_pages, (
        f"llms-full sources differ from MkDocs navigation: "
        f"missing={sorted(nav_pages - source_pages)}, "
        f"extra={sorted(source_pages - nav_pages)}"
    )
