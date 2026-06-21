# God-file navigation map — `orchestrator.py` & `main_window.py`

> **อ่านก่อน navigate 2 ไฟล์นี้.** ทั้งคู่เป็น single-class monolith (`Orchestrator` / `MainWindow`)
> — ทุก method แตะ `self` ดังนั้น "โครงสร้างจริง" คือ **cluster ของ method ที่ถือ state ก้อนเดียวกัน**
> ไม่ใช่ section ในไฟล์. แผนที่นี้บอกว่า function ไหนอยู่ cluster ไหน, cluster ไหนถือ state อะไร,
> และ **edge ที่ซ่อนอยู่** (ที่ import graph มองไม่เห็น) อยู่ตรงไหน — เพื่อไม่ให้ grep มั่วแล้วเดา.
>
> คู่กับ: `depgraph.json` (import map ระดับ module) · Serena MCP (`find_symbol` / `find_referencing_symbols`
> ดึง caller จริงระดับ symbol — ใช้แทนการอ่านทั้งไฟล์ 5,828 บรรทัด).

---

## ⚠️ Hidden edges — จุดที่ import graph ตอบไม่ได้ (Claude หลงบ่อยสุด)

| edge | จากไหน → ไปไหน | ทำไมหลง |
|---|---|---|
| **IPC string-dispatch** | `cli.py` สร้าง JSON payload → ส่งผ่าน **TCP socket** → `cli_server.py` string-dispatch `req['cmd']` → method บน `Orchestrator` | `takkub assign` **ไม่ได้** call `orchestrator.assign()` ตรงๆ — ไม่มี import edge เลย. "assign รันที่ไหน" ตอบจาก import ไม่ได้ ต้องไล่ผ่าน socket. ดู `cli_server.py` ตาราง `cmd → method` |
| **Re-export façade** | `orchestrator.py` re-export ~30 symbol จาก `lead_context` / `pane_env` / `vault_mirror` (มี comment "re-exported for test/doctor imports") | นิยามจริงอยู่คนละไฟล์กับที่ import. `doctor.py` import จาก `orchestrator` แต่ของจริงอยู่ `lead_context`. อย่าเชื่อว่า orchestrator เป็นเจ้าของ symbol — เช็คว่ามันแค่ re-export |
| **Late / lazy import** | ~60+ `from .X import` อยู่ **ในตัว def** (กัน cycle + lazy-load Qt) | top-of-file import นับ fan-out **ต่ำกว่าจริง**. `main_window` top-of-file ~13 แต่จริง 24+. ต้อง grep ทั้งไฟล์ ไม่ใช่แค่หัวไฟล์ |
| **String-keyed role tables** | `'critic'/'designer'/'gemini'/'codex'` ซ้ำใน ≥5 ตาราง (`roles`, `provider_config`, `routing_planner`, `shared_dev_tools`, + 37 literal ใน `orchestrator`) ไม่มี shared enum | แก้ role 1 ตัวต้องไล่ทุกตาราง — grep string เดียวยังพลาด |
| **Prompt ↔ code drift** | `CLAUDE.md` routing table ↔ `routing_planner.classify()` regex | code = authoritative (มี comment บอก). prose ไม่ใช่ edge — เชื่อ `routing_planner.py` |
| **Vendor-string coupling** | `pty_session.py` ready/busy detect = substring-match footer ของ external CLI (agy/codex/claude) | พฤติกรรมผูกกับ string ของ vendor — เปลี่ยน CLI version อาจพัง ready-detect |

---

## `orchestrator.py` — 5,828 LOC · class `Orchestrator(QObject)` · fan-out 20

Core engine: spawn/assign/route panes · events · handoff · provider-degrade. **9 cluster** เรียงจาก
แตกง่าย→ยาก. แต่ละ cluster = method ที่ถือ state ก้อนเดียวกัน.

### 🟢 แตกง่าย (low-risk, ทำก่อน)

**`pipeline_and_fanout`** → ปลายทาง `pipeline_executor.py` *(เป้าหมาย refactor รอบ 1)*
- ถือ state: `_pipeline_runs`, `_shard_groups` + dataclass `PipelineRun`, `ShardGroup`
- outbound dep เดียว: `_notify_lead` / `_inject_to_lead`
- methods: `pipeline_precheck`, `run_pipeline`, `_fire_pipeline_hop`, `_finalize_pipeline_hop`, `_advance_pipeline`, `_pipeline_tag`, `_maybe_fire_auto_chain_handoff`, `_inject_auto_chain_handoff`, `_inject_shard_fanout_handoff`, `_check_shard_group_timeout`, `_defer`

**`orchestrator_text_helpers`** → `orchestrator_text.py` + `transcript_scan.py` (pure, no `self`)
- ANSI strip, paste framing, enter-delay, transcript tail/prune, digest/hot.md render, codex rewrite, cwd resolve
- methods: `_sanitize_pane_text`, `_paste_payload`, `_enter_delay_ms`, `_rewrite_task_for_codex`, `_delayed_enter`, `_delayed_enter_verified`, `_read_tail_bytes`, `prune_old_transcripts`, `scan_artifacts`, `_render_daily_digest`, `_render_hot_md`, `_teammate_tier`, `_lead_model_override`, `_cwd_within_project`, `_resolve_project_memory`, `_build_transcript_path`, `_exit_key`, `_split_shard`, `_log_event`

