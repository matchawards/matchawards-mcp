"""Output models of the tools. Each tool's outputSchema is generated from its return model, and the SDK returns
the model as structuredContent (with the same JSON as text content, for hosts that read only that).

Fields the API adds later are dropped (pydantic's default extra="ignore") until a model declares them, so the
published schema only changes on purpose.
"""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr

Text = str | None
# AI text from the API, cut to 1000 characters (the API cuts it too; a longer value is shortened, not rejected).
AiText = Annotated[
    Annotated[str, Field(max_length=1000)] | None, BeforeValidator(lambda v: v[:1000] if isinstance(v, str) else v)
]
Agency = Annotated[str | None, BeforeValidator(lambda v: tidy_agency(v) if isinstance(v, str) else v)]

# Kept upper-case when an all-caps agency name is title-cased (tidy_agency).
ACRONYMS = {
    "US", "USA", "VA", "NASA", "GSA", "DLA", "DHS", "DOD", "DOE", "DOJ", "DOL", "DOT", "HHS", "HUD", "USDA", "FAA",
    "FEMA", "NIH", "CDC", "EPA", "IRS", "ICE", "CBP", "TSA", "NOAA", "NIST", "USACE", "AMC", "ACC", "NAVSEA",
    "NAVFAC", "NAVAIR", "NAVSUP", "DISA", "DARPA", "DHA", "USPS", "SBA", "OPM", "SSA", "FBI", "DEA", "ATF",
}
SMALL_WORDS = {"of", "the", "and", "for", "on", "in", "to", "a", "an", "at", "by"}


def _title(text: str) -> str:
    """An all-caps name in title case: acronyms, codes with digits and (PARENTHESISED) tokens kept."""
    out = []
    for i, w in enumerate(text.split()):
        core = w.strip("(),")
        if core in ACRONYMS or any(c.isdigit() for c in w) or (w.startswith("(") and w.endswith(")")):
            out.append(w)
        elif i and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w.lower().capitalize())
    return " ".join(out)


def tidy_agency(value: str | None) -> str | None:
    """An agency name as people read it: spaces collapsed, 'Of'/'The' mid-name lower-cased, an all-caps name
    title-cased ('FEDERAL ACQUISITION SERVICE' -> 'Federal Acquisition Service'), and a SAM.gov agency path
    ('DEPT OF DEFENSE.DEPT OF THE ARMY.AMC...') cut to its first two distinct levels, 'Dept of Defense · Dept of the
    Army'. Short all-caps acronyms ('NASA') and mixed-case names otherwise stay as they are."""
    if not value:
        return value
    text = " ".join(value.split())
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        if "." in text and "U.S." not in text:  # a SAM path: its top two levels, without repeats
            levels = []
            for part in (p.strip() for p in text.split(".")):
                if part and part not in levels:
                    levels.append(part)
            return " · ".join(_title(p) for p in levels[:2])
        return text if " " not in text and len(text) <= 6 else _title(text)
    return " ".join(w.lower() if i and w in ("Of", "The", "And", "For") else w for i, w in enumerate(text.split()))


class Naics(BaseModel):
    code: Text = None
    title: Text = None


class Contact(BaseModel):
    name: Text = None
    title: Text = None


class OpportunityRow(BaseModel):
    """One search result. Jobs fill title, agency (the employer), state, city, posted_at and url only."""

    id: str = Field(strict=True, description="Pass to get_opportunity for the description and contacts.")
    type: Text = Field(None, description="federal, state, grant or job.")
    title: Text = None
    agency: Agency = None
    agency_id: Text = Field(None, description="The agency's MatchAwards id.")
    office: Text = None
    naics: Naics | None = None
    matched_naics: list[str] | None = Field(
        None, description="Only when you filtered by naics: the requested codes this record carries. It matches if any "
        "of its NAICS codes does; naics shows its primary code, which may differ.")
    set_aside: Text = None
    state: Text = None
    city: Text = None
    posted_at: Text = Field(None, description="Date, YYYY-MM-DD.")
    response_deadline: Text = Field(None, description="Date, YYYY-MM-DD; null if none.")
    no_fixed_deadline: bool | None = Field(None, description="true: the deadline is a placeholder date.")
    url: Text = Field(None, description="The posting on matchawards.com. Show it as a markdown link.")
    sam_url: Text = Field(None, description="The SAM.gov notice (federal only). Show it as a markdown link.")
    solicitation_number: Text = None


