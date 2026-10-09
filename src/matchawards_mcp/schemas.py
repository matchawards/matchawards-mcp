"""Output models of the tools. Each tool's outputSchema is generated from its return model, and the SDK returns
the model as structuredContent (with the same JSON as text content, for hosts that read only that).

Fields the API adds later are dropped (pydantic's default extra="ignore") until a model declares them, so the
published schema only changes on purpose.
"""

from typing import Any

from pydantic import BaseModel, Field

Text = str | None


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
