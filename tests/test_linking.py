"""Account linking: settings, the protected-resource metadata, the /me token check and its cache. No network."""

import asyncio
import inspect
import json
import logging
from collections import OrderedDict
from types import SimpleNamespace
from typing import Annotated

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel

from matchawards_mcp import linking, server, usage
from matchawards_mcp import api as ma_api
from matchawards_mcp.server import RateLimiter

from test_http import LEGACY, http, no_api_budget_leak  # noqa: F401  (fixtures)
from test_server import ROOT, anyio_backend, api  # noqa: F401  (fixtures)

ISSUER = "https://matchawards.com"
METADATA = {
    "resource": "https://matchawards.com/mcp",
    "authorization_servers": ["https://matchawards.com"],
    "scopes_supported": ["profile:read"],
    "bearer_methods_supported": ["header"],
    "resource_name": "MatchAwards",
}
PATHS = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp")
TOKEN = "tok-AbC123._~+/xyz=="
ME = {"account": {"id": "123", "username": "acme"}, "organization": "Acme LLC", "naics": ["541512", "541511"],
      "set_aside": "SBA", "state": "va", "uei": "ABCDEF123456", "resource": "https://matchawards.com/mcp"}


def profile(account_id="123", **kw):
    return linking.parse_profile({**ME, "account": {"id": account_id, "username": "acme"}, **kw})


@pytest.fixture
def me_api(monkeypatch):
    """/me on a mock: me_api.reply(request) answers every call (a profile by default; it may be async);
    me_api.requests logs them. The profile cache starts empty."""
    me_api = SimpleNamespace(requests=[], reply=lambda req: httpx.Response(200, json=ME))

    async def handler(req):
        me_api.requests.append(req)
        reply = me_api.reply(req)
        return await reply if inspect.isawaitable(reply) else reply

    monkeypatch.setattr(linking, "client", httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=dict(linking.client.headers),
        cookies=linking.client.cookies.jar,  # the same no-cookies policy
    ))
    monkeypatch.setattr(linking, "_cache", OrderedDict())
    monkeypatch.setattr(linking, "_invalid", OrderedDict())
    return me_api


def forget():
    """Empty both token caches (profiles and rejected tokens)."""
    linking._cache.clear()
    linking._invalid.clear()


@pytest.fixture
def issuer(monkeypatch):
    """MATCHAWARDS_AUTH_ISSUER=https://matchawards.com: linking on, so resource() is https://matchawards.com/mcp."""
    monkeypatch.setattr(linking, "ISSUER", ISSUER)


@pytest.fixture
def linked(issuer, monkeypatch):
    """Linking on, with the server rebuilt for it."""
    monkeypatch.setattr(server, "mcp", server.build_mcp())


# --- Off ----------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_off_is_the_server_without_linking(me_api):
    """Off (the default), initialize and tools/list are byte for byte what 1.0.2 served (snapshot taken before
    linking existed; serverInfo.version left out), the metadata paths are 404, and a bearer is never checked."""
    assert not linking.enabled()
    golden = json.loads((ROOT / "tests" / "golden" / "linking_off.json").read_text())
    async with http() as hc:
        listed = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=LEGACY)
        init = await hc.post("/mcp", headers=LEGACY, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                           "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        for path in PATHS:
            for method in ("GET", "HEAD", "POST"):
                r = await hc.request(method, path)
                assert r.status_code == 404, (method, path)
        await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "lookup_naics", "arguments": {"query": "roofing"}}}, 
            headers={**LEGACY, "Authorization": f"Bearer {TOKEN}"})
    assert listed.json()["result"] == golden["tools/list"]
    result = init.json()["result"]
    result["serverInfo"].pop("version")
    assert result == golden["initialize"]
    assert me_api.requests == []


