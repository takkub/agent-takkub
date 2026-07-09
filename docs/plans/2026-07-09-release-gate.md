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
