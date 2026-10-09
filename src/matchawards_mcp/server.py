"""MatchAwards MCP server: read-only search over the public matchawards.com API.

No auth, no state. The tools live in tools.py, their output models in schemas.py and the API client in api.py;
this module registers the tools and serves them: stdio by default, `--http` for stateless Streamable HTTP at /mcp.
"""

import argparse
import asyncio
import contextlib
import contextvars
import functools
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

import httpx
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__, api, tools, usage

# The essentials sit in the first 512 characters: which tool for which intent, links as markdown, where the data
# comes from and that it may be incomplete, no "incumbent", and that AI text is labelled. test_server.py checks it.
INSTRUCTIONS = (
    "Read-only search of US government opportunities. Contracts, bids, RFPs, set-asides: search_contracts. "
    "Grants or funding: search_grants. Jobs: search_jobs. One result's details and contacts: get_opportunity "
    "with its id. Print every url (and sam_url for federal) as a markdown link. Results come from MatchAwards' "
    "catalog (SAM.gov notices, state portals, Grants.gov, USAJOBS) and may be incomplete. Never call a "
    "firm the incumbent. AI-generated text is labelled; keep the label when you quote it. "
    "When has_more is true, call again with cursor=next_cursor and the same filters. Nothing here needs an account."
)

# HTTP mode only: the usage ledger (None when disabled, and always in stdio mode) and the request being served.
ledger: usage.Ledger | None = None
_request: contextvars.ContextVar[dict | None] = contextvars.ContextVar("matchawards_request", default=None)

mcp = MCPServer(
    name="matchawards",
    title="MatchAwards",
    instructions=INSTRUCTIONS,
    website_url="https://matchawards.com",
    version=__version__,
    # The tool list is static: let clients cache it for a day, shared across users.
    cache_hints={"tools/list": CacheHint(ttl_ms=86_400_000, scope="public")},
    # No server-initiated notifications, so do not serve subscriptions/listen.
    subscriptions=False,
)

# Closed world: the tools read MatchAwards' own catalog, not arbitrary third-party sites.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# No account needed. Declared in _meta only: the SDK sends tools/list through the spec's Tool model, which drops
# unknown top-level fields, so a top-level securitySchemes never reaches the wire.
SECURITY_SCHEMES = [{"type": "noauth"}]

# The result card (MCP Apps): one self-contained HTML resource, no network, that hosts with UI support render for
# the search tools below from their structuredContent. Off unless MATCHAWARDS_WIDGETS=1: then the resource is listed
# and those tools point at it in _meta. Results are the same either way. Hosts cache a card by URI, so a breaking
# change to the HTML bumps the version in CARD_URI.
WIDGETS = os.environ.get("MATCHAWARDS_WIDGETS", "").strip() == "1"
CARD_URI = "ui://matchawards/results-v1.html"
CARD_MIME = "text/html;profile=mcp-app"
CARD_TOOLS = ("search_contracts", "search_grants")
CARD_HTML = (Path(__file__).parent / "data" / "results-v1.html").read_text(encoding="utf-8")
CARD_META = {
    "ui": {
        "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
        "domain": "https://matchawards.com",
        "prefersBorder": True,
    },
    # ChatGPT's own key: links to these origins open without the "Check this link is safe" dialog.
    "openai/widgetCSP": {
        "connect_domains": [],
        "resource_domains": [],
        "redirect_domains": ["https://matchawards.com"],
    },
    "openai/widgetDescription": (
        "A MatchAwards results card: the opportunities found, with agency, place, deadline, set-aside and NAICS. "
        "The card already lists them, so do not repeat the whole list; summarize what matters and link the "
        "results you mention."
    ),
}


