# Switchboard — Product Requirements Document

**Working codename:** Switchboard (rename freely)
**Owner:** Valnet Auto portfolio (HotCars, CarBuzz, TopSpeed)
**Status:** Draft for build
**Audience:** Claude Code (implementation agent) + the engineer supervising it

---

## 0. How to use this document

This PRD is a build spec, not a finished design you should improvise on. Build it **phase by phase** (Section 12). After each phase, run that phase's **acceptance criteria** before moving on. Two sections are load-bearing and must not be violated even if a shortcut seems attractive: **Section 3 (Non-goals)** and **Section 8 (Governor & safety)**. If a requirement here conflicts with something you find in the existing repos, stop and surface the conflict rather than guessing.

The single most important architectural rule: **agents coordinate only through shared memory. They never call each other and never call each other's tools.** Everything else follows from that.

---

## 1. Overview

Switchboard is a thin **orchestration + shared-state layer** that sits on top of the Auto portfolio's *existing* tools and agents and coordinates a small team of specialized agents against them. It is not a rewrite of any existing system. It does three things the current setup can't:

1. Gives a **single shared memory** that every agent reads from and writes to, with typed entries and provenance.
2. Runs an **orchestrator** that each morning synthesizes cross-system state, proposes a prioritized plan, and — after a human approves — dispatches work.
3. Enforces a **governor**: budget caps, dry-run defaults, human-approval gates, and provenance rules, so an always-on multi-agent loop can't quietly burn money or push bad work to production.

The portfolio already has strong, deliberately human-in-the-loop systems: SEO/Discover ideation (Claude Albert), an SEO fork with a ranking-decay scanner and Update Strategist (Seona), an AI writer, an Asana outline reviewer, a second viral-trend ideation + AI-writer + CMS-draft pipeline (HC Viral Hits), a content-depth auditor, a writer-performance monitor (writers-dashboard), a daily editorial email digest, a newsletter builder, a social-post generator, and a paid-media spend/ROI tracker. Switchboard **wraps** these — spanning editorial content, distribution, and paid media — it does not replace them.

---

## 2. Goals

- One shared, queryable memory across all agents and existing systems, with provenance and a verified-fact vs. unverified-claim distinction.
- Six specialized worker agents (Research, Opportunity, Production, Analytics, Reporting & Distribution, Paid-Media), each owning one data domain with no overlap.
- An orchestrator that produces a daily prioritized plan for human approval, then dispatches approved work.
- Human-approved **distribution artifacts** — the daily editorial digest, CarBuzz newsletter drafts, and social posts are assembled and surfaced for review; a human does the final send/post (nothing distributes autonomously).
- **Paid-media spend & ROI observability** — ad spend and conversion/lead performance surfaced into the morning plan alongside editorial signals.
- A governor that hard-caps spend (Ahrefs units, LLM tokens, BigQuery bytes), defaults external side effects to dry-run, and requires human sign-off before any production action.
- A centralized credentials/access layer so every integration authenticates through one place.
- Wrap existing systems as tools/adapters; reuse, don't rebuild.
- Full cost and action observability (mirrors the cost tracking the existing systems already do).

**Success looks like:** an editor opens Switchboard in the morning, sees a synthesized cross-brand picture and a proposed plan, approves or edits it in one place, and the approved work flows to the existing systems (Asana tasks, AI-writer routes, decay-refresh queue, etc.) — with spend bounded and every claim traceable to a source.

---

## 3. Non-goals (do NOT build these)

These are explicit guardrails. Violating them is a defect, not a feature.

- **No autonomous execution.** No agent — including the orchestrator — performs a production side effect (Asana write, AI-writer route, Emaki CMS draft-push, digest email send, newsletter/social publish, Slack post beyond internal notifications, publish, spend above a floor) without a recorded human approval.
- **No autonomous distribution.** Distribution tooling *does* exist in this portfolio, but it is deliberately draft-only: the daily-reporting digest, the CarBuzz newsletter builder, and the social-post generator all produce artifacts (HTML, images, captions) that a human reviews and sends/posts themselves. Switchboard wraps these as human-approved artifact tools. It never sends an email, publishes a newsletter, or posts to a social platform autonomously — and it adds no new send/post integration where none exists today.
- **No swarm / consensus framework.** Do not adopt agent "meta-harnesses," swarm topologies, or consensus protocols (e.g. Ruflo/Claude-Flow, mesh/Raft/BFT). This is a small, supervised, explicit system.
- **No agent-to-agent messaging.** Agents share state only through the memory store. No direct calls, no message bus between agents, no one agent invoking another's tools.
- **No rebuilding existing pipelines.** Do not reimplement Claude Albert's or Seona's ideation pipelines, the AI writer, the outline reviewer, HC Viral Hits, the digest/monitoring agents, the ranking-decay scanner, the content-depth auditor, the daily-reporting digest, the newsletter builder, the social-post generator, or the paid-media spend pipeline. Integrate them.
- **No new "org-chart" agents without a tool behind them.** Every worker agent must own a real, existing tool. In particular: **no sales/CRM agent** — there is no CRM tooling in this portfolio. (The Reporting & Distribution and Paid-Media agents added in this revision each wrap real, existing systems, so they satisfy this rule.)
- **No raw filesystem or production-credential access for agents.** All external I/O goes through audited tool adapters; all file artifacts go through an artifact store; secrets live in the credentials layer, never in agent code or prompts.
- **No role-playing multi-agent chat frameworks** (CrewAI/AutoGen-style) as the core orchestration. Prefer an explicit, observable graph or plain orchestration.

---

## 4. Existing systems to integrate (glossary + reuse map)

