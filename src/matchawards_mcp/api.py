"""The public matchawards.com API: one shared HTTP client, error wording the model can act on, search paging and
agency-name resolution.

Every call is a GET under `{MATCHAWARDS_API_BASE}/api/public/v1/`: `opportunities` and `awards` (a search may follow
the cursor for up to MAX_PAGES requests), `opportunities/{id}`, `companies/{uei}`, `naics` and `agencies`.
"""

import asyncio
import contextvars
import math
import os
import re
import time
import unicodedata
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from . import __version__
from .schemas import OpportunityRow

API_BASE = os.environ.get("MATCHAWARDS_API_BASE", "https://matchawards.com").rstrip("/")
USER_AGENT = f"matchawards-mcp/{__version__} (+https://github.com/matchawards/matchawards-mcp)"
TIMEOUT_S = 10.0  # the API answers within about 2 s by design
SEARCH_PATH = "/api/public/v1/opportunities"
AWARDS_PATH = "/api/public/v1/awards"
AGENCIES_PATH = "/api/public/v1/agencies"
COMPANIES_PATH = "/api/public/v1/companies"
NAICS_PATH = "/api/public/v1/naics"
MAX_PAGES = 3  # API requests one search tool call may make while filling `limit`
PAGE_LIMIT = 50  # the API's largest `limit`

# HTTP mode only: API calls per minute for the whole server (the API does not rate-limit this server's address,
# and one search can make up to MAX_PAGES calls). None in stdio mode, where the API's own per-IP limit applies.
# Tools that combine several API calls in one call may need it raised, together with the API side's allowlist
# for this server's address (PUBLIC_API_ALLOW_IPS), which is what lets this server past the API's per-IP limit.
# server.http_app() sets it.
api_budget = None
# HTTP mode only: API requests in flight for the whole server (MATCHAWARDS_API_MAX_IN_FLIGHT). The rate alone does
# not protect the API's database: slow searches hold a database connection each, so what matters is how many run at
# once. A request waits up to SLOT_WAIT_S for a free slot, then the tool answers busy. None in stdio mode.
# server.http_app() sets it.
api_slots: asyncio.Semaphore | None = None
SLOT_WAIT_S = 5.0
SLOT_DEADLINE_MARGIN_S = 0.05

# HTTP mode only: called after every upstream request with (endpoint, outcome, seconds) for mcp_api_duration_seconds;
# server.http_app() sets it. None in stdio mode, which records nothing.
on_request = None
# HTTP mode only: the upstream requests of the tool call being served, as a one-item list the tool wrapper sets and
# reads (mcp_tool_api_pages). A list, so the tasks a tool starts (show_results' fetches) add to the same count.
tool_requests: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("matchawards_tool_requests",
                                                                                 default=None)
# The endpoint label of an exact API path (endpoint()).
ENDPOINTS = {SEARCH_PATH: "search", AWARDS_PATH: "awards", AGENCIES_PATH: "agencies", NAICS_PATH: "naics"}

# Tests swap this for an httpx.AsyncClient on a MockTransport.
client = httpx.AsyncClient(
    base_url=API_BASE,
    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    timeout=TIMEOUT_S,
)


def fail(outcome: str, message: str) -> ToolError:
    """A ToolError tagged with its usage-ledger outcome. An untagged ToolError is recorded as upstream_error;
    bad_response is a reply in a shape the tools cannot read."""
    e = ToolError(message)
    e.outcome = outcome
    return e


