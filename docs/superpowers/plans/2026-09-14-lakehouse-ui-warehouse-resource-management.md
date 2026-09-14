# lakehouse-ui Warehouse Resource Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create/delete datasets and tables, resource details, and a
view-only Access tab — the minimal essential BigQuery-console-style
resource management features, built on what was live-confirmed Polaris
actually supports (see the design doc's opening section).

**Architecture:** Tasks 1-4 are backend (`app/polaris_client.py` +
`app/main.py`), Tasks 5-7 are frontend (`app/static/*`) — both groups
chain-merge the prior tasks' unmerged branches before building (same
pattern the reliability & scaling plan used successfully: each task
starts by merging every earlier task's branch, so nobody works from a
stale `main`). Tasks 8-10 are main-session-only: merge, deploy, verify
live against the design doc's DoD.

**Tech Stack:** Python 3.12, FastAPI, vanilla JS (no framework, matching
the existing frontend).

---

### Task 1: `get_table_details` + `get_namespace_details`

**Files (in `lakehouse-ui`):**
- Modify: `app/polaris_client.py`
- Modify: `tests/test_polaris_client.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/table-namespace-details
```

- [ ] **Step 2: Write the failing tests**

Read `tests/test_polaris_client.py` first to see its exact
`_fake_urlopen`/`_response`/`TOKEN_RESPONSE` helpers (already defined at
the top of the file — reuse them, don't redefine). Add these tests
anywhere in the file:

```python
def test_get_table_details_returns_fields_location_and_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "namespaces/nyc_taxi/tables/trips": {
                    "metadata": {
                        "location": "s3://lakehouse/nyc_taxi/trips",
                        "last-updated-ms": 1789357660542,
                        "current-schema-id": 0,
                        "current-snapshot-id": 999,
                        "schemas": [
                            {
                                "schema-id": 0,
                                "fields": [
                                    {"name": "VendorID", "type": "int", "required": False},
                                ],
                            }
                        ],
                        "snapshots": [
                            {
                                "snapshot-id": 999,
                                "timestamp-ms": 1789357660542,
                                "summary": {
                                    "operation": "overwrite",
                                    "total-records": "2964624",
                                    "total-data-files": "5",
                                },
                            }
                        ],
                        "properties": {"created-at": "2026-09-14T03:47:19Z"},
                    }
                },
            }
        ),
    )

    result = get_table_details(
        "http://polaris:8181/api/catalog", "cid", "secret", "lakehouse", "nyc_taxi", "trips"
    )

    assert result == {
        "fields": [{"name": "VendorID", "type": "int", "required": False}],
        "location": "s3://lakehouse/nyc_taxi/trips",
        "last_updated_ms": 1789357660542,
        "current_snapshot": {
            "operation": "overwrite",
            "total_records": "2964624",
            "total_data_files": "5",
            "timestamp_ms": 1789357660542,
        },
        "properties": {"created-at": "2026-09-14T03:47:19Z"},
    }


def test_get_table_details_current_snapshot_is_none_for_an_empty_table(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "namespaces/nyc_taxi/tables/empty_table": {
                    "metadata": {
                        "location": "s3://lakehouse/nyc_taxi/empty_table",
                        "last-updated-ms": 123,
                        "current-schema-id": 0,
                        "current-snapshot-id": None,
                        "schemas": [{"schema-id": 0, "fields": []}],
                        "snapshots": [],
                        "properties": {},
                    }
                },
            }
        ),
    )

    result = get_table_details(
        "http://polaris:8181/api/catalog", "cid", "secret", "lakehouse", "nyc_taxi", "empty_table"
    )

    assert result["current_snapshot"] is None
    assert result["fields"] == []


def test_get_namespace_details_returns_properties(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "namespaces/nyc_taxi": {"properties": {"location": "s3://lakehouse/nyc_taxi/"}},
            }
        ),
    )

    result = get_namespace_details(
        "http://polaris:8181/api/catalog", "cid", "secret", "lakehouse", "nyc_taxi"
    )

    assert result == {"properties": {"location": "s3://lakehouse/nyc_taxi/"}}
```

Add `get_table_details` and `get_namespace_details` to the existing
`from app.polaris_client import (...)` import at the top of the test
file (alongside whatever's already imported there — read the file to see
the exact current import block and extend it, don't replace it).

- [ ] **Step 3: Run the tests to verify they fail**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_polaris_client.py -v
```
Expected: the 3 new tests FAIL (`ImportError` or `AttributeError` — the
functions don't exist yet); all pre-existing tests in this file still
pass.

- [ ] **Step 4: Add the functions to `app/polaris_client.py`**

Read the file first. Add these two functions after the existing
`get_table_schema` function (before `get_principal_roles`):

```python
def get_table_details(
    catalog_endpoint: str,
    client_id: str,
    client_secret: str,
    catalog: str,
    namespace: str,
    table: str,
) -> dict:
    """Return richer table details than get_table_schema: fields, storage
    location, last-updated timestamp, current snapshot's row/file counts
    (None if the table has never been written to), and properties.

    Same underlying Iceberg REST response get_table_schema already
    fetches — no new Polaris call, just extracting more of it (confirmed
    live against a real table, 2026-09-14)."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(
        catalog_endpoint, token, f"/v1/{catalog}/namespaces/{namespace}/tables/{table}"
    )
    metadata = body.get("metadata", {})
    schemas = metadata.get("schemas", [])
    current_schema_id = metadata.get("current-schema-id")
    schema = next(
        (s for s in schemas if s.get("schema-id") == current_schema_id),
        schemas[0] if schemas else {},
    )
    try:
        fields = [
            {"name": f["name"], "type": f["type"], "required": f["required"]}
            for f in schema.get("fields", [])
        ]
    except (KeyError, TypeError) as exc:
        raise PolarisClientError(f"unexpected response shape from Polaris: {exc}") from exc

    current_snapshot = None
    current_snapshot_id = metadata.get("current-snapshot-id")
    if current_snapshot_id is not None:
        for snap in metadata.get("snapshots", []):
            if snap.get("snapshot-id") == current_snapshot_id:
                summary = snap.get("summary", {})
                current_snapshot = {
                    "operation": summary.get("operation"),
                    "total_records": summary.get("total-records"),
                    "total_data_files": summary.get("total-data-files"),
                    "timestamp_ms": snap.get("timestamp-ms"),
                }
                break

    return {
        "fields": fields,
        "location": metadata.get("location"),
        "last_updated_ms": metadata.get("last-updated-ms"),
        "current_snapshot": current_snapshot,
        "properties": metadata.get("properties", {}),
    }


def get_namespace_details(
    catalog_endpoint: str, client_id: str, client_secret: str, catalog: str, namespace: str
) -> dict:
    """Return a namespace's own properties (mainly its storage location)."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(catalog_endpoint, token, f"/v1/{catalog}/namespaces/{namespace}")
    return {"properties": body.get("properties", {})}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_polaris_client.py -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add app/polaris_client.py tests/test_polaris_client.py
git commit -m "feat: add get_table_details and get_namespace_details"
```

Do not push.

---

### Task 2: Management API functions for the Access view

**Files (in `lakehouse-ui`):**
- Modify: `app/polaris_client.py`
- Modify: `tests/test_polaris_client.py`

- [ ] **Step 1: Branch (chain-merging Task 1)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/access-polaris-client
git merge --no-edit feat/table-namespace-details
```
If this reports a conflict, STOP and report BLOCKED with the exact
conflict details.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_polaris_client.py`:

```python
def test_list_principals_returns_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "principals": {
                    "principals": [{"name": "root"}, {"name": "loader"}, {"name": "lakehouse-ui"}]
                },
            }
        ),
    )

    result = list_principals(
        "http://polaris:8181/api/catalog", "http://polaris:8181/api/management", "root", "rootsecret"
    )

    assert result == ["root", "loader", "lakehouse-ui"]


