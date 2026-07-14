"""SEMrush Analytics API client — rising related search phrases as a
search-demand signal (docs/trend-pipeline.md). SEMrush returns CSV
(semicolon-delimited); each call consumes API units. Soft-fails without a key."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.semrush")

_BASE = "https://api.semrush.com/"


class SemrushClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("SEMRUSH_API_KEY not configured")
        self._key = api_key

    async def related_phrases(self, phrase: str, *, database: str = "us",
                              limit: int = 10) -> list[dict[str, Any]]:
        """Related phrases + monthly volume for a seed phrase, as normalized
        signal items [{title, url, source, published_at, snippet}]."""
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(_BASE, params={
                "type": "phrase_related", "key": self._key, "phrase": phrase,
                "database": database, "export_columns": "Ph,Nq,Cp", "display_limit": limit,
            })
            resp.raise_for_status()
            text = resp.text.strip()
        if not text or text.upper().startswith("ERROR"):
            log.info("[semrush] no data / error response: %s", text[:120])
            return []
        lines = text.splitlines()
        header = [h.strip() for h in lines[0].split(";")]
        out: list[dict[str, Any]] = []
        for line in lines[1:]:
            cells = line.split(";")
            row = dict(zip(header, cells))
            phrase_v = row.get("Keyword") or row.get("Ph") or ""
            vol = row.get("Search Volume") or row.get("Nq") or ""
            if not phrase_v:
                continue
            out.append({
                "title": phrase_v,
                "url": f"https://www.semrush.com/analytics/keywordoverview/?q={phrase_v.replace(' ', '+')}",
                "source": "SEMrush", "published_at": "",
                "snippet": f"~{vol}/mo searches" if vol else "",
            })
        return out
