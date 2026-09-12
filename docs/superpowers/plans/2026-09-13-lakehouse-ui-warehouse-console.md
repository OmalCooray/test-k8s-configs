# lakehouse-ui Warehouse Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement Tasks 1–17. Tasks 18–19 touch the live cluster and must be
> driven from the main session (interactive, watched, fixed live) — do not
> dispatch them to a subagent. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** Grow `lakehouse-ui` from a single SQL box into a small
Snowflake/BigQuery-style console: per-principal login backed by Polaris's
own RBAC, a catalog browser, multiple query worksheets, query history, and
a syntax-highlighted editor.

**Architecture:** Two repos, as before. `lakehouse-ui` gets a session/auth
layer (login exchanges a Polaris principal's credentials for identity, no
separate user system), a `polaris_client` module for the Iceberg Catalog +
Management REST APIs, a Postgres-backed query-history store, and a
frontend rewrite (login page, sidebar catalog/history, worksheet tabs,
CodeMirror 5 editor). `test-k8s-configs` gets a second database on the
existing `polaris-postgres` instance and chart/env changes for
`lakehouse-ui` — no new infrastructure.

**Tech stack:** FastAPI + DuckDB (unchanged) + `psycopg[binary]` (new, for
query history) in `lakehouse-ui`; CodeMirror 5 via CDN for the editor; the
existing `polaris-postgres` chart (groundhog2k/postgres) gets a second
database.

**Design doc:** `docs/superpowers/specs/2026-09-13-lakehouse-ui-warehouse-console-design.md`

---

## Task 1: Session store (`app/session.py`)

**Files (in `lakehouse-ui`):**
- Create: `app/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_session.py`:
```python
from app.session import create_session, delete_session, get_session


def test_create_session_returns_an_id_that_get_session_resolves():
    session_id = create_session("cid", "secret", "loader")

    session = get_session(session_id)

    assert session is not None
    assert session.client_id == "cid"
    assert session.client_secret == "secret"
    assert session.principal_name == "loader"


def test_get_session_returns_none_for_unknown_id():
    assert get_session("does-not-exist") is None


def test_get_session_returns_none_for_none():
    assert get_session(None) is None


def test_two_sessions_get_different_ids():
    first = create_session("cid1", "secret1", "loader")
    second = create_session("cid2", "secret2", "lakehouse-ui")

    assert first != second
    assert get_session(first).principal_name == "loader"
    assert get_session(second).principal_name == "lakehouse-ui"


def test_delete_session_removes_it():
    session_id = create_session("cid", "secret", "loader")

    delete_session(session_id)

    assert get_session(session_id) is None


def test_delete_session_on_unknown_id_does_not_raise():
    delete_session("does-not-exist")
    delete_session(None)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_session.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.session'`.

- [ ] **Step 3: Write minimal implementation**

`app/session.py`:
```python
"""In-memory session store for logged-in Polaris principals.

Sessions are keyed by a random session ID (the value of the session
cookie) and hold the principal's own Polaris credentials plus its name.
Deliberately in-memory (not Postgres/Redis) — acceptable at `replicas: 1`
(this chart's existing setup); a pod restart logs everyone out, a fine
trade for the simplicity.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Session:
    client_id: str
    client_secret: str
    principal_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_sessions: dict[str, Session] = {}


def create_session(client_id: str, client_secret: str, principal_name: str) -> str:
    """Create a session, returning its ID (the cookie value)."""
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = Session(
        client_id=client_id,
        client_secret=client_secret,
        principal_name=principal_name,
    )
    return session_id


def get_session(session_id: str | None) -> Session | None:
    if session_id is None:
        return None
    return _sessions.get(session_id)


def delete_session(session_id: str | None) -> None:
    if session_id is not None:
        _sessions.pop(session_id, None)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_session.py -v
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/session.py tests/test_session.py
git commit -m "feat: add in-memory session store"
```

---

## Task 2: Polaris login (`app/polaris_auth.py`)

**Files (in `lakehouse-ui`):**
- Create: `app/polaris_auth.py`
- Test: `tests/test_polaris_auth.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_polaris_auth.py`:
```python
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from app.polaris_auth import LoginError, login

# A real Polaris token has 3 dot-separated base64url segments; only the
# payload (middle) segment matters here. These were generated the same way
# and decode to {"sub": "loader"} / {"foo": "bar"} respectively — verified
# against app.polaris_auth's own decode logic before being pasted in here.
TOKEN_WITH_SUB = "eyJhbGciOiAiUlMyNTYiLCAidHlwIjogIkpXVCJ9.eyJzdWIiOiAibG9hZGVyIn0.sig"
TOKEN_WITHOUT_SUB = "eyJhbGciOiAiUlMyNTYiLCAidHlwIjogIkpXVCJ9.eyJmb28iOiAiYmFyIn0.sig"


def _fake_response(body: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode()
    response.__enter__.return_value = response
    return response


def test_login_returns_principal_name_from_sub_claim(monkeypatch):
    fake = _fake_response({"access_token": TOKEN_WITH_SUB})
    monkeypatch.setattr(
        "app.polaris_auth.urllib.request.urlopen", lambda req, timeout=10: fake
    )

    principal = login("http://polaris:8181/api/catalog", "cid", "secret")

    assert principal == "loader"


def test_login_raises_on_http_error(monkeypatch):
    def raise_http_error(req, timeout=10):
        raise urllib.error.HTTPError("url", 401, "unauthorized", {}, None)

    monkeypatch.setattr("app.polaris_auth.urllib.request.urlopen", raise_http_error)

    with pytest.raises(LoginError, match="invalid client_id or client_secret"):
        login("http://polaris:8181/api/catalog", "cid", "wrong")


def test_login_raises_when_polaris_unreachable(monkeypatch):
    def raise_url_error(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("app.polaris_auth.urllib.request.urlopen", raise_url_error)

    with pytest.raises(LoginError, match="could not reach Polaris"):
        login("http://polaris:8181/api/catalog", "cid", "secret")


def test_login_raises_when_no_access_token_in_response(monkeypatch):
    fake = _fake_response({"error": "nope"})
    monkeypatch.setattr(
        "app.polaris_auth.urllib.request.urlopen", lambda req, timeout=10: fake
    )

    with pytest.raises(LoginError, match="access_token"):
        login("http://polaris:8181/api/catalog", "cid", "secret")


def test_login_raises_when_token_has_no_sub_claim(monkeypatch):
    fake = _fake_response({"access_token": TOKEN_WITHOUT_SUB})
    monkeypatch.setattr(
        "app.polaris_auth.urllib.request.urlopen", lambda req, timeout=10: fake
    )

    with pytest.raises(LoginError, match="sub"):
        login("http://polaris:8181/api/catalog", "cid", "secret")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_polaris_auth.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.polaris_auth'`.

- [ ] **Step 3: Write minimal implementation**

`app/polaris_auth.py`:
```python
"""Exchange a Polaris principal's credentials for an access token, and
decode the principal name out of it — used only at login time.

Confirmed live against a real Polaris deployment: the token's JWT payload
carries `sub` (the principal name), `principalId`, `client_id`, `scope` —
no role list. Signature verification is deliberately skipped: we received
this token directly from Polaris over the connection we're about to use
it on, not a token presented by a third party, so there's nothing to
verify against.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class LoginError(RuntimeError):
    """Raised when the given credentials are rejected by Polaris, or the
    response can't be used to identify the principal."""


def _decode_jwt_payload(token: str) -> dict:
    payload_segment = token.split(".")[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def login(polaris_endpoint: str, client_id: str, client_secret: str) -> str:
    """Exchange credentials for a token, return the principal name (the
    token's `sub` claim). Raises LoginError on invalid credentials, an
    unreachable Polaris, or a response that can't be used to identify the
    principal.
    """
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "PRINCIPAL_ROLE:ALL",
        }
    ).encode()
    request = urllib.request.Request(
        f"{polaris_endpoint}/v1/oauth/tokens",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LoginError("invalid client_id or client_secret") from exc
    except urllib.error.URLError as exc:
        raise LoginError(f"could not reach Polaris: {exc}") from exc

    access_token = body.get("access_token")
    if not access_token:
        raise LoginError("Polaris did not return an access_token")

    claims = _decode_jwt_payload(access_token)
    principal_name = claims.get("sub")
    if not principal_name:
        raise LoginError("token had no 'sub' claim")
    return principal_name
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_polaris_auth.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/polaris_auth.py tests/test_polaris_auth.py
git commit -m "feat: exchange Polaris credentials for identity at login"
```

---

## Task 3: Login/logout endpoints + auth dependency

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Test: `tests/test_auth_routes.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_auth_routes.py`:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_auth import LoginError
from app.session import get_session

client = TestClient(app)


def test_login_success_sets_cookie_and_returns_principal(monkeypatch):
    monkeypatch.setattr(main_module, "polaris_login", lambda endpoint, cid, secret: "loader")

    response = client.post("/login", json={"client_id": "cid", "client_secret": "secret"})

    assert response.status_code == 200
    assert response.json() == {"principal": "loader"}
    cookie = response.cookies.get("lakehouse_session")
    assert cookie is not None
    assert get_session(cookie).principal_name == "loader"


def test_login_failure_returns_401_and_sets_no_cookie(monkeypatch):
    def raise_login_error(endpoint, cid, secret):
        raise LoginError("invalid client_id or client_secret")

    monkeypatch.setattr(main_module, "polaris_login", raise_login_error)

    response = client.post("/login", json={"client_id": "cid", "client_secret": "wrong"})

    assert response.status_code == 401
    assert "invalid" in response.json()["detail"]
    assert response.cookies.get("lakehouse_session") is None


def test_logout_clears_the_session(monkeypatch):
    monkeypatch.setattr(main_module, "polaris_login", lambda endpoint, cid, secret: "loader")
    login_response = client.post("/login", json={"client_id": "cid", "client_secret": "secret"})
    cookie = login_response.cookies.get("lakehouse_session")

    response = client.post("/logout", cookies={"lakehouse_session": cookie})

    assert response.status_code == 200
    assert get_session(cookie) is None


def test_query_requires_a_session():
    response = client.post("/query", json={"sql": "SELECT 1"})
    assert response.status_code == 401


def test_index_redirects_to_login_when_not_authenticated():
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/login"


def test_index_serves_the_app_when_authenticated(monkeypatch):
    monkeypatch.setattr(main_module, "polaris_login", lambda endpoint, cid, secret: "loader")
    login_response = client.post("/login", json={"client_id": "cid", "client_secret": "secret"})
    cookie = login_response.cookies.get("lakehouse_session")

    response = client.get("/", cookies={"lakehouse_session": cookie}, follow_redirects=False)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_login_page_route_serves_html():
    response = client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_auth_routes.py -v
```
Expected: FAIL — none of `/login`, `/logout`, `/login` (GET) exist yet (404s);
`test_query_requires_a_session` fails because `/query` doesn't require auth
yet; `test_index_redirects...` fails because `/` doesn't check auth yet.

- [ ] **Step 3: Write the implementation**

This step only adds login/logout/auth-gating — it does NOT yet touch what
`/query` does internally (that's Task 5). For now, `/query` just needs to
require a valid session (via the new `require_session` dependency) — its
body stays exactly as it is today, just with the dependency added.

Add to `app/main.py`, near the top (after the existing imports):
```python
from fastapi import Cookie, Depends, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.polaris_auth import LoginError
from app.polaris_auth import login as polaris_login
from app.session import Session, create_session, delete_session, get_session
```

Add a static files mount right after `STATIC_DIR = Path(__file__).parent / "static"`:
```python
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
```

Add new request/response models near `QueryResponse`:
```python
class LoginRequest(BaseModel):
    client_id: str
    client_secret: str


