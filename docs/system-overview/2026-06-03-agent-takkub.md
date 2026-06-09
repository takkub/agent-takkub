---
date: 2026-06-03
project: agent-takkub — ระบบเก่งถึงระดับไหน (Capability Map · v0.6.0)
reviewer: Lead (Claude Opus 4.8)
shots:
  - runtime/exports/2026-06-03/agent-takkub/screenshots/pipeline-settings-viewport.png
---

# agent-takkub เก่งถึงระดับไหน — Capability Map (v0.6.0)

เอกสารนี้ประเมิน **ความสามารถจริง** ของ agent-takkub ณ v0.6.0 โดยอ้างอิงจาก source
จริง (46 โมดูล, ~1,490 tests) ไม่ใช่คำโฆษณา — แยกเป็น "เก่งจริงตรงไหน" กับ "เพดาน
อยู่ตรงไหน" ให้เห็นภาพตรงๆ

> **บรรทัดเดียว:** ระดับ *"orchestrator สั่งทีม dev หลาย agent ใช้งานจริงได้ทุกวัน
> สำหรับ solo-dev / ทีมเล็ก"* — multi-agent dispatch + auto-routing + parallel verify
> + self-healing lifecycle ครบมือ **แต่เพดานคือ single-machine เครื่องเดียว + พึ่ง
> Claude Code CLI** ยังไม่ก้าวเป็น platform หลายคน/หลายเครื่อง

นี่คือหน้า Pipeline Settings — ตัวอย่างความ "โต" ด้าน UX: ตั้ง dev pipeline (hops ·
parallel/sequential · templates · providers) ผ่าน UI โดยไม่ต้องแก้ code:

## สถาปัตยกรรมโดยย่อ (สิ่งที่กำลังประเมิน)

- **1 process** (Qt) ถือทุก widget + `Orchestrator` + `CliServer` บน main thread เดียว
  (ไม่มี lock by design) — เบื้องหลังมี reader-thread ต่อ pane + watchdog daemon
- **แต่ละ teammate = 1 pane:** `AgentPane` → `PtySession` (WinPTY) → `TerminalWidget`
  (QWebEngineView host xterm.js) — render เป็น xterm.js จริง ไม่ใช่ text widget
- **IPC:** `takkub` CLI → JSON over localhost TCP → `cli_server` → `Orchestrator`
- **`orchestrator.py` = หัวใจ** (~3.5k LOC): spawn / assign / send / done / close +
  auto-chain + watchdogs + continuity
- **`routing_planner.py` = สมองเลือก role** — เป็น Python testable ไม่ใช่แค่ prompt

## Capability Map — เก่งจริงตรงไหน

### 1. Orchestration (จุดที่โตสุด — mature)

- **Parallel / sequential dispatch** — Lead ตัดสินเองว่า task ไหน independent (ยิง
  คู่ขนาน `&`+`wait`) หรือ depend กัน (รอ done ทีละตัว) ตาม decision rule ใน code
- **Auto-chain** — impl pane ที่ติด `--auto-chain` พอ done ครบทุกตัวในโปรเจค →
  orchestrator inject handoff เข้า Lead → fire qa+reviewer อัตโนมัติ (one hop)
- **QA shard fan-out (v0.6.0)** — `--shards N` แตก QA หลาย pane รัน UI smoke คู่ขนาน
  แต่ละ shard แยก Chrome port + user-data-dir ไม่ชนกัน รวมผลเป็น handoff ก้อนเดียว
  timeout 45 นาที — feature ค่อนข้าง advanced ที่ตัว framework รองรับ multi-instance
- **Multi-project isolation** — 1 tab = 1 Lead = 1 project; pane รู้ project ตัวเอง
  ผ่าน env `TAKKUB_PROJECT` → ไม่ cross-talk ข้ามโปรเจค

### 2. Auto-routing intelligence (mature)

- `routing_planner.classify(msg, context)` คืน `RoutingAction` — keyword → primary role
  + cross-check, เลือก **propose-then-fire** vs **fire ตรง** ตาม ambiguity/irreversibility
- intent พิเศษ: `EXPLAIN_SYSTEM` (อธิบายระบบ → HTML explainer — เอกสารนี้เอง) /
  `GENERATE_GUIDE_HTML` (คู่มือ → HTML) แยกจากงาน dev ปกติ
- เป็น **authoritative testable spec** — prompt drift แพ้ code เสมอ (มี test suite คุม)

### 3. Provider abstraction + substitution (solid)

- รองรับ 3 backend: `claude` / `codex` (OpenAI) / `gemini` (Google) — รันคนละ binary
- **Substitution อัตโนมัติ** — codex/gemini ปิด toggle หรือไม่ได้ติดตั้ง →
  `effective_provider_for` degrade เป็น claude, spawn pane ชื่อ role เดิมแต่รัน
  `claude.exe` ("Claude รับตำแหน่งแทน") ตำแหน่งไม่ตกหล่น (แต่เสีย model diversity)

### 4. Lifecycle robustness (เพิ่ง hardened — solid)

- **`PaneState` dataclass** — ยุบ ~14 per-pane dict เป็นก้อนเดียว, teardown = `pop` ครั้ง
  เดียว → ฆ่า bug class state-divergence/leak ที่ตามมาจากลืม clear dict
