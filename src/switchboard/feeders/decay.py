"""Ranking-decay feeder. Reads Seona's decay candidates (its scanner detects two
14-day windows with pos-delta ≥ 2.0 AND click-ratio ≤ 0.70 above a baseline
floor, queuing pending update_runs) and drops them into memory as decay-candidate
flags for the Analytics/Production agents to act on. Read-only; endpoint is
env-configurable and degrades softly."""

from __future__ import annotations

from ..adapters._http import get_json
from ..adapters.base import AdapterUnavailable
from ..context import RunContext
from ..db.enums import EntryType
from ..interfaces import EntryDraft
from ..logging_ import get_logger

log = get_logger("feeder.decay")


class DecayScanFeeder:
    name = "decay_scan"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def run(self, brand: str) -> int:
        base = self.ctx.settings.endpoints.get("seona")
        path = self.ctx.creds.resolve("SEONA_DECAY_LIST_PATH", secret=False) or "/api/decay/candidates"
        try:
            data = await get_json(base, path, params={"brand": brand})
        except AdapterUnavailable as exc:
            log.info("[decay] httpx unavailable: %s", exc)
            return 0
        except Exception as exc:  # noqa: BLE001 — Seona may be unreachable
            log.info("[decay] Seona decay endpoint unavailable: %s", exc)
            return 0
        candidates = data if isinstance(data, list) else data.get("candidates", data.get("data", []))
        written = 0
        for c in (candidates or [])[:50]:
            await self.ctx.store.write(EntryDraft(
                type=EntryType.FLAG, brand=brand, source_agent="decay_scan", source_system="seona",
                payload={"kind": "decay_candidate", "url": c.get("url") or c.get("permalink"),
                         "pos_delta": c.get("pos_delta"), "click_ratio": c.get("click_ratio"),
                         "severity": "medium"},
                source_urls=[c["url"]] if c.get("url") else None, ttl_seconds=3 * 24 * 3600,
            ))
            written += 1
        log.info("[decay] wrote %d decay candidates for %s", written, brand)
        return written
