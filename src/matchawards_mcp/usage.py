"""Usage stats for --http mode: a SQLite ledger, in-memory Prometheus counters and the `stats` command.

Never recorded: tool arguments, request bodies (only the JSON-RPC method and clientInfo are read from the first
8 KiB of a request), full client addresses (IPv4 is kept per /24, IPv6 per /48) and raw header values.
stdio mode imports this module but never opens a ledger or counts anything.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import sqlite3
import sys
import threading
import typing
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("matchawards_mcp.usage")

DEFAULT_DB = "/data/usage.db"
RETENTION_DAYS = 365
MAX_BUCKETS_PER_CLIENT_DAY = 50
OBSERVE_BYTES = 8192  # an initialize body is well under 2 KiB
OTHER = "_other"

# First match wins, so the more specific names come first (claude-user and claudebot before claude).
FAMILIES = (
    "openai-mcp", "claude-user", "claudebot", "claude", "chatgpt-user", "perplexity", "cursor", "vscode",
    "matchawards-mcp", "matchawards-cli", "n8n-nodes-matchawards", "matchawards-test",
)
# Words directory, uptime and census tooling puts in its own UA. A bare curl/httpx/node UA has none of them.
PROBER_WORDS = (
    "probe", "healthcheck", "health", "monitor", "indexer", "census", "discovery", "crawler", "liveness",
    "readiness", "oracle", "verifier", "collector", "scout", "registry", "bot-check",
)
NOT_REAL = ("prober", "matchawards-test")  # left out of the distinct-client and "real" numbers


def classify_ua(ua: str) -> str:
    """The agent family of a User-Agent: a FAMILIES name, 'prober', or 'other'. Self-declared, so attribution only."""
    ua = ua.lower()
    return next((f for f in FAMILIES if f in ua), None) or (
        "prober" if any(w in ua for w in PROBER_WORDS) else "other")


def _known_methods() -> frozenset[str]:
    """Every client-to-server method the installed SDK defines, so a caller cannot invent new labels."""
    from mcp import types

    found = set()
    for union in (types.ClientRequest, types.ClientNotification):
        for model in typing.get_args(union):
            field = getattr(model, "model_fields", {}).get("method")
            args = typing.get_args(field.annotation) if field else ()
            if args and isinstance(args[0], str):
                found.add(args[0])
    return frozenset(found)


KNOWN_METHODS = _known_methods()
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"  # 2026-07-28 clients send clientInfo in params._meta


def clean(value, limit: int) -> str:
    """A caller-supplied string made safe to store and print: clamped, control characters replaced."""
    if not isinstance(value, str):
        return ""
    return "".join(c if c.isprintable() else "?" for c in value[:limit])


def parse_body(body: bytes) -> tuple[str, str, str]:
    """(method, client name, client version) from the start of a JSON-RPC body. Total: junk gives ('_other', '', '')."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return OTHER, "", ""
    if not isinstance(msg, dict):
        return OTHER, "", ""
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    info = params.get("clientInfo") if method == "initialize" else None
    if not isinstance(info, dict) and isinstance(params.get("_meta"), dict):
        info = params["_meta"].get(CLIENT_INFO_META_KEY)
    info = info if isinstance(info, dict) else {}
    return (method if method in KNOWN_METHODS else OTHER,
            clean(info.get("name"), 100), clean(info.get("version"), 100))


def truncate_key(key: str) -> str:
    """A rate-limit client key cut to its IPv4 /24 or IPv6 /48: '203.0.113.7' -> '203.0.113.0/24'."""
    try:
        net = ipaddress.ip_network(key, strict=False)
    except ValueError:
        return "unknown"
    return str(net.supernet(new_prefix=24 if net.version == 4 else 48))


def fingerprint(headers: dict[bytes, bytes], salt: bytes | None) -> str | None:
    """16-hex HMAC of x-openai-subject, to count distinct ChatGPT callers. None without salt.

    Not x-openai-session: it changes per conversation, so it would count conversations, not callers."""
    value = headers.get(b"x-openai-subject")
    if not salt or not value:
        return None
    return hmac.new(salt, value, hashlib.sha256).hexdigest()[:16]