- **กัน freeze/zombie:** single-instance QLockFile + dead-man watchdog (1s heartbeat,
  `os._exit` ถ้า main thread ค้าง >30s) + render coalescing (buffer bytesIn ~16ms กัน
  pane พ่น output ทำ main thread ตาย) + throughput watchdog จับ runaway pane
- **Stuck-recover** — snapshot/restore uuid+task+auto-chain+commit-gate ข้าม close→respawn

### 5. Continuity / resume (partial)

- snapshot state + resume briefs + post-compact brief + daily digest + pending CC/done
  delivery (รอด Lead restart/compact)
- mirror `takkub done` notes เข้า Obsidian vault
- *ยังไม่ใช่* full event-replay / checkpoint-restore ระดับลึก — เป็น best-effort resume

### 6. UX / self-management (solid)

- **Pipeline Settings UI** (ภาพข้างบน) — drag-drop hop builder + custom templates +
  provider/role toggles, persist ที่ `~/.takkub/pipelines.json` (self-heal)
- **AI-generated project rules** — สร้าง `<project>/CLAUDE.md` ผ่าน headless claude
- **2 updaters แยกกัน** — update ตัว cockpit เอง (git pull) + update Claude Code CLI
  (npm, มี compatibility analysis + auto-file issue ถ้า breaking)
- **`takkub release`** — bump version + roll CHANGELOG + tag + push + GitHub Release จบ
  ในคำสั่งเดียว (เอกสารนี้สร้างหลัง release v0.6.0 ด้วย flow นี้)

### 7. Observability + testing (strong)

- `runtime/events.log` (JSONL audit) · `token_meter` (per-pane context occupancy) ·
  `logs_panel` · `doctor` (health check) · `skill_audit` (TF-IDF role overlap)
- **~1,490 tests** (90 ไฟล์), CI windows-latest (ruff + pytest), routing เป็น
  authoritative spec; งานใหญ่ผ่าน cross-check 2 โมเดล (gemini + codex) ก่อน ship

## เพดาน / ข้อจำกัด (ตรงไปตรงมา)

- **Single-machine · single-user** — 1 Qt process ผูกเครื่องเดียว ไม่มี multi-user /
  remote backend; นี่คือเพดานใหญ่สุด *impact: high*
- **พึ่ง Claude Code CLI + Max OAuth** — ไม่ใช่ engine อิสระ ถ้า CLI / quota /
  login มีปัญหา = สะดุดทั้งระบบ (single point of dependency) *impact: high*
- **Lead = LLM ตัดสินใจ** — routing ดีขึ้นมากแต่ยังพลาดได้ จึงต้องมี propose-then-fire
  คั่นงาน irreversible; ไม่ใช่ deterministic agent ที่ self-correct ทุกเคส *impact: med*
- **ไม่มี durable replay/checkpoint ลึก** — crash-resume มีบางส่วน ไม่ใช่ full
  state-machine recovery *impact: med*
- **ผูกกับ terminal-pane** (WinPTY + xterm.js) — สื่อสารผ่าน terminal I/O + CLI JSON
  ไม่ใช่ structured agent protocol → งานบางอย่างต้อง parse หน้าจอ *impact: med*
- **Windows-first** — `mac-port` ยังเป็น branch ยังไม่ merge *impact: low*

## เทียบกับ autonomous agents อื่น (คร่าวๆ)

| มิติ | agent-takkub | Devin / autonomous SWE | OpenHands | Claude Code (เดี่ยว) |
|---|---|---|---|---|
| Multi-agent หลาย role พร้อมกัน | ✅ จุดแข็ง | ส่วนใหญ่ single-agent | บางส่วน | ✗ เดี่ยว |
| Human-in-the-loop (propose→confirm) | ✅ core design | น้อย (autonomous) | กลางๆ | ✅ |
| Autonomy เต็มรูป (วางแผน→ลงมือเอง) | กลางๆ (Lead ขับ) | ✅ จุดขาย | ✅ | ✗ |
| Visual cockpit เห็น pane ทำงานสด | ✅ เด่นมาก | dashboard | web UI | ✗ terminal |
| Multi-user / cloud | ✗ เพดาน | ✅ | ✅ self-host | ✗ |
| ผูก vendor เดียว | claude-first | proprietary | provider-agnostic | claude |

**อ่านตาราง:** agent-takkub ไม่ได้แข่งในสนาม "autonomous เต็มตัว ทำเองหมด" — มันเลือกอยู่
สนาม **"human-orchestrated multi-agent cockpit"** ที่ user มองเห็น/แทรกแก้ได้ทุกจังหวะ
ซึ่งเป็นจุดที่มันทำได้ดีกว่าเครื่องมือ autonomous ที่เป็นกล่องดำ

## ระดับสรุป

> **เก่งระดับ: เครื่องมือ orchestrate dev-team ที่ production-ready สำหรับ solo-dev**
> — มี multi-agent dispatch, auto-routing แบบ testable, parallel verify, provider
> substitution, self-healing lifecycle (lock/watchdog/coalescing) ครบ และ self-manage
> ตัวเองได้ (release/update/rules อัตโนมัติ) **เพดานถัดไป** ถ้าจะดันต่อ = แตกออกจาก
> single-machine (remote/multi-user backend) + ลดการผูกกับ Claude Code CLI ตัวเดียว
