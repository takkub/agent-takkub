# #229 — watchdog SOFT stall 1.5–1.7s: `_resolve_claude_jsonl_path` glob on the Qt main thread

## Symptom

`boot.log` (prod install, `~/.agent-takkub/runtime/boot.log`) contains 13 `[watchdog] SOFT stall`
entries in a single boot, 1.5s–1.8s each, all with the identical main-thread stack:

```
app.py:691 main
  -> remote/notify.py:1417 _poll_all
  -> remote/notify.py:1353 _resync
  -> remote/notify.py:1321 _resolve_jsonl
  -> remote/notify.py:1077 <lambda>
  -> remote/notify.py:345 _resolve_claude_jsonl_path
  -> pathlib.py:953 glob -> pathlib.py:340 _select_from -> pathlib.py:953 exists -> pathlib.py:1013 stat
```

## Root cause (proven, not guessed)

`LeadNotifier` (`remote/notify.py`) polls every open project's Lead session on a **200ms QTimer**
(`_POLL_MS = 200`, line 75) running on the Qt **main thread**, and also on every
`orch.statusChanged` signal (fires from `orchestrator.py`/`spawn_engine.py` on pane state
transitions — frequent while any pane is actively working).

Each tick calls `_poll_all()` → `_resync()`. Before this fix, `_resync()`'s tail-establishment
loop called `self._resolve_jsonl(...)` — which for the `claude` provider bottoms out in
`_resolve_claude_jsonl_path()`'s `base.glob(f"*/{session_uuid}.jsonl")` (a stat-per-directory
filesystem walk over every directory under `<config_dir>/projects/`) — **unconditionally, for
every open project, on every single tick**, even for a project whose session was already
resolved and is already being tailed:

```python
for project_ns, (provider, session_uuid, spawn_ts) in wanted.items():
    path = self._resolve_jsonl(project_ns, session_uuid, provider, spawn_ts)   # <- glob every tick
    if path is None:
        continue
    current = self._tails.get(project_ns)
    if current is not None and current.path == path:   # <- redundancy only discovered *after* the I/O
        continue
    ...
```

The eviction loop immediately above this one already guarantees the missing invariant: any
`self._tails[project_ns]` entry whose `(provider, session_uuid, spawn_ts)` no longer matches
`wanted[project_ns]` is deleted *before* the resolve loop runs. So if `project_ns` is still in
`self._tails` when the resolve loop starts, its identity is already proven current — the glob
can only re-derive the same path. Doing it anyway is pure waste on the busiest thread in the
process, 5x/second, times however many project tabs are open.

## Direct measurement

Local `~/.claude/projects` on this dev box: 185 project directories, 2343 `.jsonl` files.
A single warm-cache `_resolve_claude_jsonl_path()` call here costs ~7ms (`base.glob` timeit,
10 iterations, 5.9–9.3ms range). The prod install's `~/.agent-takkub`-backed profile is far
colder/heavier (larger `~/.claude/projects`, disk/AV contention on a Windows box with an
actively-running cockpit) — consistent with the observed 1.5–1.8s single-call stalls in
`boot.log`, especially once `statusChanged` bursts stack multiple `_resync()` calls back to back
while several teammate panes are transitioning state at once.

### Head-to-head harness (`LeadNotifier` against 20 fake open projects, steady state — all
sessions already resolved and tailed, then 50 more `_resync()` ticks fired to simulate the
QTimer + `statusChanged` burst pattern)

| | glob calls (`_resolve_claude_jsonl_path`) | wall time for 50 `_resync()` calls |
|---|---|---|
| **Before fix** | 1000 (20 projects × 50 ticks, unconditional) | 1229.58ms |
| **After fix** | 0 | 3.39ms |

(Harness: `LeadNotifier` constructed with 20 fake projects each already pointing at a real
on-disk `.jsonl`, `notifier._timer.stop()` to drive `_resync()` manually instead of via the
wall clock, `_resolve_claude_jsonl_path` wrapped with a call counter. Verified against both the
pre-fix and post-fix code on this exact source tree — see below.)

## Fix

`remote/notify.py::_resync()` — skip the resolve/glob entirely once `project_ns` already has a
live tail, since the eviction loop above already proved its identity still matches `wanted`:

```python
for project_ns, (provider, session_uuid, spawn_ts) in wanted.items():
    if project_ns in self._tails:
        continue
    path = self._resolve_jsonl(project_ns, session_uuid, provider, spawn_ts)
    if path is None:
        continue
    ...
```

This is a cache keyed by `(project_ns, provider, session_uuid, spawn_ts)` identity, invalidated
by the pre-existing eviction loop (pane closed, session uuid changed on respawn/resume, or
provider changed) — exactly the "cache per session id" remedy the issue proposed, minus the
mtime bookkeeping (unnecessary: the eviction loop's identity check is already the correct,
tighter invalidation signal — a session's resolved JSONL path is a pure function of
`(project_ns, session_uuid)` and cannot change while that identity is unchanged).

A newly-spawned/resumed pane whose `.jsonl` hasn't been created yet still retries every tick
until the file appears (unchanged pre-existing behavior, explicitly documented in the code
comment) — that's a brief, bounded startup window, not the reported steady-state stall.

### Why not a worker thread instead

The issue's other proposed remedy (move `_resolve_jsonl` off the Qt main thread) was rejected
in favor of the cache: `_poll_all`/`_resync` run on the Qt main thread by construction (QTimer
+ `statusChanged` signal handlers), and `_resync()` also does `self._broadcaster.push(...)`
(Qt/SSE broadcaster) inline — moving only the glob to a worker thread would still need to marshal
the result back to the main thread before touching `self._tails`/`self._broadcaster`, adding
cross-thread complexity for a call that the cache makes nearly always unnecessary in the first
place. The cache is strictly simpler and removes ~100% of the I/O in steady state instead of
just moving it off-thread.

### Multi-provider scope

The fix lives in the shared `_resync()` loop, not in `_resolve_claude_jsonl_path` itself — it
applies uniformly to every provider registered in `_HISTORY_SCANNERS` (`claude`, `gemini`,
`codex`), not just Claude. Gemini/Codex resolvers were already exposed to the identical
unconditional-every-tick call pattern before this fix; they now get the same skip. No
provider-specific gap to flag for #103.

### Cross-platform

`pathlib.Path`-only, no OS-specific code touched. The fix changes *when* the existing
`config_dir_for(...).glob(...)` runs, not how — behavior is identical on Windows and macOS.

## Verification

- `tests/test_remote_notify.py` — 99 passed (covers tail establishment, session-uuid-change
  eviction/reset, session_changed event emission, retry-until-jsonl-appears, provider switch).
- `tests/test_orchestrator_notify_lead.py`, `tests/test_remote_pwa_resume.py`,
  `tests/test_lead_draft_guard.py` — all passed (same `LeadNotifier` class, different angles).
- `ruff check src/agent_takkub/remote/notify.py` — clean.
- `lint-imports` — 25/25 contracts kept, 0 broken.
- Empirical before/after harness above, run against both the reverted and fixed code on this
  exact worktree.

## Files changed

- `src/agent_takkub/remote/notify.py` — `LeadNotifier._resync()`: skip re-resolving a
  project's JSONL path once it already has a live, identity-matched tail.