Switchboard treats each of these as a **tool the agents call**, via a read adapter (writes findings into shared memory) and/or an action adapter (performs a side effect, governor-gated). Do not modify these systems' internals beyond adding thin, read-only or well-scoped API surfaces where needed.

**Note on two error corrections in this revision:** (1) The earlier draft treated "Seona / Claude-Albert" as one system — they are two separate repos in a fork relationship (see below). The AI Writer and Outline Reviewer live in **Claude Albert**; the ranking-decay scanner and Update Strategist live in **Seona**. (2) The earlier draft claimed no social/distribution tooling exists — it does (draft-only). Both are corrected here.

| System (repo) | Stack / location | What Switchboard uses it for | Owning agent |
|---|---|---|---|
| **Claude Albert** (`Calbert`) — Discover/editorial ideation + AI Writer + Outline Reviewer | Python 3.11, FastAPI, SQLAlchemy async, SQLite, Next.js; multi-agent `pipeline.py` (Perf Analyst → Pattern Miner → Opportunity Scout → Feasibility Grader → Brief Writer). BigQuery `pubinsights_ods_data.new_article_analysis`. Cost tables `agent_usage` / `writer_agent_usage` / `agent_event` | Trigger/read ideation runs; read proposed/accepted topics + rejection memory (`BrandMemory`) | Opportunity (trigger), Production (status) |
| **AI Writer** (in Claude Albert, `writer.py`) | Draft state machine: `queued → researching → writing → fact_checking → editing → ready → published / failed`; single-worker loop | Route accepted topics to writer; read draft/queue state | Production |
| **Outline Reviewer** (in Claude Albert, `outline_review.py`) | Polls each brand's Asana "Outline Approval Request" section every 600s; posts feedback + moves task by verdict; **dry-run default** via `ALBERT_OUTLINE_REVIEWER_DRY_RUN` | Read review verdicts/queue depth; surface stuck outlines | Production |
| **Seona** (`seona-albert`) — SEO ideation fork of Claude Albert | Same Albert base + `trafilatura`. GSC via BigQuery (`gsc.<brand>_com_searchdata_url_impression`) + **Ahrefs API v3** (cached in `ahrefs_cache`, 7-day TTL, ~10 units/row). Sweep + Seed ideation modes | Trigger/read SEO ideation; read topic candidates + briefs | Opportunity (trigger), Production (status) |
| **Ranking-decay scanner** (in Seona) | `ranking_decay.py` + `decay_queue.py` + `decay_scheduler.py`; two 14-day windows, pos-delta ≥ 2.0 **and** click-ratio ≤ 0.70, baseline floor; queues pending `update_run` → **Update Strategist** (`update_pipeline.py`) | Scheduled feeder: emit decay candidates into memory | Scheduled job → Analytics/Production |
| **HC Viral Hits** (`hc-viral-hits`) — viral-trend ideation + AI writer + CMS draft-push | Python 3.11, FastAPI, SQLAlchemy async, SQLite, Next.js; RSS/Trends poll → multi-agent Claude pipeline (angle ideator → fact-checker → grader → brief → writer → line-editor) → **Emaki CMS draft** via headless Playwright. Own cost tracking (`AgentEvent`, `compute_cost_cents`). HotCars + TopSpeed-Moto. Forked from gaming `Ideation-Writer` | Read graded angles/topics/drafts + queue state; trigger poll/ideate (governor-gated); push CMS drafts on approval | Opportunity (trigger/read), Production (drafting + Emaki publish) |
| **Content-depth auditor** (`content-depth-auditor`) | FastAPI, Playwright, WeasyPrint, APScheduler; BigQuery `pubinsights_consum_data.auto_new_article_analysis`; Sentinel Pro API; Slack alerts w/ buttons; Sonnet 4.6; per-run cost | Scheduled feeder: emit content-audit findings into memory | Scheduled job → Analytics/Opportunity |
| **Writers-dashboard** (`writers-dashboard`) — writer-performance monitor **(= the "performance monitoring Slack app")** | Node/TS, Next.js, node-cron (`startCron.ts`), `@anthropic-ai/sdk` (Sonnet 4.6); `digestAgent`, `monitoringAgent`, `slackQueryHandler`. BigQuery `pubinsights_consum_data.auto_new_article_analysis` + Google Sheets (writer quotas). Also does quota mgmt, Sheets write-backs, weekly MTD emails | Reuse metric logic; Analytics supersedes its digest/monitor as the performance brain (leave quota mgmt + sheet write-backs in place) | Analytics |
| **Daily-reporting-agent** (`daily-reporting-agent`) — per-brand editorial email digest | Python; Sentinel + BigQuery Discover (`pubinsights_ods_data`) + competitor RSS/Google-News (feedparser, Playwright). Gmail send 10:10am ET (launchd). **No LLM** | Read the daily KPI/Discover/competitor digest into memory; assemble + send digest (human-approved action) | Reporting & Distribution |
| **Newsletter builder** (`newsletter-creator-auto`) — CarBuzz newsletter | Python FastAPI + React/Vite; BigQuery `pubinsights_consum_data` + Claude (Opus 4.5) + MJML → HTML. **Draft-only** (human copies HTML out; no send integration) | Assemble newsletter draft artifact for human review | Reporting & Distribution |
| **Social-post generator** (`social-media-posts-creator`) — IG/FB/Pinterest/TikTok | TS/Next.js; BigQuery `pubinsights_consum_data.auto_new_article_analysis` + Claude (Sonnet 4.6, tool-use + verbatim check) + Playwright render → PNG. **Draft-only** (no platform posting API) | Assemble social-post draft artifacts (images + captions) for human review | Reporting & Distribution |
| **MP spend tracker** (`mp-spend-dashboard`) — paid-media spend/ROI | Python (Cloud Run daily ~7am ET) + Next.js dashboard; Google/Meta/Bing Ads + Sentinel conversion events + Lotlinx/Carzing/Cars&Bids leads → Google Sheets. GCP Secret Manager. **No LLM** | Read ad spend + conversion/ROI into memory; surface spend/ROI context to the plan | Paid-Media |
| **Slack** | Slack bot + webhooks | Notification/approval surface (post briefs, alerts, escalations) | Orchestrator / all (notify only) |