def _tool(fn, title: str):
    """Register a read-only tool; its docstring is the description the model reads, its return model the
    outputSchema.

    In --http mode each call that reaches the tool is counted and written to the usage ledger: tool name, outcome,
    latency and who called (see HttpGuard). Never the arguments. Outside an HTTP request (stdio) it records nothing."""

    @functools.wraps(fn)
    async def recorded(*args, **kwargs):
        req = _request.get()
        if req is None:
            return await fn(*args, **kwargs)
        start, outcome = time.monotonic(), "internal_error"
        try:
            result = await fn(*args, **kwargs)
            outcome = "ok"
            return result
        except ToolError as e:
            outcome = getattr(e, "outcome", "upstream_error")
            raise
        finally:
            usage.metrics.inc("mcp_tool_calls_total", tool=fn.__name__, outcome=outcome)
            if ledger is not None:
                ledger.submit(usage.Ledger.write_call, fn.__name__, outcome, req["client"], req["ua"],
                              req["family"], req["fp"], round((time.monotonic() - start) * 1000))

    # One line per paragraph: the docstring's wrapping never reaches the published description.
    description = "\n\n".join(" ".join(p.split()) for p in inspect.cleandoc(fn.__doc__).split("\n\n"))
    meta = {"securitySchemes": SECURITY_SCHEMES}
    if WIDGETS and fn.__name__ in CARD_TOOLS:
        meta["ui"] = {"resourceUri": CARD_URI}
    return mcp.tool(title=title, annotations=READ_ONLY, meta=meta, description=description)(recorded)


for _fn, _title in tools.TOOLS:
    _tool(_fn, _title)

if WIDGETS:
    mcp.resource(CARD_URI, name="matchawards-results", title="MatchAwards results", mime_type=CARD_MIME,
                 meta=CARD_META)(lambda: CARD_HTML)


# --- HTTP mode -------------------------------------------------------------------------------------------

access_log = logging.getLogger("matchawards_mcp.access")
_LOG_SALT = os.urandom(16)  # client keys in the log are hashed with a per-process salt, never written raw
_SUBJECT_SALT = os.urandom(16)  # x-openai-subject is kept only as a keyed hash, as a rate-limit key
_LOG_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_LOG_PATHS = {"/mcp", "/healthz"}
MAX_BODY_BYTES = 64 * 1024  # a JSON-RPC request to these tools is well under 2 KB


class RateLimiter:
    """Sliding window per client key: at most `per_min` requests in 60 s and `burst` in 5 s."""

    # ponytail: per-process memory. One container runs one process, so this is the whole limit. If the service
    # is ever scaled out (several workers or containers), each keeps its own counts and the real limit multiplies:
    # then move the counters to a shared store (Redis) or to nginx limit_req keyed on the same client address.
    # Memory is capped at max_keys clients (about 1.5 KB each); past that the least recently seen key is dropped,
    # which resets that client's count. Behind nginx keys are real client addresses, so only a flood from more
    # than max_keys addresses in one minute gets there.

    def __init__(self, per_min: int, burst: int, clock=None, max_keys: int = 20_000):
        self.per_min, self.burst, self.max_keys = per_min, burst, max_keys
        self.clock = clock or time.monotonic
        self.hits: dict[str, deque[float]] = {}  # one deque per key, never longer than per_min
        self.next_sweep = 0.0

    def check(self, key: str) -> int:
        """Count one request. Return 0 if it is allowed, else the seconds to wait (it is then not counted)."""
        wait = self.wait(key)
        if not wait:
            self.record(key)
        return wait

    def record(self, key: str) -> None:
        self.hits.setdefault(key, deque()).append(self.clock())

    def wait(self, key: str) -> int:
        """Seconds until one more request from `key` would be allowed (0: now). Counts nothing."""
        now = self.clock()
        if now >= self.next_sweep:  # once a minute, forget idle clients so memory tracks active ones only
            self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - 60}
            self.next_sweep = now + 60
        q = self.hits.pop(key, None) or deque()
        self.hits[key] = q  # re-insert: dict order is least recently seen first
        if len(self.hits) > self.max_keys:
            del self.hits[next(iter(self.hits))]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= self.per_min:
            return max(1, math.ceil(q[0] + 60 - now))
        if self.burst and len(q) >= self.burst and q[-self.burst] > now - 5:
            return max(1, math.ceil(q[-self.burst] + 5 - now))
        return 0


