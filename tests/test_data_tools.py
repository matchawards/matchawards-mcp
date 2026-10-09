"""lookup_naics, search_awards, get_company and the agency-name filter, against a mocked API. No network."""

import json

import httpx
import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from matchawards_mcp import server, tools
from matchawards_mcp import api as ma_api

from test_server import PATH, api, anyio_backend, ok, page  # noqa: F401  (fixtures)

AWARDS = "/api/public/v1/awards"
AGENCIES = "/api/public/v1/agencies"
COMPANIES = "/api/public/v1/companies"
NAICS = "/api/public/v1/naics"
PII = {"email": "co@example.com", "phone": "202-555-0100", "contacts": [{"name": "Jo", "email": "jo@example.com"}]}


@pytest.fixture(autouse=True)
def fresh_agency_cache(monkeypatch):
    monkeypatch.setattr(ma_api, "_agencies", {})


def award(i, **kw):
    return {
        "id": str(i), "type": "contract_award", "title": f"Award {i}",
        "awardee": {"name": f"FIRM {i} LLC", "uei": f"UEI{i:09d}", "duns": None},
        "amount": 1000.0 * i, "award_date": "2026-10-01", "period": None, "agency": "DEPT OF THE ARMY",
        "agency_id": "dept_of_the_army", "naics": {"code": "541512", "title": "Computer Systems Design Services"},
        "set_aside": None, "state": "VA", "city": "Arlington", "solicitation_number": f"W{i}",
        "contract_number": f"C{i}", "cfda_number": None, "url": f"https://matchawards.com/a/posts/{i}",
        "source_url": f"https://sam.gov/opp/{i}", **kw,
    }


def awards_page(ids, has_more=False, cursor=None, **kw):
    return {"as_of": "2026-10-09T12:00:00Z", "results": [award(i, **kw) for i in ids], "has_more": has_more,
            "next_cursor": cursor}


def agencies(*pairs):
    return {"as_of": "x", "results": [{"agency_id": a, "name": n, "url": f"https://matchawards.com/{a}"}
                                      for a, n in pairs]}


def route(**bodies):
    """A reply by path: {path: body}; the opportunities path defaults to an empty page."""
    table = {PATH: page([]), **{v: bodies[k] for k, v in
                                (("awards", AWARDS), ("agencies", AGENCIES), ("naics", NAICS)) if k in bodies}}
    return lambda req: httpx.Response(200, json=table[req.url.path])


def params(api, path=None):
    return [dict(r.url.params) for r in api.requests if path is None or r.url.path == path]


async def run_dump(coro):
    return (await coro).model_dump()


async def schema_checked(name, args):
    """Call through the MCP layer; check structuredContent against the published outputSchema."""
    schema = {t.name: t.output_schema for t in await server.mcp.list_tools()}[name]
    out = await server.mcp.call_tool(name, args)
    assert not out.is_error, out.content
    jsonschema.validate(out.structured_content, schema)
    assert json.loads(out.content[0].text) == out.structured_content
    return out.structured_content


# --- lookup_naics -------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("query, sent", [
    ("cybersecurity", {"q": "cybersecurity", "limit": "10"}),
    ("  janitorial   services ", {"q": "janitorial services", "limit": "10"}),
    ("5415", {"code": "5415", "limit": "10"}),
    ("541512", {"code": "541512", "limit": "10"}),
    ("1234567", {"q": "1234567", "limit": "10"}),  # longer than a code: words
])
async def test_lookup_naics_sends_code_or_words(api, query, sent):
    api.replies = [ok({"as_of": "x", "results": []})]
    await tools.lookup_naics(query)
    assert [r.url.path for r in api.requests] == [NAICS] and params(api) == [sent]


@pytest.mark.anyio
async def test_lookup_naics_output(api):
    api.replies = [ok({"as_of": "x", "results": [
        {"code": "541512", "title": "Computer Systems Design Services",
         "opportunities_url": "https://matchawards.com/groups/908", "email": "x@example.com"},
        {"code": "541519", "title": "Other Computer Related Services", "opportunities_url": None}]})]
    out = await schema_checked("lookup_naics", {"query": "cybersecurity", "limit": 2})
    assert [r["code"] for r in out["results"]] == ["541512", "541519"]
    assert "markdown link" in out["note"] and "email" not in json.dumps(out)
    assert params(api) == [{"q": "cybersecurity", "limit": "2"}]


