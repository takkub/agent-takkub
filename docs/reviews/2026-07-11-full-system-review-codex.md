# Full-system second-opinion review — Codex (2026-07-11)

Scope: read-only review of the current checkout, with extra attention to
`pipeline_executor`, `orchestrator_text`, `lead_inbox`, `spawn_engine`, and
`PaneRegistry`. I also checked the alleged `broadcast_actions` extraction and
the engine/UI/IPC ownership boundaries.

## Executive summary

I found **two high-severity state-management defects**, **one low-severity
capability-token leak**, and two verified cleanup/documentation opportunities.
The two high-severity findings have executable reproductions; neither is merely
an inferred race:

1. Lead notices are removed from both the live and durable queues before the
   PTY write is known to have succeeded. A session teardown at exactly that
   point loses a teammate's completion notice permanently.
2. Concurrent pipelines in the same project can claim the same pane. The newer
   run overwrites the pane's single `pipeline_run_id`, leaving the older run
   permanently pending.

The repository's existing gates are otherwise healthy: all 18 import-linter
contracts pass, Ruff passes, and all 3,600 collected tests complete successfully
(2 skipped). The suite is broad but does not exercise the three failure modes
above.

## Findings

### HIGH — Lead notification is dequeued before PTY delivery, so a teardown loses it permanently

**Locations:**

- `src/agent_takkub/lead_inbox.py:980-985`
- `src/agent_takkub/lead_inbox.py:1037-1040`
- `src/agent_takkub/lead_inbox.py:1112-1117`

**Evidence:** `_pump_lead_notify()` executes `queue.popleft()` at line 982 and
then calls `_notify_sess.write(payload)` at line 985 without a `try`/rollback or
durable spill. This is a check-to-write race: the session passed the liveness
checks at lines 907-925 but may be torn down before or during `write()`.

I ran a minimal `LeadInboxMixin` holder with an alive/ready session whose
`write()` raises `RuntimeError("session torn down")`. The observed state was:

```text
raised= session torn down
live_queue= []
durable_queue= []
```

The durable replay path magnifies the same failure. `_flush_pending_done_notices`
pops the **entire** durable project list and persists the empty state before it
loops through `_notify_lead()` (lines 1037-1040). If the first synchronous pump
write raises, the first notice and all remaining notices are gone from disk and
memory. `_force_deliver_done_notices()` repeats pop-before-write at lines
1112-1117.

This is not covered by the current notification tests. A caller search found
mid-write exception coverage only for the separate CC path in
`tests/test_peer_cc_durability.py:513-530`; that implementation explicitly uses
deliver-then-dequeue. `tests/test_orchestrator_notify_lead.py` covers busy,
death-before-pump, retry spill, and replay, but no `session.write` exception.

**Impact:** a real pane completion/failure notice can disappear, leaving Lead
unaware and an orchestration chain apparently stalled. Because the durable file
is already cleared in the replay case, restart cannot recover it.

**Recommendation:** make one queue owner responsible for an atomic
deliver-then-ack transition. Peek (`queue[0]`), attempt the write, and dequeue
only after success; on an exception or stale-session check, spill the untouched
item(s) to durable storage. For durable replay, do not pop/persist-empty in bulk;
ack each successfully transferred item. Apply the same rule to force delivery.
Add tests where write fails on item 1 and item N.

### HIGH — overlapping pipelines overwrite the pane's run ownership and orphan the older run

**Locations:**

- `src/agent_takkub/pipeline_executor.py:161-205`
- `src/agent_takkub/pipeline_executor.py:239-260`
- `src/agent_takkub/orchestrator.py:1779-1785`
- `src/agent_takkub/orchestrator.py:1919-1927`

**Evidence:** `run_pipeline()` always creates a new `PipelineRun` and inserts it
into `_pipeline_runs`; it performs no same-project overlap or role-ownership
check. `_fire_pipeline_hop()` treats any truthy result from `spawn()` as success
and assigns the pane's sole `PaneState.pipeline_run_id` to the new run at line
246. `spawn()` also returns success for an already-running pane, so this affects
both truly concurrent starts and reuse of a live role.

