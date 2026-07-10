# 📋 Task Ledger — agent-takkub

> สารบัญงานทั้งหมด · เปิดไฟล์เดียวเห็นว่า **สั่งอะไร · ใครทำ · เสร็จยัง** · คลิกชื่อไฟล์อ่าน detail เต็ม
> สถานะ: `[ ]` รอคิว · `[~]` กำลังทำ · `[x]` เสร็จ · `[!]` FAILED · `[-]` ปิด/ยกเลิก

---

## 📅 2026-07-10 13:30 — 🎯 เป้าหมาย: Roadmap re-audit + dev (A1–A7 · B1–B4)

`progress: 6/9 เสร็จ · 1 กำลังทำ · 2 รอคิว`

### ✅ 1. Audit ทั้ง roadmap (3-model cross-check)
- [x] `12:40` **codex**  · agent-takkub · cross-check A1–B4 + เจาะ A3 root cause → [124005-codex.md](2026-07-10/124005-codex.md) — ✅ done `13:05`
- [x] `12:40` **gemini** · agent-takkub · มุมที่ 3 + design A6/A7 outline       → [124007-gemini.md](2026-07-10/124007-gemini.md) — ✅ done `13:02`

### 🔨 2. A3 — แก้ Lead draft-hold หลุด (พิมพ์ค้างโดนส่ง)
- [~] `13:30` **backend** · agent-takkub · แยก 'draft ค้างจริง' ออกจาก 'not-ready' — ห้าม 60s force-flush ทับ draft → [133012-backend.md](2026-07-10/133012-backend.md) — ⏳ กำลังทำ

### ⏳ 3. A7 — task ledger (ตัวที่คุณกำลังดูอยู่นี่)
- [ ] `—` **backend** · agent-takkub · `task_ledger.py` เขียนทุก assign + gen INDEX.md + flip ✅ ตอน done → *รอคิว (ต่อจาก A3)*

### ⏳ 4. A6 — role/skill manager UI (net-new)
- [ ] `—` **backend**  · agent-takkub · create-role dialog + `~/.takkub/custom-roles.json` → *รอ wave*
- [ ] `—` **frontend** · agent-takkub · skill catalog tab + default tool assignment       → *รอ wave*

### ⏳ 5. B2 — per-option picker ฝั่ง web
- [ ] `—` **backend**  · agent-takkub · `notify.py` ส่ง AskUserQuestion options ไป PWA (เลิก strip) → *รอ wave*
- [ ] `—` **frontend** · agent-takkub · render tappable option chips + ส่งกลับ                      → *รอ wave*

### ⏱ verify (ปุ่มจบ — รันหลัง A3+A7 เสร็จ)
- [ ] `—` **qa** · agent-takkub · targeted → full suite gate (Win+Mac) → *รอ dev เสร็จ*

---

## 📅 2026-07-09 (session ก่อน) — 🎯 เป้าหมาย: core-upgrade #1–#6
### ✅ งานที่ยืนยันเสร็จ (audit รอบนี้)
- [x] A1 file-based delivery      → `orchestrator_text.py:434`
- [x] A2 remove fanout cap (code) → `lead_context.py:467`
- [x] A5 screenshot→lead evidence → `orchestrator.py:1591`
- [x] B1 close project (web)      → `app.js:405`
- [x] B3 resume + session (web)   → `app.js:1174`
- [x] B4 Pulse shows Lead         → `api.py:91`

### [-] ยกเลิก
- [-] A4 `/remote-control` auto-bridge — user สั่งตัดทิ้ง (ลบหมดแล้ว `28136df`)

---

### ตัวอย่างเคส FAILED (กันงานหลุด — ไม่หายเงียบ)
> ถ้า qa verify ไม่ผ่าน จะขึ้นแบบนี้ ไม่ใช่หายไป:
- [!] `14:05` **qa** · agent-takkub · smoke /login — **FAILED**: invalid-creds ไม่ขึ้น error toast → [140501-qa.md](2026-07-10/140501-qa.md) — ❌ `14:12` · รอ fix loop