class SearchResult(BaseModel):
    as_of: Text = Field(None, description="When the API generated the results (ISO time).")
    results: list[OpportunityRow]
    has_more: bool = Field(description="true: call again with cursor=next_cursor and the same filters.")
    next_cursor: Text = None
    note: Text = None
    warning: Text = Field(None, description="Set when paging stopped early; the results so far are still valid.")


class ShownResults(BaseModel):
    as_of: Text = Field(None, description="When the API generated the first row shown (ISO time).")
    results: list[OpportunityRow]
    missing: list[str] = Field(default_factory=list, description="Ids not shown because they were not found or "
                               "are not a contract or grant (a job). Retrying will not add them.")
    failed: list[str] = Field(default_factory=list, description="Ids not fetched because MatchAwards was busy or "
                              "failed. Call show_results again with just these ids to add them.")
    note: Text = None
    warning: Text = Field(None, description="Set when some ids could not be fetched (see failed).")
    _partial: bool = PrivateAttr(False)


class AiSummary(BaseModel):
    nft_title: AiText = None
    nft_insight: AiText = None
    nft_advantage: AiText = None


class AiNotes(BaseModel):
    """AI-generated notes MatchAwards wrote when it imported the notice. Every field is AI-generated text."""

    fit: AiText = Field(None, description="Who this opportunity suits.")
    reasoning: AiText = None
    subcontracting: AiText = Field(None, description="Angles for subcontractors.")
    large_prime: AiText = Field(None, description="Angles for large prime contractors.")
    summary: AiSummary | None = None


AI_FIELD = Field(None, description="AI-generated notes on this opportunity, or null. Label them as AI-generated "
                 "when you show or quote them.")


def _cut_words(value, cap: int = 2000):
    """Text longer than `cap` cut at the last word boundary before it, with an ellipsis."""
    if not isinstance(value, str) or len(value) <= cap:
        return value
    head = value[:cap - 1]
    return (head.rsplit(None, 1)[0] if " " in head else head) + "…"


class OpportunityDetail(OpportunityRow):
    as_of: Text = None
    description: Annotated[Text, BeforeValidator(_cut_words)] = Field(None, description="Up to 2000 characters.")
    contacts: list[Contact] = Field(default_factory=list, description="Published points of contact; [] if none.")
    ai: AiNotes | None = AI_FIELD
    note: Text = None


class NaicsCode(BaseModel):
    code: str = Field(strict=True, description="Six-digit NAICS code; pass it as naics to the search tools.")
    title: Text = None
    opportunities_url: Text = Field(None, description="The code's page on matchawards.com, or null. Show it as a "
                                    "markdown link.")


class NaicsLookup(BaseModel):
    as_of: Text = None
    results: list[NaicsCode]
    note: Text = None


class Awardee(BaseModel):
    name: Text = None
    uei: Text = Field(None, description="Pass to get_company for the firm's award history.")
    duns: Text = None


class Period(BaseModel):
    start: Text = None
    end: Text = None


class AwardRow(BaseModel):
    """One award. Federal contract awards fill solicitation_number and contract_number; grant awards fill
    cfda_number and period, and their title is the recipient's name."""

    id: Text = Field(None, strict=True, description="Award id; null when the award has no page on matchawards.com.")
    type: Text = Field(None, description="contract_award or grant_award.")
    title: Text = None
    awardee: Awardee | None = None
    amount: float | None = Field(None, description="USD; null when the source value does not parse.")
    award_date: Text = Field(None, description="Date, YYYY-MM-DD.")
    period: Period | None = Field(None, description="Grant awards only: start and end dates.")
    agency: Agency = None
    agency_id: Text = None
    naics: Naics | None = None
    matched_naics: list[str] | None = Field(
        None, description="Only when you filtered by naics: the requested codes this record carries. It matches if any "
        "of its NAICS codes does; naics shows its primary code, which may differ.")
    set_aside: Text = None
    state: Text = None
    city: Text = None
    solicitation_number: Text = None
    contract_number: Text = None
    cfda_number: Text = None
    url: Text = Field(None, description="The award on matchawards.com. Show it as a markdown link.")
    source_url: Text = Field(None, description="The award notice at SAM.gov or USAspending. Show it as a markdown "
                             "link.")


class AwardSearchResult(SearchResult):
    results: list[AwardRow]


class Totals(BaseModel):
    count: int | None = None
    amount: float | None = Field(None, description="USD, summed over amounts that parse.")


class CompanyTotals(BaseModel):
    contract_awards: Totals | None = None
    grant_awards: Totals | None = None


class Location(BaseModel):
    city: Text = None
    state: Text = None
    country: Text = None