def test_get_catalog_roles_for_principal_role_returns_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "principal-roles/loader_role/catalog-roles/lakehouse": {
                    "roles": [{"name": "loader_catalog_role"}]
                },
            }
        ),
    )

    result = get_catalog_roles_for_principal_role(
        "http://polaris:8181/api/catalog",
        "http://polaris:8181/api/management",
        "root",
        "rootsecret",
        "lakehouse",
        "loader_role",
    )

    assert result == ["loader_catalog_role"]


def test_get_grants_for_catalog_role_returns_privilege_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "catalogs/lakehouse/catalog-roles/loader_catalog_role/grants": {
                    "grants": [{"privilege": "CATALOG_MANAGE_CONTENT", "type": "catalog"}]
                },
            }
        ),
    )

    result = get_grants_for_catalog_role(
        "http://polaris:8181/api/catalog",
        "http://polaris:8181/api/management",
        "root",
        "rootsecret",
        "lakehouse",
        "loader_catalog_role",
    )

    assert result == ["CATALOG_MANAGE_CONTENT"]
```

Add `list_principals`, `get_catalog_roles_for_principal_role`,
`get_grants_for_catalog_role` to the test file's existing
`from app.polaris_client import (...)` block.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_polaris_client.py -v
```
Expected: the 3 new tests FAIL; everything else still passes.

- [ ] **Step 4: Add the functions to `app/polaris_client.py`**

Add after the existing `get_principal_roles` function (at the end of the
file):

```python
def list_principals(
    catalog_endpoint: str, management_endpoint: str, client_id: str, client_secret: str
) -> list[str]:
    """Return the names of every principal Polaris knows about.

    Root-only in practice: a regular principal gets a 403
    (LIST_PRINCIPALS not authorized) — confirmed live, 2026-09-14."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(management_endpoint, token, "/v1/principals")
    try:
        return [p["name"] for p in body.get("principals", [])]
    except (KeyError, TypeError) as exc:
        raise PolarisClientError(f"unexpected response shape from Polaris: {exc}") from exc


def get_catalog_roles_for_principal_role(
    catalog_endpoint: str,
    management_endpoint: str,
    client_id: str,
    client_secret: str,
    catalog: str,
    principal_role: str,
) -> list[str]:
    """Return the catalog role names assigned to `principal_role` on
    `catalog`. Root-only, same reasoning as list_principals."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(
        management_endpoint,
        token,
        f"/v1/principal-roles/{principal_role}/catalog-roles/{catalog}",
    )
    try:
        return [r["name"] for r in body.get("roles", [])]
    except (KeyError, TypeError) as exc:
        raise PolarisClientError(f"unexpected response shape from Polaris: {exc}") from exc


def get_grants_for_catalog_role(
    catalog_endpoint: str,
    management_endpoint: str,
    client_id: str,
    client_secret: str,
    catalog: str,
    catalog_role: str,
) -> list[str]:
    """Return the privilege names granted to `catalog_role` on `catalog`.
    Root-only, same reasoning as list_principals."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(
        management_endpoint,
        token,
        f"/v1/catalogs/{catalog}/catalog-roles/{catalog_role}/grants",
    )
    try:
        return [g["privilege"] for g in body.get("grants", [])]
    except (KeyError, TypeError) as exc:
        raise PolarisClientError(f"unexpected response shape from Polaris: {exc}") from exc
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_polaris_client.py -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add app/polaris_client.py tests/test_polaris_client.py
git commit -m "feat: add Management API functions for the Access view"
```

Do not push.

---

### Task 3: `/catalog/tables/{namespace}/{table}/details` + `/catalog/namespaces/{namespace}/details` routes

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Modify: `tests/test_catalog_routes.py`

