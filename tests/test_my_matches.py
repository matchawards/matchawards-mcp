"""my_matches: open contracts for the linked account's profile. The tool directly, then end to end over HTTP with
the token check on a mock /me. No network."""

import contextlib

import httpx
import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from matchawards_mcp import linking, server, tools, usage

from test_http import LEGACY, http, no_api_budget_leak  # noqa: F401  (fixtures)
from test_linking import (  # noqa: F401  (fixtures)
    CLAUDE_UA, ME, OPENAI_UA, TOKEN, call, header, issuer, linked, me_api, naics, profile,
)
from test_server import UTM, anyio_backend, api, ok, page, tagged  # noqa: F401  (fixtures)

PATH = "/api/public/v1/opportunities"
SETTINGS = f"https://matchawards.com/settings/match_settings?{UTM}"
NOTE = "Open contracts matching the user's MatchAwards profile (profile_used)."


@contextlib.contextmanager
def authed(outcome):
    """linking.auth as HttpGuard sets it for a checked call."""
    token = linking.auth.set(outcome)
    try:
        yield
    finally:
        linking.auth.reset(token)


async def run(outcome=None, **kwargs):
    with authed(profile() if outcome is None else outcome):
        return await tools.my_matches(**kwargs)


# --- The tool -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_search_carries_the_profiles_filters(api):
    api.replies = [ok(page([1, 2]))]
    out = await run()
    [req] = api.requests
    assert req.url.path == PATH
    assert dict(req.url.params) == {"type": "contract", "naics": "541512,541511", "state": "VA", "open": "true",
                                    "limit": "20"}  # no set_aside: a small business can bid on open competitions too
    assert out.model_dump(exclude_none=True) == {
        **tagged({k: v for k, v in page([1, 2]).items() if v is not None}),
        "profile_used": {"naics": ["541512", "541511"], "set_aside": "SBA", "set_aside_name": "Total Small Business",
                         "set_aside_applied": False, "state": "VA"},
        "note": f"{NOTE} {tools.STATE_NOTE} {tools._set_aside_note('SBA', False)} {tools.LINK_RULE}",
    }
    assert tools.TRUNCATED_NOTE not in out.note
    assert "set-aside is SBA" in out.note and "only_my_set_aside=true" in out.note


@pytest.mark.anyio
async def test_only_my_set_aside_filters_by_the_profiles_set_aside(api):
    out = await run(only_my_set_aside=True)
    assert api.requests[0].url.params["set_aside"] == "SBA"
    assert (out.profile_used.set_aside, out.profile_used.set_aside_applied) == ("SBA", True)
    assert "Only notices set aside for the user's set-aside (SBA) are listed." in out.note


@pytest.mark.anyio
async def test_options_type_limit_and_ignore_state(api):
    out = await run(type="federal", limit=5, ignore_state=True)
    assert dict(api.requests[0].url.params) == {"type": "federal", "naics": "541512,541511", "open": "true",
                                                "limit": "5"}
    assert out.profile_used.state is None and tools.STATE_NOTE not in out.note


@pytest.mark.anyio
@pytest.mark.parametrize("only", [True, False])
async def test_state_notices_never_get_the_set_aside_filter(api, only):
    """The API takes set_aside with federal or mixed contracts only (a 400 for type=state)."""
    out = await run(type="state", only_my_set_aside=only)
    assert "set_aside" not in api.requests[0].url.params
    assert (out.profile_used.set_aside, out.profile_used.set_aside_applied) == ("SBA", False)
    assert "The set-aside filter applies to federal notices only" in out.note
    assert "only_my_set_aside=true" not in out.note


@pytest.mark.anyio
async def test_mixed_and_federal_searches_take_the_set_aside(api):
    for type_ in (None, "federal"):
        out = await run(type=type_, only_my_set_aside=True)
        assert api.requests[-1].url.params["set_aside"] == "SBA" and out.profile_used.set_aside_applied


@pytest.mark.anyio
async def test_a_profile_without_set_aside_or_state_sends_neither(api):
    out = await run(profile(set_aside=None, state=None), only_my_set_aside=True)
    assert dict(api.requests[0].url.params) == {"type": "contract", "naics": "541512,541511", "open": "true",
                                                "limit": "20"}
    assert not out.profile_used.set_aside_applied and "set-aside" not in out.note


@pytest.mark.anyio
async def test_pages_follow_the_cursor_with_the_same_filters(api):
    api.replies = [ok(page([1, 2], has_more=True, cursor="c1"))]
    first = await run(limit=2)
    assert (first.has_more, first.next_cursor) == (True, "c1")
    api.replies = [ok(page([3], has_more=False))]
    second = await run(limit=2, cursor=first.next_cursor)
    sent = [dict(r.url.params) for r in api.requests]
    assert sent[1] == {**sent[0], "cursor": "c1"}
    assert [r.id for r in second.results] == ["3"] and not second.has_more


