# #233 `assign` hangs unbounded + #230 restart re-sends already-done tasks

Status: fixed (defense-in-depth; #233's exact field-incident mechanism could
not be reproduced or proven — see "What was ruled out" and "What is still
unproven" below).

## #233 — `takkub assign` blocked >120s, no timeout, no error, garbage stdout

### Reported symptom

`takkub assign --role devops --cwd <repo root> "<task>"` didn't return for
>120s (previous 5 assigns in the same session all returned in <400ms). The
one line of stdout that eventually appeared (`Container saas_admin-postgres-1
Running`) is `docker compose` output, not anything `cli.py`/`cli_server.py`
ever writes. `takkub list`, queried while the hang was in progress, showed
only `lead active` — the panes that had been up moments earlier (backend#1,
frontend#1-3) were gone from the listing.

### Root cause found and fixed: `_request()`'s timeout was per-call, not total

`cli.py::_request()` (the client side of every `takkub` command) did:

```python
s.settimeout(15)
while b"\n" not in buf:
    chunk = s.recv(4096)
    ...
```

`socket.settimeout()` bounds a single blocking call. It does **not** bound
the loop's total duration — a server that keeps returning non-empty,
non-newline-terminated chunks slower than 15s apart, or that otherwise resets
the "last activity" clock before each individual `recv()` expires, can hold
this loop open indefinitely while every individual `recv()` still "succeeds"
within its own budget. This is a real, provable defect independent of what
specifically caused the field incident: nothing in the old code guaranteed
`takkub assign` returns within a fixed ceiling, ever, for any reason.

**Fix** (`cli.py`): `_request()` now computes a wall-clock `deadline =
time.monotonic() + response_timeout` once, and shrinks each `recv()`'s
per-call timeout to the *remaining* budget on every loop iteration. If the
deadline passes — whether because the server went silent, or because it kept
dribbling data that never completed a frame — the call returns a clear `{"ok":
False, "msg": "timed out waiting for orchestrator response after 15s ..."}`
instead of hanging or raising an uncaught exception. `response_timeout`
defaults to the same 15s the code already documented as intentional (codex/
gemini spawn readiness), so no behavior-visible timing changed for the happy
path — the difference is only that the ceiling is now actually enforced.

Also hardened: a response that arrives but isn't valid JSON (which
`cli_server._reply()` never produces — so any occurrence means something
else's bytes landed in this socket) now returns a clear `"malformed response
from orchestrator ..."` message with a snippet of what was actually received,
instead of letting `json.JSONDecodeError` propagate as an unrelated-looking
traceback.

### What was ruled out (checked in code, not guessed)

- **The #240 resource-governor "queued — waiting for slot" path is not the
  cause.** `ResourceGovernor` is explicitly a "non-blocking admission
  controller" (`resource_governor.py`); `Orchestrator.assign()`'s
  `governor.request_slot()` call returns a decision synchronously with no
  wait, and when a slot isn't available it registers a callback and returns
  immediately. More importantly, `cli_server._dispatch()` acks the `assign`
  request (`"task queued for {role} ..."`) *before* `Orchestrator.assign()`
  ever runs — the real work is deferred via `QTimer.singleShot(delay, ...)`
  to the next event-loop tick, specifically so a slow assign can never block
  the reply (see the comment already in `cli_server.py` above the
  spawn/assign branch). Whatever `assign()` does internally, including the
  resource-queue path, happens strictly after the client's ack was already
  flushed.
- **No code path relays another pane's PTY/subprocess stdout into the CLI's
  TCP response socket.** `cli_server._reply()` only ever writes
  `json.dumps(...) + "\n"` to the specific `QTcpSocket` for that connection;
  there is no shared buffer between a pane's `PtySession` and the CLI
  server's per-connection sockets in the code as it stands today.

### What is still unproven

The literal mechanism that put a `docker compose` output line into the
`assign` command's observed stdout was not reproduced and no code path
producing it was found. The most consistent explanation given everything
above — that the hang and the stray line were two independent things
observed in the same window (a genuinely wedged/slow socket read on the
client, plus leftover terminal output from a separately-running `docker
compose up` process sharing the operator's terminal) — is plausible but not
proven. It was not asserted as the cause in the fix; the fix instead makes
the *class* of bug (unbounded wait, unclear response) impossible regardless
of which exact mechanism triggered it in the field. If this recurs, the new
"malformed response" error message will now surface a byte-snippet of
whatever actually arrived, which should make the next repro much easier to
pin down.

### Idempotency (proposal 3): dedup window on the server

An operator who sees "timed out" after the deadline fix and reruns the exact
same `takkub assign` must not risk double-dispatching a side-effecting task
(a migration, `docker compose up` running twice concurrently) if the first
request actually landed, just slower than the client waited.

**Fix** (`cli_server.py`): `CliServer` now tracks a short-lived fingerprint
`(project, role, blake2b(task))` → last-seen timestamp for `assign` requests.
An identical fingerprint seen again within `_ASSIGN_DEDUP_WINDOW_S` (8s) is
acked as `"task already queued for {role} moments ago (deduped — safe retry,
not re-dispatched)"` without a second call into `Orchestrator.assign()`.
Fingerprints are pruned on the existing 1s idle-connection reaper tick, so
the table never grows unbounded. A retry after the window, or with different
task text, dispatches normally — this only protects the narrow
timeout-then-retry case the issue describes.

## #230 — cockpit restart re-sends an already-`done()` task

### Reported symptom

backend#1 called `done()` (verified: 4 endpoints, tests passing) at
19:38:02. Cockpit then restarted (npm auto-update). On restore, backend#1
came back with `[cockpit restart] backend#1 pane restored from last session
and last task re-sent automatically` and re-ran the same, already-finished
task — a second `done()` report for identical work, and for a
side-effecting task (migration, push) this would re-run real work.

### What the existing code already gets right — and where it still isn't enough

`Orchestrator.done()` synchronously (same Qt thread, no race window)
pops the in-memory `PaneState` for that pane (so `last_assigned_task` is
gone) and sets `pane.state = "done"`. `snapshot_state()` (called fresh,
synchronously, immediately before every graceful restart —
`update_panel._restart_cockpit()`) explicitly excludes panes whose state
isn't `"active"`/`"working"`, so a snapshot taken right after a graceful
`done()` should never carry that pane's `last_task` in the first place.

That graceful path is sound. The gap is everything **not** on it:
`_restart_cockpit()` wraps its `self.orch.write_session_snapshot()` call in a
bare `except Exception: pass` (`update_panel.py`) — if snapshot writing ever
raises for any reason, the restart proceeds anyway using whatever
`last-session.json` already holds on disk from an earlier write. Any restart
that doesn't go through `_restart_cockpit()`/`MainWindow.closeEvent` at all
(a crash, a forced kill, an update mechanism that terminates the process more
abruptly than the two known graceful call sites) leaves the same stale file
in place. In either case, the *in-memory* state correctly reflected "done",
but the *on-disk* snapshot the next boot reads did not — and nothing before
this fix cross-checked that discrepancy at restore time.

### Fix: cross-check the durable task ledger before resending

`Orchestrator.done()` already calls `task_ledger.mark_done()`, which pops the
project's `open[role]` entry the moment a role's task resolves (ok/fail/
closed) — durable, on-disk, and independent of the in-memory snapshot's
freshness. `restore_teammates()` now reads `task_ledger.load_state(project)`
before re-sending a snapshot's `last_task`: if `role` has no entry in
`open`, the task is treated as already resolved (or never tracked — the
ledger write is itself best-effort, so an absent entry could mean either;
both cases fail closed, i.e. skip the resend, matching the issue's own
stated risk that re-running is worse than not re-running) and is **not**
re-sent. Instead of the previous unconditional resend notice, Lead sees:

> ⚠️ [cockpit restart] {role} pane restored from last session — its last task
> has no open row in the task ledger (already completed, or was never
> tracked), so it was NOT re-sent automatically to avoid duplicate/side-effect
> work. Re-assign manually if it still needs to run.

When the ledger does show the role still open (the pane really was mid-task
when the restart happened), behavior is unchanged from before: resend +
the original "re-sent automatically" notice.

## Files changed

- `src/agent_takkub/cli.py` — `_request()` total-deadline enforcement +
  malformed-response handling.
- `src/agent_takkub/cli_server.py` — assign-fingerprint dedup window.
- `src/agent_takkub/orchestrator.py` — `restore_teammates()` ledger
  cross-check before resend.
- Tests: `tests/test_cli.py` (`TestRequestDeadline`), `tests/test_cli_server.py`
  (`TestAssignDedup`; `TestSpawnStagger.test_parallel_assigns_are_staggered`
  updated to use distinct task text per call so it isn't collapsed by the new
  dedup guard), `tests/test_session_resume.py` (new `ledger_open_roles`
  fixture + `test_skips_resend_when_task_ledger_has_no_open_row` /
  `test_resends_when_task_ledger_shows_role_still_open`; two existing tests
  updated to opt into "ledger still open" so they keep testing the resend
  path they were written for).

## Verification

- `ruff check` / `ruff format --check` on all changed files: clean.
- `lint-imports` (24 contracts): all kept.
- Targeted pytest (not full suite, per project convention):
  `test_session_resume.py`, `test_cli.py`, `test_cli_server.py`,
  `test_cli_server_auth.py`, `test_cli_server_role_gate.py`,
  `test_cli_server_harvest.py`, `test_cli_server_hook.py`,
  `test_cli_server_session_report.py`, `test_task_ledger.py`,
  `test_orchestrator_session_uuid.py`, `test_end_session.py`,
  `test_session_brief.py`, `test_cli_bin_check.py`, `test_cli_status.py`,
  `test_cli_guard.py` — all pass.
- `test_installed_cli_bin_integration.py`'s two console-script-placement
  tests fail identically on a clean stash of this worktree (confirmed by
  `git stash` / rerun / `git stash pop`) — pre-existing packaging/environment
  issue in this sandbox, unrelated to this change; not touched here.
