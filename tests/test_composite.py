"""assess_opportunity and find_teaming_partners against a mocked API: request caps, the deadline, fallbacks,
ranking, the figures and the wording rules. No network."""

import asyncio
import json
from datetime import date

import httpx
import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from matchawards_mcp import server, tools
from matchawards_mcp.schemas import AwardRow

from test_data_tools import AWARDS, award, awards_page, params
from test_server import PATH, api, anyio_backend, ok  # noqa: F401  (fixtures)

AI = {"fit": "Suits machine shops.", "reasoning": "Repair work.", "subcontracting": "Plating and testing subs.",
      "large_prime": None, "summary": {"nft_title": "Heat exchangers", "nft_insight": None, "nft_advantage": None},
      "generated": True}
OPP = {
    "id": "117412688283576625", "type": "federal", "title": "Remanufacture of B-1B Heat Exchangers",
    "agency": "United States Department of the Air Force", "agency_id": "dept_of_the_air_force",
    "naics": {"code": "336413", "title": "Other Aircraft Parts"}, "set_aside": "SBA", "state": "OK",
    "response_deadline": "2026-10-26", "url": "https://matchawards.com/af/posts/1", "sam_url": "https://sam.gov/opp/1",
    "description": "Remanufacture.", "contacts": [{"name": "Jo Smith", "title": "CO", "email": "jo@example.com"}],
}


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(tools, "_today", lambda: date(2026, 10, 10))


def detail(**kw):
    return {"as_of": "2026-10-10T12:00:00Z", "result": {**OPP, **kw}}


def win(i, name, uei, amount, day, **kw):
    """An award row won by `name`."""
    return award(i, awardee={"name": name, "uei": uei, "duns": None}, amount=amount, award_date=day, **kw)


def router(detail_body, by_query):
    """Detail at PATH/<id>; awards by the query's agency/state: by_query[(agency, state)] is a list of pages (the
    last repeats)."""
    def reply(req):
        if req.url.path.startswith(PATH + "/"):
            return httpx.Response(200, json=detail_body)
        key = (req.url.params.get("agency"), req.url.params.get("state"))
        pages = by_query[key]
        return httpx.Response(200, json=pages.pop(0) if len(pages) > 1 else pages[0])
    return reply


def results(rows, has_more=False, cursor=None):
    return {"as_of": "x", "results": rows, "has_more": has_more, "next_cursor": cursor}


async def call(name, args):
    """Through the MCP layer: structuredContent checked against the outputSchema, and the wording rules."""
    schema = {t.name: t.output_schema for t in await server.mcp.list_tools()}[name]
    out = await server.mcp.call_tool(name, args)
    assert not out.is_error, out.content
    jsonschema.validate(out.structured_content, schema)
    assert json.loads(out.content[0].text) == out.structured_content
    for text in (json.dumps(out.structured_content), out.content[0].text):
        assert "incumbent" not in text.lower()
        assert "email" not in text and "phone" not in text and "@example.com" not in text
        assert "ma_profile_url" not in text
    return out.structured_content


ROWS = [
    win(1, "ALPHA LLC", "AAAAAAAAAAA1", 100.0, "2026-09-01"),
    win(2, "BETA INC", "BBBBBBBBBBB2", 900.0, "2026-08-01"),
    win(3, "ALPHA LLC", "AAAAAAAAAAA1", 300.0, "2026-07-01"),
    win(4, "GAMMA CO", None, None, "2026-10-05"),
    win(5, "Gamma, Co.", None, 50.0, "2025-01-01"),  # same firm by normalised name (no UEI)
    win(6, "BETA INC", "bbbbbbbbbbb2", 100.0, "2024-06-01"),  # UEI matched case-insensitively
    win(7, "DELTA", "DDDDDDDDDDD4", 2000.0, None),
]


