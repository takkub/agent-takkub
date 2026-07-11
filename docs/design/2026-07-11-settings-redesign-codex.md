# Settings Management Redesign — Codex second opinion

วันที่: 2026-07-11  
สถานะ: design proposal (สร้างเป็น module ใหม่ ไม่แก้ `settings_window.py` เดิม)

## Executive decision

Settings ใหม่ควรเป็น **entity-centric list–detail workspace** ไม่ใช่ชุดหน้า form และ matrix ที่แยกกันตามชนิด operation

- Sidebar มี 5 เมนูหลักเท่านั้น: **Roles / Skills / MCP Servers / Plugins / Providers**
- ทุกเมนูใช้ interaction grammar เดียวกัน: **ค้นหา/กรอง + รายการด้านซ้าย + New ด้านบน + detail/editor ด้านขวา + Delete ใน danger zone**
- `Role` เป็นจุดรวมความสัมพันธ์: แก้ Skills, MCP, Plugins และ Provider ของ role ได้ในหน้า Role detail เลย
- Matrix ไม่ใช่ workflow หลัก เพราะทำให้ผู้ใช้ต้องแปลสองแกนและหาว่าจะ create/edit/delete ที่ไหน ให้คงเป็น **Access overview (advanced)** ที่เปิดจาก Roles หรือจาก toolbar เพื่อ audit/bulk editเท่านั้น
- built-in และ external/discovered item ต้องเห็นว่า read-only ตั้งแต่ list row ถึง detail header ไม่ใช่ปล่อยให้ผู้ใช้กดแล้วค่อยเจอ error
- ใช้ staged draft ต่อ entity: `Save changes` และ `Discard` อยู่ใน detail pane ของ entity นั้น ส่วน create/delete ที่สำเร็จ refresh list ทันที ไม่ใช้ footer Save & Apply อันเดียวครอบหลาย storage โดยไม่บอกขอบเขต

แนวคิดหลักคือผู้ใช้ตอบคำถามเดียวทุกหน้า: **“ฉันกำลังจัดการอะไรอยู่ และ action ของสิ่งนั้นอยู่ตรงไหน”**

## สิ่งที่ UI เดิมบอกเรา

จาก `settings_window.py` และ data layer ปัจจุบัน:

1. Role lifecycle แตกออกจากกัน
   - create อยู่หน้า `New Role`
   - enable/provider/delete อยู่หน้า `Providers & Roles`
   - skills อยู่ `Skill Matrix`
   - MCP/plugins อยู่คนละ matrix
   - instruction overlap อยู่หน้า audit อีกหน้า

2. Catalog กับ policy ถูกแยกโดยศัพท์ภายในระบบ
   - Skill Catalog เป็น read-only browser
   - Skill Matrix เป็น assignment editor
   - MCP มีปุ่ม add บน matrix แต่ edit/delete master entry ไม่มีในหน้าเดียวกัน
   - Plugins มีเพียง discovered marketplaces และ assignment; lifecycle จริงไม่ปรากฏ

3. Save model ไม่สอดคล้องกัน
   - matrix/provider/hops staged แล้ว Save & Apply รวมกัน
   - template duplicate/delete เขียนทันที
   - role create/delete เขียนทันที
   - ผู้ใช้จึงเดาไม่ได้ว่า Cancel จะย้อนอะไรได้บ้าง

4. Data model มี distinction สำคัญที่ UI ใหม่ต้องรักษา
   - built-in roles มาจาก `roles.ALL_DEFAULT`; custom roles มาจาก `custom-roles.json` + instruction `.md`
   - `lead`, `codex`, `gemini` มี provider แบบ forced
   - missing role tool policy ไม่เท่ากับ explicit empty policy: MCP/plugins มี fallback defaults
   - skill policy ไม่มี fallback; missing และ empty มีผลเหมือนกัน
   - MCP browser entries บางตัวเป็น managed/built-in และลบ/override ไม่ได้
   - plugin list ปัจจุบันเป็นสิ่งที่ discover จาก Claude installed registry ไม่ใช่ registry ที่ Cockpit เป็นเจ้าของเต็มรูปแบบ
   - provider specs เป็น frozen built-in registry ใน Python; UI ปัจจุบันจัดการเพียง enable/disable และ role override

