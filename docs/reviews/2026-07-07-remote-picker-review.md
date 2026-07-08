# Remote-control per-project picker — security/correctness review

**Date:** 2026-07-07 · **Reviewer:** reviewer (Claude) · **Scope:** working-tree diff (uncommitted)
**Files:** `remote/notify.py`, `remote/http_server.py`, `remote/api.py`, `remote/static/app.js`

**Verdict: PASS — no blocker, no high.** The two critical trust-boundary
concerns (cross-project Lead leak H-A, ticket forge) are both correctly
closed. Findings below are low-severity notes only.

---

## Critical vectors — all verified SOUND

### 1. Cross-project Lead leak (H-A) — PASS
`notify.py` now hooks **every** open project's Lead pane at once, but leak is
prevented at three independent layers:

- **Per-connection ns binding** (`notify.py:154`): each `bytesIn` hook is a
  `functools.partial(self._on_lead_bytes, project_ns)` bound at connect time to
  that session's own namespace. There is no shared "current project" pointer;
  `_on_lead_bytes(project_ns, data)` receives the correct ns for the source
  session regardless of what tab is active.
- **Per-chunk stamping, not read-at-flush** (`notify.py:163-166`, `181-190`):
  each buffered chunk stores its own `(project_ns, bytes)`; `_flush` pushes with
  the *stored* ns, so a mid-coalesce project switch cannot re-stamp old bytes.
  The old B1 race is genuinely gone.
- **Exact-equality broadcaster filter** (`http_server.py:183-185`): `push`
  skips any client whose ticket ns `!= project_ns`. A client scoped to project
  B can never receive `lead`/`done` events stamped A.

**Namespace alignment confirmed** (the invariant the whole scheme rests on):
ticket ns, `_panes_by_project` keys, and `agentDone` emit all use the same raw
project-name namespace:
- `agentDone.emit(project_ns=…)` with `project_ns = _resolve_project(project)` (orchestrator.py:1515,1656)
- `_panes_by_project` keyed by `_resolve_project(project)` (orchestrator.py:628)
- ticket ns = `get_open_tabs()` entry or `active_project()` name (http_server.py:103, config.py:413/270)

All resolve to the projects.json project **name** — no display-name vs
resolved-ns skew. `push(..., project_ns=None)` (broadcast-to-all) is never
reachable: the only two callers (`notify.py:190,196`) always pass a concrete ns.

### 2. Ticket forge — PASS
`_Bridge._resolve_scoped_project` (http_server.py:95-105):
```python
if isinstance(requested, str) and requested in _config.get_open_tabs():
    return requested
return self._orch._resolve_project(None)   # fallback: active project
```
- Non-string / missing / stale / forged / traversal (`"../x"`, long, unicode)
  name → not in `get_open_tabs()` → silent fallback to active. A client can
  never scope a ticket to a project it doesn't already have open.
- No path traversal risk: `requested` is only ever a dict-key equality test
  against known names, never a filesystem path. The fallback `_resolve_project(None)`
  passes `None`, so untrusted input never even reaches `validate_name`.
- Not a privilege escalation: the remote password gate is a single shared
  secret for the whole cockpit (single-user), and `/api/projects` already
  lists all projects — scoping to any *open* project is what the authenticated
  owner is entitled to. "Someone else's project" does not apply here.
- Runs on the **Qt main thread**: `_resolve_scoped_project` is called inside
  `_Bridge._handle` (http_server.py:109,117), the queued signal slot — same
  thread that writes projects.json. For off-thread actions it is resolved on
  the Qt thread *before* the worker thread starts (http_server.py:108-110), and
  the already-validated name is what the worker uses. No TOCTOU leak (a project
  closing after validation just makes the send/pulse no-op, never misroute).

### 3. statusChanged resync — PASS
`_resync_lead_sessions` (notify.py:136-156) is diff-based:
- Disconnects hooks whose project closed **or** whose Lead session changed
  (respawn): `wanted.get(project_ns) is not session` (notify.py:142).
- Connects only newly-discovered sessions (`if project_ns in self._hooks: continue`,
  notify.py:152) → no double-connect on repeated `statusChanged` bursts.
