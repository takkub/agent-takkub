# Wave D — browser-QA infra issues (#167, #177, #178, #182)

วันที่: 2026-08-13
ผู้ตรวจ: qa (worktree `wt/qa-1786616082`, branch `release/2026-08-13` base)
ขอบเขต: static code trace + targeted regression tests เท่านั้น — ไม่มี live cockpit
fan-out ให้ repro สด (worktree เดี่ยว ไม่ใช่ multi-pane cockpit instance)

## สรุปผลรวม

| Issue | สถานะหลังตรวจ | Action |
|---|---|---|
| **#167** ad-hoc `qa#N` ชน CDP 9222 | **ถูกแก้แล้วในโค้ดปัจจุบัน** — ไม่ต้องแก้เพิ่ม | เขียน regression test ล็อกไว้ |
| **#177** Playwright MCP ไม่ connect บน shard | **ยังพิสูจน์ root cause ไม่ได้ (ต้อง live repro)** | เพิ่ม mitigation (widen spawn stagger) ตามข้อเสนอของ audit เดิม — ไม่ claim ว่าแก้ขาด |
| **#178** ไม่มี `--output-dir` → screenshot หาย | **บั๊กจริง, แก้แล้ว** | เพิ่ม `--output-dir` เข้า playwright MCP template |
| **#182** ไฟล์ว่าง/ซ้ำไม่ถูกตรวจจับ | ส่วน size/header (#159) **แก้แล้วอยู่ก่อนแล้ว**; ส่วน **duplicate-content ยังไม่มี → เพิ่มแล้ว** | เพิ่ม md5 dedup detection |

---

## #167 — ad-hoc `qa#N` instances ชน CDP 9222

### ข้อสรุป: **ถูกแก้แล้วในโค้ดปัจจุบัน (release/2026-08-13)**

Trace เต็มเส้นทาง `--role qa#2` (พิมพ์ตรงๆ ไม่ผ่าน `--shards`):

1. `cli.py:cmd_assign` — `--role qa#2` ไม่มี `--shards` flag → ตกลง branch ปกติ
   (บรรทัด 303-318) ส่ง request **ไม่มี `shard_total` field เลย**
2. `spawn_engine.py:1318` — `base_role, shard_idx = _split_shard(role_name)` ใช้
   `pipeline_executor._split_shard("qa#2")` → `("qa", 2)` — **ตัวนี้อ่านจาก pane key
   ("qa#2") อย่างเดียว ไม่สนใจ shard_total เลย**
3. `browser_chrome.should_manage_native_chrome("qa", 2, ...)` → `shard_idx is not
   None` → คืน `False` — cockpit ไม่จัดการ native Chrome ให้ ad-hoc instance นี้
   เหมือนกับ real shard ทุกประการ
4. `pane_guard.classify("mb ...", "qa#2")` — เช็ค `"#" in raw_role` (ไม่ใช่
   shard_total) → **บล็อก `mb` ทันที** พร้อม reason ชี้ไปที่ Playwright MCP
   (`pane_guard.py:202-211`, มี test คุมอยู่แล้วที่ `test_pane_guard.py:136-150`
   ใช้ role `"qa#1"` ตรงๆ — ครอบ ad-hoc pattern นี้อยู่แล้วโดยไม่รู้ตัว)
5. `browser_profile_mcp_config_path("qa", 2, project)` — สร้าง isolated
   `--user-data-dir`/`--userDataDir` แยกต่อ pane เหมือน shard จริงทุกประการ (ไม่มี
   `shard_total` parameter ในฟังก์ชันนี้เลย — เห็นแค่ int ที่แยกมาแล้ว)

**สรุป:** ทุกกลไก isolation (native-Chrome opt-out, mb block, browser-profile
isolation) key ด้วย **int ที่ parse จาก pane key เอง** ไม่ใช่ `shard_total` — ทำให้
ad-hoc `qa#2` (พิมพ์ตรงๆ) กับ real fan-out shard #2 **เดินโค้ดเดียวกันทุกจุด**
วันที่ issue ถูกยื่น (2026-07-08, migrate จาก local tracker) น่าจะเป็นก่อนที่
`pane_guard.py`'s generic `"#" in raw_role` check (commit `0746118` "fix browser
QA with native Chrome on Windows") landed — เช็ค git log แล้วพบว่า commit นี้แก้
"native Chrome" ใน context เดียวกับที่ #167 อธิบาย

