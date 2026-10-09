"""The public matchawards.com API: one shared HTTP client, error wording the model can act on, and search paging.

Every call is a GET to `{MATCHAWARDS_API_BASE}/api/public/v1/opportunities` (a search may follow the cursor for up
to MAX_PAGES requests) or to `.../opportunities/{id}`.
"""

import os
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from . import __version__
from .schemas import OpportunityRow

API_BASE = os.environ.get("MATCHAWARDS_API_BASE", "https://matchawards.com").rstrip("/")
USER_AGENT = f"matchawards-mcp/{__version__} (+https://github.com/matchawards/matchawards-mcp)"
TIMEOUT_S = 10.0  # the API answers within about 2 s by design
SEARCH_PATH = "/api/public/v1/opportunities"
MAX_PAGES = 3  # API requests one search tool call may make while filling `limit`

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
    if body.get("error") == "grants_unavailable":
        return "Grant search is temporarily unavailable on MatchAwards. Try again later; contracts and jobs still work."
    if r.status_code >= 500:
        return f"MatchAwards server error (HTTP {r.status_code}). It is temporary; retry in a minute."
    if r.status_code == 400 and body.get("parameter"):
        return f"MatchAwards rejected the parameter '{body['parameter']}': {error}. Fix it and call again."
    if r.status_code == 404:
        return f"Not found on MatchAwards ({error}). Use an id from a search result."
    return f"MatchAwards rejected the request (HTTP {r.status_code}): {error}"


async def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET one API path; turn every failure into a ToolError the model can act on."""
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


async def search(type_: str, limit: int = 20, keyword: str | None = None, **filters: Any) -> dict[str, Any]:
    """Collect up to `limit` unique valid rows. A sparse filter can return a short page with has_more, so
    follow next_cursor with the same filters, for at most MAX_PAGES requests.

    A row that is not a valid OpportunityRow is skipped: it does not count toward `limit` and is reported in
    `warning`, so one bad row does not fail the search. Returns {as_of, results (OpportunityRow), has_more,
    next_cursor}, plus `warning` when a follow-up page failed or rows were skipped."""
    if filters.get("naics"):
        filters["naics"] = ",".join(c.strip() for c in filters["naics"].split(","))
    if filters.get("state"):
        filters["state"] = filters["state"].upper()
    params = {"type": type_, "q": keyword, **filters}
    rows: dict[str, OpportunityRow] = {}
    page: dict[str, Any] = {}
    as_of = warning = None
    skipped = 0
    for _ in range(MAX_PAGES):
        try:
            # Ask only for what is still missing, so no row is read past the cursor and then dropped.
            next_page = await get(SEARCH_PATH, {**params, "limit": limit - len(rows)})
            results = next_page.get("results")
            if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                raise fail("bad_response",
                           "MatchAwards returned an unexpected API response (results is not a list of objects).")
        except ToolError as e:
            if not page:
                raise  # first page: nothing to return, so the call fails
            # Follow-up page: keep what we have; the last good page's next_cursor still points at the rest.
            warning = f"Stopped after {len(rows)} rows: {e} More results may exist; call again with next_cursor."
            break
        page = next_page
        as_of = as_of or page.get("as_of")
        for raw in results:
            try:
                row = OpportunityRow.model_validate(raw)
            except ValidationError:
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
    if skipped:
        warning = " ".join(filter(None, [warning, f"{skipped} result(s) skipped: unexpected format."]))
    return {**out, "warning": warning} if warning else out
