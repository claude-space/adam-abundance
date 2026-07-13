"""Paid-Media read adapters (PRD §6.7, §8). Google/Meta/Bing Ads spend, Sentinel
conversion events, and lead feeds — all **read-only**. None define ``_act``, so
they are structurally incapable of changing a bid, budget, or campaign, or
pushing an offline conversion. Every entry is a ``metric`` tagged
``domain:'paid_media'`` (or a ``flag``).

The heavy ad SDKs are optional (`pip install .[ads]`); a missing SDK or
credential degrades to a logged, empty result via ``AdapterUnavailable``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from .base import AdapterUnavailable, BaseAdapter
from .clients.sentinel import SentinelClient

log = get_logger("adapter.paid_media")

_MP_PREFIX = "[CB] -M-"
_CAD_DEFAULT = 0.73


def _campaign_config(ctx) -> dict[str, list[dict[str, Any]]]:
    raw = ctx.creds.resolve("CAMPAIGN_CONFIG", secret=False)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("CAMPAIGN_CONFIG is not valid JSON")
        return {}


def _metric(brand: str, payload: dict[str, Any]) -> EntryDraft:
    payload = {"domain": "paid_media", **payload}
    return EntryDraft(
        type=EntryType.METRIC, brand=brand, source_agent="paid_media",
        source_system=payload.get("platform", "paid_media"), payload=payload,
        confidence=0.9, ttl_seconds=2 * 24 * 3600,
    )


class GoogleAdsAdapter(BaseAdapter):
    name = "google_ads"
    source_system = "google_ads"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        creds = self.ctx.creds.google_ads()
        if not creds.refresh_token:
            raise AdapterUnavailable("Google Ads credentials not configured")
        cfg = _campaign_config(self.ctx)
        ids = [c["campaign_id"] for c in cfg.get("google", [])]
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cad = float(self.ctx.creds.resolve("GOOGLE_CAD_TO_USD_RATE", secret=False) or _CAD_DEFAULT)

        def _run() -> list[dict[str, Any]]:
            try:
                from google.ads.googleads.client import GoogleAdsClient  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise AdapterUnavailable("google-ads not installed (pip install .[ads])") from exc
            client = GoogleAdsClient.load_from_dict({
                "developer_token": creds.developer_token,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "login_customer_id": (creds.customer_id or "").replace("-", ""),
                "use_proto_plus": True,
            })
            svc = client.get_service("GoogleAdsService")
            id_list = ",".join(ids) if ids else ""
            where_ids = f"AND campaign.id IN ({id_list})" if id_list else ""
            gaql = (
                "SELECT campaign.id, campaign.name, metrics.cost_micros, "
                "metrics.impressions, metrics.clicks FROM campaign "
                f"WHERE segments.date='{yesterday}' AND campaign.status != 'REMOVED' {where_ids}"
            )
            out = []
            for row in svc.search(customer_id=(creds.customer_id or "").replace("-", ""), query=gaql):
                name = row.campaign.name
                if not name.startswith(_MP_PREFIX) and ids == []:
                    continue
                out.append({"campaign_id": str(row.campaign.id), "campaign_name": name,
                            "spend": round(row.metrics.cost_micros / 1e6 * cad, 2),
                            "impressions": int(row.metrics.impressions),
                            "clicks": int(row.metrics.clicks)})
            return out

        rows = await asyncio.to_thread(_run)
        total = round(sum(r["spend"] for r in rows), 2)
        drafts = [_metric(brand, {"platform": "google_ads", "kind": "ad_spend", "date": yesterday,
                                  "total_spend_usd": total, "campaigns": rows})]
        if total == 0 and rows:
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="paid_media",
                                     source_system="google_ads",
                                     payload={"domain": "paid_media", "kind": "zeroed_spend",
                                              "platform": "google_ads", "severity": "medium"}))
        return drafts, CostSpec()


class MetaAdsAdapter(BaseAdapter):
    name = "meta_ads"
    source_system = "facebook_ads"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        creds = self.ctx.creds.facebook_ads()
        if not creds.access_token:
            raise AdapterUnavailable("Facebook Ads credentials not configured")
        cfg = _campaign_config(self.ctx)
        ids = {c["campaign_id"] for c in cfg.get("facebook", [])}
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cad = float(self.ctx.creds.resolve("GOOGLE_CAD_TO_USD_RATE", secret=False) or _CAD_DEFAULT)

        def _run() -> list[dict[str, Any]]:
            try:
                from facebook_business.adobjects.adaccount import AdAccount  # type: ignore
                from facebook_business.api import FacebookAdsApi  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise AdapterUnavailable("facebook-business not installed (pip install .[ads])") from exc
            FacebookAdsApi.init(access_token=creds.access_token)
            account = AdAccount(creds.ad_account_id)
            insights = account.get_insights(
                fields=["campaign_id", "campaign_name", "spend", "impressions", "inline_link_clicks"],
                params={"level": "campaign", "time_range": {"since": yesterday, "until": yesterday}},
            )
            out = []
            for row in insights:
                cid = str(row.get("campaign_id"))
                if ids and cid not in ids:
                    continue
                out.append({"campaign_id": cid, "campaign_name": row.get("campaign_name"),
                            "spend": round(float(row.get("spend") or 0) * cad, 2),
                            "impressions": int(row.get("impressions") or 0),
                            "clicks": int(row.get("inline_link_clicks") or 0)})
            return out

        rows = await asyncio.to_thread(_run)
        total = round(sum(r["spend"] for r in rows), 2)
        return [_metric(brand, {"platform": "facebook_ads", "kind": "ad_spend", "date": yesterday,
                                "total_spend_usd": total, "campaigns": rows})], CostSpec()


class BingAdsAdapter(BaseAdapter):
    """Microsoft/Bing Ads spend (read-only). Auth is via a Google OAuth refresh
    token (the account was migrated to a Google login). Requires the ``bingads``
    SDK; degrades softly otherwise. Never replicates mp-spend's offline-conversion
    push."""

    name = "bing_ads"
    source_system = "bing_ads"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        creds = self.ctx.creds.bing_ads()
        if not creds.refresh_token or not creds.developer_token:
            raise AdapterUnavailable("Bing Ads credentials not configured")
        # The bingads reporting flow (report request -> poll -> download CSV) is
        # heavyweight; we surface a marker metric so the plan knows Bing is wired
        # but defer the full report pull to the mp-spend Cloud Run job (which
        # already writes RAW_DATA). Switchboard reads that via the sheet adapter.
        raise AdapterUnavailable(
            "Bing spend is sourced from mp-spend RAW_DATA (see PaidMediaSheetAdapter); "
            "direct bingads report pull intentionally deferred (read-only, no offline push)"
        )


class SentinelEventsAdapter(BaseAdapter):
    """Marketplace conversion counts from Sentinel `events/` (read-only)."""

    name = "sentinel_events"
    source_system = "sentinel"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        api_key, account = self.ctx.creds.sentinel()
        client = SentinelClient(api_key, account)
        r = self.ctx.creds.resolve
        events = [e for e in (r("SENTINEL_EVENT_J", secret=False),
                              r("SENTINEL_EVENT_K", secret=False),
                              r("SENTINEL_EVENT_L", secret=False)) if e]
        if not events:
            events = ["lotlinx_marketplace", "carzing_marketplace", "CarsAndBids_marketplace"]
        property_id = r("SENTINEL_PROPERTY_ID", secret=False) or "www.carbuzz.com"
        page_path = r("SENTINEL_PAGE_PATH", secret=False) or "/marketplace/mb/"
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        payload = {
            "filters": {
                "date": {"gte": yesterday, "lt": today},
                "eventName": {"in": events},
                "pagePath": {"in": [page_path]},
                "propertyId": {"in": [property_id]},
            },
            "dimensions": ["utmCampaign", "eventName"],
        }
        rows = await client.events(payload, max_pages=5)
        by_event: dict[str, int] = {}
        for row in rows:
            ev = row.get("eventName") or "unknown"
            by_event[ev] = by_event.get(ev, 0) + int(row.get("count") or row.get("events") or 1)
        return [_metric(brand, {"platform": "sentinel_events", "kind": "conversions",
                                "date": yesterday, "by_event": by_event})], CostSpec()


class LeadFeedsAdapter(BaseAdapter):
    """Lotlinx lead counts (read-only). Carzing/QuoteWizard live in sheets/S3;
    those are read via the paid-media sheet adapter to avoid duplicating
    mp-spend's ingestion."""

    name = "lead_feeds"
    source_system = "lotlinx"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        client_id, client_secret = self.ctx.creds.lotlinx()
        if not client_secret:
            raise AdapterUnavailable("Lotlinx credentials not configured")
        month = date.today().strftime("%Y-%m")

        async def _run() -> int:
            try:
                import httpx  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise AdapterUnavailable("httpx not installed") from exc
            base = "https://publisher-api.lotlinx.com"
            async with httpx.AsyncClient(timeout=30.0) as http:
                tok = await http.post(f"{base}/v1/auth/token",
                                      json={"client_id": client_id, "client_secret": client_secret})
                tok.raise_for_status()
                bearer = tok.json().get("token") or tok.json().get("access_token")
                headers = {"Authorization": f"Bearer {bearer}"}
                resp = await http.get(f"{base}/v1/reports/click-scrub/{month}/page-1", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data") or data.get("rows") or []
                today = date.today().isoformat()
                return sum(1 for it in items
                           if it.get("status_label") == "VALID"
                           and str(it.get("create_time", ""))[:10] == today)

        valid = await _run()
        return [_metric(brand, {"platform": "lotlinx", "kind": "leads", "date": date.today().isoformat(),
                                "valid_leads": valid, "value_per": 0.75,
                                "value_usd": round(valid * 0.75, 2)})], CostSpec()


class PaidMediaSheetAdapter(BaseAdapter):
    """Read mp-spend's authoritative RAW_DATA / RAW_DATA_ORGANIC sheet tabs
    (read-only SA). This is the canonical spend/ROI source — mp-spend's Cloud Run
    job writes it; Switchboard only reads."""

    name = "paid_media_sheet"
    source_system = "sheets"
    owner_agent = "paid_media"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        from .clients.sheets import SheetsClient

        spreadsheet_id = self.ctx.creds.resolve("SPREADSHEET_ID", secret=False)
        if not spreadsheet_id:
            raise AdapterUnavailable("paid-media SPREADSHEET_ID not configured")
        client = SheetsClient(self.ctx.creds.google_sa())
        yesterday = date.today() - timedelta(days=1)
        want = f"{yesterday.month}/{yesterday.day}/{yesterday.year}"  # mp-spend M/D/YYYY

        raw = await client.read_records(spreadsheet_id, "RAW_DATA")
        rows = [r for r in raw if str(r.get("date", "")).strip() == want]
        spend = round(sum(float(r.get("spend_usd") or 0) for r in rows), 2)
        leads = sum(int(float(r.get("lotlinx") or 0)) + int(float(r.get("carzing_sentinel") or 0))
                    + int(float(r.get("carsandbids") or 0)) for r in rows)
        by_platform: dict[str, float] = {}
        for r in rows:
            p = r.get("platform") or "unknown"
            by_platform[p] = round(by_platform.get(p, 0.0) + float(r.get("spend_usd") or 0), 2)
        payload = {"platform": "raw_data", "kind": "spend_roi", "date": yesterday.isoformat(),
                   "total_spend_usd": spend, "total_leads": leads, "by_platform": by_platform,
                   "cpl": round(spend / leads, 2) if leads else None, "row_count": len(rows)}
        drafts = [_metric(brand, payload)]
        if not rows:
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="paid_media",
                                     source_system="sheets",
                                     payload={"domain": "paid_media", "kind": "no_spend_rows",
                                              "date": yesterday.isoformat(), "severity": "low"}))
        return drafts, CostSpec()
