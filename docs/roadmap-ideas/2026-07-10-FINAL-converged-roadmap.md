# Takkub Cockpit — Idea Roadmap (Converged) · 2026-07-10

สังเคราะห์จาก 4 เอกสาร (2 รอบ research + 2 รอบ cross-critique โดย gemini/codex ข้ามกัน):
- `2026-07-10-ideas-gemini-provider-features.md` (12 idea — Claude/Codex/Gemini CLI features)
- `2026-07-10-ideas-codex-external-patterns.md` (12 idea — pattern จาก LangGraph/OpenHands/Cline/Devin/Claude SDK)
- `2026-07-10-critique-gemini-on-codex.md` (gemini วิจารณ์ 12 ของ codex + เสนอใหม่ 4)
- `2026-07-10-critique-codex-on-gemini.md` (codex วิจารณ์ 12 ของ gemini + เสนอใหม่ 5, verify ด้วย local CLI จริง)

**Loop เสร็จสมบูรณ์:** generate → cross-critique (ข้ามมุม ไม่ self-review) → cut ของไม่ดี → generate ใหม่แทน — ตามที่สั่ง

---

## 🥇 Tier 1 — ทำก่อน (effort ต่ำ-กลาง, value สูง, verified จริง)

| # | Idea | ที่มา | ทำไมอยู่ tier 1 |
|---|---|---|---|
| **A1** | **Provider capability-drift doctor** — `takkub doctor providers` probe ความสามารถจริงของ Claude/Codex/AGY เทียบกับ `provider_spec.py` (เจอแล้วว่า spec ล้าสมัยจริง — Codex/AGY รองรับ hooks/resume/slash-cmd แล้วแต่ spec บอกไม่รองรับ) | codex critique | ถูก verify แล้วว่ามีปัญหาจริง วันนี้ · effort ต่ำ · ป้องกัน idea อื่นถูกออกแบบบน assumption ผิด |
| **A2** | **Native resume/fork adapters** — ผูก session identity ต่อ provider จริง (Claude `--resume/--fork-session`, Codex `resume/fork`, AGY `--conversation/--continue`) แทน crash → replay ทั้ง task ใหม่ | codex critique | ปิด gap ใหญ่สุดตอนนี้ (2 provider resume ไม่ได้จริงทั้งที่ CLI รองรับแล้ว) |
| **#2 (re-scope)** | **Native hook adapters สำหรับ diagnostic context** — เพิ่ม failed-test/lint context เข้า turn ถัดไปผ่าน hook จริงของแต่ละ provider (Claude/Codex `additionalContext`, AGY `injectSteps`) — **ห้าม** simulate ผ่าน PTY stdin (ชนกับ draft ผู้ใช้ได้ ตรงกับปัญหา A3/#114 ที่เพิ่งแก้ไปเลย!) | gemini idea + codex re-scope | มูลค่าสูง เชื่อมกับ known pain point ของ cockpit เอง |
| **A4** | **Per-pane autonomy profile** (`fast/bypass` · `workspace-safe` · `ask`) แทน global skip-permissions เดียว — map ไปยัง flag จริงของแต่ละ provider | codex critique | ความปลอดภัย + UX ที่ solo-dev เลือกได้ต่อ pane |
| **New-1** | **Lightweight Git savepoint + `takkub rollback`** — สร้าง temp branch/stash อัตโนมัติก่อน assign ที่แก้ไฟล์ → ย้อนได้ 100% ด้วย git จริง (ไม่ต้องมี replay-engine ซับซ้อน) | gemini new-idea | ง่ายมาก ใช้ของที่มีอยู่แล้ว (git) ปลอดภัยกว่า idea replay-engine ที่ถูกตัด |
| **New-2** | **Interactive prompt interceptor** — PTY จับ regex ของ prompt แบบ `[y/N]` ที่ทำให้ pane ค้าง → ดึงมาโชว์ใน Lead ให้ตอบ/ตั้ง auto-answer | gemini new-idea | ตรงกับ pain point จริงที่ CLAUDE.md เตือนอยู่แล้ว (command ค้าง) |

## 🥈 Tier 2 — น่าทำ ต้องมีเงื่อนไข/prerequisite ก่อน

