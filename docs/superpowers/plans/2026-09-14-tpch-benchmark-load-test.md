# TPC-H Benchmark & Load Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load TPC-H SF1 into a new `lakehouse.tpch` namespace, benchmark the
22 standard queries against the real stack (DuckDB → Polaris → Iceberg →
MinIO), load-test `lakehouse-ui` under concurrency, and publish a report
comparing our numbers to publicly available TPC-H-derived figures.

**Architecture:** Four scripts in a new `benchmark/` directory (mirroring
the existing `analytics/` one-off-in-cluster-pod pattern), plus a one-line
addition to the Polaris bootstrap config for the new namespace. Tasks 1-5
write and commit the scripts (subagent-driven, no cluster access needed —
syntax-checked only). Tasks 6-11 run live against the real cluster (main
session only, same boundary this repo has used for every prior live-deploy
phase).

**Tech Stack:** Python 3.12, DuckDB 1.5.3 (`tpch`, `iceberg`, `httpfs`
extensions), httpx 0.27.2, existing Polaris/MinIO/lakehouse-ui stack.

---

### Task 1: Add the `tpch` namespace to the Polaris bootstrap config

**Files:**
- Modify: `bootstrap/polaris-setup-config.yaml`

- [ ] **Step 1: Add `tpch` to the `lakehouse` catalog's `namespaces:` list**

Read the current file first. It has a `catalogs: - name: "lakehouse"`
block ending in:
```yaml
    namespaces:
      - nyc_taxi
```
Change that to:
```yaml
    namespaces:
      - nyc_taxi
      - tpch
```
Do not touch anything else in the file — `principals`, `principal_roles`,
`catalogs[0].roles`, and the `nyc_taxi` entry all stay exactly as they are.
This file's own header comment already documents that it's create-only and
safe to re-apply (`polaris setup apply` skips existing entities) — adding a
second namespace here is exactly the mechanism it's designed for.

- [ ] **Step 2: Commit**

```bash
cd C:/claude/test-k8s-configs
git switch master
git pull
git switch -c feat/tpch-namespace
git add bootstrap/polaris-setup-config.yaml
git commit -m "feat: add tpch namespace to the Polaris bootstrap config"
```

Do not push — this stops at a local commit (a later main-session task
merges it before applying it live).

---

### Task 2: Write `benchmark/tpch_load.py`

**Files:**
- Create: `benchmark/tpch_load.py`

- [ ] **Step 1: Write the script**

