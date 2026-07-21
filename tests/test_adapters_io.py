"""I/O adapter tests: images / research / trend_sources (PRD §6.2, §6.6).

Every network + S3 seam is mocked at the boundary the source actually uses:

* ``httpx.AsyncClient`` — all three modules import ``httpx`` lazily inside the
  call and use ``httpx.AsyncClient(...)``; we monkeypatch that attribute so the
  real client/adapter code runs against canned responses.
* ``boto3`` — ``images._s3_media`` does ``import boto3`` then ``boto3.client``;
  boto3 is NOT installed in this env, so we inject a fake module into
  ``sys.modules`` (covers the full listing path without requiring the SDK) and,
  separately, force the ImportError branch with ``sys.modules['boto3'] = None``.
* ``feedparser`` — ``research.CompetitorNewsAdapter`` does ``import feedparser``
  then ``feedparser.parse``; also not installed, so we inject a fake module.

An autouse fixture points ``httpx.AsyncClient`` at a blocker so any un-mocked
path fails loudly instead of touching the network.  asyncio_mode="auto" (see
pyproject) means async tests need no decorator.
"""

from __future__ import annotations

import sys
import types

import pytest

from switchboard.adapters import images
from switchboard.adapters.base import AdapterUnavailable
from switchboard.adapters.research import _COMPETITOR_FEEDS, CompetitorNewsAdapter, SimilarwebAdapter
from switchboard.adapters.trend_sources import (
    _MAX_ITEMS,
    _SIGNAL_TTL,
    FirecrawlTrendAdapter,
    NewsApiTrendAdapter,
    PerplexityTrendAdapter,
    SemrushTrendAdapter,
    TavilyTrendAdapter,
    XTrendAdapter,
    YouTubeTrendAdapter,
    _norm,
    _signals_draft,
)
from switchboard.db.enums import PORTFOLIO, EntryType
from switchboard.interfaces import CostSpec

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeCreds:
    """Duck-typed stand-in for ``switchboard.credentials.Credentials``: a dict
    behind ``.resolve`` plus the typed accessors the adapters call."""

    def __init__(self, **values):
        self._v = values

    def resolve(self, key, *, required=False, secret=True):
        val = self._v.get(key)
        if (val is None or val == "") and required:
            raise RuntimeError(f"missing {key}")
        return val if val not in (None, "") else None

    def similarweb_key(self):
        return self.resolve("SIMILARWEB_API_KEY")

    def tavily_key(self):
        return self.resolve("TAVILY_API_KEY")

    def newsapi_key(self):
        return self.resolve("NEWSAPI_API_KEY")

    def firecrawl_key(self):
        return self.resolve("FIRECRAWL_API_KEY")

    def perplexity_key(self):
        return self.resolve("PERPLEXITY_API_KEY")

    def semrush_key(self):
        return self.resolve("SEMRUSH_API_KEY")

    def x_bearer(self):
        return self.resolve("X_BEARER_TOKEN")

    def youtube_key(self):
        return self.resolve("YOUTUBE_API_KEY")


class FakeTrends:
    def __init__(self, base_query="automotive industry news", watchlist=()):
        self.base_query = base_query
        self.watchlist = tuple(watchlist)


class FakeBrand:
    def __init__(self, domain):
        self.domain = domain


class FakeSettings:
    def __init__(self, trends=None, brands=None):
        self.trends = trends or FakeTrends()
        self._brands = brands or {}

    def brand(self, key):
        return self._brands[key]


class FakeCtx:
    def __init__(self, creds, settings=None):
        self.creds = creds
        self.settings = settings or FakeSettings()


class FakeResp:
    def __init__(self, *, json_data=None, text="", status_code=200, error=None):
        self._json = json_data
        self.text = text
        self.status_code = status_code
        self._error = error

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


def _status_error(code=500):
    import httpx

    request = httpx.Request("GET", "https://blocked.test")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request,
                                 response=httpx.Response(code, request=request))


