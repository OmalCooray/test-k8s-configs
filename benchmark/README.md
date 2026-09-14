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
