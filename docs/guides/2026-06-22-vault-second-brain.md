---
title: คู่มือ Vault Second Brain (3-tier) — agent-takkub
date: 2026-06-22
---

# 🧠 คู่มือ Vault Second Brain (3-tier)

หลัง vault-knowledge-refactor — vault แยกเป็น 3 ชั้นชัดเจน เพื่อให้ **graph มีความหมาย** + เก็บเฉพาะของจำเป็น + ใช้ได้จริง

## ปัญหาเดิม (ก่อนแก้)

vault มี 2,232 notes แต่ลิงก์ไปแค่ 53 target, 86% ชี้ project hub, เฉลี่ย **0.86 link/note** → graph เป็น **hub-and-spoke** (ดาวกระจาย) เพราะทุก `takkub done` ฝัง `[[project]]` backlink ปลอมตัวเดียว = log archive ปลอมตัวเป็น second brain

## โครงสร้างใหม่ — 3 tier

| Tier | คือ | อยู่ที่ | ใน graph? | เก็บนาน |
|---|---|---|---|---|
| 🟢 **Knowledge** | decision+เหตุผล, bug pattern, สิ่งที่เรียนรู้ | `02-Areas/` (MOC), `01-Projects/<p>.md` | **ใช่** (ลิงก์จริง) | ถาวร |
| 🟡 **Log** | resume brief, daily digest | `99-Logs/briefs/`, `05-Daily/` | ไม่ | brief 30 วัน |
| 🔴 **Session log** | บันทึกแต่ละ `takkub done` | `99-Logs/sessions/` | ไม่ | 14 วัน (เก็บ last 5/project) |

**หลักคิด:** `takkub done` = **log** ไม่ใช่ **knowledge** — log ไม่ฝัง backlink ปลอมอีก, ไปอยู่ `99-Logs/` (ซ่อนจาก graph), prune อัตโนมัติ

## สิ่งที่เปลี่ยนในระบบ

1. **Session log → `99-Logs/sessions/<project>/`** (เดิม `01-Projects/<p>/sessions/`) + **เลิกฝัง `[[project]]` backlink ปลอม** บน log
2. **Brief → `99-Logs/briefs/`** (resume value คงเดิม แค่ออกนอก graph)
3. **Auto-prune:** session log >14 วันลบ (เก็บ last 5/project), brief >30 วันลบ
4. **Distill layer:** ตอน session จบ → สกัด durable fact (decision/bug/pattern) → append `## Decisions & Learnings` ลง `01-Projects/<p>.md` พร้อมลิงก์ไป MOC (best-effort ไม่ block)
5. **Obsidian graph filter:** ซ่อน `99-Logs / sessions / briefs / 05-Daily` + ปิด orphans → graph เหลือเฉพาะ knowledge

## วิธีใช้งานจริง

### หา knowledge
เริ่มที่ [[../../second-brain/02-Areas/_index|02-Areas/_index.md]] (MOC กลาง) → ตามลิงก์ไป `architecture-decisions` / `bug-patterns` / project page

### หา "เคยทำอะไรไว้" (resume)
`99-Logs/briefs/<project>-<วันที่>.md` — transcript tail 20 exchange ล่าสุด (Lead ดึงอัตโนมัติตอน resume งานต่อ)

### graph view
ตอนนี้โชว์เฉพาะ knowledge ที่ลิงก์กันจริง — ไม่มีก้อน log รก ถ้ายังเห็นก้อนเก่า: รัน migration (ด้านล่าง) เพื่อย้าย log เก่าออกจริง

## One-time migration (รันเองตอนสะดวก)

log เก่า ~1,931 ไฟล์ยังอยู่ใน `01-Projects/*/sessions/` (graph ซ่อนให้แล้ว แต่ไฟล์ยังอยู่). ย้ายออกจริงด้วย:

```bash
# preview ก่อน (ไม่เขียนอะไร)
python scripts/migrate_vault_logs.py --dry-run
# รันจริง: move ~1,279 (< 14 วัน) → 99-Logs, delete ~652 (เกิน 14 วัน)
python scripts/migrate_vault_logs.py
```

> ⚠️ ลบ 652 ไฟล์ที่เกิน retention (knowledge สำคัญถูก distill/extract ออกมาแล้ว) — irreversible ตรวจ `--dry-run` ก่อน

## ของที่ extract ไว้แล้ว (ตัวอย่าง knowledge layer)

ตัวอย่างต่อไปนี้เป็น path ภายใน Obsidian vault ภายนอก repository:

```text
02-Areas/architecture-decisions.md  — decision ของ cockpit
02-Areas/bug-patterns.md            — bug pattern ที่ซ้ำ
01-Projects/agent-takkub.md         — enriched + Decisions & Learnings
```

(gemini extract จาก brief/post-mortem เดิม 17 links — เป็น format ให้ distill layer เลียนแบบต่อ)
