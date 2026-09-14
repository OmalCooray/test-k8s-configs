# lakehouse-ui Reliability & Scaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move sessions to Postgres (survive restarts, enable >1 replica),
add a query concurrency guard (the actual root cause of the benchmark's
OOM), and update the chart (2Gi memory, 2 replicas, tuned probes).

**Architecture:** Tasks 1-6 touch `lakehouse-ui` (subagent-driven, no
cluster access — full local `pytest` suite covers correctness). Task 7
touches `test-k8s-configs`'s chart (subagent-driven, `helm lint`/
`template` only). Tasks 8-10 are main-session-only: merge everything,
deploy live, verify the actual DoD from the design doc against the real
cluster.

**Tech Stack:** Python 3.12, FastAPI, psycopg (existing dependency,
already used by `app/history.py`), pytest.

---

### Task 1: Extract the shared Postgres connection helper into `app/db.py`

**Files (in `lakehouse-ui`):**
- Create: `app/db.py`
- Modify: `app/history.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/shared-db-connection-helper
```

- [ ] **Step 2: Create `app/db.py`**

This is `app/history.py`'s existing `_connect()`/`_require_env()`/
`_REQUIRED_ENV_VARS`/`ExecutableConnection`/`HistoryConfigError` moved
verbatim into a shared module — `history.py` and the new session store
both need identical connection logic, and duplicating it would drift.
`HistoryConfigError` is renamed `DBConfigError` here since it's no longer
history-specific; `history.py` keeps re-exporting `HistoryConfigError` as
an alias so nothing importing it breaks.

```python
"""Shared Postgres connection helper for lakehouse_ui's own tables
(query_history, sessions) — a dedicated database on the existing
polaris-postgres instance (see charts/polaris-postgres).

Connection info comes from environment variables (LAKEHOUSE_UI_DB_HOST/
_NAME/_USER/_PASSWORD), the same explicit-env-var pattern app.catalog
uses for POLARIS_* — no ORM.
"""
from __future__ import annotations

import os
from typing import Any, Protocol


class DBConfigError(RuntimeError):
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
        raise DBConfigError(f"missing required environment variable {name}")
    return value


def connect() -> ExecutableConnection:
    env = {name: _require_env(name) for name in _REQUIRED_ENV_VARS}
    import psycopg

    return psycopg.connect(
        host=env["LAKEHOUSE_UI_DB_HOST"],
        dbname=env["LAKEHOUSE_UI_DB_NAME"],
        user=env["LAKEHOUSE_UI_DB_USER"],
        password=env["LAKEHOUSE_UI_DB_PASSWORD"],
        connect_timeout=5,
    )
```

- [ ] **Step 3: Refactor `app/history.py` to use it**

Replace the top of `app/history.py` (everything from the module docstring
through the `_connect` function definition) with:

```python
"""Query history, stored in Postgres — a dedicated `lakehouse_ui` database
on the existing polaris-postgres instance (see charts/polaris-postgres).

Connection handling lives in app.db (shared with app.session_store).
"""
from __future__ import annotations

from app.db import DBConfigError, ExecutableConnection, connect as _connect

# Old name, kept as an alias — nothing importing HistoryConfigError from
# here should need to change.
HistoryConfigError = DBConfigError
```

Everything below that in `history.py` (`ensure_schema`, `record_query`,
`get_history`) is **unchanged** — they already call `_connect()` by that
exact name, which now resolves to the imported shared function instead of
a locally-defined one.

- [ ] **Step 4: Run the existing tests — must still pass unchanged**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_history.py tests/test_history_routes.py -v
```
Expected: all passing, identical to before this refactor (this step
changes zero behavior — if anything fails, the refactor introduced a
regression, fix it before proceeding).

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/history.py
git commit -m "refactor: extract the shared Postgres connection helper into app/db.py"
```

Do not push.

---

### Task 2: `app/session_store.py` — Postgres-backed sessions

**Files (in `lakehouse-ui`):**
- Create: `app/session_store.py`
- Create: `tests/test_session_store.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/postgres-session-store
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_session_store.py`:

```python
from datetime import datetime, timedelta, timezone

from app.session_store import Session, create_session, delete_session, ensure_schema, get_session


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """In-memory stand-in for a psycopg connection, shaped around exactly
    the SQL app.session_store issues — mirrors tests/test_history.py's
    own FakeConnection pattern for this codebase's Postgres-backed
    modules."""

    def __init__(self):
        self.executed = []
        self.committed = False
        self.closed = False
        self._table: dict[str, tuple] = {}

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("CREATE TABLE"):
            return self
        if normalized.startswith("INSERT INTO SESSIONS"):
            session_id, client_id, client_secret, principal_name, created_at = params
            self._table[session_id] = (client_id, client_secret, principal_name, created_at)
            return self
        if normalized.startswith("SELECT"):
            session_id = params[0]
            row = self._table.get(session_id)
            return FakeCursor([row] if row else [])
        if normalized.startswith("DELETE"):
            session_id = params[0]
            self._table.pop(session_id, None)
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_create_session_returns_an_id_and_stores_it(monkeypatch):
    fake = FakeConnection()

    session_id = create_session("cid", "secret", "loader", conn=fake)

    assert isinstance(session_id, str) and len(session_id) > 20
    assert session_id in fake._table


def test_get_session_returns_the_stored_session(monkeypatch):
    fake = FakeConnection()
    session_id = create_session("cid", "secret", "loader", conn=fake)

    session = get_session(session_id, conn=fake)

    assert session == Session(client_id="cid", client_secret="secret", principal_name="loader")


def test_get_session_returns_none_for_unknown_id(monkeypatch):
    fake = FakeConnection()

    assert get_session("not-a-real-id", conn=fake) is None


def test_get_session_returns_none_for_none_id(monkeypatch):
    fake = FakeConnection()

    assert get_session(None, conn=fake) is None


def test_get_session_returns_none_and_deletes_an_expired_session(monkeypatch):
    fake = FakeConnection()
    session_id = create_session("cid", "secret", "loader", conn=fake)
    # Backdate it past the 12-hour TTL.
    client_id, client_secret, principal_name, _created_at = fake._table[session_id]
    fake._table[session_id] = (
        client_id,
        client_secret,
        principal_name,
        datetime.now(timezone.utc) - timedelta(hours=13),
    )

    session = get_session(session_id, conn=fake)

    assert session is None
    assert session_id not in fake._table


def test_delete_session_removes_it(monkeypatch):
    fake = FakeConnection()
    session_id = create_session("cid", "secret", "loader", conn=fake)

    delete_session(session_id, conn=fake)

    assert session_id not in fake._table
    assert get_session(session_id, conn=fake) is None


def test_delete_session_is_a_no_op_for_none_id(monkeypatch):
    fake = FakeConnection()

    delete_session(None, conn=fake)  # must not raise


def test_create_session_owns_and_closes_its_own_connection_when_none_given(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr("app.session_store._connect", lambda: fake)

    create_session("cid", "secret", "loader")

    assert fake.committed is True
    assert fake.closed is True


def test_get_session_owns_and_closes_its_own_connection_when_none_given(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr("app.session_store._connect", lambda: fake)
    session_id = create_session("cid", "secret", "loader", conn=fake)
    fake.closed = False  # reset after the setup call above

    get_session(session_id)

    assert fake.closed is True


def test_ensure_schema_creates_the_table(monkeypatch):
    fake = FakeConnection()

    ensure_schema(conn=fake)

    assert any("CREATE TABLE" in sql.upper() for sql, _ in fake.executed)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_session_store.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.session_store'`.

- [ ] **Step 4: Write `app/session_store.py`**

