# Scorecard

Company scorecard web app: TV display mode (with optional scheduled night
screensaver), weekly editing, Slack alerts, JSON API for AI-agent integration.
Methodology and product rules: SPEC.md.

## Privacy rule (hard)

This repo is public. NOTHING company-specific goes in tracked files: no client or
staff names, no emails, no revenue figures, no hostnames, IPs, ports, or SSH users.
Company data lives only in gitignored files: SPEC.local.md,
deploy/DEPLOY.local.md, and anything matching `*.local.json` (seed data, the
Slack app manifest). Check `git grep` before every commit.

## Architecture

- FastAPI + Jinja2 + htmx, SQLite (WAL) at data/scorecard.db, single container.
- app/weeks.py + app/scoring.py are PURE (no I/O, clock passed in) - keep them that
  way; they are the unit-tested core and feed TV, edit grid, API, and alerts identically.
- Week key = Monday date "YYYY-MM-DD" in the business timezone (America/Chicago).
  A week belongs to the month/quarter of its Monday. Never store derived state
  (colors, streaks, subtotals).
- The IN-PROGRESS week scores accumulating counts on PACE (scoring.pace_fraction),
  against what the FINISHED days owed - not the full target, and not the current
  hour. Three of eight videos on a Thursday is on schedule, not red. Two rules
  make this safe and both are load-bearing: only rollup='sum' + direction='up'
  metrics pace, because a point-in-time value (MRR, a percentage) is already
  whole every day it is read and scaling its target would call 5k of 25k MRR
  "on pace"; and pace reads the DATE, never the clock, so a cell cannot change
  colour while someone is watching the board and nobody is called behind at 5pm
  for work due Saturday. Closed weeks are never paced. This is colour only -
  alerts score last_closed_week exclusively (alerts.py), so pace can neither
  fire nor suppress an escalation. MetricInfo.rollup defaults to None so any
  caller that omits it keeps the old unpaced behaviour rather than silently
  acquiring a new one.
- Alert dedupe lives in alerts_sent; sweeps are idempotent, scheduled by APScheduler
  (week-closed summary: Tue 08:00, red ladder: Tue 08:05, stale roll-up: Wed 08:00,
  business timezone). Message VOLUME is a rule, not a detail: the channel gets
  counts and the DM gets to-dos, so every sweep posts at most ONE top-level
  message (alerts.post_rollup) with its per-metric detail in a thread, and DMs
  one message per PERSON, never per metric. Posting per metric is what this
  replaced - fourteen posts one Wednesday morning, which is an alert nobody
  reads. slack_threads holds the parent ts per week+kind and doubles as the
  idempotency record, so a re-run appends instead of announcing twice; ''
  means "posted through a webhook", which cannot thread. Red week 1 is DM-only
  on purpose (SPEC.md) - spending the channel on the most common rung is how
  it gets muted before week 3 arrives. red_sweep runs 5 minutes AFTER
  summary_sweep because it replies into that thread; same-minute cron jobs
  would race for the parent.
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
- Credentials in transit (password reset links) go ONLY over channels.PRIVATE
  and ONLY to a public_base_url that is configured. Both halves are load-bearing:
  channels.ready is true for Teams/Google Chat, but those are shared-space
  webhooks, so a reset link there is an account takeover for whoever clicks
  first - use channels.deliver_secret, never ready. And the base URL must come
  from the setting, never request.base_url: the app binds the LAN directly, so
  a forged Host header would put an attacker's domain in a real user's DM.
  magic_links.purpose enforces the other direction - a check-in link is DM'd
  weekly and lives 7 days, so consume_magic_link matches purpose rather than
  assuming it, or the weakest token in the system becomes the strongest.
- Routes that mint or remove a CREDENTIAL use auth.require_self, never
  require_viewer. View-as resolves to the TARGET everywhere else by design, so
  under require_viewer an admin viewing as someone could register a passkey
  that belongs to them - the one thing an admin can leave behind that nothing
  takes away, since it outlives the impersonation, survives the target's next
  password reset (which keeps passkeys on purpose) and works after a demotion.
  The reset link travels in a URL but is exchanged for a path-scoped HttpOnly
  cookie on arrival (same move /checkin?t= makes), so it is not left in the
  address bar, history, or every proxy log line for the page.