class Metrics:
    """Counters for the Prometheus text format. Every label value comes from a fixed set."""

    HELP = {
        "mcp_tool_calls_total": "Tool calls by tool and outcome.",
        "mcp_requests_total": "Requests served on /mcp by JSON-RPC method.",
        "mcp_rejected_total": "Requests rejected before the MCP layer, by reason.",
        "mcp_agent_requests_total": "Requests served on /mcp by User-Agent family.",
        "mcp_ledger_errors_total": "Usage ledger writes that failed (the request itself was served).",
    }

    def __init__(self):
        self.counts: Counter = Counter()
        self.lock = threading.Lock()  # ledger errors are counted from the writer threads

    def inc(self, name: str, **labels: str) -> None:
        with self.lock:
            self.counts[(name, tuple(sorted(labels.items())))] += 1

    def render(self) -> str:
        with self.lock:
            counts = dict(self.counts)
        lines = []
        for name, text in self.HELP.items():
            lines += [f"# HELP {name} {text}", f"# TYPE {name} counter"]
            series = sorted((labels, n) for (metric, labels), n in counts.items() if metric == name)
            if not series and name == "mcp_ledger_errors_total":
                series = [((), 0)]
            for labels, n in series:
                body = ",".join(f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                                for k, v in labels)
                lines.append(f"{name}{{{body}}} {n}" if body else f"{name} {n}")
        return "\n".join(lines) + "\n"


metrics = Metrics()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
  ts TEXT NOT NULL, tool TEXT NOT NULL, outcome TEXT NOT NULL, client_key TEXT NOT NULL,
  ua TEXT NOT NULL, family TEXT NOT NULL, caller_fp TEXT, latency_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS tool_calls_ts ON tool_calls (ts);
