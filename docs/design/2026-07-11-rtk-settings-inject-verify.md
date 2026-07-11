# Verification: ย้าย rtk PreToolUse hook ออกจาก `<project>/.claude/settings.json` → inject ตอน spawn ผ่าน `--settings`

- **วันที่:** 2026-07-11
- **โดย:** maintainer
- **Audit item:** 3.5-3 (`docs/design/2026-07-11-central-home-audit.md`)
- **claude CLI:** 2.1.207
- **OS ทดสอบ:** Windows 11 (ConPTY host) — *ยังไม่ได้ทดสอบ macOS ดู §Cross-platform gap*
- **ขอบเขต:** ทดลองจริงล้วน ไม่แตะ `src/` (ยังเป็นช่วงพิสูจน์)

---

## คำถามที่ต้องตอบ

claude CLI รวม `PreToolUse` hook จากไฟล์ settings ที่ส่งผ่าน `--settings <path>` (ไฟล์อยู่**นอก** project)
แล้ว**ยิง hook จริงตอนรัน Bash tool**ไหม? — ถ้าจริง เราจะย้าย rtk hook ออกจาก
`<project>/.claude/settings.json` ไป inject ตอน spawn แทน (rtk = personal, user ยืนยันว่าไม่ commit)

---

## สรุปคำตอบ (TL;DR)

| ประเด็น | ผล |
|---|---|
| external `--settings` PreToolUse/Bash hook ยิงตอน Bash tool จริงไหม | ✅ **ยิง** (Test A) |
| merge กับ `<project>/.claude/settings.json` ของ user โดยไม่ทับกันไหม | ✅ **merge — ทั้งคู่ยิง, env project layer อยู่ครบ** (Test B) |
| ใส่ `--settings` **2 อัน** แล้ว hook รวมกันไหม | ❌ **ไม่รวม — อันหลังทับ `hooks` object ของอันแรกทั้งก้อน** (Test C / C2) |

> **ใช้ได้ ✅ แต่มีเงื่อนไขบังคับ 1 ข้อ:** cockpit ใช้ `--settings` slot ไปแล้ว 1 อัน (`hook_wiring.py` → Stop/Notification/SessionStart)
> **ห้ามเพิ่ม `--settings` อันที่ 2 สำหรับ rtk** เพราะอันหลังจะลบ hook เดิมทั้งหมด →
> **ต้อง merge rtk PreToolUse เข้าไฟล์ `hook-settings.json` เดียวกัน** (แก้ `_HOOK_SETTINGS` dict ใน `hook_wiring.py`)

**flag ชุดจริงที่ pane ใช้** (ตรวจจาก `spawn_engine.py:1532-1549`):
`--dangerously-skip-permissions --setting-sources project,local --settings <hook-settings.json>` — ทุก test ใช้ชุดนี้เป๊ะ

---

## Test A — external `--settings` hook ยิงตอน Bash tool ไหม (project ไม่มี `.claude/settings.json`)

**Setup:** `proj_a/` ว่างเปล่า (ไม่มี `.claude/`), settings ไฟล์นอก project มี PreToolUse/matcher=Bash hook
ที่ `echo` marker ลงไฟล์ temp

settings/ext_settings_a.json (scratch):
```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "echo EXT_HOOK_FIRED > .../marker_a.txt" } ] }
    ]
  }
}
```

**คำสั่ง:**
```bash
cd <scratch>/proj_a && claude -p "Use the Bash tool to run exactly this one command: echo hello123. Do nothing else after." \
  --dangerously-skip-permissions --setting-sources project,local --settings <scratch>/settings/ext_settings_a.json
```

**Output จริง:**
```
hello123
=== EXIT: 0 ===
--- marker_a.txt: ---
EXT_HOOK_FIRED
```

**ผล: ✅ PASS** — hook ยิง (marker ถูกสร้าง) แม้ไฟล์ settings อยู่นอก project และ project ไม่มี `.claude/` เลย

---

## Test B — merge กับ `<project>/.claude/settings.json` ของ user (ต้องไม่ทับ)

**Setup:** proj_b/.claude/settings.json (scratch) = settings ของ user เอง มี **key อื่น (`env`)** + **PreToolUse hook ของตัวเอง** (marker คนละไฟล์)
รันด้วย external settings (marker_a) ตัวเดิม → ถ้า merge จริง **ทั้ง 2 hook ต้องยิง**

proj_b/.claude/settings.json (scratch):
```json
{
  "env": { "TAKKUB_VERIFY_PROJECT_ENV": "project_layer_alive" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "echo PROJECT_HOOK_FIRED > .../marker_b_project.txt" } ] }
    ]
  }
}
```

**คำสั่ง:** (external `--settings` = `ext_settings_a.json` เหมือน Test A, cwd = `proj_b`)
```bash
cd <scratch>/proj_b && claude -p "Use the Bash tool to run exactly this one command: echo hello_b_456. Do nothing else after." \
  --dangerously-skip-permissions --setting-sources project,local --settings <scratch>/settings/ext_settings_a.json
```

**Output จริง:**
```
hello_b_456
=== EXIT: 0 ===
--- marker_a.txt (external hook): ---
EXT_HOOK_FIRED
--- marker_b_project.txt (project's own hook): ---
PROJECT_HOOK_FIRED
```

**ผล: ✅ PASS** — **ทั้ง 2 marker ยิง** → `--settings` (นอก project) **merge** กับ `project` source layer
โดย hook array ต่อกัน (additive) ไม่ทับกัน · project layer ยังอยู่ครบ (env key ไม่หาย)

> ⇒ ถ้า user มี `.claude/settings.json` ของตัวเองในโปรเจค rtk hook ที่ inject ผ่าน `--settings` จะ**อยู่ร่วมกันได้** ไม่ลบของ user

