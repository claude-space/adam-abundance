# Competitor Trend Pipeline

**Status:** implemented (this document is the architecture reference).
**Scope:** trend sourcing → detection → human-approved trigger → content generation
→ preview review → approve / regenerate / decline → gated publish hand-off.

The pipeline turns the competitor signal Switchboard already observes (the
Daily-Agent-Report-style RSS coverage, Similarweb, HC-Viral) plus new external
sources (Tavily, Perplexity, Firecrawl, NewsAPI) into an **event-driven,
human-gated content pipeline**. It follows every existing Switchboard rule:

- Agents/services coordinate through **shared memory** (`memory_entry`) — the
  scout also writes `flag`/`context` entries so the morning planner sees trends.
- **Nothing external happens without a recorded human approval.** The trigger
  request is approve/decline; every generated preview is approve/regenerate/
  reject; publishing is a second, explicit, confirmed gate.
- **Kill switch + spend caps** apply: generation and publishing are refused when
  `SWITCHBOARD_KILL_SWITCH=1`; all LLM spend is metered through the governor.
- **Distribution stays draft + human-send.** "Publish" means: push an
  *unpublished* Emaki CMS draft via the existing sanctioned HC-Viral path, or
  mark the artifact ready for manual hand-off. Switchboard never posts to a
  social network, never sends email from this pipeline, never publishes live.

## Flow

```
       (scheduler: trend_scan feeder, every TREND_SCAN_INTERVAL_MIN)
  Tavily · Perplexity · Firecrawl · NewsAPI · competitor RSS (existing)
        │  trend_signal context entries (shared memory)
        ▼
  TrendDetector — cluster (token/entity), score (outlets, velocity,
        │          watchlist, coverage-gap, breaking terms), dedupe
        ▼
  trend row (status=detected) ──► dossier build (Tavily deep + Firecrawl
        │                         extracts + Perplexity + LLM synthesis;
        │                         key facts written as CLAIMs → fact-gate)
        ▼
  content_pipeline row (pending_approval)  ──►  Slack notify (approve link)
        │
   HUMAN: approve (pick content types + instructions) / decline
        ▼
  content_job per content type (queued → running → preview_ready)
        │   transports: llm (built-in, default) · hc_viral_hits ·
        │   social_api · newsletter_api · shellagent_run (generic /run)
        ▼
   HUMAN per preview: approve → publish gate (confirm; Emaki unpublished
        draft or manual hand-off)  ·  regenerate with new instructions
        (attempt history kept)  ·  reject
        ▼
  pipeline → published / partially_published / closed;
  DECISION + REPORT memory entries record the outcome.
```

## Data model (migration `0003_trend_pipeline`)

Three tables; all lifecycle columns are TEXT (matching plan/plan_item style).

- **`trend`** — one clustered competitor story/topic. `brand`, `cluster_key`
  (stable dedupe key), `headline`, `summary`, `score` + `score_breakdown`
  (explainable scoring), `velocity`, `source_count`, `signal_count`,
  `covered_by_us`/`coverage_gap`, `entities`, `evidence` (list of
  {origin, source, title, url, published_at}), `dossier` JSONB + `dossier_ref`
  artifact pointer, `status`
  (`detected → dossier_building → proposed → approved | declined | dismissed |
  expired | completed`), `first_seen_at`, `last_seen_at`, `expires_at`.
- **`content_pipeline`** — one trigger request. `trend_id`, `brand`, `status`
  (`pending_approval → approved → generating → previews_ready →
  published | partially_published | declined | closed | failed | expired`),
  `requested_by`, `approved_by/at`, `declined_by/at`, `close_reason`,
  `instructions`, `content_types`, `events` (audit timeline JSONB).
- **`content_job`** — one generator invocation per content type.
  `pipeline_id`, `content_type` (`article | social_post | newsletter_blurb |
  video_script`), `transport`, `status` (`queued → running → preview_ready →
  approved → published | rejected | failed | cancelled`), `attempt`,
  `instructions`, `history` (previous attempts + their previews), `preview_ref`
  (artifact pointer), `preview_meta`, `external_ref` (e.g. hc-viral topic id),
  `result_ref` (publish outcome), `cost`, `error`.

Human-approval invariants mirror `PlanRepo`: approver must be a real user
(never empty/`system`/`trend_scout`/`orchestrator`), transitions are validated,
and RBAC (`can_approve(role, brands, brand)`) gates every mutating route.

## Sourcing

New thin async clients in `adapters/clients/` (soft-fail
`AdapterUnavailable` when the key is missing): `tavily.py`, `perplexity.py`,
`firecrawl.py`, `newsapi.py`. New read adapters in `adapters/trend_sources.py`,
owned by **research** (its web/news domain), each writing one portfolio-scoped
`context` entry `kind="trend_signals"` with normalized items. The detector also
consumes the existing `competitor_coverage` RSS entries, so the Daily-Agent-
Report competitor feed set participates in clustering for free.

Env keys (names only, in `~/.claude/.env` / `switchboard.env`):
`TAVILY_API_KEY`, `PERPLEXITY_API_KEY`, `FIRECRAWL_API_KEY`, `NEWSAPI_API_KEY`.

## Detection & scoring

`trends/detector.py` is pure/deterministic (unit-tested without a DB):
tokenize titles, cluster by Jaccard overlap or shared entity anchor (OEM/model
regexes), score 0–100 with an explainable breakdown:

| factor | signal |
|---|---|
| outlet breadth | distinct competitor outlets covering the story |
| velocity | signals per hour since first sighting |
| watchlist | `TREND_WATCHLIST` term match (per-brand boost) |
| coverage gap | we have not published a matching story (our news sitemaps) |
| breaking terms | recall/reveal/lawsuit/crash/… regex |
| engagement | comment/social counts when a source provides them |

