# God-file navigation map — `orchestrator.py` & `main_window.py`

> **อ่านก่อน navigate 2 ไฟล์นี้.** ทั้งคู่เป็น single-class monolith (`Orchestrator` / `MainWindow`)
> — ทุก method แตะ `self` ดังนั้น "โครงสร้างจริง" คือ **cluster ของ method ที่ถือ state ก้อนเดียวกัน**
> ไม่ใช่ section ในไฟล์. แผนที่นี้บอกว่า function ไหนอยู่ cluster ไหน, cluster ไหนถือ state อะไร,
> และ **edge ที่ซ่อนอยู่** (ที่ import graph มองไม่เห็น) อยู่ตรงไหน — เพื่อไม่ให้ grep มั่วแล้วเดา.
>
> คู่กับ: `depgraph.json` (import map ระดับ module — ground truth, auto-refresh ทุก commit).

## สถานะ (refreshed 2026-07-11 — verified against source, not the old 2026-06-21 claim)

`orchestrator.py` **4,045 LOC** · `main_window.py` **1,270 LOC** (68 / ~35 top-level methods
respectively — grep-verified, not estimated).

Round-1 extraction (2026-06-21) shrank both files once, but **both have grown again since** from
real feature work (session goals #50, fanout queue, evidence auto-scan #5, exec-mode, task_dock,
tutorial overlay, npm self-update, remote/pipeline chips, …) — this is expected, not drift to "fix
back down"; the map below reflects **current** reality, not the post-extraction snapshot.

- **Engine mixins that genuinely exist as separate modules today:** `pipeline_executor.py` ·
  `orchestrator_text.py` · `lead_inbox.py` · `spawn_engine.py` (+ `PaneRegistry` dataclass in
  `spawn_engine.py` holding 7 state dicts). **`broadcast_actions.py` was planned but never
  shipped** — verified 2026-07-11 (codex full-system review): none of its four named methods
  (`broadcast_bug_check`, `_build_bug_check_prompt`, `_build_lead_bug_check_prompt`,
  `broadcast_design_review`) exist anywhere in `src/`. The UI Review / Bug Check buttons that would
  have called them are also gone from `user_actions.py` — the feature was cut, not hidden
  elsewhere. Do not treat this doc's old bullet as ground truth; it was aspirational.
- **Three planned "🟡 กลาง" clusters were never extracted and still live inside `orchestrator.py`
  itself:** `command_surface`, `session_persistence`, `watchdogs` (see their sections below — every
  method listed is grep-verified present in `orchestrator.py`, not in a separate file). This — not
  regrowth of the four modules above — is the main reason `orchestrator.py` is still >4k LOC.
- **UI mixins on `MainWindow`:** `update_panel.py` · `project_wizard.py` · `user_actions.py` ·
  `limit_panel.py` · `status_header.py` — all five are real, but their method lists below have
  drifted (buttons removed, chips added since 2026-06-21); refreshed per-file below.
- **guardrail:** import-linter **18 contracts** (`pyproject.toml`, not 13 — recount 2026-07-11) ·
  `_SAFE_PLUGINS` lives in `config` (avoids the hidden edge `cli→doctor→orchestrator`).
- **`inject_slash_command_when_ready`** (the generic slash-command injector this doc used to flag
  as "kept but callerless") was **removed 2026-07-11** (codex LOW finding cleanup) — see
  `docs/reviews/2026-07-11-codex-low-findings-cleanup.md`. It no longer appears anywhere in this
  map's method lists.

cluster sections ด้านล่าง = แผนที่ method→cluster ปัจจุบัน — ใช้นำทางต่อได้ (แต่ละ method list
grep-verified ตอน refresh นี้ ไม่ใช่ inherited จากรอบก่อน).

---

## ⚠️ Hidden edges — จุดที่ import graph ตอบไม่ได้ (Claude หลงบ่อยสุด)

