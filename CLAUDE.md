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
- Routes stay SYNC (`def`, not `async def`). db_dep yields a plain sqlite3
  connection, which may only be used on the thread that opened it; an async
  endpoint runs on the event loop while its dependency ran in the threadpool,
  and every query raises. JSON bodies come in through Body(), not
  `await request.json()`.
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