```python
"""Load TPC-H SF1 data into lakehouse.tpch.*.

One-off, run inside the cluster (Polaris vends storage credentials scoped
to the in-cluster MinIO DNS name, unreachable from a kubectl
port-forwarded local process — same constraint as
analytics/load_trips.py).

Generates the 8 standard TPC-H tables via DuckDB's built-in tpch extension
(no network I/O for generation — pure local dbgen), then writes each into
the lakehouse Iceberg catalog's tpch namespace. Verifies each table's row
count against the well-known TPC-H SF1 reference counts before declaring
success, so a partial/corrupt load fails loudly instead of silently
benchmarking bad data.

## Running it

    kubectl create configmap tpch-load -n lakehouse \\
      --from-file=tpch_load.py=benchmark/tpch_load.py

    kubectl run tpch-load --restart=Never -n lakehouse \\
      --image=python:3.12-slim \\
      --overrides='{
        "spec": {
          "containers": [{
            "name": "runner",
            "image": "python:3.12-slim",
            "command": ["/bin/sh", "-c", "pip install --quiet duckdb==1.5.3 && python /scripts/tpch_load.py"],
            "env": [
              {"name": "POLARIS_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
              {"name": "POLARIS_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
            ],
            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
          }],
          "volumes": [{"name": "script", "configMap": {"name": "tpch-load"}}]
        }
      }'

    kubectl logs -f tpch-load -n lakehouse
    kubectl delete pod tpch-load -n lakehouse
    kubectl delete configmap tpch-load -n lakehouse

Requires the `tpch` namespace to already exist in the `lakehouse` catalog
(added via bootstrap/polaris-setup-config.yaml + `polaris setup apply` —
see benchmark/README.md).
"""
import os

import duckdb

# Well-known TPC-H SF1 reference row counts (deterministic -- dbgen(sf=1)
# always produces exactly these counts for every table except lineitem,
# whose count depends on a per-order random line count and lands very
# close to, but not exactly, 6,000,000).
EXPECTED_ROW_COUNTS = {
    "region": 5,
    "nation": 25,
    "supplier": 10_000,
    "customer": 150_000,
    "part": 200_000,
    "partsupp": 800_000,
    "orders": 1_500_000,
}
# lineitem's count varies run to run; sanity-check it's in the expected
# range instead of an exact match.
LINEITEM_EXPECTED_RANGE = (5_900_000, 6_100_000)

client_id = os.environ["POLARIS_CLIENT_ID"]
client_secret = os.environ["POLARIS_CLIENT_SECRET"]

con = duckdb.connect(":memory:")

print("Generating TPC-H SF1 data locally (dbgen)...", flush=True)
con.execute("INSTALL tpch")
con.execute("LOAD tpch")
con.execute("CALL dbgen(sf=1)")

con.execute("INSTALL iceberg")
con.execute("LOAD iceberg")
con.execute("INSTALL httpfs")
con.execute("LOAD httpfs")
con.execute(
    f"""
    CREATE SECRET polaris_secret (
        TYPE iceberg,
        CLIENT_ID '{client_id}',
        CLIENT_SECRET '{client_secret}',
        ENDPOINT 'http://polaris:8181/api/catalog'
    )
    """
)
con.execute(
    """
    ATTACH 'lakehouse' AS lakehouse (
        TYPE iceberg,
        ENDPOINT 'http://polaris:8181/api/catalog',
        ACCESS_DELEGATION_MODE 'vended_credentials'
    )
    """
)

tables = ["region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem"]

for table in tables:
    print(f"Loading {table} into lakehouse.tpch.{table} ...", flush=True)
    try:
        con.execute(f"DROP TABLE lakehouse.tpch.{table}")
    except duckdb.Error as exc:
        if "does not exist" not in str(exc).lower():
            raise
    con.execute(f"CREATE TABLE lakehouse.tpch.{table} AS SELECT * FROM {table}")

print("\n=== verifying row counts ===", flush=True)
failures = []
for table in tables:
    count = con.execute(f"SELECT count(*) FROM lakehouse.tpch.{table}").fetchone()[0]
    if table == "lineitem":
        lo, hi = LINEITEM_EXPECTED_RANGE
        ok = lo <= count <= hi
        expected_desc = f"{lo}-{hi}"
    else:
        expected = EXPECTED_ROW_COUNTS[table]
        ok = count == expected
        expected_desc = str(expected)
    status = "OK" if ok else "MISMATCH"
    print(f"{table}: {count} rows (expected {expected_desc}) [{status}]", flush=True)
    if not ok:
        failures.append(table)

if failures:
    raise SystemExit(f"FAIL: row count mismatch for: {', '.join(failures)}")

print("\nDone. All 8 tables loaded and verified.", flush=True)
```

- [ ] **Step 2: Verify syntax**

```bash
cd C:/claude/test-k8s-configs
python3 -m py_compile benchmark/tpch_load.py
```
Expected: no output, exit code 0. (This only checks syntax — it does not
run against the cluster; that happens in Task 7, main session only.)

- [ ] **Step 3: Commit**

```bash
git add benchmark/tpch_load.py
git commit -m "feat: add the TPC-H SF1 data load script"
```

---

### Task 3: Write `benchmark/tpch_bench.py`

**Files:**
- Create: `benchmark/tpch_bench.py`