# --- Metadata -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_metadata_is_served_at_both_paths_on_any_allowed_or_other_host(linked, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_ALLOWED_HOSTS", "matchawards.com")
    for host in ("matchawards.com", "127.0.0.1:8765", "staging.matchawards.com"):
        async with http(host=host) as hc:
            for path in PATHS + (PATHS[1] + "/",):
                r = await hc.get(path)
                assert (r.status_code, r.json()) == (200, METADATA), (host, path)
                assert r.headers["content-type"] == "application/json"
                assert (await hc.head(path)).status_code == 200
                assert (await hc.post(path)).status_code == 405
            assert (await hc.get("/.well-known/oauth-authorization-server")).status_code == 404  # not ours


@pytest.mark.anyio
async def test_metadata_is_not_rate_limited(linked, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "2")
    async with http() as hc:
        assert {(await hc.get(PATHS[1])).status_code for _ in range(10)} == {200}


def test_resource_and_metadata_url(monkeypatch):
    monkeypatch.setattr(linking, "ISSUER", "https://staging.matchawards.com")
    assert linking.resource() == "https://staging.matchawards.com/mcp"
    assert linking.metadata_url() == "https://staging.matchawards.com/.well-known/oauth-protected-resource/mcp"
    monkeypatch.setattr(linking, "RESOURCE", "http://127.0.0.1:8765/mcp/")
    assert linking.metadata()["resource"] == "http://127.0.0.1:8765/mcp/"
    assert linking.metadata_url() == "http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp"
    assert linking.metadata_paths() == set(PATHS)


@pytest.mark.parametrize("name, value, message", [
    ("ISSUER", "matchawards.com", "must be an https URL"), ("ISSUER", "ftp://matchawards.com", "must be an https URL"),
    ("ISSUER", "http://matchawards.com", "must be an https URL"),
    ("RESOURCE", "https://x.test/mcp?a=1", "must be an https URL"),
    ("RESOURCE", "http://127.0.0.1:8765/mcp", "must be an https URL"),
    ("AGENT_API_BASE", "not a url", "must be an http\\(s\\) URL"),
    ("AGENT_API_BASE", "ftp://api.test", "must be an http\\(s\\) URL"),
])
def test_bad_settings_stop_startup(monkeypatch, name, value, message):
    monkeypatch.setattr(linking, "ISSUER", ISSUER)
    monkeypatch.setattr(linking, name, value)
    with pytest.raises(SystemExit, match=message):
        server.http_app()


def test_the_agent_api_base_may_be_plain_http(monkeypatch):
    monkeypatch.setattr(linking, "ISSUER", ISSUER)
    monkeypatch.setattr(linking, "AGENT_API_BASE", "http://10.0.0.5:3000")
    linking.check_settings()


def test_off_has_no_settings_to_check(monkeypatch):
    monkeypatch.setattr(linking, "RESOURCE", "not a url")
    linking.check_settings()  # off: nothing read


# --- The challenge header -----------------------------------------------------------------------------------------


def test_challenge_header(linked):
    assert linking.challenge_header("invalid_token", "This tool needs a linked MatchAwards account.") == (
        'Bearer realm="matchawards", '
        'resource_metadata="https://matchawards.com/.well-known/oauth-protected-resource/mcp", '
        'scope="profile:read", error="invalid_token", error_description="This tool needs a linked MatchAwards account."'
    )
    assert linking.challenge_header("x", 'say "hi" \\ bye').endswith('error_description="say \\"hi\\" \\\\ bye"')


