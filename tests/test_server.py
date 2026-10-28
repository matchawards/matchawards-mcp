import json
import re
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import Client, StdioServerParameters
import jsonschema
from mcp.server.mcpserver.exceptions import ToolError

from matchawards_mcp import __version__, server, tools
from matchawards_mcp import api as ma_api

ROOT = Path(__file__).resolve().parents[1]
PATH = "/api/public/v1/opportunities"
TOOLS = ["search_contracts", "search_grants", "search_jobs", "get_opportunity", "lookup_naics", "search_awards",
         "get_company", "assess_opportunity", "find_teaming_partners"]
NOTE = tools.LINK_RULE
UTM = "utm_source=mcp&utm_medium=ai_agent&utm_campaign=matchawards_mcp"  # outside an HTTP request


def tagged(body):
    """An API row (or page of rows) as a tool returns it: its matchawards.com url carries the UTM parameters."""
    if "results" in body:
        return {**body, "results": [tagged(r) for r in body["results"]]}
    return {**body, "url": f"{body['url']}?{UTM}"}


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
    # The default reply suits both shapes: a search page and a single opportunity ({result: ...}).
    api = SimpleNamespace(requests=[], replies=[lambda req: httpx.Response(200, json={**page([1]), "result": row(1)})])

    def handler(req):
        api.requests.append(req)
        reply = api.replies.pop(0) if len(api.replies) > 1 else api.replies[0]
        return reply(req)

    monkeypatch.setattr(ma_api, "client", httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test",
        headers=dict(ma_api.client.headers),
    ))
    return api


def ok(body):
    return lambda req: httpx.Response(200, json=body)


async def run(tool, *args, **kwargs):
    """Call a tool function directly (no MCP layer) and return its output model as a dict."""
    return (await tool(*args, **kwargs)).model_dump()


