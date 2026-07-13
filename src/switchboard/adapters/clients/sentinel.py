"""Sentinel Pro API client (httpx async).

Ports the shape used by content-depth-auditor and daily-reporting-agent:
``GET https://{account}.sentinelpro.com/api/v1/{endpoint}/`` with the query
encoded as ``?data=<json>`` and a ``SENTINEL-API-KEY`` header. Two endpoints:
``traffic`` (sessions/engagement) and ``events`` (paid-media conversions).
Paginates on ``totalPage`` with a gentle ~1 req/s pace.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.sentinel")


class SentinelClient:
    def __init__(self, api_key: str | None, account: str = "valnet", *, pace_seconds: float = 1.0) -> None:
        if not api_key:
            raise AdapterUnavailable("SENTINEL_API_KEY not configured")
        self._api_key = api_key
        self._base = f"https://{account}.sentinelpro.com/api/v1"
        self._pace = pace_seconds

    async def _get(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        url = f"{self._base}/{endpoint}/"
        headers = {"SENTINEL-API-KEY": self._api_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(4):
                resp = await client.get(url, params={"data": json.dumps(payload)}, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                resp.raise_for_status()
                return resp.json()
        raise RuntimeError(f"Sentinel {endpoint} failed after retries")

    async def query(self, endpoint: str, payload: dict[str, Any], *, max_pages: int = 10) -> list[dict[str, Any]]:
        """Return the concatenated ``data`` array across pages."""
        rows: list[dict[str, Any]] = []
        page = 1
        pagination = dict(payload.get("pagination") or {})
        pagination.setdefault("pageSize", 1000)
        while page <= max_pages:
            pagination["pageNumber"] = page
            body = {**payload, "pagination": pagination}
            data = await self._get(endpoint, body)
            batch = data.get("data") or []
            rows.extend(batch)
            total_pages = int(data.get("totalPage") or 1)
            if page >= total_pages or not batch:
                break
            page += 1
            await asyncio.sleep(self._pace)
        return rows

    async def traffic(self, payload: dict[str, Any], *, max_pages: int = 10) -> list[dict[str, Any]]:
        return await self.query("traffic", payload, max_pages=max_pages)

    async def events(self, payload: dict[str, Any], *, max_pages: int = 10) -> list[dict[str, Any]]:
        return await self.query("events", payload, max_pages=max_pages)