| edge | จากไหน → ไปไหน | ทำไมหลง |
|---|---|---|
| **IPC string-dispatch** | `cli.py` สร้าง JSON payload → ส่งผ่าน **TCP socket** → `cli_server.py` string-dispatch `req['cmd']` → method บน `Orchestrator` | `takkub assign` **ไม่ได้** call `orchestrator.assign()` ตรงๆ — ไม่มี import edge เลย. "assign รันที่ไหน" ตอบจาก import ไม่ได้ ต้องไล่ผ่าน socket. ดู `cli_server.py` ตาราง `cmd → method` |
| **Re-export façade** | `orchestrator.py` re-export symbols จาก `lead_context` / `pane_env` / `vault_mirror` (มี comment "re-exported for test/doctor imports") | นิยามจริงอยู่คนละไฟล์กับที่ import. `doctor.py` import จาก `orchestrator` แต่ของจริงอยู่ `lead_context`. อย่าเชื่อว่า orchestrator เป็นเจ้าของ symbol — เช็คว่ามันแค่ re-export |
| **Late / lazy import** | จำนวนมาก `from .X import` อยู่ **ในตัว def** (กัน cycle + lazy-load Qt) | top-of-file import นับ fan-out **ต่ำกว่าจริง** เสมอ — ต้อง grep ทั้งไฟล์ ไม่ใช่แค่หัวไฟล์ |
| **String-keyed role tables** | `'critic'/'designer'/'gemini'/'codex'` ซ้ำในหลายตาราง (`roles`, `provider_config`, `routing_planner`, `shared_dev_tools`, + literal ใน `orchestrator`) ไม่มี shared enum | แก้ role 1 ตัวต้องไล่ทุกตาราง — grep string เดียวยังพลาด |
| **Prompt ↔ code drift** | `CLAUDE.md` routing table ↔ `routing_planner.classify()` regex | code = authoritative (มี comment บอก). prose ไม่ใช่ edge — เชื่อ `routing_planner.py` |
| **Vendor-string coupling** | `pty_session.py` ready/busy detect = substring-match footer ของ external CLI (agy/codex/claude) | พฤติกรรมผูกกับ string ของ vendor — เปลี่ยน CLI version อาจพัง ready-detect |
| **Bash guard = hook round-trip** | `hook_wiring.py` เขียน string `"takkub _guard"` ลง `runtime/hook-settings.json` → claude spawn ด้วย `--settings` → **claude เรียก CLI กลับมาเป็น subprocess** ทุก Bash call → `cli.cmd_guard` → `pane_guard.classify()` | ไม่มี import edge จาก `hook_wiring` ไป `pane_guard` เลย — เชื่อมด้วย **string ในไฟล์ settings + PATH** เท่านั้น. "ใครบล็อก `npx playwright`" ตอบจาก import graph ไม่ได้ · ผลข้างเคียง: `pane_guard` ต้องเป็น pure leaf (import 156ms ทั้ง `cli`) เพราะยิงทุก Bash call |
| **Tool policy 2 ชั้น คนละกลไก** | MCP → `pane_tools_policy.py` + `--strict-mcp-config` (spawn argv) · Bash → `pane_guard.py` (PreToolUse hook) | ปิด MCP **ไม่ได้ปิด shell** — pane ยัง `npx playwright` ได้ (bug 2026-07-23). ต้องแก้ทั้งคู่เสมอเมื่อจำกัดเครื่องมือ role |
| **`BROWSER_ROLES` ↔ role-file prose** | `pane_guard.BROWSER_ROLES` (hard block, **claude เท่านั้น**) ↔ `.claude/agents/*.md` (prose, provider อื่นเห็นแค่นี้ — #103) | prose คือ enforcement เดียวของ codex/gemini/opencode/kimi/cursor. แก้ `BROWSER_ROLES` แล้วไม่แก้ไฟล์ role = pane ค้าง (ถูกบอกว่าทำได้ แต่ hook บล็อก) — `tests/test_agent_role_files_have_browser_guard.py` กันไว้ |

---

## `orchestrator.py` — 4,045 LOC · class `Orchestrator(QObject)` · fan-out 23 (highest static
fan-out in the repo per `depgraph.json`, though lazy imports mean the true number is higher for
every module in this list — see hidden-edges table)

