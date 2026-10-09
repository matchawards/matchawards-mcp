"""The tools. Each docstring is the description the model reads, frozen for directory listings: keep them tight.
Paragraphs are separated by a blank line; server.py joins the lines inside a paragraph, so no line break falls
mid-sentence in the published description.

server.py registers TOOLS with their titles, annotations and securitySchemes, and counts every call.
"""

import asyncio
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

from . import api
from .schemas import (
    Assessment, AwardRow, AwardSearchResult, Company, NaicsLookup, OpportunityDetail, OpportunityRow,
    OpportunitySummary, SearchResult, TeamingPartners,
)

LINK_RULE = (
    "Show each item's url to the user verbatim as a markdown link, [title](url); "
    "it opens the full posting on matchawards.com. Show sam_url the same way when it is set."
)
GRANT_NOTE = f"{LINK_RULE} Open status may lag for grants that closed recently: check response_deadline."
AWARD_LINK_RULE = (
    "Show each award's url (its page on matchawards.com) and source_url (the SAM.gov or USAspending record) to the "
    "user verbatim as markdown links when they are set. Head a grant award with its awardee name and CFDA number, "
    "not its title."
)
AWARD_COVERAGE = "Award records may be incomplete."
AI_NOTE = "The ai fields are AI-generated: label them as AI-generated whenever you show or quote them."

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
AgencyName = Annotated[
    str | None,
    Field(min_length=2, max_length=100, description="Agency name, e.g. 'Department of Veterans Affairs'; matched to "
          "the closest MatchAwards agency, which the result note names."),
]
Uei = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{12}$", description="12-character UEI, e.g. 'C3KXNLTAAY98'.")]


