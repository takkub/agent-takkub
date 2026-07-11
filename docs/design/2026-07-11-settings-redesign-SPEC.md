# Settings Management Redesign — synthesized SPEC (Lead)

วันที่: 2026-07-11 · สังเคราะห์จาก 2 proposals อิสระ:
- `2026-07-11-settings-redesign-gemini.md` (IA + layout)
- `2026-07-11-settings-redesign-codex.md` (interaction grammar + engineering plan)

**ทั้งคู่เห็นตรงกันโดยไม่ได้นัดกัน** → confidence สูงว่าทิศถูก:
sidebar ตรงไป 5 entity + ทุกหน้าเป็น master-detail (list ซ้าย + detail ขวา) pattern เดียวกันเป๊ะ

## Decision (Lead ฟันธง)

| เรื่อง | ตัดสินใจ | มาจาก |
|---|---|---|
| IA | Sidebar 5 เมนู: Roles / Skills / MCP Servers / Plugins / Providers — ไม่มี hub, ไม่มี modal | ตรงกันทั้งคู่ (gemini Option A = codex IA) |
| CRUD pattern | list + search/filter ซ้าย · `+ New` gold CTA มุมขวาบน · คลิก row = detail ขวา · Save/Discard ใน detail footer (ไม่ใช่ global) · Delete ใน Danger zone ของ detail | codex (ละเอียดกว่า) |
| Relationships | แก้จากหน้า Role เป็นหลัก (Role > Access tab: provider + skills + MCP + plugins ที่เดียว) · Matrix เดิมกลายเป็น "Access overview" (advanced/bulk view เปิดจากปุ่ม secondary — **ไม่อยู่ใน nav หลัก**) | codex — "user คิดจาก role ไม่ใช่จาก tool" |
| Read-only affordance | badge `BUILT-IN` / `MANAGED` / `EXTERNAL` / `PROJECT` ทั้งบน row และ detail header · field immutable แสดงเป็น value row ไม่ใช่ disabled input · ปุ่มที่ทำไม่ได้ = ไม่แสดง หรือ disabled + เหตุผล | codex |
| Defaults vs empty | MCP/Plugins ต้องมี tri-state มองเห็นได้: Use defaults / Custom (N) / No access (data layer แยก semantics นี้อยู่แล้ว) · Skills two-state พอ | codex (data-model จริง) |
| Save model | staged draft ต่อ entity · สลับ row ตอน dirty = ถาม Save/Discard/Keep · create/delete สำเร็จ = refresh ทันที | codex |
| Coexistence | module ใหม่ `settings_management/` แยกจาก `settings_window.py` เดิม 100% · flag `TAKKUB_SETTINGS_UI=legacy|new|compare` (default legacy จนกว่า review ผ่าน) · ใช้ storage เดิมผ่าน adapters — สลับกลับได้ทุกเมื่อ ข้อมูลไม่เสีย | codex + คำสั่ง user "ไม่ต้องไปยุ่งของเก่า เดี๋ยวพัง" |
| Visual | token จาก cockpit-ui-style เท่านั้น (gold CTA, grounds, IBM Plex, RADIUS_SM/MD/LG) ห้าม inline hex | ทั้งคู่ |

## Module layout (ตาม codex §Technical module proposal)

`src/agent_takkub/settings_management/` — window shell + models + commands + transaction +
repositories (roles/skills/mcps/plugins/providers — adapter ทับ data layer เดิม, UI ห้ามแตะ JSON ตรง) +
services (relationships/cleanup/validation) + widgets (management_page/entity_list/detail_*/danger_zone/access_overview) + pages ×5

Repository contract: `list / get / capabilities / create / update / delete_plan / delete` —
ปุ่มที่ data layer ยังทำจริงไม่ได้ **ต้องไม่ปรากฏเป็นปุ่มพร้อมใช้** (กติกาเหล็กจาก codex)

## Implementation phases (ปรับจาก codex ให้เข้ากับงานที่ landed แล้ว)

- **Phase 0+1 (เริ่มทันที):** characterization tests ของ JSON semantics + shell + **Roles vertical slice** (list/detail/create/edit/delete + Access tab เขียน provider/skill/MCP/plugin ผ่าน transaction เดียว + reference-aware delete)
- **Phase 2:** Skills (reuse ฟังก์ชัน create/delete ที่ frontend#3 กำลังทำบน skill_scan) + MCP (shared_dev_tools adapter + managed protection)
- **Phase 3:** Plugins (installer adapter — Install/Uninstall ไม่ใช่ New/Delete)
- **Phase 4:** Providers (built-in read-only + operational override · custom provider spec ซ่อนหลัง flag แยกจน registry ผ่าน e2e)
- **Phase 5:** dogfood `compare` → default `new` → เก็บ legacy ไว้ 1 release

## Open items ที่ต้อง sync
- ผล central-home audit (maintainer กำลังทำ) อาจย้าย path ของ skills/ledger → กระทบแค่ชั้น repository (ออกแบบกันไว้แล้ว)
- Plugin installer + custom provider registry = data gap จริง — ห้ามโชว์ปุ่มจนมี service

## Addendum (2026-07-11, codex cross-check MED-4): `compare` dropped

`TAKKUB_SETTINGS_UI=compare` (row "Coexistence" ด้านบน + Phase 5) ไม่เคย implement จริง —
`user_actions._open_settings_window` special-case แค่ `new`, ทุกค่าอื่น (รวม `compare`) เปิด
legacy อย่างเดียว ทำให้ doc สัญญา "เปิดทั้งคู่" แต่ code ไม่ทำ. แก้โดยตัด `compare` ออกจาก
`feature_flags.SettingsUI` (ค่าที่ไม่รู้จัก fallback เป็น `new` เหมือนเดิม) แทนที่จะ implement
dual-window จริง — การเปิดสองหน้าต่างพร้อมกันเป็น feature ใหม่ที่อยู่ชั้น window shell
(`window.py`/`user_actions.py`), ไม่ใช่ one-line fix ในชั้น flag resolution นี้. Phase 5's
dogfood step is now just `legacy → new` (no compare stage).
