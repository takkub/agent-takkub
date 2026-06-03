# agent-takkub Improvement Audit — v0.6.0

**วันที่:** 2026-06-03 · **วิธี:** 8-dimension fan-out + adversarial verify (60 agents) + completeness critic
**ผล:** 51 raw findings → **45 confirmed** (6 rejected/already-handled) + **6 cross-cutting gaps**

> หมายเหตุ: verifier อ่าน code จริงทุก finding และปรับ severity ลงหลายตัว (med→low) เมื่อบริบท solo-dev/
> single-user desktop ทำให้ impact จริงต่ำ. severity ในเอกสารนี้ = ค่าหลัง verify แล้ว.

---

## 🔴 Tier 1 — Quick wins (S effort, ควรแก้ก่อน) — ปิด cluster bug ของ v0.6.0 shard

| # | Finding | kind | location | fix |
|---|---|---|---|---|
| 1 | `--shards N` ไม่มี upper bound — พิมพ์ผิด `--shards 50` spawn 50 pane จริง (เพดาน ~999) | bug/med | cli.py:620-627, cmd_assign 143-174 | clamp `1 <= shards <= 8` ใน cmd_assign ก่อน loop + test |
| 2 | Stale 45-min shard-timeout timer ฆ่า fan-out รอบใหม่ key เดียวกัน (ไม่มี generation guard) | race/med | orchestrator.py:1909-1915, 2313-2320 | เพิ่ม `generation:int` ใน ShardGroup, capture ใน lambda, bail ถ้า mismatch |
| 3 | Shard done() มาหลัง timeout → ถูก drop เงียบ Lead ไม่รู้ว่าเสร็จ | bug/med | orchestrator.py:2643, 2680-2690 | done() ที่ไม่เจอ group → ส่ง late-complete notice แทน swallow |
| 4 | Shard crash ตอน Lead down → ไม่ถูก mark failed → group ค้าง 45 นาที | bug/med | orchestrator.py:2081-2117 | ย้าย failed-bookkeeping ขึ้นก่อน Lead-alive early return (หรือ hoist เข้า _on_session_exit) |
| 5 | Shard spawn fail → orphan group เต็ม 45 นาที + label ผิดเป็น "NO RESPONSE" | bug/low | orchestrator.py:1880-1886 | บันทึก spawn-failed shard เข้า group.failed (mirror respawn-capped path) |
| 6 | Dead-man watchdog `os._exit(1)` ข้าม snapshot + resume briefs → state <60s + briefs หายหมด | debt/low | app.py:115-143 | best-effort `write_session_snapshot()`+`write_resume_briefs()` ใน try/except ก่อน exit (ทั้งคู่ Qt-free) |
| 7 | `display_lines()` ใช้ `hash(tuple())` salted/collision-silent → อาจ respawn pane ที่ทำงานอยู่ | debt/low | orchestrator.py:3583-3592 | เปลี่ยนเป็น `hashlib.blake2b(...).hexdigest()` |

## 🟠 Tier 2 — Correctness / robustness (M)

