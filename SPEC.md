# Scorecard - Product Spec

A company scorecard web app in the EOS / Dan Martell tradition: one screen that
shows whether the business is on track, updated weekly by a small team, displayed
full-time on an office TV, and readable by AI agents over a JSON API.

Company-specific configuration (people, clients, targets, historical data,
deployment) lives in gitignored local files, never in this repo. See
migrate/seed_data.example.json for the shape.

## Core ideas

- **Rolling weeks, no month tabs.** Weeks run Monday-Sunday and roll continuously.
  The edit grid shows the last 2-4 calendar months (admin setting) of weekly
  columns grouped under month header bands, labeled quarter-relative
  (Q3-W1 ... W13/W14). Nobody ever wonders when a week starts or which sheet
  tab to open.
- **A week belongs to the month/quarter containing its Monday.** One rule drives
  month bands, quarter labels, and target selection.
- **Nothing derived is stored.** Colors, streaks, staleness, and subtotals are
  pure functions over entries; retroactive edits recompute everything.

## Scoring

- Numeric metrics: green >= 100% of target, yellow 70-99%, red < 70%. Metrics can
  be direction=down (lower is better); the ratio inverts.
- Binary metrics: green or red only. No partial credit on a yes/no.
- Status metrics: DRI sets R/Y/G directly (e.g. per-client health rows).
- Targets are per quarter with a ramp: baseline applies quarter-weeks 1-6,
  stretch from week 7 through quarter end (including W14 when it exists).
- A metric with no target shows its raw value neutrally, is excluded from red
  streaks, but staleness still applies: the data is due regardless.

## Staleness vs red

Entries for last week are due Monday end of day (business timezone). If nothing
is entered by Wednesday 08:00, the cell turns gray ("no data") and a Slack alert
fires. Gray is deliberately distinct from red ("bad number"): different problem,
different conversation.

## Red escalation ladder

- Week 1 red: the DRI files a 1-3-1 (one problem, three options, one
  recommendation) in-app before the weekly sync.
- Week 2 red on the same metric: a 15-minute 1:1 outside the sync.
- Week 3+: structural conversation.
Streak counting skips stale weeks: you cannot dodge escalation by not entering
a number. Each escalation level Slack-notifies exactly once (dedupe table).

## Surfaces

- **TV display** (`/tv` redirects to `/display?token=...`): read-only, no
  login, tokenized URL. One dark board sized in viewport units so it fills
  any TV exactly once at any resolution or zoom - no scrolling, ever. Top to
  bottom: goal band (configurable metric, long-range goal, pace marker,
  milestones), metric rows in two balanced columns (status-colored value chip
  vs target, owner chip, 4-week trend, red-streak / no-data / last-wk flags),
  and an ACT footer line with each red's escalation step. Refreshes every
  60s via htmx; on a rotated token it bounces through `/tv` to recover; a
  "not updating" badge appears if refreshes stop; hard-reloads every 6h to
  pick up deploys.
- **Edit grid** (login): same layout, tap a cell, type one number, done.
- **Admin**: sections/metrics CRUD (type, DRI, direction, rollup, start week,
  archive), per-quarter targets, users (roles: admin/editor/viewer, temp
  passwords, deactivate), API tokens, Slack settings, display-token rotation.
- **API** (`/api/v1`): bearer-token access for AI agents and automations.
  GET /scorecard returns full scored state (including stale and red lists);
  POST /metrics/{id}/entries writes values. Same scoring code path as the UI.

Every surface names the metric's DRI next to the item: TV views, edit grid,
summary strips, 1-3-1 page, admin pages, and API responses. Accountability is
never more than a glance away.

## Review cadence (methodology, enforced socially not in code)

Weekly sync, scorecard first, discuss only yellows and reds. Green means no
discussion. Sparklines give 4 weeks of context so a first-time dip reads
differently than a long slide.