### หลักฐาน: regression test ใหม่

`tests/test_issue_167_adhoc_instance_isolation.py` (6 tests, ทั้งหมดผ่าน) — trace
เต็มเส้นทางข้ามโมดูล (`pipeline_executor._split_shard` → `browser_chrome
.should_manage_native_chrome` → `pane_guard.classify` → `shared_dev_tools
.browser_profile_mcp_config_path`) พิสูจน์ว่า:
- `"qa#2"` แยกเป็น `("qa", 2)` เหมือน real shard
- native Chrome ถูกปฏิเสธสำหรับ ad-hoc instance เหมือน real shard
- `mb` ถูกบล็อกสำหรับ ad-hoc instance suffix (ไม่ใช่แค่ shard ที่มาจาก `--shards`)
- instance แรก (`"qa"` ไม่มี suffix) ยังใช้ `mb`/native Chrome ได้ปกติ — ไม่ได้บล็อกมั่ว
- browser profile ของ ad-hoc instance ถูก isolate จริง แยกจาก instance แรก

**ไม่ต้องแก้โค้ด production เพิ่มสำหรับ #167** — ปิด issue ได้ด้วยหลักฐานนี้

---

## #177 — Playwright MCP ไม่ connect บน `qa --plan --shards N` (single pane ปกติ)

### สถานะ: **ยังไม่มี root cause ที่พิสูจน์แล้ว — คงสถานะ open, เพิ่ม mitigation เท่านั้น**

Static audit รอบก่อน (`docs/audit/2026-08-04-issue-146-playwright-shards.md`, 2 รอบ
เต็ม) ไล่ argv construction, provider resolution, env injection, policy lookup —
**ไม่พบบั๊กเชิงโครงสร้าง** ทุก code path เหมือนกับ pane เดี่ยวทุกประการ ยกเว้นค่าที่
ตั้งใจให้ต่าง (shard-specific config path) เหลือแค่ 3 hypothesis ที่ต้อง live repro:

- **H1** (น่าจะเป็นสุด): concurrent MCP cold-start (N shard × 2 MCP process/shard)
  ชน claude code's internal MCP connect timeout window ที่ configure ไม่ได้
- **H2**: Node/Chromium process contention บนเครื่องที่มี memory pressure อยู่แล้ว
- **H3** (น้อยสุด): npx `_npx` cache lock บน Windows

งานนี้ (worktree เดี่ยว, ไม่มี live multi-pane cockpit ให้ repro) **ไม่สามารถพิสูจน์/
หักล้าง hypothesis เหล่านี้ได้เพิ่มเติม** — ยังต้องให้คนที่มี cockpit จริงรัน repro
plan เดิม (idle vs busy machine, จับเวลา MCP-init จริง)

### สิ่งที่ทำเพิ่ม: mitigation ตามข้อเสนอของ audit เดิม (ไม่ claim ว่าแก้ขาด)

audit เดิมเสนอไว้ว่า "เพิ่ม `_SPAWN_STAGGER_MS` เฉพาะ browser-role shard fan-out
ให้กว้างขึ้นเป็นวินาทีระดับ (คล้าย `_CODEX_SPAWN_STAGGER_MS`)" — implement แล้ว:

- **`pipeline_executor.py`**: เพิ่ม `_BROWSER_SHARD_SPAWN_STAGGER_MS` (default
  3000ms, env override `TAKKUB_BROWSER_SHARD_SPAWN_STAGGER_MS`) ใช้ใน
  `_fire_qa_plan_fanout` แทน `_SPAWN_STAGGER_MS` เดิม เมื่อ `base_role` อยู่ใน
  `pane_guard.BROWSER_ROLES` — mirror pattern เดียวกับ codex gap ที่มีอยู่แล้ว