| # | Finding | location | fix |
|---|---|---|---|
| 8 | Auto-chain verify hop **ไม่ fire** ถ้า pane auto-chain ตัวสุดท้ายถูก `close` แทน `done` | orchestrator.py:2460-2462, 2671-2678 | close() เช็ค auto_chain ก่อน pop → trigger handoff/warn Lead (mirror respawn-capped fix line 1795) |
| 9 | Restored teammate pane **เสีย task ที่ค้าง** เงียบๆ หลัง cockpit restart (task/uuid in-memory เท่านั้น) | orchestrator.py:3228-3268, 3182-3213 | persist last_assigned_task+uuid ลง last-session.json → restore ด้วย --resume หรือ re-paste task; อย่างน้อยแจ้ง user ว่า pane เริ่มใหม่ |
| 10 | `done()` requires-commit รัน `git status` timeout 10s **บน Qt main thread** (freeze class เดิม #33-35) | orchestrator.py:2599-2605 | ย้ายไป QThreadPool worker (pattern มีใน update_worker แล้ว) หรือลด timeout เป็น ~3s |
| 11 | `scan_artifacts` (harvest) os.walk + stat ทั้ง tree บน main thread | orchestrator.py:196-250 | QThreadPool worker หรือ wall-clock budget (priority ต่ำ — manual command เท่านั้น) |
| 12 | Per-shard Chrome isolation เป็น **convention ใน qa.md เท่านั้น ไม่ enforce ใน code** | orchestrator.py:1374-1428 | inject env ที่ orchestrator คุม (TAKKUB_CHROME_PORT_FILE/PROFILE_DIR). **⚠ อย่าใช้ `9222+idx`** — ทีม reject ไปแล้ว (ชนข้าม project) ใช้ `--port 0` + per-shard port-file/profile-dir |

## 🟡 Tier 3 — Product / feature (value สูง)

| # | Finding | effort | note |
|---|---|---|---|
| 13 | **Pipeline Settings เป็น dead-end** — save template แต่ไม่มีปุ่ม/CLI สั่งรัน, routing ไม่อ่าน, Lead ไม่เห็น (gap เด่นสุด v0.6.0) | L | เพิ่ม `orchestrator.run_pipeline(template_id, project)` อ่าน hops → fire parallel-in-hop/sequential-between (reuse `_inject_auto_chain_handoff`) + ปุ่ม ▶ Run + CLI `takkub pipeline run` |
| 14 | **Incremental win** ก่อน executor เต็ม: inject activeTemplate + rolesEnabled เข้า Lead context | S | `_render_lead_context` เพิ่ม section จาก pipeline_config.load() → ตั้งค่าใน dialog มีผลต่อ Lead ทันที |
| 15 | ปุ่ม 🎨 UI Review hard-code path ไม่อ่าน template 'design' (qa+frontend hop หาย) | M | เมื่อมี run_pipeline แล้วให้ปุ่มเรียก `run_pipeline('design')` แทน |
| 16 | `takkub doctor` มีแค่ CLI — ไม่มีปุ่มใน cockpit | M | ปุ่ม 🩺 Doctor → dialog `format_report()` + ปุ่ม Fix (`run_auto_fixes()`) |
| 17 | Provider chip ไม่แยก "not installed" — โชว์เขียวทั้งที่ spawn เป็น claude (false model-diversity) | S | 3-state chip: เขียว=available / เทา=toggled-off / เหลือง=enabled-but-not-installed (ใช้ `_provider_available()` ที่มีแล้ว) |

## 🔵 Tier 4 — Tech debt (incremental, gated by ~1490 tests)

| # | Finding | effort |
|---|---|---|
| 18 | `orchestrator.py` 4003 LOC god object — แยก ContinuityManager / WatchdogService / PendingDeliveryStore / ShardCoordinator (1 collaborator/PR, ไม่ rewrite) | L |
| 19 | `spawn()` 620 บรรทัด 4 provider branch copy-paste (env block ซ้ำ 5 จุด) — ProviderSpec + shared `_finalize_spawn` (ตัด ~250 บรรทัด) | M |
| 20 | `main_window.__init__` 536 บรรทัด — แยก `_build_status_bar`/`_build_tabs`/`_wire_signals` (ordering hazard มี comment เตือนอยู่) | M |
| 21 | Magic-number `singleShot(150,...)` ซ้ำ ~15 จุด — hoist เป็น named constant | S |

## ⚪ Tier 5 — Docs (CLAUDE.md sync ดีแล้ว — drift กระจุกที่ ARCHITECTURE.md)

| # | Finding | location |
|---|---|---|
| 22 | ARCHITECTURE.md regeneration: list `provider_dialog.py` (ลบไปแล้ว), ขาด pipeline_config/pipeline_dialog/project_rules, miscount 41 vs 45 | docs/ARCHITECTURE.md:46-117 |
| 23 | spawn flags doc บอก `--continue` (ทีม reject ไปใช้ `--resume/--session-id`) + ขาด --strict-mcp-config/--model/Task-deny | :157-164 |
| 24 | IPC schema list 7/15 commands + assign payload stale (ขาด requires_commit/auto_chain/shard_total) | :191-209 |
| 25 | Persistence table ขาด `~/.takkub/` ทั้งหมด (4 JSON: role-providers/disabled-providers/pipelines/plan) | :211-222 |
| 26 | ขาด QA shard fan-out + test surface บอก 3 ไฟล์ (จริง 90) | :240-256 |
|    | **action:** regenerate 1 รอบ + wire `docs_verify.py` เข้า CI (มี module แล้ว ยังไม่ wire) | — |

## ➕ Tests ที่ควรเพิ่ม (S ละ, bundle ได้)

shard dead-Lead queue path · all-shards-failed aggregate · `effective_provider_for` combined toggle+not-installed + gemini probe + except path · `snapshot_state` filtering · `provider_state.load` stale-key/non-dict

---

## 🧠 Cross-cutting root causes (จาก completeness critic — leverage สูงสุด)

หลาย finding ข้างบนไม่ใช่ bug อิสระ — เป็น **อาการของ decision เดียวกัน** ที่ auditor มองข้าม:

### A. Control plane เป็น open-loop บน marker-scraping (ไม่มี ack channel) — fragility สูงสุด
orchestrator สั่ง teammate ด้วยการเขียน bracketed-paste bytes เข้า PTY แล้ว "ยืนยัน" delivery ด้วยการ
scrape rendered terminal text (`is_at_ready_prompt()` match hardcoded English substring: 'bypass permissions',
'esc to interrupt', 'trust this folder'...). **ไม่มี machine-readable handshake กลับจาก claude/codex/gemini.**
- ผล: ถ้า upstream CLI เปลี่ยน UI/i18n → delivery + done-detection + idle/stuck watchdog **พังพร้อมกันหมด** โดยไม่มี test จับ
- timing magic-numbers ทั้งหมด (PASTE_ENTER_DELAY, 45s timeout, 5s flush) เป็น downstream ของ root นี้
- **action:** รวม marker strings เป็น registry module 1 ที่ + e2e smoke test ที่ fail ดังๆ เมื่อ marker ไม่ match CLI ที่ติดตั้ง; หาว่ามี structured done-signal จาก CLI ไหม

### B. Durable queue แต่ final hop lossy (pop-before-confirm)
`_pending_lead_cc`/`_pending_done_notices` persist ลง disk เพื่อรอด Lead-down — แต่ flush คือ fire-and-forget
paste แล้ว **pop+re-save empty ก่อนยืนยันว่าถึง** (line 2200/2342). ถ้า paste ตอน Lead mid-render → หายถาวร =
persistence layer เป็น theater. **action:** อย่า pop จน confirm; gate บน `is_at_ready_prompt()` เหมือน `_send_when_ready`

### C. Multi-tab isolation + shard-group state untested & ไม่ persist
`_shard_groups` key ถูก (`project_ns::base_role`) แต่ **ไม่ persist** ต่าง `_pending_*` — restart/watchdog kill กลาง
fan-out = group หายเงียบ + re-spawned shards report done เป็น standalone, consolidated handoff ไม่มาเลย. ไม่มี
integration test crash-mid-fanout / cross-project queue isolation. **action:** persist shard-group state หรือ
ให้ restore ปฏิเสธ re-spawn shard + บอก Lead "re-issue"; + cross-project leak test

### D. Main-thread blocking เป็น pattern ไม่ใช่ 2 จุดโดด
git status (#10), scan_artifacts (#11), QWebEngine spawn — รันบน Qt thread ทั้งหมด, backstop เดียวคือ 30s
suicide watchdog (ที่เองก็ drop state). ไม่มี graceful degradation ระหว่าง "fast op" กับ "นuke process".
**action:** ตั้ง invariant "no blocking I/O on Qt main thread" + audit ทุก call site เป็น 1 workstream

### E. Security threat-model ต้อง decide ชัด
`send` command เปิดให้ local process ใดก็ได้ที่อ้าง non-lead role (cli_server.py:121-129 guard แค่ from=lead) →
forge peer message ได้ไม่ต้อง token; + message body ไม่ sanitize control-sequence ก่อนเขียน PTY (short path
<200 char เขียน raw) = terminal-escape injection เข้า agent ที่รัน --dangerously-skip-permissions. **action:**
decide+document threat model (ถ้า "trust all local" = เขียนให้ชัด; ถ้าไม่ = token ทุก command), strip ESC/CSI จาก body

### F. events.log เป็น write-only — ไม่มี read/triage path
เขียน rich JSONL audit (spawn_failed, delivery_unconfirmed, shard_group_timeout, ...) แต่ไม่มี command/panel อ่านกลับ
— bug ตระกูล "fail silently" ทั้งหมดมองไม่เห็นเพราะ record เดียวคือ log ที่ไม่มีใครอ่าน. **action:** `takkub events
--since` / cockpit panel surface failure-class events = mitigation ถูกสุดของทั้งตระกูล silent-failure โดยไม่ต้อง redesign

---

## ลำดับแนะนำ

1. **Tier 1 batch** (shard cluster + clamp + watchdog snapshot) — S ทั้งหมด, ปิดหนี้ v0.6.0, ทำรอบเดียว
2. **F (events read path)** — cheap, ทำให้ silent-failure ทั้งหมดมองเห็น (วัดผล Tier 1 ได้ด้วย)
3. **Tier 2** (auto-chain close, restored-task, off-main-thread) + **#14** (Lead-context inject — incremental Pipeline win)
4. **Docs regeneration** (Tier 5, 1 รอบ) + wire docs_verify CI
5. **#13 Pipeline executor** (L — feature ใหญ่, vote ว่าคุ้มไหมก่อน)
6. **A (marker registry)** + **Tier 4 god-object decomposition** — งานยาว ทำเป็น track แยก

**Dropped (adversarial filter ตัดออก):** CLI shard non-atomic (async ack ไม่ใช่ sync), paneRequested coupling
(synchronous same-thread ยืนยันแล้ว), 27 broad except (โดน surface/queue/isolate หมด), scalability ceiling
(design decision ไม่ใช่ defect), pane-crash ไม่แจ้ง Lead (มี _warn_lead_respawn_capped แล้ว), events.log TOCTOU
(single-thread ไม่มี race)
