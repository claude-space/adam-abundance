"""Ahrefs API v3 client (httpx async) — METERED.

Ahrefs bills ~10 units/row (PRD §4). The client only performs the HTTP call and
reports a unit estimate; the Opportunity adapter is responsible for charging the
governor and for cache-first behavior (it checks shared memory for a fresh cached
result before spending units — mirroring Seona's 7-day ``ahrefs_cache`` TTL
without reaching into Seona's SQLite).
"""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.ahrefs")

_BASE = "https://api.ahrefs.com/v3"
UNITS_PER_ROW = 10
CACHE_TTL_SECONDS = 7 * 24 * 3600  # match Seona's ahrefs_cache TTL


class AhrefsClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("AHREFS_API_KEY not configured")
        self._key = api_key

    async def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        url = f"{_BASE}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def units_for(row_count: int) -> int:
        return max(0, row_count) * UNITS_PER_ROW