def _ip(raw: str):
    """A valid IP address (IPv4-mapped IPv6 unwrapped to IPv4), or None."""
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def client_ip(scope, trusted_proxies):
    """The client address: the socket peer, or X-Real-IP when the peer is a trusted proxy. None without a TCP peer.

    X-Real-IP from any other peer is ignored, so a client cannot pick its own address. A value that is not a valid
    IP is ignored too (the peer is used)."""
    ip = peer = _ip((scope.get("client") or ("",))[0])
    if peer is not None and any(peer in net for net in trusted_proxies):
        real_ip = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-real-ip"), "")
        ip = _ip(real_ip) or peer
    return ip


def ip_key(ip) -> str:
    """The rate-limit key of a client address: the address itself, IPv6 per /64. "unknown" for None (a unix
    socket)."""
    if ip is None:
        return "unknown"
    return str(ipaddress.ip_network((ip, 64), strict=False)) if ip.version == 6 else str(ip)


def client_key(scope, trusted_proxies) -> str:
    """The rate-limit key of a request (client_ip, then ip_key)."""
    return ip_key(client_ip(scope, trusted_proxies))


def prefix_key(key: str) -> str | None:
    """The /48 around an IPv6 client key ('2001:db8:1:2::/64' -> '2001:db8:1::/48'); None for IPv4 and 'unknown'.

    One subscriber usually holds a /56 or a /48, so up to 65,536 /64s to rotate through. The /48 gets its own
    shared limit on top of the per-/64 one, so rotating cannot get past it, while separate users behind one /48
    (a carrier, a company, a cloud egress) still get their own per-/64 limits inside it."""
    return str(ipaddress.ip_network(key).supernet(new_prefix=48)) if ":" in key else None


EGRESS_URL = "https://openai.com/chatgpt-connectors.json"
EGRESS_SNAPSHOT = Path(__file__).parent / "data" / "chatgpt-connectors.json"  # bundled; used until a fetch works
EGRESS_REFRESH_S = 86_400  # after a good fetch
EGRESS_RETRY_S = 3_600  # after a failed one
SUBJECT_MAX_LEN = 256  # a longer x-openai-subject counts as absent
# A whole list covering more IPv4 addresses than this is rejected: the October 2026 snapshot covers 36,439, so
# this is about 4x that. Every address in the list skips its per-IP limit, so a list grown far past what OpenAI
# publishes (an error upstream, or a tampered copy) must not take effect. Raise it if OpenAI's list really grows.
EGRESS_MAX_IPV4 = 150_000
# ~4x today's 283 prefixes: bounds IPv6 too (it has no address-count cap) and the per-request scan.
EGRESS_MAX_ENTRIES = 1_200
egress_log = logging.getLogger("matchawards_mcp.egress")


def parse_egress(data) -> list | None:
    """The networks in an egress list ({creationTime, prefixes: [{ipv4Prefix | ipv6Prefix: CIDR}]}), or None if
    there are none or they cover more than EGRESS_MAX_IPV4 IPv4 addresses (logged). An entry that is not a CIDR
    string, or is wider than an IPv4 /16 or IPv6 /32 (its clients would all skip their per-IP limit), is skipped
    and logged."""
    prefixes = data.get("prefixes") if isinstance(data, dict) else None
    nets = []
    for entry in prefixes if isinstance(prefixes, list) else []:
        raw = (entry.get("ipv4Prefix") or entry.get("ipv6Prefix")) if isinstance(entry, dict) else None
        try:
            net = ipaddress.ip_network(raw if isinstance(raw, str) else "")
        except ValueError:
            net = None
        if net is None or net.prefixlen < (16 if net.version == 4 else 32):
            egress_log.warning("egress list: skipped a bad entry %.100r", entry)
        else:
            nets.append(net)
    if len(nets) > EGRESS_MAX_ENTRIES:
        egress_log.warning("egress list rejected: %d prefixes, more than EGRESS_MAX_ENTRIES", len(nets))
        return None
    v4 = ipaddress.collapse_addresses(n for n in nets if n.version == 4)  # overlaps counted once
    if (total := sum(n.num_addresses for n in v4)) > EGRESS_MAX_IPV4:
        egress_log.warning("egress list rejected: %d IPv4 addresses, more than EGRESS_MAX_IPV4", total)
        return None
    return nets or None


