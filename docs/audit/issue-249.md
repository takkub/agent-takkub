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

## Round 2 follow-ups (Lead diff review, 2026-08-15)

Two more races found reviewing the round-1 diff before merge, both fixed in
the same commit.

### A. `assign` → immediate `wait` false-"done" (terminal-state carve-out was
too trusting)

**Symptom / proven mechanics.** `takkub assign --role X ...` followed
immediately by `takkub wait --role X` is Lead's standard sequence
(`docs/lead/cli-reference.md`). `pane.set_state("working", ...)` only fires
once `_send_when_ready`'s async ready-poll actually delivers the paste
(`lead_inbox.py` — nothing in `spawn()`/`_assign_dispatch()` flips pane
state synchronously; confirmed by grep, zero `set_state(` calls in
`spawn_engine.py`). So for the first several poll ticks after a fresh
`assign`, `pane.state` can still show the PREVIOUS cycle's terminal value
(e.g. `"done"` from before the reassignment). The round-1 terminal-state
carve-out (`if state in _WAIT_TERMINAL_PANE_STATES: if event is not None:
return done/failed`) trusted **any** `_wait_done_events` record once the
pane looked terminal, with no check against the ACTIVE assign — so it
surfaced last cycle's stale report as if it were the brand-new task's
result. In the unattended pattern this matters most for (`assign` then
`wait` with no human watching), Lead would see a false "done" and move on
to verify/merge work that hadn't even started.

**Fix.** `_resolve_role_wait_status`'s terminal-state branch now also reads
`PaneState.assign_ts` (`_pane_state[project_ns::role]`, stamped by
`_assign_dispatch` right before every `spawn()` call, and reset — the entry
is popped — every time `done()` fires). An event only counts as covering
the terminal state if `event["ts"] >= assign_ts`:

- No reassign since the last `done()` → `_pane_state` entry is absent
  (popped) → `assign_ts` defaults to `0.0` → any real event trivially
  covers it → unchanged behavior, round-1's tests still pass verbatim.
- A fresh assign landed → `assign_ts` is the new dispatch's wall-clock,
  newer than the stale event → the event no longer "covers" the terminal
  state → falls to a NEW `"pending"` branch (`"assign ใหม่กำลังส่งเข้า pane —
  รอ report รอบใหม่"`) instead of either the old stale `"done"` or a bare
  `"gone"` (which would have been just as wrong — the pane is not
  abandoned, it's mid-delivery).

No change to `_assign_dispatch` itself was needed — `assign_ts` already
existed for the screenshot-evidence feature (#5) and turned out to be
exactly the "when did the CURRENTLY active task start" signal this needed;
clearing `_wait_done_events` at assign time was considered and rejected as
a less precise duplicate of the same information.

**Test:** `test_reassigned_role_with_stale_terminal_event_stays_pending`
(`tests/test_lead_wait.py`) — terminal pane state + stale event + a fresh
`PaneState(assign_ts=now)` → asserts `pending`, not `done`/`failed`/`gone`.

### B. Attached waiter's poll after resolution returned an error, not the result

**Symptom (live incident, Lead-reported).** Two `takkub wait` processes
attached to the same project registration (per #242's union-role-set
design). One poll call observed every watched role resolved and popped the
registration (`poll_wait`'s `if not pending or expired:
self._active_waits.pop(...)`). The OTHER attached process's very next poll
— same `wait_id`, just a beat later on its own backoff schedule — hit
`active is None`, fell into the "not found" branch, and printed:

```
[wait] attached to an existing wait already covering: backend, backend#2, backend#3
err: wait session no longer active (already ended, timed out, or superseded)
```

`cmd_wait` treats any `poll.get("ok") is False` as a hard error
(`return poll` before the summary/exit-code logic), so this is
indistinguishable from a genuinely broken wait even though the underlying
work had just succeeded.

**Fix — `_wait_resolved_echo`, not a `wait_id`-scoped lock.** A per-project
dict (`Orchestrator.__init__`) storing the exact payload the popping poll
call returned, keyed by `wait_id`, for `_WAIT_RESOLVED_ECHO_GRACE_S` (30s —
comfortably above the CLI's own poll-backoff ceiling,
`cli._WAIT_POLL_MAX_INTERVAL_S` = 15s). `poll_wait`'s "not found" branch
checks this before erroring: a matching, still-fresh echo is returned
verbatim (`ok: True` and all) instead of the manufactured error.

Two cases were deliberately kept as real errors, not echoed:

- **Explicit `end_wait`/`cancel_wait`** never write an echo — there is no
  terminal result to hand back for a wait someone actively tore down, and a
  straggler seeing "no longer active" there is accurate, not a false
  positive. Covered by
  `test_cancelled_wait_gives_stragglers_a_real_error_not_an_echo`.
- **A registration popped by its OWN timeout** (`expired=True`, roles still
  `pending`) is NOT treated as a cancel-equivalent — it's echoed too, with
  its real `pending`/`expired` payload intact, because that is exactly what
  the resolving poller itself got and is a legitimate (if unhappy) terminal
  answer, not a lost result. Covered by
  `test_echoed_timeout_result_still_carries_real_pending_set`. This is a
  deliberate refinement of the plain-language incident report, which
  grouped "cancel/timeout/superseded" together as one error bucket — timeout
  already carries a real, non-fabricated payload today for whichever poller
  observes it directly, so echoing that same payload to a straggler is
  strictly more correct than inventing a generic error for it.

**Tests** (`tests/test_lead_wait.py::TestResolvedEcho`, all passing):
`test_straggling_attacher_gets_echoed_result_not_error`,
`test_echoed_timeout_result_still_carries_real_pending_set`,
`test_echo_expires_after_grace_window`,
`test_cancelled_wait_gives_stragglers_a_real_error_not_an_echo`. Also
updated `TestPollWait.test_registration_auto_removed_once_all_resolved`,
which previously asserted a same-client re-poll after resolution errors —
that was the identical bug from a single-client angle, now correctly
asserts the echoed result.

## Round-2 verification

- `pytest tests/test_lead_wait.py` — 35 passed (30 pre-existing round-1 + 1
  new terminal-state/assign_ts case + 4 new `TestResolvedEcho` cases; one
  round-1 assertion updated to match the now-correct echo behavior).
- `ruff check` / `ruff format --check` on every touched file — clean.
- `lint-imports` — 25/25 contracts kept, including `lead-wait-layer`
  (`lead_wait.py` only gained an import of `orchestrator_text._exit_key`, a
  leaf module already imported by `lead_inbox.py` under the same layer
  contract).
