# Iceberg Lakehouse (MinIO + Iceberg + Polaris + DuckDB) — Design

**Status:** Approved for planning
**Date:** 2026-09-11

## Goal

Stand up an open-source data lakehouse on the existing local Kubernetes cluster,
deployed via Argo CD (using the `argocd-gitops-plugin`, same as every other app
in this repo): S3-compatible object storage (MinIO) holding Apache Iceberg
tables, cataloged by Apache Polaris (Iceberg REST catalog), queried by DuckDB.
Prove the full read/write path with a real sample dataset, then expose it
through a small self-built web UI so queries can be run without a local
DuckDB install or port-forward.

## Why

- Rounds out the cluster's data platform story (Airflow orchestrates, MySQL is
  OLTP, Metabase/Trino/Grafana query and visualize) with an open table-format
  lakehouse — the pattern most modern data platforms converge on.
- Further dogfoods `argocd-gitops-plugin` against 3 more upstream Helm charts
  (MinIO, Postgres, Polaris), and — deliberately — exercises the one thing the
  plugin's roadmap explicitly deferred: deploying an app the user builds
  themselves (`lakehouse-ui`), which this design keeps *outside* the plugin's
  chart-wrapping workflow rather than expanding plugin scope to cover it.

## Architecture

Four layers, each independently swappable:

```
DuckDB (query engine)
  │ reads/writes via Iceberg REST Catalog API (CREATE SECRET + ATTACH)
Apache Polaris (metadata catalog)
  │ tracks tables/schemas/snapshots; persists to Postgres; points into storage
Apache Iceberg (table format — not a service, a file layout: Parquet + Avro + JSON)
  │ data/metadata files stored as objects
MinIO (S3-compatible object storage)
```

Deployed as 4 new Argo CD Applications in `environments/local/`, plus one new
repo for the custom UI:

```
┌─────────────────────────────────────────────────────────────┐
│ test-k8s-configs (this repo)                                 │
│                                                                │
│  charts/minio/            → Application: minio                │
│  charts/polaris-postgres/ → Application: polaris-postgres      │
│  charts/polaris/          → Application: polaris                │
│  charts/lakehouse-ui/     → Application: lakehouse-ui           │
│    (thin chart: Deployment + ClusterIP Service,                 │
│     image pulled from ghcr.io/omalcooray/lakehouse-ui)          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ lakehouse-ui (new repo)                                       │
│                                                                │
│  Dockerfile, FastAPI app, one HTML page                        │
│  .github/workflows/build.yml → builds + pushes image to GHCR    │
│    on merge to main (public, ghcr.io/omalcooray/lakehouse-ui)   │
└─────────────────────────────────────────────────────────────┘
```

## Components

### `minio`