async def fetch_egress() -> list | None:
    """The current egress list from EGRESS_URL, or None (logged) if it cannot be fetched or has no valid entry."""
    try:
        r = await api.client.get(EGRESS_URL, timeout=10.0)
        r.raise_for_status()
        nets = parse_egress(r.json())
    except (httpx.HTTPError, ValueError) as e:
        egress_log.warning("egress list refresh failed, keeping the last good list: %s", type(e).__name__)
        return None
    if nets is None:
        egress_log.warning("egress list refresh had no usable list, keeping the last good list")
    return nets


class EgressRanges:
    """The addresses ChatGPT calls from: OpenAI's published list, shared by every ChatGPT user.

    Starts from the bundled snapshot and refreshes from EGRESS_URL in the background, started by the request that
    finds it due (the first request, then every EGRESS_REFRESH_S). A failed or empty fetch keeps the last good list,
    so the limits never fall open."""

    def __init__(self, nets: list, clock=None):
        self.nets, self.clock = nets, clock or time.monotonic
        self.due = 0.0
        self.task: asyncio.Task | None = None  # held so the running refresh is not garbage-collected

    def __contains__(self, ip) -> bool:
        return ip is not None and any(ip in net for net in self.nets)

    def refresh_if_due(self) -> None:
        if self.clock() >= self.due and (self.task is None or self.task.done()):
            self.due = self.clock() + EGRESS_RETRY_S  # until the fetch succeeds
            self.task = asyncio.create_task(self.refresh())

    async def refresh(self) -> None:
        if nets := await fetch_egress():
            self.nets, self.due = nets, self.clock() + EGRESS_REFRESH_S


def _host_name(host: str) -> str:
    """The Host header without its port, lower-cased: 'matchawards.com:443' -> 'matchawards.com'."""
    name, sep, port = host.rpartition(":")
    return (name if sep and port.isdigit() else host).lower()


