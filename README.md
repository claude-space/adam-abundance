# Switchboard

A thin **orchestration + shared-state layer** over the Valnet Auto portfolio's
existing systems (HotCars, CarBuzz, TopSpeed). Switchboard does not rewrite any
existing system — it **wraps** them as audited tool adapters, coordinates a small
team of specialized agents against a single shared memory, and puts a
**governor** in front of every action so an always-on multi-agent loop can't
quietly burn money or push bad work to production.

See [`PRD-switchboard.md`](PRD-switchboard.md) for the full spec. This README is
the operator's view.

## The one rule everything else follows

> **Agents coordinate only through shared memory. They never call each other and
> never call each other's tools.** An agent that produces a finding writes it to
> Postgres; any agent that needs it *queries* memory. The orchestrator dispatches
> work by writing approved `plan_item` rows that the assigned agent picks up.

## Guardrails (non-negotiable — PRD §3, §8, §14)

- **Nothing hits production without a recorded human approval.**
- **External actions are dry-run by default.** Live writes need an *approved*
  `plan_item` with `dry_run=false`.
- **The governor hard-caps spend** — Ahrefs units, LLM micros, BigQuery bytes —
  per run and per day; a hit cap refuses the action and writes a flag.
- **Facts are search-verified or they're claims.** The Research fact-gate is the
  only path to `verified=true`.
- **Secrets live in the credentials layer** — never in code, prompts, memory, or
  logs (all logging is redacted).
- **Distribution is draft + human-send.** Digests/newsletters/social posts are
  assembled for review; Switchboard never sends or posts autonomously.
- **Paid-media is read-only.** Never touches bids, budgets, or campaigns.
- No swarm framework, no CRM/sales agent, no raw filesystem access for agents.

## Architecture

```
Editor/admin ── Orchestrator (+ governor) ── Slack (notify)
                      │
             Shared memory (Postgres): typed entries · provenance · TTL
                      │
   Research · Opportunity · Production · Analytics · Reporting&Distribution · Paid-Media
                      │
        tool adapters (read → memory; act → governor-gated side effect)
                      │
             Credentials & access layer (service accounts · API keys)
```

Six worker agents, each owning one data domain with no overlap; an orchestrator
that holds the governor; scheduled feeders (ranking-decay, content-audit) that
drop typed entries into memory. Full detail in the PRD (§5–§6).

## Layout

```
src/switchboard/
  config.py         Non-secret operational config (brands, models, caps, flags)
  credentials.py    The credentials plane: secret resolution + redaction registry
  logging_.py       Redacting logger (registry + shape backstops)
  interfaces.py     Component contracts (ToolAdapter/Agent/Governor + DTOs)
  context.py        RunContext: store + governor + creds bundled per transaction
  db/               SQLAlchemy models + enums + async engine (the §7 DDL)
  memory/           MemoryStore: typed read/write, fact-gate, TTL sweep, audit log
  governor/         Spend caps, approval gate, kill switch, provenance enforcement
  adapters/         Integration plane (Phase 1+): one wrapper per existing system
  agents/           The six worker agents (Phase 2+)
  orchestrator/     Morning cycle: plan → approve → dispatch (Phase 3+)
  api/              FastAPI approval surface + observability (Phase 3+)
migrations/         Alembic (async) — 0001_initial matches the PRD DDL
docs/               INTEGRATION-NOTES.md — how each wrapped system actually works
```

## Running it locally

Secrets come from `switchboard.env` (already present; **gitignored — never
commit**). Override the path with `SWITCHBOARD_ENV_FILE`.

```bash
# 1. Install (core only; heavy SDKs are optional extras)
pip install -e .                       # add: .[data,ads,browser,research] as needed

# 2. Start Postgres (shared memory)
docker compose up -d

# 3. Apply the schema
alembic upgrade head

# 4. Verify the foundation
switchboard selfcheck                  # config + redaction + dummy write/read + TTL sweep
```

Other commands: `switchboard sweep` (TTL sweep), `switchboard observe <agent>
<brand>` (Phase 2+), `switchboard cycle <brand>` (Phase 3+), `switchboard serve`
(approval UI/API, Phase 3+).

## Build status (phased — PRD §12)

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations: memory, governor, credentials, migrations | ✅ built |
| 1 | Read adapters (observe only) — all six domains | ✅ built |
| 2 | Worker agents + Research fact-gate | ✅ built |
| 3 | Orchestrator + approval surface (Google OAuth) + governor caps | ✅ built |
| 4 | Action adapters + dispatch (dry-run first) | ✅ built |
| 5 | Scheduled feeders (decay + content-audit) + observability | ✅ built |
| 6 | Hardening: retries/backoff, TTL + supersede sweeps, kill switch, tests | ✅ built |

**Built vs. verified.** Every phase's code is in place and the tree compiles;
the dependency-free safety tests (secret redaction, LLM cost model) pass. The
remaining acceptance criteria (migrations apply, live dry-run/live dispatch,
end-to-end cycle on HotCars) require a Postgres instance + `pip install -e .` and
should be run in a real environment — see *Verification* below. Adapters that hit
live external systems (Ahrefs, Emaki, ad platforms, Albert/Seona endpoints) are
written to spec and degrade softly; a few Albert/Seona routes are env-configurable
and marked pending endpoint confirmation.

## Verification

```bash
pip install -e ".[data,research]"      # add ads,browser for those adapters
docker compose up -d && alembic upgrade head
switchboard selfcheck                   # config + redaction + dummy write/read + TTL sweep
switchboard observe analytics hotcars   # one agent's observe pass
switchboard cycle hotcars               # full observe → draft plan → brief
switchboard serve                       # approval UI at http://localhost:8080
# In the UI: approve items (dry-run or LIVE), then Dispatch. Governor gates every item.
pytest                                  # full suite (needs the stack installed)
```

Dependency-free tests run anywhere: `PYTHONPATH=src python tests/test_redaction.py`.

## Security note

`switchboard.env` holds **live production secrets**, several individually-owned.
It is gitignored. Move these into a secret manager and rotate anything
consolidated away (the file's own header lists keys flagged for rotation). The
credentials layer registers every secret it resolves with the redacting logger,
so values never reach logs or the `tool_call_log` audit table.