- **`cli_server.py`**: เพิ่ม slot ที่ 3 (`_browser_shard_slot_until`,
  `_browser_shard_gap_ms`) ใน `_next_spawn_delay_ms` เพื่อครอบ plain (ไม่ใช่
  `--plan`) `--shards N` path ด้วย — path นี้ยิง N request แยกผ่าน CLI socket
  ไม่ได้ผ่าน `_fire_qa_plan_fanout` เลย ต้องแก้คนละจุด

Non-browser fan-out (backend/frontend shards) **ไม่ได้รับผลกระทบ** — ยัง 400ms
gap เดิม เฉพาะ shard ที่ base_role เป็น qa/critic/designer เท่านั้นที่ gap กว้างขึ้น
เป็น 3s

### หลักฐาน: regression tests ใหม่

- `tests/test_cli_server.py::TestBrowserShardSpawnStagger` (4 tests) — พิสูจน์ gap
  กว้างขึ้นเฉพาะ `qa#N`/`critic#N`/`designer#N`, ไม่กระทบ non-shard หรือ
  non-browser shard
- `tests/test_qa_plan_fanout.py::TestFireFanoutStagger` (2 tests) — พิสูจน์ delay
  sequence จริงที่ `_fire_qa_plan_fanout` ยิงออกไป (`[0, 3000, 6000]` สำหรับ 3
  shards ของ qa)

**คำเตือน:** นี่คือ mitigation จาก hypothesis ที่ยังไม่ verified ไม่ใช่ proven fix —
ถ้า live repro (H1/H2 idle-vs-busy test) ยืนยันภายหลังว่าไม่ใช่สาเหตุจริง ต้องกลับมา
พิจารณาถอด/ปรับ constant นี้ #177 **ควรเปิดค้างไว้จนกว่าจะมี live evidence** ว่า gap
ที่กว้างขึ้นทำให้ shard connect สำเร็จจริง (หรือไม่)

---

## #178 — Playwright MCP ไม่มี `--output-dir` → screenshot หายเข้า temp dir

### สถานะ: **บั๊กจริง ยืนยันจาก source, แก้แล้ว**

`shared_dev_tools.BROWSER_MCPS["playwright"]["args"]` มีแค่ `-y
@playwright/mcp@<pinned>` ไม่มี `--output-dir` เลย — `browser_profile_mcp_config_path`
เดิมเติมแค่ `--user-data-dir` (profile isolation) ไม่ได้เติม output dir ให้
→ relative `filename` ที่ role prompt ทุกตัวบอกให้ agent ส่ง (ตาม `@playwright/mcp`
README) ตกไปอยู่ใน temp dir ภายในของ MCP server เอง (ตามที่ issue สืบมา)

### การแก้: เติม `--output-dir` ให้ playwright เท่านั้น (ไม่ใช่ chrome-devtools)

`shared_dev_tools.py`:
- เพิ่ม `_OUTPUT_DIR_FLAG = {"playwright": "--output-dir"}` — **ไม่รวม
  chrome-devtools-mcp** เพราะไม่มี flag นี้ในเอกสารของมัน (ต่างจาก
  `--user-data-dir`/`--userDataDir` ที่ทั้งคู่มี) ใส่ flag ที่ CLI ไม่รู้จักเสี่ยงทำให้
  server ปฏิเสธ startup ทั้งตัว
- `browser_profile_mcp_config_path` เติม `--output-dir <SHARED_MCP_FILE.parent
  >/exports/<date>/<project>/screenshots` ให้ playwright ทุก pane (isolated
  หรือไม่ก็ตาม) — ใช้ path indirection เดียวกับที่ `profiles_root` ใช้อยู่แล้ว
  (`SHARED_MCP_FILE.parent`) เพื่อให้ redirect ได้ในเทส และตรงกับ production
  จริง (`SHARED_MCP_FILE.parent == RUNTIME_DIR`)
- **screenshots dir ใช้ path เดียวกันข้าม shard โดยตั้งใจ** (ต่างจาก
  `--user-data-dir` ที่ต้องแยกต่อ shard) — ทุก shard ควรเขียน evidence ลงที่เดียว
  ให้ Lead หาเจอที่เดียว ไม่ใช่กระจายคนละโฟลเดอร์ต่อ shard
