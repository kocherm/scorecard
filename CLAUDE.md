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
- How a sweep SENDS is as load-bearing as what it sends:
  - Claim (alerts_sent) and COMMIT first, make the network calls with no
    write transaction open, then record the results in a second short
    transaction. Slack calls have 10s timeouts and every logged-in request
    writes sessions.last_seen_at, so a send inside the write lock is a
    "database is locked" 500 for whoever is using the board.
  - A send that fails RELEASES its claim, so the next run retries it, and
    only delivered messages are counted in sweep_runs. The exception is the
    stale claim: it means "flagged", and releasing it would repost the
    channel roll-up.
  - Only a closed week that is itself red escalates (row.last_state).
    consecutive_red_weeks skips PENDING weeks on purpose, so on a Tuesday
    when last week has no number yet the streak still reads 2, and claiming
    on it re-posted an escalation the channel already had.
  - Red DMs are one per person, private channels only (deliver_secret). A
    check-in nudge carries its magic link only where deliver_secret allows;
    a shared channel gets the plain /checkin URL.
  - stale_sweep skips hidden sections, like check-in and nudges do.
    Otherwise it named people in the channel for numbers no DM ever asked
    them for.
- The scheduler has a 4h misfire grace and coalesce, and every job runs
  through alerts.guarded. It runs one sweep at a time and records a crash as
  outcome 'error' on its OWN connection, because the sweep's transaction
  rolls back with the crash. It stores the exception type only: a message
  can hold a webhook URL. 30s after boot, alerts.catch_up_sweeps runs any
  sweep whose slot this week passed with no run, in the order summary, red,
  stale. A fresh job's next run is computed from when it is added, so a
  container that was down over Tuesday 08:00 would otherwise skip the week.
  alerts.SCHEDULE must match the CronTriggers.
- app/mcp.py is a remote MCP server (JSON-RPC over Streamable HTTP at /mcp) so
  Claude can READ the board conversationally. It is read-only on purpose: an
  API write is attributed to a token, and the connector authenticates as one
  shared credential, so a write would show up in Activity as "the Claude token"
  instead of the person. Its tools call app/api.py's build_scorecard /
  metrics_rows - never a second query path.
- The API is documented in three places that must agree, and a test holds
  them together (tests/test_api_docs.py): docs/API.md (the written reference,
  one "### METHOD /path" section per endpoint), docs/openapi.json (generated:
  `uv run python -m app.api_docs > docs/openapi.json`), and the live
  /api/v1/openapi.json + /api/docs from app/api_docs.py. The schema is built
  from the /api/v1 router ONLY and the app-wide /openapi.json is off
  (openapi_url=None): the default described every admin form and webhook.
  Response models in app/api.py are passed as `responses=` documentation,
  NEVER response_model - a model silently drops undeclared keys, and those
  builders are shared with the MCP server. HTTPBearer there is auto_error=False
  and exists only to put the scheme in the schema; auth stays in
  api_token_from_request. Bump api_docs.VERSION and the API.md changelog when
  the contract changes.
- The admin-scope API (users, sections, metric create/PATCH, targets) writes
  through app/admin_ops.py, the same functions the admin screens call - never
  a second copy of "one live name per section" or "a target is a pair". It can
  NOT touch admin accounts: roles are editor/viewer only and an admin's row is
  read-only (409). That is load-bearing, not caution: a token that can mint an
  admin reads back its password, and one that can edit an admin's
  slack_member_id gets that admin's /forgot link DM'd wherever it likes -
  either way the token becomes a browser admin with Settings and API tokens.
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
  Redeeming a check-in link gives a LIMITED session (sessions.scope =
  'checkin'): 12 hours, never extended by activity, never in view-as. A
  nudge is the most-forwarded credential the app sends, and a full session
  from one made whoever opened an admin's link an admin for 30 days, with
  a passkey to keep. auth.RequireRole refuses it on require_admin and
  otherwise allows only auth.link_scope_allows paths. That is default-deny,
  so a new route is closed to it until someone adds it on purpose.
  require_self refuses it too. A refusal sends the person to
  /login?why=full, and a password or passkey login replaces the session
  with a full one.
- Other credential rules:
  - /tv hands out the display token, so it answers only private, loopback
    and link-local clients. That depends on --proxy-headers: behind the
    reverse proxy the internet arrives as public IPs. FORWARDED_ALLOW_IPS is
    "*" in the image, so set it to the proxy's address in a local compose
    override.
  - /mcp/t/<token> accepts read-scope tokens only.
  - Any credential carried in a URL must also be redacted from the access
    log by RedactCredentialsFilter in main.py.
  - Changing a password signs out the user's other sessions.
  - Redirect targets taken from input go through main._is_local_path.
  - CrossSiteGuard refuses cross-site unsafe methods, checking
    Sec-Fetch-Site first and then Origin. A new machine endpoint (webhook,
    bearer route) must be added to its exemptions, or it gets a 403.
  - Never put {{ }} inside an on*= attribute or a <script> block: the
    browser decodes the HTML escaping before the JS runs. Pass values
    through a data- attribute. A test enforces this.
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
- A CEO tile can be BROKEN DOWN (ceo.BREAKDOWN_SLOTS: expenses, revenue,
  leads - the ones that SUM; CAC by channel does not add up to CAC, so a
  share bar would lie). Categories are ordinary metrics created in the tile
  metric's section with its unit/owner/direction, linked by an ordered id
  list in settings (ceo_breakdown_<slot>), exactly as a slot is linked to its
  metric. The panel on /ceo reads every number for the ONE week the tile
  shows (grid.build_breakdowns); the TV view ignores breakdowns on purpose.
  POST /api/v1/ceo/{slot}/breakdown is the automation path and is
  all-or-nothing: every name is resolved before anything is written, unknown
  names are refused rather than created, and total="sum" writes the tile only
  when every category has a number that week - a short sum written
  confidently is worse than no total. "Use the sum" on the page applies the
  same rule and recomputes from entries, never from the form.