def _error_code(r: httpx.Response) -> str | None:
    """The `error` field of a JSON error body, or None."""
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def error_message(r: httpx.Response) -> str:
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
    if body.get("error") == "timeout":  # the API's own time budget (503 with Retry-After)
        wait = r.headers.get("Retry-After") or 60
        return (
            f"MatchAwards took too long on this request. Retry in {wait} seconds; if it happens again, narrow the "
            "search (fewer NAICS codes, a shorter time window)."
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


def endpoint(path: str) -> str:
    """The endpoint label of an API path, from a fixed set: search, awards, agencies, naics, opportunity (one
    record), company, or other. Never the path itself (it holds ids)."""
    if path in ENDPOINTS:
        return ENDPOINTS[path]
    if path.startswith(SEARCH_PATH + "/"):
        return "opportunity"
    return "company" if path.startswith(COMPANIES_PATH + "/") else "other"


# A request cancelled this close to (or past) its deadline was cut off by it: wait_for may fire within the clock's
# resolution of the deadline.
DEADLINE_SLACK_S = 0.01


def _outcome(r: httpx.Response) -> str:
    """The mcp_api_duration_seconds outcome of an API answer: ok (below 400), timeout (the API's own timeout
    answer: 503 with error 'timeout'), client_error (any other 4xx: a bad parameter or id, 401/403 from /me, 429)
    or error (5xx)."""
    if r.status_code < 400:
        return "ok"
    if r.status_code == 503 and _error_code(r) == "timeout":
        return "timeout"
    return "client_error" if r.status_code < 500 else "error"


async def send(http: httpx.AsyncClient, label: str, url: str, deadline: float | None = None,
               **kwargs) -> tuple[httpx.Response, str]:
    """(http.get(url, **kwargs), its _outcome), counted in the tool call's requests (tool_requests) and timed
    (on_request) under `label`. Changes nothing about the request.

    No answer within the client timeout is outcome timeout, as is a cancellation at `deadline` (event-loop time:
    asyncio.wait_for cut the request off at a tool's time limit); any other transport failure is error. Any other
    cancellation (the client went away, shutdown) is not an API outcome and records nothing."""
    if (count := tool_requests.get()) is not None:
        count[0] += 1
    start, outcome = time.monotonic(), None
    try:
        r = await http.get(url, **kwargs)
        outcome = _outcome(r)
        return r, outcome
    except httpx.TimeoutException:
        outcome = "timeout"
        raise
    except asyncio.CancelledError:
        if deadline is not None and asyncio.get_running_loop().time() >= deadline - DEADLINE_SLACK_S:
            outcome = "timeout"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        if on_request is not None and outcome is not None:
            on_request(label, outcome, time.monotonic() - start)


async def _send_in_slot(path: str, deadline: float | None, query: dict[str, Any]) -> tuple[httpx.Response, str]:
    """send() the GET for get(), holding one of api_slots for its duration when there are slots (HTTP mode), and
    spending api_budget once a slot is held, so a request that never gets a slot spends nothing. No free slot within
    SLOT_WAIT_S is busy, before anything is sent; with a `deadline` the wait ends SLOT_DEADLINE_MARGIN_S before it,
    so a saturated server answers busy rather than the caller's wait_for cutting the wait off as a timeout."""
    if api_slots is None:
        _spend_budget()
        return await send(client, endpoint(path), path, deadline, params=query)
    wait = SLOT_WAIT_S
    if deadline is not None:
        wait = min(wait, deadline - asyncio.get_running_loop().time() - SLOT_DEADLINE_MARGIN_S)
    try:
        if wait <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(api_slots.acquire(), wait)
    except asyncio.TimeoutError:
        retry = max(1, math.ceil(SLOT_WAIT_S))
        raise fail("busy", f"MatchAwards is busy right now. Retry in {retry} seconds.") from None
    try:
        _spend_budget()
        return await send(client, endpoint(path), path, deadline, params=query)
    finally:
        api_slots.release()


def _spend_budget() -> None:
    """One call from api_budget (HTTP mode); busy when it is spent."""
    if api_budget is not None and (wait := api_budget.check("api")):
        raise fail("busy", f"MatchAwards is busy right now. Retry in {wait} seconds.")


async def get(path: str, params: dict[str, Any] | None = None, not_found: str | None = None,
              deadline: float | None = None) -> dict[str, Any]:
    """GET one API path; turn every failure into a ToolError the model can act on. `not_found` replaces the
    wording of a 404. `deadline`: the event-loop time a caller's asyncio.wait_for cuts it off at (timing only)."""
    query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}  # "" = unset, e.g. cursor=""
    try:
        r, outcome = await _send_in_slot(path, deadline, query)
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
        raise fail("rate_limited_upstream", error_message(r))
    if outcome == "timeout":  # the API's own timeout answer
        raise fail("upstream_timeout", error_message(r))
    if r.status_code == 404 and not_found:
        raise fail("invalid_input", not_found)
    if r.status_code >= 400:  # 4xx: the API rejected the parameters or the id (grants_unavailable is a 503)
        raise fail("upstream_error" if r.status_code >= 500 else "invalid_input", error_message(r))
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise fail(
            "bad_response",
            f"MatchAwards returned an unexpected response (HTTP {r.status_code}, not a JSON object). Retry later.",
        )
    return data