---

## Test C / C2 — ⚠️ gotcha: `--settings` หลายอัน **ไม่รวมกัน** (อันหลังทับทั้งก้อน)

cockpit ใช้ `--settings` slot ไปแล้ว 1 อัน (`hook_wiring.py`) → คำถามสำคัญ: ถ้าเพิ่ม `--settings` อันที่ 2 สำหรับ rtk hook เดิมยังอยู่ไหม?

### Test C — 2 ไฟล์ ต่างมี PreToolUse คนละ marker
**คำสั่ง:** `... --settings ext_settings_a.json --settings ext_settings_c2.json`

**Output จริง:**
```
hello_c_789
=== EXIT: 0 ===
--- marker_a.txt (1st --settings):  (NOT created)
--- marker_c2.txt (2nd --settings): SECOND_SETTINGS_FIRED
```
→ อันแรกโดนทับ ยิงแค่อันหลัง

### Test C2 — ไฟล์ 1 = PreToolUse, ไฟล์ 2 = **Stop (คนละ event)**
พิสูจน์ว่าอันหลังทับ **ทั้ง `hooks` object** ไม่ใช่แค่ key เดียวกัน

**คำสั่ง:** `... --settings ext_pretooluse_only.json --settings ext_stop_only.json`

**Output จริง:**
```
hello_granularity
=== EXIT: 0 ===
--- marker_first_pretool.txt (1st file, PreToolUse):  (NOT created)
--- marker_second_stop.txt (2nd file, Stop):          SECOND_STOP_FIRED
```

**ผล: ❌** อันที่ 2 (มีแค่ `Stop`) **ลบ `PreToolUse` ของอันแรกทิ้งทั้งหมด** — ยิงแค่ `Stop`
⇒ `--settings` หลายอัน = **last wins, ทับ `hooks` object ทั้งก้อน** ไม่ merge ระหว่างกันเอง

> **นัยสำคัญต่อ cockpit:** ถ้าเผลอเพิ่ม `--settings <rtk.json>` เป็นอันที่ 2 → จะ**ลบ Stop/Notification/SessionStart hook**
> ของ `hook_wiring.py` ทิ้ง → pane-state signal (turn-end/idle/session_uuid) พังทันที

---

## เงื่อนไข/ข้อสรุปสำหรับการ implement (ยังไม่ทำในรอบนี้ — proof only)

**ใช้ได้ ✅** ย้าย rtk PreToolUse hook ออกจาก `<project>/.claude/settings.json` ไป inject ตอน spawn ได้จริง
แต่ต้องทำแบบเดียวเท่านั้น:

1. **merge rtk PreToolUse เข้า `_HOOK_SETTINGS` dict ใน `hook_wiring.py`** (ไฟล์ `hook-settings.json` เดียวกัน)
   — เพิ่ม key `"PreToolUse"` ข้างๆ `Stop`/`Notification`/`SessionStart` ที่มีอยู่
   **ห้าม** เพิ่ม `--settings` อันที่ 2 (Test C2 → จะลบ hook เดิมทิ้ง)
2. rtk hook จะ merge กับ `<project>/.claude/settings.json` ของ user เอง (ถ้ามี) โดยไม่ทับ (Test B)
   → ปลอดภัยกับโปรเจคที่ user มี settings ของตัวเอง
3. หลังย้าย → **rtk ไม่ต้องอยู่ใน `<project>/.claude/settings.json` อีก** = ไม่มี personal config รั่วเข้า repo
   (เดิมเป็น 1 ใน 3 จุดที่ยังเขียนลง cwd จริง — ดู central-home-audit)

### จุดที่ต้องคิดต่อตอน implement (ไม่ block ผลพิสูจน์)
- **rtk hook command เป็น personal/per-machine** (path ของ rtk binary) — `hook_wiring.py._HOOK_SETTINGS`
  เป็น static dict เดียว shared ทุก pane · ถ้า rtk command ต่างเครื่อง อาจต้อง compute path ตอน `ensure_hook_settings_file()`
  แทน literal · หรือ gate ด้วยเงื่อนไข "rtk ติดตั้งไหม" (มี `rtk_helper.py` อยู่แล้ว)
- **matcher scope:** rtk hook ปัจจุบันครอบ tool ไหนบ้าง (Bash เท่านั้น? หรือมากกว่า) — ตอน merge ต้องคง matcher เดิม

### Cross-platform gap ⚠️
- ทดสอบบน **Windows เท่านั้น** — hook command ในการทดลองใช้ `echo ... > <path>` (รันได้ทั้ง cmd/bash)
- **ยังไม่ได้ยืนยัน macOS** (`_pty_backend`) ว่า `--settings` merge เหมือนกัน — กลไกเป็น claude CLI layer เดียวกัน
  น่าจะได้ทั้ง 2 OS แต่ **ต้อง smoke ฝั่ง mac ก่อน ship** ตามกฎ cross-platform ของ repo
- rtk binary path / shell ของ hook command ต่าง OS → เป็นเรื่องของ implement ไม่ใช่กลไก `--settings`

---

## Reproduce

Scratch (ลบทิ้งได้): `<TEMP>/claude/.../scratchpad/rtk-verify/`
- `proj_a/` — project ว่าง (ไม่มี `.claude/`)
- proj_b/.claude/settings.json (scratch) — จำลอง user settings + own hook
- `settings/ext_*.json` — settings ไฟล์นอก project (marker hooks)

ทุก run ใช้ `claude -p ... --dangerously-skip-permissions --setting-sources project,local --settings <ext>`
(= flag ชุดเดียวกับ pane จริง `spawn_engine.py:1532-1549`) สั่งงานที่ trigger Bash 1 ครั้ง แล้วเช็ค marker file
