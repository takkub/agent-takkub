---
date: 2026-07-11
project: agent-takkub
role: maintainer
topic: Skill/Role concept separation + design-system color/font migration
---

# Skill vs Role separation + cockpit UI color/font migration

สรุปงาน 2 ก้อนใหญ่: (1) แยก concept **Skill** (ความรู้ที่ reuse ได้) ออกจาก
**Role** (ตำแหน่งทีม) ใน Settings UI ให้ผู้ใช้ดูออกทันที และ (2) migrate สี/font
ของ UI ทั้ง cockpit ให้ตรง design system เดียว (`cockpit_theme.py`) — จบ complaint
"ไม่เป็นไปในทางเดียวกัน". รวมทั้งแก้บั๊ก word-wrap ของ Task List dock ที่ Lead แจ้ง.

## 1. แยก Skill กับ Role ใน Settings (`settings_window.py`)

**ปัญหาเดิม:** เมนู "Skill Catalog" หลอกตา — จริงๆ มันเรียก
`skill_audit.load_all_role_docs()` แล้ววัด **scope overlap ของ ROLE แต่ละตัว**
(TF-IDF) ไม่เกี่ยวกับ skill file เลย. อยู่ใต้ section POLICY ปนกับ role/tools.

**สิ่งที่ทำ:**

- เปลี่ยนชื่อเมนูเดิม → **"Role Overlap"** (พูดตรงตามที่มันทำ) ย้ายไป section
  **ROLE** คู่กับ "Providers & Roles". internal widget เปลี่ยนชื่อ
  `_skill_* → _overlap_*` เพื่อไม่ให้คำว่า "skill" หลงอยู่ในหน้า role-audit.
- สร้าง **"Skill Catalog" ใหม่จริง** ใน section **SKILL** (แยกขาดจาก role/tools)
  — ใช้ `skill_scan.scan_skills()` (ตัวเดียวกับ New Role picker) list skill จริง
  จาก `.claude/skills/*/SKILL.md` โชว์ name + description + **role ไหนอ้างถึง
  skill นั้น** (substring match บน role instruction docs).
- **จัด section sidebar ใหม่** ให้ concept ไม่ปนกัน:
  `PIPELINE` · `ROLE` (Providers & Roles, Role Overlap) · `TOOLS` (MCP Matrix,
  Plugins Matrix) · `SKILL` (Skill Catalog) · `ACCOUNT` (Users).
- View index: 0–7 คงเดิม (external callers/tests อ้าง constant พวกนี้);
  `VIEW_SKILL_CATALOG` (เดิม=5) → `VIEW_ROLE_OVERLAP=5`, real Skill Catalog เป็น
  index ใหม่ **8** (append ท้าย stack).

**Tests:** `TestRoleOverlapView` (เดิม TestSkillCatalogView) + `TestSkillCatalogView`
ใหม่ (list/desc/referencing-roles + empty placeholder) — เขียว.

## 2. Role-color single source of truth

