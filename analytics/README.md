# NYC taxi data model

A small SQL data model on top of `lakehouse.nyc_taxi.trips` (the raw table
loaded per the design spec). No dbt — just `nyc_taxi_model.sql`, run by
`run_model.py`. Safe to re-run any time `trips` is refreshed.

```
trips (raw)
  │
  ├── dim_vendor, dim_payment_type, dim_rate_code   -- small lookups,
  │                                                     NYC TLC data dictionary
  │
  └── fct_trips        -- cleaned, typed, joined to the dims above;
        │                  obviously-bad rows dropped (negative amounts,
        │                  non-positive distance, dropoff <= pickup)
        │
        └── mart_daily_summary   -- daily rollup by vendor + payment type
```

All 5 tables live in the same `nyc_taxi` namespace as the raw table. Readers
of the `lakehouse` catalog (e.g. `lakehouse-ui`, whose grant is at the
catalog level) can query them immediately — no separate grant needed for
tables created after the grant.

## Running it

This has to run **inside the cluster**, not locally with `kubectl
port-forward` — Polaris vends credentials scoped to its configured storage
endpoint (`http://minio:9000`, the in-cluster DNS name), which a local
process can't resolve. Same reason the initial `trips` load ran this way.

```bash
kubectl create configmap nyc-taxi-model -n lakehouse \
  --from-file=run_model.py=analytics/run_model.py \
  --from-file=nyc_taxi_model.sql=analytics/nyc_taxi_model.sql

kubectl run nyc-taxi-model --rm -it --restart=Never -n lakehouse \
  --image=python:3.12-slim \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "runner",
        "image": "python:3.12-slim",
        "command": ["/bin/sh", "-c", "pip install --quiet duckdb==1.5.3 && python /scripts/run_model.py"],
        "env": [
          {"name": "POLARIS_CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_ID"}}},
          {"name": "POLARIS_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "loader-polaris-credentials", "key": "CLIENT_SECRET"}}}
        ],
        "volumeMounts": [{"name": "script", "mountPath": "/scripts"}]
      }],
      "volumes": [{"name": "script", "configMap": {"name": "nyc-taxi-model"}}]
    }
  }'

kubectl delete configmap nyc-taxi-model -n lakehouse
```

Uses the `loader` principal (`CATALOG_MANAGE_CONTENT` on the `lakehouse`
catalog) — the same one the initial data load used. `lakehouse-ui`'s
principal is read-only and can't run this.

## Known data quality note

A handful of raw rows have wildly wrong pickup dates (e.g. `2002-12-31`,
`2009-01-01`) despite otherwise-plausible fare/distance/duration values —
they pass the current filters (which only reject negative amounts,
non-positive distance, and dropoff-before-pickup) since nothing in the
source flags them as invalid. Not fixed here since it's a genuine judgment
call (what date range counts as "obviously wrong" for a moving dataset);
worth a `pickup_date BETWEEN ...` filter if it matters for your use of
`mart_daily_summary`.
