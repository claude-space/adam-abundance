# Agent integration endpoints — what Switchboard reads/calls

Switchboard wraps each external agent through an **audited read/act adapter** that hits
one specific HTTP endpoint. To "connect" an agent, expose that endpoint so it's reachable
**without a browser login session** (the ShellAgent `/agents/<name>/` UIs redirect to
`/login`, which the adapters can't pass).

**Conventions that make this painless**
- **Auth:** gate each machine endpoint on an **API key header** — reuse HC-Viral's
  convention `X-API-Key: <key>`. Give the key to whoever runs Switchboard; it goes in
  `switchboard.env` and the adapter sends it. (Open/unauthenticated also works — that's what
  `content-depth-auditor` does today — but it's world-readable on `shellagent.io`.)
- **Trailing slashes are fine.** ShellAgent 308-redirects `/api/x` → `/api/x/`; the adapters
  now follow redirects (headers preserved on the same-origin hop).
- **Response parsing is lenient.** Each adapter accepts a bare JSON array *or* a wrapped
  object under a common key (`items`/`drafts`/`topics`/`records`/`data`), and tolerates
  field aliases (`id`|`topic_id`, `title`|`headline`). Canonical shapes are below.
- All reads are `GET` with a `brand` query param (`hotcars` | `carbuzz` | `topspeed`).

Each adapter's path is **overridable** via the env var noted, so you can serve a different
route than the default and we'll point at it.

---

## Claude Albert (`calbert`) — 3 read endpoints

### 1. Writer queue  ·  `GET /api/writer/queue/?brand=<brand>`  ·  env `ALBERT_WRITER_PATH`
```json
{ "items": [
  { "state": "queued" },
  { "state": "writing" },
  { "state": "failed" }
] }
```
`state` ∈ `queued | researching | writing | fact_checking | editing | ready | published | failed`.
Switchboard reports the count per state and raises a high-severity flag when any `failed`.

### 2. Outline-review status  ·  `GET /api/outline-review/status/?brand=<brand>`  ·  env `ALBERT_OUTLINE_PATH`
```json
{ "pending": 3 }
```
(`queue_depth` accepted as an alias; a bare array also works — its length is the pending
count.) Flags when `pending > 5`.

### 3. Ideation topics  ·  `GET /api/topics/?status=proposed&brand=<brand>`  ·  env `ALBERT_IDEATION_PATH`
```json
{ "topics": [
  { "id": "t_123", "title": "…", "status": "proposed" }
] }
```
Proposed topic candidates; Switchboard promotes the strongest into plan-item proposals.

---

## Seona — 1 read endpoint

### Ideation topics  ·  `GET /api/topics/?status=proposed&brand=<brand>`  ·  env `SEONA_IDEATION_PATH`
Same shape as Albert #3 — SEO topic candidates.
```json
{ "topics": [ { "id": "s_9", "title": "…", "status": "proposed" } ] }
```

---

## newsletter-creator-auto — 1 action endpoint

### Compile  ·  `POST /api/newsletter/compile/`
Request body = the `content` object Switchboard assembles; response returns the HTML doc:
```json
// response
{ "html": "<!doctype html> …" }
```
Draft-only — Switchboard stores the HTML as a distribution draft for a human to send.

---

## social-media-posts-creator — 1 action endpoint

### Generate  ·  `POST /api/generate/`
Request body = post params (topic, brand, etc.); response = captions + image spec (any JSON):
```json
// response (example)
{ "captions": ["…"], "image_spec": { "…": "…" } }
```
Switchboard stores the JSON as a social draft.

---

## HC Viral Hits — ✅ already connected

`GET /api/cms/drafts/?brand=<brand>&status=ready` with header `X-API-Key` — verified working.
```json
{ "drafts": [ { "topic_id": 426, "title": "…", "content_type": "…", "word_count": 900 } ] }
```
**Optional add for cost routing:** a usage endpoint that exposes `compute_cost_cents`, e.g.
`GET /api/cms/usage/?brand=<brand>` → `{ "compute_cost_cents": 1234 }`. Switchboard already
converts cents → the governor's `llm_micros` unit; with this it can absorb HC-Viral spend
into the shared `spend_ledger`.

---

## content-depth-auditor — ✅ already connected

`GET /api/tracking/?brand=<brand>` → `{ "records": [ … ] }`. No change needed.

---

## Nothing needed

- **writers-dashboard** — Switchboard does **not** call it; it reproduced the writer-pace SQL
  directly against BigQuery. The Systems row is only a reachability probe. Per the
  consolidation decision it stays writer-tracking only.
- **daily-reporting-agent** — connected via Gmail; "top live articles" is computed on
  Switchboard's side from the BigQuery consum table. No endpoint to add.

---

## For cost routing (Albert)

To route Albert's spend into the governor, expose its per-run cost, e.g.
`GET /api/writer/usage/?brand=<brand>` → `{ "cost_micros": 50000 }` (micro-USD), or include a
`cost_micros` field on the writer-queue items. Switchboard will charge it to `spend_ledger`.

---

*Once these exist, give Switchboard each agent's base URL (+ API key). We wire the env var,
send the key, and verify each live — same as HC Viral Hits.*