class LoginResponse(BaseModel):
    principal: str
```

Add the auth dependency (place it near the top-level functions, after
`_is_single_statement`):
```python
def require_session(
    lakehouse_session: str | None = Cookie(default=None),
) -> Session:
    session = get_session(lakehouse_session)
    if session is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return session
```

Replace the existing `index()` route with an auth-gated version, and add
the `/login` page route right after it:
```python
@app.get("/")
def index(lakehouse_session: str | None = Cookie(default=None)):
    if get_session(lakehouse_session) is None:
        return RedirectResponse(url="/login")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")
```

Add the login/logout routes (place them after `healthz`, before `/query`):
```python
@app.post("/login", response_model=LoginResponse)
def login_route(request: LoginRequest, response: Response) -> LoginResponse:
    polaris_endpoint = os.environ.get("POLARIS_ENDPOINT")
    if not polaris_endpoint:
        raise HTTPException(
            status_code=500, detail="server missing POLARIS_ENDPOINT configuration"
        )
    try:
        principal_name = polaris_login(
            polaris_endpoint, request.client_id, request.client_secret
        )
    except LoginError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    session_id = create_session(request.client_id, request.client_secret, principal_name)
    response.set_cookie(
        key="lakehouse_session",
        value=session_id,
        httponly=True,
        samesite="lax",
    )
    return LoginResponse(principal=principal_name)


@app.post("/logout")
def logout_route(
    response: Response, lakehouse_session: str | None = Cookie(default=None)
) -> dict:
    delete_session(lakehouse_session)
    response.delete_cookie("lakehouse_session")
    return {"status": "ok"}
```

Add `import os` near the top of the file if it isn't already imported (it
isn't yet — `app/main.py` currently only imports `pathlib.Path`, FastAPI
pieces, and `app.catalog`).

Finally, add the `require_session` dependency to `/query`'s signature —
change:
```python
def run_query(request: QueryRequest) -> QueryResponse:
```
to:
```python
def run_query(
    request: QueryRequest, session: Session = Depends(require_session)
) -> QueryResponse:
```
(The `session` parameter isn't used inside the function body yet — that's
Task 5. This step's only job is making `/query` require authentication.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS — all tests, including the pre-existing ones (some of which
will need small adjustments if they call `/query` or `/` without a session;
if `tests/test_query.py`'s existing tests now fail with 401, that's
expected and is what Task 5 fixes — for THIS task, only make
`tests/test_auth_routes.py` and `tests/test_session.py`/
`tests/test_polaris_auth.py` pass; if pre-existing tests in
`tests/test_query.py` or `tests/test_index.py` newly fail because of the
auth requirement, leave them failing and report this clearly — Task 5 and
Task 13 fix those specifically, don't try to fix them here.)

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_auth_routes.py
git commit -m "feat: add login/logout, gate / and /query behind a session"
```

## Note for the implementer of Task 3

`tests/test_query.py` and `tests/test_index.py` (from before this feature)
will now have failing tests, since they call `/query` and `/` without a
session cookie. This is expected and intentional — do not modify those
files in this task. Report it in your DONE summary so the controller knows
which specific tests are expected to be red until Task 5 (query.py session
wiring) and Task 13 (frontend + index test rewrite) land.

---

## Task 4: `catalog.py` — parameterize `build_connection`

**Files (in `lakehouse-ui`):**
- Modify: `app/catalog.py`
- Modify: `tests/test_catalog.py`

**Context:** `build_connection()` currently reads `POLARIS_CLIENT_ID`/
`POLARIS_CLIENT_SECRET` from the environment. Per-user login means those
values now come from the caller's session, not a fixed env var. This task
changes the function's signature to take them as parameters, keeping
`POLARIS_ENDPOINT`/`POLARIS_CATALOG` as env vars (still fixed, not
per-user).

- [ ] **Step 1: Update the tests first (still red until Step 3)**

Replace `tests/test_catalog.py`'s `REQUIRED_ENV` fixture and every test
that references `POLARIS_CLIENT_ID`/`POLARIS_CLIENT_SECRET` env vars. The
new signature is `build_connection(client_id, client_secret, conn=None)` —
`POLARIS_ENDPOINT`/`POLARIS_CATALOG` stay as env vars.

Full replacement for `tests/test_catalog.py`:
```python
import sys
import types

import pytest

from app.catalog import CatalogConfigError, build_connection


class FakeConnection:
    def __init__(self):
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        return self


REQUIRED_ENV = {
    "POLARIS_ENDPOINT": "http://polaris.lakehouse.svc.cluster.local:8181/api/catalog",
    "POLARIS_CATALOG": "lakehouse",
}


def _set_env(monkeypatch, **overrides):
    values = {**REQUIRED_ENV, **overrides}
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_build_connection_requires_polaris_endpoint(monkeypatch):
    _set_env(monkeypatch, POLARIS_ENDPOINT=None)
    with pytest.raises(CatalogConfigError, match="POLARIS_ENDPOINT"):
        build_connection("lakehouse-ui", "s3cr3t", conn=FakeConnection())


def test_build_connection_requires_catalog(monkeypatch):
    _set_env(monkeypatch, POLARIS_CATALOG=None)
    with pytest.raises(CatalogConfigError, match="POLARIS_CATALOG"):
        build_connection("lakehouse-ui", "s3cr3t", conn=FakeConnection())


def test_build_connection_installs_extensions_and_attaches(monkeypatch):
    _set_env(monkeypatch)
    fake = FakeConnection()

    result = build_connection("lakehouse-ui", "s3cr3t", conn=fake)

    assert result is fake
    assert fake.executed == [
        "INSTALL iceberg",
        "LOAD iceberg",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CREATE OR REPLACE SECRET polaris_secret ("
        "TYPE iceberg, "
        "CLIENT_ID 'lakehouse-ui', "
        "CLIENT_SECRET 's3cr3t', "
        "ENDPOINT 'http://polaris.lakehouse.svc.cluster.local:8181/api/catalog'"
        ")",
        "ATTACH 'lakehouse' AS \"lakehouse\" ("
        "TYPE iceberg, "
        "ENDPOINT 'http://polaris.lakehouse.svc.cluster.local:8181/api/catalog', "
        "ACCESS_DELEGATION_MODE 'vended_credentials'"
        ")",
    ]


def test_build_connection_escapes_single_quotes_in_secret(monkeypatch):
    _set_env(monkeypatch)
    fake = FakeConnection()

    build_connection("lakehouse-ui", "o'brien", conn=fake)

    assert "CLIENT_SECRET 'o''brien'" in "\n".join(fake.executed)


def test_build_connection_opens_in_memory_duckdb_when_no_conn_given(monkeypatch):
    _set_env(monkeypatch)
    fake = FakeConnection()
    calls = []

    def fake_connect(path):
        calls.append(path)
        return fake

    fake_duckdb_module = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "duckdb", fake_duckdb_module)

    result = build_connection("lakehouse-ui", "s3cr3t")

    assert result is fake
    assert calls == [":memory:"]
    assert "INSTALL iceberg" in "\n".join(fake.executed)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_catalog.py -v
```
Expected: FAIL — `build_connection()` still takes `conn` as its only
parameter, so calls like `build_connection("lakehouse-ui", "s3cr3t", conn=FakeConnection())`
raise a `TypeError`.

- [ ] **Step 3: Update the implementation**

Replace `app/catalog.py`'s `_REQUIRED_ENV_VARS` and `build_connection` with:
```python
_REQUIRED_ENV_VARS = (
    "POLARIS_ENDPOINT",
    "POLARIS_CATALOG",
)


def build_connection(
    client_id: str, client_secret: str, conn: ExecutableConnection | None = None
) -> ExecutableConnection:
    """Return a DuckDB connection with the Polaris catalog attached, using
    the given principal's credentials.

    Pass an existing `conn` to attach onto it instead of opening a fresh
    in-memory DuckDB connection — this is what makes the attach logic
    testable with a stub in place of real DuckDB/network calls.
    """
    env = {name: _require_env(name) for name in _REQUIRED_ENV_VARS}
    endpoint = env["POLARIS_ENDPOINT"]
    catalog = env["POLARIS_CATALOG"]

    if conn is None:
        import duckdb

        conn = duckdb.connect(":memory:")

    conn.execute("INSTALL iceberg")
    conn.execute("LOAD iceberg")
    conn.execute("INSTALL httpfs")
    conn.execute("LOAD httpfs")
    conn.execute(
        "CREATE OR REPLACE SECRET polaris_secret ("
        "TYPE iceberg, "
        f"CLIENT_ID '{_sql_quote(client_id)}', "
        f"CLIENT_SECRET '{_sql_quote(client_secret)}', "
        f"ENDPOINT '{_sql_quote(endpoint)}'"
        ")"
    )
    conn.execute(
        f"ATTACH '{_sql_quote(catalog)}' AS {_sql_identifier(catalog)} ("
        "TYPE iceberg, "
        f"ENDPOINT '{_sql_quote(endpoint)}', "
        "ACCESS_DELEGATION_MODE 'vended_credentials'"
        ")"
    )
    return conn
```
Also update the module docstring's env var list (remove
`POLARIS_CLIENT_ID`/`POLARIS_CLIENT_SECRET`, note they're now function
parameters):
```python
"""Build a DuckDB connection attached to the Polaris Iceberg REST catalog.

POLARIS_ENDPOINT/POLARIS_CATALOG are fixed env vars (not per-user); the
principal's own client_id/client_secret are passed in by the caller (from
the logged-in session — see app.session), not read from the environment.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_catalog.py -v
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/catalog.py tests/test_catalog.py
git commit -m "feat: take Polaris credentials as parameters, not env vars"
```

---

## Task 5: `/query` uses session credentials, guard removed

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Modify: `tests/test_query.py`

- [ ] **Step 1: Replace the query tests**

