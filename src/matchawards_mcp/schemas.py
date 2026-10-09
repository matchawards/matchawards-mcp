"""Output models of the tools. Each tool's outputSchema is generated from its return model, and the SDK returns
the model as structuredContent (with the same JSON as text content, for hosts that read only that).

Fields the API adds later are dropped (pydantic's default extra="ignore") until a model declares them, so the
published schema only changes on purpose.
"""

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

Text = str | None

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
    agency: Text = None
    office: Text = None
    naics: Naics | None = None
    set_aside: Text = None
    state: Text = None
    city: Text = None
    posted_at: Text = Field(None, description="Date, YYYY-MM-DD.")
    response_deadline: Text = Field(None, description="Date, YYYY-MM-DD; null if none.")
    no_fixed_deadline: bool | None = Field(None, description="true: the deadline is a placeholder date.")
    url: Text = Field(None, description="The posting on matchawards.com. Show it as a markdown link.")
    sam_url: Text = Field(None, description="The SAM.gov notice (federal only). Show it as a markdown link.")
    solicitation_number: Text = None

    @field_validator("agency")
    @classmethod
    def _tidy_agency(cls, v: str | None) -> str | None:
        return tidy_agency(v)


class SearchResult(BaseModel):
    as_of: Text = Field(None, description="When the API generated the results (ISO time).")
    results: list[OpportunityRow]
    has_more: bool = Field(description="true: call again with cursor=next_cursor and the same filters.")
    next_cursor: Text = None
    note: Text = None
    warning: Text = Field(None, description="Set when paging stopped early; the results so far are still valid.")


class OpportunityDetail(OpportunityRow):
    as_of: Text = None
    description: Text = Field(None, description="Up to 2000 characters.")
    contacts: list[Contact] = Field(default_factory=list, description="Published points of contact; [] if none.")
    ai: dict[str, Any] | None = Field(None, description="AI-generated notes on this opportunity, or null. "
                                      "Label them as AI-generated when you show them.")
    note: Text = None
