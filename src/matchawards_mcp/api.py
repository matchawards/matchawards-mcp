"""The public matchawards.com API: one shared HTTP client, error wording the model can act on, search paging and
agency-name resolution.

Every call is a GET under `{MATCHAWARDS_API_BASE}/api/public/v1/`: `opportunities` and `awards` (a search may follow
the cursor for up to MAX_PAGES requests), `opportunities/{id}`, `companies/{uei}`, `naics` and `agencies`.
"""

import asyncio
import os
import re
import time
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


async def get(path: str, params: dict[str, Any] | None = None, not_found: str | None = None) -> dict[str, Any]:
    """GET one API path; turn every failure into a ToolError the model can act on. `not_found` replaces the
    wording of a 404."""
    query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}  # "" = unset, e.g. cursor=""
    if api_budget is not None and (wait := api_budget.check("api")):
        raise fail("busy", f"MatchAwards is busy right now. Retry in {wait} seconds.")
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
        raise fail("rate_limited_upstream", error_message(r))
    if r.status_code == 503 and _error_code(r) == "timeout":
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


async def get_one(id: str) -> dict[str, Any]:
    """One opportunity: the API answers {as_of, result: {row..., description, contacts}}; return the row with as_of."""
    data = await get(f"{SEARCH_PATH}/{id}")
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
    for _ in range(pages):
        try:
            # Ask only for what is still missing, so no row is read past the cursor and then dropped.
            left = None if deadline is None else deadline - asyncio.get_running_loop().time()
            try:
                if left is not None and left <= 0:
                    raise asyncio.TimeoutError  # out of time: no request
                next_page = await asyncio.wait_for(get(path, {**params, "limit": min(limit - len(rows), PAGE_LIMIT)}),
                                                   left)
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
    if not rows and not warning and out["has_more"] and out["next_cursor"]:
        # A sparse filter (an agency, a rare name) scans newest first in slices: the pages ran out before a match.
        warning = (f"No matches in the newest results scanned; more may exist further back. Call again with "
                   f"cursor={out['next_cursor']} to keep searching.")
    if skipped:
        warning = " ".join(filter(None, [warning, f"{skipped} result(s) skipped: unexpected format."]))
    if stopped:
        out["stopped"] = True
    return {**out, "warning": warning} if warning else out


# Agency name -> agency ids, for the `agency` filter. Agencies are MatchAwards accounts, and one department can have
# several (dept_of_defense and department_of_defense), so a name resolves to the best match plus the other
# accounts of the same department. AGENCY_SLUGS_PER_QUERY of them are sent as agency= (comma-separated).
AGENCY_SLUGS_PER_QUERY = 1
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
    data = await get(AGENCIES_PATH, {"q": " ".join(name.split()), "limit": 10})
    found = [a for a in data.get("results") or [] if isinstance(a, dict)
             and isinstance(a.get("agency_id"), str) and isinstance(a.get("name"), str)]
    found.sort(key=lambda a: 0 if a["name"].casefold() == key else 1 if a["name"].casefold().startswith(key) else 2)
    if not found:
        ids, note = [], f"No agency matched '{name}'; showing all agencies."
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
