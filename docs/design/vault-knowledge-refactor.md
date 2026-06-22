# Design: Vault Knowledge Refactor (3-tier)

**Status:** approved (autonomous run 2026-06-22) · **Owner:** Lead → backend + gemini
**Goal:** vault ปัจจุบัน = log archive ปลอมตัวเป็น second brain (2,232 notes, 53 link target, 86% ชี้ project hub 10 อัน, 0.86 link/note → hub-and-spoke graph ไร้ประโยชน์). แยก **log** ออกจาก **knowledge** ให้ graph มีความหมาย + เก็บเฉพาะของจำเป็น + ใช้ได้จริง

> **หลักคิดเดียว:** `takkub done` = **log** ไม่ใช่ **knowledge** → หยุดทำให้ทุก event เป็น permanent graph node ที่มี backlink ปลอม

---

## สถานะปัจจุบัน (อิงโค้ดจริง 2026-06-22)

| ตัวเขียน | เขียน → ที่ไหน | ปัญหา |
|---|---|---|
| `done()` `orchestrator.py:1160` (+ lead session end :1238) | ทุก `takkub done` → `01-Projects/<p>/sessions/<ts>-<role>.md` + `_render_decision_note` ฝัง `[[01-Projects/<p>]]` | สร้าง ~1,900 stub + **backlink ปลอม** (= ต้นตอ hub-and-spoke) |
| `write_resume_briefs()` `orchestrator.py:1714` | ปิด cockpit → `07-AI-Command-Center/briefs/<p>-<ts>.md` (transcript tail 24h) | 233 brief — **มีค่า (resume)** แต่อยู่ใน graph + accumulate ไม่ prune |
| `write_daily_digest()` `orchestrator.py:1764` | → `05-Daily/<date>.md` | ปกติ |
| `_render_decision_note()` `vault_mirror.py:128` | body กลาง (local + vault) ฝัง `[[01-Projects/<p>]]` ที่บรรทัด 158 | จุดแก้หลัก |
| junk filter `vault_mirror.py:89` | drop "ok"/<15char/test project | มีแล้ว — แต่ backlink ปลอมยังอยู่ |

