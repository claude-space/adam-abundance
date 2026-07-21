"""HTTP client-adapter tests (PRD §4 / docs/trend-pipeline.md).

Every external client instantiates ``httpx.AsyncClient`` directly (the shared
``adapters/_http.py`` helper does too). We mock at *that* boundary: monkeypatch
``httpx.AsyncClient`` with a factory that injects an ``httpx.MockTransport``, so
the real httpx request machinery (URL/param/header building, redirects,
``raise_for_status``, ``.json()``/``.text``) runs but no socket is ever opened.

asyncio_mode="auto" (see pyproject) → async tests need no decorator.
"""

from __future__ import annotations

import json as _jsonlib
import re
import types

import httpx
import pytest

from switchboard.adapters import _http
from switchboard.adapters.base import AdapterUnavailable
from switchboard.adapters.clients.ahrefs import AhrefsClient
from switchboard.adapters.clients.firecrawl import FirecrawlClient
from switchboard.adapters.clients.hcviral import HCViralClient
from switchboard.adapters.clients.newsapi import NewsApiClient
from switchboard.adapters.clients.perplexity import PerplexityClient
from switchboard.adapters.clients.semrush import SemrushClient
from switchboard.adapters.clients.similarweb import SimilarwebClient
from switchboard.adapters.clients.tavily import TavilyClient
from switchboard.adapters.clients.x_api import XClient
from switchboard.adapters.clients.youtube import YouTubeClient