- [ ] **Step 1: Write the script**

```python
"""Benchmark the 22 standard TPC-H queries against lakehouse.tpch.*.

One-off, run inside the cluster (see benchmark/tpch_load.py's docstring
for why). Requires benchmark/tpch_load.py to have already loaded and
verified the SF1 data.

Runs each of the 22 official TPC-H queries (DuckDB's own tpch_queries()
table function -- the real query text, not a hand-copied version) 5 times
(1 cold + 4 warm) against the full real stack: DuckDB -> Polaris REST
catalog -> MinIO S3 -> Iceberg format. Every individual timing is
recorded, not just an average, since cold-vs-warm variance is itself one
of the things this benchmark is measuring (each cold run pays for a fresh
Polaris metadata fetch + MinIO credential vend that a published
DuckDB-native number wouldn't have to).

## Running it

    kubectl create configmap tpch-bench -n lakehouse \\
      --from-file=tpch_bench.py=benchmark/tpch_bench.py

    kubectl run tpch-bench --restart=Never -n lakehouse \\
      --image=python:3.12-slim \\
      --overrides='{
        "spec": {
          "containers": [{
            "name": "runner",
            "image": "python:3.12-slim",
            "command": ["/bin/sh", "-c", "pip install --quiet duckdb==1.5.3 && python /scripts/tpch_bench.py"],
            "env": [
              {"name": "POLARIS_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
              {"name": "POLARIS_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
            ],
            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
          }],
          "volumes": [{"name": "script", "configMap": {"name": "tpch-bench"}}]
        }
      }'

    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/tpch-bench -n lakehouse --timeout=600s
    kubectl logs tpch-bench -n lakehouse
    kubectl cp lakehouse/tpch-bench:/tmp/tpch_sf1_raw_latency.csv benchmark/results/tpch_sf1_raw_latency.csv
    kubectl delete pod tpch-bench -n lakehouse
    kubectl delete configmap tpch-bench -n lakehouse
"""
import csv
import os
import time

import duckdb

RUNS_PER_QUERY = 5
OUTPUT_PATH = "/tmp/tpch_sf1_raw_latency.csv"

client_id = os.environ["POLARIS_CLIENT_ID"]
client_secret = os.environ["POLARIS_CLIENT_SECRET"]

con = duckdb.connect(":memory:")
con.execute("INSTALL tpch")
con.execute("LOAD tpch")
con.execute("INSTALL iceberg")
con.execute("LOAD iceberg")
con.execute("INSTALL httpfs")
con.execute("LOAD httpfs")
con.execute(
    f"""
    CREATE SECRET polaris_secret (
        TYPE iceberg,
        CLIENT_ID '{client_id}',
        CLIENT_SECRET '{client_secret}',
        ENDPOINT 'http://polaris:8181/api/catalog'
    )
    """
)
con.execute(
    """
    ATTACH 'lakehouse' AS lakehouse (
        TYPE iceberg,
        ENDPOINT 'http://polaris:8181/api/catalog',
        ACCESS_DELEGATION_MODE 'vended_credentials'
    )
    """
)
con.execute("USE lakehouse.tpch")

queries = con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall()
print(f"Loaded {len(queries)} standard TPC-H queries.", flush=True)

rows = []
for query_nr, query_text in queries:
    for run_number in range(1, RUNS_PER_QUERY + 1):
        label = "cold" if run_number == 1 else "warm"
        started = time.monotonic()
        con.execute(query_text)
        # Force full materialization so timing reflects the whole query,
        # not just planning + first-batch latency.
        con.fetchall()
        latency_ms = (time.monotonic() - started) * 1000
        rows.append((query_nr, run_number, label, round(latency_ms, 2)))
        print(f"[Q{query_nr:02d} run {run_number} ({label})] {latency_ms:.1f} ms", flush=True)

with open(OUTPUT_PATH, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["query_nr", "run_number", "run_type", "latency_ms"])
    writer.writerows(rows)

print(f"\nDone. Wrote {len(rows)} timings to {OUTPUT_PATH}", flush=True)
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -m py_compile benchmark/tpch_bench.py
```
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add benchmark/tpch_bench.py
git commit -m "feat: add the TPC-H raw-engine latency benchmark script"
```

---

### Task 4: Write `benchmark/load_test.py`

**Files:**
- Create: `benchmark/load_test.py`

- [ ] **Step 1: Write the script**

```python
"""Concurrent load test against lakehouse-ui's /query endpoint, using the
22 standard TPC-H queries as the traffic.

One-off, run inside the cluster so it talks to lakehouse-ui over cluster
DNS (http://lakehouse-ui.lakehouse.svc.cluster.local:8000) rather than a
local kubectl port-forward tunnel -- this measures the app's own latency,
not this session's tunnel stability. Requires benchmark/tpch_load.py to
have already loaded the SF1 data (all 22 queries read from lakehouse.tpch).

Logs in once via /login, then runs the resulting session cookie through a
ThreadPoolExecutor of N workers per concurrency level, each looping
through random queries from the 22 for a fixed duration. Concurrency
levels 1, 5, 10, 20 run sequentially with a cooldown between them so each
level starts from a clean baseline. Every request's latency, query
number, and status code is recorded.

## Running it

    kubectl create configmap load-test -n lakehouse \\
      --from-file=load_test.py=benchmark/load_test.py

    kubectl run load-test --restart=Never -n lakehouse \\
      --image=python:3.12-slim \\
      --overrides='{
        "spec": {
          "containers": [{
            "name": "runner",
            "image": "python:3.12-slim",
            "command": ["/bin/sh", "-c", "pip install --quiet httpx==0.27.2 duckdb==1.5.3 && python /scripts/load_test.py"],
            "env": [
              {"name": "LOADER_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
              {"name": "LOADER_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
            ],
            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
          }],
          "volumes": [{"name": "script", "configMap": {"name": "load-test"}}]
        }
      }'

    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/load-test -n lakehouse --timeout=1200s
    kubectl logs load-test -n lakehouse
    kubectl cp lakehouse/load-test:/tmp/load_test_raw.csv benchmark/results/load_test_raw.csv
    kubectl delete pod load-test -n lakehouse
    kubectl delete configmap load-test -n lakehouse

(This needs duckdb installed too, only to fetch the 22 canonical query
texts from tpch_queries() locally -- it never attaches the lakehouse
catalog itself, all querying happens over HTTP against lakehouse-ui.)
"""
import csv
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import duckdb
import httpx

