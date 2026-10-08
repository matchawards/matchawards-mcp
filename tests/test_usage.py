"""Usage stats in --http mode: the ledger, the transport aggregate, /metrics and the stats command. No network."""

import json
import logging
import sqlite3
import sys

import httpx
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from matchawards_mcp import server, usage

from test_http import LEGACY, http
from test_server import api, anyio_backend, page  # noqa: F401  (fixtures)

SECRET = "zebracornsecret"  # an argument value that must never be stored


@pytest.fixture(autouse=True)
def usage_db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("MATCHAWARDS_USAGE_DB", str(path))
    monkeypatch.setattr(server, "api_budget", None)
    monkeypatch.setattr(server, "ledger", None)
    monkeypatch.setattr(usage, "metrics", usage.Metrics())
    return path


def rows(path, table="tool_calls"):
    server.ledger.flush()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def call(name, args, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}}


def post(hc, body, ip="203.0.113.7", **headers):
    return hc.post("/mcp", content=json.dumps(body), headers={
        **LEGACY, "Content-Type": "application/json", "X-Real-IP": ip, **headers})


@pytest.mark.anyio
async def test_ledger_rows_for_ok_and_error_outcomes_without_arguments(api, usage_db):
    api.replies = [
        lambda req: httpx.Response(200, json=page([1])),
        lambda req: httpx.Response(404, json={"error": "not_found"}),
        lambda req: httpx.Response(500, json={"error": "internal_error"}),
        lambda req: httpx.Response(429, headers={"Retry-After": "3"}),
    ]
    async with http(headers={"User-Agent": "openai-mcp/1.0.0", "X-Real-IP": "203.0.113.7"}) as hc:
        async with Client(streamable_http_client("http://matchawards.com/mcp", http_client=hc)) as client:
            ok = await client.call_tool("search_contracts", {"keyword": SECRET, "state": "VA"})
            assert not ok.is_error
            for _ in range(3):
                assert (await client.call_tool("get_opportunity", {"id": SECRET})).is_error
    got = rows(usage_db)
    assert sorted((r["tool"], r["outcome"]) for r in got) == [  # two writer threads: rows land in any order
        ("get_opportunity", "invalid_input"), ("get_opportunity", "rate_limited_upstream"),
        ("get_opportunity", "upstream_error"), ("search_contracts", "ok"),
    ]
    [r] = [r for r in got if r["outcome"] == "ok"]
    assert (r["client_key"], r["ua"], r["family"], r["caller_fp"]) == (
        "203.0.113.0/24", "openai-mcp/1.0.0", "openai-mcp", None)
    assert r["latency_ms"] >= 0
    server.ledger.flush()
    raw = b"".join(p.read_bytes() for p in usage_db.parent.glob("usage.db*"))
    assert SECRET.encode() not in raw and b"203.0.113.7" not in raw
    text = usage.metrics.render()
    assert 'mcp_tool_calls_total{outcome="ok",tool="search_contracts"} 1' in text
    assert 'mcp_agent_requests_total{family="openai-mcp"}' in text
    assert 'mcp_requests_total{method="tools/call"} 4' in text


@pytest.mark.anyio
async def test_busy_and_internal_error_outcomes(api, usage_db, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_GLOBAL_API_PER_MIN", "1")
    async with http() as hc:
        assert (await post(hc, call("get_opportunity", {"id": "1"}))).status_code == 200
        assert (await post(hc, call("get_opportunity", {"id": "2"}))).status_code == 200  # API budget used up
        monkeypatch.setattr(server, "_get_one", lambda id: 1 / 0)
        await post(hc, call("find_contacts", {"id": "3"}))
    assert sorted(r["outcome"] for r in rows(usage_db)) == ["busy", "internal_error", "ok"]


@pytest.mark.anyio
async def test_rate_limited_calls_leave_no_row(api, usage_db, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "2")
    async with http() as hc:
        codes = [(await post(hc, call("get_opportunity", {"id": "7"}))).status_code for _ in range(5)]
    assert codes == [200, 200, 429, 429, 429]
    assert len(rows(usage_db)) == 2
    assert len(rows(usage_db, "transport_daily")) == 1
    assert 'mcp_rejected_total{reason="rate_limit_client"} 3' in usage.metrics.render()


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "acme-agent", "version": "1.2"}}}


@pytest.mark.anyio
async def test_client_info_from_initialize_and_meta_and_the_8k_window(usage_db):
    modern = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {
        usage.CLIENT_INFO_META_KEY: {"name": "new-agent", "version": "2.0"}}}}
    padded = {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {
        "pad": "x" * 9000, "clientInfo": {"name": "hidden", "version": "9"}}}
    async with http() as hc:
        await post(hc, INIT)
        await post(hc, INIT)
        await post(hc, modern)
        await post(hc, padded)  # clientInfo sits past the first 8 KiB, so it is never read
    got = {(r["method"], r["client_name"], r["client_version"]): r["count"] for r in rows(usage_db, "transport_daily")}
    assert got == {("initialize", "acme-agent", "1.2"): 2, ("tools/list", "new-agent", "2.0"): 1, ("_other", "", ""): 1}


