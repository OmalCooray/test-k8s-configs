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
