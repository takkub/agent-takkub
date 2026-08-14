# #177 — Playwright MCP ไม่ connect บน `qa --plan --shards` (round 3)

วันที่: 2026-08-14
ผู้ตรวจ: qa (worktree `wt/qa-1786662501`)
ขอบเขต: static re-verification of current code + a new local (non-cockpit) empirical
timing experiment. **ไม่มี live multi-pane cockpit repro** — เหตุผลอยู่ในหัวข้อ
"ทำไมไม่ live-repro" ด้านล่าง.

## สรุปสถานะ

**ยังไม่มี root cause ที่พิสูจน์ขาด — ยังต้องเปิด issue ค้างไว้** เหมือนสองรอบก่อนหน้า
(`docs/audit/2026-08-04-issue-146-playwright-shards.md`, `docs/audit/2026-08-13-issues-wave-d.md`).
งานรอบนี้ทำ 2 อย่างเพิ่ม:

1. **ตัดสมมติฐาน "deny-by-default policy" ที่ผู้มอบหมายงานเสนอไว้อันดับ 1 ออกอย่างเด็ดขาด**
   ด้วยหลักฐาน code-path เต็ม + regression test ใหม่ (ไม่ใช่แค่ static reasoning เหมือนสองรอบก่อน)
2. **เพิ่มหลักฐานเชิงประจักษ์ใหม่** (local timing experiment) ที่ falsify บางส่วนของ H2/H3
   จากรอบ 2026-08-04 — ไม่เคยมีใครทำมาก่อน เพราะสองรอบก่อนเป็น static-only

ไม่มีการเปลี่ยน production code เพิ่มในรอบนี้ (mitigation จาก wave D วันที่ 2026-08-13 —
`_BROWSER_SHARD_SPAWN_STAGGER_MS` — ยังอยู่ตามเดิม, ตรวจแล้วว่ายังทำงานถูกต้อง)

---

## Hypothesis ranking (อัปเดตจากสองรอบก่อน)

| # | Hypothesis | สถานะ | หลักฐาน |
|---|---|---|---|
| 1 | **Policy deny-by-default บน shard-suffixed role name** (`qa#2`/`qa-shard2` ไม่ได้ normalize กลับเป็น `qa` ก่อนเข้า `role_mcp_allowlist`) | **ตัดออกแล้ว — พิสูจน์ขาดด้วย code trace + regression test** | ดูหัวข้อ "เฟส 1" ด้านล่าง |
| 2 | H1 (เดิม): concurrent MCP cold-start ชน claude code's internal (unconfigurable) MCP connect timeout window | **ยัง live-unverified แต่ได้หลักฐานเสริมใหม่ที่อ่อนลงเล็กน้อย** (ดู "เฟส 2") | timing experiment รอบนี้ |
| 3 | H2 (เดิม): Node/Chromium process contention บนเครื่องที่มี memory pressure | **ยัง live-unverified** — บนเครื่องนี้ตอน idle ไม่เห็นสัญญาณ contention รุนแรง | timing experiment รอบนี้ |
| 4 | H3 (เดิม): npx `_npx` cache lock บน Windows | **อ่อนลง** — 6 concurrent `npx` process ไม่เจอ EBUSY/serialize เลยตอน cache อุ่นแล้ว | timing experiment รอบนี้ |
| 5 | (ใหม่ ยังไม่ทดสอบ) claude code's own per-session MCP concurrency cap (ไม่ใช่แค่ timeout แต่เป็น "จำนวน MCP server พร้อมกันสูงสุด") | **ไม่มีหลักฐาน — เป็น idea เสริมเฉยๆ** | ไม่มี, ต้องดู claude code changelog/behavior จริง |

---

## เฟส 1 — ตัดสมมติฐาน "deny-by-default policy" ทิ้ง (static, พิสูจน์ขาด)

