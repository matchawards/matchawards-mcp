"""UTM parameters on matchawards.com links: the utm_source label per User-Agent family, which URLs are tagged, and
that every tool result carries them. No network."""

import contextlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from matchawards_mcp import links, tools, usage

from test_data_tools import AWARDS, COMPANY, awards_page, by_id, opp, schema_checked
from test_http import http, no_api_budget_leak  # noqa: F401  (fixture)
from test_server import api, anyio_backend, ok, page  # noqa: F401  (fixtures)

SITE = "https://matchawards.com/a/posts/1"


def utm(url):
    """The utm_* parameters of a URL, one value each."""
    return {k: v for k, [v] in parse_qs(urlsplit(url).query).items() if k.startswith("utm_")}


def params(source="mcp"):
    return {"utm_source": source, "utm_medium": "ai_agent", "utm_campaign": "matchawards_mcp"}


# --- utm_source ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("family, label", [
    ("openai-mcp", "chatgpt"),
    ("claude-user", "claude"), ("claudebot", "claude"), ("claude", "claude"),
    ("cursor", "cursor"), ("vscode", "vscode"),
    ("n8n-nodes-matchawards", "n8n"),
    ("chatgpt-user", "mcp"), ("perplexity", "mcp"), ("matchawards-mcp", "mcp"), ("matchawards-cli", "mcp"),
    ("matchawards-test", "mcp"), ("prober", "mcp"), ("other", "mcp"), ("", "mcp"), (None, "mcp"),
])
def test_utm_source_per_family(family, label):
    assert links.utm_source(family) == label


@pytest.mark.parametrize("ua", ["Mozilla/5.0 (evil) utm_source=spam", "x" * 500, "Claude-User/1.0 <script>"])
def test_utm_source_is_a_fixed_label_whatever_the_user_agent(ua):
    assert links.utm_source(usage.classify_ua(ua)) in {"chatgpt", "claude", "cursor", "vscode", "n8n", "mcp"}


# --- tagged ----------------------------------------------------------------------------------------------------

def test_a_site_link_is_tagged_with_the_default_source_outside_a_request():
    assert links.tagged(SITE) == f"{SITE}?utm_source=mcp&utm_medium=ai_agent&utm_campaign=matchawards_mcp"


@contextlib.contextmanager
def as_source(label):
    """links.source set for the block and reset after it, so it cannot leak into the next test."""
    token = links.source.set(label)
    try:
        yield
    finally:
        links.source.reset(token)


def test_the_request_source_is_used():
    token = links.source.set("claude")
    try:
        assert utm(links.tagged(SITE)) == params("claude")
    finally:
        links.source.reset(token)


@pytest.mark.parametrize("url", [
    "https://sam.gov/opp/1/view", "https://www.usaspending.gov/award/X", "https://www.usajobs.gov/job/1",
    "https://matchawards.com.example.net/a", "https://evilmatchawards.com/a",
    "https://evil.example/?u=https://matchawards.com/a",
    "https://matchawards.com@evil.example/a", "mailto:x@matchawards.com", "javascript:alert(1)//matchawards.com",
    "/a/posts/1", "not a url", "http://[::1/a",
    "https://evil.example\\.matchawards.com/a",  # a browser reads the backslash as a slash: host evil.example
    "https://evil.example%2f.matchawards.com/a", "https://matchawards.com/a\\b",
])
def test_other_links_are_unchanged(url):
    assert links.tagged(url) == url


def test_subdomains_are_tagged_too():  # staging links come back while the API base is an internal address
    assert utm(links.tagged("https://staging.matchawards.com/a/posts/1")) == params()
    assert utm(links.tagged("https://www.matchawards.com/a/1")) == params()
    assert utm(links.tagged("HTTPS://MatchAwards.com/a")) == params()  # host compared case-insensitively


def test_existing_query_and_fragment_are_kept():
    out = links.tagged("https://matchawards.com/groups/908?page=2&q=a%20b#top")
    assert out == ("https://matchawards.com/groups/908?page=2&q=a%20b&utm_source=mcp&utm_medium=ai_agent"
                   "&utm_campaign=matchawards_mcp#top")


@pytest.mark.parametrize("url", [
    "https://matchawards.com/a?utm_source=newsletter",
    "https://matchawards.com/a?x=1&utm_campaign=spring",
    "https://matchawards.com/a?UTM_Medium=email",
    "https://matchawards.com/a?utm_source=mcp&utm_medium=ai_agent&utm_campaign=matchawards_mcp",
])
def test_an_already_tagged_link_is_unchanged(url):
    assert links.tagged(url) == url


@pytest.mark.parametrize("url", [None, ""])
def test_no_link_stays_none(url):
    assert links.tagged(url) is None