> **Out of scope:** `Ideation-Writer` is a **gaming** ideation/writer platform (owner robert.m), not an Auto system — it is the architectural ancestor HC Viral Hits was forked from. Listed here only to disambiguate; Switchboard does not integrate it.

### External tools & data sources

| Tool | Access | Owning agent | Notes |
|---|---|---|---|
| **BigQuery** (warehouse) | `google-cloud-bigquery`, service account (`GOOGLE_APPLICATION_CREDENTIALS`), project `data-science-458422` | Analytics | Contains the datasets below. **Two article-analysis tables exist** — see §13 |
| **PubInsights (consum)** dataset | `pubinsights_consum_data.auto_new_article_analysis` (ArticleTitle, URL, PubDate, PriCat, Intent, `ActSessSentinel`, AVD, engaged-depth) | Analytics | Primary published-performance source (writers-dashboard, content-auditor, social) |
| **PubInsights (ODS)** dataset | `pubinsights_ods_data.new_article_analysis` | Analytics | Discover-performance source (Claude Albert, daily-reporting-agent) — reconcile with the consum table |
| **GSC exports** | `gsc.<brand>_com_searchdata_url_impression` (per-brand `gsc_table`; **currently empty for the Auto trio — must be populated**) | Opportunity | Search demand + striking-distance |
| **Sentinel (Pro)** | HTTPS API `https://valnet.sentinelpro.com/api/v1/` (`traffic/`, `events/`), `SENTINEL-API-KEY` header, `propertyId` = site domain; visits, averageEngagedDepth, averageEngagedDuration; day/5-min granularity | Analytics / Paid-Media | Reliable day-of sessions/engagement (a real service; `ActSessSentinel` is a BigQuery column derived from it) |
| **Google Sheets** | Google API, service account | Analytics / Paid-Media | Writer quotas/output/baselines; paid-media `RAW_DATA` spend log |
| **Ahrefs** | API v3 (existing client, `https://api.ahrefs.com/v3`), SQLite `ahrefs_cache` (7-day TTL), ~10 units/row | Opportunity | Competitor keywords, SERP, backlinks — **metered; see governor** |
| **Emaki CMS** | Headless Playwright automation of `emakicms.com` (no API); storage-state auth (`.emaki-state.json`) | Production | Push HC Viral Hits article drafts (unpublished) — **action adapter, governor-gated** |
| **Gmail API** | `google-api-python-client`, OAuth token, scope `gmail.send` | Reporting & Distribution | Send the daily digest / weekly writer emails — **action adapter, human-approval-gated** |
| **Google Ads / Meta Ads / Bing Ads** | `google-ads`, `facebook-business`, `bingads` SDKs; GCP Secret Manager creds | Paid-Media | Read ad spend + campaign metrics (`[CB] -M-` marketplace campaigns) — **read-only** |
| **Lead feeds** (Lotlinx / Carzing / Cars&Bids) | Lotlinx API; Carzing/QuoteWizard CSVs via AWS S3 `valnet-quotewizard`; Sentinel conversion events | Paid-Media | Conversion/lead counts for ROI (per-lead values in memory payload) — **read-only** |
| **RSS / Google Trends / SerpAPI** | feedparser; Trends RSS + optional `SERPAPI_API_KEY` | Opportunity / Research | Viral-trend ideation feeds (HC Viral Hits) + competitor watch (daily-reporting) |
| **MJML** | Node `mjml` compile | Reporting & Distribution | Newsletter HTML compilation |
| **Similarweb** | Similarweb API (`SIMILARWEB_API_KEY`) | Research / Reporting & Distribution | **Already in use** — daily-reporting-agent calls it for competitor traffic estimates; reuse that client |
| **Bing Webmaster / Bing Search** | Bing Webmaster Tools API / Bing Search API | Opportunity / Research | **Not present in any current repo — aspirational; confirm accounts/keys before building (see §13)** |
| **Web search + News/RSS** | web_search tool, RSS feeds | Research | External context + fact-checking |
| **Asana** | Asana API (`ASANA_PAT`), per-brand project/section GIDs | Production | Tasks + outline-approval workflow |
| **Anthropic / Claude** | `ANTHROPIC_API_KEY` | (substrate) | All LLM-backed agents run on Claude; models per Section 11 (daily-reporting + mp-spend use no LLM) |
| **Playwright** | Headless Chromium (content auditor, social render, Emaki automation, competitor comment counts) | Scheduled job / Production / Reporting & Distribution | Article scraping, social-image render, CMS automation |

---

## 5. Architecture

```
                 Editor / admin            Scheduled jobs
              (approve · decide)          (decay · content audit)
                      │                            │
        Slack ── Orchestrator (coordinator + governor)
        (notify)      │                            │
                      ▼                            ▼
        ┌──────────────── Shared memory (Postgres) ────────────────┐
        │            typed entries · provenance · TTL              │
        └──┬────────┬────────┬────────┬───────────┬────────┬───────┘
           │        │        │        │           │        │
        Research Opportnty Producton Analytics  Reporting  Paid-      (worker agents)
           │        │        │        │         & Distrib  Media
           │        │        │        │           │        │
        web/    Ahrefs/   Asana/AI  BigQuery/   daily-rpt/ Google/    (owned tools)
        News/   GSC/      writer/   PubInsights/ newsletter Meta/Bing
        RSS/    HC-Viral  outline/  Sentinel/    (MJML)/    Ads/
        Trends  ideation  HC-Viral  Sheets       social     Sentinel/
                          Emaki                  (render)/  Lotlinx/
                          publish                Gmail send Carzing
           └────────┴────────┴────────┴───────────┴────────┘
                                  │
                    Credentials & access layer
       (Google service accounts · Secret Manager · API keys · OAuth tokens)
```

