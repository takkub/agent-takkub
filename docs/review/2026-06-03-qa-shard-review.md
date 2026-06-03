---
title: Code Review — QA shard fan-out (--shards N)
date: 2026-06-03
reviewer: reviewer (Claude)
scope: 8 files (orchestrator.py, main_window.py, agent_pane.py, cli.py, cli_server.py, config.py, qa.md, tests)
verdict: pass-with-minor
blockers: 0
---

# Code Review — QA shard fan-out

## Verdict: **pass-with-minor** · 0 blocker · 1 major · 5 minor

การ implement แยก `pane_key` (`qa#1`) ออกจาก `base_role` (`qa`) ทำได้ **ครบและถูกต้อง** ทั้ง 21 จุดที่ codex flag — จุดที่ต้องใช้ `base_role` (role file, provider, cwd default, CHROME_BIN, MCP, model tier, plugins) ใช้ถูกหมด, จุดที่ต้องคง `pane_key` (registry, `_pane_state`, transcript, respawn, close, signals, done) คงถูกหมด. Backward-compat (ไม่มี `--shards` → key เดิม ไม่มี suffix) ผ่าน. Security (validate shard suffix, ไม่หลุดเป็น path) แข็งแรง. Tests 24/24 + cli_server 6/6 ผ่าน.

จุดที่ควรแก้: **per-shard `[done]` notice ยัง spam Lead** (N+1 ข้อความ) — ขัดกับเป้าหมาย "consolidated handoff" ของ gemini review. ที่เหลือเป็น minor/edge-case.

---

## 🔴 Blocker — ไม่มี

---

## 🟠 Major

### M1. Per-shard `[done]` notice ยัง spam Lead — เป้าหมาย "consolidated handoff" ทำได้แค่ครึ่งเดียว
`orchestrator.py:2634-2643` (done) เขียน `[{from_role} done] {note}` เข้า Lead **ทุก shard** เหมือนเดิม **แล้วค่อย** ยิง consolidated handoff ตอน shard สุดท้าย (line 2677-2686). ผล: Lead ได้ **N + 1 ข้อความ** ต่อ 1 fan-out.

- Gemini design review ข้อ 3 ระบุชัด: *"Consolidated Handoff เพื่อให้ Lead ไม่โดน spam ด้วย N ข้อความ"* → ยังไม่บรรลุ
- Test `test_no_handoff_until_all_done` (line 262) **ยืนยันพฤติกรรมนี้เอง**: `assert lead.session.write.call_count == 2  # 2 per-shard "[done]" notices` ก่อน handoff ตัวที่ 3

**Suggested fix:** เมื่อ `had_shard_total > 0` ให้ **suppress** การ write per-shard notice เข้า Lead (ปล่อยให้ consolidated handoff เป็นข้อความเดียวที่ Lead เห็น) — หรืออย่างน้อยลดเหลือ progress dot สั้นๆ. ยังควร `_save_decision_note` per shard ไว้ (status view ใช้) แต่ไม่ต้องยิงเข้า Lead pane.

```python
# done(), ~line 2640 — gate the per-shard Lead write
if lead and lead.session and lead.session.is_alive and had_shard_total == 0:
    lead.session.write(notice); ...
# (ยัง queue/บันทึก decision note ได้ตามเดิม; consolidated handoff คือข้อความเดียวเข้า Lead)
```
> Severity major เพราะเป็น design-goal miss ของฟีเจอร์นี้โดยตรง — ไม่ใช่ crash แต่ทำลายคุณค่าหลัก (ลด noise) ที่ตั้งใจสร้าง

---

## 🟡 Minor