- [ ] **Step 1: Branch (chain-merging Tasks 1-2)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/details-routes
git merge --no-edit feat/table-namespace-details
git merge --no-edit feat/access-polaris-client
```
STOP and report BLOCKED on any conflict.

- [ ] **Step 2: Write the failing tests**

Read `tests/test_catalog_routes.py` first for its exact
`_logged_in_cookie()` helper and monkeypatch style (matches the pattern
used throughout this file — `monkeypatch.setattr(main_module, "...", ...)`).
Add:

```python
def test_table_details_returns_the_details_dict(monkeypatch):
    fake_details = {
        "fields": [{"name": "id", "type": "long", "required": True}],
        "location": "s3://lakehouse/nyc_taxi/trips",
        "last_updated_ms": 123,
        "current_snapshot": {
            "operation": "overwrite",
            "total_records": "100",
            "total_data_files": "2",
            "timestamp_ms": 123,
        },
        "properties": {},
    }
    monkeypatch.setattr(main_module, "get_table_details", lambda *a, **kw: fake_details)

    response = client.get(
        "/catalog/tables/nyc_taxi/trips/details", cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    assert response.json() == fake_details


def test_table_details_returns_502_on_polaris_client_error(monkeypatch):
    def raise_error(*a, **kw):
        raise PolarisClientError("not authorized for op (403)")

    monkeypatch.setattr(main_module, "get_table_details", raise_error)

    response = client.get(
        "/catalog/tables/nyc_taxi/trips/details", cookies=_logged_in_cookie()
    )

    assert response.status_code == 502


def test_table_details_requires_a_session():
    response = client.get("/catalog/tables/nyc_taxi/trips/details")
    assert response.status_code == 401


def test_namespace_details_returns_properties_and_table_count(monkeypatch):
    monkeypatch.setattr(
        main_module, "get_namespace_details",
        lambda *a, **kw: {"properties": {"location": "s3://lakehouse/nyc_taxi/"}},
    )
    monkeypatch.setattr(main_module, "list_tables", lambda *a, **kw: ["trips", "fct_trips"])

    response = client.get(
        "/catalog/namespaces/nyc_taxi/details", cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    assert response.json() == {
        "properties": {"location": "s3://lakehouse/nyc_taxi/"},
        "table_count": 2,
    }


def test_namespace_details_returns_502_on_polaris_client_error(monkeypatch):
    def raise_error(*a, **kw):
        raise PolarisClientError("not authorized (403)")

    monkeypatch.setattr(main_module, "get_namespace_details", raise_error)

    response = client.get(
        "/catalog/namespaces/nyc_taxi/details", cookies=_logged_in_cookie()
    )

    assert response.status_code == 502


def test_namespace_details_requires_a_session():
    response = client.get("/catalog/namespaces/nyc_taxi/details")
    assert response.status_code == 401
```

Check whether `PolarisClientError` is already imported at the top of
this test file (it likely is, for existing tests) — if not, add
`from app.polaris_client import PolarisClientError` to its imports.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_catalog_routes.py -v
```
Expected: the 6 new tests FAIL (404s, since the routes don't exist yet);
everything else still passes.

- [ ] **Step 4: Add the routes to `app/main.py`**

Read the file first. Add `get_table_details, get_namespace_details` to
the existing `from app.polaris_client import (...)` block (extend it,
don't replace the other names already there — `PolarisClientError`,
`get_principal_roles`, `get_table_schema`, `list_namespaces`,
`list_tables` should all still be imported from that same block).

Add these two routes immediately after the existing
`@app.get("/catalog/tables/{namespace}/{table}/schema")` route (find it
first — do not modify that existing route at all):

```python
@app.get("/catalog/tables/{namespace}/{table}/details")
def catalog_table_details(
    namespace: str, table: str, session: Session = Depends(require_session)
) -> dict:
    endpoint, catalog = _catalog_config()
    try:
        details = get_table_details(
            endpoint, session.client_id, session.client_secret, catalog, namespace, table
        )
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return details


@app.get("/catalog/namespaces/{namespace}/details")
def catalog_namespace_details(
    namespace: str, session: Session = Depends(require_session)
) -> dict:
    endpoint, catalog = _catalog_config()
    try:
        details = get_namespace_details(
            endpoint, session.client_id, session.client_secret, catalog, namespace
        )
        tables = list_tables(
            endpoint, session.client_id, session.client_secret, catalog, namespace
        )
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    details["table_count"] = len(tables)
    return details
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing (run the full suite, not just this file, to catch
any import-ordering issue from the extended import block).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_catalog_routes.py
git commit -m "feat: add table and namespace details routes"
```

Do not push.

---

### Task 4: `/access` route

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Create: `tests/test_access_route.py`

- [ ] **Step 1: Branch (chain-merging Tasks 1-3)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/access-route
git merge --no-edit feat/table-namespace-details
git merge --no-edit feat/access-polaris-client
git merge --no-edit feat/details-routes
```
STOP and report BLOCKED on any conflict.

- [ ] **Step 2: Write the failing tests**

Read `tests/test_me_route.py` first — `/access` follows the exact same
root-credential pattern `/me` does (env vars, 500 if missing, 502 on
`PolarisClientError`). Create `tests/test_access_route.py`:

```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_client import PolarisClientError

client = TestClient(app)


def _logged_in_cookie(principal="loader"):
    session_id = main_module.create_session("cid", "secret", principal)
    return {"lakehouse_session": session_id}


def _set_env(monkeypatch):
    monkeypatch.setenv("POLARIS_ENDPOINT", "http://polaris:8181/api/catalog")
    monkeypatch.setenv("POLARIS_MANAGEMENT_ENDPOINT", "http://polaris:8181/api/management")
    monkeypatch.setenv("POLARIS_CATALOG", "lakehouse")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "root")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "rootsecret")


def test_access_requires_a_session():
    response = client.get("/access")
    assert response.status_code == 401


def test_access_returns_500_when_root_credentials_missing(monkeypatch):
    monkeypatch.setenv("POLARIS_ENDPOINT", "http://polaris:8181/api/catalog")
    monkeypatch.setenv("POLARIS_CATALOG", "lakehouse")
    monkeypatch.delenv("POLARIS_ROOT_CLIENT_ID", raising=False)

    response = client.get("/access", cookies=_logged_in_cookie())

    assert response.status_code == 500


def test_access_returns_the_full_principal_role_grant_chain(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(main_module, "list_principals", lambda *a, **kw: ["loader"])
    monkeypatch.setattr(main_module, "get_principal_roles", lambda *a, **kw: ["loader_role"])
    monkeypatch.setattr(
        main_module, "get_catalog_roles_for_principal_role", lambda *a, **kw: ["loader_catalog_role"]
    )
    monkeypatch.setattr(
        main_module, "get_grants_for_catalog_role", lambda *a, **kw: ["CATALOG_MANAGE_CONTENT"]
    )

    response = client.get("/access", cookies=_logged_in_cookie())

    assert response.status_code == 200
    assert response.json() == {
        "principals": [
            {
                "name": "loader",
                "principal_roles": [
                    {
                        "name": "loader_role",
                        "catalog_roles": [
                            {"name": "loader_catalog_role", "grants": ["CATALOG_MANAGE_CONTENT"]}
                        ],
                    }
                ],
            }
        ]
    }


def test_access_returns_502_on_polaris_client_error(monkeypatch):
    _set_env(monkeypatch)

    def raise_error(*a, **kw):
        raise PolarisClientError("not authorized (403)")

    monkeypatch.setattr(main_module, "list_principals", raise_error)

    response = client.get("/access", cookies=_logged_in_cookie())

    assert response.status_code == 502
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_access_route.py -v
```
Expected: FAIL (404s — the route doesn't exist yet).

- [ ] **Step 4: Add the route to `app/main.py`**

Read the file first. Add `list_principals,
get_catalog_roles_for_principal_role, get_grants_for_catalog_role` to the
existing `from app.polaris_client import (...)` block (extend, don't
replace).

Add this route immediately after the existing `@app.get("/me")` route
(find it first — do not modify that route):

```python
@app.get("/access")
def access_route(session: Session = Depends(require_session)) -> dict:
    root_client_id = os.environ.get("POLARIS_ROOT_CLIENT_ID")
    root_client_secret = os.environ.get("POLARIS_ROOT_CLIENT_SECRET")
    catalog_endpoint = os.environ.get("POLARIS_ENDPOINT")
    management_endpoint = os.environ.get("POLARIS_MANAGEMENT_ENDPOINT")
    _, catalog = _catalog_config()
    if not root_client_id or not root_client_secret or not catalog_endpoint or not management_endpoint:
        raise HTTPException(
            status_code=500,
            detail="server missing POLARIS_ROOT_CLIENT_ID/POLARIS_ROOT_CLIENT_SECRET/"
            "POLARIS_ENDPOINT/POLARIS_MANAGEMENT_ENDPOINT configuration",
        )
    try:
        principal_names = list_principals(
            catalog_endpoint, management_endpoint, root_client_id, root_client_secret
        )
        principals = []
        for name in principal_names:
            principal_roles = get_principal_roles(
                catalog_endpoint, management_endpoint, root_client_id, root_client_secret, name
            )
            role_entries = []
            for principal_role in principal_roles:
                catalog_roles = get_catalog_roles_for_principal_role(
                    catalog_endpoint,
                    management_endpoint,
                    root_client_id,
                    root_client_secret,
                    catalog,
                    principal_role,
                )
                catalog_role_entries = []
                for catalog_role in catalog_roles:
                    grants = get_grants_for_catalog_role(
                        catalog_endpoint,
                        management_endpoint,
                        root_client_id,
                        root_client_secret,
                        catalog,
                        catalog_role,
                    )
                    catalog_role_entries.append({"name": catalog_role, "grants": grants})
                role_entries.append(
                    {"name": principal_role, "catalog_roles": catalog_role_entries}
                )
            principals.append({"name": name, "principal_roles": role_entries})
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"principals": principals}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_access_route.py
git commit -m "feat: add the /access route"
```

Do not push.

---

### Task 5: Create/delete dataset + create/delete table UI

**Files (in `lakehouse-ui`):**
- Modify: `app/static/index.html`
- Modify: `app/static/app.css`
- Modify: `app/static/app.js`

- [ ] **Step 1: Branch (chain-merging Tasks 1-4)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/create-delete-ui
git merge --no-edit feat/table-namespace-details
git merge --no-edit feat/access-polaris-client
git merge --no-edit feat/details-routes
git merge --no-edit feat/access-route
```
STOP and report BLOCKED on any conflict.

- [ ] **Step 2: Add the modal container to `index.html`**

Read the file first. Add this as the last element inside `<div id="app">`,
right before `</div>` closes it (after the existing `<div id="main">...
</div>` block, still nested inside `#app`) — actually simpler and
equally correct: add it as a sibling right after `</div>` that closes
`#app`, before the CodeMirror `<script>` tags:

```html
  <div id="modal-overlay" hidden>
    <div id="modal-box"></div>
  </div>
```

Also add a "+ Dataset" button to `#sidebar-header`, right after the
existing `#refresh-catalog` button:

```html
        <button id="new-dataset-btn" type="button">+ Dataset</button>
```

- [ ] **Step 3: Add modal + tree-action CSS to `app.css`**

Append to the end of `app.css`:

```css
#modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
#modal-box {
  background: #141a21;
  border: 1px solid #2a333d;
  border-radius: 6px;
  padding: 1.2rem;
  min-width: 320px;
  max-width: 90vw;
  max-height: 85vh;
  overflow: auto;
}
#modal-box h3 { margin: 0 0 0.8rem; font-size: 1rem; }
#modal-box label { display: block; font-size: 0.85rem; color: #8a95a1; margin-bottom: 0.3rem; }
#modal-box input[type="text"] {
  width: 100%;
  background: #0f1419;
  border: 1px solid #2a333d;
  color: #e8edf2;
  padding: 0.4rem 0.6rem;
  border-radius: 4px;
  margin-bottom: 0.8rem;
  font-family: inherit;
}
#modal-box .modal-actions { display: flex; justify-content: flex-end; gap: 0.6rem; margin-top: 0.8rem; }
#modal-box button.primary { background: #2b8a6b; color: white; border: none; padding: 0.4rem 1rem; border-radius: 4px; cursor: pointer; }
#modal-box button.secondary { background: none; border: 1px solid #2a333d; color: #8a95a1; padding: 0.4rem 1rem; border-radius: 4px; cursor: pointer; }
#modal-box .modal-error { color: #e05252; font-size: 0.85rem; margin-top: 0.5rem; }
.tree-namespace-row, .tree-table-row { display: flex; align-items: center; justify-content: space-between; }
.tree-row-actions { display: flex; gap: 0.4rem; opacity: 0; }
.tree-namespace-row:hover .tree-row-actions, .tree-table-row:hover .tree-row-actions { opacity: 1; }
.tree-row-actions button { background: none; border: none; color: #8a95a1; cursor: pointer; font-size: 0.8rem; padding: 0 0.2rem; }
.tree-row-actions button:hover { color: #e8edf2; }
#new-dataset-btn { background: none; border: none; color: #8a95a1; cursor: pointer; padding: 0.6rem; font-size: 0.85rem; }
.new-table-column-row { display: flex; gap: 0.4rem; align-items: center; margin-bottom: 0.3rem; }
.new-table-column-row input[type="text"] { width: auto; flex: 1; margin-bottom: 0; }
.new-table-col-required { font-size: 0.75rem; color: #8a95a1; white-space: nowrap; }
```

- [ ] **Step 4: Add the modal system and create/delete functions to `app.js`**

Read the file first. Add these functions right after the existing
`apiFetch` function (before `newWorksheet`):

```javascript
function openModal(html) {
  const overlay = document.getElementById('modal-overlay');
  const box = document.getElementById('modal-box');
  box.innerHTML = html;
  overlay.hidden = false;
}

function closeModal() {
  document.getElementById('modal-overlay').hidden = true;
  document.getElementById('modal-box').innerHTML = '';
}

function openCreateDatasetModal() {
  openModal(`
    <h3>New dataset</h3>
    <label for="new-dataset-name">Dataset name</label>
    <input type="text" id="new-dataset-name" autocomplete="off">
    <div class="modal-error" id="new-dataset-error"></div>
    <div class="modal-actions">
      <button class="secondary" id="new-dataset-cancel" type="button">Cancel</button>
      <button class="primary" id="new-dataset-create" type="button">Create</button>
    </div>
  `);
  document.getElementById('new-dataset-cancel').addEventListener('click', closeModal);
  document.getElementById('new-dataset-create').addEventListener('click', submitCreateDataset);
  document.getElementById('new-dataset-name').focus();
}

async function submitCreateDataset() {
  const nameInput = document.getElementById('new-dataset-name');
  const errorEl = document.getElementById('new-dataset-error');
  const name = nameInput.value.trim();
  if (!name) {
    errorEl.textContent = 'Dataset name is required.';
    return;
  }
  try {
    await apiFetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: `CREATE SCHEMA lakehouse.${name}` }),
    });
  } catch (err) {
    if (err.message === 'not authenticated') return;
    errorEl.textContent = err.message;
    return;
  }
  closeModal();
  loadCatalog();
}

function confirmDeleteDataset(name) {
  openModal(`
    <h3>Delete dataset "${name}"?</h3>
    <p>This deletes the dataset and everything in it. This cannot be undone.</p>
    <div class="modal-error" id="delete-dataset-error"></div>
    <div class="modal-actions">
      <button class="secondary" id="delete-dataset-cancel" type="button">Cancel</button>
      <button class="primary" id="delete-dataset-confirm" type="button">Delete</button>
    </div>
  `);
  document.getElementById('delete-dataset-cancel').addEventListener('click', closeModal);
  document.getElementById('delete-dataset-confirm').addEventListener('click', () => submitDeleteDataset(name));
}

async function submitDeleteDataset(name) {
  const errorEl = document.getElementById('delete-dataset-error');
  try {
    await apiFetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: `DROP SCHEMA lakehouse.${name}` }),
    });
  } catch (err) {
    if (err.message === 'not authenticated') return;
    errorEl.textContent = err.message;
    return;
  }
  closeModal();
  loadCatalog();
}

let newTableColumnCount = 0;

function openCreateTableModal(namespace) {
  newTableColumnCount = 0;
  openModal(`
    <h3>New table in ${namespace}</h3>
    <label for="new-table-name">Table name</label>
    <input type="text" id="new-table-name" autocomplete="off">
    <label>Columns</label>
    <div id="new-table-columns"></div>
    <button class="secondary" id="new-table-add-column" type="button" style="margin-top:0.4rem;">+ Add column</button>
    <div class="modal-error" id="new-table-error"></div>
    <div class="modal-actions">
      <button class="secondary" id="new-table-cancel" type="button">Cancel</button>
      <button class="primary" id="new-table-create" type="button">Create</button>
    </div>
  `);
  document.getElementById('new-table-cancel').addEventListener('click', closeModal);
  document.getElementById('new-table-add-column').addEventListener('click', addNewTableColumnRow);
  document.getElementById('new-table-create').addEventListener('click', () => submitCreateTable(namespace));
  addNewTableColumnRow();
  document.getElementById('new-table-name').focus();
}

function addNewTableColumnRow() {
  const id = 'col-' + newTableColumnCount++;
  const row = document.createElement('div');
  row.className = 'new-table-column-row';
  row.id = id;
  row.innerHTML = `
    <input type="text" class="new-table-col-name" placeholder="column_name">
    <select class="new-table-col-type">
      <option value="VARCHAR">VARCHAR</option>
      <option value="BIGINT">BIGINT</option>
      <option value="INTEGER">INTEGER</option>
      <option value="DOUBLE">DOUBLE</option>
      <option value="BOOLEAN">BOOLEAN</option>
      <option value="DATE">DATE</option>
      <option value="TIMESTAMP">TIMESTAMP</option>
    </select>
    <label class="new-table-col-required"><input type="checkbox"> NOT NULL</label>
    <button type="button" class="new-table-col-remove">✕</button>
  `;
  row.querySelector('.new-table-col-remove').addEventListener('click', () => row.remove());
  document.getElementById('new-table-columns').appendChild(row);
}

async function submitCreateTable(namespace) {
  const errorEl = document.getElementById('new-table-error');
  const name = document.getElementById('new-table-name').value.trim();
  if (!name) {
    errorEl.textContent = 'Table name is required.';
    return;
  }
  const rows = Array.from(document.querySelectorAll('#new-table-columns .new-table-column-row'));
  if (rows.length === 0) {
    errorEl.textContent = 'At least one column is required.';
    return;
  }
  const columnDefs = [];
  for (const row of rows) {
    const colName = row.querySelector('.new-table-col-name').value.trim();
    const colType = row.querySelector('.new-table-col-type').value;
    const required = row.querySelector('.new-table-col-required input').checked;
    if (!colName) {
      errorEl.textContent = 'Every column needs a name.';
      return;
    }
    columnDefs.push(`${colName} ${colType}${required ? ' NOT NULL' : ''}`);
  }
  const sql = `CREATE TABLE lakehouse.${namespace}.${name} (${columnDefs.join(', ')})`;
  try {
    await apiFetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql }),
    });
  } catch (err) {
    if (err.message === 'not authenticated') return;
    errorEl.textContent = err.message;
    return;
  }
  closeModal();
  loadCatalog();
}

function confirmDeleteTable(namespace, table) {
  openModal(`
    <h3>Delete table "${namespace}.${table}"?</h3>
    <p>This cannot be undone.</p>
    <div class="modal-error" id="delete-table-error"></div>
    <div class="modal-actions">
      <button class="secondary" id="delete-table-cancel" type="button">Cancel</button>
      <button class="primary" id="delete-table-confirm" type="button">Delete</button>
    </div>
  `);
  document.getElementById('delete-table-cancel').addEventListener('click', closeModal);
  document.getElementById('delete-table-confirm').addEventListener('click', () => submitDeleteTable(namespace, table));
}

async function submitDeleteTable(namespace, table) {
  const errorEl = document.getElementById('delete-table-error');
  try {
    await apiFetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: `DROP TABLE lakehouse.${namespace}.${table}` }),
    });
  } catch (err) {
    if (err.message === 'not authenticated') return;
    errorEl.textContent = err.message;
    return;
  }
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 5: Replace `loadCatalog` in `app.js`**

Find the existing `loadCatalog` function (in the `// --- Catalog tree`
section) and replace it entirely with:

```javascript
async function loadCatalog() {
  const tree = document.getElementById('catalog-tree');
  tree.textContent = 'Loading...';
  let namespaces;
  try {
    const body = await apiFetch('/catalog/namespaces');
    namespaces = body.namespaces;
  } catch (err) {
    if (err.message === 'not authenticated') return;
    tree.textContent = 'Failed to load catalog: ' + err.message;
    return;
  }

  tree.innerHTML = '';
  namespaces.forEach((ns) => {
    const nsRow = document.createElement('div');
    nsRow.className = 'tree-namespace-row';

    const nsEl = document.createElement('div');
    nsEl.className = 'tree-namespace';
    nsEl.textContent = '▸ ' + ns;
    nsEl.style.flex = '1';

    const nsActions = document.createElement('div');
    nsActions.className = 'tree-row-actions';
    const addTableBtn = document.createElement('button');
    addTableBtn.type = 'button';
    addTableBtn.textContent = '+';
    addTableBtn.title = 'New table in ' + ns;
    addTableBtn.setAttribute('aria-label', 'New table in ' + ns);
    addTableBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openCreateTableModal(ns);
    });
    const deleteNsBtn = document.createElement('button');
    deleteNsBtn.type = 'button';
    deleteNsBtn.textContent = '🗑';
    deleteNsBtn.title = 'Delete dataset ' + ns;
    deleteNsBtn.setAttribute('aria-label', 'Delete dataset ' + ns);
    deleteNsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      confirmDeleteDataset(ns);
    });
    nsActions.appendChild(addTableBtn);
    nsActions.appendChild(deleteNsBtn);

    nsRow.appendChild(nsEl);
    nsRow.appendChild(nsActions);

    const tablesEl = document.createElement('div');
    tablesEl.className = 'tree-tables';
    tablesEl.hidden = true;
    let loaded = false;

    nsRow.addEventListener('click', async () => {
      tablesEl.hidden = !tablesEl.hidden;
      nsEl.textContent = (tablesEl.hidden ? '▸ ' : '▾ ') + ns;
      if (!loaded) {
        loaded = true;
        try {
          const tablesBody = await apiFetch('/catalog/tables/' + encodeURIComponent(ns));
          tablesBody.tables.forEach((t) => {
            const tRow = document.createElement('div');
            tRow.className = 'tree-table-row';

            const tEl = document.createElement('div');
            tEl.className = 'tree-table';
            tEl.textContent = t;
            tEl.style.flex = '1';
            tEl.addEventListener('click', (e) => {
              e.stopPropagation();
              editor.replaceSelection('lakehouse.' + ns + '.' + t);
              editor.focus();
            });

            const tActions = document.createElement('div');
            tActions.className = 'tree-row-actions';
            const deleteTBtn = document.createElement('button');
            deleteTBtn.type = 'button';
            deleteTBtn.textContent = '🗑';
            deleteTBtn.title = 'Delete table ' + t;
            deleteTBtn.setAttribute('aria-label', 'Delete table ' + t);
            deleteTBtn.addEventListener('click', (e) => {
              e.stopPropagation();
              confirmDeleteTable(ns, t);
            });
            tActions.appendChild(deleteTBtn);

            tRow.appendChild(tEl);
            tRow.appendChild(tActions);
            tablesEl.appendChild(tRow);
          });
        } catch (err) {
          loaded = false;
          if (err.message === 'not authenticated') return;
          tablesEl.textContent = 'Failed to load tables: ' + err.message;
        }
      }
    });

    tree.appendChild(nsRow);
    tree.appendChild(tablesEl);
  });
}
```
(This is intentionally the same structure as before, with `+`/`🗑`
tree-action buttons added — details (`ⓘ`) buttons come in Task 6, which
will replace this function again to add them; don't add them here.)

- [ ] **Step 6: Wire up the "+ Dataset" button in `init()`**

Find the `init()` function's existing
`document.getElementById('refresh-catalog').addEventListener(...)` line
and add right after it:
```javascript
  document.getElementById('new-dataset-btn').addEventListener('click', openCreateDatasetModal);
```

- [ ] **Step 7: Verify syntax**

```bash
cd C:/claude/lakehouse-ui
node --check app/static/app.js
```
Expected: no output, exit code 0.

- [ ] **Step 8: Run the full pytest suite (unaffected by frontend changes, confirms nothing broke)**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add app/static/index.html app/static/app.css app/static/app.js
git commit -m "feat: create/delete datasets and tables from the UI"
```

Do not push.

---

### Task 6: Resource details panels + save query results as a table

**Files (in `lakehouse-ui`):**
- Modify: `app/static/index.html`
- Modify: `app/static/app.css`
- Modify: `app/static/app.js`

- [ ] **Step 1: Branch (chain-merging Tasks 1-5)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/resource-details-ui
git merge --no-edit feat/table-namespace-details
git merge --no-edit feat/access-polaris-client
git merge --no-edit feat/details-routes
git merge --no-edit feat/access-route
git merge --no-edit feat/create-delete-ui
```
STOP and report BLOCKED on any conflict.

- [ ] **Step 2: Add a "Save as table" button to `index.html`**

Find `#toolbar` (containing `#run-btn` and `#query-status`) and add a
new button right after `#run-btn`:

```html
        <button id="save-as-table-btn" type="button">Save as table</button>
```

- [ ] **Step 3: Add details-modal and save-as-table CSS to `app.css`**

Append to the end of `app.css`:

```css
.detail-row { display: flex; justify-content: space-between; gap: 1rem; padding: 0.2rem 0; font-size: 0.85rem; }
.detail-label { color: #8a95a1; }
.detail-schema-table { width: 100%; border-collapse: collapse; margin-top: 0.8rem; font-size: 0.8rem; }
.detail-schema-table th, .detail-schema-table td { border: 1px solid #2a333d; padding: 0.25rem 0.5rem; text-align: left; }
#save-as-table-btn {
  background: none;
  border: 1px solid #2a333d;
  color: #8a95a1;
  padding: 0.35rem 0.9rem;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.85rem;
}
```

- [ ] **Step 4: Replace `loadCatalog` again, this time adding the details (ⓘ) buttons**

Find the `loadCatalog` function Task 5 added and replace it entirely
with this version (identical to Task 5's, plus a `ⓘ` details button on
both the namespace row and the table row):

```javascript
async function loadCatalog() {
  const tree = document.getElementById('catalog-tree');
  tree.textContent = 'Loading...';
  let namespaces;
  try {
    const body = await apiFetch('/catalog/namespaces');
    namespaces = body.namespaces;
  } catch (err) {
    if (err.message === 'not authenticated') return;
    tree.textContent = 'Failed to load catalog: ' + err.message;
    return;
  }

  tree.innerHTML = '';
  namespaces.forEach((ns) => {
    const nsRow = document.createElement('div');
    nsRow.className = 'tree-namespace-row';

    const nsEl = document.createElement('div');
    nsEl.className = 'tree-namespace';
    nsEl.textContent = '▸ ' + ns;
    nsEl.style.flex = '1';

    const nsActions = document.createElement('div');
    nsActions.className = 'tree-row-actions';
    const nsDetailsBtn = document.createElement('button');
    nsDetailsBtn.type = 'button';
    nsDetailsBtn.textContent = 'ⓘ';
    nsDetailsBtn.title = 'Details for ' + ns;
    nsDetailsBtn.setAttribute('aria-label', 'Details for ' + ns);
    nsDetailsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openNamespaceDetailsModal(ns);
    });
    const addTableBtn = document.createElement('button');
    addTableBtn.type = 'button';
    addTableBtn.textContent = '+';
    addTableBtn.title = 'New table in ' + ns;
    addTableBtn.setAttribute('aria-label', 'New table in ' + ns);
    addTableBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openCreateTableModal(ns);
    });
    const deleteNsBtn = document.createElement('button');
    deleteNsBtn.type = 'button';
    deleteNsBtn.textContent = '🗑';
    deleteNsBtn.title = 'Delete dataset ' + ns;
    deleteNsBtn.setAttribute('aria-label', 'Delete dataset ' + ns);
    deleteNsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      confirmDeleteDataset(ns);
    });
    nsActions.appendChild(nsDetailsBtn);
    nsActions.appendChild(addTableBtn);
    nsActions.appendChild(deleteNsBtn);

    nsRow.appendChild(nsEl);
    nsRow.appendChild(nsActions);

    const tablesEl = document.createElement('div');
    tablesEl.className = 'tree-tables';
    tablesEl.hidden = true;
    let loaded = false;

    nsRow.addEventListener('click', async () => {
      tablesEl.hidden = !tablesEl.hidden;
      nsEl.textContent = (tablesEl.hidden ? '▸ ' : '▾ ') + ns;
      if (!loaded) {
        loaded = true;
        try {
          const tablesBody = await apiFetch('/catalog/tables/' + encodeURIComponent(ns));
          tablesBody.tables.forEach((t) => {
            const tRow = document.createElement('div');
            tRow.className = 'tree-table-row';

            const tEl = document.createElement('div');
            tEl.className = 'tree-table';
            tEl.textContent = t;
            tEl.style.flex = '1';
            tEl.addEventListener('click', (e) => {
              e.stopPropagation();
              editor.replaceSelection('lakehouse.' + ns + '.' + t);
              editor.focus();
            });

            const tActions = document.createElement('div');
            tActions.className = 'tree-row-actions';
            const detailsBtn = document.createElement('button');
            detailsBtn.type = 'button';
            detailsBtn.textContent = 'ⓘ';
            detailsBtn.title = 'Details for ' + t;
            detailsBtn.setAttribute('aria-label', 'Details for ' + t);
            detailsBtn.addEventListener('click', (e) => {
              e.stopPropagation();
              openTableDetailsModal(ns, t);
            });
            const deleteTBtn = document.createElement('button');
            deleteTBtn.type = 'button';
            deleteTBtn.textContent = '🗑';
            deleteTBtn.title = 'Delete table ' + t;
            deleteTBtn.setAttribute('aria-label', 'Delete table ' + t);
            deleteTBtn.addEventListener('click', (e) => {
              e.stopPropagation();
              confirmDeleteTable(ns, t);
            });
            tActions.appendChild(detailsBtn);
            tActions.appendChild(deleteTBtn);

            tRow.appendChild(tEl);
            tRow.appendChild(tActions);
            tablesEl.appendChild(tRow);
          });
        } catch (err) {
          loaded = false;
          if (err.message === 'not authenticated') return;
          tablesEl.textContent = 'Failed to load tables: ' + err.message;
        }
      }
    });

    tree.appendChild(nsRow);
    tree.appendChild(tablesEl);
  });
}
```

- [ ] **Step 5: Add the details-modal and save-as-table functions to `app.js`**

Add these functions right after `submitDeleteTable` (from Task 5):

```javascript
async function openTableDetailsModal(namespace, table) {
  openModal('<h3>' + namespace + '.' + table + '</h3><p>Loading…</p>');
  let details;
  try {
    details = await apiFetch(
      '/catalog/tables/' + encodeURIComponent(namespace) + '/' + encodeURIComponent(table) + '/details'
    );
  } catch (err) {
    if (err.message === 'not authenticated') return;
    openModal(
      '<h3>' + namespace + '.' + table + '</h3><p class="modal-error">' + err.message +
      '</p><div class="modal-actions"><button class="secondary" id="details-close" type="button">Close</button></div>'
    );
    document.getElementById('details-close').addEventListener('click', closeModal);
    return;
  }
  const snap = details.current_snapshot;
  const rowsHtml = `
    <div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">${details.location || '—'}</span></div>
    <div class="detail-row"><span class="detail-label">Last updated</span><span class="detail-value">${details.last_updated_ms ? new Date(details.last_updated_ms).toISOString() : '—'}</span></div>
    <div class="detail-row"><span class="detail-label">Rows</span><span class="detail-value">${snap ? Number(snap.total_records).toLocaleString() : '—'}</span></div>
    <div class="detail-row"><span class="detail-label">Data files</span><span class="detail-value">${snap ? snap.total_data_files : '—'}</span></div>
  `;
  const schemaRows = details.fields
    .map((f) => `<tr><td>${f.name}</td><td>${f.type}</td><td>${f.required ? 'NOT NULL' : ''}</td></tr>`)
    .join('');
  openModal(`
    <h3>${namespace}.${table}</h3>
    ${rowsHtml}
    <table class="detail-schema-table"><thead><tr><th>Column</th><th>Type</th><th></th></tr></thead><tbody>${schemaRows}</tbody></table>
    <div class="modal-actions"><button class="secondary" id="details-close" type="button">Close</button></div>
  `);
  document.getElementById('details-close').addEventListener('click', closeModal);
}

async function openNamespaceDetailsModal(namespace) {
  openModal('<h3>' + namespace + '</h3><p>Loading…</p>');
  let details;
  try {
    details = await apiFetch('/catalog/namespaces/' + encodeURIComponent(namespace) + '/details');
  } catch (err) {
    if (err.message === 'not authenticated') return;
    openModal(
      '<h3>' + namespace + '</h3><p class="modal-error">' + err.message +
      '</p><div class="modal-actions"><button class="secondary" id="ns-details-close" type="button">Close</button></div>'
    );
    document.getElementById('ns-details-close').addEventListener('click', closeModal);
    return;
  }
  openModal(`
    <h3>${namespace}</h3>
    <div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">${(details.properties && details.properties.location) || '—'}</span></div>
    <div class="detail-row"><span class="detail-label">Tables</span><span class="detail-value">${details.table_count}</span></div>
    <div class="modal-actions"><button class="secondary" id="ns-details-close" type="button">Close</button></div>
  `);
  document.getElementById('ns-details-close').addEventListener('click', closeModal);
}

function openSaveAsTableModal() {
  const worksheet = worksheets.find((w) => w.id === activeWorksheetId);
  if (!worksheet || !worksheet.columns.length) return;
  openModal(`
    <h3>Save results as table</h3>
    <label for="save-table-namespace">Dataset</label>
    <input type="text" id="save-table-namespace" autocomplete="off" placeholder="nyc_taxi">
    <label for="save-table-name">Table name</label>
    <input type="text" id="save-table-name" autocomplete="off">
    <div class="modal-error" id="save-table-error"></div>
    <div class="modal-actions">
      <button class="secondary" id="save-table-cancel" type="button">Cancel</button>
      <button class="primary" id="save-table-create" type="button">Save</button>
    </div>
  `);
  document.getElementById('save-table-cancel').addEventListener('click', closeModal);
  document.getElementById('save-table-create').addEventListener('click', submitSaveAsTable);
  document.getElementById('save-table-namespace').focus();
}

async function submitSaveAsTable() {
  const worksheet = worksheets.find((w) => w.id === activeWorksheetId);
  const errorEl = document.getElementById('save-table-error');
  const namespace = document.getElementById('save-table-namespace').value.trim();
  const name = document.getElementById('save-table-name').value.trim();
  if (!namespace || !name) {
    errorEl.textContent = 'Dataset and table name are both required.';
    return;
  }
  const originalSql = worksheet.sql.trim();
  const sql = `CREATE TABLE lakehouse.${namespace}.${name} AS ${originalSql}`;
  try {
    await apiFetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql }),
    });
  } catch (err) {
    if (err.message === 'not authenticated') return;
    errorEl.textContent = err.message;
    return;
  }
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 6: Wire up the "Save as table" button in `init()`**

Add right after the existing
`document.getElementById('run-btn').addEventListener(...)` line:
```javascript
  document.getElementById('save-as-table-btn').addEventListener('click', openSaveAsTableModal);
```

- [ ] **Step 7: Verify syntax**

```bash
node --check app/static/app.js
```
Expected: no output, exit code 0.

- [ ] **Step 8: Run the full pytest suite**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add app/static/index.html app/static/app.css app/static/app.js
git commit -m "feat: resource details panels and save-query-as-table"
```

Do not push.

---

### Task 7: Access tab

**Files (in `lakehouse-ui`):**
- Modify: `app/static/index.html`
- Modify: `app/static/app.css`
- Modify: `app/static/app.js`

- [ ] **Step 1: Branch (chain-merging Tasks 1-6)**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/access-tab-ui
git merge --no-edit feat/table-namespace-details
git merge --no-edit feat/access-polaris-client
git merge --no-edit feat/details-routes
git merge --no-edit feat/access-route
git merge --no-edit feat/create-delete-ui
git merge --no-edit feat/resource-details-ui
```
STOP and report BLOCKED on any conflict.

- [ ] **Step 2: Add the Access tab button and panel to `index.html`**

Find `#sidebar-header` and add a third tab button right after the
existing `#tab-history` button (before `#refresh-catalog`):
```html
        <button id="tab-access" class="sidebar-tab" type="button">Access</button>
```
Find `#history-list` (`<div id="history-list" class="sidebar-panel" hidden></div>`)
and add a new panel div right after it:
```html
      <div id="access-panel" class="sidebar-panel" hidden></div>
```

- [ ] **Step 3: Add Access-tab CSS to `app.css`**

Append to the end of `app.css`:

```css
.access-principal { margin-bottom: 0.8rem; }
.access-principal-name { font-weight: 600; margin-bottom: 0.2rem; }
.access-principal-role { padding-left: 0.8rem; color: #8fb8e8; font-size: 0.85rem; }
.access-catalog-role { padding-left: 1.6rem; color: #8a95a1; font-size: 0.8rem; }
```

- [ ] **Step 4: Extend `showSidebarPanel` in `app.js`**

Find the existing `showSidebarPanel` function and replace it entirely
with:

```javascript
function showSidebarPanel(panel) {
  document.getElementById('catalog-tree').hidden = panel !== 'catalog';
  document.getElementById('history-list').hidden = panel !== 'history';
  document.getElementById('access-panel').hidden = panel !== 'access';
  document.getElementById('tab-catalog').classList.toggle('active', panel === 'catalog');
  document.getElementById('tab-history').classList.toggle('active', panel === 'history');
  document.getElementById('tab-access').classList.toggle('active', panel === 'access');
  if (panel === 'history') loadHistory();
  if (panel === 'access') loadAccess();
}
```

- [ ] **Step 5: Add `loadAccess` to `app.js`**

Add this function right after the existing `loadHistory` function (in
the `// --- History` section, before the `// --- Sidebar panel
switching` comment):

```javascript
async function loadAccess() {
  const panel = document.getElementById('access-panel');
  panel.textContent = 'Loading...';
  let principals;
  try {
    const body = await apiFetch('/access');
    principals = body.principals;
  } catch (err) {
    if (err.message === 'not authenticated') return;
    panel.textContent = 'Failed to load access: ' + err.message;
    return;
  }

  panel.innerHTML = '';
  principals.forEach((p) => {
    const pEl = document.createElement('div');
    pEl.className = 'access-principal';
    const pName = document.createElement('div');
    pName.className = 'access-principal-name';
    pName.textContent = p.name;
    pEl.appendChild(pName);

    p.principal_roles.forEach((pr) => {
      const prEl = document.createElement('div');
      prEl.className = 'access-principal-role';
      prEl.textContent = pr.name;
      pEl.appendChild(prEl);

      pr.catalog_roles.forEach((cr) => {
        const crEl = document.createElement('div');
        crEl.className = 'access-catalog-role';
        crEl.textContent = cr.name + ': ' + cr.grants.join(', ');
        pEl.appendChild(crEl);
      });
    });

    panel.appendChild(pEl);
  });
}
```

- [ ] **Step 6: Wire up the Access tab button in `init()`**

Add right after the existing
`document.getElementById('tab-history').addEventListener(...)` line:
```javascript
  document.getElementById('tab-access').addEventListener('click', () => showSidebarPanel('access'));
```

- [ ] **Step 7: Verify syntax**

```bash
node --check app/static/app.js
```
Expected: no output, exit code 0.

- [ ] **Step 8: Run the full pytest suite one more time**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing.

- [ ] **Step 9: Commit and push**

```bash
git add app/static/index.html app/static/app.css app/static/app.js
git commit -m "feat: add the view-only Access tab"
git push
```

This is the task in this repo's sequence that pushes — matching every
prior phase's pattern (push once at the end so CI runs on the
accumulated work).