def _note(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


async def _agency(name: str | None) -> tuple[str | None, str | None]:
    """The agency= value for an agency name (None: no filter) and the note that says what was used."""
    if not name:
        return None, None
    ids, note = await api.resolve_agency(name)
    return ",".join(ids[:api.AGENCY_SLUGS_PER_QUERY]) or None, note


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
    agency: AgencyName = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> SearchResult:
    """Use this when the user wants US federal or state government contract opportunities to bid on: bids, RFPs,
    solicitations, set-asides.

    Filters: naics (up to 10 codes), state, keyword (title only), set_aside (federal only), type (federal or
    state), posted_within_days (1 to 180), open (default true), agency (a name; the note says which agency it
    matched), limit (1 to 50, default 20). If has_more is true, call again with cursor=next_cursor and the same
    filters. Example: naics="238160", state="VA", keyword="roof".

    Print each result's url, and sam_url for federal notices, as markdown links.

    Do not use for grants (search_grants), jobs (search_jobs) or one result's details (get_opportunity).
    """
    agency_id, agency_note = await _agency(agency)
    out = await api.search(
        type or "contract", naics=naics, state=state, keyword=keyword, set_aside=set_aside,
        posted_within_days=posted_within_days, open=open, agency=agency_id, limit=limit, cursor=cursor,
    )
    return _parse(SearchResult, {**out, "note": _note(agency_note, LINK_RULE)})


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


def _ai(data: dict) -> dict | None:
    """The detail's `ai` object, or None when it is absent, null or not an object."""
    return data.get("ai") if isinstance(data.get("ai"), dict) else None


async def get_opportunity(id: OppId) -> OpportunityDetail:
    """Use this when the user wants the details of one search result, or who to contact about it.

    Pass the id from a search result. Returns the result's row plus its description (up to 2000 characters),
    published points of contact (name and title only) and, when MatchAwards has them, ai: AI-generated notes on
    fit, subcontracting and large-prime angles and a summary. AI text is AI-generated: label it as such whenever
    you quote it. Example: id="117402307943537453".

    Print url, and sam_url for federal notices, as markdown links.

    Do not use for searching (search_contracts, search_grants, search_jobs) or for contact details beyond the
    published names and titles.
    """
    data = await api.get_one(id)
    ai = _ai(data)
    return _parse(OpportunityDetail, {
        **data, "contacts": data.get("contacts") or [], "ai": ai, "note": _note(LINK_RULE, ai and AI_NOTE),
    })


async def lookup_naics(
    query: Annotated[str, Field(min_length=2, max_length=100, description="Words, e.g. 'cybersecurity', or the "
                                "first 2 to 6 digits of a code, e.g. '5415'.")],
    limit: Annotated[int, Field(ge=1, le=25, description="Number of codes to return, 1 to 25.")] = 10,
) -> NaicsLookup:
    """Use this when the user describes a line of work in words and a NAICS code is needed, or asks what a code
    or code prefix covers.

    query is words (2 to 100 characters; common terms such as cybersecurity or janitorial list their usual codes
    first) or 2 to 6 digits (every six-digit code starting with them). limit is 1 to 25, default 10. Pass the codes
    as naics to search_contracts, search_grants, search_jobs or search_awards.

    Print each opportunities_url that is set as a markdown link.

    Do not use for searching opportunities or awards themselves (the search tools take the codes).
    """
    query = " ".join(query.split())
    by = "code" if re.fullmatch(r"\d{2,6}", query) else "q"
    data = await api.get(api.NAICS_PATH, {by: query, "limit": limit})
    return _parse(NaicsLookup, {**data, "note": "Show each opportunities_url that is set as a markdown link."})


async def search_awards(
    type: Annotated[
        Literal["contract_award", "grant_award", "any"],
        Field(description="contract_award (federal contracts), grant_award, or any (both)."),
    ] = "any",
    naics: Naics = None,
    agency: AgencyName = None,
    state: Annotated[
        str | None, Field(pattern=r"^[A-Za-z]{2}$", description="Two-letter US state code of the place of performance.")
    ] = None,
    awardee: Annotated[
        str | None, Field(min_length=2, max_length=100, description="Part of the awardee's name, 2 to 100 characters.")
    ] = None,
    uei: Annotated[
        str | None, Field(pattern=r"^[A-Za-z0-9]{12}$", description="The awardee's 12-character UEI.")
    ] = None,
    keyword: Keyword = None,
    awarded_within_days: Annotated[
        int, Field(ge=1, le=1095, description="Only awards from the last N days, 1 to 1095 (default 365).")
    ] = 365,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> AwardSearchResult:
    """Use this when the user asks who won government contracts or grants: past awards by NAICS, agency, state,
    awardee name or UEI, with amounts and dates.

    Filters: type (contract_award, grant_award or any), naics (up to 10 codes), agency (a name; the note says which
    agency it matched), state, awardee (part of a name), uei, keyword (title only), awarded_within_days (1 to 1095,
    default 365), limit (1 to 50, default 20). If has_more is true, call again with cursor=next_cursor and the same
    filters. Example: naics="541512", agency="Department of Veterans Affairs".

    Award records may be incomplete, so say so. Head a grant award with its awardee name and CFDA number, not its
    title. Pass an awardee's uei to get_company for its full history.

    Print each award's url and source_url as markdown links.

    Do not use for open opportunities to bid on (search_contracts, search_grants) or for one firm's history when its
    UEI is known (get_company).
    """
    agency_id, agency_note = await _agency(agency)
    out = await api.search(
        type, path=api.AWARDS_PATH, model=AwardRow, naics=naics, agency=agency_id, state=state, awardee=awardee,
        uei=uei and uei.upper(), keyword=keyword, awarded_within_days=awarded_within_days, limit=limit, cursor=cursor,
    )
    return _parse(AwardSearchResult, {**out, "note": _note(AWARD_COVERAGE, agency_note, AWARD_LINK_RULE)})


async def get_company(uei: Uei) -> Company:
    """Use this when the user wants one firm's government award history and its UEI is known, e.g. from a
    search_awards row.

    Returns the firm's name and other spellings, location (city, state, country), totals for contract and grant
    awards, first and last award dates, top NAICS codes and agencies, and its 10 most recent awards. No contact
    details. Award records may be incomplete, so say so. Example: uei="C3KXNLTAAY98".

    Print each recent award's url and source_url as markdown links.

    Do not use for finding firms by name or NAICS (search_awards) or for contact details (none are available).
    """
    data = await api.get(
        f"{api.COMPANIES_PATH}/{uei.upper()}",
        not_found="No award records for this UEI on MatchAwards. Check the UEI, or find firms with search_awards.",
    )
    result = data.get("result")
    if not isinstance(result, dict):
        raise api.fail("bad_response", "MatchAwards returned an unexpected API response (no result object).")
    return _parse(Company, {**result, "as_of": data.get("as_of"), "note": _note(AWARD_COVERAGE, AWARD_LINK_RULE)})


# --- Composite tools: one opportunity plus the award records around it ------------------------------------

# Hard caps per call: 1 detail request + COMPOSITE_QUERIES award queries of COMPOSITE_PAGES requests each (1 + 6),
# all within COMPOSITE_DEADLINE_S. Cut short, a tool answers with what it read, a warning and ledger outcome partial.
COMPOSITE_DEADLINE_S = 6.0
COMPOSITE_QUERIES = 2
COMPOSITE_PAGES = 3
COMPOSITE_ROWS = 150  # per award query: 3 pages of 50
AWARDS_WINDOW_DAYS = 1095  # 3 years
try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # ponytail: no tz database (Windows without tzdata): EST all year, a day off at most
    EASTERN = timezone(timedelta(hours=-5))
COMPOSITE_COVERAGE = ("Based on award records in MatchAwards, which may be incomplete. These are firms that won "
                      "similar work, not a statement about who holds this contract now.")


def _today() -> date:
    return datetime.now(EASTERN).date()


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


async def _opportunity(id: str, deadline: float) -> tuple[dict, OpportunityRow]:
    """The detail (raw, for agency_id and ai) and its row; contracts and grants only."""
    try:
        data = await asyncio.wait_for(api.get_one(id), deadline - asyncio.get_running_loop().time())
    except asyncio.TimeoutError:
        raise api.fail("upstream_timeout", f"MatchAwards did not return the opportunity within "
                       f"{COMPOSITE_DEADLINE_S:g} seconds. Retry in a minute.") from None
    opp = _parse(OpportunityRow, data)
    if opp.type == "job":
        raise api.fail("invalid_input", "This id is a job posting; this tool covers contracts and grants.")
    return data, opp


def _summary(opp: OpportunityRow) -> OpportunitySummary:
    due = _date(opp.response_deadline)
    days = (due - _today()).days if due else None
    return OpportunitySummary(
        **opp.model_dump(include={"id", "type", "title", "naics", "set_aside", "state", "response_deadline", "url",
                                  "sam_url"}),
        agency=opp.agency, open=days is not None and days >= 0, days_left=None if opp.no_fixed_deadline else days,
    )


async def _awards(deadline: float, type_: str, **filters) -> tuple[list[AwardRow], str | None, bool]:
    """One capped award query: (rows, error or None, whether the request budget ran out with more to read). A
    failed or timed-out query is not fatal: the tool answers with the other figures."""
    try:
        out = await api.search(type_, path=api.AWARDS_PATH, model=AwardRow, limit=COMPOSITE_ROWS,
                               pages=COMPOSITE_PAGES, deadline=deadline, awarded_within_days=AWARDS_WINDOW_DAYS,
                               **filters)
    except ToolError as e:
        return [], str(e), False
    rows = out["results"]
    error = out.get("warning") if out.get("stopped") else None
    return rows, error, bool(out["has_more"] and len(rows) < COMPOSITE_ROWS and not error)


def _name_key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def _firms(rows: list[AwardRow]) -> list[dict]:
    """Awardees of `rows` (newest first), one per UEI, or per normalised name for rows without one."""
    firms: dict[str, dict] = {}
    for r in rows:
        name, uei = (r.awardee.name, r.awardee.uei) if r.awardee else (None, None)
        if not (name or uei):
            continue
        f = firms.setdefault(uei.upper() if uei else f"name:{_name_key(name)}",
                             {"name": name, "uei": uei, "rows": []})
        f["rows"].append(r)
    for f in firms.values():
        amounts = [r.amount for r in f["rows"] if r.amount is not None]
        dates = [r.award_date for r in f["rows"] if _date(r.award_date)]
        f.update(count=len(f["rows"]), total=round(sum(amounts), 2) if amounts else None, last=max(dates, default=None),
                 first=min(dates, default=None), typical=statistics.median(amounts) if amounts else None,
                 agencies=[a for a, _ in Counter(r.agency for r in f["rows"] if r.agency).most_common(3)])
    return list(firms.values())


def _value_range(rows: list[AwardRow]) -> dict | None:
    amounts = [r.amount for r in rows if r.amount is not None]
    if len(amounts) < 3:
        return None
    p25, median, p75 = (round(q, 2) for q in statistics.quantiles(amounts, n=4, method="inclusive"))
    return {"p25": p25, "median": median, "p75": p75}


def _cut_note(rows: list, naics: str) -> str:
    if not rows:
        return ("No matching awards in the newest records read; more may exist further back. search_awards with "
                f"naics={naics} can keep looking.")
    return "The award lookup reached its request limit; the figures cover the newest matching awards only."


async def assess_opportunity(
    id: OppId,
    my_naics: Naics = None,
    my_set_aside: Annotated[str | None, Field(max_length=40, description="The user's set-aside code, e.g. 'SBA' or "
                                              "'SDVOSBC', to compare with the opportunity's.")] = None,
) -> Assessment:
    """Use this when the user asks whether to bid on one opportunity, or wants a qualification brief on it: days
    left, set-aside, AI fit notes, similar past awards, the firms that typically win such work and the usual award
    value.

    Pass the id from a search result, and optionally my_naics (up to 10 codes) and my_set_aside to check the fit.
    Similar awards are the same NAICS at the same agency over the last 3 years, or at all agencies when that agency
    has none (basis says which). Award records may be incomplete, so say so; ai text is AI-generated, so label it.
    Never call a firm the incumbent: these are firms that won similar work. Example: id="117412688283576625".

    Print url, sam_url and each award's url and source_url as markdown links.

    Do not use for searching (search_contracts, search_grants), for finding partners (find_teaming_partners) or
    for job postings.
    """
    deadline = asyncio.get_running_loop().time() + COMPOSITE_DEADLINE_S
    data, opp = await _opportunity(id, deadline)
    code = opp.naics.code if opp.naics else None
    agency_id = data.get("agency_id") if isinstance(data.get("agency_id"), str) else None
    type_ = "grant_award" if opp.type == "grant" else "contract_award"
    rows, errors, cut, basis, notes = [], [], False, None, []
    if not code:
        notes.append("This opportunity has no NAICS code, so no similar awards were looked up.")
    else:
        if agency_id:
            rows, error, cut = await _awards(deadline, type_, naics=code, agency=agency_id)
            errors.append(error)
            basis = "same NAICS and agency"
        if not rows:
            if agency_id and not errors[-1]:
                notes.append(f"No NAICS {code} awards from this agency were found, so similar awards cover all "
                             "agencies.")
            rows, error, cut = await _awards(deadline, type_, naics=code)
            errors.append(error)
            basis = "same NAICS, all agencies"
    errors = [f"Award lookup cut short: {e}" for e in errors if e]
    winners = sorted(_firms(rows), key=lambda f: (f["count"], f["total"] or 0), reverse=True)[:5]
    mine = {c.strip() for c in my_naics.split(",")} if my_naics else None
    ai = _ai(data)
    out = _parse(Assessment, {
        "as_of": data.get("as_of"),
        "opportunity": _summary(opp).model_dump(),
        "naics_match": None if mine is None else code in mine,
        "set_aside_match": None if not my_set_aside else (opp.set_aside or "").casefold() == my_set_aside.casefold(),
        "ai": ai,
        "similar_awards": [r.model_dump() for r in sorted(rows, key=lambda r: r.award_date or "", reverse=True)[:5]],
        "typical_winners": [{"name": f["name"], "uei": f["uei"], "awards": f["count"], "total_amount": f["total"],
                             "last_award_date": f["last"]} for f in winners],
        "award_value_range": _value_range(rows),
        "awards_read": len(rows),
        "basis": basis,
        "coverage_note": COMPOSITE_COVERAGE,
        "note": _note(*notes, ai and AI_NOTE, LINK_RULE, AWARD_LINK_RULE),
        "warning": _note(*errors, cut and _cut_note(rows, code)) or None,
    })
    out._partial = bool(errors or cut)
    return out


async def find_teaming_partners(
    id: OppId,
    role: Annotated[
        Literal["sub", "prime", "any"],
        Field(description="sub: the user looks for subcontractors (smaller typical awards first among equals); "
              "prime: for a prime to join (larger first); any (default)."),
    ] = "any",
) -> TeamingPartners:
    """Use this when the user wants possible teaming partners, subcontractors or primes for one opportunity: firms
    that won similar awards in the same state or at the same agency.

    Pass the id from a search result, and optionally role (sub, prime or any). Returns up to 10 firms from public
    award records over the last 3 years (same NAICS in the opportunity's state, then at its agency, else anywhere),
    ranked by similar awards won, then the most recent, then the total, each with a why line; get_company(uei)
    shows a firm's full history. No contact details. Award records may be incomplete, so say so; ai_subcontracting is
    AI-generated, so label it. Never call a firm the incumbent. Example: id="117412688283576625".

    Print the opportunity's url and sam_url as markdown links.

    Do not use for searching awards in general (search_awards), for one firm's history (get_company) or for job
    postings.
    """
    deadline = asyncio.get_running_loop().time() + COMPOSITE_DEADLINE_S
    data, opp = await _opportunity(id, deadline)
    code = opp.naics.code if opp.naics else None
    agency_id = data.get("agency_id") if isinstance(data.get("agency_id"), str) else None
    type_ = "grant_award" if opp.type == "grant" else "contract_award"
    agency = opp.agency or "this agency"
    queries = []  # (where the awards are, filters); at most COMPOSITE_QUERIES run
    if code and opp.state:
        queries.append((f"in {opp.state.upper()}", {"state": opp.state}))
    if code and agency_id:
        queries.append((f"at {agency}", {"agency": agency_id}))
    anywhere = (f"in NAICS {code}", {})
    if code and not queries:
        queries.append(anywhere)
    rows, where, errors, cut, notes, ran = {}, {}, [], False, [], 0
    while queries and ran < COMPOSITE_QUERIES:
        place, filters = queries.pop(0)
        ran += 1
        found, error, budget_cut = await _awards(deadline, type_, naics=code, **filters)
        errors += [f"Award lookup cut short: {error}"] if error else []
        cut = cut or budget_cut
        for r in found:
            if r.id not in rows:
                rows[r.id], where[r.id] = r, place
        if filters and not (queries or rows or errors) and ran < COMPOSITE_QUERIES:  # none nearby: NAICS anywhere
            queries.append(anywhere)
            notes.append(f"No similar awards in the opportunity's state or at its agency, so these firms won NAICS "
                         f"{code} awards anywhere.")
    firms = _firms(list(rows.values()))
    # Order: most awards, then the most recent, then the largest total; role reorders ties on the award count.
    firms.sort(key=lambda f: f["total"] or 0, reverse=True)
    firms.sort(key=lambda f: f["last"] or "", reverse=True)
    if role == "sub":
        firms.sort(key=lambda f: (f["typical"] is None, f["typical"] or 0))
    elif role == "prime":
        firms.sort(key=lambda f: (f["typical"] is not None, f["typical"] or 0), reverse=True)
    firms.sort(key=lambda f: f["count"], reverse=True)
    partners = []
    for f in firms[:10]:
        counts = Counter(where[r.id] for r in f["rows"])
        text = " and ".join(f"{n} similar award{'s' if n != 1 else ''} {place}" if i == 0 else f"{n} {place}"
                            for i, (place, n) in enumerate(counts.items()))
        since = f" since {f['first'][:4]}" if f["first"] else ""
        partners.append({"name": f["name"], "uei": f["uei"], "awards_count": f["count"], "total_amount": f["total"],
                         "last_award_date": f["last"], "agencies": f["agencies"], "why": f"Won {text}{since}."})
    ai_sub = (_ai(data) or {}).get("subcontracting")
    if not code:
        notes.append("This opportunity has no NAICS code, so no partners were looked up.")
    if role != "any":
        notes.append(f"role={role}: among firms with the same number of awards, those with "
                     f"{'smaller' if role == 'sub' else 'larger'} typical awards come first.")
    out = _parse(TeamingPartners, {
        "as_of": data.get("as_of"),
        "opportunity": _summary(opp).model_dump(),
        "role": role,
        "partners": partners,
        "ai_subcontracting": ai_sub if isinstance(ai_sub, str) else None,
        "coverage_note": COMPOSITE_COVERAGE,
        "note": _note(*notes, "get_company(uei) shows a firm's full award history.",
                      isinstance(ai_sub, str) and "ai_subcontracting is AI-generated: label it as such whenever you "
                      "show or quote it.", LINK_RULE),
        "warning": _note(*errors, cut and _cut_note(partners, code)) or None,
    })
    out._partial = bool(errors or cut)
    return out


# Registration order is the tools/list order.
TOOLS = (
    (search_contracts, "Search government contracts"),
    (search_grants, "Search grants"),
    (search_jobs, "Search jobs"),
    (get_opportunity, "Get opportunity details"),
    (lookup_naics, "Look up NAICS codes"),
    (search_awards, "Search past awards"),
    (get_company, "Get company award history"),
    (assess_opportunity, "Assess an opportunity"),
    (find_teaming_partners, "Find teaming partners"),
)
