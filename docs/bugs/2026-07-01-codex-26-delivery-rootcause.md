# Root Cause + Fix Proposal — codex pane delivery/report failure (#26 family)

**Date:** 2026-07-01
**Author:** Lead (Claude Opus 4.8) — debug-mantra session
**Trigger:** stress test วันนี้ codex ดรอป **2/2 ครั้ง** (single-role ×9 + cross-idea brainstorm)
**Status:** root cause **CONFIRMED (live repro + codex official docs)** · fix validated · **ยังไม่ implement** (รอ confirm)

---

## TL;DR

`#26` ที่เห็น (`delivery-unconfirmed @45s`) **ไม่ใช่บั๊กเดียว** — เป็น **3 failure mode ซ้อนกัน** ที่มาจากคนละสาเหตุ:

| Mode | อาการ | สาเหตุ | ระดับ | Fix effort |
|---|---|---|---|---|
| **B (primary)** | codex ทำงานได้ แต่ **ค้าง "working"** เรียก `takkub done` ไม่ผ่าน | `-s workspace-write` sandbox **block loopback network** → `takkub done` (TCP 127.0.0.1) hang 15s→fail | 🔴 deterministic (macOS) | 1 บรรทัด |
| **A (secondary)** | codex pane **exit เปล่า** ไม่ทำงานเลย | codex-cli **auto-update ตอน launch → exit** ("restart Codex"), ไม่มี flag ปิด | 🟠 intermittent | medium |
| **C (tertiary)** | `delivery-unconfirmed @45s` warning เด้ง (แม้ paste ติด) | `_ready_wait_ms` ให้ 90s เฉพาะ GEMINI, codex ยัง 45s + ready-banner scroll หลุด bottom-6 rows | 🟡 cosmetic | 1 บรรทัด |

**ลำดับแนะนำ:** Mode B → C → A (B คือตัวที่ทำให้ codex ใช้ไม่ได้จริงบน macOS)

---

## Breadcrumb ledger (debug mantra step 4)

1. **Round 1 วันนี้ (9-role):** codex `delivery-unconfirmed @45s` — แต่ codex **กำลัง search `cli.py` จริง** (paste ติด) → ไม่เคย `takkub done` → ค้าง "working" → ปิดมือ. = **Mode B**
2. **Round 2 วันนี้ (cross-idea):** codex `delivery-unconfirmed @45s` **+ pane exited** ไม่ทำงานเลย. = **Mode A**
3. **Transcript session เก่า** (`runtime/sessions/.../lead-194319.transcript.log`) บันทึกไว้แล้ว: *"codex flaky — รอบแรก codex-cli auto-update ตัวเองตอน launch → exit ('restart Codex'); รอบสอง process งานได้แต่ติดตอนเรียก takkub done ในenv นี้ → ค้าง working ตัวเดียวที่ต้อง close มือ"* → **ตรง Mode A + B เป๊ะ**
4. **gemini วันนี้:** `done` ผ่านปกติ (เขียนไฟล์ + report). gemini(agy) ใช้ `--dangerously-skip-permissions` = **ไม่มี sandbox**
5. **Code:** `takkub done` → `cli.py:59` `socket.create_connection(("127.0.0.1", port), timeout=15)` = **loopback TCP, timeout 15s**
6. **Code:** codex argv (`spawn_engine.py:1015-1027`) — macOS/Linux = `-s workspace-write` (**sandbox on**); Windows = `--dangerously-bypass-approvals-and-sandbox` (**sandbox off**)
7. **Code:** `is_at_ready_prompt` codex marker = `"openai codex (v"` (`pty_session.py:224`), สแกนแค่ bottom-6 rows; `_ready_wait_ms` (`lead_inbox.py:343-352`) extend 90s **เฉพาะ GEMINI**

---

## Mode B — PRIMARY: codex ทำงานได้แต่ `takkub done` ไม่ผ่าน

### Differential (สาเหตุถูกแยกออกมาชัด)

| pane | flag | sandbox | loopback net | `takkub done` |
|---|---|---|---|---|
| claude | `--dangerously-skip-permissions` | none | ✓ | ✓ |
| gemini/agy | `--dangerously-skip-permissions` | none | ✓ | ✓ (วันนี้ done ได้) |
| **codex macOS** | `-s workspace-write` | **YES** | **✗ blocked** | **✗ hang 15s→fail** |
| codex Windows | `--dangerously-bypass-approvals-and-sandbox` | none | ✓ | ✓ |

**กลไก:** codex `-s workspace-write` = เขียนไฟล์ได้เฉพาะ cwd **แต่ปิด outbound network เป็น default** (รวม 127.0.0.1). `takkub done` เปิด TCP ไป `127.0.0.1:<port>` (cli_server) → sandbox block → `socket.create_connection` ค้างจน timeout 15s → raise → codex report done ไม่ได้ → pane ค้าง "working" ตลอด

