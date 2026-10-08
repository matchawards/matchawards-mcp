"""MatchAwards MCP server: read-only search over the public matchawards.com API.

Six tools, no auth, no state. Each call is a GET to `{MATCHAWARDS_API_BASE}/api/public/v1/opportunities`
(a search tool may follow the cursor for up to MAX_PAGES requests) or to `.../opportunities/{id}`.
Serves stdio by default; `--http` serves the same tools over stateless Streamable HTTP at /mcp.
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
import logging
import math
import os
import re
import sys
import time
from collections import deque
from typing import Annotated, Any, Literal

import httpx
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__, usage

API_BASE = os.environ.get("MATCHAWARDS_API_BASE", "https://matchawards.com").rstrip("/")
USER_AGENT = f"matchawards-mcp/{__version__} (+https://github.com/matchawards/matchawards-mcp)"
TIMEOUT_S = 10.0  # the API answers within about 2 s by design
SEARCH_PATH = "/api/public/v1/opportunities"
MAX_PAGES = 3  # API requests one search tool call may make while filling `limit`

LINK_RULE = (
    "Show each item's url to the user verbatim as a markdown link, [title](url); "
    "it opens the full posting on matchawards.com."
)
INSTRUCTIONS = (
    "Search US government contract opportunities (federal and state), grants and jobs from matchawards.com. "
    f"Read-only. Every result row has a matchawards.com `url`. {LINK_RULE}"
)

# HTTP mode only: API calls per minute for the whole server (the API does not rate-limit this server's address,
# and one search can make up to MAX_PAGES calls). None in stdio mode, where the API's own per-IP limit applies.
api_budget = None

# HTTP mode only: the usage ledger (None when disabled, and always in stdio mode) and the request being served.
ledger: usage.Ledger | None = None
_request: contextvars.ContextVar[dict | None] = contextvars.ContextVar("matchawards_request", default=None)

# Tests swap this for an httpx.AsyncClient on a MockTransport.
client = httpx.AsyncClient(
    base_url=API_BASE,
    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    timeout=TIMEOUT_S,
)

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

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


def _tool(fn):
    """Register a read-only tool; its cleaned docstring is the description the model reads.

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

    return mcp.tool(annotations=READ_ONLY, description=inspect.cleandoc(fn.__doc__))(recorded)


def _fail(outcome: str, message: str) -> ToolError:
    """A ToolError tagged with its usage-ledger outcome. An untagged ToolError is recorded as upstream_error."""
    e = ToolError(message)
    e.outcome = outcome
    return e


# Optional filters are Annotated[X | None, Field(...)] so the description sits at the top level of
# the JSON schema while the constraint still applies to the non-null value.
_NAICS = Field(
    pattern=r"^\s*\d{6}(\s*,\s*\d{6}){0,9}\s*$",
    description="Up to 10 six-digit NAICS codes, comma-separated, e.g. '236220' or '541511,541512'. "
    "Unknown codes are ignored.",
)
Naics = Annotated[str, _NAICS]
OptNaics = Annotated[str | None, _NAICS]
State = Annotated[
    str | None,
    Field(pattern=r"^[A-Za-z]{2}$", description="Two-letter US state code, e.g. 'VA'. Many federal notices "
          "have no state, so this filter skips them."),
]
Keyword = Annotated[
    str | None, Field(min_length=2, max_length=100, description="2 to 100 characters matched against the title only.")
]
SetAside = Annotated[str | None, Field(description="Set-aside code, e.g. 'SBA' or 'SDVOSBC'. Federal contracts only.")]
PostedWithin = Annotated[
    int | None, Field(ge=1, le=180, description="Only items posted in the last N days, 1 to 180 (default 180).")
]
JobsPostedWithin = Annotated[int, Field(ge=1, le=30, description="Only jobs posted in the last N days, 1 to 30.")]
Open = Annotated[
    bool | None,
    Field(description="true (the default): only items with a response deadline of today or later, US Eastern; "
          "an item with no deadline counts as closed. false: include closed ones."),
]
Limit = Annotated[int, Field(ge=1, le=50, description="Number of results to return, 1 to 50.")]
Cursor = Annotated[
    str | None,
    Field(description="next_cursor from the previous response. Pass it with the same filters to get the next page."),
]
OppId = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$", description="The id field of a search result.")
]
NaicsType = Annotated[
    Literal["contract", "federal", "state", "grant", "job"],
    Field(description="contract = federal and state contracts; federal or state = one level only; grant; job."),
]


