# #190: proactive idle compaction re-firing every ~27min on truly-idle panes

## Round 2: `proactive_compact_pending` could stay stuck True across real work

Round 1 (below) fixed the repeat-fire itself but left one gap: if the pane
that just ran `/compact` is **never observed back at its ready prompt**
before real work lands on it — a task gets assigned, or someone types
directly into the pane, in the same window the compact was still
finishing — `proactive_compact_pending` never gets the chance to clear
itself the normal way (the pane-is-ready branch that clears it never runs;
the pane goes straight from "not-ready running /compact" to "not-ready
running real work" with no ready observation in between).

Effect: the not-ready branch keeps skipping the `idle_since = None` reset
(because `pending` is still True), so `idle_since` stays pinned at its stale
pre-compact value for as long as the pane keeps being busy on the real work.
Once that real work finally finishes and the pane goes idle again,
`idle_since` is still the OLD value — the pre-existing `sent_ts >=
idle_since` gate reads that as "already compacted this episode" and stays
quiet, even though a brand-new idle episode (with a fresh, uncompacted
transcript) just started. Net effect: a missed `/compact` the next time the
pane is genuinely idle — lower severity than the original repeat-fire (it
under-fires instead of spamming), but still defeats #161's intent.

### Fix

Added `PROACTIVE_COMPACT_PENDING_CEILING_S` (orchestrator.py, `os.environ`
override `TAKKUB_PROACTIVE_COMPACT_PENDING_CEILING_S`, default 10 minutes).
The not-ready branch now only trusts `proactive_compact_pending` as "still
our own /compact running" while `now - proactive_compact_sent_ts` is within
this ceiling; once a not-ready stretch outlives it, `pending` is treated as
stale and the branch falls back to the ordinary new-work path — clear
`pending`, reset `idle_since` to `None` — exactly as if `pending` had never
been set.

Value chosen from observed reality, not a guessed round number:
`_check_idle_teammates` (the watchdog this all runs under) ticks every
`IDLE_WATCHDOG_INTERVAL_MS` = 5s, so there is ample granularity to catch a
compact that actually finishes well inside any reasonable ceiling. The
original #190 repeat-fire cycles logged in `runtime/events.log` show real
`/compact` runtime around ~2 minutes (threshold+27min cycles against a
25min threshold). 10 minutes leaves roughly 5x margin over that observed
runtime before a stuck `pending` gets mistaken for a still-running compact,
while staying well under `PROACTIVE_COMPACT_IDLE_AFTER_S`'s 25-minute
default so it can never itself become the dominant delay.

### Tests

- `test_pending_stale_past_ceiling_is_treated_as_real_work` — pane goes
  not-ready, is never observed ready again, and the not-ready stretch
  outlives `PROACTIVE_COMPACT_PENDING_CEILING_S`. Confirmed **red** against
  the round-1-only code (`assert 1 == 2`, second `/compact` never fired)
  before adding the ceiling logic; green after.
- `test_pending_within_ceiling_still_suppresses_repeat_compact` — companion
  guard: a not-ready stretch shorter than the ceiling must still be trusted
  as "our own compact running" and must NOT cause a second `/compact` for
  the same idle episode (the ceiling must not regress the round-1 fix for
  the normal, fast case).

Both pass alongside the full `tests/test_idle_watchdog.py` (46 tests).

### Files changed (round 2)

- `src/agent_takkub/orchestrator.py` — new `PROACTIVE_COMPACT_PENDING_CEILING_S` constant; not-ready branch now bounds how long `proactive_compact_pending` is trusted; docstring updated.
- `src/agent_takkub/spawn_engine.py` — `PaneState.proactive_compact_pending` comment updated to note the ceiling.
- `tests/test_idle_watchdog.py` — two new tests above.

## Round 1

## Root cause

`Orchestrator._check_proactive_compact` (orchestrator.py) tracked one idle
episode via two `PaneState` fields:

- `proactive_compact_idle_since` — wall-clock when the pane was first seen
  continuously at its ready prompt.
- `proactive_compact_sent_ts` — wall-clock of the last `/compact` this
  watchdog injected.

The gate `sent_ts >= idle_since` is meant to mean "already compacted this
idle stretch, skip." But the not-ready branch unconditionally reset
`idle_since = None` whenever the pane wasn't at its ready prompt — and that
included the pane being busy running the very `/compact` the watchdog had
just sent.

Sequence that produced the bug:

1. Pane idle 25min (`PROACTIVE_COMPACT_IDLE_AFTER_S`) → watchdog sends
   `/compact`, sets `sent_ts = t0`.
2. Next tick: pane is busy running `/compact` → not at ready prompt →
   `idle_since` reset to `None`.
3. Compact finishes, pane returns to ready → `idle_since` set to `t1` (now,
   a few seconds/minutes after `t0`).
4. Gate check: `sent_ts (t0) >= idle_since (t1)` → **False**, because `t1 >
   t0`. The watchdog now believes this is a brand-new, never-compacted idle
   episode.
5. 25 more minutes of genuine idling later → fires `/compact` again. Repeat
   forever.

This matches `runtime/events.log` exactly: fires at 18:54:04 → 19:21:04
(+27m0s) and 23:21:46 → 23:49:12 (+27m26s) — threshold (~25min) +
compact-run time (~2min) — on a pane with `idle_for` pinned at
1500/1501/1502 (machine-driven, not user input). The second fire of each
pair returned "Not enough messages to compact", confirming the pane really
was idle the whole time.

## Signal chosen for the fix

Added `PaneState.proactive_compact_pending: bool` (spawn_engine.py), set the
instant the watchdog writes `/compact` and cleared the first time the pane
is subsequently observed back at its ready prompt.

While `proactive_compact_pending` is `True`, the not-ready branch no longer
nulls `idle_since` — that busy stretch is known, deterministically, to be
the watchdog's own compact running, not new work. `idle_since` is left
untouched through that stretch, so once the pane settles the pre-existing
`sent_ts >= idle_since` gate correctly recognises "already compacted this
episode" and stays quiet.

A genuinely new not-ready observed *after* `proactive_compact_pending` has
already been cleared is real work (task assign, user input, etc.) and resets
`idle_since` exactly as before — a fresh idle episode still earns its own
`/compact` later.

## Signals considered and rejected

`PaneState` already carries several timestamps that looked like candidates
for "did new input arrive" (spawn_engine.py:460-615):

- **`last_send_ts`** — only updated by the orchestrator-mediated `send()`
  path (teammate `send`/`assign`/CC-Lead). It is **never** updated by a user
  typing directly into a pane's PTY (most relevantly, Lead's own terminal),
  which `_check_proactive_compact` explicitly targets (`Lead pane is
  eligible` — see `test_lead_pane_is_eligible`). Using this as the "real
  input arrived" signal would silently under-protect exactly the pane this
  feature cares most about. Rejected.
