# PaneState Refactor Adversarial Review

Scope: `git diff c2c71f8 -- src/agent_takkub/orchestrator.py`

## Findings

### Medium: stuck-recovery cooldown is not preserved by the real close path

- `src/agent_takkub/orchestrator.py:3388`
- `src/agent_takkub/orchestrator.py:3401`
- `src/agent_takkub/orchestrator.py:2225`

`_auto_recover_stuck()` stamps `self._ps(key).last_stuck_recover = now`, then immediately calls `self.close(role, project=project)`. The real `close()` does an atomic `self._pane_state.pop(key, None)`, so the cooldown stamp is deleted before the respawn callback runs.

The watchdog later reads `last_recover = ps_ck.last_stuck_recover` at `src/agent_takkub/orchestrator.py:3359`, but the saved value is gone. This means a pane that remains or becomes stuck again can be recovered again without honoring `STUCK_RECOVER_COOLDOWN_S`.

Why this is easy to miss: `tests/test_stuck_recover.py:59` and `tests/test_stuck_recover.py:73-87` use a fake `close()` that explicitly preserves `_pane_state`/`last_stuck_recover`, and the comment says the fake intentionally diverges from real close. So the tests prove the desired cooldown contract, but not the production implementation.

This may not be a pure new regression from the old per-dict code, because old `close()` also popped `_last_stuck_recover`. However, after the PaneState refactor the comment at `src/agent_takkub/orchestrator.py:708-710` says `close()`/`done()` atomically pop all per-pane state, while `last_stuck_recover` is still modeled as a field whose watchdog semantics require survival across the recovery close. That lifecycle mismatch is now more hidden because it is co-located with teardown-only state.

Suggested fix direction: keep `last_stuck_recover` outside `PaneState`, or snapshot/restore it in `_auto_recover_stuck()` the same way `session_uuid`, `last_assigned_task`, `auto_chain`, and `requires_commit_on_done` are restored.

### Low: failed stuck-respawn rollback leaves an empty PaneState entry behind

- `src/agent_takkub/orchestrator.py:3409-3418`
- `src/agent_takkub/orchestrator.py:3428-3444`

In `_do_respawn()`, restoring any snapshotted field creates a `PaneState` via `_ps(key)`. If `spawn()` fails, rollback clears only the restored fields on that object:

- `session_uuid = None`
- `session_uuid_cwd = ""`
- `last_assigned_task = None`
- `auto_chain = False`
- `requires_commit_on_done = False`

The empty `PaneState` remains in `_pane_state`. With the old separate dictionaries, the equivalent rollback popped each restored dict entry and left no consolidated membership marker. Today this does not appear to break the checked call sites because most readers inspect fields, not `key in _pane_state`. The main iterator at `src/agent_takkub/orchestrator.py:2430-2433` filters on `s.auto_chain`, so a false field is harmless.

Still, this weakens the "popped atomically by close()/done()" leak-prevention story and can make future `len(_pane_state)` or membership checks wrong. If keeping the empty object is intentional, consider documenting it. Otherwise, after rollback, pop the whole `PaneState` when all fields are back at defaults.

## Checks Against Prompted Failure Modes

- `_ps(key)` on read paths: most pure reads correctly use `self._pane_state.get(key)` or a non-inserting `PaneState()` fallback. The intentional writers are `send()`, rate-limit detection, stuck content tracking, auto-respawn accounting, and spawn bookkeeping.
- default mismatches: no high-confidence mismatch found for `rate_limited_until`, `last_send_ts`, `harvest_hint_ts`, `auto_respawn_attempts`, or `last_content_change_ts`. The `0.0` and `None` defaults match the observed old `dict.get(..., default)` behavior at the migrated call sites.
- `.pop(key, None)` return values: I did not find a migrated pop whose returned old value was used and lost. `done()` snapshots `requires_commit`/`auto_chain` before popping.
- `_recent_exits` and `_idle_state`: both remain separate, and the reviewed call sites still treat them as separate lifecycle maps.
- `_session_uuids` split: `spawn()` reads `session_uuid` plus `session_uuid_cwd`; `_auto_recover_stuck()` snapshots/restores both and synthesizes `_recent_exits` with the cwd. This path looked semantically intact.
- teardown coverage: `close()`/`done()` clear `_pane_state`; crash auto-respawn preserves attempts through `_from_auto_respawn=True`; stuck recovery restores the critical resume/task/chain/commit fields, but not the cooldown field noted above.

## Verification

Ran:

```text
pytest -q tests/test_stuck_recover.py tests/test_lifecycle_recovery.py::TestDoRespawnRollbackOnFailure
```

Result: `16 passed`.

Important caveat: the stuck-recovery tests use a fake `close()` that intentionally preserves cooldown state, so this passing result does not cover the real `close()`/`PaneState.pop()` lifecycle.