- ผลลัพธ์นี้**ตรงกับ `$TAKKUB_ARTIFACTS_DIR/screenshots`** ที่ role prompt ทุกตัวใช้
  อยู่แล้ว (`pane_env.py:_apply_artifacts_dir`) — relative filename กับ absolute
  path (ที่ role ส่งตอนนี้เป็น workaround) จะลงเอยที่เดียวกัน

### หลักฐาน: regression tests ใหม่

`tests/test_browser_mcps.py::TestBrowserProfileOutputDir` (5 tests) — พิสูจน์:
`--output-dir` ชี้ไปที่ `screenshots/` subdir ของ artifacts dir, path ตรงกับ
`TAKKUB_ARTIFACTS_DIR` convention เป๊ะ, chrome-devtools ไม่ได้ flag นี้, shard
ต่างกันได้ output-dir เดียวกัน (แต่ user-data-dir ยังแยกกัน), idempotent (ไม่ append
ซ้ำ)

---

## #182 — screenshot พลาด (ว่าง/ซ้ำ) ไม่ถูกตรวจจับ

### สถานะ: **ครึ่งหนึ่งแก้แล้วมาก่อน (#159), อีกครึ่งเพิ่มใหม่วันนี้ (duplicate-content)**

ตรวจ `orchestrator.py:_scan_done_evidence`/`_evidence_format_entry` แล้วพบว่า
**ข้อเสนอ 1-2 ของ #182 (size check + แสดงขนาดในรายการ) ถูกแก้ไปแล้วก่อนหน้านี้ผ่าน
issue #159** (`_EVIDENCE_SUSPECT_MIN_BYTES = 10*1024`, `_evidence_looks_valid_image`
magic-byte sniff, tag `⚠small`/`⚠bad-header` ต่อไฟล์) — มี test คลุมอยู่แล้วที่
`TestSuspectCaptureFlagging` และ role prompt (`#159 — เช็คก่อนรายงาน` section ใน
qa/critic role file) ก็มีขั้นตอน self-check ตามข้อเสนอ 3 อยู่แล้วเช่นกัน

**สิ่งที่ #182 ยังไม่ครอบ:** เคสจริงที่ผู้ assign ระบุมา (critic ส่งภาพ 3 ใบที่
**ซ้ำกันทั้งไบต์/md5 เดียวกัน** — `r3_06_confirm.png` ถูกตรวจจับด้วย ls -la ว่าเล็ก
ผิดปกติ 5.0KB) — การเช็ค size/header ต่อไฟล์เดี่ยวๆ **ไม่มีทางจับ duplicate-content
ข้ามไฟล์ได้** เพราะแต่ละไฟล์ที่ซ้ำกันอาจผ่าน size/header check ได้ปกติทุกใบ (ไฟล์
5KB ที่ diff กันหลุดผ่านเพราะ magic bytes ถูกต้อง ขนาดอาจจะเกิน suspect floor ก็ได้
— ปัญหาคือ "เนื้อหาซ้ำกัน" ไม่ใช่ "ไฟล์เสีย")

### การแก้ที่เพิ่มวันนี้: md5 dedup detection ในชุด evidence เดียวกัน

`orchestrator.py`:
- เพิ่ม `_evidence_content_hash(path, size)` — md5 ของทั้งไฟล์, cap ที่ 8MB
  (`_EVIDENCE_DEDUP_MAX_BYTES`) กัน I/O ช้าถ้ามีไฟล์ใหญ่ผิดปกติหลุดเข้ามา (screenshot
  ปกติเล็กกว่านี้มาก)
- `_scan_done_evidence` คำนวณ hash ของทุกไฟล์ใน batch ที่จะแสดง (`newest`, cap
  `_EVIDENCE_MAX_FILES` = 10 อยู่แล้ว) — ไฟล์แรกที่เจอ hash นั้น (เรียงจาก mtime
  ใหม่สุดก่อน) ไม่ถูกแท็ก ไฟล์ถัดๆ ไปที่ hash ซ้ำกันถูกแท็ก `⚠dup-of:<ชื่อไฟล์แรก>`