- /forgot must stay constant-time with respect to whether the account exists:
  the delivery call goes through BackgroundTasks, and the request itself does
  one indexed SELECT on every path. Identical wording is not enough on its own
  when one branch makes an HTTP call to Slack and the other does not - the
  latency is the oracle. The admin Send-reset-link button is deliberately
  synchronous: it is behind require_admin, so it is nobody's oracle, and the
  admin needs to be told whether the message went out.
- app/passkeys.py derives the WebAuthn RP ID and origin from the live REQUEST,
  not from public_base_url: they must match what the browser sends, and a stale
  setting would silently invalidate every registered passkey. This depends on
  uvicorn's --proxy-headers (Dockerfile) for the https scheme behind Caddy.
  Passkeys are additive forever - a lost device is recovered with the password,
  so a reset keeps passkeys and no user is ever passkey-only.
- Routes stay SYNC (`def`, not `async def`). An async endpoint runs on the
  event loop while its dependency ran in the threadpool, and every query
  raises. JSON bodies come in through Body(), not `await request.json()`.
  db.connect passes check_same_thread=False, and that is load-bearing rather
  than lazy: a sync generator dependency does not get to choose its threads -
  Starlette runs the `yield` and the teardown as two separate to_thread calls -
  so under concurrency the per-request connection is CLOSED on a different
  worker than opened it. That surfaced as /display returning 500 for 79 of 80
  concurrent requests while a single request always worked, because one client
  (the TV, polling alone) gets the same thread back every time. It is safe ONLY
  because db_dep opens one connection per REQUEST and hands it to exactly one
  request, so the threads touch it strictly one after another; never share a
  connection between requests, and anything wanting one on its own schedule
  (sweeps, migrations) calls connect() for itself. tests/test_concurrency.py
  guards both halves.
- A 1-3-1 SUPERSEDES rather than overwrites (migrate/oto_revisions): the old
  row keeps its text and gains superseded_at, the way entry_audit keeps a
  number you corrected. The route used INSERT OR IGNORE against a UNIQUE, so a
  second filing vanished silently - the one place in this app where a write
  disappeared without saying so. Uniqueness is now a PARTIAL index (one live
  draft per metric-week) created by the migration, NOT by schema.sql:
  schema.sql is replayed on every startup BEFORE the migrations, so an index
  naming superseded_at fails against a not-yet-migrated table and takes the app
  down on boot instead of migrating it. Any future constraint that depends on a
  new column has the same trap.
- TV views (main.TV_VIEWS) are arrangements of the ONE TvVM build_tv returns,
  never a second query path. The "CEO metrics" view (app/ceo.py + grid.build_ceo
  + _view_ceo.html) finds its seven rows through a slot mapping in settings
  (ceo_slot_<slot> -> metric id) read from the DATA db through the same
  connection build_tv uses, exactly like hud_mrr_metric_id: it is a map of
  metric ids, which mean nothing across databases, so demo mode gets its own
  (or the name fallback). The template installer creates ordinary metrics -
  there is no second kind of row - and TvVM.rows carries every live row
  unfolded because `columns` drops the goal metric and folded greens, which
  is exactly what a by-id lookup would miss. Derived numbers (close rate,
  margin) are computed in build_ceo from BoardRow.latest_raw and only when
  both rows share week_note; a ratio across two weeks is a number nobody
  asked for. On the TV the number's font is sized from its glyph count via
  --len and container-query units (cqw): vh alone truncated every $ figure,
  because a 16:9 panel runs out of width long before height.
- /ceo is the CEO view as a PAGE (sidebar, everyone who can see the board),
  drawn from the same build_ceo as the TV view. Two rules: slots may point into
  a HIDDEN section - hidden means off the board, check-in and nudges, not
  off the CEO's desk - so grid.ceo_rows adds those rows (building the hidden
  grid only when a slot needs it) for both the page and the TV; and because
  such a metric has no board row, the page reuses the board's quick editor
  with origin=ceo (main.QUICK_ORIGINS), which answers a save with HX-Refresh
  instead of _render_row. A TV pin (?view=, /tv?view=ceo) only has to name a
  real view with content - Settings > TV views decides what an UNATTENDED
  screen rotates through, not what a person pointed one at.