# --- assess_opportunity -------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_assess_reads_the_detail_then_same_naics_and_agency_awards(api):
    api.replies = [router(detail(ai=AI), {("dept_of_the_air_force", None): [results(ROWS)]})]
    out = await call("assess_opportunity", {"id": OPP["id"], "my_naics": "541512, 336413", "my_set_aside": "sba"})
    assert [r.url.path for r in api.requests] == [f"{PATH}/{OPP['id']}", AWARDS]
    assert params(api, AWARDS) == [{"type": "contract_award", "naics": "336413", "agency": "dept_of_the_air_force",
                                    "awarded_within_days": "1095", "limit": "50"}]
    assert out["opportunity"] == {
        "id": OPP["id"], "type": "federal", "title": OPP["title"], "agency": OPP["agency"], "naics": OPP["naics"],
        "set_aside": "SBA", "state": "OK", "response_deadline": "2026-10-26", "days_left": 16, "open": True,
        "url": OPP["url"], "sam_url": OPP["sam_url"]}
    assert (out["naics_match"], out["set_aside_match"]) == (True, True)
    assert out["ai"]["fit"] == "Suits machine shops." and "generated" not in out["ai"]
    assert out["basis"] == "same NAICS and agency"
    assert out["sample"] == {"awards_read": 7, "oldest_award_date": "2024-06-01", "newest_award_date": "2026-10-05",
                             "complete": True}
    assert out["note"].startswith("Based on all 7 award records found (2024-06-01 to 2026-10-05). ")
    assert tools.NO_UEI_NOTE in out["note"]  # GAMMA CO has no UEI
    assert [r["id"] for r in out["similar_awards"]] == ["4", "1", "2", "3", "5"]  # by award_date, newest first
    assert [(w["name"], w["awards"], w["total_amount"], w["last_award_date"]) for w in out["typical_winners"]] == [
        ("BETA INC", 2, 1000.0, "2026-08-01"),  # 2 awards each: the larger total first
        ("ALPHA LLC", 2, 400.0, "2026-09-01"),
        ("GAMMA CO", 2, 50.0, "2026-10-05"),
        ("DELTA", 1, 2000.0, None),
    ]
    assert out["award_value_range"] == {"p25": 100.0, "median": 200.0, "p75": 750.0}
    assert tools.AI_NOTE in out["note"] and "markdown links" in out["note"] and out["warning"] is None
    assert out["coverage_note"] == tools.COMPOSITE_COVERAGE


@pytest.mark.anyio
async def test_assess_falls_back_to_all_agencies_and_says_so(api):
    api.replies = [router(detail(), {("dept_of_the_air_force", None): [results([])],
                                     (None, None): [results(ROWS[:2])]})]
    out = await call("assess_opportunity", {"id": OPP["id"]})
    assert [p.get("agency") for p in params(api, AWARDS)] == ["dept_of_the_air_force", None]
    assert out["basis"] == "same NAICS, all agencies" and out["sample"]["awards_read"] == 2
    assert tools.NO_UEI_NOTE not in out["note"]  # both firms have a UEI
    assert "No NAICS 336413 awards from this agency were found, so similar awards cover all agencies." in out["note"]
    assert (out["naics_match"], out["set_aside_match"], out["ai"], out["award_value_range"]) == (None, None, None, None)
    assert tools.AI_NOTE not in out["note"]


@pytest.mark.anyio
async def test_assess_without_agency_or_naics(api):
    api.replies = [router(detail(agency_id=None, type="grant"), {(None, None): [results(ROWS[:3])]})]
    out = await call("assess_opportunity", {"id": OPP["id"], "my_set_aside": "8A"})
    assert [p["type"] for p in params(api, AWARDS)] == ["grant_award"] and "agency" not in params(api, AWARDS)[0]
    assert (out["basis"], out["set_aside_match"]) == ("same NAICS, all agencies", False)
    api.requests.clear()
    api.replies = [router(detail(naics=None), {})]
    out = await call("assess_opportunity", {"id": OPP["id"], "my_naics": "336413"})
    assert params(api, AWARDS) == [] and out["basis"] is None and out["naics_match"] is False
    assert "no NAICS code, so no similar awards were looked up" in out["note"]


@pytest.mark.anyio
@pytest.mark.parametrize("deadline, days_left, is_open", [
    ("2026-10-10", 0, True), ("2026-10-09", -1, False), (None, None, False), ("TBD", None, False)])