# --------------------------------------------------------------------------- #
# Mock harness
# --------------------------------------------------------------------------- #
# Captured once at import, before any patching, so repeated ``mock_httpx`` calls
# within a single test always wrap the genuine client (never a prior factory).
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class Captured:
    """Records every ``httpx.Request`` served by the mocked transport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no HTTP request was made"
        return self.requests[-1]

    def body(self, idx: int = -1) -> object:
        return _jsonlib.loads(self.requests[idx].content)

    def param(self, name: str, idx: int = -1) -> str | None:
        return self.requests[idx].url.params.get(name)

    def header(self, name: str, idx: int = -1) -> str | None:
        return self.requests[idx].headers.get(name)


def mock_httpx(monkeypatch, responses) -> Captured:
    """Patch ``httpx.AsyncClient`` so every request hits a MockTransport.

    ``responses`` may be:
      * an ``httpx.Response`` — reused for every call;
      * a callable ``(request) -> httpx.Response``;
      * a list of ``httpx.Response`` — served in order (the last repeats).
    Returns a :class:`Captured` recorder.
    """
    cap = Captured()
    real = _REAL_ASYNC_CLIENT

    if isinstance(responses, httpx.Response):
        def producer(_idx, _req, _r=responses):
            return _r
    elif callable(responses):
        def producer(_idx, req):
            return responses(req)
    else:
        seq = list(responses)

        def producer(idx, _req, _seq=seq):
            return _seq[min(idx, len(_seq) - 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = len(cap.requests)
        cap.requests.append(request)
        return producer(idx, request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return cap


def jresp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def tresp(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text)


# =========================================================================== #
# Ahrefs
# =========================================================================== #
def test_ahrefs_requires_key():
    with pytest.raises(AdapterUnavailable):
        AhrefsClient(None)
    with pytest.raises(AdapterUnavailable):
        AhrefsClient("")


async def test_ahrefs_get_happy_path(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"metrics": {"org_traffic": 42}}))
    client = AhrefsClient("secret-key")
    out = await client.get("/site-explorer/overview", {"target": "hotcars.com", "mode": "domain"})

    assert out == {"metrics": {"org_traffic": 42}}
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "api.ahrefs.com"
    # endpoint's leading slash is stripped, appended to the /v3 base once.
    assert req.url.path == "/v3/site-explorer/overview"
    assert cap.header("authorization") == "Bearer secret-key"
    assert cap.header("accept") == "application/json"
    assert cap.param("target") == "hotcars.com"
    assert cap.param("mode") == "domain"


async def test_ahrefs_get_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({"error": "forbidden"}, status=403))
    with pytest.raises(httpx.HTTPStatusError):
        await AhrefsClient("k").get("site-explorer", {})


def test_ahrefs_units_for():
    assert AhrefsClient.units_for(3) == 30
    assert AhrefsClient.units_for(0) == 0
    assert AhrefsClient.units_for(-5) == 0  # clamped at 0, never negative


# =========================================================================== #
# NewsAPI
# =========================================================================== #
def test_newsapi_requires_key():
    with pytest.raises(AdapterUnavailable):
        NewsApiClient(None)


async def test_newsapi_everything_happy_path(monkeypatch):
    payload = {
        "articles": [
            {
                "title": "EV sales surge",
                "url": "https://ex.com/a",
                "source": {"name": "Example News"},
                "publishedAt": "2026-07-20T10:00:00Z",
                "description": "Electric vehicles keep climbing.",
            }
        ]
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await NewsApiClient("news-key").everything("electric vehicles", page_size=5)

    assert out == [
        {
            "title": "EV sales surge",
            "url": "https://ex.com/a",
            "source": "Example News",
            "published_at": "2026-07-20T10:00:00Z",
            "snippet": "Electric vehicles keep climbing.",
        }
    ]
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "newsapi.org"
    assert req.url.path == "/v2/everything"
    assert cap.header("x-api-key") == "news-key"
    assert cap.param("q") == "electric vehicles"
    assert cap.param("sortBy") == "publishedAt"
    assert cap.param("pageSize") == "5"
    assert cap.param("language") == "en"


async def test_newsapi_source_string_and_missing_defaults(monkeypatch):
    # source as a bare string (not a dict) -> "" ; None source -> "" ; missing fields -> defaults.
    payload = {
        "articles": [
            {"title": "A", "source": "PlainString"},
            {"url": "https://x/y", "source": None},
            {},
        ]
    }
    mock_httpx(monkeypatch, jresp(payload))
    out = await NewsApiClient("k").everything("q")
    assert out[0] == {"title": "A", "url": "", "source": "", "published_at": "", "snippet": ""}
    assert out[1] == {"title": "", "url": "https://x/y", "source": "", "published_at": "", "snippet": ""}
    assert out[2] == {"title": "", "url": "", "source": "", "published_at": "", "snippet": ""}


async def test_newsapi_snippet_truncated_to_400(monkeypatch):
    payload = {"articles": [{"description": "x" * 900}]}
    mock_httpx(monkeypatch, jresp(payload))
    out = await NewsApiClient("k").everything("q")
    assert len(out[0]["snippet"]) == 400


async def test_newsapi_empty_and_missing_articles(monkeypatch):
    mock_httpx(monkeypatch, jresp({}))
    assert await NewsApiClient("k").everything("q") == []
    mock_httpx(monkeypatch, jresp({"articles": None}))
    assert await NewsApiClient("k").everything("q") == []


async def test_newsapi_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({"status": "error"}, status=429))
    with pytest.raises(httpx.HTTPStatusError):
        await NewsApiClient("k").everything("q")


# =========================================================================== #
# Firecrawl
# =========================================================================== #
def test_firecrawl_requires_key():
    with pytest.raises(AdapterUnavailable):
        FirecrawlClient(None)


async def test_firecrawl_search_happy_path(monkeypatch):
    payload = {
        "data": [
            {"title": "T1", "url": "https://www.example.com/path", "description": "desc one"},
            {"title": "T2", "url": "https://sub.foo.co.uk/a/b", "markdown": "md two"},
        ]
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await FirecrawlClient("fc-key").search("trend query", limit=3)

    assert out[0] == {
        "title": "T1",
        "url": "https://www.example.com/path",
        "source": "example.com",  # "//"-split, first path segment, www. stripped
        "snippet": "desc one",
    }
    # falls back to markdown when description absent; www. only stripped, sub. kept.
    assert out[1]["source"] == "sub.foo.co.uk"
    assert out[1]["snippet"] == "md two"

    req = cap.last
    assert req.method == "POST"
    assert req.url.host == "api.firecrawl.dev"
    assert req.url.path == "/v1/search"
    assert cap.header("authorization") == "Bearer fc-key"
    assert cap.header("content-type") == "application/json"
    assert cap.body() == {"query": "trend query", "limit": 3}


async def test_firecrawl_search_non_dict_data_is_empty(monkeypatch):
    # data is a list (not the {"data": [...]} envelope) -> guarded to [].
    mock_httpx(monkeypatch, jresp(["not", "an", "envelope"]))
    assert await FirecrawlClient("k").search("q") == []


async def test_firecrawl_scrape_happy_path(monkeypatch):
    payload = {"data": {"metadata": {"title": "Page Title"}, "markdown": "# Heading\nbody"}}
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await FirecrawlClient("k").scrape("https://target.com/article")

    assert out == {
        "url": "https://target.com/article",  # echoes the INPUT url, not the response
        "title": "Page Title",
        "markdown": "# Heading\nbody",
    }
    assert cap.last.url.path == "/v1/scrape"
    assert cap.body() == {"url": "https://target.com/article", "formats": ["markdown"]}


async def test_firecrawl_scrape_missing_data_defaults(monkeypatch):
    mock_httpx(monkeypatch, jresp({"data": None}))
    out = await FirecrawlClient("k").scrape("https://t.com")
    assert out == {"url": "https://t.com", "title": "", "markdown": ""}


async def test_firecrawl_scrape_markdown_truncated_to_12000(monkeypatch):
    payload = {"data": {"metadata": {}, "markdown": "m" * 15000}}
    mock_httpx(monkeypatch, jresp(payload))
    out = await FirecrawlClient("k").scrape("https://t.com")
    assert len(out["markdown"]) == 12000


async def test_firecrawl_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({}, status=500))
    with pytest.raises(httpx.HTTPStatusError):
        await FirecrawlClient("k").search("q")


# =========================================================================== #
# Tavily
# =========================================================================== #
def test_tavily_requires_key():
    with pytest.raises(AdapterUnavailable):
        TavilyClient(None)


async def test_tavily_search_news_happy_path(monkeypatch):
    payload = {
        "results": [
            {
                "title": "News A",
                "url": "https://www.site.com/x",
                "published_date": "2026-07-19",
                "content": "the content",
                "score": 0.87,
            }
        ]
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await TavilyClient("tv-key").search_news("query here", days=3, max_results=7)

    assert out == [
        {
            "title": "News A",
            "url": "https://www.site.com/x",
            "source": "site.com",
            "published_at": "2026-07-19",
            "snippet": "the content",
            "relevance": 0.87,
        }
    ]
    req = cap.last
    assert req.method == "POST"
    assert req.url.host == "api.tavily.com"
    assert req.url.path == "/search"
    assert cap.header("authorization") == "Bearer tv-key"
    assert cap.body() == {
        "query": "query here",
        "topic": "news",
        "days": 3,
        "max_results": 7,
        "search_depth": "basic",
    }


async def test_tavily_deep_search_happy_path(monkeypatch):
    payload = {
        "answer": "synthesized answer",
        "results": [
            {"title": "R", "url": "https://u", "raw_content": "raw wins", "content": "fallback"},
            {"title": "R2", "url": "https://u2", "content": "only content"},
        ],
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await TavilyClient("k").deep_search("q", max_results=2)

    assert out["answer"] == "synthesized answer"
    assert out["results"][0]["content"] == "raw wins"  # raw_content preferred
    assert out["results"][1]["content"] == "only content"  # falls back to content
    assert cap.body() == {
        "query": "q",
        "search_depth": "advanced",
        "max_results": 2,
        "include_answer": True,
        "include_raw_content": True,
    }


async def test_tavily_empty_results(monkeypatch):
    mock_httpx(monkeypatch, jresp({}))
    assert await TavilyClient("k").search_news("q") == []
    mock_httpx(monkeypatch, jresp({}))
    assert await TavilyClient("k").deep_search("q") == {"answer": "", "results": []}


async def test_tavily_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({}, status=401))
    with pytest.raises(httpx.HTTPStatusError):
        await TavilyClient("k").search_news("q")


# =========================================================================== #
# Perplexity
# =========================================================================== #
def test_perplexity_requires_key():
    with pytest.raises(AdapterUnavailable):
        PerplexityClient(None)


async def test_perplexity_ask_happy_path(monkeypatch):
    payload = {
        "choices": [{"message": {"content": "the answer"}}],
        "citations": ["https://a", "https://b", 12345],  # non-str filtered out
        "search_results": [
            {"title": "S1", "url": "https://s1", "date": "2026-01-01"},
            "not-a-dict",  # ignored
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await PerplexityClient("pk").ask("prompt text", system="be terse", max_tokens=256)

    assert out["text"] == "the answer"
    assert out["citations"] == ["https://a", "https://b"]
    assert out["search_results"] == [{"title": "S1", "url": "https://s1", "date": "2026-01-01"}]
    assert out["micros"] == 150  # prompt + completion tokens

    req = cap.last
    assert req.method == "POST"
    assert req.url.host == "api.perplexity.ai"
    assert req.url.path == "/chat/completions"
    assert cap.header("authorization") == "Bearer pk"
    body = cap.body()
    assert body["model"] == "sonar"
    assert body["max_tokens"] == 256
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "prompt text"},
    ]


async def test_perplexity_no_system_message(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"choices": [{"message": {"content": "hi"}}]}))
    await PerplexityClient("k").ask("just a prompt")
    assert cap.body()["messages"] == [{"role": "user", "content": "just a prompt"}]


async def test_perplexity_custom_model(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({}))
    await PerplexityClient("k", model="sonar-pro").ask("q")
    assert cap.body()["model"] == "sonar-pro"


async def test_perplexity_malformed_response_degrades(monkeypatch):
    # No choices/citations/usage — must not crash; text "", micros 0.
    mock_httpx(monkeypatch, jresp({}))
    out = await PerplexityClient("k").ask("q")
    assert out == {"text": "", "citations": [], "search_results": [], "micros": 0}


async def test_perplexity_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({}, status=502))
    with pytest.raises(httpx.HTTPStatusError):
        await PerplexityClient("k").ask("q")


# =========================================================================== #
# X (Twitter) API v2
# =========================================================================== #
def test_x_requires_token():
    with pytest.raises(AdapterUnavailable):
        XClient(None)


async def test_x_search_recent_happy_path(monkeypatch):
    payload = {
        "data": [
            {"id": "111", "text": "line one\nline two", "created_at": "2026-07-20T00:00:00Z"},
        ]
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await XClient("bearer-xyz").search_recent("tesla", max_results=5)

    assert out == [
        {
            "title": "line one line two",  # newlines collapsed to spaces
            "url": "https://x.com/i/web/status/111",
            "source": "X",
            "published_at": "2026-07-20T00:00:00Z",
            "snippet": "line one line two",
        }
    ]
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "api.x.com"
    assert req.url.path == "/2/tweets/search/recent"
    assert cap.header("authorization") == "Bearer bearer-xyz"
    # query gets the recency/language operators appended.
    assert cap.param("query") == "tesla -is:retweet -is:reply lang:en"
    # max_results clamped up to the API floor of 10.
    assert cap.param("max_results") == "10"
    assert cap.param("tweet.fields") == "created_at,public_metrics"


async def test_x_max_results_clamped_high(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"data": []}))
    await XClient("t").search_recent("q", max_results=500)
    assert cap.param("max_results") == "100"  # clamped down to API ceiling


async def test_x_empty_data(monkeypatch):
    mock_httpx(monkeypatch, jresp({}))
    assert await XClient("t").search_recent("q") == []


async def test_x_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({"title": "Unauthorized"}, status=403))
    with pytest.raises(httpx.HTTPStatusError):
        await XClient("t").search_recent("q")


# =========================================================================== #
# YouTube Data API v3
# =========================================================================== #
def test_youtube_requires_key():
    with pytest.raises(AdapterUnavailable):
        YouTubeClient(None)


async def test_youtube_search_recent_happy_path(monkeypatch):
    payload = {
        "items": [
            {
                "id": {"videoId": "vid123"},
                "snippet": {
                    "title": "Fast Cars",
                    "channelTitle": "AutoChan",
                    "publishedAt": "2026-07-20T09:00:00Z",
                    "description": "a review",
                },
            },
            {"id": {"videoId": None}, "snippet": {"title": "skip me"}},  # no videoId -> skipped
            {"id": {"videoId": "vid456"}, "snippet": {"title": "No Channel"}},  # channelTitle absent
        ]
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await YouTubeClient("yt-key").search_recent("cars", days=2, max_results=10)

    assert len(out) == 2
    assert out[0] == {
        "title": "Fast Cars",
        "url": "https://www.youtube.com/watch?v=vid123",
        "source": "AutoChan",
        "published_at": "2026-07-20T09:00:00Z",
        "snippet": "a review",
    }
    # channelTitle key missing -> default "YouTube".
    assert out[1]["source"] == "YouTube"
    assert out[1]["url"] == "https://www.youtube.com/watch?v=vid456"

    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "www.googleapis.com"
    assert req.url.path == "/youtube/v3/search"
    assert cap.param("key") == "yt-key"
    assert cap.param("part") == "snippet"
    assert cap.param("type") == "video"
    assert cap.param("order") == "date"
    assert cap.param("q") == "cars"
    assert cap.param("maxResults") == "10"
    # publishedAfter is an RFC-3339 UTC instant.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cap.param("publishedAfter"))


async def test_youtube_empty_items(monkeypatch):
    mock_httpx(monkeypatch, jresp({}))
    assert await YouTubeClient("k").search_recent("q") == []


async def test_youtube_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({"error": {}}, status=403))
    with pytest.raises(httpx.HTTPStatusError):
        await YouTubeClient("k").search_recent("q")


# =========================================================================== #
# SEMrush (CSV / semicolon-delimited response)
# =========================================================================== #
def test_semrush_requires_key():
    with pytest.raises(AdapterUnavailable):
        SemrushClient(None)


async def test_semrush_related_phrases_happy_path(monkeypatch):
    csv = "Ph;Nq;Cp\ntesla model 3;1000;0.55\n;500;0.10\nbmw i4;;0.20"
    cap = mock_httpx(monkeypatch, tresp(csv))
    out = await SemrushClient("sr-key").related_phrases("tesla", database="uk", limit=5)

    # Row 1 parsed; row 2 skipped (blank phrase); row 3 has no volume -> empty snippet.
    assert out == [
        {
            "title": "tesla model 3",
            "url": "https://www.semrush.com/analytics/keywordoverview/?q=tesla+model+3",
            "source": "SEMrush",
            "published_at": "",
            "snippet": "~1000/mo searches",
        },
        {
            "title": "bmw i4",
            "url": "https://www.semrush.com/analytics/keywordoverview/?q=bmw+i4",
            "source": "SEMrush",
            "published_at": "",
            "snippet": "",  # missing volume -> no snippet text
        },
    ]
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "api.semrush.com"
    assert cap.param("type") == "phrase_related"
    assert cap.param("key") == "sr-key"
    assert cap.param("phrase") == "tesla"
    assert cap.param("database") == "uk"
    assert cap.param("export_columns") == "Ph,Nq,Cp"
    assert cap.param("display_limit") == "5"


async def test_semrush_error_response_returns_empty(monkeypatch):
    mock_httpx(monkeypatch, tresp("ERROR 50 :: NOTHING FOUND"))
    assert await SemrushClient("k").related_phrases("x") == []


async def test_semrush_blank_response_returns_empty(monkeypatch):
    mock_httpx(monkeypatch, tresp("   \n  "))
    assert await SemrushClient("k").related_phrases("x") == []


async def test_semrush_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, tresp("boom", status=500))
    with pytest.raises(httpx.HTTPStatusError):
        await SemrushClient("k").related_phrases("x")


# =========================================================================== #
# Similarweb (api_key is a QUERY PARAM, not a header)
# =========================================================================== #
def test_similarweb_requires_key():
    with pytest.raises(AdapterUnavailable):
        SimilarwebClient(None)


async def test_similarweb_available_range_happy_path(monkeypatch):
    payload = {
        "total_traffic_and_engagement": {
            "countries": {"world": {"start_date": "2025-01", "end_date": "2026-06"}}
        }
    }
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await SimilarwebClient("sw-key").available_range("hotcars.com")

    assert out == {"start_date": "2025-01", "end_date": "2026-06"}
    req = cap.last
    assert req.method == "GET"
    assert req.url.host == "api.similarweb.com"
    assert req.url.path == "/v1/website/hotcars.com/total-traffic-and-engagement/describe"
    # The defining quirk: key is a query param, not an Authorization header.
    assert cap.param("api_key") == "sw-key"
    assert cap.param("format") == "json"
    assert cap.header("authorization") is None
    assert cap.header("user-agent") == "valnet-switchboard/1.0"


async def test_similarweb_available_range_missing_country(monkeypatch):
    mock_httpx(monkeypatch, jresp({"total_traffic_and_engagement": {"countries": {}}}))
    assert await SimilarwebClient("k").available_range("d.com", country="fr") == {}
    # entirely empty payload also degrades to {}
    mock_httpx(monkeypatch, jresp({}))
    assert await SimilarwebClient("k").available_range("d.com") == {}


async def test_similarweb_visits_happy_path(monkeypatch):
    payload = {"visits": [{"date": "2026-07-01", "visits": 123.0}]}
    cap = mock_httpx(monkeypatch, jresp(payload))
    out = await SimilarwebClient("k").visits("d.com", "2026-06", "2026-07", country="us")

    assert out == [{"date": "2026-07-01", "visits": 123.0}]
    assert cap.last.url.path == "/v1/website/d.com/total-traffic-and-engagement/visits"
    assert cap.param("start_date") == "2026-06"
    assert cap.param("end_date") == "2026-07"
    assert cap.param("country") == "us"
    assert cap.param("granularity") == "daily"
    assert cap.param("main_domain_only") == "false"


async def test_similarweb_visits_empty(monkeypatch):
    mock_httpx(monkeypatch, jresp({}))
    assert await SimilarwebClient("k").visits("d.com", "2026-06", "2026-07") == []


async def test_similarweb_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({}, status=404))
    with pytest.raises(httpx.HTTPStatusError):
        await SimilarwebClient("k").available_range("d.com")


# =========================================================================== #
# HC Viral Hits (key checked lazily in _headers, X-API-Key header)
# =========================================================================== #
def test_hcviral_init_does_not_require_key():
    # Unlike the other clients, construction with no key is allowed; the guard
    # fires only when a request is attempted.
    client = HCViralClient("https://hc.example.com/", None)
    assert client is not None


async def test_hcviral_missing_key_raises_on_call(monkeypatch):
    mock_httpx(monkeypatch, jresp([]))  # never reached — guard is pre-network
    with pytest.raises(AdapterUnavailable):
        await HCViralClient("https://hc.example.com", None).list_drafts("hotcars")


async def test_hcviral_list_drafts_happy_path(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp([{"id": 1}, {"id": 2}]))
    # trailing slash on base_url is stripped so the path joins cleanly.
    out = await HCViralClient("https://hc.example.com/", "api-key").list_drafts("hotcars")

    assert out == [{"id": 1}, {"id": 2}]
    req = cap.last
    assert req.method == "GET"
    assert str(req.url).startswith("https://hc.example.com/api/cms/drafts")
    assert req.url.path == "/api/cms/drafts"
    assert cap.header("x-api-key") == "api-key"
    assert cap.header("accept") == "application/json"
    assert cap.param("brand") == "hotcars"
    assert cap.param("status") is None  # omitted when not passed


async def test_hcviral_list_drafts_with_status_and_dict_envelope(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"drafts": [{"id": 9}]}))
    out = await HCViralClient("https://hc.example.com", "k").list_drafts("hotcars", status="ready")
    assert out == [{"id": 9}]  # unwrapped from {"drafts": [...]}
    assert cap.param("status") == "ready"


async def test_hcviral_list_drafts_dict_without_drafts_key(monkeypatch):
    mock_httpx(monkeypatch, jresp({"unexpected": True}))
    assert await HCViralClient("https://h", "k").list_drafts("b") == []


async def test_hcviral_list_topics_variants(monkeypatch):
    # list passthrough
    mock_httpx(monkeypatch, jresp([{"t": 1}]))
    assert await HCViralClient("https://h", "k").list_topics("b") == [{"t": 1}]
    # dict with "topics"
    cap = mock_httpx(monkeypatch, jresp({"topics": [{"t": 2}]}))
    assert await HCViralClient("https://h", "k").list_topics("b") == [{"t": 2}]
    assert cap.last.url.path == "/api/cms/topics"
    # dict without "topics" falls back to "drafts"
    mock_httpx(monkeypatch, jresp({"drafts": [{"t": 3}]}))
    assert await HCViralClient("https://h", "k").list_topics("b") == [{"t": 3}]


async def test_hcviral_non_200_raises(monkeypatch):
    mock_httpx(monkeypatch, jresp({}, status=404))
    with pytest.raises(httpx.HTTPStatusError):
        await HCViralClient("https://h", "k").list_topics("b")


# =========================================================================== #
# Shared _http helper (get_json / post_json): retry + backoff + non-JSON
# =========================================================================== #
def _no_sleep(monkeypatch):
    """Neutralize the backoff sleep so retry tests don't actually wait."""
    async def _nap(*_a, **_k):
        return None

    monkeypatch.setattr(_http, "asyncio", types.SimpleNamespace(sleep=_nap))


