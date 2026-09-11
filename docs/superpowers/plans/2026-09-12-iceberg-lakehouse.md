# Iceberg Lakehouse (MinIO + Iceberg + Polaris + DuckDB) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement Tasks 1–13. Tasks 14–17 touch the live cluster and must be driven
> from the main session (interactive, watched, fixed live) — do not dispatch them
> to a subagent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an open-source data lakehouse (MinIO → Iceberg → Polaris →
DuckDB) on the local Kubernetes cluster via `argocd-gitops-plugin`, prove the
read/write path with a real dataset, and expose it through a small self-built
query UI (`lakehouse-ui`).

**Architecture:** Two repos. `test-k8s-configs` gets 4 new Argo CD Applications
(`minio`, `polaris-postgres`, `polaris`, `lakehouse-ui`) all in namespace
`lakehouse`. A new repo `lakehouse-ui` holds a small FastAPI app (one `/query`
endpoint, one static HTML page) that runs DuckDB in-process, attached to the
Polaris REST catalog. Polaris realm bootstrap and catalog/principal setup are
one-time runbook scripts (`bootstrap/polaris-setup.sh`), not Argo CD hooks —
`apache/polaris-admin-tool bootstrap`'s rerun-safety against an
already-bootstrapped realm is not documented/guaranteed, unlike the Airflow
hook-Job pattern already used elsewhere in this repo.

**Tech stack:** FastAPI + DuckDB 1.5.3 (Python 3.12) for `lakehouse-ui`; MinIO
Helm chart 5.4.0; groundhog2k/postgres 1.6.8; Apache Polaris Helm chart 1.7.0;
`apache-polaris` CLI (PyPI) for catalog/principal setup.

**Design doc:** `docs/superpowers/specs/2026-09-11-iceberg-lakehouse-design.md`

---

## Task 1: `lakehouse-ui` repo scaffold + health endpoint

**Files (new repo, cloned to `C:\claude\lakehouse-ui`):**
- Create: `lakehouse-ui/requirements.txt`
- Create: `lakehouse-ui/requirements-dev.txt`
- Create: `lakehouse-ui/.gitignore`
- Create: `lakehouse-ui/app/__init__.py`
- Create: `lakehouse-ui/app/main.py`
- Test: `lakehouse-ui/tests/test_healthz.py`

- [ ] **Step 1: Create the GitHub repo and clone it**

```bash
gh repo create OmalCooray/lakehouse-ui --public \
  --description "Minimal query UI for a MinIO/Iceberg/Polaris/DuckDB lakehouse"
git clone git@github.com:OmalCooray/lakehouse-ui.git C:/claude/lakehouse-ui
cd C:/claude/lakehouse-ui
```

- [ ] **Step 2: Scaffold the project files**

`requirements.txt`:
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
duckdb==1.5.3
pydantic==2.10.4
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest==8.3.4
httpx==0.28.1
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
.venv/
```

`app/__init__.py`: (empty file)

- [ ] **Step 3: Write the failing test**

`tests/test_healthz.py`:
```python
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_returns_ok():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it fails**

```bash
pip install -r requirements-dev.txt
pytest tests/test_healthz.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 5: Write minimal implementation**

`app/main.py`:
```python
"""lakehouse-ui: a minimal query UI for the MinIO/Iceberg/Polaris/DuckDB stack."""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="lakehouse-ui")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
pytest tests/test_healthz.py -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-dev.txt .gitignore app/ tests/
git commit -m "feat: scaffold FastAPI app with a healthz endpoint"
```

---

## Task 2: Polaris catalog connection (`app/catalog.py`)

**Files:**
- Create: `lakehouse-ui/app/catalog.py`
- Test: `lakehouse-ui/tests/test_catalog.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_catalog.py`:
```python
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
    "POLARIS_CLIENT_ID": "lakehouse-ui",
    "POLARIS_CLIENT_SECRET": "s3cr3t",
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
        build_connection(FakeConnection())


def test_build_connection_requires_client_id(monkeypatch):
    _set_env(monkeypatch, POLARIS_CLIENT_ID=None)
    with pytest.raises(CatalogConfigError, match="POLARIS_CLIENT_ID"):
        build_connection(FakeConnection())


def test_build_connection_installs_extensions_and_attaches(monkeypatch):
    _set_env(monkeypatch)
    fake = FakeConnection()

    result = build_connection(fake)

    assert result is fake
    joined = "\n".join(fake.executed)
    assert "INSTALL iceberg" in joined
    assert "LOAD iceberg" in joined
    assert "INSTALL httpfs" in joined
    assert "LOAD httpfs" in joined
    assert "CLIENT_ID 'lakehouse-ui'" in joined
    assert "CLIENT_SECRET 's3cr3t'" in joined
    assert "ATTACH 'lakehouse' AS lakehouse" in joined
    assert (
        "ENDPOINT 'http://polaris.lakehouse.svc.cluster.local:8181/api/catalog'"
        in joined
    )
    assert "ACCESS_DELEGATION_MODE 'vended_credentials'" in joined


def test_build_connection_escapes_single_quotes_in_secret(monkeypatch):
    _set_env(monkeypatch, POLARIS_CLIENT_SECRET="o'brien")
    fake = FakeConnection()

    build_connection(fake)

    assert "CLIENT_SECRET 'o''brien'" in "\n".join(fake.executed)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_catalog.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.catalog'`.

- [ ] **Step 3: Write minimal implementation**

