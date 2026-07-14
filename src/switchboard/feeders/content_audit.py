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


def _rel_drop(baseline: float | None, snapshot: float | None) -> float:
    """Fractional decline baseline→snapshot (0.0 if it held, improved, or isn't
    computable)."""
    try:
        if baseline is None or snapshot is None or float(baseline) <= 0:
            return 0.0
        return max(0.0, (float(baseline) - float(snapshot)) / float(baseline))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


class ContentAuditFeeder:
    name = "content_audit"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def run(self, brand: str) -> int:
        base = self.ctx.creds.resolve("CONTENT_AUDITOR_URL", secret=False) or "http://localhost:8600"
        path = self.ctx.creds.resolve("CONTENT_AUDITOR_TRACKING_PATH", secret=False) or "/api/tracking"
        token = self.ctx.creds.resolve("CONTENT_AUDITOR_TOKEN")
        # The auditor 403s non-browser user-agents; the Authorization header is only
        # sent when a token is configured (the tracking endpoint is otherwise open).
        headers = {"User-Agent": "Switchboard/1.0 (+content-audit-feeder)"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            data = await get_json(base, path, headers=headers, params={"brand": brand})
        except AdapterUnavailable as exc:
            log.info("[content_audit] httpx unavailable: %s", exc)
            return 0
        except Exception as exc:  # noqa: BLE001 — auditor may be unreachable / needs auth
            log.info("[content_audit] auditor endpoint unavailable: %s", exc)
            return 0

        # Response shape: {"records": [{url, property_id, status, baseline_depth_pct,
        # snapshot_depth_pct, baseline_avd_seconds, snapshot_avd_seconds, ...}]}.
        if isinstance(data, list):
            records = data
        else:
            records = data.get("records") or data.get("tracking") or data.get("items") or []

        written = scanned = matched = 0
        for f in records:
            scanned += 1
            url = f.get("url")
            if not url:
                continue
            # The endpoint returns the whole Auto group; keep only this brand's own
            # domain (property_id looks like "www.hotcars.com").
            prop = (f.get("property_id") or "").lower()
            if prop and brand.lower() not in prop:
                continue
            matched += 1
            base_depth, snap_depth = f.get("baseline_depth_pct"), f.get("snapshot_depth_pct")
            base_avd, snap_avd = f.get("baseline_avd_seconds"), f.get("snapshot_avd_seconds")
            dd, ad = _rel_drop(base_depth, snap_depth), _rel_drop(base_avd, snap_avd)
            worst = max(dd, ad)
            if worst < 0.10:  # only articles that materially slipped are findings
                continue
            await self.ctx.store.write(EntryDraft(
                type=EntryType.FLAG, brand=brand, source_agent="content_audit",
                source_system="content_depth_auditor",
                payload={"kind": "content_audit_finding", "url": url,
                         "property_id": f.get("property_id"), "status": f.get("status"),
                         "depth_pct": snap_depth, "baseline_depth_pct": base_depth,
                         "avd_seconds": snap_avd, "baseline_avd_seconds": base_avd,
                         "depth_drop_pct": round(dd * 100, 1), "avd_drop_pct": round(ad * 100, 1),
                         "severity": "high" if worst >= 0.25 else "medium"},
                source_urls=[url], ttl_seconds=5 * 24 * 3600,
            ))
            written += 1
            if written >= 50:
                break
        log.info("[content_audit] %s: scanned %d, brand-matched %d, flagged %d decayed",
                 brand, scanned, matched, written)
        return written