Full replacement for `tests/test_query.py` (drops every test about the
read-only/single-statement guard — that logic is being removed — and adds
session handling):
```python
import app.main as main_module
from app.catalog import CatalogConfigError
from fastapi.testclient import TestClient

from app.main import app
from app.session import create_session

client = TestClient(app)


class FakeResult:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns] if columns else None
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        return FakeResult(self._columns, self._rows)


def _logged_in_cookie():
    session_id = create_session("cid", "secret", "loader")
    return {"lakehouse_session": session_id}


def test_query_runs_select_and_returns_rows(monkeypatch):
    fake = FakeConnection(["id", "name"], [[1, "a"], [2, "b"]])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake
    )

    response = client.post(
        "/query", json={"sql": "SELECT * FROM nyc_taxi.trips"}, cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"columns": ["id", "name"], "rows": [[1, "a"], [2, "b"]]}
    assert fake.executed == ["SELECT * FROM nyc_taxi.trips"]


def test_query_passes_the_session_principals_own_credentials(monkeypatch):
    captured = {}

    def fake_build_connection(client_id, client_secret):
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        return FakeConnection(["x"], [[1]])

    monkeypatch.setattr(main_module, "build_connection", fake_build_connection)

    client.post(
        "/query", json={"sql": "SELECT 1"}, cookies=_logged_in_cookie()
    )

    assert captured == {"client_id": "cid", "client_secret": "secret"}


def test_query_rejects_empty_sql():
    response = client.post("/query", json={"sql": "   "}, cookies=_logged_in_cookie())
    assert response.status_code == 400


def test_query_allows_write_statements_now(monkeypatch):
    # The app-level guard is gone — Polaris itself is the authorization
    # boundary now. A DDL statement should reach build_connection/execute,
    # not be rejected by the app.
    fake = FakeConnection([], [])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake
    )

    response = client.post(
        "/query",
        json={"sql": "CREATE TABLE lakehouse.nyc_taxi.x AS SELECT 1"},
        cookies=_logged_in_cookie(),
    )

    assert response.status_code == 200
    assert fake.executed == ["CREATE TABLE lakehouse.nyc_taxi.x AS SELECT 1"]


def test_query_allows_multi_statement_sql_now(monkeypatch):
    fake = FakeConnection(["x"], [[1]])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake
    )

    response = client.post(
        "/query",
        json={"sql": "CREATE TABLE t AS SELECT 1; SELECT * FROM t"},
        cookies=_logged_in_cookie(),
    )

    assert response.status_code == 200


def test_query_returns_500_on_catalog_config_error(monkeypatch):
    def raise_config_error(client_id, client_secret):
        raise CatalogConfigError("missing required environment variable POLARIS_ENDPOINT")

    monkeypatch.setattr(main_module, "build_connection", raise_config_error)

    response = client.post("/query", json={"sql": "SELECT 1"}, cookies=_logged_in_cookie())

    assert response.status_code == 500
    assert "POLARIS_ENDPOINT" in response.json()["detail"]


def test_query_returns_400_on_duckdb_error(monkeypatch):
    class RaisingConnection:
        def execute(self, sql):
            raise ValueError("Catalog Error: Table with name trips does not exist!")

    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: RaisingConnection()
    )

    response = client.post(
        "/query", json={"sql": "SELECT * FROM nyc_taxi.trips"}, cookies=_logged_in_cookie()
    )

    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_query_returns_401_without_a_session():
    response = client.post("/query", json={"sql": "SELECT 1"})
    assert response.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_query.py -v
```
Expected: FAIL — `build_connection()` is still called with no arguments in
`app/main.py`, and the read-only/single-statement guards still reject
`CREATE TABLE ...` and multi-statement SQL.

- [ ] **Step 3: Update `run_query`**

In `app/main.py`, remove `_is_read_only`, `_is_single_statement`, and
`_READ_ONLY_PREFIXES` entirely (they're dead code now — grep the file to
confirm nothing else references them before deleting). Replace `run_query`
with:
```python
@app.post("/query", response_model=QueryResponse)
def run_query(
    request: QueryRequest, session: Session = Depends(require_session)
) -> QueryResponse:
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="sql must not be empty")

    try:
        connection = build_connection(session.client_id, session.client_secret)
    except CatalogConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"failed to connect to catalog: {exc}"
        ) from exc

    try:
        result = connection.execute(request.sql)
        columns = [d[0] for d in result.description] if result.description else []
        rows = [list(row) for row in result.fetchall()]
    except Exception as exc:  # DuckDB/catalog errors surface as plain Exceptions
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return QueryResponse(columns=columns, rows=rows)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS for `tests/test_query.py` and `tests/test_auth_routes.py`.
`tests/test_index.py`'s pre-existing test will still be red until Task 13 —
that's expected, leave it.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_query.py
git commit -m "feat: /query uses the session's own credentials, drop the app-level guard"
```

---

## Task 6: `app/polaris_client.py` — Iceberg Catalog + Management REST client

**Files (in `lakehouse-ui`):**
- Create: `app/polaris_client.py`
- Test: `tests/test_polaris_client.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_polaris_client.py`:
```python
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from app.polaris_client import (
    PolarisClientError,
    get_principal_roles,
    get_table_schema,
    list_namespaces,
    list_tables,
)

TOKEN_RESPONSE = {"access_token": "tok"}


def _response(body):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    return resp


def _fake_urlopen(responses):
    """`responses` maps a URL substring to the JSON body to return for the
    first request whose URL contains that substring."""

    def _urlopen(request, timeout=10):
        url = request.full_url
        for substring, body in responses.items():
            if substring in url:
                return _response(body)
        raise AssertionError(f"unexpected URL: {url}")

    return _urlopen


def test_list_namespaces_returns_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "lakehouse/namespaces": {
                    "namespaces": [["nyc_taxi"]],
                    "next-page-token": None,
                },
            }
        ),
    )

    result = list_namespaces("http://polaris:8181/api/catalog", "cid", "secret", "lakehouse")

    assert result == ["nyc_taxi"]


def test_list_tables_returns_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "namespaces/nyc_taxi/tables": {
                    "identifiers": [
                        {"namespace": ["nyc_taxi"], "name": "trips"},
                        {"namespace": ["nyc_taxi"], "name": "fct_trips"},
                    ],
                    "next-page-token": None,
                },
            }
        ),
    )

    result = list_tables(
        "http://polaris:8181/api/catalog", "cid", "secret", "lakehouse", "nyc_taxi"
    )

    assert result == ["trips", "fct_trips"]


def test_get_table_schema_returns_fields_from_the_current_schema(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "tables/trips": {
                    "metadata": {
                        "current-schema-id": 0,
                        "schemas": [
                            {
                                "schema-id": 0,
                                "fields": [
                                    {"id": 1, "name": "VendorID", "required": False, "type": "int"},
                                    {"id": 2, "name": "trip_distance", "required": False, "type": "double"},
                                ],
                            }
                        ],
                    }
                },
            }
        ),
    )

    result = get_table_schema(
        "http://polaris:8181/api/catalog", "cid", "secret", "lakehouse", "nyc_taxi", "trips"
    )

    assert result == [
        {"name": "VendorID", "type": "int", "required": False},
        {"name": "trip_distance", "type": "double", "required": False},
    ]


def test_get_principal_roles_returns_role_names(monkeypatch):
    monkeypatch.setattr(
        "app.polaris_client.urllib.request.urlopen",
        _fake_urlopen(
            {
                "oauth/tokens": TOKEN_RESPONSE,
                "principals/loader/principal-roles": {
                    "roles": [{"name": "loader_role", "federated": False}]
                },
            }
        ),
    )

    result = get_principal_roles(
        "http://polaris:8181/api/management", "root", "rootsecret", "loader"
    )

    assert result == ["loader_role"]


def test_list_namespaces_raises_polaris_client_error_on_http_error(monkeypatch):
    def _urlopen(request, timeout=10):
        if "oauth/tokens" in request.full_url:
            return _response(TOKEN_RESPONSE)
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

    monkeypatch.setattr("app.polaris_client.urllib.request.urlopen", _urlopen)

    with pytest.raises(PolarisClientError, match="403"):
        list_namespaces("http://polaris:8181/api/catalog", "cid", "secret", "lakehouse")


def test_list_namespaces_raises_on_unreachable_polaris(monkeypatch):
    def raise_url_error(request, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("app.polaris_client.urllib.request.urlopen", raise_url_error)

    with pytest.raises(PolarisClientError, match="could not"):
        list_namespaces("http://polaris:8181/api/catalog", "cid", "secret", "lakehouse")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_polaris_client.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.polaris_client'`.

- [ ] **Step 3: Write minimal implementation**

`app/polaris_client.py`:
```python
"""Thin REST client for Polaris's Iceberg Catalog API (namespaces, tables,
table schemas) and Management API (principal role lookups).

Every call does its own fresh OAuth exchange with the given credentials —
no token caching. Polaris's default token lifetime is 1 hour; a
long-lived session shouldn't silently break because a cached token expired,
and the extra HTTP round trip per call is cheap for how infrequently these
are called (sidebar browsing, not the hot query path).

Response shapes below were confirmed against a real Polaris 1.7.0
deployment, not assumed from docs:
  namespaces: {"namespaces": [["nyc_taxi"]], "next-page-token": null}
  tables:     {"identifiers": [{"namespace": [...], "name": "trips"}], ...}
  schema:     {"metadata": {"current-schema-id": 0, "schemas": [
                 {"schema-id": 0, "fields": [{"name":..,"type":..,"required":..}]}
               ]}}
  roles:      {"roles": [{"name": "loader_role", ...}]}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class PolarisClientError(RuntimeError):
    """Raised when a Polaris REST call fails (auth, network, or non-2xx)."""


def _get_token(base_url: str, client_id: str, client_secret: str) -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "PRINCIPAL_ROLE:ALL",
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/v1/oauth/tokens",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise PolarisClientError(f"could not authenticate with Polaris: {exc}") from exc
    token = body.get("access_token")
    if not token:
        raise PolarisClientError("Polaris did not return an access_token")
    return token


def _get(base_url: str, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise PolarisClientError(f"Polaris returned {exc.code} for {path}") from exc
    except urllib.error.URLError as exc:
        raise PolarisClientError(f"could not reach Polaris: {exc}") from exc


def list_namespaces(
    catalog_endpoint: str, client_id: str, client_secret: str, catalog: str
) -> list[str]:
    """Return the top-level namespace names in `catalog`."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(catalog_endpoint, token, f"/v1/{catalog}/namespaces")
    return [".".join(parts) for parts in body.get("namespaces", [])]


def list_tables(
    catalog_endpoint: str, client_id: str, client_secret: str, catalog: str, namespace: str
) -> list[str]:
    """Return table names in `catalog`.`namespace`."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(catalog_endpoint, token, f"/v1/{catalog}/namespaces/{namespace}/tables")
    return [identifier["name"] for identifier in body.get("identifiers", [])]


def get_table_schema(
    catalog_endpoint: str,
    client_id: str,
    client_secret: str,
    catalog: str,
    namespace: str,
    table: str,
) -> list[dict]:
    """Return the table's current schema as a list of
    {"name", "type", "required"} dicts."""
    token = _get_token(catalog_endpoint, client_id, client_secret)
    body = _get(
        catalog_endpoint, token, f"/v1/{catalog}/namespaces/{namespace}/tables/{table}"
    )
    metadata = body.get("metadata", {})
    schemas = metadata.get("schemas", [])
    current_id = metadata.get("current-schema-id")
    schema = next(
        (s for s in schemas if s.get("schema-id") == current_id),
        schemas[0] if schemas else {},
    )
    return [
        {"name": f["name"], "type": f["type"], "required": f["required"]}
        for f in schema.get("fields", [])
    ]


def get_principal_roles(
    management_endpoint: str, client_id: str, client_secret: str, principal_name: str
) -> list[str]:
    """Return the principal role names assigned to `principal_name`.

    `client_id`/`client_secret` here are a credential authorized to look up
    *other* principals' roles (a regular principal is not authorized to
    list even its own — confirmed live) — in practice this is always
    called with the app's own root service credential, never a session's.
    """
    token = _get_token(management_endpoint, client_id, client_secret)
    body = _get(
        management_endpoint, token, f"/principals/{principal_name}/principal-roles"
    )
    return [role["name"] for role in body.get("roles", [])]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_polaris_client.py -v
```
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/polaris_client.py tests/test_polaris_client.py
git commit -m "feat: add a Polaris Catalog + Management API client"
```

---

## Task 7: Catalog browser endpoints

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Test: `tests/test_catalog_routes.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_catalog_routes.py`:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_client import PolarisClientError
from app.session import create_session

client = TestClient(app)


def _logged_in_cookie():
    session_id = create_session("cid", "secret", "loader")
    return {"lakehouse_session": session_id}


def test_namespaces_requires_a_session():
    response = client.get("/catalog/namespaces")
    assert response.status_code == 401


def test_namespaces_returns_list(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "list_namespaces",
        lambda endpoint, cid, secret, catalog: ["nyc_taxi"],
    )

    response = client.get("/catalog/namespaces", cookies=_logged_in_cookie())

    assert response.status_code == 200
    assert response.json() == {"namespaces": ["nyc_taxi"]}


def test_namespaces_uses_the_sessions_own_credentials(monkeypatch):
    captured = {}

    def fake_list_namespaces(endpoint, client_id, client_secret, catalog):
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        return []

    monkeypatch.setattr(main_module, "list_namespaces", fake_list_namespaces)

    client.get("/catalog/namespaces", cookies=_logged_in_cookie())

    assert captured == {"client_id": "cid", "client_secret": "secret"}


def test_tables_returns_list(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "list_tables",
        lambda endpoint, cid, secret, catalog, namespace: ["trips", "fct_trips"],
    )

    response = client.get("/catalog/tables/nyc_taxi", cookies=_logged_in_cookie())

    assert response.status_code == 200
    assert response.json() == {"tables": ["trips", "fct_trips"]}


def test_table_schema_returns_fields(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_table_schema",
        lambda endpoint, cid, secret, catalog, namespace, table: [
            {"name": "VendorID", "type": "int", "required": False}
        ],
    )

    response = client.get(
        "/catalog/tables/nyc_taxi/trips/schema", cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    assert response.json() == {
        "fields": [{"name": "VendorID", "type": "int", "required": False}]
    }


def test_namespaces_returns_502_on_polaris_client_error(monkeypatch):
    def raise_error(endpoint, cid, secret, catalog):
        raise PolarisClientError("Polaris returned 403 for /v1/lakehouse/namespaces")

    monkeypatch.setattr(main_module, "list_namespaces", raise_error)

    response = client.get("/catalog/namespaces", cookies=_logged_in_cookie())

    assert response.status_code == 502
    assert "403" in response.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_catalog_routes.py -v
```
Expected: FAIL — none of these routes exist yet (404s).