**ของดีที่ต่อยอด:** `memory/` (MEMORY.md + per-fact + `[[link]]` จริง) = โมเดลถูก · `vault_graph.py` (#49 wiki-lint) · `extract_decisions()` `chatlog_scanner.py` · `_render_daily_digest()` `orchestrator_text.py:400`

vault ใช้ PARA-ish numbering อยู่แล้ว: `01-Projects` `02-Areas` `03-Resources` `04-Archive` → ออกแบบให้เข้ากับของเดิม

---

## 3-tier model

| Tier | เนื้อหา | ที่อยู่ | graph? | retention |
|---|---|---|---|---|
| 🟢 **Knowledge** | bug post-mortem, decision + **เหตุผล**, reusable pattern, curated fact | `01-Projects/<p>.md` (project page), `02-Areas/` (MOC ข้าม project) | **ใช่** (ลิงก์จริง) | ถาวร |
| 🟡 **Log** | resume brief, daily digest | `99-Logs/briefs/`, `05-Daily/` | **ไม่** (filter ออก) | brief 30 วัน |
| 🔴 **Ephemeral** | per-`done` session stub | `99-Logs/sessions/<p>/` | **ไม่** | 14 วัน (เก็บ last 5/project) |

`99-Logs/` = tier นอก PARA (เรียงท้ายสุด, ชัดว่าเป็น log) ตั้ง Obsidian graph filter `-path:99-Logs` → graph เหลือเฉพาะ knowledge

---

## Phase A — Storage refactor (backend, แก้ก่อน ผลชัดสุด)

**ไฟล์:** `vault_mirror.py`, `orchestrator.py` (writers), + tests

1. **ย้าย session log → `99-Logs/sessions/<project>/`** (เดิม `01-Projects/<p>/sessions/`) ทั้ง `done()` :1182 และ lead session :1268
2. **เลิกฝัง backlink ปลอมบน log** — `_render_decision_note` :158 + lead body :1238: ลบบรรทัด `**Project:** [[01-Projects/<p>]]` ออกจาก **log** (log อยู่ใน folder ตาม project พอ ไม่ต้อง graph edge). frontmatter `project:` ยังอยู่ (Dataview ใช้ได้)
3. **ย้าย briefs → `99-Logs/briefs/`** (`write_resume_briefs` :1735) — resume value คงไว้ แค่ออกนอก graph
4. **Retention/prune** (ฟังก์ชันใหม่ เรียกตอน cockpit close หรือ idle tick): session log > 14 วัน ลบ (เก็บ last 5/project), brief > 30 วัน ลบ. best-effort swallow OSError
5. **Strengthen junk filter** `vault_mirror.py:89` — เพิ่ม dedup near-identical note (เช่น hash 1st line) + ยกเกณฑ์ถ้าเหมาะ
6. **Obsidian graph filter** — เขียน `<vault>/.obsidian/graph.json` (ใน vault) ใส่ `"search": "-path:99-Logs -path:07-AI-Command-Center/Logs"` (หา key จริงของ Obsidian graph config ก่อน — อาจชื่อ `colorGroups`/`search`)
7. tests: writer เขียนถูก folder, ไม่มี backlink ปลอมใน log, prune ลบถูกตัว, junk dedup

> **Migration:** เขียน one-shot ย้าย `01-Projects/*/sessions/` ที่มีอยู่ → `99-Logs/sessions/` (หรือลบถ้าเก่าเกิน retention) — รันครั้งเดียว เก็บเป็น script `scripts/migrate_vault_logs.py`

## Phase B — Distill layer (backend, ต่อจาก A)

**เป้า:** แทน dump ดิบ → สกัด durable fact เป็น linked knowledge

1. ตอน **Finish Job / session end**: ใช้ `extract_decisions()` (มีอยู่) ดึง decision → กรองเฉพาะ "durable" (decision + เหตุผล, bug→root cause, reusable pattern) ทิ้ง noise (command, status)
2. เขียน/append เข้า **curated note** ต่อ project: `01-Projects/<project>.md` section `## Decisions & Learnings` (มี `[[link]]` ไป MOC ที่เกี่ยว) — **ไม่ใช่ stub แยกไฟล์**
3. **MOC scaffolding** ใน `02-Areas/`: สร้าง/อัปเดต hub ตาม theme ข้าม project (เช่น `<vault>/02-Areas/bug-patterns.md`, `<vault>/02-Areas/architecture-decisions.md`) ลิงก์ post-mortem/decision จริงเข้าไป
4. distill เป็น **opt-in/best-effort** — พังไม่กระทบ done flow (try/except, log event `distill_error`)
5. tests: extract durable เท่านั้น, append idempotent, MOC link ถูก

## Phase C — One-time extraction (gemini 1M context, ขนานกับ A/B — read-only)

อ่าน `99-Logs/briefs/agent-takkub-*` + `04-Archive/agent-takkub/bugs/*` + existing curated → สกัด decision/bug-pattern/architecture ของ agent-takkub → เขียน **linked knowledge notes ชุดแรก** (`<vault>/01-Projects/agent-takkub.md` enrichment + `02-Areas/*` MOC) เป็นตัวอย่าง "knowledge layer จริง" ให้ Phase B เลียนแบบ format. **เขียนเฉพาะ knowledge tier ห้ามแตะ log/code**

## Phase D — Obsidian config + usage guide (Lead)

1. ยืนยัน graph filter ทำงาน (เปิด vault เช็ค)
2. MOC index page `<vault>/02-Areas/_index.md` (Map of Contents กลาง)
3. usage guide → HTML (`docs/guides/<date>-vault-second-brain.md` → converter) วิธีใช้ 3-tier + resume + cross-project nav

---

## Decisions (locked, autonomous)
- retention: session 14d (keep last 5/project), brief 30d
- log tier folder: `99-Logs/` (นอก PARA)
- MOC tier: `02-Areas/` (PARA Areas = cross-cutting themes)
- distill = best-effort, ไม่ block done
- RAG/embeddings = **out of scope** รอบนี้ (optional later ถ้า query บ่อย)
- ห้ามแตะ: pane lifecycle, spawn, routing, idle/stuck watchdog (เพิ่งแก้ #62/#64)