### m1. `--shards` + `--auto-chain` → double handoff เข้า Lead
CLI ส่ง `auto_chain` ให้ทุก shard (`cli.py:158`). ตอน shard สุดท้าย done จะยิง **2 ข้อความ**: (a) auto-chain handoff (`orchestrator.py:2667-2674`, pending ว่าง) + (b) shard fan-out handoff (line 2685). Lead อาจ fire verify ซ้ำ/งง.
- Convention ใน CLAUDE.md ห้าม verify hop (qa) ใส่ `--auto-chain` อยู่แล้ว แต่**ไม่มีอะไรบังคับ**
- **Fix:** reject `--shards N` + `--auto-chain` พร้อมกันใน CLI (exit 1 + ข้อความ) หรือ dedupe: ถ้า shard group active ให้ข้าม auto-chain handoff (ใช้ consolidated เป็นตัวเดียว)

### m2. Shard group ไม่ persist ข้าม cockpit restart → fan-out ค้างเงียบ
`_shard_groups` + `QTimer` 45 นาที เป็น in-memory ล้วน. ถ้า cockpit restart กลางคัน fan-out: group หาย, timer หาย → **ไม่มี consolidated handoff และ timeout ไม่ยิง** (per-shard done ที่เหลือยิงเป็น notice เดี่ยวๆ แต่ไม่ aggregate). `shard_total` ใน `_pane_state` recover เฉพาะ auto-respawn path (line 1096-1099) ไม่ใช่ full restart.
- รับได้สำหรับ v1 (smoke fan-out เสร็จในไม่กี่นาที, restart กลางคันยาก) แต่ **ควร log/note ไว้**. Fix ระยะยาว: persist group เหมือน `_pending_done_notices`

### m3. `TAKKUB_PROJECT_CWD` ที่ qa.md อ้างถึง — ไม่มีใคร inject
`qa.md` shard section ใช้ `${TAKKUB_PROJECT_CWD:-.}` สร้าง path port-file/profile. grep ทั้ง `src/` **ไม่พบการ set `TAKKUB_PROJECT_CWD`** → fallback เป็น `.` (cwd) เสมอ.
- ใช้งานได้จริง (cwd เดียวกันทุก shard, port file แยกด้วย `qa-${SHARD}.port`) แต่ doc อ้าง env ที่ไม่มีจริง = ชวนเข้าใจผิด
- **Fix:** เอา `TAKKUB_PROJECT_CWD` ออกจาก qa.md (ใช้ `.` ตรงๆ) หรือ inject env นั้นจริงใน `spawn()`. แนะนำอย่างแรก (เบากว่า)

### m4. Chrome port isolation = prompt-enforced ล้วน (ไม่มี code guarantee)
Q3 แก้ถูกทาง (ephemeral `--port 0` + shard-specific port file `.takkub/chrome/qa-N.port` + per-shard `--user-data-dir`) — ตรงกับ codex recommended fix **แต่ทั้งหมดอยู่ใน qa.md เป็นคำสั่งให้ agent ทำเอง**. ไม่มีอะไรกัน stray `mb` เขียนทับ default `.chrome_port`. ถ้า agent ไม่ทำตาม qa.md เป๊ะ → cross-shard browser collision กลับมา.
- เป็นข้อจำกัดเชิงสถาปัตยกรรม (mb รันใน agent ไม่ใช่ orchestrator) — รับได้ แต่ควร **gitignore `.takkub/`** (ยังไม่เห็นใน .gitignore diff — เพิ่มแค่ `.gemini/`, `docs/eval/`) กัน port-file/profile หลุดเข้า repo

### m5. `_split_shard()` throw ValueError ถ้าเจอ key `qa#` หรือ `qa#x` (defensive gap)
`_split_shard` ทำ `int(idx)` โดยไม่ try/except. `validate_name` กันไว้ upstream แล้ว (ทุก path ที่เข้า registry ผ่าน validate ก่อน) จึงปลอดภัยใน flow ปัจจุบัน — แต่ helper เป็น module-level ถูกเรียกหลายที่ (main_window, status). ถ้าอนาคตมี caller ที่ไม่ผ่าน validate → crash. **Fix (optional):** guard `idx.isdigit()` ก่อน `int()`, return `(key, None)` ถ้าไม่ใช่เลข

