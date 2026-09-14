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
            "command": ["/bin/sh", "-c", "pip install --quiet httpx==0.27.2 duckdb==1.5.3 && python /scripts/load_test.py && sleep 300"],
            "env": [
              {"name": "LOADER_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
              {"name": "LOADER_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
            ],
            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
          }],
          "volumes": [{"name": "script", "configMap": {"name": "load-test"}}]
        }
      }'

    kubectl logs -f load-test -n lakehouse   # watch until the "Done." line
    kubectl cp lakehouse/load-test:/tmp/load_test_raw.csv benchmark/results/load_test_raw.csv
    kubectl delete pod load-test -n lakehouse
    kubectl delete configmap load-test -n lakehouse

(This needs duckdb installed too, only to fetch the 22 canonical query
texts from tpch_queries() locally -- it never attaches the lakehouse
catalog itself, all querying happens over HTTP against lakehouse-ui. The
trailing `&& sleep 300` matters: `kubectl cp` execs into the container to
run `tar`, which fails once the container has already exited -- confirmed
live, 2026-09-14, a `restart=Never` pod that already reached `Succeeded`
can't be exec'd into at all. Run `kubectl cp` as soon as you see the
script's own "Done." line in the logs; `kubectl delete pod` cleans up
immediately once you're done, you don't have to wait out the full 300s.)
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
# tpch_bench.py used for the raw-engine numbers. Their FROM clauses are
# unqualified (`FROM lineitem`, not `FROM lakehouse.tpch.lineitem`) --
# tpch_bench.py handles that with its own connection's `USE
# lakehouse.tpch`, but lakehouse-ui's /query route builds a brand new
# connection per request with no default schema, so an unqualified query
# submitted as-is 400s with "Table with name lineitem does not exist"
# (confirmed live, 2026-09-14: the first real run of this script hit a
# 100% error rate at every concurrency level for exactly this reason).
# Prepending `USE lakehouse.tpch;` as a second statement works because
# /query already supports multi-statement SQL (main.py's own
# test_query_allows_multi_statement_sql_now covers this).
_con = duckdb.connect(":memory:")
_con.execute("INSTALL tpch")
_con.execute("LOAD tpch")
_raw_queries = _con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall()
_con.close()
QUERIES = [(query_nr, f"USE lakehouse.tpch;\n{query}") for query_nr, query in _raw_queries]
print(f"Loaded {len(QUERIES)} queries to drive the load test with.", flush=True)

client = httpx.Client(base_url=BASE_URL, timeout=120.0)
login_lock = threading.Lock()


def _login() -> None:
    resp = client.post("/login", json={"client_id": client_id, "client_secret": client_secret})
    resp.raise_for_status()


_login()
print("Logged in.", flush=True)

results = []
results_lock = threading.Lock()


def worker(concurrency_level: int, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        query_nr, query_text = random.choice(QUERIES)
        started = time.monotonic()
        try:
            resp = client.post("/query", json={"sql": query_text})
            if resp.status_code == 401:
                # The session is server-side in-memory (app/session.py) --
                # a restart (e.g. the liveness-probe-driven one this
                # benchmark itself triggered live, 2026-09-14) wipes it,
                # and every request would otherwise 401 for the rest of
                # the run. Re-login once and retry rather than let one
                # restart poison every remaining concurrency level's data.
                with login_lock:
                    _login()
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
