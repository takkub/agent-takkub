# Red-Team Review: PyQt6 Orchestrator Proposed Features (A, B, G)
**Date:** 2026-06-30  
**Author:** Antigravity (Gemini Specialised Agent)

วิเคราะห์และประเมินผลกระทบเชิงสถาปัตยกรรมของ 3 ฟีเจอร์ที่เสนอใน `agent-takkub` cockpit:

---

## 1. Feature A: Phone Push Notifications (via Remote MCP) & Away Auto-Summary
*ข้อเสนอ: ส่ง Push notification ไปมือถือผ่าน Claude Code Remote MCP เมื่อ pane done/blocked/hit-limit + รวบ done-handoff เป็น 1 สรุปเมื่อ user ไม่อยู่*

### จุดที่ "ทำแล้วไร้ค่า / อันตราย / ซ้ำของเดิม" (Critique):
1. **ผิด Layer และทำงานไม่ได้จริงตอน Pane ค้าง/หมดสิทธิ์ (Remote MCP Mismatch):**
   - **เมื่อ Pane Done:** Pane จะถูก Orchestrator ปิดการทำงานเกือบจะทันที (มี delay เพียง 2.5 วินาที อ้างอิงจาก `orchestrator.py` บรรทัด 1232) ดังนั้น Pane ไม่มีเวลาหรือสถานะพอที่จะทำงานเรียก Remote MCP ได้
   - **เมื่อ Pane Blocked หรือ Hit-Limit:** ตัว Pane หรือ LLM จะอยู่ในสถานะค้าง (frozen) หรือถูกจำกัดการทำคำสั่ง (rate-limited) ซึ่งหมายความว่าตัวมัน **ไม่สามารถประมวลผลหรือรัน MCP tool ใดๆ ได้อีก** การพยายามให้ Pane ยิง notification ผ่าน Remote MCP จึงเป็นความคิดที่พังแต่แรก
   - **ทางแก้ที่ถูกต้อง:** Orchestrator (PyQt6 Python process) คือผู้คุมสถานะและเป็นตัวเดียวที่เห็นว่า Pane ใด done/blocked/hit-limit (ผ่าน telemetry signals) ตัวมันควรเป็นผู้ส่ง Push notification โดยตรง ผ่านการยิง HTTPS Request สั้นๆ ไปยัง Notification gateway (เช่น ntfy.sh, Discord webhook, หรือ Telegram bot) ซึ่งเสถียรและเสียน้อยกว่าการรันโมเดลเรียก MCP tool
2. **Away Auto-Summary มีความเปราะบางและขัดกับระบบคิวที่มีอยู่:**
   - **ตรวจจับความเคลื่อนไหว (Away) ไม่เสถียร:** การวัด idle time ของ OS เพื่อระบุว่า user "ไม่อยู่" มักเจอปัญหา non-deterministic (เช่น แค่อ่านเอกสารอยู่นอกจอ)
   - **ระบบคิวในเครื่องมีอยู่แล้ว:** `lead_inbox.py` มีการทำ `_notify_lead` ซึ่งจะนำข้อความ Done ของเพื่อนร่วมทีมไปต่อคิวในหน่วยความจำ (`_lead_notify_queue`) และจะค่อยๆ ป้อน (pump) ให้ Lead ทีละอันเฉพาะเมื่อ Lead อยู่ที่ prompt สถานะพร้อมเท่านั้น (`is_at_ready_prompt`) หาก Lead ไม่รันอยู่ ข้อความจะถูกเก็บในรูปแบบ durable fallback (`_pending_done_notices`) บนดิสก์
   - **เสี่ยงขาดการควบคุม (Runaway Loop):** หากรวบสรุปแล้วให้ Lead คาดเดาและดำเนินการเองทั้งหมดระหว่างที่ผู้ใช้ไม่อยู่ อาจทำให้เกิดการทำงานค้างหรือหมดเครดิตโดยที่ผู้ใช้ไม่สามารถกดหยุดได้ทัน

---

## 2. Feature B: Stall Watchdog
*ข้อเสนอ: ตรวจจับ pane ค้าง > N นาที แล้วยิงเตือน (detect+notify only, ไม่แตะ pane)*

