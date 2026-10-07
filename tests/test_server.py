import json
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from matchawards_mcp import __version__, server

ROOT = Path(__file__).resolve().parents[1]
PATH = "/api/public/v1/opportunities"
TOOLS = ["search_contracts", "search_grants", "search_jobs", "search_by_naics", "get_opportunity", "find_contacts"]
NOTE = server.LINK_RULE


def row(i):
    return {"id": str(i), "type": "contract", "title": f"Notice {i}", "url": f"https://matchawards.com/a/posts/{i}"}


def page(ids, has_more=False, cursor=None):
    return {"as_of": "2026-10-07T12:00:00Z", "results": [row(i) for i in ids], "has_more": has_more,
            "next_cursor": cursor}


DETAIL = {**row(7), "description": "Replace the roof.", "contacts": [{"name": "Jo Smith", "title": "Contracting Officer"}]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(monkeypatch):
    """Point the server's HTTP client at a mock. api.replies is a queue; the last reply repeats."""
    api = SimpleNamespace(requests=[], replies=[lambda req: httpx.Response(200, json=page([1]))])

    def handler(req):
        api.requests.append(req)
        reply = api.replies.pop(0) if len(api.replies) > 1 else api.replies[0]
        return reply(req)

    monkeypatch.setattr(server, "client", httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test",
        headers=dict(server.client.headers),
    ))
    return api


def ok(body):
    return lambda req: httpx.Response(200, json=body)


@pytest.mark.anyio
@pytest.mark.parametrize("call, path, params", [
    (lambda: server.search_contracts(), PATH, {"type": "contract", "limit": "20"}),
    (lambda: server.search_contracts(
        naics="236220, 238210", state="va", keyword="roof", set_aside="SBA", type="federal",
        posted_within_days=7, open=False, limit=5, cursor="abc=",
    ), PATH, {"type": "federal", "naics": "236220,238210", "state": "VA", "q": "roof", "set_aside": "SBA",
              "posted_within_days": "7", "open": "false", "limit": "5", "cursor": "abc="}),
    (lambda: server.search_grants(keyword="broadband", state="NC", open=True),
     PATH, {"type": "grant", "q": "broadband", "state": "NC", "open": "true", "limit": "20"}),
    (lambda: server.search_jobs(), PATH, {"type": "job", "posted_within_days": "30", "limit": "20"}),
    (lambda: server.search_jobs(keyword="electrician", posted_within_days=7),
     PATH, {"type": "job", "q": "electrician", "posted_within_days": "7", "limit": "20"}),
    (lambda: server.search_contracts(cursor="", set_aside=""), PATH, {"type": "contract", "limit": "20"}),
    (lambda: server.search_by_naics("541511", cursor=""), PATH, {"type": "contract", "naics": "541511", "limit": "20"}),
    (lambda: server.search_by_naics("541511,541512", type="grant", cursor="c1"),
     PATH, {"type": "grant", "naics": "541511,541512", "limit": "20", "cursor": "c1"}),
    (lambda: server.get_opportunity("abc123"), f"{PATH}/abc123", {}),
    (lambda: server.find_contacts("abc-123_x"), f"{PATH}/abc-123_x", {}),
])
async def test_query_building(api, call, path, params):
    await call()
    [req] = api.requests
    assert req.method == "GET"
    assert req.url.path == path
    assert dict(req.url.params) == params
    assert req.headers["User-Agent"] == f"matchawards-mcp/{__version__} (+https://github.com/matchawards/matchawards-mcp)"


@pytest.mark.anyio
async def test_search_returns_rows_cursor_and_link_note(api):
    api.replies = [ok(page([1, 2], has_more=True, cursor="c1"))]
    assert await server.search_contracts(limit=2) == {**page([1, 2], has_more=True, cursor="c1"), "note": NOTE}


@pytest.mark.anyio
async def test_sparse_pages_are_followed_with_same_filters_and_deduplicated(api):
    api.replies = [
        ok(page([1, 2], has_more=True, cursor="c1")),
        ok(page([2, 3], has_more=True, cursor="c2")),  # 2 repeats across pages
        ok(page([4, 5], has_more=True, cursor="c3")),
    ]
    out = await server.search_contracts(state="TX", limit=5)
    assert [r["id"] for r in out["results"]] == ["1", "2", "3", "4", "5"]
    assert (out["has_more"], out["next_cursor"]) == (True, "c3")
    sent = [dict(r.url.params) for r in api.requests]
    assert [(p.get("cursor"), p["limit"]) for p in sent] == [(None, "5"), ("c1", "3"), ("c2", "2")]
    assert all(p["state"] == "TX" and p["type"] == "contract" for p in sent)


@pytest.mark.anyio
async def test_following_stops_after_three_requests(api):
    api.replies = [ok(page([], has_more=True, cursor="c"))]
    out = await server.search_grants(limit=10)
    assert len(api.requests) == 3
    assert (out["results"], out["has_more"], out["next_cursor"]) == ([], True, "c")


@pytest.mark.anyio
async def test_following_stops_when_the_api_has_no_more(api):
    api.replies = [ok(page([1], has_more=False))]
    out = await server.search_jobs()
    assert len(api.requests) == 1
    assert (out["has_more"], out["next_cursor"]) == (False, None)