---

## ✅ Codex 21-point checklist — ผลตรวจ

จุดที่ต้องใช้ **base_role**:
| # | จุด | ใช้ base_role ถูกไหม |
|---|---|---|
| 1 | register_pane key | ✅ main_window สร้าง `Role(name="qa#1")` → registry เก็บ `qa#1` ไม่ทับกัน |
| 2 | `_ensure_teammate_pane` by_name | ✅ `by_name(base_role)` เอา color/grid + `Role(name=pane_key)` |
| 3 | validate_name `#` | ✅ shard suffix regex แยก, base ผ่าน `_SAFE_NAME`, `#` ไม่ใช่ path sep |
| 4 | default_cwd_for_role | ✅ `default_cwd_for_role(base_role)` (line 1325). `_cwd_within_project(...,role_name)` ใช้ role_name แค่เช็ค `==LEAD` → shard ไม่ใช่ Lead, ไม่กระทบ |
| 5 | provider lookup | ✅ `effective_provider_for(base_role)` (line 1182, 1891, main_window 2132) |
| 6 | role staging / markdown | ✅ `agent_role_dir(base_role)` + append-system-prompt staging (line 1324) |
| 7 | TAKKUB_ROLE/env | ✅ `TAKKUB_ROLE`=pane_key คงเดิม + เพิ่ม `TAKKUB_BASE_ROLE/SHARD/SHARD_TOTAL` |
| 8 | CHROME_BIN | ✅ `base_role == "qa"` (line 1417) |
| 9 | `_teammate_tier` | ✅ `_teammate_tier(base_role)` (line 1472) |
| 10 | plugins + MCP | ✅ `_default_plugin_dirs(base_role)` + `shared_mcp_config_path_for_role(base_role)` |
| 17 | status screenshot roles | ✅ `_split_shard(role)[0] in (...)` (line 2905, 3002) |
| 19 | claude proc count | ✅ main_window `effective_provider_for(_split_shard(role)[0])` |

