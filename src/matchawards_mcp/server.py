"""MatchAwards MCP server: read-only search over the public matchawards.com API.

Six tools, no auth, no state. Each call is a GET to `{MATCHAWARDS_API_BASE}/api/public/v1/opportunities`
(a search tool may follow the cursor for up to MAX_PAGES requests) or to `.../opportunities/{id}`.
"""

import inspect
import os
from typing import Annotated, Any, Literal

import httpx
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__

API_BASE = os.environ.get("MATCHAWARDS_API_BASE", "https://matchawards.com").rstrip("/")
USER_AGENT = f"matchawards-mcp/{__version__} (+https://github.com/matchawards/matchawards-mcp)"
TIMEOUT_S = 20.0
SEARCH_PATH = "/api/public/v1/opportunities"
MAX_PAGES = 3  # API requests one search tool call may make while filling `limit`

POSITIONING = (
    "MatchAwards gives AI agents direct access to continuously updated government and business "
    "contracts, grants, jobs, awards and collaboration opportunities."
)
LINK_RULE = (
    "Show each item's url to the user verbatim as a markdown link, [title](url); "
    "it opens the full posting on matchawards.com."
)

# Tests swap this for an httpx.AsyncClient on a MockTransport.
client = httpx.AsyncClient(
    base_url=API_BASE,
    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    timeout=TIMEOUT_S,
)

mcp = MCPServer(
    name="matchawards",
    title="MatchAwards",
    instructions=f"{POSITIONING}\nEvery result row has a matchawards.com `url`. {LINK_RULE}",
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
    """Register a read-only tool; its cleaned docstring is the description the model reads."""
    return mcp.tool(annotations=READ_ONLY, description=inspect.cleandoc(fn.__doc__))(fn)


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
JobsPostedWithin = Annotated[
    int | None, Field(ge=1, le=30, description="Only jobs posted in the last N days, 1 to 30 (default 30).")
]
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
OppType = Annotated[
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
    error = body.get("error") or r.text.strip()[:200] or r.reason_phrase
    if r.status_code == 429:
        wait = r.headers.get("Retry-After") or body.get("retry_after") or 60
        return f"MatchAwards rate limit reached (60 requests per minute per IP). Wait {wait} seconds, then retry."
    if error == "grants_unavailable":
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
    query = {k: v for k, v in (params or {}).items() if v is not None}
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
    if r.status_code >= 400:
        raise ToolError(_error_message(r))
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ToolError(
            f"MatchAwards returned an unexpected response (HTTP {r.status_code}, not a JSON object). Retry later."
        )
    return data


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
    as_of = None
    for _ in range(MAX_PAGES):
        try:
            # Ask only for what is still missing, so no row is read past the cursor and then dropped.
            next_page = await _get(SEARCH_PATH, {**params, "limit": limit - len(rows)})
        except ToolError:
            if not page:
                raise
            break  # keep what we have; the last good page's next_cursor still points at the rest
        page = next_page
        as_of = as_of or page.get("as_of")
        for row in page.get("results") or []:
            rows.setdefault(row.get("id"), row)
        if len(rows) >= limit or not (page.get("has_more") and page.get("next_cursor")):
            break
        params["cursor"] = page["next_cursor"]
    return {
        "as_of": as_of,
        "results": list(rows.values()),
        "has_more": bool(page.get("has_more")),
        "next_cursor": page.get("next_cursor"),
        "note": LINK_RULE,
    }


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
    state: State = None,
    keyword: Keyword = None,
    posted_within_days: PostedWithin = None,
    open: Open = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Search US government grant and funding opportunities.

    Use this when the user wants grants or funding, not contracts. Returns {as_of, results, has_more,
    next_cursor}; each result has id, title, agency, naics, state, posted_at, response_deadline and a
    matchawards.com url. Filters: naics, state, keyword, posted_within_days, open (default true: open
    grants of any age), limit, cursor. If has_more is true, call again with cursor=next_cursor and the
    same filters. Example: keyword="broadband", state="NC".
    """
    return await _search(
        "grant", naics=naics, state=state, keyword=keyword,
        posted_within_days=posted_within_days, open=open, limit=limit, cursor=cursor,
    )


@_tool
async def search_jobs(
    naics: OptNaics = None,
    state: State = None,
    keyword: Keyword = None,
    posted_within_days: JobsPostedWithin = None,
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
async def search_by_naics(naics: Naics, type: OppType = "contract", cursor: Cursor = None) -> dict[str, Any]:
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
    return {**await _get(f"{SEARCH_PATH}/{id}"), "note": LINK_RULE}


@_tool
async def find_contacts(id: OppId) -> dict[str, Any]:
    """Get only the published points of contact for one opportunity by its id.

    Use this when the user asks who to contact about a result. Returns {id, title, url, contacts}, where
    contacts is [{name, title}] as the public matchawards.com page shows them; an empty list means none
    are published. Example: id="abc123".
    """
    data = await _get(f"{SEARCH_PATH}/{id}")
    return {
        "id": data.get("id", id),
        "title": data.get("title"),
        "url": data.get("url"),
        "contacts": data.get("contacts") or [],
    }


def main() -> None:
    """Console entry point. v0.1.0 serves stdio only.

    The tools live on the module-level `mcp`, so a later `--http` flag can serve the same set with
    `mcp.streamable_http_app(stateless_http=True, json_response=True)` from here.
    """
    mcp.run(transport="stdio")