class NaicsCount(Naics):
    count: int | None = None


class AgencyCount(BaseModel):
    agency: Agency = None
    agency_id: Text = None
    count: int | None = None


class Company(BaseModel):
    as_of: Text = None
    uei: str = Field(strict=True)
    name: Text = None
    other_names: list[str] = Field(default_factory=list, description="Other spellings in award records.")
    duns: Text = None
    location: Location | None = None
    totals: CompanyTotals | None = None
    first_award_date: Text = None
    last_award_date: Text = None
    top_naics: list[NaicsCount] = Field(default_factory=list, description="From contract awards only.")
    top_agencies: list[AgencyCount] = Field(default_factory=list)
    recent_awards: list[AwardRow] = Field(default_factory=list, description="Up to 10, newest first.")
    coverage_note: Text = None
    note: Text = None


class OpportunitySummary(BaseModel):
    id: str = Field(strict=True)
    type: Text = None
    title: Text = None
    agency: Agency = None
    naics: Naics | None = None
    set_aside: Text = None
    state: Text = None
    response_deadline: Text = Field(None, description="Date, YYYY-MM-DD; null if none.")
    days_left: int | None = Field(None, description="Days from today (US Eastern) to response_deadline; negative "
                                  "once it has passed; null without a deadline or with a placeholder date.")
    open: bool = Field(description="true: the deadline is today or later, US Eastern (no deadline: false).")
    url: Text = Field(None, description="The posting on matchawards.com. Show it as a markdown link.")
    sam_url: Text = Field(None, description="The SAM.gov notice (federal only). Show it as a markdown link.")


class Winner(BaseModel):
    name: Text = None
    uei: Text = Field(None, description="Pass to get_company for the firm's award history.")
    awards: int = Field(description="Similar awards won, among those read.")
    total_amount: float | None = Field(None, description="USD, summed over amounts that parse; null if none do.")
    last_award_date: Text = None


class Sample(BaseModel):
    """What the award figures are based on: the award records read, the dates they cover, and whether every matching
    record was read (complete)."""

    awards_read: int
    oldest_award_date: Text = None
    newest_award_date: Text = None
    complete: bool = Field(description="true: every matching award was read; false: older awards were not read.")


class ValueRange(BaseModel):
    p25: float
    median: float
    p75: float


class Assessment(BaseModel):
    as_of: Text = None
    opportunity: OpportunitySummary
    naics_match: bool | None = Field(None, description="Whether the opportunity's NAICS is one of my_naics; null "
                                     "when my_naics was not given.")
    set_aside_match: bool | None = Field(None, description="Whether the opportunity is set aside for my_set_aside; "
                                         "null when it was not given. An opportunity without a set-aside is open "
                                         "to all firms.")
    ai: AiNotes | None = AI_FIELD
    similar_awards: list[AwardRow] = Field(description="The 5 newest similar awards.")
    typical_winners: list[Winner] = Field(description="Up to 5 firms that won similar work, most awards first, "
                                          "then largest total.")
    award_value_range: ValueRange | None = Field(None, description="Quartiles of the similar awards' amounts (USD); "
                                                 "null with fewer than 3 amounts.")
    sample: Sample
    basis: Text = Field(None, description="'same NAICS and agency', 'same NAICS, all agencies', or null (no NAICS).")
    coverage_note: Text = None
    note: Text = None
    warning: Text = Field(None, description="Set when the award lookup was cut short; the figures cover what was "
                          "read.")
    _partial: bool = PrivateAttr(False)


class Partner(BaseModel):
    name: Text = None
    uei: Text = Field(None, description="Pass to get_company for the firm's award history.")
    awards_count: int
    total_amount: float | None = Field(None, description="USD, summed over amounts that parse; null if none do.")
    last_award_date: Text = None
    agencies: list[str] = Field(default_factory=list, description="Up to 3 agencies it won them from.")
    why: str = Field(description="Why it is listed, from the award records.")


class TeamingPartners(BaseModel):
    as_of: Text = None
    opportunity: OpportunitySummary
    role: str
    partners: list[Partner] = Field(description="Up to 10 firms that won similar work.")
    sample: Sample
    ai_subcontracting: AiText = Field(None, description="AI-generated notes on subcontracting angles for this "
                                      "opportunity, or null. Label them as AI-generated when you show them.")
    coverage_note: Text = None
    note: Text = None
    warning: Text = Field(None, description="Set when the award lookup was cut short; the list covers what was "
                          "read.")
    _partial: bool = PrivateAttr(False)
