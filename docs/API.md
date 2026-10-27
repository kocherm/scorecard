# Scorecard API reference

Version 1.3 &middot; Base path `/api/v1` &middot; JSON over HTTPS

The Scorecard API lets integrations read the scored company scorecard and write
weekly numbers into it: an n8n flow copying figures from your accounting tool,
a script posting yesterday's sales, or an AI agent reporting what it measured.
With an `admin` token it can also do the quarterly jobs: add people, add and
reassign metrics, and set targets.
Everything the API returns is scored by the same engine as the board and the TV,
so an integration never sees a different colour from the one on the wall.

- **Interactive reference:** `https://<your-scorecard>/api/docs` (try requests
  in the browser with your token)
- **OpenAPI 3.1 schema:** `https://<your-scorecard>/api/v1/openapi.json`, also
  committed as [`docs/openapi.json`](openapi.json) for Postman, n8n and client
  generators
- **Claude / MCP:** an MCP server lives at `/mcp`, read-only unless the
  token can write; see [MCP](#mcp-server) below

Examples use `https://scorecard.example.com` and a token in `$TOKEN`.

## Contents

- [Quick start](#quick-start)
- [Authentication](#authentication)
- [Concepts](#concepts)
- [Errors](#errors)
- [Endpoints](#endpoints)
  - [GET /scorecard](#get-scorecard)
  - [GET /metrics](#get-metrics)
  - [POST /metrics/{metric_id}/entries](#post-metricsmetric_identries)
  - [GET /ceo](#get-ceo)
  - [POST /ceo/{slot}/breakdown](#post-ceoslotbreakdown)
  - [POST /metrics/{metric_id}/archive](#post-metricsmetric_idarchive)
  - [POST /metrics/{metric_id}/unarchive](#post-metricsmetric_idunarchive)
  - [GET /users](#get-users)
  - [POST /users](#post-users)
  - [PATCH /users/{user_id}](#patch-usersuser_id)
  - [GET /sections](#get-sections)
  - [POST /sections](#post-sections)
  - [POST /metrics](#post-metrics)
  - [PATCH /metrics/{metric_id}](#patch-metricsmetric_id)
  - [PUT /metrics/{metric_id}/targets](#put-metricsmetric_idtargets)
- [Recipes](#recipes)
- [MCP server](#mcp-server)
- [Changelog](#changelog)

## Quick start

1. In the Scorecard, go to **Admin > API tokens**, create a token with the
   `write` scope, and copy it. It is shown once.
2. Find the id of the metric you want to write:

   ```bash
   curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/metrics
   ```

3. Write last week's number:

   ```bash
   curl -X POST https://scorecard.example.com/api/v1/metrics/2/entries \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"value": 12}'
   ```

   ```json
   {"ok": true, "metric_id": 2, "week_start": "2026-09-07", "week_label": "Q3-W10"}
   ```

The number is on the board within seconds, attributed to your token.

## Authentication

Every request carries a bearer token:

```http
Authorization: Bearer sc_3pQ...
```

Tokens start with `sc_`. Admins create, rotate and revoke them on
**Admin > API tokens**. Only a hash is stored, so a lost token cannot be shown
again; rotate it instead.

| Scope        | Read (`GET`) | Write values | Admin endpoints |
|--------------|:------------:|:------------:|:---------------:|
| `read`       | yes          | no           | no              |
| `write`      | yes          | yes          | no              |
| `read_write` | yes          | yes          | no              |
| `admin`      | yes          | yes          | yes             |

The admin endpoints are everything tagged *Admin*: people, sections, metric
definitions and owners, targets, and archiving. `GET /users` and
`GET /sections` are admin-only too, since they list email addresses and hidden
sections.

**Admin accounts are managed in the app only.** The API can create and change
editors and viewers, but it cannot create an admin, promote anyone to admin,
or change an admin's account in any way. Otherwise a leaked token could turn
itself into a signed-in admin, for example by pointing an admin's Slack id at
another person and requesting a password reset.

Give each integration its own token with the lowest scope it needs. The audit
trail and the "automated" label on Sections & metrics name the token that wrote
each number, which is only useful if tokens are not shared.

**Rotation.** Rotating issues a new secret and keeps the old one working for a
grace period (7 days by default, up to 90), so you can update the integration
without downtime. The tokens page shows when the old secret is still in use.
**Revoking** stops a token immediately.

## Concepts

### Weeks

The scorecard is weekly. A week is identified by its **Monday**, written
`YYYY-MM-DD`, in the business timezone (America/Chicago by default).

- **Current week:** the week containing today.
- **Last closed week:** the week before it. This is the week that is *due*, and
  the default for every write when you leave `week_start` out.
- A week's numbers are **due** by 23:59 on the following Monday and turn
  **stale** at 08:00 that Wednesday if still missing.

Writes are refused for a future week, for a date that is not a Monday, and for
a week before the metric started.

### Metric types

| Type      | You send                 | Example                     |
|-----------|--------------------------|-----------------------------|
| `numeric` | `"value": number`        | `{"value": 18250.5}`        |
| `binary`  | `"value": 0` or `1`      | `{"value": 1}` (any non-zero is yes) |
| `status`  | `"status": "R"`, `"Y"` or `"G"` | `{"status": "Y"}`    |

Units (`$`, `%`) are display only. Send plain numbers: `18250.5`, not `"$18,250.50"`.

### States

Values are scored against the target for that week:

| State       | Meaning |
|-------------|---------|
| `green`     | On or better than target |
| `yellow`    | 70-99% of target (for lower-is-better metrics, up to about 1.4x the budget) |
| `red`       | Below 70% of target |
| `pending`   | No number yet, deadline not passed |
| `stale`     | No number, deadline passed |
| `no-target` | A number, but no target is set for that quarter |
| `na`        | Before the metric started, or after it was archived |

The **current week** is still in progress, so higher-is-better counts that add
up over the week (calls booked, videos published) are scored on pace: each
finished day owes a sixth of the target, so 4 of a weekly target of 8 is green
on a Thursday. Point-in-time numbers (MRR, a percentage) and closed weeks are
always scored against the full target.

### Writes replace, and are safe to repeat

A write sets the value for that metric and week, replacing whatever was there,
including a number someone typed by hand. Sending the same request twice leaves
the same result, so a scheduled job can re-send a whole week without checking
first. Every write is recorded in the audit trail with the token that made it.

Because the latest write wins, a metric written by an automation should be
corrected at the source (or in the automation), not by hand in the Scorecard;
the next run would replace the manual correction.

### Hidden sections

Admins can hide a section from the board. `GET /metrics`, `GET /ceo` and every
write include metrics in hidden sections; `GET /scorecard` returns only what the
board shows.

## Errors

Errors use standard HTTP status codes and a JSON body with a `detail` field.

| Status | When |
|--------|------|
| `401`  | No token, or the token is invalid, expired or revoked |
| `403`  | The token's scope is too low (`"Token is read-only"`, `"Token lacks admin scope"`) |
| `404`  | Unknown metric, or a tile that cannot be broken down |
| `409`  | The request conflicts with what exists: a duplicate email, section or metric name; an admin account; a breakdown with no categories yet |
| `422`  | The request is well-formed but not acceptable (bad week, missing value, unknown category) |

`detail` is usually a sentence:

```json
{"detail": "Cannot write a future week"}
```

Two exceptions:

- **Unknown breakdown categories** return an object listing them and the valid names.
  See [POST /ceo/{slot}/breakdown](#post-ceoslotbreakdown).
- **A body of the wrong shape** (a string where a number belongs, invalid JSON)
  is rejected before the token is checked, with FastAPI's list form:

  ```json
  {"detail": [{"type": "float_parsing", "loc": ["body", "value"],
               "msg": "Input should be a valid number, unable to parse string as a number",
               "input": "lots"}]}
  ```

There are no rate limits.

## Endpoints

### GET /scorecard

Returns the whole board, scored: every visible metric with its target, last
closed and current week, trend and red streak, plus ready-made lists of what is
pending, stale and red. This is the endpoint for "how is the company doing".

**Scope:** any

```bash
curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/scorecard
```

**Response `200`** (trimmed to one metric):

```json
{
  "current_week": "2026-09-14",
  "current_week_label": "Q3-W11",
  "last_closed_week": "2026-09-07",
  "entries_due_by": "2026-09-14T23:59:59-05:00",
  "stale_after": "2026-09-16T08:00:00-05:00",
  "sections": [
    {
      "name": "Sales Activity",
      "metrics": [
        {
          "id": 1,
          "name": "New qualified conversations",
          "type": "numeric",
          "unit": null,
          "dri": "Jordan Lee",
          "target": "48",
          "last_closed_week": {"state": "green", "value": 61.0, "target": 48.0},
          "current_week": {"state": "green", "value": 51.0, "target": 48.0},
          "red_streak": 0,
          "escalation_level": 0,
          "one_three_one_filed": false,
          "trend": ["green", "green", "green", "green"]
        }
      ]
    }
  ],
  "pending": [],
  "stale": [],
  "red": [
    {"id": 5, "name": "Proposals sent", "dri": "Alex Rivera",
     "dri_slack_member_id": null, "weeks_red": 2, "one_three_one_filed": false}
  ]
}
```

| Field | Description |
|-------|-------------|
| `entries_due_by`, `stale_after` | Deadlines for the last closed week, with timezone offset |
| `metrics[].dri` | The owner's name; `"-"` when nobody owns it |
| `metrics[].target` | The last closed week's target, formatted for display (the target for `last_closed_week.value`); `"-"` when none, `"G"` for status metrics |
| `metrics[].*_week.value` | The number, or `"R"`/`"Y"`/`"G"` for status metrics; `null` when empty |
| `metrics[].*_week.target` | The target that week was scored against: its own quarter's, baseline in weeks 1-6 and stretch from 7, before any in-week pacing; `null` when none |
| `metrics[].escalation_level` | `0` none, `1` file a 1-3-1, `2` a 15-minute 1:1, `3` a structural conversation |
| `metrics[].trend` | States of the last four closed weeks, oldest first |
| `pending` / `stale` | Metrics missing last closed week's number, before / after the deadline, with the owner's Slack member id when known |
| `red` | Metrics in a red streak, with its length and whether a 1-3-1 is filed |

**Errors:** `401`

### GET /metrics

Lists metrics with their ids, for writers. Includes metrics in hidden sections.

**Scope:** any

| Query parameter | Type | Default | Description |
|-----------------|------|---------|-------------|
| `include_archived` | boolean | `false` | Also list archived metrics |

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://scorecard.example.com/api/v1/metrics?include_archived=true"
```

**Response `200`:**

```json
[
  {"id": 1, "name": "New qualified conversations", "metric_type": "numeric",
   "unit": null, "archived_at": null, "section_id": 1, "section": "Sales Activity",
   "dri_user_id": 4, "dri": "Jordan Lee"},
  {"id": 2, "name": "Follow-ups sent to pipeline", "metric_type": "numeric",
   "unit": null, "archived_at": null, "section_id": 1, "section": "Sales Activity",
   "dri_user_id": 4, "dri": "Jordan Lee"}
]
```

`archived_at` is the Monday a metric was archived from, or `null`. `dri` and
`dri_user_id` are `null` when unowned.

**Errors:** `401`

### POST /metrics/{metric_id}/entries

Writes one metric's value for one week, replacing any existing value.

**Scope:** `write`, `read_write` or `admin`

| Path parameter | Description |
|----------------|-------------|
| `metric_id` | From `GET /metrics` |

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `week_start` | string (Monday) | no | Defaults to the last closed week |
| `value` | number | numeric and binary metrics | The number. For binary, any non-zero is yes |
| `status` | `"R"`, `"Y"`, `"G"` | status metrics | The colour |

```bash
curl -X POST https://scorecard.example.com/api/v1/metrics/2/entries \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"week_start": "2026-09-07", "value": 12}'
```

**Response `200`:**

```json
{"ok": true, "metric_id": 2, "week_start": "2026-09-07", "week_label": "Q3-W10"}
```

**Errors**

| Status | `detail` |
|--------|----------|
| `403` | `Token is read-only` |
| `404` | `Unknown or archived metric` |
| `422` | `Cannot write a future week` |
| `422` | `week key '2026-09-08' is not a Monday` |
| `422` | `Metric starts 2026-07-06` |
| `422` | `value is required for numeric/binary metrics` |
| `422` | `status must be "R", "Y" or "G"` |

### GET /ceo

Lists the seven CEO tiles (Revenue, Expenses, Leads, Conversions, CAC, Retention,
Profit), the metric filling each, and the categories of tiles that are broken
down. Use it to discover the category names a breakdown call accepts.

**Scope:** any

```bash
curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/ceo
```

**Response `200`** (trimmed):

```json
{
  "slots": [
    {"slot": "revenue", "label": "Revenue", "metric_id": 6,
     "metric_name": "Current MRR", "unit": "$", "breakdown": []},
    {"slot": "expenses", "label": "Expenses", "metric_id": 22,
     "metric_name": "Expenses", "unit": "$",
     "breakdown": [
       {"metric_id": 32, "name": "Payroll", "unit": "$"},
       {"metric_id": 33, "name": "Contractors", "unit": "$"},
       {"metric_id": 34, "name": "Other", "unit": "$"}
     ]},
    {"slot": "cac", "label": "CAC", "metric_id": 25,
     "metric_name": "Customer acquisition cost", "unit": "$"}
  ]
}
```

- `metric_id` is `null` when no metric fills the tile.
- `breakdown` appears only on `revenue`, `expenses` and `leads`, the tiles whose
  parts add up to the whole. It is empty until an admin adds categories under
  **Admin > Settings > CEO view > categories**.
- To write a tile that has no breakdown (CAC, Retention, Profit...), use
  [POST /metrics/{metric_id}/entries](#post-metricsmetric_identries) with its `metric_id`.

**Errors:** `401`

### POST /ceo/{slot}/breakdown

Writes one week of a tile's categories in a single call, and optionally the
tile's total. This is the endpoint for accounting exports: send the week's
expense categories and the Expenses tile is set to their sum.

**Scope:** `write`, `read_write` or `admin`

| Path parameter | Description |
|----------------|-------------|
| `slot` | `expenses`, `revenue` or `leads` |

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `week_start` | string (Monday) | no | Defaults to the last closed week |
| `categories` | object | yes | Category name (case-insensitive) or metric id, mapped to an amount. Send `0` for none |
| `total` | `"sum"`, number or `null` | no (default `"sum"`) | What to write to the tile itself; see below |

**`total`:**

- `"sum"` writes the tile as the categories added up, **only once every
  category has a number for that week**. Until then the tile is left alone and
  `missing` lists what is outstanding. A partial sum is never written.
- A number writes that total as given, for when your books report a total that
  includes things you do not break down.
- `null` leaves the tile's total untouched.

**Behaviour**

- **All-or-nothing validation.** Every category is checked before anything is
  written. One unknown name fails the whole request and writes nothing.
- **Names are never created.** An unknown name is refused with the list of
  valid ones, so a typo in a mapping cannot add a category. Admins add
  categories in the Scorecard.
- **Partial updates are fine.** Categories you leave out keep their current
  value, so separate jobs can each send their own part of the same week, and
  the total is written once the last part arrives.

```bash
curl -X POST https://scorecard.example.com/api/v1/ceo/expenses/breakdown \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"week_start": "2026-09-07",
       "categories": {"Payroll": 18000, "Contractors": 6500, "Other": 1200}}'
```

**Response `200`, every category sent:**

```json
{
  "ok": true,
  "slot": "expenses",
  "week_start": "2026-09-07",
  "written": [
    {"metric_id": 32, "name": "Payroll", "value": 18000.0},
    {"metric_id": 33, "name": "Contractors", "value": 6500.0},
    {"metric_id": 34, "name": "Other", "value": 1200.0}
  ],
  "missing": [],
  "total_metric_id": 22,
  "total_written": 25700.0
}
```

**Response `200`, only part of the week so far:**

```json
{
  "ok": true,
  "slot": "expenses",
  "week_start": "2026-08-31",
  "written": [{"metric_id": 32, "name": "Payroll", "value": 18000.0}],
  "missing": ["Contractors", "Other"],
  "total_metric_id": 22,
  "total_written": null
}
```

**Response `422`, unknown category** (nothing written):

```json
{
  "detail": {
    "error": "unknown categories",
    "unknown": ["Software"],
    "categories": ["Payroll", "Contractors", "Other"]
  }
}
```

**Errors**

| Status | `detail` |
|--------|----------|
| `403` | `Token is read-only` |
| `404` | `No breakdown for 'cac'; one of expenses, revenue, leads` |
| `409` | `expenses has no categories yet - add them in Admin > Settings > CEO view` |
| `422` | Unknown categories (object above) |
| `422` | `categories is empty` |
| `422` | `Payroll: value is required (send 0 for none)` |
| `422` | `Payroll starts 2026-09-14; nothing was written` |
| `422` | `Cannot write a future week` / not a Monday |
| `422` | `total` that is not `"sum"`, a number or `null` (list form) |

### POST /metrics/{metric_id}/archive

Takes a metric off every surface (board, TV, check-in, alerts, API lists) while
keeping its history. This is how a churned client is removed. Calling it again
moves the effective week, which is how a wrong date is corrected.

**Scope:** `admin`

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `effective_week` | string (Monday) | no | The week tracking stopped. Defaults to this week. Cannot be in the future or before the metric started |

The body is optional.

```bash
curl -X POST https://scorecard.example.com/api/v1/metrics/12/archive \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"effective_week": "2026-08-31"}'
```

**Response `200`:**

```json
{
  "ok": true,
  "metric_id": 12,
  "name": "Delta Wellness",
  "archived": true,
  "was_already_archived": false,
  "effective_week": "2026-08-31",
  "effective_week_label": "Q3-W9"
}
```

**Errors:** `401`; `403` `Token lacks admin scope`; `404` `Unknown metric`;
`422` `Cannot archive effective a future week`, `Metric starts ...`, not a Monday

### POST /metrics/{metric_id}/unarchive

Restores an archived metric. Safe to call on a metric that is not archived.

**Scope:** `admin`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://scorecard.example.com/api/v1/metrics/12/unarchive
```

**Response `200`:**

```json
{"ok": true, "metric_id": 12, "name": "Delta Wellness", "archived": false, "was_archived": true}
```

**Errors:** `401`; `403` `Token lacks admin scope`; `404` `Unknown metric`

### GET /users

Lists everyone with a login, active or not, with the ids `dri_user_id` takes
and how many live metrics each person owns.

**Scope:** `admin`

```bash
curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/users
```

**Response `200`:**

```json
[
  {"id": 4, "display_name": "Jordan Lee", "email": "jordan@example.com",
   "role": "editor", "is_active": true, "slack_member_id": "U012ABCDEF",
   "metrics_owned": 3}
]
```

**Errors:** `401`; `403` `Token lacks admin scope`

### POST /users

Adds a person and returns a temporary password, once. They must change it the
first time they sign in. Pass it on privately. If the response is lost, reset
the password on **Admin > Users**.

**Scope:** `admin`

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `display_name` | string | yes | The name on the board |
| `email` | string | yes | Their sign-in; must be unique |
| `role` | `"editor"` or `"viewer"` | no | Defaults to `editor`. Admins are made in the app |
| `slack_member_id` | string | no | Where nudges and reset links are sent, e.g. `U012ABCDEF` |

```bash
curl -X POST https://scorecard.example.com/api/v1/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"display_name": "Sam Rivera", "email": "sam@example.com"}'
```

**Response `200`:** the person as in `GET /users`, plus `temp_password`.

```json
{"id": 7, "display_name": "Sam Rivera", "email": "sam@example.com", "role": "editor",
 "is_active": true, "slack_member_id": null, "metrics_owned": 0,
 "temp_password": "EXAMPLE-ONLY"}
```

Automations log their responses, so an n8n execution log or a CI transcript
will contain this password until the person changes it. Tell them to sign in
promptly.

**Errors:** `401`; `403`; `409` `... already exists`; `422` not an email address,
or `role` is `admin`

### PATCH /users/{user_id}

Changes only the fields you send.

**Scope:** `admin`

| Body field | Type | Description |
|------------|------|-------------|
| `display_name` | string | |
| `email` | string | Must stay unique |
| `role` | `"editor"` or `"viewer"` | |
| `is_active` | boolean | `false` signs them out everywhere and stops their nudges. The metrics they own keep them as owner until you reassign them |
| `slack_member_id` | string or `null` | `null` clears it |

```bash
curl -X PATCH https://scorecard.example.com/api/v1/users/7 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"slack_member_id": "U034GHIJKL"}'
```

**Response `200`:** the person as in `GET /users`.

**Errors:** `401`; `403`; `404` `Unknown user`;
`409` `Admin accounts are changed in the app, not through the API`, or a
duplicate email; `422` a required field sent as `null`, or `role` is `admin`

### GET /sections

Lists every section, hidden ones included, with the ids `section_id` takes.

**Scope:** `admin`

**Response `200`:**

```json
[{"id": 1, "name": "Client Health", "icon": "heart", "is_enabled": true, "metrics": 6}]
```

`is_enabled` is `false` for a section hidden from the board. `metrics` counts
live metrics.

**Errors:** `401`; `403`

### POST /sections

Adds a section at the bottom of the board. A second section with the same name
(ignoring case) is refused, so a setup script can be re-run safely.

**Scope:** `admin`

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `name` | string | yes | |
| `icon` | string | no | Icon name, as on Admin > Sections & metrics. Defaults to `chart` |

**Response `200`:** the section as in `GET /sections`.

**Errors:** `401`; `403`; `409` `... already exists`; `422` `name is empty`

### POST /metrics

Adds a metric at the bottom of its section. In a client roster section, this is
how you add a client. A second live metric with the same name in the same
section is refused. Set its target afterwards with
[PUT /metrics/{metric_id}/targets](#put-metricsmetric_idtargets).

**Scope:** `admin`

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `section_id` | integer | yes | From `GET /sections` |
| `name` | string | yes | |
| `metric_type` | `"numeric"`, `"binary"`, `"status"` | yes | See [Metric types](#metric-types). Cannot be changed later |
| `rollup` | `"sum"` or `"average"` | no | Numeric only: how weeks add up into a month. Defaults to `sum` |
| `direction` | `"up"` or `"down"` | no | `down` for lower-is-better (costs, open bugs). Defaults to `up` |
| `unit` | string | no | Display only, e.g. `$` or `%` |
| `dri_user_id` | integer | no | The owner, from `GET /users`. Must be an active person |
| `start_week` | string (Monday) | no | Defaults to this week. Cannot be in the future |

```bash
curl -X POST https://scorecard.example.com/api/v1/metrics \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"section_id": 1, "name": "Delta Wellness", "metric_type": "status", "dri_user_id": 7}'
```

**Response `200`:**

```json
{"id": 41, "name": "Delta Wellness", "section_id": 1, "section": "Client Health",
 "metric_type": "status", "rollup": null, "direction": "up", "unit": null,
 "dri_user_id": 7, "dri": "Sam Rivera", "start_week": "2026-09-28",
 "is_key": false, "archived_at": null}
```

**Errors:** `401`; `403`; `409` `... already exists in this section`;
`422` unknown section, `No active user ...`, a bad `start_week`

### PATCH /metrics/{metric_id}

Changes only the fields you send. Use it to hand a client to someone else,
rename a metric, or move it to another section. It also works on archived
metrics.

**Scope:** `admin`

| Body field | Type | Description |
|------------|------|-------------|
| `dri_user_id` | integer or `null` | The new owner; `null` leaves it unowned. Must be an active person |
| `name` | string | Must stay unique among live metrics in its section |
| `section_id` | integer | From `GET /sections` |
| `unit` | string or `null` | `null` clears it |
| `direction` | `"up"` or `"down"` | |
| `rollup` | `"sum"` or `"average"` | Numeric metrics only |
| `is_key` | boolean | |
| `start_week` | string (Monday) | Cannot be in the future |

`metric_type` cannot be changed, because the numbers already written would stop
meaning anything. Archive the metric and add a new one instead.

```bash
curl -X PATCH https://scorecard.example.com/api/v1/metrics/41 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dri_user_id": 4}'
```

**Response `200`:** the metric as in `POST /metrics`.

A new owner takes over from the next nudge: weekly check-ins, stale reminders
and the red ladder all follow the metric's current owner.

**Errors:** `401`; `403`; `404` `Unknown metric`; `409` `... already exists in this
section`; `422` unknown section, `No active user ...`, `rollup applies to numeric
metrics only`, a required field sent as `null`

### PUT /metrics/{metric_id}/targets

Sets one quarter's target for a numeric metric. A target is a pair: the
baseline scores weeks 1-6 of the quarter and the stretch scores weeks 7-13.
Both are required, because half a target would score half the quarter
against nothing. Safe to repeat.

**Scope:** `admin`

| Body field | Type | Required | Description |
|------------|------|----------|-------------|
| `year` | integer | yes | |
| `quarter` | 1-4 | yes | |
| `baseline` | number | yes | Weekly target for weeks 1-6 |
| `stretch` | number | yes | Weekly target for weeks 7-13 |

```bash
curl -X PUT https://scorecard.example.com/api/v1/metrics/2/targets \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"year": 2026, "quarter": 4, "baseline": 8, "stretch": 10}'
```

**Response `200`:**

```json
{"ok": true, "metric_id": 2, "year": 2026, "quarter": 4,
 "baseline": 8, "stretch": 10, "changed": true}
```

`changed` is `false` when the target was already exactly that.

**Errors:** `401`; `403`; `404` `Unknown or archived metric`;
`422` `Only numeric metrics have targets`, a missing `baseline` or `stretch`

## Recipes

### n8n: accounting export to the Expenses breakdown

1. **Schedule Trigger**: weekly, Monday morning.
2. Your accounting node (QuickBooks, Xero...) fetches last week's expenses by
   account.
3. A **Code** or **Set** node maps accounts onto the Scorecard's category names
   (check them with `GET /api/v1/ceo`) and builds:

   ```json
   {"week_start": "2026-09-07",
    "categories": {"Payroll": 18000, "Contractors": 6500, "Other": 1200}}
   ```

   Compute `week_start` as the Monday of the week you are reporting. Leave it
   out to report the week that just closed.
4. **HTTP Request** node:
   - Method `POST`, URL `https://scorecard.example.com/api/v1/ceo/expenses/breakdown`
   - Authentication: *Generic > Header Auth*, name `Authorization`, value `Bearer sc_...`
   - Body: JSON, from the previous node
   - Settings: *On Error > Stop Workflow*, so a `422` for a renamed account
     surfaces in n8n instead of passing silently.

Re-running the workflow for the same week is safe; it replaces the numbers.

### A single number from any script

```bash
curl -fsS -X POST "https://scorecard.example.com/api/v1/metrics/$METRIC_ID/entries" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"value\": $VALUE}"
```

`-f` makes curl exit non-zero on a `4xx`, so cron and CI notice failures.

### Hand a client to a new owner

```bash
# 1. Find the person's id, adding them first if they are new
curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/users
# 2. Find the client's metric id
curl -H "Authorization: Bearer $TOKEN" https://scorecard.example.com/api/v1/metrics
# 3. Reassign it
curl -X PATCH https://scorecard.example.com/api/v1/metrics/41 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dri_user_id": 7}'
```

### An AI agent

Give an agent that only answers questions a `read` token for
[MCP](#mcp-server). Give one that also reports numbers its own `write` token,
sent as a Bearer header to `/mcp`: it then gets `report_number` alongside the
read tools. The CEO breakdown and the admin endpoints are REST-only. Tell it to
call `list_metrics` (or `GET /api/v1/metrics` / `GET /api/v1/ceo`) first and to
use the ids and names it finds there, never ones it remembers.

## MCP server

`POST /mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server (Streamable HTTP, JSON-RPC 2.0) for Claude and other MCP clients. The
token's scope decides the tools:

| Token | Tools |
|---|---|
| `read` | `get_scorecard`, `get_check_in_status`, `list_metrics` - the same builders as `GET /scorecard` and `GET /metrics` |
| `write`, `read_write`, `admin` | the above, plus `report_number` |

`report_number` takes `metric_id`, and `value` or `status`, and optionally
`week_start`. It is [POST /metrics/{metric_id}/entries](#post-metricsmetric_identries):
the same validation, the same replace-what-is-there upsert, audited as `api` to
the token. A refusal comes back as a tool result with `isError: true` and
writes nothing. Admin actions are not offered over MCP, whatever the token's
scope.

- Authenticate with `Authorization: Bearer sc_...`, or, for clients that only
  accept a URL, `POST /mcp/t/{token}`. The URL form puts the token in proxy
  logs, so it accepts a `read` token only; rotate it if the URL leaks. A
  shared connector (Claude in Slack) should use this form: a write through it
  would be attributed to the connector's token instead of the person
  reporting the number.
- Admins get the connector URL for a token when it is created on
  **Admin > API tokens**.

## Changelog

**1.3.0** (October 2026)

- `GET /scorecard`: `metrics[].target` is now the target of the last closed
  week - the week whose value, colour and red streak the response reports.
  It used to be the current week's, which at a quarter start is next
  quarter's target and from quarter-week 7 the stretch target, beside a value
  that was judged on something else.
- `last_closed_week` and `current_week` each gain `target`: the number that
  week was scored against (unpaced), or `null`.

**1.2.0** (September 2026)

- Added the admin endpoints for people and structure: `GET`/`POST /users`,
  `PATCH /users/{user_id}`, `GET`/`POST /sections`, `POST /metrics`,
  `PATCH /metrics/{metric_id}` and `PUT /metrics/{metric_id}/targets`. Admin
  accounts stay app-only.
- `GET /metrics` now also returns `section_id` and `dri_user_id`.

**1.1.0** (September 2026)

- Added `GET /ceo` and `POST /ceo/{slot}/breakdown`.
- Published the OpenAPI schema at `/api/v1/openapi.json` and the interactive
  reference at `/api/docs`. The application-wide `/openapi.json` was removed;
  it described internal routes, not the API.

**1.0.0**

- `GET /scorecard`, `GET /metrics`, `POST /metrics/{id}/entries`,
  `POST /metrics/{id}/archive`, `POST /metrics/{id}/unarchive`.
