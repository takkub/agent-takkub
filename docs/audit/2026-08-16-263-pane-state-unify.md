# #263 — pane state has 3 disagreeing sources of truth (full fix, round 2)

Continuation of the partial fix landed in
`docs/audit/2026-08-16-263-264-266-notify-truth.md` (now merged into
`main` as part of the 1.0.64 release). That pass shipped only the
delivery-unconfirmed flag and deliberately left the ready-marker/
provider-calibration items unattempted because `provider_spec.py` and the
pty_section ready-marker fixture table were reserved for a parallel pane
(#256/#257) at the time. Both have since merged — this pass does the rest.

## What was still open

1. Unify `pane.state` (orchestrator-declared at dispatch) / the ready
   marker (screen-scraped) / the progress clock into one state with a
   clear priority order.
2. Distinguish "login-required", "booting", "waiting-delivery" as their
   own states in `takkub list`/`status` instead of collapsing everything
   into bare `working`/`active`.
3. A provider whose ready-marker table is still uncalibrated (`cursor`)
   must read `unknown`, not a confidently-wrong `active`.
4. Tests built from real per-provider transcript fixtures, covering the
   3 evidence cases from the issue: gemini stuck sign-in, codex stuck MCP
   boot / genuinely busy with no task ever dispatched, kimi stuck needing
   `/login`.

## Design decision: additive `display_state`, not a rewrite of `state`

`list_status_detailed()["state"]` (== `_pane_display_state(pane)`) already
has real dependents whose behavior must not change:

- `lead_wait.py::_resolve_role_wait_status` does `if state == "working": ...`
  to decide whether a watched role is still genuinely pending.
- The pre-existing `_pane_display_state` unit tests
  (`test_pane_display_state.py`) pin its exact spawning/active/ready
  vocabulary, including a test that literally encodes today's bug
  (`test_active_session_with_content_not_ready_is_active` — codex's own
  "esc to interrupt" hard-blocker reading as bare "active").

Rewriting `"state"` in place to fix #263's evidence would have meant
either breaking `_resolve_role_wait_status`'s wait-resolution logic or
rewriting that pinned test's expected value under the "don't regress
1.0.64" instruction's shadow — riskier than necessary for what is, at
bottom, a *display* problem: Lead needs to SEE the richer truth, not have
every internal state-comparison silently start meaning something new.

Fix: a new `Orchestrator._derive_display_state(pane, base_state,
delivery_unconfirmed)` method, layered ON TOP of the unchanged
`_pane_display_state` output, producing a **new, additive**
`"display_state"` key in `list_status_detailed()` — same contract
`blocked_reason`/`delivery_unconfirmed` already established in the
previous pass. `"state"` itself is untouched, byte-for-byte, everywhere.
`takkub list`/`status`'s CLI rendering (`cli_server.py`'s `cmd == "list"`
handler, `cli.py`'s `_print_status_report`) switch to reading
`display_state` (falling back to `state` for a payload predating this
key); every internal consumer (`_resolve_role_wait_status`, stall
detection, the pinned unit tests) keeps reading `state`, completely
unaffected.

## Priority order (`_derive_display_state`, first match wins)

Screen-scraped ground truth always beats an orchestrator-declared or
notice-derived label — the CLI itself cannot be doing two contradictory
things at once:

1. **`login-required`** — `session.auth_failure_reason(provider)` matches
   (kimi's "send /login to login", gemini's "Signing in..." once past its
   transient grace period). Both detectors already existed
   (`PtySession.auth_failure_reason`, shipped for #248/#247 round 2 /
   #256 / #257) — this pass only *wires* the existing signal into the
   display layer, it adds no new marker text.
2. **`booting`** — `session.shows_startup_marker()` (also pre-existing:
   `_STARTUP_MARKERS` = "booting mcp server" / "starting mcp server" /
   "tab to queue message", the codex/agy MCP cold-boot chrome). Distinct
   from `base_state == "spawning"` (nothing printed at all yet) — this is
   "printed a banner, still not at its own ready prompt".
3. **`waiting-delivery`** — `base_state == "working"` and
   `delivery_unconfirmed` is True (the previous pass's flag). Covers a
   provider with no calibrated auth marker hitting the same underlying
   shape tier 1 catches directly for gemini/kimi.
4. **`busy`** — `base_state == "active"` (i.e. never promoted to
   "working" by any dispatch) but the screen's own hard-blocker markers
   say the CLI is actively generating/interrupting anyway. This is the
   codex evidence: `takkub list` said "active" while the screen plainly
   showed "Working (0s - esc to interrupt)". New detector:
   `PtySession.is_hard_blocked_for(provider)`.
5. **`unknown`** — `base_state == "active"` and the pane's provider is in
   `provider_spec.uncalibrated_providers()` (empty `ready_rules` —
   currently only `cursor`). The active/ready split is meaningless for a
   provider whose ready-marker table was never confirmed; claiming either
   would be a guess dressed up as fact.
6. Otherwise `base_state` unchanged.

Every screen-scrape check is wrapped in its own `try/except` and falls
through to the next tier on failure — same fail-open contract
`_pane_display_state` already uses, so a loose test double or a
torn-down session degrades gracefully instead of raising.

## New primitive: `PtySession.is_hard_blocked_for(provider)`

`is_at_ready_prompt()`/`is_at_ready_prompt_cached()` collapse "hard-blocked
(genuinely busy)" and "no ready marker matched yet (ambiguous)" into the
same bare `False` — exactly what hid the codex evidence. The new method
answers only the first question, scoped to one provider's own
`ready_hard_blockers`, reusing the same `_ready_region` window and
"verifying your account" / "please try again shortly" carve-out
`_classify_ready_for_provider` already uses for its self-test.

Deliberately **duplicates** rather than refactors that carve-out instead
of extracting a shared helper both would depend on — `_classify_ready` /
`_classify_ready_for_provider` are covered by `ready_marker_selftest()`'s
shipped-table self-test, and risking that self-tested precedence chain
for an unrelated feature wasn't worth it. `tests/test_auth_failure_detection.py`'s
new `TestIsHardBlockedFor` class proves the duplicate stays faithful
(codex hard-blocked/idle, unknown-provider/uncalibrated-provider never
match, ready-region scoping, the "verifying your account" exception).

## Files touched

- `src/agent_takkub/pty_session.py` — new `is_hard_blocked_for(provider)`
  method.
- `src/agent_takkub/orchestrator.py` — new `_derive_display_state`; wired
  into `list_status_detailed` (new `"display_state"` key, `"state"`
  unchanged) and `pane_status_report` (same, plus surfaces
  `delivery_unconfirmed`, previously silently dropped by that report
  despite being in `list_status_detailed`'s output already).
- `src/agent_takkub/cli_server.py` — `cmd == "list"` handler renders
  `display_state` instead of the raw `state`.
- `src/agent_takkub/cli.py` — `_print_status_report` (`takkub status`)
  renders `display_state` and a new "❓ delivery unconfirmed" suffix.
- Tests: `tests/test_derive_display_state.py` (new, 19 cases — priority
  ordering in isolation, real per-provider transcript fixtures for all 3
  evidence cases from the issue, `list_status_detailed` wiring proving
  `state` stays unchanged while `display_state` is added),
  `tests/test_auth_failure_detection.py` (new `TestIsHardBlockedFor`
  class, 6 cases).

## Deliberately out of scope

- Renaming the existing `"ready"`/`"active"` display vocabulary itself
  (the issue's "idle" language) — those two already correctly distinguish
  "confirmed idle at prompt" from "has content, not confirmed idle", and
  renaming them would touch every consumer of the *unchanged* `"state"`
  key for a purely cosmetic gain. Only the genuinely NEW distinctions the
  issue asked for (`login-required`/`booting`/`waiting-delivery`/`busy`/
  `unknown`) were added.
- Re-deriving `pane.state` itself (the underlying `AgentPane.state`
  attribute the UI widget also reads for spinner/button chrome) — out of
  scope by design; this is a display-layer fix over the existing
  dispatch-declared state, not a rewrite of dispatch bookkeeping.

## Verification

Targeted runs only (per instruction — full suite is qa's batch gate):

```
tests/test_derive_display_state.py ................... [19 passed]
tests/test_auth_failure_detection.py + test_pane_display_state.py +
  test_delivery_unconfirmed_status_flag.py + test_delivery_unconfirmed.py
  -> 89 passed

tests/test_resource_queue_visibility.py + test_pending_done_notice_visibility.py +
  test_orchestrator_stall.py + test_lead_wait.py + test_cli_status.py +
  test_cli_server.py -> 145 passed

tests/test_pty_ready_prompt.py + test_pty_session_reader_proc_race.py +
  test_pty_session_spawn_timeout.py + test_pty_session_threading.py +
  test_doctor.py -> 164 passed

The full previous-pass batch (#264/#266/#263 partial's own targeted list,
re-run to prove no regression from this round's changes to
list_status_detailed/pane_status_report/cli_server.py/cli.py):
tests/test_lead_wait.py test_fan_out_delivery_race.py test_lead_draft_guard.py
  test_lead_draft_state.py test_adaptive_digest_window.py test_lead_inbox_digest.py
  test_notice_revalidation.py test_delivery_unconfirmed_status_flag.py
  test_delivery_unconfirmed.py test_inbox_report.py test_auto_chain.py
  test_fix_round2_edge_cases.py test_pipeline_executor.py test_pty_writer_queue_v2.py
  test_reap_multiproject.py test_pending_done_notice_visibility.py
  test_orchestrator_notify_lead.py test_done_notice_draft_churn.py
  test_resource_queue_visibility.py test_orchestrator_stall.py
  -> 404 passed (identical count to the prior pass — no regression)
```

Run via the repo's shared venv against this worktree's `src/`:

```
PYTHONPATH="<worktree>/src" PYTHONIOENCODING=utf-8 \
  <repo>/.venv/Scripts/python.exe -m pytest <files>
```