@pytest.mark.anyio
@pytest.mark.parametrize("codes", [[], ["54", "5415"]])
async def test_a_profile_without_naics_codes_gets_the_settings_link_and_no_search(api, codes):
    out = await run(profile(naics=naics(*codes)))
    assert api.requests == []
    assert out.model_dump(exclude_none=True) == {
        "results": [], "has_more": False, "settings_url": SETTINGS, "note": tools.NO_NAICS_NOTE,
        "profile_used": {"naics": [], "set_aside": "SBA", "set_aside_name": "Total Small Business",
                         "set_aside_applied": False, "state": "VA"},
    }


@pytest.mark.anyio
@pytest.mark.parametrize("url, expected", [
    ("https://staging.matchawards.com/settings/match_settings",
     f"https://staging.matchawards.com/settings/match_settings?{UTM}"),
    ("https://matchawards.com/settings/match_settings?tab=naics", f"https://matchawards.com/settings/"
     f"match_settings?tab=naics&{UTM}"),
    (None, SETTINGS),  # none on the profile: the default
    ("https://evil.example/settings", SETTINGS),  # not the site: dropped at parse, so the default
])
async def test_the_settings_link_is_the_profiles_own_when_it_has_one(api, url, expected):
    out = await run(profile(naics=[], match_settings_url=url))
    assert out.settings_url == expected


@pytest.mark.anyio
async def test_a_truncated_profile_says_only_the_first_ten_codes_were_searched(api):
    codes = [f"5415{i:02d}" for i in range(10)]
    out = await run(profile(naics=naics(*codes), naics_truncated=True))
    assert api.requests[0].url.params["naics"] == ",".join(codes)
    assert out.profile_used.naics == codes
    assert tools.TRUNCATED_NOTE in out.note and "first 10 NAICS codes" in tools.TRUNCATED_NOTE


@pytest.mark.anyio
async def test_more_than_ten_codes_are_cut_to_ten_and_the_note_says_so(api):
    """/me lists at most 10; should it send more, only the first 10 are searched."""
    codes = [f"5415{i:02d}" for i in range(12)]
    out = await run(profile(naics=naics(*codes)))
    assert api.requests[0].url.params["naics"] == ",".join(codes[:10])
    assert out.profile_used.naics == codes[:10]
    assert tools.TRUNCATED_NOTE in out.note


@pytest.mark.anyio
@pytest.mark.parametrize("outcome, error", [
    (None, "invalid_token"), (linking.INVALID, "invalid_token"), (linking.INSUFFICIENT, "insufficient_scope"),
    ("something else", "invalid_token"), (object(), "invalid_token"), ({"sub": ME["sub"]}, "invalid_token"),
])
async def test_without_a_profile_it_never_searches_and_answers_the_challenge(api, issuer, outcome, error):
    with authed(outcome):
        result = await tools.my_matches()
    assert api.requests == []
    assert result.is_error and result.content[0].text == tools.LINK_PROMPT
    assert result.meta == {"mcp/www_authenticate": [header(error)]}


@pytest.mark.anyio
async def test_a_refused_account_gets_a_plain_error_not_a_challenge(api):
    with pytest.raises(ToolError, match="This MatchAwards account can't use my_matches. Check the account on "
                       "matchawards.com.") as e:
        await run(linking.FORBIDDEN)
    assert e.value.outcome == "forbidden"
    assert api.requests == []


@pytest.mark.anyio
async def test_matchawards_not_answering_the_check_is_a_busy_error_not_a_challenge(api):
    with pytest.raises(ToolError, match="MatchAwards is busy right now"):
        await run(linking.UNAVAILABLE)
    assert api.requests == []


def test_the_link_prompt_says_what_linking_gives():
    assert "link their MatchAwards account" in tools.LINK_PROMPT and "Google or email" in tools.LINK_PROMPT
    assert "NAICS" in tools.LINK_PROMPT


# --- Registration -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tools_list_with_linking_on(linked):
    async with http() as hc:
        r = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=LEGACY)
    listed = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert list(listed)[-1] == "my_matches"
    mine = listed.pop("my_matches")
    assert mine["title"] == "My matches"
    assert mine["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                   "openWorldHint": False}
    assert mine["_meta"]["securitySchemes"] == [{"type": "noauth"}, {"type": "oauth2", "scopes": ["profile:read"]}]
    assert set(mine["outputSchema"]["properties"]) == {"as_of", "results", "has_more", "next_cursor", "note",
                                                       "warning", "profile_used", "settings_url"}
    assert set(mine["inputSchema"]["properties"]) == {"type", "ignore_state", "only_my_set_aside", "limit", "cursor"}
    for name, t in listed.items():
        assert t["_meta"]["securitySchemes"] == [{"type": "noauth"}], name


@pytest.mark.anyio
async def test_off_my_matches_is_not_registered():
    assert "my_matches" not in {t.name for t in await server.mcp.list_tools()}


def test_instructions_point_at_my_matches_only_with_linking_on(issuer, monkeypatch):
    assert server.build_mcp().instructions == server.INSTRUCTIONS + " " + server.LINKED_INSTRUCTIONS
    monkeypatch.setattr(server, "WIDGETS", True)
    assert server.build_mcp().instructions == " ".join(
        [server.INSTRUCTIONS, server.LINKED_INSTRUCTIONS, server.CARD_INSTRUCTIONS])
    assert "my_matches" in server.LINKED_INSTRUCTIONS and "my matches" in server.LINKED_INSTRUCTIONS
    monkeypatch.setattr(linking, "ISSUER", "")
    assert "my_matches" not in server.build_mcp().instructions


@pytest.mark.anyio
async def test_with_the_card_my_matches_points_at_show_results(issuer, api, monkeypatch):
    monkeypatch.setattr(server, "WIDGETS", True)
    srv = server.build_mcp()
    tool = {t.name: t for t in await srv.list_tools()}["my_matches"]
    assert tool.description.endswith(server.SHOW_HINT)
    with authed(profile()):
        out = await srv.call_tool("my_matches", {})
        assert out.structured_content["note"].endswith(server.SHOW_HINT)
    with authed(linking.INVALID):  # the challenge passes through the hint unchanged
        out = await srv.call_tool("my_matches", {})
    assert out.is_error and out.content[0].text == tools.LINK_PROMPT


# --- End to end over HTTP -----------------------------------------------------------------------------------------


async def _schema(hc):
    r = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=LEGACY)
    return next(t["outputSchema"] for t in r.json()["result"]["tools"] if t["name"] == "my_matches")


