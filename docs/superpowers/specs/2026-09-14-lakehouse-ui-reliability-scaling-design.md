# lakehouse-ui Reliability & Scaling Design

**Goal:** Fix the concurrency ceiling the TPC-H load test found (OOM at 5
concurrent users), and make `lakehouse-ui` survive a pod restart and run
more than one replica — the first of several independent "production
grade" sub-projects for this system (see below for the others, deferred).

**Scope:** `lakehouse-ui` only. Postgres's and MinIO's own high
availability are separate, larger projects, not touched here.

**Not in this pass** (the other four sub-projects identified when this
work was scoped): security hardening (TLS, secure cookies, secret
rotation, rate limiting, audit trail), observability (metrics/dashboards/
alerting), data governance & warehouse features (finer RBAC, query
cancellation, saved/scheduled queries, materialized views, result
caching), backup & disaster recovery (Postgres/MinIO backups, a recovery
runbook). Each gets its own design when picked up.

---

## Architecture

Three independent pieces, in `lakehouse-ui` (app code) and
`test-k8s-configs` (chart):

### 1. Sessions move to Postgres

Today, `app/session.py` holds sessions in an in-memory
`dict[str, Session]` — wiped on every pod restart (confirmed live during
the benchmark: an OOM restart mid-load-test logged out every in-flight
request), and unshareable across replicas, which is why `lakehouse-ui`
has stayed at `replicas: 1` since it was built.

- **New file `app/db.py`**: extracts the Postgres connection helper
  `app/history.py` already has (`_connect()`, reading
  `LAKEHOUSE_UI_DB_HOST/_NAME/_USER/_PASSWORD`) into a shared module.
  `app/history.py` is refactored to import from it instead of keeping its
  own copy — no behavior change there, just de-duplication.
- **New file `app/session_store.py`**: same public shape as today's
  `app/session.py` (`create_session`, `get_session`, `delete_session`),
  same `conn=None` dependency-injection pattern `history.py` already
  uses, but backed by a new `sessions` table instead of a dict:
  ```sql
  CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY,
      client_id TEXT NOT NULL,
      client_secret TEXT NOT NULL,
      principal_name TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
  ```
  Created by an `ensure_schema()` call alongside `history.py`'s existing
  one, in the same startup event.
- **12-hour TTL, checked lazily.** `get_session` deletes and returns
  `None` for any row older than 12h at lookup time — no separate cleanup
  job. Good enough for an internal tool; not trying to build a real
  session-eviction service.
- **`app/main.py`** swaps its `from app.session import ...` for the new
  module; every route that reads `Session` (`require_session`, `/query`,
  `/catalog/*`, `/me`, `/history`) is otherwise unchanged, since the
  `Session` shape and the function signatures stay the same.
- `client_secret` moves from in-memory-only to a Postgres column, in
  plaintext, matching today's trust boundary exactly (not worse, not
  better) — encrypting it at rest is explicitly deferred to the security
  hardening pass.

### 2. Query concurrency guard

Today, `/query` has no limit on how many DuckDB executions can run at
once in a single pod — confirmed live as the actual root cause of the
OOM: 5 concurrent large joins over `lineitem`-scale data blew well past
512Mi with no admission control at all.

- A module-level `threading.Semaphore(MAX_CONCURRENT_QUERIES)` in
  `app/main.py` (not `asyncio.Semaphore` — `/query` is a sync `def` route
  FastAPI already runs in its threadpool, so the blocking primitive is
  the correct one here).
- `MAX_CONCURRENT_QUERIES = 3`, overridable via env var for future
  tuning without a code change.
- Non-blocking acquire (`semaphore.acquire(blocking=False)`) right before
  `connection.execute(...)`. On failure: `HTTPException(429, "Too many
  concurrent queries — try again in a moment")`, released connection,
  *no* history record written (it never ran) and *no* queueing — a
  bounded queue would just delay the same OOM, not prevent it. Release
  the semaphore in a `finally` so a query that raises still frees its
  slot.

### 3. Chart: memory, replicas, probes

`charts/lakehouse-ui/values.yaml` / `templates/deployment.yaml`:

- `resources.limits.memory`: `512Mi` → `2Gi` (requests bumped to `512Mi`,
  matching today's old limit, as a sane floor). Sized for 3 concurrent
  large joins to have real headroom, not just enough for one — a
  starting point to verify empirically (see Verification below), not a
  number treated as self-evidently correct.
- `replicaCount`: `1` → `2`. Safe now that sessions live outside any one
  pod's memory — one pod OOMing and restarting no longer logs out every
  other user's session.
- Explicit `livenessProbe`/`readinessProbe` timing (today implicit/
  default, which is part of why a busy-but-healthy pod got killed
  mid-query during the benchmark): `initialDelaySeconds: 5`,
  `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 3`.
  `/healthz` itself is unchanged — still a trivial `{"status": "ok"}`
  with no Postgres/Polaris calls, so this is purely about giving a
  genuinely busy pod more room before Kubernetes decides it's dead.

## Data flow

```
Before: session in Deployment pod's own memory
  → pod restart (OOM, redeploy, crash) = every session lost

After:  session in Postgres (lakehouse_ui.sessions)
  → any pod, any replica, can read any session
  → pod restart loses nothing; replicas=2 means one restarting
    doesn't interrupt users on the other
```

```
Before: /query -> unlimited concurrent DuckDB executions -> OOM at ~5

After:  /query -> acquire 1 of 3 semaphore slots (or 429 immediately)
        -> execute -> release
        memory limit raised to 2Gi to give those 3 real headroom
```

## Verification / Definition of Done

- `sessions` table created on startup; a session survives a
  `kubectl rollout restart` (log in, restart the deployment, confirm the
  cookie is still valid — the actual scenario that broke mid-benchmark).
- Two replicas both `Ready`; killing one doesn't invalidate sessions
  created against the other.
- 4th concurrent query attempt (with 3 already in flight) gets a 429, not
  a hang or a 500.
- Rerun `benchmark/load_test.py` at concurrency=5 (the level that
  OOMKilled before): no `CrashLoopBackOff`, no `OOMKilled` events. Some
  429s are an acceptable, correct outcome at that concurrency — a clean
  429 is the system working as designed, not a failure.
- `helm lint`/`helm template` clean; rendered Deployment shows
  `replicas: 2`, `memory: 2Gi` limit, and the explicit probe block.
- Existing `pytest` suite still green, plus new tests for
  `session_store.py` (mirroring `history.py`'s existing test patterns:
  owns-connection vs caller-provides-connection, TTL expiry) and for the
  429 path on `/query`.