Core engine: spawn/assign/route panes · events · handoff · provider-degrade. **7 cluster** — 4
genuinely live in their own module, 3 remain inside `orchestrator.py`.

### 🟢 Extracted (separate module today)

**`pipeline_and_fanout`** → `pipeline_executor.py`
- ถือ state: `_pipeline_runs`, `_shard_groups` + dataclass `PipelineRun`, `ShardGroup`
- outbound dep เดียว: `_notify_lead` / `_inject_to_lead`
- methods: `pipeline_precheck`, `run_pipeline`, `_fire_pipeline_hop`, `_finalize_pipeline_hop`, `_advance_pipeline`, `_pipeline_tag`, `_maybe_fire_auto_chain_handoff`, `_inject_auto_chain_handoff`, `_inject_shard_fanout_handoff`, `_check_shard_group_timeout`, `_defer`, `_split_shard`, `_log_event`

**`orchestrator_text_helpers`** → `orchestrator_text.py` (pure, no `self`; there is no separate
`transcript_scan.py` — the old doc's mention of one was aspirational/never materialized, all of
this lives in one file)
- ANSI strip, paste framing, enter-delay, transcript tail/prune, digest/hot.md render, codex rewrite, cwd resolve
- methods: `_sanitize_pane_text`, `_paste_payload`, `_enter_delay_ms`, `_rewrite_task_for_codex`, `_read_tail_bytes`, `prune_old_transcripts`, `scan_artifacts`, `_render_daily_digest`, `_render_hot_md`, `_teammate_tier`, `_lead_model_override`, `_cwd_within_project`, `_resolve_project_memory`, `_build_transcript_path`, `_exit_key`, `_log_event`

**`lead_notify_pump`** → `lead_inbox.py` (⚠️ share โดยหลาย cluster: command, pump, pipeline, watchdogs)
- ถือ: `_lead_notify_queue`, `_pending_lead_cc`, `_pending_done_notices`, `_lead_draft_state`
- `_notify_lead`, `_arm_lead_notify_pump`, `_pump_lead_notify`, `_inject_to_lead`, `inject_lead_prompt`, `_send_when_ready`, `_ready_wait_ms`, `_flush_pending_lead_cc`, `_save_pending_cc`, `_load_pending_cc`, `_save_pending_done_notices`, `_load_pending_done_notices`, `_flush_pending_done_notices`, `_reap_pending_done_notices`, `_pending_cc_path`, `_pending_done_path`, `_warn_lead_delivery_unconfirmed`, `_warn_lead_spawn_failed`, `_warn_lead_respawn_capped`, `_delayed_enter`, `_delayed_enter_verified`, Lead draft-typing guard (`_track_lead_draft_input` / `_lead_can_accept_injection` / `_lead_draft_hold_expired`)
- **กฎ:** treat `_notify_lead` + queue/persistence เป็น **module เดียว** ที่ตัวอื่น depend **ทางเดียว**

**`spawn_engine`** → `spawn_engine.py` — gravitational center (densest tangle — Claude หลงที่นี่สุด)
- `spawn()` ตัวเดียว ~800+ LOC · ถือ `_pane_state`, `_recent_exits`, `_pane_tokens`, `_spawn_queue`, `_spawn_deferred`, `_spawn_in_progress`, `_panes_by_project` + `PaneState` (via `PaneRegistry`)
- เกือบทุก cluster เรียก `spawn()` / `_ps()` / `_notify_lead`
- methods: `spawn`, `_launch_session`, `_mint_pane_token`, `set_spawn_guard`, `_is_spawn_blocked`, `_retry_deferred_spawn`, `_drain_spawn_queue`, `_final_gate_clear`, `_toctou_redefer`, `_on_codex_exit`, `_write_codex_crash_dump`, `_on_session_exit`, `_auto_respawn`, `_auto_trust`, `register_pane`, `unregister_pane`, `_ps`, `PaneState`
- **กฎ:** แตกท้ายสุด และต้องทำเป็น **state object** ไม่ใช่ mixin — mixin จะทำ dict เป็น hidden cross-mixin coupling

### 🟡 ยังไม่ extract — อยู่ใน `orchestrator.py` เอง (verified 2026-07-11, not aspirational)