def install_httpx(monkeypatch, handler):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``handler(method,
    url, init_kwargs, req_kwargs) -> FakeResp``.  Returns a list of recorded
    calls (each a dict with method/url/init + the get/post kwargs)."""
    import httpx

    calls: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            self.init = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            calls.append({"method": "GET", "url": url, "init": self.init, **kw})
            return handler("GET", url, self.init, kw)

        async def post(self, url, **kw):
            calls.append({"method": "POST", "url": url, "init": self.init, **kw})
            return handler("POST", url, self.init, kw)

        async def request(self, method, url, **kw):
            calls.append({"method": method, "url": url, "init": self.init, **kw})
            return handler(method, url, self.init, kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


def route(*routes, default=None):
    """Build a handler dispatching on a URL substring. Each route is
    (substring, FakeResp | callable(req_kwargs) -> FakeResp)."""

    def handler(method, url, init, kw):
        for sub, resp in routes:
            if sub in url:
                return resp(kw) if callable(resp) else resp
        if default is not None:
            return default
        raise AssertionError(f"unexpected {method} {url}")

    return handler


def call_with(calls, sub):
    for c in calls:
        if sub in c["url"]:
            return c
    raise AssertionError(f"no call to {sub!r}; saw {[c['url'] for c in calls]}")


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Any httpx use not explicitly mocked by a test raises rather than dialing
    out. Tests that need HTTP call ``install_httpx`` which re-patches this."""
    import httpx

    class _Blocked:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("real network blocked in tests")

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

        async def post(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

        async def request(self, *a, **k):
            raise RuntimeError("real network blocked in tests")

    monkeypatch.setattr(httpx, "AsyncClient", _Blocked)


# ===========================================================================
# images.py — Unsplash
# ===========================================================================


def _unsplash_body():
    return {"results": [
        {"id": "a", "urls": {"small": "s1", "regular": "r1"},
         "user": {"name": "Alice", "links": {"html": "https://u/alice"}},
         "width": 800, "height": 600},
        {"id": "b", "urls": {"thumb": "t2", "full": "f2"},  # falls back to thumb/full
         "user": {}, },                                      # name -> Unknown, credit_url None
        {"id": "c", "urls": {}, "user": {"name": "Nobody"}},  # no usable urls -> dropped
    ]}


async def test_unsplash_maps_credits_and_drops_urlless(monkeypatch):
    calls = install_httpx(monkeypatch, route(("unsplash", FakeResp(json_data=_unsplash_body()))))
    out = await images._unsplash("ev trucks", key="UKEY", limit=6, page=2)

    assert [c["id"] for c in out] == ["unsplash:a", "unsplash:b"]  # 'c' filtered out
    assert all(c["source"] == "unsplash" for c in out)
    a, b = out
    assert (a["thumb_url"], a["full_url"]) == ("s1", "r1")
    assert a["credit"] == "Alice / Unsplash" and a["credit_url"] == "https://u/alice"
    assert (a["width"], a["height"]) == (800, 600)
    assert (b["thumb_url"], b["full_url"]) == ("t2", "f2")  # small/regular missing -> fallback
    assert b["credit"] == "Unknown / Unsplash" and b["credit_url"] is None
    assert b["width"] is None

    call = call_with(calls, "unsplash")
    assert call["url"] == "https://api.unsplash.com/search/photos"
    assert call["params"] == {"query": "ev trucks", "per_page": 6, "page": 2, "orientation": "landscape"}
    assert call["init"]["headers"]["Authorization"] == "Client-ID UKEY"


async def test_unsplash_raises_on_http_error(monkeypatch):
    install_httpx(monkeypatch, route(("unsplash", FakeResp(error=_status_error(401)))))
    with pytest.raises(Exception):
        await images._unsplash("q", key="K", limit=3, page=1)


# ===========================================================================
# images.py — Pexels
# ===========================================================================


def _pexels_body():
    return {"photos": [
        {"id": 1, "src": {"medium": "m1", "large": "l1"}, "photographer": "Bob",
         "photographer_url": "https://p/bob", "url": "https://px/1", "width": 1000, "height": 700},
        {"id": 2, "src": {"small": "s2", "original": "o2"},  # medium/large missing -> fallback
         "url": "https://px/2"},                              # photographer missing -> Unknown
        {"id": 3, "src": {"medium": "m3", "large": "l3"}, "photographer": "Cyd"},  # sliced away
    ]}


async def test_pexels_maps_and_slices_before_filter(monkeypatch):
    calls = install_httpx(monkeypatch, route(("pexels", FakeResp(json_data=_pexels_body()))))
    out = await images._pexels("suv", key="PKEY", limit=2, page=1)  # limit=2 -> only first two

    assert [c["id"] for c in out] == ["pexels:1", "pexels:2"]
    p1, p2 = out
    assert (p1["thumb_url"], p1["full_url"]) == ("m1", "l1")
    assert p1["credit"] == "Bob / Pexels" and p1["credit_url"] == "https://p/bob"
    assert (p2["thumb_url"], p2["full_url"]) == ("s2", "o2")  # fallback ordering
    assert p2["credit"] == "Unknown / Pexels"
    assert p2["credit_url"] == "https://px/2"  # photographer_url missing -> url fallback

    call = call_with(calls, "pexels")
    assert call["url"] == "https://api.pexels.com/v1/search"
    assert call["params"]["per_page"] == 2
    assert call["init"]["headers"]["Authorization"] == "PKEY"  # raw key, no scheme


# ===========================================================================
# images.py — _s3_media (boto3)
# ===========================================================================


class FakeS3:
    def __init__(self, contents, presigned="https://signed.example/x", raise_on_list=False):
        self._contents = contents
        self._presigned = presigned
        self._raise = raise_on_list
        self.calls: list = []

    def list_objects_v2(self, **kw):
        self.calls.append(("list", kw))
        if self._raise:
            raise RuntimeError("s3 list boom")
        return {"Contents": self._contents}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        self.calls.append(("presign", op, Params, ExpiresIn))
        return self._presigned


def _install_boto3(monkeypatch, s3):
    mod = types.SimpleNamespace(client_calls=[])

    def client(service, **kw):
        mod.client_calls.append((service, kw))
        return s3

    mod.client = client
    monkeypatch.setitem(sys.modules, "boto3", mod)
    return mod


def test_s3_media_unconfigured_returns_empty():
    # no prefix / bucket -> bail before importing boto3
    assert images._s3_media(FakeCreds(), 6) == []
    assert images._s3_media(FakeCreds(IMAGE_LIBRARY_PREFIX="brand/"), 6) == []  # bucket missing
    assert images._s3_media(FakeCreds(S3_BUCKET_NAME="b"), 6) == []             # prefix missing


def test_s3_media_boto3_missing_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # -> ImportError branch
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket")
    assert images._s3_media(creds, 6) == []


def test_s3_media_cdn_urls_and_extension_filter(monkeypatch):
    s3 = FakeS3(contents=[
        {"Key": "brand/a.jpg"},
        {"Key": "brand/readme.txt"},   # non-image -> skipped
        {"Key": "brand/b.PNG"},        # case-insensitive ext match
        {"Key": "brand/c.webp"},
    ])
    mod = _install_boto3(monkeypatch, s3)
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket",
                      IMAGE_CDN_BASE="https://cdn.example/",  # trailing slash stripped
                      S3_ACCESS_KEY_ID="ak", S3_SECRET_ACCESS_KEY="sk")
    out = images._s3_media(creds, 6)

    assert [o["id"] for o in out] == ["s3:brand/a.jpg", "s3:brand/b.PNG", "s3:brand/c.webp"]
    assert out[0]["thumb_url"] == out[0]["full_url"] == "https://cdn.example/brand/a.jpg"
    assert out[0]["source"] == "media-library"
    assert out[0]["credit"] == "Valnet media library" and out[0]["credit_url"] is None
    # credentials threaded into boto3.client(...)
    assert mod.client_calls[0][0] == "s3"
    assert mod.client_calls[0][1]["aws_access_key_id"] == "ak"
    # MaxKeys = max(limit*2, 10)
    assert s3.calls[0] == ("list", {"Bucket": "bucket", "Prefix": "brand/", "MaxKeys": 12})
    # CDN configured -> presign never called
    assert all(c[0] != "presign" for c in s3.calls)


