# lakehouse-ui Warehouse Resource Management Design

**Goal:** Bring the minimal essential set of BigQuery-console-style
resource-management features to `lakehouse-ui`: create/delete datasets
and tables, view resource details (schema, storage location, row/file
counts), and view (read-only) who has access to what.

**Grounded live against the real Polaris instance** (2026-09-14) before
writing this, not assumed:

- A principal with `CATALOG_MANAGE_CONTENT` (`loader`, already the
  principal used for writes today) can `CREATE SCHEMA`/`DROP SCHEMA`
  directly through **the existing `/query` endpoint** — confirmed live:
  `CREATE SCHEMA lakehouse.scratch_probe_schema` returned 200, the
  namespace showed up in a direct Catalog API `namespaces` list, and
  `DROP SCHEMA` cleanly removed it. Same mechanism already handles
  `CREATE TABLE`.
- The same principal gets a clean `403 Forbidden` on every
  Management-API admin operation (`LIST_PRINCIPALS`,
  `LIST_CATALOG_ROLES`, `LIST_GRANTS_FOR_CATALOG_ROLE`) — confirmed live.
  Only `root` can see this information; there is no principal-scoped
  "show me what I can access" self-service view Polaris exposes.
- The Iceberg REST table-metadata response `app.polaris_client.
  get_table_schema` already fetches (and only partially uses) contains
  `location`, `last-updated-ms`, `current-snapshot-id`, and a `snapshots`
  array whose latest entry's `summary` has `total-records`/
  `total-data-files`/`operation` — confirmed live against
  `nyc_taxi.trips`. No new Polaris call needed for table details, just
  extracting more of a response already in hand.
- The exact Management API paths for the access view, confirmed live
  with real 200s: `GET /v1/principals`, `GET
  /v1/principals/{name}/principal-roles` (already used by `/me`), `GET
  /v1/principal-roles/{role}/catalog-roles/{catalog}`, `GET
  /v1/catalogs/{catalog}/catalog-roles/{role}/grants`.

---

## Architecture

### 1. Create/delete dataset — frontend-only, no backend changes

