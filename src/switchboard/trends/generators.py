"""Content-generation transports for the trend pipeline (docs/trend-pipeline.md).

One entry point — :func:`generate` — dispatches on the job's transport:

* ``llm``            built-in governed drafting via LLMClient (always available)
* ``hc_viral_hits``  force-add-from-url → angle → brief → full pipeline → draft
* ``social_api``     social-media-posts-creator ``POST /api/generate``
* ``newsletter_api`` newsletter-creator-auto ``POST /api/article/process``
* ``shellagent_run`` generic ShellAgent workflow contract (``POST {url}/run``)

All previews land as markdown artifacts so the console can render one shape.
Generation is draft-only by design; publishing is a separate, human-gated step
(pipeline.py). A transport failure marks the job failed with a readable error —
the editor can regenerate on the ``llm`` fallback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..adapters._http import post_json
from ..adapters.base import AdapterUnavailable
from ..adapters.clients.llm import LLMClient
from ..context import RunContext
from ..db.enums import EntryType
from ..db.models import ContentJob, ContentPipeline, Trend
from ..logging_ import get_logger

log = get_logger("trends.generators")

TRANSPORTS = ("llm", "hc_viral_hits", "social_api", "newsletter_api", "shellagent_run")


@dataclass
class GenerationResult:
    ok: bool
    pending: bool = False                 # external pipeline still running — re-poll later
    preview_markdown: str = ""
    preview_meta: dict[str, Any] = field(default_factory=dict)
    external_ref: dict[str, Any] = field(default_factory=dict)
    cost_micros: int = 0
    error: str | None = None


# -- the brief -------------------------------------------------------------------

_CONTENT_ASKS = {
    "article": (
        "Write a complete, publication-ready news article draft (700–900 words) in Markdown. "
        "Start with three headline options as a bulleted list, then '## SEO' with a suggested "
        "SEO title and meta description, then the article body with H2 section headings, "
        "then '## Sources' listing every source URL used."
    ),
    "social_post": (
        "Write social media post options in Markdown: a '## Instagram' section with 3 caption "
        "options, '## Facebook' with 3 caption options, '## X/Twitter' with 3 post options "
        "(≤280 chars each), and '## Pinterest' with 2 options. Each caption must stand alone "
        "and include a hook. No hashtag spam (≤3 hashtags each)."
    ),
    "newsletter_blurb": (
        "Write newsletter content in Markdown: '## Subject lines' (3 options), '## Lead' "
        "(a 3-sentence lead with a why-it-matters line), and '## Brief' (a 2-sentence teaser "
        "with a click-through hook)."
    ),
    "video_script": (
        "Write a 45–60 second short-form video script in Markdown: '## Hook' (first 3 seconds), "
        "'## Script' as a two-column style list of SHOT — VO lines, and '## CTA'. "
        "Conversational, punchy, no invented footage of real events."
    ),
}


def build_brief(trend: Trend, pipeline: ContentPipeline, job: ContentJob,
                verified_facts: list[str], pending_claims: list[str]) -> str:
    """The generation brief every transport receives. Verified facts are
    separated from unverified claims so no generator states a claim as fact."""
    dossier = trend.dossier or {}
    lines: list[str] = [
        f"TREND: {trend.headline}",
        f"BRAND: {pipeline.brand}",
        f"CONTENT TYPE: {job.content_type}",
        "",
        "SUMMARY:",
        dossier.get("summary") or trend.summary or "(no dossier — rely on the evidence below)",
    ]
    if dossier.get("timeline"):
        lines += ["", "TIMELINE:"] + [f"- {t}" for t in dossier["timeline"][:8]]
    if verified_facts:
        lines += ["", "VERIFIED FACTS (search-confirmed — safe to state):"]
        lines += [f"- {f}" for f in verified_facts[:8]]
    if pending_claims:
        lines += ["", "UNVERIFIED CLAIMS (attribute to reports; never state as fact):"]
        lines += [f"- {c}" for c in pending_claims[:8]]
    if dossier.get("angles"):
        lines += ["", "SUGGESTED ANGLES:"]
        lines += [f"- {a.get('title', '')}: {a.get('rationale', '')}" for a in dossier["angles"][:5]]
    evidence = trend.evidence or []
    if evidence:
        lines += ["", "COMPETITOR COVERAGE / SOURCES:"]
        lines += [f"- {e.get('source', '?')}: {e.get('title', '')} — {e.get('url', '')}"
                  for e in evidence[:10]]
    instructions = " ".join(x for x in [pipeline.instructions, job.instructions] if x)
    if instructions:
        lines += ["", f"EDITOR INSTRUCTIONS (must follow): {instructions}"]
    lines += ["", "TASK:", _CONTENT_ASKS.get(job.content_type, _CONTENT_ASKS["article"])]
    return "\n".join(lines)


async def gather_fact_context(ctx: RunContext, trend: Trend) -> tuple[list[str], list[str]]:
    """(verified_facts, pending_claims) for this trend from shared memory."""
    claims = await ctx.store.query(
        brand=trend.brand, types=[EntryType.CLAIM],
        payload_contains={"kind": "trend_key_fact", "trend_id": trend.id}, limit=20,
    )
    pending = [c.payload.get("statement", "") for c in claims if c.payload]
    dossier_statements = {s for s in pending if s}
    for f in (trend.dossier or {}).get("key_facts", []):
        if isinstance(f, dict) and f.get("statement"):
            dossier_statements.add(f["statement"])
    facts = await ctx.store.query(
        brand=trend.brand, types=[EntryType.FACT], verified=True,
        payload_contains={"kind": "verified_fact"}, fresh_within_seconds=7 * 24 * 3600, limit=50,
    )
    verified = [f.payload.get("statement", "") for f in facts
                if f.payload and f.payload.get("statement") in dossier_statements]
    pending = [s for s in pending if s and s not in set(verified)]
    return verified, pending


# -- dispatch --------------------------------------------------------------------

async def generate(ctx: RunContext, job: ContentJob, pipeline: ContentPipeline,
                   trend: Trend) -> GenerationResult:
    transport = job.transport
    try:
        if transport == "hc_viral_hits":
            return await _gen_hc_viral(ctx, job, pipeline, trend)
        if transport == "social_api":
            return await _gen_social_api(ctx, pipeline, trend)
        if transport == "newsletter_api":
            return await _gen_newsletter_api(ctx, trend)
        verified, claims = await gather_fact_context(ctx, trend)
        brief = build_brief(trend, pipeline, job, verified, claims)
        if transport == "shellagent_run":
            return await _gen_shellagent(ctx, job, brief)
        return await _gen_llm(ctx, job, brief)
    except AdapterUnavailable as exc:
        return GenerationResult(ok=False, error=f"{transport} unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 — a transport bug must not kill the worker
        log.warning("[generate] %s transport failed for job %s: %s", transport, job.id, exc)
        return GenerationResult(ok=False, error=f"{transport} failed: {exc}",
                                external_ref=dict(job.external_ref or {}))


# -- transports ------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are a senior editor at a major automotive publisher writing for the {brand} "
    "audience. You write clean, factual, engaging drafts. Never invent facts, quotes, "
    "or numbers; only use what the brief provides. Attribute unverified claims "
    "(\"according to <outlet>\"). Output Markdown only."
)

_LLM_MAX_TOKENS = {"article": 3000, "social_post": 1200, "newsletter_blurb": 800,
                   "video_script": 1500}


async def _gen_llm(ctx: RunContext, job: ContentJob, brief: str) -> GenerationResult:
    llm = LLMClient(ctx)
    result = await llm.complete(
        system=_LLM_SYSTEM.format(brand=job.pipeline.brand if job.pipeline else "portfolio"),
        prompt=brief,
        model=ctx.settings.models.default,
        max_tokens=_LLM_MAX_TOKENS.get(job.content_type, 2000),
        agent="trend_pipeline",
    )
    text = result.text.strip()
    if not text:
        return GenerationResult(ok=False, error="LLM returned an empty draft")
    return GenerationResult(
        ok=True, preview_markdown=text,
        preview_meta={"title": _first_heading(text), "word_count": len(text.split()),
                      "generator": "switchboard-llm", "model": ctx.settings.models.default},
        cost_micros=result.micros,
    )


async def _gen_social_api(ctx: RunContext, pipeline: ContentPipeline,
                          trend: Trend) -> GenerationResult:
    """social-media-posts-creator: needs {title, bodyText, url}."""
    base = ctx.settings.endpoints.get("social")
    dossier = trend.dossier or {}
    body_text = dossier.get("summary") or trend.summary or trend.headline
    url = next((e.get("url") for e in (trend.evidence or []) if e.get("url")), None)
    data = await post_json(base, "/api/generate",
                           json={"title": trend.headline, "bodyText": body_text, "url": url})
    captions = data.get("captions") or {}
    excerpts = data.get("excerpts") or []
    if not captions:
        return GenerationResult(ok=False, error="social API returned no captions")
    md = [f"# Social posts — {trend.headline}", ""]
    for platform, opts in captions.items():
        md.append(f"## {platform}")
        md += [f"{i}. {o}" for i, o in enumerate(opts if isinstance(opts, list) else [opts], 1)]
        md.append("")
    if excerpts:
        md += ["## On-image excerpts (verbatim)"] + [f"- {e}" for e in excerpts]
    return GenerationResult(
        ok=True, preview_markdown="\n".join(md),
        preview_meta={"title": f"Social posts — {trend.headline}", "generator": "social_api",
                      "raw": {"captions": captions, "excerpts": excerpts}},
    )


async def _gen_newsletter_api(ctx: RunContext, trend: Trend) -> GenerationResult:
    """newsletter-creator-auto: process one article URL into newsletter fields."""
    base = ctx.settings.endpoints.get("newsletter")
    url = next((e.get("url") for e in (trend.evidence or []) if e.get("url")), None)
    if not url:
        raise AdapterUnavailable("newsletter transport needs at least one evidence URL")
    data = await post_json(base, "/api/article/process",
                           json={"url": url, "role": "brief", "title": trend.headline},
                           timeout=120.0)
    fields_md = "\n".join(f"- **{k}**: {v}" for k, v in data.items()
                          if isinstance(v, (str, int, float)) and k != "warnings")
    return GenerationResult(
        ok=True,
        preview_markdown=f"# Newsletter blurb — {trend.headline}\n\n{fields_md}",
        preview_meta={"title": f"Newsletter blurb — {trend.headline}",
                      "generator": "newsletter_api", "raw": data},
    )


async def _gen_shellagent(ctx: RunContext, job: ContentJob, brief: str) -> GenerationResult:
    """Generic ShellAgent Workflow contract: POST {url}/run, Bearer token,
    {"input": brief} → {"output": "<string>"} (workspace CLAUDE.md)."""
    url, token = ctx.creds.trend_agent(job.content_type)
    if not url:
        raise AdapterUnavailable(
            f"TREND_AGENT_{job.content_type.upper()}_URL not configured")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = await post_json(url, "/run", json={"input": brief}, headers=headers, timeout=290.0)
    output = (data or {}).get("output") if isinstance(data, dict) else None
    if not output:
        err = (data or {}).get("error") if isinstance(data, dict) else None
        return GenerationResult(ok=False, error=f"agent returned no output: {err or data}")
    return GenerationResult(
        ok=True, preview_markdown=str(output),
        preview_meta={"title": _first_heading(str(output)) or f"{job.content_type} draft",
                      "generator": "shellagent_run", "agent_url": url},
    )


# -- hc-viral-hits (article path with real CMS hand-off) --------------------------

_HCV_POLL_SECONDS = 8
_HCV_POLL_ATTEMPTS = 12       # ~96s in-process; the job sweep resumes if still pending
_HCV_DEADLINE_MINUTES = 45    # give up on the external pipeline after this


async def _gen_hc_viral(ctx: RunContext, job: ContentJob, pipeline: ContentPipeline,
                        trend: Trend) -> GenerationResult:
    """Drive hc-viral-hits end to end: force-add-from-url → top angle → brief →
    full pipeline → wait for the topic to reach the *ready* CMS queue (post
    line-edit/fact-check) → fetch the draft. Progress is checkpointed into
    external_ref after each irreversible step, so a transient failure resumes
    instead of re-running paid intake; if the external pipeline is still running
    when in-process polling ends, we return pending and the job sweep resumes."""
    try:
        import httpx  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AdapterUnavailable("httpx not installed") from exc

    base = (ctx.settings.endpoints.get("hc_viral_hits") or "").rstrip("/")
    api_key = ctx.creds.resolve("HC_VIRAL_HITS_API_KEY")
    if not base or not api_key:
        raise AdapterUnavailable("hc_viral_hits endpoint/API key not configured")
    brand = pipeline.brand
    ref = dict(job.external_ref or {})

    if "topic_id" not in ref or not ref.get("pipeline_started"):
        email = ctx.creds.resolve("HC_VIRAL_HITS_LOGIN_EMAIL", secret=False)
        password = ctx.creds.resolve("HC_VIRAL_HITS_LOGIN_PASSWORD")
        if not email or not password:
            raise AdapterUnavailable(
                "HC_VIRAL_HITS_LOGIN_EMAIL/_PASSWORD not configured (the intake "
                "endpoints are session-authed)")
        primary = next((e.get("url") for e in (trend.evidence or []) if e.get("url")), None)
        if not primary:
            raise AdapterUnavailable("hc_viral transport needs at least one evidence URL")
        try:
            async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
                resp = await client.post("/api/auth/login",
                                         json={"email": email, "password": password})
                resp.raise_for_status()
                # Brand-scoped writes run under the session's active brand — a
                # failed switch would silently create content for the WRONG brand.
                brand_resp = await client.post("/api/auth/active-brand",
                                               json={"brand_slug": brand})
                if brand_resp.status_code >= 400:
                    return GenerationResult(
                        ok=False, external_ref=ref,
                        error=f"hc-viral active-brand '{brand}' failed "
                              f"({brand_resp.status_code}) — refusing to run under the "
                              "session's default brand")
                if "topic_id" not in ref:
                    # ManualSource objects, primary excluded (the API skips repeats
                    # but the slot is better spent on a distinct outlet).
                    extra = [{"url": e["url"]} for e in (trend.evidence or [])
                             if e.get("url") and e["url"] != primary][:6]
                    resp = await client.post("/api/topics/force-add-from-url",
                                             json={"url": primary, "additional_sources": extra})
                    resp.raise_for_status()
                    intake = resp.json()
                    angles = intake.get("angles") or []
                    if not angles:
                        return GenerationResult(ok=False,
                                                error="hc-viral produced no angles for this URL")
                    angle_id = angles[0].get("id") or angles[0].get("angle_id")
                    brief_payload: dict[str, Any] = {}
                    extra_ids = intake.get("additional_raw_item_ids") or []
                    if extra_ids:
                        brief_payload["attach_raw_item_ids"] = extra_ids
                    resp = await client.post(f"/api/angles/{angle_id}/brief", json=brief_payload)
                    resp.raise_for_status()
                    topic = resp.json()
                    topic_id = topic.get("topic_id") or topic.get("id")
                    if not topic_id:
                        return GenerationResult(ok=False, error=f"no topic id from brief: {topic}")
                    # Checkpoint before the pipeline kickoff: a failure past this
                    # point resumes from the existing topic instead of paying for
                    # a duplicate intake.
                    ref = {"topic_id": topic_id, "angle_id": angle_id, "source_url": primary,
                           "started_at": datetime.now(timezone.utc).isoformat()}
                resp = await client.post(f"/api/pipeline/full/{ref['topic_id']}", json={})
                resp.raise_for_status()
                ref["pipeline_started"] = True
        except (httpx.HTTPError, OSError) as exc:
            return GenerationResult(ok=False, external_ref=ref,
                                    error=f"hc-viral intake failed: {exc}")

    started = parse_iso(ref.get("started_at")) or datetime.now(timezone.utc)
    deadline_hit = (datetime.now(timezone.utc) - started) > timedelta(minutes=_HCV_DEADLINE_MINUTES)

    # Wait on the API-key CMS surface. The publish queue only lists topics whose
    # pipeline finished (status=ready — after line edit + fact check), so we never
    # capture a pre-edit draft as the human-review preview.
    headers = {"X-API-Key": api_key}
    async with httpx.AsyncClient(base_url=base, timeout=30.0, headers=headers) as client:
        for _ in range(_HCV_POLL_ATTEMPTS):
            queue_resp = await client.get("/api/cms/drafts",
                                          params={"brand": brand, "status": "ready"})
            if queue_resp.status_code in (401, 403):
                return GenerationResult(ok=False, external_ref=ref,
                                        error=f"hc-viral CMS auth error {queue_resp.status_code} "
                                              "(check HC_VIRAL_HITS_API_KEY)")
            if queue_resp.status_code == 200:
                data = queue_resp.json()
                drafts = data if isinstance(data, list) else data.get("drafts", [])
                if any(d.get("topic_id") == ref["topic_id"] for d in drafts):
                    resp = await client.get(f"/api/cms/drafts/{ref['topic_id']}",
                                            params={"brand": brand})
                    if resp.status_code == 200:
                        return _hcv_draft_result(resp.json(), trend, ref)
            await asyncio.sleep(_HCV_POLL_SECONDS)
    if deadline_hit:
        return GenerationResult(
            ok=False, external_ref=ref,
            error=f"hc-viral topic {ref['topic_id']} not ready after "
                  f"{_HCV_DEADLINE_MINUTES} min — check its pipeline in hc-viral-hits")
    return GenerationResult(ok=True, pending=True, external_ref=ref)


def _hcv_draft_result(draft: dict[str, Any], trend: Trend, ref: dict[str, Any]) -> GenerationResult:
    html = draft.get("html") or ""
    md = [f"# {draft.get('title', trend.headline)}", "",
          f"*SEO title: {draft.get('seo_title', '')} · slug: {draft.get('slug', '')}*",
          "", html]
    if draft.get("sources_list"):
        md += ["", "## Sources"] + [f"- {s}" for s in draft["sources_list"]]
    return GenerationResult(
        ok=True, preview_markdown="\n".join(md),
        preview_meta={"title": draft.get("title", ""), "generator": "hc_viral_hits",
                      "word_count": draft.get("word_count"),
                      "seo_title": draft.get("seo_title"),
                      "excerpt": draft.get("excerpt")},
        external_ref=ref,
    )


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("# ").strip()
        if line:
            return line[:120]
    return ""
