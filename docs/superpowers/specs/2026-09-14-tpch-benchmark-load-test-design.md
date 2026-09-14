# TPC-H Benchmark & Load Test Design

**Goal:** Get a real, defensible sense of how this lakehouse stack (DuckDB →
Polaris REST catalog → Iceberg → MinIO) performs, both in absolute terms
(compared to published TPC-H-derived numbers for other engines) and under
concurrent load through the actual `lakehouse-ui` web app.

**Scope:** A new `benchmark/` sub-project in `test-k8s-configs`, independent
of (and not modifying) the existing `analytics/` NYC taxi data model. Uses
TPC-H at scale factor 1 (~1GB, ~6M rows across 8 tables).

**A note on naming, up front:** the TPC's Fair Use Policy restricts who may
call a result an official "TPC-H Result" — that requires a formal audited
submission. What we produce here is an unofficial, single-node run against
our own stack, reported alongside other publicly available *TPC-H-derived*
numbers (the same term the TPC itself asks unaudited publishers to use).
Nothing here is "the TPC-H benchmark result" for this system — it's a
useful, honestly-labeled data point.

---

## Architecture

Four phases, each a script in the new `benchmark/` directory (same
one-off-in-cluster-pod pattern already established by `analytics/`):

```
Phase 1: tpch_load.py   — generate SF1 data, write it into lakehouse.tpch.*
Phase 2: tpch_bench.py  — run the 22 standard queries directly, record latency
Phase 3: load_test.py   — concurrent HTTP load against lakehouse-ui's /query
Phase 4: report         — published HTML artifact comparing our numbers to
                           publicly available reference points
```

### Phase 1 — Load TPC-H SF1 data

`benchmark/tpch_load.py`, run in-cluster (same shape as
`analytics/load_trips.py`: a one-off pod using the `loader` principal's
credentials from `loader-polaris-credentials`).

- `INSTALL tpch; LOAD tpch; CALL dbgen(sf=1);` generates the 8 standard
  TPC-H tables (`region`, `nation`, `supplier`, `customer`, `part`,
  `partsupp`, `orders`, `lineitem`) in an in-memory DuckDB — no network
  I/O, this is pure local generation.
- ATTACH the `lakehouse` Iceberg catalog (identical secret/endpoint
  pattern to every other script in this repo) and `CREATE TABLE
  lakehouse.tpch.<name> AS SELECT * FROM <generated table>` for each of
  the 8 tables.
- **New namespace, not a new principal or grant.** `tpch` is added as a
  second entry under the existing `lakehouse` catalog's `namespaces:` list
  in `bootstrap/polaris-setup-config.yaml` (that file is explicitly
  create-only/idempotent — re-running `polaris setup apply` only adds the
  new namespace, it doesn't touch `nyc_taxi` or anything else). The
  `loader` principal's existing `CATALOG_MANAGE_CONTENT` grant is already
  catalog-wide, so no new grant is needed for it to write here.
- **Correctness check, not just "it ran":** after loading, assert each
  table's row count against the well-known TPC-H SF1 reference counts
  (`region`=5, `nation`=25, `supplier`=10,000, `customer`=150,000,
  `part`=200,000, `partsupp`=800,000, `orders`=1,500,000,
  `lineitem`≈6,001,215). A mismatch fails the script loudly rather than
  silently benchmarking a partial/corrupt load.

### Phase 2 — Raw-engine benchmark

`benchmark/tpch_bench.py`, also in-cluster, same `loader` credentials.

- ATTACH the catalog, then `USE lakehouse.tpch;` so the standard queries'
  unqualified table references (`FROM lineitem`, `FROM orders`, ...)
  resolve without editing the canonical query text.
- DuckDB's `tpch` extension exposes the 22 official query texts via `SELECT
  query_nr, query FROM tpch_queries()` — we use those verbatim, not
  hand-copied text, so there's no risk of transcription drift from the
  real TPC-H query set.
- For each of the 22 queries: run it 5 times (1 cold + 4 warm) and record
  every individual timing, not just an average — cold-vs-warm matters a
  lot here specifically *because* of the extra network hops our stack adds
  (Polaris metadata calls, MinIO credential vending) that a published
  DuckDB-native number wouldn't have to pay. Expect our numbers to be
  visibly slower than DuckDB-native, especially cold — that gap is itself
  one of the interesting things this benchmark should surface, not
  something to explain away.
- Output: a CSV (`query_nr, run_number, latency_ms`) written inside the
  pod and pulled out via `kubectl cp` once the pod completes, landing in
  `benchmark/results/tpch_sf1_raw_latency.csv` in the repo.

### Phase 3 — Load test (through the real app)

`benchmark/load_test.py`, run as an in-cluster pod so it talks to
`http://lakehouse-ui.lakehouse.svc.cluster.local:8000` directly (cluster
DNS, not the flaky local `kubectl port-forward` tunnel — the load test
should measure the app's own latency, not this session's tunnel
stability).