### จุดที่ "ทำแล้วไร้ค่า / อันตราย / ซ้ำของเดิม" (Critique):
1. **ซ้ำของเดิมที่มีประสิทธิภาพสูงกว่า (Duplicate & Downgrade):**
   - ใน `orchestrator.py` มีตัวตรวจจับและฟื้นฟูเพื่อนร่วมทีมค้างอยู่แล้วใน `_check_stuck_panes(now)` ซึ่งทำงานทุก 5 วินาที
   - ตัวตรวจจับเดิมมีความฉลาดสูงมาก:
     - ใช้ **Blake2b content hashing** ตรวจสอบการอัปเดตหน้าจอโดยไม่นำ volatile text (เวลา, token counters) และ spinner region มารวมในการตรวจสอบความเคลื่อนไหว (แก้ปัญหา Claude ค้างบน MCP แบบเงียบๆ)
     - มีระบบ **Active Auto-Recovery** ในตัว (`_auto_recover_stuck`) ที่จะสั่งปิดหน้าต่างแล้วเปิดใหม่พร้อมส่ง `--resume <uuid>` เพื่อดึง Claude กลับเข้าสู่ session เดิมโดยงานไม่หาย
   - ข้อเสนอที่บอกว่าจะเปลี่ยนเป็น "detect+notify only" ถือเป็นการ **Downgrade** ลดความสามารถการประมวลผลของระบบ Pipeline ที่อุตส่าห์กู้ชีพตัวเองได้โดยไม่ต้องรอคนมากด
2. **สร้างความน่ารำคาญ (Lead Pane Spam):**
   - ข้อกำหนดเดิมในระบบยกเว้น Lead Pane จากการตรวจสอบค้าง เพราะปกติ Lead มักจะรอรับคำสั่งจาก User (ซึ่งค้างได้เป็นชั่วโมงโดยไม่ใช่บั๊ก) หาก Stall watchdog ไม่ยกเว้น Lead Pane มันจะส่งสัญญาณเตือนไปมือถือผู้ใช้ซ้ำๆ ทุกครั้งที่ไม่มีการป้อนงาน

---

## 3. Feature G: Multi-Mode Preview
*ข้อเสนอ: ก่อน fan-out parallel โชว์ plan (K features จะ spawn กี่ pane เทียบ machine cap) ให้ user approve*

### จุดที่ "ทำแล้วไร้ค่า / อันตราย / ซ้ำของเดิม" (Critique):
1. **ขัดกับสถาปัตยกรรม Decoupled CLI/IPC (Architecture Mismatch):**
   - ในระบบขนาน (Parallel Mode) Lead จะสั่งรันคำสั่งพร้อมกันใน terminal ของมันเอง เช่น:
     `takkub assign --role frontend#1 ... & takkub assign --role backend#1 ... & wait`
   - คำสั่ง `takkub assign` ส่งผ่าน CLI IPC ทีละกระบวนการแยกขาดจากกัน (asynchronous calls) ไปยัง `cli_server.py`
   - **Orchestrator ไม่มีแผนรวมล่วงหน้าก่อนที่คำสั่งแรกจะวิ่งเข้ามา** ทำให้ไม่มีวิธีรวบรวมเพื่อแสดง popup preview ว่าจะมี pane ทั้งหมดกี่หน้าต่าง นอกจากจะใช้วิธีหน่วงเวลารับคำสั่ง (ซึ่งเสี่ยงต่อการผิดพลาด) หรือต้องบังคับให้ผู้ใช้ยืนยัน spawn ทีละหน้าต่าง (ซึ่งจะสร้าง popup ถี่ๆ รบกวนผู้ใช้มาก)
2. **โมเดลรู้อยู่แล้วผ่าน System Prompt (Context Control):**
   - Cockpit จะดึงค่าสูงสุดที่เครื่องรองรับได้ผ่าน `exec_mode.machine_fanout_cap()` (ประเมินจาก CPU Cores และ RAM ที่เหลือ) และ inject ลงไปในระบบของ Lead โดยตรง (`lead_context.md`):
     > *"Cap K <= {_cap} สำหรับเครื่องนี้ (คิดจาก CPU/RAM กันเครื่องล่ม) — ถ้า feature เกิน {_cap} แบ่งเป็น waves"*
   - Lead (Claude) จึงมีหน้าที่ประเมินและตัดสินใจตาม Cap นี้ก่อนการยิงรันคำสั่ง และต้องเขียนบอกผู้ใช้ใน Terminal chat เสมอ ผู้ใช้สามารถพิมพ์หยุดใน Terminal ได้โดยตรง ไม่จำเป็นต้องเพิ่มชั้น UI กั้นอีกชั้น