I executed two one-hop runs against a holder whose `spawn()` returned
`(True, "already running")`, matching the production contract. The observed
state was:

```text
after_run1_tag= run-one
after_run2_tag= run-two
run1_still_pending= ['backend'] run2_pending= ['backend']
```

When `backend` later calls `done`, `orchestrator.done()` snapshots only the
newest single tag (line 1784) and advances only that run (lines 1919-1927).
`run-one` remains in `_pipeline_runs` with `backend` pending and has no timeout
or other owner capable of advancing it.

The suite has extensive single-run coverage and a multi-project isolation test
(`tests/test_pipeline_executor.py:1044-1110`), but a caller/test grep found no
same-project concurrent-run or overlapping-role test.

**Impact:** the first pipeline silently hangs forever, leaks run state, and
never sends its completion/abort notification. A pipeline can also attach itself
to an already-running manual task and misinterpret that task's later `done` as
its own hop completion.

**Recommendation:** reject a pipeline start when any requested current-hop role
already has a non-closed pipeline owner (and decide explicitly whether a live
non-pipeline pane may be adopted). If overlapping pipelines are a supported
feature, replace the scalar `pipeline_run_id` with explicit many-to-many
ownership and route `done` with an unambiguous run/hop identity. The simpler and
safer current model is one active owner per pane.

### LOW — rejected explicit resume leaves a valid pane capability token registered

**Locations:**

- `src/agent_takkub/spawn_engine.py:1403-1419`
- `src/agent_takkub/spawn_engine.py:1678-1683`
- `tests/test_resume_session_picker.py:311-321`

**Evidence:** non-Lead spawn mints and registers `pane_tok` at line 1419, but an
invalid `resume_uuid` returns at line 1682 before entering the spawn
`try`/`except` that revokes tokens. I reused the repository's real
`_spawn_capture` harness with `_resume_uuid_matches_cwd=False`; the result was:

```text
result= (False, 'resume_uuid does not match cwd for backend')
tokens_after_rejected_resume= 1 [('default', 'backend')]
```

The existing rejection test correctly asserts no native spawn but does not
assert token cleanup. The leak is bounded per `(project, role)` because the next
mint revokes its predecessor, and the leaked secret was never delivered to a
child process, so this is low rather than high severity.

**Recommendation:** validate the explicit resume UUID before minting, or funnel
all post-mint returns through one `finally`/cleanup helper. Extend the existing
test with `assert not orch._pane_tokens`.

### LOW — slash-command injection is verified production-dead infrastructure

**Locations:**

- `src/agent_takkub/lead_inbox.py:331-472`
- `src/agent_takkub/orchestrator.py:593-601`

The method's own docstring says its only production callers were removed on
2026-07-10. Repository-wide caller search confirms that: outside the definition,
the only source hit is its self-recursive queue advance at `lead_inbox.py:395`.
All other callers are tests or historical docs. The method retains roughly 140
lines of polling/serialization logic plus `_slash_inject_busy` and
`_slash_inject_queue` state in every Orchestrator instance.

This is not a runtime defect, but it is a concrete refactor opportunity and a
maintenance trap: three focused test areas continue to preserve behavior no
production path can reach. Remove it and its state/tests unless a near-term
caller is explicitly planned; otherwise move it to a small opt-in component so
the core Lead inbox does not own dormant complexity.

### LOW — architecture map is stale about the engine extraction state

**Location:** `docs/architecture/godfile-map.md:13-18,46-53`

The map claims extraction is complete, lists `broadcast_actions.py`, and reports
`orchestrator.py` at 2,618 LOC. Verified current state:

```text
orchestrator.py        4055 LOC
main_window.py         1268 LOC
pipeline_executor.py    634 LOC
orchestrator_text.py     756 LOC
lead_inbox.py           1132 LOC
spawn_engine.py         2048 LOC
broadcast_actions.py    missing
```

A repository-wide search also finds none of the four named broadcast methods in
source; only the map contains their names. This is documentation drift rather
than dead code. It matters because the map calls itself the required navigation
ground truth and the review task named `broadcast_actions` as an existing mixin.

Update the map to distinguish completed/current modules from removed or planned
ones, and generate LOC/module-presence portions automatically if possible.

## Test coverage gaps (verified by symbol/test search)

`coverage.py` is not installed in the environment, so I used full-suite execution
plus source-to-test symbol/caller comparison rather than inventing a line-coverage
percentage.

Highest-value missing tests:

1. `LeadInboxMixin._pump_lead_notify`: `session.write` raises after the ready
   check; item must remain live or durable.
2. `_flush_pending_done_notices`: write fails on the first/middle replayed item;
   every unacknowledged item must remain durable across reload.
3. `PipelineMixin.run_pipeline`: two same-project pipelines share a role; second
   start must reject or preserve both owners without orphaning either run.
4. Pipeline start against an already-live non-pipeline pane: behavior must be
   explicit and tested rather than relying on `spawn(...)->True`.
5. Invalid `resume_uuid`: registered pane-token count must remain unchanged.
6. `PaneRegistry` itself has only indirect coverage and a token-lazy-init test;
   add a focused lifecycle test asserting all seven fields survive independent
   property setter use and that close/unregister removes the expected token/pane
   entries without resetting unrelated registry fields.

By contrast, every named pure helper in `orchestrator_text.py` had at least one
test-file hit, including transcript pruning/tails, artifact scan, paste framing,
Codex rewrite, digest/hot rendering, cwd/project memory, and transcript paths.

## Refactor observations

- `lead_inbox.py:704-774` duplicates the save/load mechanics for pending CC and
  pending done notices. A typed durable-queue helper would reduce drift; the
  different delivery semantics should remain explicit callbacks, not be hidden.
- Seven near-identical compatibility properties at
  `spawn_engine.py:448-530` are mechanical. A small descriptor can remove the
  repetition, but only if it preserves the current lazy fixture behavior. This
  is lower priority than removing the dormant slash injector.
- `orchestrator.py` is still 4,055 LOC. The extracted mixins meaningfully reduce
  conceptual load, but command/session/watchdog clusters remain coupled. Avoid
  another broad mixin extraction until the two ownership bugs above are fixed;
  both demonstrate that state transition ownership, not file length alone, is
  the important boundary.

## Verified non-findings / gates run

- `lint-imports`: **18 kept, 0 broken**, 87 files / 274 dependencies analyzed.
  No import-cycle/layer-contract claim is warranted from the current checkout.
- `python -m ruff check src tests`: **pass**.
- `python -m pytest -q`: **pass**, 3,600 collected tests, 2 skipped, wall time
  161.3 s.
- CLI IPC executes on the Qt main thread (`cli_server.py:8`), and remote HTTP
  requests marshal Orchestrator access through a queued Qt signal
  (`remote/http_server.py:1-11,79-97`). I found no evidence that background
  threads directly mutate `PaneRegistry` dictionaries.
- `PaneRegistry` does centralize the seven documented spawn-state containers;
  source-wide assignment search found production reassignments only through the
  compatibility properties. The concrete defects above are transition/ownership
  bugs, not cross-thread raw-dict mutation.

## Suggested fix order

1. Make Lead notice delivery deliver-then-ack and add failure-injection tests.
2. Enforce one pipeline owner per pane (or design explicit multi-owner state)
   and add overlap/live-pane tests.
3. Move resume validation before pane-token minting and add the cleanup assert.
4. Remove or isolate the production-dead slash injector.
5. Refresh `godfile-map.md` and consolidate duplicated durable-queue mechanics.