```python
"""Session store for logged-in Polaris principals, backed by Postgres —
a `sessions` table in the same `lakehouse_ui` database query_history
already uses (see charts/polaris-postgres, and app.db for the shared
connection helper).

Replaces the old in-memory dict (app/session.py, now removed): a pod
restart no longer logs everyone out, and more than one replica can now
share sessions — confirmed live as the actual cause of a real incident
during the TPC-H load test, where an OOM-triggered restart wiped every
in-flight session.

Sessions expire after SESSION_TTL_HOURS, checked lazily at lookup time —
no separate cleanup job.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.db import ExecutableConnection, connect as _connect

SESSION_TTL_HOURS = 12


@dataclass
class Session:
    client_id: str
    client_secret: str = field(repr=False)
    principal_name: str


def ensure_schema(conn: ExecutableConnection | None = None) -> None:
    """Create the sessions table if it doesn't already exist. Safe to call
    on every app startup, same owns-connection contract as
    app.history.ensure_schema."""
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id     TEXT PRIMARY KEY,
                client_id      TEXT NOT NULL,
                client_secret  TEXT NOT NULL,
                principal_name TEXT NOT NULL,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def create_session(
    client_id: str,
    client_secret: str,
    principal_name: str,
    conn: ExecutableConnection | None = None,
) -> str:
    """Create a session, returning its ID (the cookie value)."""
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    try:
        session_id = secrets.token_urlsafe(32)
        conn.execute(
            """
            INSERT INTO sessions (session_id, client_id, client_secret, principal_name, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, client_id, client_secret, principal_name, datetime.now(timezone.utc)),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()
    return session_id


def get_session(session_id: str | None, conn: ExecutableConnection | None = None) -> Session | None:
    if session_id is None:
        return None
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    try:
        cursor = conn.execute(
            "SELECT client_id, client_secret, principal_name, created_at FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        client_id, client_secret, principal_name, created_at = row
        if datetime.now(timezone.utc) - created_at > timedelta(hours=SESSION_TTL_HOURS):
            conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            if owns_conn:
                conn.commit()
            return None
        return Session(client_id=client_id, client_secret=client_secret, principal_name=principal_name)
    finally:
        if owns_conn:
            conn.close()


def delete_session(session_id: str | None, conn: ExecutableConnection | None = None) -> None:
    if session_id is None:
        return
    owns_conn = conn is None
    if conn is None:
        conn = _connect()
    try:
        conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_session_store.py -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add app/session_store.py tests/test_session_store.py
git commit -m "feat: add the Postgres-backed session store"
```

Do not push.

---

### Task 3: Wire `app/main.py` to the new session store + add the query concurrency guard

**Files (in `lakehouse-ui`):**
- Modify: `app/main.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/wire-session-store-and-concurrency-guard
```

- [ ] **Step 2: Swap the session import**

Change:
```python
from app.session import Session, create_session, delete_session, get_session
```
to:
```python
from app.session_store import Session, create_session, delete_session, get_session
```

- [ ] **Step 3: Add `threading` import and the semaphore**

Add near the top of the file, alongside the other stdlib imports:
```python
import threading
```

Add right after the `MAX_RESULT_ROWS = 10_000` constant (keep that
constant and its comment exactly as they are):
```python
# Caps concurrent DuckDB executions per pod. Confirmed live (2026-09-14):
# with no admission control at all, 5 concurrent large joins over
# lineitem-scale data blew well past the container's memory limit and
# OOMKilled the pod — this bounds it instead of just raising the limit
# and hoping. A request that can't get a slot immediately fails fast with
# 429 rather than queueing (a queue just delays the same OOM).
MAX_CONCURRENT_QUERIES = int(os.environ.get("MAX_CONCURRENT_QUERIES", "3"))
_query_semaphore = threading.Semaphore(MAX_CONCURRENT_QUERIES)
```

- [ ] **Step 4: Add the sessions schema init to the startup event**

Change:
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
to:
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
    try:
        ensure_session_schema()
    except Exception as exc:  # noqa: BLE001 — same reasoning as above;
        # login/query will fail per-request until Postgres is reachable,
        # rather than crash-looping the whole app at startup.
        print(f"WARNING: could not initialize sessions schema at startup: {exc}")
```

This needs `ensure_session_schema` imported and distinguished from
`history.py`'s `ensure_schema` (both modules export a function with that
same name — importing both unqualified would collide). Change the
existing history import line:
```python
from app.history import ensure_schema, get_history, record_query
```
to:
```python
from app.history import ensure_schema, get_history, record_query
from app.session_store import ensure_schema as ensure_session_schema
```
and merge that second import into the session_store import line from
Step 2 above — the final imports should read:
```python
from app.session_store import (
    Session,
    create_session,
    delete_session,
    ensure_schema as ensure_session_schema,
    get_session,
)
```
(replacing the single-line import from Step 2 with this multi-line form).

- [ ] **Step 5: Add the concurrency guard to `/query`**

Change:
```python
@app.post("/query", response_model=QueryResponse)
def run_query(
    request: QueryRequest, session: Session = Depends(require_session)
) -> QueryResponse:
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="sql must not be empty")

    try:
        connection = build_connection(session.client_id, session.client_secret)