`roles.py Role.color` (grid palette เก่า: lead=#f5c542, frontend cyan …) กับ
`cockpit_theme.ROLE_COLORS` (design system: lead=#E3B341, frontend teal …)
**ไม่ตรงกันทุก role** → grid กับ Settings เรนเดอร์คนละสี.

**แก้:** `cockpit_theme.ROLE_COLORS` เป็น canonical (เติม codex/gemini/shell ให้ครบ
built-in ทุกตัว) · `roles.py` mirror ค่าเดียวกันเป๊ะเป็น literal (คงความเป็น
**pure-leaf** — ไม่ import Qt เข้า CLI path) · call sites อ่าน
`ROLE_COLORS.get(name, role.color)` (custom role fallback ที่ `Role.color`).
เพิ่ม guard test `test_builtin_role_colors_mirror_cockpit_theme_role_colors` +
`test_every_builtin_role_has_a_cockpit_theme_color` กัน drift.

> ⚠️ **User-visible:** สี role บน main grid เปลี่ยน 8 ตัว (lead/frontend/backend/
> mobile/devops/qa/reviewer/critic) ให้เท่ากับ Settings. codex/gemini/shell คงเดิม.

## 3. Color/font migration (design token เดียว)

เพิ่ม semantic token ใน `cockpit_theme.py` (keep meaning, tokenize value):
`PROVIDER_CODEX/GEMINI` · `STATE_OK/WARN/ERROR/INFO` + `_BRIGHT` + `STATE_WARN_ALT`
+ `STATE_EXITED` · `METER_CLAY/_ALT` + `METER_AMBER/_LIGHT` ·
`BANNER_{WARN,OK,ERROR,INFO}_{BG,BORDER,TEXT,HOVER}` · `CHIP_PLAN_MAX/EXEC_PARALLEL/
REMOTE_ON` · `AVATAR_TINTS` (ย้ายจาก project_nav `_AVATAR_COLORS`, ค่าเดิมเป๊ะ) ·
`ROLE_COLOR_FALLBACK`.

ไฟล์ที่ migrate (grounds→`GROUND_*`, greys→`TEXT_*`, borders→`BORDER_*`,
active/selected/primary accent indigo/blue/green→**gold**, brand/state→token):

| ไฟล์ | สาระ |
|---|---|
| `project_nav.py` | indigo active accent (#6366f1…) → **gold**; selection ring → gold; avatar palette → `AVATAR_TINTS` |
| `project_tab.py` | pane-tab selected accent indigo → **gold** |
| `status_header.py` | provider chip → `PROVIDER_*`; plan/exec/remote chip → `CHIP_*`; amber → `STATE_WARN*`; ghost/danger buttons → tokens; rtk button → `METER_AMBER` |
| `task_dock.py` | dock QSS + status dots + card → tokens (**+ word-wrap fix, ดู §4**) |
| `agent_pane.py` | state dots → `STATE_*`; pane/header/button QSS → grounds/borders; title สี → `ROLE_COLORS.get` |
| `update_panel.py` | blue CTA → **gold**; banner buttons (warn/ok/error/info) → `BANNER_*` |
| `user_actions.py` | green/blue dialog buttons → gold-primary / secondary; report view → mono font + tokens |
| `main_window.py` | app chrome grounds/splitter → `GROUND_*`/`BORDER_*`; custom-role fallback → token |
| `usage_meter.py` / `limit_panel.py` | Claude coral → `METER_CLAY`; state ramp → `METER_AMBER`/`STATE_*` |
| `logs_panel.py` | event colors → state/provider tokens; view QSS → grounds |
| `tutorial_overlay.py` | coral → `METER_CLAY`; callout/buttons → tokens |
| `settings_window.py` | 5 stray `#94a3b8` fallback → `ROLE_COLOR_FALLBACK` |

**ข้อยกเว้นตั้งใจ:** `token_meter.py` (usage_color 4 literals) **ไม่ migrate** —
เป็น pure-leaf ที่ engine (spawn_engine) import; การ import `cockpit_theme` (Qt)
เข้าไปจะลาก PyQt6 เข้า pure module path. ค่ามันตรงกับ state ramp อยู่แล้ว.
`issues.py` (GitHub label hex) ปล่อยไว้ตาม audit (external data).

## 4. Task List dock word-wrap bug (Lead flagged)

**อาการ:** goal/feature/task label ยาวโชว์ `...` ไม่ยอมตัดขึ้นบรรทัด 2 แม้โค้ดตั้ง
`ElideNone` + `wordWrap(True)` + `setUniformRowHeights(False)` +
`updateGeometries()` + `scheduleDelayedItemsLayout()`.

**Root cause (พิสูจน์ด้วย headless repro):** `QTreeView` default delegate **วาด**
wrapped text ได้ แต่ **ไม่ขยายความสูง row** — row height มาจาก single-line
sizeHint. Repro: tree เดียวกัน row 110 ตัวอักษร สูง 12px **เท่ากับ** row 10 ตัว
(12px). เฉพาะ project row รอด เพราะมันเป็น **item widget** (`ProjectCardWidget`)
ที่ propagate ความสูงตัวเองเข้า `item.setSizeHint()`.

**Fix:** เพิ่ม `_WrapItemDelegate(QStyledItemDelegate)` ตั้งบน tree — `sizeHint()`
วัดความสูง wrapped text เทียบ content width จริง (viewport − indentation ตาม depth)
แล้วคืนความสูงนั้น → view จองแถวสูงพอ. row ที่ text ว่าง (project row หลัง mount
card) fall through เป็น base เดิม ไม่ยุ่ง. **พิสูจน์:** delegate ทำให้ row ยาว
โต 116px, row สั้น 18px. Regression test `TestWrapItemDelegate` (3 เคส) เขียว.

## Verification

- `ruff check` + `ruff format`: ผ่าน
- `import-linter`: **18/18 contracts KEPT** (roles ยัง pure, ไม่มี layering ใหม่พัง)
- targeted tests: `test_task_dock` · `test_settings_window` · `test_role_registry_sync`
  · `test_main_window_status_bar` · `test_cockpit_theme` · `test_update_chip_color`
  · `test_update_auto` · `test_roles` · `test_project_nav` · `test_logs_panel`
  · `test_agent_pane_*` · `test_limit_panel_teardown` · `test_custom_roles`
  · `test_routing_planner` — **ทั้งหมดเขียว**
- อัปเดต test ที่ hardcode สีเก่า: `test_main_window_status_bar.test_disabled_is_gray`
  → อ้าง `cockpit_theme.TEXT_MUTED` แทน `#71717a`

**หมายเหตุ:** ยังไม่รัน full suite (targeted-tests rule — full คือ qa batch gate).
Visual/pixel verify ของทุกหน้ายังเป็นงาน user/critic (โดยเฉพาะ role-color grid ที่
เปลี่ยน + indigo→gold accents).

---

## Follow-up: แก้ 2 minor จาก codex cross-check (2026-07-11)

codex cross-check (`docs/reviews/2026-07-11-skill-role-migration-codex-crosscheck.md`
ข้อ 1) ชี้ 2 จุดค้างก่อน merge — แก้แล้วทั้งคู่:

### 1. Stale docstring — `pane_tools_dialog.py:3-11`
docstring เดิมบอกว่า SettingsWindow "👥 Team" ประกอบด้วย
`VIEW_MCP_MATRIX / VIEW_PLUGINS_MATRIX / VIEW_SKILL_CATALOG / VIEW_NEW_ROLE`
ซึ่งเป็น concept เก่าตอนก่อนแยก Skill/Role — ทำให้ดูเหมือน Skill Catalog
consume policy module นี้ (ทั้งที่ไม่ใช่).
**แก้:** ระบุให้ตรงว่า module นี้ถูก consume โดย **per-role MCP/plugin views เท่านั้น**
(`VIEW_MCP_MATRIX / VIEW_PLUGINS_MATRIX / VIEW_NEW_ROLE`) ส่วน skill-facing views
(Role Overlap, Skill Catalog — แยกกัน 2026-07-11) อ่าน role/skill docs ไม่ใช่ policy นี้.

### 2. Reference detection false-positive — `settings_window.py:_roles_referencing_skill`
เดิมใช้ raw substring `skill_name.lower() in doc.lower()` → skill ชื่อสั้น/คำทั่วไป
(`git`, `test`, `review`) ไป match ตัวอักษรใน prose/คำอื่น เช่น `"git" in "github"` → True
= role false positive ใน Skill Catalog.
**แก้:** เปลี่ยนเป็น word-boundary regex `re.compile(rf"\b{re.escape(skill_name)}\b", re.IGNORECASE)`
— match skill name แบบทั้งคำ (ครอบทั้ง generated marker `อ่าน skill: <name>` ที่
`_append_skill_references()` สร้าง และ prose ที่เขียนมือ) แต่ไม่ชนคำอื่นที่แค่มีตัวอักษรตรงกัน.
เพิ่ม `import re` ใน settings_window.py.

**Negative test เพิ่ม:** `TestSkillCatalogView::test_short_skill_name_does_not_false_match_on_prose`
— skill ชื่อ `git`, role doc "push to github and deploy the digital dashboard"
(มี substring "git" แต่ไม่เป็นคำเดี่ยว) → **ต้องไม่** surface role นั้น; อีก role
ที่อ้าง `อ่าน skill: git` แบบทั้งคำ → surface. พิสูจน์ว่า regex แก้ false positive
ที่ substring เดิมทำพลาด.

### Verify
- `pytest tests/test_settings_window.py tests/test_role_registry_sync.py tests/test_task_dock.py tests/test_main_window_status_bar.py` → **116 passed** (เดิม 115 + negative test 1)
- `ruff check` + `ruff format --check` (3 ไฟล์ที่แตะ) → **ผ่าน**
- `lint-imports` → **18 kept, 0 broken**