Dedupe: a new cluster matching an existing open/dismissed trend's
`cluster_key` (or high token overlap) within `TREND_DEDUP_DAYS` updates that
trend instead of re-proposing it.

## Dossier

`trends/dossier.py` — "collect everything we can": Tavily advanced search,
Firecrawl extraction of top evidence URLs, Perplexity summary, then one
synthesis-model LLM pass producing summary/timeline/key facts/angles/suggested
content types. Key facts are also written as `claim` entries with
`needs_verification=true` so the **existing Research fact-gate** verifies them;
generation prompts label verified facts vs. unverified claims. The rendered
dossier is stored via `ArtifactStore` and linked on the trend.

## Pipeline engine

`trends/pipeline.py` + `trends/repo.py`. Jobs are queued in the DB; the web
process fast-paths them via FastAPI `BackgroundTasks`, and the scheduler
(`pipeline_jobs` sweep, every 2 min) is the cross-process fallback — claims use
`UPDATE … WHERE status='queued'` so double-processing can't happen. Generation
respects the kill switch and LLM caps. Every preview lands as an artifact plus
a `distribution_draft` memory entry (`kind="trend_content_draft"`), so the
existing Distribution page and memory browser see pipeline output too.

Transports (per content type, env-selectable `TREND_TRANSPORT_<TYPE>`):

- `llm` (default, works out of the box) — Switchboard's own governed LLMClient
  drafts the article/social captions/newsletter blurb/video script.
- `hc_viral_hits` — `POST /api/topics/force-add-from-url` → top angle → brief →
  `pipeline/full` → poll → `GET /api/drafts/{id}`; publish =
  `emaki-publish` (unpublished CMS draft, the sanctioned path).
- `social_api` — social-media-posts-creator `POST /api/generate`.
- `newsletter_api` — newsletter-creator-auto `POST /api/article/process`.
- `shellagent_run` — generic ShellAgent Workflow contract
  (`POST {url}/run`, Bearer token, `{"input": …}` → `{"output": …}`) for any
  future agent; env `TREND_AGENT_<TYPE>_URL` / `TREND_AGENT_<TYPE>_TOKEN`.

Regeneration: each regenerate records the prior attempt (instructions +
preview pointer) in `history`, bumps `attempt`, and re-queues with cumulative
editor instructions. Rejection and pipeline decline/close record actor + reason
in the audit timeline.

## Notifications

`orchestrator/slack.py` gains a generic `post_message()` (same gate:
`SLACK_NOTIFY_ENABLED=1`, per-brand `SLACK_BOT_TOKEN_<BRAND>` /
`SLACK_CHANNEL_ID_<BRAND>`, optional `SLACK_CHANNEL_ID_TRENDS` override) and
`notify_trend_event()` used for: trigger request created, pipeline approved/
declined, previews ready, content published/handed off. When Slack is not
wired (current production posture) everything degrades to log-only.

## Console

- **`/trends`** — Trend Radar: KPI tiles, open trends ranked by score with
  badges (coverage gap, breaking, watchlist), pending trigger requests with
  inline Approve/Decline, "Scan now" button, manual "Add trend" form (URL or
  topic — the pipeline treats it exactly like a detected trend).
- **`/trends/{id}`** — evidence list, score breakdown, dossier, verified facts
  vs. claims, pipeline history, trigger form (content types + brand +
  instructions; "Create & approve" fast path for approvers).
- **`/pipelines`**, **`/pipelines/{id}`** — job cards with preview links
  (served from `/artifacts/...`), per-job Approve → Publish (confirm dialog),
  Regenerate (instructions textarea), Reject; pipeline-level Decline/Close.
- `/api/data` and `POST /run` include trend/pipeline counts (both stay
  read-only). Systems page lists the new sources; the trend scout appears with
  the feeders.

## Ops & config

- Scheduler jobs: one portfolio-wide `trend_scan:portfolio` (every
  `TREND_SCAN_INTERVAL_MIN`, default 120), the `pipeline_jobs` sweep (2 min),
  and `trend_expire` (hourly, runs even when scans are disabled) — perishable
  trends expire after `TREND_TTL_HOURS` (default 48) if nobody acts, and their
  pending trigger requests expire with them.
- CLI: `switchboard feed trend_scan <brand>`, `switchboard trend-scan
  [portfolio|<brand>]`, `switchboard pipeline-worker`.
- Caps: at most `TREND_MAX_OPEN_PIPELINES` (default 5) open trigger requests
  per brand — the scout stops proposing (and flags) beyond that.
- All knobs in `TrendConfig` (see `.env.example`): `TREND_PIPELINE_ENABLED`,
  `TREND_SCORE_THRESHOLD`, `TREND_MIN_SOURCES`, `TREND_WATCHLIST`,
  `TREND_AUTO_DOSSIER`, `TREND_DEFAULT_CONTENT_TYPES`, `TREND_DEDUP_DAYS`,
  transports and agent URLs as above.

## Extras beyond the original ask

- Explainable scoring (`score_breakdown` surfaced in the UI).
- Coverage-gap detection against our own news sitemaps (gap = boost + badge).
- Fact-gate integration: dossier claims are verified by the Research agent;
  prompts separate verified facts from claims.
- Watchlist boosts, dedupe/snooze, trend expiry (breaking news is perishable).
- Manual trend entry (editor-pasted URL/topic rides the same pipeline).
- Per-attempt regeneration history and a full audit timeline on each pipeline.
- Cost metering per job + governor caps + kill-switch refusal paths.
- Morning-cycle integration: high-score trends also land as `flag` entries so
  the daily plan proposes a `notify` item — the two surfaces stay consistent.
