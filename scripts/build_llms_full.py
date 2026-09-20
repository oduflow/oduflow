"""Build docs/llms-full.txt from the maintained documentation pages."""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUTPUT = DOCS / "llms-full.txt"

# Changelog is intentionally excluded: llms-full is the current product manual,
# not release history. Keep this in mkdocs navigation order.
SOURCES = (
    "llms.txt",
    "index.md",
    "quick-start.md",
    "installation.md",
    "architecture.md",
    "use-cases.md",
    "templates.md",
    "environments.md",
    "production.md",
    "agent.md",
    "services.md",
    "extra-addons.md",
    "stacks.md",
    "web-api.md",
    "mcp-tools.md",
    "cli.md",
    "traefik.md",
    "multi-instance.md",
    "security.md",
    "docker.md",
    "internals.md",
    "troubleshooting.md",
    "licensing.md",
)


def _strip_front_matter(text: str) -> str:
    """Remove MkDocs YAML front matter while preserving the page body."""
    if not text.startswith("---\n"):
        return text
    marker = text.find("\n---\n", 4)
    return text[marker + 5 :] if marker >= 0 else text


def render() -> str:
    pages = []
    for filename in SOURCES:
        text = (DOCS / filename).read_text(encoding="utf-8")
        pages.append(_strip_front_matter(text).strip())
    return "\n\n---\n\n".join(pages) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if docs/llms-full.txt is not up to date",
    )
    args = parser.parse_args()
    expected = render()

    if args.check:
        actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if actual == expected:
            return 0
        diff = difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=str(OUTPUT),
            tofile="generated llms-full.txt",
            lineterm="",
        )
        print("\n".join(diff))
        return 1

    OUTPUT.write_text(expected, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)} from {len(SOURCES)} sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