- Revenue per employee on /ceo (grid.build_rpe) is the one MONTHLY number
  in the app. Two rules carry it. A summed revenue metric is the average
  week x 52/12, never the month's sum: months are the Mondays in them, so a
  plain sum swings 25% between four- and five-Monday months and reads a
  missing week as a slump. And headcount is a dated history in settings
  (ceo_headcount = "YYYY-MM:n,...", ceo.headcount_for carries it forward),
  NOT a metric. A metric is a weekly number: in a visible section, check-in
  and the stale sweep would ask for it every week, for a number that changes
  twice a year. In a hidden one it would still need a value every week to
  have a history. Annualized is ?rpe=year, x12 at display only. Only CLOSED
  months count (weeks.last_closed_month: the month whose last Monday's week
  has ended), so the month in progress never shows up looking low; a closed
  month missing weeks is estimated from the weeks entered and flagged
  "N of M weeks entered", never silently read short. A point-in-time revenue
  metric (rollup 'average', e.g. MRR) is averaged, not scaled. The TV view
  does not show it.
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
  /checkin only lists metrics you own. The route is /m/{metric_id:int}:
  without the converter it swallowed /m/{id}.csv and answered 422.
- Every number is paired with its OWN week's target, read from that week's
  own quarter (Cell.target). The red streak has exactly one implementation,
  grid.closed_states_desc, which both the board and /m/{id} call. The page
  used to score the previous quarter's weeks against this quarter's target
  and disagreed with the board in the first weeks of every quarter. The TV,
  CEO tiles and API (`target`, API 1.3.0) show the displayed week's target,
  never the current week's. That mismatch paired a Q3 number with a Q4
  target and called a week-6 number short of the week-7 stretch. The goal
  band's pace is the one deliberate exception.
- Cell hints ("why is this red") and the CSV exports (app/hints.py,
  app/export.py) read Cell.target, Cell.pace and the state cell_state
  already computed. They never score again: an export that disagreed with
  the board would be worse than none. Red and yellow also carry a shape,
  a corner notch and an underline (the --notch/--bar block in
  scorecard.css), so state is not colour-only. A new view that colours by
  state adds its selector there.
- A metric's type is fixed once it has entries (admin_ops.type_change_refused,
  enforced by the form and by the API, which never takes a type). A numeric
  value read as a status crashed every surface at once. scoring.entry_fits
  makes any stray mismatched row read as no data rather than a 500.
  display_months is clamped to grid.DISPLAY_MONTHS (2-4): one month can
  leave the window with no weeks.

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
# after any dependency change (uv.lock is gitignored; this file is the pin):
uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
```

## Gotchas

- Tests that build fixtures relative to datetime.now() pass most of the year
  and fail in the first weeks of a quarter. "Eight weeks back" crosses into
  the previous quarter, which has no target row, and the quarter on the page
  holds only one or two weeks. In October 2026 (Q4 W2) this sent five tests
  red on unmodified code, and the fixtures have since been fixed. That
  red run was also how the /m/{id} streak bug surfaced, so a test like this
  can be catching a real bug. A fixture must create targets for every
  quarter its data touches, or pass a fixed `now`. To prove a test doesn't
  depend on the date, run it under time-machine at boundary dates (quarter
  weeks 1-2 and 14, Dec 31/Jan 1, Sunday 23:30 Chicago), one process per
  date. SQLite's datetime('now') can't be frozen, so the activity LATE flag
  and the token grace tests fail under a frozen clock no matter what.
- Boot ALTERs go through main._add_column, which ignores only "duplicate
  column name". A bare `except OperationalError: pass` also swallowed
  "database is locked". The schema.sql replay trap now applies to
  sessions.scope too (migrate/session_scope), and sweep_runs' 'error' outcome
  came in through a table rebuild (migrate/sweep_error).
- /healthz stays unauthenticated and sync, opens its own connection, and
  returns only {ok, db, scheduler}. The compose healthcheck uses it.
- Never copy scorecard.db by hand under WAL. deploy/backup.sh snapshots it
  through the backup API, runs integrity_check before rotating, and can copy
  off-host (BACKUP_REMOTE). To restore, move the db, -wal and -shm aside
  together (README).

- A metric can be OWNED BY AN AUTOMATION writing through the JSON API, not by a
  person typing. POST /entries is an upsert of an absolute value, so a daily
  writer silently replaces anything entered by hand for that week - a number
  someone corrects on Tuesday is gone by Wednesday morning, with no error and
  nothing in the UI saying why. Before "fixing" a metric that keeps reverting,
  check entry_audit for source='api' and find the writer. The upsert shape is
  deliberate: it makes a re-run idempotent and lets a missed run self-heal, but
  it means "last writer wins" and the automation usually writes last.

- The goal band (grid.py MrrHud) shows the SINGLE NEWEST week that has a value
  for the goal metric, in-progress week included - a point-in-time number is
  whole the day it is read. The edit grid and check-in page default to the LAST
  CLOSED week. So when an API writer (or anyone) puts a value in the current
  week and a person then "corrects" it by typing in the grid, the correction
  lands one column to the left and the band never moves: the TV keeps showing
  the number they just replaced, with no error and nothing stale-flagged.
  Symptom: "the TV didn't update" while entry_audit shows the manual write
  landing fine. Check which week_start each write hit before suspecting the
  poll or the kiosk. Fix is a value in the newest week, not a redeploy. If this
  keeps biting, the band should prefer the newest week a HUMAN touched, or
  the grid should default point-in-time metrics to the current week.

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
