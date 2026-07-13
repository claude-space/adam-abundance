#!/usr/bin/env bash
#
# bootstrap.sh — one-time (and idempotent) VM setup for the Switchboard agent.
#
# Run this once after the first clone, and again whenever Python dependencies
# change. It creates the project virtualenv, installs the package, and applies
# database migrations. After it succeeds, start the app with:
#
#     pm2 start ecosystem.config.js
#
# The deploy webhook (on push to main) only needs:
#
#     git pull && pm2 reload ecosystem.config.js --update-env
#
# Requirements: Python >= 3.11 on PATH (set $PYTHON to override), and a populated
# repo-local .env (see .env.prod.example) providing DATABASE_URL etc.

set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
  done
fi
echo "==> Using interpreter: $PYTHON ($($PYTHON --version 2>&1))"

# 1) virtualenv
if [ ! -d .venv ]; then
  echo "==> Creating virtualenv at .venv"
  "$PYTHON" -m venv .venv
fi

# 2) install the package (+ data extras for BigQuery/Sheets/etc.)
echo "==> Installing switchboard into .venv"
.venv/bin/pip install --upgrade pip >/dev/null
if .venv/bin/pip install -e '.[data]' 2>/dev/null; then
  echo "    installed with [data] extras"
else
  echo "    [data] extras unavailable; installing base package"
  .venv/bin/pip install -e .
fi

# 3) load .env into the environment for the migration step (PM2 does this for the
#    running processes; the one-off alembic run below needs it too). Only simple
#    KEY=VALUE lines; surrounding quotes are stripped.
if [ -f .env ]; then
  echo "==> Loading .env for migration"
  set -a
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key=${line%%=*}
    val=${line#*=}
    val=${val#\"}; val=${val%\"}
    val=${val#\'}; val=${val%\'}
    export "$key=$val"
  done < .env
  set +a
  # Let the credentials layer also read the file directly (belt + suspenders).
  export SWITCHBOARD_ENV_FILE="$PWD/.env"
else
  echo "!! No .env found — create one from .env.prod.example before running migrations."
  exit 1
fi

# 4) migrate
echo "==> Applying database migrations (alembic upgrade head)"
.venv/bin/alembic upgrade head

echo ""
echo "==> Bootstrap complete."
echo "    Start:   pm2 start ecosystem.config.js && pm2 save"
echo "    Verify:  .venv/bin/switchboard selfcheck"
