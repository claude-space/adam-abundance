# Integration notes — how each wrapped system actually works

Distilled from a read-only pass over the reference repos in
`C:\Users\Intern\Documents\AutoDashboard\`. These are the load-bearing facts the
Switchboard adapters code against (exact tables, columns, URLs, auth, cost
formulas, endpoints, model IDs). **Reference repos are read-only** — Switchboard
never modifies them.

> Corrections found during research (supersede the PRD where noted):
> - **newsletter-creator-auto no longer uses MJML/Node** — it renders via a pure
>   Python mini-Handlebars over `backend/templates/template_raw.html`. No MJML
>   dependency for the adapter.
> - **daily-reporting-agent Gmail auth is a file-based OAuth token** (`credentials/
>   gmail_token.json`), not client-id/secret/refresh env vars — but the
>   consolidated `switchboard.env` *does* carry `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`,
>   so Switchboard's own Gmail adapter builds creds from those.
> - **content-depth-auditor calls only Sentinel `traffic/`**, never `events/`.
>   The `events/` endpoint is used by **mp-spend** (paid-media conversions).

---

## Warehouse: BigQuery — TWO article-analysis tables (PRD §13.8)

Project `data-science-458422`.

| Table | Brand key | Used by | Key columns |
|---|---|---|---|
| `pubinsights_consum_data.auto_new_article_analysis` | **short** `HC`/`CB`/`TPS` (col `Brand`) | writers-dashboard, content-auditor, newsletter, social | `ArticleTitle, URL, PubDate, PriCat, Intent, ActSessSentinel, AVD, viewAvgEngagedDepthPercentage, Writer, ContentType, PubType, wordCount` |
| `pubinsights_ods_data.new_article_analysis` | **full** `HotCars`/`CarBuzz`/`TopSpeed` (col `brandName`) | Claude Albert, daily-reporting, hc-viral BQ profile | `permalink, intentName, writerName, editorName, primaryCategoryName, contentTypeName, tagNames(JSON), discoverClicks, discoverImpressions, discoverCTR, datePublished, dateUpdated` |

Auth patterns seen: ADC (`google.auth.default()` + `bigquery.Client(project=...)`)
**or** service-account from inline JSON (`BQ_KEY_JSON`) / key file
(`GOOGLE_APPLICATION_CREDENTIALS`). Switchboard uses the consolidated compute SA
(`GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` inline, read-scoped).

**Reusable SQL — writer performance** (writers-dashboard
`lib/data/getWritersData.ts`, reuse in Analytics):
```sql
SELECT Writer AS writerName, Intent AS intentName,
  COALESCE(SUM(ActSessSentinel),0) AS totalSessions,
  COUNT(DISTINCT URL) AS totalArticles,
  SAFE_DIVIDE(COALESCE(SUM(ActSessSentinel),0), COUNT(DISTINCT URL)) AS sessionsPerArticle
FROM `data-science-458422.pubinsights_consum_data.auto_new_article_analysis`
WHERE Brand=@brand AND Intent IN ('Feed','Evergreen','Sniping','Short-Term')
  AND PubDate >= DATE_TRUNC(CURRENT_DATE('America/New_York'), MONTH)
  AND PubDate <  CURRENT_DATE('America/New_York')
  AND Writer IS NOT NULL AND Writer!='' AND ContentType!='Resource'
GROUP BY Writer, Intent ORDER BY Intent, sessionsPerArticle DESC
```
Aggregation: `Evergreen`+`Sniping` merge into "evergreen"; `Feed`→feed;
`Short-Term`→news. `relativeIndex = writerSPA / brandAvgSPA` where
`brandAvgSPA = ΣtotalSessions/ΣtotalArticles`. Output projection:
`projectedTotal = round(articles/daysElapsed*daysInMonth)`; `On track ≥95% of
quota, Behind ≥75, At risk <75`; default quota 20.

## Sentinel Pro — real-time sessions/engagement + conversion events

- **traffic/** (Analytics): `https://{account}.sentinelpro.com/api/v1/traffic/`
  (account `valnet` → `https://valnet.sentinelpro.com/...`). Header
  `SENTINEL-API-KEY: <key>`. GET with `params={"data": json.dumps(payload)}`.
  Payload: `filters.date.{gte/gt,lt}`, `filters.propertyId.in:[<www.domain>]`,
  optional `filters.pagePath.in`, `filters.device.in`, `metrics:[visits,
  sessions,views,averageEngagedDuration,averageEngagedDepth]`, `dimensions`,
  `granularity: daily|hourly|fiveMinutes`, `pagination{pageSize:1000-2000}`.
  Response: `{data:[{date,pagePath,propertyId,intent,visits,averageEngagedDepth,
  averageEngagedDuration}...], totalPage}`. `propertyId` = site domain
  (`www.hotcars.com`, `www.carbuzz.com`, `www.topspeed.com`). ~1 req/s limiter;
  retry {429,5xx}. Times are America/New_York.