async def get_one(id: str, deadline: float | None = None) -> dict[str, Any]:
    """One opportunity: the API answers {as_of, result: {row..., description, contacts}}; return the row with as_of.
    `deadline` as in get()."""
    data = await get(f"{SEARCH_PATH}/{id}", deadline=deadline)
    result = data.get("result")
    if not isinstance(result, dict):
        raise fail("bad_response", "MatchAwards returned an unexpected API response (no result object).")
    return {**result, "as_of": data.get("as_of")}


async def search(type_: str, limit: int = 20, keyword: str | None = None, *, path: str = SEARCH_PATH,
                 model: type[BaseModel] = OpportunityRow, pages: int = MAX_PAGES, deadline: float | None = None,
                 **filters: Any) -> dict[str, Any]:
    """Collect up to `limit` unique valid rows of `model` from `path`. A sparse filter can return a short page,
    even an empty one, with has_more, so follow next_cursor with the same filters, for at most `pages` requests
    (each asks for at most PAGE_LIMIT rows), and none past `deadline` (event-loop time).

    A row that is not a valid `model` with an id is skipped: it does not count toward `limit` and is reported in
    `warning`, so one bad row does not fail the search. Returns {as_of, results, has_more, next_cursor}, plus
    `warning` when a follow-up page failed (then also `stopped`: True), rows were skipped, or the pages ran out
    with no row and more to scan."""
    if filters.get("naics"):
        filters["naics"] = ",".join(c.strip() for c in filters["naics"].split(","))
    if filters.get("state"):
        filters["state"] = filters["state"].upper()
    params = {"type": type_, "q": keyword, **filters}
    rows: dict[str, BaseModel] = {}
    page: dict[str, Any] = {}
    as_of = warning = None
    skipped = 0
    stopped = False
    sent = 0  # requests made (the composite tools share a page budget)
    for _ in range(pages):
        try:
            # Ask only for what is still missing, so no row is read past the cursor and then dropped.
            left = None if deadline is None else deadline - asyncio.get_running_loop().time()
            try:
                if left is not None and left <= 0:
                    raise asyncio.TimeoutError  # out of time: no request
                sent += 1
                next_page = await asyncio.wait_for(
                    get(path, {**params, "limit": min(limit - len(rows), PAGE_LIMIT)}, deadline=deadline), left)
            except asyncio.TimeoutError:  # not the builtin TimeoutError before Python 3.11
                raise fail("upstream_timeout", "MatchAwards did not answer within this tool's time limit.") from None
            results = next_page.get("results")
            if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                raise fail("bad_response",
                           "MatchAwards returned an unexpected API response (results is not a list of objects).")
        except ToolError as e:
            if not page:
                raise  # first page: nothing to return, so the call fails
            # Follow-up page: keep what we have; the last good page's next_cursor still points at the rest.
            warning = f"Stopped after {len(rows)} rows: {e} More results may exist; call again with next_cursor."
            stopped = True
            break
        page = next_page
        as_of = as_of or page.get("as_of")
        for raw in results:
            try:
                row = model.model_validate(raw)
            except ValidationError:
                row = None
            if row is None or row.id is None:
                skipped += 1
                continue
            rows.setdefault(row.id, row)
        if len(rows) >= limit or not (page.get("has_more") and page.get("next_cursor")):
            break
        params["cursor"] = page["next_cursor"]
    out = {
        "as_of": as_of,
        "results": list(rows.values()),
        "has_more": bool(page.get("has_more")),
        "next_cursor": page.get("next_cursor"),
    }
    if not rows and not stopped and out["has_more"] and out["next_cursor"]:
        # A sparse filter (an agency, a rare name) scans newest first in slices: the pages ran out before a match.
        cursor = out["next_cursor"]
        again = f"cursor={cursor}" if re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", cursor) else "next_cursor"
        warning = f"No matches in the newest results scanned; more may exist further back. Call again with {again} " \
                  "to keep searching."
    if skipped:
        warning = " ".join(filter(None, [warning, f"{skipped} result(s) skipped: unexpected format."]))
    out["requests"] = sent  # internal: dropped by the output models
    if stopped:
        out["stopped"] = True
    return {**out, "warning": warning} if warning else out