def test_s3_media_presigned_when_no_cdn(monkeypatch):
    s3 = FakeS3(contents=[{"Key": "brand/a.jpg"}], presigned="https://signed/a")
    _install_boto3(monkeypatch, s3)
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket")  # no IMAGE_CDN_BASE
    out = images._s3_media(creds, 6)

    assert out[0]["thumb_url"] == out[0]["full_url"] == "https://signed/a"
    presign = [c for c in s3.calls if c[0] == "presign"][0]
    assert presign[2] == {"Bucket": "bucket", "Key": "brand/a.jpg"} and presign[3] == 3600


def test_s3_media_respects_limit(monkeypatch):
    s3 = FakeS3(contents=[{"Key": f"brand/{i}.jpg"} for i in range(10)])
    _install_boto3(monkeypatch, s3)
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket",
                      IMAGE_CDN_BASE="https://cdn/")
    out = images._s3_media(creds, 3)
    assert len(out) == 3  # breaks once limit reached


def test_s3_media_listing_exception_returns_empty(monkeypatch):
    s3 = FakeS3(contents=[], raise_on_list=True)
    _install_boto3(monkeypatch, s3)
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket",
                      IMAGE_CDN_BASE="https://cdn/")
    assert images._s3_media(creds, 6) == []  # exception swallowed -> []


