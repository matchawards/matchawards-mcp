"""Account linking: settings, the protected-resource metadata, the /me token check and its cache. No network."""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
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
SUB = "0123456789abcdef0123456789abcdef"
SETTINGS_URL = "https://matchawards.com/settings/match_settings"


def naics(*codes):
    """/me's naics: objects with a code and a title."""
    return [{"code": c, "title": f"Title of {c}"} for c in codes]


ME = {"sub": SUB, "naics": naics("541512", "541511"), "naics_truncated": False,
      "set_aside": {"code": "SBA", "name": "Total Small Business"}, "state": "va", "uei": "ABCDEF123456",
      "organization_name": "Acme LLC", "match_settings_url": SETTINGS_URL, "scope": "profile:read",
      "token_expires_at": "2099-04-11T12:00:00Z", "resource": "https://matchawards.com/mcp"}


def profile(**kw):
    return linking.parse_profile({**ME, **kw})


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
    monkeypatch.setattr(linking, "_refused", OrderedDict())
    monkeypatch.setattr(linking, "_refused_floor", -1)
    return me_api


def forget():
    """Empty both token caches (profiles and rejected tokens)."""
    linking._cache.clear()
    linking._invalid.clear()
    linking._refused.clear()


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
    linking existed, then regenerated for the 1.1 get_opportunity and assess_opportunity wording and the 1.1.2
    business-ask instruction; serverInfo.version
    left out), the metadata paths are 404, and a bearer is never checked."""
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
    ("ISSUER", "http://127.0.0.2", "must be an https URL"),
    ("RESOURCE", "https://x.test/mcp?a=1", "must be an https URL"),
    ("RESOURCE", "http://matchawards.com/mcp", "must be an https URL"),
    ("RESOURCE", "http://localhost.example/mcp", "must be an https URL"),
    ("AGENT_API_BASE", "not a url", "must be an http\\(s\\) URL"),
    ("AGENT_API_BASE", "ftp://api.test", "must be an http\\(s\\) URL"),
    ("ISSUER", "https://user:pass@matchawards.com", "must be an https URL"),
    ("RESOURCE", "https://user@matchawards.com/mcp", "must be an https URL"),
    ("AGENT_API_BASE", "http://user:pass@10.0.0.5:3000", "must be an http\\(s\\) URL"),
    ("ISSUER", "https://matchawards.com:99999", "must be an https URL"),
    ("RESOURCE", "https://matchawards.com:x/mcp", "must be an https URL"),
    ("AGENT_API_BASE", "http://10.0.0.5:-1", "must be an http\\(s\\) URL"),
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


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
def test_a_local_run_may_use_http_on_a_loopback_host(monkeypatch, host):
    monkeypatch.setattr(linking, "ISSUER", f"http://{host}:3000")
    monkeypatch.setattr(linking, "RESOURCE", f"http://{host}:8765/mcp")
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
    assert got == linking.Profile(sub=SUB, organization="Acme LLC", naics=("541512", "541511"), naics_truncated=False,
                                  set_aside="SBA", set_aside_name="Total Small Business", state="VA",
                                  uei="ABCDEF123456", match_settings_url=SETTINGS_URL)
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
async def test_a_profile_is_never_cached_past_its_token_expiry(issuer, me_api, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(linking, "clock", lambda: now[0])
    soon = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    me_api.reply = lambda req: httpx.Response(200, json={**ME, "token_expires_at": soon})
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    now[0] += 12  # inside the 60 s cache time, past the token's expiry
    await linking.me(TOKEN)
    assert len(me_api.requests) == 2


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
    (lambda req: httpx.Response(200, json={"sub": "", "naics": []}), linking.UNAVAILABLE),
    (lambda req: httpx.Response(200, json={"sub": SUB}), linking.UNAVAILABLE),  # naics missing
    (lambda req: httpx.Response(200, json={**ME, "sub": None}), linking.UNAVAILABLE),
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
    ("HTTPS://MatchAwards.COM/mcp", "profile"),
    (" https://matchawards.com/mcp ", "profile"),
    ("https://matchawards.com/MCP", linking.INVALID),
    ("https://matchawards.com:8443/mcp", linking.INVALID),
    ("http://matchawards.com/mcp", linking.INVALID),
    ("https://matchawards.com/mcp?x=1", linking.INVALID),
    ("https://matchawards.com:x/mcp", linking.INVALID),
    ("https://user@matchawards.com/mcp", linking.INVALID),
    ("https://user:pass@matchawards.com/mcp", linking.INVALID),
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
async def test_a_refusal_landing_during_a_slow_check_keeps_its_profile_out_of_the_cache(issuer, me_api):
    """Two checks of one token in flight: the second is refused (401) while the first, started earlier, waits for
    its 200. The 200 still answers its own call, but is not cached: the next call sees the refusal."""
    first_sent, refused = asyncio.Event(), asyncio.Event()

    async def reply(req):
        if not first_sent.is_set():
            first_sent.set()
            await refused.wait()  # the 200 lands after the 401
            return httpx.Response(200, json=ME)
        return httpx.Response(401)

    me_api.reply = reply
    slow = asyncio.create_task(linking.me(TOKEN))
    await first_sent.wait()
    assert await linking.me(TOKEN) == linking.INVALID
    refused.set()
    assert isinstance(await slow, linking.Profile)
    assert not linking._cache
    assert await linking.me(TOKEN) == linking.INVALID  # from the negative cache, no new request
    assert len(me_api.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("refusal", [lambda req: httpx.Response(401), lambda req: httpx.Response(403),
                                     lambda req: httpx.Response(403, json={"error": "insufficient_scope"}),
                                     lambda req: httpx.Response(200, json={**ME, "resource": "https://x.test/mcp"}),
                                     lambda req: httpx.Response(200, json={**ME, "token_expires_at": "2020-01-01"})])
async def test_a_refusal_drops_a_profile_cached_while_it_was_in_flight(issuer, me_api, refusal):
    """The other order: the 200 lands and is cached first, then the refusal of a check that was already in flight
    drops it."""
    first_sent, cached = asyncio.Event(), asyncio.Event()

    async def reply(req):
        if not first_sent.is_set():
            first_sent.set()
            await cached.wait()
            return refusal(req)
        return httpx.Response(200, json=ME)

    me_api.reply = reply
    slow = asyncio.create_task(linking.me(TOKEN))
    await first_sent.wait()
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    assert linking._cache
    cached.set()
    assert await slow in (linking.INVALID, linking.INSUFFICIENT, linking.FORBIDDEN)
    assert not linking._cache


@pytest.mark.anyio
async def test_a_refusal_dropped_for_room_still_keeps_an_older_200_out_of_the_cache(issuer, me_api, monkeypatch):
    """The refusal of a token in flight is pushed out of _refused by others before its 200 lands: the floor (the
    newest refusal dropped) still keeps the 200 out of the cache. A check started after it caches as usual."""
    monkeypatch.setattr(linking, "CACHE_MAX", 2)
    first_sent, refused = asyncio.Event(), asyncio.Event()

    async def reply(req):
        token = req.headers["authorization"].removeprefix("Bearer ")
        if token == TOKEN and not first_sent.is_set():
            first_sent.set()
            await refused.wait()
            return httpx.Response(200, json=ME)
        return httpx.Response(401) if token.startswith("bad") or token == TOKEN else httpx.Response(200, json=ME)

    me_api.reply = reply
    slow = asyncio.create_task(linking.me(TOKEN))
    await first_sent.wait()
    assert await linking.me(TOKEN) == linking.INVALID
    for token in ("bad1", "bad2"):  # push TOKEN's refusal out of _refused
        await linking.me(token)
    assert linking._key(TOKEN) not in linking._refused
    refused.set()
    assert isinstance(await slow, linking.Profile)  # it answers its own call
    assert linking._key(TOKEN) not in linking._cache
    assert isinstance(await linking.me("good"), linking.Profile) and linking._key("good") in linking._cache


@pytest.mark.anyio
async def test_a_profile_after_a_refusal_clears_it(issuer, me_api, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(linking, "clock", lambda: now[0])
    me_api.reply = lambda req: httpx.Response(403, json={"error": "insufficient_scope"})
    assert await linking.me(TOKEN) == linking.INSUFFICIENT
    me_api.reply = lambda req: httpx.Response(200, json=ME)  # the user granted the scope
    assert isinstance(await linking.me(TOKEN), linking.Profile) and linking._cache
    me_api.reply = lambda req: httpx.Response(401)
    linking._cache.clear()
    assert await linking.me(TOKEN) == linking.INVALID and linking._invalid
    now[0] += 31  # the refusal expired; a profile now replaces it
    me_api.reply = lambda req: httpx.Response(200, json=ME)
    assert isinstance(await linking.me(TOKEN), linking.Profile)
    assert linking._cache and not linking._invalid


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
    ({**ME, "naics": [{"code": "541512"}, {"code": " 541330 ", "title": None}, {"code": "23"}, {"code": "5415121"},
                      {"code": "abc123"}, {"code": 541511}, {"code": "５４１５１１"}, {"title": "no code"}, "541519",
                      None, ["541519"], {"code": "541512", "title": "again"}]},
     {"naics": ("541512", "541330")}),
    ({**ME, "naics": []}, {"naics": (), "naics_truncated": False}),
    ({**ME, "naics_truncated": True}, {"naics_truncated": True}),
    ({**ME, "naics_truncated": "true"}, {"naics_truncated": False}),
    ({k: v for k, v in ME.items() if k != "naics_truncated"}, {"naics_truncated": False}),
    ({**ME, "set_aside": None, "state": None, "uei": None, "organization_name": None, "match_settings_url": None},
     {"set_aside": None, "set_aside_name": None, "state": None, "uei": None, "organization": None,
      "match_settings_url": None}),
    ({**ME, "set_aside": {"code": "SBA,WOSB", "name": "Two"}, "state": "Virginia", "uei": "short",
      "organization_name": "Acme\nLLC\x00"},
     {"set_aside": None, "set_aside_name": None, "state": None, "uei": None, "organization": "Acme LLC"}),
    ({**ME, "set_aside": "SBA"}, {"set_aside": None, "set_aside_name": None}),  # a bare code is not the shape
    ({**ME, "set_aside": {"code": " WOSB "}}, {"set_aside": "WOSB", "set_aside_name": None}),
    ({**ME, "set_aside": {"code": "8A", "name": " 8(a)\nSet-Aside "}},
     {"set_aside": "8A", "set_aside_name": "8(a) Set-Aside"}),
    ({**ME, "organization_name": "  "}, {"organization": None}),
    ({**ME, "organization": "Old field", "organization_name": None}, {"organization": None}),
])
def test_parse_profile_keeps_only_clean_fields(body, expected):
    got = linking.parse_profile(body)
    assert {k: getattr(got, k) for k in expected} == expected


@pytest.mark.parametrize("body", [None, [], {}, {"sub": SUB}, {"naics": []}, {"sub": SUB, "naics": "541512"},
                                  {**ME, "sub": SUB.upper()}, {**ME, "sub": SUB[:31]}, {**ME, "sub": SUB + "0"},
                                  {**ME, "sub": f" {SUB}"}, {**ME, "sub": SUB[:31] + "g"}, {**ME, "sub": 123},
                                  {**ME, "sub": None},
                                  {**{k: v for k, v in ME.items() if k != "sub"}, "account": {"id": "123"}}])
def test_parse_profile_rejects_a_body_that_is_not_a_profile(body):
    assert linking.parse_profile(body) is None


@pytest.mark.parametrize("url, kept", [
    (SETTINGS_URL, True),
    ("https://staging.matchawards.com/settings/match_settings", True),
    ("https://www.matchawards.com/settings/match_settings?tab=naics#codes", True),
    ("https://matchawards.com:443/settings/match_settings", True),
    ("http://matchawards.com/settings/match_settings", False),
    ("https://matchawards.com.evil.example/settings", False),
    ("https://evilmatchawards.com/settings", False),
    ("https://user@matchawards.com/settings", False),
    ("https://matchawards.com:8443/settings", False),
    ("https://matchawards.com:x/settings", False),
    ("javascript:alert(1)", False),
    ("//matchawards.com/settings", False),
    ("https://matchawards.com/a b", False),
    ("https://evil.example\\.matchawards.com/settings", False),  # a browser goes to evil.example
    ("https://evil.example%2f.matchawards.com/settings", False),
    ("https://matchawards.com/settings\\x", False),
    ("https://matchawards.com/\n", False),
    ("https://matchawards.com/" + "a" * 500, False),
    ("", False), (None, False), (["https://matchawards.com/"], False),
])
def test_parse_profile_keeps_only_a_match_settings_url_on_the_site(url, kept):
    assert profile(match_settings_url=url).match_settings_url == (url if kept else None)


@pytest.mark.anyio
@pytest.mark.parametrize("expires, outcome", [
    ("2020-01-01T00:00:00Z", linking.INVALID),
    ("2020-01-01T00:00:00+00:00", linking.INVALID),
    ("2020-01-01T00:00:00", linking.INVALID),  # no zone: UTC
    ("2020-01-01", linking.INVALID),
    ("2099-01-01T00:00:00Z", "profile"),
    ("2099-01-01T00:00:00.123456z", "profile"),
    ("not a time", "profile"),  # unreadable: ignored, as when it is missing
    ("", "profile"), (None, "profile"), (1700000000, "profile"), ("missing", "profile"),
])
async def test_me_refuses_a_token_past_its_expiry(issuer, me_api, expires, outcome):
    body = {k: v for k, v in ME.items() if k != "token_expires_at"} if expires == "missing" \
        else {**ME, "token_expires_at": expires}
    me_api.reply = lambda req: httpx.Response(200, json=body)
    got = await linking.me(TOKEN)
    if outcome == "profile":
        assert isinstance(got, linking.Profile) and linking._cache
    else:
        assert got == outcome and not linking._cache and linking._invalid


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [
    lambda req: httpx.Response(403, json={"error": "forbidden"}),
    lambda req: httpx.Response(403, json={"error": "account_suspended"}),  # an error /me does not name
    lambda req: httpx.Response(403, json={"error": None}),
    lambda req: httpx.Response(403, json=["insufficient_scope"]),
    lambda req: httpx.Response(403, text="Forbidden"),
    lambda req: httpx.Response(403),
])
async def test_me_tells_a_refused_account_from_a_missing_scope(issuer, me_api, reply):
    """403 insufficient_scope is INSUFFICIENT (see above); any other 403 is FORBIDDEN, the safe side: never a
    challenge, and never cached (asked again next time)."""
    me_api.reply = reply
    assert await linking.me(TOKEN) == linking.FORBIDDEN
    assert await linking.me(TOKEN) == linking.FORBIDDEN
    assert not linking._cache and not linking._invalid
    assert len(me_api.requests) == 2


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
        return Seen(account=got.sub)
    if got == linking.UNAVAILABLE:
        raise ToolError("MatchAwards is busy right now. Retry in a minute.")
    if got == linking.FORBIDDEN:
        raise ToolError("This account can't use it.")
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
            assert not result.get("isError") and result["structuredContent"] == {"account": SUB}
    assert len(me_api.requests) == 1  # the second call used the cache


@pytest.mark.anyio
@pytest.mark.parametrize("ua", [OPENAI_UA, "OpenAI-MCP/2.0 (test)"])
@pytest.mark.parametrize("token, reply, error", [
    (None, None, "invalid_token"),
    (TOKEN, lambda req: httpx.Response(401), "invalid_token"),
    (TOKEN, lambda req: httpx.Response(403, json={"error": "insufficient_scope"}), "insufficient_scope"),
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
@pytest.mark.parametrize("ua", [CLAUDE_UA, OPENAI_UA, "curl/8.0"])
@pytest.mark.parametrize("error", ["forbidden", "something_new"])
async def test_a_refused_account_is_never_a_challenge(with_tool, me_api, ua, error):
    """A 403 other than insufficient_scope (a disabled or suspended user, a token of an app that is not an agent app)
    goes on to the tool like a check without an answer: linking again would not help."""
    me_api.reply = lambda req: httpx.Response(403, json={"error": error})
    usage.metrics.counts.clear()
    async with http() as hc:
        r = await call(hc, ua=ua, token=TOKEN)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True and "_meta" not in result
    assert "can't use it" in result["content"][0]["text"]
    assert not any(name == "mcp_link_challenges_total" for name, _ in usage.metrics.counts)


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
        return httpx.Response(200, json={**ME, "sub": token[-1] * 32})

    me_api.reply = reply
    tokens = [f"tok{i % 4}" for i in range(24)]
    async with http() as hc:
        responses = await asyncio.gather(*(call(hc, token=t) for t in tokens))
    for t, r in zip(tokens, responses):
        assert r.json()["result"]["structuredContent"] == {"account": t[-1] * 32}, t
    assert len(me_api.requests) >= 4


@pytest.mark.anyio
async def test_the_token_never_reaches_a_log_or_a_response(with_tool, me_api, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    async with http() as hc:
        for ua in (CLAUDE_UA, OPENAI_UA):
            for reply in (lambda req: httpx.Response(401), lambda req: httpx.Response(403),
                          lambda req: httpx.Response(403, json={"error": "insufficient_scope"}),
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