- แท็กนี้**รวมกับ** `⚠small`/`⚠bad-header` เดิมได้ (เช่น `⚠small+dup-of:x.png`) —
  ไม่ replace mechanism เดิม เป็นการเพิ่มเข้าไป

### หลักฐาน: regression tests ใหม่

`tests/test_done_evidence.py::TestDuplicateContentFlagging` (6 tests) — พิสูจน์:
ไฟล์ 3 ใบ byte-identical → ใบล่าสุด (ตาม sort order ของ scan) ไม่ถูกแท็ก อีก 2 ใบถูก
แท็ก `dup-of`, เนื้อหาต่างกันไม่ถูกแท็กผิด, แท็ก dup รวมกับ small ได้, ไฟล์เกิน
dedup cap ถูกข้าม (`_evidence_content_hash` คืน `None`), hash เสถียรสำหรับไฟล์เนื้อ
เดียวกัน, `_evidence_format_entry` ใส่ tag `dup-of:<name>` ถูกต้อง

**หมายเหตุ side-effect:** พบว่า test helper เดิม `_touch_old_enough` (ใน
`test_done_evidence.py`) เขียน content คงที่ `b"fake-image-bytes"` ให้ทุกไฟล์ที่
เรียกมัน — ทำให้ test เดิม 2 ตัวที่สร้างไฟล์ 15 ใบด้วย helper นี้ (`test_max_files_cap`,
`test_shared_tag_does_not_break_max_files_cap`) เจอ dup-of tag ทุกใบไปด้วย (คาดว่า
ไม่ตั้งใจมาก่อน — ไม่เคยมีใครทดสอบ duplicate-content จนวันนี้) แก้โดยทำให้ helper
เขียน content unique ต่อชื่อไฟล์ (`b"fake-image-bytes:" + name`) — ไม่กระทบ
intent เดิมของ helper (แค่ "ไฟล์ settled แล้ว, อายุพอที่จะนับเป็น evidence")

---

## ไฟล์ที่แก้/เพิ่ม

**Production:**
- `src/agent_takkub/shared_dev_tools.py` — `--output-dir` templating (#178)
- `src/agent_takkub/orchestrator.py` — duplicate-content detection (#182)
- `src/agent_takkub/pipeline_executor.py` — browser-shard spawn stagger (#177 mitigation)
- `src/agent_takkub/cli_server.py` — browser-shard spawn stagger สำหรับ plain `--shards` path (#177 mitigation)

**Tests (ใหม่/แก้):**
- `tests/test_browser_mcps.py` — `TestBrowserProfileOutputDir` (ใหม่, 5 tests)
- `tests/test_done_evidence.py` — `TestDuplicateContentFlagging` (ใหม่, 6 tests) + แก้ `_touch_old_enough` helper
- `tests/test_issue_167_adhoc_instance_isolation.py` — ไฟล์ใหม่ทั้งไฟล์ (6 tests)
- `tests/test_cli_server.py` — `TestBrowserShardSpawnStagger` (ใหม่, 4 tests)
- `tests/test_qa_plan_fanout.py` — `TestFireFanoutStagger` (ใหม่, 2 tests)

**รวม: 23 test ใหม่, targeted suite ทั้งหมด (264 tests ข้าม 9 ไฟล์ที่เกี่ยวข้อง) ผ่าน
100%, ruff clean, import-linter 23/23 kept.**

## สิ่งที่ยังไม่ปิด

**#177** ยังไม่มี live evidence ว่า mitigation (widen stagger) แก้ปัญหาจริง —
ต้องให้คนที่มี cockpit จริง (real multi-pane fan-out, ไม่ใช่ worktree เดี่ยว) รัน
`qa --plan --shards 3` จริงแล้วสังเกตว่า MCP connect สำเร็จทุก shard หรือไม่ ถ้ายัง
พังอยู่ ต้องกลับไปพิจารณา H2/H3 ต่อ (ไม่ใช่แค่เพิ่ม stagger อีก)