# ===========================================================================
# images.py — image_candidates orchestrator
# ===========================================================================


async def test_image_candidates_all_disabled():
    res = await images.image_candidates(FakeCreds(), "ev news", per_source=4, page=1)
    assert res["candidates"] == []
    assert res["sources"] == {"media-library": "unconfigured",
                              "unsplash": "unconfigured", "pexels": "unconfigured"}
    assert res["query"] == "ev news" and res["page"] == 1


@pytest.mark.parametrize("given,expected", [(None, "automotive"), ("   ", "automotive"), ("  cars ", "cars")])
async def test_image_candidates_query_defaulting(given, expected):
    res = await images.image_candidates(FakeCreds(), given)
    assert res["query"] == expected


@pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (None, 1), (3, 3)])
async def test_image_candidates_page_clamp(given, expected):
    res = await images.image_candidates(FakeCreds(), "cars", page=given)
    assert res["page"] == expected


async def test_image_candidates_merges_unsplash_and_pexels(monkeypatch):
    handler = route(
        ("unsplash", FakeResp(json_data=_unsplash_body())),
        ("pexels", FakeResp(json_data=_pexels_body())),
    )
    calls = install_httpx(monkeypatch, handler)
    creds = FakeCreds(UNSPLASH_ACCESS_KEY="UK", PEXELS_API_KEY="PK")
    res = await images.image_candidates(creds, "trucks", per_source=6, page=4)

    assert res["sources"] == {"media-library": "unconfigured", "unsplash": "on", "pexels": "on"}
    srcs = [c["source"] for c in res["candidates"]]
    assert "unsplash" in srcs and "pexels" in srcs
    # page threads through to both stock sources
    assert call_with(calls, "unsplash")["params"]["page"] == 4
    assert call_with(calls, "pexels")["params"]["page"] == 4


async def test_image_candidates_unsplash_error_soft_fails(monkeypatch):
    handler = route(
        ("unsplash", FakeResp(error=_status_error(429))),
        ("pexels", FakeResp(json_data=_pexels_body())),
    )
    install_httpx(monkeypatch, handler)
    creds = FakeCreds(UNSPLASH_ACCESS_KEY="UK", PEXELS_API_KEY="PK")
    res = await images.image_candidates(creds, "trucks", per_source=6)

    assert res["sources"]["unsplash"] == "error"
    assert res["sources"]["pexels"] == "on"
    assert {c["source"] for c in res["candidates"]} == {"pexels"}  # only pexels survived


async def test_image_candidates_media_library_on_and_merged(monkeypatch):
    s3 = FakeS3(contents=[{"Key": "brand/a.jpg"}])
    _install_boto3(monkeypatch, s3)
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/", S3_BUCKET_NAME="bucket",
                      IMAGE_CDN_BASE="https://cdn/")
    res = await images.image_candidates(creds, "cars")
    assert res["sources"]["media-library"] == "on"
    assert [c["source"] for c in res["candidates"]] == ["media-library"]


async def test_image_candidates_media_library_on_even_without_bucket():
    # SURPRISING: 'on' keys off the prefix alone; with no bucket _s3_media -> []
    creds = FakeCreds(IMAGE_LIBRARY_PREFIX="brand/")  # bucket absent
    res = await images.image_candidates(creds, "cars")
    assert res["sources"]["media-library"] == "on"
    assert res["candidates"] == []


# ===========================================================================
# research.py — SimilarwebAdapter
# ===========================================================================


def _similarweb_adapter(key="SW", domain="hotcars.com"):
    settings = FakeSettings(brands={"hotcars": FakeBrand(domain)})
    return SimilarwebAdapter(FakeCtx(FakeCreds(SIMILARWEB_API_KEY=key), settings))