- A long client list is absorbed in three steps, in this order, and the order
  is the design (grid._layout_board): a roster section (every row R/Y/G) first
  WIDENS into up to MAX_SUBCOLS sub-columns, then folds its greenest rows into
  a "+N" cell, and once anything folds the "Clients" view (_view_clients.html,
  a tile wall sized by grid._roster_cols) enrols ITSELF in a rotating TV via
  main._tv_view - the one view that does, because "a client nobody can see"
  is not a state an admin should have to notice and fix in Settings. It
  leaves again when everyone fits, and never joins when rotation is off
  (there the "+N" cell would promise a view that never comes). Adding a
  client is a one-field form at the TOP of the roster panel on Admin >
  Sections & metrics (the board's section head links to it for admins, not
  in demo mode - the link carries a real-db section id); POST /admin/metrics
  refuses a second live row with the same name in a section.
- Settings > Display embeds the real /display in an iframe, scaled. scale()
  takes a NUMBER, so the ratio is measured in JS and set as --tvprev-scale;
  calc(100cqw / 1920) is a length and silently does nothing. The frame is
  rendered at 1920 and scaled DOWN on purpose - reflowing the board to a 600px
  viewport would preview a layout nobody will ever see.

- Admin > Targets shows LAST quarter's target, actual and hit rate on the row
  you are typing into (grid.build_target_rows), and saves the whole page at
  once. The actual is the weekly MEAN, not the quarter total: a weekly target
  is compared with a weekly number, and summing would put 28 next to a target
  of 10. A target is a PAIR - baseline scores weeks 1-6 and stretch the rest -
  so the POST refuses one without the other rather than leaving half a quarter
  scored against nothing.
- Admin > Settings is three server-side groups (?tab=display|notify|advanced),
  and SETTINGS_TABS in main.py is the single map from panel id to group. It is
  what _settings_saved uses too: an anchor cannot land on a panel the active
  tab is not rendering, so adding a panel means adding a row there.

- /m/{id} (grid.build_metric) is the metric's own page: one quarter week by
  week, its 1-3-1s, and every write with its source. It scores through
  sc.cell_state on the same data the grid uses - a metric that read red there
  and green on the board would be worse than not having the page. Entry links
  point at whichever page can actually save that week (/checkin, ?week= for the
  current one, /checkin/catch-up for older), and only for the DRI, since
  /checkin only lists metrics you own.

- The BOARD is writable, and says so. Every row carries a pencil in the metric
  column (sticky, so it survives the horizontal scroll) which opens a <dialog>
  quick editor: one metric, one week, one number, defaulting to the DUE week
  and with - / + for the "someone rang, that is one more" case. Clicking a cell
  has always opened an inline editor, but nothing on the page said so and a
  50px cell is not a touch target, so the number people came to the board to
  change was the one thing the board did not offer. It is deliberately NOT a
  metric editor - renaming, re-owning and re-targeting are quarterly jobs with
  their own admin screens. Two rules: the - / + buttons move the FIELD and
  leave saving to Save, because every write lands in entry_audit and four taps
  must not be four rows of history; and a bad value re-renders the DIALOG with
  the message rather than returning 422, because htmx does not swap a 4xx, so
  an error status is a Save button that silently does nothing.
- A write from the board answers with EVERYTHING that number changed, and the
  shape of that response is load-bearing (main._render_row). The unit is the
  ROW, not the cell - state dot, month subtotal, Actual, sparkline and the
  1-3-1 badge are sibling cells - plus the lede and "Act on this" above the
  grid, which are the same rows added up. Returning only the cell left five
  other numbers reading yesterday's answer, and left "0 of 5 on target" over a
  green row. Everything is hx-swap-oob and both callers swap "none". The order
  is not cosmetic: htmx picks its fragment parser from the response's FIRST tag
  (makeFragment), so leading with <tr> wraps the body in <table><tbody> and the
  HTML parser silently drops the lede <div> - no error, the swap just never
  happens. The div goes first and the row travels inside its own <table>.
  htmx's OOB scan is a deep querySelectorAll, so that nesting is fine.
- Navigation groups by CADENCE, not by route: the rail holds Board, My numbers
  and a single Admin entry; the seven admin screens live in base.html's
  `admin_pages` list and render as a second-level .subnav on admin pages only.
  Add an admin screen by adding a row there - putting it in the rail puts a
  quarterly job next to a weekly one. /admin lands on /admin/status, not the
  metric editor: "does this instance work?" comes before "what shall I change?",
  and that page is also the first-run setup list.

