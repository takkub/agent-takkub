# Issue #146 — Playwright MCP ไม่ connect บน qa `--plan --shards N` panes

วันที่ตรวจ: 2026-08-04
ผู้ตรวจ: backend (static-only — ไม่มีสิทธิ์รัน browser driver / ไม่มี cockpit instance จริงให้ repro สด)
สถานะ: **ยังปิดไม่ได้ — ต้อง repro สด** (ไม่พบบั๊กจาก static trace ทั้ง argv/env/config generation)

## ขอบเขตที่ตรวจ

ไล่ code path เต็มของ `qa --plan --shards N` ตั้งแต่ planner pane → `_fire_qa_plan_fanout` →
`assign()` → `spawn()` → argv/env construction เทียบกับ path ของ `qa` เดี่ยว (ไม่ shard) บรรทัดต่อบรรทัด
เพื่อหาจุดที่สอง path แตกต่างกัน — ไม่ได้ live-repro (ไม่มี cockpit instance รันอยู่ในบริบทนี้ และ
role file ห้ามลง/รัน browser driver เอง — เป็นหน้าที่ qa)

**ตัดออกไปแล้วก่อนเริ่มงานนี้ (ผู้ assign ระบุมา):** per-shard config file generation — ตรวจแล้วว่า
`~/.agent-takkub/runtime/shared-mcp-<project>-qa-shard{1,2,3}.json` ถูกสร้างครบ มี `playwright` +
`chrome-devtools` พร้อม `--user-data-dir` แยกต่อ shard จริง (`shared_dev_tools.py:278-319`)

## สิ่งที่พิสูจน์แล้วว่า "ไม่ใช่สาเหตุ" (static, มี line ref)

### 1. `--mcp-config` argv ถูก inject เข้า shard pane ถูกต้อง เหมือน qa เดี่ยวทุกกรณี

`spawn_engine.py:2127` เรียก `mcp_argv_for_provider("claude", base_role, shard_idx, project_ns)`
เป็น**โค้ดเส้นเดียวกัน**ทั้ง shard และ non-shard — ต่างกันแค่ค่า `shard_idx` (int สำหรับ shard, `None`
สำหรับ pane เดี่ยว) ที่มาจาก `_split_shard(role_name)` ที่บรรทัด 1145 ของไฟล์เดียวกัน

Dispatch chain (ยืนยันแล้วว่า claude ในโปรเจคนี้ = provider ของ role `qa`, ดูข้อ 4):
- `mcp_bridge.py:238` → `_claude_mcp_argv(base_role, shard_idx, project_ns)`
- `mcp_bridge.py:150-157` → เรียก `shared_dev_tools.browser_profile_mcp_config_path(base_role, shard_idx, project_ns)`
  แล้ว return `["--mcp-config", cfg_path, "--strict-mcp-config"]` — ไม่มี branch ไหนข้าม flag นี้เมื่อ
  `shard_idx is not None`
- `shared_dev_tools.py:258-324` — `shard_suffix = f"-shard{shard_idx}" if shard_idx is not None else ""`
  เขียนไฟล์ output แยกต่อ shard (`shared-mcp-<project>-qa-shard<N>.json`) แต่ **อ่าน** base config
  (`shared_mcp_config_path_for_role(base_role)`, บรรทัด 282) — เป็น read-only จากไฟล์ role-variant กลาง
  (`shared-mcp-qa.json`) เหมือนกันทุก caller ไม่มี write-write race ระหว่าง shard เพราะแต่ละ shard เขียนคนละไฟล์ output

**สรุป:** argv ที่ shard pane ได้รับ = `--mcp-config <shard-specific-path> --strict-mcp-config` เหมือน
โครงสร้างกับ qa เดี่ยวทุกประการ ต่างแค่ path ปลายทาง (ซึ่งตรวจแล้วว่าเนื้อหาถูกต้องตามที่ผู้ assign ระบุ)

### 2. Provider resolution shard-aware ถูกต้อง (ไม่ mis-route ไป provider อื่น)

