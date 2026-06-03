---
title: Design Review: QA shard fan-out
date: 2026-06-03
reviewer: gemini
---

# Design Review: QA shard fan-out (Parallel UI Smoke)

Review ของแผนการขนานงาน QA smoke test โดยการ spawn หลาย pane (shards) เพื่อลด wall-clock time.

## Q1: `qa#n` key vs separate `shard` field?
**Recommendation: ใช้ `qa#n` เป็น Pane Key (Registry Key) ใน Orchestrator**

- **เหตุผล:** การเปลี่ยน registry เป็น `dict[str, AgentPane]` ให้รองรับ key ที่ไม่ใช่ role name โดยตรง (แต่ map กลับไปหา role ได้) เป็นการแตะโค้ดที่ "leaf node" มากที่สุด Existing logic ใน `orchestrator.py` เช่น `_ps()`, `_idle_state`, และ `unregister_pane` ทำงานบน string key อยู่แล้ว ถ้าเปลี่ยนเป็น field แยก จะต้อง refactor dictionary structure ทั่วทั้ง class
- **Risk:** ต้องระวังจุดที่ใช้ `pane.role.name` เพื่อหาไฟล์คอนฟิก (เช่น `qa.md`) ต้องเปลี่ยนไปใช้ `_split_shard(key)[0]` เสมอ
- **Trade-off:** ความสวยงามของ data model (field แยกดีกว่า) vs ความง่ายในการ implement (key suffix ง่ายกว่ามาก) → ในระบบที่มี legacy เยอะแบบนี้ **Key Suffix ชนะ**

## Q2: Planner-first (A) vs Self-select (B)?
**Recommendation: ทางเลือก A (Planner-first)**

- **เหตุผล:** UI Testing มีความไม่แน่นอนสูง (flaky) การมี "Single Source of Truth" ในรูปของ `qa-plan.md` ที่ระบุชัดเจนว่า Shard 1 ทำหน้า A, B และ Shard 2 ทำหน้า C, D จะช่วยให้:
    1. Lead ตรวจสอบได้ว่า "เก็บตกครบทุกหน้าไหม"
    2. ไม่เกิด Race condition ในการแย่งกันจองคิวงาน
    3. กรณี Shard ใด Shard หนึ่งตาย เราจะรู้ทันทีว่า "หน้าไหนที่ยังไม่ได้ test"
- **Trade-off:** เสียเวลาเพิ่ม 1 step (latency) สำหรับการรัน planner แต่คุ้มค่ากับความ Robust

## Q3: Chrome port collision ข้าม project?
**Recommendation: Project-based Port Offset + Dynamic Assignment**

- **เหตุผล:** แค่ `9222 + shard_idx` ไม่พอถ้า user เปิด 2 project พร้อมกันใน cockpit (Project A:qa#1 และ Project B:qa#1 จะชนกันที่ 9223)
- **Solution:** 
    1. ใช้ `9222 + (project_hash % 100) * 10 + shard_idx`
    2. หรือดีกว่า: ให้ `mb-start-chrome` รับ `--port 0` เพื่อหา ephemeral port แล้ว write port นั้นลงไฟล์ `.chrome_port` ใน working dir เพื่อให้ `mb` คำสั่งถัดๆ ไปอ่าน (Standard practice สำหรับ parallel testing)

## Q4: Shard crash behavior?
**Recommendation: Partial-pass + Gap Reporting**

- **เหตุผล:** UI smoke มักจะมี 1-2 หน้าที่พังบ่อย (flaky/bug) การที่ Shard 1 พังไม่ควรทำให้ผลงานของ Shard 2-3 ที่รันผ่านแล้วหายไป
- **Solution:** Orchestrator ควรรอจนครบ timeout หรือทุด shard ส่ง done/fail แล้วรวบรวมเป็น 1 handoff ที่สรุปว่า "Passed: 80%, Failed: 20% (Shard 1 crashed on /settings)" เพื่อให้ Lead ตัดสินใจว่าจะรันซ่อมเฉพาะจุดหรือเดินหน้าต่อ

## Q5: UI Grid clutter?
**Recommendation: "Logical Grouping" ใน UI (Phase 2)**

- **เหตุผล:** การมี 10 pane โผล่มาพร้อมกันทำลาย UX ของ cockpit
- **Solution:** 
    - **Short-term:** ยอมให้โผล่มา แต่ตั้งชื่อ pane ให้ชัด (เช่น `[QA#1]`, `[QA#2]`)
    - **Long-term:** พัฒนา "Grouped Pane View" ที่ยุบ shards ทั้งหมดไว้ในแถวเดียว และแสดง progress bar/status dots แทน

## Q6: Simpler approach? (Devil's Advocate)
**Question: Design นี้ over-engineer ไปไหม?**

- **Challenge:** ทำไมไม่รัน `playwright --workers 5` ใน `qa` pane เดียวจบ?
- **Analysis:**
    - **ข้อดีของแผนปัจจุบัน:** แยก Context ชัดเจน (Token meter ไม่ปนกัน, Idle watchdog ทำงานแยกราย shard, ลอจิกการคิดของ Claude ไม่ตีกันใน 1 stream)
    - **จุดที่อาจจะ Simple กว่า:** ถ้าเราไม่อยากแก้ Orchestrator เยอะ — เราสามารถใช้ `takkub assign` สร้าง role ใหม่ชั่วคราว เช่น `qa_worker_1`, `qa_worker_2` โดยชี้ symlink ไปที่ `qa.md` เดียวกัน วิธีนี้ไม่ต้องแก้ Registry key logic เลย แต่จะไปรกที่ `takkub list` แทน
- **Verdict:** แผนปัจจุบันที่เสนอ (แก้ `_split_shard` ใน orchestrator) เป็นทางที่ **สายกลางและยั่งยืนที่สุด** เพราะมันยอมรับว่า "Parallelism ใน takkub คือระดับ Pane" ซึ่งตรงกับสถาปัตยกรรมหลัก

## Summary Verdict: **Go with Changes**

แผนนี้แข็งแรงและแก้ปัญหาคอขวดได้จริง แนะนำให้ดำเนินการต่อโดยเพิ่ม:
1. **Dynamic Port Discovery** เพื่อแก้ Port collision (Q3)
2. **Partial Aggregate Logic** เพื่อไม่ให้งานทั้งหมดเสียเปล่าถ้ามี 1 shard ตาย (Q4)
3. **Consolidated Handoff** เพื่อให้ Lead ไม่โดน spam ด้วย N ข้อความ

---
*Reviewer: Gemini (as Third-Brain specialist)*
