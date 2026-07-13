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


async def post_brief(ctx: RunContext, brand: str, text: str) -> bool:
    """Post (or log) the morning brief. Returns True if actually posted."""
    enabled = (ctx.creds.resolve("SLACK_NOTIFY_ENABLED", secret=False) or "0").lower() in ("1", "true", "yes")
    if not enabled:
        log.info("[slack] notify disabled (SLACK_NOTIFY_ENABLED!=1); brief for %s logged only:\n%s", brand, text)
        return False
    token = ctx.creds.slack_bot_token(brand)
    channel = (
        ctx.creds.resolve(f"SLACK_CHANNEL_ID_{brand.upper()}", secret=False)
        or ctx.creds.resolve("SLACK_CHANNEL_ID", secret=False)
    )
    if not token or not channel:
        log.info("[slack] no token/channel for %s; brief logged only", brand)
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
            log.info("[slack] posted brief for %s: ok=%s", brand, ok)
            return bool(ok)
    except Exception as exc:  # noqa: BLE001
        log.warning("[slack] post failed for %s: %s", brand, exc)
        return False