ข้อเสนอจึงต้องไม่ทำให้ “CRUD ครบ” กลายเป็นปุ่มหลอก เมื่อ data layer ยังรองรับแค่บาง operation

## Information architecture

```text
Settings
├─ Roles                  list + lifecycle + relationships
├─ Skills                 list + lifecycle + assigned roles
├─ MCP Servers            list + lifecycle + allowed roles
├─ Plugins                list + lifecycle + allowed roles
└─ Providers              list + lifecycle/capabilities + assigned roles

Secondary destinations (ไม่อยู่เป็น CRUD menu หลัก)
├─ Access overview        role × {skills|MCP|plugins}, advanced/bulk
├─ Role overlap           audit, เปิดจาก Roles > More
└─ Pipeline/Templates     workflow configuration แยกจาก management module นี้
```

Pipeline, Templates และ Users ไม่ควรยัดเข้า CRUD shell ใหม่นี้ใน wave แรก เพราะเป็นคนละ mental model แต่ window เดิมยังอยู่หลัง feature flag จนกว่าจะย้ายโดเมนเหล่านั้นในงานถัดไป

## Unified CRUD pattern

ทุก entity page ใช้ component และตำแหน่ง action เหมือนกัน:

1. Page header
   - ชื่อ entity + จำนวนรายการ
   - `New <entity>` เป็น gold primary CTA มุมขวาบน
   - optional `Access overview` / `Refresh` เป็น secondary action

2. List pane
   - search ที่ตำแหน่งเดิมทุกหน้า
   - filters: All / Custom / Built-in / External / Disabled ตามที่ใช้ได้
   - row แสดง name, one-line metadata, source badge, state
   - selected row ใช้ gold focus/selection treatment
   - ไม่มี icon-only delete ใน row เพื่อลด accidental deletion

3. Detail pane
   - header: icon/chip, display name, immutable ID, source badge
   - tabs ใช้ order เดิม: `General`, `Access/Assignments`, `Advanced` (เฉพาะเมื่อจำเป็น)
   - editable field เปลี่ยนแล้วแสดง unsaved marker ที่ detail footer
   - footer: `Discard` ซ้าย, `Save changes` ขวา
   - switching selection ระหว่างมี draft: dialog `Save / Discard / Keep editing`

4. Create
   - กด New แล้วเปิด **blank detail pane ใน shell เดิม** ไม่เปิดหน้าใหม่หรือ dialog คนละ pattern
   - list pane ยังอยู่ ทำให้เห็น context และยกเลิกได้
   - footer เปลี่ยนเป็น `Cancel` + `Create <entity>`
   - validation แสดงใต้ field และ focus field แรกที่ผิด

5. Delete
   - อยู่ล่างสุดของ `Advanced > Danger zone` ทุก entity
   - confirm แสดงชื่อ, source files/config ที่กระทบ, relationship count และสิ่งที่จะถูกล้าง
   - destructive CTA ต้องพิมพ์ชื่อเมื่อมี downstream references; confirm ปกติพอสำหรับ orphan custom item
   - หลังสำเร็จเลือก item ถัดไปและแสดง toast พร้อมผลที่ทำจริง

6. Read-only
   - badge `BUILT-IN`, `MANAGED`, หรือ `EXTERNAL` แสดงทั้งใน row และ header
   - field ที่ immutable แสดงเป็น text/value row ไม่ใช่ disabled input ทั้ง form
   - action ที่ทำไม่ได้ไม่แสดง หรือแสดง disabled พร้อมเหตุผลที่มองเห็นได้
   - settings ที่อนุญาตให้ override ได้อยู่ใน card แยกชื่อ `Your overrides` เพื่อไม่ทำให้เข้าใจว่ากำลังแก้ definition ต้นฉบับ

