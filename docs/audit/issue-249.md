# #249 — `takkub wait` hangs on already-finished roles, no heartbeat, no cancel

## Symptom (from the issue)

Three sequential `takkub wait` invocations in one project tab, each watching
a different role set, all showed as `(running)` with **zero bytes of
output** long after every role they watched had reported `done` and its
pane had closed. Docs claim "at most one waiter per project (new calls
attach)" but the evidence read as three independent, non-attached
registrations.

## Root cause (proven by code trace, `src/agent_takkub/lead_wait.py`)

`_resolve_role_wait_status()` only special-cased `pane.state == "working"`.
Every other pane state fell through to a single generic branch:

```python
return "pending", f"pane state: {state or 'unknown'}"
```

Pane state machine (`agent_pane_model.py` + `orchestrator.done()` /
`orchestrator.close()`):

- `done()` sets `pane.state = "done"`, then schedules `close()` **2.5s**
  later via `QTimer.singleShot`.
- `close()` sets `pane.state = "empty"` — **the pane object is never
  removed from `_panes_by_project[ns]`**, it just sits there forever with
  state `"empty"` (confirmed: no `.pop()` on that dict anywhere outside
  `_project_panes()`'s own `setdefault`).
- An unexpected crash sets `pane.state = "exited"` (`decide_exit_state`).

So `"done"`, `"empty"`, and `"exited"` are all **terminal** — a role in one
of these states can never produce a new report without a fresh
`assign`/spawn (which would flip it back to `"active"`/`"working"` on the
very next poll tick, self-correcting). But the resolver treated all three
identically to a role that's simply still working: **pending, unconditionally,
until the wait's own timeout** (up to the 2h hard ceiling).

The staleness guard made this worse, not better. `_wait_done_events`
records ARE written at the moment `done()` fires — but `poll_wait` only
trusts them when `event["ts"] >= started_ts` (deliberately, to stop an old
completion from a *previous* work cycle short-circuiting a wait for a
*freshly re-spawned* run of the same role — see the `#241` staleness
comment). Any `takkub wait` call issued **after** a role had already
finished — the completely ordinary case of Lead calling `wait` slightly
late, or right at the 2.5s done→close boundary — discarded the only
report that role would ever produce and fell straight into the
"pending: pane state: done/empty" trap. That's items 1 and 2 in the issue,
confirmed: this is exactly the failure mode described (roles long finished,
waiter still blocked).

A never-spawned role (`panes.get(role) is None`) hit the same shape of bug:
permanently `"pending"` with a "role not found" reason, no path to resolve
before the timeout either.

### Item 3 (single-waiter enforcement)

Read `begin_wait()`'s attach path closely (`lead_wait.py:98-116`): if
`_active_waits[project_ns]` exists and its `last_poll_ts` is within
`timeout_s + 120s` grace, a second `begin_wait` call **always** attaches
(unions the role set) rather than creating an independent registration —
this logic is correct as written and is exercised by
`test_second_call_attaches_and_unions_roles`. No separate defect was found
in the attach/staleness code itself.

The "3 concurrent waiters" symptom is best explained as a **downstream
effect of the item-1 bug**, not an independent one: because a role that had
genuinely already finished stayed "pending" instead of resolving, an
already-alive registration looked (to a human skimming `takkub list`, or to
Lead moving on to dispatch a new, unrelated batch of work) like it had
nothing left to do — when in fact the *client-side* CLI process was still
blocked polling it, alive, and (per the attach logic) perfectly able to
absorb a second `begin_wait`. No log evidence from that specific incident
was available to inspect directly in this worktree (`runtime/events.log` —
this is a dev-checkout worktree with no runtime state of its own), so this
conclusion rests on the proven mechanics of the code, not on a captured
repro. Added `TestCancelWait.test_close_all_teammates_cancels_active_wait`
+ regression coverage of the attach path's fresh-vs-stale registration
handling either way, since fixing item 1 removes the only mechanism that
could make a live registration look abandoned.

## Fix

`src/agent_takkub/lead_wait.py`:

- New `_WAIT_TERMINAL_PANE_STATES = {"empty", "done", "exited", "error"}`.
  `_resolve_role_wait_status` now resolves a role sitting in one of these
  states **immediately**, using whatever `_wait_done_events` record exists
  (even a stale one — it's the only report that lifecycle will ever
  produce) rather than falling through to the generic pending branch. No
  record at all → resolves as a new **"gone"** kind (never blocks to
  timeout, but isn't silently reported as `"done"` either).
- New `_WAIT_NEVER_SPAWNED_GRACE_S = 15.0`: `panes.get(role) is None` now
  only resolves `"gone"` once 15s have passed since the wait started —
  bridges the async-spawn race (`takkub assign` can return "spawning
  async" before the pane entry exists) without going back to blocking to
  the full timeout.
- `poll_wait()` gained a fourth bucket, `"gone"` — resolved the same as
  done/failed (removed from `pending`, so the CLI's `not pending` exit
  check now fires immediately) but reported to the caller separately so
  Lead can tell "will never report" apart from "actually failed".
- New `cancel_wait(project_ns)` — pops the active registration without
  needing its `wait_id` (item 5). Wired into `close_all_teammates()`
  (`orchestrator.py`) so a board reset always sweeps away any stale
  registration, and into a new `takkub wait --cancel` CLI flag /
  `wait-cancel` server command (Lead-only, same gate as the other
  `wait-*` commands).

`src/agent_takkub/cli.py` (`cmd_wait`):

- `--cancel` short-circuits to a single `wait-cancel` request, no poll loop.
- Heartbeat output (item 4): prints the instant any role resolves
  (`"[wait] resolved: X — N still pending (Ys elapsed)"`), and otherwise at
  most every `_WAIT_HEARTBEAT_INTERVAL_S` (30s) while something is still
  pending — enough to distinguish "still waiting" from "hung" from outside
  the process without spamming a fast-resolving wait.
- Final summary now also prints a `gone:` section.

`src/agent_takkub/cli_server.py` / `orchestrator.py`: `"wait-cancel"` added
to `_LEAD_ONLY_CMDS`; `close_all_teammates()` calls `cancel_wait()` first.

## Tests

`tests/test_lead_wait.py` — 14 new cases added (30 total, all passing):
terminal-state-with-stale-event → done/failed, terminal-state-with-no-event
→ gone, never-spawned within/past grace, `cancel_wait` (present/absent/via
`close_all_teammates`), CLI `--cancel` short-circuit. Existing 16 cases
(attach/union, staleness-for-a-still-working-role, timeout/expiry,
`end_wait`) all still pass unmodified — the terminal-state handling is
additive and only changes behavior for states the old code treated as an
undifferentiated "pending" catch-all.

## Verification

- `pytest tests/test_lead_wait.py` — 30 passed.
- `pytest tests/test_cli.py tests/test_cli_server_auth.py
  tests/test_cli_server_role_gate.py tests/test_headless_window.py
  tests/test_lead_self_protection.py tests/test_restart_cockpit.py` — all
  passed (no regression in the `close-all` / role-gate paths touched).
- `ruff check` / `ruff format --check` on every touched file — clean.
- `lint-imports` — 25/25 contracts kept, including `lead-wait-layer`
  (this module still imports nothing from orchestrator/main_window/app/cli).

## Multi-provider / cross-platform note

Every changed path reads only pane/queue state the orchestrator already
tracks per role (`pane.state`, `_wait_done_events`, `_active_waits`) — none
of it is Claude-specific or platform-specific, consistent with `wait`'s
existing design.
