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
- The **in-progress week judges accumulating counts on pace**, not on the full
  target. A week's work is due by the end of Saturday, so pace runs over six
  days and each finished day owes a sixth of the target; the bands then apply
  unchanged to that smaller number. Pace is measured against the close of
  yesterday, never the current hour, so Monday owes nothing and a cell never
  changes colour while someone is watching the board. Without this, every
  "do N things this week" metric reads red from Monday morning, which is noise:
  nobody is behind on Monday. Only accumulating metrics are paced
  (rollup=sum, direction=up). A point-in-time value - MRR, churn risk, a
  percentage - is already whole every day it is read, so scaling its target
  would call 5k of 25k MRR "on pace" on Tuesday, which is simply false.
  Closed weeks are never paced, and pace changes colour only: alerts score the
  last closed week, so nothing here can fire or suppress an escalation.

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
  10s via htmx - that poll is also how every Admin > Settings change reaches
  the TV, so it sets the latency for all of them; on a rotated token it
  bounces through `/tv` to recover; a
  "not updating" badge appears once nine polls in a row are missed (~90s at
  the 10s interval - it is counted in missed polls, not wall-clock, so it
  tracks the refresh rate automatically); hard-reloads every 6h to pick up
  deploys.
  Status sections (client health) sort worst-first: active red streaks,
  then yellow, no data, awaiting entry, green last. When a column would
  push rows below legibility, the greenest rows fold into a single "+N"
  summary row (chip colored by the worst hidden state) instead of shrinking
  the type - problems always stay visible, and the edit grid always shows
  the complete list. Curated numeric sections never fold.
  Optional night screensaver (Admin > Settings, off by default): between a
  configured start and end time (business timezone, window may cross
  midnight, e.g. 21:00-06:00) the board blacks out with only a dim clock
  that drifts position each minute (burn-in protection; the clock offsets
  itself by its own size, so it can drift edge to edge without clipping).
  Decided server-side on the normal refresh, so the board sleeps and wakes
  on its own within about ten seconds; the "not updating" badge still shows
  above the blackout if refreshes stop.
- **Edit grid** (login): same layout, tap a cell, type one number, done.
- **My numbers** (`/checkin`): the one-minute weekly entry surface. Each
  editor sees only the metrics they own (DRI), missing due-week numbers
  first and highlighted, big mobile-friendly inputs (one-tap G/Y/R), plus
  optional early entry for the current week. Each card also folds out
  "Earlier weeks" - the rest of the display window - for catching up on
  gaps (auto-expanded when any exist) or correcting a number after the
  fact. Late entry and retroactive correction are legitimate workflow, not
  cheating-proofed away; instead every write lands in the audit trail and
  surfaces on Admin > Activity. Logging in lands here automatically
  whenever numbers are missing; a nav badge shows the count. Nudges
  deep-link here via expiring magic links (`?t=`) that sign the DRI
  straight in - no password on a phone.
- **Admin**: sections/metrics CRUD (type, DRI, direction, rollup, start week,
  archive), per-quarter targets, users (roles: admin/editor/viewer, temp
  passwords, deactivate), API tokens, Slack settings, display-token rotation.
  **API token rotation**: rotating mints a replacement with the same name and
  scope and puts the old secret on a grace clock (default 7 days, 0 = cut over
  now) instead of killing it - so the integration keeps running while you paste
  the new value in. The old generation stays listed under the new one with its
  countdown, and is flagged "still in use" if anything has called with it since
  the rotation, which is the signal that it is not yet safe to revoke. Expired
  secrets stop authenticating on their own; revoke is still there for the
  compromised-token case, where downtime is the point.
  **View as user**: from the Users page an admin can see the app exactly as
  any active user does (their role, nav, My Numbers). A loud banner shows
  while active; edits made during view-as save normally but the audit trail
  records the real admin. State lives on the session and dies with it.
  **Activity** (`/admin/activity`): the audit trail made visible - last 200
  writes, old value -> new value, actor (never the impersonated user),
  source channel, and a LATE chip on anything written after that week's
  Wednesday-8am staleness deadline. Nothing blocks a late edit; nothing
  hides one either.
  **Setup & status** (`/admin/status`): does this instance actually do what it
  is configured to do? Every setting here is storable without being
  verifiable - a bot token is an opaque string whether it belongs to your
  workspace or someone else's, alerts and nudges are independent switches
  either of which means total silence, and an empty public base URL makes the
  nudge sweep stop before sending anything. The page answers from live state,
  in dependency order, so an unconfigured instance reads as a setup list and a
  configured one as a health dashboard. Each row is a state chip, what is
  actually true, and a deep link to the settings panel that fixes it; blocked
  rows also raise a count on the nav icon, so a broken instance is visible from
  every page. Checks come in two tiers: **local** ones read settings and the DB
  (safe on every page load), while **network** ones call Slack - `auth.test`
  for the workspace the token really belongs to, `conversations.info` for
  channel membership, `users.info` per stored member ID - and run only when you
  press Re-check, their results cached with a timestamp so the page states its
  own staleness instead of faking freshness. A member ID from another
  workspace resolves perfectly through Slack Connect, so those are compared on
  `team_id` and reported as wrong rather than fine. Saving the Slack panel
  verifies the token then and there and names the workspace in the
  confirmation. "Match Slack IDs from email" joins users to Slack accounts on
  email and shows the diff; an admin applies it, because an ID nobody checked
  is the failure being fixed. The page also shows the last run of each
  scheduled sweep *with its reason* - "skipped, no public base URL" - since a
  sweep that stops early is otherwise indistinguishable from one that never
  fired.
- **MCP** (`/mcp`): a remote MCP server exposing the board to Claude as tools -
  `get_scorecard` (full scored state), `get_check_in_status`, and
  `list_metrics`. `get_check_in_status` splits who still owes a number into
  **pending** (due, deadline not passed) and **stale** (already late), each
  with the DRI's Slack member ID so Claude can tag them: chasing only helps
  while a metric is pending, so a stale-only list would answer "who was late"
  rather than "who should I nudge now". The connector URL carries the token in
  its path and is shown once, next to a freshly created or rotated token on
  Admin > API tokens - tokens are hashed, so it cannot be reassembled later. Read-only, deliberately: writes over the API are attributed
  to a token, and the connector is one shared credential, so a write here would
  land in the audit trail as the connector rather than the person who reported
  the number. Reminders stay with the scheduler - the sweeps are idempotent and
  keep firing whether or not Claude is reachable; Claude is the ad-hoc layer on
  top ("who's behind?"), not the reliable one.
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

- The DM leads with the magic link as a named link ("Update your numbers
  here" - rendered per channel: `<url|label>` on Slack and Google Chat,
  markdown on Teams, a bare URL where there is no markup to render), then
  lists the missing metrics with targets, then offers the typed reply. Link
  first because it works for every metric type on every channel; the reply is
  the shortcut for people who would rather not leave the thread.
- Replying `1: 12, 2: G` records those values directly - parsed by a strict
  deterministic grammar, **never by AI** - and a confirmation DM reports the
  saved values with their scored colors. `help` re-sends the numbered list.
  Bad items come back itemized. The grammar is forgiving about shape but not
  about meaning: `1: 12`, `1. 12`, `1) 12`, `#1 12`, `1 - 12` and one-per-line
  all work, a comma inside a number stays part of it (`1: $1,500`), and a
  bare list is taken in order **only** when its length exactly matches the
  metrics asked for - a partial list would be guessing which were answered.
  `1.5` stays the number 1.5, never item 1 value 5.
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