3. **พึ่งพา Dead Code:**
   - จากระบบตรวจสอบ (`docs/reviews/2026-06-01-system-gap-audit.md`) `routing_planner.classify()` **เป็น Dead Code ที่รันเฉพาะในการทดสอบ (Tests)** เท่านั้น ไม่เคยทำงานใน runtime จริง เนื่องจาก cockpit ยึดหลักให้โมเดลควบคุมการทำ route เองผ่าน CLAUDE.md ดังนั้น การเขียนโค้ดเพิ่มในฝั่ง `routing_planner` เพื่อแสดงพรีวิว UI จึงไม่มีประโยชน์

---

## 4. แนะนำฟีเจอร์ทางเลือก (Impact สูง, Effort สมเหตุสมผล) ที่ Lead มองข้าม

1. **Process Tree Reaper (มีประโยชน์สูงสุด):**
   - **ปัญหา:** Teammate pane รันพวก dev server/watcher (เช่น `next dev`, `tsc --watch`, Node.js compiler) และเมื่อ pane ถูกปิดค้าง หรือ auto-recover, โปรเซสย่อยพวกนี้จะกลายเป็นโปรเซสกำพร้า (orphan processes) ค้างอยู่ใน OS จากรายงานเคยเกิดเคส Node.js process รั่วสะสมถึง 3,170 กระบวนการ (กิน RAM 18GB)
   - **การแก้ปัญหา:** ทุกครั้งที่ปิด Pane หรือทำ Auto-recover ให้ไล่ฆ่าทั้ง Process Tree (`taskkill /T /F` บน Windows หรือ recursive kill บน Unix) ผ่าน `_pty_backend.py` เพื่อล้างหน่วยความจำให้สะอาด 100%
2. **Done Checklist Verification CLI:**
   - **ปัญหา:** Teammates มักลืมตรวจสอบ Git working directory หรือลืมบันทึกรายงานการวิเคราะห์ลง `docs/` ก่อนสั่ง `takkub done` พอกดเสร็จ pane ก็จะปิดทันทีใน 2.5 วินาที ทำให้การทำงานที่ผ่านมาสูญหายหรือค้างในสเตจ uncommitted
   - **การแก้ปัญหา:** เพิ่มกระบวนการตรวจสอบ เช่น `takkub done --verify` เพื่อเช็คว่ามีเอกสารวิเคราะห์ถูกเขียนใน `docs/` หรือยัง และแจ้งเตือนโมเดลให้เขียนบันทึกก่อนจะปิดตัวเองลงจริง
3. **Direct HTTP Webhook Notify (จาก Orchestrator):**
   - **ปัญหา:** การมี push notification มีประโยชน์จริงหากโมเดลค้างหรือหมดเครดิตการใช้งาน
   - **การแก้ปัญหา:** พัฒนาระบบส่ง webhook ตรงผ่าน HTTP request ใน Orchestrator ไปยัง ntfy.sh หรือ Discord เมื่อตรวจพบ pane error/stuck แทนการรันผ่านระบบ Remote MCP

---

## 5. การจัดอันดับลำดับความสำคัญ (Prioritization)

| ลำดับ | ฟีเจอร์ | สถานะที่แนะนำ | เหตุผล |
| :---: | :--- | :---: | :--- |
| **1** | **Process Tree Reaper** | **DO FIRST (ต้องทำด่วน)** | ป้องกันเครื่องอืด RAM รั่ว จากโปรเซสสะสมของ next dev ซึ่งขัดขวางการทำงานระยะยาวอย่างรุนแรง |
| **2** | **Direct Webhook Notification** | **DO SECOND (ทำรองลงมา)** | ตอบโจทย์ฟีเจอร์ A (Phone Notification) ในแบบที่ใช้งานได้จริง ไม่ล่มเมื่อโมเดลค้าง และมีโครงสร้างเรียบง่าย |
| **3** | **Done Checklist Verification CLI**| **DO THIRD (ทำภายหลัง)** | ปกป้อง Hard Rules ไม่ให้ teammates ปิด pane ทิ้งก่อนเขียน docs ลง repository |
| **4** | **Stall Watchdog (Feature B)** | **SKIP (ปัดตก)** | ซ้ำซ้อนกับ stuck-watchdog ที่ทำงานกู้ชีพตัวเองได้ดีอยู่แล้วในเครื่อง |
| **5** | **Away Auto-Summary (Feature A)** | **SKIP (ปัดตก)** | ระบบ queueing เดิมทำไว้ดีแล้ว การรวบสรุปเสี่ยงต่อการควบคุมโมเดลและเกิด runaway loops |
| **6** | **Multi-mode Preview (Feature G)** | **SKIP (ปัดตก)** | โครงสร้าง IPC แยกขาดเกินไปที่จะคำนวณล่วงหน้า และ Lead คุม Cap ตัวเองผ่าน context prompt ได้อยู่แล้ว |