## Relationship decision: edit from Role first, matrix second

### ทางเลือกที่เลือก

แก้ relationship จาก `Roles > <role> > Access` เป็น workflow หลัก:

- Provider: single select พร้อมสถานะ installed/enabled/substituted
- Skills: searchable multi-select list
- MCP Servers: searchable multi-select list
- Plugins: searchable multi-select list
- ทุก section มี count และ `Clear override / Use defaults` ที่อธิบาย semantics ชัด

เหตุผลที่งงน้อยกว่า matrix:

- ผู้ใช้มักเริ่มจาก intent ว่า “backend ต้องใช้อะไร” ไม่ใช่ “playwright ต้องแจกให้ใครบ้าง”
- role เป็น aggregate ที่มี provider + instructions + capabilities อยู่แล้ว
- assignment ทั้งหมดถูกบันทึกจาก context เดียว ลดการเดินข้าม 4 หน้า
- จัดการรายการจำนวนมากได้ด้วย search/filter โดยไม่เกิด matrix แนวนอนที่ล้น

### Matrix ยังมีประโยชน์ตรงไหน

คง `Access overview` เป็น advanced bulk/audit surface:

- เปิดจากปุ่ม secondary ใน Roles header หรือ `View assignments` จาก entity detail
- selector ด้านบนเลือก dimension ทีละอย่าง: Skills / MCP / Plugins
- frozen role column, horizontal scroll, search ทั้ง row/column
- batch bar เมื่อแก้: `3 changes · Discard · Apply 3 changes`
- ไม่ create/edit/delete entity ใน matrix; cell มีหน้าที่ assignment อย่างเดียว
- double-click header เปิด entity detail, click role chip เปิด role detail

กฎสำคัญ: **ไม่มีคำว่า Matrix ใน main navigation** เพราะ matrix คือ view mode ไม่ใช่ entity

