# Settings window UI (Phase 1) — code review

ขอบเขต: foundation, **Providers & Roles**, **New Role**, และ theme/QSS ของ
`settings_window.py` + `cockpit_theme.py` ณ วันที่ 2026-07-10 โดย trace ต่อไปถึง
caller/store ที่ UI เรียกจริงเมื่อจำเป็น

## Findings

| issue | severity | file:line | fix |
|---|---|---|---|
| **Save ลบ provider override ของ role ที่ UI ไม่ได้แสดง** — `_role_provider_combos` มีเฉพาะ `_OVERRIDABLE_ROLES` จาก `pipeline_config.VALID_ROLES` (default roles แบบ static) แต่ `save_role_overrides()` มี contract ว่า input คือ mapping ทั้งก้อนและเขียนทับไฟล์ทั้งไฟล์ ดังนั้น override ของ custom role หรือ role ใหม่ในอนาคตจะหายทันทีแม้ผู้ใช้ไม่ได้แตะมัน | **High** | `src/agent_takkub/settings_window.py:105-107,397-400`; `src/agent_takkub/provider_config.py:120-161` | เพิ่ม API แบบ patch/merge ใน `provider_config` หรือ merge กับ `load_providers(project)` โดย preserve key ที่อยู่นอก scope ของ UI ก่อนเขียน และเพิ่ม regression test ที่ seed `{"custom-role":"codex"}` แล้ว Save built-in role โดย custom key ต้องยังอยู่ |
| **Footer “Save & Apply” เป็น no-op สำหรับ New Role แล้วปิด dialog ทิ้ง input** — handler บันทึกเฉพาะ provider/pipeline แล้ว `accept()` เสมอ; มันไม่เรียก `_on_create_role_clicked()`. ผู้ใช้กรอก form แล้วกด CTA หลักด้านล่างจะเสีย form โดยไม่มี warning ขณะที่ปุ่ม Create เป็น transaction แยกและ Cancel หลัง Create ก็ย้อน role ที่สร้างแล้วไม่ได้ | **High** | `src/agent_takkub/settings_window.py:385-412,694-718` | ทำ footer dispatch ตาม active view: New Role ต้อง create/validate และปิดเฉพาะเมื่อสำเร็จ หรือซ่อน/disable footer Save ใน view นี้และเปลี่ยน Cancel เป็น Close หลังมี immediate commit; เพิ่ม close-confirm เมื่อ form ยังมีค่า |
| **Save & Apply ไม่ atomic และอาจรายงาน “Save failed” หลังเขียนบาง store ไปแล้ว** — role-provider file ถูกเขียนก่อน pipeline file; ถ้า `pipeline_config.save()` ล้มเหลว override แรกค้างอยู่และ Cancel คืนไม่ได้ หลัง dialog accepted ยังมี provider-state store ชุดที่สามซึ่ง caller เขียนภายหลัง; `toggle_provider()` ปล่อย `OSError` จาก disk write หลุดออกมาแม้ docstring บอกว่าล้มเหลวได้เฉพาะ unknown provider | **High** | `src/agent_takkub/settings_window.py:397-412`; `src/agent_takkub/user_actions.py:357-366`; `src/agent_takkub/orchestrator.py:1397-1414`; `src/agent_takkub/provider_state.py:58-64` | สร้าง transaction boundary ชัดเจน: validate/serialize ทุก payload ก่อน, เขียน temp ทุกไฟล์, replace พร้อม rollback/captured originals; ให้ orchestrator แปลง I/O error เป็น `(False, message)` และ caller แสดง error. อย่า `accept()` จนทุกส่วนที่นิยามว่า Apply สำเร็จจริง |
| **“Reset to default” ไม่ได้ reset เป็น default** — implementation โหลดค่าที่ save อยู่กลับมา จึงเป็น Revert unsaved changes; provider override ยังเป็น codex/gemini และ role ที่ปิดไว้ยังปิดต่อไป ต่างจากข้อความบนปุ่ม | **Medium** | `src/agent_takkub/settings_window.py:341-342,375-383,555-572` | ถ้าต้องการ Revert ให้เปลี่ยน label; ถ้าต้องการ default จริงให้ set providers enabled, role CLI = Claude/default และ roles enabled = true แล้ว mark dirty เพื่อให้ Save ยืนยันการ persist |
| **dirty state แบบ global/sticky ผิดเมื่อมีหลาย view** — Reset view ปัจจุบันเรียก `_clear_dirty()` แม้ view อื่นยังมี staged changes; กลับค่าจุดเดิมด้วยมือก็ยัง dirty. New Role fields/swatch/default-tools toggle ไม่ต่อ `_mark_dirty()` เลย จึงไม่แสดง unsaved indicator และไม่ช่วยเตือนก่อน footer Save/Cancel | **Medium** | `src/agent_takkub/settings_window.py:365-383,459,545,550,593-674` | เก็บ baseline + dirty ต่อ view แล้ว aggregate ด้วยการ diff ค่าปัจจุบัน ไม่ใช้ latch boolean; Reset ล้างเฉพาะ view นั้น และต่อ `textChanged/valueChanged/currentIndexChanged/toggled` ของ New Role เข้ากับ recompute-dirty |
| **Default MCP+Plugins toggle เป็น control ที่กดได้แต่ไม่มีผล** — state ไม่ถูกอ่านตอน Create และคำว่า “ตาม column” ยังไม่มี backend preset จริง. การปล่อย custom role แบบไม่มี override ก็ไม่เท่ากับ safe column default: MCP ที่ไม่มี built-in policy ใช้ `None` ซึ่งหมายถึง master passthrough ส่วน plugin ตกไป teammate default | **Medium** | `src/agent_takkub/settings_window.py:647-659,694-701`; `src/agent_takkub/pane_tools_policy.py:217-247`; `src/agent_takkub/shared_dev_tools.py:173-180` | ระยะสั้น disable/hide switch ให้ชัดว่า unavailable. ถ้าจะ wire ให้เพิ่ม backend `defaults_for_column(column)` ที่คืน allowlist ของ **ทั้ง** MCP และ plugins, persist ทั้งคู่ผ่าน `pane_tools_policy.set_role_items()`, ตรวจ return value และเรียก `shared_dev_tools.regen_role_variants()`; อย่าใช้ `reset_role()` เป็น “column default” จนกว่าจะนิยาม preset จริง |
| **Create Role มี partial-commit edge** — `custom-roles.json` ถูกบันทึกก่อนสร้าง agent markdown; ถ้าเขียน markdown ไม่ได้ ฟังก์ชันคืน failure แต่ role อยู่ใน registry file แล้ว การ retry จะติด duplicate และ UI แสดงภาพว่าการสร้างไม่สำเร็จทั้งที่ state เปลี่ยนแล้ว | **High** | `src/agent_takkub/custom_roles.py:183-199`; call site `src/agent_takkub/settings_window.py:701-704` | เขียน role markdown ไป temp ก่อน แล้ว commit registry + rename โดยมี rollback; อย่างน้อยเมื่อขั้นสองล้มเหลวต้อง restore registry เดิมและลบ temp พร้อม test จำลอง `write_text()` failure |
| **Disabled ToggleSwitch ยังวาดเหมือน enabled และไม่มี focus/accessibility cue** — lead switch ถูก disable แต่ยังเป็น gold/on; custom painter ไม่ใช้ `isEnabled()`, ไม่วาด focus และ toggle ไม่มี accessible name จึงดูเหมือนกดได้และ keyboard/screen reader บอกความหมายไม่ได้ | **Low** | `src/agent_takkub/cockpit_theme.py:383-406`; `src/agent_takkub/settings_window.py:531-537` | วาด disabled track/knob ด้วย muted palette, ตั้ง cursor ตาม enabled state, วาด focus ring และกำหนด `accessibleName`/toolTip ที่ call site (provider/role name + on/off semantics) |
| **Dark QSS ยังพึ่ง native subcontrols บางส่วน** — popup item view และ scroll viewport ถูกกำหนดสีแล้ว แต่ `QComboBox` drop-down/arrow, `QSpinBox` up/down, scrollbar และ popup container ไม่ได้ style; `QSpinBox` ยังตกจาก focus selector จึงมีโอกาสเกิด light/native patches ต่างกันตาม platform style | **Low** | `src/agent_takkub/cockpit_theme.py:339-374` | เพิ่ม `QSpinBox:focus`, style subcontrols/scrollbars และ popup container/frame; smoke-render ด้วย Windows/macOS/Linux platform styles หรืออย่างน้อย Fusion + native Windows |
| **Fallback font ระบุชื่อ Windows เท่านั้น** — bundle path ใช้ `pathlib` และ package data ถูกต้อง แต่เมื่อ TTF หาย/เสีย `Segoe UI`/`Cascadia Mono` ไม่ใช่ fallback ที่มีจริงบน macOS/Linux; Qt จะ substitute แบบไม่ควบคุมและ flag บอกเพียง bundled false | **Low** | `src/agent_takkub/cockpit_theme.py:103-107,139-166` | ใช้ `QFontDatabase.systemFont()`/generic family หรือเลือก candidate list ตาม availability แทน hardcode ชื่อ Windows; คืน resolved family จริงเพื่อ logging/diagnostic |