# --- The bearer ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("headers, token", [
    ([(b"authorization", f"Bearer {TOKEN}".encode())], TOKEN),
    ([(b"authorization", f"bearer   {TOKEN} ".encode())], TOKEN),
    ([(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")], "a"),
    ([], None),
    ([(b"authorization", b"Bearer")], None),
    ([(b"authorization", b"Bearer ")], None),
    ([(b"authorization", b"Basic dXNlcjpwYXNz")], None),
    ([(b"authorization", b"Bearer a b")], None),
    ([(b"authorization", b"Bearer a\"b")], None),
    ([(b"authorization", b"Bearer =abc")], None),
    ([(b"authorization", b"Bearer " + b"a" * 4097)], None),
])
def test_bearer(headers, token):
    assert linking.bearer({"headers": headers}) == token


# --- /me ----------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_me_asks_with_the_token_and_returns_the_profile(issuer, me_api):
    got = await linking.me(TOKEN)
    assert got == linking.Profile(account_id="123", username="acme", organization="Acme LLC",
                                  naics=("541512", "541511"), set_aside="SBA", state="VA", uei="ABCDEF123456")
    [req] = me_api.requests
    assert str(req.url) == "https://matchawards.com/api/agent/v1/me"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["user-agent"] == ma_api.USER_AGENT
    assert TOKEN not in repr(got)


@pytest.mark.anyio
async def test_me_uses_the_agent_api_base(issuer, me_api, monkeypatch):
    monkeypatch.setattr(linking, "AGENT_API_BASE", "http://agent-api.test:3000")
    await linking.me(TOKEN)
    assert str(me_api.requests[0].url) == "http://agent-api.test:3000/api/agent/v1/me"


@pytest.mark.anyio
async def test_me_caches_a_profile_until_it_expires(issuer, me_api, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(linking, "clock", lambda: now[0])
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    now[0] += 59
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    assert len(me_api.requests) == 1
    await linking.me("another-token")  # another token is its own entry
    assert len(me_api.requests) == 2
    now[0] += 2  # past 60 s from the first answer
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    assert len(me_api.requests) == 3
    assert TOKEN.encode() not in b"".join(linking._cache)  # keyed by a hash, never the token


@pytest.mark.anyio
async def test_me_cache_is_bounded_least_recently_used_first(issuer, me_api, monkeypatch):
    monkeypatch.setattr(linking, "CACHE_MAX", 2)
    for token in ("a", "b", "a", "c"):  # "a" used again before "c" arrives, so "b" goes
        await linking.me(token)
    assert len(linking._cache) == 2
    n = len(me_api.requests)
    await linking.me("a")
    await linking.me("c")
    assert len(me_api.requests) == n
    await linking.me("b")
    assert len(me_api.requests) == n + 1


def _raise(exc):
    def reply(req):
        raise exc
    return reply


@pytest.mark.anyio
@pytest.mark.parametrize("reply, outcome", [
    (lambda req: httpx.Response(403, json={"error": "insufficient_scope"}), linking.INSUFFICIENT),
    (lambda req: httpx.Response(500, text="oops"), linking.UNAVAILABLE),
    (lambda req: httpx.Response(503, json={"error": "timeout"}), linking.UNAVAILABLE),
    (lambda req: httpx.Response(404, text="Not Found"), linking.UNAVAILABLE),
    (lambda req: httpx.Response(429, json={}), linking.UNAVAILABLE),
    (lambda req: httpx.Response(302, headers={"Location": "https://evil.example/"}), linking.UNAVAILABLE),
    (lambda req: httpx.Response(200, text="<html>"), linking.UNAVAILABLE),
    (lambda req: httpx.Response(200, json=["not", "a", "profile"]), linking.UNAVAILABLE),
    (lambda req: httpx.Response(200, json={"account": {}, "naics": []}), linking.UNAVAILABLE),
    (lambda req: httpx.Response(200, json={"account": {"id": "1"}}), linking.UNAVAILABLE),  # naics missing
    (_raise(httpx.ReadTimeout("slow")), linking.UNAVAILABLE),
    (_raise(httpx.ConnectError("down")), linking.UNAVAILABLE),
])
async def test_me_failures_other_than_invalid_are_told_apart_and_never_cached(issuer, me_api, reply, outcome):
    me_api.reply = reply
    assert await linking.me(TOKEN) == outcome
    assert await linking.me(TOKEN) == outcome
    assert len(me_api.requests) == 2 and not linking._cache and not linking._invalid


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [
    lambda req: httpx.Response(401, json={"error": "invalid_token"}),
    lambda req: httpx.Response(200, json={**ME, "resource": "https://other.example/mcp"}),
])
async def test_me_remembers_an_invalid_token_for_30_seconds(issuer, me_api, monkeypatch, reply):
    now = [1000.0]
    monkeypatch.setattr(linking, "clock", lambda: now[0])
    me_api.reply = reply
    assert await linking.me(TOKEN) == linking.INVALID
    now[0] += 29
    assert await linking.me(TOKEN) == linking.INVALID
    assert len(me_api.requests) == 1 and not linking._cache
    assert TOKEN.encode() not in b"".join(linking._invalid)  # keyed by a hash, never the token
    now[0] += 2  # past 30 s: asked again
    me_api.reply = lambda req: httpx.Response(200, json=ME)
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    assert len(me_api.requests) == 2


@pytest.mark.anyio
async def test_me_rejected_tokens_are_bounded_and_never_push_out_profiles(issuer, me_api, monkeypatch):
    monkeypatch.setattr(linking, "CACHE_MAX", 2)
    await linking.me("good")
    me_api.reply = lambda req: httpx.Response(401)
    for token in ("bad1", "bad2", "bad3"):
        await linking.me(token)
    assert list(linking._cache) == [linking._key("good")]
    assert len(linking._invalid) == 2
    n = len(me_api.requests)
    assert isinstance(await linking.me("good"), linking.Profile)
    assert await linking.me("bad3") == linking.INVALID
    assert len(me_api.requests) == n
    await linking.me("bad1")  # the least recently used, dropped
    assert len(me_api.requests) == n + 1


@pytest.mark.anyio
@pytest.mark.parametrize("value, outcome", [
    ("https://matchawards.com/mcp", "profile"),
    ("https://matchawards.com/mcp/", "profile"),
    ("https://matchawards.com/", linking.INVALID),
    ("https://matchawards.com/mcp/other", linking.INVALID),
    ("https://staging.matchawards.com/mcp", linking.INVALID),
    ("https://evil.example/mcp", linking.INVALID),
    ("", linking.INVALID),
    (None, linking.INVALID),
    (["https://matchawards.com/mcp"], linking.INVALID),
    ("missing", linking.INVALID),
])
async def test_me_takes_only_a_token_issued_for_this_resource(issuer, me_api, value, outcome):
    body = {k: v for k, v in ME.items() if k != "resource"} if value == "missing" else {**ME, "resource": value}
    me_api.reply = lambda req: httpx.Response(200, json=body)
    got = await linking.me(TOKEN)
    if outcome == "profile":
        assert isinstance(got, linking.Profile) and linking._cache and not linking._invalid
    else:
        assert got == outcome and not linking._cache and linking._invalid


@pytest.mark.anyio
async def test_me_compares_with_the_configured_resource(issuer, me_api, monkeypatch):
    monkeypatch.setattr(linking, "RESOURCE", "http://127.0.0.1:8765/mcp/")
    assert await linking.me(TOKEN) == linking.INVALID  # issued for https://matchawards.com/mcp
    forget()
    me_api.reply = lambda req: httpx.Response(200, json={**ME, "resource": "http://127.0.0.1:8765/mcp"})
    assert isinstance(await linking.me(TOKEN), linking.Profile)


@pytest.mark.anyio
async def test_me_follows_no_redirect_and_keeps_no_cookie(issuer, me_api):
    me_api.reply = lambda req: httpx.Response(200, json=ME, headers={"Set-Cookie": "_session=abc; Path=/"})
    await linking.me("a")
    await linking.me("b")
    assert "cookie" not in me_api.requests[1].headers
    assert not linking.client.follow_redirects


@pytest.mark.anyio
async def test_me_spends_the_server_wide_api_budget(issuer, me_api, monkeypatch):
    monkeypatch.setattr(ma_api, "api_budget", RateLimiter(1, 0))
    assert isinstance(await linking.me("a"), linking.Profile)
    assert await linking.me("b") == linking.UNAVAILABLE  # budget spent: no request made
    assert await linking.me("a") == profile()  # a cached profile needs no request, so no budget
    assert len(me_api.requests) == 1


@pytest.mark.anyio
async def test_me_never_logs_the_token(issuer, me_api, caplog):
    caplog.set_level(logging.DEBUG)
    for reply in (lambda req: httpx.Response(200, json=ME), lambda req: httpx.Response(401),
                  lambda req: httpx.Response(500), _raise(httpx.ConnectError(f"refused Bearer {TOKEN}")),
                  lambda req: httpx.Response(200, text="junk"),
                  lambda req: httpx.Response(200, json={**ME, "resource": "https://other.example/mcp"})):
        me_api.reply = reply
        forget()
        await linking.me(TOKEN)
    assert caplog.records  # the failures are logged
    for rec in caplog.records:
        assert TOKEN not in rec.getMessage() and "Bearer" not in rec.getMessage(), rec.getMessage()


@pytest.mark.parametrize("body, expected", [
    ({**ME, "account": {"id": 42}}, {"account_id": "42", "username": None}),
    ({**ME, "naics": ["541512", " 23 ", "5415121", "abc", 541511, "541512"]}, {"naics": ("541512", "23")}),
    ({**ME, "naics": []}, {"naics": ()}),
    ({**ME, "set_aside": None, "state": None, "uei": None, "organization": None},
     {"set_aside": None, "state": None, "uei": None, "organization": None}),
    ({**ME, "set_aside": "SBA,WOSB", "state": "Virginia", "uei": "short", "organization": "Acme\nLLC\x00"},
     {"set_aside": None, "state": None, "uei": None, "organization": "Acme LLC"}),
    ({**ME, "organization": "  "}, {"organization": None}),
])
def test_parse_profile_keeps_only_clean_fields(body, expected):
    got = linking.parse_profile(body)
    assert {k: getattr(got, k) for k in expected} == expected


@pytest.mark.parametrize("body", [None, [], {}, {"account": "1", "naics": []}, {"account": {"id": True}, "naics": []},
                                  {"account": {"id": " "}, "naics": []}, {"account": {"id": "1"}, "naics": "541512"}])
def test_parse_profile_rejects_a_body_that_is_not_a_profile(body):
    assert linking.parse_profile(body) is None


# --- The challenge (HTTP 401/403, and ChatGPT's tool result) ------------------------------------------------------

CLAUDE_UA = "Claude-User/1.0"
OPENAI_UA = "openai-mcp/1.0.0"
LINK_TEXT = "Link your MatchAwards account to see contracts that match your profile."
HEADER = ('Bearer realm="matchawards", '
          'resource_metadata="https://matchawards.com/.well-known/oauth-protected-resource/mcp", '
          'scope="profile:read", error="{error}", error_description="{description}"')


class Seen(BaseModel):
    account: str


async def my_matches() -> Annotated[CallToolResult, Seen]:
    """Test stand-in for a linked tool: the account it was called for."""
    got = linking.auth.get()
    await asyncio.sleep(0.01)  # concurrent calls interleave here
    if isinstance(got, linking.Profile):
        assert linking.auth.get() is got
        return Seen(account=got.account_id)
    if got == linking.UNAVAILABLE:
        raise ToolError("MatchAwards is busy right now. Retry in a minute.")
    return linking.challenge_result(got or linking.INVALID, LINK_TEXT)


@pytest.fixture
def with_tool(linked, monkeypatch):
    """Linking on, and the server's linked tool is the stand-in above instead of the real my_matches."""
    srv = server.build_mcp()
    srv.remove_tool("my_matches")
    server._tool(srv, my_matches, "My matches")
    monkeypatch.setattr(server, "mcp", srv)


def call(hc, name="my_matches", ua=CLAUDE_UA, token=None, ip=None, arguments=None):
    headers = {**LEGACY, "User-Agent": ua, **({"Authorization": f"Bearer {token}"} if token else {}),
               **({"X-Real-IP": ip} if ip else {})}
    return hc.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": name, "arguments": arguments or {}}})


def header(error):
    return HEADER.format(error=error, description=linking.DESCRIPTIONS[error])


@pytest.mark.anyio
@pytest.mark.parametrize("token, reply, status, error", [
    (None, None, 401, "invalid_token"),
    ("not a token", None, 401, "invalid_token"),
    (TOKEN, lambda req: httpx.Response(401), 401, "invalid_token"),
    (TOKEN, lambda req: httpx.Response(403, json={"error": "insufficient_scope"}), 403, "insufficient_scope"),
])
async def test_a_client_without_a_usable_token_gets_the_http_challenge(with_tool, me_api, token, reply, status, error):
    me_api.reply = reply
    async with http() as hc:
        r = await call(hc, token=token)
    assert r.status_code == status
    assert r.headers["www-authenticate"] == header(error)
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"error": error, "error_description": linking.DESCRIPTIONS[error]}
    assert len(me_api.requests) == (1 if reply else 0)  # no well-formed token: nothing to ask


