# #113 (recurrence) — `/remote-control` bridge vs `/resume` picker race — FIX

## Root cause (confirmed via `runtime/events.log`, 2 days of "Resume cancelled" reports)

```
10:02:37 auto_slash_command /resume
10:02:40 auto_slash_command /remote-control   <- bridge fires 3s later, into the still-open picker
```

`consume_session_report` (orchestrator.py) unconditionally calls
`_maybe_fire_remote_bridge` (spawn_engine.py) for **every** `SessionStart` hook
firing on the Lead role, regardless of `source`. When the user runs `/resume`
(button or hand-typed — the trigger is the hook, not the button),
claude opens its own interactive session picker and, around the same moment,
fires `SessionStart(source="resume")`. That call started
`inject_slash_command_when_ready("/remote-control", ...)` polling *immediately*.

`is_at_ready_prompt()` gates the poll, and `/resume`'s picker footer includes
`"esc to cancel"` — already one of `_READY_HARD_BLOCKERS` — so once the picker
has painted, the poll correctly refuses to fire. The bug is the **paint gap**:
for a brief window right after `/resume` is submitted, the screen can still
show the *previous* idle footer (e.g. `"bypass permissions"`) before the
picker has actually redrawn. A poll that lands in that gap reads
`is_at_ready_prompt() == True`, injects `/remote-control` + auto-Enter, and
that Enter submits/cancels whatever row the just-opened picker had selected —
observed as "Resume cancelled".

The existing uuid/session-object dedup guards (#107/#110/#112) don't help
here: this is a fresh, never-fired session, so none of the "already
fired/pending/delivered" guards apply. The race is purely about *when* the
poll is allowed to start, not whether it's a duplicate.

## Fix

Two layers, both additive — none of the #107/#110/#112 dedup guards were
touched.

**1. Defer the fire itself for `source="resume"`/`"compact"`**
(`Orchestrator.consume_session_report`, orchestrator.py): these two sources
are the ones that can leave a modal (picker / post-compact repaint) briefly
on screen. Instead of calling `_maybe_fire_remote_bridge` directly, the hook
now just logs `remote_bridge_deferred` and returns — no poll starts at all.
The **existing** idle-watchdog reap (`_reap_remote_bridge`, called every tick
for every Lead pane, gated on `is_at_ready_prompt()`) becomes the sole
trigger for these sources: by the time the next tick runs (~5s later), the
picker has had a full tick to either paint (and correctly block via its own
"esc to cancel" hard blocker) or close (and the pane is genuinely ready). No
new "needs-bridge" flag was needed — `_reap_remote_bridge` already retries
unconditionally on every tick; deferring here just means the first attempt
comes from the tick, not from the hook.

`source="startup"` (fresh boot / tab open — never involves the picker) keeps
firing immediately, unchanged — no #107 regression.

**2. Defense-in-depth: explicit picker detection before every poll start**
(`SpawnEngineMixin._maybe_fire_remote_bridge`, spawn_engine.py): added
`PtySession.is_at_resume_picker()` (pty_session.py) — detects the picker via
its confirmed markers (footer `"esc to cancel"` paired with either
`"show all projects"` or `"type to search"`, or header `"resume session"`
alone for the transitional paint moment before the footer has drawn).
`_maybe_fire_remote_bridge` now checks this immediately before registering
the pending-poll state and starting `inject_slash_command_when_ready` — if
the picker is up, it logs `remote_bridge_deferred` and returns without
touching any dedup state, so a later call (reap tick, or another
`consume_session_report`) tries again cleanly. This covers any invocation
path (not just the two above) that might race the picker's paint, even
though `"esc to cancel"` being a ready-hard-blocker already covers the common
case on its own.

## Files changed

- `src/agent_takkub/pty_session.py` — new `PtySession.is_at_resume_picker()`.
- `src/agent_takkub/spawn_engine.py` — `_maybe_fire_remote_bridge` gains the
  picker check before polling.
- `src/agent_takkub/orchestrator.py` — `consume_session_report` defers
  `source in ("resume", "compact")` to the reap path instead of firing
  directly.

## Tests

- `tests/test_pty_ready_prompt.py` — `TestIsAtResumePicker`: marker detection
  (footer combos, header-alone, and non-picker busy/idle screens that must
  NOT match).
- `tests/test_consume_session_report.py` — `TestRemoteBridgeSourceGating`:
  `resume`/`compact` never call `_maybe_fire_remote_bridge` directly;
  `startup`/`clear`/non-Lead roles keep firing exactly as before (regression
  guard).
- `tests/test_remote_bridge_autofire.py` —
  `TestSessionReportSourceResumeDefersToReap` /
  `...CompactSourceAlsoDefersToReap`: end-to-end — deferred source doesn't
  fire, a reap tick while the picker is still open doesn't fire either, and a
  reap tick once the pane is genuinely ready fires exactly once.
  `TestSessionReportSourceStartupStillFiresImmediately`: no regression.
  `TestPickerMarkerBlocksFireAsDefenseInDepth`: `is_at_resume_picker()` blocks
  `_maybe_fire_remote_bridge` even in the hypothetical where
  `is_at_ready_prompt()` misreads `True`.

Targeted run: `test_remote_bridge_autofire.py` + `test_slash_inject_serialize.py`
+ `test_consume_session_report.py` + `test_pty_ready_prompt.py` — **83 passed**.
`ruff check` + `ruff format` clean on all changed files. `lint-imports` — 18/18
contracts kept. Full suite intentionally NOT run (targeted-tests-only rule —
full suite runs once at the QA batch gate). Not committed, per task instructions.
