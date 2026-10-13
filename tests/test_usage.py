"""Usage stats in --http mode: the ledger, the transport aggregate, /metrics (counters, latency histogram,
distinct-caller gauges) and the stats command. No network."""

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
from matchawards_mcp import api as ma_api

from test_http import LEGACY, egress, http  # noqa: F401  (egress is a fixture)
from test_server import api, anyio_backend, page  # noqa: F401  (fixtures)

SECRET = "zebracornsecret"  # an argument value that must never be stored


@pytest.fixture(autouse=True)
def usage_db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("MATCHAWARDS_USAGE_DB", str(path))
    monkeypatch.setattr(ma_api, "api_budget", None)
    monkeypatch.setattr(server, "ledger", None)
    monkeypatch.setattr(usage, "metrics", usage.Metrics())
    monkeypatch.setattr(usage, "distinct", usage.Distinct())
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
        monkeypatch.setattr(ma_api, "get_one", lambda id: 1 / 0)
        await post(hc, call("get_opportunity", {"id": "3"}))
    assert sorted(r["outcome"] for r in rows(usage_db)) == ["busy", "internal_error", "ok"]


@pytest.mark.anyio
async def test_a_reply_the_tools_cannot_read_is_bad_response(api, usage_db):
    api.replies = [
        lambda req: httpx.Response(200, json={"as_of": "x"}),  # no result object
        lambda req: httpx.Response(200, json={"as_of": "x", "result": {"title": "no id"}}),  # fails the schema
        lambda req: httpx.Response(200, json={"results": "nope"}),  # a search envelope without a results list
    ]
    async with http() as hc:
        for body in (call("get_opportunity", {"id": "1"}), call("get_opportunity", {"id": "2"}),
                     call("search_grants", {})):
            assert (await post(hc, body)).json()["result"]["isError"] is True
    assert sorted((r["tool"], r["outcome"]) for r in rows(usage_db)) == [
        ("get_opportunity", "bad_response"), ("get_opportunity", "bad_response"), ("search_grants", "bad_response")]


@pytest.mark.anyio
async def test_an_api_timeout_is_upstream_timeout(api, usage_db):
    api.replies = [lambda req: httpx.Response(503, json={"error": "timeout"}, headers={"Retry-After": "60"})]
    async with http() as hc:
        assert (await post(hc, call("search_contracts", {}))).json()["result"]["isError"] is True
    assert [(r["tool"], r["outcome"]) for r in rows(usage_db)] == [("search_contracts", "upstream_timeout")]


@pytest.mark.anyio
async def test_a_composite_cut_short_is_partial(api, usage_db):
    def reply(req):
        if req.url.path.endswith("/opportunities/1"):
            return httpx.Response(200, json={"as_of": "x", "result": {
                "id": "1", "type": "federal", "naics": {"code": "336413"}, "agency_id": "dept_of_the_air_force"}})
        return httpx.Response(200, json={"results": [], "has_more": True, "next_cursor": "c"})  # sparse: limit hit

    api.replies = [reply]
    async with http() as hc:
        for name in ("assess_opportunity", "find_teaming_partners"):
            assert (await post(hc, call(name, {"id": "1"}))).json()["result"]["isError"] is False
    assert sorted((r["tool"], r["outcome"]) for r in rows(usage_db)) == [
        ("assess_opportunity", "partial"), ("find_teaming_partners", "partial")]


@pytest.mark.anyio
async def test_rate_limited_calls_leave_no_row(api, usage_db, monkeypatch):
    monkeypatch.setenv("MATCHAWARDS_RATE_BURST", "2")
    async with http() as hc:
        codes = [(await post(hc, call("get_opportunity", {"id": "7"}))).status_code for _ in range(5)]
    assert codes == [200, 200, 429, 429, 429]
    assert len(rows(usage_db)) == 2
    assert len(rows(usage_db, "transport_daily")) == 1
    text = usage.metrics.render()
    assert 'mcp_rejected_total{reason="rate_limit_client"} 3' in text
    assert 'mcp_tool_duration_seconds_count{tool="get_opportunity"} 2' in text  # the ledger rows, no more


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
async def test_chatgpt_fingerprint_is_hmac_and_needs_salt(api, usage_db, egress, monkeypatch, salt):
    if salt:
        monkeypatch.setenv("MATCHAWARDS_USAGE_SALT", salt)
    async with http() as hc:
        for header in ({"x-openai-subject": "raw-subject-value"}, {"x-openai-session": "raw-session-value"}):
            await post(hc, call("get_opportunity", {"id": "7"}), ip="192.0.2.10", **header)
        # Not from ChatGPT egress: anyone can send the header, so it is not fingerprinted.
        await post(hc, call("get_opportunity", {"id": "7"}), **{"x-openai-subject": "raw-subject-value"})
    fps = [r["caller_fp"] for r in rows(usage_db)]  # any order
    server.ledger.flush()
    raw = b"".join(p.read_bytes() for p in usage_db.parent.glob("usage.db*"))
    assert b"raw-subject-value" not in raw and b"raw-session-value" not in raw
    if salt:  # x-openai-session is never fingerprinted: it changes per conversation
        assert sorted(fps, key=str) == sorted([usage.fingerprint(b"raw-subject-value", salt.encode()), None, None],
                                              key=str)
        assert all(len(f) == 16 and int(f, 16) >= 0 for f in fps if f)
    else:
        assert fps == [None, None, None]