async def test_days_left_is_counted_in_us_eastern_dates(api, deadline, days_left, is_open):
    api.replies = [router(detail(response_deadline=deadline, naics=None), {})]
    out = (await tools.assess_opportunity(OPP["id"])).opportunity
    assert (out.days_left, out.open) == (days_left, is_open)


@pytest.mark.anyio
async def test_a_job_id_is_invalid_input(api):
    api.replies = [router(detail(type="job"), {})]
    with pytest.raises(ToolError, match="job posting") as e:
        await tools.assess_opportunity("1")
    assert e.value.outcome == "invalid_input"


@pytest.mark.anyio
@pytest.mark.parametrize("tool", [tools.assess_opportunity, tools.find_teaming_partners])
async def test_sparse_awards_use_at_most_one_detail_and_six_award_requests(api, tool):
    empty_more = [results([], has_more=True, cursor="c")]
    api.replies = [router(detail(), {("dept_of_the_air_force", None): empty_more, (None, "OK"): empty_more,
                                     (None, None): empty_more})]
    out = await tool(OPP["id"])
    assert len(api.requests) == 7 and len(params(api, AWARDS)) == 6
    assert out._partial is True
    assert out.warning == ("No matching awards in the newest records read; more may exist further back. "
                           "search_awards with naics=336413 can keep looking.")


@pytest.mark.anyio
async def test_the_deadline_cuts_the_award_lookup_short_and_keeps_what_was_read(api, monkeypatch):
    monkeypatch.setattr(tools, "COMPOSITE_DEADLINE_S", 0.3)

    async def slow_awards(req):
        if req.url.path.startswith(PATH + "/"):
            return httpx.Response(200, json=detail())
        await asyncio.sleep(0.2)
        n = len(params(api, AWARDS))
        return httpx.Response(200, json=awards_page([n], has_more=True, cursor=f"c{n}"))

    api.replies = [slow_awards]
    out = await tools.assess_opportunity(OPP["id"])
    # Page 1 lands at 0.2 s and page 2 is cut at 0.3 s; with a row read, there is no fallback query.
    assert len(params(api, AWARDS)) == 2 and out.sample.awards_read == 1 and out._partial is True
    assert out.sample.complete is False and "older awards were not read" in out.note
    assert out.basis == "same NAICS and agency"
    assert out.warning.startswith("Award lookup cut short: Stopped after 1 rows: MatchAwards did not answer within "
                                  "this tool's time limit.")


@pytest.mark.anyio
async def test_a_detail_past_the_deadline_is_an_upstream_timeout(api, monkeypatch):
    monkeypatch.setattr(tools, "COMPOSITE_DEADLINE_S", 0.05)

    async def slow(req):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=detail())

    api.replies = [slow]
    with pytest.raises(ToolError, match="did not return the opportunity within 0.05 seconds") as e:
        await tools.find_teaming_partners(OPP["id"])
    assert e.value.outcome == "upstream_timeout"


@pytest.mark.anyio
async def test_a_failed_award_query_is_partial_not_fatal(api):
    def reply(req):
        if req.url.path.startswith(PATH + "/"):
            return httpx.Response(200, json=detail())
        return httpx.Response(503, json={"error": "timeout"}, headers={"Retry-After": "30"})

    api.replies = [reply]
    out = await tools.assess_opportunity(OPP["id"])
    assert out._partial is True and out.sample.awards_read == 0 and out.similar_awards == []
    assert out.sample.complete is False and "No similar award records were found" not in out.note
    assert out.warning.count("Award lookup cut short: MatchAwards took too long") == 2  # agency, then all agencies
    assert "No NAICS" not in out.note  # an error is not "none found"


@pytest.mark.parametrize("amounts, expected", [
    ([5.0, None], None),
    ([1.0, 2.0], None),
    ([1.0, 2.0, 3.0], {"p25": 1.5, "median": 2.0, "p75": 2.5}),
    ([4.0, 1.0, 3.0, 2.0], {"p25": 1.75, "median": 2.5, "p75": 3.25}),
    ([7.0, 7.0, 7.0, None], {"p25": 7.0, "median": 7.0, "p75": 7.0}),
])
def test_award_value_range(amounts, expected):
    rows = [AwardRow.model_validate(award(i, amount=a)) for i, a in enumerate(amounts)]
    assert tools._value_range(rows) == expected