@pytest.mark.anyio
async def test_a_valid_token_reaches_the_tool_with_its_profile(with_tool, me_api):
    async with http() as hc:
        for ua in (CLAUDE_UA, OPENAI_UA):
            r = await call(hc, token=TOKEN, ua=ua)
            assert r.status_code == 200 and "www-authenticate" not in r.headers
            result = r.json()["result"]
            assert not result.get("isError") and result["structuredContent"] == {"account": "123"}
    assert len(me_api.requests) == 1  # the second call used the cache


@pytest.mark.anyio
@pytest.mark.parametrize("ua", [OPENAI_UA, "OpenAI-MCP/2.0 (test)"])
@pytest.mark.parametrize("token, reply, error", [
    (None, None, "invalid_token"),
    (TOKEN, lambda req: httpx.Response(401), "invalid_token"),
    (TOKEN, lambda req: httpx.Response(403), "insufficient_scope"),
])
async def test_chatgpt_gets_the_challenge_as_a_tool_result(with_tool, me_api, ua, token, reply, error):
    me_api.reply = reply
    async with http() as hc:
        r = await call(hc, ua=ua, token=token)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": LINK_TEXT}]
    assert result["_meta"] == {"mcp/www_authenticate": [header(error)]}


@pytest.mark.anyio
@pytest.mark.parametrize("ua", [CLAUDE_UA, OPENAI_UA])
@pytest.mark.parametrize("reply", [lambda req: httpx.Response(502), _raise(httpx.ReadTimeout("slow")),
                                   lambda req: httpx.Response(200, text="junk")])