- [ ] **Step 10: Verify CI is green**

```bash
gh run list --branch main --limit 1
```
If red, `gh run view <id> --log-failed`, diagnose, fix, commit, push,
recheck. Don't leave this repo with failing CI.

---

### Task 8 (MAIN SESSION — interactive, not a subagent): Merge and deploy

- [ ] Merge Tasks 1-6's branches into `lakehouse-ui`'s `main` in order
  (Task 7 already pushed directly and includes everything via its chain
  merges — check whether Tasks 1-6's branches are already fully
  contained in what Task 7 pushed; if so, this step may just be
  confirming `main` already has everything rather than merging more PRs).
- [ ] Confirm `pytest tests/ -v` is fully green on `main`.
- [ ] Confirm CI on `main` is green; get the new image tag
  (`gh run view <id> --log | grep "pushing manifest"`).
- [ ] Update `environments/local/values/lakehouse-ui.yaml` in
  `test-k8s-configs` with the new image tag, commit, push, open a PR,
  merge (confirm with the user before merging, per established practice
  this session).
- [ ] Resync `lakehouse-ui` (`kubectl annotate application lakehouse-ui -n
  argocd argocd.argoproj.io/refresh=hard --overwrite`), watch the
  rollout complete — this is a live deploy, watch it happen.