@pytest.mark.anyio
async def test_a_linked_user_gets_rows_that_validate_against_the_schema(linked, me_api, api):
    api.replies = [ok(page([1, 2]))]
    async with http() as hc:
        schema = await _schema(hc)
        r = await call(hc, token=TOKEN, arguments={"limit": 2})
    result = r.json()["result"]
    assert r.status_code == 200 and not result.get("isError")
    out = result["structuredContent"]
    jsonschema.validate(out, schema)
    assert [row["id"] for row in out["results"]] == ["1", "2"]
    assert out["results"][0]["url"].endswith("utm_source=claude&utm_medium=ai_agent&utm_campaign=matchawards_mcp")
    [req] = api.requests
    assert (req.url.params["naics"], req.url.params["state"]) == ("541512,541511", "VA")
    assert "set_aside" not in req.url.params
    assert "authorization" not in req.headers  # the token goes to /me only, never to the public API


@pytest.mark.anyio
async def test_a_profile_without_naics_validates_against_the_schema(linked, me_api, api):
    me_api.reply = lambda req: httpx.Response(200, json={**ME, "naics": [], "match_settings_url": None})
    async with http() as hc:
        schema = await _schema(hc)
        r = await call(hc, token=TOKEN, ua=OPENAI_UA)
    out = r.json()["result"]["structuredContent"]
    jsonschema.validate(out, schema)
    assert out["settings_url"].startswith("https://matchawards.com/settings/match_settings?utm_source=chatgpt")
    assert api.requests == []


@pytest.mark.anyio
async def test_chatgpt_without_a_token_gets_the_challenge_result(linked, me_api, api):
    async with http() as hc:
        r = await call(hc, ua=OPENAI_UA)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": tools.LINK_PROMPT}]
    assert result["_meta"]["mcp/www_authenticate"] == [header("invalid_token")]
    assert api.requests == [] and me_api.requests == []


@pytest.mark.anyio
async def test_claude_without_a_token_gets_401_before_the_tool(linked, me_api, api):
    async with http() as hc:
        r = await call(hc, ua=CLAUDE_UA)
    assert r.status_code == 401 and r.headers["www-authenticate"] == header("invalid_token")
    assert api.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("ua", [CLAUDE_UA, OPENAI_UA])
async def test_matchawards_not_answering_the_check_is_a_busy_error_end_to_end(linked, me_api, api, ua):
    me_api.reply = lambda req: httpx.Response(502)
    async with http() as hc:
        r = await call(hc, ua=ua, token=TOKEN)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True and "_meta" not in result
    assert result["content"][0]["text"].endswith("MatchAwards is busy right now. Retry in a minute.")
    assert api.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("ua", [CLAUDE_UA, OPENAI_UA])
async def test_a_refused_account_is_a_plain_error_end_to_end(linked, me_api, api, ua):
    me_api.reply = lambda req: httpx.Response(403, json={"error": "forbidden"})
    usage.metrics.counts.clear()
    async with http() as hc:
        r = await call(hc, ua=ua, token=TOKEN)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True and "_meta" not in result
    assert result["content"][0]["text"].endswith(tools.FORBIDDEN_TEXT)
    assert api.requests == []
    assert usage.metrics.counts[("mcp_tool_calls_total", (("outcome", "forbidden"), ("tool", "my_matches")))] == 1
    assert not any(name == "mcp_link_challenges_total" for name, _ in usage.metrics.counts)


@pytest.mark.anyio
async def test_calls_are_counted_by_outcome(linked, me_api, api):
    usage.metrics.counts.clear()
    async with http() as hc:
        await call(hc, ua=OPENAI_UA)
        await call(hc, token=TOKEN)
    counts = usage.metrics.counts
    assert counts[("mcp_tool_calls_total", (("outcome", "link_required"), ("tool", "my_matches")))] == 1
    assert counts[("mcp_tool_calls_total", (("outcome", "ok"), ("tool", "my_matches")))] == 1