- [ ] **Step 3: Write the implementation**

Add to `app/main.py`'s imports:
```python
from app.polaris_client import (
    PolarisClientError,
    get_table_schema,
    list_namespaces,
    list_tables,
)
```

Add a small helper (near the top, after `require_session`) to read
`POLARIS_ENDPOINT`/`POLARIS_CATALOG` once per request:
```python
def _catalog_config() -> tuple[str, str]:
    endpoint = os.environ.get("POLARIS_ENDPOINT")
    catalog = os.environ.get("POLARIS_CATALOG")
    if not endpoint or not catalog:
        raise HTTPException(
            status_code=500,
            detail="server missing POLARIS_ENDPOINT/POLARIS_CATALOG configuration",
        )
    return endpoint, catalog
```

Add the three routes (place after `/query`):
```python
@app.get("/catalog/namespaces")
def catalog_namespaces(session: Session = Depends(require_session)) -> dict:
    endpoint, catalog = _catalog_config()
    try:
        namespaces = list_namespaces(endpoint, session.client_id, session.client_secret, catalog)
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"namespaces": namespaces}


@app.get("/catalog/tables/{namespace}")
def catalog_tables(
    namespace: str, session: Session = Depends(require_session)
) -> dict:
    endpoint, catalog = _catalog_config()
    try:
        tables = list_tables(
            endpoint, session.client_id, session.client_secret, catalog, namespace
        )
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"tables": tables}


@app.get("/catalog/tables/{namespace}/{table}/schema")
def catalog_table_schema(
    namespace: str, table: str, session: Session = Depends(require_session)
) -> dict:
    endpoint, catalog = _catalog_config()
    try:
        fields = get_table_schema(
            endpoint, session.client_id, session.client_secret, catalog, namespace, table
        )
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"fields": fields}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS for the new file; no regressions elsewhere (aside from the
already-known, expected-red `tests/test_index.py` test from Task 3).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_catalog_routes.py
git commit -m "feat: add catalog browser endpoints (namespaces/tables/schema)"
```

---

## Task 8: `/me` endpoint

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Test: `tests/test_me_route.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_me_route.py`:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_client import PolarisClientError
from app.session import create_session

client = TestClient(app)


def _logged_in_cookie(principal="loader"):
    session_id = create_session("cid", "secret", principal)
    return {"lakehouse_session": session_id}


def test_me_requires_a_session():
    response = client.get("/me")
    assert response.status_code == 401


def test_me_returns_principal_and_roles(monkeypatch):
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "root")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "rootsecret")
    monkeypatch.setenv("POLARIS_MANAGEMENT_ENDPOINT", "http://polaris:8181/api/management")
    monkeypatch.setattr(
        main_module,
        "get_principal_roles",
        lambda endpoint, cid, secret, principal: ["loader_role"],
    )

    response = client.get("/me", cookies=_logged_in_cookie("loader"))

    assert response.status_code == 200
    assert response.json() == {"principal": "loader", "roles": ["loader_role"]}


def test_me_uses_the_root_service_credential_not_the_sessions(monkeypatch):
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "root")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "rootsecret")
    monkeypatch.setenv("POLARIS_MANAGEMENT_ENDPOINT", "http://polaris:8181/api/management")
    captured = {}

    def fake_get_principal_roles(endpoint, client_id, client_secret, principal):
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        return []

    monkeypatch.setattr(main_module, "get_principal_roles", fake_get_principal_roles)

    client.get("/me", cookies=_logged_in_cookie("loader"))

    assert captured == {"client_id": "root", "client_secret": "rootsecret"}


def test_me_returns_500_when_root_credentials_missing(monkeypatch):
    monkeypatch.delenv("POLARIS_ROOT_CLIENT_ID", raising=False)
    monkeypatch.delenv("POLARIS_ROOT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("POLARIS_MANAGEMENT_ENDPOINT", raising=False)

    response = client.get("/me", cookies=_logged_in_cookie())

    assert response.status_code == 500


def test_me_returns_502_on_polaris_client_error(monkeypatch):
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "root")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "rootsecret")
    monkeypatch.setenv("POLARIS_MANAGEMENT_ENDPOINT", "http://polaris:8181/api/management")

    def raise_error(endpoint, cid, secret, principal):
        raise PolarisClientError("Polaris returned 404")

    monkeypatch.setattr(main_module, "get_principal_roles", raise_error)

    response = client.get("/me", cookies=_logged_in_cookie())

    assert response.status_code == 502
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_me_route.py -v
```
Expected: FAIL — `/me` doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Add `get_principal_roles` to the `app.polaris_client` import line added in
Task 7 (it should now read):
```python
from app.polaris_client import (
    PolarisClientError,
    get_principal_roles,
    get_table_schema,
    list_namespaces,
    list_tables,
)
```

Add the route (place after the catalog routes):
```python
@app.get("/me")
def me_route(session: Session = Depends(require_session)) -> dict:
    root_client_id = os.environ.get("POLARIS_ROOT_CLIENT_ID")
    root_client_secret = os.environ.get("POLARIS_ROOT_CLIENT_SECRET")
    management_endpoint = os.environ.get("POLARIS_MANAGEMENT_ENDPOINT")
    if not root_client_id or not root_client_secret or not management_endpoint:
        raise HTTPException(
            status_code=500,
            detail="server missing POLARIS_ROOT_CLIENT_ID/POLARIS_ROOT_CLIENT_SECRET/"
            "POLARIS_MANAGEMENT_ENDPOINT configuration",
        )
    try:
        roles = get_principal_roles(
            management_endpoint, root_client_id, root_client_secret, session.principal_name
        )
    except PolarisClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"principal": session.principal_name, "roles": roles}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS for the new file, no regressions.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_me_route.py
git commit -m "feat: add /me (principal + roles, via the root service credential)"
```

---

## Task 9: Query history store (`app/history.py`)

**Files (in `lakehouse-ui`):**
- Create: `app/history.py`
- Test: `tests/test_history.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_history.py`:
```python
import datetime

import pytest

from app.history import HistoryConfigError, ensure_schema, get_history, record_query


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, fetch_rows=None):
        self.executed = []
        self._fetch_rows = fetch_rows or []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return FakeCursor(self._fetch_rows)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


REQUIRED_ENV = {
    "LAKEHOUSE_UI_DB_HOST": "polaris-postgres",
    "LAKEHOUSE_UI_DB_NAME": "lakehouse_ui",
    "LAKEHOUSE_UI_DB_USER": "lakehouse_ui",
    "LAKEHOUSE_UI_DB_PASSWORD": "s3cr3t",
}


def _set_env(monkeypatch, **overrides):
    values = {**REQUIRED_ENV, **overrides}
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_ensure_schema_creates_table_and_index_and_does_not_own_the_connection():
    fake = FakeConnection()

    ensure_schema(conn=fake)

    joined = "\n".join(sql for sql, _ in fake.executed)
    assert "CREATE TABLE IF NOT EXISTS query_history" in joined
    assert "CREATE INDEX IF NOT EXISTS query_history_principal_run_at" in joined
    assert fake.committed is False
    assert fake.closed is False


def test_record_query_inserts_a_success_row():
    fake = FakeConnection()

    record_query(
        principal="loader",
        sql_text="SELECT 1",
        status="success",
        duration_ms=42,
        row_count=1,
        conn=fake,
    )

    sql, params = fake.executed[0]
    assert "INSERT INTO query_history" in sql
    assert params == ("loader", "SELECT 1", "success", 1, None, 42)


def test_record_query_inserts_an_error_row():
    fake = FakeConnection()

    record_query(
        principal="lakehouse-ui",
        sql_text="DROP TABLE x",
        status="error",
        duration_ms=5,
        error_message="not authorized",
        conn=fake,
    )

    _, params = fake.executed[0]
    assert params == ("lakehouse-ui", "DROP TABLE x", "error", None, "not authorized", 5)


def test_get_history_returns_rows_for_the_given_principal():
    run_at = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    fake = FakeConnection(
        fetch_rows=[(1, "SELECT 1", "success", 1, None, 10, run_at)]
    )

    result = get_history("loader", conn=fake)

    assert result == [
        {
            "id": 1,
            "sql_text": "SELECT 1",
            "status": "success",
            "row_count": 1,
            "error_message": None,
            "duration_ms": 10,
            "run_at": "2026-09-13T12:00:00+00:00",
        }
    ]
    sql, params = fake.executed[0]
    assert "WHERE principal = %s" in sql
    assert params == ("loader", 50, 0)


def test_get_history_respects_limit_and_offset():
    fake = FakeConnection(fetch_rows=[])

    get_history("loader", limit=10, offset=20, conn=fake)

    _, params = fake.executed[0]
    assert params == ("loader", 10, 20)