async def test_matchawards_not_answering_the_check_is_never_a_challenge(with_tool, me_api, ua, reply):
    me_api.reply = reply
    async with http() as hc:
        r = await call(hc, ua=ua, token=TOKEN)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True and "_meta" not in result
    assert "busy" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_other_requests_are_never_checked_or_challenged(with_tool, me_api, api, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "100")
    monkeypatch.setenv("MATCHAWARDS_RATE_PER_MIN", "100")
    me_api.reply = lambda req: pytest.fail("/me called")
    async with http() as hc:
        for token in (None, TOKEN, "not a token"):
            auth = {"Authorization": f"Bearer {token}"} if token else {}
            init = await hc.post("/mcp", headers={**LEGACY, **auth}, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            listed = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                   headers={**LEGACY, **auth})
            ping = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "ping"}, headers={**LEGACY, **auth})
            searched = await call(hc, "search_contracts", token=token, arguments={"limit": 1})
            unknown = await call(hc, "no_such_tool", token=token)
            for r in (init, listed, ping, searched, unknown):
                assert r.status_code == 200 and "www-authenticate" not in r.headers
            assert not searched.json()["result"].get("isError")
    assert me_api.requests == []


@pytest.mark.anyio
async def test_the_linked_tool_keeps_its_output_schema(with_tool):
    async with http() as hc:
        r = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=LEGACY)
    [tool] = [t for t in r.json()["result"]["tools"] if t["name"] == "my_matches"]
    assert tool["outputSchema"]["properties"] == {"account": {"title": "Account", "type": "string"}}


