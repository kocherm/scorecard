# Scorecard API reference

Version 1.1 &middot; Base path `/api/v1` &middot; JSON over HTTPS

The Scorecard API lets integrations read the scored company scorecard and write
weekly numbers into it: an n8n flow copying figures from your accounting tool,
a script posting yesterday's sales, or an AI agent reporting what it measured.
Everything the API returns is scored by the same engine as the board and the TV,
so an integration never sees a different colour from the one on the wall.

- **Interactive reference:** `https://<your-scorecard>/api/docs` (try requests
  in the browser with your token)
- **OpenAPI 3.1 schema:** `https://<your-scorecard>/api/v1/openapi.json`, also
  committed as [`docs/openapi.json`](openapi.json) for Postman, n8n and client
  generators
- **Claude / MCP:** a read-only MCP server lives at `/mcp`; see
  [MCP](#mcp-server) below

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

| Scope        | Read (`GET`) | Write values | Archive / unarchive |
|--------------|:------------:|:------------:|:-------------------:|
| `read`       | yes          | no           | no                  |
| `write`      | yes          | yes          | no                  |
| `read_write` | yes          | yes          | no                  |
| `admin`      | yes          | yes          | yes                 |

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
| `409`  | The request conflicts with configuration (a breakdown with no categories yet) |
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
          "last_closed_week": {"state": "green", "value": 61.0},
          "current_week": {"state": "green", "value": 51.0},
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
| `metrics[].target` | This week's target, formatted for display; `"-"` when none |
| `metrics[].*_week.value` | The number, or `"R"`/`"Y"`/`"G"` for status metrics; `null` when empty |
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
   "unit": null, "archived_at": null, "section": "Sales Activity", "dri": "Jordan Lee"},
  {"id": 2, "name": "Follow-ups sent to pipeline", "metric_type": "numeric",
   "unit": null, "archived_at": null, "section": "Sales Activity", "dri": "Jordan Lee"}
]
```

`archived_at` is the Monday a metric was archived from, or `null`. `dri` is
`null` when unowned.

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

### An AI agent

Give the agent a `read` token for [MCP](#mcp-server) to answer questions from the
board, and a separate `write` token for the REST endpoints if it reports numbers.
Tell it to call `GET /api/v1/metrics` or `GET /api/v1/ceo` first and to use the
ids and names it finds there, never ones it remembers.

## MCP server

`POST /mcp` is a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server (Streamable HTTP, JSON-RPC 2.0) for Claude and other MCP clients. It
answers from the same builders as `GET /scorecard` and `GET /metrics`, and it
cannot write: a write through a shared connector would be attributed to the
connector's token instead of the person reporting the number.

- Authenticate with `Authorization: Bearer sc_...`, or, for clients that only
  accept a URL, `POST /mcp/t/{token}`. The URL form puts the token in proxy
  logs, so use a `read` token there and rotate it if the URL leaks.
- Admins get the connector URL for a token when it is created on
  **Admin > API tokens**.

## Changelog

**1.1.0** (September 2026)

- Added `GET /ceo` and `POST /ceo/{slot}/breakdown`.
- Published the OpenAPI schema at `/api/v1/openapi.json` and the interactive
  reference at `/api/docs`. The application-wide `/openapi.json` was removed;
  it described internal routes, not the API.

**1.0.0**

- `GET /scorecard`, `GET /metrics`, `POST /metrics/{id}/entries`,
  `POST /metrics/{id}/archive`, `POST /metrics/{id}/unarchive`.
