"""The tools. Each docstring is the description the model reads, frozen for directory listings: keep them tight.
Paragraphs are separated by a blank line; server.py joins the lines inside a paragraph, so no line break falls
mid-sentence in the published description.

server.py registers TOOLS with their titles, annotations and securitySchemes, and counts every call.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from . import api
from .schemas import OpportunityDetail, SearchResult

LINK_RULE = (
    "Show each item's url to the user verbatim as a markdown link, [title](url); "
    "it opens the full posting on matchawards.com. Show sam_url the same way when it is set."
)
GRANT_NOTE = f"{LINK_RULE} Open status may lag for grants that closed recently: check response_deadline."

# Sent as `source=` on job searches when set (e.g. one job board only). None: the API's default mix.
JOB_SOURCE: str | None = None

# Optional filters are Annotated[X | None, Field(...)] so the description sits at the top level of
# the JSON schema while the constraint still applies to the non-null value.
Naics = Annotated[
    str | None,
    Field(pattern=r"^\s*\d{6}(\s*,\s*\d{6}){0,9}\s*$",
          description="Up to 10 six-digit NAICS codes, comma-separated, e.g. '236220' or '541511,541512'. "
          "Unknown codes are ignored."),
]
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
    int, Field(ge=1, le=30, description="Only jobs posted within the last N days, 1 to 30 (default 30).")
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


def _parse(model: type[BaseModel], data: dict) -> BaseModel:
    """The API's JSON as an output model; JSON the model cannot hold is an upstream fault (bad_response), not a
    crash."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise api.fail(
            "bad_response",
            f"MatchAwards returned an unexpected API response ({e.error_count()} invalid fields). Retry later.",
        ) from None


async def search_contracts(
    naics: Naics = None,
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
) -> SearchResult:
    """Use this when the user wants US federal or state government contract opportunities to bid on: bids, RFPs,
    solicitations, set-asides.

    Filters: naics (up to 10 codes), state, keyword (title only), set_aside (federal only), type (federal or
    state), posted_within_days (1 to 180), open (default true), limit (1 to 50, default 20). If has_more is true,
    call again with cursor=next_cursor and the same filters. Example: naics="238160", state="VA", keyword="roof".

    Print each result's url, and sam_url for federal notices, as markdown links.

    Do not use for grants (search_grants), jobs (search_jobs) or one result's details (get_opportunity).
    """
    out = await api.search(
        type or "contract", naics=naics, state=state, keyword=keyword, set_aside=set_aside,
        posted_within_days=posted_within_days, open=open, limit=limit, cursor=cursor,
    )
    return _parse(SearchResult, {**out, "note": LINK_RULE})


async def search_grants(
    naics: Naics = None,
    keyword: Keyword = None,
    posted_within_days: PostedWithin = None,
    open: Open = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> SearchResult:
    """Use this when the user wants US federal grants or funding opportunities.

    Filters: naics (up to 10 codes), keyword (title only), posted_within_days (1 to 180), open (default true:
    open grants of any age), limit (1 to 50, default 20). Grant notices carry no state. If has_more is true, call
    again with cursor=next_cursor and the same filters. Example: keyword="broadband".

    Open status may lag for grants that closed recently, so check response_deadline.

    Print each result's url as a markdown link.

    Do not use for contracts to bid on (search_contracts) or jobs (search_jobs).
    """
    out = await api.search(
        "grant", naics=naics, keyword=keyword, posted_within_days=posted_within_days, open=open, limit=limit,
        cursor=cursor,
    )
    return _parse(SearchResult, {**out, "note": GRANT_NOTE})


async def search_jobs(
    naics: Naics = None,
    state: State = None,
    keyword: Keyword = None,
    posted_within_days: JobsPostedWithin = 30,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> SearchResult:
    """Use this when the user is looking for a job, or for hiring signals, among postings from the last 30 days.

    Filters: naics (up to 10 codes), state, keyword (title only), posted_within_days (posted within the last 1 to
    30 days, default 30), limit (1 to 50, default 20). Rows carry title, agency (the employer), state, city,
    posted_at and url. If has_more is true, call again with cursor=next_cursor and the same filters. Example:
    keyword="electrician", state="TX".

    Print each result's url as a markdown link.

    Do not use for contracts (search_contracts) or grants (search_grants).
    """
    out = await api.search(
        "job", naics=naics, state=state, keyword=keyword, posted_within_days=posted_within_days, limit=limit,
        cursor=cursor, source=JOB_SOURCE,
    )
    return _parse(SearchResult, {**out, "note": LINK_RULE})


async def get_opportunity(id: OppId) -> OpportunityDetail:
    """Use this when the user wants the details of one search result, or who to contact about it.

    Pass the id from a search result. Returns the result's row plus its description (up to 2000 characters) and
    published points of contact (name and title only). Example: id="117402307943537453".

    Print url, and sam_url for federal notices, as markdown links.

    Do not use for searching (search_contracts, search_grants, search_jobs) or for contact details beyond the
    published names and titles.
    """
    data = await api.get_one(id)
    # ai and teaming stay null until their models are typed: no untyped upstream text passes through.
    return _parse(OpportunityDetail, {
        **data, "contacts": data.get("contacts") or [], "ai": None, "teaming": None, "note": LINK_RULE,
    })


# Registration order is the tools/list order.
TOOLS = (
    (search_contracts, "Search government contracts"),
    (search_grants, "Search grants"),
    (search_jobs, "Search jobs"),
    (get_opportunity, "Get opportunity details"),
)