`app/catalog.py`:
```python
"""Build a DuckDB connection attached to the Polaris Iceberg REST catalog.

Configuration is entirely via environment variables so the same code runs
locally (port-forwarded) and in-cluster (values mounted from a Secret):

  POLARIS_ENDPOINT       e.g. http://polaris.lakehouse.svc.cluster.local:8181/api/catalog
  POLARIS_CLIENT_ID      the scoped principal's client id
  POLARIS_CLIENT_SECRET  the scoped principal's client secret
  POLARIS_CATALOG        e.g. lakehouse
"""
from __future__ import annotations

import os
from typing import Any, Protocol


class CatalogConfigError(RuntimeError):
    """Raised when a required POLARIS_* environment variable is missing."""


class ExecutableConnection(Protocol):
    def execute(self, sql: str) -> Any: ...


_REQUIRED_ENV_VARS = (
    "POLARIS_ENDPOINT",
    "POLARIS_CLIENT_ID",
    "POLARIS_CLIENT_SECRET",
    "POLARIS_CATALOG",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CatalogConfigError(f"missing required environment variable {name}")
    return value


def _sql_quote(value: str) -> str:
    """Escape a value for embedding as a single-quoted SQL string literal."""
    return value.replace("'", "''")


def build_connection(conn: ExecutableConnection | None = None) -> ExecutableConnection:
    """Return a DuckDB connection with the Polaris catalog attached.

    Pass an existing `conn` to attach onto it instead of opening a fresh
    in-memory DuckDB connection — this is what makes the attach logic
    testable with a stub in place of real DuckDB/network calls.
    """
    endpoint, client_id, client_secret, catalog = (
        _require_env(name) for name in _REQUIRED_ENV_VARS
    )

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
        f"CLIENT_SECRET '{_sql_quote(client_secret)}'"
        ")"
    )
    conn.execute(
        f"ATTACH '{_sql_quote(catalog)}' AS {catalog} ("
        "TYPE iceberg, "
        f"ENDPOINT '{_sql_quote(endpoint)}', "
        "ACCESS_DELEGATION_MODE 'vended_credentials'"
        ")"
    )
    return conn
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_catalog.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/catalog.py tests/test_catalog.py
git commit -m "feat: build a DuckDB connection attached to the Polaris catalog"
```

---

## Task 3: `POST /query` endpoint

