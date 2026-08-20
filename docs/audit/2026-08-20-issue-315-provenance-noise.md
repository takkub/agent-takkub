# #315 — "unverified origin" fires on ~every digest: root cause + fix

## Symptom (reported)

Overnight ~8h session, Lead cycling `takkub assign` across backend/admin/frontend/qa
repeatedly. Nearly every `📬 [Lead Inbox Digest]` / done-event carried:

> ⚠️ [unverified origin — {role} pane was respawned since this report was queued;
> confirm current status with the live pane before acting on it]

Every single occurrence was independently re-verified by Lead (`git diff`/`git status`)
and was accurate — zero false positives all night, just constant boilerplate.

## Where the check lives

`_provenance_stale(project_ns, role, pane_token)` in `lead_inbox.py` (pre-fix, ~line 2715),
called from three sites that render `_STALE_ORIGIN_BANNER`:

- `_flush_lead_digest` (~2696) — the digest path, the one in the symptom report
- `_pump_lead_notify` (~3180) — live-queue delivery
- `_force_deliver_done_notices` (~3451) — reaper's last-resort paste

Pre-fix, the check was a bare identity comparison:

```python
current = self._current_pane_identity(project_ns, role)
return current != pane_token
```

`pane_token` is stamped once, in `Orchestrator.done()` (orchestrator.py:4092):

```python
origin_pane_token = self._current_pane_identity(project_ns, from_role)
```

captured **synchronously, at the exact moment `done()` runs** — i.e. it always equals
whatever token is "current" for that role at call time. It is not a claim the calling
pane makes about itself; it's the orchestrator's own live lookup, taken before any
teardown.

## Why it fires on ~every digest — traced, not guessed

`takkub done` closes the reporting pane within ~2.5s (role-file contract, confirmed in
`orchestrator.py::done()` comments: "close() (scheduled 2.5s later)"). `_mint_pane_token`
(spawn_engine.py) revokes the previous token for `(project_ns, role_name)` and mints a
fresh one **every time that role is spawned again** — including the completely ordinary
case of Lead handing the same role its next assignment.

Sequence for one ordinary task cycle (this is the ~100% case in an active session, not
an edge case):

1. `t0`: role's pane token minted → `A`.
2. `t1`: pane finishes, calls `done()` → `origin_pane_token = _current_pane_identity(...)`
   reads `A` (nothing has changed yet) → report queued with `pane_token=A`, `queued_ts=t1`.
3. Pane closes (`t1 + 2.5s`).
4. `t2` (`t2 > t1`, usually seconds to a couple minutes later — Lead cycles fast):
   Lead assigns the role's next task → `_mint_pane_token` revokes `A`, mints `B`.
5. `t3` (digest debounce window, `_INBOX_DIGEST_WINDOW_MS` = 15s, or later if the burst
   waited behind other mail): digest flushes. `_current_pane_identity` now returns `B`.
   `B != A` → **pre-fix: always flagged**, regardless of the fact that nothing at all
   went wrong — the role simply moved on to its next task, which is *why* Lead was
   reading a done digest in the first place.

Because step 4 (a fresh mint for the SAME role) happens after essentially every `done()`
in continuous operation, and step 3's debounce window is easily long enough for step 4 to
land first, the bare-mismatch check is true for nearly every digest item in a busy
session. This matches the reported "effectively every task completion" pattern exactly.

### Why "current pane is busy on something else" alone does not fix it

The task brief's suggested calibration ("respawn happened after queued, and the new pane
is busy on other work") was checked against this trace: in the reported session, Lead
reassigns roles continuously, so the NEW pane (`B` above) is *virtually always* busy on
its next task by the time any digest flushes. Gating only on "new pane currently busy"
would still fire on nearly every item — it does not discriminate the reported case.

### The actual discriminating signal: mint-time vs. queued-time ordering

Because `done()` always stamps `pane_token` as whatever's current **at call time**, a
genuinely live report can *only* go stale via a **later** respawn (`mint_ts(current) >
queued_ts`) — the ordinary sequential hand-off case above, and it is never informative.

The only way a report can carry a `pane_token` that differs from a token that was
**already current at or before `queued_ts`** (`mint_ts(current) <= queued_ts`) is if the
report was never actually produced by a live `done()` call matching that state — a
replayed/duplicate/mis-tagged item (the durable-store round-trip class of bug #228 itself
named: "a done report was queued while one pane instance was alive but delivered only
after a later instance took over the same role name"). That ordering is impossible for an
honest, freshly-produced report and is exactly the class of bug #228 exists to catch.

## Fix

1. `spawn_engine.py::_mint_pane_token` now also stamps
   `_pane_token_minted_at[tok] = time.time()` (plain, unpersisted dict — soft heuristic
   metadata, not auth state, so it's deliberately not routed through the `_registry`-backed
   `_pane_tokens` property and never explicitly revoked; a few stray floats for long-dead
   tokens are harmless).
2. New `_current_pane_mint_ts(project_ns, role_name)` reads that dict for whichever token
   is currently live.
3. `_provenance_stale` gained an optional `queued_ts` parameter. New logic:
   - role/pane_token absent → not stale (unchanged).
   - current identity matches → not stale (unchanged).
   - no live pane at all under the role → still unconditionally stale (unchanged — there
     is nothing to compare against, so this stays conservative; it's also not the pattern
     the reported symptom is about, since a role is rarely fully vacant for a whole digest
     cycle in an active session).
   - identity mismatch, and `queued_ts` given, and the current token's mint time is
     recorded → stale **only if** `mint_ts(current) <= queued_ts` (see discriminator
     above). Otherwise (`queued_ts` missing, or no mint-time record — e.g.
     legacy/test-constructed `_pane_tokens` entries that bypassed `_mint_pane_token`) →
     fails safe to the old, conservative `stale=True`.
4. All three call sites (`_flush_lead_digest`, `_pump_lead_notify`,
   `_force_deliver_done_notices`) now pass the item's own `queued_ts` through.
   `Orchestrator.inbox_report`'s `origin_confirmed` field (the same check, exposed via
   `takkub inbox`) was updated identically so the two surfaces never disagree.
5. `_STALE_ORIGIN_BANNER` shortened from a 2-line, full-sentence paragraph to one short
   line — now that it only fires on genuinely informative mismatches, it no longer needs
   to front-load a justification for why Lead should care.

## Test coverage

`tests/test_inbox_report.py::TestInboxReportOriginProvenance` (existing #228 true-positive
tests — unaffected, still pass unchanged, since none of them supply `queued_ts` and so
still hit the fail-safe `stale=True` branch):

- `test_confirmed_when_role_slot_unchanged`
- `test_flagged_stale_after_role_slot_respawned`
- `test_flagged_stale_when_role_no_longer_has_a_live_pane`
- `test_none_when_no_origin_was_recorded`

New, added for #315:

- `test_ordinary_next_task_respawn_not_flagged_stale` — false positive repro: pane token
  `A` mints, queues a report, then role respawns to `B` strictly AFTER `queued_ts`
  (ordinary steady-state hand-off) → `origin_confirmed` must be `True`.
- `test_phantom_report_predating_current_pane_still_flagged_stale` — #228 true positive
  under the new timing-aware path: current token `B` was already minted BEFORE a report
  naming a different token `A` claims to have been queued → must still be flagged
  (`origin_confirmed` is `False`).
