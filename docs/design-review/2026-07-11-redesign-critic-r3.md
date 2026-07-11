---
date: 2026-07-11
project: agent-takkub
reviewer: critic (source-verified — screen locked, Win32 capture unavailable)
view: Settings redesign (new) — R3 checklist verification
shots: []
---

# UI review · agent-takkub · Settings redesign R3

## 📸 Scope
รอบ R3 คือ post-polish + post-swap (default = new UI) verification ของ 5 ข้อ checklist บนหน้า **Settings (new)** ที่เปิด standalone ด้วย `python -m agent_takkub.settings_management`.

> ⚠️ **Capture blocked:** เครื่องถูก **LOCK** ตอนรีวิว — ทั้ง `fullscreen.ps1` และ `capwin.ps1` คืน Windows lock screen (Spotlight + นาฬิกาเดินจริง 17:26→17:27) และ Win32 `SetForegroundWindow`/click ไปไม่ถึงแอป (secure desktop). แจ้ง Lead แล้ว. **ผมจึง verify ทั้ง 5 ข้อผ่าน source code แทน** ซึ่งสำหรับ checklist ชุดนี้ (locale, widget placement, notice branch) เป็นหลักฐานที่ **authoritative กว่า pixel** — แต่ item ที่เป็น visual pure (เช่น elide จริงตากว้าง, สีจริง) ควร re-capture ยืนยันอีกครั้งเมื่อ unlock.

**Verdict สรุป:** 3 PASS · 1 warning (med) · **1 blocker (high)** → done --fail

---

## ✅ ของดีที่ควรเก็บไว้
- **Danger zone ย้ายมาอยู่ระดับ detail (ใต้ tabs) เหมือนกัน 4 entity** — Roles/Skills/MCP/Plugins วาง `DangerZone` ที่ `detail_layout` ใต้ `tabs` เป๊ะตำแหน่งเดียวกัน (`roles_page.py:77`, `skills_page.py:69`, `mcp_page.py:80`, `plugins_page.py:74`). Delete หาได้ที่เดิมทุกหน้า — consistency ตาม SPEC สำเร็จ.
- **DangerZone.set_plan(None) ซ่อน widget ทั้งก้อน** (`danger_zone.py:48 setVisible(False)`) — entity ที่ลบไม่ได้ (built-in role/managed MCP) ไม่มีปุ่มตายด้านโผล่ (R1 blocker แก้แล้ว ยืนยันคงอยู่).
- **Message box ทุกจุดใช้ `theme.themed_message_box`** — 0 raw `QMessageBox()` ใน settings_management ทั้ง module (draft-guard, delete-confirm, error) → ไม่มี native light dialog หลุด (R1/R2 carryover แก้แล้ว).
- **List row elide + tooltip ครบ** (`entity_list.py:69-70` ScrollBarAlwaysOff + ElideRight, `:86` setToolTip ทุก row) — shared widget → Skills/Plugins ได้ฟรีทันที.

---

## 🔧 ปรับ (findings)

