# Wave B — `takkub migrate apply` ครั้งแรกบน prod จริง (epic #309)

วันที่: 2026-08-23 · เครื่อง: Windows 11, prod install `C:\Users\monch\.agent-takkub` (v1.0.86)
อ้างอิงแผน: `docs/v2/2.0.0-migration-plan.md` §2.3 (apply sequence) · §3 Wave B

## สรุป

**สำเร็จ** — 8/8 step ใน `apply` และ `validate`, `doctor --storage-layout` = `mixed` ตามเกณฑ์
ไม่มี rollback, ไม่มี V1 ไฟล์ไหนถูกลบหรือแก้

## เป้าหมาย

`shape = installed_merged` (`SETTINGS_HOME == DATA_HOME == C:\Users\monch\.agent-takkub`) —
**ไม่ใช่** dev checkout (ซึ่งเป็น `dev_split`: `DATA_HOME = <repo>`, `SETTINGS_HOME = ~/.takkub`)
การซ้อมทั้งหมดก่อนหน้านี้ รวม dry-run ของ phase 8b §8b-4 ทำบน `dev_split` — รอบนี้เป็นครั้งแรก
ที่ ladder แตะ `installed_merged` ของจริง

## Pre-flight (§2.3 ข้อ 1)

| ตรวจ | ผล |
|---|---|
| `doctor --storage-layout` | `state: v1` · 7 ladder step not yet applied |
| `migrate inspect` | 8/8 ✓ |
| `migrate plan` | 8/8 ✓ |
| `migrate dry-run` | 8/8 ✓ (no disk writes) |

### ตัวเลขต่างจาก baseline §8b-4 — สอบแล้วทั้ง 4 จุด ไม่ใช่ regression

baseline วัดบน `dev_split` (worktree `DATA_HOME` + `~/.takkub`) รอบนี้วัดบน `installed_merged`
คนละบ้าน ตัวเลขจึงต้องต่าง:

| step | baseline (dev_split) | prod (installed_merged) | เหตุผล |
|---|---|---|---|
| credential-reference | 1 provider | **3 provider** | codex/opencode isolation เป็น installed-build-only by design (`provider_home_env` คืน `{}` บน dev) — เอกสาร §8b-4 ระบุไว้เอง |
| project | 0/0 | **21 project** | worktree ไม่มี `projects.json`, prod มี |
| runtime-triage | 0/0 | **4 state dir** | worktree ไม่มี `runtime/` ของจริง |
| capability | 2/2 | **1/2 source** | prod ไม่มี `skill-policy.json` (ไม่เคยตั้ง skill policy) — เคส source-absent ที่ `RegistryCopyStep` รองรับอยู่แล้ว |

### Backup (§2.3 ข้อ 1, "manual backup outside the tool entirely")

`~/.agent-takkub` ทั้งก้อน = **~53 GB** ซึ่งสำเนาทั้งหมดไม่สมเหตุผล จึง backup แบบเลือก:

| ส่วน | ขนาด | backup |
|---|---|---|
| `worktrees/` | 41.1 GB | ✗ git สร้างใหม่ได้ + fixture test พิสูจน์แล้วว่า ladder ไม่คัดลอก worktree |
| `venv/` | 685 MB | ✗ ติดตั้งใหม่ได้ |
| `graft-graphs` + `graft-staging` | 2.55 GB | ✗ build output ไม่เกี่ยวกับ ladder |
| `runtime/browser-profiles` + `runtime/exports` | 2.49 GB | ✗ `runtime-triage` จัดเป็น cache/junk เอง (classified only, never touched) |
| `claude-config/` | 5.9 GB | ✓ |
| `runtime/{sessions,tasks,role-memory,knowledge,core,...}` | ~721 MB | ✓ (4 ใน 5 นี้คือ state dir ที่ ladder copy) |
| **root `*.json`** | **76.6 KB** | ✓ ← ของจริงที่ ladder อ่านทั้งหมด |

ผล: `C:\Users\monch\.agent-takkub-backup-20260823` — **36,446 ไฟล์ / 6.78 GB / FAILED = 0**
ตรวจสำเนา: root json 15/15, `runtime/sessions` 46/46, `projects.json`/`provider-models.json`/
`role-models.json`/`role-providers.json`/`pane-tools.json` ครบ

> **robocopy exit code 1 = สำเร็จ** (แปลว่า "มีไฟล์ถูกคัดลอก") ไม่ใช่ error — ตัว wrapper ที่รัน
> มัน mark เป็น failed ต้องอ่านตาราง Total/Copied/FAILED เอง

