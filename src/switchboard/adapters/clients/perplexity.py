"""Perplexity (sonar) client — search-grounded answers with citations, used for
trend sourcing + dossier synthesis (docs/trend-pipeline.md)."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.perplexity")

_BASE = "https://api.perplexity.ai"


class PerplexityClient:
    def __init__(self, api_key: str | None, *, model: str = "sonar") -> None:
        if not api_key:
            raise AdapterUnavailable("PERPLEXITY_API_KEY not configured")
        self._api_key = api_key
        self._model = model

    async def ask(self, prompt: str, *, system: str | None = None, max_tokens: int = 1024) -> dict[str, Any]:
        """Returns {text, citations: [urls], search_results: [{title, url, date}],
        micros} — micros is the estimated cost in micro-USD from reported usage
        (sonar ≈ $1/M in + $1/M out), so callers can meter it as llm_micros."""
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model, "messages": messages, "max_tokens": max_tokens},
            )
            resp.raise_for_status()
            data = resp.json()
        text = ""
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            log.info("perplexity: unexpected response shape")
        citations = [c for c in (data.get("citations") or []) if isinstance(c, str)]
        search_results = []
        for r in data.get("search_results") or []:
            if isinstance(r, dict):
                search_results.append({
                    "title": r.get("title", ""), "url": r.get("url", ""),
                    "date": r.get("date") or "",
                })
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        total_tokens = int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
        micros = total_tokens  # ≈ $1/M tokens → 1 micro-USD per token for sonar
        return {"text": text, "citations": citations, "search_results": search_results,
                "micros": micros}