def test_ledger_drops_writes_past_the_queue_bound(tmp_path):
    ledger = usage.Ledger.open(str(tmp_path / "usage.db"))
    ledger.pending = usage.Ledger.MAX_PENDING
    before = usage.metrics.counts[("mcp_ledger_errors_total", ())]
    ledger.submit(usage.Ledger.write_transport, "192.0.2.0/24", "initialize", "", "", "x")
    assert usage.metrics.counts[("mcp_ledger_errors_total", ())] == before + 1
    assert ledger.pending == usage.Ledger.MAX_PENDING


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
    for name in ("mcp_tool_calls_total", "mcp_requests_total", "mcp_rejected_total", "mcp_agent_requests_total",
                 "mcp_clients_total", "mcp_distinct_evictions_total"):
        assert f"# TYPE {name} counter" in r.text
    assert "# TYPE mcp_tool_duration_seconds histogram" in r.text
    assert "# TYPE mcp_distinct_callers gauge" in r.text
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
    await server.mcp.call_tool("get_opportunity", {"id": "7"})  # through the usage wrapper, outside a request
    assert rows(usage_db) == [] and usage.metrics.counts == {} and usage.metrics.histograms == {}
    assert usage.distinct.seen == {"client": {}, "chatgpt_subject": {}}


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


def test_histogram_buckets_are_cumulative():
    m = usage.Metrics()
    for s in (0.05, 0.3, 3.0, 45.0):
        m.observe("mcp_tool_duration_seconds", s, tool="search_contracts")
    text = m.render()
    assert 'mcp_tool_duration_seconds_bucket{tool="search_contracts",le="0.1"} 1' in text
    assert 'mcp_tool_duration_seconds_bucket{tool="search_contracts",le="0.5"} 2' in text
    assert 'mcp_tool_duration_seconds_bucket{tool="search_contracts",le="5"} 3' in text
    assert 'mcp_tool_duration_seconds_bucket{tool="search_contracts",le="+Inf"} 4' in text
    assert 'mcp_tool_duration_seconds_count{tool="search_contracts"} 4' in text
    assert 'mcp_tool_duration_seconds_sum{tool="search_contracts"} 48.35' in text
    assert "# TYPE mcp_tool_duration_seconds histogram" in text


def test_distinct_windows_and_hashing(monkeypatch):
    d = usage.Distinct()
    now = [1_000_000.0]
    monkeypatch.setattr(usage.time, "monotonic", lambda: now[0])
    d.see("client", "203.0.113.0/24")
    d.see("client", "203.0.113.0/24")
    d.see("client", "198.51.100.0/24")
    now[0] += 2 * 3600  # 2 h later: still in 24h, out of 1h
    d.see("chatgpt_subject", "user-abc")
    assert d.counts("chatgpt_subject") == ({"1h": 1, "24h": 1}, 0)
    assert all("203.0.113" not in k for k in d.seen["client"])  # stored hashed only
    assert d.counts("client") == ({"1h": 0, "24h": 2}, 0)
    now[0] += 86400  # past the day: expired keys are dropped on the next see, not counted as evictions
    d.see("client", "192.0.2.0/24")
    assert len(d.seen["client"]) == 1 and d.evicted["client"] == 0