async def test_similarweb_portfolio_unavailable():
    with pytest.raises(AdapterUnavailable):
        await _similarweb_adapter()._observe("portfolio")


async def test_similarweb_missing_key_unavailable():
    with pytest.raises(AdapterUnavailable):
        await _similarweb_adapter(key=None)._observe("hotcars")


async def test_similarweb_observe_builds_metric(monkeypatch):
    describe = {"total_traffic_and_engagement": {"countries": {
        "world": {"end_date": "2026-06-30", "fresh_data": "2026-06-30"}}}}
    visits = {"visits": [{"date": f"2026-06-{d:02d}", "visits": d * 100} for d in range(1, 11)]}
    handler = route(
        ("/describe", FakeResp(json_data=describe)),
        ("/visits", FakeResp(json_data=visits)),
    )
    calls = install_httpx(monkeypatch, handler)

    drafts, cost = await _similarweb_adapter()._observe("hotcars")
    assert len(drafts) == 1
    d = drafts[0]
    assert d.type == EntryType.METRIC and d.brand == "hotcars"
    assert d.source_agent == "research" and d.source_system == "similarweb"
    assert d.confidence == 0.7
    assert d.payload["kind"] == "similarweb_range" and d.payload["domain"] == "hotcars.com"
    assert d.payload["end_month"] == "2026-06" and d.payload["fresh_data"] == "2026-06-30"
    assert len(d.payload["recent_daily_visits"]) == 7  # visits[-7:]
    assert d.payload["recent_daily_visits"][0]["date"] == "2026-06-04"
    assert cost == CostSpec()
    # key is a query param, not a header (per client docstring)
    assert call_with(calls, "/describe")["params"]["api_key"] == "SW"
    assert call_with(calls, "/visits")["params"]["granularity"] == "daily"


async def test_similarweb_no_end_month_skips_visits(monkeypatch):
    describe = {"total_traffic_and_engagement": {"countries": {}}}  # world absent -> {}
    calls = install_httpx(monkeypatch, route(("/describe", FakeResp(json_data=describe)),
                                             ("/visits", FakeResp(json_data={"visits": []}))))
    drafts, _ = await _similarweb_adapter()._observe("hotcars")
    assert "recent_daily_visits" not in drafts[0].payload
    assert drafts[0].payload["end_month"] == ""
    assert all("/visits" not in c["url"] for c in calls)  # visits never fetched


# ===========================================================================
# research.py — CompetitorNewsAdapter (feedparser)
# ===========================================================================


def _entry(**kw):
    return types.SimpleNamespace(**kw)


def _install_feedparser(monkeypatch, *, entries=None, per_url=None, raise_for=()):
    mod = types.ModuleType("feedparser")

    def parse(url):
        if url in raise_for:
            raise RuntimeError("feed boom")
        ents = (per_url or {}).get(url, entries or [])
        return types.SimpleNamespace(entries=ents)

    mod.parse = parse
    monkeypatch.setitem(sys.modules, "feedparser", mod)
    return mod


def _news_adapter():
    return CompetitorNewsAdapter(FakeCtx(FakeCreds()))


async def test_competitor_news_feedparser_missing_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "feedparser", None)  # -> ImportError -> AdapterUnavailable
    with pytest.raises(AdapterUnavailable):
        await _news_adapter()._observe(PORTFOLIO)


async def test_competitor_news_aggregates_and_truncates(monkeypatch):
    # 20 entries/feed -> per-feed [:15] -> 5 feeds * 15 = 75; payload items[:60]
    entries = [_entry(title=f"t{i}", link=f"http://l/{i}", published="2026-07-20") for i in range(20)]
    _install_feedparser(monkeypatch, entries=entries)

    drafts, cost = await _news_adapter()._observe(PORTFOLIO)
    d = drafts[0]
    assert d.type == EntryType.CONTEXT and d.brand == "portfolio"
    assert d.source_system == "rss" and d.payload["kind"] == "competitor_coverage"
    assert d.ttl_seconds == 24 * 3600
    assert d.payload["item_count"] == 75          # counts before payload truncation
    assert len(d.payload["items"]) == 60          # items[:60]
    assert {it["source"] for it in d.payload["items"]} <= set(_COMPETITOR_FEEDS)
    assert isinstance(d.payload["collected_at"], str) and "T" in d.payload["collected_at"]
    assert cost == CostSpec()