## Main shell — ASCII mockup

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ takkub COCKPIT  /  Settings                                      vX.Y.Z     │
├───────────────┬──────────────────────────────────────────────────────────────┤
│ MANAGEMENT    │ Roles                                         [+ New Role] │
│ ▌ Roles       │ Manage team identities, providers and access                │
│   Skills      ├──────────────────────┬───────────────────────────────────────┤
│   MCP Servers │ 🔎 Search roles...   │ Backend                 [BUILT-IN]    │
│   Plugins     │ [All] [Custom]       │ backend · enabled                     │
│   Providers   │                      │                                       │
│               │ ● Lead     BUILT-IN  │ [General] [Access] [Advanced]         │
│               │ ● Frontend BUILT-IN  │                                       │
│               │ ● Backend  BUILT-IN  │ Display name  [Backend             ]  │
│               │ ● Data Eng CUSTOM    │ Provider      [Codex ▾] [available]  │
│               │                      │ Enabled       [●────────]             │
│               │                      │                                       │
│               │                      │             [Discard] [Save changes]  │
├───────────────┴──────────────────────┴───────────────────────────────────────┤
│ No unsaved changes                                      [Close Settings]    │
└──────────────────────────────────────────────────────────────────────────────┘
```

Window-level footer มีเพียง status และ Close; save ownership อยู่ใน detail pane เพื่อไม่ให้ผู้ใช้สงสัยว่าปุ่ม global จะบันทึกหน้าใดบ้าง

## Roles

### List row

`role color dot · Label · id · BUILT-IN/CUSTOM · enabled state`

### General tab

- Display name
- Role ID (`--role`) — immutable หลัง create เพราะเป็น key/path
- Instructions editor + `Open source file`
- Color ผ่าน canonical `cockpit_theme.ROLE_COLORS`; custom fallback เท่านั้นที่เลือกสีได้
- Grid placement เป็น Advanced เพราะไม่ใช่งานประจำ
- Enabled in project

### Access tab

```text
┌ Role / Backend / Access ────────────────────────────────────────────────────┐
│ Provider                                                                  │
│ [Codex ▾]   ✓ installed   ✓ enabled                                       │
│ If unavailable: Claude substitutes; model diversity is reduced            │
│                                                                            │
│ Skills (2)                                     [Search skills...]          │
│ [✓] cockpit-ui-style   [✓] github   [ ] release-notes                     │
│                                                                            │
│ MCP Servers (1)                                [Use defaults]              │
│ [✓] playwright  MANAGED   [ ] chrome-devtools   [ ] obsidian-vault        │
│                                                                            │
│ Plugins (2)                                    [Use defaults]              │
│ [✓] github  [✓] frontend-design  [ ] security-guidance (blocked by policy)│
│                                                      [Save changes]        │
└────────────────────────────────────────────────────────────────────────────┘
```

### Built-in role affordance

- header badge `BUILT-IN · definition locked`
- instructions/name/color แสดงแบบ read-only
- `Your overrides` ยังแก้ provider (ถ้าไม่ forced), enabled และ access policy ได้
- lead แสดง `Provider: Claude · fixed by cockpit infrastructure`
- codex/gemini แสดง provider fixed และเหตุผล
- built-in ไม่มี danger zone / delete

### Custom role lifecycle

- create, edit label/color/instructions/grid placement
- delete ต้อง preview cleanup:
  - custom role registry entry
  - instruction file
  - provider override
  - pipeline `rolesEnabled` และ template references (ต้องกำหนดว่าจะ block หรือ cascade)
  - skill policy entry
  - pane-tools policy entry
- ข้อเสนอ: ถ้ามี template references ให้ **block delete** และลิงก์ไปแก้ references ก่อน; policy/override ที่เป็น leaf ให้ cascade และแสดงใน confirm

## Skills

```text
┌ Skills (12)                                                   [+ New Skill]┐
├──────────────────────┬──────────────────────────────────────────────────────┤
│ 🔎 Search skills...  │ cockpit-ui-style                       [PROJECT]    │
│ [All][Project][Ship] │ General | Assigned roles                             │
│                      │                                                      │
│ cockpit-ui-style     │ Name         [cockpit-ui-style]                      │
│ github               │ Description  [.................................]     │
│ release-notes        │ Instructions [markdown editor..................]     │
│                      │ Path         .claude/skills/.../SKILL.md             │
│                      │                                                      │
│                      │ Assigned to  Backend, Frontend     [Manage roles]    │
│                      │                            [Discard] [Save changes]   │
│                      │ Danger zone                         [Delete skill]   │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

Ownership badges:

- `PROJECT`: file under writable project `.claude/skills`; full CRUD
- `SHIPPED`: bundled/cockpit asset; read-only, optional `Duplicate to project`
- `EXTERNAL`: discovered from a root Cockpit does not own; read-only with `Open folder`

Create wizard ใช้ fields `name`, `description`, `instructions`; เขียน valid frontmatter + `SKILL.md` atomically. Edit ต้อง preserve unknown frontmatter keys. Delete แสดง roles ที่ถูก assign และล้าง `skill-policy.json` references หลัง confirm

Data gap: `skill_scan.py` ปัจจุบันเป็น read-only scanner จึงต้องเพิ่ม repository/service สำหรับ create/update/delete ก่อนเปิด action จริง

## MCP Servers

