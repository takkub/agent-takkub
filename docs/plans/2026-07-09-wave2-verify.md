# Wave 2 batch verify — 2026-07-09

Scope: 11 commits `0faac8d..HEAD` (Wave 1 #2 tail-through Wave 2 #3/#4/#1/#5/#99/#106 + roles feat + wording fix).
Ref: `docs/plans/2026-07-09-core-upgrade-plan.md`.

## 1. Full pytest suite

```
rtk proxy python -m pytest -q --junitxml=<scratch>/wave2-junit.xml
```

**Result:** `tests=3274 failures=2 errors=0 skipped=2` — matches known baseline exactly, no regressions.

- `tests/test_plugin_policy.py::test_teammate_gets_superpowers_and_pordee_not_addy` — FAIL (baseline, env-dependent: `_default_plugin_dirs` returns `''` in this sandbox because the fake `~/.claude/plugins/cache` fixture doesn't mirror the real installed-plugin layout)
- `tests/test_plugin_policy.py::test_design_roles_get_ui_ux_pro_max` — FAIL (same root cause)
- Skips: `tests/test_orchestrator_harvest.py:76,172` — "symlinks not supported" / "cannot create directory symlink on this platform" — pre-existing Windows limitation, unrelated to this wave.

## 2. Integration cross-checks

### Draft guard (#3) × pointer delivery (#1) × auto /remote-control (#4)
All three paths gate through the **same** single choke point, `LeadInboxMixin._lead_can_accept_injection()` (`lead_inbox.py`):
- `_pump_lead_notify` (done-notice delivery) — line 865
- `_flush_pending_lead_cc` (CC queue flush) — line 701
- `inject_slash_command_when_ready` (auto `/remote-control` bridge, #4) — line 374-376 (gated only for `role_name == LEAD.name`; teammate panes have no draft tracker fed, so the check is a documented no-op there)

Held-draft spill path (`_lead_draft_hold_expired`, line 874) requeues via the same `notify_draft_spill` system-note mechanism regardless of which of the three producers is trying to deliver — no separate/competing queue found, so no starvation/deadlock path where one producer's items get stuck behind another's hold timeout indefinitely. Task-pointer delivery itself (`_task_handoff_pointer`, `orchestrator_text.py`) writes the file up-front at assign time and only the short pointer string goes through the paste path above — same gate applies, no bypass.

**Verdict: correctly integrated, no regression.**

### Evidence scan (#5) × artifacts dir (#1)
- `pane_env._apply_artifacts_dir()` stamps `TAKKUB_ARTIFACTS_DIR = RUNTIME_DIR / "exports" / <date> / <project_ns>`
- `Orchestrator._scan_done_evidence()` scans `RUNTIME_DIR / "exports" / <date> / <project_ns>` for screenshots newer than the assign timestamp

Identical path formula (`config.RUNTIME_DIR / "exports" / today / project_ns`) — confirmed by direct read of both call sites. **Paths match; no drift.**

### Codex ready-marker (#99) × other pane delivery ordering
`_delayed_enter_verified` (`lead_inbox.py`) cross-checks `shows_pending_input()` before trusting a "not ready" read — used identically at its 3 call sites (`inject_lead_prompt`, `_pump_lead_notify`, task-pointer paste path). The `'openai codex (v'` banner was removed from ready markers per commit 529a35a; codex now only reports ready on composer-bar detection. No other pane-type delivery path references the removed banner string (grepped — 0 hits outside `pty_session.py`'s own marker table and its tests). Full-suite pass includes `tests/test_fix_round2_edge_cases.py` (updated in the same commit) and `tests/test_pty_session_threading.py` — both green.

### Cached ready-state (#106) × `is_at_ready_prompt` call sites
`is_at_ready_prompt_cached()` (lock-free, reader-thread-fed) is used at exactly **one** call site: `agent_pane.py:487` (UI status-dot rendering — jank fix target). Every other call site that gates a correctness-sensitive decision (draft guard, submit-verify, staleness escalation, auto-respawn, slash-command delivery) still calls the live, lock-taking `is_at_ready_prompt()` unchanged. **No correctness-critical path was silently switched to the stale cache.**

## 3. New role files — analyst / security / docs

All three (`.claude/agents/{analyst,security,docs}.md`) carry the full protocol block matching existing roles: SPECIALIST OVERRIDE, Version control (บังคับ) w/ allow/deny git command lists, `🗂️ ไฟล์ชั่วคราว/อ่านไฟล์`, `การสื่อสารระหว่าง agents`, `⚠️ Blocked/clarification`, `การรายงานกลับเมื่อเสร็จ`. Structurally complete, nothing missing vs. sibling role files.

**Gap found (not a regression, pre-existing pattern extended to new roles):** `pane_tools_policy.KNOWN_ROLES` (`pane_tools_policy.py:36-49`) was not updated to include `"analyst"`, `"security"`, `"docs"`. Effect:
- `takkub mcp list/allow/deny --role analyst|security|docs` and the `plugins` equivalents all reject with `"unknown role"` (`cli.py:1079,1092,1147,1160` validate against `KNOWN_ROLES`).
- Runtime tool resolution itself (`effective_mcps`/`effective_plugins`) is **not** gated by `KNOWN_ROLES` — it just checks the policy-file dict — so panes for the 3 new roles still spawn and get whatever `default` the caller passes; this is a CLI-management gap, not a spawn-time functional bug.
- `takkub assign --role <any>` has no `choices=` restriction, so spawning these roles works fine; `takkub send --to <role>` is also unrestricted.

**Recommendation:** add `"analyst"`, `"security"`, `"docs"` to `KNOWN_ROLES` in a follow-up so Lead can manage their MCP/plugin allowlists via the documented CLI/status-bar chip.

## 4. Multi-provider wording sweep (claude-only strings)

Commit `13f12b8` fixed the **generated task-pointer** wording (`orchestrator_text.py`) from "Read tool" → "file-read tool". Grepping the **static role definition files** (`.claude/agents/*.md`) for the same string turned up the fix was not swept there:

```
grep -c "Read tool" .claude/agents/*.md
analyst.md:1   backend.md:2   codex.md:1   critic.md:2   designer.md:2
devops.md:2    docs.md:1      frontend.md:2  gemini.md:2  mobile.md:2
qa.md:2        reviewer.md:1  security.md:1
```

**Notable:** `codex.md` and `gemini.md` are the role files actually used when the real Codex/Gemini CLI panes run (not just Claude substitutes) — both still instruct `"อ่านไฟล์ด้วย **Read tool** เสมอ"`, which is Claude-specific terminology and doesn't match either CLI's actual tool naming. This directly falls under the multi-provider directive ("wording ใน task/pointer อย่าผูก claude-only … โดยไม่มี fallback"). Pre-existing across all 13 role files (not introduced by this wave's 3 new roles — they just inherited the existing template), but flagged now since #4 of this verify pass asked specifically for this sweep and the directive was reinforced this same wave.

**Recommendation:** follow-up pass to reword the `🗂️ ไฟล์ชั่วคราว/อ่านไฟล์` section across all 13 role files to neutral wording (e.g. "เครื่องมืออ่านไฟล์ของคุณ (file-read tool)", matching the just-fixed pointer wording), same as `13f12b8` did for the generated pointer string.

## Summary

- **No test regressions.** 3274 tests, 2 known-baseline fails (env-dependent, unrelated to this wave), 2 known-baseline skips (Windows symlink limitation).
- **No integration conflicts found** across draft-guard × pointer-delivery × auto-remote-control, evidence-scan × artifacts-dir, codex-marker × delivery ordering, or cached-ready-state × correctness call sites.
- **2 non-blocking gaps found**, both pre-existing patterns extended/left untouched by this wave, not new regressions:
  1. `KNOWN_ROLES` in `pane_tools_policy.py` missing the 3 new roles (CLI-management gap only).
  2. "Read tool" claude-only wording still present in all 13 static role files, including `codex.md`/`gemini.md` (multi-provider directive gap, only partially fixed by `13f12b8`).
