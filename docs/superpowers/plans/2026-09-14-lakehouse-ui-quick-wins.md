# Lakehouse UI Quick Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the three quick-win UX fixes identified by the 2026-09-14 gap analysis — surfaced query duration/row count, success toasts on silent catalog mutations, and persistently-visible tree action icons — in the lakehouse-ui repo.

**Architecture:** Two small, independent backend/frontend changes (query stats, toast component) plus one CSS-only fix, landing as five sequential tasks in `C:\claude\lakehouse-ui`. No new files, no new dependencies, no infra/chart changes.

**Tech Stack:** FastAPI + Pydantic (backend), vanilla JS + hand-written CSS (frontend, no framework/build step), pytest + FastAPI TestClient (backend tests).

**Design doc:** `docs/superpowers/specs/2026-09-14-lakehouse-ui-quick-wins-design.md`

---

### Task 1: Return duration_ms and row_count from /query

**Files:**
- Modify: `app/main.py:106-109` (QueryResponse class), `app/main.py:262` (success return in `_run_query_body`)
- Test: `tests/test_query.py`

- [ ] **Step 1: Loosen the existing exact-equality test and add a new failing test**

In `tests/test_query.py`, replace the body of `test_query_runs_select_and_returns_rows` (the `assert body == {...}` exact-dict check will break once new fields are added) with field-by-field assertions, and add a new test right after it:

```python
def test_query_runs_select_and_returns_rows(monkeypatch):
    fake = FakeConnection(["id", "name"], [[1, "a"], [2, "b"]])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake
    )
    monkeypatch.setattr(main_module, "record_query", lambda **kwargs: None)

    response = client.post(
        "/query", json={"sql": "SELECT * FROM nyc_taxi.trips"}, cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["columns"] == ["id", "name"]
    assert body["rows"] == [[1, "a"], [2, "b"]]
    assert body["truncated"] is False
    assert fake.executed == ["SELECT * FROM nyc_taxi.trips"]


def test_query_response_includes_duration_and_row_count(monkeypatch):
    fake = FakeConnection(["id"], [[1], [2], [3]])
    monkeypatch.setattr(
        main_module, "build_connection", lambda client_id, client_secret: fake
    )
    monkeypatch.setattr(main_module, "record_query", lambda **kwargs: None)

    response = client.post(
        "/query", json={"sql": "SELECT * FROM t"}, cookies=_logged_in_cookie()
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["duration_ms"], int)
    assert body["duration_ms"] >= 0
    assert body["row_count"] == 3
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pytest tests/test_query.py::test_query_response_includes_duration_and_row_count -v`
Expected: FAIL with `KeyError: 'duration_ms'`

- [ ] **Step 3: Add the fields to QueryResponse and return them**

In `app/main.py`, change:

```python
class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    truncated: bool = False
```

to:

```python
class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    truncated: bool = False
    duration_ms: int
    row_count: int
```

Then, in `_run_query_body`, change the success return (currently `return QueryResponse(columns=columns, rows=rows, truncated=truncated)`) to:

```python
    return QueryResponse(
        columns=columns,
        rows=rows,
        truncated=truncated,
        duration_ms=duration_ms,
        row_count=len(rows),
    )
```

`duration_ms` is already in scope at this point (computed a few lines above, at the `duration_ms = int((time.monotonic() - started_at) * 1000)` line used by the `record_query(...)` call right before this return) — reuse that same variable rather than recomputing it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_query.py -v`
Expected: All tests PASS, including both tests touched in Step 1.

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest`
Expected: All tests PASS (no other test in the suite asserts an exact `QueryResponse` shape, but this confirms it).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_query.py
git commit -m "feat: return query duration and row count from /query"
```

---

### Task 2: Surface duration/row count in the query status line

**Files:**
- Modify: `app/static/app.js:466-518` (`runActiveWorksheet`)

**Depends on:** Task 1 (reads `body.duration_ms` / `body.row_count`, which Task 1 adds to the `/query` response).

No JS test harness exists in this repo (confirmed in the design doc's Testing section) — this task is verified by a syntax check plus manual inspection, matching how every prior frontend change in this project has been verified.

- [ ] **Step 1: Add a formatting helper**

In `app/static/app.js`, add this function immediately above `async function runActiveWorksheet() {` (currently line 466):

```javascript
function formatQueryStatus(durationMs, rowCount, truncated) {
  const durationText = durationMs >= 1000 ? `${(durationMs / 1000).toFixed(1)}s` : `${durationMs}ms`;
  const rowText = rowCount === 1 ? '1 row' : `${rowCount} rows`;
  const truncatedText = truncated ? ' (truncated)' : '';
  return `Done in ${durationText} · ${rowText}${truncatedText}`;
}
```

- [ ] **Step 2: Set the status line on success, and stop clearing it unconditionally**

In `runActiveWorksheet`, this block:

```javascript
    body = await apiFetch('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql }),
      });
      worksheet.error = null;
      worksheet.columns = body.columns;
      worksheet.rows = body.rows;
      worksheet.truncated = body.truncated;
    } catch (err) {
      if (err.message === 'not authenticated') {
        // Redirect to /login is already in flight; don't flash an error.
        return;
      }
      worksheet.error = err.message;
      worksheet.columns = [];
      worksheet.rows = [];
      worksheet.truncated = false;
    }