# --- search_awards ------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_search_awards_query_building(api):
    api.replies = [ok(awards_page([]))]
    await tools.search_awards()
    await tools.search_awards(type="grant_award", naics="541512, 541519", state="tx", awardee="acme",
                              uei="c3kxnltaay98", keyword="cyber", awarded_within_days=1095, limit=5, cursor="c1")
    assert params(api, AWARDS) == [
        {"type": "any", "awarded_within_days": "365", "limit": "20"},
        {"type": "grant_award", "naics": "541512,541519", "state": "TX", "awardee": "acme", "uei": "C3KXNLTAAY98",
         "q": "cyber", "awarded_within_days": "1095", "limit": "5", "cursor": "c1"},
    ]


@pytest.mark.anyio
async def test_search_awards_rows_note_and_schema(api):
    api.replies = [ok({**awards_page([1, 2], has_more=True, cursor="c1"), **PII})]
    out = await schema_checked("search_awards", {"limit": 2})
    first = out["results"][0]
    assert first["awardee"] == {"name": "FIRM 1 LLC", "uei": "UEI000000001", "duns": None}
    assert (first["amount"], first["agency"], first["agency_id"]) == (1000.0, "Dept of the Army", "dept_of_the_army")
    assert (out["has_more"], out["next_cursor"]) == (True, "c1")
    assert out["note"].startswith("Award records may be incomplete. ")
    assert out["note"].endswith(tools.AWARD_LINK_RULE) and "markdown links" in out["note"]


@pytest.mark.anyio
async def test_search_awards_pages_dedupes_and_skips_bad_rows(api):
    bad = [{"title": "no id"}, {**award(9), "id": None}, {**award(8), "amount": "a lot"}, {**award(7), "id": 7}]
    api.replies = [
        ok({**awards_page([1, 2], has_more=True, cursor="c1"), "results": [award(1), *bad, award(2)]}),
        ok(awards_page([2, 3], has_more=True, cursor="c2")),
        ok(awards_page([4], has_more=True, cursor="c3")),
    ]
    out = await run_dump(tools.search_awards(limit=5))
    assert [r["id"] for r in out["results"]] == ["1", "2", "3", "4"]
    assert [p["limit"] for p in params(api)] == ["5", "3", "2"] and len(api.requests) == 3
    assert out["warning"] == "4 result(s) skipped: unexpected format."


@pytest.mark.anyio
async def test_no_contact_data_from_award_rows(api):
    api.replies = [ok(awards_page([1], **PII))]
    out = await server.mcp.call_tool("search_awards", {})
    for text in (json.dumps(out.structured_content), out.content[0].text):
        assert "email" not in text and "phone" not in text and "@example.com" not in text and "555-01" not in text


@pytest.mark.anyio
@pytest.mark.parametrize("tool, args", [
    ("search_awards", {"type": "federal"}),
    ("search_awards", {"awarded_within_days": 0}),
    ("search_awards", {"awarded_within_days": 1096}),
    ("search_awards", {"uei": "C3KXNLTAAY9"}),
    ("search_awards", {"uei": "C3KXNLTAAY98!"}),
    ("search_awards", {"awardee": "a"}),
    ("search_awards", {"agency": "x"}),
    ("search_awards", {"limit": 51}),
    ("search_awards", {"naics": "5415"}),
    ("search_contracts", {"agency": "a" * 101}),
    ("lookup_naics", {"query": "a"}),
    ("lookup_naics", {"query": "cyber", "limit": 26}),
    ("get_company", {"uei": "short"}),
    ("get_company", {"uei": "../companies"}),
])
async def test_bad_input_is_rejected_before_any_request(api, tool, args):
    with pytest.raises(ToolError):
        await server.mcp.call_tool(tool, args)
    assert api.requests == []


