# Cross-Platform Fix Wave — Follow-up (backend, FU1 + FU2)

**Date:** 2026-07-10
**Scope:** two follow-up items from the cross-platform audit (`docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md` META section + reviewer's M2 note), both touching `spawn_engine.py`.

---

## FU1 — provider pane self-update-exit dropping in-flight task silently

**Ask:** codex pane self-updated mid-task (`Update ran successfully! Please restart Codex.`) and exited, dropping the assigned task with no retry. Extend auto-respawn to detect a `working` pane exiting unexpectedly (not `takkub done`/`close`) and re-deliver the last-assigned task, capped at `AUTO_RESPAWN_MAX`, across every provider (claude/codex/gemini, #103), both OS.

**Finding: the detect → cap → respawn → re-deliver pipeline already existed and is already tested end-to-end** (`_on_session_exit`/`_on_codex_exit` → `_auto_respawn` in `spawn_engine.py`, backed by `tests/test_orchestrator_auto_respawn_replay.py`). Traced the exact scenario:

1. `agent_pane.AgentPane._on_exit` distinguishes expected exit (`close()`/`done()` called `mark_expected_exit()` first) from unexpected — any provider process dying on its own (crash, OOM, self-update-exit, `/exit`) sets `pane.state = "exited"`. This is provider-agnostic: it fires from the same Qt `processExited` signal regardless of whether the pane is running claude, codex, or gemini/agy.
2. `spawn_engine._on_session_exit` (wired for shell/gemini and, via `_on_codex_exit`, for codex) only proceeds when `pane.state == "exited"` — i.e. only for exactly this "process died without an expected-exit mark" case — and schedules `_auto_respawn` after an exponential-backoff delay, gated by `AUTO_RESPAWN_MAX` (existing cap, unchanged) so a deterministically-crashing pane can't spawn-loop.
3. `_auto_respawn` calls `self.spawn(role_name, ...)`, which re-resolves the role's *current* effective provider (claude/codex/gemini) each time — so a codex-role crash respawns through the codex branch again, not a stuck claude branch. Codex/gemini both funnel through the shared `_launch_session` tail, so this path is identical for all three providers and both OS (Windows ConPTY / macOS `_pty_backend`) — nothing here is claude-only.
4. On successful respawn, `_auto_respawn` re-delivers `PaneState.last_assigned_task` (the full task text cached at `assign()` time) via `_send_when_ready`, **unless** the fresh spawn was a claude `--resume` (in which case claude's own conversation history already has it — re-pasting risks duplicate non-idempotent work, the Bug-5 guard).

This is exactly the FU1 ask, already implemented and already regression-tested (`test_task_replayed_after_crash`, `test_no_replay_when_no_prior_assign`, `test_no_replay_after_manual_close`, `test_no_replay_when_spawn_fails`, `test_done_clears_replay_cache_so_late_crash_does_not_replay`).

### Real gap found: stale `last_spawn_resumed` across provider substitution

The one genuine cross-provider bug: `last_spawn_resumed` (the flag `_auto_respawn` reads to decide whether to skip replay) is only ever *written* inside the claude branch's `--resume`/`--session-id` logic. The shell/gemini/codex branches (which share `_launch_session`) never touch it — so if the **same role slot** was previously spawned via the claude branch (e.g. codex/gemini toggled off or not installed → claude substitute stood in and did an actual `--resume`, setting the flag `True`) and then re-spawns on its real codex/gemini branch after the provider becomes available again, the stale `True` would survive and incorrectly suppress task replay on a subsequent crash — even though codex/gemini have no `--resume` concept at all and this spawn was never a resume.

**Fix:** `_launch_session` now explicitly resets `last_spawn_resumed = False` right after `attach_session`, before wiring the exit handler — since shell/gemini/codex spawns are never resumes, this makes the flag correct unconditionally instead of "whatever it happened to be last time this role slot went through the other branch."

**Files:** `src/agent_takkub/spawn_engine.py` (`_launch_session`).

**Tests added:**
- `tests/test_launch_session.py::TestLaunchSessionCommonTail::test_resets_stale_last_spawn_resumed_flag` — pins the flag reset itself.
- `tests/test_orchestrator_auto_respawn_replay.py::TestAutoRespawnReplay::test_provider_self_update_exit_replays_task` — end-to-end repro of the exact FU1 scenario (codex pane `working` → unexpected exit while a stale `last_spawn_resumed=True` is present from an earlier claude-substitute spawn → `_auto_respawn` still respawns + replays the cached task).

**Not changed (out of scope / already sufficient):**
- No codex-self-update-text detection was added (e.g. sniffing `"Update ran successfully"` from the PTY buffer) — unnecessary, since detection is generic process-exit based and doesn't depend on *why* the process exited. Issue #62's existing `is_at_update_splash()` + Enter-dismiss path already covers the *different* case where codex shows an update prompt but does **not** exit (stays alive, needs a keypress) — that's a separate mechanism from this one and was left untouched.
- Logging (`auto_respawn_scheduled` / `auto_respawn_done` / `auto_respawn_replay` / `auto_respawn_capped` `_log_event` calls) was already in place and sufficient for diagnosing this class of incident; no changes made.

---

## FU2 — `_default_plugin_dirs` ignored a project's custom profile

**Ask:** `lead_context._default_plugin_dirs()` always resolved the plugin cache from `config.default_claude_config_dir()`, ignoring a project pinned to a custom profile (`CLAUDE_CONFIG_DIR` override via `user_profile`) — so a profile-scoped project's GUI-injected `--plugin-dir` list would point at the wrong (default) profile's cache instead of its own.

**Fix:**
- `_default_plugin_dirs(role: str | None = None, project: str | None = None)` — new optional `project` param. When given, resolves the cache base via `user_profile.config_dir_for(project)` (falls back to the default profile's dir when the project has none registered — identical result to omitting `project`). When omitted (`project=None`), behaviour is byte-identical to before (`config.default_claude_config_dir()`), so every existing caller (doctor, smoke tests, the plugin-policy test suite) is unaffected.
- Call site `spawn_engine.spawn()` (claude branch, ~line 1575) now passes `project=project_ns` — the same project namespace already resolved at the top of `spawn()` and already used by the sibling F1-fixed `_resume_uuid_matches_cwd` call a few hundred lines earlier in the same function, so this is consistent with the existing pattern in this file.

**Files:** `src/agent_takkub/lead_context.py` (`_default_plugin_dirs`), `src/agent_takkub/spawn_engine.py` (call site).

**Tests added** (`tests/test_plugin_policy.py::TestCustomProfilePluginDirs`):
- `test_project_with_no_profile_matches_default` — a project with no `set_profile()` call resolves identically with/without a `project` arg.
- `test_custom_profile_project_uses_its_own_cache` — a project pinned to a custom profile sees *that* profile's plugin cache (and explicitly does **not** see a plugin only present in the default cache — proves it isn't silently falling back).
- `test_different_project_default_profile_unaffected_by_others_custom` — registering a custom profile for one project doesn't leak into a different project still on the default profile.

New `isolate_profiles` fixture also isolates `pane_tools_policy.PANE_TOOLS_POLICY_FILE` to a tmp path — this dev machine's real `~/.takkub/pane-tools.json` has role overrides that would otherwise leak into `effective_plugins()` and make these tests flaky depending on whose machine runs them (see note below).

**Not changed (explicitly out of scope per task spec):** `pane_tools_dialog.py:56` and `plugin_installer.py` were also flagged by the audit's M2/F2 notes for the same class of hardcoded-`~/.claude` issue, but the FU2 task text scoped this follow-up to `_default_plugin_dirs` + its one call site only. F2 (`plugin_installer.installed_on_disk`) is a separate reviewer-flagged regression, not part of this task.

---

## Test run

Targeted (not full suite, per project convention):
```
tests/test_launch_session.py
tests/test_orchestrator_auto_respawn_replay.py
tests/test_plugin_policy.py
tests/test_lifecycle_recovery.py
tests/test_codex_crash_instrumentation.py
tests/test_spawn_gate.py
tests/test_task_handoff.py
tests/test_fix_round2_edge_cases.py
tests/test_regression_findings_2026_06.py
tests/test_pipeline_executor.py
tests/test_auto_chain.py
tests/test_project_scoping.py
tests/test_orchestrator_session_uuid.py
```
All green except 2 **pre-existing** failures unrelated to this diff (confirmed by re-running against `git stash` / unmodified tree — same 2 tests fail there too):
- `TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Root cause: this dev machine's real `~/.takkub/pane-tools.json` has a role-plugin override that the pre-existing test fixture (`fake_cache`, which only patches `Path.home()`) never isolated `PANE_TOOLS_POLICY_FILE` against — an ambient test-isolation gap in tests that predate this task, not something introduced by it. Not touched (out of scope for FU1/FU2); the new `isolate_profiles` fixture added for the FU2 tests isolates it correctly for the new test class only.

`ruff check` + `ruff format` clean on all touched files. `lint-imports`: 18/18 contracts kept (the new `lead_context.py → user_profile` import doesn't cross a layer boundary).

No commit made — Lead commits after QA gate.