```

becomes:

```javascript
    body = await apiFetch('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql }),
      });
      worksheet.error = null;
      worksheet.columns = body.columns;
      worksheet.rows = body.rows;
      worksheet.truncated = body.truncated;
      statusEl.textContent = formatQueryStatus(body.duration_ms, body.row_count, body.truncated);
    } catch (err) {
      if (err.message === 'not authenticated') {
        // Redirect to /login is already in flight; don't flash an error.
        return;
      }
      worksheet.error = err.message;
      worksheet.columns = [];
      worksheet.rows = [];
      worksheet.truncated = false;
      statusEl.textContent = '';
    }
```

And the `finally` block (currently):

```javascript
  } finally {
    queryInFlight = false;
    statusEl.textContent = '';
    runButton.disabled = false;
  }
```

drops the now-redundant (and previously destructive — it was overwriting the text this task just set) `statusEl.textContent = '';` line:

```javascript
  } finally {
    queryInFlight = false;
    runButton.disabled = false;
  }
```

- [ ] **Step 3: Verify syntax**

Run: `node --check app/static/app.js`
Expected: no output (exit code 0)

- [ ] **Step 4: Commit**

```bash
git add app/static/app.js
git commit -m "feat: show query duration and row count after a successful run"
```

---

### Task 3: Add the toast component

**Files:**
- Modify: `app/static/app.js:49-52` (add `showToast` after `closeModal`)
- Modify: `app/static/app.css:158` (add `#toast` rules after the modal-error rule, before the tree-row rules)

- [ ] **Step 1: Add showToast to app.js**

In `app/static/app.js`, insert immediately after the existing `closeModal` function (currently lines 49-52):

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

- [ ] **Step 2: Add toast styles to app.css**

In `app/static/app.css`, insert immediately after the `#modal-box .modal-error` rule (currently line 158, right before the `.tree-namespace-row, .tree-table-row` rule):

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

- [ ] **Step 3: Verify syntax**

Run: `node --check app/static/app.js`
Expected: no output (exit code 0)

- [ ] **Step 4: Commit**

```bash
git add app/static/app.js app/static/app.css
git commit -m "feat: add a toast component for transient success confirmations"
```

---

### Task 4: Wire toasts into the five silent catalog-mutation actions

**Files:**
- Modify: `app/static/app.js` — `submitCreateDataset`, `submitDeleteDataset`, `submitCreateTable`, `submitDeleteTable`, `submitSaveAsTable`

**Depends on:** Task 3 (`showToast` must exist).

Each of these five functions currently ends its success path with `closeModal(); loadCatalog();` and nothing else. Each gets one `showToast(...)` line inserted immediately before `closeModal()`.

- [ ] **Step 1: submitCreateDataset**

Find (inside `submitCreateDataset`, currently ending around line 90):

```javascript
  closeModal();
  loadCatalog();
}
```

(the first occurrence, immediately following the try/catch block whose `sql` is `` `CREATE SCHEMA lakehouse.${name}` ``) and change to:

```javascript
  showToast(`Dataset "${name}" created.`);
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 2: submitDeleteDataset**

Find (inside `submitDeleteDataset`, currently ending around line 122):

```javascript
  closeModal();
  loadCatalog();
}
```

(the occurrence immediately following the try/catch whose `sql` is `` `DROP SCHEMA lakehouse.${name}` ``) and change to:

```javascript
  showToast(`Dataset "${name}" deleted.`);
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 3: submitCreateTable**