def test_tagging_twice_is_tagging_once():
    assert links.tagged(links.tagged(SITE)) == links.tagged(SITE)


# --- tool results ----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_search_contracts_rows_carry_utm_and_sam_url_is_untouched(api):
    rows = [{**r, "sam_url": f"https://sam.gov/opp/{r['id']}/view"} for r in page([1, 2])["results"]]
    api.replies = [ok({**page([]), "results": rows})]
    with as_source("chatgpt"):  # as HttpGuard does
        out = await tools.search_contracts(limit=2)
    assert [utm(r.url) for r in out.results] == [params("chatgpt")] * 2
    assert [r.sam_url for r in out.results] == ["https://sam.gov/opp/1/view", "https://sam.gov/opp/2/view"]


@pytest.mark.anyio
async def test_get_opportunity_url_carries_utm_through_the_mcp_layer(api):
    api.replies = [ok({"as_of": "x", "result": {"id": "7", "type": "federal", "url": "https://matchawards.com/a/7",
                                                "sam_url": "https://sam.gov/opp/7/view"}})]
    out = await schema_checked("get_opportunity", {"id": "7"})
    assert utm(out["url"]) == params() and out["sam_url"] == "https://sam.gov/opp/7/view"


@pytest.mark.anyio
async def test_award_rows_carry_utm_and_source_url_is_untouched(api):
    api.replies = [ok(awards_page([1]))]
    [row] = (await tools.search_awards()).results
    assert utm(row.url) == params() and row.source_url == "https://sam.gov/opp/1"


@pytest.mark.anyio
async def test_naics_and_company_links_carry_utm(api):
    api.replies = [ok({"as_of": "x", "results": [
        {"code": "541512", "title": "t", "opportunities_url": "https://matchawards.com/groups/908"}]})]
    [code] = (await tools.lookup_naics("software")).results
    assert utm(code.opportunities_url) == params()
    api.replies = [ok({"as_of": "x", "result": COMPANY})]
    first, no_page = (await tools.get_company("C3KXNLTAAY98")).recent_awards
    assert utm(first.url) == params() and first.source_url == "https://sam.gov/opp/1" and no_page.url is None


@pytest.mark.anyio
async def test_composite_summary_and_similar_awards_carry_utm(api):
    detail = {"id": "9", "type": "federal", "naics": {"code": "541512"}, "agency_id": "dept_of_the_army",
              "url": "https://matchawards.com/a/9", "sam_url": "https://sam.gov/opp/9/view"}
    api.replies = [lambda req: httpx.Response(200, json=awards_page([1]) if req.url.path == AWARDS
                                              else {"as_of": "x", "result": detail})]
    out = await tools.assess_opportunity("9")
    assert utm(out.opportunity.url) == params() and out.opportunity.sam_url == "https://sam.gov/opp/9/view"
    assert [utm(a.url) for a in out.similar_awards] == [params()]
    assert out.opportunity.url.count("utm_source") == 1  # the summary is built from the tagged row: not twice


@pytest.mark.anyio
async def test_show_results_rows_carry_utm(api):
    api.replies = by_id(_1=httpx.Response(200, json=opp(1, "federal")), _2=httpx.Response(200, json=opp(2, "grant")))
    with as_source("claude"):
        out = await tools.show_results(["1", "2"])
    assert [utm(r.url) for r in out.results] == [params("claude")] * 2


@pytest.mark.anyio
@pytest.mark.parametrize("ua, label", [("openai-mcp/1.0.0", "chatgpt"), ("Claude-User/1.0", "claude"),
                                       ("n8n-nodes-matchawards/0.3.0", "n8n"), ("curl/8.0", "mcp")])
async def test_over_http_the_source_comes_from_the_user_agent(api, ua, label):
    api.replies = [ok(page([1]))]
    async with http(headers={"User-Agent": ua, "X-Real-IP": "203.0.113.7"}) as hc:
        async with Client(streamable_http_client("http://matchawards.com/mcp", http_client=hc)) as client:
            out = await client.call_tool("search_contracts", {"state": "VA"})
    assert not out.is_error, out.content
    [row] = out.structured_content["results"]
    assert utm(row["url"]) == params(label)
    assert links.source.get() == "mcp"  # reset after the request


# --- go-links (MATCHAWARDS_GO_LINKS=1) --------------------------------------------------------------------------

PAGE = "https://matchawards.com/a/posts/7?utm_source=mcp&utm_medium=ai_agent&utm_campaign=matchawards_mcp"


@pytest.fixture
def go_on(monkeypatch):
    monkeypatch.setattr(links, "GO_LINKS", True)


