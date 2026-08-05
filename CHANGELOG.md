# Changelog

All notable changes to agent-takkub. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.46] - 2026-08-05

### Added (เพิ่ม)
- **graft = แผนที่โค้ดให้ทุก pane** — เสียบ [`@nanonets/graft`](https://github.com/NanoNets/Graft) `0.8.2` เป็น shared MCP ตัวที่ 5 ต่อจาก playwright/chrome-devtools/context7/notebooklm · pane ที่ต้องอ่านโค้ด (frontend/backend/mobile/devops/qa/reviewer/critic) ถาม graph ได้แทนการ grep แล้วเดา — `ask` (ค้น symbol), `skeleton` (API surface ของไฟล์), `callers` (ใครเรียกใคร)
  - **ใช้เฉพาะชั้น structural** (tree-sitter) — ไม่ใช้ `--deep` จึง**ไม่ต้องมี API key และไม่มีค่าใช้จ่ายเพิ่ม**
  - วัดจริงบน `src/agent_takkub` (`docs/audit/2026-08-05-graft-pilot.md`): parse Python **136/136 ไฟล์ (100%)** · build 4.68s · `skeleton orchestrator.py` = 12.5KB จากไฟล์จริง 213KB → **ลด token ~94%**
  - **macOS ใช้ได้ไม่ต้อง compile** — tree-sitter มี prebuild ครบ darwin-arm64/x64 + win32-x64 · ขอแค่ Node ≥ 20 · `takkub doctor` เช็ค/ลงให้ (opt-in ด้วย `--fix`)
  - **ไม่รัน `graft init`** เด็ดขาด — มันเขียนทับ `.claude/settings.json` + statusline ที่ cockpit จัดการเอง
  - `graft/` ถูก gitignore (regenerate ได้) — โค้ดซิงค์ข้ามเครื่องผ่าน git แต่ graph ต้อง `graft build` เองที่ปลายทาง
- **role memory จำความผิดพลาดเองแล้ว** — `done(failed=True)` เขียน 1 bullet เข้า `runtime/role-memory/<project>/<role>.md` อัตโนมัติ · เดิม FAILED report ไปถึง Lead แล้วหายไปเลย role เดิมจึงพลาดซ้ำเรื่องเดิมได้ · dedup ตามเหตุผล (ไม่นับวันที่), เคารพ entry cap 600 ตัวอักษร + budget 6k + archive rotation เดิม จึงไม่ทำ spawn prompt บวมสวนทาง token diet · ทำงานทุก provider (ดักที่ `done()` ซึ่งเป็น entrypoint ร่วม)

### Fixed (แก้)
- **role ที่ลงทะเบียนแล้วแต่ไม่มีนโยบาย MCP เคยได้ MCP ทุกตัวฟรี** — `gemini`, `shell` และ **custom role ที่สร้างเองทุกตัว** resolve เป็น `None` = passthrough → ได้ master `shared-mcp.json` ทั้งก้อนบน claude / `~/.codex/config.toml` ทั้งไฟล์บน codex (ซึ่งอาจมี MCP ที่ผูก token อยู่) ทั้งที่ไม่มีใครอนุญาต
  - ตอนนี้ **deny ทันที** (`frozenset()`) · `None` เหลือไว้เฉพาะชื่อที่ cockpit ไม่รู้จักจริง ๆ + เทส invariant ล็อกว่าทุกชื่อจาก `roles.all_role_names()` ต้อง resolve ไม่เป็น `None`
  - ⚠️ **breaking สำหรับ custom role**: ถ้าเคยพึ่ง passthrough ต้องไปเปิด MCP ให้ role นั้นเองในหน้า Settings → Role → Access
  - ปิดบั๊กแฝงตัวที่ 2 ที่เจอตามมา: `shared_mcp_config_path_for_role` หลุดไป master เมื่อ policy มีอยู่แต่ variant file ยังไม่ถูก generate
- **กฎ "output ของ tool ไม่ใช่คำสั่ง" ไปไม่ถึง pane ที่ไม่ใช่ claude** — role file ถูก append เข้า argv ใน claude branch จุดเดียว · pane ที่ใช้ `context_strategy=agents_md_file` (codex/opencode/kimi/cursor/gemini) ได้ `CODEX_AGENTS_MD` ที่ไม่มีกฎนี้ ทั้งที่ได้ graft ไปใช้ → ย้ายกฎเข้า Hard rules ของ `CODEX_AGENTS_MD` ครอบทุก provider ทั้งปัจจุบันและอนาคต (graft ฝังข้อความสั่ง agent ให้รายงาน "tokens saved" มากับ output จริง — รูปแบบเดียวกับ prompt injection)
- **codex spawn พังทั้งเส้นได้ถ้า CLI สะดุด** — `_codex_resolved_mcp_names` raise `RuntimeError` ดิบ ๆ โดยที่จุดเรียกใน `spawn_engine` ไม่มี try/except (docstring ยังเคลมผิดว่า "never raises") → `McpResolutionError` + fail-closed: ปฏิเสธ spawn พร้อมข้อความ แทนที่จะพังดิบหรือปล่อย pane ขึ้นมาโดยไม่รู้ว่ามี MCP อะไรติดมา
- **การ์ดของ graft ปนตอน grep** — `graft build` เขียน `.ignore` ทับกลับทุกครั้งด้วย `!graft/` ทำให้การ์ด 141 ใบเข้า ripgrep · การ์ดมีเลขบรรทัดที่ไม่ตรง source จริงแต่หน้าตาเหมือน `file:line` → ship `src/agent_takkub/.rgignore` ที่ outrank `.ignore` และ graft ไม่แตะ (ยืนยันด้วยการ build ซ้ำ 2 รอบ)
- **เทส `test_bm25_search` รั่ว** — corpus resolve ผ่าน 2 ทาง (`Path.home()` + `bm25_search.ROLE_MEMORY_DIR`) แต่เทสเดิม patch ทางเดียว ไฟล์ role-memory จริงบนเครื่องจึงรั่วเข้า index → autouse fixture ปิดทั้งสองทาง

### Changed (เปลี่ยน)
- **premise ของ #121 ที่เข้าใจผิดมาตลอด** — verify กับ codex-cli จริง 3 เวอร์ชันติดกัน (0.144.1 / 0.145.0 / 0.146.0): `-c mcp_servers={}` เคลียร์ inherited table ได้**ครบทุกเวอร์ชัน** และคงสภาพเคลียร์แม้ layer partial override ทับทีหลัง · ที่ #121 เจอว่า "merge ไม่ replace" คือเคส bare partial override ที่ไม่มี `mcp_servers={}` นำหน้า ซึ่ง cockpit ไม่เคยส่ง
  - gate ขั้นตอน resolve-and-disable ที่ซ้ำซ้อนด้วย `_CODEX_RESOLVE_SAFE_MIN_VERSION = (0,144,1)` — เวอร์ชันเก่าสุดที่ทดสอบจริง ไม่ใช่ 0.146.0 · ต่ำกว่านั้นหรืออ่านเวอร์ชันไม่ออก = คงพฤติกรรมระวังตัวแบบเดิม (เราไม่ได้ทดสอบทุก build ที่เคยออกมา จึงไม่เหมาว่า "codex ไม่เคย merge")
  - ผลข้างเคียง: spawn ของ codex ประหยัดไป ~80ms (เลิกยิง `codex mcp list` 440ms เหลือ `codex --version` 363ms)

### Known gaps (ยังปิดไม่ได้)
- **ยังไม่ได้ smoke pane codex ตัวจริง** — qa ไม่มีสิทธิ์ `takkub assign` (CLI gate กันไว้เอง) · unit test คุมไว้ 32 ตัว แต่ไม่เท่าของจริง
- `codex --version` ยังยิงทุก spawn (363ms) ยังไม่ cache ระดับ session
- `graft ask` เป็น lexical (BM25) ล้วนเพราะไม่ใช้ `--deep` — query ที่ไม่มีคำร่วมกับโค้ดเป้าหมายจะหาไม่เจอ ให้ fallback ไป grep
- `graft callers` under-report ได้ (เจอเคส type-annotated instantiation) — ผลลัพธ์ "no callers" **ห้ามใช้เป็นหลักฐานว่าโค้ดตายแล้ว**

## [1.0.45] - 2026-08-05

### Added (เพิ่ม)
- **`takkub search` เป็น BM25 ranking แล้ว** (#152) — เดิมเป็น grep ต้องเดาคำเป๊ะ · ตอนนี้ค้นแบบจัดอันดับความเกี่ยวข้อง (Okapi BM25) ข้าม session logs ทุกโปรเจค + role-memory archives พร้อม score/บรรทัดที่เจอ · **รองรับภาษาไทย**ด้วย character trigram (ไทยไม่มีวรรค — ไม่ต้องพึ่ง segmenter ภายนอก) · เขียนเองทั้งตัว **ไม่มี dependency ใหม่** · query สั้นเกิน/index พัง → ตกกลับ grep เดิมอัตโนมัติ, flag `--grep` บังคับโหมดเก่าได้
- **role-memory L2 archive** (#151) — เดิม entry ที่โดนตัด/ทิ้งตอน curation (token diet) หายถาวร · ตอนนี้เนื้อเต็มถูกเก็บลง `<role>-archive.md` ข้างไฟล์เดิม (ประทับวันที่ ISO, cap 200k แบบ rotate ตัวเก่าสุดออก) — **ไม่ถูก inject เข้า pane** จึงไม่กระทบ token budget แต่ค้นกลับได้ผ่าน `takkub search` · header ของไฟล์ memory ชี้ archive ให้ agent รู้
- **Evidence-cite check** — done note ของ verify roles (qa/reviewer/critic/designer) ที่สรุปลอยๆ โดยไม่อ้างหลักฐานเลย (ไม่มี path ของ report/shots/log และไม่มีผลรันจริงอย่าง "N passed"/"exit 0") จะถูก tag **`⚠ no evidence cited`** ให้ Lead เห็นใน done notice — เตือนอย่างเดียวไม่ block · เช็คที่ engine จึงไม่กิน token ของ pane, role files แตะเพียง 1–2 บรรทัด · reviewer ถูกเพิ่มเข้ากลุ่มที่ตรวจด้วย

### Fixed (แก้)
- **เครื่องที่ติดตั้งผ่าน npm ได้ CLAUDE.md ที่ pointer ด้วน** — CLAUDE.md หลัง token diet (1.0.44) อ้าง `docs/lead/cli-reference.md` / `docs/lead/patterns.md` รวม 6 จุด แต่ wheel ไม่เคย ship โฟลเดอร์ `docs/` เลย และต่อให้ ship มา path แบบ relative ก็ resolve ไม่ได้จาก cwd ของโปรเจค user (เครื่อง dev ไม่เจออาการเพราะ cwd คือ repo เอง)
  - `setup.py` stage `docs/lead/*.md` เข้า wheel แล้ว + **build จะ fail ทันที**ถ้า CLAUDE.md อ้างไฟล์ docs/lead ที่ไม่มีจริง (กัน pointer ด้วนตั้งแต่ต้นทางทุก release ต่อไป)
  - เครื่องติดตั้ง: ตอน render Lead context ระบบ rewrite `docs/lead/` เป็น absolute path ของสำเนาที่ ship มา — Lead เปิดอ่านได้จริง · เครื่อง dev พฤติกรรมเดิมไม่เปลี่ยน
  - เพิ่ม assert เข้า installed-mode gate ใน CI (build + ติดตั้ง wheel จริงทั้ง Windows/macOS ทุก commit) — ช่องโหว่แบบนี้จะไม่หลุดอีก

### Changed (เปลี่ยน)
- **README ใหม่** — เพิ่มกลุ่ม feature "Token efficiency & memory" + "Reliability & diagnostics" (pull-on-demand context, role memory + archive, BM25 search, evidence-gated QA, doctor --live, Shift+Enter multiline พร้อมเครดิต [@than-aa](https://github.com/than-aa)) + อัปเดตตารางคำสั่ง — หน้าเดียวกันนี้แสดงทั้ง GitHub และ npm

## [1.0.44] - 2026-08-05

### Added (เพิ่ม)
- **Shift+Enter / Alt+Enter ขึ้นบรรทัดใหม่ได้ในช่องพิมพ์ของ pane** (PR #149 โดย [@than-aa](https://github.com/than-aa) ทดสอบบน Mac จริง) — เดิมกดแล้ว submit ทันทีเหมือน Enter ธรรมดา ต้องพิมพ์ multiline ผ่านวิธีอ้อม
  - ทำงานแบบ **provider-aware** ผ่าน field ใหม่ `ProviderSpec.multiline_newline_seq`: claude / gemini (Ink TUI — ESC เปล่าเป็น no-op) ได้ `ESC+CR` · **codex ไม่ intercept** (ratatui ตีความ ESC เป็น interrupt/ล้างช่องพิมพ์ — จะพังแทน) · opencode/kimi/cursor ยังไม่ยืนยัน toolkit → ไม่ intercept เช่นกัน = พฤติกรรมเดิม 100%
  - Ctrl+Enter / Cmd+Enter **ไม่ถูกแตะ** — ยัง submit ตามเดิมทุก pane
- **แก้ Mac IME/ภาษาไทยกินตัวอักษรหลังสลับภาษา** (PR #149) — เพิ่ม fallback ผ่าน helper textarea จับตัวอักษรที่ xterm.js ทิ้งหลัง CapsLock/สลับ layout (Cocoa NSTextInput) พร้อม guard ครบ: ไม่ flush ระหว่าง IME composition (จีน/ญี่ปุ่น/เกาหลี/ไทย), CapsLock intercept เฉพาะ macOS, ตัวอักษรที่พิมพ์ก่อน bridge ต่อเสร็จถูก queue ไว้ flush ตามลำดับ (เดิมหายเงียบ)

### Fixed (แก้)
- **แก้ 3 blocker ที่เจอตอนรีวิว PR #149 ก่อน merge** (รีวิวเต็ม: `docs/reviews/2026-08-04-pr149-mac-ime-shift-enter.md`) — พิมพ์ backquote/`~` ไม่ได้ทุก OS (xterm custom handler คืน false โดยไม่ `preventDefault` ตัวอักษรค้างใน textarea), IME ส่งข้อความที่ compose ไม่เสร็จเข้า PTY, Shift+Enter ทิ้ง `\n` ค้างจนปิด fallback ของตัวเอง · และ blocker รอบสอง: method ใหม่ถูกแทรกกลาง `__init__` ของ `terminal_widget.py` ทำ pane เปิดมาจอว่าง (CI จับไม่ได้เพราะ GUI ไม่ construct ใน headless — เพิ่ม structural test แบบ AST กันซ้ำแล้ว)

### Changed (เปลี่ยน) — token reduction wave 3 (ลดต้นทุนเปิด pane)
- **Lead context ลด −53%** (~15.4k → ~7.2k token โดยประมาณ) — cockpit CLAUDE.md ตัดจาก ~12.1k เหลือ ~4k โดยย้ายตัวอย่าง/reference ทั้งหมดไป `docs/lead/cli-reference.md` + `docs/lead/patterns.md` ให้อ่าน on-demand · กฎ/สัญญาพฤติกรรมอยู่ครบ
- **role memory ต่อ pane โดนคุมขนาดจริงจัง** — entry ที่ agent เขียนยาวเกิน 600 ตัวอักษร (เคยเจอ paste done-report ทั้งย่อหน้า ~1,500 token ต่อ bullet) ถูกตัดเหลือประโยคแรกอัตโนมัติตอน spawn · budget ต่อไฟล์ลด 16k → 6k bytes · header ระบุกฎ "entry ละไม่เกิน 2-3 บรรทัด" กันที่ต้นทาง
- **`.claude/agents/qa.md` ตัดจาก ~4.7k → ~2.6k token** (ยาวผิดปกติเกือบ 2 เท่าของ role อื่น) — พฤติกรรมครบ: verdict rubric, mb surface, shard mode, blocked protocol
- **snapshot `CLAUDE.spawn-*.md` เก่าถูกเก็บกวาดอัตโนมัติ** ตอน spawn (เก็บ 3 ตัวล่าสุดของ pane ที่ปิดแล้ว ไม่แตะ pane ที่เปิดอยู่)
- **ถอด notebooklm MCP ออกจาก Lead** (แทบไม่ได้ใช้ — ลด ~20 tools + instruction block ทุก session)

## [1.0.43] - 2026-08-04

### Fixed (แก้)
- **ข้อความสั้นที่มีหลายบรรทัดถูกตัดกลางทาง** (จาก PR #149 โดย [@than-aa](https://github.com/than-aa)) — LF ดิบที่เขียนลง PTY **คือปุ่ม Enter** ที่ชั้น TUI (`lead_draft_state._ENTER_BYTES` นับ `0x0A` เป็น submit) และ `_sanitize_pane_text` ก็จงใจเก็บ `\n` ไว้สำหรับ task หลายบรรทัด · ผลคือ payload ที่ **สั้นกว่า 200 ตัวอักษรแต่มีหลายบรรทัด** จะถูก submit ตั้งแต่ `\n` แรก แล้วมาถึงแบบขาดเป็นท่อน — เคสที่เจอจริงคือ done-notice หลายอันที่ถูกรวมเป็นก้อนเดียว (`lead_inbox.py:1669`) และข้อความจาก `takkub send`
  - ตอนนี้ payload ที่มีหลายบรรทัด **ห่อ bracketed paste เสมอไม่ว่าสั้นแค่ไหน** → ทั้งก้อนถึงปลายทางพร้อมกัน
  - ผลข้างเคียงที่ตั้งใจ: notice สั้นหลายบรรทัดเปลี่ยนไปใช้ `_PASTE_ENTER_DELAY_MS` (200 ms → 800 ms) มี test pin ไว้แล้วว่าเป็นความตั้งใจ ไม่ใช่ drift
  - ไม่กระทบ provider ตัวไหน — `_paste_payload` ไม่อ่าน provider spec เลย ทุก provider ได้ bracketed paste กับ task spec ปกติอยู่แล้ว
- **มือถือเห็นจำนวน pane ของทั้งทีม ทั้งที่ตั้งให้ mirror เฉพาะ Lead** — `api.pulse()` เป็น reader ตัวเดียวที่ **ตกหล่น** ตอนเพิ่ม `LEAD_ONLY_STREAM` เมื่อ 2026-07-23 (`api.activity` กับ `notify.LeadNotifier` ถูก gate ไปแล้ว) → `total` ยังนับทุก pane ที่เปิดอยู่ และ `working` ยังสะท้อนว่า teammate กำลังทำงาน · ตอนนี้ scope ลงเหลือเฉพาะ entry ของ Lead (`total` เป็น 1 หรือ 0 เท่านั้น ไม่บอกขนาดทีม) · PWA ที่ ship อยู่ไม่ได้เรียก endpoint นี้แล้ว (ใช้ `/api/activity`) แต่ route ยัง live + authenticated จึงยังเป็นช่องรั่วจริงของ API surface
- **error ตอน `--cwd` ผิด โชว์ path ซ้ำสองรอบ** (#150) — โปรเจคที่ตั้ง path ไว้ตัวเดียว (เช่น cockpit เอง) จะได้ "project root" เป็นค่าเดียวกับ configured path อยู่แล้ว เพราะ common parent ของ path เดียวคือตัวมันเอง · ตอนนี้ติดป้าย `(project root)` ที่บรรทัดเดิมแทนการต่อท้ายซ้ำ · โปรเจคหลาย path ยังเห็น root เป็นบรรทัดเพิ่มเหมือนเดิม

### Notes (หมายเหตุ)
- **PR #149 ยังไม่ merge ทั้งก้อน** — รับมาเฉพาะฝั่ง Python (ข้อแรกด้านบน) ส่วน `terminal.html` ติด 3 blocker ที่กระทบผู้ใช้ทุก OS ไม่ใช่แค่ Mac: พิมพ์ `` ` ``/`~` ไม่ได้ (xterm `attachCustomKeyEventHandler` คืน `false` ไม่ได้เรียก `preventDefault` ตัวอักษรเลยค้างใน helper textarea), CJK IME ส่งข้อความที่ยัง compose ไม่เสร็จเข้า PTY, และ Shift+Enter ทิ้ง `\n` ค้างจนปิด fallback ของตัวเอง · รายละเอียดเต็ม: `docs/reviews/2026-08-04-pr149-mac-ime-shift-enter.md`
- **#146 (Playwright MCP บน `qa --plan --shards`) ยังเปิดอยู่** — repro รอบใหม่บนเครื่อง dev: 3 shard แยก config + แยก browser profile จริง แล้ว **connect ได้ทั้ง 3** → per-shard isolation ไม่ใช่สาเหตุ · ระหว่างทางเจอ false lead ที่บันทึกไว้กันคนหลังหลงซ้ำ: `RUNTIME_DIR` ของ dev checkout (`<repo>/runtime/`) กับ installed (`~/.agent-takkub/runtime/`) เป็นคนละที่ ทำให้ดูเผินๆ เหมือนไฟล์ per-shard ไม่ถูกสร้าง

## [1.0.42] - 2026-08-04

รอบนี้เก็บบั๊กจาก issue queue ที่ค้างอยู่ทั้งหมด **8 เรื่อง** (#127, #139–#145) — ส่วนใหญ่เจอตอนใช้งานจริงกับโปรเจค wash-locker เมื่อ 4 ส.ค.

### Fixed (แก้)
- **งานทั้งระบบค้าง 47 นาที — assign แล้วไม่มี pane ขึ้นเลย ต้อง `takkub restart` อย่างเดียวถึงหาย** (#139) — อาการ: สั่ง `takkub assign` 6 รอบ pane โชว์ `empty` ตลอด → root cause: การเรียก **native PTY spawn** (pywinpty/ConPTY) บล็อกยาว **56.9 นาที** (`spawn_native_ms=3,412,178`) โดยถือธง `_spawn_in_progress` ของ FIFO arbiter ไว้ตลอดช่วงนั้น · คิว spawn ถูกระบายได้จาก `finally` ของ spawn เท่านั้น ไม่มีทางเข้าอื่นเลย และ watchdog เป็น diagnostic-only โดยเจตนา → ไม่มีอะไรกู้ได้เอง
  - แก้: หุ้ม native call ด้วย `spawn_pty_bounded()` + `PtySpawnTimeout` (worker thread + join แบบมี timeout · default **30 วินาที** ปรับได้ด้วย `TAKKUB_PTY_SPAWN_TIMEOUT_SEC`) — ครอบทั้ง **Windows (pywinpty) และ macOS/Linux (ptyprocess)** เหมือนกัน · ถ้า timeout แล้ว native call เสร็จช้าตามมาทีหลัง ตัว worker จะ `terminate(force=True)` process นั้นทิ้งเอง ไม่ปล่อยเป็น ghost
  - เพิ่ม **escape hatch ของคิว**: งานที่ค้างหัวคิวเกิน **120 วินาที** (`TAKKUB_SPAWN_QUEUE_STUCK_SEC`) จะถูกปล่อย arbiter + drain คิวอัตโนมัติ โดยเช็คบน tick 5 วินาทีของ idle watchdog (ไม่ต้องรอ spawn ตัวใหม่มาสะดุด)
  - log ที่เคยจมอยู่ใน `events.log`: เพิ่ม `spawn_native_slow` (เกิน 5 วินาที) และ `spawn_native_failed` (พร้อมเวลาที่ใช้จริง) แยกออกมาให้ grep เจอ
  - \+ 3 ไฟล์เทสใหม่ (`test_pty_spawn_timeout.py`, `test_pty_session_spawn_timeout.py`, `test_spawn_queue_stuck.py`)
- **assign ที่ตกเข้าคิวแล้วค้าง — Lead ไม่ได้รับสัญญาณอะไรเลย** (#140) — เดิมแจ้ง Lead เฉพาะตอน spawn **fail** (`[spawn-failed]`) ส่วนทางที่เข้าคิวคืนค่า "queued" แบบเงียบๆ ไม่มี notice ผูกอยู่เลย → Lead ต้องไปไล่อ่าน `runtime/events.log` เองถึงรู้ว่างานไม่ได้ออก · ตอนนี้มี **`[spawn-stuck]`** เด้งเข้า Lead (นับเป็น blocking notice แบบเดียวกับ `[spawn-failed]` — ข้ามคิว digest) พร้อมบอกว่า retry ให้อัตโนมัติแล้ว
- **งานที่ส่งให้ pane ที่กำลังยุ่ง เงียบไป 30 นาทีก่อนจะแจ้ง** (#144) — เดิมตอนเข้าโหมดรอ pane ว่าง ระบบ log แค่ event ไม่แจ้ง Lead แล้วเงียบยาวจนชนเพดาน `BUSY_WAIT_CEILING_SEC` (30 นาที) ถึงค่อยเด้ง `[delivery-unconfirmed]` → เพิ่ม **`[delivery-busy-wait]`** แจ้งครั้งเดียวตั้งแต่วินาทีที่เริ่มรอ (ไม่ flood — ครั้งเดียวต่อการส่ง 1 ครั้ง) ส่วน notice ตอนชนเพดานยังอยู่เหมือนเดิม
- **`takkub assign --cwd <path ผิด>` ตอบ `ok: task queued` ก่อน แล้วค่อย fail ทีหลัง** (#143) — validation อยู่ใน `spawn()` ซึ่งรัน async **หลัง** CLI ตอบ ok ไปแล้ว → Lead เข้าใจว่างานออกไปแล้ว · ตอนนี้ตรวจ cwd **แบบ sync ที่ `cli_server` ก่อน ack** — ผิดคือ exit non-zero ทันที และ error บอก **valid paths ของโปรเจคนั้นทั้งหมด** (ไม่ใช่แค่บอกว่าผิด) · เพิ่มด้วยว่า **root ของโปรเจคเองใช้เป็น cwd ได้แล้ว** (เป็น parent ของทุก path ที่ตั้งไว้อยู่แล้ว) · ตัว check ใน `spawn()` ยังอยู่เป็น backstop สำหรับ caller ที่ไม่ผ่าน socket
- **`takkub issue list` บอกว่าไม่มี issue ทั้งที่เพิ่งสร้างสำเร็จ** (#142) — `issue new` (default = ลง repo agent-takkub) เขียนลง `~/.agent-takkub/.takkub_issues.json` แต่ `issue list` อ่านจาก cwd ของ pane → คนละ store กัน เกือบวินิจฉัยผิดเป็น "ข้อมูลหายหลัง restart" (จริงๆ มีครบ 11 รายการ) · แก้ให้ `list` / `close` / `show` รับ `--cockpit-bug` / `--no-cockpit-bug` แบบเดียวกับ `new` (default ตรงกัน) + **แสดงบรรทัด `scope:` บอกว่ากำลังอ่าน store ไหน** เพื่อไม่ให้ "(no issues)" เปล่าๆ อ่านเหมือนข้อมูลหาย
- **`takkub status` มี ANSI escape ดิบปนจนอ่านไม่ออก** (#145) — regex เดิมครอบไม่ครบ 3 แบบที่เจอจริง: `[?25h`/`[?25l` (private-mode `?`), `[3G` (final byte `G`), และ `]0;…` (OSC — คนละตระกูลกับ CSI) · เปลี่ยนเป็น stripper เต็มสเปค ECMA-48 ครอบทั้ง CSI + OSC
- **`--model` ไม่เช็คว่า model id เป็นของ provider นั้นจริง** (#127) — เคสจริง: ส่ง `--model claude-haiku-4-5` ให้ role ที่ map เป็น gemini → CLI เตือนแล้ว fallback ไป default เงียบๆ · ตอนนี้ **บล็อกตั้งแต่ CLI เมื่อผิด provider ชัดเจน** (เช่น `claude-*` เข้า agy) พร้อม error ที่ชี้ mapping `role → provider` ให้เห็น · ส่วน id ที่ไม่รู้จักแต่ไม่ขัดกับใคร = **เตือนเฉยๆ ไม่บล็อก** (กัน model รุ่นใหม่ที่เพิ่งออกโดนบล็อกผิด) · provider แบบ router (opencode/cursor) ที่รับ model ได้หลายเจ้าโดยออกแบบ = ไม่แตะเลย

### Added (เพิ่ม)
- **`takkub doctor --live`** (#141) — `doctor` ปกติเป็น pure-logic ไม่คุยกับ cockpit **โดยเจตนา** จึงมองไม่เห็น state ใน memory ของ orchestrator: ตอนคิว spawn ค้างอยู่จริง 4 งาน doctor ยังรายงาน "all checks passed — 31 ok" · เพิ่ม endpoint `spawn-queue-status` (read-only) + check `[spawn-queue]` ที่เรียก endpoint นั้นเฉพาะเมื่อใส่ `--live` — คิวค้างเกิน 60 วินาที = FAIL พร้อมบอกให้ `takkub restart` · **`takkub doctor` เปล่าๆ ยังไม่ต้องมี cockpit รันเหมือนเดิม 100%** และถ้า cockpit ปิดอยู่ `--live` จะขึ้น SKIP ไม่ใช่ FAIL

### Changed (เปลี่ยน)
- ruff 0.16.0 → **0.16.1** (ไม่มีโค้ดต้องแก้ตาม) และ **กัน dependabot เสนอ PyQt6 ข้าม minor/major** — PyQt6 ถูก pin ที่ซีรีส์ **6.8 LTS โดยเจตนา** (ผูกกับ check ของ `takkub doctor` ผ่าน `test_version_sync.py`) · PR #138 ที่ขยายเป็น `<6.12` ทำ CI แดงทั้ง 3 OS จึงปิดไป และแทนที่ด้วยกฎ `ignore` ใน `.github/dependabot.yml` (security update ภายใน 6.8.x ยังผ่านได้ปกติ)
- อัป GitHub Actions: `github/codeql-action` 4 → 4.37.4 (PR #137) · `actions/setup-python` 6 → 7 (PR #122)

### Notes (หมายเหตุ)
- **#146 (Playwright MCP ไม่ connect บน `qa --plan --shards`) ยังไม่ปิด** — ไล่โค้ดแบบ static ครบทั้ง argv, provider resolution, env injection และ policy lookup แล้ว **ไม่พบบั๊กเชิงโครงสร้าง** (`--mcp-config` ถูกส่งเข้า shard pane ถูกต้อง, per-shard config ถูกสร้างครบ) · สาเหตุที่เหลือเป็นเรื่อง **timing/contention ตอน runtime** ซึ่งพิสูจน์ได้เฉพาะ repro สดเท่านั้น — บันทึกหลักฐาน + repro plan ไว้ที่ `docs/audit/2026-08-04-issue-146-playwright-shards.md`
- **ต้อง restart cockpit** ถึงจะได้ engine fix ของ #139/#140/#143/#144 (โค้ดที่รันอยู่เป็นตัวเก่าใน memory)

## [1.0.41] - 2026-08-03

### Added (เพิ่ม)
- **ตั้งระดับ Effort (ความลึกในการคิด) ได้เองต่อ role** — Settings → Providers & Roles มีช่อง **Effort** เพิ่มมาข้างช่อง model ของแต่ละ role · เดิมค่านี้ hardcode อยู่ในโค้ด (`_ROLE_MODEL_TIERS`) ปรับได้ทางเดียวคือ env `TAKKUB_TEAMMATE_EFFORT` ซึ่งมีผลทั้งระบบพร้อมกัน
  - เก็บใน `role-models.json` ไฟล์เดิม (ที่เก็บ provider+model อยู่แล้ว) แบบ sparse — เลือก "(ตามค่าเริ่มต้นของ role)" = ไม่เขียนลงไฟล์ · ไฟล์เก่าที่ไม่มีค่านี้ยังโหลดได้ปกติ
  - **รู้ว่า CLI ไหนรับอะไรได้จริง**: claude รับ `low/medium/high/xhigh/max` · codex รับ `low/medium/high` (ผ่าน `-c model_reasoning_effort=`) · agy/opencode/kimi/cursor **ไม่รองรับ** (agy ฝัง effort ไว้ในชื่อ model อยู่แล้ว เช่น `gemini-3.1-pro-high`) → ช่องจะ disable พร้อมบอกเหตุผล
  - **`claude-haiku-4-5` ไม่รองรับ effort** (CLI จะ error ถ้าส่งไป) → เลือก model นี้เมื่อไหร่ ช่อง Effort ปิดทันทีและไม่ส่ง flag แม้เคยตั้งค่าไว้
  - ลำดับความสำคัญ: **ค่าที่ตั้งใน Settings > env `TAKKUB_TEAMMATE_EFFORT` > ค่าเริ่มต้นของ role**
  - เปลี่ยน provider/model แล้วรายการอัปเดตทันที · ค่าที่ใช้ไม่ได้จะไม่ถูกลบเงียบ — คงไว้จนกด Save & Apply แล้วค่อยตัดพร้อมแจ้ง

### Fixed (แก้)
- **macOS: เปิดแอปจาก Finder/Launchpad/Dock แล้วหา CLI ไม่เจอ** (จาก PR #136 โดย [@than-aa](https://github.com/than-aa) ทดสอบบน Mac Intel i7) — แอป GUI บน mac **ไม่ได้รับ PATH ของ shell** ทำให้ตอน boot มองไม่เห็น `codex` / `agy` / `opencode` / `npm` → provider ที่ตั้งไว้ถูกมองว่า "ใช้ไม่ได้" แล้ว degrade กลับเป็น claude **ทุกครั้งที่เปิดใหม่** (อาการที่ผู้ใช้เห็นคือ "ตั้ง Lead เป็น codex แล้วไม่จำค่า")
  - `ensure_gui_path()` เติม path มาตรฐานเข้า PATH ตอน boot: Homebrew ทั้ง **Apple Silicon (`/opt/homebrew/bin`) และ Intel (`/usr/local/bin`)**, nvm, fnm, n, volta, asdf, `~/.local/bin`
  - หา `npm` เจอแม้ไม่อยู่ใน PATH — ครอบทั้ง Windows (`%APPDATA%\npm`, nodejs, `npm.cmd/.exe`), macOS และ Linux
  - **แก้บั๊กเลือก Node ผิดเวอร์ชัน**: การเรียงโฟลเดอร์ nvm แบบข้อความทำให้ `v9` มาก่อน `v20` (เพราะ `'9' > '2'`) เลยได้ Node เก่ากว่า — เปลี่ยนเป็นเรียงตามเลขเวอร์ชันจริง
  - รวมโค้ดค้นหาที่เคยซ้ำกัน 3 ที่ (`claude_update.py`, `update_panel.py`, `config.py`) ให้เหลือที่เดียว
  - เอาการเรียก PATH-bootstrap ออกจาก `_provider_available()` (ถูกเรียกทุกครั้งที่ spawn/เปิด Settings) + memoize ให้ทำงานจริงครั้งเดียวต่อการเปิดแอป
- **macOS: icon ไม่ขึ้นบน Dock / App Switcher** และเพิ่ม launcher ลง `~/Applications` ให้เปิดจาก Launchpad ได้ (PR #136)
- **`_ROLE_MODEL_TIERS` ยังชี้ `claude-opus-4-8`** — ตารางนี้อยู่คนละที่กับ dropdown ใน Settings เลยตกหล่นตอนอัปรายชื่อ model รอบที่แล้ว (1.0.40) · อัปเป็น `claude-opus-5` แล้ว พร้อมคอมเมนต์ชี้จุดที่ต้องแก้คู่กันเวลามีรุ่นใหม่

### Notes (หมายเหตุ)
- เครื่อง dev และ CI (`macos-latest`) เป็น **Apple Silicon** ทั้งคู่ — path ของ Homebrew ฝั่ง **Intel (`/usr/local/bin`) ยืนยันด้วยเทสที่จำลอง filesystem เท่านั้น** ยังไม่ได้รันบนเครื่อง Intel จริง (ขอให้ผู้ส่ง PR ช่วยยืนยันไว้แล้ว)
- ส่วนที่ PR #136 แถมมาแต่ไม่รับเข้า: การแก้ `save_role_overrides()` (ตรวจแล้วไม่ได้เปลี่ยนพฤติกรรมจริง + เสี่ยงลบค่าที่สัญญาว่าจะเก็บ) — รายละเอียดในรีวิว `docs/reviews/2026-08-03-pr136-mac-intel.md`

## [1.0.40] - 2026-08-03

### Fixed (แก้)
- **หน้าต่างดำเด้งแว๊บๆ ตอนเปิด cockpit (user รายงานว่า "รู้สึกเหมือนโดนไวรัส")** — cockpit รันใต้ `pythonw.exe` ซึ่งเป็น GUI process ที่ไม่มี console ของตัวเอง · บน Windows เมื่อ process แบบนี้เรียกโปรแกรม console (git.exe / npm / node) โดยไม่ใส่ `creationflags=CREATE_NO_WINDOW` ระบบจะ **สร้าง console หน้าต่างใหม่ให้ลูกทุกครั้ง** = หน้าต่างดำแว๊บแล้วหาย (การ redirect `capture_output` ไม่ช่วย — คนละเรื่องกัน)
  - พิสูจน์ด้วยตัวเลขจริง (harness รันใต้ `pythonw`): ลูกที่ไม่มี flag → `GetConsoleWindow()` = `396036` (มี console จริง) · ใส่ flag → `0`
  - จุดที่ทำให้เห็นตอน boot: **git ของระบบ worktree** (`prune_orphan_worktrees_boot()` รันทุกครั้งที่เปิดแอป), `git ls-files` ตอนเปิดโปรเจค, เช็ค npm อัปเดตหลัง boot, และตอน spawn pane ของ codex
  - แก้ **13 จุด** ที่ขาด flag (worktree · skill scan · update panel/worker · mcp bridge · issues · browser · limit status · remote settings/tunnel · skills page) — ค่าเป็น `0` บน macOS จึงไม่กระทบฝั่ง mac
  - เหลือ **1 จุดที่ตั้งใจไม่แก้**: `takkub issue new` ที่เปิด `$EDITOR` (vim/nano/notepad) แบบพิมพ์โต้ตอบ — ต้องใช้ console จริง ถ้าซ่อนจะพิมพ์ไม่ได้ · กำกับด้วยคอมเมนต์ `# subprocess-console-ok:`
  - **กันกลับมาเป็นซ้ำ**: เพิ่มเทสที่เดินอ่าน AST ทุกไฟล์ใต้ `src/agent_takkub/` (136 ไฟล์) แล้วฟ้องทันทีถ้ามี subprocess ใหม่ที่ลืม `creationflags` (ยกเว้นได้ด้วยคอมเมนต์ระบุเหตุผล) · QA ทดสอบแล้วว่าฟ้องแดงจริงเมื่อจงใจใส่โค้ดผิด
- **แถบนับ token คิดเปอร์เซ็นต์ผิดสำหรับ model รุ่นใหม่** — ตารางขนาด context ผูกกับชื่อที่มี `[1m]` ต่อท้ายเท่านั้น ทำให้ `claude-opus-5` / `claude-sonnet-5` / `claude-fable-5` / opus-4.x / sonnet-4.6 (ซึ่ง context **1M เป็นค่า default อยู่แล้ว**) ถูกคิดเป็น 200k → pane ที่ใช้ไป 150k โชว์ ~75% ทั้งที่จริง ~15% · ตอนนี้ผูกกับชื่อ model ตรงๆ และจับ `[1m]` ด้วย regex (ครอบคลุมรุ่นใหม่ที่ยังไม่อยู่ในตารางด้วย)

### Changed (เปลี่ยน)
- **รายชื่อ model ใน Settings → Providers & Roles เป็นรุ่นล่าสุด** — ของเดิมเป็น snapshot เดือน ก.ค. และหลายตัวใช้ไม่ได้จริง · รอบนี้ดึงจาก CLI ที่ติดตั้งอยู่บนเครื่องจริง ไม่ได้เดา:
  - **claude**: `claude-opus-5` ขึ้นเป็นตัวหลัก (เดิมสุดที่ `claude-opus-4-8`) · มี `claude-sonnet-5` / `claude-haiku-4-5` / `claude-fable-5` · เก็บ `claude-opus-4-8` ไว้เป็นรุ่นเก่าที่ยังใช้ได้
  - **codex**: `gpt-5.6` เดี่ยวไม่มีแล้ว กลายเป็น `gpt-5.6-sol` / `-terra` / `-luna` (ยืนยันจาก cache ของ codex เอง) · ตัด `gpt-5.3-codex` ที่หายไปแล้วออก
  - **gemini (agy)**: เปลี่ยนจากชื่อโชว์ (`"Gemini 3.5 Flash"` ซึ่งใส่แล้วใช้ไม่ได้) เป็น token จริงที่ `--model` รับ เช่น `gemini-3.6-flash-high`, `gemini-3.1-pro-high`
  - **opencode**: `anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`
  - **kimi / cursor**: คงค่าเดิม + คอมเมนต์ระบุชัดว่า **ยังยืนยันไม่ได้** (kimi ไม่มีคำสั่งลิสต์ model, cursor CLI ไม่ได้ติดตั้งบนเครื่องนี้) — ไม่เดาให้
  - ทุกช่องยังพิมพ์เองได้เหมือนเดิม (CLI ออกรุ่นใหม่เร็วกว่ารอบ release)
- `plan_tier.PRO_LEAD_MODEL` → `claude-opus-5` (เจตนาเดิมคือ "Opus ตัวล่าสุดที่ไม่ใช่ 1M variant")

## [1.0.39] - 2026-07-28

### Added (เพิ่ม)
- **แท็บ 🌿 Git ใน Task dock — ดูสถานะ git ของโปรเจคที่เปิดอยู่ได้ในที่เดียว** · dock ขวา (chip `📋 Tasks` / Ctrl+Shift+T) แยกเป็น 2 แท็บ `📋 Tasks` / `🌿 Git`
  - tree แบบเดียวกับ task list: 1 แถวต่อ repo → **branch ปัจจุบัน · ↑ahead ↓behind เทียบ upstream · ●modified +untracked (หรือ `clean`)** กางออกเห็น **commit ล่าสุด 8 ตัว** (sha สั้น · subject · เวลาแบบ "2 hours ago") และหมวด **`wt/*` worktrees** ที่ pane isolation สร้างไว้ (commits ahead + dirty)
  - **โปรเจคที่มีหลาย repo รองรับเต็ม** — path key ทุกอันใน `projects.json` ถูกอ่านแยกกัน แล้ว **ยุบ key ที่ชี้ repo เดียวกัน** ด้วย `git rev-parse --show-toplevel` (monorepo ไม่โชว์ซ้ำ · repo แยกจริงโชว์ครบทุกตัว) · path ที่ไม่ใช่ git repo โชว์เป็นแถวสีเทาบอกเหตุ ไม่ใช่หายเงียบ
  - engine ใหม่ `git_status.py` (ไม่มี PyQt — เทสแยกได้) เรียก git ผ่าน subprocess ที่มี timeout, ไม่เด้งหน้าต่าง console บน Windows, และ**ไม่ raise ทุกกรณี** (git หาย / timeout / repo พัง → แถว error)
  - อ่าน git ใน **worker thread** เสมอ ไม่บล็อก UI · refresh เมื่อสลับโปรเจค/สลับมาแท็บ Git/กดปุ่ม ↻ และทุก 30 วิ **เฉพาะตอน dock เปิดและแท็บ Git ถูกเลือกอยู่** (ยุบ dock หรือสลับแท็บ = หยุด poll ทันที)

### Changed (เปลี่ยน)
- **Task List โชว์เฉพาะโปรเจคของ tab ที่เปิดอยู่** (ตามที่ user ขอ) — เดิมไล่ทุกโปรเจคใน `projects.json` มากองรวมกันในลิสต์เดียว ทำให้หางานของโปรเจคตัวเองไม่เจอ · ตอนนี้ผูกกับ tab ที่ active: สลับโปรเจค card เปลี่ยนตาม, card ของโปรเจคอื่นถูก**เอาออกจริง** (รวม avatar ใน rail ตอนยุบ) และ event `ledgerChanged` ของโปรเจคอื่นถูกข้าม

### Fixed (แก้)
- **#134 (HIGH) งานที่เพิ่ง spawn ถูก paste ซ้ำ 2 ก้อนติดกันจนกลายเป็นข้อความเพี้ยน** — pane ที่ preload task ผ่าน system-prompt จะได้ trigger สั้นๆ 1 บรรทัด แต่ตัวกู้ paste มองว่าหาย เลย paste ซ้ำ กลายเป็น `...block now.Start the current task from the one-shot system-prompt block now.` แล้ว submit ทั้งก้อน (หลักฐาน: `task_deliver_repaste` 2 ครั้ง 10:24:34 / 10:32:18 ตามหลัง `spawn_initial_task_preloaded` ทันที บน 1.0.38 ที่มี fix #133 ครบแล้ว)
  - สาเหตุ: ข้อความสั้นบรรทัดเดียว **ไม่มี `[Pasted text]` placeholder** ให้ตรวจ พอ CR ลงไปแล้วช่องพิมพ์ก็ว่าง → สัญญาณ "ready + ช่องว่าง" แยกไม่ออกระหว่าง *ส่งไปแล้ว* กับ *ยังไม่ได้ส่ง* → เดาผิดข้างเดียวก็ paste ทับทันที
  - trigger ตัวนี้เปลี่ยนไปใช้โหมด **CR-only** (`allow_repaste=False`) — กด Enter ซ้ำได้ แต่**ห้าม paste ซ้ำเด็ดขาด** · ถ้า trigger หายจริง idle watchdog (`[auto-reminder]`) ยังกู้ให้เหมือนเดิม · **การส่ง task ปกติไม่เปลี่ยน** (ยังกู้ paste ที่ถูกกลืนจริงตาม #26)
  - เพิ่ม log `task_deliver_verify_decision` ที่จุดตัดสินใจกู้ทั้ง 4 จุด (บันทึก ready / มีข้อความค้าง / มี output ใหม่ / เงียบมากี่วินาที) — ครั้งหน้ามีหลักฐานตรงๆ ไม่ต้องเดาจากจำนวนครั้งที่ retry
- **#135 hook ของ plugin หมดเวลาแล้วผลถูกทิ้งตอนเปิดหลาย pane พร้อมกัน** — ขึ้นข้อความแดงในจอ pane ว่า `UserPromptSubmit hook timed out after 5s — output discarded` · ต้นเหตุคือ plugin ตั้ง timeout ของตัวเองไว้ต่ำ (pordee = 5 วินาที ทั้ง `SessionStart` และ `UserPromptSubmit`) ซึ่ง node เย็นๆ ตอน fan-out หลาย pane ทำไม่ทัน
  - ก่อน inject plugin เข้า pane cockpit จะ **ยก timeout ที่ต่ำกว่า 30 วินาทีขึ้นเป็น 30** ให้อัตโนมัติ (ปรับได้ด้วย env `TAKKUB_PLUGIN_HOOK_TIMEOUT_FLOOR`) ทั้งใน `.claude-plugin/plugin.json` และ `hooks/hooks.json` · เขียนแบบ atomic และ**เขียนเฉพาะตอนค่าเปลี่ยนจริง** · ทำใหม่ทุกครั้งที่ spawn เพราะ plugin update ทับค่ากลับ · manifest พังหรือเขียนไม่ได้ = ข้ามเงียบ ไม่ทำให้ spawn ล้ม · มี log `plugin_hook_timeout_raised` ตอนยกจริง

## [1.0.38] - 2026-07-28

### Fixed (แก้)
- **#133 (HIGH) เปิดหลาย pane พร้อมกันแล้ว pane ค้างไม่เริ่มงาน** — fan-out จาก Multi mode ทำให้ trigger `Start the current task from the one-shot system-prompt block now.` ถูก **paste ทับซ้อน 4 ก้อนในช่องพิมพ์และไม่เคยถูก submit** · Lead เองก็ได้ notice ซ้ำและ **ตัวอักษรเพี้ยน** (`isolawted`, `fkontend-2`, `Imerge`) จากการเขียนชนกันใน PTY เดียว · เกิดทุกครั้งที่ fan-out
  - **สาเหตุที่ 1 — ตัดสินด้วยนาฬิกาที่หยุดเดิน:** ตัวตรวจว่า paste หายหรือไม่ อ่านจากสถานะที่ Qt main thread เป็นคนอัปเดต (จอที่ render แล้ว, timestamp ของ output ล่าสุด, ช่วง grace) · fan-out ทำให้ event loop ค้างทีละ ~1 วินาที แล้ว timer ของ verify ที่ค้างคิวไว้จะ **ยิงพรวดพร้อมกันทันทีที่คิวคลาย** ทำให้ grace ที่ตั้งใจให้กินเวลาจริงหลายวินาทียุบเหลือหลักมิลลิวินาที — paste ที่แค่ยังวาดไม่เสร็จจึงถูกตัดสินว่าหาย แล้วโดน repaste ทับ (หลักฐานจริง: `task_deliver_repaste` 3 ครั้ง/pane ห่างกัน ~1 วินาที ขนาบด้วย `main_thread_stall` 938ms / 1125ms / 1141ms)
  - ตอนนี้เช็ค heartbeat ของ main thread ก่อนทุกครั้งที่จะตัดสิน ถ้าเพิ่งค้างจะ **เลื่อนการตัดสินโดยไม่กินโควตา repaste** (จำกัด 4 ครั้ง) — paste ที่หายจริงยังกู้ได้เหมือนเดิม
  - **สาเหตุที่ 2 — เขียนชนกัน:** log มี `remaining:3` ซ้ำ = มี verify chain 2 เส้นเขียนเข้า Lead พร้อมกัน · `_pump_lead_notify` กับ `_force_deliver_done_notices` ใช้ guard ร่วมกันแล้ว ปล่อยคืนผ่าน callback ตอน chain จบ ทำให้เขียนได้ทีละเส้น · เคสที่ถูกกันไว้จะไม่ดึงคิวออก ของยังอยู่ครบให้รอบถัดไปส่ง

## [1.0.37] - 2026-07-27

### Fixed (แก้)
- **#132 (HIGH · แก้ของที่หลุดไปกับ 1.0.36) boot sweep อาจลบงานที่ยังไม่ commit** — `prune_orphan_worktrees_boot()` รันเองทุกครั้งที่เปิด cockpit แล้วลบ orphan worktree **ทั้งโฟลเดอร์** โดยตัดสินจาก metadata ล้วน (ไม่มี `.git` / resolve repo ไม่ได้ / ไม่อยู่ใน `git worktree list`) ซึ่งไม่ได้แปลว่าไม่มีอะไรจะเสีย — worktree ที่ `.git` pointer หายแต่ยังมีไฟล์ที่ไม่เคย commit จะหายถาวรโดยไม่มีใครสั่ง · เคสจริง: ตอนเก็บกวาดพื้นที่พบ orphan 3 ตัว โดยตัวหนึ่งมีไฟล์ที่ไม่ใช่ node_modules อยู่ ~96 MB
  - sweep อัตโนมัติลบได้เฉพาะสิ่งที่ **สร้างใหม่ได้แน่นอน**: โฟลเดอร์ว่าง หรือมีแต่ `node_modules` และต้อง clean + ไม่มี commit ค้างบน branch
  - orphan ที่ยังมีไฟล์ source ถูกเลื่อนไปหมวดใหม่ `orphan-worktrees-review` (ระดับ review) ต้องสั่งเองด้วย `takkub prune --level review --category orphan-worktrees-review --yes` และ CLI **พิมพ์ path ที่จะหายให้เห็นก่อนลบเสมอ**
  - `classify_worktree()` เติม `dirty`/`branch`/`ahead` ให้เคสที่ยังอ่าน git ได้ แทนที่จะ return ทันทีที่ path หลุดจาก `git worktree list`
- **#130 `delivery-unconfirmed` เตือนผิดทุกครั้งที่ pane กำลังทำงาน** — เกณฑ์เดิมคือ "ไม่ถึง ready prompt ใน 90 วินาที" ซึ่ง pane ที่กำลังรันงานยาว (เช่น full test suite 6 นาที) ก็เข้าเงื่อนไขเดียวกัน · วันที่ 2026-07-27 เตือนผิด **6 จาก 6 ครั้ง** (ทุกเคส `takkub status` แสดง state=working และจบด้วย done ปกติ) ทำให้ notice นี้ไร้ความหมาย และมันยังสั่งให้ Lead re-assign ซึ่งจะส่งงานซ้ำให้ pane ที่ทำอยู่
  - แยก busy ออกจาก stuck ด้วยสัญญาณ PTY output (provider-agnostic — codex/agy เหมือน claude ไม่ต้อง parse ข้อความ) เตือนเฉพาะเมื่อ pane เงียบต่อเนื่องเกิน `STALL_THRESHOLD_SEC` ซึ่งเป็นเกณฑ์เดียวกับที่ `takkub status` ใช้บอกว่า pane stalled
  - มีเพดานรวม `BUSY_WAIT_CEILING_SEC` (default 1800s · env `TAKKUB_BUSY_WAIT_CEILING_SEC`) กัน pane ที่ **ค้างแบบมีเสียง** (TUI วน redraw แต่ไม่รับ input) รอไม่รู้จบจนไม่มีใครรู้ — ครบเพดานจะเตือนด้วยข้อความและ log event **คนละแบบ** กับเคสเงียบ จะได้ไม่วินิจฉัยผิดทาง
- **#131 เทส watchdog flaky บน CI** — `test_single_instance_watchdog.py` ใช้ `sleep()` เวลาคงที่แล้ว assert ผลของ daemon thread ทันที ซึ่งแพ้ runner ที่ CPU ถูกแชร์ (แดงแล้วผ่านเองตอน re-run โค้ดชุดเดิม) · เปลี่ยนเป็น poll-until-condition (เพดาน 5 วินาที) 3 จุด และขยาย margin ให้เคสที่พิสูจน์ว่า "ไม่เกิด" ซึ่ง poll ไม่ได้ · ยืนยันด้วยการรัน 15 รอบ รวม 5 รอบภายใต้ CPU stress

## [1.0.36] - 2026-07-27

### Added (ใหม่)
- **`takkub disk` / `takkub prune` — ดูว่าอะไรกินพื้นที่ แล้วเลือกลบได้** (`disk_usage.py`) · `disk` รายงาน DATA_HOME แยกหมวดพร้อมป้ายความปลอดภัย **safe / review / never** (+`--json`) · `prune` **dry-run เป็นค่าเริ่มต้น** ต้อง `--yes` ถึงลบจริง เลือกได้ด้วย `--category` / `--level` / `--older-than` · หมวด `never` (venv, config, tasks, worktree ที่ยังใช้งาน) ถูกปฏิเสธเสมอแม้สั่งเอง · ทุกเป้าหมายถูก resolve แล้วเช็คว่าอยู่ใต้ DATA_HOME จริง กัน path escape
  - **หมวด `node-modules`** — สแกน `node_modules` ทุกชั้นใต้ `worktrees/` แยก **orphan** (worktree ที่ git ไม่รู้จักแล้ว = ลบได้เลย) ออกจากตัวที่ยังใช้งาน (ข้ามโดยอัตโนมัติ ต้อง `--include-live` เอง) — เคสจริงจากเครื่อง user: `~/.agent-takkub` บวมถึง **14.5 GB / 472,973 ไฟล์** โดย 8.1 GB เป็น worktree ซาก
  - **Windows long-path** — ลบผ่าน `\\?\` extended-length prefix + ปลด read-only แล้วลองซ้ำ และ **รายงานไฟล์ที่ลบไม่ออกเสมอ ไม่เงียบแล้วบอกว่าสำเร็จ**
  - boot auto-prune กวาด orphan worktree (เฉพาะระดับ safe) ต่อจาก transcript/browser-profile prune เดิม
- **agy conversation แยกตามโปรเจคแล้ว (#132)** — เดิม cockpit ไม่เคยส่ง `--project` ให้ agy ทำให้ทุกบทสนทนาตกลงถัง `default-cli-project` ถังเดียว (วัดจริง: **212 conversation จาก 11+ โปรเจคปนกัน**) กด resume ทีเดียวเห็นทุกโปรเจค · ตอนนี้ resolve project id จาก registry ของ agy เองแล้วส่ง `--project <id>` (ไม่เจอ → `--new-project` ซึ่ง agy จะจดโฟลเดอร์ให้เอง = สร้างครั้งเดียวต่อ cwd) · กันสร้างซ้ำตอน spawn ขนานด้วย in-process claim + poll registry แบบมี timeout · ทำผ่าน `ProviderSpec` (`project_scope_flag/_resolver/_new_flag`) ไม่ hardcode ชื่อ provider ใน spawn path

### Changed (ปรับ)
- **เตือน context น้อยลงมาก** — เดิมมี 3 ช่องซ้อนกัน (badge 80% + session cap + **paste ข้อความเตือนเข้า pane**) และ cap เป็นเลขตายตัว 180k ไม่ผูกกับ context window จริง ทำให้ session 1M โดนเตือนตั้งแต่ใช้ไปราว 18% (พบใน log 21 ครั้ง prompt 503k–538k) · ตอนนี้ **cap = สัดส่วนของ context window จริง** (default 0.85 · `0` = ปิด · เลขเดิมแบบ token ยังใช้ได้) · **ตัด tray toast ทั้ง 2 จุด** · **ตัดการ paste advisory เข้า pane ทิ้ง** (CLI auto-compact จัดการเองอยู่แล้ว) เหลือ status bar บรรทัดเดียวตอนข้าม cap · badge %/สีบน pane header + tab ยังอยู่ครบ

### Fixed (แก้)
- **#129 token meter อ่าน session ของ pane อื่น เมื่อหลาย role ใช้ cwd เดียวกัน** — โค้ดเลือก "ไฟล์ JSONL ใหม่สุดใน cwd" แทนที่จะยึด session uuid ของ pane ตัวเอง โดย docstring เขียนสมมติฐานไว้ว่า "one-pane-per-cwd ทำให้ปนกันไม่ได้" ซึ่งไม่จริงกับโปรเจค single-repo · เห็นสดๆ ตอน qa pane เพิ่ง spawn 2 วินาทีแล้วถูกรายงานว่าใช้ 185k tokens (เลขของ Lead) · ตอนนี้ยึด `<uuid>.jsonl` ตรงๆ ตามกติกา exact-uuid-หรือไม่แสดงเลย (ไม่เดา)
- **CI แดงมาตั้งแต่ 2026-07-24 โดยไม่มีใครเห็น** — `pyproject` ขอ `ruff>=0.7` แบบลอย ทำให้ CI ลง **0.16.0** ขณะที่เครื่อง dev อยู่ 0.15.12 และ pre-commit ปักไว้ 0.15.13 → gate ในเครื่องผ่าน แต่ CI ล้มที่ `ruff check` **ก่อนได้รัน pytest เลย** (รวมถึง commit ที่ release 1.0.32/1.0.33/1.0.35) · ตรึง `ruff==0.16.0` ให้ตรงกันทั้ง 3 ที่ + กัน ruff ไปไล่ `worktrees/**` (เครื่อง dev lint 503 ไฟล์ ขณะที่ CI เห็น 362) และ `*.md`
- **เทสที่ผูกกับสภาพเครื่องที่รัน** — พอ CI รัน pytest ได้อีกครั้งจึงโผล่มา: path แบบ Windows ที่ต้อง lowercase (แดงบน Linux ซึ่ง case-sensitive ถูกต้องแล้ว), เทสที่เรียก binary `codex` จริง, `TERM` ที่รั่วจาก shell ของ host, และ macOS runner ที่มี Chrome จริงที่ `/Applications` ชนะ fixture เสมอ (แยก path ออกเป็น const ให้ monkeypatch ได้ ค่า default ไม่เปลี่ยน) · รวมถึง `~/.gemini` จริงที่หลุดเข้าเทส 6 จุด
- **PtySession re-export หลุดหาย** ทำให้เทส 112 ตัวพัง — `orchestrator.py` เป็น re-export façade โดยเจตนา (มี per-file F401 ignore กำกับ) แต่ import ถูกลบเพราะดูเหมือนไม่ถูกใช้ · คืนกลับ + เพิ่มเทสที่ทำให้การลบ re-export แดงที่จุดเดียวแทนที่จะพัง 112 จุดกระจาย

## [1.0.32] - 2026-07-24

### Fixed (แก้)
- **#126 agy pane "ตายเงียบ" — task ค้างใน composer ไม่ถูก submit** — ระหว่าง agy ขึ้น "Signing in / Verifying your account" (Google eligibility check ฝั่ง server) จอยังโชว์ idle footer ทำ cockpit เข้าใจว่า ready → paste แล้ว Enter โดน swallow → pane นั่งเงียบจน user ต้องสั่งซ้ำเอง (เจอ 3 เคสใน 1 วัน) · ตอนนี้ marker ทั้งสองเป็น **ready hard-blocker**: cockpit รอให้พ้น check ก่อนส่ง + submit verification/resend หลังพ้น (calibrate จาก transcript จริง แนวเดียวกับที่เคยแก้ codex #99)

## [1.0.31] - 2026-07-24

### Fixed (แก้)
- **#125 (HIGH) agy pane เปลี่ยน model เองเมื่อเจอ `--effort`** — regression จาก 1.0.29: role tier effort ที่ inject ให้ agy ชนกับ model ที่ user ตั้งไว้ (slug ของ agy encode effort อยู่แล้ว เช่น `gemini-3.1-pro-low`) แล้ว agy ตอบโต้ด้วยการ**ทิ้ง model ไปใช้ default เงียบๆ** · ตัด `effort_flag` ฝั่ง agy ทิ้ง (หลักการ: flag เสริมห้ามทำ model เปลี่ยน) — claude/codex ยังได้ effort ตาม tier ปกติ
- **`browser_chrome` อ่าน env จริงของ host ทั้งที่ caller ส่ง dict ว่าง** — `env or os.environ` ทำ `{}` (falsy) fallback ไป host env → `CHROME_BIN` รั่วเข้า resolution · แก้เป็น None-check + regression test

## [1.0.30] - 2026-07-24

### Added (ใหม่)
- **Lead Inbox Digest** — done notices + peer CC ที่มาเป็น burst ถูกรวมส่งเข้า Lead เป็น**ข้อความเดียว** (window 60 วิ · `TAKKUB_INBOX_DIGEST_MS`, `0` = ปิดกลับพฤติกรรมเดิม) ลดการปลุก Lead ให้ลาก context เต็มซ้ำต่อ notice (~5.1M tokens/7วันจาก audit) · `[FAILED]` / spawn-failed / delivery-unconfirmed **ไม่เข้า digest** ส่งทันทีและแซงคิว · auto-chain handoff ถูกแนบต่อท้าย digest ใน turn เดียวกัน — ลำดับ "เห็น done ก่อน act" คงเดิม
- **เลือก model ต่อ assign ได้** (`takkub assign --model <id>`) — ยิงงาน scan/audit รอบแรกด้วยรุ่นถูก (haiku/flash) แล้ว escalate เป็นรุ่นใหญ่เฉพาะรอบ final ตามกลยุทธ์ Hybrid Tiered Scanning · precedence: **assign > role > provider > CLI default** (ชนะ `TAKKUB_TEAMMATE_MODEL` — แคบสุดชนะ) · provider ที่ไม่มี `model_flag` = error ชัดตอน assign ไม่เงียบ · มีผลเฉพาะ pane ที่ spawn ใหม่
- **Browser QA ผ่าน `mb` ใช้ได้ทุก provider บน Windows (#123)** — cockpit จัดการ Chrome lifecycle เอง (module ใหม่ `browser_chrome.py`: launch native / reuse / cleanup ผ่าน CDP 9222 ตามทางที่พิสูจน์ empirical ใน issue) · `takkub doctor --fix` ติดตั้ง mb ให้ (run ปกติ = read-only) · #92 (mb+shard ชน CDP) บังคับที่ `pane_guard` แล้ว ไม่ใช่แค่ prose

### Changed (ปรับ)
- **Idle reminder ไม่ปลุก model แล้ว** — เตือน pane ที่ลืม `takkub done` ผ่าน **UI notice (status bar + tray)** แทนการ write+Enter เข้า PTY (เดิมปลุก model turn เต็ม ~296k tokens/ครั้ง) ใช้ได้ทุก provider (#103) · ยังมี escalation: idle ต่อเนื่องเกิน N รอบ (`TAKKUB_IDLE_REMIND_ESCALATE_ROUNDS`, default 3, `0` = UI-only ตลอด) ค่อย inject PTY หนเดียว — pane ที่เสร็จจริงแล้วเงียบยังถูกดันให้รายงาน

### Fixed (แก้)
- **#121 codex pane ค้าง "Starting MCP servers" ทั้งที่ policy ไม่ให้ MCP** — MCP รั่วจาก `~/.codex/config.toml` ระดับ user เพราะ codex merge config table · ตอนนี้ role ที่ policy deny-all จะถูกปิดรายตัวผ่าน `codex mcp list --json` + `features.plugins=false` (session-scoped, fail-closed, ไม่แตะไฟล์ user) · provider อื่นยังไม่มี surface เทียบเท่า — ระบุ gap ใน spec (#103)
- **`$TAKKUB_ARTIFACTS_DIR` หายจาก pane gemini** — ย้ายการ stamp เข้า env builder (`pane_env`) โดยตรง provider branch ที่ early-return ก็ไม่ทำหลุดอีก
- **#124 ปิดแบบ invalid diagnosis** — "one-shot delivery ไม่ทำงาน" ที่รายงานไว้ แท้จริง pointer เป็น by-design ของ role ที่ map เป็น codex/agy (ไม่มี system-prompt-file) — claude pane preload ปกติมาตลอด · ได้ของแถม: assign event มี `initial_delivery_reason` แยกเหตุชัด (provider-unsupported / fallback-after-fail / preloaded) + retry/queue path hardening + integration test 3-pane ผ่าน QTimer จริง

## [1.0.29] - 2026-07-24

### Added (ใหม่)
- **ลด token ชุดแรก (จาก audit ข้อมูลจริง 7 วัน = 4.34B tokens, 98.25% เป็น cache reads)** — report เต็มที่ `docs/qa-reports/2026-07-24-token-audit.md`:
  - **session-cap watchdog** (`session_cap.py`) — pane ที่ prompt ทะลุ cap (default 180k · `TAKKUB_SESSION_CAP_TOKENS` / QSettings) เตือนแบบ edge-trigger: teammate ได้ advisory ให้จบงานก่อนแล้ว `/compact` (รอ ready prompt เสมอ ไม่ตัดกลางงาน) · Lead ได้แค่ UI notice ไม่ auto-compact เด็ดขาด · เฉพาะ claude pane (provider อื่นไม่มี JSONL ให้อ่าน — #103 gap ระบุแล้ว)
  - **one-shot spawn task delivery** — assign ที่ spawn pane ใหม่ฝัง task เต็มเข้า `--append-system-prompt-file` + trigger สั้นๆ แทน pointer→Read round-trip (~60k tokens/spawn) · pane รันอยู่/provider อื่น = pointer เดิม · **known gap #124: ยังไม่ engage ตอน staggered fan-out**
- **reasoning effort ต่อ provider** — role tier effort มีผลกับ pane ที่ไม่ใช่ claude แล้ว: agy `--effort` (1.1.5+) · codex `-c model_reasoning_effort=` · opencode/kimi/cursor ไม่มี surface = ระบุ gap ใน spec (#103)
- **Team Settings redesign** (👥 Team) ตาม control-plane mockup — ธีมดำ+gold เดิม: sidebar หมวด+SVG icons · heading kicker · card rows · matrix legend/sublabel/separator/empty-state · sticky footer พร้อม dirty dot · ผ่าน qa 3 รอบ + critic verify SHIP
- **Thai font fallback ทั้งแอป** — bundle Noto Sans Thai (OFL) + per-OS stack (Win: Leelawadee UI · mac: Thonburi) แก้ข้อความไทย/glyph เป็นกล่อง tofu จากการประกาศ font family เดียว

### Fixed (แก้)
- **#120 restart port-file drift** — successor ของ `takkub restart` เคยสืบทอด `TAKKUB_PORT_FILE` ของ PID เก่า ทำ CLI วิ่งผิด instance ("unauthorized: lead-only") · ตอนนี้ค่าที่ app ตั้งเองถูก strip (provenance marker) ส่วน override จริงจาก shell ยังรอด
- **#117 over-capacity advisory เตือนเกินจริง** — budget ลดจาก 2GB → 0.5GB/pane (วัดจริง ~350MB) + ฐาน RAM ใช้ `max(available, 25% ของ total)` กัน sample แกว่ง
- **#118 delivery-unconfirmed false positive** (ส่วนที่เหลือจาก 1.0.26) — claude pane ที่ policy ให้ MCP (qa/critic/designer) ได้ ready-wait 90s เท่า provider ช้า + ข้อความเตือนรายงานเลข wait จริง
- **ลูกศร dropdown ของ QComboBox กลับมาแสดง** — สไตล์ `::down-arrow` โดยไม่มี `image:` ทำ Qt ลบลูกศร native ทิ้ง ใส่ SVG glyph ตรงๆ (พร้อม state เปิด/disabled)

## [1.0.28] - 2026-07-23

### Fixed (แก้)
- **pane เดินอ้อม tool policy ผ่าน shell ไม่ได้อีกแล้ว** — `pane_tools_policy` คุมแค่ **MCP** แต่ทุก pane spawn ด้วย `--dangerously-skip-permissions` แปลว่า **Bash ไม่มี gate เลย**. pane `frontend` ที่ถูกปฏิเสธ browser MCP จึงติดตั้ง browser เองด้วย `npx --yes playwright` แล้วขับ Chromium จาก ad-hoc script (จับได้สดๆ 2026-07-23 พร้อม `find / -maxdepth 6 -iname playwright` ที่สแกนทั้งไดรฟ์จนเครื่องกระตุก). ปิดทางที่ถูกต้องโดยไม่ปิดทางอ้อม = agent เดินอ้อมเอง.
  - **module ใหม่ `pane_guard.py`** (pure leaf, stdlib ล้วน) — 2 rule: `browser_driver` (playwright / puppeteer / selenium / headless chrome ทุกช่องทาง: `npx`, `npm|pnpm|yarn|bun install|add|dlx`, `pip install`, `python -m`, bare invoke, inline `require()`/`import`, `chrome --headless`) และ `disk_scan` (`find /`, `find C:\`, `Get-ChildItem <root> -Recurse`)
  - **บังคับจริงที่ hook** — `takkub _guard` ต่อเป็น `PreToolUse`/`Bash` ให้ทุก claude pane (unconditional, เรียงก่อน rtk) · deny ด้วย **exit code 2 + เหตุผลทาง stderr** ซึ่งเป็น contract ที่ Claude Code ทุก build เข้าใจ (JSON `permissionDecision` เสี่ยง fail-open เงียบ)
  - **`BROWSER_ROLES` = qa / critic / designer** เท่านั้นที่ขับ browser ได้ — shard (`qa#3`) ได้สิทธิ์ตาม role แม่ · `lead` / `shell` ไม่โดน guard (user พิมพ์เอง)
  - **fail-open ทุกทาง** — role ว่าง / command ว่าง / payload พัง / guard เองพัง = ปล่อยผ่านเสมอ. hook นี้ยิงทุก Bash call จึงห้ามทำ pane ค้างเด็ดขาด
  - **อ่านยังได้หมด** — `grep playwright`, `ls ~/AppData/Local/ms-playwright`, `cat package.json` ไม่โดนบล็อก บล็อกเฉพาะ **ติดตั้ง/รัน**
- **นับ "ตำแหน่งกำลังทำงาน" บนมือถือถูกต้อง** — `renderPulse` เดิมนับจาก `roles` อย่างเดียว พอ `roles` ว่างจะขึ้น "0 ตำแหน่งกำลังทำงาน" ทั้งที่ chip Lead หมุนอยู่ตรงนั้น ตอนนี้นับ Lead ที่ working ด้วย และ Lead ที่ idle ยังได้ card ("Lead ว่าง")

### Changed (ปรับ)
- **remote-control mirror เฉพาะ Lead** (`remote/config.py: LEAD_ONLY_STREAM = True`) — เดิม teammate ทุกตัวไปโผล่บนมือถือ 2 ทาง: เป็นแถวใน `/api/activity` และเป็น `done` SSE ต่อ 1 งาน. fan-out ปกติ (frontend + backend + qa + reviewer บางทีมี shard) = notification ระเบิดเรื่องงานที่ user มอบหมายไปแล้วเพื่อจะได้ไม่ต้องนั่งดู. Lead สรุปงานทีมให้อยู่แล้ว traffic ของ teammate จึงเป็นของซ้ำล้วนๆ บนจอ 6 นิ้ว
  - `api.activity()` ยัง emit key `roles` แต่**ว่างเสมอ** (ลบ key ทิ้งจะพัง PWA build เก่าที่อ่าน `p.roles.length`)
  - `notify._on_done` drop done ของ teammate, ของ Lead ยังผ่าน
  - เปลี่ยน `LEAD_ONLY_STREAM = False` = ทีมทั้งหมดกลับมาเหมือนเดิม (มีเทสกันไว้ทั้ง 2 ทิศ)
- **role file 16 ไฟล์เพิ่มหมวด "Browser & เครื่องมือหนัก (บังคับ)"** — 13 role ได้ข้อห้าม + ชี้ทางส่งต่อให้ qa, ส่วน qa/critic/designer ได้ข้อความอนุญาต + เตือนอย่าลง browser ซ้ำ (cache บนเครื่อง dev บวมถึง 2.88 GB / chromium 4 builds). **#103:** Claude Code hook มีเฉพาะ claude — pane ที่รัน codex / gemini-agy / opencode / kimi / cursor บังคับด้วย prose ตรงนี้เท่านั้น

### Added (ใหม่)
- **เทสใหม่** — `test_pane_guard.py` (87 เคส รวม false-positive ทั้งชุด), `test_cli_guard.py` (18 เคส wiring + fail-open), `test_agent_role_files_have_browser_guard.py` (กัน prose ↔ `BROWSER_ROLES` drift), `test_hook_wiring.py::TestGuardInjection`
- **`docs/architecture/godfile-map.md`** เพิ่ม hidden edge 3 เส้น — guard เชื่อมด้วย **string ในไฟล์ settings + PATH** ไม่มี import edge เลย, tool policy 2 ชั้นคนละกลไก, และ `BROWSER_ROLES` ↔ role-file prose

## [1.0.27] - 2026-07-22

### Added (ใหม่)
- **เตือนทันทีถ้าติดตั้งแบบไม่ใส่ `-g`** — postinstall ตรวจว่าเป็น local install แล้วบอกตรงๆ ให้ลงใหม่ด้วย `npm install -g agent-takkub`. เดิมการลงแบบ local จะ "สำเร็จ" เงียบๆ แต่คำสั่ง `takkub` ไม่เคยขึ้น PATH เลย (bin shim กับ PATH provisioning ผูกกับ npm global bin dir) — ผู้ใช้เจอแค่ "command not found" โดยไม่รู้สาเหตุ.

### Changed (ปรับ)
- **`description` ของแพ็กเกจบอกวิธีติดตั้งแบบ global แล้ว** — หน้า npm และผลค้นหาแสดง `description` จาก package.json ซึ่งเดิมไม่ได้บอกว่าต้องใส่ `-g` คนที่เห็นจาก npm โดยไม่เปิด README จึงพลาดได้ง่าย (README บอกไว้ครบอยู่แล้ว).

### Fixed (แก้)
- **role file ที่ขาดของ kimi / cursor** — ทั้งคู่ถูกเพิ่มเป็น forced role ใน 1.0.26 แต่ไม่มี `.claude/agents/<role>.md` เลย เมื่อ CLI ยังไม่ได้ติดตั้ง (สถานะปกติของเครื่องส่วนใหญ่) pane จะ degrade เป็น claude substitute แล้วหาไฟล์ role ไม่เจอ → ไม่ได้รับ SPECIALIST OVERRIDE → เสี่ยงอ่าน CLAUDE.md ของโปรเจคแล้วทำตัวเป็น Lead แทนที่จะเป็น teammate ที่ทำงานเองแล้วรายงาน. เพิ่ม stand-in role file ครบทั้ง kimi/cursor และ track `opencode.md` ที่ค้างอยู่.

## [1.0.26] - 2026-07-21

### Added (ใหม่)
- **เลือก model ได้ต่อ provider และต่อ role** — Settings → Providers & Roles มี dropdown เลือก model ให้ทุก provider และทุก role (รายการ preset เปลี่ยนตาม CLI ที่ role นั้นเลือก, พิมพ์ id เองได้เพราะแต่ละ CLI ออก model ใหม่คนละจังหวะ). เก็บที่ `~/.takkub/provider-models.json` + `~/.takkub/role-models.json`. ลำดับความสำคัญตอน spawn: **model ของ role > model ของ provider > default ของ CLI** (ฝั่ง claude: `TAKKUB_TEAMMATE_MODEL` env ยังชนะทุกอย่างเหมือนเดิม และค่าว่างยังแปลว่า "ไม่ส่ง `--model`"). CLI: `takkub provider model <name> [<model>|--clear]`.
- **provider ใหม่ 2 ตัว** — **Kimi CLI** (MoonshotAI, `uv tool install --python 3.13 kimi-cli`, autonomy `--yolo`, Windows ต้องมี Git Bash / ตั้ง `KIMI_CLI_GIT_BASH_PATH`) และ **Cursor CLI** (`cursor-agent`, autonomy `--force`, ติดตั้งเองเท่านั้นเพราะ installer เป็น remote script — cockpit ไม่รันสคริปต์จากเน็ตให้). ทั้งคู่อ่าน `AGENTS.md` ได้จริง cockpit จึง plant teammate cheatsheet ให้ (ไม่งั้น pane ไม่รู้ว่าต้องเรียก `takkub done`). **ready/busy marker ยังไม่ calibrate** — spawn ได้แต่ยังไม่ควรใช้เป็น role หลักจนกว่าจะเก็บ marker จาก TUI จริง.
- **ติดตั้ง provider CLI จาก cockpit** — `takkub provider list` ดูสถานะ, `takkub provider install <name>` ติดตั้งรายตัว (lead-only), verify ว่า binary ขึ้น PATH จริงก่อนบอกว่าสำเร็จ.

### Changed (ปรับ)
- **doctor ไม่ติดตั้ง provider ให้อัตโนมัติแล้ว** — `takkub doctor --fix` จะ **ข้าม** การติดตั้ง provider พร้อมพิมพ์ `[skipped (opt-in)]` ต้องสั่ง `--install-providers` (หรือ `takkub provider install <name>`) เอง — กันการลง CLI หลายตัวโดยไม่ตั้งใจบนทุกเครื่องที่รัน `--fix`.
- **ถอด provider toggle chips ออกจาก status bar** — เปิด/ปิด provider ทำที่ Settings ที่เดียว (chips ซ้ำซ้อนกับหน้า Providers & Roles อยู่แล้ว).
- **ready-wait ตอนส่ง task แรกอ่านจาก ProviderSpec แล้ว** — เดิม hardcode ให้เฉพาะ codex/gemini ได้ 90 วิ ทำให้ opencode/kimi/cursor ตกไปใช้ค่า claude 45 วิ แล้วโดน blind paste ตอน cold-boot; ตอนนี้แต่ละ provider ใช้ `ready_wait_ms` ของตัวเอง.

### Fixed (แก้)
- **usage/limit meter รับมือ endpoint ที่ถูก harden แล้ว** — `oauth/usage` เริ่มตอบ 403/429 จริงจัง ทำให้ meter เดิมยิงซ้ำจนโดนบล็อกและโชว์ 0% ทั้งที่แค่อ่านค่าไม่ได้. ตอนนี้ทุก cockpit/instance ใช้ shared state ร่วมกันที่ `<config_dir>/takkub-usage-state.json` (cache + backoff ที่ persist ข้าม process), poll ห่างขึ้น 120→600 วิ และค่าที่อ่านไม่ได้แสดงเป็น `—` ไม่ใช่ 0%. **ข้อจำกัดที่รู้อยู่:** shared state ยังไม่มี inter-process lock — สอง instance ที่เริ่มพร้อมกันเป๊ะอาจยิง fetch ซ้อนกันหนึ่งรอบ (เท่าพฤติกรรมเดิมก่อนมี cache จึงไม่ใช่ regression) ไว้ปิดใน release ถัดไป.
- **auto-reminder ยิงรัวใส่ pane ตอน codex/agy กำลัง boot** — ระหว่าง cold-boot MCP servers, codex จะ **queue** task ที่เพิ่งส่งไว้ก่อน แต่ status bar อ่านว่า idle (`Fast off`) ทำให้ idle-watchdog เข้าใจผิดว่า "ทำเสร็จแล้วลืม `takkub done`" แล้วยิง `[auto-reminder]` พอกอยู่ในช่อง composer ทุก 90 วิ (งานไม่เคยพัง — พอ boot เสร็จ codex กลืน queue แล้วรันจนจบ แต่รกและกิน context). ตอนนี้ watchdog จะเริ่มนับก็ต่อเมื่อ pane **เคยเข้า turn ทำงานจริง** อย่างน้อยหนึ่งครั้งหลังรับ task และหยุดนับระหว่างเห็น marker ของ boot/queue — งานที่ทำเสร็จจริงแล้วลืมรายงานยังโดนเตือนเหมือนเดิม.

## [1.0.25] - 2026-07-13

### Added (ใหม่)
- **Instance banner แยก dev/prod (v1.0.25)** — `takkub list`/`status` ขึ้นหัวบอกว่ากำลังคุม cockpit ตัวไหน (`dev · <repo>` / `v<ver>` + port + path) และเตือนเมื่อมีอีก instance (dev↔prod) รันพร้อมกัน — เลิกงมว่าคำสั่งเข้า cockpit ตัวไหน.
- **`TAKKUB_NPM_REGISTRY` override (v1.0.25)** — ตั้ง npm registry สำหรับ public package (Claude CLI / takkub) ได้เอง เผื่อองค์กรที่ mirror แพ็กเกจไว้ใน private registry ของตัวเอง.
- **Task Ledger + Task Dock ครบวงจร** — ทุก assign มี markdown record, สถานะ flip ตอน done/failed/reassign และ cockpit แสดงงานข้าม project แบบ responsive.
- **Role/Skill lifecycle และ multi-provider architecture** — custom roles, skill catalog/matrix, shipped skill bundle, ProviderSpec registry, per-provider skill injection และ MCP bridge สำหรับ Codex/AGY.
- **Headless server mode** — แยก pane model ออกจาก Qt view, เพิ่ม headless entrypoint, Docker/Compose และ Ubuntu CI โดยยังคง desktop cockpit เดิม.
- **Remote/PWA controls** — close project, Lead pulse, quick replies, AskUserQuestion option chips และ session resume picker.
- **Settings Management รุ่นใหม่แบบ opt-in** — CRUD สำหรับ Roles/Skills/MCP/Plugins/Providers พร้อม aggregate transactions, secret-safe MCP editing และ checked role-variant regeneration; เปิดทดลองด้วย `TAKKUB_SETTINGS_UI=new`.

### Changed (ปรับ)
- **Settings ค่าเริ่มต้นกลับเป็น legacy** — correctness ของ UI ใหม่ผ่าน gate แล้ว แต่ feedback ผู้ใช้จริงพบว่า workflow ใช้ยากกว่าเดิม จึงเก็บรุ่นใหม่หลัง feature flag จนกว่า issue #115 จะผ่าน usability acceptance.
- **งานยาวและ done note ใช้ file handoff** — ลด paste/Enter race, เก็บ artifacts ใต้ runtime และแนบ screenshot evidence อัตโนมัติสำหรับ role ตรวจสอบ.
- **Parallel guidance ไม่บังคับ numeric cap** — capacity เป็น telemetry/warning เท่านั้น พร้อมแนะนำแบ่งงานเป็น waves ตามภาระจริง.
- **README ยกเครื่องใหม่ (v1.0.25)** — hero/badges โปรขึ้น, callout `-g` เตือนต้องลง global, เพิ่มจุดขาย **3 model brains** (Claude/Codex/Gemini + substitution) และ execution mode **1:1 ↔ Multi**; installer เลิก set npm registry global (เหลือ report อย่างเดียว).

### Fixed (แก้)
- **Full-system code review sweep + reliability hardening (v1.0.24)** — รีวิวโค้ดทั้งระบบแบบ multi-agent + adversarial verify แก้ครบ 92 findings: กัน **PyQt6 exit-127** (unhandled exception ใน Qt/QTimer slot ที่ทำ cockpit ตายเงียบ) ทั่ว config/remote-server/spawn/pane-tools/project-wizard, teardown **PTY resource leak** บน exit→respawn, sharded `done --fail` → เข้า fix-loop, route timer/watchdog notices ผ่าน `_notify_lead` (draft-guard กัน draft-clobber), canonical MEMORY path encoder, Windows `npm/npx` resolve, macOS Keychain login guard, doctor per-check isolation, secrets เขียน 0600 + ขยาย detection, `design_review_html` sanitize กัน HTML/JS injection, cli role-gate (`provision`/`migrate-skills`), provider `#N` shard normalize + auto-resume telemetry แบบ provider-gated, และ data-loss guards + atomic writes ใน issues/vault/config/skill-policy.
- **Multi-spawn submit reliability (v1.0.24)** — เปิด codex ≥3 pane พร้อมกันแล้ว task ถูกทิ้ง (submit CR โดนกลืนตอน MCP boot ช้าจน budget หมด); แยก **boot-retry budget (~90s) ออกจาก swallow budget** ให้ pane ที่ boot ช้าได้ CR ครบ พร้อม deterministic regression test.
- **Lead delivery/draft reliability** — แก้ draft-hold, split escape sequence, done-notice churn, duplicate bridge firing, swallowed paste/Enter และ stale project state หลายชุดที่พบจาก live repro.
- **Cross-platform Windows/macOS/Linux** — แยก PTY backend, path/process handling และ doctor checks ให้ headless/desktop ใช้ contract เดียวกัน.
- **Settings data integrity** — ป้องกัน masked secrets เขียนทับ credential จริง, rollback partial writes ของ role/skill/MCP, provider broadcast และ dirty-navigation data loss.
- **CI hermetic + version sync** — Plugins repository test ไม่พึ่ง marketplace registry ของเครื่อง dev อีกต่อไป และเพิ่ม gate ให้ `pyproject.toml`, `package.json`, `agent_takkub.__version__` ตรงกันเสมอ.
- **Hotfix Qt dependency resolution (v1.0.23)** — v1.0.22 ระบุช่วง `<6.12` กว้างเกินไปจน npm production install ดึง Qt 6.11 ซึ่ง doctor บล็อกเพราะ pane-teardown crash regression; pin ทั้ง PyQt6/WebEngine และ binary wheels ให้อยู่สาย 6.8 LTS (`>=6.8,<6.9`) พร้อมตรวจจาก registry install จริง.
- **Plugin half-clone self-repair (v1.0.25)** — plugin ที่ clone ค้างครึ่งทาง (registry บอก installed แต่ cache ไม่มีไฟล์ปลั๊กอินจริง) ทำให้ `claude plugin install` ตอบ "already installed" วนไม่จบ **restart ก็ไม่หาย** (เจอจริงกับ Claude Mem บน prod); ตอนนี้ตรวจเจอแล้วซ่อมเอง 1 รอบ (uninstall → purge cache แบบ read-only-safe → reinstall) แทนข้อความ "try restart".
- **npm private-registry รองรับ (v1.0.25)** — เครื่องที่ตั้ง npm registry เป็น private (Nexus/Artifactory) เดิม update Claude CLI/takkub ไม่ได้ (E404 เพราะ public package ไม่อยู่ใน registry ภายใน); เปลี่ยนเป็น pass `--registry` แบบ scoped เฉพาะคำสั่งที่ดึง public package (installer + Claude-CLI updater) โดย **ไม่แตะ global npm config** ของผู้ใช้ (default public · override ผ่าน `TAKKUB_NPM_REGISTRY`), และ `takkub doctor` เพิ่ม check เตือนแบบ read-only เมื่อ registry เป็น private.

## [1.0.17] - 2026-07-06

### Security (ความปลอดภัย)
- **ชุด security hardening ครบวงจร** (repo infra — ไม่กระทบ package ที่ผู้ใช้โหลด):
  - **SECURITY.md** — นโยบายแจ้งช่องโหว่ + threat model (ระบุชัด: loopback IPC socket
    + รัน shell = by design ไม่ใช่ช่องโหว่ · in-scope = secret leak/RCE/token bypass)
  - **Dependabot** — สแกน+อัปเดต dependency อัตโนมัติทุกจันทร์ (pip · npm · vscode-ext · github-actions)
  - **CodeQL** — code scanning Python + JS/TS (query `security-extended`) ทุก push/PR + weekly
  - **gitleaks** — secret scan ทั้ง git history (CI hard-fail) + pre-commit hook บล็อกก่อน commit
  - **pip-audit** — เช็ก CVE ใน Python deps (informational)
- **GitHub repo settings** (เปิดผ่าน gh api): vulnerability alerts + automated security fixes +
  private vulnerability reporting + **secret scanning & push protection** + **branch protection บน main** (solo-friendly)
- sync `__version__` ใน `__init__.py` (0.7.0 → ตรงกับ package version)

### Housekeeping
- เคลียร์ stale branches → เหลือ `main` อย่างเดียว (ลบ vscode-ide-migration + branch ที่ merge/superseded/obsolete แล้ว)

## [1.0.16] - 2026-07-05

### Added (ใหม่)
- **🌙 Auto-resume ข้าม usage limit** — pane ที่ชน quota ระหว่างมี task ค้าง จะถูก park
  แล้ว**ปลุกทำต่ออัตโนมัติตอน window reset จริง** (เวลาอ่านจาก usage API ไม่ใช่เดา):
  detection 2 ชั้น (banner บนจอ + usage API ยืนยัน) กัน false positive · cap ≤3 รอบ/task
  + ชน limit ซ้ำใน 10 นาทีหลังปลุก = หยุดถาวรคืนการตัดสินใจให้ Lead · ปลุกเฉพาะงานที่สั่ง
  ค้างไว้ ไม่รับงานใหม่เอง · **default OFF** — เปิดด้วย chip 🌙 ใน status bar (persist +
  broadcast [system] เหมือน exec-mode) · ตอน OFF ระบบ inert สนิท พฤติกรรมเดิม 100%
- **Skills เสริมจาก mattpocock/skills (คัด 4 จาก 38)** — `/grill-with-docs` + `/grilling`
  (interview เค้นแผนพร้อมสร้าง ADR/glossary), `/domain-modeling` (gemini/reviewer),
  `/codebase-design` (reviewer/codex) — ติดที่ user skills, role files ชี้การใช้แล้ว
- **ซูม font ใน pane ด้วยเมาส์** — Ctrl+scroll (Mac: Cmd+scroll) บน pane ไหน font pane นั้น
  ใหญ่/เล็กทันที (8–24pt) · Ctrl/Cmd+0 reset · ขนาดล่าสุด persist เป็น default ของ pane
  ใหม่ข้าม restart (ต่อ role) · scroll เปล่าเลื่อนปกติ + กัน Chromium page-zoom ซ้อน +
  แจ้ง PTY resize ให้ TUI ข้างใน reflow ถูก

### Changed (ปรับ)
- **Title bar สะอาดขึ้น** — แก้ identity ซ้ำ 2 รอบ (`agent-takkub [prod v..] — ... -
  agent-takkub [prod v..]`) · ตัดคำว่า `prod` เหลือ `agent-takkub v1.0.15 — dev team cockpit`
  (ฝั่ง dev ยังมี `[dev · <repo>]` ไว้แยก instance) · ถอด version chip + Changelog dialog
  ออกจาก status bar ล่าง (version มีบน title แล้ว · update chip npm/git ยังอยู่ครบ)

### Fixed (แก้)
- **first-boot ของ prod ค้าง 8+ นาที (หน้าต่างไม่ขึ้น นึกว่าแอปดับ) + โปรไฟล์ torn** — bootstrap
  clone ของ 1.0.13 `copytree` ทั้ง `~/.claude` (สนามจริง 2.9GB — ประวัติแชต 2.2GB) บน main
  thread ก่อน UI ขึ้น เปิดซ้ำก็ auto-kill ไม่ลง (ติด I/O) เจอ dialog "already running" และถ้าโดน
  kill กลางทางจะเหลือโปรไฟล์ครึ่งๆ ที่ `dest.exists()` เช็คผ่านตลอดไป. แก้ 3 ชั้น: **allowlist**
  (clone เฉพาะ CLAUDE.md/settings/keybindings/agents/commands/skills/plugins — ขยะ
  cache/security/snapshots ไม่มีทางหลุดมา) + **ประวัติแชตเอาเฉพาะ 10 session ล่าสุดต่อโปรเจค**
  (ข้ามไฟล์เดี่ยว >50MB) + **atomic** (build ใน `.partial` + marker + `os.replace` — kill
  กลางทางกี่รอบก็ไม่มี torn profile, โปรไฟล์ที่ login แล้วไม่มีวันถูกแตะ). boot แรกจบใน <10 วินาที
- **prod install ไม่มี playbook/role files เลย (Lead ไม่รู้จัก `takkub assign`)** — wheel ship แค่ `.py`
  ส่วน `REPO_ROOT` ของ installed build ชี้ `venv/Lib` ที่ว่างเปล่า → `render_lead_context()` คืน
  `None` เงียบๆ (Lead spawn มาแบบไม่มีคู่มือ), `AGENTS_DIR` ว่าง (role files teammate หายทุก role),
  `REPO_ROOT/bin` ไม่มีจริง (pane ตกไปใช้ takkub CLI ตัว dev จาก user PATH = code-version skew).
  แก้: `ASSETS_ROOT` (installed → `agent_takkub/_assets` ship ใน wheel ผ่าน setup.py build_py —
  root files ยังเป็น single source, build **fail ทันที**ถ้า assets หาย) + `CLI_BIN_DIR` prepend
  venv Scripts เข้า pane PATH + lead-context หายต้อง log event ไม่เงียบอีก
- **REPO_ROOT sweep 9 จุด (audit โดย gemini + adversarial check โดย codex — ดู `docs/audit/`)** —
  `boot.log`/`rtk_button.log`/`startup_pull.log` เขียนลง `venv/Lib` → ย้ายไป `RUNTIME_DIR` ·
  `takkub issue` fallback DB อยู่ใน `venv/Lib` โดนลบทุกครั้งที่ update → ย้ายไป DATA_HOME ·
  pane cwd fallback เลิก spawn ลง `venv/Lib` · `skill_audit`→`AGENTS_DIR` · doctor แนะ
  `npm update -g` ฝั่ง installed · `install.ps1` เลิก hardcode path · `takkub release` fail สวยๆ

### Added (ใหม่)
- **เปิด dev + prod คู่กันได้จริง** — single-instance lock เปลี่ยนจาก global ไฟล์เดียว (ที่ทำให้
  instance เปิดทีหลัง **auto-kill process tree ของตัวแรกทั้งยวง** — ต้นเหตุ "prod หายเอง" ที่ไล่กัน
  มาหลายรอบ) → lock **ต่อ DATA_HOME** · เปิดซ้ำ home เดิมยังกันเหมือนเดิม 100%
- **Instance identity** — window title/taskbar แยกชัด: `agent-takkub [prod v1.0.13]` vs
  `[dev · <repo>]` + breadcrumb `instance_boot` ลง events.log ทุก boot (DATA_HOME/ASSETS_ROOT/
  CLI_BIN_DIR/port/lock ครบ — debug "ตัวไหนเป็นตัวไหน" จาก log ได้เลย)
- **Prod Claude profile แยกจาก dev** — installed default `CLAUDE_CONFIG_DIR` =
  `~/.agent-takkub/claude-config` + first-boot **โคลนจาก `~/.claude` อัตโนมัติ** (ทุกอย่างรวม
  ประวัติแชต ยกเว้น `.credentials.json` — login ใหม่ครั้งเดียว, doctor เตือนถ้ายังไม่ login) ·
  `chatlog_scanner`/`takkub search`/resume-brief ตาม profile ของ instance · dev = `~/.claude` เดิมเป๊ะ
- **Installed-mode CI gate** — job ใหม่ build wheel → ติดตั้ง venv จริง → เทสจาก installed layout
  บน Windows+macOS ทุก commit (ปิดช่องโหว่ "dev test เขียวแต่ prod พัง" ที่ทำให้บั๊กชุดนี้รอดมา
  ถึง 1.0.12) + doctor หมวด `[installed]` + `docs/release-checklist.md`
- **prod cockpit spawn teammate ไม่ได้ (connection refused ทั้งที่ cockpit เปิดอยู่)** — pane ของ
  cockpit ที่ติดตั้งผ่าน npm/pip (single-instance, DATA_HOME=`~/.agent-takkub`) resolve `takkub`
  บน PATH ไปเจอ CLI ของ **dev checkout** (repo/bin มาก่อนใน user PATH) → CLI อ่าน port file
  ของ DATA_HOME **ตัวเอง** (`repo/runtime/port` — port เก่าที่ตายแล้ว) แทนของ cockpit ที่ spawn
  มัน → `takkub assign/send/done` โดน `WinError 10061` ทุกครั้ง Lead เลย spawn role อื่นไม่ได้เลย
  (ถ้า dev cockpit เปิดคู่กัน อาการยิ่งแย่กว่า: คำสั่งวิ่งเข้า **ผิด instance**). Root cause:
  `TAKKUB_PORT_FILE` ถูก set เฉพาะ multi-instance mode (`app.py`) — single-instance ไม่มี env นี้
  ให้ forward. แก้: `_apply_port_file()` ใน `pane_env.py` stamp `config._get_port_file()` เข้า env
  ของ**ทุก pane ทุก role รวม Lead เสมอ** (ทั้ง single/multi-instance) → CLI copy ไหนบน PATH ก็
  dial cockpit ที่ spawn ตัวเองถูกตัว. + 8 tests (`test_pane_port_file.py`) + integration verify
  3 scenario (single/multi/CLI-side read).
  npm/pip (ไม่ใช่ git checkout) เข้า branch `not_repo` ของ `_refresh_update_button` ที่
  **hardcode เขียวตลอด** — ไม่เคย query npm registry ว่ามี version ใหม่ไหม เลยเขียวแม้มีอัพเดต
  จริง (ฝั่ง git checkout ยังเปลี่ยน เขียว→น้ำเงิน ตามปกติ). แก้: poll npm registry เบื้องหลังทุก
  5 นาที (ผ่าน `_NpmUpdateThread("check")` แบบ modal-free — sibling ของ click path) → cache
  latest เทียบ current → chip เปลี่ยนเป็น **น้ำเงิน "📦 Update available (vX)"** เมื่อมีของใหม่
  (สีเดียวกับ git behind-state) และเขียว "🔄 Update via npm" เมื่อ up-to-date / ยังไม่ได้เช็ค /
  เช็คไม่ผ่าน (ไม่ false-alarm). + 8 tests.
- **self-update ค้างที่ `git fetch/pull` → restart storm + false "cockpit ไม่ได้เปิด"** —
  pane เจอ `connection refused (10061)` ทั้งที่ cockpit เปิดอยู่ — สืบจาก stack trace ใน
  `boot.log` เจอ **2 บั๊กซ้อน**: **(1)** `update_helper._git` เรียก git โดยไม่ปิด interactive
  credential prompt → บน Windows `git fetch/pull` ผ่าน HTTPS spawn `git-credential-manager`
  ที่ **สืบทอด (inherit) pipe stdout/stderr** ไปด้วย พอ `timeout=` เตะ มันฆ่าแค่ process `git`
  แต่ไม่ฆ่า credential helper ที่แยกตัวไป → pipe ไม่มีวันถึง EOF → `communicate()` join
  reader-thread **ค้างนิรันดร์** = timeout เป็นหมัน → updater thread + main thread ค้าง →
  watchdog wedge → launch ใหม่ไล่ auto-kill ตัวเก่ารัวๆ (restart storm) → pane ที่ spawn ตอน
  cockpit ครึ่งตายเลยต่อ cli_server ไม่ติด. `try_silent_self_update` (pre-UI, ก่อน
  single-instance lock) ก็ค้างท่าเดียวกัน → เหลือซาก process 1-thread/5MB ค้างเครื่อง.
  **(2)** `update_worker` `emit()` `finished` signal หลัง `_WorkerSignals` ถูก Qt ลบทิ้งตอน
  restart → `RuntimeError: wrapped C/C++ object ... has been deleted` (unhandled) รก boot.log.
  **แก้:** เพิ่ม `git_env()` (`GIT_TERMINAL_PROMPT=0` + `GCM_INTERACTIVE=never`) + ใส่
  `-c credential.helper=` ทุก git call ผ่าน `_git` → ไม่มี credential helper ถูก spawn เลย =
  ไม่มี grandchild มาถือ pipe ค้าง → timeout ทำงานจริง; route git call ตรงๆ ใน
  `try_silent_self_update` (rev-parse/pull) ให้ผ่าน `_git` ด้วย (เดิมเป็น `subprocess.run`
  เปล่า ไม่ได้ hardening) → ตัดต้นตอ husk; ครอบ `emit()` ด้วย `_safe_emit` กลืนเฉพาะ
  `RuntimeError` ของ receiver ที่ถูกลบ. + 9 tests (git_env / `-c credential.helper=` / `_safe_emit`).
- **pane ต่อ cli_server ผิด instance — `TAKKUB_PORT_FILE` ตกจาก env allowlist** —
  `_build_pane_env()`/`_build_lead_env()` กรอง env ของ pane ด้วย allowlist แต่ **ไม่มี
  `TAKKUB_PORT_FILE`** ในลิสต์ → ตอน multi-instance mode ที่ `app.py` ตั้ง per-PID port file
  ไว้ (`agent-takkub-port.<pid>`) มันโดนตัดทิ้งตอน spawn → pane ไม่เคยได้ port file ของ
  cockpit ตัวเอง เลย fall back ไปอ่าน `runtime/port` ที่เป็นซากของ instance เก่าที่ตายแล้ว →
  `takkub list/assign` เจอ `connection refused` ทั้งที่ cockpit เปิดอยู่ (comment ที่ `app.py`
  ว่า "panes inherit this env" เป็นเท็จมาตลอด — allowlist ตัดทิ้ง). แก้: เพิ่ม
  `TAKKUB_PORT_FILE` เข้า `_PANE_ENV_ALLOWLIST` (ไม่ใช่ secret — เป็น path temp) → pane ต่อ
  cli_server ถูก instance ทั้ง single mode (fall back `runtime/port` ที่ server เดียวเป็นเจ้าของ)
  และ multi mode (per-PID file). + 2 regression tests.
- **delivery self-heal กู้ swallowed paste ได้ — pane ไม่ค้าง empty อีก (#79, follow-up #26)** —
  `⚠️ [delivery-unconfirmed]` ยิงซ้ำ ~16 ครั้ง/2 สัปดาห์ ทุก role/provider (qa/backend/
  frontend/devops/codex/gemini) เพราะ task paste โดน swallow ตอน race กับ TUI render →
  pane ค้าง empty ไม่เคย report done (อาการเดิม #26). root cause: self-heal
  (`_delayed_enter_verified`) กู้ได้แค่ **Enter ที่หาย** (resend CR) — ครอบ #22 (input box มี
  `[Pasted text]` placeholder ค้าง) แต่ **กู้ paste ที่หายไม่ได้** เพราะ input ว่าง resend CR
  ลงไปก็ไม่มีอะไร submit. แก้: ทำ self-heal ให้ **paste-aware** — ตอน verify ถ้า pane ยังอยู่
  ที่ ready prompt ให้เช็คว่า input box มี content จริงไหม (`PtySession.shows_pending_input`:
  หา `[Pasted text` placeholder หรือ fragment ของ task ใน bottom region, scoped กัน body
  poison เหมือน `is_at_ready_prompt`): มี content → resend CR (#22), **input ว่าง → re-paste
  payload แล้วค่อย submit (#26)**. ครอบทุก paste+submit path (task deliver, lead-notify pump,
  force-deliver, peer send) + log event `*_repaste`. backward-compatible (ไม่ส่ง payload =
  พฤติกรรมเดิม). + 6 tests.

## [v0.9.0] - 2026-06-22

### Added (เพิ่ม)
- **`takkub doctor` รายงาน version-behind** (`check_version`) — เดิม "ตามหลัง main กี่ commit"
  อยู่แค่ใน GUI update chip → user ที่ใช้ CLI ล้วน (เพื่อนที่เพิ่ง install) ไม่เห็น. เพิ่ม check
  ใน doctor: โชว์ version (`git describe`) + behind count เทียบ origin/main + hint วิธีอัพ
  (`git pull --ff-only` + `pip install -e .` ถ้า deps เปลี่ยน) + เตือน local edits. เป็น check
  เดียวที่แตะ network (best-effort `git fetch` timeout สั้น, offline → ใช้ ref ล่าสุด + บอกว่า
  offline). ไม่ใช่ git repo → INFO บอกแปลงเป็น checkout ก่อน. + tests.

### Changed (เปลี่ยน)
- **self-update sync deps อัตโนมัติ — เครื่องอื่นอัพแล้ว "เท่า main" จริง (ไม่ใช่แค่ code)** —
  self-update chip ที่มีอยู่ (poll `origin/main` ทุก 5 นาที → tray balloon + ปุ่มกะพริบเตือน
  เมื่อ behind → คลิก pull + auto-restart; ZIP install แปลงเป็น git checkout ได้) เดิม**แค่เตือน**
  ให้ user รัน `pip install -e .` เองเมื่อ pull แล้ว `pyproject.toml` เปลี่ยน → ถ้าข้าม =
  boot ทับ deps เก่า (ของไม่ครบ เครื่องอื่นไม่เท่า main จริง). แก้: เมื่อ pull เปลี่ยน deps →
  `_restart_with_pip_sync()` spawn detached script (`build_pip_sync_script`, pattern เดียวกับ
  Claude-CLI updater): รอ cockpit ตาย → `pip install -e .` ใน venv → relaunch (relaunch แม้ pip
  fail = ไม่ brick, fallback เป็น restart ปกติถ้า spawn fail). + unit tests. → one-click update
  ลง **code + deps ครบ** อัตโนมัติ ไม่ต้องทำ manual step.
- **Vault knowledge refactor (3-tier): แยก log ออกจาก knowledge** — vault เดิม 2,232
  notes แต่ลิงก์แค่ 53 target (86% ชี้ project hub, 0.86 link/note) เพราะทุก `takkub done`
  ฝัง `[[01-Projects/<p>]]` backlink ปลอมตัวเดียว → graph เป็น hub-and-spoke ไร้ประโยชน์
  (log archive ปลอมตัวเป็น second brain). แก้เป็น 3-tier: 🟢 **knowledge**
  (`02-Areas/` MOC + `01-Projects/<p>.md`, ลิงก์จริง, อยู่ใน graph), 🟡 **log**
  (`99-Logs/`, ซ่อนจาก graph, prune), 🔴 **session** (14 วัน เก็บ last 5/project).
  เปลี่ยน: session log → `99-Logs/sessions/`, brief → `99-Logs/briefs/`, **เลิกฝัง
  backlink ปลอมบน log** (`_render_decision_note`), auto-prune (session 14d / brief 30d),
  strengthen junk filter + dedup, **distill layer** (`distill_session_facts()` —
  session จบ → สกัด durable fact → append `## Decisions & Learnings` + MOC scaffolding,
  best-effort), Obsidian graph filter ซ่อน log/orphans. + migration script
  `scripts/migrate_vault_logs.py` (move <14d → 99-Logs, delete >14d) + 64 tests.
  design: `docs/design/vault-knowledge-refactor.md` · guide: `docs/guides/2026-06-22-vault-second-brain.md`.
- **Verify flow ใหม่: DEV เสร็จทุกอย่าง → devops ยก stack ขึ้น (port-safe) → QA ท้ายสุด** —
  เดิม impl done → fire qa+reviewer คู่ขนานทันที ตอนนี้ QA เป็น "ปุ่มจบ" รันท้ายสุด
  ต่อเมื่อ DEV งานหลักเสร็จหมด **และ** (ถ้าโปรเจคมี docker compose) devops ยก stack
  ขึ้น local บน **port ที่ไม่ชนกับ docker ที่รันอยู่** ก่อน (devops เช็ค `docker ps`
  เลือก port ว่าง / offset + unique project name, `up -d --wait`, report URLs ให้ QA).
  reviewer ย้ายเป็น gate ตอน PR (qa-only mid-cycle). กระทบ: auto-chain handoff prompt,
  CLAUDE.md playbook, devops role file, built-in "feature" pipeline template
  (hop: impl → devops → qa). โปรเจคที่ไม่มี compose ข้าม devops ตรงไป QA.
- **role `gemini` เปลี่ยนเครื่องยนต์จาก Gemini CLI → Antigravity CLI (`agy`)** —
  Google ปิดบริการ Gemini CLI standalone เมื่อ 2026-06-18 แทนที่ด้วย Antigravity
  CLI (binary ชื่อ `agy`, ติดตั้งเป็น native installer จาก antigravity.google ลง
  `%LOCALAPPDATA%\agy\bin`, auth = Google Sign-In / `ANTIGRAVITY_API_KEY`).
  **role/provider ยังชื่อ `gemini` เหมือนเดิม** — เปลี่ยนแค่ binary ที่อยู่เบื้องหลัง:
  one-shot `takkub gemini` ใช้ `agy -p` (เดิม `gemini -p`), pane spawn ใช้
  `agy --dangerously-skip-permissions` (เดิม `gemini -y`), helper เปลี่ยนเป็น
  `find_agy_executable()` (`which("agy")`). substitution (Claude รับตำแหน่งแทนเมื่อ
  ไม่มี binary), routing, สี, grid, toggle ทั้งหมดทำงานเหมือนเดิม.

### Removed (ลบ)
- **`gemini_md.py` (auto-plant GEMINI.md) ถูกลบ** — `agy` auto-discover `AGENTS.md`/
  `.agents/` ไม่ใช่ `GEMINI.md` แล้ว → gemini(agy) pane ใช้ `codex_agents_md.ensure_agents_md`
  ร่วมกับ codex (AGENTS.md เดียว, marker เดียว, idempotent ไม่ชน race เมื่อ codex +
  gemini แชร์ cwd เดียวกัน). cheatsheet กลางเปลี่ยน title เป็น "agent-takkub Teammate"
  (เลิกผูกกับ codex) + เพิ่มกฎ "ใช้ path รูปตรงๆ ห้าม recursive grep หา .png".

### Fixed (แก้)
- **structural stale-marker detection — silent break ของ ready-detection ให้ดังขึ้น (#20)** —
  marker ทั้งหมดเป็น natural-language text ของ upstream CLI → reword เมื่อไหร่ detection
  พังเงียบ (idle watchdog stall). full structural rewrite (exit code/ANSI) ทำไม่ได้ (CLI เป็น
  interactive TUI long-lived ไม่มี exit code ตอนรัน, raw mode เสมอ) → ใช้ layered mitigation
  แทน: เพิ่ม **output-quiescence primitive** (`PtySession.seconds_since_output()` — structural
  signal ไม่พึ่ง text: CLI ที่ generate จะ stream ตลอด) + **stale-marker detector**
  (`Orchestrator._check_stale_markers`): pane alive + quiet เกิน `STALE_MARKER_QUIET_S` (20s) +
  ไม่ match marker ใดเลย → log `ready_marker_possibly_stale` พร้อม footer text จริง (rate-limit
  10 นาที/pane) → operator เห็น prompt ที่ reword แล้ว rescue ด้วย `TAKKUB_EXTRA_READY_MARKERS`
  ได้ (silent stall → loud diagnostic). + version-dependence registry (เอกสาร marker ไหน
  เปราะ blast radius เท่าไหร่) ใน pty_session. + tests. ปิด #20 (mitigation ครบ 4 ชั้น:
  footer-scope + env-override + doctor selftest + field detector).
- **ready-detection scope ทั้งจอ → conversation body poison ได้ (#20 ราก, ต่อจาก #70)** —
  `is_at_ready_prompt()` / `is_at_update_splash()` match marker (`bypass permissions`,
  `esc to interrupt`, `update available!` ฯลฯ) ทั่ว **ทั้งจอ** → text ใน conversation body
  ที่บังเอิญมี marker string (เช่น Lead ที่กำลังคุยเรื่อง marker เอง) ทำ verdict ผิด —
  เป็น root cause ของ #70 false-busy stall. แก้: scope detection เฉพาะ **bottom region**
  (`_ready_region`, bottom 6 non-blank rows = footer/status/input chrome) เหมือนที่
  `_TTY_PROMPT_RE` anchor bottom rows อยู่แล้ว → body text ที่ scroll เหนือ region ไม่ poison.
  `is_at_trust_prompt` คง full-screen (modal ต้องการ 2 marker พร้อมกัน, poison แทบเป็นไปไม่ได้).
  + regression tests (body-quote ไม่ busy, real footer ยัง detect, short screen ไม่เปลี่ยน).
  หมายเหตุ: #20 ส่วน "structural signals (exit code/ANSI) แทน text" ยังเหลือ — fix นี้แก้ facet
  conversation-poison ที่กระทบจริง, ลด fragility มาก แต่ยังเป็น text-marker อยู่.
- **done-notice spill ไม่ถูก reap เมื่อมี >1 project active → Lead chain ค้าง (#70)** —
  teammate ทำเสร็จ + ส่ง `takkub done` จริง แต่ notice spill ลง durable แล้ว reaper
  (`_reap_pending_done_notices`) ไม่ flush กลับ Lead → autonomous/auto-chain run ค้างเงียบ
  (เจอตอน 2 project รันขนาน: tak-game flush ได้ แต่ agent-takkub starve ~10 นาที).
  **Root cause (พิสูจน์โดย elimination + repro):** reaper logic ถูกต้องทุก project แต่ gate
  ด้วย `is_at_ready_prompt()` ซึ่งเป็น false-negative ได้ (blocker marker ในจอ conversation
  ของ Lead เองอ่านเป็น busy — marker fragility #20) → Lead alive แต่ never-ready →
  reaper skip ถาวร ไม่มี escalation. **แก้:** staleness escalation — track
  `_pending_done_since` ต่อ project, ถ้า Lead alive-but-not-ready นานเกิน
  `_DONE_NOTICE_STALE_S` (60s) → `_force_deliver_done_notices()` paste รวมเป็นข้อความเดียว
  (1 paste + verified submit, ไม่ clobber) bypass ready gate + log `done_notice_force_flush`.
  guarantee delivery ไม่ค้างถาวรแม้ ready-detection พัง. + repro/regression tests.
- **teammate pane ค้างที่ codex `update available!` splash (#62)** — codex CLI splash
  modal ถือเป็น soft-block (`is_at_ready_prompt()` = False ถาวร) → orchestrator ไม่
  deliver task + idle watchdog ไม่เตือน → pane ค้างไม่จำกัดเวลา Lead รอ `takkub done`
  ที่ไม่มีวันมา. แก้: เพิ่ม `is_at_update_splash()` (detect splash, กัน false-trigger บน
  gemini passive footer), `_check_stuck_panes` ส่ง Enter (`b"\r"`) dismiss + cooldown 30s
  (`SPLASH_DISMISS_COOLDOWN_S`) → fall through ไป close→respawn ถ้ายังค้าง. Lead exempt.
- **log noise: `spawn_still_blocked` ยิงทุก 50ms tick (#64)** — spawn-gate retry log
  ทุก tick → 213 entries ที่เป็น normal flow ไม่ใช่ error. แก้: `_spawn_blocked_first_ts`
  dedup — log ครั้งเดียวตอนเริ่ม block + เตือนอีกเมื่อ block นานเกิน
  `SPAWN_BLOCK_WARN_AFTER_S` (5s), ลบ episode key เมื่อ gate เคลียร์.
- **error spam: `idle_watchdog_pane_error` (#64)** — `_check_idle_teammates` มี
  `except Exception` ที่กลืน error โดยไม่ log type/message + วนทุก 5s tick → events.log
  เดียวมี 3279 entries ที่วินิจฉัยอะไรไม่ได้ (3210 = pms Lead pane ตัวเดียว). แก้:
  capture `err=type+message` + rate-limit (log ครั้งเดียวต่อ error/pane, cooldown 5 นาที)
  → ครั้งหน้าเห็นสาเหตุจริง 1 บรรทัด แทน spam เปล่า.
- **docs-verify gate ครอบ point-in-time artifact ใน subdir ไม่ทั่ว** — `docs/code-review/*`
  ไม่ครอบ `docs/code-review/<subdir>/*.md` (PurePath `*` ไม่ข้าม `/`, Python 3.11 ไม่มี
  recursive `**`) → snapshot เก่าที่อ้างไฟล์ที่ลบแล้ว block commit. แก้: pattern `dir/*`
  ครอบ nested path ด้วย (prefix match).
- **error sources จาก runtime (#64)** — audit events.log: เพิ่มเอกสาร `main_thread_stall`
  (989×, UI freeze 1-2.6s, ไม่ใช่ตอน spawn), big-file cache-bloat + "Error writing file"
  retry-loop (แก้ด้วย BIG_FILE_GUARD/STALE_FILE_GUARD), delivery_unconfirmed (แก้ด้วย
  agy ready-wait 90s) — รายละเอียด + action items ใน issue #64.

## [v0.8.0] - 2026-06-16

### Added (เพิ่ม)
- **`takkub goal "<objective>"`** (#50) — Lead ตั้งเป้าหมาย session ก่อน fan-out
  parallel; orchestrator prepend goal block เข้าทุก `assign` task หลังจากนั้น
  อัตโนมัติ → ทุก role เห็น big picture เดียวกัน กัน scope drift เก็บแบบ volatile
  ราย project (ไม่ persist, แต่ละ tab ไม่ leak กัน), prepend แบบ idempotent (กัน
  double บน auto-respawn replay), ride ไปกับ task replay ด้วย `takkub goal` โชว์
  goal ปัจจุบัน, `--clear` ล้าง lead-only (gate ทั้ง CLI + server).

### Fixed (แก้)
- **#51 gemini ไม่ส่ง report กลับ Lead หลัง CLI update** — gemini 0.46.0 ที่มีรุ่น
  ใหม่กว่า upstream โชว์ footer `"Gemini CLI update available! …"` ค้างถาวร (passive,
  prompt ใช้ได้ปกติ) แต่ `is_at_ready_prompt()` ดัน block บน substring
  `"update available!"` (ตั้งใจไว้สำหรับ codex splash modal) → gemini ถูกอ่านว่า
  "ทำงานอยู่ตลอด" → idle watchdog ไม่เคยถึง threshold nudge `takkub done` → report
  ไม่ถึง Lead แก้โดยเช็ค gemini ready marker (`type your message or`) **ก่อน**
  generic update blocker (codex splash ยัง block เหมือนเดิม) + regression test.
- **context % ไม่ขึ้นบน tab ที่ใช้ user profile อื่น** — `token_meter` hardcode
  `~/.claude/projects` ตายตัว แต่ pane ที่รันใต้ profile อื่น (`CLAUDE_CONFIG_DIR`
  ต่าง) เก็บ session JSONL ที่ `<config_dir>/projects/` → `find_latest_session`
  หาไม่เจอ → badge ไม่โผล่ แก้: `PtySession` จำ `CLAUDE_CONFIG_DIR` จาก spawn env,
  `find_latest_session(config_dir=...)` scope ตาม config home ของ pane นั้น
  (None = default `~/.claude` เหมือนเดิม) + regression test.

### Added (เพิ่ม · instrumentation)
- **main-thread stall logging** — dead-man watchdog เก็บ event `main_thread_stall`
  ลง `events.log` ทุก freeze > 0.75s (peak duration + `spawn_in_progress`) +
  heartbeat ถี่ขึ้น 1s→250ms, soft stack-dump 3s→1.5s เพื่อจับ UI freeze สั้นๆ
  ตอนพิมพ์ (ไว้ยืนยันว่า freeze เกิดตอน pane spawn จริงไหม). ปรับ threshold ผ่าน
  env `TAKKUB_STALL_LOG_S` / `TAKKUB_WATCHDOG_SOFT_STALL_S` / `TAKKUB_WATCHDOG_POLL_S`.

### Security (ความปลอดภัย · vNEXT-hardening)
- **one-click exec hardening** (M3#13) — คลิก path ที่ pane print แล้วเปิดผ่าน OS
  default app เดิมไม่มี guard เลย ปิด 3 ช่อง: (1) **exec extension** (`.bat/.cmd/.ps1/
  .exe/.hta/.lnk/.vbs/.msi/…`) เดิมคลิก = **รันทันที** → เปลี่ยนเป็น reveal-in-folder,
  (2) **path confinement** — absolute path ที่ไหนก็ได้ (หรือ `../` traversal) เปิดได้ →
  จำกัดให้อยู่ใต้ cwd/repo เท่านั้น, (3) **drop `file://`** จาก clickable URL (bypass
  guard ทั้งหมด). logic เป็น pure helper + 13 test.
- **OSC 52 clipboard-set strip** (M3#14) — PTY output ไหลเข้า `term.write` ตรงๆ; pane
  ส่ง `ESC]52;c;<base64>BEL` เขียน system clipboard เงียบๆ ได้ → filter ที่ render
  boundary (split-across-batch carry) เป็น defense-in-depth.
- **gate `status` transcript + screenshot** (M3#16) — `takkub status` เดิมคืน
  transcript tail + screenshot path ของ **ทุก pane ให้ caller ใดก็ได้** (teammate/manual
  อ่าน secret ใน transcript คนอื่นได้) → redact 2 field นี้เว้นแต่ caller ถือ Lead token
  (state/stall ยังเห็นได้ปกติ; status bar ใน UI อ่าน method ตรง ไม่กระทบ).
- **bracketed-paste breakout** (M6#28) — content ที่ inject เข้า pane ถ้าฝัง `ESC]201~`
  จะจบ paste mode ก่อน แล้ว byte ที่เหลือ (รวม `\r`) รันเป็น keystroke จริง = auto-submit
  คำสั่งที่ถูกแทรก → strip paste marker ออกก่อน wrap เสมอ.
- **vault decision-note scrub** (sec-w1) — strip control byte + neutralize frontmatter
  dash นำ + cap length ก่อนเขียน Obsidian.

### Performance / Token diet (vNEXT-hardening)
- **ไม่ freeze GUI ตอน `--requires-commit` done** (M2) — `git status --porcelain` เดิม
  รัน sync timeout 10s บน Qt main thread (จอค้างทั้งตัว) → ย้ายไป **QProcess** (event-loop
  driven = non-blocking + single-thread ไม่มี worker race); done notice ออกทันที, warning
  uncommitted ตามมาเป็น follow-up.
- **ลด token ต่อ spawn** — skip inject project CLAUDE.md ซ้ำเมื่อ claude auto-discover จาก
  cwd อยู่แล้ว (~750 tok/Lead spawn, tok-4); ไม่ dump role-memory skeleton ว่าง → ชี้ทาง
  บรรทัดเดียว (~100-150 tok/teammate, tok-5); cap session goal ที่ set-time (กัน 64KiB
  re-paste, tok-3).
- **bounded transcript tail-read** (M4#22) — `takkub status` เดิมอ่าน transcript ทั้งไฟล์
  (MB) เข้า memory แค่เอา 5 บรรทัดท้าย → seek อ่านแค่ 64KiB ท้าย.

### Fixed (แก้ robustness · vNEXT-hardening)
- **`takkub harvest` dead-on-arrival** (M0#1) — payload ไม่มี `from` stamp → server role-gate
  ปัดทิ้ง; เพิ่ม stamp ทั้ง 2 จุด.
- **C0/C1 control-byte scrub** (M0#2) — sanitizer strip 8-bit C1 + DEL เพิ่มจาก C0.
- **central ready-prompt marker table** (M4#17) — marker detection เดิม hardcode อังกฤษ
  กระจาย; upstream reword = provider stall (เกิด 3 ครั้ง) → รวมเป็น `_READY_RULES` ตัวเดียว
  (first-match-wins, faithful) + env override `TAKKUB_EXTRA_READY_MARKERS` (กู้ reword โดยไม่
  แก้ code) + `takkub doctor` self-test จับ marker เสีย.
- **pipeline-run pre-check** (bug-1) — เดิมตอบ `ok=true` เสมอทิ้ง error message →
  validate template+hops ก่อน schedule.
- **auto-chain handoff release** (bug-1) — blocker ตัวสุดท้ายตายโดยไม่ส่ง done →
  chain deadlock; release ที่ crash-cap + stuck-give-up ครบ 4 จุด.
- **CC flush durability** (M4#22) — เดิม pop+persist-empty ก่อน write → write fail กลางทาง
  = message ที่เหลือหาย → deliver-then-dequeue.
- **brick-guard updater** (M4#21) — รอ cockpit PID exit จริงแทน `sleep 3s` (race) + capture
  install exit code → sentinel `.failed`.
- **Windows key ถูกกลืนตอน cockpit focus** — Chromium (QtWebEngine) MediaKeysListener ลง
  low-level keyboard hook บน Windows → `--disable-features=HardwareMediaKeyHandling,
  GlobalMediaControls` (cockpit ไม่ใช้ media key).

### Changed (internal refactor · vNEXT-hardening)
- **แตก spawn() 3 branch** — extract `_launch_session` (shell/gemini/codex) drift เป็น param
  ชัด (M5#23) + `_mint_pane_token` (M5#24) + named pane-geometry const (M5#25).
- **🐴 ponytail minimal-code rules** — ดูด ruleset "lazy senior dev" (MIT) เข้า role file
  `frontend/backend/mobile/devops` + `reviewer` (over-engineering lens) ไม่ลง Node-hook (กัน brick).

## [v0.7.0] - 2026-06-06

### Added (เพิ่ม)
- **Per-project pipeline + role→CLI settings** — pipeline templates และ per-role
  CLI mapping เก็บแยกราย project (`~/.takkub/projects/<slug>/`) แต่ละ tab ไม่ชนกัน;
  provider on/off (`disabled-providers.json`) ยัง global (เป็น machine capability).
  `load/save/provider_for/effective_provider_for` รับ `project=None` (None = global +
  fallback ที่ project ใหม่ inherit จนกว่าจะ save เอง). แก้กับดัก "แก้ built-in
  pipeline → save → reverted" (built-in identity ล็อค แต่ override hops ได้;
  save() ข้าม built-in ที่ไม่ถูกแตะให้ track code ต่อ).
- **Inline learned-notes content เข้า spawn prompt** — เดิม inject แค่ *pointer*
  ให้ pane Read() เอง (มักข้ามตอนงานด่วน → ค้นความรู้เดิมซ้ำทุก spawn). ตอนนี้ฝัง
  content ตรงๆ ใน `<learned-notes>` block (cap 200 บรรทัดท้ายสุด + truncation notice)
  ให้ pane เห็นความรู้ของ project ตั้งแต่ token 0. concat ไม่ f-string (กัน literal
  braces เช่น Go templates `{{.x}}`), read มี try/except OSError.
- **Per-role × project learned memory + QA จำ login** (`role_memory.py`) — แต่ละ
  teammate role สะสมความรู้ของ project ข้ามรอบงานใน
  `runtime/role-memory/<project>/<role>.md` (conventions/gotchas/decisions; qa: test
  login/flow) อ่านตอน spawn + append เมื่อเจอของไม่ obvious.

### Changed (เปลี่ยน)
- **ซ่อนปุ่ม ▶ Run pipeline** จาก status bar (handlers เก็บไว้ตาม pattern ปุ่มที่
  ซ่อน restore ได้ง่าย); pipeline backend + CLI ใช้งานปกติ.
- **role-memory curation** — เก็บ learned notes ไม่ให้บวมเกิน: dedup bullet
  (เก็บอันใหม่สุด) + size-cap (16 KB / 120 entries, ตัดเก่าสุด) ตอนอ่าน โดยคง
  header + seeded skeleton, best-effort ไม่ raise, atomic write (#43).

### Fixed (แก้)
- **#44 parallel spawn ชน ConPTY** — ยิง `takkub assign` หลาย role พร้อมกัน /
  shard fan-out / pipeline hop spawn บน tick เดียว → ConPTY COM call ตัวหลังชน
  input-sync dispatch ของตัวก่อน (`RPC_E_CANTCALLOUT`) → `spawn_failed_warned`.
  เพิ่ม **non-blocking stagger** (QTimer slot-reservation ใน cli_server + `_defer`
  seam ใน pipeline hop; env `TAKKUB_SPAWN_STAGGER_MS` default 400ms) ครอบทุก spawn
  path. assign แรก delay 0 (ของเดิมไม่เปลี่ยน). **ไม่แตะ ConPTY/main-thread** กัน
  freeze RCA (ไม่มี `time.sleep`).
- **#38 codex npm self-update ชน EBUSY** (mitigated) — codex 2 ตัว spawn พร้อมกัน
  รัน `npm i -g @openai/codex` ทับกัน. stagger codex ด้วย gap ใหญ่กว่า (env
  `TAKKUB_CODEX_SPAWN_STAGGER_MS` default 10s) + detect ผ่าน `effective_provider_for`
  (ครอบ role ที่ remap→codex, ไม่ stagger ผิดให้ codex ที่ degrade เป็น claude).
  codex ไม่มี update off-switch จึงเป็น mitigation ไม่ใช่ full prevention.
- **#41 stuck-recovery loop ไม่มี max-attempts cap** — pane ที่ค้างแต่ยังไม่ตาย
  (wedged-alive) ถูก close→respawn วนไม่จบ → pipeline ค้างถาวร. เพิ่ม
  `STUCK_RECOVER_MAX=3` + counter ที่ survive close-pop → ครบแล้วเลิก recover +
  fail/advance pipeline hop + เตือน Lead (one-shot).
- **#42 prune `runtime/browser-profiles/`** — per-shard Chrome profile สะสม
  ไม่จำกัด → age-prune (>14 วัน, env-tunable) ตอน startup เก็บ login profile
  ที่เพิ่งใช้.
- **#40 stray 'M' ในทุก pane shell** — pin `.bat`/`.cmd` เป็น CRLF verbatim
  (`-text` ใน `.gitattributes`) แก้ cmd.exe parse bug ที่ทำให้มีตัว `M`/REM
  fragment โผล่ทุก pane.
- **Cockpit freeze hardening (RCA 2026-06-04)** — `boot.log` rotation (>256 KB) +
  soft-stall watchdog dump main-thread stack ก่อน kill; แยก RCA Issue A (CLI
  pile-up, fixed) vs Issue B (ConPTY GIL freeze, open) ใน
  `docs/cockpit-freeze-rca-2026-06-04.md`.
- **Audit Wave 1** — shard lifecycle (stale-timer guard, spawn-fail bookkeeping),
  watchdog durability, doctor UI.

### Notes
- Issue B (single-spawn ~12s GIL-hold GUI freeze) ยัง **open** — ConPTY spawn ยัง
  sync บน Qt main thread (remedies off-thread + WinPTY backend ถูก revert ก่อนหน้า
  เพราะ GIL-starve / live-typing lag). v0.7.0 stagger เฉพาะ **parallel** spawn
  collision (spawn_failed) เท่านั้น ไม่ได้แก้ single-spawn freeze.

## [v0.6.0] - 2026-06-03

### Added (เพิ่ม)
- **QA shard fan-out** — `takkub assign --role qa --shards N` spawn QA หลาย pane
  (qa#1..qa#N) แชร์ base role `qa` รัน UI smoke คู่ขนาน. แต่ละ shard แยก Chrome
  port + user-data-dir ของตัวเอง (ไม่ชนกัน), ผลรวมเป็น Lead handoff ก้อนเดียว
  พร้อม timeout 45 นาที. cross-check โดย gemini (design) + codex (21 side-effects)
  ก่อน ship.
- **Pipeline Settings dialog** — ปุ่ม **⚙ Pipelines** ใน status bar เปิดหน้า
  ตั้งค่า dev pipeline ผ่าน UI ไม่ต้องแก้ code: (1) drag-drop hop builder (role
  ใน hop เดียว = parallel, ระหว่าง hop = sequential; ตั้ง cwd/requires-commit/
  auto-chain รายตัวใน Inspector), (2) custom templates (สร้าง/rename/duplicate/
  delete; built-in 3 ตัวล็อกแก้ไม่ได้ + ปุ่ม ↺ Restore defaults), (3) Provider &
  Role toggles เปิด/ปิด codex/gemini + per-role enable. เพิ่ม `pipeline_config.py`
  (store `~/.takkub/pipelines.json` + self-heal) + `pipeline_dialog.py` (QWebChannel
  bridge) + `static/pipeline_settings.html`.
- **Edit project config ผ่าน right-click tab** (#32) — เมนู "Edit project…" แก้
  description + path mapping แล้ว save+reload **ไม่ต้อง restart** (atomic write +
  refresh list, validate path มีจริงก่อน save, preserve presets เดิม).
- **GENERATE_GUIDE_HTML routing** (#30) — เอกสาร user-facing (setup guide / how-to /
  checklist / คู่มือ / วิธีตั้งค่า) route ไปผลิต md source + แปลงเป็น HTML ผ่าน
  `design_review_html` converter อัตโนมัติ. เช็คก่อน EXPLAIN_SYSTEM กัน precedence ชน
  + กัน false-positive (setup docker→devops, checklist component→frontend).
- **AI-generated project rules** — เพิ่ม project ใหม่ผ่านปุ่ม **＋ Add Project** เลือก
  "New project (AI rules)" → cockpit รัน Claude Code headless สร้าง `<project>/CLAUDE.md`
  ให้อัตโนมัติ (ใช้เวลา ~15–60 วินาที). preview + แก้ใน editor dialog ก่อน save
  หรือกด 🔄 Regenerate ถ้าไม่พอใจ. แก้ทีหลังได้ผ่านปุ่ม **✏ Rules**. Lead pane
  ทุก spawn โหลด rules เข้า context อัตโนมัติ (cap 3000 chars) ผ่าน `lead_context.py`.
  เพิ่ม `project_rules.py` (read/write helpers) + `_RulesGeneratorThread` (headless
  claude QThread worker) + `_generate_rules_with_ui()` / `_show_rules_editor_dialog()`
  ใน `main_window.py`.

### Changed (เปลี่ยน)
- **รวม role→CLI provider mapping เข้า Pipeline Settings** — ลบปุ่ม **🤖 Providers**
  + `provider_dialog.py` (dead code หลังย้าย); ตั้ง provider ต่อ role (claude/codex/
  gemini) ในแท็บ Providers & Roles ของ ⚙ Pipelines แทน. team/provider/role config
  รวมจบที่ปุ่มเดียว.
- **ยุบ ~14 per-pane state dict เป็น `PaneState` dataclass** — แก้ root cause ของ
  lifecycle bug class: teardown เคยต้อง pop ~14 dict แยกกัน (diverge ง่าย → state-loss/
  leak) เหลือ `_pane_state.pop(key)` ครั้งเดียว. ~60 call sites migrated. pure refactor
  (1430 tests pass, 2 independent reviews).
- **รวม `_show_rules_preview_dialog` กับ `_show_rules_editor_dialog` เหลือเมธอดเดียว** —
  ทั้งสองทำงานเหมือนกัน, ลบ `_show_rules_preview_dialog` (dead duplicate)
- **ลบ `MainWindow._rebalance_teammates` สองอัน** (dead code) — caller จริงใช้
  `tab.rebalance_teammates()` ใน `project_tab.py` โดยตรงอยู่แล้ว
- **เพิ่มปุ่ม `?` ใน status bar** + `QShortcut(F1)` ระดับ window สำหรับ help dialog
  (เดิม F1 ใช้ได้แค่ตอน main window focused — ตอนนี้ทำงานแม้ pane terminal focused)

### Fixed (แก้)
- **กัน main-thread freeze / zombie orchestrator / memory drop** (#33 #34 #35) — มาจาก
  freeze incident จริง (teammate pane พ่น output ต่อเนื่อง): (#35) coalesce bytesIn เป็น
  buffer ~16ms/จำกัดขนาด แทน render ทีละ chunk, (#34) single-instance QLockFile guard +
  dead-man watchdog (1s heartbeat), (#33) inject MEMORY.md pointer เข้า teammate spawn
  prompt.
- **gap-audit lifecycle/routing fixes** — stuck-recover snapshot/restore (uuid/task/
  auto-chain/commit-gate) + rollback on spawn fail; gate multi-role UI+API ด้วย impl-verb
  (review/test/refactor ไม่โดน shadow); แยก provider toggle-off vs not-installed
  (Claude-on-Claude); กัน save-empty/preset-loss.

## [v0.5.2] - 2026-06-01

### Added (เพิ่ม)
- **`takkub release` สร้าง GitHub Release ให้อัตโนมัติ** — เดิมทำแค่ commit + git
  tag, `git push --follow-tags` ดัน tag ขึ้นแต่หน้า Releases ว่าง (เหตุที่
  v0.4.0–v0.5.1 ไม่โผล่). ตอนนี้ step สุดท้าย: push + `gh release create` โดยใช้
  section ของ version นั้นใน CHANGELOG เป็น notes → changelog โชว์บนหน้า Releases
  ทันที. best-effort (gh ไม่มี / offline → เตือนแต่ไม่ fail release เพราะ commit+tag
  ในเครื่องสำเร็จแล้ว). ปิดด้วย `--no-github-release` (กลับไป commit+tag เฉยๆ ไม่ push).
  เพิ่ม `extract_release_notes()` + `create_github_release()` ใน `release.py`.

## [v0.5.1] - 2026-06-01

### Fixed (แก้)
- **issue ไม่รั่วไป repo ของโปรเจคอื่นแล้ว** — `new_issue` เปลี่ยน default เป็น
  `cockpit_bug=True` → `takkub issue new` ลง **agent-takkub repo เสมอ** ไม่ว่าจะ
  สั่งจาก pane ของโปรเจคไหน (cockpit tracker มีไว้สำหรับบั๊กของ cockpit/orchestrator/
  CLI/UI). เดิม bug-check prompt *ขอให้* ใส่ `--cockpit-bug` แต่พอ agent ลืม issue
  ก็หลุดไปลง repo ของโปรเจคที่ active อยู่ (เช่น pms-api) เพิ่ม `--no-cockpit-bug`
  เป็น opt-out ไว้ตั้งใจลง repo ของโปรเจค active. อัพเดต bug-check prompts; เพิ่ม
  `.takkub_issues.json` (local fallback) ใน gitignore.

### Added (ปุ่มอัพเดต Claude CLI)
- **ปุ่ม `⬆ Claude CLI` ใน status bar** — เช็คว่ามี Claude Code CLI
  (`@anthropic-ai/claude-code`, npm global) version ใหม่ไหม ถ้ามีจะ **วิเคราะห์
  ความเข้ากันได้ด้วย AI** ก่อนอัพเดต: ดึง CHANGELOG ของ upstream, ตัดเฉพาะส่วนที่
  ใหม่กว่าที่ติดตั้ง, แล้วให้ headless `claude -p` ประเมินเทียบกับ flags ที่ cockpit
  พึ่งพา (`--append-system-prompt-file`, `--resume`/`--session-id`, `--mcp-config`,
  `--plugin-dir`, `--fallback-model`, `--disallowed-tools`, รูปแบบ JSONL ของ
  token-meter ฯลฯ) → report ภาษาไทย (กระทบ / เอามาใช้ได้ / ปลอดภัย + คำแนะนำ).
  เพิ่ม `claude_update.py` + `ClaudeUpdateCheckWorker` (รัน version/network/analysis
  นอก Qt thread).
- **อัพเดตปลอดภัยบน Windows** — ตอน apply จะเขียน detached updater script, ปิด
  cockpit (Lead + claude pane ทุกตัวปล่อย binary), รัน
  `npm install -g @anthropic-ai/claude-code@latest` ตอนไม่มีอะไรจับไฟล์อยู่, แล้ว
  เปิด cockpit ใหม่. เลี่ยง file-lock ที่เคยทำ CLI พัง (เหตุผลที่ปิด autoupdate).
  popup ยืนยันบอกจำนวน claude pane ที่จะถูกปิด.
- **เจอว่าต้องแก้ → เปิด GitHub issue อัตโนมัติ** — ผลวิเคราะห์จบด้วย machine-readable
  verdict `<<<TAKKUB>>>` (`ACTION_REQUIRED`/`SEVERITY`/`ISSUE_TITLE`). ถ้า version
  ใหม่หมายความว่า agent-takkub ต้องแก้ระบบ → cockpit เปิด GitHub issue เข้า repo
  ตัวเองให้ (`new_issue(cockpit_bug=True)`, tag `claude-update`), dedup ตาม version
  range กันสแปมเวลากดเช็คซ้ำ. dialog โชว์เลข issue + URL → งานความเข้ากันได้ไม่หาย
  ตอนปิด dialog ผู้ใช้มาแก้ทีหลังตามจังหวะตัวเองได้.

## [v0.5.0] - 2026-06-01

### Added (provider substitution — Claude รับตำแหน่งแทน)
- **role codex/gemini ที่ใช้ไม่ได้ ตกมาเป็น Claude แทน** แทนที่จะปฏิเสธ. 2 กรณีที่
  provider ใช้ไม่ได้ — **ปิดผ่าน toggle** ใน status bar หรือ **ยังไม่ได้ติดตั้ง CLI**
  — รวมจัดการที่ spawn layer: `provider_config.effective_provider_for()` (runtime
  "ตอนนี้ CLI ไหนใช้ได้" ต่างจาก `provider_for()` ที่บอก "ตั้งค่าไว้เป็นอะไร") จะ
  degrade role codex/gemini ที่ใช้ไม่ได้ → `claude`. `orchestrator._spawn` gate
  branch codex/gemini ด้วยค่านี้ → provider ที่ใช้ไม่ได้ไหลลง branch claude **โดยคง
  ชื่อ role เดิม** → pane "gemini"/"codex" ยังอยู่ตำแหน่ง/slot เดิม แต่รันด้วย
  `claude.exe`.
- **stand-in role prompts** `.claude/agents/{gemini,codex}.md` — อ่านเฉพาะตอน
  substitute; บอก claude pane ว่ากำลังรับบทแทน (report ขึ้นต้น `[claude-substitute
  for <role>]`) และเตือนว่าเสีย model diversity.

### Changed (เปลี่ยน)
- **routing ไม่ปฏิเสธ codex/gemini ที่ถูกปิดอีกแล้ว** — `routing_planner.classify()`
  route ตามปกติ (ไม่มี `ASK_CLARIFY`, ไม่ strip cross_check) + ใส่ substitution note
  ใน `reason`; one-shot ที่ถูกปิด degrade เป็น `FIRE_ASSIGN` (pane ที่ backed ด้วย
  claude — one-shot ไม่มี substitute path). Lead spawn context (`lead_context.py`),
  toggle broadcast notice, และ `CLAUDE.md` เปลี่ยนเป็นบอก Lead ให้ propose/fire role
  แล้วหมายเหตุเรื่อง substitution แทนที่จะบอกผู้ใช้ให้ไปเปิด provider ก่อน.

## [v0.4.0] - 2026-05-31

### Added (terminal UX + review/release tooling)
- **Clickable URLs & file paths in panes** — click a link or path in any pane
  to open it: URLs go to the OS browser (`QDesktopServices`, since QtWebEngine
  blocks `window.open`), file paths open in the OS default app (resolved against
  the pane cwd, then repo root). `terminal_widget.py` + `static/terminal.html`
  (WebLinksAddon handler + a custom xterm link provider).
- **Self-contained HTML design reviews** — `design_review_html.py` renders a
  review `.md` → portable `.html` (screenshots from front-matter `shots:`
  inlined as base64, `*impact: …*` tags → colored badge cards via CSS `:has()`).
  `critic.md` runs the converter after writing the markdown and reports both paths.
- **`EXPLAIN_SYSTEM` routing intent** — "รีวิวระบบ / อธิบายระบบ / explain
  architecture / system overview" classifies as `ActionKind.EXPLAIN_SYSTEM` and
  produces an HTML system explainer for the project instead of a chat answer;
  normal work tasks stay markdown. `routing_planner.py`.
- **Changelog viewer** — clicking the status-bar version chip opens CHANGELOG.md
  rendered in an in-app dialog (`QTextBrowser.setMarkdown`); copy-version moved
  inside it. `main_window.py`.
- **`takkub release`** — one-shot version bump (major/minor/patch or `--version`)
  + CHANGELOG `[vNEXT]` roll + git commit & annotated tag; push left to the user.
  Guards (run before any write, so `--dry-run` is a real preflight): empty
  changelog, downgrade/same/malformed version, duplicate tag. `release.py`.

### Changed (status bar visual cleanup)
- **Neutralized the status bar** (design-review findings) — action buttons
  dropped their per-button rainbow fills for a quiet ghost style; only End
  Session (closes all panes = destructive) keeps a restrained red accent.
  Provider/plan chips became outline + status dot (codex/gemini stay clickable
  toggles). Token meter de-duplicated: the tab shows `%` only, the status-bar Σ
  shows only with 2+ panes, and the pane header stays the canonical per-pane
  meter. `main_window.py`.

### Changed (per-role model tiers)
- **Teammate model is now picked per role instead of one flat Sonnet-medium
  tier.** The cockpit owner runs on Claude Max (per-token cost irrelevant), so
  model choice trades latency for quality, not dollars — spend the bigger tier
  where a miss is expensive, stay snappy where it isn't:
  - **reviewer, critic** → Opus 4.8 high effort (gate roles: last line before
    ship, run infrequently at verify/pre-ship hops where the user already
    waits). Fallback degrades only to Sonnet.
  - **backend, devops** → Sonnet 4.6 **high** effort (API contracts, schema,
    migrations, irreversible deploy/infra — high frequency, so keep Sonnet for
    turn speed but raise effort to cut subtle-bug rework).
  - **frontend, mobile, qa, designer** → Sonnet 4.6 medium (unchanged default
    — high-frequency execution, low blast radius, latency matters).
  - `_ROLE_MODEL_TIERS` / `_teammate_tier()` in `orchestrator.py`. The global
    `TAKKUB_TEAMMATE_MODEL` / `_EFFORT` / `_FALLBACK` env vars still override
    every role at once when explicitly set.

### Added (graceful model fallback under load)
- **`--fallback-model` on every spawned claude pane.** When a pane's model is
  overloaded (HTTP 529) or not found, claude now switches to a fallback model
  for the rest of the session instead of hard-failing the turn (CC 2.1.152
  made the switch session-wide; 2.1.144 made it survive `/bg`+detach). In a
  multi-pane cockpit, 4-8 panes can hit the Max rate ceiling at the same
  instant — a falling-back pane keeps working rather than erroring mid-task
  and forcing a respawn. Defaults: teammates → `claude-haiku-4-5`,
  Lead → `claude-sonnet-4-6`. Override with `TAKKUB_TEAMMATE_FALLBACK` /
  `TAKKUB_LEAD_FALLBACK` (set to `""` to disable). `orchestrator.py` spawn argv.

### Added (user-level plugin + MCP inheritance)
- **User MCP allowlist-merge**: `ensure_user_mcps()` in `shared_dev_tools.py`
  reads `~/.claude.json` top-level `mcpServers` at cockpit boot and merges a
  curated allowlist into `runtime/shared-mcp.json`. Included by default:
  `obsidian-vault` and `postgres-pms` (stdio, no credentials). Skipped by
  default: `pms` (HTTP + bearer token — security regression risk); any entry
  with `headers.Authorization` or env vars matching TOKEN/KEY/SECRET. Set
  `TAKKUB_INCLUDE_PMS=1` to opt pms back in. Browser MCPs (playwright,
  chrome-devtools) always win on name collision. Authorization header values
  are never logged.
- **`ecc` plugin** added to `_SAFE_PLUGINS` — ECC tools available in panes;
  noisy hooks remain muted via `ECC_GATEGUARD=off` + `ECC_DISABLED_HOOKS`.
- **`claude-obsidian-marketplace` intentionally NOT added** — cached 1.4.3
  still ships a `SessionStart` prompt-hook that crashed all panes in v0.2.0.
  Gated on a manual spawn smoke-test before enabling.

## [0.3.8] — 2026-05-12

### Added (token usage meter)
- **Per-pane token badge** ("สรุปการใช้งาน token"): each pane header now shows
  `<prompt> / <limit> · <pct>%` derived from the active claude session's JSONL
  on disk (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`). Polls every 5s
  by reading the last assistant turn's `usage` block. Hover for full
  breakdown (input + cache create + cache read + output, model name, limit).
  Colour ramps: grey < 50% → yellow 50-80% → orange 80-95% → red ≥ 95%.
- **Aggregate status-bar meter**: shows `Σ <total> · max <pct>%` summing
  prompt tokens across every active pane. Tooltip lists per-role usage so
  the user can spot which pane is bumping the cap. Headline percentage is
  the **largest single pane's ratio**, not a sum — each pane has its own
  context window so the team-wide ratio is "closest pane to its cap".
- New `token_meter.py` module: `encode_path_for_claude`, `find_latest_session`,
  `read_last_usage`, `format_tokens`, `usage_color`, `context_limit_for_model`.
  Default limit 200k; override via `TAKKUB_CONTEXT_LIMIT` env var for the
  Opus 4.7 [1m] mode.

### Fixed
- **UI freeze during typing** ("อาการ ค้างของการพิมพ์"): Typing while Claude was busy printing large amounts of text caused the entire cockpit UI to freeze. This happened because `winpty.write()` is a blocking call. Fixed by moving `PtySession.write()` to a background `_WriterThread` with a non-blocking queue. Input keystrokes are now immediately queued and the UI remains responsive.
- **Typing delay and ghost characters** ("พิมแล้วดีเลย์/ตัวหนังสือโดนแทนที่"): Switched the PTY backend from WinPTY to ConPTY. WinPTY operates by scraping the hidden console screen buffer on an interval and generating ANSI diffs, which introduced a ~50-150ms roundtrip delay and caused characters to appear out of order or replace each other during rapid typing. ConPTY provides a direct, native ANSI rendering pipeline (same as VS Code and Windows Terminal), resulting in a "super real-time" typing experience.

## [0.3.7] — 2026-05-12

### Added (Lead hybrid policy)
- **Lead direct-edit hybrid policy.** Old guidance was a single soft bullet
  ("Lead ห้ามทำงานเอง") which Lead ignored under pressure — user saw Lead
  doing direct multi-file refactors in pms-web (i18n locales + workload
  page tsx + CSS) instead of delegating to `frontend`. New policy keeps
  flexibility for *meta* work (cockpit config, planning, task specs) but
  draws a hard line for *project* work.
- New decision matrix in `CLAUDE.md` (cockpit):
  - ✅ Lead may edit: cockpit files, plan-time Read/Grep/Glob, single-line
    typos at user-pinned paths, task-spec markdown.
  - 🚫 Lead must delegate: anything under a project path, >1 file,
    >30-line edits in a round, specialist-context work (CSS, API
    contracts, schemas, infra), explicit user assignment.
- **Auto-injected `BLOCKED_DIRS` at every Lead spawn**
  (`orchestrator._render_lead_context`): renders cockpit `CLAUDE.md`
  plus a dynamic section listing the active project's `paths` so Lead
  starts each session knowing the *exact* off-limits directories. Tracks
  `projects.json` so switching projects updates the policy automatically.
- Tools are *not* hard-locked (`--disallowed-tools` unused) — Lead keeps
  Edit/Write for cockpit-side work. The hybrid relies on a sharp,
  spawn-time injected rule rather than coarse tool removal.

### Fixed (stalled-frame bug)
- **Idle pane no longer holds a stale frame.** Symptom the user saw: a
  teammate finishes its turn, the *final* batch of PTY output reaches
  xterm.js, but the DOM paint never happens — the pane sits stuck on
  the second-to-last frame until you press a key or click into it.
  `term.write` had already run; the render simply wasn't painted.
- Root cause: Chromium aggressively pauses requestAnimationFrame and
  paint scheduling for any view that isn't the foreground tab. A
  multi-pane cockpit always has N−1 panes in that state.
- Fix is three-pronged so a single layer failing won't bring the bug
  back:
  1. **Chromium flags** (`app.py`, set before QtWebEngine boots):
     `--disable-background-timer-throttling`,
     `--disable-renderer-backgrounding`,
     `--disable-backgrounding-occluded-windows`,
     `--disable-features=CalculateNativeWinOcclusion`.
  2. **In-page RAF self-loop** (`terminal.html`): a one-line
     `requestAnimationFrame(pulse)` recursive scheduler keeps xterm.js's
     render service warm at the page's native refresh rate.
  3. **Python heartbeat** (`terminal_widget.py`): a 250 ms `QTimer`
     fires `runJavaScript("void 0;")` to force a JS task-queue tick if
     the RAF loop is ever paused for any reason. Cheap on capable
     hardware, harmless on weak.
- User intent for this fix: *"เครื่องฉันแรง อยากให้มันตื่นตัวอยู่ตลอดเวลา"*
  — render service is always on.

[0.3.7]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.7

## [0.3.6] — 2026-05-12

### Removed (final word on local echo)
- **All local echo logic** — for real this time. v0.3.0..v0.3.5 kept
  flip-flopping between "echo locally for snappiness" and "pass-through
  for correctness". Under fast input, claude's TUI renders arrive out
  of order (e.g. a delayed render of `"กพ"` replays *after* the user
  backspaces it away), so a smart-echo gate is not enough — the
  symptom we keep hitting is "I deleted everything, but `กพ` is stuck
  on screen until I press another key".
- xterm.js is now a pure pass-through, same as iTerm / Windows
  Terminal / wezterm. claude is the only writer to the screen. When
  claude is busy, the user perceives a roundtrip of latency per
  keystroke — that is the *correct* terminal behaviour for an
  unresponsive program. The display will never be stuck or desynced.

### Kept
- `window.termSetIdle()` remains as a no-op so the Python-side wiring
  (`AgentPane._sync_idle_flag`, `TerminalWidget.set_idle`) doesn't
  have to be ripped out in lock-step. Reintroducing optimistic
  rendering later just needs to replace the function body.

[0.3.6]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.6

## [0.3.5] — 2026-05-12

### Hardened
- **Idle-flag poll throttled to 150 ms** so the smart-local-echo gate
  doesn't fire 50+ times per second on chatty TUI output. Pyte's
  `is_at_ready_prompt()` scans every line of the screen on each call;
  combined with `outputUpdated` firing per byte chunk, the original
  v0.3.4 wiring was wasting real CPU.
- **Initial idle state forced to `False`** on every pane attach.
  Previously we left `_last_idle = None` and waited for the first state
  flip — meaning a race-condition early keystroke could see the JS
  default (which is whatever the previous pane left there) and local-
  echo into a not-yet-ready terminal.
- **`set_idle()` swallows JS bridge exceptions** so a single
  `runJavaScript` hiccup can't tear the whole `outputUpdated` signal
  chain down.
- **`_sync_idle_flag()` swallows pyte exceptions too** — pyte
  occasionally throws on malformed escape sequences, and we never
  want that to disable the idle gate.

[0.3.5]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.5

## [0.3.4] — 2026-05-12

### Added
- **Smart local echo** — re-introduces optimistic local rendering, but
  only when claude is sitting at the `❯` ready prompt (`is_at_ready_prompt`
  returns true). At that point ink.js re-renders synchronously on every
  keystroke, so local echo + claude's redraw match cell-for-cell and the
  user gets instant feedback again.
- When claude is busy ("Sautéed for 17s") the path collapses to pure
  pass-through, so the v0.3.2-era ghost-character desync can't happen.

### Wiring
- `TerminalWidget.set_idle(bool)` exposes the flag to the JS side via a
  new `window.termSetIdle()` JS function.
- `AgentPane._sync_idle_flag()` listens to `PtySession.outputUpdated`,
  reads `is_at_ready_prompt()` from the pyte screen, and pushes the
  flag whenever it flips. Only edge-triggered updates cross the bridge
  to keep IPC chatter low.

[0.3.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.4

## [0.3.3] — 2026-05-12

### Removed
- **All local echo / local backspace handling.** v0.3.0–0.3.2 tried to
  mask the round-trip latency of "type → JS → Python → PTY → claude →
  PTY → JS → render" by writing keystrokes to xterm.js immediately,
  but ink.js TUI input boxes batch their re-renders while claude is
  busy and our stale local state ended up fighting claude's delayed
  redraws. Symptom: typing a char then backspacing repeatedly left a
  ghost char on screen until the user pressed an unrelated key, which
  triggered claude to finally redraw and "consume" the buffered
  backspaces in one go.
- Now xterm.js is a pure pass-through: every keystroke goes straight
  to the PTY and claude is the only source of truth for the input
  area's display. Worst-case latency per keystroke matches every other
  terminal emulator (~roundtrip when claude is busy), but the display
  never desyncs.

[0.3.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.3

## [0.3.2] — 2026-05-12

### Fixed
- **Backspace ค้าง** — v0.3.0 local echo wrote each typed char to xterm.js
  instantly but never erased on backspace, so typing "[backend" then
  hitting backspace 8 times left the chars visibly stuck until claude
  caught up and redrew the input area. Local echo now writes `\b \b`
  (erase last cell) when the user presses Backspace/DEL, keeping the
  display in sync with the user's intent even when claude is mid-think.
- **Local-echo filter tightened** — previously `\r`, `\n`, `\t` were
  treated as printable and got written locally, which could nudge the
  cursor in ways that conflicted with claude's redraw. Now only
  0x20..0x7e + non-control multi-byte (Thai, CJK) get local echo;
  everything else passes through to claude untouched.

[0.3.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.2

## [0.3.1] — 2026-05-12

### Added
- **`agent-takkub.bat` at repo root** — single-file launcher that newcomers
  can double-click. Checks Python 3.11+ on PATH, checks `claude` CLI on
  PATH, creates `.venv` + installs deps on first run, copies
  `projects.json.example` to `projects.json` and opens it in Notepad if
  missing, then launches the cockpit detached.
- **Quick start** section in `README.md` — 3-step setup with the exact
  commands a fresh user needs (install Python + Claude CLI + clone +
  double-click the launcher).
- **Troubleshooting** table in `README.md` covering the seven most likely
  setup snags (missing Python / claude, sub-window dying, missing
  takkub shim, Thai diacritics, hook errors, wrong Lead cwd).

### Changed
- `scripts/run.bat` is now a thin one-line wrapper that delegates to
  the root `agent-takkub.bat`. Kept for backward compat with existing
  shortcuts / muscle memory.

### Fixed
- `agent-takkub.bat` initial drafts had unescaped `)` inside `echo`
  text blocks (e.g. `echo Log in: claude (one-time)`), which closed
  the surrounding `if` block early and caused unconditional `goto :fail`.
  Replaced with `--` separators.

[0.3.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.1

## [0.3.0] — 2026-05-12

### Changed (breaking architecture)

The terminal rendering layer is now **xterm.js running inside a
QWebEngineView**, the same emulator VS Code / Hyper / GitHub Codespaces
ship with. The Iter 1–9 QPlainTextEdit + pyte rebuild pipeline was a
"fake terminal" that hit hard walls on Thai/CJK shaping, alt-screen
scrollback, and TUI form alignment — every "สระหาย / กระตุก / ลบไม่หมด"
report v0.2.x couldn't fully solve.

xterm.js handles these natively: browser layout engine for complex
script shaping (Thai combining marks, BiDi, CJK width), built-in 10k
scrollback, proper mouse modes, and first-class selection/copy/paste.

### Added
- `src/agent_takkub/static/` bundle: `terminal.html`, `xterm.js` 5.5.0,
  `xterm.css`, `addon-fit`, `addon-web-links` — shipped in the package
  via `package_data` so the app works offline.
- `TerminalWidget` rewritten as `QWebEngineView` + `QWebChannel` bridge:
  - `bridge.sendInput(str)` → `inputBytes` signal → PTY
  - `bridge.resize(cols, rows)` → `resized` signal → `PtySession.resize()`
  - `bridge.ready()` → flush bytes queued during boot
- `PtySession.bytesIn(bytes)` signal emitting raw PTY chunks for xterm.js
  to consume directly (no pyte → rich rebuild).
- **Local echo** for printable input in xterm.js so each typed character
  appears the moment the key is pressed instead of waiting for claude's
  ink.js TUI to redraw on the *next* keystroke. Control sequences (Esc,
  arrows, Ctrl-keys, DEL) still go untouched to claude.
- Batched output writes: multiple `write_bytes()` calls within the same
  Qt event-loop tick coalesce into a single `runJavaScript` IPC hop
  (0 ms QTimer). Chatty TUI frames now cost one round trip instead of
  dozens.
- `PyQt6-WebEngine>=6.6` dependency (~150 MB Chromium bundle).

### Kept
- `pyte.Screen` still lives in `PtySession` purely for state-detection
  helpers (`is_at_trust_prompt`, `is_at_ready_prompt`, and `display_lines`
  for export). The double-parse cost buys us keeping every v0.2.x
  orchestrator behaviour — auto-trust, ready-detect, audit log, presets,
  session resume — unchanged.

### Migration
- `pip install -e .` (pulls PyQt6-WebEngine ~150 MB Chromium).
- Same `scripts\run.bat`, same `projects.json`, same `takkub` CLI.
- All v0.2.x behaviour preserved: Lead in project root, role-aware cwd,
  superpowers + agent-skills plugins, audit log, tray notifications,
  bash-friendly `takkub` shim.

### Known caveats
- Per-pane font size shortcut (Ctrl+= / Ctrl+-) wired but untested in the
  xterm.js context; xterm's own Ctrl+= / Ctrl+- works regardless.
- Export pane buffer still goes via pyte (`display_lines`) so it captures
  only the visible viewport. Future patch: switch to xterm.js's full
  buffer (`term.buffer.active`).
- The pyte-mode-detection mouse-wheel path from v0.2.2 is unused —
  xterm.js's built-in scroll handles wheel correctly.

[0.3.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.0

## [0.2.4] — 2026-05-12

### Fixed
- **Lead was working on agent-takkub itself, not on the user's project.**
  Lead spawned in `REPO_ROOT` (the cockpit source tree), so its Read/Grep/
  Bash tools all landed in cockpit files instead of the active project's
  code. Lead now spawns in the project root (common parent of all
  `paths`, or first listed path), and the cockpit's `CLAUDE.md` is passed
  via `--append-system-prompt-file` so Lead still knows the `takkub`
  cheatsheet without losing project context.
- `config.lead_cwd()` helper resolves the right directory:
  - `projects.json → projects.<name>.lead` explicit key, if set
  - else the common parent of all `paths` (e.g. `pms/` for `pms-web` + `pms-api`)
  - else the first listed path

### Changed
- Render debounce 20 ms → 0 ms (next-tick coalesce). Qt still batches
  many `outputUpdated` emits within a single event-loop tick into one
  redraw, so we don't thrash, but we also never artificially hold a
  frame back. IME echo and TUI form navigation feel live now.

[0.2.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.4

## [0.2.3] — 2026-05-12

### Fixed
- **`takkub: command not found` from Lead's bash** — Lead's Bash tool spawns
  `/usr/bin/bash` (MSYS) which does not auto-append `.cmd` to commands, so
  `bin/takkub.cmd` was invisible to it. Added a POSIX shell shim at
  `bin/takkub` (no extension) that delegates to the same `.venv` Python
  module. cmd.exe/PowerShell still use `bin/takkub.cmd`.
- **UI felt stale ("ไม่ขยับ")** — the v0.2.2 `_last_rendered_rich` diff
  cache was skipping legitimate redraws when row tuples looked identical
  to the previous frame, even though pyte had mutated cursor state /
  refreshed a status line / pulsed a blink. Removed the cache entirely;
  every frame now redraws.
- Bumped debounce 33ms → 20ms (~50 fps) so typing echo feels live again
  while staying cheap enough that idle frames don't thrash.

[0.2.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.3

## [0.2.2] — 2026-05-12

### Fixed
- **Thai diacritics rendering** — `QTextCharFormat.setFont(QFont(widget.font()))`
  was collapsing the families fallback chain in some Qt builds, so combining
  marks (◌ิ ◌ี ◌่ ◌้ ◌์ ฯลฯ) silently disappeared. Switched to
  `setFontFamilies(...)` + individual `setFontWeight/Italic/Underline` which
  preserves per-glyph fallback through Tahoma/Leelawadee UI.
- **Typing stutter** — added a `_last_rendered_rich` diff cache so identical
  screen states skip the full QTextDocument rebuild (~360 insertText calls).
  pyte fires `outputUpdated` for every byte chunk including no-op sequences
  (mouse-mode toggles, cursor save/restore), and the old path paid the rebuild
  on every keystroke.
- Bumped debounce 16ms→33ms (30fps) so typing storms collapse into fewer
  frames.
- Auto-scroll-to-bottom only fires when the user was already at the bottom
  before the refresh. Scrolling up to inspect history no longer gets yanked
  away by the next pyte update.

### Added
- **Smart mouse-wheel forwarding** — when claude has SGR mouse tracking on
  (mode 1006, the modern default), wheel events go out as proper
  `\x1b[<64;1;1M` / `\x1b[<65;1;1M` press events so claude scrolls its own
  buffer smoothly. Falls back to PgUp/PgDn when mouse tracking is off.
- `AgentPane._refresh_terminal` reads `screen.mode` and sets
  `TerminalWidget.mouse_tracking_on` accordingly on every frame.

[0.2.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.2

## [0.2.1] — 2026-05-12

### Fixed
- Default `--setting-sources` reverted to `project,local`. The v0.2.0 switch to
  `user,project,local` re-exposed claude-obsidian 1.4.3's `SessionStart` hook
  bug (`ToolUseContext is required for prompt hooks. This is a bug.`) inside
  every spawned pane.
- Cleared `presets: ["frontend"]` from the shipped `projects.json`. Auto-spawn
  was firing on every cockpit launch regardless of whether the user wanted a
  frontend pane. Lead now stays alone until you `takkub assign` or click "+ pane".

### Added
- `_default_plugin_dirs()` + explicit `--plugin-dir` args so spawned agents
  still inherit **superpowers** and **agent-skills** even though user-level
  settings are skipped. claude-obsidian is intentionally excluded until its
  hook is fixed upstream.
- `TAKKUB_EXTRA_PLUGINS` env var (semicolon-separated paths) to override the
  default plugin allowlist — set to empty string to suppress, or point at
  custom plugin directories.

[0.2.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.1

## [0.2.0] — 2026-05-12

### Changed
- `--setting-sources` default flipped from `project,local` to `user,project,local`
  so spawned agents inherit the user's installed Claude Code plugins (superpowers,
  agent-skills, claude-obsidian) and MCP servers. The original Iter 1 SessionStart
  hook bug that motivated the previous isolation appears resolved in claude-obsidian 1.4.3.

### Added
- `TAKKUB_SETTING_SOURCES` env var to override the default (e.g.
  `TAKKUB_SETTING_SOURCES=project,local` to fall back to the isolated v0.1 behaviour
  if a global plugin misbehaves).
- Orphan cleanup hook in `app.py`: atexit + SIGINT/SIGTERM/SIGBREAK handlers terminate
  every spawned claude/winpty-agent before the Qt process exits, so a crash or kill
  can't leave child processes pinned to the venv.
- Lead's `CLAUDE.md` now starts with a takkub quick-reference table + a "Tooling
  available to agents" section pointing at superpowers / agent-skills / MCP. Lead
  sees this on every session start, no more "what commands exist?".

[0.2.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.0

## [0.1.0] — 2026-05-12

First release. Replaces the tmux-based `agent-teams` setup with a native Windows desktop cockpit. Built in 9 iterations on the same day.

### Added — Iter 1 (baseline)
- PyQt6 main window with 3-column splitter (Lead · middle · right)
- `pywinpty` PTY backend, `pyte` ANSI screen model
- TCP-based `takkub` CLI (list / spawn / assign / send / close / done) for agent-to-orchestrator IPC
- Initial migration of 7 role definitions from `agent-teams` (replaced tmux-send-keys with `takkub` CLI calls)
- `scripts/run.bat` launcher that creates the .venv on first run

### Fixed — Iter 1.5 (post-launch debugging)
- Hidden `cmd.exe`/`conhost.exe` console window after spawn (`ConsoleWindowClass` SW_HIDE diff)
- Use `pythonw.exe` + `start ""` in `run.bat` so the launcher batch exits immediately
- pywinpty `read(size=...)` signature fix (`num_bytes` kwarg was wrong)
- pywinpty `write()` expects `str` not `bytes` — silent TypeError was eating every keystroke
- EOFError handling: check `isalive()` before treating an empty read as termination
- Thai diacritic regression after rich rendering — preserve `QFont` family fallback chain inside `QTextCharFormat`

### Added — Iter 2
- Auto-trust folder prompt (poll for "trust this folder" modal → send Enter)
- Auto-detect idle `❯` prompt before pasting `assign` task (replaces 12s fixed wait)
- Mouse wheel forwarded as PgUp/PgDn so claude's alt-screen scroll works
- Pane fully removed from layout on close (was leaving an empty placeholder)

### Added — Iter 3
- ANSI colour rendering via `QTextCharFormat` cache + custom 16-colour palette (bold/italic/underline/reverse honoured)
- Spinner animation + elapsed-time counter on `working` panes
- Project switcher combo in status bar (writes back to `projects.json`)
- "+ pane" button to open a default or custom role

### Added — Iter 4
- Window geometry + splitter sizes persisted via `QSettings`
- Role-aware default cwd resolution (frontend→web, backend→api, ...)
- `--append-system-prompt-file <role.md>` so specialist override applies even when cwd is the project root
- Event audit log at `runtime/events.log` (JSONL: spawn/assign/send/close/done)
- Cleaned redundant 2.7s close path in main_window

### Added — Iter 5
- Crash recovery: `_expected_exit` flag distinguishes user-close from claude crash; crashed panes show orange "exited" state with respawn affordance
- Spawn errors surfaced in status bar
- Font-size shortcuts inside terminal (Ctrl+= / Ctrl+- / Ctrl+0)
- Lead pane shows active project name in header (`Lead · pms`)
- Verified `takkub done` end-to-end (done → 2.5s grace → orchestrator.close → pane removed)

### Added — Iter 6
- Bottom dock `LogsPanel` that tails `runtime/events.log` every 1s
- F1 / `?` help dialog with `takkub` cheatsheet + shortcuts
- "⟶ assign" quick-assign button (role picker + multi-line task input)
- `takkub close-all` command (closes every teammate, keeps Lead)

### Added — Iter 7
- Session resume: `claude --continue` passed automatically on respawn within 5min in the same cwd
- Desktop notification (`QSystemTrayIcon`) when an agent calls `takkub done`
- Export pane buffer to `.txt` via `⤓` button in the header (`runtime/exports/<role>-<ts>.txt`)
- Per-role font size persisted in `QSettings`

### Added — Iter 8
- Pane header shows cwd basename (`Frontend · pms-web`)
- Status bar live count: active panes + working panes (2s tick)
- Auto-spawn presets per project (`projects.json` → `presets: ["frontend", "backend"]`)
- Logs panel: filter by event type + role substring

### Added — Iter 9
- Pane minimise/restore toggle (`▾`/`▸` button collapses the body to the header strip)
- Logs panel text search (case-insensitive substring across rendered line)
- Custom-role colour picker via `QColorDialog` in the "+ pane → custom..." flow
- README rewritten to reflect all current features

### Verification — Iter 9 (final)
- End-to-end multi-agent flow tested live with the real PMS project:
  - backend created `pms-api/src/health/health.controller.ts` + module wiring
  - frontend waited for backend's `takkub send` message before implementing `pms-web/app/agent-takkub-test/page.tsx` with Ant Design (agent inspected project conventions instead of using the suggested shadcn)
  - both agents called `takkub done`; both panes auto-closed without manual intervention
- Multi-agent peer-to-peer comms + auto-close lifecycle verified against `runtime/events.log`

[0.1.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.1.0