def _error_message(r: httpx.Response) -> str:
    """Turn an API error response ({error, parameter?} JSON) into advice the model can act on."""
    try:
        body = r.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    error = body.get("message") or body.get("error") or r.text.strip()[:200] or r.reason_phrase
    if r.status_code == 429:
        wait = r.headers.get("Retry-After") or body.get("retry_after") or 60
        return (
            f"MatchAwards rate limit reached (per IP: 60 requests per minute, 20 per 5 seconds). "
            f"Wait {wait} seconds, then retry."
        )
    if body.get("error") == "grants_unavailable":
        return "Grant search is temporarily unavailable on MatchAwards. Try again later; contracts and jobs still work."
    if r.status_code >= 500:
        return f"MatchAwards server error (HTTP {r.status_code}). It is temporary; retry in a minute."
    if r.status_code == 400 and body.get("parameter"):
        return f"MatchAwards rejected the parameter '{body['parameter']}': {error}. Fix it and call again."
    if r.status_code == 404:
        return f"Not found on MatchAwards ({error}). Use an id from a search result."
    return f"MatchAwards rejected the request (HTTP {r.status_code}): {error}"


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET one API path; turn every failure into a ToolError the model can act on."""
    query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}  # "" = unset, e.g. cursor=""
    if api_budget is not None and (wait := api_budget.check("api")):
        raise _fail("busy", f"MatchAwards is busy right now. Retry in {wait} seconds.")
    try:
        r = await client.get(path, params=query)
    except httpx.TimeoutException:
        raise ToolError(
            f"MatchAwards did not answer within {TIMEOUT_S:.0f} seconds. Retry once; if it fails again, "
            "narrow the search (fewer NAICS codes, a shorter posted_within_days) or try later."
        ) from None
    except httpx.RequestError as e:
        raise ToolError(
            f"Could not reach MatchAwards at {API_BASE} ({type(e).__name__}). Check the network and retry later."
        ) from None
    if r.status_code == 429:
        raise _fail("rate_limited_upstream", _error_message(r))
    if r.status_code >= 400:  # 4xx: the API rejected the parameters or the id (grants_unavailable is a 503)
        raise _fail("upstream_error" if r.status_code >= 500 else "invalid_input", _error_message(r))
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ToolError(
            f"MatchAwards returned an unexpected response (HTTP {r.status_code}, not a JSON object). Retry later."
        )
    return data


async def _get_one(id: str) -> dict[str, Any]:
    """One opportunity: the API answers {as_of, result: {row..., description, contacts}}; return the row with as_of."""
    data = await _get(f"{SEARCH_PATH}/{id}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ToolError("MatchAwards returned an unexpected API response (no result object).")
    return {**result, "as_of": data.get("as_of")}


async def _search(type_: str, limit: int = 20, keyword: str | None = None, **filters: Any) -> dict[str, Any]:
    """Collect up to `limit` unique rows. A sparse filter can return a short page with has_more, so
    follow next_cursor with the same filters, for at most MAX_PAGES requests."""
    if filters.get("naics"):
        filters["naics"] = ",".join(c.strip() for c in filters["naics"].split(","))
    if filters.get("state"):
        filters["state"] = filters["state"].upper()
    params = {"type": type_, "q": keyword, **filters}
    rows: dict[Any, dict[str, Any]] = {}
    page: dict[str, Any] = {}
    as_of = warning = None
    for _ in range(MAX_PAGES):
        try:
            # Ask only for what is still missing, so no row is read past the cursor and then dropped.
            next_page = await _get(SEARCH_PATH, {**params, "limit": limit - len(rows)})
            results = next_page.get("results")
            if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                raise ToolError("MatchAwards returned an unexpected API response (results is not a list of objects).")
        except ToolError as e:
            if not page:
                raise  # first page: nothing to return, so the call fails
            # Follow-up page: keep what we have; the last good page's next_cursor still points at the rest.
            warning = f"Stopped after {len(rows)} rows: {e} More results may exist; call again with next_cursor."
            break
        page = next_page
        as_of = as_of or page.get("as_of")
        for row in results:
            rows.setdefault(row.get("id"), row)
        if len(rows) >= limit or not (page.get("has_more") and page.get("next_cursor")):
            break
        params["cursor"] = page["next_cursor"]
    out = {
        "as_of": as_of,
        "results": list(rows.values()),
        "has_more": bool(page.get("has_more")),
        "next_cursor": page.get("next_cursor"),
        "note": LINK_RULE,
    }
    return {**out, "warning": warning} if warning else out


@_tool
async def search_contracts(
    naics: OptNaics = None,
    state: State = None,
    keyword: Keyword = None,
    set_aside: SetAside = None,
    type: Annotated[
        Literal["federal", "state"] | None,
        Field(description="Narrow to federal or to state contracts. Leave empty for both."),
    ] = None,
    posted_within_days: PostedWithin = None,
    open: Open = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Search US federal and state government contract opportunities (bids, RFPs, solicitations).

    Use this when the user wants contracts to bid on. Returns {as_of, results, has_more, next_cursor};
    each result has id, type, title, agency, office, naics {code, title}, set_aside, state, city,
    posted_at, response_deadline, url (matchawards.com), sam_url and solicitation_number.
    Filters: naics, state, keyword, set_aside (federal only), type (federal or state), posted_within_days,
    open (default true), limit, cursor. If has_more is true, call again with cursor=next_cursor and the
    same filters. Example: naics="238160", state="VA", keyword="roof".
    For grants use search_grants, for jobs search_jobs.
    """
    return await _search(
        type or "contract", naics=naics, state=state, keyword=keyword, set_aside=set_aside,
        posted_within_days=posted_within_days, open=open, limit=limit, cursor=cursor,
    )