def go_url(via, record_id, host="matchawards.com"):
    return (f"https://{host}/go/{via}/{record_id}"
            f"?utm_source={via}&utm_medium=ai_agent&utm_campaign=matchawards_mcp")


def test_with_go_links_off_the_source_link_is_plain(monkeypatch):
    monkeypatch.setattr(links, "GO_LINKS", False)
    assert links.go("7", PAGE, "https://sam.gov/opp/7/view") == "https://sam.gov/opp/7/view"


def test_a_source_link_becomes_the_records_go_link_with_the_caller_as_via(go_on):
    with as_source("chatgpt"):
        assert links.go("7", PAGE, "https://sam.gov/opp/7/view") == go_url("chatgpt", "7")
    assert links.go("7", PAGE, "https://www.usaspending.gov/award/X") == go_url("mcp", "7")


def test_the_largest_site_status_id_is_go_linked(go_on):
    assert links.go(str(2**63 - 1), PAGE, "https://sam.gov/x") == go_url("mcp", str(2**63 - 1))


def test_a_staging_record_gets_a_staging_go_link(go_on):
    page = "https://staging.matchawards.com/a/posts/7"
    assert links.go("7", page, "https://sam.gov/opp/7/view") == go_url("mcp", "7", "staging.matchawards.com")


@pytest.mark.parametrize("record_id, page, url", [
    (None, PAGE, "https://sam.gov/x"),                     # an award with no MatchAwards page
    ("", PAGE, "https://sam.gov/x"),
    ("7/../admin", PAGE, "https://sam.gov/x"),             # not a number: never pasted into the path
    ("1e5", PAGE, "https://sam.gov/x"),
    (str(2**63), PAGE, "https://sam.gov/x"),               # past the site's status ids
    ("1" * 20, PAGE, "https://sam.gov/x"),
    ("7", None, "https://sam.gov/x"),                      # no page on the site to take the host from
    ("7", "https://evil.example/a/posts/7", "https://sam.gov/x"),
    ("7", "https://matchawards.com.evil.example/a/7", "https://sam.gov/x"),
    ("7", "http://matchawards.com/a/7", "https://sam.gov/x"),
    ("7", "https://user@matchawards.com/a/7", "https://sam.gov/x"),
    ("7", "https://evil.example\\@matchawards.com/a/7", "https://sam.gov/x"),
    ("7", "https://[::1/a", "https://sam.gov/x"),          # unparsable page URL
    ("7", PAGE, "https://matchawards.com/go/mcp/7"),       # already a site link: left alone, not wrapped twice
])
def test_the_plain_link_is_kept_when_no_safe_go_link_can_be_built(go_on, record_id, page, url):
    assert links.go(record_id, page, url) == url


@pytest.mark.parametrize("url", [None, ""])
def test_no_source_link_stays_none(go_on, url):
    assert links.go("7", PAGE, url) is None


@pytest.mark.anyio
async def test_with_go_links_on_search_rows_summaries_and_awards_carry_go_links(api, go_on):
    rows = [{**r, "sam_url": f"https://sam.gov/opp/{r['id']}/view"} for r in page([1, 2])["results"]]
    api.replies = [ok({**page([1, 2]), "results": rows})]
    with as_source("claude"):
        out = await tools.search_contracts(state="VA")
    assert [r.sam_url for r in out.results] == [go_url("claude", "1"), go_url("claude", "2")]

    detail = {"id": "9", "type": "federal", "naics": {"code": "541512"}, "agency_id": "dept_of_the_army",
              "url": "https://matchawards.com/a/9", "sam_url": "https://sam.gov/opp/9/view"}
    api.replies = [lambda req: httpx.Response(200, json=awards_page([1]) if req.url.path == AWARDS
                                              else {"as_of": "x", "result": detail})]
    out = await tools.assess_opportunity("9")
    assert out.opportunity.sam_url == go_url("mcp", "9")  # the summary keeps the row's go-link, not wrapped twice
    assert [a.source_url for a in out.similar_awards] == [go_url("mcp", "1")]

    api.replies = [ok({"as_of": "x", "result": COMPANY})]
    first, no_page = (await tools.get_company("C3KXNLTAAY98")).recent_awards
    assert first.source_url == go_url("mcp", "1")
    assert no_page.source_url == "https://sam.gov/opp/2"  # no MatchAwards id: the plain link


@pytest.mark.anyio
async def test_with_go_links_on_the_output_still_matches_the_published_schema(api, go_on):
    api.replies = [ok({"as_of": "x", "result": {"id": "7", "type": "federal", "url": "https://matchawards.com/a/7",
                                                "sam_url": "https://sam.gov/opp/7/view"}})]
    out = await schema_checked("get_opportunity", {"id": "7"})
    assert out["sam_url"] == go_url("mcp", "7")