Distribution + Emaki-publish tools are **action adapters** (governor-gated, dry-run by default, human-approval-gated); paid-media tools are **read-only**. Everything else follows the same read-adapter → memory pattern.

**Planes:**

- **Human plane** — the editor/admin approves plans and makes accept/reject decisions; Slack is the notification surface.
- **Orchestration plane** — the orchestrator (with the governor as an in-process policy component).
- **Shared state** — Postgres. The single coordination substrate. All reads/writes are typed entries with provenance.
- **Worker plane** — the six agents. Each owns one data domain; each only touches shared memory + its own tool adapters.
- **Integration plane** — tool adapters wrapping the existing systems and external APIs.
- **Credentials plane** — one place for all service-account keys and API keys; the governor enforces access and spend here.

**Coordination model (critical):** Agents never message each other. An agent that produces a finding writes it to shared memory; any other agent that needs it *queries* memory. The orchestrator dispatches work by writing approved `plan_item` rows to memory that the assigned agent picks up. This keeps context/cost bounded, preserves provenance, and is the deliberate opposite of a swarm.

---

## 6. Agents

Each agent implements a common interface (Section 10). Agents are stateless between runs; all state lives in shared memory.

### 6.1 Orchestrator (chief of staff)
- **Job:** No domain work. Each morning: read shared memory → synthesize a cross-system, cross-brand picture → draft a **prioritized plan** of `plan_item`s → send to a human for approval → on approval, dispatch (write approved items back to memory) → post a brief to Slack.
- **Inputs:** all recent memory entries (metrics, flags, decisions, candidates), scoped by brand and freshness.
- **Outputs:** one `plan` (draft) per brand/day with ordered `plan_item`s; a Slack brief; dispatched (approved) plan items.
- **Contains the governor** (Section 8) as a policy component invoked on the dispatch path.

### 6.2 Research (outside-in + fact-gate)
- **Job:** Pre-fetch external context the other agents need (market/news/competitor landscape) and **act as the fact-integrity gate**.
- **Owns:** web_search, Bing Search, News/RSS, Similarweb.
- **Fact-gate rule:** any entry another agent wants stored as a **verified fact** must pass Research verification (search-confirmed). Otherwise it is stored as an **unverified claim**. This mirrors the existing fact-checker's discipline: search-confirmed or it's a claim, never training-data recall promoted to fact.
- **Outputs:** `fact` / `claim` entries with source URLs + verification status; market-context `metric`/`flag` entries.