---

### Task 9 (MAIN SESSION — interactive, not a subagent): Verify against the design doc's Definition of Done

- [ ] Create a dataset through the UI; confirm it appears in the catalog
  tree and via a direct Iceberg REST `namespaces` list.
- [ ] Create a table through the guided column editor; confirm its
  schema matches what was entered.
- [ ] Run a query, use "Save as table"; confirm the new table's row
  count matches the original query's result count.
- [ ] View details for `nyc_taxi.trips`: confirm location, row count,
  and file count all match a direct Iceberg REST call to the same table.
- [ ] View details for the `nyc_taxi` namespace: confirm location and
  table count.
- [ ] Delete a table and a dataset created during testing; confirm both
  gone from the tree and from a direct Iceberg REST list.
- [ ] Log in as `lakehouse-ui` (read-only role), attempt to create a
  dataset: confirm it fails with a Polaris 403 surfaced as a query
  error, not a crash.
- [ ] View the Access tab: confirm it shows all 3 known principals, each
  with their real principal role → catalog role → grant chain, matching
  a direct root-credentialed Management API query of the same data.

---

### Task 10 (MAIN SESSION — interactive, not a subagent): Append verification results to the design doc

- [ ] Append a "Verification results" section to
  `docs/superpowers/specs/2026-09-14-lakehouse-ui-warehouse-resource-management-design.md`
  (same pattern as every prior phase this session) — what worked as
  designed, anything that needed a live fix along the way.
- [ ] Commit and push:
  ```bash
  git add docs/superpowers/specs/2026-09-14-lakehouse-ui-warehouse-resource-management-design.md
  git commit -m "docs: append verification results to the warehouse resource management design doc"
  git push
  ```