def test_get_history_requires_env_vars_when_no_conn_given(monkeypatch):
    _set_env(monkeypatch, LAKEHOUSE_UI_DB_HOST=None)
    with pytest.raises(HistoryConfigError, match="LAKEHOUSE_UI_DB_HOST"):
        get_history("loader")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_history.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.history'`.

- [ ] **Step 3: Write minimal implementation**

`app/history.py`:
```python
"""Query history, stored in Postgres — a dedicated `lakehouse_ui` database
on the existing polaris-postgres instance (see charts/polaris-postgres).

Connection info comes from environment variables (LAKEHOUSE_UI_DB_HOST/
_NAME/_USER/_PASSWORD), the same explicit-env-var pattern app.catalog uses
for POLARIS_* — no ORM.
"""
from __future__ import annotations

import os
from typing import Any, Protocol


class HistoryConfigError(RuntimeError):
    """Raised when a required LAKEHOUSE_UI_DB_* environment variable is missing."""


class ExecutableConnection(Protocol):
    def execute(self, sql: str, params: tuple = ()) -> Any: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


_REQUIRED_ENV_VARS = (
    "LAKEHOUSE_UI_DB_HOST",
    "LAKEHOUSE_UI_DB_NAME",
    "LAKEHOUSE_UI_DB_USER",
    "LAKEHOUSE_UI_DB_PASSWORD",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise HistoryConfigError(f"missing required environment variable {name}")
    return value


def _connect() -> ExecutableConnection:
    env = {name: _require_env(name) for name in _REQUIRED_ENV_VARS}
    import psycopg

    return psycopg.connect(
        host=env["LAKEHOUSE_UI_DB_HOST"],
        dbname=env["LAKEHOUSE_UI_DB_NAME"],
        user=env["LAKEHOUSE_UI_DB_USER"],
        password=env["LAKEHOUSE_UI_DB_PASSWORD"],
    )


def ensure_schema(conn: ExecutableConnection | None = None) -> None:
    """Create the query_history table/index if they don't already exist.
    Safe to call on every app startup. If `conn` is not given, this opens
    its own connection and closes it; if `conn` IS given (tests, or a
    caller managing its own transaction), this never commits/closes it —
    that's the caller's responsibility.
    """
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS query_history (
            id            BIGSERIAL PRIMARY KEY,
            principal     TEXT NOT NULL,
            sql_text      TEXT NOT NULL,
            status        TEXT NOT NULL,
            row_count     INTEGER,
            error_message TEXT,
            duration_ms   INTEGER NOT NULL,
            run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS query_history_principal_run_at "
        "ON query_history (principal, run_at DESC)"
    )
    if owns_conn:
        conn.commit()
        conn.close()


def record_query(
    principal: str,
    sql_text: str,
    status: str,
    duration_ms: int,
    row_count: int | None = None,
    error_message: str | None = None,
    conn: ExecutableConnection | None = None,
) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    conn.execute(
        """
        INSERT INTO query_history
            (principal, sql_text, status, row_count, error_message, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (principal, sql_text, status, row_count, error_message, duration_ms),
    )
    if owns_conn:
        conn.commit()
        conn.close()


def get_history(
    principal: str, limit: int = 50, offset: int = 0, conn: ExecutableConnection | None = None
) -> list[dict]:
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    cursor = conn.execute(
        """
        SELECT id, sql_text, status, row_count, error_message, duration_ms, run_at
        FROM query_history
        WHERE principal = %s
        ORDER BY run_at DESC
        LIMIT %s OFFSET %s
        """,
        (principal, limit, offset),
    )
    rows = cursor.fetchall()
    result = [
        {
            "id": row[0],
            "sql_text": row[1],
            "status": row[2],
            "row_count": row[3],
            "error_message": row[4],
            "duration_ms": row[5],
            "run_at": row[6].isoformat(),
        }
        for row in rows
    ]
    if owns_conn:
        conn.close()
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_history.py -v
```
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/history.py tests/test_history.py
git commit -m "feat: add a Postgres-backed query-history store"
```

---

## Task 10: Wire history into `/query`, add `GET /history`

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`
- Test: `tests/test_history_routes.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_history_routes.py`:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.session import create_session

client = TestClient(app)


def _logged_in_cookie(principal="loader"):
    session_id = create_session("cid", "secret", principal)
    return {"lakehouse_session": session_id}


class FakeResult:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns] if columns else None
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def execute(self, sql):
        return FakeResult(self._columns, self._rows)


def test_history_requires_a_session():
    response = client.get("/history")
    assert response.status_code == 401


def test_history_returns_the_current_principals_rows(monkeypatch):
    captured = {}

    def fake_get_history(principal, limit=50, offset=0):
        captured["principal"] = principal
        captured["limit"] = limit
        captured["offset"] = offset
        return [
            {
                "id": 1,
                "sql_text": "SELECT 1",
                "status": "success",
                "row_count": 1,
                "error_message": None,
                "duration_ms": 5,
                "run_at": "2026-09-13T12:00:00+00:00",
            }
        ]

    monkeypatch.setattr(main_module, "get_history", fake_get_history)

    response = client.get("/history", cookies=_logged_in_cookie("loader"))

    assert response.status_code == 200
    assert response.json()["history"][0]["sql_text"] == "SELECT 1"
    assert captured["principal"] == "loader"
    assert captured["limit"] == 50
    assert captured["offset"] == 0


def test_history_accepts_limit_and_offset_query_params(monkeypatch):
    captured = {}

    def fake_get_history(principal, limit=50, offset=0):
        captured["limit"] = limit
        captured["offset"] = offset
        return []

    monkeypatch.setattr(main_module, "get_history", fake_get_history)

    client.get("/history?limit=10&offset=20", cookies=_logged_in_cookie())

    assert captured == {"limit": 10, "offset": 20}


def test_successful_query_is_recorded_in_history(monkeypatch):
    fake_conn = FakeConnection(["x"], [[1]])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake_conn
    )
    recorded = []
    monkeypatch.setattr(
        main_module,
        "record_query",
        lambda **kwargs: recorded.append(kwargs),
    )

    client.post("/query", json={"sql": "SELECT 1"}, cookies=_logged_in_cookie("loader"))

    assert len(recorded) == 1
    assert recorded[0]["principal"] == "loader"
    assert recorded[0]["sql_text"] == "SELECT 1"
    assert recorded[0]["status"] == "success"
    assert recorded[0]["row_count"] == 1
    assert recorded[0]["error_message"] is None
    assert isinstance(recorded[0]["duration_ms"], int)


def test_failed_query_is_recorded_in_history_too(monkeypatch):
    class RaisingConnection:
        def execute(self, sql):
            raise ValueError("boom")

    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: RaisingConnection()
    )
    recorded = []
    monkeypatch.setattr(
        main_module,
        "record_query",
        lambda **kwargs: recorded.append(kwargs),
    )

    client.post("/query", json={"sql": "SELECT bad"}, cookies=_logged_in_cookie("loader"))

    assert len(recorded) == 1
    assert recorded[0]["status"] == "error"
    assert recorded[0]["row_count"] is None
    assert "boom" in recorded[0]["error_message"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_history_routes.py -v
```
Expected: FAIL — `/history` doesn't exist; `record_query` is never called
from `/query`.

- [ ] **Step 3: Write the implementation**

Add to `app/main.py`'s imports:
```python
import time

from app.history import get_history, record_query
```

Replace `run_query` (from Task 5) with a version that times the execution
and records history on both the success and failure paths:
```python
@app.post("/query", response_model=QueryResponse)
def run_query(
    request: QueryRequest, session: Session = Depends(require_session)
) -> QueryResponse:
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="sql must not be empty")

    try:
        connection = build_connection(session.client_id, session.client_secret)
    except CatalogConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"failed to connect to catalog: {exc}"
        ) from exc

    started_at = time.monotonic()
    try:
        result = connection.execute(request.sql)
        columns = [d[0] for d in result.description] if result.description else []
        rows = [list(row) for row in result.fetchall()]
    except Exception as exc:  # DuckDB/catalog errors surface as plain Exceptions
        duration_ms = int((time.monotonic() - started_at) * 1000)
        record_query(
            principal=session.principal_name,
            sql_text=request.sql,
            status="error",
            duration_ms=duration_ms,
            error_message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    duration_ms = int((time.monotonic() - started_at) * 1000)
    record_query(
        principal=session.principal_name,
        sql_text=request.sql,
        status="success",
        duration_ms=duration_ms,
        row_count=len(rows),
    )
    return QueryResponse(columns=columns, rows=rows)
```

Add the `/history` route (place after `/me`):
```python
@app.get("/history")
def history_route(
    limit: int = 50, offset: int = 0, session: Session = Depends(require_session)
) -> dict:
    history = get_history(session.principal_name, limit=limit, offset=offset)
    return {"history": history}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS for the new file, no regressions (aside from the
already-known `tests/test_index.py` red test, still pending Task 13).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_history_routes.py
git commit -m "feat: record query history, add GET /history"
```

---

## Task 11: `requirements.txt`, app startup schema init, Docker check

**Files (in `lakehouse-ui`):**
- Modify: `requirements.txt`
- Modify: `app/main.py`

- [ ] **Step 1: Add the Postgres driver**

`requirements.txt` (add one line, keep the rest as-is):
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
duckdb==1.5.3
pydantic==2.10.4
psycopg[binary]==3.2.3
```

- [ ] **Step 2: Call `ensure_schema()` at app startup**

Add to `app/main.py`'s imports (extend the `app.history` import line from
Task 10):
```python
from app.history import ensure_schema, get_history, record_query
```

Add a FastAPI startup hook, placed right after `app = FastAPI(title="lakehouse-ui")`:
```python
@app.on_event("startup")
def _init_history_schema() -> None:
    try:
        ensure_schema()
    except Exception as exc:  # noqa: BLE001 — don't crash the whole app if
        # Postgres isn't reachable yet at startup; /history and query
        # recording will just fail per-request until it is, same as any
        # other downstream-dependency-not-ready case this app already
        # tolerates (e.g. Polaris being briefly unreachable).
        print(f"WARNING: could not initialize query_history schema at startup: {exc}")
```

- [ ] **Step 3: Verify locally**

```bash
cd C:/claude/lakehouse-ui
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pip install -r requirements.txt
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: `psycopg` installs without needing system build tools (the
`[binary]` extra ships a prebuilt wheel for linux/amd64 and most common
platforms) — if it needs to compile from source on this machine, note
that in your report; it should still work in the Docker image
(`python:3.12-slim` on linux/amd64) either way. All tests still pass.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt app/main.py
git commit -m "feat: add psycopg dependency, init query_history schema at startup"
```

---

## Task 12: `login.html`

**Files (in `lakehouse-ui`):**
- Create: `app/static/login.html`

- [ ] **Step 1: Write the page**

