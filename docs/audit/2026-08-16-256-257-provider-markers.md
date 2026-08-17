# #256 / #257 — provider marker fixes (2026-08-16)

Scope: `provider_spec.py` (data layer) + the ready-marker fixture/selftest
table in `pty_session.py` + marker-focused tests only. No touches to
`lead_inbox.py` / `lead_wait.py` / `task_delivery.py` / `pty_session.py`'s
writer-queue code / `remote/*.py` / `worktree_manager.py` — those are other
panes' concurrent work (#258 in particular owns `pty_session.py`'s
writer/queue).

## #256 — gemini/agy false-positive auth-failure on every cold start

**Root cause confirmed by Lead's transcript** (`gemini-090814.transcript.log`):
agy's own cold-start banner reads *"You are currently not signed in."* on
**every single spawn**, before the CLI has even started its OAuth check —
not just when auth genuinely failed. `"not signed in"` lived in
`GENERIC_AUTH_ERROR_MARKERS`, the zero-grace **instant**-fail table applied
to every provider via `auth_error_markers_for()`. Every agy cold boot
therefore tripped a false `[auth-failure]` notice to Lead, even while the
pane went on to sign in and run its task normally seconds later.

### Fix

1. **Removed `"not signed in"` from `GENERIC_AUTH_ERROR_MARKERS` entirely**
   (not per-provider-excluded — no such mechanism exists on that table, and
   building one for a single known offender would be premature). The three
   remaining generic markers (`"please sign in again"`, `"please log in
   again"`, `"please authenticate"`) are untouched.
2. **Added `"not signed in"` to `gemini_spec.auth_transient_markers`** —
   the same grace-gated tier its existing `"signing in"` /
   `"verifying your account"` entries already use. `auth_failure_reason()`
   only convicts a transient marker once the screen has been **static**
   (`seconds_since_output() >= AUTH_TRANSIENT_GRACE_SEC`, default 45s) —
   a normal boot keeps producing new output (banner → spinner → signed-in
   header), so the clock never accumulates that long.
3. **Requirement "override a stale marker once a newer identity is on
   screen" needed no new code.** `_ready_region()` already scopes every
   marker check to the bottom `_READY_TAIL_ROWS` (6) non-blank rows. Once
   the signed-in identity header and real task output push the banner out
   of that window, the marker simply cannot match anymore — the exact
   tail-scoping that already protects every other check in this module
   (see the module-top note above `_ready_region`) doubles as the
   "override" mechanism #256 asked for. No identity-parsing (email/model
   name) was added — it wasn't needed once the transient-tier move was made.
4. Tests: `TestGeminiColdBootNotSignedIn` in
   `tests/test_auth_failure_detection.py` — not-in-generic-table, no
   false-positive during a normal boot (low `seconds_since_output`), still
   convicts if genuinely stuck past grace, never fires for another
   provider, and a direct "banner scrolled out of the ready region" case
   proving point 3 above.

### What did NOT change

- `auth_transient_markers_for()`'s "never falls back to a generic table"
  design (documented in its own docstring) was kept — a transient marker
  only makes sense paired with one provider's exact wording, so
  `"not signed in"` was NOT also added as a cross-provider generic
  transient entry, even though `GENERIC_AUTH_ERROR_MARKERS` exists for the
  instant tier. If a second provider's own boot chrome is later confirmed
  to say the same thing, add it to that provider's own
  `auth_transient_markers`, the same way this fix did for gemini.