@pytest.mark.anyio
async def test_a_challenge_is_rate_limited_like_a_call(with_tool, me_api, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    usage.metrics.counts.clear()
    async with http() as hc:
        codes = [(await call(hc, ip="198.51.100.9")).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    assert usage.metrics.counts[("mcp_requests_total", (("method", "tools/call"),))] == 3


@pytest.mark.anyio
async def test_off_a_linked_tool_name_is_never_checked(me_api, monkeypatch):
    """With linking off, a tool by that name (there is none in 1.x off) is called like any other: no body read
    first, no check, no challenge, and the tool sees no outcome."""
    srv = server.build_mcp()
    server._tool(srv, my_matches, "My matches")
    monkeypatch.setattr(server, "mcp", srv)
    async with http() as hc:
        r = await call(hc, token=TOKEN)
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    assert me_api.requests == []


@pytest.mark.anyio
async def test_concurrent_calls_with_different_tokens_never_share_a_profile(with_tool, me_api, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "100")
    monkeypatch.setenv("MATCHAWARDS_RATE_PER_MIN", "100")

    async def reply(req):
        token = req.headers["authorization"].removeprefix("Bearer ")
        await asyncio.sleep(0.02 if token.endswith("1") else 0.001)  # the answers come back out of order
        return httpx.Response(200, json={**ME, "account": {"id": f"acct-{token}"}})

    me_api.reply = reply
    tokens = [f"tok{i % 4}" for i in range(24)]
    async with http() as hc:
        responses = await asyncio.gather(*(call(hc, token=t) for t in tokens))
    for t, r in zip(tokens, responses):
        assert r.json()["result"]["structuredContent"] == {"account": f"acct-{t}"}, t
    assert len(me_api.requests) >= 4


@pytest.mark.anyio
async def test_the_token_never_reaches_a_log_or_a_response(with_tool, me_api, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    async with http() as hc:
        for ua in (CLAUDE_UA, OPENAI_UA):
            for reply in (lambda req: httpx.Response(401), lambda req: httpx.Response(403),
                          lambda req: httpx.Response(500), lambda req: httpx.Response(200, json=ME)):
                me_api.reply = reply
                forget()
                r = await call(hc, ua=ua, token=TOKEN)
                seen.append(r.text + json.dumps(dict(r.headers)))
            seen.append((await call(hc, ua=ua)).text)
    text = "\n".join([r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records] + seen)
    assert "POST /mcp 401" in text  # the access log was captured
    assert TOKEN not in text and TOKEN[:8] not in text
    for key in [*linking._cache, *linking._invalid]:  # nor its keyed hash
        assert key.hex() not in text


@pytest.mark.anyio
async def test_read_body_replays_what_it_read_then_hands_over():
    messages = [{"type": "http.request", "body": b"ab", "more_body": True},
                {"type": "http.request", "body": b"cd", "more_body": False},
                {"type": "http.disconnect"}]
    it = iter(messages)

    async def receive():
        return next(it)

    body, replay = await server.read_body(receive, 10)
    assert body == b"abcd"
    assert [await replay() for _ in range(3)] == messages

    it = iter([{"type": "http.request", "body": b"x" * 6, "more_body": True},
               {"type": "http.request", "body": b"y" * 6, "more_body": True},
               {"type": "http.request", "body": b"z", "more_body": False}])
    body, replay = await server.read_body(receive, 10)
    assert body == b"x" * 6 + b"y" * 4  # stopped past the cap: the rest is left for the app
    assert [(await replay())["body"] for _ in range(3)] == [b"x" * 6, b"y" * 6, b"z"]


@pytest.mark.anyio
async def test_body_limits_hold_with_linking_on(with_tool):
    async with http() as hc:
        big = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "my_matches", "arguments": {}, "pad": "x" * (64 * 1024)}}
        assert (await hc.post("/mcp", json=big, headers=LEGACY)).status_code == 413
        padded = {**big, "params": {**big["params"], "pad": "x" * (60 * 1024)}}
        assert (await hc.post("/mcp", json=padded, headers={**LEGACY, "User-Agent": CLAUDE_UA})).status_code == 401
