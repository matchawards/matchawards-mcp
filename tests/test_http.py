"""--http mode: the SDK's ASGI app driven in-process over httpx2.ASGITransport. No network."""

import contextlib
from types import SimpleNamespace
import ipaddress
import logging

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from matchawards_mcp import __version__, server

from test_server import TOOLS, api, anyio_backend, page  # noqa: F401  (fixtures)

TRUSTED = [ipaddress.ip_network(n) for n in ("127.0.0.1/32", "::1/128", "172.16.0.0/12")]
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
LEGACY = {"MCP-Protocol-Version": "2025-06-18", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def no_api_budget_leak(monkeypatch):
    """http_app() installs a server-wide API budget; put the stdio default (None) back after each test."""
    monkeypatch.setattr(server, "api_budget", None)


@contextlib.asynccontextmanager
async def http(host="matchawards.com", headers=None):
    """A fresh app (the SDK's session manager runs once per app) with its lifespan running."""
    app = server.http_app()
    async with app.router.lifespan_context(app):
        yield httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=f"http://{host}", headers=headers or {}
        )


def ping(hc, ip=None, path="/mcp"):
    return hc.post(path, json=PING, headers={**LEGACY, **({"X-Real-IP": ip} if ip else {})})


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])  # auto = 2026-07-28 server/discover; legacy = initialize
async def test_handshake_list_and_call_over_http(api, mode):
    proxy_headers = {
        "X-Real-IP": "203.0.113.7", "X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "203.0.113.7",
        "Authorization": "Bearer secret",
    }
    api.replies = [lambda req: httpx.Response(200, json=page([1, 2]))]
    async with http(headers=proxy_headers) as hc:
        async with Client(streamable_http_client("http://matchawards.com/mcp", http_client=hc), mode=mode) as client:
            assert (client.server_info.name, client.server_info.version) == ("matchawards", __version__)
            if mode == "auto":
                assert client.protocol_version == "2026-07-28"
            assert [t.name for t in (await client.list_tools()).tools] == TOOLS
            out = await client.call_tool("search_contracts", {"state": "VA", "limit": 2})
            assert not out.is_error
            assert [r["id"] for r in out.structured_content["results"]] == ["1", "2"]
    [req] = api.requests
    assert req.url.params["state"] == "VA"
    # The API must see this server's own address, never the end user's: nothing from the inbound request leaks.
    for name in ("x-real-ip", "x-forwarded-for", "cf-connecting-ip", "authorization"):
        assert name not in req.headers


@pytest.mark.anyio
async def test_get_and_delete_are_405_and_healthz_is_ok():
    async with http() as hc:
        for method in ("GET", "DELETE"):
            r = await hc.request(method, "/mcp", headers={"Accept": "text/event-stream"})
            assert (r.status_code, r.headers["Allow"]) == (405, "POST")
        r = await hc.get("/healthz")
        assert (r.status_code, r.text) == (200, "ok")


