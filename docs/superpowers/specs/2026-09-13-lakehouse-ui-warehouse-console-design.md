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

## Verification results (2026-09-12, live against the real cluster)

All Definition of Done items above were verified live, end to end,
through the actual deployed UI — not just via `pytest`. Three real bugs
surfaced during this pass that no earlier unit test or code review had
caught, because none of them exercised a real request against the real
Polaris/DuckDB/Postgres stack from inside the actual container:

1. **`/me` 404'd, then 502'd.** `get_principal_roles`'s Management API
   call was missing the `/v1` path prefix every other Catalog/Management
   path in the client has (`/principals/{name}/principal-roles` instead
   of `/v1/principals/{name}/principal-roles`) — confirmed live: Polaris
   1.7.0 returns a 404 for the un-prefixed path. Fixed in
   `lakehouse-ui@4e7169e`.
2. **`/me` still 502'd after that fix.** `get_principal_roles` only ever
   received `management_endpoint` and used it as the base for both the
   OAuth token exchange *and* the roles lookup — but Polaris has no
   `/v1/oauth/tokens` under the Management API base path, only under the
   Catalog API base path (confirmed live: a 404 on the management base).
   The function now takes `catalog_endpoint` (token) and
   `management_endpoint` (roles lookup) as separate parameters; `/me`
   passes `POLARIS_ENDPOINT` and `POLARIS_MANAGEMENT_ENDPOINT`
   accordingly. Fixed in `lakehouse-ui@5a49316`, with regression tests
   pinning the token request to the catalog endpoint specifically.
3. **The first real write query failed**: `CREATE TABLE ... AS SELECT`
   returned `IO Error: Failed to create directory "data": Read-only file
   system`. DuckDB's Iceberg extension creates a CWD-relative `data`
   directory as part of a write, and the image's `WORKDIR` (`/app`) sits
   on the container's read-only root filesystem. Fixed in
   `charts/lakehouse-ui` (test-k8s-configs): `workingDir: /tmp` (already
   a writable `emptyDir` mount, added during the original hardening pass
   for DuckDB's extension/temp-file needs) plus `PYTHONPATH=/app` so
   `app.main` stays importable without `/app` as the process's CWD.
   Reproduced and confirmed fixed directly inside the running pod before
   committing.

None of these were caught earlier because: the `/me` route's tests mock
`get_principal_roles` entirely (by design — it's a thin REST client, not
business logic to re-test at that layer), and `test_polaris_client.py`'s
fake HTTP server matched requests by URL *substring*, which is exactly
why a missing path segment or a token request hitting the wrong base URL
could both pass silently. All three are now covered by tests that assert
the literal expected URL, not just "some request happened."

With all three live-discovered bugs fixed, every Definition of Done item
was reverified against the real cluster:

- ✅ `loader` (write grants): `CREATE TABLE lakehouse.nyc_taxi.test_x AS
  SELECT 1` succeeds (result: `Count = 1`).
- ✅ `lakehouse-ui` principal (no write grants): the same shape of query
  fails with a Polaris 403 (`not authorized for op
  CREATE_TABLE_STAGED_WITH_WRITE_DELEGATION`), surfaced as a query error
  in the UI — no crash, no 500.
- ✅ Sidebar catalog tree for `nyc_taxi` (`dim_payment_type`,
  `mart_daily_summary`, `fct_trips`, `dim_rate_code`, `dim_vendor`,
  `trips`) matches a direct Iceberg Catalog API `tables` listing exactly.
- ✅ Query history (7 entries logged in while testing as `loader`)
  survived a full `kubectl rollout restart deployment/lakehouse-ui` —
  confirmed it's really in Postgres, not in-memory.
- ✅ Per-principal isolation: after the restart, `loader`'s `/history`
  shows all 7 of its own entries; `lakehouse-ui`'s principal's `/history`
  shows only its own single (403'd) attempt — no cross-principal leakage
  either direction.
- ✅ Editor shows visible SQL syntax highlighting (CodeMirror SQL mode);
  Ctrl+Enter runs the active worksheet's query.
- ✅ `helm lint`/`helm template` clean for both `charts/lakehouse-ui` and
  `charts/polaris-postgres` on the final merged `master`.
- ✅ `kubectl get deployment lakehouse-ui -n lakehouse -o yaml | grep -i
  POLARIS_CLIENT_ID` returns nothing.
- No `.claude/CLAUDE.md` update needed — `lakehouse-ui` was already
  listed in both the catalog inventory and deployment matrix tables from
  the original lakehouse build; this pass changed an existing
  Application, it didn't add one.