@_tool
async def search_grants(
    naics: OptNaics = None,
    keyword: Keyword = None,
    posted_within_days: PostedWithin = None,
    open: Open = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Search US federal grant and funding opportunities.

    Use this when the user wants grants or funding, not contracts. Returns {as_of, results, has_more,
    next_cursor}; each result has id, title, agency, naics, posted_at, response_deadline and a matchawards.com
    url. Filters: naics, keyword, posted_within_days, open (default true: open grants of any age), limit,
    cursor. Grant notices carry no state, so there is no state filter. If has_more is true, call again with
    cursor=next_cursor and the same filters. Example: keyword="broadband" or naics="541715".
    """
    return await _search(
        "grant", naics=naics, keyword=keyword, posted_within_days=posted_within_days, open=open, limit=limit,
        cursor=cursor,
    )


@_tool
async def search_jobs(
    naics: OptNaics = None,
    state: State = None,
    keyword: Keyword = None,
    posted_within_days: JobsPostedWithin = 30,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Search job postings from the last 30 days listed on MatchAwards.

    Use this when the user is looking for a job or for hiring signals, not for contracts or grants.
    Returns {as_of, results, has_more, next_cursor}; each result has id, title, agency (the employer),
    state, city, posted_at and a matchawards.com url; other fields are null. Filters: naics, state,
    keyword, posted_within_days (up to 30), limit, cursor. If has_more is true, call again with
    cursor=next_cursor and the same filters. Example: keyword="electrician", state="TX", posted_within_days=7.
    """
    return await _search(
        "job", naics=naics, state=state, keyword=keyword,
        posted_within_days=posted_within_days, limit=limit, cursor=cursor,
    )


@_tool
async def search_by_naics(naics: Naics, type: NaicsType = "contract", cursor: Cursor = None) -> dict[str, Any]:
    """List the newest opportunities for one or more NAICS industry codes.

    Use this when the user gives NAICS codes and nothing else. Returns up to 20 results, {as_of, results,
    has_more, next_cursor}, each with a matchawards.com url. Filters: naics (required), type (contract,
    federal, state, grant or job), cursor (next_cursor of the previous call, same naics and type). For
    keyword, state or other filters use search_contracts, search_grants or search_jobs.
    Example: naics="541511,541512", type="federal".
    """
    return await _search(type, naics=naics, cursor=cursor)


@_tool
async def get_opportunity(id: OppId) -> dict[str, Any]:
    """Get the full record of one opportunity by its id from a search result.

    Use this when the user wants details on one result. Returns the search row plus description
    (up to 2000 characters) and contacts [{name, title}], with the matchawards.com url. Example: id="abc123".
    """
    return {**await _get_one(id), "note": LINK_RULE}


@_tool
async def find_contacts(id: OppId) -> dict[str, Any]:
    """Get only the published points of contact for one opportunity by its id.

    Use this when the user asks who to contact about a result. Returns {id, title, url, contacts}, where
    contacts is [{name, title}] as the public matchawards.com page shows them; an empty list means none
    are published. Example: id="abc123".
    """
    data = await _get_one(id)
    return {
        "id": data.get("id", id),
        "title": data.get("title"),
        "url": data.get("url"),
        "contacts": data.get("contacts") or [],
    }


# --- HTTP mode -------------------------------------------------------------------------------------------

access_log = logging.getLogger("matchawards_mcp.access")
_LOG_SALT = os.urandom(16)  # client keys in the log are hashed with a per-process salt, never written raw
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


def client_key(scope, trusted_proxies) -> str:
    """The rate-limit key: the socket peer, or X-Real-IP when the peer is a trusted proxy. IPv6 is keyed per /64.

    X-Real-IP from any other peer is ignored, so a client cannot pick its own key. A value that is not a valid IP
    is ignored too (the peer is used). "unknown" only happens with no TCP peer at all (a unix socket)."""
    ip = peer = _ip((scope.get("client") or ("",))[0])
    if peer is not None and any(peer in net for net in trusted_proxies):
        real_ip = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-real-ip"), "")
        ip = _ip(real_ip) or peer
    if ip is None:
        return "unknown"
    return str(ipaddress.ip_network((ip, 64), strict=False)) if ip.version == 6 else str(ip)


def prefix_key(key: str) -> str | None:
    """The /48 around an IPv6 client key ('2001:db8:1:2::/64' -> '2001:db8:1::/48'); None for IPv4 and 'unknown'.

    One subscriber usually holds a /56 or a /48, so up to 65,536 /64s to rotate through. The /48 gets its own
    shared limit on top of the per-/64 one, so rotating cannot get past it, while separate users behind one /48
    (a carrier, a company, a cloud egress) still get their own per-/64 limits inside it."""
    return str(ipaddress.ip_network(key).supernet(new_prefix=48)) if ":" in key else None


def _host_name(host: str) -> str:
    """The Host header without its port, lower-cased: 'matchawards.com:443' -> 'matchawards.com'."""
    name, sep, port = host.rpartition(":")
    return (name if sep and port.isdigit() else host).lower()


class HttpGuard:
    """Runs before the MCP layer, in this order: path (404), Host (421), method (405), body size (411/413),
    per-client then global rate limit (429). Writes one access line per request.

    A rejection is counted in mcp_rejected_total{reason}. A request passed on to /mcp is counted by JSON-RPC method
    and User-Agent family, added to the daily transport aggregate, and made visible to the tool wrapper (_tool)."""

    def __init__(self, app, limiter: RateLimiter, prefix_limiter: RateLimiter, global_limiter: RateLimiter,
                 trusted_proxies, allowed_hosts, usage_salt: bytes | None = None):
        self.app, self.limiter, self.prefix_limiter, self.global_limiter = app, limiter, prefix_limiter, global_limiter
        self.trusted_proxies, self.allowed_hosts, self.usage_salt = trusted_proxies, allowed_hosts, usage_salt

    def reject(self, scope, path: str, key: str) -> tuple[str, Response] | None:
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
        # the global count (IPv4 address 30/min, IPv6 /48 120/min, so 600 needs 5+ /48s or 20+ IPv4 addresses),
        # and a lockout at one level does not use up the budgets of the others. A distributed flood from that many
        # sources can still trip the global cap for everyone; Cloudflare in front of nginx is the outer layer.
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
        start, status, key = time.monotonic(), 0, client_key(scope, self.trusted_proxies)
        path = re.sub(r"/+", "/", scope["path"]).rstrip("/") or "/"  # '//mcp' and '/mcp/' are '/mcp'

        async def send_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            if rejected := self.reject(scope, path, key):
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
    global api_budget, ledger
    api_budget = RateLimiter(_env_int("MATCHAWARDS_GLOBAL_API_PER_MIN", 900), 0)
    ledger = usage.Ledger.open(os.environ.get("MATCHAWARDS_USAGE_DB", usage.DEFAULT_DB))
    # Peers whose X-Real-IP is believed. Loopback only by default; in Docker the deployment names the exact
    # address nginx connects from (the compose network's gateway), never a broad private range.
    try:
        trusted = [ipaddress.ip_network(n.strip()) for n in os.environ.get(
            "MATCHAWARDS_TRUSTED_PROXIES", "127.0.0.1/32,::1/128").split(",") if n.strip()]
    except ValueError as e:
        raise SystemExit(f"MATCHAWARDS_TRUSTED_PROXIES must be comma-separated CIDRs: {e}") from None
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
        global_limiter=RateLimiter(_env_int("MATCHAWARDS_GLOBAL_PER_MIN", 600), 0),
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