@pytest.mark.anyio
async def test_unknown_host_is_rejected_and_local_hosts_work(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_ALLOWED_HOSTS", "matchawards.com")
    async with http(host="evil.example") as hc:
        assert (await ping(hc)).status_code == 421
    for host in ("staging.matchawards.com", "attacker.matchawards.com.evil"):
        async with http(host=host) as hc:
            assert (await ping(hc)).status_code == 421
    for host in ("matchawards.com", "127.0.0.1:8765", "localhost:8765"):
        async with http(host=host) as hc:
            assert (await ping(hc)).status_code == 200


@pytest.mark.anyio
async def test_rate_limit_per_real_ip_and_ipv6_per_64(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    async with http() as hc:
        assert [(await ping(hc, "198.51.100.1")).status_code for _ in range(3)] == [200] * 3
        r = await ping(hc, "198.51.100.1")
        assert r.status_code == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 5
        body = r.json()
        assert (body["jsonrpc"], body["id"], body["error"]["code"]) == ("2.0", None, -32000)
        assert "Wait" in body["error"]["message"]
        assert (await ping(hc, "198.51.100.2")).status_code == 200  # another client has its own budget
        assert (await ping(hc)).status_code == 200  # no X-Real-IP: keyed on the socket peer

        for ip in ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:2:ffff::9"):  # one /64
            assert (await ping(hc, ip)).status_code == 200
        assert (await ping(hc, "2001:db8:1:2::3")).status_code == 429
        assert (await ping(hc, "2001:db8:1:3::1")).status_code == 200  # next /64, same /48: its own limit
        assert (await ping(hc, "2001:db8:2::1")).status_code == 200  # another /48


def test_rate_limiter_windows_and_cleanup():
    now = [1000.0]
    rl = server.RateLimiter(per_min=5, burst=3, clock=lambda: now[0])
    assert [rl.check("a") for _ in range(3)] == [0, 0, 0]
    assert rl.check("a") == 5  # burst: 3 within 5 s
    now[0] += 5
    assert [rl.check("a") for _ in range(2)] == [0, 0]
    assert rl.check("a") == 55  # 5 per minute; the oldest falls out at 1060
    now[0] += 55
    assert rl.check("a") == 0
    rl.check("b")
    now[0] += 61
    rl.check("c")  # the once-a-minute sweep forgets idle clients
    assert set(rl.hits) == {"c"}


@pytest.mark.parametrize("headers, peer, key", [
    ([(b"x-real-ip", b"203.0.113.9")], ("127.0.0.1", 5), "203.0.113.9"),
    ([(b"x-real-ip", b"2001:db8:aa:bb:1:2:3:4")], ("127.0.0.1", 5), "2001:db8:aa:bb::/64"),
    ([(b"x-real-ip", b"::ffff:203.0.113.9")], ("127.0.0.1", 5), "203.0.113.9"),
    ([(b"x-real-ip", b"not-an-ip")], ("10.0.0.5", 5), "10.0.0.5"),
    ([], ("10.0.0.5", 5), "10.0.0.5"),
    ([], None, "unknown"),
    ([(b"x-real-ip", b"203.0.113.9")], None, "unknown"),  # no peer, so nothing to trust the header from
])
def test_client_key(headers, peer, key):
    assert server.client_key({"headers": headers, "client": peer}, TRUSTED) == key


@pytest.mark.parametrize("peer, key", [
    (("203.0.113.66", 5), "203.0.113.66"),  # untrusted peer: forged X-Real-IP ignored
    (("2001:db8::7", 5), "2001:db8::/64"),
    (("127.0.0.1", 5), "198.51.100.77"),  # trusted proxies: X-Real-IP used
    (("172.18.0.1", 5), "198.51.100.77"),
    (("::1", 5), "198.51.100.77"),
])
def test_x_real_ip_is_used_only_from_trusted_proxies(peer, key):
    scope = {"headers": [(b"x-real-ip", b"198.51.100.77")], "client": peer}
    assert server.client_key(scope, TRUSTED) == key


@pytest.mark.anyio
async def test_forged_x_real_ip_from_untrusted_peer_cannot_dodge_the_limit(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    app = server.http_app()
    async with app.router.lifespan_context(app):
        hc = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app, client=("203.0.113.66", 5)),
                                base_url="http://matchawards.com")
        codes = [(await ping(hc, f"198.51.100.{i}")).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]  # every request keyed on the peer, whatever X-Real-IP says


def test_main_parses_http_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(("stdio", kw)))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(("http", kw)))
    monkeypatch.setenv("MATCHAWARDS_HTTP_PORT", "9999")
    for logger in (server.access_log, logging.getLogger()):  # main() configures logging; undo it after the test
        for attr in ("handlers", "level", "propagate"):
            monkeypatch.setattr(logger, attr, getattr(logger, attr) if attr != "handlers" else [])
    server.main([])
    server.main(["--http"])
    assert calls[0] == ("stdio", {"transport": "stdio"})
    assert calls[1][0] == "http"
    kw = calls[1][1]
    assert (kw["host"], kw["port"], kw["access_log"], kw["proxy_headers"]) == ("127.0.0.1", 9999, False, False)
    assert (kw["limit_concurrency"], kw["timeout_keep_alive"]) == (100, 5)


@pytest.mark.anyio
async def test_access_log_has_no_raw_client_values(caplog):
    caplog.set_level(logging.INFO, logger="matchawards_mcp.access")
    async with http() as hc:
        await hc.get("/mcp%0d%0aFAKE 200 0ms client=forged", headers={"X-Real-IP": "203.0.113.50"})
        await ping(hc, "203.0.113.50")
        await hc.post("/mcp", json=PING, headers={**LEGACY, "Authorization": "Bearer secret"})
    lines = [r.getMessage() for r in caplog.records if r.name == "matchawards_mcp.access"]
    assert len(lines) == 3
    assert lines[0].startswith("GET OTHER 404 ")
    assert lines[1].startswith("POST /mcp 200 ")
    text = "\n".join(lines)
    for raw in ("\r", "FAKE", "forged", "203.0.113.50", "secret", "ping"):
        assert raw not in text


