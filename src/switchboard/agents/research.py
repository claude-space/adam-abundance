"""Research agent (PRD §6.2): outside-in context + the **fact-integrity gate**.

It is the only agent permitted to certify a ``fact`` (``verified=true``). Its
fact-gate scans pending claims that other agents flagged for verification, uses
web search to confirm them, and — only on search-confirmation — writes a verified
fact citing sources. Everything unconfirmed stays a claim. This mirrors the
existing fact-checker discipline: search-confirmed or it's a claim, never
training-data recall promoted to fact.
"""

from __future__ import annotations

from ..adapters.base import AdapterUnavailable
from ..adapters.clients.llm import LLMClient
from ..db.enums import EntryType
from ..interfaces import EntryDraft
from ..logging_ import get_logger
from .base import BaseAgent

log = get_logger("agent.research")

_MAX_VERIFY_PER_RUN = 5


class ResearchAgent(BaseAgent):
    name = "research"

    async def observe(self, brand: str) -> int:
        written = await super().observe(brand)          # Similarweb + competitor news context
        written += await self._run_fact_gate(brand)
        return written

    async def _run_fact_gate(self, brand: str) -> int:
        """Promote a bounded number of pending claims to verified facts."""
        pending = await self.ctx.store.query(
            brand=brand, types=[EntryType.CLAIM],
            payload_contains={"needs_verification": True}, limit=_MAX_VERIFY_PER_RUN,
        )
        if not pending:
            return 0
        llm = LLMClient(self.ctx)
        promoted = 0
        for claim in pending:
            statement = (claim.payload or {}).get("statement") or (claim.payload or {}).get("claim")
            if not statement:
                continue
            verified, urls = await self._verify(llm, statement)
            if verified:
                await self.ctx.store.write(
                    EntryDraft(
                        type=EntryType.FACT, brand=claim.brand, source_agent="research",
                        source_system="web_search",
                        payload={"kind": "verified_fact", "statement": statement,
                                 "verified_from_claim": claim.id},
                        verified=True, source_urls=urls, confidence=0.9,
                    ),
                    fact_gate_ok=True,  # Research is the certifying authority
                )
                await self.ctx.store.supersede([claim.id])
                promoted += 1
            else:
                log.info("[research] claim %s not search-confirmed; remains a claim", claim.id)
        log.info("[research] fact-gate promoted %d/%d claims", promoted, len(pending))
        return promoted

    async def _verify(self, llm: LLMClient, statement: str) -> tuple[bool, list[str]]:
        try:
            result = await llm.web_search(f"Verify this statement: {statement}", agent="research")
        except AdapterUnavailable as exc:
            log.info("[research] verification unavailable (%s); treating as unverified", exc)
            return False, []
        # The model may wrap the verdict in markdown ("**VERIFIED**") or prose;
        # judge on the first line, and never let "UNVERIFIED" count as verified.
        first_line = result.text.strip().upper().replace("*", "").replace("#", "").strip().split("\n", 1)[0]
        verified = "VERIFIED" in first_line and "UNVERIFIED" not in first_line
        return verified, result.citations
