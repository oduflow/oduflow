"""Behavioral coverage for Agent Chat tool-result rendering (images vs text)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHAT_JS = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "static" / "chat.js"
)

_PNG = "iVBORw0KGgo="

# A result big enough that walking and re-parsing it per update would be felt.
_BULK_ROWS = json.dumps([{"id": index, "name": "row"} for index in range(2000)])


def _instrumented_chat_js() -> str:
    source = _CHAT_JS.read_text(encoding="utf-8")
    marker = "})();"
    body, separator, trailer = source.rpartition(marker)
    assert separator, "chat.js IIFE terminator not found"
    return (
        body + "\nwindow.__chatToolTest = {\n"
        "  toolOutputParts: toolOutputParts,\n"
        "  renderToolPayload: renderToolPayload\n"
        "};\n" + separator + trailer
    )


def _tool_scenarios() -> dict[str, object]:
    harness = (
        "var parseCalls = 0;\n"
        "var realParse = JSON.parse;\n"
        "JSON.parse = function (text) { parseCalls += 1; return realParse(text); };\n"
        "function node(tag) {\n"
        "  var n = {\n"
        "    tag: tag, className: '', src: null, alt: null, children: [], _text: '',\n"
        "    appendChild: function (child) { this.children.push(child); return child; },\n"
        "    addEventListener: function () {},\n"
        "    replaceWith: function () {},\n"
        "    setAttribute: function () {}\n"
        "  };\n"
        "  Object.defineProperty(n, 'textContent', {\n"
        "    get: function () { return this._text; },\n"
        "    set: function (value) { this._text = value; this.children.length = 0; }\n"
        "  });\n"
        "  return n;\n"
        "}\n"
        "global.window = {};\n"
        "global.document = { createElement: node };\n"
        + _instrumented_chat_js()
        + "\nvar api = window.__chatToolTest;\n"
        "function shape(container) {\n"
        "  return container.children.map(function (child) {\n"
        "    return { tag: child.tag, cls: child.className, src: child.src,"
        " text: child.textContent };\n"
        "  });\n"
        "}\n"
        "function render(content) {\n"
        "  var tool = {\n"
        "    input: node('pre'), output: node('div'),\n"
        "    hasRawInput: false, rawInput: undefined,\n"
        "    hasRawOutput: false, rawOutput: undefined,\n"
        "    hasContent: true, content: content,\n"
        "    hasRenderedOutput: false, renderedOutput: undefined\n"
        "  };\n"
        "  api.renderToolPayload(tool);\n"
        "  return tool;\n"
        "}\n"
        "var imageBlock = { type: 'image', mimeType: 'image/png', data: '"
        + _PNG
        + "' };\n"
        "var inline = render([\n"
        "  { type: 'content', content: { type: 'text', text: 'Screenshot:' } },\n"
        "  { type: 'content', content: imageBlock }\n"
        "]);\n"
        "var serialized = render([\n"
        "  { type: 'content', content: { type: 'text',\n"
        "    text: JSON.stringify([{ type: 'text', text: 'shot' }, imageBlock]) } }\n"
        "]);\n"
        "var bulkRows = " + json.dumps(_BULK_ROWS) + ";\n"
        "parseCalls = 0;\n"
        "var bulk = render([\n"
        "  { type: 'content', content: { type: 'text', text: bulkRows } }\n"
        "]);\n"
        "var bulkParseCalls = parseCalls;\n"
        "var firstChild = bulk.output.children[0];\n"
        "api.renderToolPayload(bulk);\n"
        "var bulkRerenderKeptNode = bulk.output.children[0] === firstChild"
        " && bulk.output.children.length === 1;\n"
        "bulk.content = [{ type: 'content', content: { type: 'text', text: 'done' } }];\n"
        "api.renderToolPayload(bulk);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  inline: shape(inline.output),\n"
        "  serialized: shape(serialized.output),\n"
        "  bulk: shape(bulk.output),\n"
        "  bulkParseCalls: bulkParseCalls,\n"
        "  bulkRerenderKeptNode: bulkRerenderKeptNode,\n"
        "  missingOutput: shape(render(undefined).output),\n"
        "  svgRejected: shape(render([{ type: 'content', content: {\n"
        "    type: 'image', mimeType: 'image/svg+xml', data: 'PHN2Zz48L3N2Zz4='\n"
        "  } }]).output)\n"
        "}));\n"
    )
    result = subprocess.run(
        ["node", "-"], input=harness, text=True, capture_output=True, check=True
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def scenarios() -> dict[str, object]:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed")
    return _tool_scenarios()


def test_image_blocks_render_as_pictures_next_to_their_text(scenarios):
    inline = scenarios["inline"]

    assert [part["tag"] for part in inline] == ["pre", "img"]
    assert inline[0]["text"] == "Screenshot:"
    assert inline[1]["src"] == f"data:image/png;base64,{_PNG}"
    assert inline[1]["cls"] == "chat-tool-image"


def test_image_block_serialized_into_a_text_block_still_renders(scenarios):
    serialized = scenarios["serialized"]

    assert [part["tag"] for part in serialized] == ["pre", "img"]
    assert serialized[1]["src"] == f"data:image/png;base64,{_PNG}"


def test_svg_data_is_never_turned_into_an_image_element(scenarios):
    # SVG can carry script, so it stays inert text instead of becoming an <img>.
    svg = scenarios["svgRejected"]

    assert [part["tag"] for part in svg] == ["pre"]
    assert "svg+xml" in svg[0]["text"]


def test_imageless_output_is_shown_verbatim_without_being_reparsed(scenarios):
    # A large imageless result must keep its exact text, and the image walk must
    # not pay to parse it: the parse would only be thrown away.
    bulk = scenarios["bulk"]

    assert scenarios["bulkParseCalls"] == 0
    assert [part["tag"] for part in bulk] == ["pre"]
    assert bulk[0]["text"] == "done"


def test_unchanged_output_is_not_rendered_again(scenarios):
    # Status-only updates re-run renderToolPayload; re-rendering a big result
    # every time is wasted work, so the previous nodes must be kept.
    assert scenarios["bulkRerenderKeptNode"] is True


def test_absent_output_reports_that_the_agent_provided_none(scenarios):
    missing = scenarios["missingOutput"]

    assert [part["text"] for part in missing] == ["Not provided by agent"]