async def test_competitor_news_missing_attrs_default_blank(monkeypatch):
    _install_feedparser(monkeypatch, entries=[_entry(title="only-title")])  # no link/published
    drafts, _ = await _news_adapter()._observe(PORTFOLIO)
    it = drafts[0].payload["items"][0]
    assert it["title"] == "only-title" and it["link"] == "" and it["published"] == ""


async def test_competitor_news_feed_failure_isolated(monkeypatch):
    bad = _COMPETITOR_FEEDS["The Drive"]
    _install_feedparser(monkeypatch, entries=[_entry(title="t", link="l", published="p")],
                        raise_for={bad})
    drafts, _ = await _news_adapter()._observe(PORTFOLIO)
    # 4 healthy feeds * 1 entry = 4; the failing feed contributes nothing, no crash
    assert drafts[0].payload["item_count"] == 4
    assert "The Drive" not in {it["source"] for it in drafts[0].payload["items"]}


# ===========================================================================
# trend_sources.py — pure helpers
# ===========================================================================


def test_norm_maps_and_falls_back():
    full = _norm("tavily", {"source": "s", "title": "t", "url": "u",
                            "published_at": "2026", "snippet": "sn"})
    assert full == {"origin": "tavily", "source": "s", "title": "t", "url": "u",
                    "published_at": "2026", "snippet": "sn"}
    # published_at falls back to 'date'; everything else defaults to ""
    fb = _norm("x", {"date": "2025-01-01"})
    assert fb["published_at"] == "2025-01-01"
    assert fb["source"] == fb["title"] == fb["url"] == fb["snippet"] == ""


def test_signals_draft_shape_and_truncation():
    items = [{"url": f"u{i}"} for i in range(_MAX_ITEMS + 10)]
    d = _signals_draft("tavily", items)
    assert d.type == EntryType.CONTEXT and d.brand == PORTFOLIO
    assert d.source_agent == "research" and d.source_system == "tavily"
    assert d.ttl_seconds == _SIGNAL_TTL
    assert d.payload["kind"] == "trend_signals" and d.payload["origin"] == "tavily"
    assert d.payload["item_count"] == _MAX_ITEMS + 10   # count before truncation
    assert len(d.payload["items"]) == _MAX_ITEMS        # items[:_MAX_ITEMS]


# ===========================================================================
# trend_sources.py — _query building + portfolio scoping (via Tavily)
# ===========================================================================


def _trend_ctx(trends=None, **keys):
    return FakeCtx(FakeCreds(**keys), FakeSettings(trends=trends))


def test_query_no_watchlist_is_base_query():
    adp = TavilyTrendAdapter(_trend_ctx(FakeTrends(base_query="auto news", watchlist=())))
    assert adp._query() == "auto news"


def test_query_appends_first_six_watchlist_terms():
    adp = TavilyTrendAdapter(_trend_ctx(
        FakeTrends(base_query="auto news", watchlist=[f"w{i}" for i in range(8)])))
    assert adp._query() == "auto news w0 OR w1 OR w2 OR w3 OR w4 OR w5"  # only first 6


async def test_trend_source_non_portfolio_returns_empty(monkeypatch):
    # brand != portfolio short-circuits before any client/network
    adp = TavilyTrendAdapter(_trend_ctx(TAVILY_API_KEY="TK"))
    drafts, cost = await adp._observe("hotcars")
    assert drafts == [] and cost == CostSpec()


async def test_trend_source_missing_key_unavailable():
    adp = TavilyTrendAdapter(_trend_ctx())  # no TAVILY_API_KEY
    with pytest.raises(AdapterUnavailable):
        await adp._observe(PORTFOLIO)


# ===========================================================================
# trend_sources.py — per-provider _pull / _observe
# ===========================================================================