def test_parse_body():
    assert usage.parse_body(json.dumps(INIT).encode()) == ("initialize", "acme-agent", "1.2")
    assert usage.parse_body(b'{"method": "evil/invented"}') == ("_other", "", "")
    assert usage.parse_body(b'{"method": "tools/call", "params": {"clientInfo": {"name": "x"}}}') == (
        "tools/call", "", "")  # clientInfo is read from initialize only (or _meta)
    assert usage.parse_body(b"[1]") == usage.parse_body(b"\xff") == usage.parse_body(b"") == ("_other", "", "")
    assert usage.parse_body(b'{"method": "initialize", "params": {"clientInfo": {"name": "a\\u001b[2Jb"}}}')[1] == "a?[2Jb"
    assert {"initialize", "tools/list", "tools/call", "ping", "server/discover"} <= usage.KNOWN_METHODS


def test_bucket_cap_per_client_and_day_then_other(tmp_path):
    ledger = usage.Ledger.open(str(tmp_path / "u.db"))
    conn = sqlite3.connect(ledger.path)
    for i in range(60):
        usage.Ledger.write_transport(conn, "198.51.100.0/24", "initialize", f"name-{i}", "1", "ua")
    usage.Ledger.write_transport(conn, "198.51.100.0/24", "initialize", "name-3", "1", "ua")  # existing bucket
    usage.Ledger.write_transport(conn, "198.51.101.0/24", "initialize", "name-59", "1", "ua")  # another client
    got = conn.execute("SELECT client_key, method, client_name, count FROM transport_daily").fetchall()
    assert len([r for r in got if r[0] == "198.51.100.0/24"]) == 51  # 50 buckets + the _other fold
    assert ("198.51.100.0/24", "_other", "", 10) in got
    assert ("198.51.100.0/24", "initialize", "name-3", 2) in got
    assert ("198.51.101.0/24", "initialize", "name-59", 1) in got


@pytest.mark.parametrize("ua, family", [
    ("openai-mcp/1.0.0", "openai-mcp"),
    ("Mozilla/5.0 (compatible; Claude-User/1.0)", "claude-user"),
    ("ClaudeBot/1.0", "claudebot"),
    ("claude-code/2.1.0", "claude"),
    ("ChatGPT-User/1.0", "chatgpt-user"),
    ("Perplexity-User/1.0", "perplexity"),
    ("Cursor/1.4", "cursor"),
    ("vscode/1.99", "vscode"),
    ("matchawards-mcp/0.3.0", "matchawards-mcp"),
    ("matchawards-cli/1.0", "matchawards-cli"),
    ("n8n-nodes-matchawards/0.1", "n8n-nodes-matchawards"),
    ("matchawards-test/1", "matchawards-test"),
    ("x402-census-probe/1", "prober"),
    ("UptimeHealthCheck", "prober"),
    ("mcp-registry-scout", "prober"),
    ("AgentReadinessScanner", "prober"),
    ("curl/8.7.1", "other"),
    ("python-httpx/0.28.1", "other"),
    ("node", "other"),
    ("", "other"),
])
def test_ua_classifier(ua, family):
    assert usage.classify_ua(ua) == family


@pytest.mark.parametrize("salt", ["s3cret", None])
@pytest.mark.anyio
async def test_chatgpt_fingerprint_is_hmac_and_needs_salt(api, usage_db, monkeypatch, salt):
    if salt:
        monkeypatch.setenv("MATCHAWARDS_USAGE_SALT", salt)
    async with http() as hc:
        await post(hc, call("get_opportunity", {"id": "7"}), **{"x-openai-subject": "raw-subject-value"})
        await post(hc, call("get_opportunity", {"id": "7"}), **{"x-openai-session": "raw-session-value"})
    fps = [r["caller_fp"] for r in rows(usage_db)]  # any order
    server.ledger.flush()
    raw = b"".join(p.read_bytes() for p in usage_db.parent.glob("usage.db*"))
    assert b"raw-subject-value" not in raw and b"raw-session-value" not in raw
    if salt:
        assert all(len(f) == 16 and int(f, 16) >= 0 for f in fps) and fps[0] != fps[1]
    else:
        assert fps == [None, None]