BASE_URL = "http://lakehouse-ui.lakehouse.svc.cluster.local:8000"
CONCURRENCY_LEVELS = [1, 5, 10, 20]
DURATION_PER_LEVEL_SECONDS = 60
COOLDOWN_SECONDS = 10
OUTPUT_PATH = "/tmp/load_test_raw.csv"

client_id = os.environ["LOADER_CLIENT_ID"]
client_secret = os.environ["LOADER_CLIENT_SECRET"]

# Pull the 22 canonical query texts locally (pure in-memory DuckDB, no
# catalog attach) so the load test sends the exact same queries
# tpch_bench.py used for the raw-engine numbers.
_con = duckdb.connect(":memory:")
_con.execute("INSTALL tpch")
_con.execute("LOAD tpch")
QUERIES = _con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall()
_con.close()
print(f"Loaded {len(QUERIES)} queries to drive the load test with.", flush=True)

client = httpx.Client(base_url=BASE_URL, timeout=120.0)

login_resp = client.post("/login", json={"client_id": client_id, "client_secret": client_secret})
login_resp.raise_for_status()
print("Logged in.", flush=True)

results = []
results_lock = threading.Lock()


def worker(concurrency_level: int, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        query_nr, query_text = random.choice(QUERIES)
        started = time.monotonic()
        try:
            resp = client.post("/query", json={"sql": query_text})
            status_code = resp.status_code
        except httpx.HTTPError:
            status_code = -1
        latency_ms = (time.monotonic() - started) * 1000
        with results_lock:
            results.append((concurrency_level, query_nr, round(latency_ms, 2), status_code))


for concurrency in CONCURRENCY_LEVELS:
    print(f"\n=== concurrency={concurrency}, running for {DURATION_PER_LEVEL_SECONDS}s ===", flush=True)
    stop_at = time.monotonic() + DURATION_PER_LEVEL_SECONDS
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker, concurrency, stop_at) for _ in range(concurrency)]
        for f in futures:
            f.result()

    level_results = [r for r in results if r[0] == concurrency]
    latencies = sorted(r[2] for r in level_results)
    errors = sum(1 for r in level_results if r[3] != 200)
    n = len(level_results)
    p50 = latencies[n // 2] if n else 0
    p95 = latencies[int(n * 0.95)] if n else 0
    error_pct = (100 * errors / n) if n else 0.0
    print(
        f"concurrency={concurrency}: {n} requests, {errors} errors "
        f"({error_pct:.1f}%), p50={p50:.0f}ms, p95={p95:.0f}ms",
        flush=True,
    )

    if concurrency != CONCURRENCY_LEVELS[-1]:
        print(f"Cooling down {COOLDOWN_SECONDS}s before next level...", flush=True)
        time.sleep(COOLDOWN_SECONDS)

with open(OUTPUT_PATH, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["concurrency_level", "query_nr", "latency_ms", "status_code"])
    writer.writerows(results)

print(f"\nDone. Wrote {len(results)} request records to {OUTPUT_PATH}", flush=True)
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -m py_compile benchmark/load_test.py
```
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add benchmark/load_test.py
git commit -m "feat: add the concurrent load test script"
```

---

### Task 5: Write `benchmark/README.md` and a results placeholder

**Files:**
- Create: `benchmark/README.md`
- Create: `benchmark/results/.gitkeep`

- [ ] **Step 1: Write the README**

```markdown
# TPC-H benchmark & load test

Loads TPC-H SF1 (~1GB, ~6M rows across 8 tables) into a dedicated
`lakehouse.tpch` namespace, benchmarks the 22 standard TPC-H queries
against the real stack (DuckDB -> Polaris REST catalog -> Iceberg ->
MinIO), and load-tests `lakehouse-ui` itself under concurrency.

See `docs/superpowers/specs/2026-09-14-tpch-benchmark-load-test-design.md`
for the full design and the reasoning behind each phase. A note on
naming: nothing produced here is an official, TPC-audited "TPC-H Result"
-- these are unofficial, informally-run numbers, reported the way the
TPC's own Fair Use Policy asks unaudited publishers to describe them
("TPC-H-derived").

Independent of, and doesn't modify, the existing `analytics/` NYC taxi
data model -- separate namespace, separate scripts.

## Prerequisites

The `tpch` namespace must already exist in the `lakehouse` catalog
(`bootstrap/polaris-setup-config.yaml` + a `polaris setup apply` run --
create-only, safe to re-run, won't touch `nyc_taxi` or anything else).

## Running it, in order

1. **Load the data** -- see the docstring in `tpch_load.py` for the exact
   `kubectl run` invocation. Verifies row counts against the known TPC-H
   SF1 reference counts before declaring success.
2. **Raw-engine benchmark** -- see `tpch_bench.py`'s docstring. Runs all
   22 standard queries 5x each (1 cold + 4 warm), writes
   `benchmark/results/tpch_sf1_raw_latency.csv`.
3. **Load test** -- see `load_test.py`'s docstring. Drives concurrent HTTP
   traffic at `lakehouse-ui`'s `/query` endpoint at concurrency levels 1,
   5, 10, 20 (60s each), writes `benchmark/results/load_test_raw.csv`.

Each script is a one-off in-cluster pod (same pattern as
`analytics/load_trips.py` and `analytics/run_model.py`) using the
`loader` principal's existing catalog-wide grant -- no new credentials or
grants needed.

## Results

Raw per-request/per-query timings live in `benchmark/results/*.csv`,
committed alongside the scripts that produced them so the numbers are
reproducible from source. The comparison report (our numbers against
publicly available TPC-H-derived figures, with charts) is published as
an Artifact -- see the design doc's "Verification results" section
(appended after this plan's live-execution tasks) for the link.
```

- [ ] **Step 2: Create the results placeholder**

```bash
mkdir -p benchmark/results
touch benchmark/results/.gitkeep
```

- [ ] **Step 3: Commit**

```bash
git add benchmark/README.md benchmark/results/.gitkeep
git commit -m "docs: add benchmark README"
```

---

### Task 6 (MAIN SESSION — interactive, not a subagent): Merge and apply the namespace change

- [ ] Merge `feat/tpch-namespace` and the 3 script-writing branches (or
  confirm with the user before merging, per established practice this
  session) — push each, open PRs, merge to `master`.
- [ ] Pull `master` locally, confirm `bootstrap/polaris-setup-config.yaml`
  now has both `nyc_taxi` and `tpch` under `namespaces:`.
- [ ] Port-forward Polaris and re-run `polaris setup apply` against the
  live cluster (same pattern as the original bootstrap):
  ```bash
  kubectl port-forward -n lakehouse svc/polaris 8181:8181 &
  ROOT_CLIENT_SECRET=$(kubectl get secret polaris-root-credentials -n lakehouse -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d)
  polaris --host localhost --port 8181 \
    --client-id root --client-secret "$ROOT_CLIENT_SECRET" \
    setup apply bootstrap/polaris-setup-config.yaml
  ```
- [ ] Confirm the output shows only the `tpch` namespace being created
  (everything else should log as already existing / skipped) — if
  anything about `nyc_taxi` or the existing principals gets touched,
  stop and investigate before proceeding.
- [ ] Verify live: `polaris --host localhost --port 8181 --client-id root --client-secret "$ROOT_CLIENT_SECRET" namespaces list --catalog lakehouse` shows both `nyc_taxi` and `tpch`.

---

### Task 7 (MAIN SESSION — interactive, not a subagent): Load TPC-H SF1 data

- [ ] Run `benchmark/tpch_load.py` in-cluster per its own docstring.
- [ ] Watch the logs live (`kubectl logs -f tpch-load -n lakehouse`) —
  this is a real data load, not a background job; confirm it's actually
  progressing rather than firing and forgetting it.
- [ ] Confirm the script's own row-count verification passed (its exit
  code and the "Done. All 8 tables loaded and verified." line) — if it
  reports a MISMATCH, diagnose before proceeding to benchmarking against
  bad data.
- [ ] Independent double-check outside the script's own assertions:
  `SELECT count(*) FROM lakehouse.tpch.lineitem` via a quick `polaris`/
  DuckDB check, or via the `lakehouse-ui` UI itself logged in as `loader`.
- [ ] Clean up the pod and configmap per the script's docstring.

---

### Task 8 (MAIN SESSION — interactive, not a subagent): Run the raw-engine benchmark

- [ ] Run `benchmark/tpch_bench.py` in-cluster per its own docstring.
  This takes a while (22 queries x 5 runs against a real Iceberg/MinIO
  backend) — watch progress live via `kubectl logs -f`, don't just fire
  it and wait silently.
- [ ] Once the pod reaches `Succeeded`, `kubectl cp` the CSV out to
  `benchmark/results/tpch_sf1_raw_latency.csv`.
- [ ] Sanity-check the CSV: 22 queries x 5 runs = 110 rows (plus header),
  every `latency_ms` is a positive number, no query is missing all 5 of
  its runs.
- [ ] Clean up the pod and configmap.
- [ ] Commit the results file:
  ```bash
  git add benchmark/results/tpch_sf1_raw_latency.csv
  git commit -m "chore: add TPC-H SF1 raw-engine latency results"
  git push
  ```

---

### Task 9 (MAIN SESSION — interactive, not a subagent): Run the load test

- [ ] Confirm `lakehouse-ui` is currently `Synced`/`Healthy` before
  starting (a load test against an already-unhealthy app measures the
  wrong thing).
- [ ] Run `benchmark/load_test.py` in-cluster per its own docstring. This
  runs for `4 levels x 60s + 3 x 10s cooldown` ≈ 4.5 minutes — watch it
  live (`kubectl logs -f load-test -n lakehouse`), and in a second
  terminal/pane keep an eye on `kubectl get pods -n lakehouse -w` for
  `lakehouse-ui` so a crash/restart during the test is caught in the
  moment, not discovered afterward from a gap in the data.
- [ ] Once the pod reaches `Succeeded`, `kubectl cp` the CSV out to
  `benchmark/results/load_test_raw.csv`.
- [ ] Note the per-level summary lines the script printed (p50/p95/error
  rate at each concurrency level) — if `lakehouse-ui` crashed or restarted
  during the test, that's a legitimate and important finding, not a
  failed run; record what happened (which level, what the pod events/logs
  showed) for the report in Task 10.
- [ ] Clean up the pod and configmap.
- [ ] Commit the results file:
  ```bash
  git add benchmark/results/load_test_raw.csv
  git commit -m "chore: add load test results"
  git push
  ```

---

### Task 10 (MAIN SESSION — interactive, not a subagent): Research, compare, and publish the report

- [ ] Research genuinely published TPC-H SF1 (or clearly-noted other
  scale factor) numbers via live web search — at minimum, DuckDB's own
  published TPC-H numbers (duckdb.org blog/docs), since that's the same
  engine unqualified and isolates what the Polaris/Iceberg/MinIO layer
  costs. Add other credible, clearly-sourced TPC-H-derived figures
  (Snowflake/BigQuery/Redshift/ClickHouse) where genuinely available —
  do not fabricate or estimate numbers that aren't actually published.
- [ ] Compute summary stats from `benchmark/results/tpch_sf1_raw_latency.csv`
  (per-query median of the 4 warm runs, cold-vs-warm gap) and from
  `benchmark/results/load_test_raw.csv` (p50/p95/throughput/error-rate per
  concurrency level).
- [ ] Load the `dataviz` skill, then build and publish an HTML Artifact
  report with: a per-query latency chart (our warm-median numbers next to
  the published baselines, clearly labeled with each source), a
  cold-vs-warm chart, and a concurrency-scaling chart (latency/throughput/
  error-rate vs. concurrency level) from the load test. Include the
  "TPC-H-derived, not an official TPC-H Result" framing from the design
  doc's intro directly in the report, not just in this repo's docs.
- [ ] Send the user the artifact link.

---

### Task 11 (MAIN SESSION — interactive, not a subagent): Append verification results to the design doc

- [ ] Work through the design doc's Definition of Done item by item,
  confirming each live: row counts, all 22 queries ran without error,
  load test completed all 4 levels (or documents clearly where/why it
  didn't), report published with at least the DuckDB-native comparison,
  results CSVs committed, README accurate.
- [ ] Append a "Verification results" section to
  `docs/superpowers/specs/2026-09-14-tpch-benchmark-load-test-design.md`
  (same pattern as every prior phase this session) — what the numbers
  actually showed, the report Artifact link, anything that needed a live
  fix along the way.
- [ ] Commit and push:
  ```bash
  git add docs/superpowers/specs/2026-09-14-tpch-benchmark-load-test-design.md
  git commit -m "docs: append verification results to the TPC-H benchmark design doc"
  git push
  ```
