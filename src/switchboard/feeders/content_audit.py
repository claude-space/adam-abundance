"""Content-depth-audit feeder. Reads the content-depth-auditor's recent findings
(articles below the depth + AVD thresholds) and drops them into memory as
content-audit flags for the Analytics/Opportunity agents. Read-only; the
auditor's REST API is JWT-gated, so a token is env-configurable and the feeder
degrades softly when it can't authenticate."""

from __future__ import annotations

from ..adapters._http import get_json
from ..adapters.base import AdapterUnavailable
from ..context import RunContext
from ..db.enums import EntryType
from ..interfaces import EntryDraft
from ..logging_ import get_logger

log = get_logger("feeder.content_audit")


class ContentAuditFeeder:
    name = "content_audit"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def run(self, brand: str) -> int:
        base = self.ctx.creds.resolve("CONTENT_AUDITOR_URL", secret=False) or "http://localhost:8600"
        path = self.ctx.creds.resolve("CONTENT_AUDITOR_TRACKING_PATH", secret=False) or "/api/tracking"
        token = self.ctx.creds.resolve("CONTENT_AUDITOR_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            data = await get_json(base, path, headers=headers, params={"brand": brand})
        except AdapterUnavailable as exc:
            log.info("[content_audit] httpx unavailable: %s", exc)
            return 0
        except Exception as exc:  # noqa: BLE001 — auditor may be unreachable / needs auth
            log.info("[content_audit] auditor endpoint unavailable: %s", exc)
            return 0
        findings = data if isinstance(data, list) else data.get("tracking", data.get("items", []))
        written = 0
        for f in (findings or [])[:50]:
            await self.ctx.store.write(EntryDraft(
                type=EntryType.FLAG, brand=brand, source_agent="content_audit",
                source_system="content_depth_auditor",
                payload={"kind": "content_audit_finding", "url": f.get("url"),
                         "depth_pct": f.get("depth_pct") or f.get("depth"),
                         "avd_seconds": f.get("avd_seconds") or f.get("avd"), "severity": "medium"},
                source_urls=[f["url"]] if f.get("url") else None, ttl_seconds=5 * 24 * 3600,
            ))
            written += 1
        log.info("[content_audit] wrote %d findings for %s", written, brand)
        return written