@pytest.mark.anyio
@pytest.mark.parametrize("call, path, params", [
    (lambda: tools.search_contracts(), PATH, {"type": "contract", "limit": "20"}),
    (lambda: tools.search_contracts(
        naics="236220, 238210", state="va", keyword="roof", set_aside="SBA", type="federal",
        posted_within_days=7, open=False, limit=5, cursor="abc=",
    ), PATH, {"type": "federal", "naics": "236220,238210", "state": "VA", "q": "roof", "set_aside": "SBA",
              "posted_within_days": "7", "open": "false", "limit": "5", "cursor": "abc="}),
    (lambda: tools.search_grants(keyword="broadband", naics="541715", open=True),
     PATH, {"type": "grant", "naics": "541715", "q": "broadband", "open": "true", "limit": "20"}),
    (lambda: tools.search_jobs(), PATH, {"type": "job", "posted_within_days": "30", "limit": "20"}),
    (lambda: tools.search_jobs(keyword="electrician", posted_within_days=7),
     PATH, {"type": "job", "q": "electrician", "posted_within_days": "7", "limit": "20"}),
    (lambda: tools.search_contracts(cursor="", set_aside=""), PATH, {"type": "contract", "limit": "20"}),
    (lambda: tools.search_contracts(naics="541511,541512", type="federal", cursor="c1"),
     PATH, {"type": "federal", "naics": "541511,541512", "limit": "20", "cursor": "c1"}),
    (lambda: tools.get_opportunity("abc123"), f"{PATH}/abc123", {}),
    (lambda: tools.get_opportunity("abc-123_x"), f"{PATH}/abc-123_x", {}),
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
    out = (await tools.search_contracts(limit=2)).model_dump(exclude_none=True)
    assert out == {**tagged(page([1, 2], has_more=True, cursor="c1")), "note": NOTE}


@pytest.mark.anyio
async def test_sparse_pages_are_followed_with_same_filters_and_deduplicated(api):
    api.replies = [
        ok(page([1, 2], has_more=True, cursor="c1")),
        ok(page([2, 3], has_more=True, cursor="c2")),  # 2 repeats across pages
        ok(page([4, 5], has_more=True, cursor="c3")),
    ]
    out = await run(tools.search_contracts, state="TX", limit=5)
    assert [r["id"] for r in out["results"]] == ["1", "2", "3", "4", "5"]
    assert (out["has_more"], out["next_cursor"]) == (True, "c3")
    sent = [dict(r.url.params) for r in api.requests]
    assert [(p.get("cursor"), p["limit"]) for p in sent] == [(None, "5"), ("c1", "3"), ("c2", "2")]
    assert all(p["state"] == "TX" and p["type"] == "contract" for p in sent)


@pytest.mark.anyio
async def test_following_stops_after_three_requests(api):
    api.replies = [ok(page([], has_more=True, cursor="c"))]
    out = await run(tools.search_grants, limit=10)
    assert len(api.requests) == 3
    assert (out["results"], out["has_more"], out["next_cursor"]) == ([], True, "c")


@pytest.mark.anyio
async def test_following_stops_when_the_api_has_no_more(api):
    api.replies = [ok(page([1], has_more=False))]
    out = await run(tools.search_jobs)
    assert len(api.requests) == 1
    assert (out["has_more"], out["next_cursor"]) == (False, None)


@pytest.mark.anyio
async def test_failed_follow_up_keeps_the_rows_already_collected(api):
    api.replies = [ok(page([1], has_more=True, cursor="c1")), lambda req: httpx.Response(429, headers={"Retry-After": "5"})]
    out = await run(tools.search_contracts, limit=5)
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
        await tools.search_contracts()


@pytest.mark.anyio
async def test_malformed_follow_up_page_keeps_rows_with_warning(api):
    api.replies = [ok(page([1], has_more=True, cursor="c1")), ok({"results": [None], "has_more": False})]
    out = await run(tools.search_contracts, limit=5)
    assert [r["id"] for r in out["results"]] == ["1"]
    assert "unexpected API response" in out["warning"]


@pytest.mark.anyio
async def test_bad_rows_are_skipped_and_counted_not_fatal(api):
    # A numeric id is invalid (ids are strings: snowflakes exceed 2**53), never coerced.
    api.replies = [ok({**page([1, 2]), "results": [row(1), {"title": "no id"}, {**row(3), "id": 3}, row(2)]})]
    out = await run(tools.search_contracts)
    assert [r["id"] for r in out["results"]] == ["1", "2"]
    assert out["warning"] == "2 result(s) skipped: unexpected format."


@pytest.mark.anyio
async def test_skipped_rows_do_not_count_toward_limit(api):
    api.replies = [
        ok({**page([1], has_more=True, cursor="c1"), "results": [row(1), {"id": 2}, {"id": None}]}),
        ok({**page([3, 4], has_more=True, cursor="c2"), "results": [row(3), {"id": ["x"]}, row(4)]}),
    ]
    out = await run(tools.search_contracts, limit=3)
    assert [r["id"] for r in out["results"]] == ["1", "3", "4"]
    assert [r.url.params["limit"] for r in api.requests] == ["3", "2"]  # only valid rows count toward the limit
    assert out["warning"] == "3 result(s) skipped: unexpected format."
    assert (out["has_more"], out["next_cursor"]) == (True, "c2")


@pytest.mark.anyio
async def test_paging_warning_counts_valid_rows_and_keeps_the_skip_count(api):
    api.replies = [ok({**page([1], has_more=True, cursor="c1"), "results": [row(1), {"id": None}]}),
                   lambda req: httpx.Response(500)]
    out = await run(tools.search_jobs, limit=5)
    assert out["warning"].startswith("Stopped after 1 rows: MatchAwards server error")
    assert out["warning"].endswith("call again with next_cursor. 1 result(s) skipped: unexpected format.")
    assert [r["id"] for r in out["results"]] == ["1"]


@pytest.mark.anyio
async def test_complete_search_has_no_warning(api):
    api.replies = [ok(page([1, 2], has_more=True, cursor="c1"))]
    assert (await tools.search_contracts(limit=2)).warning is None


@pytest.mark.anyio
async def test_get_opportunity_returns_the_detail_with_link_note(api):
    api.replies = [ok({"as_of": "2026-10-07T12:00:00Z", "result": DETAIL})]
    out = (await tools.get_opportunity("7")).model_dump(exclude_none=True)
    assert out == {**tagged(DETAIL), "as_of": "2026-10-07T12:00:00Z", "note": f"{NOTE} {tools.ASSESS_HINT}"}


@pytest.mark.anyio
async def test_get_opportunity_points_contracts_and_grants_to_assess_opportunity_not_jobs(api):
    for type_, hinted in (("federal", True), ("state", True), ("grant", True), ("job", False)):
        api.replies = [ok({"as_of": "x", "result": {**row(7), "type": type_}})]
        note = (await tools.get_opportunity("7")).note
        assert note.startswith(NOTE) and note.endswith(tools.ASSESS_HINT) is hinted, type_
    assert "call assess_opportunity with this id" in tools.ASSESS_HINT


@pytest.mark.anyio
async def test_get_opportunity_contacts_and_reserved_fields(api):
    api.replies = [ok({"as_of": "x", "result": {**row(8), "contacts": None}})]
    out = await run(tools.get_opportunity, "8")
    assert (out["contacts"], out["ai"], out["note"]) == ([], None, f"{NOTE} {tools.ASSESS_HINT}")  # null, no AI label
    assert "teaming" not in out
    # ai is typed: its declared fields pass, cut to 1000 characters; anything else (and teaming) is dropped.
    api.replies = [ok({"as_of": "x", "result": {**row(8), "teaming": {"angle": "x"}, "ai": {
        "fit": "Good fit.", "reasoning": "r" * 1500, "subcontracting": None, "generated": True, "extra": "x",
        "summary": {"nft_title": "T", "nft_insight": "I", "explanation": "dropped"}}}})]
    out = await run(tools.get_opportunity, "8")
    assert out["ai"] == {"fit": "Good fit.", "reasoning": "r" * 1000, "subcontracting": None, "large_prime": None,
                         "summary": {"nft_title": "T", "nft_insight": "I", "nft_advantage": None}}
    assert "teaming" not in out and out["note"] == f"{NOTE} {tools.AI_NOTE} {tools.ASSESS_HINT}"
    api.replies = [ok({"as_of": "x", "result": {**row(8), "ai": "not an object"}})]
    assert (await run(tools.get_opportunity, "8"))["ai"] is None
    api.replies = [ok(row(9))]  # old top-level shape: not accepted
    with pytest.raises(ToolError, match="no result object"):
        await tools.get_opportunity("9")
    api.replies = [ok({"as_of": "x", "result": {"title": "no id"}})]
    with pytest.raises(ToolError, match="unexpected API response \\(1 invalid fields\\)"):
        await tools.get_opportunity("9")


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
    (lambda req: httpx.Response(400, json={"error": "unsupported_filter", "parameter": "set_aside",
                                           "message": "set_aside is only for contract or federal"}),
     "parameter 'set_aside': set_aside is only for contract or federal"),
    (lambda req: httpx.Response(400, json={"error": "invalid_parameter", "parameter": "state"}),
     "parameter 'state': invalid_parameter"),
    (lambda req: httpx.Response(404, json={"error": "not_found"}), "Not found on MatchAwards"),
    (lambda req: httpx.Response(503, json={"error": "grants_unavailable", "message": "open grant list is stale"}),
     "Grant search is temporarily unavailable"),
    (lambda req: httpx.Response(503, json={"error": "timeout"}, headers={"Retry-After": "60"}),
     "took too long on this request. Retry in 60 seconds"),
    (lambda req: httpx.Response(503, json={"error": "timeout"}), "Retry in 60 seconds"),
    (lambda req: httpx.Response(500, json={"error": "internal_error"}), "server error \\(HTTP 500\\)"),
    (lambda req: httpx.Response(502, text="<html>bad gateway</html>"), "server error \\(HTTP 502\\)"),
    (_raise(httpx.ReadTimeout), "did not answer within 10 seconds"),
    (_raise(httpx.ConnectError), "Could not reach MatchAwards"),
    (lambda req: httpx.Response(200, text="<html>maintenance</html>"), "not a JSON object"),
    (lambda req: httpx.Response(200, json=[1, 2]), "not a JSON object"),
])
async def test_http_failures_become_tool_errors(api, reply, message):
    api.replies = [reply]
    with pytest.raises(ToolError, match=message):
        await tools.search_contracts()
    with pytest.raises(ToolError, match=message):
        await tools.get_opportunity("7")