@pytest.mark.anyio
async def test_metrics_is_not_on_the_mcp_app(monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_METRICS_TOKEN", "tok")
    async with http() as hc:
        for headers in ({}, {"Authorization": "Bearer tok"}):
            assert (await hc.get("/metrics", headers=headers)).status_code == 404


@pytest.mark.anyio
async def test_metrics_listener_needs_the_bearer_token():
    usage.metrics.inc("mcp_rejected_total", reason="host")
    hc = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=server.metrics_app("tok")), base_url="http://10.0.0.5:9765")
    assert (await hc.get("/metrics")).status_code == 401
    assert (await hc.get("/metrics", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await hc.get("/metrics", headers={"Authorization": "Bearer tok", "X-Real-IP": "1.2.3.4"})).status_code == 200
    r = await hc.get("/metrics", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    for name in ("mcp_tool_calls_total", "mcp_requests_total", "mcp_rejected_total", "mcp_agent_requests_total"):
        assert f"# TYPE {name} counter" in r.text
    assert 'mcp_rejected_total{reason="host"} 1' in r.text and "mcp_ledger_errors_total 0" in r.text
    assert (await hc.get("/mcp", headers={"Authorization": "Bearer tok"})).status_code == 404


def test_main_starts_the_metrics_listener_only_with_a_token(monkeypatch):
    import uvicorn

    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append("mcp only"))
    monkeypatch.setattr(server.asyncio, "run", lambda coro: (ran.append("both"), coro.close()))
    for logger in (server.access_log, logging.getLogger()):  # main() configures logging; undo it after the test
        for attr in ("handlers", "level", "propagate"):
            monkeypatch.setattr(logger, attr, getattr(logger, attr) if attr != "handlers" else [])
    server.main(["--http"])
    monkeypatch.setenv("MATCHAWARDS_METRICS_TOKEN", "tok")
    server.main(["--http"])
    assert ran == ["mcp only", "both"]


@pytest.mark.anyio
async def test_unwritable_db_disables_the_ledger_without_failing_requests(api, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MATCHAWARDS_USAGE_DB", str(tmp_path / "missing" / "usage.db"))
    async with http() as hc:
        assert server.ledger is None
        assert (await post(hc, call("get_opportunity", {"id": "7"}))).json()["result"]["isError"] is False
    assert "usage ledger disabled" in caplog.text
    assert not (tmp_path / "missing").exists()


@pytest.mark.anyio
async def test_a_failing_ledger_write_is_counted_not_raised(api, usage_db, tmp_path):
    async with http() as hc:
        server.ledger.path = str(tmp_path)  # a directory: every write now fails
        assert (await post(hc, call("get_opportunity", {"id": "7"}))).json()["result"]["isError"] is False
        server.ledger.flush()
    assert "mcp_ledger_errors_total 2" in usage.metrics.render()  # the tool row and the transport row


@pytest.mark.anyio
async def test_direct_calls_outside_http_record_nothing(api, usage_db, tmp_path):
    server.ledger = usage.Ledger.open(str(usage_db))
    await server.get_opportunity("7")
    assert rows(usage_db) == [] and usage.metrics.counts == {}


@pytest.mark.anyio
async def test_stdio_mode_writes_nothing(tmp_path):
    db = tmp_path / "stdio.db"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from matchawards_mcp.server import main; main()"],
        env={"MATCHAWARDS_API_BASE": "http://127.0.0.1:9", "MATCHAWARDS_USAGE_DB": str(db)},
    )
    async with Client(params) as client:
        assert (await client.call_tool("get_opportunity", {"id": "7"})).is_error  # dead upstream
    assert list(tmp_path.iterdir()) == []


def seed(path):
    ledger = usage.Ledger.open(str(path))
    conn = sqlite3.connect(path)
    with conn:
        for tool, outcome, key, ua, fp in [
            ("search_contracts", "ok", "198.51.100.0/24", "openai-mcp/1.0.0", "a" * 16),
            ("search_contracts", "ok", "198.51.101.0/24", "openai-mcp/1.0.0", "b" * 16),
            ("get_opportunity", "upstream_error", "2001:db8::/48", "Claude-User", None),
            ("search_jobs", "ok", "203.0.113.0/24", "mcp-census-probe", None),
            ("search_jobs", "ok", "192.0.2.0/24", "matchawards-test/1", None),
        ]:
            usage.Ledger.write_call(conn, tool, outcome, key, ua, usage.classify_ua(ua), fp, 12)
        for method, name in [("initialize", "openai-mcp"), ("initialize", "openai-mcp"), ("tools/list", "")]:
            usage.Ledger.write_transport(conn, "198.51.100.0/24", method, name, "1.0.0" if name else "", "openai-mcp/1.0.0")
        usage.Ledger.write_transport(conn, "203.0.113.0/24", "initialize", "census", "1", "mcp-census-probe")
    conn.close()
    return ledger


def test_stats_command_on_a_seeded_db(usage_db, capsys):
    seed(usage_db)
    server.main(["stats", "--days", "7", "--db", str(usage_db)])
    out = capsys.readouterr().out
    assert "search_contracts  ok              2" in out
    assert "openai-mcp        2      2" in out
    assert "tool calls                       3     1        1" in out
    assert "transport requests (by last UA)  3     1        0" in out
    assert "Distinct client keys with tool calls (/24 or /48, no probers or own tests): 3" in out
    assert "Distinct ChatGPT callers (x-openai-subject fingerprints): 2" in out
    assert "openai-mcp  1.0.0    2" in out


def test_stats_command_on_an_empty_or_missing_db(usage_db, capsys):
    usage.Ledger.open(str(usage_db))
    server.main(["stats", "--db", str(usage_db)])
    out = capsys.readouterr().out
    assert "last 7 day(s)" in out and "(none)" in out and "fingerprints): 0" in out
    with pytest.raises(SystemExit, match="no usage database"):
        server.main(["stats", "--db", str(usage_db.parent / "nope.db")])
