"""UTM parameters on matchawards.com links: the utm_source label per User-Agent family, which URLs are tagged, and
that every tool result carries them. No network."""

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


def test_the_request_source_is_used():
    token = links.source.set("claude")
    try:
        assert utm(links.tagged(SITE)) == params("claude")
    finally:
        links.source.reset(token)


@pytest.mark.parametrize("url", [
    "https://sam.gov/opp/1/view", "https://www.usaspending.gov/award/X", "https://www.usajobs.gov/job/1",
    "https://matchawards.com.example.net/a", "https://evil.example/?u=https://matchawards.com/a",
    "https://matchawards.com@evil.example/a", "mailto:x@matchawards.com", "javascript:alert(1)//matchawards.com",
    "/a/posts/1", "not a url", "http://[::1/a",
])
def test_other_links_are_unchanged(url):
    assert links.tagged(url) == url


def test_the_api_host_is_tagged_too(monkeypatch):
    monkeypatch.setattr(links, "SITE_HOSTS", frozenset({"matchawards.com", "staging.example.org"}))
    assert utm(links.tagged("https://staging.example.org/a/posts/1")) == params()
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
    links.source.set("chatgpt")  # as HttpGuard does; an async test runs in its own context, so it ends with the test
    rows = [{**r, "sam_url": f"https://sam.gov/opp/{r['id']}/view"} for r in page([1, 2])["results"]]
    api.replies = [ok({**page([]), "results": rows})]
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
    links.source.set("claude")
    api.replies = by_id(_1=httpx.Response(200, json=opp(1, "federal")), _2=httpx.Response(200, json=opp(2, "grant")))
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
