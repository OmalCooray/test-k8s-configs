"""One-off raw data load for lakehouse.nyc_taxi.trips.

The very first thing that has to exist before analytics/nyc_taxi_model.sql
(or anything else in the nyc_taxi namespace) can run. Not previously saved
to this repo — reconstructed live on 2026-09-14 after a full cluster reset
required rebuilding it from scratch, which is exactly why it's checked in
now: a fresh cluster should never again depend on someone's memory of the
public data URL and DuckDB-Iceberg attach incantation.

Safe to re-run: DROPs and recreates the table, so a partial/bad prior load
doesn't linger.

## Running it

Same constraint as analytics/run_model.py: has to run inside the cluster
(Polaris vends storage credentials scoped to the in-cluster MinIO DNS name
`http://minio:9000`, unreachable from a `kubectl port-forward`ed local
process).

    kubectl create configmap nyc-taxi-load -n lakehouse \\
      --from-file=load_trips.py=analytics/load_trips.py

    kubectl run nyc-taxi-load --rm -it --restart=Never -n lakehouse \\
      --image=python:3.12-slim \\
      --overrides='{
        "spec": {
          "containers": [{
            "name": "runner",
            "image": "python:3.12-slim",
            "command": ["/bin/sh", "-c", "pip install --quiet duckdb==1.5.3 && python /scripts/load_trips.py"],
            "env": [
              {"name": "POLARIS_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
              {"name": "POLARIS_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
            ],
            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
          }],
          "volumes": [{"name": "script", "configMap": {"name": "nyc-taxi-load"}}]
        }
      }'

    kubectl delete configmap nyc-taxi-load -n lakehouse

Uses the `loader` principal (CATALOG_MANAGE_CONTENT on the `lakehouse`
catalog) — same one analytics/run_model.py uses. `lakehouse-ui`'s principal
is read-only and can't run this.
"""
import os

import duckdb

# One month of NYC Yellow Taxi trip data, public TLC open-data Parquet.
# Small enough for a quick reload (a few hundred thousand rows), large
# enough to be a meaningful demo dataset. Bump the month here to load more.
SOURCE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet"

client_id = os.environ["POLARIS_CLIENT_ID"]
client_secret = os.environ["POLARIS_CLIENT_SECRET"]

con = duckdb.connect(":memory:")
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

print(f"Loading {SOURCE_URL} ...", flush=True)
# DuckDB-Iceberg doesn't support CREATE OR REPLACE TABLE (found live,
# analytics/nyc_taxi_model.sql's header comment) — DROP + CREATE instead.
try:
    con.execute("DROP TABLE lakehouse.nyc_taxi.trips")
except duckdb.Error as exc:
    if "does not exist" not in str(exc).lower():
        raise
    print("  (table did not exist yet, skipping drop)", flush=True)

con.execute(
    f"CREATE TABLE lakehouse.nyc_taxi.trips AS SELECT * FROM read_parquet('{SOURCE_URL}')"
)

count = con.execute("SELECT count(*) FROM lakehouse.nyc_taxi.trips").fetchone()[0]
print(f"\nDone. lakehouse.nyc_taxi.trips: {count} rows", flush=True)