## Wiring / correctness ที่ตรวจแล้ว

- Provider toggle **ต่อจริงแบบ staged**: อ่าน `provider_state.is_disabled()`, สร้าง
  `pending_provider_disabled`, แล้ว `user_actions._on_team_chip_clicked()` route ค่าที่เปลี่ยน
  เข้า `orchestrator.toggle_provider()` หลัง dialog Accepted.
- Role provider combo และ role enable toggle persist จริงผ่าน
  `provider_config.save_role_overrides()` และ `pipeline_config.save()` ตามลำดับ
  (แต่มี overwrite/atomicity gaps ตาม findings ด้านบน).
- New Role เรียก `custom_roles.create_role()` จริงและ register เข้า `roles_mod` ใน process
  ทันทีเมื่อสำเร็จ.
- ลำดับ `VIEW_*` ตรงกับลำดับ `QStackedWidget.addWidget()` ทั้ง 7 หน้า; lambda จับ
  `view_idx`/swatch color ด้วย default argument ถูกต้อง และไม่พบ signal/slot หรือ parent
  ownership leak ใน Phase 1 (`setWidget()` รับ ownership ของ inner widget).
- mnemonic ที่มี `&` ใน scope นี้ถูก escape แล้ว: nav ใช้ `replace("&", "&&")` และ
  footer ใช้ `Save && Apply`; ไม่พบ button text ที่ตกหล่น.
- `QSpinBox` New Role บังคับ `QLocale.C` จึงแสดง 0-9 ASCII; font asset ใช้ `Path`
  และถูกประกาศใน package data; ไม่พบ hardcoded path separator หรือ `sys.platform` gate
  ในสองไฟล์ที่รีวิว.

## Test evidence / coverage gaps

รันแล้ว:

```text
python -m pytest -q tests/test_settings_window.py tests/test_cockpit_theme.py
........................                                                 [100%]
24 passed
```

ชุดทดสอบปัจจุบันครอบคลุม happy path ของ stack switching, create role, provider staging,
role/provider save, toggle และ font load แต่ยังไม่มี regression สำหรับ unknown/custom-role
override preservation, footer Save จาก New Role, cross-view dirty/reset, partial write/rollback,
provider apply I/O failure และ visual state ของ disabled/focused toggle.