`app/static/login.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>lakehouse-ui — log in</title>
  <style>
    body {
      font-family: system-ui, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      background: #0f1419;
      color: #e8edf2;
    }
    form { background: #1b222b; padding: 2rem; border-radius: 8px; width: 320px; }
    h1 { font-size: 1.1rem; margin: 0 0 1.2rem; }
    label { display: block; font-size: 0.85rem; margin-bottom: 0.3rem; color: #8a95a1; }
    input {
      width: 100%;
      box-sizing: border-box;
      padding: 0.5rem;
      margin-bottom: 1rem;
      background: #141a21;
      border: 1px solid #2a333d;
      border-radius: 4px;
      color: #e8edf2;
    }
    button {
      width: 100%;
      padding: 0.6rem;
      background: #2b8a6b;
      color: white;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-weight: 600;
    }
    button:disabled { opacity: 0.6; cursor: default; }
    #error { color: #e05252; font-size: 0.85rem; margin-top: 0.8rem; min-height: 1.2em; }
  </style>
</head>
<body>
  <form id="login-form">
    <h1>lakehouse-ui</h1>
    <label for="client_id">Client ID</label>
    <input id="client_id" name="client_id" autocomplete="username" required>
    <label for="client_secret">Client Secret</label>
    <input id="client_secret" name="client_secret" type="password" autocomplete="current-password" required>
    <button type="submit" id="submit-btn">Log in</button>
    <div id="error"></div>
  </form>
  <script>
    const form = document.getElementById('login-form');
    const errorBox = document.getElementById('error');
    const submitButton = document.getElementById('submit-btn');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errorBox.textContent = '';
      submitButton.disabled = true;
      try {
        const client_id = document.getElementById('client_id').value;
        const client_secret = document.getElementById('client_secret').value;

        let response, body;
        try {
          response = await fetch('/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id, client_secret })
          });
          body = await response.json();
        } catch (err) {
          errorBox.textContent = 'Request failed: ' + err;
          return;
        }

        if (!response.ok) {
          errorBox.textContent = body.detail || 'Login failed';
          return;
        }
        window.location.href = '/';
      } finally {
        submitButton.disabled = false;
      }
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify it's served**

```bash
cd C:/claude/lakehouse-ui
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_auth_routes.py::test_login_page_route_serves_html -v
```
Expected: PASS (this test was already written in Task 3 and should have
been red until this file existed — confirm it's green now).

- [ ] **Step 3: Commit**

```bash
git add app/static/login.html
git commit -m "feat: add the login page"
```

---

## Task 13: `index.html` shell rewrite + static mount + updated index test

**Files (in `lakehouse-ui`):**
- Modify: `app/static/index.html` (full rewrite)
- Modify: `tests/test_index.py`

**Context:** `app/main.py` already mounts `/static` (Task 3) and gates `/`
behind a session (Task 3). This task replaces the old single-file
textarea+table page with the new shell: sidebar (catalog tree ⇄ history),
worksheet tabs, a CodeMirror editor container, results area, whoami/logout.
The actual interactive logic (tab switching, catalog fetching, running
queries) is Task 14's `app.js` — this task only needs the markup +
references to be correct; `app.js` doesn't need to exist yet for this
task's own test to pass (the test only checks markup, not behavior), but
write the `<script src="/static/app.js">` reference now since Task 14
won't need to touch this file again.

- [ ] **Step 1: Update the index test**

Replace `tests/test_index.py`:
```python
from fastapi.testclient import TestClient

from app.main import app
from app.session import create_session

client = TestClient(app)


def _logged_in_cookie():
    session_id = create_session("cid", "secret", "loader")
    return {"lakehouse_session": session_id}


def test_index_serves_the_app_shell_when_authenticated():
    response = client.get("/", cookies=_logged_in_cookie())

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="editor-container"' in response.text
    assert 'id="worksheet-tabs"' in response.text
    assert 'id="catalog-tree"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="whoami-text"' in response.text


def test_static_assets_are_served():
    response = client.get("/static/app.css")
    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_index.py -v
```
Expected: FAIL — the old `index.html` has none of these element IDs, and
`app.css` doesn't exist yet.

- [ ] **Step 3: Write the new shell**

`app/static/index.html` (full replacement):
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>lakehouse-ui</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <div id="app">
    <div id="sidebar">
      <div id="sidebar-header">
        <button id="tab-catalog" class="sidebar-tab active" type="button">Catalog</button>
        <button id="tab-history" class="sidebar-tab" type="button">History</button>
        <button id="refresh-catalog" type="button" title="Refresh catalog">⟳</button>
      </div>
      <div id="catalog-tree" class="sidebar-panel"></div>
      <div id="history-list" class="sidebar-panel" hidden></div>
    </div>
    <div id="main">
      <div id="topbar">
        <div id="worksheet-tabs"></div>
        <div id="whoami">
          <span id="whoami-text">...</span>
          <button id="logout-btn" type="button">Log out</button>
        </div>
      </div>
      <div id="editor-container"></div>
      <div id="toolbar">
        <button id="run-btn" type="button">▶ Run</button>
        <span id="query-status"></span>
      </div>
      <div id="results-container">
        <table id="results"></table>
        <div id="error"></div>
      </div>
    </div>
  </div>

  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/sql/sql.min.js"></script>
  <script src="/static/app.js"></script>
</body>
</html>
```

Also create a minimal placeholder `app/static/app.css` in THIS task (just
enough for `test_static_assets_are_served` to pass — the full styling is
also written in this task, in full, right now, not deferred: see Step 4).

- [ ] **Step 4: Write the stylesheet**

`app/static/app.css`:
```css
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; background: #0f1419; color: #e8edf2; }
#app { display: flex; height: 100vh; }

#sidebar {
  width: 240px;
  background: #141a21;
  border-right: 1px solid #2a333d;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
#sidebar-header { display: flex; border-bottom: 1px solid #2a333d; }
.sidebar-tab {
  flex: 1;
  background: none;
  border: none;
  color: #8a95a1;
  padding: 0.6rem;
  cursor: pointer;
  font-size: 0.85rem;
}
.sidebar-tab.active { color: #e8edf2; border-bottom: 2px solid #2b8a6b; }
#refresh-catalog { background: none; border: none; color: #8a95a1; cursor: pointer; padding: 0.6rem; }
.sidebar-panel { flex: 1; overflow: auto; padding: 0.5rem; font-size: 0.85rem; }
.tree-namespace { cursor: pointer; padding: 0.2rem 0; }
.tree-tables { padding-left: 1rem; }
.tree-table { cursor: pointer; padding: 0.15rem 0; color: #8fb8e8; }
.tree-table:hover { text-decoration: underline; }
.history-item {
  cursor: pointer;
  padding: 0.4rem 0.3rem;
  border-bottom: 1px solid #1e262f;
  font-family: monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.history-item-error { color: #e05252; }

#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #141a21;
  border-bottom: 1px solid #2a333d;
}
#worksheet-tabs { display: flex; overflow-x: auto; }
.worksheet-tab {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 1rem;
  border-right: 1px solid #2a333d;
  cursor: pointer;
  color: #8a95a1;
  white-space: nowrap;
}
.worksheet-tab.active { color: #e8edf2; background: #1b222b; }
.worksheet-tab-close { opacity: 0.6; }
.worksheet-tab-close:hover { opacity: 1; }
.worksheet-tab-add { padding: 0.5rem 0.8rem; cursor: pointer; color: #5a6572; }
#whoami { display: flex; align-items: center; gap: 0.8rem; padding: 0 1rem; font-size: 0.85rem; color: #8a95a1; flex-shrink: 0; }
#logout-btn {
  background: none;
  border: 1px solid #2a333d;
  color: #8a95a1;
  padding: 0.3rem 0.6rem;
  border-radius: 4px;
  cursor: pointer;
}

#editor-container { border-bottom: 1px solid #2a333d; }
.CodeMirror { height: 150px; font-size: 0.9rem; }

#toolbar {
  padding: 0.5rem 1rem;
  background: #141a21;
  border-bottom: 1px solid #2a333d;
  display: flex;
  align-items: center;
  gap: 1rem;
}
#run-btn {
  background: #2b8a6b;
  color: white;
  border: none;
  padding: 0.4rem 1.2rem;
  border-radius: 4px;
  cursor: pointer;
  font-weight: 600;
}
#run-btn:disabled { opacity: 0.6; cursor: default; }
#query-status { color: #8a95a1; font-size: 0.85rem; }

#results-container { flex: 1; overflow: auto; padding: 1rem; }
#results { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
#results th, #results td { border: 1px solid #2a333d; padding: 0.3rem 0.6rem; text-align: left; }
#results th { background: #141a21; }
#error { color: #e05252; white-space: pre-wrap; margin-top: 1rem; }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/ -v
```
Expected: PASS for `tests/test_index.py`. Every other test file should
also be green now — this was the last piece Task 3 flagged as
expected-red.

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html app/static/app.css tests/test_index.py
git commit -m "feat: rewrite index.html as the console shell (sidebar, tabs, editor, results)"
```

---

## Task 14: `app.js` — worksheets, catalog tree, history, whoami

**Files (in `lakehouse-ui`):**
- Create: `app/static/app.js`

**Context:** This is pure frontend logic with no dedicated automated test
suite (consistent with how the original `index.html`'s JS was never
unit-tested — `tests/test_index.py` checks markup only). Verify this task
by actually exercising it in a browser (Step 2) against the real deployed
app once it's live (Task 19) — that's the real verification for this
piece, not something this task can fully self-certify statically.

- [ ] **Step 1: Write the script**

`app/static/app.js`:
```javascript
// lakehouse-ui frontend — worksheets, catalog browser, history, whoami.
// No framework; CodeMirror 5 (loaded via <script> tags in index.html) for
// SQL syntax highlighting only (no autocomplete).

let worksheets = [];
let activeWorksheetId = null;
let nextWorksheetNumber = 1;
let editor = null;

function newWorksheet(sql = '') {
  const id = 'ws-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);
  const worksheet = {
    id,
    title: 'Worksheet ' + nextWorksheetNumber++,
    sql,
    columns: [],
    rows: [],
    error: null,
  };
  worksheets.push(worksheet);
  return worksheet;
}

function activateWorksheet(id) {
  if (activeWorksheetId) {
    const current = worksheets.find((w) => w.id === activeWorksheetId);
    if (current) current.sql = editor.getValue();
  }
  activeWorksheetId = id;
  const worksheet = worksheets.find((w) => w.id === id);
  editor.setValue(worksheet.sql);
  renderTabs();
  renderResults(worksheet);
}

function closeWorksheet(id) {
  const index = worksheets.findIndex((w) => w.id === id);
  if (index === -1) return;
  worksheets.splice(index, 1);
  if (worksheets.length === 0) {
    const fresh = newWorksheet();
    activateWorksheet(fresh.id);
    return;
  }
  if (activeWorksheetId === id) {
    const next = worksheets[Math.max(0, index - 1)];
    activateWorksheet(next.id);
  } else {
    renderTabs();
  }
}

function renderTabs() {
  const container = document.getElementById('worksheet-tabs');
  container.innerHTML = '';
  worksheets.forEach((w) => {
    const tab = document.createElement('div');
    tab.className = 'worksheet-tab' + (w.id === activeWorksheetId ? ' active' : '');

    const title = document.createElement('span');
    title.textContent = w.title;
    title.addEventListener('click', () => activateWorksheet(w.id));

    const close = document.createElement('span');
    close.className = 'worksheet-tab-close';
    close.textContent = '✕';
    close.addEventListener('click', (e) => {
      e.stopPropagation();
      closeWorksheet(w.id);
    });

    tab.appendChild(title);
    tab.appendChild(close);
    container.appendChild(tab);
  });

  const addButton = document.createElement('div');
  addButton.className = 'worksheet-tab-add';
  addButton.textContent = '+';
  addButton.addEventListener('click', () => {
    const fresh = newWorksheet();
    activateWorksheet(fresh.id);
  });
  container.appendChild(addButton);
}

