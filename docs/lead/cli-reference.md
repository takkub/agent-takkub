# takkub CLI — full reference (Lead)

> ย้ายมาจาก cockpit CLAUDE.md (token diet 2026-08-04) — CLAUDE.md เก็บเฉพาะคำสั่งที่ใช้บ่อย
> ไฟล์นี้คือ reference เต็ม อ่าน on-demand เมื่อต้องใช้คำสั่งที่ไม่คุ้น

## คำสั่งทั้งหมด

```bash
takkub list                                            # ดูสถานะ panes ทั้งหมด
takkub status                                           # per-pane progress + stall detection (post-compact awareness)
takkub inbox [--role <role>]                           # (lead) อ่านเนื้อ done/FAILED report ที่ยังค้างคิว — status บอกแค่ "queued", inbox โชว์เนื้อจริง (#231); ⚠ unconfirmed origin = role ถูก respawn ระหว่างรอส่ง (#228)
takkub wait [--role <r>]... [--timeout <s>]            # (lead) บล็อกจนกว่า done/FAILED report ของ role นั้นๆ ถึง Lead pane จริง — ไม่ใช่แค่หายจาก list (#242); ไม่ใส่ --role = รอทุก role active; ไม่ใส่ --timeout = default 1800s, cap 1800s (#253 — เดิม 7200s); waiter ซ้อนกันใน project เดียวกัน attach เข้าตัวเดิมอัตโนมัติ ไม่กองซ้ำ; role ที่ done/pane ปิดไปแล้วหรือไม่เคยถูก spawn resolve ทันที ไม่รอครบ timeout (#249); พิมพ์ heartbeat ระหว่างรอ; **#253:** ตื่นก่อนกำหนดถ้ามี blocking report (FAILED/spawn-failed/ฯลฯ) จาก role นอก --role ค้างอยู่ (`interrupted — [role] needs attention: ...`, exit code ≠ 0) — เช็คเนื้อหาด้วย `takkub inbox` ก่อน แล้วยิง `takkub wait` ใหม่เพื่อ resume watch role ที่ยังค้าง
takkub wait --cancel                                   # (lead) เลิกรอ waiter ที่ค้างอยู่ของ project นี้ ไม่ต้องรู้ wait_id (#249); `close-all` ก็เก็บให้อัตโนมัติ
takkub assign --role frontend "<task>"                 # spawn (ถ้ายังไม่เปิด) + ส่ง task
takkub assign --role reviewer --mode subagent "<scan>" # native child ของ provider เดียวกับ Lead; ไม่เปิด pane/ไม่ใช่ model-diversity
takkub subagent-done --role reviewer "<summary>"        # child ปิดงานเข้า ledger/inbox/wait (คำสั่งอยู่ใน task capsule)
takkub assign --role backend --cwd <path> "<task>"     # override role-aware default cwd
takkub assign --role qa --model <haiku-or-flash-id> "<scan>" # override model เฉพาะ pane ที่ spawn ใหม่; precedence: assign > role > provider > CLI default
takkub assign --role backend --effort low "<task>"     # (#323) override reasoning-effort เฉพาะ pane ที่ spawn ใหม่ (low/medium/high); precedence: assign > role > TAKKUB_TEAMMATE_EFFORT env > tier default; provider ไม่มี effort knob (opencode/kimi/cursor วันนี้ — gap #103) = ignore เงียบ ไม่ error; provider มี knob แต่ไม่รับ level นี้ (เช่น xhigh บน codex) = error ชัดก่อน spawn; agy/gemini มี `--effort` จริง (#125 ที่เคย disable ไว้ ถูก fix ต้นทางใน agy 1.1.10 แล้ว — ดู provider_spec.gemini_spec)
takkub assign --role backend --requires-commit "<task>" # gate done: flag uncommitted changes ให้ Lead (Lead commit)
takkub assign --role backend --auto-chain "<task>"     # impl done → auto verify sequence (devops→qa) ไม่ต้อง propose
takkub assign --role qa --shards 4 "<task>"            # fan-out N parallel shard panes (<role>#1…#N · env TAKKUB_SHARD/_TOTAL)
takkub assign --role qa --plan --shards 4 "<task>"     # plan-first: planner pane แบ่ง N buckets → auto fan-out qa#1…#N ฉลาด (ต้อง --shards ≥ 2)
takkub assign --role frontend --isolation worktree "<task>" # pane รันใน git worktree+branch แยก (wt/<role>-<ts>) — build ขนานไม่ชนกัน · done → Lead ได้ merge PROPOSAL (ไม่ auto) · ไม่ใช่ git repo → fallback shared+warn (#81)
takkub worktree list [--cwd <path>]                    # ดู wt/* worktrees + commits-ahead + dirty (lead only · ใช้ได้แม้ cockpit ปิด)
takkub worktree merge --role <r> [--keep]              # merge --no-ff branch ล่าสุดของ role กลับ main + cleanup (conflict → auto-abort, worktree อยู่ครบ) · หรือ --branch wt/... ระบุเอง
takkub worktree clean [--force]                        # เก็บกวาด wt/* ที่เหลือค้าง — default ลบเฉพาะ clean+ไม่มี commit · --force ลบหมด (งาน dirty หาย!)
takkub send --to backend "<message>"                   # peer message (CC Lead อัตโนมัติ)
takkub goal "<objective>"                              # ตั้งเป้าหมาย session — prepend เข้าทุก assign task หลังจากนี้
takkub goal                                            # โชว์ goal ปัจจุบัน
takkub goal --clear                                    # ล้าง goal
takkub harvest --role <role>                           # กู้งานของ pane ที่ทำเสร็จแต่ลืม takkub done (scan artifacts)
takkub close --role qa                                 # ปิด pane เดียว
takkub close-all                                       # ปิด teammate ทั้งหมด (Lead รอด)
takkub end-session --note "<สรุป>"                     # เขียน session summary ลง runtime/sessions + vault mirror
takkub qa-gate                                         # canonical gate (#325): venv-check → full pytest → ruff check src/tests/ → lint-imports, 1 table + exit code, report → docs/qa/ · pure-local (cockpit ปิดก็รันได้) · ทุก role/CI/มือ user เรียกตัวเดียวกัน — ห้ามพิมพ์ pytest/ruff/lint-imports ดิบ
takkub qa-gate --targeted <path...>                    # tier กลางทาง: pytest เฉพาะ path ที่ให้ (ข้าม ruff/lint-imports, ไม่เขียน report) — full gate ไม่ใส่ flag รันครั้งเดียวที่ batch gate
takkub qa-gate --v2-flags                              # รัน gate ซ้ำพร้อมบังคับ TAKKUB_V2_ROUTER/_CONVERSATION/_CONTEXT/_BRAIN/_SCHEDULER=1 ก่อน default-on (#309)
takkub doctor                                          # diagnose cockpit env (claude/node/plugins/mcps/projects) · --fix auto-fix (ไม่ลง provider ให้ ต้องใส่ --install-providers)
takkub doctor --live                                   # + เช็ค spawn-queue wedge จาก orchestrator ที่รันอยู่ (#141) · cockpit ปิด = SKIP
takkub provider list                                   # provider ทั้งหมด + สถานะติดตั้ง + model ที่ตั้งไว้
takkub provider install <name>                         # ติดตั้ง CLI ของ provider นั้น (lead only · cursor/gemini ติดตั้งเองเท่านั้น)
takkub provider model <name>                           # ดู model ที่ provider นั้นใช้
takkub provider model <name> <model>                   # ตั้ง model (ว่าง/--clear = ใช้ default ของ CLI) · ต่อ role ตั้งที่ Settings
takkub ma [--since-hours N] [--no-net] [--json]         # (operator) maintenance sweep: issue ค้าง → PR + สถานะ CI → สิ่งที่ events.log ของ cockpit ที่รันอยู่บอกว่าพังจริง → repo พร้อม ship ไหม → แผนทำต่อ · **อ่านอย่างเดียว ไม่แก้อะไร** (การตัดสินใจว่าจะแก้อะไรเป็นงานของ Lead ไม่ใช่สคริปต์) · --no-net = ดูเฉพาะ log ในเครื่อง
takkub restart                                         # restart cockpit ทั้งแอปจาก terminal (persist state → relaunch · panes respawn) — lead/terminal only
takkub search "<query>"                                # grep บทสนทนา Claude Code เก่าทุกโปรเจค (--days N · --all · --project)
takkub services start|stop|ps|logs                     # docker compose ของ project ที่ active (cwd)
takkub pipeline run <template>                         # start pipeline template (lead only)
takkub issue new ... --severity <s>                     # (#297) cockpit เปิด issue ให้อัตโนมัติด้วยเมื่อเจอ crash หรือสัญญาณผิดปกติใน events.log — เปิดเป็น default ปิดที่ Settings → Performance หรือ TAKKUB_AUTO_ISSUE=0 · ส่งแค่ชนิด event + จำนวน + version + platform ไม่ส่งเนื้อ task/path/token
takkub issue list                                      # ดู cockpit issue queue (default อ่าน store ของ agent-takkub · --no-cockpit-bug = ของ project)
takkub issue new "<title>" --severity <low|med|high> --tag <a,b> --body "..."  # ลง agent-takkub repo เสมอ (default)
takkub issue new "<title>" --no-cockpit-bug --body "..."  # opt-out: ลง repo ของ project ที่ active (cwd) แทน
takkub mcp|plugins list·allow·deny·reset·add·remove    # ปรับ MCP/plugin policy ต่อ role (mutation lead-only)
takkub report publish <file> [--name n] [--project p] [--expires 30d|12h|none] [--label "..."]  # (#367, lead only) แชร์ไฟล์ standalone (.html/.png/.jpg/.svg/.pdf/.json/.md) ผ่าน share-token URL บน tunnel domain ของ cockpit เอง แทน Claude Artifact URL — ชื่อเดิม republish = ไฟล์ใหม่ token เดิม (ลิงก์ไม่เปลี่ยน); ปฏิเสธ HTML ที่มี external <script>/<link>/<img> (ต้อง standalone จริง)
takkub report list [--project p]                       # ดูรายการ report ที่ publish แล้ว + URL แต่ละอัน
takkub report revoke <name> [--project p] [--delete]    # ตัดลิงก์ (token ตาย) — ไฟล์ยังอยู่เว้นแต่ใส่ --delete
takkub report rotate <name> [--project p]               # ออก token ใหม่ — ลิงก์เก่าตายทันที ลิงก์ใหม่ใช้ได้
```

