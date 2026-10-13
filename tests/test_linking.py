"""Account linking: settings, the protected-resource metadata, the /me token check and its cache. No network."""

import json
import logging
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest

from matchawards_mcp import linking, server
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
      "set_aside": "SBA", "state": "va", "uei": "ABCDEF123456"}


def profile(account_id="123", **kw):
    return linking.parse_profile({**ME, "account": {"id": account_id, "username": "acme"}, **kw})


@pytest.fixture
def me_api(monkeypatch):
    """/me on a mock: me_api.reply(request) answers every call (a profile by default); me_api.requests logs them.
    The profile cache starts empty."""
    me_api = SimpleNamespace(requests=[], reply=lambda req: httpx.Response(200, json=ME))

    async def handler(req):
        me_api.requests.append(req)
        return me_api.reply(req)

    monkeypatch.setattr(linking, "client", httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=dict(linking.client.headers), cookies=linking.client.cookies.jar,
    ))
    monkeypatch.setattr(linking, "_cache", OrderedDict())
    return me_api


@pytest.fixture
def linked(monkeypatch):
    """Linking on (MATCHAWARDS_AUTH_ISSUER=https://matchawards.com), with the server rebuilt for it."""
    monkeypatch.setattr(linking, "ISSUER", ISSUER)
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
        init = await hc.post("/mcp", headers=LEGACY, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        for path in PATHS:
            for method in ("GET", "HEAD", "POST"):
                r = await hc.request(method, path)
                assert r.status_code == 404, (method, path)
        await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "lookup_naics", "arguments": {"query": "roofing"}}}, headers={**LEGACY, "Authorization": f"Bearer {TOKEN}"})
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


@pytest.mark.parametrize("name, value", [
    ("ISSUER", "matchawards.com"), ("ISSUER", "ftp://matchawards.com"), ("RESOURCE", "https://x.test/mcp?a=1"),
    ("AGENT_API_BASE", "not a url"),
])
def test_bad_settings_stop_startup(monkeypatch, name, value):
    monkeypatch.setattr(linking, "ISSUER", ISSUER)
    monkeypatch.setattr(linking, name, value)
    with pytest.raises(SystemExit, match="must be an http"):
        server.http_app()


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
async def test_me_asks_with_the_token_and_returns_the_profile(me_api):
    got = await linking.me(TOKEN)
    assert got == linking.Profile(account_id="123", username="acme", organization="Acme LLC",
                                  naics=("541512", "541511"), set_aside="SBA", state="VA", uei="ABCDEF123456")
    [req] = me_api.requests
    assert str(req.url) == "https://matchawards.com/api/agent/v1/me"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["user-agent"] == ma_api.USER_AGENT
    assert TOKEN not in repr(got)


@pytest.mark.anyio
async def test_me_uses_the_agent_api_base(me_api, monkeypatch):
    monkeypatch.setattr(linking, "AGENT_API_BASE", "http://agent-api.test:3000")
    await linking.me(TOKEN)
    assert str(me_api.requests[0].url) == "http://agent-api.test:3000/api/agent/v1/me"


@pytest.mark.anyio
async def test_me_caches_a_profile_until_it_expires(me_api, monkeypatch):
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
async def test_me_cache_is_bounded_least_recently_used_first(me_api, monkeypatch):
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
    (lambda req: httpx.Response(401, json={"error": "invalid_token"}), linking.INVALID),
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
async def test_me_failures_are_told_apart_and_never_cached(me_api, reply, outcome):
    me_api.reply = reply
    assert await linking.me(TOKEN) == outcome
    assert await linking.me(TOKEN) == outcome
    assert len(me_api.requests) == 2 and not linking._cache


@pytest.mark.anyio
async def test_me_follows_no_redirect_and_keeps_no_cookie(me_api):
    me_api.reply = lambda req: httpx.Response(200, json=ME, headers={"Set-Cookie": "_session=abc; Path=/"})
    await linking.me("a")
    await linking.me("b")
    assert "cookie" not in me_api.requests[1].headers
    assert not linking.client.follow_redirects


@pytest.mark.anyio
async def test_me_spends_the_server_wide_api_budget(me_api, monkeypatch):
    monkeypatch.setattr(ma_api, "api_budget", RateLimiter(1, 0))
    assert isinstance(await linking.me("a"), linking.Profile)
    assert await linking.me("b") == linking.UNAVAILABLE  # budget spent: no request made
    assert await linking.me("a") == profile()  # a cached profile needs no request, so no budget
    assert len(me_api.requests) == 1


@pytest.mark.anyio
async def test_me_never_logs_the_token(me_api, caplog):
    caplog.set_level(logging.DEBUG)
    for reply in (lambda req: httpx.Response(200, json=ME), lambda req: httpx.Response(401),
                  lambda req: httpx.Response(500), _raise(httpx.ConnectError(f"refused Bearer {TOKEN}")),
                  lambda req: httpx.Response(200, text="junk")):
        me_api.reply = reply
        linking._cache.clear()
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