`provider_config.py:202` (`provider_for`) และ `:264` (`effective_provider_for`) ทั้งคู่ strip shard suffix
ด้วย `re.sub(r"#\d+$", "", role)` ก่อน resolve — เคยเป็นบั๊กจริง (finding #30 ใน
`docs/qa-reports/2026-07-12-full-system-review.md`: "provider_for/effective_provider_for do not strip
the '#N' shard suffix") **แต่ถูกแก้แล้ว** ก่อนงานนี้ — ยืนยันจาก source ปัจจุบันมี `re.sub` ทั้งสองจุด

ตรวจ config จริงของโปรเจคนี้ (`C:\Users\monch\.takkub\projects\agent-takkub\role-providers.json` = `{}`
ว่าง) → `qa` ใช้ default = `claude` ทั้ง shard และไม่ shard เหมือนกัน ไม่มีทาง route ไป codex/gemini
โดยไม่ตั้งใจที่จะทำให้ `--mcp-config` ไม่ถูกส่ง (เพราะ codex ใช้ adapter คนละแบบ — `mcp_bridge.py:239`)

### 3. Env vars ที่ shard pane ได้รับครบเท่า pane เดี่ยว

`spawn_engine.py:1876` — `env = _build_pane_env(project_ns)` เรียกครั้งเดียวเหมือนกันทุก role/shard
(`pane_env.py:151-178`) ซึ่งครอบ `_apply_mcp_timeout` (`MCP_TOOL_TIMEOUT=180000` ผ่าน `setdefault`,
`pane_env.py:295-304`), `_apply_non_interactive_env` (`npm_config_yes=true` กัน npx ถาม y/N,
`pane_env.py:307-325`), `_apply_port_file`, `_apply_color_term` — **ไม่มี branch แยกสำหรับ shard**
ส่วนที่ shard-specific เพิ่มเข้ามาทีหลังคือ `TAKKUB_SHARD`/`TAKKUB_SHARD_TOTAL`/`TAKKUB_BASE_ROLE`
(`spawn_engine.py:1880-1888`) ซึ่งเป็น env เสริมสำหรับ agent รู้ตัวตน ไม่ได้ไปแทนที่หรือ mask
`MCP_TOOL_TIMEOUT`/`npm_config_yes` ที่ตั้งไว้ก่อนหน้า

### 4. `role_mcp_allowlist`/`_role_variant_path` ใช้ `base_role` ("qa") ไม่ใช่ role_name ที่มี "#N" ทุกจุด

`shared_dev_tools.py:481-497` — ทั้ง `_role_variant_path(role)` และ `role_mcp_allowlist(role)`ถูกเรียก
ด้วย `base_role` เสมอ (ไล่จาก `browser_profile_mcp_config_path` บรรทัด 282 ย้อนขึ้นไปถึง
`mcp_argv_for_provider(..., base_role, ...)` ที่ `spawn_engine.py:2127`) — ไม่มีจุดไหนเผลอส่ง
`"qa#1"` เข้า policy lookup (ซึ่งถ้าเกิดขึ้นจะ fallback เป็น "role ไม่มี policy" → master passthrough
หรือ deny-all แล้วแต่ค่า `allowed`, แต่ **ไม่เกิด** เพราะพิสูจน์แล้วว่าทุก caller ส่ง base_role)

## Hypothesis ที่เหลือ — ต้อง live repro เท่านั้น (เรียงจากน่าจะเป็นมากสุด)

ทั้งหมดด้านล่างนี้เป็น **hypothesis ที่มีหลักฐานสนับสนุนทางอ้อม แต่ยังไม่ผ่านการพิสูจน์/หักล้างจริง**
ห้ามถือเป็นข้อสรุป

### H1 (แนวโน้มสูงสุด): concurrent MCP cold-start ชน claude's internal MCP connection window ภายใต้ CPU/IO contention

**หลักฐานสนับสนุนที่มีอยู่แล้วในโค้ด/เอกสาร (ไม่ใช่เดาลอยๆ):**
- `shared_dev_tools.py:86-93` (comment เดิม, มาก่อนงานนี้): เวอร์ชัน browser MCP ถูก pin เพราะ `@latest`
  "can take long enough on a cold Windows machine to blow past **claude code's MCP startup window** —
  the server then shows up as 'not connected'"
- `shared_dev_tools.py:120-147` (`warm_browser_mcps`) มีอยู่แล้วเพื่อลด cold-start latency นี้ **แต่ทำงาน
  แค่ครั้งเดียวตอน `Orchestrator.__init__` (cockpit boot)** — เรียกจาก `orchestrator.py:587` จุดเดียว
  ไม่ได้ re-warm ต่อ shard/ต่อ assign — ดังนั้นการ warm นี้ช่วยแค่ "ครั้งแรกที่ pane ไหนก็ตามเรียก
  playwright MCP หลัง cockpit เพิ่งเปิด" ไม่ได้ช่วยเรื่อง **หลาย MCP process (2 ตัว/shard ×
  N shards = 2N process) แข่งกัน spawn Node + resolve ภายในหน้าต่างเวลาสั้นๆ เดียวกัน**