class HttpGuard:
    """Runs before the MCP layer, in this order: path (404), Host (421), method (405), body size (411/413),
    rate limits (429). Writes one access line per request.

    A rejection is counted in mcp_rejected_total{reason}. A request passed on to /mcp is counted by JSON-RPC method
    and User-Agent family, added to the daily transport aggregate, and made visible to the tool wrapper (_tool)."""

    def __init__(self, app, limiter: RateLimiter, prefix_limiter: RateLimiter, global_limiter: RateLimiter,
                 subject_limiter: RateLimiter, egress_limiter: RateLimiter, egress: EgressRanges,
                 trusted_proxies, allowed_hosts, usage_salt: bytes | None = None):
        self.app, self.limiter, self.prefix_limiter, self.global_limiter = app, limiter, prefix_limiter, global_limiter
        self.subject_limiter, self.egress_limiter, self.egress = subject_limiter, egress_limiter, egress
        self.trusted_proxies, self.allowed_hosts, self.usage_salt = trusted_proxies, allowed_hosts, usage_salt

    def reject(self, scope, path: str, ip, key: str) -> tuple[str, Response] | None:
        """(reason, response) that ends this request here, or None to pass it on."""
        if path == "/healthz":
            return None  # unlimited; the route itself answers 405 to anything but GET/HEAD
        if path != "/mcp":
            return "path", PlainTextResponse("Not Found", status_code=404)
        headers = {k: v.decode("latin-1") for k, v in scope["headers"] if k in (b"host", b"content-length")}
        if _host_name(headers.get(b"host", "")) not in self.allowed_hosts:
            return "host", PlainTextResponse("Invalid Host header", status_code=421)
        if scope["method"] != "POST":
            # Stateless server: no SSE stream to GET, no session to DELETE, no CORS preflight (OPTIONS).
            return "method", Response(status_code=405, headers={"Allow": "POST"})
        length = headers.get(b"content-length")
        if length is None:
            return "size", PlainTextResponse("Content-Length required", status_code=411)
        if not length.isdigit():
            return "size", PlainTextResponse("Invalid Content-Length", status_code=400)
        if int(length) > MAX_BODY_BYTES:
            return "size", PlainTextResponse("Request body too large", status_code=413)
        # A request counts in the limits only when all of them allow it: a source adds at most its own budget to
        # the global count (IPv4 address 30/min, IPv6 /48 120/min, all of ChatGPT 600/min, so 1200 needs 10+ /48s
        # or 40+ IPv4 addresses, or ChatGPT plus half that), and a lockout at one level does not use up the budgets
        # of the others. A distributed flood from that many sources can still trip the global cap for everyone;
        # Cloudflare in front of nginx is the outer layer.
        self.egress.refresh_if_due()
        if ip in self.egress:
            # ChatGPT: many users share each of these addresses, so a per-IP limit would throttle them together.
            # Its x-openai-subject (one per user) gets the per-client budget instead, inside a pool for all ChatGPT
            # traffic. Trusted only from these addresses (anyone else could pick a fresh subject per request), and
            # never the User-Agent, which anyone can send.
            # The first header if there are several; blank or longer than SUBJECT_MAX_LEN counts as absent.
            subject = next((v for k, v in scope["headers"] if k == b"x-openai-subject"), b"").strip()
            subject = subject if len(subject) <= SUBJECT_MAX_LEN else b""
            checks = [(self.egress_limiter, "egress", "egress"), (self.global_limiter, "all", "global")]
            if subject:
                digest = hashlib.blake2s(subject, key=_SUBJECT_SALT, digest_size=16).hexdigest()
                checks.insert(0, (self.subject_limiter, digest, "subject"))
        else:
            prefix = prefix_key(key)
            checks = [(self.limiter, key, "client"), (self.global_limiter, "all", "global")]
            checks += [(self.prefix_limiter, prefix, "prefix")] if prefix else []
        for limiter, k, name in checks:
            if wait := limiter.wait(k):
                return f"rate_limit_{name}", JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32000, "message": f"Rate limit reached. Wait {wait} seconds, then retry.",
                    }},
                    status_code=429, headers={"Retry-After": str(wait)},
                )
        for limiter, k, _ in checks:
            limiter.record(k)
        return None

    async def serve_mcp(self, scope, receive, send, key: str) -> None:
        """Pass a request on to /mcp, observing it for the usage stats on the way.

        The first usage.OBSERVE_BYTES of the body are copied as they stream past (never buffered or replayed), and
        only the JSON-RPC method and clientInfo are read from them, after the response is sent."""
        headers = dict(scope["headers"])
        ua = usage.clean(headers.get(b"user-agent", b"").decode("latin-1"), 200)
        req = {"client": usage.truncate_key(key), "ua": ua, "family": usage.classify_ua(ua),
               "fp": usage.fingerprint(headers, self.usage_salt)}
        seen = bytearray()

        async def observing_receive():
            message = await receive()
            if message["type"] == "http.request" and len(seen) < usage.OBSERVE_BYTES:
                seen.extend(message.get("body", b"")[: usage.OBSERVE_BYTES - len(seen)])
            return message

        token = _request.set(req)
        try:
            await self.app(scope, observing_receive, send)
        finally:
            _request.reset(token)
            method, name, version = usage.parse_body(bytes(seen))
            usage.metrics.inc("mcp_requests_total", method=method)
            usage.metrics.inc("mcp_agent_requests_total", family=req["family"])
            if ledger is not None:
                ledger.submit(usage.Ledger.write_transport, req["client"], method, name, version, ua)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start, status, ip = time.monotonic(), 0, client_ip(scope, self.trusted_proxies)
        key = ip_key(ip)
        path = re.sub(r"/+", "/", scope["path"]).rstrip("/") or "/"  # '//mcp' and '/mcp/' are '/mcp'

        async def send_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            if rejected := self.reject(scope, path, ip, key):
                usage.metrics.inc("mcp_rejected_total", reason=rejected[0])
                await rejected[1](scope, receive, send_status)
            elif path == "/mcp":
                await self.serve_mcp({**scope, "path": path, "raw_path": path.encode()}, receive, send_status, key)
            else:
                await self.app({**scope, "path": path, "raw_path": path.encode()}, receive, send_status)
        finally:
            # Method, path, status, duration, hashed client key. Never headers (Authorization) or bodies.
            # Path and method are client-controlled (the path arrives percent-decoded, so it can hold CR/LF):
            # log only known values.
            access_log.info(
                "%s %s %d %dms client=%s",
                scope["method"] if scope["method"] in _LOG_METHODS else "OTHER",
                path if path in _LOG_PATHS else "OTHER",
                status,
                (time.monotonic() - start) * 1000, hashlib.blake2s(key.encode(), key=_LOG_SALT, digest_size=6).hexdigest(),
            )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def _env_int(name: str, default: int) -> int:
    """A whole number >= 1 from the environment; a bad value stops the server at startup with a clear message."""
    raw = os.environ.get(name, str(default))
    if not raw.strip().isdigit() or int(raw) < 1:
        raise SystemExit(f"{name} must be a whole number >= 1, got {raw!r}")
    return int(raw)