# --- find_teaming_partners ----------------------------------------------------------------------------------

STATE_ROWS = [
    win(11, "ALPHA LLC", "AAAAAAAAAAA1", 100.0, "2026-09-01"),
    win(12, "ALPHA LLC", "AAAAAAAAAAA1", 200.0, "2025-03-01"),
    win(13, "OMEGA", "OOOOOOOOOOO9", 5000.0, "2026-01-01"),
    win(14, "Acme, LLC", None, 10.0, "2026-02-01"),
]
AGENCY_ROWS = [
    win(11, "ALPHA LLC", "AAAAAAAAAAA1", 100.0, "2026-09-01"),  # the same award again: counted once
    win(21, "ALPHA LLC", "AAAAAAAAAAA1", 400.0, "2024-05-01", agency="UNITED STATES DEPARTMENT OF THE AIR FORCE"),
    win(22, "ACME LLC", None, 20.0, "2026-06-01"),
    win(23, "ZETA", "ZZZZZZZZZZZ1", 30.0, "2026-06-01"),
]


@pytest.mark.anyio
async def test_partners_from_state_then_agency_deduped_ranked_with_why(api):
    api.replies = [router(detail(ai=AI), {(None, "OK"): [results(STATE_ROWS)],
                                          ("dept_of_the_air_force", None): [results(AGENCY_ROWS)]})]
    out = await call("find_teaming_partners", {"id": OPP["id"]})
    assert [(p.get("state"), p.get("agency"), p["naics"], p["awarded_within_days"]) for p in params(api, AWARDS)] == [
        ("OK", None, "336413", "1095"), (None, "dept_of_the_air_force", "336413", "1095")]
    got = [(p["name"], p["uei"], p["awards_count"], p["total_amount"], p["last_award_date"]) for p in out["partners"]]
    assert got == [
        ("ALPHA LLC", "AAAAAAAAAAA1", 3, 700.0, "2026-09-01"),
        ("Acme, LLC", None, 2, 30.0, "2026-06-01"),  # the same firm by normalised name across both queries
        ("ZETA", "ZZZZZZZZZZZ1", 1, 30.0, "2026-06-01"),  # 1 award each: the most recent first
        ("OMEGA", "OOOOOOOOOOO9", 1, 5000.0, "2026-01-01"),
    ]
    alpha, acme, zeta, _ = out["partners"]
    assert alpha["why"] == ("Won 2 similar awards in OK and 1 at United States Department of the Air Force "
                            "since May 1, 2024.")
    assert acme["why"] == ("Won 1 similar award in OK and 1 at United States Department of the Air Force since "
                           "Feb 1, 2026.")
    assert zeta["why"] == "Won 1 similar award at United States Department of the Air Force since Jun 1, 2026."
    # The sample: 7 distinct awards (11 was read twice), all of them; the why dates fall inside it.
    assert out["sample"] == {"awards_read": 7, "oldest_award_date": "2024-05-01", "newest_award_date": "2026-09-01",
                             "complete": True}
    assert "Based on all 7 award records found (2024-05-01 to 2026-09-01)." in out["note"]
    # Acme has no UEI: no get_company for it, said once; the others still get the hint.
    assert out["note"].count(tools.NO_UEI_NOTE) == 1 and "get_company(uei) shows" in out["note"]
    assert alpha["agencies"] == ["Dept of the Army", "United States Department of the Air Force"]
    assert out["ai_subcontracting"] == "Plating and testing subs."
    assert "ai_subcontracting is AI-generated" in out["note"]
    assert out["role"] == "any" and out["warning"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("role, order", [("sub", ["SMALL", "MID", "BIG", "NONE"]),
                                         ("prime", ["BIG", "MID", "SMALL", "NONE"])])
async def test_role_orders_ties_on_the_award_count_by_typical_award(api, role, order):
    rows = [win(1, "BIG", "B" * 12, 9000.0, "2026-01-01"), win(2, "SMALL", "S" * 12, 10.0, "2025-01-01"),
            win(3, "NONE", "N" * 12, None, "2026-09-01"), win(4, "MID", "M" * 12, 500.0, "2026-02-01"),
            win(5, "TOP", "T" * 12, 1.0, "2020-01-01"), win(6, "TOP", "T" * 12, 1.0, "2020-01-02")]
    api.replies = [router(detail(state=None, ai=None), {("dept_of_the_air_force", None): [results(rows)]})]
    out = await tools.find_teaming_partners(OPP["id"], role=role)
    assert [p.name for p in out.partners] == ["TOP", *order]  # more awards still come first
    assert f"role={role}: among firms with the same number of awards" in out.note
    assert out.ai_subcontracting is None and "ai_subcontracting" not in out.note
    assert len(params(api, AWARDS)) == 1  # no state: the agency query only


@pytest.mark.anyio
async def test_partners_are_capped_at_ten(api):
    rows = [win(i, f"FIRM {i}", f"{i:012d}", 1.0, "2026-01-01") for i in range(15)]
    api.replies = [router(detail(), {(None, "OK"): [results(rows)], ("dept_of_the_air_force", None): [results([])]})]
    out = await call("find_teaming_partners", {"id": OPP["id"], "role": "sub"})
    assert len(out["partners"]) == 10


@pytest.mark.anyio
async def test_partners_without_state_or_agency_use_naics_only_and_without_naics_none(api):
    api.replies = [router(detail(state=None, agency_id=None), {(None, None): [results(ROWS[:1])]})]
    out = await tools.find_teaming_partners(OPP["id"])
    assert len(params(api, AWARDS)) == 1
    assert out.partners[0].why == "Won 1 similar award in NAICS 336413 since Sep 1, 2026."
    api.requests.clear()
    api.replies = [router(detail(naics=None), {})]
    out = await tools.find_teaming_partners(OPP["id"])
    assert params(api, AWARDS) == [] and out.partners == [] and "no partners were looked up" in out.note


@pytest.mark.anyio
async def test_composite_descriptions_say_never_incumbent_only_as_a_rule():
    listed = {t.name: t for t in await server.mcp.list_tools()}
    for name in ("assess_opportunity", "find_teaming_partners"):
        text = listed[name].description
        assert text.lower().count("incumbent") == 1 and "Never call a firm the incumbent" in text, name
    for name, t in listed.items():
        if name not in ("assess_opportunity", "find_teaming_partners"):
            assert "incumbent" not in t.description.lower(), name


@pytest.mark.anyio
async def test_partners_with_none_nearby_fall_back_to_the_naics_anywhere_within_the_page_budget(api):
    api.replies = [router(detail(state=None), {("dept_of_the_air_force", None): [results([])],
                                               (None, None): [results(ROWS[:1])]})]
    out = await tools.find_teaming_partners(OPP["id"])
    assert [p.get("agency") for p in params(api, AWARDS)] == ["dept_of_the_air_force", None]
    assert out.partners[0].why == "Won 1 similar award in NAICS 336413 since Sep 1, 2026."
    assert "Nothing came back for the opportunity's state or agency" in out.note
    api.requests.clear()  # state and agency both empty after one page each: 4 pages left for the fallback
    api.replies = [router(detail(), {(None, "OK"): [results([])], ("dept_of_the_air_force", None): [results([])],
                                     (None, None): [results(ROWS[:1])]})]
    out = await tools.find_teaming_partners(OPP["id"])
    assert len(params(api, AWARDS)) == 3 and len(out.partners) == 1 and out.sample.complete is True
    api.requests.clear()  # the state lookup fails: the fallback still runs, and only its rows make the sample

    def failing_state(req):
        if req.url.params.get("state"):
            return httpx.Response(500, json={"error": "internal_error"})
        return router(detail(), {("dept_of_the_air_force", None): [results([])],
                                 (None, None): [results(ROWS[:2])]})(req)

    api.replies = [failing_state]
    out = await tools.find_teaming_partners(OPP["id"])
    assert len(params(api, AWARDS)) == 3 and len(out.partners) == 2
    assert out.sample.complete is True and "Based on all 2 award records found" in out.note
    assert out.warning.startswith("Award lookup cut short: MatchAwards server error") and out._partial is True


@pytest.mark.anyio
@pytest.mark.parametrize("first", [
    [results([], has_more=True, cursor="c")],  # sparse: 3 empty pages, more left
    "fail",
])
async def test_a_superseded_query_does_not_mark_the_sample_incomplete(api, first):
    def reply(req):
        if first == "fail" and req.url.params.get("agency"):
            return httpx.Response(503, json={"error": "timeout"}, headers={"Retry-After": "30"})
        return router(detail(), {("dept_of_the_air_force", None): first if first != "fail" else [],
                                 (None, None): [results(ROWS[:3])]})(req)

    api.replies = [reply]
    out = await tools.assess_opportunity(OPP["id"])
    assert out.basis == "same NAICS, all agencies" and out.sample.complete is True
    assert "Based on all 3 award records found" in out.note and "older awards were not read" not in out.note
    assert len(params(api, AWARDS)) == (4 if first != "fail" else 2)
    if first == "fail":
        assert out._partial is True and "took too long" in out.warning
    else:
        assert out._partial is False and out.warning is None


@pytest.mark.anyio
async def test_totals_are_rounded_to_cents(api):
    rows = [win(i, "ALPHA", "A" * 12, a, "2026-01-01") for i, a in enumerate([0.1, 0.2, 1082394.55])]
    api.replies = [router(detail(), {("dept_of_the_air_force", None): [results(rows)]})]
    out = await tools.assess_opportunity(OPP["id"])
    assert out.typical_winners[0].total_amount == 1082394.85
    assert out.award_value_range.model_dump() == {"p25": 0.15, "median": 0.2, "p75": 541197.38}


@pytest.mark.anyio
async def test_a_capped_sample_says_older_awards_were_not_read(api):
    rows = [win(i, f"FIRM {i}", None, 1.0, f"2026-10-{1 + i % 9:02d}") for i in range(50)]
    api.replies = [router(detail(), {(None, "OK"): [results(rows, has_more=True, cursor="c")],
                                     ("dept_of_the_air_force", None): [results([])]})]
    out = await tools.find_teaming_partners(OPP["id"])
    # 3 pages of the same 50 rows (deduplicated) and more left: not complete, though 150 rows were not reached.
    assert out.sample.model_dump() == {"awards_read": 50, "oldest_award_date": "2026-10-01",
                                       "newest_award_date": "2026-10-09", "complete": False}
    assert "Based on the newest 50 award records (2026-10-01 to 2026-10-09); older awards were not read." in out.note
    assert all(p.uei is None for p in out.partners)
    assert tools.NO_UEI_NOTE in out.note and "get_company" not in out.note  # no hint when no firm has a UEI
    assert all(p.why.endswith(", 2026.") and "since Oct " in p.why for p in out.partners)


@pytest.mark.anyio
async def test_a_sparse_state_query_beside_a_full_agency_one_keeps_the_sample_complete(api):
    api.replies = [router(detail(), {(None, "OK"): [results([], has_more=True, cursor="c")],
                                     ("dept_of_the_air_force", None): [results(ROWS[:2])]})]
    out = await tools.find_teaming_partners(OPP["id"])
    assert len(params(api, AWARDS)) == 4 and out.sample.complete is True and out._partial is False


@pytest.mark.anyio
async def test_upstream_text_in_why_lines_and_agencies_is_cleaned(api):
    agency = "Dept\nof\x00 Things " + "x" * 300
    rows = [win(1, "ALPHA", "A" * 12, 1.0, "2026-01-01", agency=agency)]
    api.replies = [router(detail(state=None, agency=agency), {("dept_of_the_air_force", None): [results(rows)]})]
    out = await tools.find_teaming_partners(OPP["id"])
    [p] = out.partners
    assert len(p.why) <= 200 and len(p.agencies[0]) <= 120
    for text in (p.why, p.agencies[0], out.note):
        assert "\n" not in text and "\x00" not in text
    assert p.why.startswith("Won 1 similar award at Dept of Things xxx")


@pytest.mark.anyio
async def test_an_invalid_upstream_naics_code_is_not_sent(api):
    api.replies = [router(detail(naics={"code": "33641,3", "title": "x"}), {})]
    out = await tools.assess_opportunity(OPP["id"])
    assert params(api, AWARDS) == [] and out.basis is None