### ปิด cockpit (§2.3 ข้อ 1 ข้อสุดท้าย)

ปิด prod cockpit แบบ graceful (`CloseMainWindow()` ไม่ใช่ kill) แล้วยืนยัน 4 ชั้น:
process ตัวแอปหาย · launcher stub หายตาม · port ปล่อยแล้ว · `projects.json` นิ่ง 10 วินาที

dev cockpit ยังรันต่อระหว่าง apply **โดยตั้งใจ** (Lead pane ที่คุม migration อยู่ในนั้น) —
ยืนยันแล้วว่ามันเขียนคนละบ้าน: `DEV DATA_HOME/projects.json` = `<repo>/projects.json`,
`DEV SETTINGS_HOME/projects.json` ไม่มีไฟล์ด้วยซ้ำ ส่วนเป้าหมาย migrate คือ
`~/.agent-takkub/projects.json` — คนละไฟล์คนละบ้าน

## Apply + validate (§2.3 ข้อ 2-3)

```
apply     ✓ version-marker (app = 1.0.86) · readonly-registries 5 · role-agent (registry+routing, 0 role file)
          ✓ capability 2 · project (registry + 21) · state 4 · credential-reference 3 provider · runtime-triage 4 dir
validate  ✓ ทั้ง 8 step "match V1 source"
```

## Success criteria (§2.3 ข้อ 4)

| เกณฑ์ | ผล |
|---|---|
| ทุก step ใน `apply` `ok: true` | ✓ 8/8 |
| `validate` `ok: true` ทุก step | ✓ 8/8 |
| `doctor --storage-layout` = **`mixed`** + legacy-leftover | ✓ (`mixed` คือสำเร็จ ไม่ใช่ `v2` — copy-never-move ทำให้ V1 ยังอยู่) |
| cockpit เปิดปกติ + spawn→assign→done | **⚠ ยังไม่ปิดเกณฑ์** — cockpit เปิดปกติ (`doctor` 40 ok / 0 fail, port 52161) แต่ round trip ยังไม่ได้ทำ: `takkub assign` จาก session นี้โดน `err: unauthorized: lead-only command` เพราะ Lead pane อยู่ใน cockpit **dev** ไม่ใช่ prod (guard ทำงานถูก ไม่ใช่บั๊ก) ต้องเปิด Lead pane ในหน้าต่าง prod แล้วสั่งงาน 1 ใบ |
| full `takkub qa-gate` | ✓ PASS (`docs/qa/2026-08-23-093426-qa-gate.md`) |

## Cross-check ที่ทำเพิ่มเอง (ไม่ได้อยู่ในแผน)

- `v2/` = **696.9 MB** ทั้งหมดอยู่ใน `state/` — ตรงกับที่ประเมินจากขนาด state dir ก่อน apply (~695 MB)
- `projects.json` V1 มี 3 entry, `v2/projects/registry.json` มี 3 entry, per-project dir 21 อัน
- **Wave A's model registry ได้ทดสอบกับข้อมูลจริงครั้งแรก** (แผน §1.3 บอกเองว่างานนี้
  "cannot be meaningfully finished before `migrate apply` runs at least once"):
  ```
  read_legacy_models()         -> [('codex:gpt-5.6-terra', 'codex', 'gpt-5.6-terra')]
  read_legacy_model_profiles() -> 8 profile (backend/critic/devops/frontend/lead/mobile/qa/reviewer)
  ```
  composite id ที่แก้ไปเมื่อวันเดียวกัน (กัน 2 provider ชี้ model เดียวกันแล้วทับกัน) ทำงานถูกบนข้อมูลจริง

## Rollback readiness

ยังกลับได้ทุกเมื่อ: V1 ครบทุกไฟล์ (copy-never-move) · `takkub migrate rollback` (ลบ `v2/` root
ทั้งก้อนเมื่อทุก step คืนสำเร็จ — แก้ไปใน #350) · backup 6.78 GB ที่ `.agent-takkub-backup-20260823`

## ถัดไป

Wave C ปลดล็อกแล้ว (แผน §3): model registry resolver wiring เข้า `core/routing/` (ตอนนี้
`models/registry.json` มีข้อมูลจริงแล้ว) และ PermissionEngine rewire — แผนกำหนดให้ **แยก release
window จาก apply** เพื่อให้ regression แยกที่มาได้ · macOS ยังต้องทำ §2.3 ซ้ำอีกเครื่อง (§2.5)