- **`last_content_change_ts`** — fires on ANY transcript delta, including
  the compact's own progress/output text, so it can't distinguish
  compact-caused churn from real new work either. Rejected.
- **`assign_ts`** — only set by `_assign_dispatch` (an actual `takkub
  assign`), same gap as `last_send_ts` for direct PTY typing. Rejected.
- **`last_pending_submit_ts`** — belongs to the unrelated stuck-paste
  reaper; not a proxy for "pane became busy for a reason." Rejected.

None of the pre-existing signals cover "user typed directly into the pane's
own terminal" — that only shows up as the pane leaving its ready prompt,
which is indistinguishable, on its face, from the pane leaving its ready
prompt to run our own `/compact`. Rather than guess at a heuristic, the fix
introduces a small piece of state we control with certainty: whether *we*
just told this pane to compact. This is the most conservative option
available — it never mis-attributes real new work to the compact (a
genuinely new not-ready only appears after `pending` is cleared on the
pane's return to ready), and it never mistakes the compact's own busy
window for new work either.

## Proof the tests are not vacuous

`tests/test_idle_watchdog.py::TestProactiveIdleCompact::test_compact_execution_does_not_start_new_idle_episode`
was written and run against the pre-fix code first — confirmed red:

```
assert pane.session.write.call_count == 1
E       AssertionError: assert 2 == 1
```

(the pane's compact-induced busy→ready cycle caused a second `/compact` to
fire, reproducing #190 exactly). After the fix, this test and the full
`tests/test_idle_watchdog.py` (46 tests) pass.

The opposite-direction regression guard,
`test_new_idle_episode_after_going_busy_fires_again`, was updated to first
let the just-sent compact settle (busy→ready, must NOT count as new work)
and only THEN simulate a genuinely separate busy→ready cycle representing
real new work — proving the fix does not degrade into "compact fires at
most once per pane lifetime." Both directions are asserted by distinct call
counts (1 after the compact settles alone; 2 after real new work follows and
a full idle threshold elapses again).

## Known gaps

`proactive_compact_pending` could stay stuck True if the pane was never
observed back at ready before real work kept it busy — see **Round 2**
above for the gap and its fix (`PROACTIVE_COMPACT_PENDING_CEILING_S`).

The `proactive_compact_pending` flag is reset implicitly whenever
`done()`/`close()`/`spawn()` pop or reset the whole `PaneState` (same
lifecycle as the other proactive-compact fields), so no separate cleanup
path was needed for that case.

## Files changed

- `src/agent_takkub/spawn_engine.py` — new `PaneState.proactive_compact_pending` field.
- `src/agent_takkub/orchestrator.py` — `_check_proactive_compact` gating fix + corrected docstring (previously claimed "a pane that stays idle for hours only gets nudged once," which was true only for the single-episode case, not across compact-induced busy/ready cycles).
- `tests/test_idle_watchdog.py` — new regression test for the repeat-fire bug; existing "new episode after busy" test extended to separate the compact's own settle cycle from a genuinely new busy cycle.