# --- agency names -------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_agency_ranking_exact_then_prefix_then_api_order(api):
    api.replies = [route(agencies=agencies(
        ("vets_grants", "Legal Services for Veterans Grants"),
        ("vets_training", "Veterans Employment And Training Services"),
        ("department_of_veterans_affairs", "United States Department of Veterans Affairs"),
        ("va", "Veterans Affairs"),
    ), awards=awards_page([1]))]
    out = await run_dump(tools.search_awards(agency="veterans affairs"))
    assert params(api, AGENCIES) == [{"q": "veterans affairs", "limit": "25"}]
    assert params(api, AWARDS)[0]["agency"] == "va"  # the exact name beats prefix and substring matches
    assert "Filtered by agency: Veterans Affairs (va). Other matches: Legal Services for Veterans Grants, " \
           "Veterans Employment And Training Services, United States Department of Veterans Affairs." in out["note"]
    ma_api._agencies.clear()
    await tools.search_awards(agency="Veterans")  # no exact match: the name starting with it wins
    assert params(api, AWARDS)[-1]["agency"] == "vets_training"
    ma_api._agencies.clear()
    await tools.search_awards(agency="affairs")  # no exact or prefix match: the API's first
    assert params(api, AWARDS)[-1]["agency"] == "vets_grants"


@pytest.mark.anyio
async def test_duplicate_department_accounts_resolve_together_and_only_the_first_is_sent(api, monkeypatch):
    api.replies = [route(agencies=agencies(
        ("department_of_defense", "Department Of Defense"),
        ("dept_of_defense", "United States Department of Defense"),
        ("defense_logistics_agency", "Defense Logistics Agency"),
    ), awards=awards_page([1]))]
    ids, note = await ma_api.resolve_agency("department of defense")
    assert ids == ["department_of_defense", "dept_of_defense"]
    assert note == ("Filtered by agency: Department Of Defense (department_of_defense). Other matches: "
                    "United States Department of Defense, Defense Logistics Agency.")
    await tools.search_awards(agency="department of defense")
    assert params(api, AWARDS)[-1]["agency"] == "department_of_defense"
    monkeypatch.setattr(ma_api, "AGENCY_SLUGS_PER_QUERY", 5)  # the API may take a list later: one constant
    await tools.search_awards(agency="department of defense")
    assert params(api, AWARDS)[-1]["agency"] == "department_of_defense,dept_of_defense"


@pytest.mark.anyio
async def test_no_agency_match_searches_all_agencies_and_says_so(api):
    api.replies = [route(agencies=agencies(), awards=awards_page([1]))]
    out = await run_dump(tools.search_awards(agency="Ministry of Magic", naics="541512"))
    assert "agency" not in params(api, AWARDS)[0] and len(out["results"]) == 1
    assert "No agency matched 'Ministry of Magic'; showing all agencies." in out["note"]


@pytest.mark.anyio
async def test_agency_resolutions_are_cached_for_ten_minutes(api, monkeypatch):
    api.replies = [route(agencies=agencies(("dept_of_the_army", "Department of the Army")), awards=awards_page([]))]
    now = [1000.0]
    monkeypatch.setattr(ma_api.time, "monotonic", lambda: now[0])
    for name in ("Department of the Army", "department  of the ARMY"):
        await tools.search_awards(agency=name)
    assert len(params(api, AGENCIES)) == 1
    now[0] += 601
    await tools.search_awards(agency="Department of the Army")
    assert len(params(api, AGENCIES)) == 2
    monkeypatch.setattr(ma_api, "AGENCY_CACHE_MAX", 2)
    for name in ("aa", "bb", "cc"):
        await ma_api.resolve_agency(name)
    assert len(ma_api._agencies) == 2


