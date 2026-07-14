"""Dossier builder — "collect everything we can" about one trend
(docs/trend-pipeline.md).

Deep sources (Tavily advanced search, Firecrawl page extracts, Perplexity) feed
one synthesis-model LLM pass that produces a structured dossier. Key facts are
also written to shared memory as ``claim`` entries with
``needs_verification=true`` so the existing Research fact-gate verifies them —
the dossier itself never asserts verified facts (PRD fact-gate rule).

Every step degrades softly: a missing key or a dead API shrinks the dossier,
it never blocks the pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..adapters.base import AdapterUnavailable
from ..adapters.clients.firecrawl import FirecrawlClient
from ..adapters.clients.llm import LLMClient
from ..adapters.clients.perplexity import PerplexityClient
from ..adapters.clients.tavily import TavilyClient
from ..artifacts import ArtifactStore
from ..context import RunContext
from ..db.enums import EntryType
from ..db.models import Trend
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger

log = get_logger("trends.dossier")

_MAX_CLAIMS = 5
_MAX_EXTRACTS = 3

_SYNTHESIS_SYSTEM = (
    "You are a research analyst for an automotive publisher. Synthesize the raw "
    "material into a JSON dossier. Only include statements supported by the "
    "material; never invent facts. Respond with ONLY a JSON object with keys: "
    '"summary" (3-5 sentences), "timeline" (list of "date — event" strings), '
    '"key_facts" (list of {"statement", "source_url"}), '
    '"angles" (list of {"title", "rationale", "content_type"}), '
    '"entities" (list of strings), "risks" (list of strings).'
)


async def collect_dossier(ctx: RunContext, trend: Trend) -> dict[str, Any]:
    """Build + persist the dossier for a trend. Returns the dossier dict."""
    query = trend.headline
    material: list[str] = []
    sources: list[str] = []

    async def _audit(tool: str, ok: bool, request: dict[str, Any],
                     cost: CostSpec | None = None) -> None:
        """Every external pull leaves a tool_call_log row (PRD §8) — the dossier
        path must be as audited as adapter observes."""
        try:
            await ctx.store.log_tool_call(agent="trend_scout", tool=tool, action="read",
                                          dry_run=False, brand=trend.brand,
                                          request=request, ok=ok, cost=cost)
        except Exception as exc:  # noqa: BLE001 — auditing must not break collection
            log.warning("[dossier] audit log failed for %s: %s", tool, exc)

    # 1. Tavily advanced search (answer + raw page content).
    try:
        tavily = TavilyClient(ctx.creds.tavily_key())
        deep = await tavily.deep_search(query, max_results=5)
        if deep.get("answer"):
            material.append(f"## Tavily synthesis\n{deep['answer']}")
        for r in deep.get("results", []):
            material.append(f"## {r['title']} ({r['url']})\n{r['content'][:4000]}")
            sources.append(r["url"])
        await _audit("tavily_deep_search", True, {"trend_id": trend.id, "query": query})
    except AdapterUnavailable as exc:
        log.info("[dossier] tavily unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.info("[dossier] tavily failed: %s", exc)
        await _audit("tavily_deep_search", False, {"trend_id": trend.id, "query": query})

    # 2. Firecrawl extraction of the strongest evidence URLs.
    evidence_urls = [e.get("url") for e in (trend.evidence or []) if e.get("url")]
    try:
        firecrawl = FirecrawlClient(ctx.creds.firecrawl_key())
        for url in evidence_urls[:_MAX_EXTRACTS]:
            page = await firecrawl.scrape(url)
            if page.get("markdown"):
                material.append(f"## {page['title']} ({url})\n{page['markdown'][:4000]}")
                sources.append(url)
        await _audit("firecrawl_scrape", True,
                     {"trend_id": trend.id, "urls": len(evidence_urls[:_MAX_EXTRACTS])})
    except AdapterUnavailable as exc:
        log.info("[dossier] firecrawl unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.info("[dossier] firecrawl failed: %s", exc)
        await _audit("firecrawl_scrape", False, {"trend_id": trend.id})

    # 3. Perplexity summary with citations (a paid LLM call — metered).
    try:
        perplexity = PerplexityClient(ctx.creds.perplexity_key())
        answer = await perplexity.ask(
            f"Summarize everything known so far about: {query}. Include specifics "
            "(numbers, names, dates) and note what is still unconfirmed.",
            max_tokens=800,
        )
        if answer.get("text"):
            material.append(f"## Perplexity summary\n{answer['text']}")
            sources.extend(answer.get("citations", []))
        micros = int(answer.get("micros", 0) or 0)
        if micros:
            await ctx.governor.charge("llm_micros", micros, "trend_scout")
        await _audit("perplexity_ask", True, {"trend_id": trend.id, "query": query},
                     cost=CostSpec(llm_micros=micros))
    except AdapterUnavailable as exc:
        log.info("[dossier] perplexity unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.info("[dossier] perplexity failed: %s", exc)
        await _audit("perplexity_ask", False, {"trend_id": trend.id, "query": query})

    # Evidence titles are always available as a floor.
    if not material:
        lines = [f"- {e.get('source', '?')}: {e.get('title', '')} ({e.get('url', '')})"
                 for e in (trend.evidence or [])]
        material.append("## Competitor coverage\n" + "\n".join(lines))
        sources.extend(evidence_urls)

    dossier = await _synthesize(ctx, trend, material)
    dossier["sources"] = list(dict.fromkeys(s for s in sources if s))[:20]
    dossier["collected_at"] = datetime.now(timezone.utc).isoformat()

    # Key facts go through the fact-gate as claims (never as facts — PRD §8).
    await _write_claims(ctx, trend, dossier)

    dossier_ref = None
    try:
        dossier_ref = ArtifactStore().put_text(
            brand=trend.brand, kind="trend_dossier", ext="md",
            text=_render_markdown(trend, dossier), content_type="text/markdown",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[dossier] artifact write failed: %s", exc)

    dossier["_artifact"] = bool(dossier_ref)
    trend.dossier = dossier
    trend.dossier_ref = dossier_ref
    await ctx.session.flush()
    return dossier


async def _synthesize(ctx: RunContext, trend: Trend, material: list[str]) -> dict[str, Any]:
    """One LLM pass over the collected material → structured dossier."""
    corpus = "\n\n".join(material)[:60_000]
    try:
        llm = LLMClient(ctx)
        result = await llm.complete(
            system=_SYNTHESIS_SYSTEM,
            prompt=f"TREND: {trend.headline}\n\nRAW MATERIAL:\n{corpus}",
            model=ctx.settings.models.synthesis,
            max_tokens=2000,
            agent="trend_scout",
        )
        parsed = _parse_json(result.text)
        if parsed:
            parsed["llm_micros"] = result.micros
            return parsed
        log.info("[dossier] synthesis returned unparseable JSON; using fallback")
    except AdapterUnavailable as exc:
        log.info("[dossier] LLM unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("[dossier] synthesis failed: %s", exc)
    # Fallback: an un-synthesized dossier still carries the evidence.
    return {
        "summary": trend.summary or trend.headline,
        "timeline": [], "key_facts": [], "angles": [], "risks": [],
        "entities": list((trend.entities or {}).get("oems", [])),
    }


async def _write_claims(ctx: RunContext, trend: Trend, dossier: dict[str, Any]) -> None:
    claims = [f for f in dossier.get("key_facts", []) if isinstance(f, dict) and f.get("statement")]
    drafts = [
        EntryDraft(
            type=EntryType.CLAIM, brand=trend.brand, source_agent="trend_scout",
            source_system="trend_dossier",
            payload={"kind": "trend_key_fact", "trend_id": trend.id,
                     "statement": c["statement"], "needs_verification": True},
            source_urls=[c["source_url"]] if c.get("source_url") else None,
            confidence=0.5,
        )
        for c in claims[:_MAX_CLAIMS]
    ]
    if drafts:
        await ctx.store.write_many(drafts)


def _parse_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _render_markdown(trend: Trend, dossier: dict[str, Any]) -> str:
    lines = [f"# Trend dossier — {trend.headline}", "",
             f"*Brand: {trend.brand} · score {trend.score:.0f} · collected {dossier.get('collected_at', '')}*",
             "", "## Summary", dossier.get("summary", ""), ""]
    if dossier.get("timeline"):
        lines += ["## Timeline"] + [f"- {t}" for t in dossier["timeline"]] + [""]
    if dossier.get("key_facts"):
        lines.append("## Key facts (pending verification)")
        for f in dossier["key_facts"]:
            src = f" — {f.get('source_url')}" if f.get("source_url") else ""
            lines.append(f"- {f.get('statement', '')}{src}")
        lines.append("")
    if dossier.get("angles"):
        lines.append("## Suggested angles")
        for a in dossier["angles"]:
            lines.append(f"- **{a.get('title', '')}** ({a.get('content_type', 'article')}): "
                         f"{a.get('rationale', '')}")
        lines.append("")
    if dossier.get("risks"):
        lines += ["## Risks"] + [f"- {r}" for r in dossier["risks"]] + [""]
    if trend.evidence:
        lines.append("## Competitor coverage")
        for e in trend.evidence:
            lines.append(f"- {e.get('source', '?')}: [{e.get('title', '')}]({e.get('url', '')})")
        lines.append("")
    if dossier.get("sources"):
        lines += ["## Sources"] + [f"- {s}" for s in dossier["sources"]]
    return "\n".join(lines)
