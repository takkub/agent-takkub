# Wave 3 chunk verify — 2026-07-09

Scope: W3 resume picker (`cea32c3`), ProviderSpec registry phase 0 (`bca2e13`), KNOWN_ROLES
stopgap (`0c63e3c`).

## 1. Full pytest (qa batch gate)

```
rtk proxy python -m pytest -q --junitxml=<scratch>/wave3-full-junit.xml
```

Result: **3312 tests, 2 failures, 2 skipped, 0 errors** — 177s.

Failures (both = pre-existing baseline, confirmed env-dependent, not a regression):
- `tests/test_plugin_policy.py::TestRolePluginPolicy::test_teammate_gets_superpowers_and_pordee_not_addy`
- `tests/test_plugin_policy.py::TestRolePluginPolicy::test_design_roles_get_ui_ux_pro_max`

Both fail because `_default_plugin_dirs()` reads a real `~/.claude/plugins/cache` layout this
sandbox doesn't have (`fake_cache` fixture returns empty joined string) — matches the documented
baseline exactly. No other failures, no errors, no unexpected skips. **Gate: PASS.**

## 2. ProviderSpec behavior-neutral spot-check

- `ready_marker_selftest()` (`pty_session.py`) — ran directly: **0 failures** across all canned
  ready/busy screens, both against the combined `_READY_RULES` table AND each case re-checked
  against its own provider's spec in isolation (`_classify_ready_for_provider`) — proves no case
  secretly depends on another provider's marker being present in the concat.
- Concat order verified in code: `_READY_RULES_BY_PROVIDER = (gemini, codex, claude)` —
  preserves the original hand-written table's precedence (gemini's `"gemini cli update
  available!"` must beat codex's bare `"update available!"` substring collision).
- Spot-checked 4 field values against their cited source lines:
  - `claude_spec.autonomy_flags["default"] = ["--dangerously-skip-permissions"]` — cites
    `spawn_engine.py:1441` ✓ (matches current code)
  - `codex_spec.ready_rules` drops the `"openai codex (v"` banner-alone rule per #99, keeps only
    `"fast off"/"fast on"` — comment cites the #99 fix rationale correctly, matches
    `pty_session.py:225,247-248` cited lines
  - `gemini_spec.binary_names = ["agy", "agy.exe"]` — matches `spawn_engine.py:1050-1053`
    install-instructions text verbatim
  - `codex_spec.task_notice_preamble` — byte-identical to the pre-refactor
    `orchestrator_text.py` `_CODEX_TASK_NOTICE` constant per the module's own docstring; spot-read
    confirms the Thai text matches current `orchestrator_text.py` usage
- `pytest -k "provider"` (part of full suite above): all pass, including provider_config /
  spawn_codex_argv / pane_tools_policy provider-adjacent tests.

**Verdict: behavior-neutral refactor holds — no drift found.**

## 3. W3 resume/session picker

- `resume_uuid` param threads through `orchestrator.spawn()` → `spawn_engine.py:1610-1625`:
  when set, validated via `_resume_uuid_matches_cwd()` (`spawn_engine.py:90`) **before** being
  used — decodes the JSONL parent dir name (`chatlog_scanner.decode_project_dir`) and compares
  against the resolved spawn cwd. Mismatch → `spawn()` returns `(False, "resume_uuid does not
  match cwd for ...")`, no `--resume` flag ever reaches argv. Forgery-proof (reads from disk, not
  trusting client-supplied cwd).
- `GET /api/lead/sessions` → `api.lead_sessions()` → `notify.list_recent_lead_sessions()`:
  view-mode safe, data-min confirmed — each entry is `{uuid, mtime, preview}` only;
  `_first_user_preview()` truncates to first user-typed line, never full transcript content.
  Route requires `_check_bearer() + _check_password_gate()` same as other view routes, no
  `allows_control()` requirement (correctly read-only).
- `POST /api/lead/resume` → `api.resume_lead()`: route requires bearer + password gate **+
  `self.server.auth.allows_control()`** (`http_server.py:450`) — 403 in view mode, matching the
  same control-gate pattern as `/api/open`, `/api/close`, `/api/lead/say`. Confirmed control-only.
  `resume_lead()` itself re-validates project is open + has a cwd before closing the current Lead
  pane and respawning with the uuid (defense in depth — request-shape check happens before ever
  touching orch state; the real cwd-match check happens again inside `spawn()`).
- `tests/test_resume_session_picker.py` (573 lines) + `tests/test_remote_pwa_resume.py` (87
  lines) both green in the full run.

**Verdict: PASS — no gap found.**

## 4. Integration: spawn routing unchanged across providers

- `tests/test_spawn_codex_argv.py` + relevant subset of `tests/test_spawn_gate.py` (provider/
  argv/codex/gemini/claude-tagged cases) — all green, included in the full-suite run above.
  ProviderSpec fields marked "WIRED in Phase 0" (autonomy_flags, install_instructions,
  ready_hard_blockers/ready_rules, codex task_notice_preamble) are the only ones actually feeding
  call sites; the rest are faithful documentation for a later phase, confirmed by reading the
  module docstring and the four migrated call sites (`orchestrator_text.py`,
  `provider_config.py`, `pty_session.py`, `spawn_engine.py`) — matches the commit's own claim.

**Verdict: PASS — spec-driven argv reproduces pre-refactor behavior for all 3 providers.**

## Overall

All four checks pass. No regressions found beyond the pre-existing 2 baseline failures in
`test_plugin_policy.py` (documented, env-dependent, unrelated to this wave's changes).