### 🚩 [BLOCKER] item 5 — Lead ใน Access ไม่มี capability-loss notice
- **Lead provider dropdown เปิดได้ แต่ note ว่างเปล่า — ไม่เตือนว่าจะเสีย mobile mirror / `--resume` / remote-control history / JSONL token meter** — หลัง #101 lead ถูกถอดออกจาก `FORCED_ROLES` แล้ว (`provider_config.py:57-63` — เหลือแค่ codex/gemini) → `provider_forced=False` สำหรับ lead. แต่ `_populate_access` (`roles_page.py:214-217`) เขียน note **เฉพาะเมื่อ `provider_forced` เป็น True** ("Provider fixed by cockpit infrastructure") — เมื่อ False → `setText("")` = **dropdown เปล่าไม่มี warning เลย**. Capability loss ถูก document ไว้ชัดใน `provider_config.py:12-17` (+ `docs/reviews/2026-07-11-101-lead-unlock.md`) แต่ **UI ไม่ surface อะไรเลย** → user สลับ lead ออกจาก claude โดยไม่รู้ว่าเสียอะไร. นี่คือ "dropdown เปล่า" ที่ task ระบุว่า = finding. *impact: high*

  **Spec ให้ frontend:** ใน `roles_page._populate_access()` เพิ่ม branch: เมื่อ role เป็น lead (หรือ generic: role ที่ provider override ได้ + มี provider-specific capability) และ provider ที่เลือก ≠ claude → set `provider_note` เป็นข้อความ warning + สี warn token (amber `STATE_WARN_ALT` #f59e0b หรือ clay token ที่ใช้กับ meter):
  > "⚠️ Lead ที่ไม่ใช่ claude จะเสีย: mobile mirror · `--resume`/remote-control history · JSONL token meter (degraded mode #101)"

  ควร reactive ตาม `provider_combo.currentIndexChanged` ด้วย (เตือนสด ๆ ตอนเลือก ไม่ใช่แค่ตอน load). แหล่ง capability list = `provider_config.py:12-17` docstring — อย่า hardcode ซ้ำ, ดึงจาก provider_spec/ProviderCapabilities ถ้าทำได้ (multi-provider #103).

### [MED] item 4 — ปุ่ม 'Open legacy settings' เป็น no-op ใน standalone
- **ปุ่ม 'Open legacy settings' ใต้ sidebar มีจริง แต่ในโหมด standalone (`python -m ...` ตาม task) กดแล้วไม่เกิดอะไร** — `window.py:67` ตั้ง default `self.open_legacy_requested = lambda: None` (no-op). cockpit จริง wire ไว้ที่ `user_actions.py:340` (`_open_legacy_settings_window(VIEW_PROVIDERS_ROLES)`) — **แต่ `__main__.py` ไม่ wire** → ใน standalone harness ปุ่มตายสนิท ไม่มี feedback. checklist item 4 "กดแล้วเปิด window เก่าจริง" **verify ผ่าน standalone ไม่ได้** + user ที่รัน standalone เจอปุ่มหลอก. *impact: med*

  **Spec ให้ frontend:** `__main__.py` ควรอย่างใดอย่างหนึ่ง — (ก) wire `window.open_legacy_requested` ให้เปิด legacy `SettingsWindow` จริง (import + show) หรือ (ข) ถ้า standalone ไม่มี legacy context → `window._legacy_link.setVisible(False)` หรือ disable + tooltip "available inside cockpit only". ตอนนี้ปล่อยเป็นปุ่มเปิดใช้งานได้แต่ไม่ทำงาน = แย่สุดในสามทาง.

### [LOW] item 1 — Providers เป็นข้อยกเว้นโครงสร้าง (ไม่มี tabs/danger-zone)
- **Providers page ไม่มีทั้ง `QTabWidget` และ `DangerZone`** — เป็น single-scroll (`providers_page.py`: header → required_note → footer). ต่างจากอีก 4 entity ที่เป็น tabs + danger-zone ใต้ tabs. เหตุผลสมเหตุผล (provider เป็น spec-entity ลบไม่ได้ → ไม่มี danger-zone; config น้อย → ไม่ต้อง tabs) แต่ทำให้ "เหมือนกันทั้ง 5 entity" ไม่ 100%. ยอมรับได้ แต่ถ้าอยากให้ layout rhythm ต่อเนื่อง อาจห่อ providers ใน tab เดียว ("General") ให้ header/tab-strip alignment ตรงกับหน้าอื่น. *impact: low*

---

## ➕ เพิ่ม (nice-to-have, ไม่ block)
- **Reactive capability warning** (ดู blocker item 5) — จริง ๆ ควรเป็น pattern กลาง: provider dropdown ใด ๆ ที่เลือก provider ที่มี `spec_capabilities` ต่างจาก claude → auto-warn diff. ทำครั้งเดียวใช้ได้ทั้ง Roles + future. *impact: med*

## ➖ ลบ
- (ไม่มี — R3 ไม่พบ visual noise ใหม่จาก source; ต้อง re-capture ยืนยันเมื่อ unlock)

---

## 🚩 Heuristic violations (Nielsen)
- **#1 Visibility of system status** + **#5 Error prevention** — item 5: เปลี่ยน Lead provider เป็น action ที่ทำลาย capability เงียบ ๆ ไม่มี feedback/guard. ต้องมี notice (prevention) ก่อน commit.
- **#1 Visibility** — item 4: ปุ่มที่กดแล้วไม่มี response = ระบบไม่บอกสถานะ (ใน standalone).

---

## 🎯 Recommended next steps (สำหรับ Lead)
1. **[high]** delegate frontend แก้ item 5 — เพิ่ม capability-loss notice ใน `roles_page._populate_access()` (reactive ตาม provider combo) — **นี่คือเหตุผลที่ done --fail**.
2. **[med]** delegate frontend แก้ item 4 — `__main__.py` wire หรือ hide `_legacy_link` ใน standalone.
3. **[low]** พิจารณา wrap Providers ใน tab เดียวให้ layout rhythm ตรงหน้าอื่น (optional).
4. **[env]** ขอ user **unlock เครื่อง** แล้วให้ critic capture R3 จริงยืนยัน visual (elide/สี/spacing) — source verify ครบแล้วแต่ pixel ยังไม่ได้เก็บ.

> ✅ item 1 (danger-zone), 2 (elide+tooltip), 3 (spinbox locale + no native dialog) = **PASS ระดับ source**.
