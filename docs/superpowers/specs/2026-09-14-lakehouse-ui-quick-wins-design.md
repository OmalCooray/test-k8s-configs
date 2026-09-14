# Lakehouse UI Quick Wins (Gap Analysis Follow-up) — Design

**Status:** Approved
**Date:** 2026-09-14
**Repo:** lakehouse-ui (application code changes only — no chart/infra changes)

## Background

A product-owner gap analysis of the lakehouse-ui warehouse console (2026-09-14) identified three "quick win" UX gaps — low effort, no new infrastructure, high impact — as the top-priority next action:

1. Query duration and row count are computed server-side on every run but never shown to the user.
2. Create/delete/save-as-table actions succeed silently, with no confirmation.
3. Catalog tree action icons (create table, delete, details) are invisible until row hover, making them undiscoverable — the same failure class as the icon-clipping bug fixed earlier the same day.

This spec covers implementing all three as one bundle, since they're independent but small enough to ship together.

## 1. Query duration & row count

**Backend (`app/main.py`):**

`QueryResponse` (currently `columns`, `rows`, `truncated`) gains two fields:

```python
class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    truncated: bool = False
    duration_ms: int
    row_count: int
```

`_run_query_body`'s success return (currently `return QueryResponse(columns=columns, rows=rows, truncated=truncated)` at line 262) becomes:

```python
return QueryResponse(
    columns=columns,
    rows=rows,
    truncated=truncated,
    duration_ms=duration_ms,
    row_count=len(rows),
)
```

`duration_ms` and `row_count` are already computed at this point (lines 249 and the `record_query` call) — this just returns values that already exist rather than computing anything new. `row_count` follows the existing semantics used by `record_query`: it's `len(rows)`, the capped page size, not a separate "true total" count.

Scope: success path only. The error path (`except Exception as exc` block, line 232) raises `HTTPException` before any `QueryResponse` is constructed, so error-path duration display is out of scope for this bundle — it would require a different response shape for errors, which is a separate change.

**Frontend (`app/static/app.js`, `runActiveWorksheet`):**

Today, `#query-status` is set to `'Running...'` before the fetch and cleared to `''` in the `finally` block regardless of outcome (lines 484, 515). This changes to: on success, format `body.duration_ms` and `body.row_count` into the status line before clearing happens; on error, leave the existing clear-to-blank behavior (the error itself is already shown via `worksheet.error` / `renderResults`).

Formatting: durations under 1000ms show as `"NNNms"`; 1000ms and up show as one decimal place of seconds, e.g. `"1.2s"`. Row count is pluralized (`"1 row"` vs `"42 rows"`). When `body.truncated` is true, append `" (truncated)"`.

Example: `statusEl.textContent = `Done in 340ms · 128 rows`;` set right after a successful fetch, left in place (not cleared) since the query is no longer running. The `finally` block's unconditional clear is removed in favor of setting the final text explicitly in both the success and error branches of the existing `try`.

## 2. Success toasts

**New component (`app/static/app.js` + `app/static/app.css`):**

A single-slot toast: at most one visible at a time (these actions are all modal-gated, so concurrent triggers aren't possible). `showToast(message)` in app.js:

```javascript
let toastTimer = null;

function showToast(message) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add('visible');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 3000);
}
```

CSS (app.css), matching the existing dark palette (`#141a21` surface, `#2a333d` border, `#2b8a6b` accent used by `#run-btn`):

```css
#toast {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  background: #141a21;
  border: 1px solid #2b8a6b;
  color: #e8edf2;
  padding: 0.6rem 1rem;
  border-radius: 4px;
  font-size: 0.85rem;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.15s ease, transform 0.15s ease;
  pointer-events: none;
  z-index: 200;
}
#toast.visible {
  opacity: 1;
  transform: translateY(0);
}
```

**Call sites** — each of the following currently ends its success path with `closeModal(); loadCatalog();` (or, for save-as-table, similar) and nothing else. A `showToast(...)` call is added immediately before `closeModal()`:

- `submitCreateDataset` (app.js:70) → `showToast(`Dataset "${name}" created.`)`
- `submitDeleteDataset` (app.js:107) → `showToast(`Dataset "${name}" deleted.`)`
- `submitCreateTable` (app.js:171) → `showToast(`Table "${namespace}.${name}" created.`)`
- `submitDeleteTable` (app.js:224) → `showToast(`Table "${namespace}.${table}" deleted.`)`
- `submitSaveAsTable` (app.js:319) → `showToast(`Saved as table "${namespace}.${name}".`)`

Error paths in all five functions are unchanged — they already surface inline via each modal's `.modal-error` element and return before reaching `closeModal()`.

## 3. Persistent action icons

**CSS only (`app/static/app.css`):**

```css
.tree-row-actions { display: flex; gap: 0.4rem; opacity: 0.4; }
.tree-namespace-row:hover .tree-row-actions,
.tree-namespace-row:focus-within .tree-row-actions,
.tree-table-row:hover .tree-row-actions,
.tree-table-row:focus-within .tree-row-actions {
  opacity: 1;
}
```

This replaces the current `opacity: 0` default (app.css:160) and hover-only reveal (app.css:161), and adds `:focus-within` alongside `:hover` so keyboard/tab navigation into a row's buttons also reveals them — today's rule is mouse-hover-only, which is itself a minor accessibility gap this fix happens to close. No JS or HTML changes.

## Testing

- **Backend:** one new test in `tests/test_main.py` asserting a successful `/query` response includes `duration_ms` (int, >= 0) and `row_count` (int, equal to `len(rows)`), following the existing test patterns in that file for the `/query` route.
- **Frontend:** no JS test harness exists in this repo (confirmed — none of the prior UI work this project has done added one). The toast component, status-line formatting, and icon CSS are verified by hand against the running app, consistent with how this project has verified all prior frontend changes.

## Out of scope

- Duration/row count on the error path (would need a separate error-response shape).
- Toasts for query-run success/failure itself (query results already render visibly in the results table; this bundle's toasts are specifically for the four currently-silent catalog-mutation actions).
- Any further icon/tree redesign beyond the opacity fix (e.g., always-visible labels) — deferred per the gap analysis's own "dimmed, full on hover" recommendation.