async def test_http_get_json_happy_path(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"ok": True}))
    out = await _http.get_json("https://api.host/", "/path", headers={"H": "v"}, params={"a": "b"})
    assert out == {"ok": True}
    req = cap.last
    assert req.method == "GET"
    # base trailing slash trimmed, path appended once.
    assert str(req.url).startswith("https://api.host/path")
    assert cap.header("H") == "v"
    assert cap.param("a") == "b"


async def test_http_post_json_happy_path(monkeypatch):
    cap = mock_httpx(monkeypatch, jresp({"created": 1}))
    out = await _http.post_json("https://api.host", "/make", json={"n": 5})
    assert out == {"created": 1}
    assert cap.last.method == "POST"
    assert cap.body() == {"n": 5}


async def test_http_non_json_response_falls_back(monkeypatch):
    mock_httpx(monkeypatch, tresp("<html>not json</html>"))
    out = await _http.get_json("https://api.host", "/x")
    assert out == {"status_code": 200, "text": "<html>not json</html>"}


async def test_http_retries_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, [jresp({}, status=503), jresp({"ok": 1})])
    out = await _http.get_json("https://api.host", "/x")
    assert out == {"ok": 1}
    assert cap.count == 2  # one retry after the 503


async def test_http_retries_exhausted_raises(monkeypatch):
    _no_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, jresp({}, status=500))
    with pytest.raises(httpx.HTTPStatusError):
        await _http.get_json("https://api.host", "/x")
    assert cap.count == 3  # _MAX_ATTEMPTS


async def test_http_non_retryable_status_raises_immediately(monkeypatch):
    _no_sleep(monkeypatch)
    cap = mock_httpx(monkeypatch, jresp({}, status=404))
    with pytest.raises(httpx.HTTPStatusError):
        await _http.get_json("https://api.host", "/x")
    assert cap.count == 1  # 404 is not in the retry set — no retry
