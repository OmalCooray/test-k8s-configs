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