**`broadcast_actions`** → `broadcast_actions.py` (เล็ก cohesive)
- `broadcast_bug_check`, `_build_bug_check_prompt`, `_build_lead_bug_check_prompt`, `broadcast_design_review`

### 🟡 กลาง

**`command_surface`** — takkub verbs ที่ `cli_server` เรียก (public API)
- ถือ: session-goal apply + uncommitted-commit gate
- `assign`, `send`, `close`, `done`, `set_session_goal`, `clear_session_goal`, `get_session_goal`, `_apply_session_goal`, `toggle_provider`, `set_plan_tier`, `close_all_teammates`, `harvest_info`, `_uncommitted_warning`, `_check_uncommitted_async`, `_save_decision_note`

**`session_persistence`** — durability ข้าม restart + reporting
- ถือ: `_recent_done` + snapshot files
- `snapshot_state`, `write_session_snapshot`, `restore_teammates`, `write_resume_briefs`, `write_daily_digest`, `_write_hot_md`, `_build_post_compact_brief`, `end_session`, `list_status`, `list_status_detailed`, `pane_status_report`, `_compute_last_progress_ts`

**`watchdogs`** — QTimer health monitors
- ถือ: `_idle_state`, `_idle_err_last` + stuck/rate-limit bookkeeping
- `_check_idle_teammates`, `_check_stuck_panes`, `_auto_recover_stuck`, `_give_up_stuck`, `_inject_idle_reminder`, `_warn_lead_runaway_pane`, `_rate_limit_suppressed`, `_schedule_rate_limit_notice`, `_emit_rate_limit_reset`, `_maybe_surface_tty_block`, `_surface_tty_block_notice`, `_maybe_surface_malformed_xml`

### 🔴 อันตราย — อย่าแตกมั่ว (ทำท้ายสุด)

**`lead_notify_pump`** — Lead-inbox queue (⚠️ share โดย 4 cluster: command, pump, pipeline, watchdogs)
- ถือ: `_lead_notify_queue`, `_pending_lead_cc`, `_pending_done_notices`
- `_notify_lead`, `_arm_lead_notify_pump`, `_pump_lead_notify`, `_inject_to_lead`, `inject_lead_prompt`, `inject_slash_command_when_ready`, `_send_when_ready`, `_ready_wait_ms`, `_flush_pending_lead_cc`, `_save_pending_cc`, `_load_pending_cc`, `_save_pending_done_notices`, `_load_pending_done_notices`, `_flush_pending_done_notices`, `_reap_pending_done_notices`, `_pending_cc_path`, `_pending_done_path`, `_warn_lead_delivery_unconfirmed`, `_warn_lead_spawn_failed`, `_warn_lead_respawn_capped`
- **กฎ:** treat `_notify_lead` + queue/persistence เป็น **module เดียว** (`lead_inbox.py`) ที่ตัวอื่น depend **ทางเดียว** — แตก queue ownership กระจาย = คืน divergence bug ที่ PaneState consolidation เพิ่งแก้ (ดู comment บรรทัด ~1164–1226)

**`spawn_engine`** — gravitational center ของไฟล์ (densest tangle — Claude หลงที่นี่สุด)
- `spawn()` ตัวเดียว ~840 LOC · ถือ `_pane_state`, `_recent_exits`, `_pane_tokens`, `_spawn_queue`, `_spawn_deferred`, `_spawn_in_progress`, `_panes_by_project` + `PaneState`
- เกือบทุก cluster เรียก `spawn()` / `_ps()` / `_notify_lead`
- methods: `spawn`, `_launch_session`, `_mint_pane_token`, `set_spawn_guard`, `_is_spawn_blocked`, `_retry_deferred_spawn`, `_drain_spawn_queue`, `_final_gate_clear`, `_toctou_redefer`, `_on_codex_exit`, `_write_codex_crash_dump`, `_on_session_exit`, `_auto_respawn`, `_auto_trust`, `register_pane`, `unregister_pane`, `_ps`, `PaneState`
- **กฎ:** แตกท้ายสุด และต้องทำเป็น **state object** (`PaneRegistry`/`SpawnArbiter` ถือ dict พวกนี้) **ไม่ใช่ mixin** — mixin จะทำ dict เป็น hidden cross-mixin coupling

---

## `main_window.py` — 3,997 LOC · class `MainWindow(QMainWindow)` · fan-out 24 (สูงสุดในเรปอ)

UI god-object เดินสายทุก subsystem. ส่วนใหญ่เป็น lazy import ในตัว method.

### 🟢 แตกง่าย (self-contained dialog subsystem)