```text
┌ MCP Servers (5)                                         [+ New MCP Server] ┐
├──────────────────────┬──────────────────────────────────────────────────────┤
│ 🔎 Search servers... │ playwright                               [MANAGED]  │
│ [All][Managed][User] │ General | Allowed roles | Diagnostics               │
│                      │                                                      │
│ playwright  MANAGED  │ Command  npx                                         │
│ chrome...   MANAGED  │ Args     -y @playwright/mcp@...                      │
│ obsidian    USER     │ Source   Cockpit browser MCP                         │
│                      │ Allowed  QA, Critic                 [Manage roles]    │
│                      │                                                      │
│                      │ Managed servers cannot be edited or deleted.         │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

- user-owned MCP: create/edit/delete ผ่าน `shared_dev_tools.add_mcp_server(..., force=True)` / `remove_mcp_server`
- managed browser MCP: definition read-only; assignment ยังแก้ได้
- credential-bearing config: ไม่แสดง secret value; ใช้ masked environment-variable reference และ warning
- delete confirm แสดง affected roles; transaction ลบ master entry, policy references และ regenerate role variants
- diagnostics แสดง command found/not found และ config source แต่ไม่รัน long-lived server จาก settings

## Plugins

```text
┌ Plugins (8)                                               [+ Install Plugin]┐
├──────────────────────┬──────────────────────────────────────────────────────┤
│ 🔎 Search plugins... │ github                                  [INSTALLED] │
│ [Installed][Blocked] │ General | Allowed roles                              │
│                      │                                                      │
│ github               │ Marketplace  openai-curated                         │
│ frontend-design      │ Version      ...                                    │
│ security-guidance    │ Location     ...                                    │
│                      │ Allowed roles  Lead, Reviewer       [Manage roles]   │
│                      │                                     [Uninstall]      │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

คำใน UI ต้องตรง capability จริง:

- create = `Install Plugin` ไม่ใช่ New หาก lifecycle มาจาก marketplace
- edit = assignment/configuration; identity/version เป็น external metadata
- delete = `Uninstall`
- denylisted `security-guidance` / `remember` แสดง `BLOCKED BY COCKPIT` และ reason; assignment disabled

Data gap: ปัจจุบัน UI discover จาก `installed_plugins.json` เท่านั้น ต้องมี `PluginRepository`/installer adapter ที่คืน structured error และรองรับ non-interactive install/uninstall ก่อนเปิดปุ่ม ไม่ควรแก้ registry file ตรงจาก UI

## Providers

