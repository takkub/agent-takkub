# Core V2 — Phase 5: Capability Hub (epic #309)

> worktree `wt/backend-3-1787126395`, base `feat/v2-core` (Phase 1+2 committed) · 2026-08-19

## สรุป

ทำครบ 3 ส่วนตาม task: **5a** ย้าย skill store ออกจาก `.claude/`, **5b**
`CapabilityRegistry` + `PluginManager`, **5c** `PermissionEngine` (2 ชั้น) +
audit event. ทุกอย่างเป็น **WRAP** ตาม `REUSE_VS_REWRITE_MATRIX.md` §3 —
ไม่แก้ logic ของ `skill_scan.py` / `skill_policy.py` / `pane_tools_policy.py`
/ `mcp_bridge.py` / `pane_guard.py` / `plugin_installer.py` เลยสักบรรทัด
(ตรวจแล้วด้วย `git diff` — ไฟล์เหล่านี้ไม่อยู่ใน diff)

## 5a — ย้าย skill store

**ปัญหา**: `skill_scan.scan_skills(roots)` เขียน hardcode
`<root>/.claude/skills` ไว้ในตัวมันเอง (ไม่รับ path มาจากภายนอก) และ
claude's Skill tool เองก็ auto-discover เฉพาะ `.claude/skills` จาก cwd —
ทั้งสองจุดนี้แก้ logic ไม่ได้ตามโจทย์ ("skill_scan/skill_policy ไม่แก้
logic") ดังนั้นวิธีย้ายจริงคือ **ย้าย storage จริง + ทำ surface link
กลับมาที่ path เดิม** ไม่ใช่เปลี่ยน path ที่โค้ดอ่าน

**ทำ**:
- `git mv .claude/skills/<name> capabilities/skills/<name>` ทั้ง 6 skill
  ที่ shipped มากับ repo นี้ (cockpit-ui-style, debug-mantra,
  management-talk, post-mortem, provider-integration, scrutinize)
- `config.py`: `SKILLS_DIR` เปลี่ยนจาก constant ตรงๆ เป็น
  `_resolve_skills_dir()` — คืน `ASSETS_ROOT/capabilities/skills` ถ้ามี
  ไม่งั้น fallback `ASSETS_ROOT/.claude/skills` (legacy) — แก้เฉพาะ
  constant นี้ตามที่ task ระบุ
- **`core/capabilities/skill_store.py`** (ใหม่): `shipped_skills_root()`
  + `ensure_shipped_skill_surface()` — สร้าง **junction/symlink ต่อ-skill**
  (ไม่ใช่ทั้งโฟลเดอร์) ที่ `.claude/skills/<name>` ชี้กลับไปที่
  `capabilities/skills/<name>` โดยใช้ primitive เดิม
  `worktree_manager._make_link`/`_remove_link` (ตัวเดียวกับที่
  `skill_scan._link_skill_into_project` ใช้อยู่แล้วสำหรับ per-project
  skills — cross-platform: NTFS junction บน Windows, symlink บน
  macOS/Linux, พิสูจน์แล้วในโค้ด production) เป็น per-skill link ไม่ใช่
  link ทั้ง `.claude/skills/` เพื่อไม่ clobber โฟลเดอร์จริงของ user และ
  ไม่ทำให้ `git status` รก
- เรียก `ensure_shipped_skill_surface()` แบบ best-effort (try/except,
  ไม่ block) จาก **3 จุด** ที่ต้องเห็น skill ชุดใหม่:
  1. `spawn_engine._skill_roots_for_project()` — ทุก spawn ทุก provider
     (claude ผ่าน `render_skill_appendix`, codex/gemini ผ่าน AGENTS.md
     bridge — เรียกจุดเดียวกัน)
  2. `settings_window._new_role_skill_roots()` — New Role skill picker
  3. `settings_management/repositories/skills.py::_shipped_roots()` —
     Settings → Skills catalog (สถาปัตยกรรมใหม่ที่ UI ห้าม import
     skill_scan ตรง)
- `doctor.py`: เพิ่ม `check_capability_skill_store()` (category
  `capability-hub`) — รายงานตำแหน่ง storage จริง + repair surface +
  WARN ถ้า link พัง, ไม่ gate เฉพาะ installed build เหมือน
  `check_installed_integrity` (รันทั้ง dev checkout และ installed)
  ลงทะเบียนใน `run_all_checks()`
- `setup.py` (`_stage_assets`) + `pyproject.toml`
  (`[tool.setuptools.package-data]`): staging ของ installed wheel อ่าน
  จาก `capabilities/skills/` (fallback `.claude/skills/` ถ้า repo
  ยังไม่ได้ migrate) แล้ว stage ไปที่ `_assets/capabilities/skills/`
  ให้ตรงกับ `config.SKILLS_DIR` ตัวใหม่
- `.gitignore`: เพิ่ม `.claude/skills/*/` — surface เป็น runtime-generated
  ห้าม commit กลับเข้า git โดยไม่ตั้งใจ

**ยืนยันด้วยมือ** (ไม่ใช่แค่ unit test): รัน
`ensure_shipped_skill_surface()` จริงใน worktree นี้ → สร้าง link ทั้ง 6
สำเร็จ → `skill_scan.scan_skills([REPO_ROOT])` เจอ skill ครบ 6 ตัวผ่าน
surface path เดิม → Skill tool ของ pane นี้เอง (session ที่กำลังรันงานนี้)
ยัง list 6 skills ได้ปกติหลัง migrate (เห็นใน system-reminder ระหว่างทำงาน)

## 5b — CapabilityRegistry + PluginManager

- **`core/capabilities/registry.py`**: `CapabilityRegistry` ห่อ
  `skill_policy.effective_skills` + `skill_scan.scan_skills` →
  `resolve_skills()` (คืน `core.models.capability.Skill`),
  `pane_tools_policy.effective_mcps`/`effective_plugins` →
  `resolve_mcp_server_names()`/`resolve_plugin_names()`,
  `mcp_bridge.mcp_argv_for_provider` → `mcp_argv_for_provider()`
  (pass-through, ไม่ swallow `McpResolutionError`) และ `snapshot()` รวม
  ทั้งหมดเป็น `CapabilitySnapshot` เดียว
- **`core/capabilities/plugin_manager.py`**: `PluginManager` — backend
  `claude` เท่านั้น (`plugin_installer.install_by_id`/`uninstall_plugin`
  ตรงๆ) ทุก provider อื่น (`codex`/`gemini`/`opencode`/`kimi`/`cursor`
  ใน `NO_BACKEND_PROVIDERS`) ได้ `PluginBackendGapError` ชัดเจนแทนการ
  no-op เงียบ — ตรงกับ #103 ที่ task สั่งให้ประกาศ gap
- **`core/models/capability.py`**: เพิ่ม `CapabilityScope.SESSION`
  (มีแค่ GLOBAL/WORKSPACE/PROJECT/AGENT มาจาก Phase 1) — task ระบุ scope
  ชุด "global/project/agent/session" ชัดเจน เป็น additive-only ไม่กระทบ
  `CapabilityScope.PROJECT == "project"` ที่ test เดิม pin ไว้

## 5c — PermissionEngine (2 ชั้น) + audit

- **`core/capabilities/permission_engine.py`**: `PermissionEngine` มี
  2 method แยกกันชัดเจน **ไม่มี method รวม** —
  `mcp_allowed(role)` (layer 1: `pane_tools_policy.effective_mcps`) กับ
  `evaluate_shell_command(command, role, ...)` (layer 2:
  `pane_guard.classify` ตรงๆ) — เจตนาไม่ยุบเป็น method เดียวเพื่อให้ผู้
  เรียกที่เรียกแค่ตัวเดียวเห็นชัดว่า enforce ได้แค่ครึ่งเดียว
  (บทเรียน #242/#287 ที่ `pane_guard.py` docstring บันทึกไว้)
- `evaluate_shell_command` audit เฉพาะ **verdict ที่ถูก deny** ผ่าน
  `core.capabilities.audit.log_capability_event` (ไม่ log ทุก allowed
  call เพราะ `pane_guard.classify` รันทุก Bash call ในทุก guarded pane
  — log ทุกอันจะทำให้ EVENTS_LOG เป็น firehose โดยไม่มีประโยชน์เพิ่ม)
- **`core/capabilities/audit.py`**: `log_capability_event()` — เขียน
  JSONL เข้า **ไฟล์ `EVENTS_LOG` ไฟล์เดียวกัน** กับที่
  `orchestrator_text._log_event`/`lead_context._log_event`/
  `pipeline_executor._log_event` ใช้อยู่ (ไม่เพิ่ม event stream ที่สอง
  ตาม matrix §5) แต่ **ไม่ได้เรียกฟังก์ชันเดิมตรงๆ** — เขียน
  JSONL-append pattern เดียวกันซ้ำเอง เพราะ `core/` import
  `orchestrator_text`/`lead_context`/`pipeline_executor` ไม่ได้
  (`core-is-bottom-layer` contract จะพังทันที — ทั้งสามอยู่เหนือ
  `core/`) ทั้งสามไฟล์เดิมเองก็เป็นสำเนาซ้ำกัน 3 ชุดอยู่แล้ว ไม่ใช่
  shared helper เดียว — audit.py เป็นสำเนาที่ 4 ที่สอดคล้องกับ
  pattern เดิม ไม่ใช่การออกแบบใหม่
  รองรับ field `who`/`agent`/`provider`/`account`/`tool` ตามที่ task
  สั่ง (`event` ใหม่ชื่อ `capability.*` ไม่แก้ shape ของ event เดิม)

**ตัดสินใจเอง (ขอบเขตที่ไม่ทำ, ระบุชัด ไม่เงียบ)**: `PermissionEngine`
**ยังไม่ได้ต่อสาย** เข้า `cli.cmd_guard` (จุดที่ `takkub _guard` hook
จริงเรียก `pane_guard.classify` วันนี้) — hot path นี้ทำงานทุก Bash
call ของทุก guarded pane การเปลี่ยนจุดนี้เสี่ยง regression และต้อง
พิสูจน์ behavior-neutral ด้วย full suite ก่อน (ตาม
`docs/v2/REUSE_VS_REWRITE_MATRIX.md` กฎเหล็ก #2 "WRAP — โค้ดเดิมต้อง
ยังรันได้เหมือนเดิมเมื่อปิด feature flag") ซึ่งเกินขอบเขต targeted-test
ของ sub-task นี้ — คลาสนี้เป็น target interface พร้อมให้ phase/task
ถัดไปที่จะ rewire จริงเอาไปใช้ ไม่ใช่ behavior change วันนี้

## Multi-provider / cross-platform

- `ensure_shipped_skill_surface()` cross-platform ผ่าน `_make_link`
  เดิม (Windows junction / POSIX symlink) — ทดสอบจริงบน Windows ใน
  session นี้ (สร้าง reparse point สำเร็จ, `git status` เห็นเป็น `!!`
  ignored ตามที่ตั้งใจ)
- ทุก provider เห็น skill ชุดเดียวกัน: `_skill_roots_for_project()` เป็น
  choke point เดียวที่ทั้ง claude (`render_skill_appendix` +
  `PROVIDER_REGISTRY["claude"].context_strategy`) และ non-claude
  (`spec.context_strategy == "agents_md_file"`) เรียกอยู่แล้ว — แก้ที่
  จุดเดียวครอบคลุมทุก provider โดยไม่ต้องแตะ provider branch แยก
- `PluginManager` ประกาศ gap `codex`/`gemini`/`opencode`/`kimi`/`cursor`
  ชัดเจนแทนไม่ทำอะไรเงียบๆ (#103)

## ไฟล์ที่สร้าง/แก้

**สร้างใหม่** (6 module + 6 test file):
- `src/agent_takkub/core/capabilities/__init__.py`
- `src/agent_takkub/core/capabilities/skill_store.py`
- `src/agent_takkub/core/capabilities/registry.py`
- `src/agent_takkub/core/capabilities/plugin_manager.py`
- `src/agent_takkub/core/capabilities/permission_engine.py`
- `src/agent_takkub/core/capabilities/audit.py`
- `tests/test_core_capabilities_skill_store.py`
- `tests/test_core_capabilities_registry.py`
- `tests/test_core_capabilities_permission_engine.py`
- `tests/test_core_capabilities_plugin_manager.py`
- `tests/test_core_capabilities_audit.py`
- `tests/test_spawn_engine_skill_roots.py`

**แก้ไข**:
- `.gitignore`, `pyproject.toml`, `setup.py`
- `src/agent_takkub/config.py` (`SKILLS_DIR` → `_resolve_skills_dir()`)
- `src/agent_takkub/core/models/capability.py` (+`SESSION` scope)
- `src/agent_takkub/doctor.py` (+`check_capability_skill_store`,
  registered ใน `run_all_checks`)
- `src/agent_takkub/spawn_engine.py` (`_skill_roots_for_project` เรียก
  `ensure_shipped_skill_surface`)
- `src/agent_takkub/settings_window.py`,
  `src/agent_takkub/settings_management/repositories/skills.py`
  (skill picker roots เรียก `ensure_shipped_skill_surface` เช่นกัน)
- `tests/test_config.py` (+`TestResolveSkillsDir`),
  `tests/test_doctor.py` (+`TestCheckCapabilitySkillStore`, ลงทะเบียน
  ใน `TestRunAllChecks`)

**ย้ายไฟล์** (git mv, 6 skill): `.claude/skills/<name>/SKILL.md` →
`capabilities/skills/<name>/SKILL.md` — cockpit-ui-style, debug-mantra,
management-talk, post-mortem, provider-integration, scrutinize

## Test count

- ไฟล์ใหม่ 6 ไฟล์: **51 tests** (skill_store 6, registry 7,
  permission_engine 7, plugin_manager 13, audit 3, spawn_engine 2 +
  config TestResolveSkillsDir 3 + doctor TestCheckCapabilitySkillStore 3)
- targeted battery รวม (ไฟล์ใหม่ + ไฟล์เดิมที่แตะ): **439 passed, 0
  failed** (`test_core_capabilities_*`, `test_spawn_engine_skill_roots`,
  `test_config`, `test_core_models`, `test_doctor`, `test_skill_scan`,
  `test_skill_policy`, `test_settings_window`,
  `test_settings_management_skills`)
- ไม่ได้รัน full suite ตามนโยบาย targeted-tests-only (qa batch gate
  จะรันเต็มก่อน merge)

## Lint-imports

`lint-imports` (28 contracts): **28 kept, 0 broken** — ยืนยันว่า
`core/capabilities/*` ไม่ import PyQt6/orchestrator/main_window/app/
cli/cli_server/agent_pane/terminal_widget (ตรง `core-is-bottom-layer`)
และไม่มี contract อื่นพังจากการแก้ `spawn_engine.py`/`doctor.py`/
`settings_window.py`/`config.py`

## Ruff

`ruff check` บนไฟล์ที่แตะทั้งหมด (src + tests + setup.py +
pyproject.toml): **All checks passed**

## Gap / #103 (ประกาศชัด ไม่เงียบ)

1. `PluginManager` — ไม่มี install/uninstall backend สำหรับ
   codex/gemini/opencode/kimi/cursor (claude เท่านั้น) — raise
   `PluginBackendGapError` แทนการ no-op เงียบ
2. `PermissionEngine` — สร้างเป็น interface กลางแล้ว แต่ยังไม่ได้ต่อสาย
   เข้า live hot path (`cli.cmd_guard`) ตามที่อธิบายไว้ใน §5c ด้านบน —
   ทิ้งไว้เป็นงานของ phase/task ที่จะพิสูจน์ behavior-neutral ด้วย full
   suite ก่อน rewire จริง
3. `_skill_roots_for_project` ใช้ `REPO_ROOT` (ไม่ใช่ `ASSETS_ROOT`) เป็น
   fallback root ที่สอง — บน installed build สอง path นี้ต่างกัน (gap
   เดิมที่มีอยู่ก่อน task นี้ ไม่ใช่ regression ใหม่ แต่ยังไม่ได้แก้ —
   นอกขอบเขตของ "ย้าย skill store path" ตามที่ระบุ)
