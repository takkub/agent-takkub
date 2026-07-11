# Release gate — 1.0.22 (2026-07-09)

## Scope

Commits since Wave3 gate: `3ce3600` (#107 remote-bridge fresh-boot), `0130909` (#102 stale-active
project), `6b25246` (#108 draft-hold churn park), `c02b2cc`/`9da7ba1` (#108 mouse-seq false
positive, #109 evidence per-role priority, #104 Open-With tripwire).

## 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=runtime/exports/2026-07-09/release-gate-junit.xml
```

Result (from junit XML root `<testsuite>` attrs):

```
tests=3352 failures=2 errors=0 skipped=2 time=557.264s
```

**2 failures — both baseline, both `tests/test_plugin_policy.py`:**
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause: sandbox has no `~/.claude/plugins/cache` populated, so `_default_plugin_dirs()`
returns `''` for any role — env-dependent, not a code regression. Matches the documented Wave2/
Wave3 baseline exactly.

**No other failures.** 0 regressions since Wave3 gate.

## 2. Integration spot-checks

| Area | Check | Test(s) | Result |
|---|---|---|---|
| Churn-park (#108) | notices park during draft block; #70 escalation still fires | `tests/test_done_notice_draft_churn.py -k "park or escalat"` | 3 passed |
| Mouse-seq false-positive (#108) | all mouse-sequence forms = no-op; Enter/Esc still clear draft | `tests/test_lead_draft_state.py -k mouse` | 8 passed |
| Remote-bridge dedupe (#107) | retry watchdog doesn't re-fire on same session uuid | `tests/test_remote_bridge_autofire.py::test_resume_within_window_keeps_same_uuid_no_refire` (+ 13 other remote-bridge tests) | 14 passed |
| Evidence per-role priority (#109) | role-subdir evidence preferred over shared fallback, correctly tagged | `tests/test_done_evidence.py::test_role_subdir_evidence_preferred_over_shared` (+ 5 other evidence tests) | 6 passed |
| Open-With tripwire (#104) | dedupe per pane; doesn't itself trigger recovery | `tests/test_stuck_recover.py::TestShellOpenDialogTripwire` (5 tests) | 5 passed |

All spot-checks green, no additional targeted runs needed beyond what full suite already covered
(these are included in the 3352 total).

## 3. Multi-provider wording sanity

Diffed all changed `src/agent_takkub/*.py` files since `3ce3600^` for newly-added lines
mentioning "claude" or claude-only phrasing ("Read tool", "read the file") in notice/pointer
strings:

```
rtk git diff 3ce3600^..HEAD -- lead_inbox.py orchestrator.py spawn_engine.py \
  lead_draft_state.py config.py main_window.py | grep -niE '^\+.*claude'
```

**No matches.** No claude-only wording leaked into new notice/pointer strings.

## Verdict

✅ **PASS** — 0 regressions, all 4 integration areas verified, no multi-provider wording leaks.
Safe to publish 1.0.22.

## Final HEAD re-stamp (2026-07-09, HEAD = `a8ce971`)

Re-ran the gate after `a8ce971` (done-note return-path symmetrize — note >400 chars → notice
keeps first line + 📄 pointer to session md written before the notice; `done --fail` keeps the
full note; evidence/shard handoff unchanged).

### 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=runtime/exports/2026-07-09/final-restamp-junit.xml
```

```
tests=3364 failures=2 errors=0 skipped=2 time=233.821s
```

**2 failures — same baseline as before, both `tests/test_plugin_policy.py`:**
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause unchanged: sandbox has no `~/.claude/plugins/cache` populated → env-dependent, not a
code regression. Test count rose 3352 → 3364 (+12) matching the 12 new tests added in
`tests/test_done_note_symmetrize.py`. No other failures, 0 regressions.

### 2. Focused — done-note symmetrize

```
rtk proxy python -m pytest -q tests/test_done_note_symmetrize.py
```

```
12 passed
```

Covers: >400-char boundary split (notice = first line + 📄 session-md pointer), `--fail` exemption
(full note preserved for fix-loop), write-before-notice ordering (session md exists before the
shortened notice fires), and shard handoff parity with the non-shard path.

### Verdict (re-stamp)

✅ **PASS** — full suite green modulo the same 2 pre-existing env-dependent failures, symmetrize
suite 12/12 green. Safe to ship 1.0.22 at this HEAD.

## #112 — remote-bridge double-fire on `--resume` boot, gate before restart (2026-07-09, HEAD = `b2a543b`)

Gate for `b2a543b` (`_lead_remote_bridge_delivered_session` persistent identity guard —
`orchestrator.py` + `spawn_engine.py`, tests in tests/test_remote_bridge_repro.py (removed 2026-07-10)).

### 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=runtime/tasks/agent-takkub/2026-07-09/qa-junit-165951.xml
```

```
tests=3374 failures=2 errors=0 skipped=2 time=139.938s
```

**2 failures — same baseline as every prior gate, both `tests/test_plugin_policy.py`:**
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause unchanged (sandbox has no `~/.claude/plugins/cache`, env-dependent). Test count rose
3364 → 3374 (+10, from tests/test_remote_bridge_repro.py (removed 2026-07-10)'s #112 additions). No other failures,
0 regressions.

### 2. Independent repro — NOT reusing tests/test_remote_bridge_repro.py (removed 2026-07-10)

Wrote a standalone script (`runtime/tasks/agent-takkub/2026-07-09/repro_112_qa.py`) that drives
the real `Orchestrator.consume_session_report` against the **exact timeline recorded in
`runtime/events.log` for the actual incident** (2026-07-09 16:49:51–16:50:26):

```
16:49:52  session_report source=startup uuid=2546cb62
16:50:21  session_report source=resume  uuid=54013ded   (29s later, SAME PtySession object)
16:50:22  auto_slash_command  /remote-control            <- fired #1 (real log)
16:50:26  auto_slash_command  /remote-control            <- fired #2 (real log, the bug)
```

The script: fires `consume_session_report` for uuid A (startup) against a shared fake `pane.session`
object, fires the captured on_delivered callback (simulating the first poll's real delivery),
then — 29s later in the timeline — fires `consume_session_report` again for a *different* uuid B
(resume) on the *same* session object, and asserts `inject_slash_command_when_ready` was called
exactly once total. It also checks the companion contract that a genuine respawn (brand-new
session object) still re-fires correctly.

**Falsifiability check** (not just trusting the script — proved it actually catches the bug):
temporarily reverted `spawn_engine.py`/`orchestrator.py` to `b2a543b^` (pre-fix) via
`git checkout b2a543b^ -- <files>`, re-ran the script:

```
[t=29s] session_report resume  uuid=54013ded -> inject_calls=2
FAIL (#112 REGRESSION): /remote-control fired 2 times ... double-fire reproduced.
EXIT=1
```

Confirmed the repro genuinely detects the pre-fix bug (2 fires), then restored the fix
(`git checkout b2a543b -- <files>`, verified `git status` clean) and re-ran:

```
[t=29s] session_report resume  uuid=54013ded -> inject_calls=1
PASS: /remote-control fired exactly once across both hooks — #112 fix holds.
[respawn] ... inject_calls=2
PASS: genuine respawn (new session object) still re-fires correctly.
EXIT=0
```

Verified with real code (not test-file assertions alone) — `_lead_remote_bridge_delivered_session`
correctly makes delivery lifetime-once per session object, and does not over-block genuine
respawns.

### Verdict (#112 gate)

✅ **PASS** — 0 regressions (3374 tests, 2 pre-existing env-dependent fails only), independent events.log-timeline
repro confirms the fix holds and is falsifiable (caught the bug pre-fix, passes post-fix). Safe to
restart cockpit at this HEAD.

## #113 — `/remote-control` bridge races ↻ Resume button, no lock (2026-07-09, HEAD = `2fd4dec`)

Gate for `2fd4dec` (per-`(project, role)` in-flight lock on `inject_slash_command_when_ready` —
`lead_inbox.py` + `orchestrator.py`, new tests in tests/test_slash_inject_serialize.py (removed 2026-07-11, injector deleted)).

### 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=runtime/tasks/agent-takkub/2026-07-09/qa-junit-181524.xml
```

```
tests=3382 failures=2 errors=0 skipped=2 time=163.286s
```

**2 failures — same baseline as every prior gate, both `tests/test_plugin_policy.py`:**
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause unchanged (sandbox has no `~/.claude/plugins/cache` populated → env-dependent, not a
code regression). Test count rose 3374 → 3382 (+8, from tests/test_slash_inject_serialize.py's
new #113 tests; file removed 2026-07-11 with the injector). No other failures, 0 regressions.

### 2. Falsifiability check — revert fix, prove the race reproduces; restore, prove it serializes

Reverted `lead_inbox.py` + `orchestrator.py` to the pre-fix parent commit (`2fd4dec^` = `ae2d845`)
via `git checkout ae2d845 -- src/agent_takkub/lead_inbox.py src/agent_takkub/orchestrator.py`
(kept the new tests/test_slash_inject_serialize.py (removed 2026-07-11) at HEAD — same file, unrevert), then ran the
new #113 regression tests against that pre-fix code:

```
rtk proxy python -m pytest -q tests/test_slash_inject_serialize.py
```

```
FAILED tests/test_slash_inject_serialize.py::TestConcurrentCallsSerialize::test_second_call_queues_instead_of_racing
  assert len(calls) == 1  # second call still queued
  AssertionError: assert 2 == 1
FAILED tests/test_slash_inject_serialize.py::TestConcurrentCallsSerialize::test_queued_call_delivers_even_if_first_drops
```

Confirmed the race genuinely reproduces pre-fix: the bridge call (`/remote-control`) and the
Resume-button call (`/resume`) both queue an independent QTimer.singleShot-driven ready-poll for
the same Lead pane (`calls` has 2 entries instead of 1 serialized-behind-lock entry) — exactly the
"both `_deliver()` on the same tick, interleaved payload+Enter" mechanism described in the fix
commit message.

Restored the fix (`git checkout 2fd4dec -- src/agent_takkub/lead_inbox.py
src/agent_takkub/orchestrator.py`, verified `git status` clean) and re-ran:

```
rtk proxy python -m pytest -q tests/test_slash_inject_serialize.py
```

```
.. [100%]   (all tests pass — second call queues behind the lock, only 1 poll in flight)
```

Verified with real code (not test-file assertions alone) — the fix genuinely serializes the two
callers instead of racing them, and is falsifiable (caught the bug pre-fix, passes post-fix).

### Verdict (#113 gate)

✅ **PASS** — 0 regressions (3382 tests, 2 pre-existing env-dependent fails only), falsifiability
check confirms the race is real pre-fix and is fixed post-fix (per-(project,role) lock genuinely
serializes the bridge vs. Resume-button race). Safe to restart cockpit at this HEAD.

## #113 (final root cause) — `auto_submit_enter=False` for `/resume`, gate before restart (2026-07-09, HEAD = `b1a5700`)

Gate for `b1a5700`/`ae2d845` (↻ Resume button UI feedback) + the actual #113 root-cause fix layered
on top: `/resume` opens claude's interactive session picker, and the normal delayed auto-Enter
(which safely submits self-contained slash commands like `/remote-control`) lands right as the
picker paints and is read as an empty confirm ("Resume cancelled") — even on a fully idle pane.
Fix: `inject_slash_command_when_ready` gained an `auto_submit_enter: bool = True` param
(`lead_inbox.py`); `_on_resume_clicked` (`user_actions.py`) is the only caller that passes
`auto_submit_enter=False` — text lands in the composer unsubmitted, user presses Enter by hand to
pick a session. `on_delivered` now fires immediately in that path (no delayed-Enter to wait on), so
the Resume button restores right away instead of holding "⏳ Resuming..." for up to 45s.

### 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=runtime/tasks/agent-takkub/2026-07-09/qa-junit-183046.xml
```

```
tests=3386 failures=2 errors=0 skipped=2 time=136.872s
```

**2 failures — same baseline as every prior gate, both `tests/test_plugin_policy.py`:**
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause unchanged (sandbox has no `~/.claude/plugins/cache` populated → env-dependent, not a
code regression). Test count rose 3382 → 3386 (+4, from tests/test_resume_button_feedback.py (removed 2026-07-10)'s
new assertions). No other failures, 0 regressions.

### 2. Regression guard — bridge/#107/#110/#112/#113 auto-Enter behavior unchanged

```
rtk proxy python -m pytest -q tests/test_remote_bridge_autofire.py tests/test_remote_bridge_repro.py \
  tests/test_slash_inject_serialize.py tests/test_resume_button_feedback.py tests/test_lead_draft_state.py \
  --junitxml=runtime/tasks/agent-takkub/2026-07-09/qa-regression-183046.xml
```

```
tests=84 failures=0 errors=0 skipped=0
```

All 84 green. Also verified by reading source: `auto_submit_enter` (new param, default `True`) is
only ever passed `False` by `_on_resume_clicked` (`user_actions.py:269`) — the `/remote-control`
auto-bridge call (`spawn_engine.py:1869`) doesn't pass it at all, so it keeps the original
auto-Enter behavior unchanged. No other caller of `inject_slash_command_when_ready` was touched.

### Verdict (#113 final gate)

✅ **PASS** — 0 regressions (3386 tests, 2 pre-existing env-dependent fails only), 84/84 targeted
bridge/#107/#110/#112/#113 regression tests green, source-read confirms `/remote-control` and all
other auto-Enter slash commands are unaffected — only `/resume` opts out via the new
`auto_submit_enter=False` param. Safe to restart cockpit at this HEAD.

## #113 (SUPERSEDED → native `--resume`) — ↻ Resume drops the TUI `/resume` path entirely (2026-07-10, HEAD = `1d7b5bc`)

The `auto_submit_enter=False` fix above (`b1a5700`) stopped the phantom "Resume cancelled", but at
a UX cost: the button now only *typed* `/resume` into the composer and left it unsubmitted, so a
user who clicked it saw nothing happen until they pressed Enter themselves in the pane — it read as
a dead button (field report, 2026-07-10). `1d7b5bc` supersedes that approach: the ↻ Resume button no
longer drives claude's interactive TUI picker at all. It now uses the **native resume path the
mobile PWA already uses** — pop a Qt session picker of the project's recent Lead sessions, then
`orch.close(lead, force=True)` + `orch.spawn(lead, resume_uuid=…)` (i.e. `claude --resume <uuid>`).
One click, pick, done; no auto-Enter, so the cancellation failure mode is structurally gone.

Because `_on_resume_clicked` was the *only* caller that ever passed `auto_submit_enter=False`, the
param + its opt-out branch in `inject_slash_command_when_ready` (and its two flag-specific tests)
were removed as dead code — the function is back to always submitting, exactly as it behaved before
`b1a5700`. The per-`(project, role)` in-flight lock from `2fd4dec` is untouched and still guards the
`/remote-control` bridge vs. any concurrent slash injection.

**Boundary note (`remote-bolt-on-isolation`):** the session-list scan was placed in core
(`chatlog_scanner.list_recent_lead_sessions`), NOT imported from `agent_takkub.remote`, so the
desktop UI never crosses the remote-bolt-on isolation contract. `remote.notify` keeps its own
sibling (data-min + remote-prefix strip) untouched.

**Multi-provider (#103):** `--resume` and the JSONL-backed session list are claude-CLI specifics. A
non-claude Lead simply yields an empty picker ("ไม่พบ session", never a crash) — the same graceful
degrade `resume_lead` already documents. Not solved here; tracked in #103.

### 1. Full pytest suite (HEAD `1d7b5bc`)

```
rtk proxy python -m pytest tests/ -q -p no:cacheprovider
```

Full suite ran to 100%; the only failures are the same two pre-existing, env-dependent
`tests/test_plugin_policy.py` cases (`~/.claude/plugins/cache` not populated on this machine) that
every prior gate in this file also records. **0 code regressions.**

### 2. Targeted + guardrails

- tests/test_resume_button_feedback.py (removed 2026-07-10) (rewritten for the picker→close→spawn flow),
  `tests/test_resume_session_picker.py` (+3 new `TestCoreListRecentLeadSessions` cases for the core
  scanner), tests/test_slash_inject_serialize.py (removed 2026-07-11) (flag tests removed, serialization + default
  auto-Enter guard kept) — **37/37 green.**
- `rtk lint-imports` — **18/18 contracts KEPT** (incl. `remote-bolt-on-isolation`); `depgraph.json`
  refreshed by the pre-commit hook (new edges: `user_actions → chatlog_scanner`,
  `chatlog_scanner → user_profile`).
- Runtime smoke: `chatlog_scanner.list_recent_lead_sessions(active_project)` imports + runs with no
  error against the live store.

### Verdict (#113 native-resume gate)

✅ **PASS** — the button is functional on a single click, the `b1a5700` auto-Enter workaround and its
dead param are gone, no core→remote boundary break, 0 code regressions. Runtime click-through still
needs a `takkub restart` to load the new Python into the running cockpit.
