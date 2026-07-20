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
  Status sections (client health) sort worst-first: active red streaks,
  then yellow, no data, awaiting entry, green last. When a column would
  push rows below legibility, the greenest rows fold into a single "+N"
  summary row (chip colored by the worst hidden state) instead of shrinking
  the type - problems always stay visible, and the edit grid always shows
  the complete list. Curated numeric sections never fold.
- **Edit grid** (login): same layout, tap a cell, type one number, done.
- **My numbers** (`/checkin`): the one-minute weekly entry surface. Each
  editor sees only the metrics they own (DRI), missing due-week numbers
  first and highlighted, big mobile-friendly inputs (one-tap G/Y/R), plus
  optional early entry for the current week. Logging in lands here
  automatically whenever numbers are missing; a nav badge shows the count.
  Slack nudges deep-link here via expiring magic links (`?t=`) that sign
  the DRI straight in - no password on a phone.
- **Admin**: sections/metrics CRUD (type, DRI, direction, rollup, start week,
  archive), per-quarter targets, users (roles: admin/editor/viewer, temp
  passwords, deactivate), API tokens, Slack settings, display-token rotation.
  **View as user**: from the Users page an admin can see the app exactly as
  any active user does (their role, nav, My Numbers). A loud banner shows
  while active; edits made during view-as save normally but the audit trail
  records the real admin. State lives on the session and dies with it.
- **API** (`/api/v1`): bearer-token access for AI agents and automations.
  GET /scorecard returns full scored state (including stale and red lists);
  POST /metrics/{id}/entries writes values. Same scoring code path as the UI.
  POST /metrics/{id}/archive retires a row (the soft delete behind "this client
  churned") and /unarchive restores it; both need an admin-scoped token, because
  a bad number stays visible and argues with you while a vanished row does not.
  Archiving removes the row from every surface outright - board, edit grid, API,
  alerts - whatever its archive date. The optional effective_week only records
  *when* they left; it drives the na-tail in scoring but no shipped view passes
  include_archived, so today it is an honest date for the record, not a display
  change.

Every surface names the metric's DRI next to the item: TV views, edit grid,
summary strips, 1-3-1 page, admin pages, and API responses. Accountability is
never more than a glance away.

## Weekly check-in nudges (two-way messaging)

If enabled (Settings > Check-in nudges; requires the alerts master switch),
DRIs whose due-week numbers are missing get a message - Monday 16:00 and/or
Tuesday 09:00 Chicago, each metric nudged at most once per round. Slack is
the first-class channel; each user's channel is chosen on the Users page:

- The DM numbers their missing metrics with targets, includes a magic link
  to My Numbers, and shows the reply format.
- Replying `1: 12, 2: G` records those values directly - parsed by a strict
  deterministic grammar (index + number / G/Y/R / yes/no), **never by AI** -
  and a confirmation DM reports the saved values with their scored colors.
  `help` re-sends the numbered list. Bad items come back itemized.
- The numbered list each user was shown is pinned server-side
  (slack_prompts), so filling a metric on the web between nudge and reply
  can never shift someone's reply onto the wrong metric.
- Entries land with `source='slack'`, attributed to the replying user.
- Inbound events are verified with Slack request signing (signing secret,
  5-minute replay window) and deduplicated by event id.
- Setup (one-time, documented in Settings): Slack app with bot scopes
  `chat:write` + `im:history`, Event Subscriptions pointed at
  `/slack/events` with `message.im` subscribed, signing secret pasted into
  Settings, public base URL set, member IDs on the Users page.

Additional channels (Settings > More channels, all optional; users whose
channel is not configured are simply skipped by the sweep):

- **Telegram** - two-way. Bot token from @BotFather; "register webhook"
  points the bot at `/telegram/webhook` with a generated secret token. A
  user messages the bot once and it replies with the chat ID to enter on
  the Users page. Replies write `source='telegram'`.
- **Twilio SMS / WhatsApp** - two-way. Account SID + auth token + From
  number; point the number's incoming webhook at `/twilio/webhook`
  (X-Twilio-Signature verified against the public base URL, reply returned
  inline as TwiML). Users are matched by normalized phone number; entries
  write `source='sms'` / `'whatsapp'`.
- **Microsoft Teams / Google Chat** - notify-only (incoming webhooks post to
  one shared channel/space; per-user DMs would need a full bot). The post
  leads with the owner's name, lists their missing metrics, and carries the
  magic link; no reply parsing.

All two-way channels share one reply pipeline (`app/replies.py`) and the
pinned prompt list, so the grammar, confirmations, and safety rules are
identical everywhere. Email is deliberately not implemented yet.

## Demo data mode

Admin > Settings > "Demo data" fills the board surfaces (TV display, edit
grid, cell editing, 1-3-1s) with a fictional company for screenshots, screen
recordings, and product demos. Rules:

- Real data is never modified. The demo lives in a separate throwaway SQLite
  file (`demo.db` next to the real DB); the toggle itself is the only thing
  written to the real DB.
- The dataset is generated relative to the current week and scripted to show
  every capability at once: a climbing MRR goal band, mostly-green momentum
  with sparklines, yellows, a two-week red (15-min 1:1 due), a fresh red
  awaiting its 1-3-1, an escalated client with its 1-3-1 filed, a stale gray
  cell in history, and enough clients that the TV folds the greenest rows.
- Edits made while the toggle is on land in the demo copy - so the editing
  workflow itself can be demoed - and are discarded on rebuild.
- The demo DB is rebuilt fresh each time the toggle turns on, each new week,
  and whenever the generator version changes.
- Login, admin pages, Slack alerts, and the JSON API always use real data;
  the TV display URL (real token) keeps working and shows the demo while on.

## Review cadence (methodology, enforced socially not in code)

Weekly sync, scorecard first, discuss only yellows and reds. Green means no
discussion. Sparklines give 4 weeks of context so a first-time dip reads
differently than a long slide.