- **events/** (Paid-Media): `.../api/v1/events/`. Payload filters `eventName IN
  [...]`, `utmCampaign IN [...]`, `pagePath IN [...]`, `propertyId IN [...]`,
  `dimensions:[utmCampaign,eventName]` → pivot `{utm_campaign:{event:count}}`.
  Marketplace events: `lotlinx_marketplace`(J), `carzing_marketplace`(K),
  `CarsAndBids_marketplace`(L). Page paths `/marketplace/mb/`, `/marketplace/mb2/`.

## Similarweb (Research / Reporting) — REUSE `daily-reporting-agent/similarweb.py`

`https://api.similarweb.com/v1/website/{domain}/total-traffic-and-engagement/{path}`.
**`api_key` is a query param**, not a header. Paths: `describe` (freshest range),
`visits`, `average-visit-duration`, `pages-per-visit`, `bounce-rate`. Params
`start_date,end_date` (YYYY-MM), `country=world`, `granularity=daily`,
`main_domain_only=false`, `format=json`. `domain_traffic_28d()` →
`DomainTraffic{total_28d, daily_avg, prior_28d, pct_change, series, ...}`.

---

## Opportunity domain — Ahrefs / GSC / ideation triggers

*(Calbert + Seona research pending; section filled when that pass completes.)*
Known from PRD/env: Ahrefs API v3 `https://api.ahrefs.com/v3`, SQLite
`ahrefs_cache` 7-day TTL, ~10 units/row (metered — governor caps). GSC via
BigQuery `gsc.<brand>_com_searchdata_url_impression` (empty for Auto trio today).

## Production domain — Asana / AI writer / outline review / HC-Viral / Emaki

### HC Viral Hits (`hc-viral-hits`) — machine-facing surface exists
- **API-key reads** (`/api/cms/*`, header `X-API-Key` or `Authorization: Bearer`,
  key = `HC_VIRAL_HITS_API_KEY`, brand via `?brand=<slug>`):
  - `GET /api/cms/drafts?brand=&status=ready` — publish queue
  - `GET /api/cms/drafts/{topic_id}?brand=` — CKEditor HTML + metadata
  - `POST /api/cms/drafts/{topic_id}/mark-published?brand=` — close loop
- **Triggers** (session-auth): `POST /api/pipeline/poll`, `/api/pipeline/ideate`,
  `POST /api/topics/{topic_id}/emaki-publish`.
- **Topic states:** `proposed → accepted_self|accepted_ai → drafting → drafted →
  editing → ready → published` (+ `rejected`, `archived`). Emaki sub-state:
  `idle → publishing → published|error`.
- **Emaki push** (`emaki_link/publish.py:publish_draft`): headless Playwright,
  `storage_state` auth from `HC_VIRAL_HITS_EMAKI_STATE` (`.emaki-state.json`).
  Creates an **UNPUBLISHED draft** (Save & Stay only), **no featured image, never
  goes live** — matches the PRD's gated `emaki_publish_draft`. Brands: hotcars
  (emaki id 13), topspeed (30), topspeed-moto (30, segment "Moto").
- **Cost — `compute_cost_cents`** (`agent_runner.py`): `dollars = in*price_in +
  out*price_out + cache_write*price_in*1.25 + cache_read*price_in*0.10`; `cents =
  round(dollars*100) + 1¢*web_search_requests`. Pricing per-1M tokens:
  `haiku-4-5 {1.00/5.00}`, `sonnet-4-6 {3.00/15.00}`, `opus-4-6/4-7 {5.00/25.00}`.
  Strip trailing `-YYYYMMDD` from model id before lookup.

### content-depth-auditor (feeder)
- No API-key surface; JWT-gated REST (`POST /api/auth/login`). Reads:
  `GET /api/audit/history`, `/api/audit/{id}`, `/api/tracking`. Triggers:
  `POST /api/audit/start`, `/api/alerts/run`. Also direct SQLite `seo_audit.db`.
- BigQuery consum table; Sentinel `traffic/` only. Alert finding: `depth_pct <
  threshold AND avd_seconds < threshold` → `{url, depth_pct, avd_seconds,
  deep_link, property_id}`. Cost tracker hardcodes Sonnet 4.6 ($3/$15 per 1M).
- APScheduler (ET): alert_check Mon–Fri 9–20:00; tracking hourly :05; cleanup
  03:00.

## Analytics domain — writers-dashboard (reuse metric logic)

- Node/TS, port 3001. `GET /api/writers?brand=<Brand>` → `{feed,evergreen,news,
  output}`. Model `claude-sonnet-4-6`. node-cron (ET): sheet+BQ sync 9:50;
  missing-writers 9:55 Mon–Fri; monitoring 11:00 Mon–Fri; digest 10:30 Mon.
- Sheets: `SHEET_ID_{HOTCARS,TOPSPEED,CARBUZZ}`, col A=writer, B=email, quota col
  H. **Write-backs stay in writers-dashboard** — Switchboard reads only.

## Reporting & Distribution domain

- **daily-reporting-agent** (Python, no LLM): ODS BigQuery + Sentinel `traffic/`
  + Similarweb + competitor RSS/Google-News. Gmail send from
  `anthony.a@valnetinc.com`, `test_mode` default clamps recipients, live send
  needs typed `SEND` gate. Per-brand `BrandSnapshot{daily,live,live_breakouts,
  actions,errors}`. Stubbed `slack_text()` (not wired). **This is the only real
  send** — wrap as the human-approval-gated `send_digest_email` action.
- **newsletter-creator-auto** (FastAPI + React): consum BigQuery, Claude
  **`claude-opus-4-5`**, pure-Python HTML render. `POST /api/newsletter/compile`
  → `{html}`. Draft-only (human clicks "Copy HTML"). CarBuzz only.
- **social-media-posts-creator** (Next.js): consum BigQuery, Claude
  **`claude-sonnet-4-6`** (forced tool-use + code-verified verbatim quotes),
  Playwright render → PNG (`/api/render`, streams download, not persisted).
  Sizes: IG 1080×1350, FB 1080×1080, Pinterest 1000×1500, TikTok 1080×1440.
  `IMAGE_HOST_ALLOWLIST` SSRF guard. Draft-only (no posting API).

## Paid-Media domain — mp-spend-dashboard (read-only)

Python Cloud Run daily ~7am ET. **`GCP_PROJECT_ID` blank = local/env mode; set =
GCP Secret Manager.** Per-lead values: Lotlinx 0.75, Carzing 15.0, CarsAndBids
0.25. CAD→USD `GOOGLE_CAD_TO_USD_RATE` applied to google+facebook spend only.

- **Google Ads** (`google-ads` GAQL): `SELECT campaign.id, campaign.name,
  metrics.cost_micros, metrics.impressions, metrics.clicks FROM campaign WHERE
  campaign.id IN (...) AND segments.date='<yesterday>'`. `spend =
  cost_micros/1e6`. Campaign filter prefix **`[CB] -M-`**; `CAMPAIGN_CONFIG` maps
  ids→row labels.
- **Meta** (`facebook-business`): `account.get_insights(level=campaign, fields=[
  spend,impressions,inline_link_clicks], time_range=yesterday)`. clicks =
  `inline_link_clicks`.
- **Bing** (`bingads` v13): auth via **Google OAuth** (`GoogleOAuthWebAuthCodeGrant`
  refresh token) — Microsoft OAuth rejected. `CampaignPerformanceReport` CSV.
  ⚠️ `push_carzing_offline_conversions` (offline-conversion upload) — **Switchboard
  must NOT replicate.**
- **Leads:** Lotlinx API `https://publisher-api.lotlinx.com` (bearer via
  `/v1/auth/token`); Carzing now Google Sheet `CARZING_CONVERSIONS`; QuoteWizard
  CSV via S3 `valnet-quotewizard`. Output to Sheet tabs `RAW_DATA`(21 cols),
  `RAW_DATA_ORGANIC`, `RAW_DATA_RECONCILIATION`, `SYNC_STATUS`. ROI: `roas =
  revenue/spend`, `cpl_x = spend/leads_x`, `revenue = lotlinx*0.75 + carzing*15 +
  carsandbids*0.25`.
- Read paths for Switchboard: the `RAW_DATA*` sheet tabs (read-only SA) or the
  dashboard SQLite mirror via `GET /api/campaigns`. Metrics tagged
  `domain:'paid_media'`.