CREATE TABLE IF NOT EXISTS transport_daily (
  day TEXT NOT NULL, client_key TEXT NOT NULL, method TEXT NOT NULL, client_name TEXT NOT NULL,
  client_version TEXT NOT NULL, count INTEGER NOT NULL, last_ua TEXT NOT NULL,
  PRIMARY KEY (day, client_key, method, client_name, client_version));
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    """Best-effort writes on two dedicated threads: a failing write is counted in mcp_ledger_errors_total, never raised."""

    MAX_PENDING = 10_000  # writes queued past this (a stuck disk) are dropped and counted as ledger errors

    def __init__(self, path: str):
        self.path = path
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-ledger")
        self.pruned_day = ""
        self.warned = False
        self.pending = 0
        self.pending_lock = threading.Lock()

    @classmethod
    def open(cls, path: str) -> "Ledger | None":
        """The ledger at `path`, or None (one warning) when it cannot be created there. Never raises."""
        try:
            if not os.access(os.path.dirname(os.path.abspath(path)), os.W_OK):
                raise OSError("directory missing or not writable")
            with sqlite3.connect(path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
            conn.close()
        except (OSError, sqlite3.Error) as e:
            log.warning("usage ledger disabled: cannot use %s (%s)", path, e)
            return None
        return cls(path)

    def submit(self, fn, *args) -> None:
        with self.pending_lock:
            if self.pending >= self.MAX_PENDING:
                metrics.inc("mcp_ledger_errors_total")
                return
            self.pending += 1
        self.pool.submit(self._safe, fn, *args)

    def flush(self) -> None:
        """Wait for queued writes (tests, shutdown)."""
        self.pool.shutdown(wait=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-ledger")

    def _safe(self, fn, *args) -> None:
        with self.pending_lock:
            self.pending -= 1
        try:
            conn = sqlite3.connect(self.path, timeout=5)
            try:
                with conn:
                    fn(conn, *args)
                    self._prune(conn)
            finally:
                conn.close()
        except Exception as e:
            metrics.inc("mcp_ledger_errors_total")
            if not self.warned:
                self.warned = True
                log.warning("usage ledger write failed (counted in mcp_ledger_errors_total from now on): %s", e)

    def _prune(self, conn) -> None:
        now = _now()
        if self.pruned_day == now.date().isoformat():
            return
        cutoff = now - timedelta(days=RETENTION_DAYS)
        conn.execute("DELETE FROM tool_calls WHERE ts < ?", (cutoff.isoformat(timespec="seconds"),))
        conn.execute("DELETE FROM transport_daily WHERE day < ?", (cutoff.date().isoformat(),))
        self.pruned_day = now.date().isoformat()  # set only after the deletes, so a failed prune is retried

    @staticmethod
    def write_call(conn, tool, outcome, client_key, ua, family, caller_fp, latency_ms) -> None:
        conn.execute("INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (_now().isoformat(timespec="seconds"), tool, outcome, client_key, ua, family, caller_fp,
                      latency_ms))

    @staticmethod
    def write_transport(conn, client_key, method, name, version, ua) -> None:
        day = _now().date().isoformat()
        key = (day, client_key, method, name, version)
        exists = conn.execute("SELECT 1 FROM transport_daily WHERE day = ? AND client_key = ? AND method = ? "
                              "AND client_name = ? AND client_version = ?", key).fetchone()
        if not exists:
            (n,) = conn.execute("SELECT COUNT(*) FROM transport_daily WHERE day = ? AND client_key = ?",
                                (day, client_key)).fetchone()
            if n >= MAX_BUCKETS_PER_CLIENT_DAY:  # soft cap: concurrent writers can overshoot by one or two
                key = (day, client_key, OTHER, "", "")
        conn.execute("INSERT INTO transport_daily VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT "
                     "(day, client_key, method, client_name, client_version) DO UPDATE SET "
                     "count = count + 1, last_ua = excluded.last_ua", (*key, ua))


# --- matchawards-mcp stats -------------------------------------------------------------------------------


def _table(out, title: str, header: tuple, rows: list) -> None:
    out.append(f"\n{title}")
    if not rows:
        out.append("  (none)")
        return
    rows = [tuple(str(x) for x in r) for r in rows]
    widths = [max(len(x) for x in col) for col in zip(header, *rows)]
    out += ["  " + "  ".join(x.ljust(w) for x, w in zip(r, widths)).rstrip() for r in (header, *rows)]


def stats(conn, days: int) -> str:
    since = _now() - timedelta(days=days)
    ts, day = since.isoformat(timespec="seconds"), since.date().isoformat()
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
    not_real = ",".join("?" * len(NOT_REAL))
    out = [f"MatchAwards MCP usage, last {days} day(s), since {ts}"]
    _table(out, "Tool calls by tool and outcome", ("tool", "outcome", "calls"), q(
        "SELECT tool, outcome, COUNT(*) FROM tool_calls WHERE ts >= ? GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2", ts))
    _table(out, "Tool calls by client family", ("family", "calls", "client keys"), q(
        "SELECT family, COUNT(*), COUNT(DISTINCT client_key) FROM tool_calls WHERE ts >= ? GROUP BY 1 "
        "ORDER BY 2 DESC, 1", ts))
    [(real, real_keys)] = q(f"SELECT COUNT(*), COUNT(DISTINCT client_key) FROM tool_calls WHERE ts >= ? "
                            f"AND family NOT IN ({not_real})", ts, *NOT_REAL)
    [(probers,)] = q("SELECT COUNT(*) FROM tool_calls WHERE ts >= ? AND family = 'prober'", ts)
    [(own,)] = q("SELECT COUNT(*) FROM tool_calls WHERE ts >= ? AND family = 'matchawards-test'", ts)
    [(fps,)] = q("SELECT COUNT(DISTINCT caller_fp) FROM tool_calls WHERE ts >= ? AND family = 'openai-mcp'", ts)
    transport = q("SELECT method, client_name, client_version, count, last_ua FROM transport_daily WHERE day >= ?",
                  day)
    t_family = Counter()
    for r in transport:
        t_family[classify_ua(r[4])] += r[3]
    t_prober, t_own = t_family["prober"], t_family["matchawards-test"]
    _table(out, "Real vs probers", ("", "real", "probers", "own tests"), [
        ("tool calls", real, probers, own),
        ("transport requests (by last UA)", sum(t_family.values()) - t_prober - t_own, t_prober, t_own),
    ])
    out.append(f"\nDistinct client keys with tool calls (/24 or /48, no probers or own tests): {real_keys}")
    out.append(f"Distinct ChatGPT callers (x-openai-subject fingerprints): {fps}")
    by_method, by_client = Counter(), Counter()
    for method, name, version, count, _ in transport:
        by_method[method] += count
        if name:
            by_client[(name, version)] += count
    _table(out, "Transport requests by method", ("method", "requests"), by_method.most_common())
    _table(out, "Top clientInfo (initialize / _meta)", ("name", "version", "requests"),
           [(n, v, c) for (n, v), c in by_client.most_common(15)])
    return "\n".join(out)


def stats_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="matchawards-mcp stats", description="Print hosted-mode usage stats.")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--db", default=os.environ.get("MATCHAWARDS_USAGE_DB", DEFAULT_DB),
                        help=f"ledger path (default $MATCHAWARDS_USAGE_DB or {DEFAULT_DB})")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be 1 or more")
    if not os.path.exists(args.db):
        sys.exit(f"no usage database at {args.db}")
    conn = sqlite3.connect(Path(args.db).absolute().as_uri() + "?mode=ro", uri=True)
    try:
        print(stats(conn, args.days))
    finally:
        conn.close()