@pytest.mark.anyio
@pytest.mark.parametrize("tool, args", [
    ("search_contracts", {"naics": "12345"}),
    ("search_contracts", {"naics": ",".join(["541511"] * 11)}),
    ("search_contracts", {"type": "grant"}),
    ("search_contracts", {"posted_within_days": 181}),
    ("search_grants", {"keyword": "a"}),
    ("search_jobs", {"posted_within_days": 31}),
    ("search_jobs", {"limit": 0}),
    ("search_jobs", {"limit": 51}),
    ("get_opportunity", {"id": "../admin"}),
    ("get_opportunity", {"id": ""}),
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
        assert client.instructions == server.INSTRUCTIONS + " " + server.BUSINESS_INSTRUCTIONS
        if mode == "auto":
            assert client.protocol_version == "2026-07-28"
        listed = await client.list_tools()
        assert [t.name for t in listed.tools] == TOOLS
        for t in listed.tools:
            a = t.annotations
            hints = (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint)
            assert hints == (True, False, True, False)
            assert t.title and t.output_schema and t.meta == {"securitySchemes": [{"type": "noauth"}]}
        if mode == "auto":
            assert (listed.ttl_ms, listed.cache_scope) == (86_400_000, "public")
        bad = await client.call_tool("search_contracts", {"naics": "12"})
        assert bad.is_error


# --- Tool descriptors, structured output, instructions ------------------------------------------------------

def flat(text):
    return " ".join(text.split())  # docstring line breaks fall mid-phrase


@pytest.mark.anyio
async def test_every_tool_has_title_annotations_noauth_output_schema_and_description_rules():
    listed = {t.name: t for t in await server.mcp.list_tools()}
    assert list(listed) == TOOLS
    for name, t in listed.items():
        assert t.title, name
        assert t.annotations.model_dump(by_alias=True, exclude_none=True) == {
            "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, name
        assert t.meta == {"securitySchemes": [{"type": "noauth"}]}, name
        assert t.output_schema["type"] == "object", name
        assert t.description.startswith("Use this when "), name
        assert "\n\nDo not use for " in t.description and "markdown link" in flat(t.description), name
        # Line breaks only between paragraphs, never inside a sentence (the listing shows them as they are).
        assert all(p and "\n" not in p and p.endswith(".") for p in t.description.split("\n\n")), name
        assert "oauth2" not in json.dumps(t.model_dump(mode="json", by_alias=True)).lower(), name
    for name in ("search_contracts", "search_grants", "search_jobs"):
        assert "limit (1 to 50" in flat(listed[name].description), name
        assert "naics (up to 10 codes)" in flat(listed[name].description), name
    assert "sam_url" in listed["search_contracts"].description and "sam_url" in listed["get_opportunity"].description
    assert "Open status may lag for grants that closed recently" in flat(listed["search_grants"].description)
    # Fit and bid/no-bid questions go to assess_opportunity, not get_opportunity.
    assert listed["assess_opportunity"].description.startswith(
        "Use this when the user asks if one opportunity is a good fit for them, whether to bid, or wants a bid brief: ")
    assert "for fit or bid/no-bid questions (assess_opportunity)" in listed["get_opportunity"].description
    assert "notes on fit" not in listed["get_opportunity"].description


SKIP_CASE = [ok({**page([]), "results": [row(1), {"id": ["bad"]}]})]  # one row skipped


@pytest.mark.anyio
@pytest.mark.parametrize("name, args, replies", [
    ("search_contracts", {}, [ok(page([1, 2]))]),
    ("search_contracts", {}, [ok(page([]))]),  # no results
    ("search_contracts", {"limit": 5}, [  # a follow-up page fails: rows so far plus a warning
        ok(page([1], has_more=True, cursor="c1")), lambda req: httpx.Response(429, headers={"Retry-After": "5"})]),
    ("search_grants", {}, [ok(page([3]))]),
    ("search_jobs", {}, [ok({"as_of": None, "results": [{"id": "4", "title": "Electrician", "agency": None,
                                                         "naics": None, "sam_url": None}], "has_more": False})]),
    ("get_opportunity", {"id": "7"}, [ok({"as_of": "x", "result": {
        **DETAIL, "naics": {"code": "238160", "title": "Roofing Contractors"}, "sam_url": "https://sam.gov/opp/1",
        "no_fixed_deadline": True, "agency_id": "dept_of_the_army", "made_up_field": "x"}})]),
    ("get_opportunity", {"id": "8"}, [ok({"as_of": "x", "result": row(8)})]),  # no description, no contacts
    ("search_grants", {}, SKIP_CASE),
])
async def test_structured_content_validates_against_output_schema(api, name, args, replies):
    api.replies = replies
    schema = {t.name: t.output_schema for t in await server.mcp.list_tools()}[name]
    out = await server.mcp.call_tool(name, args)
    assert not out.is_error
    jsonschema.validate(out.structured_content, schema)
    assert json.loads(out.content[0].text) == out.structured_content  # hosts without structured output read this
    if "limit" in args:
        assert out.structured_content["warning"].startswith("Stopped after 1 rows")
    if name == "search_grants" and replies is SKIP_CASE:
        assert out.structured_content["warning"] == "1 result(s) skipped: unexpected format."
    assert "made_up_field" not in out.structured_content  # undeclared API fields stay out until a model declares them
    if name == "get_opportunity" and args["id"] == "7":
        assert out.structured_content["agency_id"] == "dept_of_the_army"  # declared: passed through


@pytest.mark.anyio
async def test_no_email_or_phone_reaches_the_output(api):
    api.replies = [ok({"as_of": "x", "email": "top@example.com", "phone": "202-555-0100", "result": {
        **DETAIL, "email": "row@example.com", "phone": "202-555-0101",
        "contacts": [{"name": "Jo Smith", "title": "Contracting Officer", "email": "jo@example.com",
                      "phone": "202-555-0102"}],
    }})]
    out = await server.mcp.call_tool("get_opportunity", {"id": "7"})
    assert out.structured_content["contacts"] == [{"name": "Jo Smith", "title": "Contracting Officer"}]
    for text in (json.dumps(out.structured_content), out.content[0].text):
        assert "email" not in text and "phone" not in text and "@example.com" not in text and "555-01" not in text


def test_instructions_carry_the_essentials_in_the_first_512_characters():
    head = server.INSTRUCTIONS[:512]
    for needle in [*TOOLS, "markdown link", "sam_url", "may be incomplete", "Never call a firm the incumbent.",
                   "AI-generated text is labelled; keep the label when you quote it.",
                   "SAM.gov", "Grants.gov", "USAJOBS", "state portals"]:
        assert needle in head, needle


def test_instructions_without_linking_ask_for_the_business_and_map_it_with_lookup_naics():
    """Without account linking, "contracts for my business" with no codes: the model asks what the business does
    (or its codes) and finds the codes with lookup_naics, instead of guessing or searching by keyword."""
    text = server.BUSINESS_INSTRUCTIONS
    assert "ask what it does" in text and "lookup_naics" in text and "my_matches" not in text


def test_instructions_steer_to_one_multi_naics_search_and_the_cursor():
    assert "naics='541511,541512'" in server.INSTRUCTIONS and "not one call per code" in server.INSTRUCTIONS
    assert "call again with that cursor before saying nothing exists" in server.INSTRUCTIONS
    assert "show_results" not in server.INSTRUCTIONS  # the card tool is not there without widgets
    assert len(server.INSTRUCTIONS) <= 1024
    for name in ("search_contracts", "search_grants", "search_jobs", "search_awards"):
        doc = " ".join(getattr(tools, name).__doc__.split())  # as published: one line per paragraph
        assert "Pass several NAICS codes in one call" in doc and "show_results" not in doc, name


@pytest.mark.anyio
async def test_card_instructions_only_with_widgets(monkeypatch):
    assert server.mcp.instructions == server.INSTRUCTIONS + " " + server.BUSINESS_INSTRUCTIONS
    monkeypatch.setattr(server, "WIDGETS", True)
    srv = server.build_mcp()
    assert srv.instructions == " ".join([server.INSTRUCTIONS, server.BUSINESS_INSTRUCTIONS, server.CARD_INSTRUCTIONS])
    assert "call show_results last with the ids you picked" in server.CARD_INSTRUCTIONS
    assert len(srv.instructions) <= 1024


@pytest.mark.anyio
async def test_job_source_is_sent_only_when_set(api, monkeypatch):
    await tools.search_jobs()
    assert "source" not in api.requests[-1].url.params
    monkeypatch.setattr(tools, "JOB_SOURCE", "usajobs")
    await tools.search_jobs(keyword="nurse")
    assert dict(api.requests[-1].url.params) == {
        "type": "job", "q": "nurse", "posted_within_days": "30", "limit": "20", "source": "usajobs"}
    await tools.search_contracts()
    assert "source" not in api.requests[-1].url.params  # jobs only


@pytest.mark.anyio
async def test_golden_prompts_name_only_listed_tools():
    golden = json.loads((ROOT / "tests" / "golden" / "prompts.json").read_text())
    listed = {t.name for t in await server.mcp.list_tools()}
    kinds = {p["kind"] for p in golden["prompts"]}
    assert kinds == {"direct", "indirect", "negative"} and len(golden["prompts"]) >= 30
    for p in golden["prompts"]:
        assert p["prompt"] and set(p["expect"]) <= listed, p
        assert (p["kind"] == "negative") == (p["expect"] == []), p
    assert listed == {t for p in golden["prompts"] for t in p["expect"]}  # every tool has a prompt


# --- The result card (MCP Apps), behind MATCHAWARDS_WIDGETS -------------------------------------------------

CARD_SCRIPT = server.CARD_HTML[server.CARD_HTML.index("<script>"):]


@pytest.mark.anyio
async def test_card_is_off_by_default():
    assert not server.WIDGETS
    assert await server.mcp.list_resources() == []
    listed = await server.mcp.list_tools()
    assert all("ui" not in (t.meta or {}) for t in listed)
    assert "show_results" not in {t.name for t in listed}  # the card tool exists only with the card


@pytest.mark.anyio
async def test_card_on_lists_the_resource_and_only_show_results_points_at_it():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from matchawards_mcp.server import main; main()"],
        env={"MATCHAWARDS_WIDGETS": "1", "MATCHAWARDS_API_BASE": "http://127.0.0.1:9"},
    )
    async with Client(params) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        assert list(listed) == TOOLS + ["show_results"]
        carded = {n for n, t in listed.items() if "ui" in t.meta}
        assert carded == {"show_results"}  # the search tools return data only
        for name, t in listed.items():
            assert t.meta["securitySchemes"] == [{"type": "noauth"}], name  # the card meta merges, never replaces
            assert "openai/outputTemplate" not in t.meta, name
        for name in carded:
            assert listed[name].meta["ui"] == {"resourceUri": "ui://matchawards/results-v2.html"}
        [res] = (await client.list_resources()).resources
        assert (str(res.uri), res.mime_type) == ("ui://matchawards/results-v2.html", "text/html;profile=mcp-app")
        [content] = (await client.read_resource("ui://matchawards/results-v2.html")).contents
    assert content.mime_type == "text/html;profile=mcp-app"
    assert content.text == server.CARD_HTML
    ui = content.meta["ui"]
    assert ui["csp"] == {"connectDomains": [], "resourceDomains": [], "frameDomains": []}
    assert (ui["domain"], ui["prefersBorder"]) == ("https://matchawards.com", True)
    assert content.meta["openai/widgetCSP"] == {
        "connect_domains": [], "resource_domains": [],
        "redirect_domains": ["https://matchawards.com"],
    }


def test_card_needs_no_network_and_survives_a_strict_csp():
    html = server.CARD_HTML
    # Nothing to fetch: no external script, style, image, font or request of any kind.
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource|<link|<img|<iframe|\bsrc=|@import|url\(",
                         html)
    # The one URL in the file is the origin a row link must have (the link itself comes from the data).
    assert set(re.findall(r"https?://[^\s\"'<>)]*", html)) == {"https://matchawards.com"}
    # No inline event handlers (a host CSP without 'unsafe-hashes' blocks them), no HTML sinks, no eval.
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", CARD_SCRIPT)


def test_card_renders_from_structured_content_and_never_calls_a_tool():
    assert "structuredContent" in CARD_SCRIPT and "toolOutput" in CARD_SCRIPT
    assert "_meta" not in CARD_SCRIPT and "toolResponseMetadata" not in CARD_SCRIPT  # host bugs: _meta leaks/varies
    # A follow-up goes out as the user's own message: only the validated id, never upstream text such as the title.
    assert '"Show details for MatchAwards opportunity " + id' in CARD_SCRIPT
    assert re.search(r'ask\([^)]*title', CARD_SCRIPT) is None
    assert "tools/call" not in CARD_SCRIPT and "callTool" not in CARD_SCRIPT  # actions go through the model
    # The opportunity layout is picked by the shape of SearchResult.
    from matchawards_mcp.schemas import SearchResult
    assert {"results", "has_more", "warning"} <= SearchResult.model_fields.keys()
    # The opportunity layout takes both SearchResult and ShownResults: results plus has_more, or plus missing.
    from matchawards_mcp.schemas import ShownResults
    assert {"results", "missing"} <= ShownResults.model_fields.keys()
    assert ('Array.isArray(d.results) && (typeof d.has_more === "boolean" || Array.isArray(d.missing))'
            in CARD_SCRIPT)
    assert '" could not be shown."' in CARD_SCRIPT
    assert "Array.isArray(d.failed)" in CARD_SCRIPT  # failed counts with missing; old payloads have no failed
    # show_results' warning is retry advice for the model: the card shows it only for a search page.
    assert "!shown && str(d.warning)" in CARD_SCRIPT
    assert 'Array.isArray(d.missing) || typeof d.has_more !== "boolean"' in CARD_SCRIPT  # show_results detection
    assert "sync();\n    size();" in CARD_SCRIPT  # the height is reported once the bridge is up


def test_card_rows_expand_and_ask_is_throttled():
    # A row expands in place (announced); the two actions live in its panel; one ask per 8 s across the card.
    assert 'setAttribute("aria-expanded"' in CARD_SCRIPT and 'setAttribute("aria-controls"' in CARD_SCRIPT
    assert "var ASK_PAUSE = 8000;" in CARD_SCRIPT
    assert "if (Date.now() < askedUntil) return;" in CARD_SCRIPT
    assert "canMessage() && id" in CARD_SCRIPT  # no Ask button when the host can't send messages
    assert "var MAX_SHOWN = 10;" in CARD_SCRIPT  # every id show_results takes
    assert 'BICiv: "Buy Indian (civilian)"' in CARD_SCRIPT
    # ChatGPT's sizing hint, feature-detected.
    assert 'typeof o.notifyIntrinsicHeight === "function"' in CARD_SCRIPT


@pytest.mark.anyio
async def test_card_only_on_show_results_and_only_with_widgets(monkeypatch):
    assert "show_results" not in {t.name for t in await server.mcp.list_tools()}  # WIDGETS off by default
    monkeypatch.setattr(server, "WIDGETS", True)
    srv = server.build_mcp()
    listed = {t.name: t for t in await srv.list_tools()}
    assert list(listed) == TOOLS + ["show_results"]
    t = listed["show_results"]
    assert t.title == "Show results"
    assert t.meta == {"securitySchemes": [{"type": "noauth"}],
                      "ui": {"resourceUri": "ui://matchawards/results-v2.html"}}
    assert t.annotations.read_only_hint and not t.annotations.open_world_hint
    assert t.input_schema["properties"]["ids"]["minItems"] == 1
    assert t.input_schema["properties"]["ids"]["maxItems"] == 10
    assert t.description.startswith("Use this last, after searching, to show the user the opportunities you picked")
    assert all(p and "\n" not in p and p.endswith(".") for p in t.description.split("\n\n"))
    for name in TOOLS:
        assert "ui" not in (listed[name].meta or {}), name
    [res] = await srv.list_resources()
    assert str(res.uri) == server.CARD_URI == "ui://matchawards/results-v2.html"
    assert server.CARD_META["openai/widgetDescription"] == (
        "A MatchAwards card listing the opportunities you chose to show, with agency, place, deadline and set-aside; "
        "rows expand for details. It already lists them, so do not repeat the list; add only what matters.")
    # The module-level server is untouched by building another one.
    assert "show_results" not in {t.name for t in await server.mcp.list_tools()}


@pytest.mark.parametrize("raw, shown", [
    ("DEPT OF DEFENSE.DEPT OF THE ARMY.AMC.ARMY CONTRACTING COMMAND.ACC-CTRS.W6QK ACC-APG DIR",
     "Dept of Defense · Dept of the Army"),
    ("DEFENSE ADVANCED RESEARCH PROJECTS AGENCY  (DARPA)", "Defense Advanced Research Projects Agency (DARPA)"),
    ("FEDERAL ACQUISITION SERVICE", "Federal Acquisition Service"),
    ("Department Of Energy   Office Of Science", "Department of Energy Office of Science"),
    ("U.S. Customs and Border Protection", "U.S. Customs and Border Protection"),
    ("NASA", "NASA"),
    ("Fairfax County Purchasing", "Fairfax County Purchasing"),
    (None, None),
])
def test_agency_names_read_as_people_write_them(raw, shown):
    from matchawards_mcp.schemas import OpportunityRow
    assert OpportunityRow.model_validate({"id": "1", "agency": raw}).agency == shown
