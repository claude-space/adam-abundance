"""Similarweb client (httpx async) — mirrors daily-reporting-agent/similarweb.py.

Key insight from that repo: the API key is a **query param** (`api_key=`), not a
header. Endpoint template:
``https://api.similarweb.com/v1/website/{domain}/total-traffic-and-engagement/{path}``.
"""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.similarweb")

_BASE = "https://api.similarweb.com/v1/website/{domain}/total-traffic-and-engagement/{path}"
_UA = "valnet-switchboard/1.0"


class SimilarwebClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("SIMILARWEB_API_KEY not configured")
        self._key = api_key

    async def _get(self, domain: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        url = _BASE.format(domain=domain, path=path)
        q = {"api_key": self._key, "format": "json", **params}
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _UA}) as client:
            resp = await client.get(url, params=q)
            resp.raise_for_status()
            return resp.json()

    async def available_range(self, domain: str, country: str = "world") -> dict[str, Any]:
        data = await self._get(domain, "describe", {})
        countries = (data.get("total_traffic_and_engagement") or {}).get("countries") or {}
        return countries.get(country) or {}

    async def visits(
        self, domain: str, start_month: str, end_month: str, country: str = "world"
    ) -> list[dict[str, Any]]:
        data = await self._get(
            domain,
            "visits",
            {
                "start_date": start_month,
                "end_date": end_month,
                "country": country,
                "granularity": "daily",
                "main_domain_only": "false",
            },
        )
        return data.get("visits") or []
