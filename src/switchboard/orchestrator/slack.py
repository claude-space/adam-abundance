"""Slack notification surface (PRD §4, §14 — notify only).

Conservative by default: posting is **disabled unless** ``SLACK_NOTIFY_ENABLED=1``
in config; otherwise the brief is logged, not sent. This keeps Switchboard from
sending anything on a human's behalf without an explicit opt-in, consistent with
the "no autonomous distribution / notify-only" posture. Never posts beyond the
configured internal channel.
"""

from __future__ import annotations

from ..context import RunContext
from ..logging_ import get_logger

log = get_logger("orchestrator.slack")


async def post_message(ctx: RunContext, brand: str, text: str, *,
                       channel_env: str | None = None, what: str = "message") -> bool:
    """Post (or log) one message to the internal channel. Returns True only if
    actually posted. Channel resolution: ``channel_env`` override →
    ``SLACK_CHANNEL_ID_<BRAND>`` → ``SLACK_CHANNEL_ID``."""
    enabled = (ctx.creds.resolve("SLACK_NOTIFY_ENABLED", secret=False) or "0").lower() in ("1", "true", "yes")
    if not enabled:
        log.info("[slack] notify disabled (SLACK_NOTIFY_ENABLED!=1); %s for %s logged only:\n%s",
                 what, brand, text)
        return False
    token = ctx.creds.slack_bot_token(brand)
    channel = (
        (ctx.creds.resolve(channel_env, secret=False) if channel_env else None)
        or ctx.creds.resolve(f"SLACK_CHANNEL_ID_{brand.upper()}", secret=False)
        or ctx.creds.resolve("SLACK_CHANNEL_ID", secret=False)
    )
    if not token or not channel:
        log.info("[slack] no token/channel for %s; %s logged only", brand, what)
        return False
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "text": text, "mrkdwn": True},
            )
            ok = resp.json().get("ok", False)
            log.info("[slack] posted %s for %s: ok=%s", what, brand, ok)
            return bool(ok)
    except Exception as exc:  # noqa: BLE001
        log.warning("[slack] post failed for %s: %s", brand, exc)
        return False


async def post_brief(ctx: RunContext, brand: str, text: str) -> bool:
    """Post (or log) the morning brief. Returns True if actually posted."""
    return await post_message(ctx, brand, text, what="brief")


_TREND_EVENT_LINES = {
    "trigger_requested": ":rotating_light: *Trend pipeline request* — approve or decline",
    "pipeline_approved": ":white_check_mark: *Trend pipeline approved* — generating content",
    "pipeline_declined": ":no_entry: *Trend pipeline declined*",
    "previews_ready": ":art: *Content previews ready for review*",
    "content_published": ":package: *Trend content approved for hand-off*",
}


async def notify_trend_event(ctx: RunContext, brand: str, event: str, *,
                             headline: str, trend_id: int | None = None,
                             pipeline_id: int | None = None,
                             score: float | None = None,
                             detail: str | None = None) -> bool:
    """Trend-pipeline notifications (docs/trend-pipeline.md). Same posture as
    the brief: opt-in via SLACK_NOTIFY_ENABLED, log-only otherwise. Channel can
    be split off with SLACK_CHANNEL_ID_TRENDS."""
    header = _TREND_EVENT_LINES.get(event, f"*Trend event: {event}*")
    lines = [header, f"> {headline}"]
    meta = [f"brand: {brand}"]
    if score is not None:
        meta.append(f"score: {score:.0f}")
    lines.append("  ·  ".join(meta))
    base = (ctx.creds.resolve("SWITCHBOARD_PUBLIC_URL", secret=False) or "").rstrip("/")
    if trend_id is not None:
        lines.append(f"{base}/trends/{trend_id}" if base else f"→ console: /trends/{trend_id}")
    if pipeline_id is not None:
        lines.append(f"{base}/pipelines/{pipeline_id}" if base else f"→ console: /pipelines/{pipeline_id}")
    if detail:
        lines.append(detail)
    return await post_message(ctx, brand, "\n".join(lines),
                              channel_env="SLACK_CHANNEL_ID_TRENDS", what=f"trend:{event}")