โจทย์: `qa#2` (ad-hoc) หรือ shard #2 ของ `--shards 3` มี role identity ที่มี suffix
(`"qa#2"` เป็น pane key) — ถ้า suffix นี้เผลอไปถึง policy lookup แทนที่จะถูก split
ออกเป็น `base_role="qa"` ก่อน จะโดน `role_mcp_allowlist`'s "registered role without a
policy entry → deny" default (ดู `shared_dev_tools.py:751` docstring) → MCP ทั้งชุดถูก
ปฏิเสธเงียบๆ → ตรงกับอาการที่ #177 รายงาน

### พิสูจน์ว่าไม่เกิดขึ้นจริง — ไล่ทุก call site

`grep -rn "role_mcp_allowlist(|_role_variant_path(|shared_mcp_config_path_for_role("` ทั่ว
`src/` เจอ caller ทั้งหมด 3 จุด (นอกเหนือจาก `_write_role_variants` ที่ generate ไฟล์ variant
ล่วงหน้าจาก `_ROLE_MCP_POLICY` keys คงที่ ไม่เกี่ยวกับ per-pane lookup):

- `mcp_bridge.py:331` → `role_mcp_allowlist(base_role)` — **`base_role`**
- `shared_dev_tools.py:436` (`browser_profile_mcp_config_path`) → `shared_mcp_config_path_for_role(base_role)` — **`base_role`**
- `spawn_engine.py:2066` → `role_mcp_allowlist(base_role)` — **`base_role`**

และ `base_role` ทุกจุดมาจาก `_split_shard(role_name)[0]` (`pipeline_executor.py:81-93`,
`"qa#2"` → `("qa", 2)`) ที่ถูกเรียกตั้งต้นที่ `spawn_engine.py:1318` แล้ว thread ผ่าน
`mcp_argv_for_provider(...)` (`spawn_engine.py:1751,2393`) ลงไปจนถึง
`browser_profile_mcp_config_path`/`_claude_mcp_argv`/`_codex_mcp_argv` — **ไม่มีจุดไหนเลย
ที่ role name แบบมี `#N` หรือ `-shardN` ถูกส่งตรงเข้า policy lookup**

**สรุป:** สมมติฐานนี้เป็นไปไม่ได้ในเชิงโครงสร้าง (architecturally impossible) ไม่ใช่แค่
"ตรวจแล้วไม่เจอบั๊ก" — function signature ของ `browser_profile_mcp_config_path(base_role,
shard_idx, project, cwd)` **แยก `base_role` กับ `shard_idx` เป็นคนละ parameter ตั้งแต่ต้น**
ไม่มีการต่อ string เป็น `"qa#2"` หรือ `"qa-shard2"` แล้วส่งเข้า policy ที่ไหนเลยในทั้ง codebase

### Regression test ใหม่ (พิสูจน์ consequence ไม่ใช่แค่ trace)

`tests/test_browser_mcps.py::TestBrowserProfileMcpConfigPath::test_shard_gets_identical_mcp_server_set_as_non_shard`
— เรียก `browser_profile_mcp_config_path("qa", None, ...)` กับ `browser_profile_mcp_config_path("qa", 2, ...)`
ตรงๆ แล้วยืนยันว่า **ชุดชื่อ MCP server ที่ได้ (`set(mcpServers.keys())`) เหมือนกันทุกประการ**
ระหว่าง shard กับ non-shard — ถ้าอนาคตมีคนเผลอแก้ให้ call site ไหนเริ่มส่ง role name
แบบมี suffix เข้า policy lookup เทสนี้จะแดงทันที (ไม่ต้องพึ่งการ trace โค้ดด้วยตาซ้ำอีก)

ผลรัน: **PASS** (พร้อม `test_browser_mcps.py` ทั้งไฟล์ 40 tests เขียวหมด)

---

## เฟส 2 — หลักฐานเชิงประจักษ์ใหม่: local concurrent MCP cold-start timing