**ข้อจำกัดสำคัญของ `takkub report`:** ลิงก์เปิดจากนอกเครื่องได้ **เฉพาะตอน Remote เปิดอยู่จริง** (Settings → Remote enabled + tunnel connect ขึ้น) — ปิด Remote อยู่ = publish/list ยังทำงาน (เขียนไฟล์ + คืน token ปกติ) แต่ลิงก์ที่ได้ยังเปิดจากนอกไม่ได้ ทุกครั้งที่ `publish`/`list`/`rotate` จะพิมพ์บรรทัดสถานะ Remote ให้ชัดเสมอ (`Remote: เปิดอยู่ (tunnel up) → URL ใช้ได้` หรือ `Remote: ปิดอยู่ → ...`) — คำสั่งนี้**ไม่เปิด Remote ให้อัตโนมัติ**, ต้องไปเปิดเองที่ Settings → Remote ก่อน (ยังไม่มีคำสั่ง CLI สำหรับเปิด/ปิด Remote)

ถ้าไม่ระบุ `--cwd`: frontend/designer→web, backend→api, mobile→mobile (fallback web), devops→api (fallback infra), qa/reviewer/critic→first matched path

`takkub spawn` ไม่ค่อยใช้ — Lead ใช้ `assign` แทน (orchestrator spawn อัตโนมัติถ้า pane ยังไม่เปิด)