Official MinIO Helm chart, standalone mode (single node — this is a local dev
cluster, not a distributed-storage exercise). One bucket, `lakehouse`, created
at install time via the chart's bucket-provisioning job. Credentials
(access/secret key) generated as a stable out-of-band Secret (same pattern as
Airflow's fernet-key secret — not chart-managed, so it survives re-syncs).

### `polaris-postgres`

A small, single-purpose Postgres (not shared with the MySQL apps — Polaris's
`relational-jdbc` backend supports Postgres and H2 only, not MySQL). One
database, `polaris`. Credentials as a stable out-of-band Secret.

### `polaris`

Official Apache Polaris Helm chart
(`helm repo add polaris https://downloads.apache.org/polaris/helm-chart`),
configured with `persistence.type=relational-jdbc` pointed at
`polaris-postgres` via the Secret above (`QUARKUS_DATASOURCE_JDBC_URL`,
`_USERNAME`, `_PASSWORD`).

**Bootstrap.** The Polaris chart does not include a bootstrap Job — the realm
and root principal must be created via `apache/polaris-admin-tool bootstrap`
against the same Postgres backend. This is the same shape as Airflow's
`createUserJob`: a Kubernetes Job wired as an Argo CD PreSync hook with
`hook-delete-policy: BeforeHookCreation`, so it reruns cleanly on every sync
without erroring on "already bootstrapped." Its generated root credential is
written to a Secret, out-of-band, not chart-managed.

**Catalog + scoped principal setup.** After bootstrap, a second idempotent
step (a script, re-runnable, documented in this repo — not a one-time manual
curl transcript) calls Polaris's REST API as root to:
1. Create the `lakehouse` catalog, pointed at the MinIO bucket + endpoint
   (storage config: `S3` type, custom endpoint override for MinIO, path-style
   access).
2. Create a namespace (e.g. `nyc_taxi`) in that catalog.
3. Create a scoped principal + role (read/write on the `lakehouse` catalog
   only) for DuckDB/UI use — root's credential is never handed to a client.

This runs as a second PreSync-hook Job (higher sync-wave than the bootstrap
Job, so it only runs once Polaris itself is Synced/Healthy), idempotent
(checks for existing catalog/namespace/principal before creating).

### Data load

A one-off, local step (not a cluster workload): DuckDB, port-forwarded to
Polaris (`:8181`) and MinIO (`:9000`), reads 1–2 months of NYC Yellow Taxi
trip data (public Parquet from the TLC open-data bucket), attaches the
`lakehouse` catalog with the scoped principal's credentials and
`ACCESS_DELEGATION_MODE 'vended_credentials'` (Polaris vends short-lived S3
credentials per request — no static MinIO keys ever configured in DuckDB or
the UI), and runs `CREATE TABLE nyc_taxi.trips AS SELECT * FROM
read_parquet(...)`. A fresh DuckDB session re-attaches and queries the table
back, as the first end-to-end proof.

### `lakehouse-ui` (new repo)

- **Backend:** FastAPI, one `POST /query` endpoint — takes a SQL string, runs
  it against an in-process DuckDB connection that attaches the `lakehouse`
  Polaris catalog at startup (scoped principal's credentials from a mounted
  Secret), returns rows as JSON. Read-focused; no auth on the endpoint itself
  for v1 (local cluster, not internet-facing) — noted as a gap if this ever
  moves beyond a local dev cluster.
- **Frontend:** one static HTML page — a textarea, a "Run" button, a results
  table. No JS framework, no build step; served directly by FastAPI as a
  static file. Deliberately minimal, matching "simple interactive UI."
- **Image:** built and pushed to `ghcr.io/omalcooray/lakehouse-ui` (public)
  by a GitHub Actions workflow on merge to the repo's main branch.
- **Chart:** a thin hand-written chart in `test-k8s-configs/charts/lakehouse-ui/`
  — Deployment (image tag pinned, not `:latest`) and a `ClusterIP` Service.
  No ingress: this cluster runs no ingress controller today (checked live —
  no Traefik, no `IngressClass` at all), and every other UI here (Grafana,
  Argo CD, Airflow, Metabase) is already reached via `kubectl port-forward`,
  so `lakehouse-ui` matches that rather than introducing one.
  This chart is *not* produced by the plugin's chart-wrapping workflow, since
  there is no upstream chart to wrap — it's written by hand, the same way any
  hand-rolled Application would be, per the roadmap's "own-app charts are out
  of scope for 1.0."

## Data flow

```
Write (one-off, local DuckDB):
  TLC public Parquet → DuckDB → Iceberg files in MinIO (lakehouse bucket),
  registered in Polaris (nyc_taxi.trips)

Read (repeatable, via UI):
  browser → lakehouse-ui (FastAPI) → DuckDB (in-process, attached to Polaris)
  → Polaris resolves table metadata + vends MinIO credentials
  → DuckDB reads Iceberg/Parquet files from MinIO → rows → JSON → browser
```

## Verification / Definition of Done

- `minio`, `polaris-postgres`, `polaris`, `lakehouse-ui` Argo CD Applications
  all Synced and Healthy.
- The one-off data load completes; a fresh local DuckDB session, reattaching
  the catalog, returns the expected row count for `nyc_taxi.trips`.
- `lakehouse-ui` is reachable via `kubectl port-forward`; submitting
  `SELECT count(*) FROM nyc_taxi.trips` (and a couple of real aggregate
  queries) in the browser returns correct results.
- Independent confirmation outside the DuckDB path: `mc ls` (or the S3 API)
  against the `lakehouse` bucket shows Iceberg data/metadata files; Polaris's
  REST API (`/api/catalog/v1/lakehouse/namespaces/nyc_taxi/tables`) lists the
  `trips` table.
- `lakehouse-ui`'s GitHub Actions workflow builds and pushes a tagged image on
  a merge to main, and that tag is what's actually deployed (no `:latest` in
  the cluster).

