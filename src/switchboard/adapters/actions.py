"""Action adapters (PRD §6, §8, Phase 4). Each performs one governor-gated side
effect for an approved plan_item and honors the dry-run contract:

* ``dry_run=True`` (the default) — log the intended effect and return an
  ``intended`` result_ref; perform **no external write** and spend nothing.
* ``dry_run=False`` — perform exactly one real action and record its cost.

External *sends* (the digest email) and *pushes* (Emaki draft) only ever happen
on a live-approved item. Assemble actions (digest/newsletter/social) produce an
artifact + pointer and never send/post. Paid-media has no action adapter at all.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Any

from ..artifacts import ArtifactStore
from ..db.enums import EntryType
from ..interfaces import ActionResult, CostSpec, EntryDraft, PlanItemView
from ..logging_ import get_logger
from ._http import get_json, post_json
from .base import AdapterUnavailable, BaseAdapter

log = get_logger("adapter.actions")


def _est(item: PlanItemView) -> CostSpec:
    e = item.cost_estimate or {}
    return CostSpec(ahrefs_units=int(e.get("ahrefs_units", 0) or 0),
                    llm_micros=int(e.get("llm_micros", 0) or 0),
                    bq_bytes=int(e.get("bq_bytes", 0) or 0))


def _dry(item: PlanItemView, desc: str, intended: dict[str, Any]) -> ActionResult:
    log.info("[dry-run] %s: would %s", item.action_type, desc)
    return ActionResult(ok=True, dry_run=True, action_type=item.action_type,
                        summary=f"[dry-run] would {desc}", result_ref={"intended": intended})


# ---------------------------------------------------------------------------
# Opportunity actions
# ---------------------------------------------------------------------------

class IdeationTriggerAdapter(BaseAdapter):
    name = "ideation_trigger"
    owner_agent = "opportunity"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        source = (item.params or {}).get("source", "hc_viral_hits")
        brand = item.brand
        routes = {
            "hc_viral_hits": (self.ctx.settings.endpoints["hc_viral_hits"], "/api/pipeline/ideate",
                              {"X-API-Key": self.ctx.creds.resolve("HC_VIRAL_HITS_API_KEY") or ""}),
            "claude_albert": (self.ctx.settings.endpoints["albert"],
                              self.ctx.creds.resolve("ALBERT_IDEATE_PATH", secret=False) or "/api/ideate", {}),
            "seona": (self.ctx.settings.endpoints["seona"],
                      self.ctx.creds.resolve("SEONA_IDEATE_PATH", secret=False) or "/api/ideate", {}),
        }
        if source not in routes:
            raise AdapterUnavailable(f"unknown ideation source '{source}'")
        base, path, headers = routes[source]
        if dry_run:
            return _dry(item, f"trigger {source} ideation for {brand}",
                        {"source": source, "endpoint": f"{base}{path}", "brand": brand})
        resp = await post_json(base, path, params={"brand": brand}, headers=headers)
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"triggered {source} ideation", result_ref={"response": resp},
                            cost=_est(item))


# ---------------------------------------------------------------------------
# Production actions
# ---------------------------------------------------------------------------

class AsanaTaskAdapter(BaseAdapter):
    name = "asana_task"
    owner_agent = "production"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        pat = self.ctx.creds.asana_pat()
        project = self.ctx.creds.resolve(f"ASANA_PROJECT_{item.brand.upper()}", secret=False)
        p = item.params or {}
        name = p.get("name") or p.get("title") or "Switchboard task"
        notes = p.get("notes") or p.get("rationale") or item.rationale or ""
        if dry_run:
            return _dry(item, f"create Asana task '{name}' in project {project}",
                        {"name": name, "project": project, "notes": notes[:200]})
        if not (pat and project):
            raise AdapterUnavailable("ASANA_PAT / ASANA_PROJECT_<brand> not configured")
        resp = await post_json(
            "https://app.asana.com/api/1.0", "/tasks",
            json={"data": {"name": name, "notes": notes, "projects": [project]}},
            headers={"Authorization": f"Bearer {pat}"},
        )
        gid = (resp.get("data") or {}).get("gid")
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"created Asana task {gid}", result_ref={"task_gid": gid})


class AlbertRouteToWriterAdapter(BaseAdapter):
    name = "albert_route_writer"
    owner_agent = "production"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        base = self.ctx.settings.endpoints["albert"]
        path = self.ctx.creds.resolve("ALBERT_ROUTE_PATH", secret=False) or "/api/writer/route"
        topic_id = (item.params or {}).get("topic_id")
        if dry_run:
            return _dry(item, f"route topic {topic_id} to the Albert AI writer",
                        {"endpoint": f"{base}{path}", "topic_id": topic_id, "brand": item.brand})
        resp = await post_json(base, path, json={"topic_id": topic_id, "brand": item.brand})
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"routed topic {topic_id} to writer", result_ref={"response": resp},
                            cost=_est(item))


class SeonaDecayRefreshAdapter(BaseAdapter):
    name = "seona_decay_refresh"
    owner_agent = "production"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        base = self.ctx.settings.endpoints["seona"]
        path = self.ctx.creds.resolve("SEONA_DECAY_PATH", secret=False) or "/api/decay/queue"
        p = item.params or {}
        if dry_run:
            return _dry(item, f"queue a Seona decay refresh for {p.get('url') or item.brand}",
                        {"endpoint": f"{base}{path}", "params": p})
        resp = await post_json(base, path, json={"brand": item.brand, **p})
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary="queued decay refresh", result_ref={"response": resp}, cost=_est(item))


class EmakiPublishAdapter(BaseAdapter):
    """Push an HC Viral Hits draft to Emaki as an UNPUBLISHED CMS draft (never
    sets featured image, never goes live) — exactly like an Asana write, one real
    push per approved item (PRD §8)."""

    name = "emaki_publish"
    owner_agent = "production"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        base = self.ctx.settings.endpoints["hc_viral_hits"]
        topic_id = (item.params or {}).get("topic_id")
        if not topic_id:
            raise AdapterUnavailable("emaki_publish requires params.topic_id")
        if dry_run:
            return _dry(item, f"push HC-Viral topic {topic_id} to Emaki as an UNPUBLISHED draft",
                        {"topic_id": topic_id, "unpublished_only": True, "featured_image": False})
        headers = {"X-API-Key": self.ctx.creds.resolve("HC_VIRAL_HITS_API_KEY") or ""}
        resp = await post_json(base, f"/api/topics/{topic_id}/emaki-publish", headers=headers)
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"pushed topic {topic_id} to Emaki (unpublished draft)",
                            result_ref={"response": resp})


# ---------------------------------------------------------------------------
# Reporting & Distribution actions
# ---------------------------------------------------------------------------

class DigestAssembleAdapter(BaseAdapter):
    """Assemble the per-brand daily digest HTML from memory (no external send).
    Produces an artifact + a report entry pointing at it."""

    name = "digest_assemble"
    owner_agent = "reporting"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        if dry_run:
            return _dry(item, f"assemble the {item.brand} daily digest artifact from memory",
                        {"brand": item.brand})
        metrics = await self.ctx.store.query(brand=item.brand, types=[EntryType.METRIC],
                                             fresh_within_seconds=2 * 24 * 3600, limit=30)
        html_doc = _digest_html(item.brand, metrics)
        pointer = ArtifactStore().put_text(brand=item.brand, kind="digest", ext="html",
                                           text=html_doc, content_type="text/html")
        entry = EntryDraft(type=EntryType.REPORT, brand=item.brand, source_agent="reporting",
                           source_system="daily_reporting",
                           payload={"kind": "daily_digest", "artifact_ref": pointer,
                                    "metric_entries": [m.id for m in metrics]})
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"assembled digest ({pointer['bytes']} bytes)",
                            result_ref={"artifact_ref": pointer}, entries=[entry], cost=_est(item))


class DigestSendAdapter(BaseAdapter):
    """Send the daily digest email via Gmail (gmail.send). Human-approval-gated,
    dry-run by default — the ONE real send in the distribution surface."""

    name = "digest_send"
    owner_agent = "reporting"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        gmail = self.ctx.creds.gmail_oauth()
        p = item.params or {}
        recipients = p.get("recipients") or [gmail.sender or ""]
        subject = p.get("subject") or f"[{item.brand}] Daily digest — {date.today().isoformat()}"
        if dry_run:
            return _dry(item, f"send digest email to {recipients} from {gmail.sender}",
                        {"recipients": recipients, "subject": subject, "sender": gmail.sender})
        if not gmail.refresh_token:
            raise AdapterUnavailable("Gmail credentials not configured")
        body_html = p.get("body_html") or "<p>See attached digest.</p>"
        msg_id = await _gmail_send(gmail, recipients, subject, body_html)
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary=f"sent digest email {msg_id}", result_ref={"message_id": msg_id})


class NewsletterAssembleAdapter(BaseAdapter):
    """Assemble the CarBuzz newsletter draft HTML via the newsletter service
    (draft-only; the human copies the HTML out and sends it themselves)."""

    name = "newsletter_assemble"
    owner_agent = "reporting"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        base = self.ctx.creds.resolve("NEWSLETTER_API_URL", secret=False) or "http://localhost:5200"
        if dry_run:
            return _dry(item, "assemble the CarBuzz newsletter draft (HTML) via the newsletter service",
                        {"endpoint": f"{base}/api/newsletter/compile"})
        resp = await post_json(base, "/api/newsletter/compile", json=(item.params or {}).get("content", {}))
        html_doc = resp.get("html") if isinstance(resp, dict) else str(resp)
        pointer = ArtifactStore().put_text(brand=item.brand, kind="newsletter", ext="html",
                                           text=html_doc or "", content_type="text/html")
        entry = EntryDraft(type=EntryType.DISTRIBUTION_DRAFT, brand=item.brand, source_agent="reporting",
                           source_system="newsletter",
                           payload={"kind": "newsletter_draft", "status": "assembled",
                                    "artifact_ref": pointer})
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary="assembled newsletter draft (HTML)", result_ref={"artifact_ref": pointer},
                            entries=[entry], cost=_est(item))


class SocialAssembleAdapter(BaseAdapter):
    """Assemble social-post draft artifacts (captions + image spec) via the social
    service (draft-only; the human downloads PNGs and posts them themselves)."""

    name = "social_assemble"
    owner_agent = "reporting"

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        base = self.ctx.creds.resolve("SOCIAL_API_URL", secret=False) or "http://localhost:3145"
        p = item.params or {}
        if dry_run:
            return _dry(item, "assemble social post captions+images via the social service",
                        {"endpoint": f"{base}/api/generate", "params": p})
        resp = await post_json(base, "/api/generate", json=p)
        import json as _json

        pointer = ArtifactStore().put_text(brand=item.brand, kind="social", ext="json",
                                           text=_json.dumps(resp, default=str),
                                           content_type="application/json")
        entry = EntryDraft(type=EntryType.DISTRIBUTION_DRAFT, brand=item.brand, source_agent="reporting",
                           source_system="social",
                           payload={"kind": "social_draft", "status": "assembled", "artifact_ref": pointer,
                                    "note": "captions assembled; render/download PNGs in the social app"})
        return ActionResult(ok=True, dry_run=False, action_type=item.action_type,
                            summary="assembled social draft (captions)", result_ref={"artifact_ref": pointer},
                            entries=[entry], cost=_est(item))


# -- helpers ----------------------------------------------------------------

def _digest_html(brand: str, metrics: list[Any]) -> str:
    rows = []
    for m in metrics:
        kind = (m.payload or {}).get("kind", "")
        rows.append(f"<tr><td>{html.escape(kind)}</td><td>{html.escape(str(m.source_system))}</td></tr>")
    return (
        f"<html><body><h2>{html.escape(brand)} — daily digest ({date.today().isoformat()})</h2>"
        f"<p>Assembled by Switchboard from {len(metrics)} memory metric(s). "
        f"Draft for human review — not sent.</p>"
        f"<table border='1' cellpadding='6'><tr><th>signal</th><th>source</th></tr>{''.join(rows)}</table>"
        f"</body></html>"
    )


async def _gmail_send(gmail, recipients: list[str], subject: str, body_html: str) -> str:
    """Send one email via the Gmail API using gmail.send-scoped OAuth creds."""
    try:
        import base64
        from email.mime.text import MIMEText

        from google.oauth2.credentials import Credentials as UserCreds  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AdapterUnavailable("google-api-python-client not installed (pip install .[data])") from exc

    creds = UserCreds(
        None, refresh_token=gmail.refresh_token, token_uri=gmail.token_uri,
        client_id=gmail.client_id, client_secret=gmail.client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    import asyncio

    def _send() -> str:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        mime = MIMEText(body_html, "html")
        mime["to"] = ", ".join(recipients)
        mime["from"] = gmail.sender or "me"
        mime["subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return sent.get("id", "")

    return await asyncio.to_thread(_send)