## Tooling ที่ pane มี (รายละเอียด)

- **superpowers / agent-skills** — skill libraries เรียกผ่าน `/skill-name`
- **MCP servers + plugins ต่อ role = policy เดียว** (`~/.takkub/pane-tools.json` · module `pane_tools_policy.py`) — default: qa/critic/designer ได้ playwright + chrome-devtools, role อื่นไม่มี MCP เลย (ประหยัด ~15k tokens/pane) · ปรับได้ 3 ทาง: แก้ไฟล์ตรง / `takkub mcp|plugins ...` (mutation lead-only) / chip **👥 Team → MCP Matrix / Plugins Matrix** ใน status bar · มีผลกับ pane ที่ spawn ใหม่ทันที · **user-level `~/.claude.json` mcpServers ไม่เข้า pane เด็ดขาด** (`--strict-mcp-config` + `--setting-sources project,local`)
- **MCP timeout** — `MCP_TOOL_TIMEOUT=180000` (3 นาที) inject ทุก pane โดย default — กัน browser MCP timeout 60s ที่ทำ Lighthouse audit/page load พังบ่อย override ที่ cockpit env ได้
- **rtk CLI** — token-optimized wrappers (ดู `~/.claude/CLAUDE.md`)

## Vault — โครงสร้าง + ที่ค้น (รายละเอียด)

vault root ของโปรเจคนี้: `~/WebstormProjects/second-brain`

**โครงสร้าง 3-tier (refactor 2026-06):** `99-Logs/` = volatile log (briefs, raw sessions) · `01-Projects/` = knowledge ที่ distill แล้ว · `04-Archive/` = post-mortem ถาวร

**ค้นที่ไหน (เรียงตามความสด):**
1. `<vault>/99-Logs/briefs/agent-takkub-<ts>.md` — resume brief สดสุด (transcript tail 20 exchanges ล่าสุด ต่อ session)
2. `<vault>/01-Projects/agent-takkub/sessions/<ts>-<role>.md` — session summary ราย role (`takkub end-session` เขียน)
3. `<vault>/04-Archive/agent-takkub/bugs/*.md` — bug post-mortem เก่า (root cause + fix)
4. `<vault>/01-Projects/agent-takkub.md` — project page (ส่วนเขียนมืออาจ stale — cross-check กับ git log ก่อนเชื่อ)

**ข้อจำกัด:** ` ```dataview ` block อ่านด้วย `Read` ตรงๆ เห็นแค่ query — ต้อง resolve ใช้ obsidian-vault MCP