## Verification results (2026-09-12)

All Definition of Done items above are met. Notes from actually running it:

- **`ACCESS_DELEGATION_MODE 'vended_credentials'` works** — but only once
  Polaris itself was given a static MinIO credential to hand out. MinIO
  isn't a real AWS STS provider, so the first attempt failed with
  `SdkClientException: Unable to load credentials from any of the providers
  in the chain`. Fixed by setting the polaris chart's `storage.secret`
  (`awsAccessKeyId`/`awsSecretAccessKey`, out-of-band Secret
  `polaris-storage-credentials`, the same MinIO root creds as
  `minio-credentials`) — see `charts/polaris/values.yaml`. After that,
  vended credentials worked end-to-end with no static keys anywhere in
  DuckDB or `lakehouse-ui`, as originally designed.
- **The one-off load ran as an in-cluster Job, not local DuckDB.** The
  design assumed port-forwarding both Polaris and MinIO to a local DuckDB
  session — that mostly works (catalog metadata calls go fine over the
  Polaris port-forward), but the vended credentials response points DuckDB
  at Polaris's *configured* storage endpoint (`http://minio:9000`, the
  in-cluster DNS name) for the actual S3 read/write, which a local process
  can't resolve. Loading from a scratch `kubectl run`/Job pod inside the
  `lakehouse` namespace (where `minio`/`polaris` resolve normally) avoided
  the mismatch entirely. Local DuckDB sessions work fine for *querying*
  through `lakehouse-ui` (the FastAPI backend runs in-cluster too, for the
  same reason) — the port-forward approach only breaks for a client
  actually writing new data from outside the cluster.
- **Two bugs surfaced only under real use, both fixed:**
  1. `app/catalog.py`'s `CREATE SECRET` needed an `ENDPOINT` field, not just
     `ATTACH` — the DuckDB version in use rejects an iceberg secret with no
     endpoint/oauth2_server_uri. Fixed in
     [OmalCooray/lakehouse-ui@d23d8d7](https://github.com/OmalCooray/lakehouse-ui/commit/d23d8d7).
  2. `lakehouse-ui`'s `readOnlyRootFilesystem: true` (added during Task 11's
     hardening review) blocked DuckDB from writing `~/.duckdb` — the
     hardening's own smoke test only checked `/healthz`, which never
     touches DuckDB, so this passed review but broke on the first real
     query. Fixed with two `emptyDir` mounts
     (`charts/lakehouse-ui/templates/deployment.yaml`).
- Queries against the catalog need the `lakehouse.` prefix
  (`lakehouse.nyc_taxi.trips`, not `nyc_taxi.trips`) — `ATTACH` doesn't make
  a catalog DuckDB's default. Fixed the UI's placeholder text to match.

## Explicitly out of scope for this pass

- Distributed/multi-node MinIO — single node is enough for a local cluster.
- Auth on the `lakehouse-ui` query endpoint — local cluster only, not
  internet-facing; flagged as a gap to close before this goes anywhere less
  trusted.
- Recurring/scheduled data loads (e.g. an Airflow DAG landing new taxi months
  automatically) — the one-off local load is enough to prove the stack;
  automating ingestion is a natural follow-up, not required here.
- Extending `argocd-gitops-plugin` to formally support "own-app" charts —
  `lakehouse-ui`'s chart is hand-written for this one case; if this pattern
  recurs, that's the trigger to revisit plugin scope, not this design.
- Write access from `lakehouse-ui` (query-only UI for v1).