**`command_surface`** — takkub verbs ที่ `cli_server` เรียก (public API)
- ถือ: session-goal apply + uncommitted-commit gate + fanout-queue (new since 2026-06-21) + done-evidence auto-scan (#5, plan item 5)
- `assign`, `_assign_dispatch`, `_assign_with_worktree`, `request_restart`, `_tag_pane_worktree`, `_finalize_worktree`, `send`, `close`, `done`, `set_session_goal`, `clear_session_goal`, `get_session_goal`, `_apply_session_goal`, `toggle_provider`, `set_plan_tier`, `set_exec_mode`, `close_all_teammates`, `harvest_info`, `task_show_info`, `_uncommitted_warning`, `_check_uncommitted_async`, `_save_decision_note`, `consume_pane_hook`, `consume_session_report`, `_condense_done_note`, `_build_verify_fail_handoff`
- evidence sub-cluster (#5): `_evidence_stat_mtime`, `_find_evidence_files`, `_scan_done_evidence`
- fanout-queue sub-cluster (not in the original plan doc — added later): `_should_queue_assign`, `_enqueue_assign`, `_drain_fanout_queue`, `_fanout_queue_path`, `_save_fanout_queue`, `_load_fanout_queue`

**`session_persistence`** — durability ข้าม restart + reporting
- ถือ: `_recent_done` + snapshot files
- `snapshot_state`, `write_session_snapshot`, `restore_teammates`, `write_resume_briefs`, `write_daily_digest`, `_write_hot_md`, `_build_post_compact_brief`, `end_session`, `list_status`, `list_status_detailed`, `pane_status_report`, `_compute_last_progress_ts`

**`watchdogs`** — QTimer health monitors
- ถือ: `_idle_state`, `_idle_err_last` + stuck/rate-limit bookkeeping
- `_check_idle_teammates`, `_check_stale_markers`, `_check_shell_open_dialog`, `_check_stuck_panes`, `_auto_recover_stuck`, `_give_up_stuck`, `_inject_idle_reminder`, `_warn_lead_runaway_pane`, `_warn_lead_over_cap`, `_maybe_submit_stuck_paste`, `_rate_limit_suppressed`, `_schedule_rate_limit_notice`, `_emit_rate_limit_reset`, `_maybe_surface_tty_block`, `_surface_tty_block_notice`, `_maybe_surface_malformed_xml`

**pane-input bridge** (small, not previously mapped — Qt slot wiring only)
- `_on_pane_spawn_clicked`, `_on_pane_close_clicked`, `_on_pane_input` (feeds `LeadInboxMixin._track_lead_draft_input` — see `lead_notify_pump` above)

---

## `main_window.py` — 1,270 LOC · class `MainWindow(QMainWindow)` · fan-out 19 (per
`depgraph.json`; `spawn_engine.py` is now tied with it at 19 — `orchestrator.py`, not
`main_window.py`, has the repo's highest **static** fan-out at 23. Lazy in-method imports still
undercount every module here, same caveat as always.)

UI god-object เดินสายทุก subsystem. ส่วนใหญ่เป็น lazy import ในตัว method.

### 🟢 Extracted (self-contained dialog subsystem) — method lists refreshed 2026-07-11

**`mw_self_update`** → `update_panel.py` — cockpit + Claude-CLI + **npm** self-update UX (own QThread)
- `_on_restart_cockpit_clicked`, `_schedule_update_check`, `_on_update_check_done`, `_schedule_npm_update_check`, `_on_npm_update_check_done`, `_notify_update_available`, `_pulse_update_button`, `_run_update_check`, `_on_claude_update_check_done`, `_show_claude_update_dialog`, `_count_live_claude_panes`, `_confirm_and_apply_claude_update`, `_start_npm_update_check`, `_start_npm_update_install`, `_refresh_update_button`, `_on_update_clicked`, `_restart_with_pip_sync`, `_restart_cockpit`, `_on_install_rtk_clicked`
- Drift from the 2026-06-21 doc: `_refresh_version_label`, `_copy_version_to_clipboard`,
  `_show_changelog`, `_on_claude_update_clicked` no longer exist (verified — not renamed
  elsewhere, just gone); the npm-update sub-flow (`_schedule_npm_update_check` through
  `_start_npm_update_install`) and `_restart_with_pip_sync` are new since then.

**`mw_project_creation_wizard`** → `project_wizard.py` — new/import project + AI rules gen (`_RulesGeneratorThread`)
- `_RulesGeneratorThread`, `_on_add_project_clicked`, `_import_existing_project`, `_new_project_with_rules`, `_ask_project_description`, `_generate_rules_with_ui`, `_run_map_paths_dialog`, `_save_and_open_project`, `_on_edit_project_rules_clicked`, `_show_rules_editor_dialog`, `_on_edit_project_clicked`
- No drift — matches the original doc exactly.

**`mw_status_bar_builder`** → `StatusHeader` QWidget (`status_header.py`)
- `_build_status_bar` (was `__init__` in the old doc — renamed), `_make_status_separator`, `_provider_chip_style`, `_provider_chip_state`, `_provider_chip_tooltip`, `_plan_chip_style`, `_plan_chip_tooltip`, `_ghost_button_style`, `_danger_button_style`, `_update_status`, `_refresh_rtk_button`
- New chips added since 2026-06-21 (not in the old doc): `_exec_mode_chip_style/_tooltip`, `_auto_resume_chip_style/_tooltip`, `_remote_chip_style/_tooltip`, `_refresh_remote_chip`.

**`mw_limit_status`** — usage/limit telemetry (`limit_panel.py`)
- `_init_limit_store`, `_on_usage_updated`, `_refresh_limit_label`
- No drift.

### 🟡 กลาง

**`mw_user_actions`** → `user_actions.py` — toolbar/button handlers
- `_show_pipelines_menu`, `_on_end_session_clicked`, `_show_end_session_summary`, `_on_team_chip_clicked`, `_open_settings_window`, `_on_open_shell_clicked`, `_on_doctor_clicked`, `_on_provider_chip_clicked`, `_on_provider_state_changed`, `_on_plan_chip_clicked`, `_on_plan_tier_changed`, `_on_exec_mode_chip_clicked`, `_on_exec_mode_changed`, `_on_auto_resume_chip_clicked`, `_on_auto_resume_changed`, `_on_remote_chip_clicked`, `_apply_remote_config`, `_on_user_changed`, `_on_add_user_clicked`
- Drift: `_on_resume_clicked` removed intentionally (commit `28136df`, 2026-07-10 — see the
  `/remote-control` note below). `_on_ui_review_clicked` and `_on_bug_check_clicked` no longer
  exist anywhere in `src/` (their `broadcast_actions` targets were also never shipped — see the
  status section above). `_open_pipeline_settings_dialog` → only `_show_pipelines_menu` remains.
  New: the exec-mode/auto-resume/remote chip handlers, `_on_team_chip_clicked` +
  `_open_settings_window` (Users tab, 2026-07-11).

### 🔴 อันตราย — เก็บไว้ด้วยกัน

**`mw_tab_project_lifecycle`** + **`mw_orchestrator_signal_bridge`** — multi-tab orchestration + Orchestrator signal handlers (both stay in `main_window.py` itself)
- ⚠️ form **cycle** กับ `Orchestrator.register_pane` + signal `paneRequested`/`paneClosed`/`agentDone` (bidirectional bridge)
- เก็บ `_ensure_teammate_pane`, `_remove_teammate_pane`, `_on_cross_tab_done`, `_track_pane_request` **ไว้กับ** tab-lifecycle (mutate ProjectTab map เดียวกัน)
- tab: `_current_tab`, `lead_pane`, `teammate_panes`, `_wire_project_tab`, `_tab_for_project`, `_open_project_tab`, `_on_new_tab_clicked`, `_on_tab_close_requested`, `_close_project_tab` (headless-safe extraction, W1 plan item — shared by desktop UI and `src/agent_takkub/remote/api.py`), `_on_tab_switched`, `_on_tab_context_menu`, `_persist_open_tabs`, `_open_projects`, `_refresh_project_list`, `_on_project_changed`, `_restart_lead_for_active_project`, `_respawn_lead_post_restart`
  - Drift: `teammate_split`/`main_split` are no longer `MainWindow` attributes (split-widget
    ownership moved into `project_tab.py`'s `ProjectTab`, not re-exposed here); `_on_tab_bar_clicked`
    and `_plus_tab_index` no longer exist; `_on_tab_bar_context_menu` → renamed `_on_tab_context_menu`.
  - New since 2026-06-21: tutorial overlay (`_build_tutorial_steps`, `_start_tutorial`,
    `_maybe_autostart_tutorial`), task dock (`_configure_tasks_dock_chrome`, `_on_toggle_tasks`,
    `_on_tasks_dock_collapse_toggled`), `_on_lead_notified`.
- bridge: `_boot`, `_spawn_lead_when_quiet`, `_restore_teammates_from_snapshot`, `_notify_agent_done`, `keyPressEvent`, `_install_shortcuts`, `_restore_window_state`, `_save_window_state`, `closeEvent`, `_on_toggle_logs`, `_show_help`
  - Drift: `_tick_heartbeat` still exists but its old cluster note calling it out separately was
    folded in here — no functional change, just a doc simplification.
- `/remote-control` auto-bridge (#4) — **REMOVED 2026-07-10** (ตีกับ `/resume` picker ที่ paint พร้อมกันแล้ว Enter cancel — 2-วัน saga). ผู้ใช้พิมพ์ `/remote-control` + `/resume` เองแทน. `_maybe_fire_remote_bridge`/`_reap_remote_bridge`/`is_at_resume_picker` + dedupe sets ถูกลบหมด. The generic `inject_slash_command_when_ready` infra in `lead_inbox.py` that this note used to say was "kept but callerless" was **also removed 2026-07-11** (codex LOW finding: no near-term plan reuses it — W2b's AskUserQuestion TUI-drive needs arrow-key picker navigation, a different mechanism, not slash-command typing) — it is gone from the codebase entirely now, along with its dedicated test file and the `_slash_inject_busy`/`_slash_inject_queue` state.

---

## ลำดับ refactor + guardrail

**ลำดับ (low-risk → high-risk) — items 1-3 done 2026-06-21, item 4 not started:**
1. ✅ orchestrator: `pipeline_and_fanout` → `pipeline_executor.py` · pure helpers → `orchestrator_text.py` · (planned `broadcast_actions.py` never shipped — see status section)
2. ✅ main_window: `update_panel.py` · `project_wizard.py` · `StatusHeader` widget
3. ✅ orchestrator: consolidate Lead-inbox → `lead_inbox.py` (ทางเดียว)
4. **ไม่เริ่ม:** lift `spawn_engine` → dedicated `PaneRegistry`/`SpawnArbiter` state object (currently a mixin over `Orchestrator`, which the original plan flagged as the wrong shape long-term)
5. **ไม่เคย plan ไว้แต่เข้าเงื่อนไขเดียวกัน:** `command_surface` / `session_persistence` / `watchdogs` remain unextracted inside `orchestrator.py` — this doc's 2026-06-21 "แตกครบแล้ว" framing never actually covered these three; they were identified but not scheduled

**กันพันใหม่:** ทุก extract → เพิ่ม import-linter contract (ตอนนี้ 18 contracts, `pyproject.toml`)
→ PR ที่ลาก edge ข้าม layer **fail CI** + regenerate `depgraph.json` ใน pre-commit → graph ไม่ stale +
Serena เห็น symbol ใหม่ทันที → orchestrator ไม่งอกกลับเป็น 5.8k LOC โดยไม่มีใครสังเกต.

**Refresh discipline:** this doc drifted for ~3 weeks (2026-06-21 → 2026-07-11) mostly from real
feature work landing directly in `orchestrator.py`/`main_window.py` instead of new modules — not
from anyone editing the doc incorrectly. There's no automation catching this (unlike
`depgraph.json`, which regenerates every commit). Re-verify method lists here (grep each name)
whenever this file is the thing being navigated by, rather than trusting it blindly for LOC/method
claims older than a few weeks.
