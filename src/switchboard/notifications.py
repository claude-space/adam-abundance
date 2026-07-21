"""Outbound trend-alert webhook (admin-configurable).

When the Trend Scout surfaces a new trend whose score clears a configured
threshold, POST a compact JSON payload to an external webhook — e.g. a
ShellAgent Workflow that relays it to Slack. Config lives in
``app_setting[TREND_ALERT_KEY]`` (JSON), editable from the Integrations admin
page, NOT in env — so operators tune the threshold / URL at runtime.

Same posture as ``orchestrator.slack``: firing is best-effort and never raises
into the scan path — a down webhook must not break trend sourcing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .db.models import AppSetting
from .logging_ import get_logger

log = get_logger("notifications")

TREND_ALERT_KEY = "trend_alert"
DEFAULT_TREND_ALERT: dict[str, Any] = {"enabled": False, "webhook_url": "", "min_score": 70.0}


def _coerce(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a stored/partial config dict to the full typed shape."""
    cfg = dict(DEFAULT_TREND_ALERT)
    if isinstance(raw, dict):
        for k in ("enabled", "webhook_url", "min_score"):
            if k in raw:
                cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["webhook_url"] = str(cfg.get("webhook_url") or "")
    try:
        cfg["min_score"] = max(0.0, min(100.0, float(cfg["min_score"])))
    except (TypeError, ValueError):
        cfg["min_score"] = DEFAULT_TREND_ALERT["min_score"]
    return cfg


async def load_trend_alert(session: Any) -> dict[str, Any]:
    """The current trend-alert config (defaults when unset)."""
    row = (await session.execute(
        select(AppSetting.value).where(AppSetting.key == TREND_ALERT_KEY)
    )).scalar_one_or_none()
    return _coerce(row)


async def save_trend_alert(session: Any, *, enabled: Any, webhook_url: Any,
                           min_score: Any, updated_by: str | None = None) -> dict[str, Any]:
    """Validate + upsert the trend-alert config. Raises ValueError on a bad URL."""
    url = str(webhook_url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("webhook_url must be an http(s) URL")
    cfg = _coerce({"enabled": enabled, "webhook_url": url, "min_score": min_score})
    existing = (await session.execute(
        select(AppSetting).where(AppSetting.key == TREND_ALERT_KEY)
    )).scalar_one_or_none()
    if existing is None:
        session.add(AppSetting(key=TREND_ALERT_KEY, value=cfg, updated_by=updated_by))
    else:
        existing.value = cfg
        existing.updated_by = updated_by
    await session.flush()
    return cfg


def _build_payload(trend: Any, threshold: float, base_url: str,
                   pipeline_id: int | None) -> dict[str, Any]:
    """Compact JSON for the webhook. `text` is a ready-to-post Slack line so a
    workflow can post {{input}} directly OR Code-parse individual fields."""
    ent = trend.entities if isinstance(getattr(trend, "entities", None), dict) else {}
    oems = ent.get("oems") or []
    url = f"{base_url}/trends/{trend.id}" if base_url else None
    score = round(float(trend.score or 0), 1)
    text = f":fire: New trend for {trend.brand} (score {score:.0f}): {trend.headline}"
    if url:
        text += f" — {url}"
    payload: dict[str, Any] = {
        "event": "trend.sourced",
        "trend": {
            "id": trend.id,
            "brand": trend.brand,
            "headline": trend.headline,
            "score": score,
            "status": trend.status,
            "source_count": trend.source_count,
            "signal_count": trend.signal_count,
            "oems": oems,
            "url": url,
        },
        "threshold": threshold,
        "text": text,
    }
    if pipeline_id is not None:
        payload["trend"]["pipeline_id"] = pipeline_id
    return payload


async def _post(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON best-effort. Returns True only on a 2xx; never raises."""
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
        ok = 200 <= resp.status_code < 300
        log.info("[alert] webhook POST → %s (status %s)", ok, resp.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001 — a down webhook must never break the caller
        log.warning("[alert] webhook POST failed: %s", exc)
        return False


async def fire_trend_alert(ctx: Any, trend: Any, *, pipeline_id: int | None = None) -> bool:
    """Fire the trend-alert webhook if enabled and ``trend.score`` clears the
    configured threshold. Best-effort: catches everything, returns True only if
    a webhook actually accepted the POST."""
    try:
        cfg = await load_trend_alert(ctx.session)
    except Exception as exc:  # noqa: BLE001
        log.warning("[alert] config load failed: %s", exc)
        return False
    if not cfg["enabled"] or not cfg["webhook_url"]:
        return False
    if float(getattr(trend, "score", 0) or 0) < cfg["min_score"]:
        log.info("[alert] trend %s below threshold (%.0f < %.0f) — not firing",
                 getattr(trend, "id", "?"), float(trend.score or 0), cfg["min_score"])
        return False
    base = (ctx.creds.resolve("SWITCHBOARD_PUBLIC_URL", secret=False) or "").rstrip("/")
    payload = _build_payload(trend, cfg["min_score"], base, pipeline_id)
    return await _post(cfg["webhook_url"], payload)


async def send_test_ping(url: str) -> tuple[bool, str]:
    """Fire a synthetic payload at a webhook URL (the admin 'Test' button)."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, "webhook_url must be an http(s) URL"
    payload = {
        "event": "trend.test",
        "trend": {"id": 0, "brand": "hotcars", "headline": "Test trend from Switchboard",
                  "score": 99, "status": "detected", "source_count": 3, "signal_count": 5,
                  "oems": ["Tesla"], "url": None},
        "threshold": 0,
        "text": ":wave: Switchboard trend-alert test ping — if you see this in Slack, the wiring works.",
    }
    ok = await _post(url, payload)
    return ok, ("webhook accepted the test ping" if ok
                else "webhook did not accept the request (check the URL)")
