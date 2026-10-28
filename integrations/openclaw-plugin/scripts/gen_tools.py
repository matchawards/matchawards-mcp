"""Generate the OpenClaw plugin's tools.json and contracts.tools from the MCP server's own tools/list.

The plugin authors no tool name, description or schema: they all come from here. Run it from the repo root after
a tool change in the server (`python integrations/openclaw-plugin/scripts/gen_tools.py`), then commit both files;
tests/test_openclaw_plugin.py fails while either one differs from what this would write.

The server is built with MATCHAWARDS_WIDGETS off, so show_results (the MCP Apps card tool, which OpenClaw does
not render) is never listed.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TOOLS_JSON = PLUGIN_DIR / "tools.json"
MANIFEST_JSON = PLUGIN_DIR / "openclaw.plugin.json"

FIELDS = ("name", "description", "inputSchema", "annotations")


async def tool_definitions() -> list[dict]:
    """[{name, description, inputSchema, annotations}] in the order the server lists them, widgets off."""
    from matchawards_mcp import server

    saved = server.WIDGETS
    server.WIDGETS = False
    try:
        srv = server.build_mcp()
    finally:
        server.WIDGETS = saved
    listed = await srv.list_tools()
    tools = []
    for tool in listed:
        wire = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
        tools.append({key: wire[key] for key in FIELDS if key in wire})
    return tools


def render_tools(tools: list[dict]) -> str:
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def render_manifest(manifest_text: str, tools: list[dict]) -> str:
    """The manifest with contracts.tools set to the sorted tool names; every other key kept as it is."""
    manifest = json.loads(manifest_text)
    manifest.setdefault("contracts", {})["tools"] = sorted(tool["name"] for tool in tools)
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    tools = anyio.run(tool_definitions)
    TOOLS_JSON.write_text(render_tools(tools), encoding="utf-8")
    MANIFEST_JSON.write_text(render_manifest(MANIFEST_JSON.read_text(encoding="utf-8"), tools), encoding="utf-8")
    print(f"wrote {len(tools)} tools to {TOOLS_JSON.name} and {MANIFEST_JSON.name}")


if __name__ == "__main__":
    main()