```text
┌ Providers (3)                                        [+ New Provider Spec] ┐
├──────────────────────┬──────────────────────────────────────────────────────┤
│ 🔎 Search providers  │ Codex                                    [BUILT-IN]│
│ [All][Enabled]       │ General | Capabilities | Assigned roles             │
│                      │                                                      │
│ Claude   required    │ Binary      codex / codex.cmd                       │
│ Codex    enabled     │ Status      ✓ installed · enabled                   │
│ Gemini   disabled    │ Context     AGENTS.md bridge                        │
│                      │ Assigned    Backend, Codex                           │
│                      │                                                      │
│                      │ Your override: [Enabled ●────]                       │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

Provider มีสองชั้นที่ต้องสื่อให้ชัด:

1. **Spec definition**: binary, flags, ready rules, capabilities
2. **Operational override**: enabled/disabled และ role assignment

Built-in `claude/codex/gemini` spec เป็น `BUILT-IN` และ definition read-only; ผู้ใช้แก้ได้เฉพาะ operational override ที่ระบบอนุญาต โดย Claude แสดง `REQUIRED` และ toggle disabled

เพื่อให้ CRUD Providers “ครบ” จริง ต้องรองรับ custom provider spec แยกจาก `provider_spec.py` เช่น `~/.takkub/providers/<id>.json` แล้ว merge กับ `PROVIDER_REGISTRY` ผ่าน validated registry service การทำเพียง form เขียน JSON ยังไม่พอ เพราะ spawn, ready detection, context strategy และ capability consumers ต้องอ่าน registry เดียวกันทั้งหมด

ข้อเสนอ rollout:

- wave แรกแสดง built-in provider management แบบ read-only definition + edit operational state
- ซ่อน `New Provider Spec` หลัง capability flag จน custom registry ผ่าน end-to-end tests
- เมื่อเปิด custom provider: full create/edit/delete เฉพาะ custom; built-in ใช้ Duplicate เป็นจุดเริ่มต้นได้ แต่ต้อง validate marker ordering และ binary discovery ที่ปลอดภัย

## Interaction specification

### Selection and drafts

- click row โหลด immutable snapshot + draft ใน detail
- field change ทำเฉพาะ draft และแสดง `Unsaved changes`
- Save เรียก service transaction ของ entity เดียว
- error ไม่ปิด editorและไม่ทิ้ง draft
- หลัง save สำเร็จ update row in place และ toast `Saved <name>`
- refresh ขณะ dirty ต้องถามก่อน

### Create

- New สร้าง pseudo-row `New <entity>` ที่ selected ชั่วคราว
- Create disabled จน required fields valid
- name collision แสดง inline และเสนอเปิด item ที่มีอยู่
- successful create เปลี่ยน pseudo-row เป็น real row โดย selection ไม่กระโดด

### Delete

- ไม่มี delete บน list row
- danger zone แสดงเฉพาะ item ที่ deletable
- confirm มีรายการ effect ไม่ใช้ generic “Are you sure?”
- failure แสดง error ที่ service คืน ไม่เอา item ออกจาก listก่อน persist สำเร็จ

### Defaults versus empty override

สำหรับ MCP/Plugins ต้องมี tri-state ที่มองเห็นได้:

- `Use role defaults` — ไม่มี policy entry
- `Custom selection (N)` — มี explicit allowlist
- `No access` — explicit empty allowlist

ห้ามใช้ unchecked grid เหมือนกันทั้ง “default” และ “empty” เพราะ data layer แยก semantics นี้อยู่แล้ว ส่วน Skill ใช้ two-state ได้เพราะ missing และ empty มีผลเหมือนกัน

### Keyboard and accessibility

- `Ctrl+N` create, `Ctrl+S` save current detail, `Esc` cancel draft (ถามเมื่อ dirty), `Delete` ไม่ลบทันทีแต่เปิด confirm
- ทุก icon button มี visible tooltip + accessible name
- status ไม่สื่อด้วยสีอย่างเดียว ใช้ text badge คู่กัน
- focus order: search → list → tabs → fields → footer actions

## Visual specification using cockpit-ui-style

- apply `cockpit_theme.build_stylesheet(sans, mono)` ที่ window root
- font ทั้งหมดผ่าน `theme.ensure_fonts_loaded()`; sans สำหรับ body/form, mono สำหรับ IDs/source/status badges
- primary CTA ใช้ `theme.gold_button`; secondary ใช้ `theme.secondary_button`
- active nav, selection, focus และ checked state ใช้ `ACCENT_GOLD`
- grounds: body/window/panel/input/select ใช้ token ตาม semantic name ห้าม inline hex
- borders ใช้ `BORDER_HAIRLINE`, `BORDER_MED`, `BORDER_STRONG`
- radii ใช้ `RADIUS_SM/MD/LG` เท่านั้น; source badges ใช้ pill
- role identity ใช้ `theme.ROLE_COLORS`, fallback จาก custom `Role.color` เท่านั้น
- semantic error/warn/success ต้องเพิ่ม token ใน `cockpit_theme.py` หากยังไม่มี ไม่แปลงทุกสถานะเป็น gold
- หลีกเลี่ยง disabled input เต็มหน้า read-only; ใช้ value display + lock/source badge ซึ่งอ่านง่ายกว่าและ contrast ดีกว่า

## Technical module proposal

สร้าง package ใหม่ ไม่เพิ่ม class ต่อท้าย `settings_window.py`:

```text
src/agent_takkub/settings_management/
├─ __init__.py
├─ window.py                 SettingsManagementWindow + routing shell
├─ models.py                 EntityKind, Ownership, Capability, DraftState
├─ commands.py               Create/Update/Delete command DTOs
├─ transaction.py            snapshot/rollback + structured OperationResult
├─ feature_flags.py          resolve new/old settings surface
├─ repositories/
│  ├─ roles.py               adapter: roles + custom_roles + instructions
│  ├─ skills.py              scan + writable SKILL.md CRUD
│  ├─ mcps.py                shared_dev_tools adapter
│  ├─ plugins.py             discovery + installer adapter
│  └─ providers.py           provider_config + ProviderSpec registry adapter
├─ services/
│  ├─ relationships.py       role aggregate read/write
│  ├─ cleanup.py             reference discovery + delete plans
│  └─ validation.py          shared names/frontmatter/spec validation
├─ widgets/
│  ├─ management_page.py     reusable list–detail shell
│  ├─ entity_list.py
│  ├─ detail_header.py
│  ├─ detail_footer.py
│  ├─ source_badge.py
│  ├─ relationship_picker.py
│  ├─ danger_zone.py
│  └─ access_overview.py
└─ pages/
   ├─ roles_page.py
   ├─ skills_page.py
   ├─ mcps_page.py
   ├─ plugins_page.py
   └─ providers_page.py