- `docs/qa-reports/2026-07-12-full-system-review.md` finding #29 ยืนยันว่า cold-start MCP timeout เป็น
  failure mode ที่เคยเกิดจริงในโปรเจคนี้ (แม้ finding #29 เองจะถูกแก้แล้ว — เรื่อง `shutil.which` ไม่ใช่
  เรื่อง concurrency)
- Fan-out stagger เพียง `_SPAWN_STAGGER_MS = 400ms` (`pipeline_executor.py:38`, ใช้ค่า default เพราะ
  ไม่มี env override) ระหว่างการ spawn *pane* — claude.exe ของแต่ละ shard เองใช้เวลาหลายวินาทีกว่าจะถึง
  ขั้นตอน MCP init ดังนั้นในทางปฏิบัติ MCP-init ของ 3-4 shard พร้อมกันมีโอกาสสูงที่จะ overlap เกือบเต็มช่วง
  ไม่ใช่ทยอยจริงตามที่ 400ms stagger ตั้งใจ

**ทำไมยังพิสูจน์ไม่ได้แบบ static:** claude code's MCP **connection/startup** timeout (ต่างจาก
`MCP_TOOL_TIMEOUT` ที่เป็น per-call timeout หลัง connect แล้ว — ดู `pane_env.py:295-304` docstring) ไม่มี
flag/env ให้ configure ในโค้ด cockpit นี้เลย (grep `spawn_engine.py` หา `mcp.*timeout`/`--timeout` ไม่เจอ
argv ไหนตั้งค่านี้) — เป็นค่าคงที่ภายใน claude.exe binary เอง อ่าน source ของ cockpit ต่อไปไม่ช่วยพิสูจน์
ต้องวัดเวลาจริงตอน N shard MCP-init พร้อมกัน

### H2: Node/Chromium process contention ทำให้ MCP handshake ช้าเกิน window เฉพาะตอนมี N ≥ 2 พร้อมกัน

Playwright MCP server เอง (ไม่ใช่ browser มันเปิด) ต้อง boot Node process + require ทั้ง package ก่อน
ตอบ stdio handshake — ถ้าเครื่องมี N=3-4 ชุด (Node MCP process × 2 ต่อ shard) spawn พร้อมกันจริง อาจกิน
CPU/memory มากพอที่แต่ละตัว boot ช้ากว่าปกติ โดยเฉพาะเครื่องนี้มีประวัติ memory pressure มาก่อน (ดู
project memory `devbox-memory-tuning-2026-07-23.md` — pagefile เคยถูก pin 32GB) เป็น**หลักฐานเชิงบริบท
ของเครื่องนี้โดยเฉพาะ ไม่ใช่หลักฐานของบั๊ก** — อาจไม่เกิดบนเครื่องอื่น (ตรงกับที่ผู้ assign บอกว่า
qa เดี่ยวต่อได้ปกติ = ไม่มี resource contention ตอนมี process เดียว)

### H3 (น่าจะเป็นน้อยสุด): npx local-cache lock contention บน Windows เมื่อหลาย `npx -y @playwright/mcp@<pinned>` รันพร้อมกัน

Version ถูก pin แล้ว (`_PLAYWRIGHT_MCP_VERSION = "0.0.75"`, `shared_dev_tools.py:101`) ดังนั้น npx ไม่ควร
ต้องออก network round-trip ไป npm registry ทุกครั้ง (ตาม comment บรรทัด 86-93) — แต่ npx ยังต้องอ่าน/ล็อก
`_npx` cache dir (`%LOCALAPPDATA%\npm-cache\_npx` บน Windows) เพื่อ resolve/verify tarball ที่ cache ไว้
หลาย process พร้อมกันอาจชน file lock ของ npm cache บน Windows (filesystem locking เข้มกว่า POSIX) —
ยังไม่เคยเจอรายงาน error message ที่ชี้ตรงนี้ (ไม่มี live log ให้ตรวจ) — severity ต่ำกว่า H1/H2 เพราะ
เนื้อไฟล์ tarball ควรถูก cache ไว้แล้วหลังการรันครั้งแรกของ project นี้ (ไม่ต้อง network/extract ใหม่)

## Repro plan (สำหรับคนที่มี cockpit + browser driver สิทธิ์รันจริง — qa role)