function renderResults(worksheet) {
  const errorBox = document.getElementById('error');
  const table = document.getElementById('results');
  table.innerHTML = '';
  errorBox.textContent = '';
  if (worksheet.error) {
    errorBox.textContent = worksheet.error;
    return;
  }
  if (!worksheet.columns.length) return;

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  worksheet.columns.forEach((c) => {
    const th = document.createElement('th');
    th.textContent = c;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  worksheet.rows.forEach((row) => {
    const tr = document.createElement('tr');
    row.forEach((value) => {
      const td = document.createElement('td');
      td.textContent = value === null ? 'NULL' : String(value);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

async function runActiveWorksheet() {
  const worksheet = worksheets.find((w) => w.id === activeWorksheetId);
  if (!worksheet) return;
  worksheet.sql = editor.getValue();
  const sql = worksheet.sql.trim();
  if (!sql) return;

  const statusEl = document.getElementById('query-status');
  const runButton = document.getElementById('run-btn');
  statusEl.textContent = 'Running...';
  runButton.disabled = true;

  try {
    let response, body;
    try {
      response = await fetch('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql }),
      });
      body = await response.json();
    } catch (err) {
      worksheet.error = 'Request failed: ' + err;
      worksheet.columns = [];
      worksheet.rows = [];
      renderResults(worksheet);
      return;
    }

    if (response.status === 401) {
      window.location.href = '/login';
      return;
    }

    if (!response.ok) {
      worksheet.error = body.detail || 'Query failed';
      worksheet.columns = [];
      worksheet.rows = [];
    } else {
      worksheet.error = null;
      worksheet.columns = body.columns;
      worksheet.rows = body.rows;
    }
    renderResults(worksheet);
    loadHistory();
  } finally {
    statusEl.textContent = '';
    runButton.disabled = false;
  }
}

// --- Catalog tree -----------------------------------------------------

async function loadCatalog() {
  const tree = document.getElementById('catalog-tree');
  tree.textContent = 'Loading...';
  let namespaces;
  try {
    const response = await fetch('/catalog/namespaces');
    if (response.status === 401) {
      window.location.href = '/login';
      return;
    }
    namespaces = (await response.json()).namespaces;
  } catch (err) {
    tree.textContent = 'Failed to load catalog: ' + err;
    return;
  }

  tree.innerHTML = '';
  namespaces.forEach((ns) => {
    const nsEl = document.createElement('div');
    nsEl.className = 'tree-namespace';
    nsEl.textContent = '▸ ' + ns;

    const tablesEl = document.createElement('div');
    tablesEl.className = 'tree-tables';
    tablesEl.hidden = true;
    let loaded = false;

    nsEl.addEventListener('click', async () => {
      tablesEl.hidden = !tablesEl.hidden;
      nsEl.textContent = (tablesEl.hidden ? '▸ ' : '▾ ') + ns;
      if (!loaded) {
        loaded = true;
        const response = await fetch('/catalog/tables/' + encodeURIComponent(ns));
        const body = await response.json();
        body.tables.forEach((t) => {
          const tEl = document.createElement('div');
          tEl.className = 'tree-table';
          tEl.textContent = t;
          tEl.addEventListener('click', (e) => {
            e.stopPropagation();
            editor.replaceSelection('lakehouse.' + ns + '.' + t);
            editor.focus();
          });
          tablesEl.appendChild(tEl);
        });
      }
    });

    tree.appendChild(nsEl);
    tree.appendChild(tablesEl);
  });
}

// --- History ------------------------------------------------------------

async function loadHistory() {
  const list = document.getElementById('history-list');
  let history;
  try {
    const response = await fetch('/history');
    if (response.status === 401) {
      window.location.href = '/login';
      return;
    }
    history = (await response.json()).history;
  } catch (err) {
    list.textContent = 'Failed to load history: ' + err;
    return;
  }

  list.innerHTML = '';
  history.forEach((item) => {
    const el = document.createElement('div');
    el.className = 'history-item' + (item.status === 'error' ? ' history-item-error' : '');
    const preview = item.sql_text.length > 60 ? item.sql_text.slice(0, 60) + '…' : item.sql_text;
    el.textContent = preview;
    el.title = item.sql_text + '\n' + item.run_at;
    el.addEventListener('click', () => {
      const fresh = newWorksheet(item.sql_text);
      activateWorksheet(fresh.id);
    });
    list.appendChild(el);
  });
}

// --- Sidebar panel switching ----------------------------------------------

function showSidebarPanel(panel) {
  document.getElementById('catalog-tree').hidden = panel !== 'catalog';
  document.getElementById('history-list').hidden = panel !== 'history';
  document.getElementById('tab-catalog').classList.toggle('active', panel === 'catalog');
  document.getElementById('tab-history').classList.toggle('active', panel === 'history');
  if (panel === 'history') loadHistory();
}

// --- Whoami / logout --------------------------------------------------

async function loadWhoami() {
  const response = await fetch('/me');
  if (response.status === 401) {
    window.location.href = '/login';
    return;
  }
  const body = await response.json();
  document.getElementById('whoami-text').textContent = body.principal + ' · ' + body.roles.join(', ');
}

async function logout() {
  await fetch('/logout', { method: 'POST' });
  window.location.href = '/login';
}

// --- Init ---------------------------------------------------------------

function init() {
  editor = CodeMirror(document.getElementById('editor-container'), {
    mode: 'text/x-sql',
    theme: 'dracula',
    lineNumbers: true,
    value: '',
  });
  editor.setOption('extraKeys', {
    'Cmd-Enter': runActiveWorksheet,
    'Ctrl-Enter': runActiveWorksheet,
  });

  const first = newWorksheet();
  activeWorksheetId = first.id;
  renderTabs();

  document.getElementById('run-btn').addEventListener('click', runActiveWorksheet);
  document.getElementById('tab-catalog').addEventListener('click', () => showSidebarPanel('catalog'));
  document.getElementById('tab-history').addEventListener('click', () => showSidebarPanel('history'));
  document.getElementById('refresh-catalog').addEventListener('click', loadCatalog);
  document.getElementById('logout-btn').addEventListener('click', logout);

  loadWhoami();
  loadCatalog();
}

init();
```

- [ ] **Step 2: Local smoke check (static analysis only — full behavior verification is Task 19, against the live deployment)**

```bash
cd C:/claude/lakehouse-ui
node --check app/static/app.js
```
Expected: no syntax errors reported (this only checks the file parses as
valid JavaScript — it does not execute it or exercise DOM interactions;
Node isn't otherwise a runtime dependency of this app).

- [ ] **Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "feat: add app.js (worksheets, catalog tree, history, whoami)"
```

---

## Task 15: README update

**Files (in `lakehouse-ui`):**
- Modify: `README.md`

- [ ] **Step 1: Rewrite the README**

Full replacement for `README.md`:
```markdown
# lakehouse-ui

A small Snowflake/BigQuery-style query console for a MinIO + Iceberg +
Polaris + DuckDB lakehouse. Log in with a Polaris principal's own
credentials — Polaris's RBAC decides what you can see and do, there's no
separate user system.

Deployed via [`test-k8s-configs`](https://github.com/OmalCooray/test-k8s-configs)
(`charts/lakehouse-ui/`), image published to
`ghcr.io/omalcooray/lakehouse-ui`.

## Features

- **Login** with any Polaris principal's client ID/secret (same mechanism
  Polaris Console uses) — every query runs as that principal, authorized
  by Polaris itself.
- **Catalog browser** — a sidebar tree of namespaces and tables, scoped to
  what your principal can see. Click a table to insert its qualified name
  into the editor.
- **Worksheets** — multiple query tabs, each with its own editor and
  results.
- **Query history** — your own past queries (stored in Postgres, not just
  this browser), click one to reopen it in a new tab.
- **Syntax-highlighted editor** (CodeMirror 5, SQL mode) — highlighting
  only, no autocomplete.

Principal/role/grant management isn't here — that's
[Polaris Console](https://github.com/apache/polaris-tools/tree/main/console)'s
job.

## Run locally

```bash
pip install -r requirements-dev.txt
export POLARIS_ENDPOINT=http://localhost:8181/api/catalog        # kubectl port-forward svc/polaris 8181:8181
export POLARIS_MANAGEMENT_ENDPOINT=http://localhost:8181/api/management
export POLARIS_CATALOG=lakehouse
export POLARIS_ROOT_CLIENT_ID=root
export POLARIS_ROOT_CLIENT_SECRET=<from the polaris-root-credentials Secret>
export LAKEHOUSE_UI_DB_HOST=localhost                              # kubectl port-forward svc/polaris-postgres 5432:5432
export LAKEHOUSE_UI_DB_NAME=lakehouse_ui
export LAKEHOUSE_UI_DB_USER=lakehouse_ui
export LAKEHOUSE_UI_DB_PASSWORD=<from the polaris-postgres Secret, key LAKEHOUSE_UI_USER_PASSWORD>
uvicorn app.main:app --reload
```

Open http://localhost:8000, log in with any Polaris principal's own
client_id/client_secret (e.g. the `loader` or `lakehouse-ui` principals
created by `bootstrap/polaris-setup.sh`).

## Tests

```bash
pytest -v
```

## Environment variables

| Var | Purpose |
|---|---|
| `POLARIS_ENDPOINT` | Polaris Iceberg REST Catalog API base URL |
| `POLARIS_MANAGEMENT_ENDPOINT` | Polaris Management API base URL (principals/roles) — a *different* base path than `POLARIS_ENDPOINT`, not derived from it |
| `POLARIS_CATALOG` | Catalog name to attach (`lakehouse`) |
| `POLARIS_ROOT_CLIENT_ID` / `POLARIS_ROOT_CLIENT_SECRET` | The app's own service credential, used only by `/me` to look up a logged-in principal's roles (a regular principal can't do this for itself) |
| `LAKEHOUSE_UI_DB_HOST` / `_NAME` / `_USER` / `_PASSWORD` | Postgres connection for query history (a dedicated database on the existing `polaris-postgres` instance) |

Per-user Polaris credentials are **not** environment variables — they come
from the login form and live only in the server-side session (in-memory,
`replicas: 1`).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README for the warehouse console rebuild"
git push
```

(This task pushes — the others don't need to; pushing here means CI runs
once at the end of this repo's task sequence and catches anything the
individual `pytest`-only steps missed, e.g. a lint/import issue only CI's
`pip install -r requirements-dev.txt` on a clean checkout would catch.)

- [ ] **Step 3: Verify CI is green**

```bash
gh run list --branch main --limit 1
```
If it's red, read the log (`gh run view <id> --log-failed`), fix, commit,
push again, and recheck — don't leave this repo with failing CI at the end
of the task sequence.

---

## Task 16: `charts/polaris-postgres` — add the `lakehouse_ui` database

**Files (in `test-k8s-configs`):**
- Modify: `charts/polaris-postgres/values.yaml`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/test-k8s-configs
git switch master
git switch -c feat/lakehouse-ui-postgres-db
```

- [ ] **Step 2: Add the second `customScripts` entry**

Add to `charts/polaris-postgres/values.yaml`, inside the `postgres:` block,
alongside the existing `userDatabase:` key (do not remove or modify
`userDatabase` — that's still `polaris`'s own database):
```yaml
  # Second database on this same Postgres instance, for lakehouse-ui's
  # query history — NOT via userDatabase (that field only creates one
  # database; groundhog2k/postgres has no list form for it), so this uses
  # the same customScripts mechanism Task 9 (of the original lakehouse
  # plan) moved AWAY from for the *first* database, for the same reason
  # that doesn't apply here: userDatabase already owns the "one extra
  # database" slot. /docker-entrypoint-initdb.d scripts run once, only
  # against an empty PGDATA — same restart-safety property as
  # userDatabase's own script (see the note on it above).
  customScripts:
    02-create-lakehouse-ui-db.sh: |
      set -e
      psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
        CREATE DATABASE "$LAKEHOUSE_UI_DB";
        CREATE USER "$LAKEHOUSE_UI_USER_NAME" WITH PASSWORD '$LAKEHOUSE_UI_USER_PASSWORD';
        GRANT ALL PRIVILEGES ON DATABASE "$LAKEHOUSE_UI_DB" TO "$LAKEHOUSE_UI_USER_NAME";
        ALTER DATABASE "$LAKEHOUSE_UI_DB" OWNER TO "$LAKEHOUSE_UI_USER_NAME";
      EOSQL

  # $LAKEHOUSE_UI_DB / $LAKEHOUSE_UI_USER_NAME / $LAKEHOUSE_UI_USER_PASSWORD
  # above are plain shell variable expansion (deliberately NOT escaped —
  # an earlier draft of this task mistakenly wrote `\$LAKEHOUSE_UI_DB`,
  # which would have produced the literal text "LAKEHOUSE_UI_DB" in the
  # SQL instead of its value; fixed before this was ever run). They need
  # to actually be in the container's environment for that expansion to
  # work — confirmed via the real chart source
  # (groundhog2k/postgres 1.6.8, templates/statefulset.yaml line ~170):
  # `extraEnvSecrets` is a list of existing Secret names, each rendered as
  # a plain `envFrom: [{secretRef: {name: ...}}]` — every key in that
  # Secret becomes a container env var under its own name. Since
  # `LAKEHOUSE_UI_DB`/`LAKEHOUSE_UI_USER_NAME`/`LAKEHOUSE_UI_USER_PASSWORD`
  # are (Task 18) added as keys on this SAME `polaris-postgres` Secret,
  # pointing extraEnvSecrets at it is enough — no per-key wiring needed:
  extraEnvSecrets:
    - polaris-postgres
```

- [ ] **Step 3: Verify**

```bash
helm dependency build charts/polaris-postgres
helm lint charts/polaris-postgres
helm template polaris-postgres charts/polaris-postgres | grep -B5 -A20 "02-create-lakehouse-ui-db"
helm template polaris-postgres charts/polaris-postgres | grep -B3 -A3 "envFrom"
```
Confirm 0 lint errors, the script content renders correctly (unescaped
`$LAKEHOUSE_UI_DB` etc.), and the StatefulSet's container spec has an
`envFrom: [{secretRef: {name: polaris-postgres}}]` block (not a `env:`
list — `extraEnvSecrets` renders as `envFrom`, confirmed against the real
chart source in the comment above).

- [ ] **Step 4: Commit**

```bash
git add charts/polaris-postgres/values.yaml
git commit -m "feat: add a lakehouse_ui database on the existing polaris-postgres"
```

---

## Task 17: `charts/lakehouse-ui` — env var changes

**Files (in `test-k8s-configs`):**
- Modify: `charts/lakehouse-ui/values.yaml`
- Modify: `charts/lakehouse-ui/templates/deployment.yaml`

- [ ] **Step 1: Branch**

```bash
git switch master
git switch -c feat/lakehouse-ui-console-chart
```

- [ ] **Step 2: Update `values.yaml`**

Replace the `polaris:` block in `charts/lakehouse-ui/values.yaml` and add a
new `postgres:` block:
```yaml
polaris:
  endpoint: http://polaris.lakehouse.svc.cluster.local:8181/api/catalog
  managementEndpoint: http://polaris.lakehouse.svc.cluster.local:8181/api/management
  catalog: lakehouse
  # Secret with CLIENT_ID / CLIENT_SECRET (created out of band by
  # bootstrap/polaris-setup.sh, not in git) — the app's own root-level
  # service credential, used only by /me. Per-user login credentials are
  # NOT here — they come from the login form, never baked into the
  # Deployment.
  rootCredentialsSecret: polaris-root-credentials

postgres:
  host: polaris-postgres
  # Secret with LAKEHOUSE_UI_DB / LAKEHOUSE_UI_USER_NAME /
  # LAKEHOUSE_UI_USER_PASSWORD keys — the SAME Secret
  # charts/polaris-postgres/values.yaml's customScripts reads (one
  # secret, not two — see the design doc's correction on this).
  credentialsSecret: polaris-postgres
```
Remove the old `credentialsSecret: lakehouse-ui-polaris-credentials` line
entirely — it's replaced by the two secrets above.

- [ ] **Step 3: Update `deployment.yaml`'s `env:` block**

Replace the `env:` list in `charts/lakehouse-ui/templates/deployment.yaml`
(everything between `env:` and `livenessProbe:`) with:
```yaml
          env:
            - name: POLARIS_ENDPOINT
              value: {{ .Values.polaris.endpoint | quote }}
            - name: POLARIS_MANAGEMENT_ENDPOINT
              value: {{ .Values.polaris.managementEndpoint | quote }}
            - name: POLARIS_CATALOG
              value: {{ .Values.polaris.catalog | quote }}
            - name: POLARIS_ROOT_CLIENT_ID
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.polaris.rootCredentialsSecret }}
                  key: CLIENT_ID
            - name: POLARIS_ROOT_CLIENT_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.polaris.rootCredentialsSecret }}
                  key: CLIENT_SECRET
            - name: LAKEHOUSE_UI_DB_HOST
              value: {{ .Values.postgres.host | quote }}
            - name: LAKEHOUSE_UI_DB_NAME
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.postgres.credentialsSecret }}
                  key: LAKEHOUSE_UI_DB
            - name: LAKEHOUSE_UI_DB_USER
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.postgres.credentialsSecret }}
                  key: LAKEHOUSE_UI_USER_NAME
            - name: LAKEHOUSE_UI_DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.postgres.credentialsSecret }}
                  key: LAKEHOUSE_UI_USER_PASSWORD