**`mw_self_update`** → `update_panel.py` — cockpit + Claude-CLI self-update UX (own QThread)
- `_schedule_update_check`, `_on_update_check_done`, `_notify_update_available`, `_pulse_update_button`, `_run_update_check`, `_refresh_version_label`, `_copy_version_to_clipboard`, `_show_changelog`, `_on_claude_update_clicked`, `_on_claude_update_check_done`, `_show_claude_update_dialog`, `_count_live_claude_panes`, `_confirm_and_apply_claude_update`, `_refresh_update_button`, `_on_update_clicked`, `_restart_cockpit`, `_on_restart_cockpit_clicked`, `_on_install_rtk_clicked`

**`mw_project_creation_wizard`** → `project_wizard.py` — new/import project + AI rules gen (`_RulesGeneratorThread` ย้ายไปด้วย)
- `_RulesGeneratorThread`, `_on_add_project_clicked`, `_import_existing_project`, `_new_project_with_rules`, `_ask_project_description`, `_generate_rules_with_ui`, `_run_map_paths_dialog`, `_save_and_open_project`, `_on_edit_project_rules_clicked`, `_show_rules_editor_dialog`, `_on_edit_project_clicked`

**`mw_status_bar_builder`** → `StatusHeader` QWidget — ดึง ~580-line `__init__` UI block + static style
- `__init__` (เหลือแค่ wiring), `_make_status_separator`, `_provider_chip_style`, `_provider_chip_state`, `_provider_chip_tooltip`, `_plan_chip_style`, `_plan_chip_tooltip`, `_ghost_button_style`, `_danger_button_style`, `_update_status`, `_refresh_rtk_button`

**`mw_limit_status`** — usage/limit telemetry (เล็ก isolated → `limit_status.py` wiring)
- `_init_limit_store`, `_on_usage_updated`, `_refresh_limit_label`

### 🟡 กลาง

**`mw_user_actions`** — toolbar/button handlers (แต่ละตัวเปิด dialog หรือ fire orchestrator)
- `_on_resume_clicked`, `_on_end_session_clicked`, `_show_end_session_summary`, `_on_ui_review_clicked`, `_on_open_shell_clicked`, `_on_bug_check_clicked`, `_on_doctor_clicked`, `_on_provider_chip_clicked`, `_on_provider_state_changed`, `_on_plan_chip_clicked`, `_on_plan_tier_changed`, `_on_user_changed`, `_on_add_user_clicked`, `_open_pipeline_settings_dialog`, `_show_pipelines_menu`

### 🔴 อันตราย — เก็บไว้ด้วยกัน

**`mw_tab_project_lifecycle`** + **`mw_orchestrator_signal_bridge`** — multi-tab orchestration + Orchestrator signal handlers
- ⚠️ form **cycle** กับ `Orchestrator.register_pane` + signal `paneRequested`/`paneClosed`/`agentDone` (bidirectional bridge)
- เก็บ `_ensure_teammate_pane`, `_remove_teammate_pane`, `_on_cross_tab_done`, `_on_pane_resumed`, `_track_pane_request` **ไว้กับ** tab-lifecycle (mutate ProjectTab map เดียวกัน)
- tab: `_current_tab`, `lead_pane`, `teammate_panes`, `teammate_split`, `main_split`, `_tab_for_project`, `_open_project_tab`, `_on_new_tab_clicked`, `_on_tab_close_requested`, `_on_tab_switched`, `_on_tab_bar_clicked`, `_plus_tab_index`, `_persist_open_tabs`, `_open_projects`, `_refresh_project_list`, `_on_project_changed`, `_restart_lead_for_active_project`, `_respawn_lead_post_restart`, `_on_tab_bar_context_menu`
- bridge: `_boot`, `_spawn_lead_when_quiet`, `_restore_teammates_from_snapshot`, `_notify_agent_done`, `_on_lead_input`, `keyPressEvent`, `_install_shortcuts`, `_tick_heartbeat`, `_restore_window_state`, `_save_window_state`, `closeEvent`, `_on_toggle_logs`, `_show_help`

---

## ลำดับ refactor + guardrail

**ลำดับ (low-risk → high-risk):**
1. orchestrator: `pipeline_and_fanout` → `pipeline_executor.py` · pure helpers → `orchestrator_text.py` + `transcript_scan.py` · `broadcast_actions.py`
2. main_window: `update_panel.py` · `project_wizard.py` · `StatusHeader` widget
3. orchestrator: consolidate Lead-inbox → `lead_inbox.py` (ทางเดียว)
4. **ท้ายสุด ระวังสุด:** lift `spawn_engine` → `PaneRegistry` state object

หลัง (1)-(3) แต่ละ god-file ลด ~1,500–2,000 LOC เหลือ core ที่ coupled จริง = spawn+command+notify triad
(นั่นคือ high-fan-in hub ที่ graph ควรเน้น)

**กันพันใหม่:** ทุก extract → เพิ่ม import-linter contract (เช่น `pipeline_executor` ห้าม import `main_window`)
→ PR ที่ลาก edge ข้าม layer **fail CI** + regenerate `depgraph.json` ใน pre-commit → graph ไม่ stale +
Serena เห็น symbol ใหม่ทันที → orchestrator ไม่งอกกลับเป็น 5.8k LOC.