- `stop()` (notify.py:207-212) disconnects every stored partial by identity and
  clears the dict. Disconnects are guarded by `except (TypeError, RuntimeError)`
  so a partial already dropped, or a deleted C++ session object, cannot raise.

### 4. pulse / lead_say from_project — PASS
Both are in `_OFF_THREAD_ACTIONS`; their `project` param is rewritten by
`_resolve_scoped_project` on the Qt thread (http_server.py:108-110) before the
worker calls `api.pulse` / `api.lead_say` (http_server.py:130-133). `lead_say`
therefore can only deliver to the Lead of a validated **open** project — no
send to a non-open or wrong project. Both are additionally gated by
bearer + password (+ `allows_control()` for say) at http_server.py:300,308-312.

### 5. General correctness — PASS
- Every dispatch path catches exceptions and answers 500/504 instead of taking
  down the Qt loop (`_handle` 121-125, `_run_off_thread` 138-140, `_issue_sse_ticket`
  376-380). `RemoteApiError` mapped to its own status (137).
- `dict(parse_qsl(...))` (http_server.py:233) yields flat **string** values, so
  `query.get("project")` is a string, not a `parse_qs` list — the
  `isinstance(str)` check behaves as intended.
- Client XSS-safe: `safeMarkdown` (app.js) escapes via `textContent`→`innerHTML`
  *before* applying bold/italic/code regex, so no tag/attribute injection.
- Client stream teardown clean: `switchView` stops the stream when leaving Lead
  (app.js:232) and `startLeadStream`'s `if (state.es || state.esTimer) return`
  guard (app.js:526) plus `stopLeadStream` before reconnect in `selectProject`
  prevent a leaked/duplicate EventSource.

---

## Findings (low severity)

### L1 — Shared coalesce buffer evicts across projects under load
**`notify.py:167-177`** · fairness, not security
`_buf` is now a single list shared by every project's live output. The cap
trim (`while total > cap`) pops/trims the **oldest** entry regardless of
project. A project spewing output can evict another project's buffered bytes
within the 150 ms coalesce window before they flush.
- **Impact:** best-effort live Lead output only (already lossy + junk-filtered);
  bounded to 16 KB / 150 ms. Evicted bytes are **dropped, never misrouted** — no
  isolation break. A noisy project can briefly starve a quiet one's live view.
- **Repro:** two open projects, drive continuous output in A; B's live text may
  gap for one coalesce window under sustained A load.
- **Suggested fix (optional):** cap per-project instead of globally, or accept
  as-is and document — live output is explicitly best-effort.

### L2 — projects.json disk read on Qt main thread per request
**`http_server.py:103` (`_resolve_scoped_project` → `get_open_tabs` → `load_projects`)**
Every pulse/sse-ticket/say now reads projects.json from disk on the Qt main
thread. Pulse polls on a timer, so this is periodic UI-thread disk I/O.
- **Impact:** consistent with the documented "these reads need Qt-thread
  ownership" pattern, but the picker adds it to the polling hot path. Micro-
  freeze risk only under a slow/contended disk. Low.
- **Suggested fix (optional):** memoize `load_projects()` for a short TTL, or
  accept — the file is tiny.

### L3 — selectProject doesn't refresh the Projects-list highlight
**`app.js` `selectProject`** · UX only
After switching project the Projects view still shows the previous row as
`selected` until `loadProjects` re-runs (next visit). No functional impact —
the stream/pulse/composer are all correctly rebound to the new project.

---

## Notes checked and cleared (not findings)
- Imported-but-closed projects render read-only (not tappable) in the client;
  a programmatic sse-ticket for a closed known name **fails safe** to active. By
  design.
- `_hooks` retains a Python ref to each session; abnormal session teardown
  without `statusChanged` would linger until next resync, but disconnect is
  RuntimeError-guarded — no crash, negligible retention. Defensive, not a bug.
- `api.projects(None, mode)` called with explicit `None` (http_server.py:115) —
  no missing-arg TypeError despite `from_project` having no default.
