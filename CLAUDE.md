# Claude-Space — Workspace Rules

Read this before doing anything else in this workspace. The agent-specific instructions live in `CLAUDE-agent.md` (next to this file); read those second.

## What this workspace is
Your Claude-Space workspace is the shared root for all the agents you own at Valnet. Each agent has its own sub-folder under `claude-space-<your-name>/<agent-name>/` in Drive and its own repo at `github.com/claude-space/<agent-name>`.

## Rules
- **Read `CLAUDE.md` (this file) and `CLAUDE-agent.md` (next door) first** every time you open a new Claude Code session.
- **Read `PRD.md`** (in the agent's Drive folder) and any docs in `docs/` before writing code.
- **Code lives in `Projects/`.** Everything else is documentation, config, or output.
- **Outputs go to `Outputs/`.** Never write reports/exports/results directly into `Projects/`.
- **Credentials are in `~/.claude/.env`** on your local machine only. Never commit them. Never write them to Drive.
- **Push to `github.com/claude-space/<agent-name>`.** Not your personal fork. The auto-deploy webhook only watches the canonical repo.
- **Per-agent env vars are prefixed by the agent's name** in your `~/.claude/.env`. E.g. `TESTING_SUPABASE_URL`, `DASHBOARD_SUPABASE_URL`. This avoids collisions when you own multiple agents.

## Conventions
- Markdown for all docs.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`) for the repo history.
- Outputs include a date in the filename when relevant.
- The agent's `README.md` covers what + why; `CLAUDE-agent.md` covers identity + production details; `docs/architecture.md` covers the how.

## Using your agent in Workflows
ShellAgent Workflows let you chain agents together in a visual pipeline. Your agent can be a step in any workflow in one of two ways:

**Chat agents — works automatically, nothing to build.**
If your agent is a standard ShellAgent chat agent, it already works as a workflow step. ShellAgent routes the workflow prompt through your agent's chat path and returns the response.

**Dashboard / VM agents — add a `/run` endpoint.**
If your agent is a live app on the VM (Next.js, FastAPI, Express, etc.), add a `POST /run` route so Workflows can call it directly.

Request your endpoint will receive:
```
POST /run
Authorization: Bearer <your-token>
Content-Type: application/json

{ "input": "<the prompt from the workflow step>" }
```

Response your endpoint must return (always JSON, even on errors):
```
// success — 200
{ "output": "<your result as a plain string>" }

// error — 4xx or 5xx
{ "error": "<description of what went wrong>" }
```

Rules:
- Always return `Content-Type: application/json`.
- Respond within 5 minutes (hard timeout).
- `output` must be a plain string — the next workflow step receives it as `{{previous_output}}`.
- Validate the `Authorization: Bearer <token>` header; return 401 if it doesn't match.

Once built, register it in ShellAgent: Studio › your agent › Settings → Workflow endpoint → paste the URL and token. After that your agent appears as a step option in the workflow builder.

## Help
- Slack `#shellagent-help` for platform issues.
- Slack DM Hich or Andrew for credentials / Drive / GitHub issues.

---

*This file is the workspace-level instruction set. The agent-level instructions are in **`CLAUDE-agent.md`** next to this file.*