สองรอบก่อนหน้า static-only ทั้งคู่ (ไม่มีสิทธิ์รัน browser driver ในรอบ 2026-08-04, ไม่มี live
cockpit ในรอบ 2026-08-13) — รอบนี้มีสิทธิ์รัน `npx`/`node` ตรงๆ ในเครื่องเดียวกับที่ cockpit จะรัน
เลยทดลองจำลอง "N shard × 2 MCP process/shard spawn พร้อมกัน" แบบง่ายที่สุดที่ทำได้โดยไม่ต้อง
เปิด cockpit จริง: รัน `npx --yes @playwright/mcp@0.0.75 --help` และ
`npx --yes chrome-devtools-mcp@0.26.0 --help` (ทั้งคู่ pinned version ตรงกับ
`shared_dev_tools.py:112-113`) จับเวลา wall-clock

### ผลลัพธ์

| การทดสอบ | จำนวน process พร้อมกัน | เวลาต่อ process | Total wall time |
|---|---|---|---|
| baseline เดี่ยว (playwright) | 1 | 3.503s | 3.503s |
| baseline เดี่ยว (chrome-devtools) | 1 | 3.853s | 3.853s |
| 3 shard จำลอง × 2 MCP/shard | 6 (parallel) | 4.19s–4.71s | **5s** (ไม่ใช่ผลรวม ~24s) |

Cache อุ่นอยู่แล้ว (เคยรันมาก่อนในเครื่องนี้ — pinned version ไม่ต้อง network fetch ใหม่)
เครื่องอยู่ในสภาพ idle (ไม่มี cockpit/browser อื่นรันขนานตอนทดสอบ)

### สิ่งที่หลักฐานนี้ตัดออก/ไม่ตัดออก

- **ไม่เจอ error/lock ใดๆ** ระหว่าง 6 process พร้อมกัน (ไม่มี `EBUSY`/timeout ใน log) → **อ่อน
  หลักฐานของ H3** (npx cache lock) — ถ้ามี hard lock จริง total wall time ควรใกล้เคียงผลรวม
  (~24s) ไม่ใช่ ~5s (parallel จริง)
- **เวลาต่อ process เพิ่มขึ้นจาก baseline แค่ ~20-25%** (3.5-3.9s → 4.2-4.7s) ไม่ใช่การพุ่งแบบ
  ทวีคูณ → **อ่อนหลักฐานของ H2** (severe resource contention) *บนเครื่องที่ idle* — ยังไม่ตัด H2
  ทิ้งทั้งหมด เพราะยังไม่ทดสอบตอนเครื่อง busy (เปิด cockpit + browser + IDE จริง) ตามที่ audit
  รอบ 2026-08-04 เสนอไว้
- **ไม่สามารถพิสูจน์/หักล้าง H1 ได้เลย** — นี่คือ `npx ... --help` (แค่โหลด CLI entry + parse
  argv) ไม่ใช่ MCP stdio handshake จริงที่ claude code ต้องรอ `initialize` response ก่อนถือว่า
  "connected" เวลาที่แท้จริงของ full MCP handshake (spawn Node → require ทั้ง package →
  launch/attach browser → ตอบ JSON-RPC `initialize`) น่าจะนานกว่านี้มาก โดยเฉพาะ
  `browser_profile_mcp_config_path` เติม `--user-data-dir`/`--userDataDir` ที่ต้องสร้าง/เปิด
  Chromium profile จริงด้วย — การทดลองนี้**ไม่ได้จำลองส่วนนั้นเลย** และ **claude code's internal
  MCP connect timeout เป็นค่าคงที่ปิดใน binary เอง ไม่มีทางวัดจากนอก process ได้โดยไม่มี live
  cockpit spawn จริง**

**สรุปเฟส 2:** เป็นหลักฐานเสริมที่ทำให้ H3 น่าเชื่อถือน้อยลง และ H2 (บนเครื่อง idle) ก็ไม่รุนแรง
เท่าที่กลัวไว้ แต่**ไม่ใช่หลักฐานเพียงพอที่จะปิด H1/H2 ทิ้งได้** — H1 โดยเฉพาะยังต้องการ live
cockpit repro เท่านั้นเพราะเป็นพฤติกรรมภายในของ `claude.exe` เอง