def clean(value: Any, cap: int = 120) -> str:
    """Upstream text for a note or a why line: control characters and line breaks out, spaces collapsed, at most
    `cap` characters."""
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in str(value))
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


# Agency name -> agency ids, for the `agency` filter. Agencies are MatchAwards accounts, and one department can have
# several (dept_of_defense and department_of_defense), so a name resolves to the best match plus the other
# accounts of the same department. AGENCY_SLUGS_PER_QUERY of them are sent as agency= (comma-separated).
AGENCY_SLUGS_PER_QUERY = 1
AGENCY_CANDIDATES = 25  # asked of /agencies before ranking (its maximum)
# Real ids hold hyphens and run past 100 characters ('..._university_-_research_grants').
AGENCY_ID = re.compile(r"[a-z0-9_-]{1,200}")
AGENCY_TTL_S = 600
AGENCY_CACHE_MAX = 512
_agencies: dict[str, tuple[float, list[str], str]] = {}  # query (case-folded) -> (expiry, ids, note)


def dept_key(name: str) -> str:
    """A department name compared loosely: 'United States Department of Defense' and 'Department Of Defense' are
    both 'department of defense'."""
    text = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("u.s.", " "))
    return " ".join(re.sub(r"\b(united states|us)\b", " ", text).split())


async def resolve_agency(name: str) -> tuple[list[str], str]:
    """The agency ids for an agency name, best first, and a note saying which agency was used (and the other
    matches). Ranking: an exact case-insensitive name match, then names starting with `name`, then the API's order.
    No match is not an error: the ids are [] and the note says the search covers all agencies. Cached in-process
    for AGENCY_TTL_S."""
    key = " ".join(name.split()).casefold()
    hit = _agencies.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1], hit[2]
    data = await get(AGENCIES_PATH, {"q": " ".join(name.split()), "limit": AGENCY_CANDIDATES})
    results = data.get("results") if isinstance(data.get("results"), list) else []
    # An id goes into agency= as is, so only ids in the slug alphabet are used (never a comma, slash or dot).
    found = [{"agency_id": a["agency_id"], "name": clean(a["name"])} for a in results if isinstance(a, dict)
             and isinstance(a.get("agency_id"), str) and AGENCY_ID.fullmatch(a["agency_id"])
             and isinstance(a.get("name"), str) and a["name"].strip()]
    found.sort(key=lambda a: 0 if a["name"].casefold() == key else 1 if a["name"].casefold().startswith(key) else 2)
    if not found:
        ids, note = [], f"No agency matched '{clean(name)}'; showing all agencies."
    else:
        best = found[0]
        ids = [best["agency_id"]] + [a["agency_id"] for a in found[1:] if dept_key(a["name"]) == dept_key(best["name"])]
        note = f"Filtered by agency: {best['name']} ({best['agency_id']})."
        if others := [a["name"] for a in found[1:4]]:
            note += f" Other matches: {', '.join(others)}."
    if len(_agencies) >= AGENCY_CACHE_MAX:
        del _agencies[next(iter(_agencies))]  # the oldest entry
    _agencies[key] = (time.monotonic() + AGENCY_TTL_S, ids, note)
    return ids, note
