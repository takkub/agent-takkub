# Takkub Brain V2 Plan — aligned with agent-takkub 1.0.68

**Baseline inspected:** `main@0aee262a2b2648b248822e3bb587a49001b14166`  
**Package version:** `1.0.68`  
**Date:** 2026-08-16

ชุดนี้แทนแผน V1 เดิมทั้งหมด เพราะ agent-takkub เปลี่ยน lifecycle หลายจุดในวันเดียวกัน

## สิ่งที่เปลี่ยนจากแผนเดิม

1. Brain context ต้องถูกประกอบตอน **assignment** ไม่ใช่ผูกกับ spawn อย่างเดียว
2. ต้องรองรับทั้ง `mode=pane` และ `mode=subagent`
3. ต้อง reuse `DigestFacts` ที่ cockpit วัดจาก git/orchestrator state
4. ห้าม hook Brain เข้ากับ Lead notification/digest เป็น source of truth
5. แยก **hot-path retrieval** ออกจาก deep BM25 search
6. ลด injected context ให้สอดคล้องกับ token-diet ของระบบ
7. หลีกเลี่ยงชื่อ `HandoffRecord` เพราะระบบมี file-based task handoff อยู่แล้ว
   - ใช้ชื่อ `ContinuationRecord` สำหรับ semantic resume state
8. autoresume park/wake ไม่ถือเป็น task completion หรือ handoff
9. shard/subagent findings ต้องไม่ถูก promote เป็น Project Brain แบบอัตโนมัติ
10. ทุก Brain path ต้องใช้ `RUNTIME_DIR` + project validation เดิม

## เป้าหมาย

หลังทำเสร็จ:

- agent ใหม่รู้ constraint/decision สำคัญของ project
- pane เดิมที่ถูก assign งานใหม่ได้ context ล่าสุด
- subagent capsule ได้ context เดียวกับ pane-mode assignment
- provider switch ต่อ task ได้จาก semantic continuation
- restart แล้ว memory ยังอยู่
- failure/lesson/outcome ถูกเก็บโดยไม่ซ้ำ
- old decision ถูก supersede
- memory ไม่กลายเป็น instruction
- memory ไม่ทำให้ Qt main thread หน่วง
- context ไม่บวมจนย้อนแย้งกับ token-diet

## เริ่มใช้งาน

ส่ง `MASTER-PROMPT.md` ให้ Lead

ก่อน coding Lead ต้องทำ Phase 0 และตรวจ HEAD ปัจจุบันอีกครั้ง
หาก HEAD ไม่ใช่ baseline นี้ ให้ current code เป็น source of truth