```
to:
```python
@app.post("/query", response_model=QueryResponse)
def run_query(
    request: QueryRequest, session: Session = Depends(require_session)
) -> QueryResponse:
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="sql must not be empty")

    if not _query_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429, detail="Too many concurrent queries — try again in a moment"
        )
    try:
        return _run_query_body(request, session)
    finally:
        _query_semaphore.release()


def _run_query_body(request: QueryRequest, session: Session) -> QueryResponse:
    try:
        connection = build_connection(session.client_id, session.client_secret)
```
Every line of the function body **after** that point (the rest of the
old `run_query`, starting from `except CatalogConfigError as exc:` all
the way to the final `return QueryResponse(columns=columns, rows=rows,
truncated=truncated)`) stays **exactly as it is today** — it's now the
body of `_run_query_body` instead of `run_query`, unindented by zero
(it's still one level deep in a function, just a different one), no
other changes.

- [ ] **Step 6: Run the full test suite**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: significant failures at this point — every test file that
imports from `app.session` directly will fail with `ModuleNotFoundError`
(since Task 5 hasn't removed `app/session.py` yet, this specific error
shouldn't occur, but tests calling the real `create_session`/`get_session`
against a live Postgres they don't have will fail or hang). **This is
expected** — Task 4 fixes the test files. Confirm the failures are
concentrated in `tests/test_auth_routes.py`, `tests/test_query.py`,
`tests/test_me_route.py`, `tests/test_index.py`,
`tests/test_catalog_routes.py`, `tests/test_history_routes.py` (the 6
files Task 4 touches) and that unrelated files (`test_db.py` doesn't
exist yet, `test_history.py`, `test_session_store.py`, `test_catalog.py`,
`test_polaris_auth.py`, `test_polaris_client.py`, `test_healthz.py`) still
pass cleanly.

- [ ] **Step 7: Commit**

```bash
git add app/main.py
git commit -m "feat: wire main.py to the Postgres session store, add a query concurrency guard"
```

Do not push. (Expected to still have a red test suite — Task 4 fixes it.
Note this explicitly in your report so the next task's implementer isn't
surprised by red tests at the start.)

---

### Task 4: Fix the 6 test files that assumed an in-memory session store

**Files (in `lakehouse-ui`):**
- Create: `tests/conftest.py`
- Modify: `tests/test_auth_routes.py`
- Modify: `tests/test_query.py`
- Modify: `tests/test_me_route.py`
- Modify: `tests/test_index.py`
- Modify: `tests/test_catalog_routes.py`
- Modify: `tests/test_history_routes.py`

**Context:** These 6 files all set up a logged-in HTTP session for tests
that are really about something else (query execution, catalog listing,
history, etc.) — they shouldn't need a real Postgres connection just to
get a valid cookie. Before this task, `test_auth_routes.py` and the other
5 files' `_logged_in_cookie()` helpers called the real `create_session`/
`get_session` (now Postgres-backed) directly. The fix: one shared,
**autouse** pytest fixture (`tests/conftest.py` — this repo has no
`conftest.py` yet; this is the first one) that monkeypatches
`app.main`'s imported `create_session`/`get_session`/`delete_session`
names to a simple in-memory dict for the duration of each test. This
repo already tests `app.session_store` directly and thoroughly (Task 2)
— these 6 files only need *a* valid session, not to re-prove Postgres
works.

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c fix/test-session-fakes
```

- [ ] **Step 2: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures.

