# lakehouse-ui warehouse console — Design

**Status:** Approved for planning
**Date:** 2026-09-13

## Goal

Grow `lakehouse-ui` from a single SQL box into a small Snowflake/BigQuery-style
warehouse console: a catalog browser, multiple query worksheets, query
history, a real syntax-highlighted editor, and — the piece that makes all of
it coherent — real per-person identity backed by Polaris's own RBAC instead
of one fixed baked-in credential.

Scope is deliberately narrower than "Snowflake": this adds what's directly
backed by Polaris's real API (catalog browsing, per-principal authorization)
plus a handful of app-native conveniences (tabs, history, a better editor).
It does **not** add principal/role/grant management — that's Polaris
Console's job, already running.

## Why

Two things converge to justify identity as the foundation of this pass:

1. Everything else asked for (a catalog tree scoped to what you can see, "am
   I even allowed to do this") is naturally RBAC-shaped, and Polaris already
   has real RBAC (see the original lakehouse design + the `loader` /
   `lakehouse-ui` principals it already created).
2. The app has run on one fixed, baked-in credential since it was built —
   explicitly flagged in the original design doc as a gap ("no auth ... flagged
   as a gap to close before this goes anywhere less trusted"). This closes it.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Browser                                                          │
│  Login (client_id/secret) → session cookie                       │
│  ┌──────────────┬─────────────────────────────────────────────┐ │
│  │ Sidebar       │ Tab bar: [Worksheet 1] [Worksheet 2] [+]     │ │
│  │ (Catalog tree │ ┌───────────────────────────────────────┐   │ │
│  │  ⇄ History)   │ │ CodeMirror editor (SQL highlighting)   │   │ │
│  │               │ ├───────────────────────────────────────┤   │ │
│  │               │ │ Results table                          │   │ │
│  │               │ └───────────────────────────────────────┘   │ │
│  └──────────────┴─────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
              │ session cookie on every request
              ▼
┌────────────────────────────────────────────────────────────────┐
│ lakehouse-ui (FastAPI)                                           │
│  Session store: in-memory dict, session_id → {client_id,         │
│    client_secret, principal_name, created_at}                    │
│  Own service credential (env, not per-session): POLARIS_ROOT_*   │
│                                                                    │
│  POST /login     → exchange creds at Polaris's OAuth endpoint,     │
│                     decode sub from the token, create session,     │
│                     set cookie                                      │
│  POST /logout    → drop session                                    │
│  GET  /me        → principal from session + roles looked up fresh  │
│                     via the root service credential                 │
│  GET  /catalog/namespaces[?parent=...]  → polaris_client, session's │
│  GET  /catalog/tables/{namespace}          creds                    │
│  GET  /catalog/tables/{namespace}/{table}/schema                    │
│  POST /query     → build_connection(session creds), run SQL,        │
│                     record a history row, return rows               │
│  GET  /history   → this principal's past queries, from Postgres     │
└────────────────────────────────────────────────────────────────┘
       │ DuckDB / REST, session   │ REST, root svc   │ SQL
       │ creds (data + catalog)   │ creds (/me only)  │
       ▼                          ▼                   ▼
┌─────────────┐          ┌────────────────┐    ┌──────────────────┐
│ Polaris      │◄─────────│ Iceberg REST /  │    │ Postgres           │
│ (data query) │ vended    │ Management API  │    │ lakehouse_ui DB    │
└─────────────┘  creds    └────────────────┘    │ on polaris-postgres │
                                                    └──────────────────┘
```

## Components

### Login / session

`POST /login` takes `{client_id, client_secret}`, calls Polaris's token
endpoint (`POST {POLARIS_ENDPOINT}/v1/oauth/tokens`, `grant_type=client_credentials`)
directly (no DuckDB involved — this is just an HTTP call). A non-200 response
is invalid credentials → 401. On success:

- Decode the returned JWT's payload (base64+JSON, no signature check needed
  — see "Infra/deployment changes" below) for the principal name: it's the
  `sub` claim (confirmed live — a real decoded token looks like
  `{"iss":"polaris","sub":"loader","principalId":...,"client_id":...,
  "scope":"PRINCIPAL_ROLE:ALL"}`; note there is **no role-list claim** —
  see the "Who am I" section's correction for how roles actually get
  looked up).
- Create a session: a random session ID, stored server-side (in-memory dict
  — acceptable at `replicas: 1`, matching the existing chart) mapping to
  `{client_id, client_secret, principal_name, created_at}`. Roles are not
  cached in the session — `/me` looks them up fresh each time via the
  root service credential (see below); cheap enough not to bother caching.
- Set an httpOnly, samesite=lax cookie holding only the session ID — the
  actual Polaris credentials never reach client-side JS.

`POST /logout` deletes the session and clears the cookie. Every other route
requires a valid session cookie (401 + redirect-to-login on the frontend if
missing/expired). No session TTL enforcement beyond process lifetime for v1
— acceptable for a local, non-internet-facing tool; noted as a gap if that
ever changes (same framing the original design used for other v1 gaps).

`app/catalog.py`'s `build_connection()` changes from reading
`POLARIS_CLIENT_ID`/`POLARIS_CLIENT_SECRET` env vars to taking them as
parameters — the caller (the `/query` route) passes the current session's
credentials. `POLARIS_ENDPOINT`/`POLARIS_CATALOG` stay as env vars (not
per-user).

### The read-only guard goes away

`_is_read_only` / `_is_single_statement` in `app/main.py` are removed.
Authorization is now entirely Polaris's job: a principal without write
grants gets a 403 from Polaris itself on a write attempt, surfaced to the
user as a normal query error. This is a deliberate, discussed trade — the
guard existed specifically because there was no real identity/authorization
before now.

### Catalog browser (`app/polaris_client.py`, new)

A thin REST client (stdlib `urllib` or `httpx` — pick at implementation
time based on what's already a dependency) for Polaris's Iceberg Catalog
API, called with the session's own credentials so results are naturally
scoped to what that principal can see:

- `list_namespaces(catalog)` → `GET /v1/{catalog}/namespaces`
- `list_tables(catalog, namespace)` → `GET /v1/{catalog}/namespaces/{ns}/tables`
- `get_table_schema(catalog, namespace, table)` → `GET /v1/{catalog}/namespaces/{ns}/tables/{table}` (the `schema` field of the response)

Exposed via `GET /catalog/namespaces`, `GET /catalog/tables/{namespace}`,
`GET /catalog/tables/{namespace}/{table}/schema`, each requiring a session.
Each call does its own fresh OAuth exchange with the session's
`client_id`/`client_secret` (same client-credentials grant `build_connection`
already uses via DuckDB's `CREATE SECRET`) rather than caching the token
from login — avoids a token-expiry edge case (Polaris's default token
lifetime is 1 hour; a long-lived session shouldn't silently break) for the
cost of one extra HTTP round trip per catalog call.

Sidebar renders this as an expandable tree (catalog → namespace → tables);
clicking a table inserts its fully-qualified name at the editor cursor.

### Worksheets (frontend only)

An array of `{id, title, sql, results}` in JS state, one active tab
rendered at a time. `+` appends a new blank one and activates it, `✕`
removes one (falling back to an adjacent tab, or a fresh blank one if it
was the last). Not persisted across a page reload — in-memory browser state
only, matching "simple" over building a saved-tabs feature nobody asked
for.

### Query history

New database on the **existing** `polaris-postgres` Postgres instance —
not a new StatefulSet/PVC. `charts/polaris-postgres/values.yaml` gets a
second `customScripts` entry (alongside the existing `userDatabase`-created
`polaris` database) creating a `lakehouse_ui` database and a dedicated
non-superuser user for it, matching the isolation Task 9's review already
established for the `polaris` database.

**One secret, not two.** The same `polaris-postgres` Secret gets 3 new
keys (`LAKEHOUSE_UI_DB`, `LAKEHOUSE_UI_USER_NAME`,
`LAKEHOUSE_UI_USER_PASSWORD`) — the `customScripts` block running inside
the Postgres pod can only read what's mounted there (the `polaris-postgres`
Secret), so the values have to live there regardless; pointing
`lakehouse-ui`'s Deployment at a *second*, separate secret holding the same
values duplicated under different names is exactly the anti-pattern Task
9's review already caught once for `polaris`/`polaris-postgres` — same fix
applies here. `lakehouse-ui`'s Deployment mounts `polaris-postgres`
directly (like `polaris`'s Deployment already does for its own keys).

One table:
```sql
CREATE TABLE query_history (
    id            BIGSERIAL PRIMARY KEY,
    principal     TEXT NOT NULL,
    sql_text      TEXT NOT NULL,
    status        TEXT NOT NULL,        -- 'success' | 'error'
    row_count     INTEGER,              -- null on error
    error_message TEXT,                 -- null on success
    duration_ms   INTEGER NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX query_history_principal_run_at ON query_history (principal, run_at DESC);
```
`POST /query` writes one row per attempt (success or error) after running.
`GET /history` returns the current session's principal's own rows, newest
first, paginated (`?limit=&offset=`). Clicking a row in the sidebar's
History view loads that SQL into a new tab. No cross-principal visibility —
each person only ever sees their own history, matching Snowflake/BigQuery's
own per-account history model.

### Editor

Replace the `<textarea>` with CodeMirror 6 (`@codemirror/lang-sql` for SQL
highlighting), loaded from a CDN — SQL keyword/string/comment highlighting
only, no autocomplete, no linting. Cmd/Ctrl+Enter still runs the active
tab's query (existing behavior, ported over).

### "Who am I"

A small header element: `{principal_name} · {principal_roles joined}`,
populated from `GET /me` on page load. A "Log out" action next to it calls
`POST /logout` and returns to the login screen.

**Correction from a live check against the real Polaris deployment**: the
login token's JWT claims do **not** include role names (confirmed by
decoding a real token — `sub`, `principalId`, `client_id`, `scope`, no
roles), and a regular principal is **not authorized to list its own roles**
via the Management API (`GET /principals/{name}/principal-roles` 403s for
a non-admin principal, confirmed live with the `loader` principal). So
`/me` cannot be answered from the session alone.

Fix: the app backend holds its own **root-level Polaris service
credential** (reusing the existing `polaris-root-credentials` Secret,
already created by `bootstrap/polaris-setup.sh`) used *only* for this one
lookup — `GET /principals/{sub}/principal-roles` as root, where `{sub}` is
the logged-in principal's own name from their login JWT. This is a real
trust boundary worth being explicit about: the app process holds a
root-equivalent Polaris credential, kept separate from and never used for
the per-session credentials that run actual data queries (those still run
strictly as the logged-in principal, Polaris-authorized per their own
grants). Confirmed live: `GET /principals/loader/principal-roles` as root
returns `{"roles":[{"name":"loader_role",...}]}` — the shape `/me` needs.

### Infra/deployment changes

- `charts/lakehouse-ui/`: remove the fixed `POLARIS_CLIENT_ID`/
  `POLARIS_CLIENT_SECRET` env vars and the `lakehouse-ui-polaris-credentials`
  Secret reference (login replaces them for the user-facing path); keep
  `POLARIS_ENDPOINT`/`POLARIS_CATALOG`. Add `POLARIS_ROOT_CLIENT_ID`/
  `POLARIS_ROOT_CLIENT_SECRET`, sourced from the existing
  `polaris-root-credentials` Secret — the service-level credential `/me`
  uses. Also add `POLARIS_MANAGEMENT_ENDPOINT` (plain value,
  `http://polaris.lakehouse.svc.cluster.local:8181/api/management`) — the
  Management API (principals/roles) lives at a **different base path**
  than `POLARIS_ENDPOINT`'s `/api/catalog` (confirmed live: `/api/catalog`
  and `/api/management` are siblings under the same host:port, not
  nested), so this needs its own explicit config rather than being derived
  by string-manipulating `POLARIS_ENDPOINT`. Add `LAKEHOUSE_UI_DB_HOST` (plain value, `polaris-postgres`),
  `LAKEHOUSE_UI_DB_NAME`/`LAKEHOUSE_UI_DB_USER`/`LAKEHOUSE_UI_DB_PASSWORD`
  sourced from the (extended) `polaris-postgres` Secret's `LAKEHOUSE_UI_DB`/
  `LAKEHOUSE_UI_USER_NAME`/`LAKEHOUSE_UI_USER_PASSWORD` keys.
- `charts/polaris-postgres/values.yaml`: add a second `customScripts` entry
  (alongside the existing `userDatabase`-created `polaris` database)
  creating a `lakehouse_ui` database and a dedicated non-superuser user for
  it — same isolation pattern Task 9's review established for `polaris`'s
  own database. 3 new keys added to the existing `polaris-postgres` Secret
  out of band: `LAKEHOUSE_UI_DB`, `LAKEHOUSE_UI_USER_NAME`,
  `LAKEHOUSE_UI_USER_PASSWORD` — no new Secret, no new StatefulSet/PVC.
- `requirements.txt`: add a Postgres driver (`psycopg[binary]`) for the
  query-history table. No JWT library needed — the JWT's payload segment
  is just base64+JSON, decoded with stdlib (`base64`, `json`); its
  signature doesn't need independent verification since the token is
  already trusted (we received it directly from Polaris over the same
  connection we're about to use it on, not a token presented by a third
  party).

## Data flow (a query, end to end)

```
1. User logs in with their Polaris principal's client_id/secret
2. Session created; sidebar loads the catalog tree using that principal's creds
3. User writes/picks a query in a worksheet tab, hits Run
4. POST /query → build_connection(session creds) → DuckDB attaches the
   catalog as that principal, runs the SQL
5. Polaris authorizes (or 403s) the operation per that principal's grants
6. Result (or error) written to query_history; rows returned to the browser
```

## Verification / Definition of Done

- Logging in with `loader`'s credentials and running
  `CREATE TABLE lakehouse.nyc_taxi.test_x AS SELECT 1` succeeds (loader has
  write grants); the same query while logged in as `lakehouse-ui`'s
  principal fails with a Polaris 403, surfaced as a query error, not an app
  crash.
- The sidebar catalog tree matches what `polaris tables list` shows for
  that same principal via the CLI.
- Query history persists across a `kubectl rollout restart` of
  `lakehouse-ui` (proves it's really in Postgres, not in-memory).
- Two people (or two browser sessions logged in as different principals)
  see only their own history in `/history`.
- The editor has visible SQL syntax highlighting; Cmd/Ctrl+Enter still runs
  the query.
- `helm lint`/`helm template` clean for both changed charts
  (`lakehouse-ui`, `polaris-postgres`); no `POLARIS_CLIENT_ID`/
  `POLARIS_CLIENT_SECRET` env vars remain in the `lakehouse-ui` Deployment.

## Explicitly out of scope for this pass

- Principal/role/grant management UI (Polaris Console's job).
- Autocomplete, linting, or any editor intelligence beyond syntax
  highlighting.
- Cross-tab/cross-device persistence of open worksheets.
- Session TTL/expiry enforcement, rate limiting on login attempts, CSRF
  protection beyond samesite cookies — real gaps, acceptable for a
  local, non-internet-facing tool; same framing as the original design's
  own explicitly-deferred items.
- A data/table preview panel (you didn't select it in scoping) — the
  catalog tree shows schema on click, but not sample rows.
- Multi-replica session sharing (session store stays in-memory,
  `replicas: 1`, same as today).