def test_a_quiet_day_leaves_no_dead_keys(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(usage.time, "monotonic", lambda: now[0])
    for i in range(3):
        usage.distinct.see("client", f"10.0.{i}.0/24")
    usage.distinct.see("chatgpt_subject", "sub")
    now[0] += 86400 + 1  # no see() since: render alone prunes
    text = usage.metrics.render()
    assert 'mcp_distinct_callers{kind="client",window="24h"} 0' in text
    assert usage.distinct.seen == {"client": {}, "chatgpt_subject": {}}


def test_counts_newest_first_stop_at_the_window(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(usage.time, "monotonic", lambda: now[0])
    d = usage.Distinct()
    for i in range(5):
        d.see("client", f"10.0.{i}.0/24")
        now[0] += 1800  # 30 min apart: the last two are within the hour
    d.see("client", "10.0.0.0/24")  # seen again: now the newest
    assert d.counts("client") == ({"1h": 3, "24h": 5}, 0)


def test_distinct_hashes_subject_bytes_as_sent():
    d = usage.Distinct()
    d.see("chatgpt_subject", b"caf\xe9")
    d.see("chatgpt_subject", "caf\xe9")  # the utf-8 encoding is other bytes: another caller
    d.see("chatgpt_subject", b"caf\xe9")
    assert len(d.seen["chatgpt_subject"]) == 2


def test_label_values_escape_backslash_quote_and_newline():
    m = usage.Metrics()
    m.inc("mcp_requests_total", method='a\\b"c\nd')
    assert 'mcp_requests_total{method="a\\\\b\\"c\\nd"} 1' in m.render()


def test_distinct_is_bounded(monkeypatch):
    monkeypatch.setattr(usage.Distinct, "MAX_KEYS", 3)
    d = usage.Distinct()
    for i in range(5):
        d.see("client", f"10.0.{i}.0/24")
    assert len(d.seen["client"]) == 3 and d.evicted["client"] == 2
    usage.distinct = d  # the autouse fixture restores the module's own after the test
    assert 'mcp_distinct_evictions_total{kind="client"} 2' in usage.metrics.render()
    assert 'mcp_distinct_evictions_total{kind="chatgpt_subject"} 0' in usage.metrics.render()


@pytest.mark.parametrize("name,bucket", [
    ("openai-mcp", "chatgpt"), ("ChatGPT", "chatgpt"), ("claude-ai", "claude"), ("Cursor", "cursor"),
    ("Visual Studio Code", "vscode"), ("n8n-nodes-matchawards", "n8n"), ("matchawards-cli", "matchawards"),
    ("mcp-inspector", "mcp-inspector"), ("", "unknown"), ("acme-bot 1.0", "other"),
])
def test_client_bucket(name, bucket):
    assert usage.client_bucket(name) == bucket


@pytest.mark.anyio
async def test_tool_call_feeds_histogram_distinct_and_clients(api, egress):
    chatgpt = {"ip": "192.0.2.10", "User-Agent": "openai-mcp/1.0.0", "x-openai-subject": "sub-1"}
    async with http() as hc:
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "openai-mcp", "version": "1"}}}
        await post(hc, init, **chatgpt)
        await post(hc, call("search_contracts", {"keyword": SECRET}), **chatgpt)
        await post(hc, INIT)  # another client (acme-agent, 203.0.113.0/24): the subject header is not sent
    text = usage.metrics.render()
    assert 'mcp_tool_duration_seconds_count{tool="search_contracts"} 1' in text
    assert 'mcp_clients_total{client="chatgpt"} 1' in text and 'mcp_clients_total{client="other"} 1' in text
    assert 'mcp_distinct_callers{kind="client",window="1h"} 2' in text
    assert 'mcp_distinct_callers{kind="chatgpt_subject",window="24h"} 1' in text
    assert SECRET not in text and "sub-1" not in text and "203.0.113" not in text and "192.0.2" not in text


@pytest.mark.anyio
async def test_a_non_ascii_subject_is_one_caller(api, egress):
    async with http() as hc:
        for _ in range(2):  # raw header bytes, as a proxy passes them on: 0xe9 alone is not valid utf-8
            await post(hc, call("get_opportunity", {"id": "7"}), ip="192.0.2.10", **{"x-openai-subject": b"caf\xe9"})
    assert len(usage.distinct.seen["chatgpt_subject"]) == 1
    [h] = usage.distinct.seen["chatgpt_subject"]
    assert h == usage.hashlib.blake2s(b"caf\xe9", key=usage.distinct.salt, digest_size=8).hexdigest()


@pytest.mark.anyio
async def test_a_subject_counts_only_from_chatgpt_egress(api, egress):
    async with http() as hc:
        await post(hc, call("get_opportunity", {"id": "7"}), **{"x-openai-subject": "spoofed"})  # not egress
        await post(hc, call("get_opportunity", {"id": "7"}), ip="192.0.2.10",
                   **{"x-openai-subject": "x" * (server.SUBJECT_MAX_LEN + 1)})  # overlong counts as absent
    text = usage.metrics.render()
    assert 'mcp_distinct_callers{kind="chatgpt_subject",window="24h"} 0' in text
    assert 'mcp_distinct_callers{kind="client",window="24h"} 2' in text


@pytest.mark.anyio
async def test_probers_are_not_distinct_clients(api):
    async with http() as hc:
        await post(hc, call("search_contracts", {}), **{"User-Agent": "uptime-monitor/2"})
        await post(hc, call("search_contracts", {}), **{"User-Agent": "matchawards-test/1"})
    text = usage.metrics.render()
    assert 'mcp_distinct_callers{kind="client",window="24h"} 0' in text
    assert 'mcp_tool_duration_seconds_count{tool="search_contracts"} 2' in text  # still timed


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


def test_retention_days_defaults_to_180_and_rejects_bad_values():
    assert usage.retention_days(None) == 180
    assert usage.retention_days("30") == 30
    for bad in ("0", "-1", "x", ""):
        with pytest.raises(SystemExit, match="MATCHAWARDS_USAGE_RETENTION_DAYS"):
            usage.retention_days(bad)