```
(This drops the old `POLARIS_CLIENT_ID`/`POLARIS_CLIENT_SECRET` env vars
entirely — login replaces them.)

- [ ] **Step 4: Verify**

```bash
cd C:/claude/test-k8s-configs
helm lint charts/lakehouse-ui
helm template lakehouse-ui charts/lakehouse-ui | grep -A40 "kind: Deployment"
```
Confirm 0 lint errors and the rendered env block matches Step 3 exactly —
no `POLARIS_CLIENT_ID`/`POLARIS_CLIENT_SECRET` anywhere in the output.

- [ ] **Step 5: Commit**

```bash
git add charts/lakehouse-ui/values.yaml charts/lakehouse-ui/templates/deployment.yaml
git commit -m "feat: chart env vars for root service credential + query-history Postgres"
```

---

## Task 18 (MAIN SESSION — interactive, not a subagent): Update secrets, deploy

- [ ] Add 3 new keys to the **existing** `polaris-postgres` Secret (do not
  replace the whole Secret — add alongside `POSTGRES_PASSWORD`,
  `POSTGRES_DB`, `POSTGRES_USER_NAME`, `POSTGRES_USER_PASSWORD`):
  `LAKEHOUSE_UI_DB=lakehouse_ui`, `LAKEHOUSE_UI_USER_NAME=lakehouse_ui`,
  `LAKEHOUSE_UI_USER_PASSWORD=<generate a fresh random value>`.
- [ ] Merge the Tasks 16–17 PRs (or confirm with the user before merging,
  per established practice this session).
- [ ] Resync `polaris-postgres` (`kubectl annotate application
  polaris-postgres -n argocd argocd.argoproj.io/refresh=hard --overwrite`),
  confirm the pod restarts cleanly and the new database/user actually got
  created (`kubectl exec` in and `psql -c '\l'` / `\du`, or query via a
  scratch DuckDB-less check — plain `psql` inside the pod is simplest).
- [ ] Merge the `lakehouse-ui` repo's final PR/branch state to `main` if
  any work is still on a branch (Tasks 1–15 should already be on `main` if
  each task's implementer pushed as they went, per Task 15's step — verify
  `git log origin/main` actually has everything before proceeding).
- [ ] Confirm CI on `lakehouse-ui`'s `main` published a new image tag; get
  the exact `sha-<short>` tag (same approach as every previous image bump
  this session — `gh run view <id> --log | grep "pushing manifest"`).
- [ ] Update `environments/local/values/lakehouse-ui.yaml` with that tag,
  merge.
- [ ] Resync `lakehouse-ui`, watch it come up. It will fail to start
  cleanly until this task's earlier steps (Secret keys, Postgres database)
  are actually in place — that's expected sequencing, not a bug; diagnose
  any failure against "did the dependency it needs actually get created"
  before assuming the app code is wrong.

---

## Task 19 (MAIN SESSION — interactive, not a subagent): Verify against the Definition of Done

Work through the design doc's Definition of Done, item by item, live:

- [ ] Log in with `loader`'s credentials (`kubectl get secret
  loader-polaris-credentials -n lakehouse` for the values), run `CREATE
  TABLE lakehouse.nyc_taxi.test_x AS SELECT 1` — succeeds.
- [ ] Log out, log back in with `lakehouse-ui`'s principal's own
  credentials (`kubectl get secret lakehouse-ui-polaris-credentials -n
  lakehouse`), run the same `CREATE TABLE ...` — fails with a Polaris 403,
  surfaced as a query error in the UI, not a crash. Clean up `test_x`
  afterward (logged in as `loader` again) so it doesn't linger in the
  catalog.
- [ ] Sidebar catalog tree (logged in as either principal) matches `polaris
  tables list --catalog lakehouse --namespace nyc_taxi` via the CLI.
- [ ] Run a few queries, then `kubectl rollout restart deployment/lakehouse-ui
  -n lakehouse`; log back in; confirm your query history from before the
  restart is still there (proves it's really in Postgres).
- [ ] Log in as `loader` in one browser/session and `lakehouse-ui`'s
  principal in another (or sequentially, checking `/history` each time);
  confirm each only sees its own history.
- [ ] Confirm the editor visibly syntax-highlights SQL, and Cmd/Ctrl+Enter
  runs the active worksheet's query.
- [ ] `helm lint`/`helm template` clean for both changed charts (already
  checked per-task, but re-confirm on the final merged state); `kubectl get
  deployment lakehouse-ui -n lakehouse -o yaml | grep -i
  POLARIS_CLIENT_ID` returns nothing.
- [ ] Update `.claude/CLAUDE.md` if anything in the catalog/deployment
  tables needs a note about this change (likely nothing — no new
  Applications were added, just existing ones changed).
- [ ] Append a short "Verification results" section to the design doc
  (same pattern as the original lakehouse design's own verification
  section) — what worked as designed, anything that needed a live fix
  along the way (matching how every prior phase of this project has been
  documented).