@pytest.mark.anyio
async def test_search_contracts_agency_filter_and_sparse_paging(api):
    api.replies = [
        ok(agencies(("dept_of_the_air_force", "Department of the Air Force"))),
        ok(page([], has_more=True, cursor="c1")),  # an agency-filtered slice with no match: keep going
        ok(page([5], has_more=True, cursor="c2")),
    ]
    out = await run_dump(tools.search_contracts(agency="Department of the Air Force", limit=1))
    sent = params(api, PATH)
    assert [(p["agency"], p.get("cursor")) for p in sent] == [("dept_of_the_air_force", None),
                                                               ("dept_of_the_air_force", "c1")]
    assert [r["id"] for r in out["results"]] == ["5"] and out["warning"] is None
    assert out["note"] == f"Filtered by agency: Department of the Air Force (dept_of_the_air_force). {tools.LINK_RULE}"


@pytest.mark.anyio
async def test_pages_used_up_with_no_match_say_where_to_continue(api):
    api.replies = [ok(agencies(("dept_of_the_navy", "Department of the Navy"))),
                   ok(page([], has_more=True, cursor="c9"))]
    out = await run_dump(tools.search_contracts(agency="Department of the Navy"))
    assert len(params(api, PATH)) == 3 and out["results"] == []
    assert out["warning"] == ("No matches in the newest results scanned; more may exist further back. Call again "
                              "with cursor=c9 to keep searching.")


# --- get_company --------------------------------------------------------------------------------------------

COMPANY = {
    "uei": "C3KXNLTAAY98", "name": "UNI OF TX HEALTH SCIENCE CTR SAN ANT",
    "other_names": ["UNIVERSITY OF TEXAS HEALTH SCIENCE CENTER OF SAN ANTONIO"], "duns": "800772162",
    "location": {"city": "SAN ANTONIO", "state": "TX", "country": "UNITED STATES", "street": "1 Main St"},
    "totals": {"contract_awards": {"count": 0, "amount": 0.0}, "grant_awards": {"count": 179, "amount": 2.8e8}},
    "first_award_date": "2000-09-30", "last_award_date": "2027-02-27", "top_naics": [],
    "top_agencies": [{"agency": "DEPARTMENT OF DEFENSE", "agency_id": "department_of_defense", "count": 28}],
    "recent_awards": [award(1, type="grant_award", cfda_number="93.398"), {**award(2), "id": None, "url": None}],
    "coverage_note": "Based on award records in MatchAwards, which may be incomplete.",
    "ma_profile_url": "https://matchawards.com/someone", **PII,
}


@pytest.mark.anyio
async def test_get_company(api):
    api.replies = [ok({"as_of": "x", "result": COMPANY})]
    out = await schema_checked("get_company", {"uei": "c3kxnltaay98"})
    assert [r.url.path for r in api.requests] == [f"{COMPANIES}/C3KXNLTAAY98"]
    assert out["top_agencies"] == [{"agency": "Department of Defense", "agency_id": "department_of_defense",
                                    "count": 28}]
    assert out["location"] == {"city": "SAN ANTONIO", "state": "TX", "country": "UNITED STATES"}  # no street
    assert [r["id"] for r in out["recent_awards"]] == ["1", None]  # an award without a page keeps its row
    assert out["coverage_note"].endswith("may be incomplete.") and out["note"].startswith("Award records")
    text = json.dumps(out)
    assert "ma_profile_url" not in text and "email" not in text and "phone" not in text and "@" not in text


@pytest.mark.anyio
async def test_get_company_not_found_and_bad_shapes(api):
    api.replies = [lambda req: httpx.Response(404, json={"error": "not_found"})]
    with pytest.raises(ToolError, match="^No award records for this UEI on MatchAwards") as e:
        await tools.get_company("C3KXNLTAAY98")
    assert e.value.outcome == "invalid_input"
    api.replies = [ok(COMPANY)]  # no result envelope
    with pytest.raises(ToolError, match="no result object"):
        await tools.get_company("C3KXNLTAAY98")


@pytest.mark.anyio
@pytest.mark.parametrize("call", [
    lambda: tools.lookup_naics("cyber"),
    lambda: tools.search_awards(),
    lambda: tools.get_company("C3KXNLTAAY98"),
    lambda: tools.search_awards(agency="Department of the Army"),  # the agencies lookup times out
])
async def test_api_timeouts_are_upstream_timeout(api, call):
    api.replies = [lambda req: httpx.Response(503, json={"error": "timeout"}, headers={"Retry-After": "30"})]
    with pytest.raises(ToolError, match="took too long on this request. Retry in 30 seconds") as e:
        await call()
    assert e.value.outcome == "upstream_timeout"


