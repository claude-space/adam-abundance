"""Anthropic client wrapper — the LLM substrate for all LLM-backed agents.

Every call is costed with the shared :mod:`switchboard.costs` formula and charged
to the governor's ``llm_micros`` ledger, so LLM spend counts against the same
caps as Ahrefs/BigQuery. The API key comes from the credentials layer and is
never placed in a prompt or log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...costs import compute_llm_micros
from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.llm")


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_requests: int = 0
    micros: int = 0
    citations: list[str] = field(default_factory=list)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


class LLMClient:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def _client(self):
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("anthropic SDK not installed") from exc
        key = self.ctx.creds.anthropic_key()
        if not key:
            raise AdapterUnavailable("ANTHROPIC_API_KEY not configured")
        return AsyncAnthropic(api_key=key)

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        agent: str = "system",
    ) -> LLMResult:
        model = model or self.ctx.settings.models.default

        # Pre-check the per-run LLM cap using a rough token ceiling.
        rough = compute_llm_micros(model, input_tokens=len(prompt) // 3, output_tokens=max_tokens)
        if not await self.ctx.governor.within_caps("llm_micros", additional=rough):
            raise AdapterUnavailable("llm_micros daily cap would be exceeded")

        client = self._client()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if tools:
            kwargs["tools"] = tools
        resp = await client.messages.create(**kwargs)

        text_parts: list[str] = []
        citations: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        web_searches = 0
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
                for c in getattr(block, "citations", None) or []:
                    url = getattr(c, "url", None)
                    if url:
                        citations.append(url)
            elif btype == "tool_use":
                tool_uses.append({"name": getattr(block, "name", ""), "input": getattr(block, "input", {})})
            elif btype and "server_tool_use" in btype:
                web_searches += 1

        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        # Server-side web search count, when reported.
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            web_searches = int(getattr(server, "web_search_requests", web_searches) or web_searches)

        micros = compute_llm_micros(
            model, input_tokens=in_tok, output_tokens=out_tok,
            cache_creation_tokens=cache_write, cache_read_tokens=cache_read,
            web_search_requests=web_searches,
        )
        await self.ctx.governor.charge("llm_micros", micros, agent)
        return LLMResult(text="".join(text_parts), input_tokens=in_tok, output_tokens=out_tok,
                         web_search_requests=web_searches, micros=micros,
                         citations=list(dict.fromkeys(citations)), tool_uses=tool_uses)

    async def web_search(self, query: str, *, model: str | None = None, agent: str = "research",
                         max_tokens: int = 512) -> LLMResult:
        """Search-confirm a query using Anthropic's server-side web_search tool.
        Returns text + citation URLs used by the Research fact-gate."""
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        return await self.complete(
            system=("You are a fact verification assistant. Use web search to confirm or refute the "
                    "user's statement. Cite sources. Answer starting with VERIFIED or UNVERIFIED."),
            prompt=query, model=model or self.ctx.settings.models.factcheck,
            max_tokens=max_tokens, tools=tools, agent=agent,
        )
