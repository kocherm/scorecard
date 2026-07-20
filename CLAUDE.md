# Scorecard

Company scorecard web app: TV display mode, weekly editing, Slack alerts, JSON API
for AI-agent integration. Methodology and product rules: SPEC.md.

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