- The boot-stall-detector-should-cover-sign-in-not-just-MCP-boot follow-up
  the issue mentions (extending #254) is out of scope here — #254's
  detector lives outside this task's file boundary.

## #257 — kimi: `ready_rules` was empty, tasks never delivered

**Root cause confirmed by Lead's live test today**: `kimi_spec.ready_rules
== ()`. `is_at_ready_prompt()` iterates `_READY_RULES` looking for a
substring match — an empty tuple can never match, so it always returned
`False` for a kimi pane, no matter what its screen showed. Delivery paths
gated on ready state therefore never fired; the task text never appeared on
screen even after a manual resend, confirmed by grep against the transcript.

### Fix

1. **Added `kimi_spec.ready_rules = (ReadyRule("ctrl-x: toggle mode",
   True),)`** — from the real idle-footer capture provided:
   `main  @: mention files | ctrl-x: toggle mode | shift-tab: plan mode |
   ctrl+o: editor` (kimi-cli 1.49.x, Windows ConPTY, signed in,
   2026-08-16). `"ctrl-x: toggle mode"` was chosen as the distinctive half
   — no substring collision with any other provider's ready/busy markers.
2. **Busy marker deliberately left uncalibrated** — no authenticated Kimi
   session running an actual task was available for this change, and the
   task explicitly forbids guessing it. Left `ready_hard_blockers=()` with
   an expanded comment explaining exactly what's missing (footer/status
   line while Kimi is actively generating) and how to capture it
   (assign kimi a real task, watch the footer mid-generation). Until then
   the cross-provider `esc to interrupt`/`esc to cancel` dedup table is the
   only busy signal, which may not even apply if Kimi words its interrupt
   hint differently.
3. **Added `kimi_spec.auth_error_markers = ("send /login to login",)`** —
   the exact text observed today on a fresh, uncredentialed kimi spawn:
   `"Model: not set, send /login to login"`. Narrowed to the
   `"send /login to login"` half (dropped `"model: not set"`) because model
   selection is settable per-role independent of login state — a future
   "model not set" wording for an unrelated reason must not silently
   convict the pane on login grounds. This is an **instant**-fail marker
   (not transient like gemini's banner): a kimi pane in this state genuinely
   cannot do anything (no model selected), unlike agy's normal-for-a-few-
   seconds boot chrome.
4. **Added `is_ready_marker_calibrated(provider) -> bool` and
   `uncalibrated_providers() -> tuple[str, ...]`** to `provider_spec.py` —
   pure data-layer predicates over `ready_rules == ()`, satisfying issue
   point 3 ("a provider with empty ready_rules shouldn't fail silently")
   at the layer this task is scoped to. **No spawn-time Lead warning is
   wired to these yet** — that requires touching `spawn_engine.py` and/or
   `lead_inbox.py`, both outside this task's allowed file list. Whoever
   picks up that follow-up should call `is_ready_marker_calibrated()`
   instead of re-deriving "empty tuple" as the signal. As of this change,
   `uncalibrated_providers()` returns `("cursor",)` — kimi is now
   calibrated for readiness (busy marker still pending, tracked in point 2
   above).
5. Tests: `test_kimi_idle_footer_is_ready` in
   `tests/test_pty_ready_prompt.py` (direct `PtySession` capture), a new
   `_READY_SELFTEST_CASES` entry tagged `"kimi"` in `pty_session.py` (run
   by `ready_marker_selftest()`, i.e. `takkub doctor`),
   `TestKimiNotLoggedIn` and `TestReadyMarkerCalibrationStatus` in
   `tests/test_auth_failure_detection.py`.

## Follow-ups NOT done here (flag to Lead)

- **Spawn-time "provider uncalibrated" warning** (#257 point 3): the data
  predicate exists (`is_ready_marker_calibrated` /
  `uncalibrated_providers`), but nothing calls it yet. Needs a
  `spawn_engine.py` or `lead_inbox.py` change — assign to whichever role
  owns that surface (backend, per the #258 split observed in this same
  batch).
- **Kimi busy marker** still unconfirmed (#257 point 2) — needs a real
  task run against an authenticated kimi pane to capture the
  mid-generation footer/status line, then a one-line data addition to
  `kimi_spec.ready_hard_blockers`.
- **#254 boot-stall detector scope** (#256's "related" note): currently
  covers MCP-boot phase only; agy's sign-in phase (~90s) is a separate gap
  the issue flags but this task's file boundary doesn't reach.
- **cursor** remains fully uncalibrated (`ready_rules=()`,
  `auth_error_markers=()`) — no CLI was available to test in this task
  either; `uncalibrated_providers()` will show it.

## Verification

Targeted tests only (per test-tier policy — full suite is qa's batch-gate
job, not this task's):

```
tests/test_auth_failure_detection.py
tests/test_pty_ready_prompt.py
tests/test_delivery_auth_failure.py
tests/test_provider_spec_effort.py
tests/test_no_content_watchdog_cap.py
```

103 passed, 0 failed. `ruff check` clean on every touched file.