@pytest.mark.anyio
async def test_garbage_real_ip_falls_back_to_the_peer_not_a_new_key(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    async with http() as hc:
        # Three different garbage values all count against the socket peer, so they cannot mint fresh budgets.
        assert [(await ping(hc, junk)).status_code for junk in ("x", "1.2.3.4.5", "evil\tvalue")] == [200] * 3
        assert (await ping(hc)).status_code == 429
        assert (await ping(hc, "198.51.100.9")).status_code == 200  # a valid address is still its own client


def test_rate_limiter_key_count_is_bounded():
    rl = server.RateLimiter(per_min=5, burst=3, clock=lambda: 1000.0, max_keys=100)
    for i in range(10_000):
        rl.check(f"10.0.{i // 256}.{i % 256}")
        rl.check("keep")  # seen on every round, so it is never the least recently seen
    assert len(rl.hits) == 100
    assert "keep" in rl.hits and "10.0.39.15" in rl.hits and "10.0.0.0" not in rl.hits


@pytest.mark.anyio
async def test_global_cap_applies_to_everyone(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_PER_MIN", "5")
    async with http() as hc:
        assert [(await ping(hc, f"198.51.100.{i}")).status_code for i in range(5)] == [200] * 5
        r = await ping(hc, "198.51.100.200")  # a fresh client, but the server-wide minute is used up
        assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60


@pytest.mark.anyio
async def test_requests_rejected_per_client_do_not_use_the_global_budget(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_PER_MIN", "5")
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    async with http() as hc:
        codes = [(await ping(hc, "198.51.100.1")).status_code for _ in range(10)]
        assert codes == [200] * 3 + [429] * 7
        assert [(await ping(hc, f"198.51.100.{i}")).status_code for i in (2, 3, 4)] == [200, 200, 429]


@pytest.mark.anyio
async def test_paths_are_normalised_before_the_guard(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    async with http() as hc:
        assert [(await ping(hc, path=p)).status_code for p in ("/mcp", "/mcp/", "http://matchawards.com//mcp")] == [200] * 3
        assert (await ping(hc, path="http://matchawards.com//mcp//")).status_code == 429  # one budget for every spelling of /mcp
        assert (await hc.get("/mcp/")).status_code == 405
        assert (await hc.get("/other")).status_code == 404
        assert (await ping(hc, path="/other")).status_code == 404
        assert (await hc.get("/healthz/")).text == "ok"


@pytest.mark.anyio
async def test_healthz_is_unlimited_and_options_is_405(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "1")
    async with http() as hc:
        assert [(await hc.get("/healthz")).status_code for _ in range(5)] == [200] * 5
        r = await hc.options("/mcp")
        assert (r.status_code, r.headers["Allow"]) == (405, "POST")


@pytest.mark.anyio
async def test_wrong_host_is_421_on_any_method(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_ALLOWED_HOSTS", "matchawards.com")
    async with http(host="evil.example") as hc:
        for method in ("GET", "DELETE", "OPTIONS", "POST"):
            assert (await hc.request(method, "/mcp", json=PING)).status_code == 421


@pytest.mark.anyio
async def test_body_size_limits():
    async with http() as hc:
        big = {**PING, "params": {"pad": "x" * (64 * 1024)}}
        assert (await hc.post("/mcp", json=big, headers=LEGACY)).status_code == 413

        async def chunked():
            yield b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

        r = await hc.post("/mcp", content=chunked(), headers={**LEGACY, "Content-Type": "application/json"})
        assert r.status_code == 411
        assert (await ping(hc)).status_code == 200


@pytest.mark.anyio
async def test_a_lying_content_length_is_still_capped_by_the_sdk():
    app = server.http_app()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * (65 * 1024), "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("matchawards.com", 80), "client": ("127.0.0.1", 5),
        "http_version": "1.1", "asgi": {"version": "3.0"},
        "headers": [(b"host", b"matchawards.com"), (b"content-type", b"application/json"),
                    (b"content-length", b"40"), (b"mcp-protocol-version", b"2025-06-18"),
                    (b"accept", b"application/json, text/event-stream")],
    }
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_one_client_hammering_leaves_the_global_budget_open(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_PER_MIN", "11")  # default burst 10: the abuser gets 10 through
    async with http() as hc:
        codes = [(await ping(hc, "198.51.100.66")).status_code for _ in range(1000)]
        assert codes.count(200) == 10 and codes.count(429) == 990
        assert (await ping(hc, "198.51.100.67")).status_code == 200  # 990 rejections never touched the global count
        assert (await ping(hc, "198.51.100.68")).status_code == 429  # and the 11-per-minute global cap holds


@pytest.mark.parametrize("name, value", [
    ("MATCHAWARDS_RATE_PER_MIN", "0"), ("MATCHAWARDS_RATE_BURST", "ten"), ("MATCHAWARDS_GLOBAL_PER_MIN", "-5"),
    ("MATCHAWARDS_RATE_PER_MIN", ""), ("MATCHAWARDS_TRUSTED_PROXIES", "10.0.0.0/33"), ("MATCHAWARDS_RATE_PER_PREFIX_MIN", "0"),
    ("MATCHAWARDS_GLOBAL_API_PER_MIN", "x"),
])
def test_bad_env_stops_startup_with_a_clear_message(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=name):
        server.http_app()


@pytest.mark.anyio
async def test_rotating_64s_inside_a_56_is_capped_by_the_48_limit(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_PER_MIN", "150")
    async with http() as hc:
        codes = [(await ping(hc, f"2001:db8:0:ab{i % 256:02x}::{i:x}")).status_code for i in range(300)]
        assert codes.count(200) == 120  # MATCHAWARDS_RATE_PER_PREFIX_MIN, not 256 fresh /64 budgets
        assert (await ping(hc, "2001:db8:0:ab00::1")).status_code == 429  # the /48 is used up for this minute
        # One /48 cannot use up the global cap: other sources still get through.
        assert (await ping(hc, "198.51.100.1")).status_code == 200
        assert (await ping(hc, "2001:db8:5::1")).status_code == 200


@pytest.mark.anyio
async def test_64s_in_different_48s_are_independent(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    monkeypatch.setenv("MATCHAWARDS_RATE_PER_PREFIX_MIN", "4")
    async with http() as hc:
        assert [(await ping(hc, "2001:db8:1:1::1")).status_code for _ in range(4)] == [200, 200, 200, 429]
        assert (await ping(hc, "2001:db8:2:1::1")).status_code == 200  # another /48: its own budgets
        assert (await ping(hc, "2001:db8:1:2::1")).status_code == 200  # same /48, another /64: 4th of 4
        assert (await ping(hc, "2001:db8:1:3::1")).status_code == 429  # the /48 is full


def test_prefix_key():
    assert server.prefix_key("2001:db8:1:2::/64") == "2001:db8:1::/48"
    assert server.prefix_key("198.51.100.1") is None and server.prefix_key("unknown") is None


@pytest.mark.anyio
async def test_a_global_lockout_does_not_use_up_a_clients_own_budget(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_PER_MIN", "3")
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "3")
    now = [1000.0]
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: now[0]))  # this module's clock only
    async with http() as hc:
        assert [(await ping(hc, f"198.51.100.{i}")).status_code for i in (1, 2, 3)] == [200] * 3
        assert [(await ping(hc, "198.51.100.9")).status_code for _ in range(5)] == [429] * 5  # global lockout
        now[0] += 61  # the global minute passes, the per-client 5 s burst window too
        # .9 was never counted while locked out, so it has its full burst of 3
        assert [(await ping(hc, "198.51.100.9")).status_code for _ in range(4)] == [200, 200, 200, 429]


@pytest.mark.anyio
async def test_api_budget_in_http_mode(api, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_API_PER_MIN", "2")
    api.replies = [lambda req: httpx.Response(200, json=page([len(api.requests)], has_more=True, cursor="c"))]
    async with http() as hc:
        async with Client(streamable_http_client("http://matchawards.com/mcp", http_client=hc)) as client:
            out = await client.call_tool("search_contracts", {"limit": 5})  # wants 3 pages, gets 2
            assert "MatchAwards is busy right now. Retry in" in out.structured_content["warning"]
            assert len(out.structured_content["results"]) == 2
            busy = await client.call_tool("get_opportunity", {"id": "7"})
            assert busy.is_error and "MatchAwards is busy right now. Retry in" in busy.content[0].text
    assert len(api.requests) == 2  # no call past the budget


@pytest.mark.anyio
async def test_stdio_mode_has_no_api_budget(api):
    assert server.api_budget is None
    for _ in range(5):
        await server.get_opportunity("7")
    assert len(api.requests) == 5