```

### Repository contract

แต่ละ repository ใช้ contract เดียวในระดับ UI แต่ประกาศ capability ราย item:

```python
list(query) -> list[EntitySummary]
get(entity_id) -> EntityDetail
capabilities(entity_id | None) -> {can_create, can_update, can_delete, reason}
create(command) -> OperationResult
update(entity_id, command) -> OperationResult
delete_plan(entity_id) -> DeletePlan
delete(entity_id, confirmed_plan_version) -> OperationResult
```

UI ไม่ควร import JSON path หรือเรียกหลาย data modules เอง การรวม transaction และ cleanup อยู่ใน service ทำให้ไม่เกิด partial state เช่น role ถูกลบจาก registry แต่ยังค้างใน policy/provider/template

### Reuse data layer เดิม

| Entity | Read/reuse | Write/reuse | สิ่งที่ต้องเพิ่ม |
|---|---|---|---|
| Roles | `roles.all_role_names/by_name`, `custom_roles.load_custom_roles` | `create_role`, `save_custom_roles`, `delete_role`, live register/unregister | atomic `update_role`; reference-aware delete service |
| Skills | `skill_scan.scan_skills` | `skill_policy.set_role_skills` สำหรับ assignment | safe SKILL.md create/update/delete + ownership resolution |
| MCP | `shared_dev_tools.list_master_mcps`, policy effective helpers | `add_mcp_server`, `remove_mcp_server`, `set_role_items`, `regen_role_variants` | multi-store transaction + managed metadata |
| Plugins | `pane_tools_dialog.discover_marketplaces`, installed registry | `pane_tools_policy.set_role_items` | installer/uninstaller service + plugin metadata/status |
| Providers | `PROVIDER_REGISTRY`, `provider_config`, `provider_state` | role overrides + enable/disable flow | custom provider registry and end-to-end dynamic consumers |

`settings_window.py` เดิมไม่ควรถูกใช้เป็น data service; helper ที่เป็น pure logic เช่น matrix diff ควรย้ายออกจาก `pane_tools_dialog.py` ไป service/module กลางก่อน reuse

## Feature flag and safe coexistence

ใช้ environment/config flag ที่ resolve ที่จุดเปิด Settings เพียงจุดเดียว:

```text
TAKKUB_SETTINGS_UI=legacy   # default ในช่วงแรก
TAKKUB_SETTINGS_UI=new      # opt-in
TAKKUB_SETTINGS_UI=compare  # dev only: menu เปิดได้ทั้งสอง surface
```

- window ใหม่อ่าน/write storage เดิมผ่าน adapters เพื่อไม่มี migration data แบบ big bang
- legacy window ยังใช้งานได้ทันทีเมื่อ flag กลับเป็น `legacy`
- เพิ่มเมนู `Open legacy settings` ใน new window เฉพาะ beta build
- telemetry/log event บันทึก operation result และ rollback โดยไม่เก็บ secret/config value
- ห้ามเปิดสอง window พร้อมแก้ entity เดียวกัน; ใช้ file revision/hash เพื่อ detect stale write และเสนอ reload

## Implementation order

### Phase 0 — contracts and characterization

1. เขียน characterization tests ของ JSON semantics: default vs explicit empty, forced providers, custom role live registration, managed MCP protection
2. สร้าง repository DTO/capability contract และ structured errors
3. ย้าย pure relationship/matrix helpers ออกจาก dialog layer

### Phase 1 — shell + Roles vertical slice

1. สร้าง window shell, sidebar, reusable list–detail, draft guard
2. Roles list/detail/create/edit/delete
3. Role Access tab เขียน provider/skill/MCP/plugin ผ่าน transaction เดียว
4. reference-aware delete + rollback tests

นี่คือ phase ที่พิสูจน์ pattern และแก้ pain หลักได้เร็วที่สุด

### Phase 2 — Skills and MCP

1. Skills ownership resolver + atomic file CRUD
2. assignment cleanup และ preserve unknown frontmatter
3. MCP master CRUD + managed protection + role variant regeneration
4. เพิ่ม Access overview advanced view หลัง role-centric flow ใช้งานได้แล้ว

### Phase 3 — Plugins

1. plugin metadata repository
2. non-interactive installer/uninstaller adapter
3. assignment + denylist affordance
4. failure/partial install recovery tests

### Phase 4 — Providers

1. built-in read-only specs + operational overrides
2. dynamic custom provider registry behind separate flag
3. wire spawn/PTY/context consumers ทั้งหมดให้ registry-driven
4. conformance tests ต่อ provider spec และ rollback/delete guard

### Phase 5 — rollout

1. dogfood `compare`
2. เปิด `new` เป็น default แต่เก็บ legacy fallback หนึ่ง release
3. ย้าย/ลบเฉพาะ code path เก่าที่ไม่มี caller หลัง metrics และ regression suite ผ่าน

## Testing acceptance criteria

- ผู้ใช้ทำ create/edit/delete ของ custom entity ทุกชนิดจากหน้า entity เดียว โดยไม่เปิด main nav หน้าอื่น
- ผู้ใช้ assign provider/skills/MCP/plugins ให้ role เดียวจาก Role detail ได้
- built-in/managed/external item ไม่มี misleading editable control
- deleting custom role/skill/MCP/plugin ไม่ทิ้ง dangling reference
- failed multi-file write rollback กลับ snapshot เดิมครบ
- default/explicit-empty semantics ของ MCP/plugins ไม่เปลี่ยน
- forced provider rules ของ lead/codex/gemini ไม่ถูก override
- keyboard-only user ทำ list → edit → save และเปิด delete confirm ได้
- theme audit ไม่พบ raw hex/font/radius ใน module ใหม่
- legacy/new ใช้ storage ชุดเดียวกันและสลับ flag กลับได้โดยข้อมูลไม่เสีย

## Final recommendation

อย่าเริ่มจาก “รวม matrix ให้สวยขึ้น” เพราะ matrix คือปลายเหตุของ information architecture เดิม ให้เริ่มจาก **Roles vertical slice ใน reusable list–detail shell** แล้วทำให้ role เป็นจุดรวม relationship ทั้งหมด เมื่อผู้ใช้เข้าใจ CRUD pattern แรกแล้ว อีกสี่ entity จะเรียนรู้ได้แทบเป็นศูนย์

หลักที่ควรยึดตลอด implementation:

> Entity lifecycle อยู่หน้า entity; relationship แก้จาก role เป็นหลัก; bulk matrix เป็นเพียง alternate view; action ที่ data layer ยังทำจริงไม่ได้ต้องไม่ปรากฏเป็นปุ่มพร้อมใช้
