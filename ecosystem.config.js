// ecosystem.config.js — PM2 config for a Next.js production build.
//
// Pattern: `npm run build` once at deploy time (or in CI), then PM2
// runs `npm start` (which is `next start` under the hood). The .env
// file in this directory is auto-loaded the same way the FastAPI
// templates do — single source of truth.
//
// IMPORTANT: Next.js needs `.next/` built before `npm start` will work.
// After `git pull`, run `npm install && npm run build` BEFORE
// `pm2 restart` — otherwise the agent will fail to start.

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
const port = envFromFile.PORT || process.env.PORT || "3000";

module.exports = {
  apps: [
    {
      name: "nextjs-dashboard",
      script: "npm",
      args: "start",
      cwd: __dirname,
      // Restart caps — prevent crash loops. SHA's monitoring alerts on
      // >10 restarts.
      max_restarts: 10,
      min_uptime: "30s",
      env: {
        ...envFromFile,
        PORT: port,
        NODE_ENV: "production",
      },
    },
  ],
};
