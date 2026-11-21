"""The committed build of the shared-environment workspace (frontend/workspace).

The bundle ships inside the package so running Oduflow never needs Node. CI
rebuilds it and fails on any diff (.github/workflows/frontend.yml); these
checks guard what the server relies on and what DESIGN.md requires of every
Oduflow page: one token system shared with the dashboard, every var(--*)
declared, and no asset loaded from anywhere but this server.
See specs/0087-chat-first-shared-workspace.md.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_TEMPLATES = _ROOT / "src" / "oduflow" / "templates"
_BUNDLE = _TEMPLATES / "static" / "workspace"
_TOKENS = _ROOT / "frontend" / "workspace" / "src" / "styles" / "tokens.css"


def _root_tokens(css: str, selector: str) -> dict[str, str]:
    start = css.index(selector + " {")
    block = css[start : css.index("}", start)]
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def _page() -> str:
    return (_BUNDLE / "index.html").read_text(encoding="utf-8")


def _referenced_assets() -> list[str]:
    return re.findall(r'(?:src|href)="/static/workspace/(assets/[^"]+)"', _page())


def test_page_template_carries_the_server_placeholders():
    page = _page()
    assert 'data-scoped-env="__SCOPED_ENV__"' in page
    assert 'data-viewer="__VIEWER__"' in page
    # White label (oduflow.branding): the product name is never hard-coded.
    assert "<title>__BRAND_NAME__</title>" in page
    assert 'data-brand-name="__BRAND_NAME__"' in page
    assert 'data-logo-class="__LOGO_CLASS__"' in page
    assert "Oduflow" not in page


def test_page_references_exactly_the_built_assets():
    referenced = sorted(_referenced_assets())
    built = sorted("assets/" + path.name for path in (_BUNDLE / "assets").iterdir())
    assert referenced, "index.html references no bundle assets"
    assert referenced == built, "stale or missing files in the committed build"
    assert any(name.endswith(".js") for name in referenced)
    assert any(name.endswith(".css") for name in referenced)


def test_nothing_is_loaded_from_another_origin():
    page = _page()
    external = re.findall(r'(?:src|href)="((?:https?:)?//[^"]+)"', page)
    assert external == []
    for path in (_BUNDLE / "assets").glob("*.css"):
        css = path.read_text(encoding="utf-8")
        assert re.search(r"url\(\s*['\"]?(?:https?:)?//", css) is None, path.name
        assert "@import" not in css, path.name


def test_tokens_match_the_dashboard():
    tokens = _TOKENS.read_text(encoding="utf-8")
    dashboard = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    for selector in (":root", ':root[data-theme="light"]'):
        ours = _root_tokens(tokens, selector)
        theirs = _root_tokens(dashboard, selector)
        assert ours == theirs, f"{selector} tokens drifted from the dashboard"


def test_every_css_variable_is_declared():
    declared = set(_root_tokens(_TOKENS.read_text(encoding="utf-8"), ":root"))
    for path in (_BUNDLE / "assets").glob("*.css"):
        used = set(re.findall(r"var\((--[a-z0-9-]+)", path.read_text(encoding="utf-8")))
        assert used <= declared, f"{path.name}: undeclared {sorted(used - declared)}"


def test_bundle_carries_the_shared_acp_client():
    # One copy of the wire client: the bundle embeds static/acp-client.js.
    script = "".join(
        path.read_text(encoding="utf-8") for path in (_BUNDLE / "assets").glob("*.js")
    )
    assert "session/request_permission" in script
    assert "_chat/notice" in script
