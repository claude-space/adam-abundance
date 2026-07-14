# Deploying Switchboard on the ShellAgent VM

Switchboard runs on the **same VM as the Auto services** (Claude Albert, Seona,
HC Viral Hits, …) so it reaches them over `localhost`. Two long-running
processes: the **web console** (`switchboard serve`) and the **scheduler**
(`switchboard schedule`). The database is external (Supabase recommended).

Your configured posture (`.env.prod.example`): **spend caps off**, **Slack not
wired** (brief is logged, never posted), **Google OAuth on**, **kill switch on
at first** so it observes + plans but cannot dispatch until you flip it.

---

## 1. Database — Supabase

Supabase *is* Postgres, so the schema runs unchanged and there's nothing to
back up yourself.

1. Create a Supabase project.
2. **Project Settings → Database → Connection string → URI.** Use the **Session
   pooler** or **Direct connection (port 5432)** — *not* the Transaction pooler
   (6543), which breaks `asyncpg`'s prepared statements.
3. Put it in `switchboard.env` as `DATABASE_URL`, changing the scheme to
   `postgresql+asyncpg://…`.
   - If you can *only* use the 6543 transaction pooler, also set
     `DB_STATEMENT_CACHE_SIZE=0`.

> Prefer a **self-hosted** Postgres instead (policy/latency)? `docker compose up
> -d` brings up the bundled one; point `DATABASE_URL` at it. Everything else is
> identical.

Apply the schema (both DB choices):
```bash
alembic upgrade head          # creates memory/plan/plan_item/tool_call_log/spend_ledger/app_user
```

---

## 2. Google OAuth

1. Google Cloud Console → **APIs & Services → Credentials → Create OAuth client
   ID → Web application**.
2. **Authorized redirect URI** must EXACTLY equal `GOOGLE_OAUTH_REDIRECT_URI`,
   e.g. `https://switchboard.internal.valnet/auth/callback`. Google requires
   **HTTPS** (only `http://localhost` is exempt) — serve behind the VM's reverse
   proxy with TLS. If the proxy mounts the app under a subpath, start uvicorn
   with `--root-path /agents/switchboard` and use that path in the redirect URI.
3. OAuth **consent screen**: Internal (Valnet Workspace).
4. Fill `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`,
   `AUTH_ALLOWLIST`, `AUTH_ADMINS`, `SESSION_SECRET`. Scopes stay `openid email
   profile` — no Gmail/Sheets/BQ at login (§9.1).

First person to sign in (or anyone in `AUTH_ADMINS`) is provisioned
**global_admin**; manage everyone else from the in-app **Users** page.

---

## 3. Secrets

Copy the working resource-credential lines from your existing `switchboard.env`
(Sentinel, Asana PAT, Ahrefs, Similarweb, the Google SA JSON, Gmail, ad
platforms, HC-Viral key, sheet IDs). Consolidate the seven Anthropic keys into
one dashboard-owned `ANTHROPIC_API_KEY` and rotate the leaked one flagged in the
file header. Better: set `SECRETS_MANAGER_PROJECT=data-science-458422` and store
them in GCP Secret Manager (secret name = lower-kebab of the env var, e.g.
`anthropic-api-key`); env stays the fallback.

---

## 4. Install & run

### Option A — native + systemd (recommended; matches the sibling apps)
```bash
sudo mkdir -p /home/robert/switchboard && cd /home/robert/switchboard
# copy the repo here (or git clone), then:
python3.12 -m venv .venv
./.venv/bin/pip install -e ".[data,research]"     # add ,ads,browser for those adapters
cp .env.prod.example switchboard.env && $EDITOR switchboard.env   # fill in §1–3
./.venv/bin/alembic upgrade head
./.venv/bin/switchboard selfcheck                  # config + redaction + DB + TTL sweep

sudo cp deploy/switchboard-web.service deploy/switchboard-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switchboard-web switchboard-scheduler
sudo systemctl status switchboard-web
```
Point the reverse proxy (TLS) at `127.0.0.1:8080`.

### Option B — Docker
```bash
cp .env.prod.example switchboard.env && $EDITOR switchboard.env
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec web alembic upgrade head
```
(Uses `network_mode: host` so the containers reach the sibling services on
localhost — Linux only.)

---

## 5. Turn it on — safely, in tiers

The kill switch starts **on**, so Tiers 1–2 run and Tier 3 is blocked until you
decide.

1. **Observe + plan (safe).** With `SWITCHBOARD_KILL_SWITCH=1`, let the scheduler
   run (or `switchboard cycle hotcars` by hand). Open the console, review the
   **Dashboard**, **Memory**, and the day's **Plan** — everything is read-only /
   dry-run. Watch **Observability** to confirm BigQuery/Sentinel reads and LLM
   spend look right.
2. **Confirm the action endpoints.** On the **Systems** page, greens are wired;
   fill the `*_PATH` / Asana GID gaps for anything you intend to run live, and
   verify the sibling services answer on their localhost ports.
3. **Go live, one action at a time.** Set `SWITCHBOARD_KILL_SWITCH=0` and
   restart. In a plan, use **Approve LIVE** on a *single* low-risk item (e.g. a
   `create_asana_task`), **Dispatch**, and confirm exactly one real effect + the
   audit row. Widen from there. Add `carbuzz,topspeed` to `SWITCHBOARD_BRANDS`
   once HotCars is trusted.

Re-engage the kill switch any time (`SWITCHBOARD_KILL_SWITCH=1` + restart) — it
halts all dispatch/live actions while observe keeps running.

---

## Operations quick reference
| Command | Purpose |
|---|---|
| `switchboard selfcheck` | verify config, redaction, DB, TTL sweep |
| `switchboard cycle <brand>` | observe all agents → draft plan (no dispatch) |
| `switchboard plan <brand>` | re-synthesize a plan from current memory |
| `switchboard dispatch <plan_id>` | dispatch an approved plan (governor-gated) |
| `switchboard feed decay\|content_audit\|trend_scan <brand>` | run a feeder once |
| `switchboard trend-scan [brand]` | one competitor trend scan (sources → trends → trigger requests) |
| `switchboard pipeline-worker` | process queued/stuck content-pipeline jobs once |
| `switchboard sweep` | expire stale + supersede duplicate memory |
| `switchboard schedule` | run the cron loop (cycle 7:30 ET + feeders + trend scan + job sweep) |
| `switchboard serve` | the web console |

Logs: `journalctl -u switchboard-web -f` / `-u switchboard-scheduler -f`.
Health: `GET /healthz`. Secret-free by construction — the redacting logger
scrubs every credential from logs and the `tool_call_log` audit table.