**ทำไม macOS โดน Windows ไม่โดน:** Windows ใช้ `--dangerously-bypass-approvals-and-sandbox` (ไม่มี sandbox) อยู่แล้ว (เพราะ issue #5 codex-windows-sandbox-setup.exe requireAdministrator) → network ผ่าน. เฉพาะ macOS/Linux ที่ยัง `-s workspace-write`

### Falsification + LIVE REPRO (mantra step 3 — รัน disproof จริง)

รัน experiment บนเครื่องนี้ (macOS, codex-cli 0.142.5) connect loopback `127.0.0.1:<cockpit port>` ต่างสภาพ sandbox:

| # | เงื่อนไข | ผล |
|---|---|---|
| EXPT 1 | no sandbox (baseline) | `CONNECT OK` |
| **EXPT 2** | **codex seatbelt sandbox** (`codex sandbox -- ...`) | **`CONNECT FAIL: PermissionError [Errno 1] Operation not permitted`** ← Mode B ตัวจริง |
| EXPT 3–V1 | `codex sandbox -c sandbox_workspace_write.network_access=true` | ยัง FAIL — **แต่เพราะ `codex sandbox` subcommand เป็น generic seatbelt runner ที่ไม่ apply `[sandbox_workspace_write]` table** ไม่ใช่ key ผิด (test harness limitation) |

**EXPT 2 = สิ่งที่ `takkub done`'s `socket.create_connection` เจอเป๊ะ** → fail → codex report done ไม่ได้ → ค้าง "working"

**ยืนยันจาก codex official docs** (`~/.codex/skills/.system/imagegen/references/codex-network.md`):
> *"`--ask-for-approval never` suppresses approval prompts. It does **not** by itself enable network access. In `workspace-write`, network access still depends on your Codex configuration (for example `[sandbox_workspace_write] network_access = true`)."*

→ argv ปัจจุบัน `--ask-for-approval never -s workspace-write` **ไม่เปิด network** = deterministic block. **สมมติฐานรอด + confirmed 2 ทาง (repro + docs)**

### Fix

**Plan A (แนะนำ — surgical, doc-confirmed key, คง sandbox):** เปิด network ใน workspace-write ด้วย config override — คง cwd write-scoping ไว้ (ปลอดภัยกว่า bypass):

```python
# spawn_engine.py — codex_argv (macOS/Linux branch, ~line 1021)
else:
    codex_argv = [
        codex_bin,
        "--ask-for-approval", "never",
        "-s", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=true",  # ← doc-confirmed: ให้ takkub done (loopback TCP) ผ่าน
    ]
```
- key `sandbox_workspace_write.network_access` = **ตรงตาม codex official docs** (อ้างข้างบน) สำหรับ interactive `-s workspace-write` path (ที่ cockpit ใช้จริง)
- **verify ตอน implement:** spawn codex จริง (interactive path ไม่ใช่ `codex sandbox` subcommand) → ให้รัน `takkub done` → ยืนยันไม่ค้าง. (`codex sandbox` subcommand ใช้เทส key นี้ไม่ได้ — คนละ policy path)
- **trade-off:** `network_access=true` เปิด outbound network ทั้งหมดให้ codex (ไม่ใช่แค่ loopback) — แต่ claude/gemini มี full network อยู่แล้ว = consistent กับ trust model เดิม

**Plan B (validated fallback — ถ้า Plan A มีปัญหา):** match Windows ใช้ `--dangerously-bypass-approvals-and-sandbox` บน macOS/Linux ด้วย → ไม่มี seatbelt → loopback ผ่านแน่นอน (**พิสูจน์แล้ว EXPT 1: no-sandbox → CONNECT OK**). เสีย cwd write-sandbox แต่ trust level เท่า claude/gemini ที่เรายอมรับอยู่แล้ว. 1 บรรทัด, deterministic

**Plan C (robust, effort สูง — เก็บไว้ถ้าอยากคง sandbox แบบ least-privilege):** codex seatbelt ยอม **AF_UNIX socket** ผ่าน `--allow-unix-socket <path>` (เห็นใน `codex sandbox --help`). ให้ cli_server bind Unix domain socket เพิ่ม (นอกจาก TCP) + `takkub` CLI ลอง UDS ก่อน → sandbox ยังปิด TCP network แต่ done ผ่าน UDS ได้. แก้ทั้ง server + client (`cli_server.py` + `cli.py`) + เพิ่ม `--allow-unix-socket` เข้า codex argv — เก็บไว้ถ้าต้องการ security สูงสุด

### Test ที่ต้องเพิ่ม
- `tests/test_spawn_codex_argv.py` — assert macOS/Linux codex argv มี `network_access=true` (กัน regression)
- manual: spawn codex → ให้รัน `takkub done` → ยืนยันไม่ค้าง (pane → done ภายใน <2s)

---

## Mode A — SECONDARY: codex auto-update → exit เปล่า

### สาเหตุ
codex-cli (`@openai/codex`) เช็ค npm หา version ใหม่ตอน launch → บาง version **self-update แล้ว exit** บอก "restart Codex". argv ปัจจุบัน **ไม่มี flag ปิด update check** → intermittent (โดนเฉพาะตอนมี version ใหม่ค้างใน npm cache)

### หลักฐาน
- Breadcrumb 2 + 3 (round-2 exit เปล่า + transcript "auto-update ตอน launch → exit")
- infra ครึ่งนึงมีแล้ว: `codex_exit=True` → `_on_codex_exit` → crash dump ที่ `runtime/codex_crash_dumps/` (`CODEX_EARLY_CRASH_WINDOW_SEC=90`) — แต่วันนี้ dir ไม่ถูกสร้าง = exit หลัง 90s หรือ path ไม่ตรง เดี๋ยว repro เก็บ dump จริงยืนยันได้

### Fix (แนะนำ)
1. **ปิด update check ตอน spawn** — set env / config ให้ codex ไม่ auto-update:
   - `env["CODEX_DISABLE_UPDATE_CHECK"] = "1"` (หรือ config `[update] check=false` ตาม schema ของ codex version — ต้อง verify key จริง)
2. **Pin codex version + `takkub doctor --fix`** — parity กับงาน pin claude ใน commit `bfdb795` (doctor enforce pinned version + reinstall). ให้ doctor จัดการ codex version เดียวเหมือน claude
3. **Auto-respawn-once เมื่อ early-crash** — ต่อยอด `_on_codex_exit`: ถ้า exit < window และไม่ใช่ manual close → respawn อัตโนมัติ 1 ครั้ง (update จบแล้ว รอบสองจะ boot ปกติ) แทนที่จะทิ้ง pane เปล่า

---

## Mode C — TERTIARY: `delivery-unconfirmed @45s` false-positive

### สาเหตุ
- `_ready_wait_ms` extend 90s **เฉพาะ GEMINI** — codex ยัง 45s
- codex ready marker = `"openai codex (v"` เป็น **startup banner** ที่ scroll หลุด bottom-6 rows พอ UI render → `is_at_ready_prompt()` detect ไม่เจอ → poll ครบ 45s → blind paste + warning (แม้ paste จะติดจริงทีหลัง เหมือน round-1)

### Fix (cheap, 1 บรรทัด + optional)
```python
# lead_inbox.py _ready_wait_ms (~line 348)
from .provider_config import CODEX, GEMINI, effective_provider_for
if effective_provider_for(role_name, project=...) in (GEMINI, CODEX):  # ← เพิ่ม CODEX
    return 90_000
```
- **Optional (robust):** เพิ่ม persistent codex idle-footer marker เข้า `_READY_RULES` (เช่น string ที่ codex prompt footer โชว์ตอน idle จริง เช่น `"❯"` prompt หรือ shortcuts hint) แทนพึ่ง banner ที่ scroll หาย — ให้ detect survive หลัง UI render
- **หมายเหตุ:** เป็น cosmetic — พอ fix Mode B แล้ว codex report ผ่าน warning นี้ก็ไม่สำคัญ แต่แก้ถูกช่วยลด blind paste (Mode A repro ชัดขึ้น)

---

## แผน implement ที่เสนอ (propose — ยังไม่ fire)

| # | Fix | Role | ไฟล์ | cwd | เหตุผลลำดับ |
|---|---|---|---|---|---|
| 1 | Mode B — `network_access=true` + test | backend | `spawn_engine.py`, `tests/` | /Users/takub/agent-takkub | deterministic, unblock codex-macOS ทันที |
| 2 | Mode C — codex→90s window | backend | `lead_inbox.py` | /Users/takub/agent-takkub | 1 บรรทัด, ลด false warning |
| 3 | Mode A — disable update + auto-respawn-once | backend | `spawn_engine.py` | /Users/takub/agent-takkub | intermittent, effort สูงกว่า |

- **1 + 2 ทำคู่กันได้** (แตะคนละไฟล์ independent) → parallel `backend#1` (Mode B) + `backend#2` (Mode C)
- **verify:** ต้อง repro จริง — spawn codex บน macOS นี้ → `takkub done` ผ่านไหม (Mode B) + ไม่มี `delivery-unconfirmed` (Mode C)
- **3 แยกรอบ** หลัง 1-2 ผ่าน (ต้อง verify codex config key จริงก่อน)

> ⚠️ ก่อน implement ต้อง **verify codex config key** (`sandbox_workspace_write.network_access`, `CODEX_DISABLE_UPDATE_CHECK`) กับ codex version ที่ติดตั้งจริง — key อาจ drift ตาม version. รัน `codex --help` + ดู `~/.codex/config.toml` schema
