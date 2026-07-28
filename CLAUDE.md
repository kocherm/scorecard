# Scorecard

Company scorecard web app: TV display mode (with optional scheduled night
screensaver), weekly editing, Slack alerts, JSON API for AI-agent integration.
Methodology and product rules: SPEC.md.

## Privacy rule (hard)

This repo is public. NOTHING company-specific goes in tracked files: no client or
staff names, no emails, no revenue figures, no hostnames, IPs, ports, or SSH users.
Company data lives only in gitignored files: migrate/seed_data.local.json,
SPEC.local.md, deploy/DEPLOY.local.md. Check `git grep` before every commit.

## Architecture

- FastAPI + Jinja2 + htmx, SQLite (WAL) at data/scorecard.db, single container.
- app/weeks.py + app/scoring.py are PURE (no I/O, clock passed in) - keep them that
  way; they are the unit-tested core and feed TV, edit grid, API, and alerts identically.
- Week key = Monday date "YYYY-MM-DD" in the business timezone (America/Chicago).
  A week belongs to the month/quarter of its Monday. Never store derived state
  (colors, streaks, subtotals).
- Alert dedupe lives in alerts_sent; sweeps are idempotent, scheduled by APScheduler
  (stale: Wed 08:00, red ladder: Tue 08:00, business timezone).
- app/mcp.py is a remote MCP server (JSON-RPC over Streamable HTTP at /mcp) so
  Claude can READ the board conversationally. It is read-only on purpose: an
  API write is attributed to a token, and the connector authenticates as one
  shared credential, so a write would show up in Activity as "the Claude token"
  instead of the person. Its tools call app/api.py's build_scorecard /
  metrics_rows - never a second query path.
- app/readiness.py backs Admin > Setup & status. Two tiers, and the split is the
  whole design: local checks read settings/DB only and run on every admin page
  load (they drive the nav badge); network checks call Slack and run ONLY from
  the Re-check button, caching into settings under verify_<key> with a
  timestamp. Never move a network check onto a page load. Checks must derive
  from the helpers the app already uses (channels.ready, entry_ops,
  db.get_setting) - readiness must not become a second source of truth about
  what is configured. Sweeps record every run in sweep_runs, including the
  early returns, so a skip has a reason attached.
- App config is key-value rows in the settings table (db.get_setting/set_setting),
  edited on Admin > Settings. Settings always live in the REAL db - demo mode
  swaps only the data db, so TV behavior toggles (demo, screensaver) keep
  working while demo is on.
- The TV decides everything server-side on its 10s htmx poll (board content,
  screensaver on/off) - never add client-side clocks or state to display.html.
  That poll is the ONLY path a settings change takes to the TV, so it doubles
  as the refresh latency for every admin toggle; keep it short.

## Commands

```bash
uv sync                          # deps
uv run pytest -q                 # engine tests
uv run python -m migrate.seed    # seed empty DB (prints creds ONCE)
uv run uvicorn app.main:app --port 8096   # dev server
docker compose up -d --build     # prod-style run on 127.0.0.1:8096
```

## Gotchas

- migrate/seed_data.local.json is required to seed; copy from seed_data.example.json.
- Passwords/tokens are hashed in DB; temp passwords and API tokens print exactly once.
- Styling: CSS custom properties in app/static/scorecard.css only - no new hex
  values, no emoji in UI. Brand reference lives outside this repo.
- Deployment specifics: deploy/DEPLOY.local.md (gitignored). Same file covers
  the office TV kiosk (a Pi running WPE/cog pointed at /tv - no desktop, no
  login; /tv resolves the display token server-side).