### 6.3 Opportunity (what to make next)
- **Owns:** Ahrefs, GSC, Bing Webmaster; the **ideation triggers** for Claude Albert, Seona, and HC Viral Hits.
- **Job:** keyword-gap mining, competitor angles, own past winners, rejection memory, and **viral-trend surfacing** (HC Viral Hits' RSS/Trends angle path). Proposes topics; can trigger a Claude Albert / Seona ideation run or an HC Viral Hits poll/ideate (actions, governor-gated).
- **Outputs:** topic opportunities as `plan_item` proposals + supporting `metric`/`flag` entries. Reads Similarweb landscape from memory (does not call it directly).

### 6.4 Production (pipeline state)
- **Owns:** Asana, Claude Albert's AI-writer queue + outline-review queue, HC Viral Hits' draft queue + Emaki CMS publish path.
- **Job:** track where every piece stands across **both writing pipelines** (proposed → accepted → drafting → in review → ready → published), flag bottlenecks, overdue items, stuck outlines, and anything needing a human. Executes approved production actions (create Asana task, route accepted topic to an AI writer, queue a decay refresh, push an HC Viral Hits draft to Emaki as an unpublished CMS draft) — all governor-gated, dry-run by default.
- **Outputs:** pipeline-state `metric`/`flag` entries; action results.

### 6.5 Analytics (how we did)
- **Owns:** BigQuery (PubInsights consum + ODS tables), Sentinel, Google Sheets.
- **Job:** performance and pace — what published, what's winning, sessions per article, week-over-week, writer output pace vs. quota. Ranks what matters. Supersedes the **writers-dashboard** digest/monitor agents as the performance brain (reuse their metric logic). Note: writers-dashboard's writer-quota management and Google-Sheets write-backs stay in that system; Analytics reads, it does not take over quota admin.
- **Outputs:** ranked `metric` entries + `flag`s (at-risk writers, decaying content, sessions drops).

### 6.6 Reporting & Distribution (what goes out)
- **Owns:** daily-reporting-agent (editorial email digest), newsletter-creator-auto (CarBuzz newsletter), social-media-posts-creator (social images/carousels). Gmail send + MJML compile + Playwright render as its adapters.
- **Job:** assemble outbound artifacts — the per-brand daily KPI/Discover/competitor digest, newsletter drafts (lead + briefs), and per-platform social posts (captions + branded images) — from published-performance data, and surface them for human review. **Nothing distributes autonomously:** the digest email send is a human-approval-gated action; newsletter HTML and social PNGs are draft artifacts a human copies out / downloads and sends/posts themselves (no send/post integration exists to add).
- **Outputs:** `report` and `distribution_draft` entries (with artifact-store pointers) + `flag`s (digest failed, competitor surge). Reads performance/competitor context from memory (written by Analytics / Research) rather than re-querying.

### 6.7 Paid-Media (what we're spending, what it returns)
- **Owns:** mp-spend-dashboard data domain — Google/Meta/Bing Ads, Sentinel conversion events, Lotlinx/Carzing/Cars&Bids lead feeds, the paid-media Google Sheet.
- **Job:** track paid-media spend and return for the marketplace campaigns (`[CB] -M-`) — daily spend by platform, conversions/leads by source, cost-per-lead and ROI, week-over-week. This is a distinct data domain from editorial performance. **Read-only** into memory; it never adjusts bids, budgets, or campaigns.
- **Outputs:** `metric` entries tagged `domain:'paid_media'` (spend, conversions, ROI) + `flag`s (spend spike, zeroed spend, ROI drop). The orchestrator folds spend/ROI context into the morning plan alongside editorial signals.

### Scheduled feeders (NOT agents)
- **Ranking-decay scan** (Seona) and **content-depth auditor** run on their existing schedules and drop candidates/findings into shared memory as typed entries (gray, not agents). The **daily-reporting** BigQuery/Sentinel pull and the **mp-spend** Cloud Run job likewise run on their existing crons and can emit their raw metrics into memory as feeders — the owning agents (Reporting & Distribution, Paid-Media) then read, rank, and act on those once the orchestrator folds them into the plan.

---

## 7. Shared memory (Postgres)

The heart of the system. A **queryable store, not a firehose** — agents query for what they need (by brand, type, freshness), they do not ingest everything.

### 7.1 Requirements
- Concurrent reads/writes across agents and adapters (hence Postgres, not SQLite).
- Every entry carries **provenance** (which agent, which source system, source URLs) and a **verified** flag.
- **Typed** entries so agents can query precisely.
- **Scoped** by brand and by freshness (`expires_at` / TTL); a scheduled sweep expires stale rows.
- The **fact vs. claim** distinction is a first-class field, enforced via the Research fact-gate.
- Add **pgvector only if** semantic recall ("have we angled this before?") is genuinely needed beyond the existing title-overlap dedup. Do not add it speculatively.

### 7.2 Core tables (reference DDL — a starting point, refine as needed)

```sql
CREATE TYPE entry_type AS ENUM ('metric','decision','flag','fact','claim','plan_item','context','report','distribution_draft');
-- 'report'             — a rendered digest/report artifact (daily-reporting) + artifact-store pointer
-- 'distribution_draft' — a newsletter or social draft (HTML / image set + captions) + artifact-store pointer
-- paid-media spend/ROI uses 'metric' with payload {domain:'paid_media', ...}

CREATE TABLE memory_entry (
  id            BIGSERIAL PRIMARY KEY,
  type          entry_type NOT NULL,
  brand         TEXT NOT NULL,              -- 'hotcars' | 'carbuzz' | 'topspeed' | 'portfolio'
  source_agent  TEXT NOT NULL,             -- 'research' | 'opportunity' | ... | 'orchestrator' | 'decay_scan'
  source_system TEXT,                       -- 'ahrefs' | 'bigquery' | 'sentinel' | ...
  payload       JSONB NOT NULL,             -- the actual content, type-specific
  verified      BOOLEAN NOT NULL DEFAULT FALSE,   -- true only for facts that passed the fact-gate
  confidence    REAL,                       -- 0..1, optional
  source_urls   TEXT[],                     -- provenance for facts/claims
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ,                -- TTL sweep target; NULL = no expiry
  status        TEXT NOT NULL DEFAULT 'active'    -- 'active' | 'expired' | 'superseded'
);
CREATE INDEX ON memory_entry (brand, type, created_at DESC);
CREATE INDEX ON memory_entry USING GIN (payload);

CREATE TABLE plan (
  id           BIGSERIAL PRIMARY KEY,
  plan_date    DATE NOT NULL,
  brand        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft',   -- draft | approved | dispatched | done | cancelled
  created_by   TEXT NOT NULL DEFAULT 'orchestrator',
  approved_by  TEXT,                             -- editor/admin identity
  approved_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plan_item (
  id            BIGSERIAL PRIMARY KEY,
  plan_id       BIGINT REFERENCES plan(id) ON DELETE CASCADE,
  rank          INT NOT NULL,
  assigned_agent TEXT NOT NULL,                  -- which worker executes it
  action_type   TEXT NOT NULL,                   -- 'trigger_ideation' | 'create_asana_task' | 'route_to_writer' | 'queue_decay_refresh' | 'emaki_publish_draft' | 'assemble_digest' | 'send_digest_email' | 'assemble_newsletter' | 'assemble_social_post' | 'notify' | ...
  params        JSONB NOT NULL,
  rationale     TEXT,                             -- why the orchestrator proposed it (human-readable)
  status        TEXT NOT NULL DEFAULT 'proposed', -- proposed | approved | rejected | dispatched | running | done | failed
  dry_run       BOOLEAN NOT NULL DEFAULT TRUE,
  cost_estimate JSONB,                            -- {ahrefs_units, llm_micros, bq_bytes}
  result_ref    JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 7.3 Observability / audit tables

```sql
-- Per external tool call: provenance + spend (mirror Seona's AgentUsage/agent_event).
CREATE TABLE tool_call_log (
  id            BIGSERIAL PRIMARY KEY,
  agent         TEXT NOT NULL,
  tool          TEXT NOT NULL,
  action        TEXT NOT NULL,                    -- 'read' | 'act'
  brand         TEXT,
  dry_run       BOOLEAN NOT NULL,
  request       JSONB,
  ok            BOOLEAN,
  cost          JSONB,                            -- {ahrefs_units, llm_micros, bq_bytes, usd}
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rolling spend ledger for the governor's caps.
CREATE TABLE spend_ledger (
  id            BIGSERIAL PRIMARY KEY,
  window_date   DATE NOT NULL,
  metric        TEXT NOT NULL,                    -- 'ahrefs_units' | 'llm_micros' | 'bq_bytes'
  amount        BIGINT NOT NULL,
  agent         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 8. Governor & safety (must-have, non-negotiable)

The governor is a policy component invoked by the orchestrator on dispatch and by every action adapter. It is the reason an always-on loop is safe.

- **Hard spend caps** per day and per run, per metric: Ahrefs units, LLM micros, BigQuery bytes scanned. When a cap is hit, the action is refused (not queued silently) and a flag is written to memory. Caps are config, not code.
- **Dry-run by default.** Every action adapter that produces an external side effect defaults `dry_run=true`: it logs what it *would* do (in `tool_call_log`) and writes the intended result, but performs no external write. Live writes require `dry_run=false`, which is only set on an **approved** `plan_item`.
- **Human-approval gate.** No `plan_item` transitions past `approved` without a recorded `approved_by` + `approved_at`. The orchestrator cannot self-approve.
- **Provenance enforcement.** Writing a `fact` (`verified=true`) is only allowed for entries that passed the Research fact-gate. Adapters/agents that try to write an unverified `fact` are downgraded to `claim`.
- **Access mediation.** All secrets come from the credentials layer at call time; they never appear in prompts, memory, logs, or agent code. Redact secrets from all logging. **User identity (Google SSO, §9.1) is kept separate from resource credentials** — the logged-in user authenticates and is attributed on approvals, but every tool call uses a service-account / app credential from the credentials layer, never the user's token.
- **Least privilege.** Each adapter gets only the credential it needs. BigQuery uses a read-scoped service account; Asana/AI-writer use scoped tokens.
- **Kill switch.** A single config flag (and/or per-agent flag) halts all dispatch and live actions, leaving read/observe running. Mirror the existing outline-reviewer dry-run default posture.
- **Distribution is human-send-only.** `send_digest_email` is a human-approval-gated action adapter, dry-run by default (logs the intended recipients/body, sends nothing until an approved `plan_item` flips `dry_run=false`). Newsletter and social artifacts are **generate-only** — Switchboard produces the HTML/PNG + captions and stops; a human sends/posts. Do **not** add any newsletter-send or social-posting integration; there is none today and building one is out of scope (§3).
- **Emaki publish is a gated production action.** `emaki_publish_draft` pushes an *unpublished* CMS draft only (never sets featured image or goes live), on an approved `plan_item`, exactly like an Asana write — dry-run by default, one real push per approved item.
- **Paid-media is read-only.** The Google/Meta/Bing Ads, Sentinel-events, and lead-feed adapters observe only. No adapter may change a bid, budget, campaign, or push an offline conversion through Switchboard. (mp-spend's own Cloud Run job keeps doing its Bing offline-conversion push outside Switchboard.)
- **Least privilege (extended).** Ad-platform credentials are read-scoped; Gmail uses a `gmail.send`-only token; Emaki uses its own storage-state; all resolve from the credentials layer at call time. Redact all of these from logs.

**Reuse note:** the existing systems already track LLM cost and Ahrefs units and default the outline reviewer to dry-run — reuse those patterns and cost formulas (Claude Albert's `agent_usage`/`cost_micros`, HC Viral Hits' `compute_cost_cents`) rather than inventing new ones.

---

## 9. Human approval surface

- Minimal at first: the daily `plan` with its ranked `plan_item`s, each showing the action, params, rationale, and cost estimate. The human can **approve / edit / reject per item** and approve the plan.
- Two acceptable implementations for Phase 3: (a) a small web view (reuse the Seona Next.js patterns and role model — `global_admin`/`portfolio_admin`/etc.), or (b) a Slack approval flow (buttons), consistent with the existing Slack app. Prefer the web view if time allows; Slack approval is acceptable for MVP.
- Approvals are recorded on the `plan`/`plan_item` rows. Nothing dispatches without them.

### 9.1 Authentication & identity (decided)

- **Login is Google OAuth (OpenID Connect), not email+password.** Replace Seona/Calbert's `albert_session` email+password flow with Google SSO. Restrict to the Valnet Workspace domain(s) via the `hd` hint **verified server-side** plus an **explicit allowlist** of users (the "list of users that can login"). Login requests only `openid email profile` — no Gmail/Sheets/BigQuery scopes at login.
- **Identity is for authentication + attribution only.** The Google identity maps to the existing role table (`global_admin` … `brand_user`, unchanged) and populates `approved_by` / `approved_at` on `plan` / `plan_item` rows — giving the governor's human-approval gate a real, Workspace-native actor.
- **Resource access stays on service accounts / app keys.** BigQuery, Google Sheets, Gmail send, Ahrefs, Asana, Sentinel, and the ad platforms all authenticate via their own service-account or app-level credentials in the credentials layer — **not** the logged-in user's token. This is required anyway: the morning cycle and scheduled feeders run unattended, with no user session. Per-user Google credentials were explicitly considered and rejected — they'd force direct warehouse/sheet grants for every editor, add per-user refresh-token custody, and still not cover the non-Google tools or the cron path.
- **Future option (not MVP):** if a digest/newsletter should send *from the triggering editor's own mailbox* rather than a shared sender, add narrow `gmail.send` delegation for that one action (incremental per-user OAuth, or Workspace domain-wide delegation on the mail service account) — gated by the same human-approval rule. Everything else remains service-account based.

---

## 10. Component contracts (reference interfaces)

Keep these small and explicit. Python (matches Seona / the auditor). Signatures are a starting point.

```python
# Tool adapter: wraps one existing system or external API.
class ToolAdapter(Protocol):
    name: str
    def observe(self, brand: str, **kwargs) -> list[MemoryEntry]:
        """Read from the underlying system; return typed entries to persist.
        Logs a 'read' tool_call_log row. Never performs side effects."""
    def act(self, item: PlanItem, *, dry_run: bool) -> ActionResult:
        """Perform a side effect for an approved plan_item.
        Governor-gated by the caller; logs an 'act' tool_call_log row.
        Must honor dry_run (log-only when true)."""

# Worker agent.
class Agent(Protocol):
    name: str
    owned_tools: list[str]
    def observe(self, brand: str) -> None:
        """Query owned adapters + memory; write findings/flags/facts to memory."""
    def execute(self, item: PlanItem) -> ActionResult:
        """Run one approved plan_item via an owned adapter (governor-gated)."""

# Orchestrator.
class Orchestrator(Protocol):
    def plan(self, brand: str, plan_date: date) -> Plan:      # read memory -> draft plan
    def dispatch(self, plan: Plan) -> None:                    # approved items -> memory (governor-checked)

# Governor.
class Governor(Protocol):
    def check_action(self, item: PlanItem) -> Decision:        # approval + budget + provenance
    def charge(self, metric: str, amount: int, agent: str) -> None
    def within_caps(self, metric: str) -> bool
```

---

## 11. Tech stack

Bias toward extending what already exists; add the minimum.

- **Language/runtime:** Python 3.11 + FastAPI (matches Claude Albert, Seona, HC Viral Hits, and the content auditor). Node/TS stays where it already lives — writers-dashboard (the perf-monitoring Slack app), social-post generator, mp-spend dashboard; Switchboard reads their metric logic or calls thin endpoints. **MJML** (Node) compiles newsletters; **Playwright** renders social images, drives Emaki, and scrapes; the **Gmail API** sends digests; the ad-platform SDKs (`google-ads`, `facebook-business`, `bingads`) read spend; **mp-spend** runs as a GCP Cloud Run job on Cloud Scheduler — leave that deployment pattern in place and read its output.
- **Shared memory:** PostgreSQL. Migrations via Alembic (as Seona does). pgvector only if/when semantic recall is needed.
- **Event/trigger:** cron/APScheduler to start the morning cycle and scheduled feeders. Add Redis for a lightweight event bus **only if** cron-plus-async starts to strain — not required for MVP.
- **Agent plumbing (optional):** if hand-wiring the orchestration graph gets painful, use **LangGraph** (explicit, stateful, first-class human-in-the-loop interrupts) or the first-party Anthropic agent SDK. **Do not** use swarm harnesses or role-playing-agent frameworks (see Non-goals).
- **Models:** reuse the existing per-task choices — Sonnet (`claude-sonnet-4-6`) for most agents; a reasoning-tier model (Opus) for synthesis-heavy work (orchestrator planning, opportunity scouting); Haiku for cheap verification (fact-gate). Keep model IDs in config.
- **Artifacts:** reuse the existing artifact-store pattern (structured record + pointer in DB, file in blob storage). Since the portfolio is on GCP, use Google Cloud Storage. Agents never touch the filesystem directly.
- **Secrets:** a secrets manager (or, minimally, the existing `GOOGLE_APPLICATION_CREDENTIALS` + env-var pattern), fronted by the credentials layer. Never in code/prompts/logs.

---

## 12. Build plan (phased, with acceptance criteria)

Build in order. Each phase must pass its acceptance criteria before the next.

### Phase 0 — Foundations
- Repo scaffold; Postgres + Alembic; config for the three brands; the credentials layer (secret lookup + redaction); the `ToolAdapter`/`Agent`/`Governor` interfaces; `memory_entry`, `plan`, `plan_item`, `tool_call_log`, `spend_ledger` tables.
- **Accept:** migrations apply; a dummy adapter can write/read a `memory_entry`; secrets resolve via the credentials layer and are redacted in logs; a TTL sweep expires an entry.

### Phase 1 — Read adapters (observe only)
- Wrap the read side of each tool, starting with the ones that already exist: BigQuery/PubInsights (both consum + ODS tables) + Sentinel + Sheets (Analytics), Ahrefs + GSC + Claude Albert/Seona/HC-Viral ideation status (Opportunity), Asana + AI-writer queue + outline-review + HC-Viral draft queue (Production), daily-reporting digest data + newsletter/social source data (Reporting & Distribution), Google/Meta/Bing Ads + Sentinel-events + lead feeds (Paid-Media). Each `observe()` writes typed entries with provenance. Similarweb/Bing/Web/News adapters for Research (Similarweb already used by daily-reporting-agent — reuse it; Bing pending account confirmation — §13).
- **Accept:** each adapter produces correctly-typed, brand-scoped `memory_entry` rows with provenance; every call logs a `tool_call_log` row; no side effects occur; Ahrefs calls respect the existing cache; paid-media adapters are provably read-only.

### Phase 2 — Worker agents (observe)
- Implement the six agents' `observe()`: query owned adapters + memory, write findings/flags. Implement the Research **fact-gate** so facts are only stored `verified=true` after search confirmation; everything else is a `claim`. Reporting & Distribution and Paid-Media populate `report`/`distribution_draft`/paid-media-`metric` entries.
- **Accept:** running all six agents populates memory with metrics/flags/facts/claims/reports; no agent calls another agent or another agent's adapter; an unverified fact is correctly downgraded to a claim; paid-media metrics carry `domain:'paid_media'`.

### Phase 3 — Orchestrator + approval + governor
- Implement `plan()` (synthesize memory → ranked draft plan with rationales + cost estimates), the human approval surface (Section 9), and the governor (Section 8). Post a Slack brief.
- **Accept:** a morning run produces a draft plan a human can approve/edit/reject per item; nothing is `approved` without a recorded approver; the governor refuses an action that would exceed a (test) cap and writes a flag; the orchestrator cannot self-approve.

### Phase 4 — Action adapters + dispatch (dry-run first)
- Implement `act()` for the production + distribution actions (trigger ideation, create Asana task, route to AI writer, queue decay refresh, **push HC-Viral draft to Emaki**, **assemble digest / send digest email**, **assemble newsletter draft**, **assemble social post**, notify), all **dry-run by default**. Newsletter/social `act()` only *assemble* artifacts (no send/post path exists). Implement `dispatch()`: approved items → memory → assigned agent executes via governor.
- **Accept:** with `dry_run=true`, actions log intended effects and write results but perform no external writes (no Emaki push, no email sent); flipping an approved item to live performs exactly one real action (one CMS draft, one digest email) and records cost; a rejected item never executes; newsletter/social actions produce an artifact + pointer and never attempt to send/post.

### Phase 5 — Scheduled feeders + observability
- Wire the ranking-decay scan and content-depth auditor to emit typed entries into memory on their existing schedules. Build an observability view: spend vs. caps, memory browser (filter by brand/type/verified), plan history, tool-call audit.
- **Accept:** decay/audit candidates appear in memory and in the next plan; the dashboard shows accurate spend against caps and a per-claim provenance trail.

### Phase 6 — Hardening
- Retries/backoff on adapters; TTL/superseded sweeps; kill switch; least-privilege credential scoping verified; secret-redaction audit; end-to-end run on one brand.
- **Accept:** a full supervised daily cycle runs on HotCars within configured caps, produces an approved plan, dispatches ≥1 live action, and leaves a complete, secret-free audit trail; the kill switch halts dispatch while observe keeps running.

---

## 13. Open questions / decisions for the human

Resolve these with the supervising engineer; don't guess.

1. **Bing / News** are not in the existing code — confirm accounts/keys exist and which endpoints to use, or defer those adapters to a later phase. (**Similarweb** *is* already wired in daily-reporting-agent — reuse that client rather than building new.)
2. **Bing** — Webmaster Tools (search demand, Opportunity) vs. Search API (context, Research): confirm which key(s) you have.
3. **Approval surface** — new web view vs. Slack buttons for MVP.
4. **Spend caps** — the actual daily/run numbers for Ahrefs units, LLM spend, and BQ bytes.
5. **Where Switchboard runs** relative to the existing services (same VM/cluster, or its own) and how it reaches their APIs/DBs.
6. **Trigger vs. read** on ideation — does Opportunity trigger Claude Albert / Seona / HC-Viral runs, or only read their output, for MVP?
7. **Cross-brand scope** — start single-brand (HotCars) or all three from day one.
8. **Two BigQuery article tables** — `pubinsights_ods_data.new_article_analysis` (Claude Albert, daily-reporting) vs `pubinsights_consum_data.auto_new_article_analysis` (writers-dashboard, content-auditor, social). Which is canonical for Analytics, and do we reconcile them?
9. **Two ideation + AI-writer pipelines** — Claude Albert (Discover/SEO writer) and HC Viral Hits (viral-trend writer + Emaki publish) overlap. Keep both as distinct paths, or converge? Affects how Opportunity/Production model drafts.
10. **Emaki auth** — the CMS push relies on an exported Playwright storage-state that expires. Who refreshes it, and how do we alert before a session lapses?
11. **Digest identity + channel** — the daily digest currently sends from `anthony.a@valnetinc.com` via Gmail. Confirm the sender identity Switchboard should use, and whether daily-reporting's stubbed Slack path should be wired.
12. **Paid-media cadence & scope** — is paid-media in the same morning cycle as editorial or a separate track? Confirm read-only ad-account scoping and which campaigns beyond `[CB] -M-` are in scope.
13. **GSC tables** — `gsc_table` is empty for the Auto trio today; Seona ideation won't run until populated. Who owns populating it?

---

## 14. Guardrails recap (pin this)

- Coordinate **only** through shared memory. No agent-to-agent calls.
- **Wrap**, don't rebuild.
- **Nothing** hits production without human approval.
- Governor **hard-caps** spend; external actions are **dry-run by default**.
- Facts are **search-verified or they're claims**; everything has provenance.
- Secrets live in the credentials layer, **never** in code, prompts, memory, or logs.
- No swarm, no **CRM/sales** agent, no raw filesystem access.
- **Distribution is draft + human-send:** digests, newsletters, and social posts are assembled for review; Switchboard never sends or posts autonomously and adds no send/post integration.
- **Paid-media is read-only:** observe spend/ROI; never touch bids, budgets, or campaigns.

---

## 15. Consolidation opportunities (surfaced, not assumed)

Wrapping the full portfolio exposed real duplication. Switchboard's shared memory is the natural place to unify these, but none of it should be done silently — flag to the engineer, don't collapse pipelines unilaterally:

- **Two BigQuery article-analysis tables** (`pubinsights_ods_data.new_article_analysis` vs `pubinsights_consum_data.auto_new_article_analysis`) feed different systems for the same brands. Pick a canonical source for Analytics or map between them (§13.8).
- **Two ideation + AI-writer pipelines** — Claude Albert (Discover/SEO) and HC Viral Hits (viral-trend, publishes to Emaki). They discover and draft in parallel with overlapping brands; memory can de-dup topic angles across both (§13.9).
- **Two performance-digest paths** — writers-dashboard (Slack, writer-centric) and daily-reporting-agent (email, editorial-leader-centric). Both compute per-brand performance from overlapping data on a daily ET cadence. Analytics + Reporting & Distribution can share one metric layer instead of two.
- **Two cost-tracking schemes** — Claude Albert's `agent_usage`/`cost_micros` and HC Viral Hits' `AgentEvent`/`compute_cost_cents`. The governor's `spend_ledger` should absorb both rather than add a third.
