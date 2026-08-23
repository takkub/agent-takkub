# Master dev plan — 2026-08-23 → 2.0.0 (user directive: "ทำแผนเตรียมตัว dev ละเอียดๆ ห้ามหลุด")

อ่านคู่กับ `docs/v2/2.0.0-migration-plan.md` (V2 layout/authority) · `docs/plans/workspace-1.2.0-design/` (IDE-lite
workspace — แผนภายนอก 27 ไฟล์ vendor เข้ามา + ข้อแก้ 3 ข้อ §4) · epic #365 (workspace) · #362 (Phase 10) · #364 (RAM diet)
· #361/#360 (auto-migrate + core_home step) · `docs/release-checklist.md` (SemVer policy ใหม่)

**Flow diagram:** `docs/architecture/v2-workspace-flow.md` (โครงสร้าง runtime · timeline · boot flow · resolver · RAM)

## 0. กฎการทำงานที่ใช้ทั้งแผน (ตกลงกับ user 2026-08-23 — ห้ามละเมิด)

| กฎ | ที่มา |
|---|---|
| **ห้ามรัน full `takkub qa-gate` ในเครื่อง user** — targeted เฉพาะไฟล์ที่แตะ รอบเดียว · CI คือ full gate | user: "รันทีไรเครื่องแทบพัง" / "รันเสร็จก็ไปรันบน CI อีก เสียเวลาเปล่า" |
| **แตะได้แค่ repo agent-takkub** — โปรเจคอื่นของ user / docker volume ห้ามลบห้ามแก้ · คำถาม ≠ คำสั่ง | user: "ยุ่งแค่ cockpit เราอย่างเดียว" |
| **เจอบั๊กระหว่างทาง → แก้เลย ไม่ต้องถาม** แต่ Lead อ่าน diff เอง + รัน targeted เองก่อน commit (ไม่เชื่อ done-report) | user: "เจอปัญหาอะไรแก้ให้หมด ไม่ต้องรอถาม" |
| **pane พร้อมกันไม่เกิน 3** (1 pane ≈ 650 MB: claude CLI ~540 + WebEngine ~99) · งานสั้น/ไม่ต้อง diversity → `--mode subagent` | #364 |
| pane >1 แตะ repo เดียวกัน → `--isolation worktree` + merge ผ่าน `takkub worktree merge --role` (ห้าม git ดิบ #358) | CLAUDE.md #81 |
| **SemVer จริง**: minor = feature ที่ของเดิมยังใช้ได้ · patch = แก้บั๊กล้วน · 2.0.0 = ของเดิมพัง (Phase 10 เท่านั้น) | user 2026-08-23 |
| ทุก feature คิดทุก provider (claude/codex/gemini-agy/opencode/kimi/cursor) + Windows/macOS | CLAUDE.md |
| release: CI เขียวครบ 6 job ก่อน tag/publish เสมอ · GitHub release + npm publish ในรอบเดียว · ยืนยัน `npm view` | release-checklist |
| ห้ามต่อคิว assign ให้ pane ที่กำลังทำงาน (queue เคยหายตอน pane ปิด) — ยิงใบใหม่เมื่อ pane ว่าง | วันนี้ |

## 1. เวอร์ชัน → เนื้อหา (ห้ามสลับ)

| เวอร์ชัน | เนื้อหา | สถานะ |
|---|---|---|
| **1.1.0** | V2 auto-migrate ตอน boot (#361) + core_home step (#360) + `apply_pending()` ทุกบูต + ปิด #343 #355 #356–#359 + Wave C (resolver shadow-read, PermissionEngine) + test isolation `~/.takkub` + `.gitignore` `/v2/` | **วันนี้** — commit สุดท้าย + CI + release |
| **1.2.0** | Workspace IDE-lite (#365) ตาม `workspace-1.2.0-design/` + ข้อแก้ 3 ข้อ (§4) + RAM levers ที่วัดแล้วได้ผล (#364) | เริ่มหลัง 1.1.0 ออก |
| **2.0.0** | Phase 10: สลับ source of truth เป็น `v2/` (#362) — dual-write → readers V2 (`TAKKUB_V2_AUTHORITY`) | หลัง 1.1.0 soak + drift = 0 |

## 2. 1.1.0 — ลำดับตายตัว

1. ✅ backend: `MigrationEngine.apply_pending()` + auto_migrate_boot ใช้มันแทน step-1-only (เครื่อง `mixed` ต้องได้ step ใหม่ทุกบูต)
2. Lead: commit รวม #360 + test isolation (qa) + apply_pending + CHANGELOG + แผนนี้ → push → CI 6 job
3. ปิด #360 #361 (โน้ตร่างไว้แล้ว)
4. bump 1.1.0 (pyproject/package.json/`__init__` + CHANGELOG heading) → `test_version_sync` → wheel → commit → tag → push → CI → GitHub release (`rel110.md`) → `npm publish` → `npm view` ยืนยัน
5. **หลัง release**: prod ของ user อัป 1.1.0 → บูตครั้งแรก `apply_pending()` รัน step 8 (core-internal-store) บน prod → ดู `doctor --storage-layout` 9/9 ไม่มี rollback · ถ้า rollback → หยุดทุกอย่าง มาดูก่อน

## 3. Phase 10 / 2.0.0 (#362) — backend

- **ชิ้น 1 dual-write** (ใบงาน `task-362a` พร้อม): helper `core/storage/dual_write.py` reuse `RegistryMapping` จาก `steps_v1.py` · V1 atomic ก่อน V2 best-effort (`v2_write_failed` ไม่ raise) · writer 6 config + projects registry (inventory ในคอมเมนต์ #362) · **เกณฑ์ผ่าน**: แก้ pin ผ่าน `role_models.save` บน fixture ที่ migrate แล้ว → `model_pin_v2_drift` = 0
- ชิ้น 1b: state writers (auto_resume/provider_state/plan_tier/exec_mode/claude_auth_config/graft_store/auto_issue_*)
- **soak**: 1.1.0 + dual-write บนเครื่องจริง ≥ 1–2 สัปดาห์ drift = 0 (auto-issue จะฟ้องเองถ้าไม่ 0)
- ชิ้น 2: readers สลับไป V2 ทีละ domain ใต้ `TAKKUB_V2_AUTHORITY` (default ON ใน 2.0.0, `=0` กลับ V1) · fail-open · resolver กลับด้านเป็น V2 authoritative · `doctor`/`validate` รายงาน `v2` · rollback path = flag 0 + `migrate rollback`
- ข้อพึ่งพา: #361 ✅ · #360 ✅ · drift telemetry

## 4. Workspace 1.2.0 (#365) — ข้อแก้ 3 ข้อต่อแผนภายนอก (บังคับ)

1. **เลขเวอร์ชัน = 1.2.0** (ไม่ใช่ 1.1.0 ที่แผนเขียน — 1.1.0 คือ V2 auto-migrate)
2. **RAM hard rule**: Monaco Editor WebView **1 ตัวทั้งแอป** + Preview WebView **1 ตัวทั้งแอป** วางใน dock คงที่นอก project tab (สลับ*เนื้อหา*ไม่สลับ widget — ไม่ชนกฎห้าม reparent) · **lazy create เมื่อใช้ครั้งแรก, destroy เมื่อปิด** (ตอนไม่ใช้ = +0, ตอนใช้ ≈ +200–300 MB ไม่โตตามจำนวนโปรเจกต์) · ใช้ lever Discard (#364) กับมันเมื่อซ่อน · `workspace.editor = monaco | native` — native (`QPlainTextEdit`+`QSyntaxHighlighter`, +5–15 MB) เป็น opt-in
3. **Obsidian hardening (เฟส 8) รอหลัง #362 ชิ้น 1** — `project_id` identity ต้องมาจาก V2 registry ตัวเดียว ไม่นิยามซ้ำ

### เฟส + owner + ไฟล์ที่แตะ (กัน PR ชนกัน)

| เฟส | เนื้องาน | owner | แตะไฟล์หลัก | ขนานได้กับ |
|---|---|---|---|---|
| 0 | audit delta vs main ปัจจุบัน, ADR, เก็บ keepalive tests | frontend | docs | ทุกอย่าง |
| 1 | Workspace Shell: QSplitter + Explorer (lazy tree, ignore policy, root containment, ctx menu, จำ width/collapsed) — **ยังไม่แก้ไฟล์** | frontend (worktree) | `project_tab.py`, `main_window.py`, `project_explorer.py`(ใหม่), `project_file_index.py`(ใหม่) | #362 (core), #364 lever 6 (doctor) |
| 2 | Monaco read-only: bundle local, QWebChannel bridge, internal tabs, terminal path → Open in Takkub | frontend + devops (packaging) | `editor_widget.py`, `static/editor/…`, `terminal_widget.py` | #362 |
| 3 | Safe edit: Ctrl+S atomic, dirty, conflict UI (mtime+size+sha256), binary/large guard — **reviewer บังคับ** (cockpit เขียนไฟล์ซอร์สของ user ครั้งแรก) | frontend + backend | `editor_service.py`, `file_watch_service.py` | — |
| 4 | Git changes + diff (background git status, debounce) | backend + frontend | `git_changes_service.py` | — |
| 5 | Preview: single WebView ทั้งแอป, URL/file, device presets, `takkub preview` CLI (IPC), navigation policy — **reviewer บังคับ** (bridge/privilege) | frontend + backend | `preview_widget.py`, `preview_controller.py`, `cli.py` | — |
| 6 | Design workflow: Designer role/policy (anti-AI checklist), publish artifact → auto-focus Preview, Approve/Revise, reuse `design_review_html.py` | critic + frontend + backend | `design_workspace.py`, `design_actions.py`, events | — |
| 7 | Optional design MCP (Storybook ก่อน, 21st/Figma/Penpot opt-in ผ่าน Capability Hub, ห้าม bypass PermissionEngine) | backend | capability hub | — |
| 8 | Obsidian hardening (canonical id/dedup/allowlist) — **หลัง #362 ชิ้น 1** | backend | obsidian modules | — |
| 9 | OpenViking adapter (optional, HTTP/MCP sidecar, read/index only) — **แยก follow-up ไม่บล็อก 1.2.0** | backend | ใหม่ | — |
| 10 | diagnostics/soak: WebEngine lifecycle บน Windows, doctor workspace checks | qa + devops | doctor, tests | — |

กฎ PR: **ห้ามมี 2 branch แตะ `ProjectTab`/`MainWindow` พร้อมกัน** — เฟส 1 ต้อง land ก่อนใครแตะสองไฟล์นี้อีก

### Acceptance
`16_ACCEPTANCE_CRITERIA.md` ทั้ง 19 ข้อ + **RAM**: วัดก่อน/หลังเปิด editor+preview บน 3 โปรเจกต์ — เพิ่มไม่เกิน +300 MB รวม และ 0 เมื่อปิด · QA gate (CI) เขียว · ไม่แก้ expected เดิมเพื่อให้ผ่าน

## 5. RAM diet (#364) — วัดก่อน ทุกข้อต้องมีตัวเลขก่อน/หลัง

| lever | ทำอะไร | owner | ลำดับ |
|---|---|---|---|
| 6 visibility | `doctor --ram`/perf chip: RAM ต่อ pane แยก claude/WebEngine/node children | devops | **ก่อนทุกอย่าง** |
| 1 discard hidden | spike `QWebEnginePage.setLifecycleState(Discarded)` บน pane ซ่อน → re-render จาก pyte · วัดคืนจริง/เวลา re-attach/ไม่ชน marker+IPC | frontend (หลังเฟส 1 land) | spike → ถ้าได้ ≥ 60 MB/pane ค่อยทำจริง |
| 2 subagent | Lead เลือก `--mode subagent` อัตโนมัติสำหรับงานสั้น/ไม่ต้อง isolation/ไม่ต้อง diversity + บอกในรายงาน | backend (orchestrator routing) | หลัง #362 ชิ้น 1 |
| 3 proactive cap | `max_panes_global` default จาก RAM จริง `(free − reserve)/650MB` ปฏิเสธ spawn ก่อนหนืด | backend (scheduler policy) | คู่กับ 2 |
| 4 MCP/node | วัดว่า pane ซ้ำ spawn MCP ไหม; role ไม่ใช้ browser ไม่ควรมี | devops | ตามผลวัด |
| 5 main process | tracemalloc top allocators (pyte history/transcript/display cache/closed-pane widgets) → cap ที่วัดได้ | backend | ตามผลวัด |

## 6. ที่ยังไม่ปิดและต้องไม่ลืม

- Wave B เกณฑ์เดียวที่ยังไม่เป็นทางการ: spawn→assign→done round-trip บน prod หลัง migrate (ใช้งานจริงทั้งวันแล้วแต่ไม่มี `takkub assign` จาก Lead ของ prod เป็นหลักฐานตรง) — ปิดได้ทันทีที่ user สั่งงานสักใบจาก Lead pane บน prod
- Mac: ตัดออกจาก critical path (user) — auto-migrate บนเครื่องเพื่อนที่เป็น Mac จะเป็นครั้งแรก safety net = auto-rollback
- ตอนอัป prod เป็น 1.1.0: `version.json` ใน v2 ค้าง 1.0.86 → `apply_pending()` ตอนบูตจะ upsert ให้เอง
- auto-issue บนเครื่องไม่มี `gh` → local fallback เท่านั้น (doctor บอก) — เพื่อนที่ไม่มี gh ต้องส่งเองถ้ามี signal
- #364 lever 1 ถ้า spike ไม่คุ้ม ต้องบันทึกตัวเลขลงใบ ไม่ทำต่อเงียบๆ
- #365 เฟส 9 OpenViking: ห้าม vendor AGPL เข้า repo MIT — sidecar เท่านั้น

## 6b. สถานะ ณ 2026-08-23 18:40 (อัปเดตโดย Lead)

| งาน | สถานะ |
|---|---|
| 1.1.0 | ✅ ออกแล้ว (npm latest, tag `v1.1.0`) |
| #362 Phase 10 | ชิ้น 1/1b/1c dual-write ครบ + audit test ✅ · ชิ้น 2 readers ใต้ `TAKKUB_V2_AUTHORITY` (default OFF) ✅ · soak drift=0 รอเวลา → flip default = release decision (2.0.0) |
| #364 RAM diet | ✅ ทุก lever ปิด (6 วัด · 4 MCP stale variant bug · 2 subagent · 3 RAM cap · 1 discard ~65 MB/pane · 5 profile ไม่ leak) |
| #365 Workspace | เฟส 0–8 + 10 ✅ merged (widget/UI `a096ef7`, เฟส 7 `dffeb63`, เฟส 8 `85b9050`, reviewer MUST-FIX + SHOULD ทั้งหมดปิด) · เฟส 9 OpenViking = follow-up นอก 1.2.0 · acceptance: RAM +155 MB/3 โปรเจกต์ (budget 300) ✅ · soak 25×3 ✅ · Monaco ใน wheel ✅ · 19 ข้อ = 13 ✅ / 5 ต้องตาคน (2,5,6,7,8 — user เช็ค 5 นาทีตอนตื่น) / 1 = CI · ⚠ #366 QtWebEngineProcess ไม่ reap หลังปิด (offscreen) → devops verify จอจริง **ก่อน bump 1.2.0** |
| CI cadence | รวบ merge → targeted ชุดเดียว (รวม repo guard) → push ครั้งเดียว — batch 1 `8880e19`, batch 2 `6e0c20a`, batch 3 `3166bb8` (macOS flake 1 test → แก้ใน batch 4), batch 4 `4bfdf75` |
| 1.2.0 release | รอ: CI batch 4 เขียว + #366 ปิด → `docs/release-checklist.md` §0b → bump minor |

## 7. ลำดับรวมหลัง 1.1.0 ออก (pane ≤ 3 พร้อมกัน)

```
backend : #362 ชิ้น 1 dual-write → ชิ้น 1b state writers → #364 lever 2+3 → (รอ soak) → #362 ชิ้น 2 → 2.0.0
frontend: #365 เฟส 0–1 (worktree) → merge → เฟส 2 → #364 lever 1 spike → เฟส 3 → 4 → 5 → 6
devops  : #364 lever 6 (doctor --ram) → lever 4 (MCP audit) → Monaco bundle packaging (เฟส 2) → release gates
qa      : ปิดท้ายทุกเฟส (targeted) + เฟส 10 soak · reviewer ที่เฟส 3/5
release : 1.2.0 เมื่อเฟส 1–5 + RAM acceptance ผ่าน (เฟส 6–9 ออกเป็น 1.2.x/1.3.0 ได้)
```