@pytest.mark.anyio
async def test_failed_follow_up_keeps_the_rows_already_collected(api):
    api.replies = [ok(page([1], has_more=True, cursor="c1")), lambda req: httpx.Response(429, headers={"Retry-After": "5"})]
    out = await server.search_contracts(limit=5)
    assert [r["id"] for r in out["results"]] == ["1"]
    assert (out["has_more"], out["next_cursor"]) == (True, "c1")
    assert out["warning"].startswith("Stopped after 1 rows: MatchAwards rate limit reached")
    assert "Wait 5 seconds" in out["warning"] and out["warning"].endswith("call again with next_cursor.")


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"results": ["1", "2"], "has_more": False},
    {"results": {"id": "1"}, "has_more": False},
    {"has_more": False},
])
async def test_malformed_results_are_a_tool_error(api, body):
    api.replies = [ok(body)]
    with pytest.raises(ToolError, match="unexpected API response"):
        await server.search_contracts()


@pytest.mark.anyio
async def test_malformed_follow_up_page_keeps_rows_with_warning(api):
    api.replies = [ok(page([1], has_more=True, cursor="c1")), ok({"results": [None], "has_more": False})]
    out = await server.search_contracts(limit=5)
    assert [r["id"] for r in out["results"]] == ["1"]
    assert "unexpected API response" in out["warning"]


@pytest.mark.anyio
async def test_complete_search_has_no_warning(api):
    api.replies = [ok(page([1, 2], has_more=True, cursor="c1"))]
    assert "warning" not in await server.search_contracts(limit=2)


@pytest.mark.anyio
async def test_get_opportunity_passes_json_through_with_link_note(api):
    api.replies = [ok(DETAIL)]
    assert await server.get_opportunity("7") == {**DETAIL, "note": NOTE}


@pytest.mark.anyio
async def test_find_contacts_returns_only_id_title_url_and_contacts(api):
    api.replies = [ok(DETAIL)]
    assert await server.find_contacts("7") == {
        "id": "7", "title": "Notice 7", "url": "https://matchawards.com/a/posts/7", "contacts": DETAIL["contacts"],
    }
    api.replies = [ok(row(8))]
    assert (await server.find_contacts("8"))["contacts"] == []


def _raise(exc_type):
    def reply(req):
        raise exc_type("boom", request=req)
    return reply


@pytest.mark.anyio
@pytest.mark.parametrize("reply, message", [
    (lambda req: httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "rate_limited", "retry_after": 30}),
     "Wait 30 seconds"),
    (lambda req: httpx.Response(429, json={"error": "rate_limited", "retry_after": 12}), "Wait 12 seconds"),
    (lambda req: httpx.Response(429), "Wait 60 seconds"),
    (lambda req: httpx.Response(400, json={"error": "only for contract or federal", "parameter": "set_aside"}),
     "parameter 'set_aside': only for contract or federal"),
    (lambda req: httpx.Response(404, json={"error": "not_found"}), "Not found on MatchAwards"),
    (lambda req: httpx.Response(503, json={"error": "grants_unavailable"}), "Grant search is temporarily unavailable"),
    (lambda req: httpx.Response(500, json={"error": "internal_error"}), "server error \\(HTTP 500\\)"),
    (lambda req: httpx.Response(502, text="<html>bad gateway</html>"), "server error \\(HTTP 502\\)"),
    (_raise(httpx.ReadTimeout), "did not answer within 20 seconds"),
    (_raise(httpx.ConnectError), "Could not reach MatchAwards"),
    (lambda req: httpx.Response(200, text="<html>maintenance</html>"), "not a JSON object"),
    (lambda req: httpx.Response(200, json=[1, 2]), "not a JSON object"),
])
async def test_http_failures_become_tool_errors(api, reply, message):
    api.replies = [reply]
    with pytest.raises(ToolError, match=message):
        await server.search_contracts()
    with pytest.raises(ToolError, match=message):
        await server.get_opportunity("7")


@pytest.mark.anyio
@pytest.mark.parametrize("tool, args", [
    ("search_contracts", {"naics": "12345"}),
    ("search_contracts", {"naics": ",".join(["541511"] * 11)}),
    ("search_contracts", {"type": "grant"}),
    ("search_contracts", {"posted_within_days": 181}),
    ("search_grants", {"state": "Virginia"}),
    ("search_grants", {"keyword": "a"}),
    ("search_jobs", {"posted_within_days": 31}),
    ("search_jobs", {"limit": 0}),
    ("search_jobs", {"limit": 51}),
    ("search_by_naics", {"naics": "541511", "type": "award"}),
    ("get_opportunity", {"id": "../admin"}),
    ("find_contacts", {"id": ""}),
])
async def test_bad_input_is_rejected_before_any_request(api, tool, args):
    with pytest.raises(ToolError):
        await server.mcp.call_tool(tool, args)
    assert api.requests == []


def test_version_is_the_same_everywhere():
    listing = json.loads((ROOT / "server.json").read_text())
    assert version("matchawards-mcp") == __version__  # pyproject reads it from __init__
    assert listing["version"] == listing["packages"][0]["version"] == __version__


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])  # auto = 2026-07-28 server/discover; legacy = initialize
async def test_stdio_handshake(mode):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from matchawards_mcp.server import main; main()"],
        env={"MATCHAWARDS_API_BASE": "http://127.0.0.1:9"},  # never reached: only a bad-input call is made
    )
    async with Client(params, mode=mode) as client:
        assert (client.server_info.name, client.server_info.version) == ("matchawards", __version__)
        assert client.instructions == server.INSTRUCTIONS
        if mode == "auto":
            assert client.protocol_version == "2026-07-28"
        listed = await client.list_tools()
        assert [t.name for t in listed.tools] == TOOLS
        for t in listed.tools:
            a = t.annotations
            assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (True, False, True, True)
        if mode == "auto":
            assert (listed.ttl_ms, listed.cache_scope) == (86_400_000, "public")
        bad = await client.call_tool("search_contracts", {"naics": "12"})
        assert bad.is_error