fake_session_store replaces app.main's imported create_session/
get_session/delete_session with a simple in-memory dict for every test
in this suite (autouse — no test needs to opt in). app.session_store's
own Postgres-backed logic is tested directly and thoroughly in
tests/test_session_store.py; nothing outside that file should need a
real Postgres connection just to get a valid session for an unrelated
route test.
"""
import pytest

import app.main as main_module
from app.session_store import Session


@pytest.fixture(autouse=True)
def fake_session_store(monkeypatch):
    store: dict[str, Session] = {}

    def fake_create_session(client_id, client_secret, principal_name):
        session_id = f"test-session-{len(store)}-{principal_name}"
        store[session_id] = Session(
            client_id=client_id, client_secret=client_secret, principal_name=principal_name
        )
        return session_id

    def fake_get_session(session_id):
        return store.get(session_id) if session_id else None

    def fake_delete_session(session_id):
        if session_id is not None:
            store.pop(session_id, None)

    monkeypatch.setattr(main_module, "create_session", fake_create_session)
    monkeypatch.setattr(main_module, "get_session", fake_get_session)
    monkeypatch.setattr(main_module, "delete_session", fake_delete_session)
```

- [ ] **Step 3: Fix `tests/test_query.py`, `tests/test_me_route.py`,
  `tests/test_index.py`, `tests/test_catalog_routes.py`,
  `tests/test_history_routes.py`**

Each of these 5 files has an identical-shaped helper near the top:
```python
def _logged_in_cookie(principal="loader"):
    session_id = create_session("cid", "secret", principal)
    return {"lakehouse_session": session_id}
```
(some pass no `principal` param, since only `test_me_route.py` and
`test_history_routes.py` use a non-default principal — check each file's
actual signature rather than assuming).

For each file:
1. Remove its `from app.session import create_session` (or
   `from app.session_store import create_session`, if some copy already
   got updated) import line entirely.
2. Change the helper's body from `session_id = create_session(...)` to
   `session_id = main_module.create_session(...)` — every one of these 5
   files already has `import app.main as main_module` at the top (used
   for their other `monkeypatch.setattr(main_module, ...)` calls), so no
   new import is needed.

Worked example for `tests/test_query.py` — before:
```python
import app.main as main_module
from app.catalog import CatalogConfigError
from fastapi.testclient import TestClient

from app.main import app
from app.session import create_session

client = TestClient(app)


class FakeResult:
    ...


def _logged_in_cookie():
    session_id = create_session("cid", "secret", "loader")
    return {"lakehouse_session": session_id}
```
after:
```python
import app.main as main_module
from app.catalog import CatalogConfigError
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class FakeResult:
    ...


def _logged_in_cookie():
    session_id = main_module.create_session("cid", "secret", "loader")
    return {"lakehouse_session": session_id}
```
Apply the same two changes (drop the direct import, prefix the
`create_session(...)` call with `main_module.`) to the other 4 files.
Nothing else in any of these 5 files changes — every test function body,
every other `monkeypatch.setattr` call, every assertion stays exactly as
it is.

- [ ] **Step 4: Fix `tests/test_auth_routes.py`**

This file is different: it tests `/login` and `/logout` themselves, so it
calls `get_session(...)` directly (not through a `_logged_in_cookie()`
helper) to assert a session was actually created/removed. Change:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_auth import LoginError
from app.session import get_session

client = TestClient(app)
```
to:
```python
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app
from app.polaris_auth import LoginError

client = TestClient(app)
```
(drop the `from app.session import get_session` import), then change
every `get_session(cookie)` call site in this file to
`main_module.get_session(cookie)`. There are exactly 2 call sites:
`test_login_success_sets_cookie_and_returns_principal` (asserts
`get_session(cookie).principal_name == "loader"`) and
`test_logout_clears_the_session` (asserts
`get_session(cookie) is None`) — update both, nothing else in the file
changes.

- [ ] **Step 5: Run the full test suite**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing now, including everything from Task 3's still-red
suite.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_auth_routes.py tests/test_query.py tests/test_me_route.py tests/test_index.py tests/test_catalog_routes.py tests/test_history_routes.py
git commit -m "fix: give route tests a fake session store instead of hitting Postgres"
```

Do not push.

---

### Task 5: Add a test for the concurrency guard's 429 path

**Files (in `lakehouse-ui`):**
- Modify: `tests/test_query.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c feat/test-query-concurrency-guard
```

- [ ] **Step 2: Write the test**

Add to `tests/test_query.py` (after the existing truncation tests are a
reasonable spot, but anywhere in the file is fine):

```python
def test_query_returns_429_when_the_concurrency_limit_is_already_held(monkeypatch):
    monkeypatch.setattr(main_module, "_query_semaphore", __import__("threading").Semaphore(0))
    # A semaphore initialized to 0 has no permits to give — the very
    # first acquire attempt fails, exactly like every slot already being
    # held by other in-flight queries.

    response = client.post("/query", json={"sql": "SELECT 1"}, cookies=_logged_in_cookie())

    assert response.status_code == 429
    assert "concurrent" in response.json()["detail"].lower()


def test_query_releases_its_concurrency_slot_even_when_the_query_errors(monkeypatch):
    import threading

    class RaisingConnection:
        def execute(self, sql):
            raise ValueError("boom")

    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: RaisingConnection()
    )
    monkeypatch.setattr(main_module, "record_query", lambda **kwargs: None)
    semaphore = threading.Semaphore(1)
    monkeypatch.setattr(main_module, "_query_semaphore", semaphore)

    response = client.post("/query", json={"sql": "SELECT 1"}, cookies=_logged_in_cookie())

    assert response.status_code == 400  # the query's own error, not a 429
    # If the slot wasn't released, this acquire would fail (already at 0).
    assert semaphore.acquire(blocking=False) is True
```

- [ ] **Step 3: Run the tests**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/test_query.py -v
```
Expected: all passing, including the 2 new tests.

- [ ] **Step 4: Run the full suite one more time**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing.

- [ ] **Step 5: Commit**

```bash
git add tests/test_query.py
git commit -m "test: cover the /query concurrency guard's 429 and slot-release paths"
```

Do not push.

---

### Task 6: Remove the superseded in-memory session module

**Files (in `lakehouse-ui`):**
- Delete: `app/session.py`
- Delete: `tests/test_session.py`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/lakehouse-ui
git switch main
git pull
git switch -c chore/remove-in-memory-session-module
```

- [ ] **Step 2: Confirm nothing still references them**

```bash
grep -rn "from app.session import\|app\.session\b" --include="*.py" .
```
Expected: no matches (or only `app/catalog.py`'s comment mentioning "see
app.session" — if so, update that comment to say "see
app.session_store" while you're here, small drive-by fix, not worth its
own task).

- [ ] **Step 3: Delete the files**

```bash
git rm app/session.py tests/test_session.py
```

- [ ] **Step 4: Run the full test suite**

```bash
PATH="C:/claude/lakehouse-ui/.venv/Scripts:$PATH" pytest tests/ -v
```
Expected: all passing (this file's own tests are superseded by
`tests/test_session_store.py`, already covering equivalent behavior plus
the new TTL/Postgres logic).

- [ ] **Step 5: Commit and push**

```bash
git commit -m "chore: remove the superseded in-memory session module"
git push
```

This is the task in this repo's sequence that pushes (matching the
pattern from the original build — push once at the end so CI runs on the
accumulated work, not once per task).

- [ ] **Step 6: Verify CI is green**

```bash
gh run list --branch main --limit 1
```
If red, `gh run view <id> --log-failed`, fix, commit, push, recheck.
Don't leave this repo with failing CI.

---

### Task 7: Chart changes — memory, replicas, probes

**Files (in `test-k8s-configs`):**
- Modify: `charts/lakehouse-ui/values.yaml`
- Modify: `charts/lakehouse-ui/templates/deployment.yaml`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/test-k8s-configs
git switch master
git pull
git switch -c feat/lakehouse-ui-reliability-chart
```

- [ ] **Step 2: Update `values.yaml`**

Read the current `charts/lakehouse-ui/values.yaml` first to find its
existing `resources:` block and confirm whether `replicaCount` already
exists as a key (add it if not). Update (or add) these top-level keys:

```yaml
replicaCount: 2

resources:
  requests: {cpu: 100m, memory: 512Mi}
  limits: {cpu: 500m, memory: 2Gi}
```

(`cpu` values unchanged from before — only the memory limit and
`replicaCount` are new/changed here. If the file's existing `resources:`
block has different key names or structure, match its existing style
rather than the exact block above — the values that matter are `memory`
limit = `2Gi`, `memory` request = `512Mi`, `replicaCount` = `2`.)

- [ ] **Step 3: Update `deployment.yaml`**

Read the current `charts/lakehouse-ui/templates/deployment.yaml` first.
Confirm `spec.replicas` reads `{{ .Values.replicaCount }}` (add that if
the chart currently hardcodes `replicas: 1` or omits the field, which
defaults to 1). Add explicit `livenessProbe`/`readinessProbe` blocks if
none exist yet, or update their timing if they do, to:

```yaml
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
```
Match the file's existing indentation level for the container spec
rather than assuming the exact indentation above. `/healthz` and `port:
http` (or whatever the existing port name is — check the `ports:` block)
should already match what's there today if probes already exist; this
step is about the *timing* fields specifically.

- [ ] **Step 4: Verify**

```bash
cd C:/claude/test-k8s-configs
helm lint charts/lakehouse-ui
helm template lakehouse-ui charts/lakehouse-ui | grep -A5 "replicas:"
helm template lakehouse-ui charts/lakehouse-ui | grep -B2 -A15 "livenessProbe:"
helm template lakehouse-ui charts/lakehouse-ui | grep -A3 "limits:"
```
Confirm: 0 lint errors; rendered Deployment shows `replicas: 2`; the
liveness/readiness blocks show the 4 timing fields above; the memory
limit renders as `2Gi`.

- [ ] **Step 5: Commit**

```bash
git add charts/lakehouse-ui/values.yaml charts/lakehouse-ui/templates/deployment.yaml
git commit -m "feat: raise lakehouse-ui memory limit, run 2 replicas, tune probe timing"
```

Do not push.

---

### Task 8 (MAIN SESSION — interactive, not a subagent): Merge and deploy

- [ ] Merge Tasks 1-5's branches into `lakehouse-ui`'s `main` in order
  (each depends conceptually on the last, though as separate local
  branches off the same base they may need sequential rebasing/merging
  rather than independent parallel merges — check for conflicts,
  especially in `app/main.py` which Tasks 3 and 5 both touch). Task 6
  already pushed directly.
- [ ] Confirm `pytest tests/ -v` is fully green on `main` after all
  merges land together (a passing suite per-branch doesn't guarantee a
  passing suite once they're combined).
- [ ] Confirm CI on `main` is green; get the new image tag
  (`gh run view <id> --log | grep "pushing manifest"`).
- [ ] Merge Task 7's chart branch in `test-k8s-configs` (confirm with the
  user before merging, per established practice this session).
- [ ] Update `environments/local/values/lakehouse-ui.yaml` with the new
  image tag, commit, merge to master.
- [ ] Resync `lakehouse-ui` (`kubectl annotate application lakehouse-ui -n
  argocd argocd.argoproj.io/refresh=hard --overwrite`), watch both new
  pods come up `Running 1/1` — this is a live rollout, watch it happen,
  don't fire-and-forget.

---

### Task 9 (MAIN SESSION — interactive, not a subagent): Verify against the design doc's Definition of Done

- [ ] Log in, confirm the session cookie still works after
  `kubectl rollout restart deployment/lakehouse-ui -n lakehouse` (the
  actual scenario that broke mid-benchmark) — session survives, no
  re-login needed.
- [ ] Confirm 2 replicas both `Ready`; `kubectl delete pod` on one,
  confirm a session created against *the other* replica keeps working
  throughout (proves sessions aren't pod-local anymore).
- [ ] Confirm the 4th concurrent query (with 3 already in flight) gets a
  429 — either by re-running `benchmark/load_test.py` at a concurrency
  level above 3, or a smaller manual check (e.g. 4 near-simultaneous
  `curl` calls to a deliberately slow query).
- [ ] Rerun `benchmark/load_test.py` at concurrency=5 specifically (the
  level that OOMKilled before): confirm no `CrashLoopBackOff`, no
  `OOMKilled` events in `kubectl get events -n lakehouse`. Some 429s are
  an acceptable, correct outcome at that concurrency, not a failure.
- [ ] `helm lint`/`helm template` clean on the final merged
  `charts/lakehouse-ui` state (already checked per-task, re-confirm on
  what's actually live).
- [ ] `kubectl get deployment lakehouse-ui -n lakehouse -o yaml` shows
  `replicas: 2` and the `2Gi` memory limit.

---

### Task 10 (MAIN SESSION — interactive, not a subagent): Append verification results to the design doc

- [ ] Append a "Verification results" section to
  `docs/superpowers/specs/2026-09-14-lakehouse-ui-reliability-scaling-design.md`
  (same pattern as every prior phase this session) — what worked as
  designed, anything that needed a live fix along the way, the actual
  load-test-at-concurrency-5 result post-fix.
- [ ] Commit and push:
  ```bash
  git add docs/superpowers/specs/2026-09-14-lakehouse-ui-reliability-scaling-design.md
  git commit -m "docs: append verification results to the reliability & scaling design doc"
  git push
  ```