A "+ New Dataset" control in the sidebar header opens a small dialog
(name only — matching BigQuery's minimal "Dataset ID" flow). On submit,
the frontend does exactly what a user typing SQL would: `POST /query`
with `CREATE SCHEMA lakehouse.<name>`, then reloads the catalog tree.
Delete is the mirror: a small trash icon next to each namespace in the
tree, a confirm dialog, then `DROP SCHEMA lakehouse.<name>` via the same
endpoint. Polaris's own authorization is the only gate, exactly as it
already is for every other write this app makes — a `lakehouse-ui`-role
principal attempting this gets the same 403-surfaced-as-a-query-error
pattern `/query` already handles today, no new error path needed.

### 2. Create/delete table — frontend-only, two creation paths

**Guided form**: a "+ New Table" control per-namespace opens a small
column editor (rows of name/type/nullable, add/remove row, matching
BigQuery's schema editor in spirit but far simpler). On submit, the
frontend builds `CREATE TABLE lakehouse.<ns>.<name> (<col> <type>[ NOT
NULL], ...)` and submits it via `/query`.

**Save query results as a table**: a "Save as table" action next to the
Run button, enabled once a query has results. Prompts for a target
namespace + table name, then re-submits the *active worksheet's own SQL*
wrapped as `CREATE TABLE lakehouse.<ns>.<name> AS <original query>` via
`/query`. Cheap to add given the same mechanism, and a genuinely common
BigQuery/Snowflake workflow (materialize a query you just wrote).

Delete: a trash icon next to each table in the tree, confirm dialog,
`DROP TABLE lakehouse.<ns>.<name>` via `/query`.

### 3. Resource details — extends one existing response, adds two new routes

**Backend**: `app/polaris_client.py` gets a new function
`get_table_details(catalog_endpoint, client_id, client_secret, catalog,
namespace, table) -> dict`, sibling to the existing `get_table_schema`
(which stays as-is, untouched, since the existing
`/catalog/tables/{namespace}/{table}/schema` route already depends on
its exact return shape). `get_table_details` calls the same underlying
`_get()` the existing function does, but extracts more of the response:
```python
{
    "fields": [...],               # same shape get_table_schema already returns
    "location": "s3://lakehouse/nyc_taxi/trips",
    "last_updated_ms": 1789357660542,
    "current_snapshot": {          # None if the table has never been written to
        "operation": "overwrite",
        "total_records": 2964624,
        "total_data_files": 5,
        "timestamp_ms": 1789357660542,
    },
    "properties": {"created-at": "...", "write.parquet.compression-codec": "zstd"},
}
```
A second new function `get_namespace_details(catalog_endpoint, client_id,
client_secret, catalog, namespace) -> dict` calls `GET
/v1/{catalog}/namespaces/{namespace}` for the namespace's own properties
(mainly `location`); table count is computed by the route from the
existing `list_tables` call, not a new Polaris concept.

**New routes** in `app/main.py`: `GET
/catalog/tables/{namespace}/{table}/details` and `GET
/catalog/namespaces/{namespace}/details` — same session-gating,
`PolarisClientError` → 502 mapping every other catalog route already
uses.

**Frontend**: a small "ⓘ" affordance next to each table/namespace in the
tree (separate from the existing click-to-insert-name behavior, which
stays exactly as it is) opens a details panel showing the fields above.

### 4. Access — view-only, root-backed, one new aggregating route

Per the scoping decision: **view-only for everyone logged in**, no
grant/revoke through the UI. A new `GET /access` route in `app/main.py`,
authenticated the same way `/me` is (any logged-in session can call it),
but internally uses the app's own root service credential
(`POLARIS_ROOT_CLIENT_ID`/`_SECRET`, already wired up for `/me`) to walk
the chain live-confirmed above, for every principal Polaris knows about:

```
list_principals()
  for each principal:
    get_principal_roles(principal)                                  # existing function, already used by /me
    for each principal_role:
      get_catalog_roles_for_principal_role(principal_role, "lakehouse")  # new
      for each catalog_role:
        get_grants_for_catalog_role(catalog_role, "lakehouse")           # new
```
returning one aggregated payload:
```json
{
  "principals": [
    {
      "name": "loader",
      "principal_roles": [
        {
          "name": "loader_role",
          "catalog_roles": [
            {"name": "loader_catalog_role", "grants": ["CATALOG_MANAGE_CONTENT"]}
          ]
        }
      ]
    }
  ]
}
```
This is N+1-ish (one call per principal × principal-role × catalog-role —
fine at this scale, 3 principals today; if that ever grows into the
dozens this would need caching, explicitly not solved here). Two new
`app/polaris_client.py` functions: `list_principals` and
`get_catalog_roles_for_principal_role`, `get_grants_for_catalog_role`
(three, all root-backed, all Management API, all against the paths
confirmed live above).

**Frontend**: a new "Access" sidebar tab (alongside the existing
Catalog/History tabs), fetched once on activation, rendered as a simple
nested list — principal → principal role(s) → catalog role(s) →
privilege(s). No editing controls anywhere on this tab.

## Data flow

```
Create/delete dataset & table:
  browser -> POST /query (CREATE SCHEMA / CREATE TABLE / DROP ...)
  -> DuckDB -> Polaris (session's own credentials) -> Iceberg REST
  (identical path every other write already takes)

Resource details:
  browser -> GET /catalog/{tables,namespaces}/.../details
  -> get_table_details / get_namespace_details (session's own credentials)
  -> Iceberg REST Catalog API (read-only)

Access (view-only):
  browser -> GET /access -> chain of Management API calls
  (app's root service credential, same as /me) -> aggregated read-only payload
```

## Verification / Definition of Done

- Create a dataset through the UI; confirm it appears in the catalog
  tree and via a direct Iceberg REST `namespaces` list.
- Create a table through the guided column editor; confirm its schema
  matches what was entered.
- Run a query, use "Save as table"; confirm the new table's row count
  matches the original query's result count.
- View details for `nyc_taxi.trips`: location, current snapshot's row
  count, and file count all match what a direct Iceberg REST call to the
  same table returns.
- View details for the `nyc_taxi` namespace: location present, table
  count matches the catalog tree's own count for that namespace.
- Delete a table and a dataset created during testing; confirm both
  gone from the tree and from a direct Iceberg REST list.
- Log in as `lakehouse-ui` (read-only role), attempt to create a
  dataset: fails with a Polaris 403 surfaced as a query error, not a
  crash — same pattern every other write-attempt-by-a-read-only-principal
  already follows.
- View the Access tab: shows all 3 known principals (`root`, `loader`,
  `lakehouse-ui`), each with their real principal role → catalog role →
  grant chain, matching a direct root-credentialed Management API query
  of the same data.
- Existing `pytest` suite stays green; new tests cover the 2 new
  `app/polaris_client.py` detail functions, the 3 new access-chain
  functions, and the 3 new routes (auth-gating, success shape, error
  mapping) — mirroring existing test patterns in
  `tests/test_catalog_routes.py`/`tests/test_me_route.py`.

## Explicitly out of scope

- Granting or revoking access through the UI (the scoping decision above
  — view-only for this pass; doing this safely needs a real per-user
  admin flag to gate it on, which doesn't exist yet).
- Editing an existing table's schema (add/drop/rename columns) —
  BigQuery has this; not essential for a first pass, and Iceberg schema
  evolution has its own sharp edges worth a dedicated look later.
- Dataset/table descriptions, labels, or other BigQuery-style metadata
  Polaris doesn't have a natural home for.
- Caching the Access tab's N+1 Management API chain — fine at 3
  principals, a real problem if this ever needs to scale past a handful.
- Any change to the existing click-to-insert-name behavior on the
  catalog tree, or to the existing `get_table_schema`/
  `/catalog/tables/{namespace}/{table}/schema` route.
