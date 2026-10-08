"""--http mode: the SDK's ASGI app driven in-process over httpx2.ASGITransport. No network."""

import contextlib
import logging

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from matchawards_mcp import __version__, server

from test_server import TOOLS, api, anyio_backend, page  # noqa: F401  (fixtures)

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
LEGACY = {"MCP-Protocol-Version": "2025-06-18", "Accept": "application/json, text/event-stream"}


@contextlib.asynccontextmanager
async def http(host="matchawards.com", headers=None):
    """A fresh app (the SDK's session manager runs once per app) with its lifespan running."""
    app = server.http_app()
    async with app.router.lifespan_context(app):
        yield httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=f"http://{host}", headers=headers or {}
        )


def ping(hc, ip=None):
    return hc.post("/mcp", json=PING, headers={**LEGACY, **({"X-Real-IP": ip} if ip else {})})


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
        assert (await ping(hc, "2001:db8:1:3::1")).status_code == 200  # next /64


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
    ([(b"x-real-ip", b"2001:db8:aa:bb:1:2:3:4")], None, "2001:db8:aa:bb::/64"),
    ([(b"x-real-ip", b"::ffff:203.0.113.9")], None, "203.0.113.9"),
    ([(b"x-real-ip", b"not-an-ip")], ("10.0.0.5", 5), "10.0.0.5"),
    ([], ("10.0.0.5", 5), "10.0.0.5"),
    ([], None, "unknown"),
])
def test_client_key(headers, peer, key):
    assert server.client_key({"headers": headers, "client": peer}) == key


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
    assert (calls[1][1]["host"], calls[1][1]["port"], calls[1][1]["access_log"]) == ("127.0.0.1", 9999, False)