async def test_tavily_observe_builds_signals(monkeypatch):
    body = {"results": [
        {"title": "EV recall", "url": "https://www.example.com/a", "content": "c" * 500,
         "published_date": "2026-07-20", "score": 0.9},
    ]}
    calls = install_httpx(monkeypatch, route(("api.tavily.com", FakeResp(json_data=body))))
    adp = TavilyTrendAdapter(_trend_ctx(FakeTrends(base_query="auto news", watchlist=("ev",)),
                                        TAVILY_API_KEY="TK"))
    drafts, cost = await adp._observe(PORTFOLIO)

    d = drafts[0]
    assert d.source_system == "tavily" and d.payload["origin"] == "tavily"
    item = d.payload["items"][0]
    assert item["title"] == "EV recall" and item["source"] == "example.com"  # _domain()
    assert item["published_at"] == "2026-07-20" and len(item["snippet"]) == 400  # content[:400]
    assert cost == CostSpec()
    call = call_with(calls, "tavily")
    assert call["json"] == {"query": "auto news ev", "topic": "news", "days": 2,
                            "max_results": 15, "search_depth": "basic"}
    assert call["headers"]["Authorization"] == "Bearer TK"


async def test_tavily_empty_results_yields_no_draft(monkeypatch):
    install_httpx(monkeypatch, route(("api.tavily.com", FakeResp(json_data={"results": []}))))
    adp = TavilyTrendAdapter(_trend_ctx(TAVILY_API_KEY="TK"))
    drafts, cost = await adp._observe(PORTFOLIO)
    assert drafts == [] and cost == CostSpec()


async def test_newsapi_pull_maps(monkeypatch):
    body = {"articles": [
        {"title": "N", "url": "https://n.com/a", "source": {"name": "NN"},
         "publishedAt": "2026-07-20T00:00:00Z", "description": "d"},
    ]}
    calls = install_httpx(monkeypatch, route(("newsapi.org", FakeResp(json_data=body))))
    adp = NewsApiTrendAdapter(_trend_ctx(NEWSAPI_API_KEY="NK"))
    drafts, _ = await adp._observe(PORTFOLIO)

    item = drafts[0].payload["items"][0]
    assert drafts[0].source_system == "newsapi"
    assert item["source"] == "NN" and item["url"] == "https://n.com/a"
    assert item["published_at"] == "2026-07-20T00:00:00Z"
    call = call_with(calls, "newsapi.org")
    assert call["headers"]["X-Api-Key"] == "NK"
    assert call["params"]["pageSize"] == 25 and call["params"]["sortBy"] == "publishedAt"


async def test_firecrawl_pull_maps(monkeypatch):
    body = {"data": [{"title": "F", "url": "https://www.foo.com/a", "description": "d"}]}
    calls = install_httpx(monkeypatch, route(("api.firecrawl.dev", FakeResp(json_data=body))))
    adp = FirecrawlTrendAdapter(_trend_ctx(FakeTrends(base_query="auto news"), FIRECRAWL_API_KEY="FK"))
    drafts, _ = await adp._observe(PORTFOLIO)

    item = drafts[0].payload["items"][0]
    assert drafts[0].source_system == "firecrawl"
    assert item["source"] == "foo.com" and item["snippet"] == "d"
    call = call_with(calls, "firecrawl")
    assert call["json"] == {"query": "auto news this week", "limit": 10}


async def test_youtube_pull_maps_and_skips_missing_video_id(monkeypatch):
    body = {"items": [
        {"id": {"videoId": "abc"}, "snippet": {"title": "V", "channelTitle": "Chan",
                                               "publishedAt": "2026-07-20", "description": "d"}},
        {"id": {}, "snippet": {"title": "no id"}},  # no videoId -> skipped
    ]}
    install_httpx(monkeypatch, route(("googleapis.com/youtube", FakeResp(json_data=body))))
    adp = YouTubeTrendAdapter(_trend_ctx(YOUTUBE_API_KEY="YK"))
    drafts, _ = await adp._observe(PORTFOLIO)

    items = drafts[0].payload["items"]
    assert len(items) == 1 and drafts[0].source_system == "youtube"
    assert items[0]["url"] == "https://www.youtube.com/watch?v=abc"
    assert items[0]["source"] == "Chan"


async def test_x_pull_maps(monkeypatch):
    body = {"data": [{"id": "111", "text": "Big EV news\nsecond line", "created_at": "2026-07-20"}]}
    install_httpx(monkeypatch, route(("api.x.com", FakeResp(json_data=body))))
    adp = XTrendAdapter(_trend_ctx(X_BEARER_TOKEN="XT"))
    drafts, _ = await adp._observe(PORTFOLIO)

    item = drafts[0].payload["items"][0]
    assert drafts[0].source_system == "x"
    assert item["url"] == "https://x.com/i/web/status/111"
    assert item["title"] == "Big EV news second line"  # newline collapsed