จุดที่ **ห้าม split** (คง pane_key เต็ม):
| # | จุด | คง pane_key ถูกไหม |
|---|---|---|
| 11 | _recent_exits / transcript / respawn / _pane_state | ✅ ไม่ split; test ยืนยัน state แยกราย shard; respawn ส่ง `_shard_total=snap_shard_total` กู้ env |
| 14 | done keyed by TAKKUB_ROLE | ✅ done ใช้ pane_key, aggregate by `{ns}::{base_role}` group |
| 16 | close / unregister | ✅ `close(from_role)` = pane_key, ไม่ strip |
| 18 | status done-event filename | ✅ `_save_decision_note` เขียน `<pane_key>-<HHMMSS>.md` = `qa#1-...`, status match `f"{role}-"` = `qa#1-` ตรง (`#` legal บน Windows) |
| 20 | AgentPane objectName/CSS/signal/export/font | ✅ objectName+CSS sanitize `#`→`-`; signal/export/font ใช้ `role.name`=pane_key (distinct, `#` ปลอดภัยใน filename/settings key) |
| 21 | pane signal routing | ✅ signals emit `role.name`=pane_key (qa#1) |

จุด aggregate/CLI:
| # | จุด | ผล |
|---|---|---|
| 12 | assign per-pane + group aggregate | ✅ `ShardGroup` keyed `{ns}::{base_role}`; ⚠️ requires_commit/auto_chain ยัง per-pane → ดู m1 |
| 13 | CLI `--shards` | ✅ มี; loop ใน CLI แต่ส่ง `shard_total` ทุก request → group share ผ่าน base_role key (ไม่ blind) |
| 15 | auto-chain timeout/partial | ✅ ShardGroup มี 45-min timeout + partial handoff (`_check_shard_group_timeout`) |

**สรุป: 21/21 addressed.** (m1 = ส่วนต่อยอดของ #12 ที่ยังไม่ครบเป้า anti-spam)

---

## 🔎 ตอบ 5 ประเด็น review ที่ Lead ถาม

**① pane_key vs base_role แยกถูกทุกจุด** — ✅ ครบ ดูตารางบน. ไม่พบจุด silent fall-back generic.

**② backward-compat** — ✅ ไม่มี `--shards` → `cmd_assign` ลง branch เดิม (line 166), ไม่ส่ง `shard_total` → server default 0 → `_split_shard("qa")=("qa",None)` → ไม่มี env shard, ไม่สร้าง group, behavior เดิม 100%. Test `test_assign_without_shard_no_group` ยืนยัน.

**③ Q3 port race** — ✅ ออกแบบถูก: ephemeral `--port 0` + port-file ต่อ shard (`qa-N.port`) + user-data-dir ต่อ shard. แก้ทั้ง intra-project (shard ชนกัน) และ cross-project (project A/B ชน 9223) เพราะ OS เลือก port ว่าง. ⚠️ แต่ **prompt-enforced** ล้วน (m4) + ควร gitignore `.takkub/`. ไม่มี atomic-write guarantee จาก orchestrator (อยู่ที่ `mb-start-chrome` ต้องรองรับ `--port-file` — เป็น external dependency ที่ยังไม่ได้ verify ใน review นี้).

**④ Q4 timeout / late-done** — ✅ มี deadline 45 นาที (`_SHARD_GROUP_TIMEOUT_MS`). Late-done **ไม่** ยิง handoff ซ้ำ: timeout pop group → done() ตามมา `get(group_key)=None` → skip (line 2680-2681). Double-fire ป้องกันด้วย `closed` flag + pop. ⚠️ ข้อจำกัด: timer ไม่ persist ข้าม restart (m2); late-done ไม่ถูก append เป็น follow-up notice (codex แนะ — optional, ไม่ทำก็ไม่ค้าง).

**⑤ security / validate_name** — ✅ แข็งแรง:
- `qa#1`✓ `qa#99`✓ `QA#1`→`qa#1`✓ (lowercase)
- reject: `qa#0`(นำ 0)✓ `qa#-1`✓ `qa#abc`✓ `qa#`(ว่าง)✓ `#1`(base ว่าง)✓ `../etc#1`(traversal)✓ `qa#1#2`✓
- `#` ไม่เคยใช้เป็น path separator: role-file/staging ใช้ `base_role` (clean `_SAFE_NAME`) เท่านั้น; pane_key (`qa#1`) ใช้แค่ registry/transcript/done-note filename ซึ่ง `#` เป็น literal ปลอดภัยบน Windows
- max shard = 999 (`[1-9][0-9]{0,2}`) — เพียงพอ

---

## จุดที่ codex flag แต่ implementation ยังไม่ทำ (ตกค้าง)
1. **(จาก #12/gemini) suppress per-shard Lead notice** → ยังไม่ทำ = **M1 (major)**
2. **(Q4 codex)** late-done append เป็น follow-up notice หลัง timeout → ไม่ทำ (optional, ไม่ทำให้ค้าง)
3. **(Q3 codex)** atomic write + `MB_CHROME_PORT_FILE` enforcement ระดับ code → เป็น prompt-only (m4)
4. **persist shard group** ข้าม restart → ไม่ทำ (m2)

---

## Tests
- `tests/test_orchestrator_shard.py` — **24/24 pass** (split_shard, validate_name suffix, pane independence, aggregate, partial-pass, timeout, assign-creates-group)
- `tests/test_cli_server.py` — **6/6 pass** (fake orch signature อัปเดตรับ `shard_total`)
- Coverage ดี. ขาด: test ยืนยันว่า `--shards`+`--auto-chain` ไม่ double-fire (m1), test cross-project port (เป็น prompt-level, test ยาก)
</content>
</invoke>
