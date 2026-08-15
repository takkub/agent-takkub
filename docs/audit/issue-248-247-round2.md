# #248/#247 fix, round 2 — fail-fast + degrade

Builds on round 1's detector core (`docs/audit/issue-248-247-round1.md`):
`PtySession.first_content_ts()` / `seconds_since_byte()` / `seconds_since_output()`
and `Orchestrator._pane_display_state()` (spawning/active/ready). Round 1 gave
the cockpit a way to *see* "spawned but silent" and "content frozen"; round 2
makes it *act* on those signals instead of riding out `BUSY_WAIT_CEILING_SEC`
(1800s default) or blind-pasting into a dead pane.

Three independent pieces, all provider-neutral (claude/codex/gemini/opencode/
kimi/cursor) and cross-platform (no OS-specific branches needed — everything
here is text/timer logic, no PTY backend calls).

## 1. Auth-failure detection

`PtySession.auth_failure_reason(provider) -> str | None` (`pty_session.py`).
Two tiers, both scoped to `_ready_region` (bottom footer rows, same as every
other prompt-state check in that class — conversation body text quoting a
marker phrase must not poison the verdict):

- **Instant markers** — `provider_spec.auth_error_markers_for(provider)`:
  that provider's own confirmed list (`ProviderSpec.auth_error_markers`,
  currently empty for every provider — none has been observed against a real
  failure screen yet) ORed with `GENERIC_AUTH_ERROR_MARKERS`, a cross-provider
  baseline (`"not signed in"`, `"authentication required"`, `"invalid api
  key"`, `"unauthorized"`, ...). Fires with **no grace period**.
- **Transient markers** — `provider_spec.auth_transient_markers_for(provider)`:
  a normally-legitimate boot-time marker that only counts as a failure once
  the screen has been **static** for `AUTH_TRANSIENT_GRACE_SEC` (45s default,
  `TAKKUB_AUTH_TRANSIENT_GRACE_SEC` override) — measured via
  `seconds_since_output()`, so an animating spinner next to the same text
  still reads as a normal cold boot. gemini/agy is the one confirmed case
  (`auth_transient_markers=("signing in", "verifying your account")`,
  reusing the exact text already in `gemini_spec.ready_hard_blockers` — same
  underlying CLI state, different question: "is it ready" vs "is it stuck").

Every other provider's `auth_error_markers`/`auth_transient_markers` field is
`()`, each with a comment flagging it as unverified rather than guessed —
per the task's explicit instruction, a wrong guess either false-matches
ordinary conversation text or never fires, and neither is caught by tests.

Wired into `lead_inbox.py::_send_when_ready`'s `_check()` poll loop, checked
every 150ms poll exactly like the existing `_prompt_block_reason` check
(`#186`) right above it: first detection warns Lead once
(`_warn_lead_auth_failure` — names the provider's display name and
`ProviderSpec.post_install_note`, e.g. "run `codex login` once to sign in")
and immediately delivers unconfirmed instead of continuing to poll — so a
genuine auth failure is reported in well under a minute instead of up to
1800s.

Defensive: `auth_failure_reason()`'s return is checked with
`isinstance(_auth_reason, str)` before trusting it — a `MagicMock()`-backed
test session (used throughout the existing test suite) otherwise returns a
truthy `Mock` object for any unconfigured method call, which broke 16
pre-existing tests during development until this guard was added.

## 2. No-content watchdog

`NO_CONTENT_WATCHDOG_SEC` (`orchestrator.py`, 75s default,
`TAKKUB_NO_CONTENT_WATCHDOG_SEC` override) — chosen above every provider's own
`ready_wait_ms` cold-boot allowance except claude's (45s; claude producing
zero output ever is a rarer, different failure mode, and claude is the
degrade target anyway so there's no further fallback for it) but far below
`BUSY_WAIT_CEILING_SEC`.

Also checked every poll in the same `_check()` loop: if `pane.session.
first_content_ts()` is still `None` once `elapsed[0] >= NO_CONTENT_WATCHDOG_SEC
* 1000`, `lead_inbox.py::_recover_no_content_pane` fires:

1. **First occurrence** — close + respawn once, no `--resume`/snapshot
   machinery needed (zero content ever rendered, so there's no conversation
   to preserve — unlike `_auto_recover_stuck`'s stuck-but-alive case).
2. **Second occurrence** (retry also silent) — degrade: force the respawn to
   claude via a new `PaneState.provider_override` field (mirrors
   `model_override`'s existing per-pane-override precedent in
   `spawn_engine.py`; `spawn()`'s `effective_provider` computation now reads
   `_ps_initial.provider_override or effective_provider_for(...)` — the same
   "unavailable provider → claude substitute" idea `provider_config.
   effective_provider_for` already applies globally off config/install-state,
   just triggered per-pane here instead).
3. Persisted via `PaneState.no_content_recover_attempts` (0/1/2 — snapshotted
   before `close()` pops the PaneState, restored after, same shape
   `_auto_recover_stuck` already uses for its own counters), so the cap holds
   across the close→respawn cycle. Cleared on a deliberate fresh (non-auto-
   respawn) spawn, kept across the watchdog's own `_from_auto_respawn=True`
   respawns — same split `stuck_recover_attempts` already uses.
4. Still silent after both attempts → falls through to the pre-existing
   `elapsed[0] >= max_wait_ms` blind-deliver-unconfirmed path as the final
   safety net (unchanged from before this round).

**Why not just keep polling the same `_check()` closure after respawn:**
`close()` fully tears down the `AgentPane` **widget** (`paneClosed` →
`main_window._remove_teammate_pane` → `destroy_terminal` + `setParent(None)`)
and a respawn (`_ensure_teammate_pane`) builds a **brand new** `AgentPane`
object — the `pane` reference `_check()`'s closure captured goes stale the
moment `close()` returns. So recovery hands off to a **fresh**
`_send_when_ready(role_name, task, project=project_ns)` call once the delayed
respawn lands (which re-fetches the pane from the registry at its own top),
the same way `_auto_recover_stuck`'s `_do_respawn` re-sends a snapshotted task
after its own close→respawn cycle. The 2s close-then-respawn pause before
`_do_respawn` also mirrors `_auto_recover_stuck` (lets the PTY/WebEngine
teardown finish before a new session binds to the same role slot).

Each occurrence gets one Lead notice (`_warn_lead_no_content`,
`[no-content-retry]` / `[no-content-degrade]`).

## 3. `takkub doctor` auth checks

`doctor.py::check_provider_auth()` (new, registered in `run_all_checks()`
right after `check_providers`). Before this, only claude had any auth signal
(`check_claude`'s credential-file check) — codex/gemini/opencode/kimi/cursor
only ever got a binary-presence check.

For every **installed** non-claude provider (shares `_resolve_provider_bin`,
factored out of `check_providers()` to module scope so both checks agree on
"installed"):

- **codex**: presence-only check against `$CODEX_HOME/auth.json`
  (`~/.codex/auth.json` default — same `CODEX_HOME` resolver
  `codex_helper.py` already uses for its sessions dir). A well-known
  `@openai/codex` CLI convention, not empirically confirmed on this machine —
  WARN (never FAIL) either way so a wrong guess can't block a healthy
  machine's `doctor` run.
- **every other provider** (gemini/opencode/kimi/cursor): `Status.INFO`
  `"unknown — no confirmed credential-file location"` + that provider's
  `post_install_note` as the fix hint. Per the task's explicit instruction —
  never claim a provider is authenticated without proof; each of these
  providers' credential storage is a black box the cockpit deliberately never
  reads (see `codex_helper.py`/`gemini_helper.py` module docstrings: "the
  cockpit never touches those credentials").

## Testing

Targeted only (per project policy — full suite is qa's batch-gate job):
`test_doctor.py`, `test_provider_spec_effort.py`, `test_pane_display_state.py`,
`test_output_content_fingerprint.py`, `test_delivery_blocked_prompt.py`,
`test_delivery_busy_wait_notice.py`, `test_delivery_unconfirmed.py`,
`test_delivery_supersede.py`, `test_spawn_task_delivery.py`,
`test_stuck_recover.py`, `test_spawn_gate.py`, `test_provider_models.py`,
`test_lifecycle_recovery.py`, `test_orchestrator_auto_respawn_replay.py`,
`test_orchestrator_done_gate.py`, `test_pty_session_*.py`,
`test_lead_inbox_digest.py`, `test_auto_chain.py`, `test_task_handoff.py`,
`test_session_resume.py`, `test_qa_plan_fanout.py`, `test_project_scoping.py`,
`test_orchestrator_shard.py`, `test_throughput_watchdog.py`,
`test_update_splash_recovery.py`, `test_done_evidence.py`,
`test_installed_cwd_fallback.py`, `test_teammate_effort_resolver.py`,
`test_spawn_codex_argv.py`, `test_provider_project_scope.py`,
`test_provider_config.py`, `test_opencode_provider.py`,
`test_lead_provider_unlock.py`, `test_lead_model_override.py`,
`test_idle_watchdog.py`, `test_h1_nonclaude_env.py`,
`test_cursor_provider.py`, `test_mcp_resolution_fail_closed.py` — all green
(one pre-existing/unrelated failure pair in `test_project_scoping.py` traced
to a missing `runtime/` dir in this fresh worktree, reproduced identically on
a clean `git stash`; not caused by this change).

`ruff check` / `ruff format --check` / `lint-imports` (25/25 contracts kept)
all clean on every touched file.

One test needed updating: `test_doctor.py::TestRunAllChecks::
test_returns_list_of_findings` patches every `run_all_checks()` check
function to isolate the two it cares about — added a
`check_provider_auth` patch alongside the existing `check_providers` one
(expected update when adding a new check to that list, not a behavior
regression).

## Files touched

- `src/agent_takkub/provider_spec.py` — `auth_error_markers` /
  `auth_transient_markers` fields on `ProviderSpec`; `GENERIC_AUTH_ERROR_
  MARKERS`, `AUTH_TRANSIENT_GRACE_SEC`, `auth_error_markers_for()`,
  `auth_transient_markers_for()`.
- `src/agent_takkub/pty_session.py` — `PtySession.auth_failure_reason()`.
- `src/agent_takkub/orchestrator.py` — `NO_CONTENT_WATCHDOG_SEC` constant.
- `src/agent_takkub/spawn_engine.py` — `PaneState.provider_override` /
  `no_content_recover_attempts`; `spawn()`'s `effective_provider` now
  consults `provider_override` first; fresh-spawn-clear block resets both
  new fields on a deliberate (non-auto-respawn) spawn.
- `src/agent_takkub/lead_inbox.py` — auth-failure + no-content checks wired
  into `_send_when_ready`'s `_check()`; `_warn_lead_auth_failure`,
  `_warn_lead_no_content`, `_recover_no_content_pane`.
- `src/agent_takkub/doctor.py` — `_resolve_provider_bin` factored to module
  scope; `check_provider_auth()`, `_codex_auth_finding()`; registered in
  `run_all_checks()`.
- `tests/test_doctor.py` — patch list update (see above).

## Round-2 follow-up (Lead review fix loop)

Lead reviewed the round-2 diff and held merge on two gaps before it landed:
the auth-failure fast-fail path could blind-deliver over a pane that was
demonstrably fine, and none of round 2's new behavior (kill+respawn, provider
swap) had a single test. Both closed below, same branch.

### 1. Auth detection could convict a pane sitting at its own ready prompt

`_check()`'s original ordering ran the auth-marker check **before** the
`is_at_ready_prompt()` check, and a single matching poll triggered an
immediate `_deliver(unconfirmed=True)` — a blind paste — with **zero**
regard for whether the pane was, at that exact instant, sitting idle at its
own ready prompt. That combination can only mean one thing: the marker text
is stale (e.g. scrolled up from a just-finished test run), because a CLI
genuinely stuck on auth can never reach its own ready prompt. Blind-pasting
into an idle, reachable pane is exactly the busy-wait/#130 failure mode this
whole detector exists to avoid re-introducing.

Compounding it: `GENERIC_AUTH_ERROR_MARKERS`' first pass included several
phrases that are ordinary HTTP/test-framework vocabulary, not CLI chrome —
`"unauthorized"` (any 401 log line), `"invalid credentials"`/`"invalid api
key"` (typical login-test assertion text), `"session expired"`, `"login
required"`, `"authentication required"`/`"authentication failed"` (generic
app error strings), and especially `"not authenticated"` — **FastAPI's own
default 401 `detail` is the literal string "Not authenticated"**, so a
backend pane's own test suite exercising its project's auth feature would
have auto-failed itself. Scoped to the 6-line footer tail
(`_ready_region`), not the whole scrollback, so a long test run's final
lines can easily still be sitting there when a poll lands — this was a real,
not theoretical, false-positive risk.

Fix, both in `lead_inbox.py::_send_when_ready`'s `_check()` and
`provider_spec.py`:

1. **Ready wins.** `is_at_ready_prompt()` is now read once per poll into
   `_pane_ready_now` and consulted *before* the auth-marker branch: if the
   pane is at its ready prompt, the auth check is skipped entirely for that
   poll (and the confirm streak below is reset to 0) — normal delivery
   proceeds via the pre-existing ready-streak path instead. The same
   `_pane_ready_now` value is reused later for the ready-streak block itself,
   so this costs one `is_at_ready_prompt()` call per poll, not two.
2. **Multi-poll confirmation.** A new `_AUTH_FAILURE_CONFIRM_POLLS` (5,
   ~750ms at the 150ms poll cadence) requires the marker to match on that
   many **consecutive** not-ready polls — tracked via a new
   `auth_failure_streak` counter, reset to 0 both on a non-match and on a
   ready poll — before `_check()` convicts. Trivial next to the ceiling this
   whole path exists to beat (`BUSY_WAIT_CEILING_SEC`, 1800s) but enough to
   filter a one-frame render artifact; a pane genuinely stuck on auth keeps
   showing the marker on every poll, so this costs it nothing.
3. **Narrowed `GENERIC_AUTH_ERROR_MARKERS`** down to `"not signed in"`,
   `"please sign in again"`, `"please log in again"`, `"please authenticate"`
   — phrases that read only as first-person CLI chrome telling an *operator*
   to re-auth, not text any plausible unrelated dev-output string
   reproduces. Missing a real failure is acceptable (falls through to the
   ordinary busy-wait/max_wait_ms path eventually); convicting normal dev
   output blind is not.

### 2. Zero test coverage for round 2's new kill+respawn / provider-swap logic

The round-1 diff (739 insertions across 6 source files) shipped with a
single added test line. Added, all targeted (`.venv\Scripts\python.exe -m
pytest`, no full suite):

- `tests/test_auth_failure_detection.py` — pure `PtySession.
  auth_failure_reason()` logic against a minimal duck-typed fake (no real
  ConPTY needed): instant marker match, `_ready_region` scoping (marker
  outside the 6-line tail window is ignored), transient marker withheld
  before `AUTH_TRANSIENT_GRACE_SEC` and fired once elapsed + screen-static,
  a transient marker never firing for a provider with none confirmed, the
  narrowed `GENERIC_AUTH_ERROR_MARKERS` table (asserts the removed phrases
  are gone + a literal FastAPI-shaped "Not authenticated" test-failure line
  no longer false-positives).
- `tests/test_delivery_auth_failure.py` — `_send_when_ready`/`_check()`
  integration: a ready pane with a marker present on every poll delivers
  normally with no auth-failure warning; a ready poll resets the confirm
  streak so a later relapse needs its own full fresh streak (proven via
  `auth_failure_reason.call_count`, not just "no warning fired"); fewer than
  `_AUTH_FAILURE_CONFIRM_POLLS` consecutive matches never fires; exactly
  `_AUTH_FAILURE_CONFIRM_POLLS` consecutive matches fires exactly once and
  blind-delivers.
- `tests/test_no_content_watchdog_cap.py` — the no-content watchdog's
  1-retry + 1-degrade cap: attempt 0 → `_recover_no_content_pane(...,
  degrade=False)`; attempt 1 → `degrade=True`; attempt 2 → recovery is
  **not** called again, falls through to the ordinary
  `elapsed[0] >= max_wait_ms` blind-deliver-unconfirmed safety net (proven
  end-to-end, including the `[delivery-unconfirmed]` Lead notice).
- `tests/test_provider_override.py` — drives a real `spawn()` call (claude
  branch fully mocked, mirrors `test_spawn_task_delivery.py`'s harness):
  `provider_override` wins over `effective_provider_for`'s answer for that
  spawn; with no override, `effective_provider_for` is honored normally;
  `_from_auto_respawn=True` does **not** clear `provider_override` /
  `no_content_recover_attempts` (the watchdog's own degrade respawn must not
  undo itself); a fresh manual spawn still honors the pre-clear override for
  *that* launch but clears both fields for the *next* one.

All four new files plus the full existing round-2 targeted list re-run
green. `ruff check` / `ruff format --check` clean on every touched file;
`lint-imports` 25/25 contracts kept.

### Files touched (this follow-up)

- `src/agent_takkub/lead_inbox.py` — `_AUTH_FAILURE_CONFIRM_POLLS` constant;
  `_check()`: `_pane_ready_now` computed once and reused; auth-marker branch
  now ready-gated + streak-gated (`auth_failure_streak`).
- `src/agent_takkub/provider_spec.py` — `GENERIC_AUTH_ERROR_MARKERS` narrowed
  from 12 entries to 4.
- `tests/test_auth_failure_detection.py`, `tests/test_delivery_auth_failure.py`,
  `tests/test_no_content_watchdog_cap.py`, `tests/test_provider_override.py`
  — new.
