"""The OpenClaw plugin (integrations/openclaw-plugin) carries no tool definitions of its own: tools.json and the
manifest's contracts.tools are generated from this server's tools/list. These tests regenerate them in memory and
fail when the committed files drift; fix with `python integrations/openclaw-plugin/scripts/gen_tools.py`."""

import importlib.util
import json
from pathlib import Path

import pytest

from matchawards_mcp import server

PLUGIN = Path(__file__).resolve().parent.parent / "integrations" / "openclaw-plugin"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_tools", PLUGIN / "scripts" / "gen_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _gen()


@pytest.mark.anyio
async def test_tools_json_matches_the_server():
    tools = await gen.tool_definitions()
    assert (PLUGIN / "tools.json").read_text(encoding="utf-8") == gen.render_tools(tools), (
        "tools.json is stale: run python integrations/openclaw-plugin/scripts/gen_tools.py")


@pytest.mark.anyio
async def test_manifest_contracts_match_the_server():
    tools = await gen.tool_definitions()
    text = (PLUGIN / "openclaw.plugin.json").read_text(encoding="utf-8")
    assert json.loads(text)["contracts"]["tools"] == sorted(t["name"] for t in tools)
    assert text == gen.render_manifest(text, tools), (
        "openclaw.plugin.json is stale: run python integrations/openclaw-plugin/scripts/gen_tools.py")


@pytest.mark.anyio
async def test_generated_with_widgets_off_even_when_they_are_on(monkeypatch):
    monkeypatch.setattr(server, "WIDGETS", True)
    tools = await gen.tool_definitions()
    names = [t["name"] for t in tools]
    assert "show_results" not in names
    assert names == [t.name for t in await server.mcp.list_tools()]  # the server's own order
    assert all(set(t) == {"name", "description", "inputSchema", "annotations"} for t in tools)
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert server.WIDGETS  # restored


def test_manifest_and_package_identity():
    manifest = json.loads((PLUGIN / "openclaw.plugin.json").read_text(encoding="utf-8"))
    package = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "matchawards"
    assert "version" not in manifest  # the version lives in package.json
    assert package["name"] == "@matchawards/openclaw-plugin"
    assert package["version"]
    assert manifest["configSchema"]["additionalProperties"] is False
    assert list(manifest["configSchema"]["properties"]) == ["endpoint"]