---

## ทำไมไม่ live-repro รอบนี้

Role file ของ qa ในโปรเจคนี้บล็อกไม่ให้ teammate pane เรียก `takkub assign`/`spawn`/`close`
เอง (CLI-level gate, exit 1) — เฉพาะ Lead เท่านั้นที่ spawn ได้ งานนี้ถูก spawn เป็น qa
teammate pane เดียว (ไม่ใช่ `--plan --shards` fan-out) จึงไม่มีสิทธิ์/context ที่จะ spawn
`qa --plan --shards 2` ขึ้นมาทดสอบเองภายใน session นี้ตามที่ task ระบุไว้ ("live repro ทำ
เฉพาะเมื่อ unit-level พิสูจน์ไม่ได้... ใช้ `--shards 2`") — **unit-level พิสูจน์ hypothesis #1
(deny-by-default) ได้ขาดแล้วในเฟส 1** จึงไม่จำเป็นต้อง live-repro สำหรับ hypothesis นั้น
ส่วน hypothesis ที่เหลือ (H1/H2/H3) เป็น **runtime timing ที่พิสูจน์ได้เฉพาะด้วยการรัน
`qa --plan --shards 2/3` จริงในเครื่องที่มีคนสั่ง `takkub assign` เท่านั้น** (Lead หรือ user)

## สิ่งที่ต้องทำต่อเพื่อปิด #177 จริง (ยังไม่ทำในรอบนี้)

1. **Lead หรือ user รัน `takkub assign --role qa --plan --shards 3 "<task ทดสอบธรรมดา>"` จริง**
   บนเครื่องนี้ แล้วสังเกตว่า `mcp__playwright__*` ขึ้น available ใน pane ทั้ง 3 shard หรือไม่
   (ก่อน commit นี้จะมี `_BROWSER_SHARD_SPAWN_STAGGER_MS=3000` มิติเกชันอยู่แล้วจาก wave D)
2. ถ้ายัง**พังอยู่แม้มี stagger 3s** → เก็บ stderr/transcript ของแต่ละ shard pane ตอน MCP
   connect fail (ไม่เคยมีใครเก็บได้จริงจนถึงตอนนี้) แยก timeout error ออกจาก lock/ENOENT error
   ตาม repro plan ข้อ 4 ของ `docs/audit/2026-08-04-issue-146-playwright-shards.md`
3. ถ้ายังพังแม้มี stagger → พิจารณาขยาย `warm_browser_mcps()` ให้ warm ต่อจำนวน shard ที่กำลัง
   จะ fan-out (ข้อเสนอเดิมที่ยังไม่ implement จาก audit รอบแรก) แทนที่จะเพิ่ม stagger อีก
4. ถ้าผ่านหมดทุก shard → ปิด #177 ได้ พร้อม cite live evidence (transcript/screenshot ที่แสดง
   `mcp__playwright__*` ใช้งานได้ในทั้ง 3 shard pane พร้อมกัน)

## ไฟล์ที่แก้รอบนี้

- `tests/test_browser_mcps.py` — เพิ่ม `test_shard_gets_identical_mcp_server_set_as_non_shard`
  (1 test ใหม่, ตัดสมมติฐาน deny-by-default อันดับ 1 ทิ้งด้วยหลักฐาน code + test)

ไม่มีการแก้ production code เพิ่ม — mitigation ของ wave D (2026-08-13) ยังอยู่ตามเดิมและตรวจ
แล้วว่ายังทำงานถูกต้อง (`pipeline_executor.py:96-104,747`, `cli_server.py:93-110`)

## ผลรัน test suite

`tests/test_browser_mcps.py tests/test_mcp_bridge.py tests/test_graft_mcp.py
tests/test_issue_167_adhoc_instance_isolation.py tests/test_cli_server.py
tests/test_qa_plan_fanout.py tests/test_pane_guard.py tests/test_pane_tools_policy.py`
— **328 passed**, ruff check + format clean บน `tests/test_browser_mcps.py`