def http_app() -> Starlette:
    """The ASGI app for --http: stateless Streamable HTTP with JSON replies at /mcp, plus GET /healthz."""
    hosts = [h.strip() for h in os.environ.get(
        "MATCHAWARDS_ALLOWED_HOSTS", "matchawards.com,staging.matchawards.com").split(",") if h.strip()]
    hosts += ["127.0.0.1", "localhost", "[::1]"]
    global ledger
    api.api_budget = RateLimiter(_env_int("MATCHAWARDS_GLOBAL_API_PER_MIN", 900), 0)
    ledger = usage.Ledger.open(os.environ.get("MATCHAWARDS_USAGE_DB", usage.DEFAULT_DB))
    # Peers whose X-Real-IP is believed. Loopback only by default; in Docker the deployment names the exact
    # address nginx connects from (the compose network's gateway), never a broad private range.
    try:
        trusted = [ipaddress.ip_network(n.strip()) for n in os.environ.get(
            "MATCHAWARDS_TRUSTED_PROXIES", "127.0.0.1/32,::1/128").split(",") if n.strip()]
    except ValueError as e:
        raise SystemExit(f"MATCHAWARDS_TRUSTED_PROXIES must be comma-separated CIDRs: {e}") from None
    global_per_min = _env_int("MATCHAWARDS_GLOBAL_PER_MIN", 1200)
    egress_per_min = _env_int("MATCHAWARDS_EGRESS_PER_MIN", 600)
    if global_per_min <= egress_per_min:
        raise SystemExit(f"MATCHAWARDS_GLOBAL_PER_MIN ({global_per_min}) must be above "
                         f"MATCHAWARDS_EGRESS_PER_MIN ({egress_per_min}): ChatGPT alone could use the whole cap")
    try:
        snapshot = parse_egress(json.loads(EGRESS_SNAPSHOT.read_text()))
    except (OSError, ValueError):
        snapshot = None
    if snapshot is None:
        raise SystemExit(f"the bundled ChatGPT egress list {EGRESS_SNAPSHOT} has no usable prefix (see the log)")
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,  # enforced on the bytes read, whatever Content-Length said
        # Host check (DNS rebinding): a Host outside the list gets 421, a foreign Origin 403.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts + [f"{h}:*" for h in hosts],
            allowed_origins=[f"{s}://{h}" for h in hosts for s in ("https", "http")]
            + [f"{s}://{h}:*" for h in hosts for s in ("https", "http")],
        ),
    )
    app.add_middleware(
        HttpGuard,
        limiter=RateLimiter(_env_int("MATCHAWARDS_RATE_PER_MIN", 30), _env_int("MATCHAWARDS_RATE_BURST", 10)),
        # All clients together: bounds the load on the API, which does not rate-limit this server's address.
        # All /64s of one IPv6 /48 together.
        prefix_limiter=RateLimiter(_env_int("MATCHAWARDS_RATE_PER_PREFIX_MIN", 120), 0),
        # Above MATCHAWARDS_EGRESS_PER_MIN (checked above): ChatGPT traffic alone can use its whole pool, and
        # everyone else still needs room under the global cap.
        global_limiter=RateLimiter(global_per_min, 0),
        # ChatGPT traffic (from OpenAI's egress addresses): per x-openai-subject, and all of it together.
        subject_limiter=RateLimiter(_env_int("MATCHAWARDS_RATE_PER_SUBJECT_MIN", 30),
                                    _env_int("MATCHAWARDS_RATE_SUBJECT_BURST", 10)),
        egress_limiter=RateLimiter(egress_per_min, 0),
        egress=EgressRanges(snapshot),
        allowed_hosts={h.lower() for h in hosts},
        trusted_proxies=trusted,
        usage_salt=os.environ.get("MATCHAWARDS_USAGE_SALT", "").encode() or None,
    )
    return app


