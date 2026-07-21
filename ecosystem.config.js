// ecosystem.config.js — PM2 config for the Switchboard FastAPI app (Python).
//
// This agent is a Python / FastAPI app, NOT the Next.js starter it was scaffolded
// from. PM2 runs two processes from the project virtualenv (.venv):
//
//   switchboard-web        uvicorn (the approval console + /api/* + /run)
//   switchboard-scheduler  APScheduler loop (morning cycle + feeders)
//
// ONE-TIME VM SETUP (before the first `pm2 start`): create the venv + install the
// package. See deploy/bootstrap.sh — run it once after the first clone. The deploy
// webhook then just needs `git pull && pm2 reload ecosystem.config.js --update-env`
// on push to main: the `switchboard-web` (`serve`) process runs `alembic upgrade
// head` on startup (see cli._auto_migrate), so pending migrations self-apply on
// every reload — no manual migrate step, and only the web process migrates (the
// scheduler doesn't, so they never race). Disable with SWITCHBOARD_AUTO_MIGRATE=0.
// Re-run bootstrap if Python deps change (it is idempotent).
//
// The repo-local `.env` (gitignored) is the single source of truth for config +
// secrets, loaded here and injected into both processes — same pattern as the
// Next.js template. Populate it from .env.prod.example.

const fs = require("fs");
const path = require("path");

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const out = {};
  for (const raw of fs.readFileSync(filePath, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

const envFromFile = loadEnvFile(path.join(__dirname, ".env"));
const port = envFromFile.PORT || process.env.PORT || "3150";
// Linux VM virtualenv interpreter (created by deploy/bootstrap.sh).
const python = path.join(__dirname, ".venv", "bin", "python");

const sharedEnv = {
  ...envFromFile,
  PORT: port,
  PYTHONUNBUFFERED: "1",
};

const common = {
  interpreter: "none", // `script` is the venv python; exec it directly (no node wrapper)
  cwd: __dirname,
  max_restarts: 10,     // SHA monitoring alerts on >10 restarts
  min_uptime: "30s",
  env: sharedEnv,
};

module.exports = {
  apps: [
    {
      ...common,
      name: "switchboard-web",
      script: python,
      args: ["-m", "switchboard", "serve", "--port", port],
    },
    {
      ...common,
      name: "switchboard-scheduler",
      script: python,
      args: ["-m", "switchboard", "schedule"],
    },
  ],
};