Find (inside `submitCreateTable`, currently ending around line 207):

```javascript
  closeModal();
  loadCatalog();
}
```

(the occurrence immediately following the try/catch whose `sql` is built from `columnDefs`/`CREATE TABLE`) and change to:

```javascript
  showToast(`Table "${namespace}.${name}" created.`);
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 4: submitDeleteTable**

Find (inside `submitDeleteTable`, currently ending around line 239):

```javascript
  closeModal();
  loadCatalog();
}
```

(the occurrence immediately following the try/catch whose `sql` is `` `DROP TABLE lakehouse.${namespace}.${table}` ``) and change to:

```javascript
  showToast(`Table "${namespace}.${table}" deleted.`);
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 5: submitSaveAsTable**

Find (inside `submitSaveAsTable`, currently ending around line 343):

```javascript
  closeModal();
  loadCatalog();
}
```

(the occurrence immediately following the try/catch whose `sql` is `` `CREATE TABLE lakehouse.${namespace}.${name} AS ${originalSql}` ``) and change to:

```javascript
  showToast(`Saved as table "${namespace}.${name}".`);
  closeModal();
  loadCatalog();
}
```

- [ ] **Step 6: Verify syntax**

Run: `node --check app/static/app.js`
Expected: no output (exit code 0)

- [ ] **Step 7: Commit**

```bash
git add app/static/app.js
git commit -m "feat: show a toast when a dataset or table is created, deleted, or saved"
```

---

### Task 5: Make catalog tree action icons persistently visible

**Files:**
- Modify: `app/static/app.css:159-161`

Fully independent of Tasks 1-4 — pure CSS, no JS/HTML changes.

- [ ] **Step 1: Update the opacity rules**

In `app/static/app.css`, change:

```css
.tree-namespace-row, .tree-table-row { display: flex; align-items: center; justify-content: space-between; }
.tree-row-actions { display: flex; gap: 0.4rem; opacity: 0; }
.tree-namespace-row:hover .tree-row-actions, .tree-table-row:hover .tree-row-actions { opacity: 1; }
```

to:

```css
.tree-namespace-row, .tree-table-row { display: flex; align-items: center; justify-content: space-between; }
.tree-row-actions { display: flex; gap: 0.4rem; opacity: 0.4; }
.tree-namespace-row:hover .tree-row-actions,
.tree-namespace-row:focus-within .tree-row-actions,
.tree-table-row:hover .tree-row-actions,
.tree-table-row:focus-within .tree-row-actions {
  opacity: 1;
}
```

- [ ] **Step 2: Commit**

```bash
git add app/static/app.css
git commit -m "fix: make catalog tree action icons persistently visible instead of hover-only"
```

---

## Definition of Done

After all five tasks are merged, verify live (the pattern established throughout this project — direct HTTP/curl checks against the deployed app, since the interactive browser tool is unavailable in this environment):

1. `POST /query` with a valid session returns `duration_ms` (int) and `row_count` (int) in its JSON body alongside the existing `columns`/`rows`/`truncated` fields.
2. The served `app/static/app.js` (curl the running pod/service) contains `formatQueryStatus`, `showToast`, and all five `showToast(...)` call sites.
3. The served `app/static/app.css` contains `#toast` and `#toast.visible` rules, and `.tree-row-actions` has `opacity: 0.4` (not `opacity: 0`) as its base rule.
4. Full backend test suite passes: `pytest`.
5. `node --check app/static/app.js` passes.

No chart or environment values changes are needed for this plan — this ships as an image rebuild + resync only, following the same deploy path used for the earlier UI bugfix (`sha-2510622`).

## Self-Review Notes

- **Spec coverage:** all three spec sections (duration/row count, toasts, persistent icons) map 1:1 to Tasks 1-2, 3-4, and 5 respectively. The spec's "Out of scope" items (error-path duration, query-run toasts, further icon redesign) are correctly absent from every task.
- **Placeholder scan:** none found — every step has literal code, exact commands, and exact expected output.
- **Type/name consistency:** `formatQueryStatus(durationMs, rowCount, truncated)` (Task 2) and `showToast(message)` (Task 3) are each defined once and referenced with matching signatures everywhere they're called (Task 2's one call site; Task 4's five call sites). `body.duration_ms` / `body.row_count` (Task 2) match the exact field names added to `QueryResponse` in Task 1.