def metrics_app(token: str) -> Starlette:
    """GET /metrics in the Prometheus text format, for its own listener (MATCHAWARDS_METRICS_HOST/PORT) only.

    Not on the /mcp app: that port trusts X-Real-IP from the proxy, so it is never the one published to the
    internal network. Bearer `token` required; anything else on this listener is 404."""
    expected = f"Bearer {token}".encode()

    async def serve_metrics(request: Request) -> Response:
        if not hmac.compare_digest(request.headers.get("authorization", "").encode("latin-1"), expected):
            return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return PlainTextResponse(usage.metrics.render(), media_type="text/plain; version=0.0.4")

    return Starlette(routes=[Route("/metrics", serve_metrics, methods=["GET"])])


def main(argv: list[str] | None = None) -> None:
    """Console entry point: stdio by default, `--http` for the hosted mode, `stats` to read the usage ledger."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["stats"]:
        usage.stats_main(argv[1:])
        return
    parser = argparse.ArgumentParser(prog="matchawards-mcp", description="MatchAwards MCP server.")
    parser.add_argument(
        "--http", action="store_true",
        help="serve stateless Streamable HTTP at /mcp (MATCHAWARDS_HTTP_HOST, MATCHAWARDS_HTTP_PORT) instead of stdio",
    )
    if not parser.parse_args(argv).http:
        mcp.run(transport="stdio")
        return

    import uvicorn

    logging.getLogger().setLevel(logging.WARNING)  # the SDK logs INFO lines per request; keep one access line
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    access_log.addHandler(handler)
    access_log.setLevel(logging.INFO)
    access_log.propagate = False
    options = dict(
        host=os.environ.get("MATCHAWARDS_HTTP_HOST", "127.0.0.1"),
        port=_env_int("MATCHAWARDS_HTTP_PORT", 8765),
        access_log=False,  # HttpGuard writes the one access line, without the raw client address
        proxy_headers=False,  # the client address comes from X-Real-IP only, never X-Forwarded-For
        server_header=False,
        limit_concurrency=100,  # past this many open connections uvicorn answers 503
        timeout_keep_alive=5,
    )
    token = os.environ.get("MATCHAWARDS_METRICS_TOKEN", "")
    if not token:
        uvicorn.run(http_app(), **options)
        return
    metrics_options = dict(
        host=os.environ.get("MATCHAWARDS_METRICS_HOST", "127.0.0.1"), port=_env_int("MATCHAWARDS_METRICS_PORT", 9765),
        access_log=False, proxy_headers=False, server_header=False, limit_concurrency=10, timeout_keep_alive=5,
    )
    asyncio.run(_serve_both(uvicorn, uvicorn.Config(http_app(), **options),
                            uvicorn.Config(metrics_app(token), **metrics_options)))


async def _serve_both(uvicorn, main_config, metrics_config) -> None:
    """The /mcp server and the metrics listener in one process. The /mcp server owns the signals (Ctrl+C, SIGTERM);
    when it stops, the metrics listener stops too."""

    class MetricsServer(uvicorn.Server):
        def capture_signals(self):
            return contextlib.nullcontext()

    metrics_server = MetricsServer(metrics_config)
    metrics_task = asyncio.create_task(metrics_server.serve())
    try:
        await uvicorn.Server(main_config).serve()
    finally:
        metrics_server.should_exit = True
        await metrics_task