# --- Upstream values that must not reach a request or a note as they are -------------------------------------

@pytest.mark.anyio
async def test_agency_ids_outside_the_slug_alphabet_are_never_used(api):
    api.replies = [route(agencies=agencies(
        ("foo,bar", "Department of Poison"), ("../x", "Department of Poison Two"), ("ok_id", "Department of Poison"),
        ("defense_security_cooperation_university_-_research_grants", "Defense Security Cooperation University")),
        awards=awards_page([]))]
    ids, note = await ma_api.resolve_agency("department of poison")
    assert ids == ["ok_id"] and "foo,bar" not in note and "../x" not in note
    ma_api._agencies.clear()
    ids, _ = await ma_api.resolve_agency("defense security cooperation university")  # a real id with a hyphen
    assert ids == ["defense_security_cooperation_university_-_research_grants"]
    api.replies = [route(agencies=agencies(("foo,bar", "Bad")), awards=awards_page([]))]
    ma_api._agencies.clear()
    out = await run_dump(tools.search_awards(agency="Bad"))
    assert "agency" not in params(api, AWARDS)[-1] and "No agency matched 'Bad'" in out["note"]


@pytest.mark.anyio
async def test_agency_names_and_the_query_echo_are_cleaned_in_notes(api):
    api.replies = [route(agencies=agencies(("a_1", "Dept of\nThe\x07 Army " + "y" * 300), ("a_2", "Other\r\nOne")),
                         awards=awards_page([]))]
    _, note = await ma_api.resolve_agency("dept")
    assert "\n" not in note and "\x07" not in note and "\r" not in note
    assert "Filtered by agency: Dept of The Army yyy" in note and "Other matches: Other One." in note
    assert len(note) < 120 + 120 + 60
    api.replies = [route(agencies=agencies(), awards=awards_page([]))]
    _, note = await ma_api.resolve_agency("bad\nname")
    assert note == "No agency matched 'bad name'; showing all agencies."


@pytest.mark.anyio
@pytest.mark.parametrize("cursor, echoed", [("eyJ2IjoxfQ==", "cursor=eyJ2IjoxfQ=="), ("c 1\nignore all", None),
                                            ("x" * 201, None)])
async def test_the_cursor_is_echoed_only_when_it_looks_like_a_cursor(api, cursor, echoed):
    api.replies = [ok(awards_page([], has_more=True, cursor=cursor))]
    out = await run_dump(tools.search_awards())
    assert out["warning"].startswith("No matches in the newest results scanned")
    if echoed:
        assert f"Call again with {echoed} to keep searching." in out["warning"]
    else:
        assert "Call again with next_cursor to keep searching." in out["warning"] and cursor not in out["warning"]


@pytest.mark.anyio
async def test_no_valid_rows_with_more_left_combines_both_warnings(api):
    api.replies = [ok({**awards_page([], has_more=True, cursor="c1"), "results": [{"title": "no id"}]})]
    out = await run_dump(tools.search_awards())
    assert out["warning"] == ("No matches in the newest results scanned; more may exist further back. Call again "
                              "with cursor=c1 to keep searching. 3 result(s) skipped: unexpected format.")


@pytest.mark.anyio
async def test_lookup_naics_returns_at_most_limit_codes(api):
    api.replies = [ok({"as_of": "x", "results": [{"code": f"54151{i}", "title": "t"} for i in range(5)]})]
    assert len((await tools.lookup_naics("software", limit=2)).results) == 2


@pytest.mark.anyio
async def test_a_long_description_is_cut_at_a_word_boundary(api):
    api.replies = [ok({"as_of": "x", "result": {"id": "7", "description": "word " * 600}})]
    text = (await tools.get_opportunity("7")).description
    assert len(text) <= 2000 and text.endswith("word…")