**Files:**
- Modify: `lakehouse-ui/app/main.py`
- Test: `lakehouse-ui/tests/test_query.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_query.py`:
```python
import app.main as main_module
from app.catalog import CatalogConfigError
from fastapi.testclient import TestClient

from app.main import app

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


def test_query_runs_select_and_returns_rows(monkeypatch):
    fake = FakeConnection(["id", "name"], [[1, "a"], [2, "b"]])
    monkeypatch.setattr(main_module, "build_connection", lambda: fake)

    response = client.post("/query", json={"sql": "SELECT * FROM nyc_taxi.trips"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"columns": ["id", "name"], "rows": [[1, "a"], [2, "b"]]}
    assert fake.executed == ["SELECT * FROM nyc_taxi.trips"]


def test_query_rejects_empty_sql():
    response = client.post("/query", json={"sql": "   "})
    assert response.status_code == 400


def test_query_rejects_write_statements(monkeypatch):
    def fail_if_called():
        raise AssertionError("should not connect for a rejected statement")

    monkeypatch.setattr(main_module, "build_connection", fail_if_called)

    response = client.post("/query", json={"sql": "DROP TABLE nyc_taxi.trips"})

    assert response.status_code == 400
    assert "read-only" in response.json()["detail"]


def test_query_accepts_with_and_show_statements(monkeypatch):
    fake = FakeConnection(["x"], [[1]])
    monkeypatch.setattr(main_module, "build_connection", lambda: fake)

    for sql in ["WITH t AS (SELECT 1 AS x) SELECT * FROM t", "SHOW TABLES"]:
        response = client.post("/query", json={"sql": sql})
        assert response.status_code == 200, sql


def test_query_returns_500_on_catalog_config_error(monkeypatch):
    def raise_config_error():
        raise CatalogConfigError("missing required environment variable POLARIS_ENDPOINT")

    monkeypatch.setattr(main_module, "build_connection", raise_config_error)

    response = client.post("/query", json={"sql": "SELECT 1"})

    assert response.status_code == 500
    assert "POLARIS_ENDPOINT" in response.json()["detail"]


def test_query_returns_400_on_duckdb_error(monkeypatch):
    class RaisingConnection:
        def execute(self, sql):
            raise ValueError("Catalog Error: Table with name trips does not exist!")

    monkeypatch.setattr(main_module, "build_connection", lambda: RaisingConnection())

    response = client.post("/query", json={"sql": "SELECT * FROM nyc_taxi.trips"})

    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_query.py -v
```
Expected: FAIL — `/query` returns 404 (route does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Replace `app/main.py` with:
```python
"""lakehouse-ui: a minimal query UI for the MinIO/Iceberg/Polaris/DuckDB stack."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.catalog import CatalogConfigError, build_connection

app = FastAPI(title="lakehouse-ui")

_READ_ONLY_PREFIXES = ("select", "with", "show", "describe", "explain", "pragma", "call")


class QueryRequest(BaseModel):
    sql: str


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]


def _is_read_only(sql: str) -> bool:
    stripped = sql.strip()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].lower()
    return first_word in _READ_ONLY_PREFIXES


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
def run_query(request: QueryRequest) -> QueryResponse:
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="sql must not be empty")
    if not _is_read_only(request.sql):
        raise HTTPException(
            status_code=400,
            detail="only read-only statements are allowed "
            f"({', '.join(_READ_ONLY_PREFIXES)})",
        )

    try:
        connection = build_connection()
    except CatalogConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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
Expected: PASS (all tests across Tasks 1–3).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_query.py
git commit -m "feat: add read-only POST /query endpoint"
```

---

## Task 4: Static query page

**Files:**
- Create: `lakehouse-ui/app/static/index.html`
- Modify: `lakehouse-ui/app/main.py`
- Test: `lakehouse-ui/tests/test_index.py`

- [ ] **Step 1: Write the failing test**

`tests/test_index.py`:
```python
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_serves_the_query_page():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<textarea id="sql"' in response.text
    assert 'id="run"' in response.text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_index.py -v
```
Expected: FAIL with 404 (no `/` route yet).

- [ ] **Step 3: Write the page and wire the route**

`app/static/index.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>lakehouse-ui</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 960px; }
    textarea { width: 100%; height: 8rem; font-family: monospace; font-size: 0.9rem; }
    button { margin-top: 0.5rem; padding: 0.5rem 1.5rem; }
    table { border-collapse: collapse; margin-top: 1rem; width: 100%; }
    th, td { border: 1px solid #ccc; padding: 0.3rem 0.6rem; text-align: left; }
    #error { color: #b00020; margin-top: 1rem; white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>lakehouse-ui</h1>
  <p>Query the <code>lakehouse</code> Iceberg catalog. Read-only.</p>
  <textarea id="sql" placeholder="SELECT * FROM nyc_taxi.trips LIMIT 10"></textarea>
  <br>
  <button id="run">Run</button>
  <div id="error"></div>
  <table id="results"></table>

  <script>
    const runButton = document.getElementById('run');
    const sqlBox = document.getElementById('sql');
    const errorBox = document.getElementById('error');
    const resultsTable = document.getElementById('results');

    async function runQuery() {
      errorBox.textContent = '';
      resultsTable.innerHTML = '';
      const sql = sqlBox.value.trim();
      if (!sql) return;

      let response;
      try {
        response = await fetch('/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sql })
        });
      } catch (err) {
        errorBox.textContent = 'Request failed: ' + err;
        return;
      }

      const body = await response.json();
      if (!response.ok) {
        errorBox.textContent = body.detail || 'Query failed';
        return;
      }
      renderResults(body.columns, body.rows);
    }

    function renderResults(columns, rows) {
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      columns.forEach((c) => {
        const th = document.createElement('th');
        th.textContent = c;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      resultsTable.appendChild(thead);

      const tbody = document.createElement('tbody');
      rows.forEach((row) => {
        const tr = document.createElement('tr');
        row.forEach((value) => {
          const td = document.createElement('td');
          td.textContent = value === null ? 'NULL' : String(value);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      resultsTable.appendChild(tbody);
    }

    runButton.addEventListener('click', runQuery);
    sqlBox.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) runQuery();
    });
  </script>
</body>
</html>
```

Add to `app/main.py` (after the `app = FastAPI(...)` line):
```python
from pathlib import Path

from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "static"
```

And add the route (after the `healthz` route):
```python
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/ -v
```
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html app/main.py tests/test_index.py
git commit -m "feat: serve a static query page at /"
```

---

## Task 5: Dockerfile

**Files:**
- Create: `lakehouse-ui/Dockerfile`
- Create: `lakehouse-ui/.dockerignore`

- [ ] **Step 1: Write the Dockerfile**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`.dockerignore`:
```
tests/
.git/
.github/
__pycache__/
*.pyc
.pytest_cache/
requirements-dev.txt
README.md
.gitignore
```

- [ ] **Step 2: Build and smoke-test the image**

```bash
docker build -t lakehouse-ui:local .
docker run -d --name lakehouse-ui-smoke -p 8000:8000 \
  -e POLARIS_ENDPOINT=http://example.invalid \
  -e POLARIS_CLIENT_ID=x -e POLARIS_CLIENT_SECRET=y -e POLARIS_CATALOG=lakehouse \
  lakehouse-ui:local
sleep 3
curl -sf http://localhost:8000/healthz
docker logs lakehouse-ui-smoke
docker rm -f lakehouse-ui-smoke
```
Expected: `curl` prints `{"status":"ok"}`, container logs show no errors (the
bogus `POLARIS_*` env vars are fine — `/healthz` never touches the catalog).

- [ ] **Step 3: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: add Dockerfile"
```

---

## Task 6: GitHub Actions CI (test + build + push to GHCR)

**Files:**
- Create: `lakehouse-ui/.github/workflows/build.yml`

- [ ] **Step 1: Write the workflow**

`.github/workflows/build.yml`:
```yaml
name: build

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements-dev.txt
      - run: pytest -v

  build-and-push:
    needs: test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      # docker/metadata-action lower-cases the image reference automatically —
      # github.repository_owner ("OmalCooray") would otherwise be an invalid,
      # mixed-case GHCR image name.
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository_owner }}/lakehouse-ui
          tags: |
            type=sha,format=short
            type=raw,value=latest
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

- [ ] **Step 2: Commit and push**

```bash
git add .github/workflows/build.yml
git commit -m "ci: test on PRs, build and push to GHCR on merge to main"
git push -u origin main
```

- [ ] **Step 3: Verify the workflow runs**

```bash
gh run list --branch main --limit 1
```
Expected: `test` job green. `build-and-push` also runs (push to `main`) and
publishes `ghcr.io/omalcooray/lakehouse-ui:latest` and a `:sha-<short-sha>` tag.

---

## Task 7: `lakehouse-ui` README

**Files:**
- Create: `lakehouse-ui/README.md`

- [ ] **Step 1: Write the README**

`README.md`:
```markdown
# lakehouse-ui

A minimal, open-source query UI for a MinIO + Iceberg + Polaris + DuckDB
lakehouse: one SQL box, one results table. Read-only.

Deployed via [`test-k8s-configs`](https://github.com/OmalCooray/test-k8s-configs)
(`charts/lakehouse-ui/`), image published to
`ghcr.io/omalcooray/lakehouse-ui`.

## Run locally

```bash
pip install -r requirements-dev.txt
export POLARIS_ENDPOINT=http://localhost:8181/api/catalog   # kubectl port-forward svc/polaris 8181:8181
export POLARIS_CLIENT_ID=lakehouse-ui
export POLARIS_CLIENT_SECRET=<from the lakehouse-ui-polaris-credentials Secret>
export POLARIS_CATALOG=lakehouse
uvicorn app.main:app --reload
```

Open http://localhost:8000.

## Tests

```bash
pytest -v
```

## Environment variables

| Var | Purpose |
|---|---|
| `POLARIS_ENDPOINT` | Polaris REST catalog API base URL |
| `POLARIS_CLIENT_ID` / `POLARIS_CLIENT_SECRET` | Scoped principal credentials (read-only on the `lakehouse` catalog) |
| `POLARIS_CATALOG` | Catalog name to attach (`lakehouse`) |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README"
git push
```

---

## Task 8: `charts/minio/` wrapper chart

**Files (in `test-k8s-configs`):**
- Create: `charts/minio/Chart.yaml`
- Create: `charts/minio/values.yaml`

- [ ] **Step 1: Branch**

```bash
cd C:/claude/test-k8s-configs
git switch -c add-chart/minio
```

- [ ] **Step 2: Write the chart**

`charts/minio/Chart.yaml`:
```yaml
apiVersion: v2
name: minio
description: Wrapper chart for minio (minio 5.4.0)
type: application
version: 0.1.0
appVersion: "5.4.0"
dependencies:
  - name: minio
    version: 5.4.0
    repository: https://charts.min.io/
```

`charts/minio/values.yaml`:
```yaml
# Base overrides for the upstream "minio" chart (charts.min.io).
# Environment-agnostic settings live here; per-env sizing is in the overlay.
minio:
  fullnameOverride: minio
  mode: standalone

  # Secret with rootUser / rootPassword (created out of band, not in git).
  existingSecret: minio-credentials

  # One bucket for every Iceberg table's data + metadata files.
  buckets:
    - name: lakehouse
      policy: none
      purge: false

  service:
    type: ClusterIP
    port: "9000"
  consoleService:
    type: ClusterIP
    port: "9001"
```

- [ ] **Step 3: Verify**

```bash
helm dependency build charts/minio
helm lint charts/minio
helm template minio charts/minio | head -60
```
Expected: `helm lint` reports 0 errors. The render shows a `minio` Deployment
(or StatefulSet — `helm template` tells you which for `mode: standalone`), a
`minio` Service on port 9000, and a bucket-provisioning Job for `lakehouse`.
If the rendered Service/Deployment name is not exactly `minio`, note the
actual name here for Task 12 (endpoints referenced by `polaris-setup-config.yaml`
in Task 13 assume the Service is named `minio`).

- [ ] **Step 4: Commit**

```bash
git add charts/minio/Chart.yaml charts/minio/values.yaml charts/minio/Chart.lock
git commit -m "feat: add minio to the chart catalog"
```

---

## Task 9: `charts/polaris-postgres/` wrapper chart

**Files (in `test-k8s-configs`):**
- Create: `charts/polaris-postgres/Chart.yaml`
- Create: `charts/polaris-postgres/values.yaml`

- [ ] **Step 1: Branch**

```bash
git switch -c add-chart/polaris-postgres
```

- [ ] **Step 2: Write the chart**

`charts/polaris-postgres/Chart.yaml`:
```yaml
apiVersion: v2
name: polaris-postgres
description: Wrapper chart for postgres (postgres 1.6.8) — Apache Polaris's relational-jdbc metastore
type: application
version: 0.1.0
appVersion: "1.6.8"
dependencies:
  - name: postgres
    version: 1.6.8
    repository: https://groundhog2k.github.io/helm-charts/
```

`charts/polaris-postgres/values.yaml`:
```yaml
# Base overrides for the upstream "postgres" chart (groundhog2k/postgres).
# Dedicated, single-purpose Postgres for Apache Polaris's relational-jdbc
# metastore — not shared with the mysql app. Polaris's relational-jdbc
# backend supports Postgres and H2 only, not MySQL.
postgres:
  fullnameOverride: polaris-postgres

  settings:
    # Secret with POSTGRES_PASSWORD (created out of band, not in git). The
    # same Secret also carries username/password/jdbcUrl keys consumed by
    # charts/polaris/values.yaml's relationalJdbc config — see Task 10.
    existingSecret: polaris-postgres
    superuserPassword:
      secretKey: POSTGRES_PASSWORD

  # Polaris connects to a dedicated "polaris" database (not the default
  # "postgres" one), created on first boot.
  customScripts:
    01-create-polaris-db.sh: |
      set -e
      psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
        CREATE DATABASE polaris;
      EOSQL

  service:
    port: 5432
```

- [ ] **Step 3: Verify**

```bash
helm dependency build charts/polaris-postgres
helm lint charts/polaris-postgres
helm template polaris-postgres charts/polaris-postgres | grep -A3 "kind: Service"
```
Expected: 0 lint errors. Render shows a Service named `polaris-postgres` on
port 5432 — confirms `fullnameOverride` took effect and the JDBC URL in
Task 10/13 (`jdbc:postgresql://polaris-postgres:5432/polaris`) is correct. If
the customScripts block doesn't render into a ConfigMap/init step as expected,
check the upstream chart's documented env var name for the bootstrap
superuser (`$POSTGRES_USER`) via `helm show values postgres --repo
https://groundhog2k.github.io/helm-charts/ --version 1.6.8` and adjust.

- [ ] **Step 4: Commit**

```bash
git add charts/polaris-postgres/Chart.yaml charts/polaris-postgres/values.yaml charts/polaris-postgres/Chart.lock
git commit -m "feat: add polaris-postgres to the chart catalog"
```

---

## Task 10: `charts/polaris/` wrapper chart

**Files (in `test-k8s-configs`):**
- Create: `charts/polaris/Chart.yaml`
- Create: `charts/polaris/values.yaml`

- [ ] **Step 1: Branch**

```bash
git switch -c add-chart/polaris
```

- [ ] **Step 2: Write the chart**

`charts/polaris/Chart.yaml`:
```yaml
apiVersion: v2
name: polaris
description: Wrapper chart for polaris (polaris 1.7.0) — Iceberg REST catalog
type: application
version: 0.1.0
appVersion: "1.7.0"
dependencies:
  - name: polaris
    version: 1.7.0
    repository: https://downloads.apache.org/polaris/helm-chart
```

`charts/polaris/values.yaml`:

**Note (updated after Task 9's review):** `charts/polaris-postgres` was
revised to create a dedicated non-superuser database user via the upstream
chart's `userDatabase` field (not the superuser) — matching how
`charts/mysql/values.yaml` does this for Airflow. The `polaris-postgres`
Secret's actual keys are: `POSTGRES_PASSWORD` (superuser, unused here),
`POSTGRES_DB` (= `polaris`), `POSTGRES_USER_NAME` (the dedicated user),
`POSTGRES_USER_PASSWORD` (its password). Point `relationalJdbc.secret`'s
`username`/`password` fields at those same key names — do NOT invent new
`username`/`password` keys with duplicated values. Only `jdbcUrl` needs a
distinct, manually-set key (not derivable from any single existing key).

```yaml
# Base overrides for the upstream "polaris" chart (Apache Polaris).
#
# Persistence: relational-jdbc against polaris-postgres (see
# charts/polaris-postgres). Reuses the SAME out-of-band Secret
# ("polaris-postgres") that chart's userDatabase config uses — Polaris
# connects as the dedicated "polaris" user (POSTGRES_USER_NAME /
# POSTGRES_USER_PASSWORD keys), not the Postgres superuser. jdbcUrl is a
# separate manually-set key (not derivable from the others):
#   jdbcUrl = jdbc:postgresql://polaris-postgres:5432/polaris
#
# Realm bootstrap (creating realm POLARIS + a root principal) is NOT part of
# this chart — see bootstrap/polaris-setup.sh (Task 13), run once after this
# app is Synced/Healthy.
polaris:
  persistence:
    type: relational-jdbc
    relationalJdbc:
      secret:
        name: polaris-postgres
        username: POSTGRES_USER_NAME
        password: POSTGRES_USER_PASSWORD
        jdbcUrl: jdbcUrl

  realmContext:
    realms:
      - POLARIS
```

- [ ] **Step 3: Verify**

```bash
helm dependency build charts/polaris
helm lint charts/polaris
helm template polaris charts/polaris | grep -A3 "kind: Service"
```
Expected: 0 lint errors. Render shows a `polaris` Service (confirm the exact
port — used in `bootstrap/polaris-setup.sh`'s port-forward, Task 13/15) and a
Deployment/StatefulSet reading `QUARKUS_DATASOURCE_*` env vars from the
`polaris-postgres` Secret. If the rendered Service name differs from
`polaris`, note the actual name for Tasks 11/13.

- [ ] **Step 4: Commit**

```bash
git add charts/polaris/Chart.yaml charts/polaris/values.yaml charts/polaris/Chart.lock
git commit -m "feat: add polaris to the chart catalog"
```

---

## Task 11: `charts/lakehouse-ui/` chart (hand-written, no upstream dependency)

**Files (in `test-k8s-configs`):**
- Create: `charts/lakehouse-ui/Chart.yaml`
- Create: `charts/lakehouse-ui/values.yaml`
- Create: `charts/lakehouse-ui/templates/deployment.yaml`
- Create: `charts/lakehouse-ui/templates/service.yaml`

This chart is **not** produced by `/argocd-add-chart` — there is no upstream
chart to wrap. It's hand-authored the same way any Application's manifests
would be, per the roadmap's "own-app charts are out of scope for 1.0" — the
`argocd-gitops-plugin` isn't extended to generate this.

- [ ] **Step 1: Branch**

```bash
git switch -c add-chart/lakehouse-ui
```

- [ ] **Step 2: Write the chart**

`charts/lakehouse-ui/Chart.yaml`:
```yaml
apiVersion: v2
name: lakehouse-ui
description: Deployment for the custom lakehouse-ui query app (own image, not an upstream chart)
type: application
version: 0.1.0
appVersion: "0.1.0"
```

`charts/lakehouse-ui/values.yaml`:
```yaml
replicas: 1

image:
  repository: ghcr.io/omalcooray/lakehouse-ui
  tag: "latest"   # pinned to a real sha tag in environments/local/values/lakehouse-ui.yaml

polaris:
  endpoint: http://polaris.lakehouse.svc.cluster.local:8181/api/catalog
  catalog: lakehouse
  # Secret with POLARIS_CLIENT_ID / POLARIS_CLIENT_SECRET (created out of
  # band by bootstrap/polaris-setup.sh, not in git — see Task 13/15).
  credentialsSecret: lakehouse-ui-polaris-credentials

resources:
  requests: {cpu: 100m, memory: 256Mi}
  limits: {cpu: 500m, memory: 512Mi}
```

`charts/lakehouse-ui/templates/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: lakehouse-ui
  labels:
    app.kubernetes.io/name: lakehouse-ui
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app.kubernetes.io/name: lakehouse-ui
  template:
    metadata:
      labels:
        app.kubernetes.io/name: lakehouse-ui
    spec:
      containers:
        - name: lakehouse-ui
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: POLARIS_ENDPOINT
              value: {{ .Values.polaris.endpoint | quote }}
            - name: POLARIS_CATALOG
              value: {{ .Values.polaris.catalog | quote }}
            - name: POLARIS_CLIENT_ID
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.polaris.credentialsSecret }}
                  key: POLARIS_CLIENT_ID
            - name: POLARIS_CLIENT_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.polaris.credentialsSecret }}
                  key: POLARIS_CLIENT_SECRET
          livenessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 5
          readinessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 5
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
```

`charts/lakehouse-ui/templates/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: lakehouse-ui
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: lakehouse-ui
  ports:
    - name: http
      port: 8000
      targetPort: http
```

- [ ] **Step 3: Verify**

```bash
helm lint charts/lakehouse-ui
helm template lakehouse-ui charts/lakehouse-ui
```
Expected: 0 lint errors, a Deployment + Service render with no template
errors. `helm template` doesn't validate that the Secret exists — the pod will
`CrashLoopBackOff` until Task 15 creates `lakehouse-ui-polaris-credentials`;
that's expected, not a bug here.

- [ ] **Step 4: Commit**

```bash
git add charts/lakehouse-ui/
git commit -m "feat: add hand-written lakehouse-ui chart"
```

---

## Task 12: Deploy all 4 apps to `local`

**Files (in `test-k8s-configs`):**
- Create: `environments/local/apps/minio.yaml`
- Create: `environments/local/apps/polaris-postgres.yaml`
- Create: `environments/local/apps/polaris.yaml`
- Create: `environments/local/apps/lakehouse-ui.yaml`
- Create: `environments/local/values/minio.yaml`
- Create: `environments/local/values/polaris-postgres.yaml`
- Create: `environments/local/values/polaris.yaml`
- Create: `environments/local/values/lakehouse-ui.yaml`

All four apps deploy into namespace `lakehouse` (a shared namespace for this
subsystem — matching this repo's existing precedent of `mysql` sharing
`airflow`'s namespace when it's a tightly-coupled dependency, rather than one
namespace per app). Sync-wave order: `minio` and `polaris-postgres` (wave -2,
independent of each other) → `polaris` (wave -1, needs the Postgres reachable)
→ `lakehouse-ui` (default wave 0, needs Polaris + the credentials Secret from
Task 15 — expected to lag until then).

- [ ] **Step 1: Branch**

```bash
git switch -c deploy/lakehouse-local
```

- [ ] **Step 2: Write the Application manifests**

`environments/local/apps/minio.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: minio
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  sources:
    - repoURL: https://github.com/OmalCooray/test-k8s-configs
      targetRevision: master
      path: charts/minio
      helm:
        valueFiles:
          - $values/environments/local/values/minio.yaml
    - repoURL: https://github.com/OmalCooray/test-k8s-configs
      targetRevision: master
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: lakehouse
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

`environments/local/apps/polaris-postgres.yaml`: identical to the above with
`name: polaris-postgres`, `path: charts/polaris-postgres`, value file
`environments/local/values/polaris-postgres.yaml`, same sync-wave `"-2"`.

`environments/local/apps/polaris.yaml`: identical shape, `name: polaris`,
`path: charts/polaris`, value file `environments/local/values/polaris.yaml`,
sync-wave `"-1"`.

`environments/local/apps/lakehouse-ui.yaml`: identical shape, `name:
lakehouse-ui`, `path: charts/lakehouse-ui`, value file
`environments/local/values/lakehouse-ui.yaml`, **no** sync-wave annotation
(default wave `0`).

- [ ] **Step 3: Write the per-environment overlays**

`environments/local/values/minio.yaml`:
```yaml
# Per-environment overrides for minio in local.
minio:
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits: {cpu: 500m, memory: 512Mi}
  persistence:
    size: 10Gi
    storageClass: hostpath
```

`environments/local/values/polaris-postgres.yaml`:
```yaml
# Per-environment overrides for polaris-postgres in local.
postgres:
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits: {cpu: 500m, memory: 512Mi}
  storage:
    requestedSize: 5Gi
    className: hostpath
```

`environments/local/values/polaris.yaml`:
```yaml
# Per-environment overrides for polaris in local.
polaris:
  resources:
    requests: {cpu: 200m, memory: 512Mi}
    limits: {cpu: "1", memory: 1Gi}
```

`environments/local/values/lakehouse-ui.yaml`:
```yaml
# Per-environment overrides for lakehouse-ui in local.
# Pinned to a real image tag once Task 6's CI has published one — replace
# "latest" with the ghcr.io sha tag before merging (never :latest in-cluster).
image:
  tag: "REPLACE-WITH-SHA-TAG-FROM-lakehouse-ui-CI"
```

- [ ] **Step 4: Verify**

```bash
for f in environments/local/apps/{minio,polaris-postgres,polaris,lakehouse-ui}.yaml; do
  kubectl apply --dry-run=client -f "$f"
done
```
Expected: all 4 pass client-side validation. (If Argo CD CRDs aren't
reachable from this shell, use the YAML-parse fallback from
`argocd-deploy.md`: `python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`.)

- [ ] **Step 5: Fill in the real image tag**

```bash
gh api /orgs/OmalCooray/packages/container/lakehouse-ui/versions 2>/dev/null \
  || gh api /users/OmalCooray/packages/container/lakehouse-ui/versions --jq '.[0].metadata.container.tags'
```
Replace `REPLACE-WITH-SHA-TAG-FROM-lakehouse-ui-CI` in
`environments/local/values/lakehouse-ui.yaml` with the actual `sha-<short>` tag
from Task 6's CI run.

- [ ] **Step 6: Update `.claude/CLAUDE.md` and commit**

Add rows for `minio`, `polaris-postgres`, `polaris`, `lakehouse-ui` to the
catalog inventory and deployment matrix tables (chart versions from Tasks
8–11; `lakehouse-ui` has no "upstream chart" — note it as `(own app)`).

```bash
git add environments/local/apps/ environments/local/values/ .claude/CLAUDE.md
git commit -m "feat: deploy minio, polaris-postgres, polaris, lakehouse-ui to local"
```

---

## Task 13: Polaris bootstrap + catalog setup runbook

**Files (in `test-k8s-configs`):**
- Create: `bootstrap/polaris-setup.sh`
- Create: `bootstrap/polaris-setup-config.yaml`

Not Argo CD-managed. `apache/polaris-admin-tool bootstrap`'s behavior against
an already-bootstrapped realm is not documented as safe to rerun (unlike
Airflow's `createUserJob`, which is idempotent by chart design) — so this is a
one-time script in the same spirit as the existing `bootstrap/install.sh`, run
by hand once `minio`, `polaris-postgres`, and `polaris` are Synced/Healthy.

- [ ] **Step 1: Write the declarative setup config**

`bootstrap/polaris-setup-config.yaml`:
```yaml
# Applied by `polaris setup apply` (see polaris-setup.sh). Create-only — safe
# to re-run; existing entities are left alone.
#
# The S3/MinIO storage block's field names (endpoint/region/path_style_access)
# are inferred from the `polaris catalogs create --storage-type s3 --endpoint
# ... --region ...` CLI flags, not independently confirmed for the `setup
# apply` YAML schema specifically — run `polaris setup apply --dry-run` first
# (Task 15, Step 3); if it reports an unknown field, run
# `polaris catalogs create --help` and rename the key here to match.
principals:
  loader:
    roles:
      - loader_role
  lakehouse-ui:
    roles:
      - lakehouse_ui_role

principal_roles:
  - loader_role
  - lakehouse_ui_role

catalogs:
  - name: "lakehouse"
    storage_type: "s3"
    default_base_location: "s3://lakehouse/"
    allowed_locations:
      - "s3://lakehouse/"
    endpoint: "http://minio:9000"
    region: "us-east-1"
    path_style_access: true
    roles:
      loader_catalog_role:
        assign_to:
          - loader_role
        privileges:
          catalog:
            # Broad on purpose — this principal is used only for the one-off
            # local data load (Task 16), never by lakehouse-ui.
            - CATALOG_MANAGE_CONTENT
      lakehouse_ui_catalog_role:
        assign_to:
          - lakehouse_ui_role
        privileges:
          catalog:
            # Read-only — confirm these are the exact privilege enum names
            # via `polaris privileges catalog --help` (Task 15, Step 3); if
            # not, use the closest read-only equivalents it lists.
            - TABLE_READ_DATA
            - NAMESPACE_LIST
    namespaces:
      - nyc_taxi
```

- [ ] **Step 2: Write the runbook script**

`bootstrap/polaris-setup.sh`:
```bash
#!/usr/bin/env bash
# One-time-per-realm setup for the Polaris catalog on this cluster:
#   1. Bootstrap the POLARIS realm + root principal (apache/polaris-admin-tool).
#   2. Apply polaris-setup-config.yaml (catalog/namespace/principals/grants)
#      via the `polaris setup apply` CLI.
#
# Not Argo CD-managed — see the header comment in polaris-setup-config.yaml.
# Run by hand after `minio`, `polaris-postgres`, and `polaris` are all
# Synced/Healthy. See docs/superpowers/specs/2026-09-11-iceberg-lakehouse-design.md.
set -euo pipefail
NS=lakehouse
ROOT_CLIENT_ID=root
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> Bootstrapping Polaris in namespace '$NS'. Ctrl-C now if that's wrong."
sleep 3

# polaris-postgres (Task 9, revised) creates a dedicated "polaris" DB user
# via the chart's userDatabase field, not the superuser — bootstrap connects
# as that same user (matches what charts/polaris/values.yaml's
# relationalJdbc config uses, so both agree on who owns the schema).
PG_USER=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_NAME}' | base64 -d)
PG_PASSWORD=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_PASSWORD}' | base64 -d)
ROOT_CLIENT_SECRET=$(openssl rand -hex 20)

echo ">> Bootstrapping realm POLARIS (root principal: $ROOT_CLIENT_ID)"
kubectl run polaris-bootstrap --rm -it --restart=Never -n "$NS" \
  --image=apache/polaris-admin-tool:1.7.0 \
  --env="POLARIS_PERSISTENCE_TYPE=relational-jdbc" \
  --env="QUARKUS_DATASOURCE_USERNAME=${PG_USER}" \
  --env="QUARKUS_DATASOURCE_PASSWORD=${PG_PASSWORD}" \
  --env="QUARKUS_DATASOURCE_JDBC_URL=jdbc:postgresql://polaris-postgres:5432/polaris" \
  -- bootstrap -r POLARIS -c "POLARIS,${ROOT_CLIENT_ID},${ROOT_CLIENT_SECRET}"

kubectl create secret generic polaris-root-credentials -n "$NS" \
  --from-literal=CLIENT_ID="$ROOT_CLIENT_ID" \
  --from-literal=CLIENT_SECRET="$ROOT_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
echo ">> Root credentials stored in Secret polaris-root-credentials (ns $NS)."

echo ">> Port-forwarding polaris:8181 to apply the catalog/principal/grants config..."
kubectl port-forward -n "$NS" svc/polaris 8181:8181 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 3

pip install --quiet apache-polaris

echo ">> Dry run first — read the output before applying for real:"
polaris --host localhost --port 8181 \
  --client-id "$ROOT_CLIENT_ID" --client-secret "$ROOT_CLIENT_SECRET" \
  setup apply --dry-run "$HERE/polaris-setup-config.yaml"

cat <<'EOF'

If the dry run looks right, apply for real:

  polaris --host localhost --port 8181 \
    --client-id "$ROOT_CLIENT_ID" --client-secret "$ROOT_CLIENT_SECRET" \
    setup apply bootstrap/polaris-setup-config.yaml

Then read its output for the generated "loader" and "lakehouse-ui" principal
credentials (printed once, at creation) and store them:

  kubectl create secret generic loader-polaris-credentials -n lakehouse \
    --from-literal=CLIENT_ID=loader --from-literal=CLIENT_SECRET=<printed>

  kubectl create secret generic lakehouse-ui-polaris-credentials -n lakehouse \
    --from-literal=POLARIS_CLIENT_ID=lakehouse-ui \
    --from-literal=POLARIS_CLIENT_SECRET=<printed>

If the dry run instead reports an invalid field in polaris-setup-config.yaml
(likely the S3 storage block — endpoint/region/path_style_access), run
`polaris catalogs create --help` to see the real flag names, fix the YAML,
and dry-run again before applying.
EOF
```

- [ ] **Step 3: Commit (script not yet run — that's Task 15)**

```bash
git add bootstrap/polaris-setup.sh bootstrap/polaris-setup-config.yaml
chmod +x bootstrap/polaris-setup.sh
git commit -m "feat: add one-time Polaris realm bootstrap + catalog setup runbook"
```

- [ ] **Step 4: Open PRs for Tasks 8–13**

Push each branch and open a PR (`gh pr create`), same convention as the
plugin's `/argocd-add-chart` / `/argocd-deploy`: title describing the change,
body with the rendered `helm template` output or config summary. These can be
separate PRs (one per task/branch) or squashed into fewer PRs if the branches
were built directly on top of each other — match whatever branching was
actually used. Do not merge yet; Task 14 (main session) merges and drives to
Healthy.

---

## Task 14 (MAIN SESSION — interactive, not a subagent): Deploy and drive to Healthy

Merge the PRs from Tasks 8–13, then watch and fix live — this is exactly the
"push → watch → fix" pattern already used for every other app in this repo
(Airflow's hook-Job fixes, MySQL 9 auth, node-exporter's hostRootFsMount,
etc.). Expect at least one live surprise; this list is the starting sequence,
not a guarantee of what will actually happen.

- [ ] Merge the Tasks 8–13 PRs (or confirm with the user before merging, per
  the plugin's interaction contract).
- [ ] `kubectl get application -n argocd` — confirm `minio`, `polaris-postgres`,
  `polaris`, `lakehouse-ui` appear (via `root-local`'s app-of-apps sync).
- [ ] Watch `minio` and `polaris-postgres` reach Synced/Healthy first (wave
  -2). Troubleshoot with `argocd-troubleshooting` skill patterns already
  established in this repo if either stalls.
- [ ] Watch `polaris` reach Synced/Healthy (wave -1). Check its logs
  (`kubectl logs -n lakehouse deploy/polaris`) for JDBC connection errors —
  if the customScripts-created `polaris` database (Task 9) didn't actually
  get created, this is where it surfaces; fix and re-sync.
- [ ] `lakehouse-ui` will be Synced but likely **not** Healthy yet
  (CrashLoopBackOff — no `lakehouse-ui-polaris-credentials` Secret exists
  until Task 15). Confirm that's the actual failure reason
  (`kubectl logs -n lakehouse deploy/lakehouse-ui`) before moving on — a
  different failure reason needs fixing now, not after Task 15.
- [ ] Report the state: 3 of 4 apps Healthy, `lakehouse-ui` blocked on
  credentials — proceed to Task 15.

---

## Task 15 (MAIN SESSION — interactive, not a subagent): Run the Polaris setup runbook

- [ ] Run `bash bootstrap/polaris-setup.sh` against the live cluster. Follow
  its printed instructions: review the `--dry-run` output, fix
  `polaris-setup-config.yaml` if any field name is rejected (commit the fix),
  then apply for real.
- [ ] Capture the `loader` and `lakehouse-ui` principal credentials from the
  apply output; create `loader-polaris-credentials` and
  `lakehouse-ui-polaris-credentials` Secrets in namespace `lakehouse` as the
  script's closing instructions describe.
- [ ] `kubectl rollout restart deployment/lakehouse-ui -n lakehouse` (or let
  Argo CD self-heal pick up the new Secret on its own — either works; the
  Deployment doesn't watch Secret changes automatically, so a restart is more
  direct).
- [ ] Confirm `lakehouse-ui` reaches Healthy: `kubectl get pods -n lakehouse`,
  `kubectl port-forward -n lakehouse svc/lakehouse-ui 8000:8000` then
  `curl localhost:8000/healthz`.
- [ ] Independently confirm the catalog exists: `kubectl port-forward -n
  lakehouse svc/polaris 8181:8181`, then
  `curl -s http://localhost:8181/api/management/v1/catalogs -H "Authorization: Bearer $TOKEN"`
  (get `$TOKEN` via the OAuth endpoint,
  `curl http://localhost:8181/api/catalog/v1/oauth/tokens --user root:$ROOT_SECRET`)
  lists `lakehouse`.

---

## Task 16 (MAIN SESSION — interactive, not a subagent): Load NYC taxi data

- [ ] Port-forward: `kubectl port-forward -n lakehouse svc/polaris 8181:8181`
  and `kubectl port-forward -n lakehouse svc/minio 9000:9000` (two terminals,
  or background both).
- [ ] Local DuckDB session (using the `loader` principal's credentials from
  Task 15):
  ```sql
  INSTALL iceberg; LOAD iceberg;
  INSTALL httpfs; LOAD httpfs;
  CREATE SECRET polaris_secret (TYPE iceberg, CLIENT_ID 'loader', CLIENT_SECRET '<loader secret>');
  ATTACH 'lakehouse' AS lakehouse (TYPE iceberg, ENDPOINT 'http://localhost:8181/api/catalog', ACCESS_DELEGATION_MODE 'vended_credentials');

  CREATE TABLE lakehouse.nyc_taxi.trips AS
  SELECT * FROM read_parquet('https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet');
  ```
- [ ] If `ACCESS_DELEGATION_MODE 'vended_credentials'` doesn't resolve real
  MinIO credentials cleanly on the first attempt (a real risk — this is the
  least-proven part of the design), fall back to static credentials: create a
  DuckDB S3 secret directly from the `minio-credentials` Secret (`CREATE
  SECRET minio_s3 (TYPE s3, KEY_ID '...', SECRET '...', ENDPOINT
  'localhost:9000', URL_STYLE 'path', USE_SSL false)`) and retry. Note in the
  design's "Verification" section (or a follow-up doc note) which path
  actually worked.
- [ ] New DuckDB session, re-attach the same catalog, confirm the round trip:
  ```sql
  SELECT count(*) FROM lakehouse.nyc_taxi.trips;
  ```
  Expected: a nonzero row count matching the source Parquet file.

---

## Task 17 (MAIN SESSION — interactive, not a subagent): Final verification and report

- [ ] Browser (or `curl`) against `lakehouse-ui` (port-forwarded):
  `SELECT count(*) FROM nyc_taxi.trips` and one real aggregate query (e.g.
  average fare by hour) both return correct results.
- [ ] `mc alias set local http://localhost:9000 <access-key> <secret-key>` then
  `mc ls --recursive local/lakehouse` shows Iceberg data/metadata files.
- [ ] Update `.claude/CLAUDE.md`'s deployment matrix (already partially done
  in Task 12 — confirm it's accurate post-deploy).
- [ ] Write up which of the two credential-vending paths from Task 16 Step 3
  actually worked, as a short note in
  `docs/superpowers/specs/2026-09-11-iceberg-lakehouse-design.md` (append,
  don't rewrite) or a new `docs/lakehouse-runbook.md` — whichever fits better
  given what was actually learned.
- [ ] Report against the design spec's Definition of Done, item by item.