- Logs in once via `/login` using `loader`'s credentials, keeps the
  resulting session cookie for every request (matches how one real logged
  in user's browser would behave — the backend already does a fresh
  DuckDB connection per request regardless of how many requests share a
  cookie, so this doesn't artificially serialize anything).
- Plain Python (`concurrent.futures.ThreadPoolExecutor` + `httpx`), not a
  separate load-testing framework — keeps this consistent with every other
  script in the repo, and the need here (a handful of concurrency levels,
  a few minutes each) doesn't justify a new dependency.
- Runs at concurrency levels **1, 5, 10, 20**, sequentially (not
  simultaneously — each level needs to see a clean baseline, not
  leftover load from the previous one), with a short cooldown between
  levels. At each level, each "virtual user" loops picking a random query
  from the 22 and POSTing it to `/query` for a fixed 60 seconds.
- Records per-request: concurrency level, query number, latency, HTTP
  status. Output: `benchmark/results/load_test_raw.csv`, pulled out via
  `kubectl cp` the same way as Phase 2.
- This directly re-exercises the row-cap/liveness-probe fix from earlier
  this session, now under sustained concurrent load rather than one query
  — a real regression here (crashes, growing error rate, probe failures)
  is exactly the kind of thing a load test exists to catch.

### Phase 4 — Comparison & report

- I pull genuinely published reference numbers via live web research (not
  invented): DuckDB's own published TPC-H SF1 numbers are the most
  directly relevant baseline, since it's the *same query engine*
  unqualified — comparing our Phase 2 numbers to those isolates what the
  Polaris/Iceberg/MinIO layer costs, which is really the core question.
  Where publicly available, I'll add commonly-cited TPC-H-derived figures
  for other warehouses (Snowflake, BigQuery, Redshift, ClickHouse) for
  broader context, clearly labeled with their source, scale factor, and
  hardware, since those won't be apples-to-apples with a laptop's
  docker-desktop cluster.
- Deliverable: a published HTML Artifact with per-query latency charts
  (ours vs. baselines) and a concurrency-scaling chart from Phase 3
  (latency/throughput/error-rate vs. concurrency level), plus a short
  written summary of what the numbers actually show. Raw data stays in
  the repo (`benchmark/results/`) so the charts are reproducible from
  source, not just images.

---

## Data flow

```
Phase 1:  dbgen(sf=1) [in-memory]  →  lakehouse.tpch.* [Iceberg on MinIO]

Phase 2:  lakehouse.tpch.* (via Polaris + MinIO) → 22 queries × 5 runs
          → benchmark/results/tpch_sf1_raw_latency.csv

Phase 3:  N concurrent HTTP workers → lakehouse-ui:8000/query
          → DuckDB (in lakehouse-ui pod) → lakehouse.tpch.*
          → benchmark/results/load_test_raw.csv

Phase 4:  our CSVs + published reference numbers (web research)
          → HTML report Artifact with charts
```

## Verification / Definition of Done

- All 8 TPC-H tables loaded with row counts matching the known SF1
  reference counts exactly.
- All 22 standard queries run successfully against `lakehouse.tpch.*`
  with recorded latencies (no unhandled errors) — `tpch_queries()`'s
  canonical text used verbatim.
- Load test completes all 4 concurrency levels; if the app degrades
  (rising error rate, timeouts, restarts), that's a valid and expected
  finding to report, not a failure of the test itself — the DoD is that
  we *know* where the ceiling is, not that there isn't one.
- A published HTML report exists, comparing our raw-engine numbers to at
  least DuckDB's own published SF1 numbers, plus whatever other
  credible public reference points turn up, each clearly sourced.
- `benchmark/results/*.csv` committed to the repo alongside the scripts
  that produced them.
- `benchmark/README.md` documents how to re-run each phase (matching
  `analytics/README.md`'s existing style).

## Explicitly out of scope

- TPC-H Power Test / Throughput Test as TPC formally defines them (fixed
  stream counts, specific update-function timing) — we're doing an
  informal latency + concurrency read, not an audit-ready procedure.
- Scale factors beyond SF1 (a natural follow-up, not this pass — see the
  earlier discussion on local-cluster resource limits).
- ClickBench or any second standard benchmark (deferred; TPC-H is the one
  this pass covers, per the discussion above).
- Changing anything about the existing `nyc_taxi` data, model, or
  `analytics/` scripts — this is a fully separate namespace and script
  set.
- Tuning or reconfiguring the stack in response to what the benchmark
  finds — this pass measures, it doesn't optimize. Any follow-up
  performance work based on the results is a separate future project.

## Verification results (2026-09-14, live against the real cluster)

All Definition of Done items above were verified live. Three real bugs
surfaced during Tasks 6-9 that no amount of code review would have
caught, because none of them exercised the actual live path:

1. **`kubectl cp` failed on every completed pod.** All three one-off
   benchmark pods use `restart=Never`, and once a container reaches
   `Succeeded` there's nothing left to exec into — `kubectl cp` execs a
   `tar` under the hood, so it failed 100% of the time on a pod that had
   already finished. Fixed by appending `&& sleep 300` to each pod's
   command, keeping the container alive long enough to `cp` the results
   out before deleting it. The very first `tpch_bench.py` run's CSV had
   to be reconstructed from `kubectl logs` output instead (`kubectl logs`
   still works on a completed pod) — all 110 timings were present in the
   log text and parsed out losslessly, confirmed by re-diffing row counts
   against the printed per-query summaries.
2. **`load_test.py`'s first real run hit a 100% error rate at every
   concurrency level** — before any memory pressure was involved.
   `tpch_queries()`'s canonical text uses unqualified table names (`FROM
   lineitem`), which resolve fine inside `tpch_bench.py`'s single
   long-lived connection (it runs `USE lakehouse.tpch` once), but
   `lakehouse-ui`'s `/query` route builds a fresh connection per HTTP
   request with no default schema — every request 400'd with "Table with
   name lineitem does not exist." Reproduced directly with `curl` both
   ways to confirm before fixing; fixed by prepending `USE
   lakehouse.tpch;` to each query before sending it.
3. **One crash poisoned every request after it.** `lakehouse-ui`'s
   session store is in-memory (`app/session.py`); `load_test.py` logged
   in exactly once at the start. When the app OOMKilled under
   concurrency=5 (see below) and restarted, every subsequent request
   401'd for the rest of that run, even long after the app itself had
   recovered — poisoning what should have been 3 of the 4 concurrency
   levels' data. Fixed by re-logging in (behind a lock, so concurrent
   workers don't all hammer `/login` at once) whenever a request comes
   back 401, then retrying it once. Rerunning after this fix produced the
   complete, honest 4-level dataset reported below.

With all three fixed, every Definition of Done item was reverified:

- ✅ All 8 TPC-H tables loaded with row counts matching the known SF1
  reference counts exactly, including the canonical `lineitem: 6,001,215`
  — independently reconfirmed through `lakehouse-ui`'s own `/query`
  endpoint (a different code path than the loader script), not just the
  loader script's own assertion.
- ✅ All 22 standard queries ran successfully (5 runs each = 110 timings)
  against the real stack with no errors. Sum of warm-median latencies
  across all 22 queries: ~2.0s. Sum of cold-run latencies: ~5.1s (a 2.5×
  penalty for the extra Polaris metadata fetch + MinIO credential vend
  every first touch pays).
- ✅ Load test completed all 4 concurrency levels (5,276 total requests)
  — and found a real ceiling, not a clean scaling curve: concurrency=1
  ran perfectly (0% errors, p50=2,012ms), concurrency=5 collapsed to
  99.7% errors as `lakehouse-ui` (512Mi memory limit) repeatedly
  OOMKilled under concurrent analytical joins, confirmed directly via
  `kubectl describe pod` (`reason: OOMKilled`, exit code 137) and a
  genuine `CrashLoopBackOff` that only cleared once load stopped.
  Concurrency=10 and 20 showed the same near-total failure — this is
  exactly the kind of finding the DoD anticipated as valid: knowing where
  the ceiling is, not that there isn't one.
- ✅ Published HTML report compares our raw-engine numbers to DuckDB's
  own published SF1 single-thread numbers (3 queries with a sourced
  reference point: Q1, Q9, Q19, from a real GitHub issue discussion —
  disclosed that a complete published SF1 table across all 22 queries
  wasn't found in public sources checked), with per-query, cold-vs-warm,
  and concurrency-scaling charts. Report:
  https://claude.ai/code/artifact/5f1b6c05-aa6e-448c-8b97-2513b3fc6b86
- ✅ `benchmark/results/tpch_sf1_raw_latency.csv` (110 rows) and
  `benchmark/results/load_test_raw.csv` (5,276 rows) committed to the
  repo.
- ✅ `benchmark/README.md` accurate — it defers to each script's own
  docstring for the exact run commands, so the `sleep 300` /
  session-resilience fixes above didn't require a separate README update.