async def test_semrush_pull_parses_csv(monkeypatch):
    csv = "Ph;Nq;Cp\nelectric cars;12000;1.5\nhybrid suv;8000;0.9\n;500;0.1"  # last row: no phrase
    calls = install_httpx(monkeypatch, route(("api.semrush.com", FakeResp(text=csv))))
    adp = SemrushTrendAdapter(_trend_ctx(FakeTrends(base_query="ev"), SEMRUSH_API_KEY="SK"))
    drafts, _ = await adp._observe(PORTFOLIO)

    items = drafts[0].payload["items"]
    assert [i["title"] for i in items] == ["electric cars", "hybrid suv"]  # empty-phrase row dropped
    assert items[0]["snippet"] == "~12000/mo searches" and items[0]["source"] == "SEMrush"
    call = call_with(calls, "semrush")
    assert call["params"]["phrase"] == "ev" and call["params"]["type"] == "phrase_related"


@pytest.mark.parametrize("text", ["", "ERROR 50 :: NOTHING FOUND"])
async def test_semrush_empty_or_error_yields_no_draft(monkeypatch, text):
    install_httpx(monkeypatch, route(("api.semrush.com", FakeResp(text=text))))
    adp = SemrushTrendAdapter(_trend_ctx(SEMRUSH_API_KEY="SK"))
    drafts, cost = await adp._observe(PORTFOLIO)
    assert drafts == [] and cost == CostSpec()


# ===========================================================================
# trend_sources.py — PerplexityTrendAdapter (own _observe override + cost)
# ===========================================================================


def _perplexity_body(*, search_results=None, citations=None, prompt=100, completion=50):
    return {
        "choices": [{"message": {"content": "wire copy"}}],
        "search_results": search_results or [],
        "citations": citations or [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


async def test_perplexity_non_portfolio_empty():
    adp = PerplexityTrendAdapter(_trend_ctx(PERPLEXITY_API_KEY="PK"))
    drafts, cost = await adp._observe("hotcars")
    assert drafts == [] and cost == CostSpec()


async def test_perplexity_search_results_and_cost(monkeypatch):
    body = _perplexity_body(
        search_results=[{"title": "S1", "url": "https://s/1", "date": "2026-07-20"}],
        prompt=100, completion=50)
    calls = install_httpx(monkeypatch, route(("api.perplexity.ai", FakeResp(json_data=body))))
    adp = PerplexityTrendAdapter(_trend_ctx(PERPLEXITY_API_KEY="PK"))
    drafts, cost = await adp._observe(PORTFOLIO)

    d = drafts[0]
    assert d.source_system == "perplexity"
    item = d.payload["items"][0]
    assert item["url"] == "https://s/1" and item["published_at"] == "2026-07-20"
    assert cost.llm_micros == 150  # prompt + completion tokens -> micros
    assert call_with(calls, "perplexity")["headers"]["Authorization"] == "Bearer PK"


async def test_perplexity_citation_fallback(monkeypatch):
    body = _perplexity_body(search_results=[], citations=["https://a", "https://b"])
    install_httpx(monkeypatch, route(("api.perplexity.ai", FakeResp(json_data=body))))
    adp = PerplexityTrendAdapter(_trend_ctx(PERPLEXITY_API_KEY="PK"))
    drafts, _ = await adp._observe(PORTFOLIO)

    urls = [i["url"] for i in drafts[0].payload["items"]]
    assert urls == ["https://a", "https://b"]  # fell back to bare citations


async def test_perplexity_empty_returns_cost_without_draft(monkeypatch):
    body = _perplexity_body(search_results=[], citations=[], prompt=10, completion=5)
    install_httpx(monkeypatch, route(("api.perplexity.ai", FakeResp(json_data=body))))
    adp = PerplexityTrendAdapter(_trend_ctx(PERPLEXITY_API_KEY="PK"))
    drafts, cost = await adp._observe(PORTFOLIO)
    assert drafts == [] and cost.llm_micros == 15  # metered even with no items


async def test_perplexity_missing_key_unavailable():
    adp = PerplexityTrendAdapter(_trend_ctx())
    with pytest.raises(AdapterUnavailable):
        await adp._observe(PORTFOLIO)