- /checkin is WEEK-major, not metric-major: one row per metric for ONE selected
  week (the due week, or the current week via the tab), with backfill moved to
  /checkin/catch-up. That split is the design - auto-opening earlier weeks on
  the weekly page meant one two-month-old gap re-expanded the card every Monday
  forever. Two things there are load-bearing: the numeric field's htmx trigger
  is `change` and NOT `submit`, because Enter in a single-field form fires
  change first and implicit submission second, so listening to both writes the
  same number - and audits it - twice (onsubmit="return false" kills the
  navigation htmx is no longer intercepting); and the catch-up matrix posts the
  rendered value alongside each field (o:<id>:<week>) so an untouched field is
  skipped rather than re-saved, since a no-op write still lands in entry_audit.
  Field names there are data, never authorisation - the POST re-checks DRI
  ownership per metric.

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

- A metric can be OWNED BY AN AUTOMATION writing through the JSON API, not by a
  person typing. POST /entries is an upsert of an absolute value, so a daily
  writer silently replaces anything entered by hand for that week - a number
  someone corrects on Tuesday is gone by Wednesday morning, with no error and
  nothing in the UI saying why. Before "fixing" a metric that keeps reverting,
  check entry_audit for source='api' and find the writer. The upsert shape is
  deliberate: it makes a re-run idempotent and lets a missed run self-heal, but
  it means "last writer wins" and the automation usually writes last.

- The left rail is fixed at 196px and .page reserves 220px for it, so until
  the max-width:760px block existed a 414px phone got ~170px of content and the
  board scrolled sideways as a whole page. That is why the rail collapses to a
  52px icon strip there rather than being a styling nicety: "open the board and
  fix the number" has to be doable from a phone, which is where the pencil is
  for. Anything hover-only needs a (hover: none) branch for the same reason - a
  control that appears on hover is a control a phone never shows.
- migrate/seed_data.local.json is required to seed; copy from seed_data.example.json.
- Passwords/tokens are hashed in DB; temp passwords and API tokens print exactly once.
- Styling: CSS custom properties in app/static/scorecard.css only - no new hex
  values, no emoji in UI. Brand reference lives outside this repo. Watch SPECIFICITY when
  adding a state class: `.ck-row.is-missing .ck-num` (0,3,0) silently beat
  `.ck-num:focus` (0,2,0) and left the field being typed in with no focus
  colour, and `.sidenav .nav-item span` matched `.nav-badge` and gave the count
  flex:1, ellipsising the label beside it. State rules go before focus rules,
  and container rules exclude the components they should not reach. ORDER is
  the third trap and the quietest: at EQUAL specificity the later rule wins, so
  a media query placed beside the component it adjusts loses to any base rule
  declared further down the file. `@media (max-width:760px) .status-body
  {min-width:55%}` sat with the other layout rules and was silently beaten by
  `.status-body {min-width:0}` 200 lines later - the block looked right, read
  right, and did nothing. Responsive overrides go at the END of the stylesheet.
- Slack two-way replies need Scorecard's OWN Slack app - never share a bot user
  with a chat agent that also lives in the workspace. Slack delivers message.im
  to exactly one consumer per app, and Socket Mode (what agent gateways use)
  disables the Events API Request URL outright, so /slack/events is simply never
  called. Nothing errors: nudges still go out over the shared token, the "reply
  1: G" line still promises a shortcut that cannot work, and the replies land in
  the agent instead. Symptom to check first: no rows in entry_audit with
  source='slack', and no POST /slack/events in the container log.
- Brand: brand/scorecard-logo.png is the master; every favicon/app icon under
  app/static is derived from it (regenerate per README, don't hand-edit). The
  Slack app's own icon has no API and stays a one-time upload; what the app sets
  is icon_url per message, which needs chat:write.customize. alerts._post_message
  retries once without the icon on a scope error - never let the avatar become a
  way for a nudge to fail silently.
- Deployment specifics: deploy/DEPLOY.local.md (gitignored) - THIS office's
  hosts, IPs and the one existing TV kiosk. Site-specific facts go there, never
  in deploy/kiosk/.
- deploy/kiosk/ is the PUBLIC, generic build kit for shippable Pi TV units
  (WPE/cog on DRM pointed at /tv - no desktop, no login). Invariants:
  user-data.example is the single authoritative definition of the appliance -
  never document a unit tweak that is only applied by hand. Everything a
  customer configures lives on the FAT boot partition (wifi-credentials,
  scorecard-kiosk.conf), because it survives power loss and is editable without
  SSH; ext4 does not, which is the whole reason the kit exists. cloud-init
  re-applies nothing unless the instance-id changes in BOTH meta-data and
  cmdline.txt - that cache is the top field-support trap. Validate the YAML
  before imaging: a broken user-data means a unit that never gets on a network.
