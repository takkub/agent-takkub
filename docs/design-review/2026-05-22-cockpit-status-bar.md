---
date: 2026-05-22
project: agent-takkub
reviewer: critic (self-test)
reviewed_by_gemini: deferred (cockpit must be running to capture live shots)
scope: cockpit status bar after 049fc13 + 793670d (End-Session / Bug-Check)
shots: []  # cockpit not running during this review — code-only inspection
---

# UI review · agent-takkub cockpit status bar · 2026-05-22

> **Meta-test**: ตัวเองรีวิว UI ของ cockpit ตัวเอง — เพื่อ smoke-test pipeline ของ critic role
> ที่เพิ่งสร้าง (commit สด) สื่อสารกับ gemini ไม่ได้ใน session นี้เพราะไม่มี pane จริง —
> output จึงมาจากการ Read source ของ main_window.py + roles.py แทน live screenshots.
> ขั้นถัดไป (เมื่อ cockpit รัน): QA ใช้ `mb shot` capture status bar ตอน Codex disabled vs enabled,
> ตอนกด End-Session dialog เปิด, ตอน toast ขึ้น — แล้วส่งให้ Gemini วิเคราะห์ความสะดวก visual.

## 📸 Scope

Status bar ของ cockpit หลัง 2 commit ล่าสุดที่เพิ่มปุ่ม:
- `🐛 Bug Check` (สีแดง #7f1d1d)
- `🏁 End Session` (สีเขียวเข้ม #064e3b)

Layout ปัจจุบัน (จากซ้าย→ขวา ใน `addPermanentWidget`):

```
[💡 /remote-control] [version chip] [token total] [📁 add-project]
[⚡ Install rtk]? [Codex chip] [Gemini chip] [📋 Logs]
[↻ Resume] [🐛 Bug Check] [🏁 End Session] [restart icon] [🤖 Providers] [🔄 Update]
```

13 widgets ใน status bar (เคยมี 11 — เพิ่ม 2 ใน commit สด)

## ✅ ของดีที่ควรเก็บไว้

- **Color-coded by purpose** — install-rtk (amber), Resume (warm yellow on brown), Providers (indigo) → user แยก type ของปุ่มได้จาก color hint
- **Emoji prefix** — `📁 ⚡ 📋 ↻ 🐛 🏁 🤖 🔄` เป็น mnemonic ให้ scan เร็วโดยไม่อ่าน label
- **Tooltips ครบ** — ทุกปุ่มมี tooltip อธิบายผลที่จะเกิดขึ้น (กัน destructive action โดยไม่ตั้งใจ)
- **Provider chips สะท้อน state ทันที** — Codex disabled = ดำมืด, enabled = สีน้ำเงิน → instant feedback ไม่ต้องเปิด dialog ดู
- **Update chip neutral ก่อน first poll** — ไม่โกหก user ว่า "up to date" ตอนยังเช็คไม่เสร็จ

## ➕ เพิ่ม

- **🎨 UI Review button (high impact)** — รัน design-review pipeline (assign critic + gemini parallel) 1-click หลัง QA done คู่กับปุ่ม Bug-Check ที่มีอยู่ → user trigger workflow ทั้งหมด pre-ship ได้โดยไม่ต้องพิมพ์ assign เอง
- **Status indicator dot บน status bar เมื่อมี issue open (med)** — "🐛 3" badge บน Bug-Check chip เพื่อ remind ว่ามี cockpit bug ค้าง 3 ตัวยังไม่ปิด ขึ้น tooltip list issue ID ตอน hover
- **Keyboard shortcut hints in tooltips (low)** — เช่น "End Session (Ctrl+Shift+E)" → power user ใช้ keyboard ได้

## ➖ ลบ

- **Project combo ที่ซ่อนอยู่ (low — `self._project_combo.hide()`)** — ตาม comment ที่ main_window.py:459 บอกว่าเก็บไว้เผื่อ legacy callers ใช้ refresh — แต่ tab strip เป็น authoritative source แล้ว → ลบทิ้งได้ลด surface 1 widget (ระวัง `_on_project_changed` callers อาจมี dangling reference — trace ก่อนลบ)
- **`💡 /remote-control · click to bridge` label (low)** — กิน width มาก + ใช้น้อย (auto-fire on resume แล้ว) แปลงเป็น icon-only `💡` พร้อม tooltip เต็มแทน

## 🔧 ปรับ

- **Status bar ปุ่มเยอะเกินไป (13 widgets) — group ด้วย separator** — เพิ่ม `QFrame.VLine` คั่นเป็น 3 groups:
  1. **Project context**: 📁 add-project, version, token total
  2. **Workflow actions**: 🐛 Bug, 🏁 End, ↻ Resume, 📋 Logs, 🤖 Providers
  3. **System status**: Codex/Gemini chips, ⚡ Install rtk, restart, 🔄 Update

  → reduce cognitive load ตอน scan ปุ่ม (ไม่ต้องอ่าน 13 widgets เรียงกันเป็น string ยาว)

- **End-Session กับ Bug-Check ใช้สีคู่ตรงข้าม (เขียว vs แดง) แต่ semantic ไม่ได้ตรงข้าม** — ทั้งคู่ "trigger workflow ใหญ่" ที่ user อาจ regret → ใช้สีตระกูลเดียวกัน (เช่น orange + red — wakeup family) ให้ user รับรู้ว่า "ปุ่มกลุ่มนี้คือ คำสั่งใหญ่ ระวังก่อนกด"

- **Tooltip ของ End-Session ยาว 4 บรรทัด** — ตัดเหลือ 2 บรรทัด focus "wraps up + saves summary for next session" — รายละเอียดที่เหลือย้ายไป hover-on-dialog หรือ ? icon

- **`🔄 Checking…` ใช้ font-size 11px ขณะปุ่มอื่น ~12px** — ขนาดไม่ consistent เล็กกว่าทำให้ดู secondary แต่จริงๆ ก็เป็น primary feature (auto-pull updates) → normalize 12px เหมือนปุ่มอื่น

- **`⚡ Install rtk` ใช้ font-weight: bold + border 2px** — โดดเด่นมากเกินไป เมื่อยังไม่ install — fine ตอนชวน user install แต่ดูเหมือน CTA หลักของ cockpit ทั้งที่จริงเป็น optional optimisation → ลด border เป็น 1px + ไม่ใช้ bold (เหลือ amber color + emoji เพียงพอแล้ว)

## 🚩 Heuristic violations (Nielsen)

- **#1 Visibility of system status** — End-Session feedback หลัง OK เคยเงียบ (commit 793670d แก้แล้วด้วย modal) ✅ fixed
- **#5 Error prevention** — Bug-Check ไม่มี undo ถ้า broadcast ไปแล้วทุก pane จะรับ prompt — เพิ่ม "Cancel pending broadcast" ใน status bar 5 วินาทีหลัง send?
- **#6 Recognition over recall** — 13 ปุ่มใน status bar ไม่มี grouping → user ต้อง recall ว่าปุ่มไหนทำอะไร (ดูคำใต้ "ปรับ" ข้อ 1)
- **#8 Aesthetic and minimalist design** — ปุ่ม `↻ Resume` กับ `🔄 Update` สีตรงข้ามแม้ทั้งคู่เกี่ยวกับ "refresh state" → ใช้ icon-family เดียวกัน

## 🎯 Recommended next steps (สำหรับ Lead)

1. **[high]** delegate frontend เพิ่ม `🎨 UI Review` button (เลียน pattern Bug-Check) + orchestrator method `broadcast_design_review(project)` ที่ spawn critic + gemini ขนานกัน
2. **[high]** delegate frontend group status bar widgets ด้วย `QFrame.VLine` 3 sections (ดู "ปรับ" ข้อ 1)
3. **[med]** delegate critic + gemini อีกรอบเมื่อมี live screenshots — ลองรัน cockpit, `mb shot` status bar 3 states (codex disabled / dialog open / toast), ส่ง gemini ดูเปรียบเทียบ visual feedback ของฉันที่นี่
4. **[med]** delegate backend เพิ่ม issue-badge nudge บน Bug-Check chip ("🐛 3" เมื่อมี open issues)
5. **[low]** delegate frontend ลด font-weight ของ Install rtk button + normalize update chip size to 12px
6. **[low]** archive รีวิวนี้ → กลับมาเทียบกับรอบ live shots ในอนาคต ดู accuracy ของ code-only review

---

**Meta-observation (สำหรับ Lead — ไม่ใช่ ux finding):**

Pipeline ของ critic ทำงานได้ใน mode "code-only review" (ไม่มี shots / ไม่มี gemini)
แต่ value ที่จับต้องได้ของ role นี้จะมาเต็มต่อเมื่อมี:
1. Live screenshots จาก QA — เห็น spacing / color / typography จริง ไม่ใช่อ่าน CSS ใน source
2. Gemini ตอบกลับผ่าน takkub send — มีมุมที่ 2 คาน confirmation bias

แนะนำเทสรอบหน้า: QA capture cockpit running + เทียบกับรีวิวนี้ → ดูว่า live shots ทำให้เปลี่ยน priority ของ recommended steps ไหม