| # | Idea | เงื่อนไขก่อนทำ |
|---|---|---|
| A3 | Cross-provider `--add-dir` | ไม่มี prerequisite จริงๆ — ทำได้เลย effort ต่ำ |
| A5 | Structured one-shot exec channel (`-p --output-format stream-json` / `exec --json`) | เฉพาะงาน review/diagnostic ที่ bounded — ห้าม force AGY เข้า schema ปลอม |
| #9 (narrow) | Claude diagnostic safe-mode pane (แยก label ชัด ไม่ auto apply กับงานปกติ) | Claude-only เขียนให้ชัดว่าไม่ cover Codex/AGY |
| #4 (redesign) | Cockpit goal เก็บใน `runtime/` ผูกทุก assign — **ห้าม** เขียนลง CLAUDE.md/AGENTS.md/GEMINI.md (dirty repo) | ต้อง provider-specific injection ที่ session/turn boundary |
| #7 (after A2) | Native conversation-fork + worktree isolation คู่กัน | ต้องมี A2 (resume adapter) ก่อน ไม่งั้น fork แค่ไฟล์ไม่ fork บทสนทนา |
| #6 (diff-derived test) | Static analysis อาจพลาด dynamic import — ต้องมี fallback full-test เสมอ + redact secret ใน evidence | — |
| #8 (durable checkpoint resume) | ต้อง idempotency key + fingerprint workspace ก่อน resume | XL effort — ทำเมื่อ pain จริงเกิดบ่อย |
| New-3 | Self-distilling bug postmortem → md อัตโนมัติหลังแก้บั๊กยาก | เบา ทำได้เลย แต่ non-urgent |
| New-4 | AST codebase map cache ลด token scan | ตรวจว่า cache invalidation ไม่พังก่อน |

## ✂️ ตัดทิ้ง (cut ด้วยเหตุผลชัดเจน — อย่าเสียเวลาทำ)

| # | Idea | ทำไมตัด |
|---|---|---|
| #3 gemini | Multi-axis judge panel (3 reviewer พร้อมกัน) | over-scope สำหรับ solo-dev, token 3-10x, latency สูง |
| #5 gemini | Execution replay/fork engine เต็มรูป | side-effect ที่ rollback ไม่ได้จริงถ้าไม่มี Docker/VM — ใช้ New-1 (git savepoint) แทนพอ |
| #10 gemini | Typed pub/sub blackboard ข้าม pane | Lead-as-hub เดิมง่ายกว่า ตรวจสอบได้กว่า สำหรับ ≤4 panes |
| #12 gemini | Role/skill marketplace + sandbox/signature | enterprise-scale, local directory install พอแล้วสำหรับ solo-dev |
| #3 codex (script orchestrator) | ซ้ำกับ pipeline_executor ที่มีอยู่แล้ว (hop/auto-chain/shard) |
| #5 codex (`/cd` mid-session) | **claim ผิดจริง** — Claude รองรับ แต่ Codex/AGY ไม่มี equivalent จริง ใช้ A3 (`--add-dir`) แทน |
| #6 codex (`/doctor` prompt-optimizer) | **claim ผิดจริง** — `/doctor` คือ health-check ไม่ใช่ prompt optimizer |
| #8 codex (auto shell-fail explain) | agent อธิบาย fail เองอยู่แล้ว, regex ไม่เสถียรข้าม provider |
| #11 codex (`/rewind` = workspace recovery) | **claim ผิดจริง** — AGY `/rewind` = conversation rollback เท่านั้น ไม่ใช่ workspace |
| #12 codex (Codex TUI passthrough) | **ทำแล้วจริง** — `terminal_widget.py` forward key sequence อยู่แล้ว ไม่มี gap |

---

## 🎯 ลำดับแนะนำ (ถ้าจะเริ่มพรุ่งนี้)
1. **A1** capability-drift doctor (ถูก, เร็ว, ป้องกันงานอื่นออกแบบผิด)
2. **New-1** git savepoint + rollback (เร็วมาก, safety net ทันที)
3. **New-2** interactive-prompt interceptor (แก้ pain point ที่มีอยู่จริง)
4. **A2** resume/fork adapters (ใหญ่กว่าหน่อยแต่ปิด gap สำคัญสุด)
5. **A4** autonomy profile ต่อ pane
6. **#2 re-scope** native hook diagnostic context

## หมายเหตุความน่าเชื่อถือ
- codex verify ด้วย local CLI help จริง (`Claude Code 2.1.206`, `codex-cli 0.144.1`) + official docs link — จับ claim ผิดได้ 3 จุด (#5, #6, #11)
- gemini คิด replacement เน้น solo-dev practicality (ไม่ enterprise pattern)
- ทุก idea ผ่านอย่างน้อย 1 รอบ critique จากมุมอื่น (ไม่ self-review)