1. **เปิด cockpit จริง**, มีอย่างน้อย 1 project ที่ role `qa` = claude (ตรวจ `role-providers.json`
   ให้ว่างหรือไม่ override qa)
2. รัน `takkub assign --role qa --plan --shards 3 "<task ธรรมดา>"` ให้ planner pane เขียน plan แล้ว
   fan-out จริง — **อย่าเทสด้วย `--shards 1`** (ต้อง N ≥ 2 เพื่อให้เกิด concurrent spawn)
3. ทันทีที่ shard pane spawn (`qa#1`, `qa#2`, `qa#3`) → เปิด Task Manager/`Get-Process node,claude`
   คู่ขนาน จับเวลา wall-clock ตั้งแต่แต่ละ claude.exe เริ่ม spawn ไปจนถึงตอนที่ pane ขึ้น
   `mcp__playwright__*` เป็น available (หรือ error "not connected") — เทียบกับ baseline ของ
   `qa` เดี่ยว (ไม่ shard) ด้วย task เดียวกัน
4. **แยก H1/H2 ออกจาก H3:** ถ้า MCP ไม่ connect และ log/stderr ของ claude แสดง timeout error (ไม่ใช่
   ENOENT/lock error) → เอียงไปทาง H1/H2 (window timeout) ถ้าเจอ error เกี่ยวกับ file lock/EBUSY บน
   `_npx` cache path → เอียงไปทาง H3
5. **แยก H1 จาก H2:** รัน `--shards 3` บนเครื่องที่ idle (ไม่มี process อื่นกิน CPU) เทียบกับรันตอนเครื่อง
   busy (เปิด browser+IDE+อื่นๆ) — ถ้า idle แล้วยังพังเหมือนกัน → H1 (แค่จำนวน concurrent MCP-init ก็พอ
   ไม่ต้องมี contention จากภายนอก) ถ้า idle แล้วผ่าน → H2 (เครื่อง/สภาพแวดล้อมเฉพาะ ไม่ใช่บั๊กเชิงโครงสร้าง)
6. เก็บ stderr/log ของ claude.exe แต่ละ shard pane (ถ้าหาได้ — cockpit ไม่ redirect stderr ไปไฟล์แยก
   ต้องดูจาก pane transcript หรือรัน claude ตรงๆ นอก cockpit ด้วย argv เดียวกันเพื่อเห็น stderr เต็ม)

## ถ้า H1/H2 ถูกยืนยันจริง — แนวทางแก้ที่ควรพิจารณา (ยังไม่ implement เพราะยังไม่พิสูจน์)

- เพิ่ม `_SPAWN_STAGGER_MS` เฉพาะ browser-role shard fan-out (`_fire_qa_plan_fanout`,
  `pipeline_executor.py:647-668`) ให้กว้างขึ้นเป็นวินาทีระดับ (คล้ายที่ `_CODEX_SPAWN_STAGGER_MS`
  แยก tier ไว้แล้วที่ `pipeline_executor.py:39`) เพื่อให้ MCP-init ของแต่ละ shard ไม่ overlap
- ขยาย `warm_browser_mcps()` ให้ warm ต่อ shard-count ที่กำลังจะ fan-out (ไม่ใช่แค่ตอน boot) — ยิง
  warm-up N รอบคู่ขนานก่อน fan-out จริงเพื่อให้ npx cache ทุก path ที่จะใช้ hot ก่อน claude.exe
  เริ่ม spawn จริง
- ถ้าเป็น H3 (npx lock) — serialize เฉพาะขั้น npx resolve (ไม่ใช่ทั้ง MCP session) ด้วย lock file ง่ายๆ
  ก่อน spawn แต่ละ shard

## บทสรุป

Static trace ครบทั้ง argv construction, provider resolution, env injection, และ policy lookup — **ไม่พบ
บั๊กเชิงโครงสร้าง** ทุก code path ที่ shard pane ใช้เหมือนกับ qa เดี่ยวทุกประการ ยกเว้นค่าที่ตั้งใจให้ต่าง
(shard-specific config path, TAKKUB_SHARD env) ซึ่งตรวจแล้วว่าถูกต้อง สาเหตุที่เหลือทั้งหมด (H1/H2/H3)
เป็นเรื่อง **timing/resource contention ตอน runtime** ที่พิสูจน์ได้เฉพาะด้วยการ repro สดเท่านั้น — งานนี้
**ยังปิดไม่ได้** ต้องส่งต่อให้ role ที่มีสิทธิ์รัน browser driver จริง (qa) ทำตาม repro plan ข้างบน
