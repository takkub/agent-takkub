# 🎯 Red-Team Analysis: PyQt6 Cockpit Orchestrator Features (A / B / G)

รายงานการวิเคราะห์และตรวจสอบความสมเหตุสมผลเชิงสถาปัตยกรรม (Architecture & Feasibility Analysis) สำหรับ Feature ที่ Lead เสนอ โดยอ้างอิงจากโค้ดจริงใน `src/agent_takkub/`

---

## 🔍 1. สรุปความเชื่อมโยงกับ Architecture จริงของ Takkub

จากการอ่านโค้ดในระบบ มีข้อมูลทางสถาปัตยกรรมที่เกี่ยวข้องกับ Features ที่เสนอ ดังนี้:
1. **`routing_planner.py` (Propose-then-Fire):** มีโครงสร้างคัดแยก Intent ของผู้ใช้อยู่แล้ว หากตรวจพบงานที่จำเป็นต้องใช้ Agent หลายบทบาท (เช่น frontend + backend พร้อมกัน) จะจัดกลุ่มเข้าสู่โหมด `PROPOSE` เพื่อแสดง Plan Table และขอให้ผู้ใช้ยืนยันการทำงานผ่านคำสั่งตระกูล Confirm (`ok`, `ลุย`, `go`) 
2. **`exec_mode.py` & `lead_context.py` (Parallel Mode):** มีระบบคัดกรองขีดจำกัดเครื่องผ่าน `machine_fanout_cap()` (ตรวจจาก CPU Core และ Available RAM) และระบบจะทำการ Inject ค่าขีดจำกัดนี้เข้าไปใน Prompt ของ Lead เพื่อคุมไม่ให้สร้าง Pane ทำงานขนานเกินที่เครื่องผู้ใช้รับไหว
3. **`orchestrator.py` (Watchdogs & Auto-Healing):** มีกลไกตรวจจับ Pane ค้าง (`_check_stuck_panes`) ที่พัฒนาขึ้นมาอย่างเป็นระบบอยู่แล้ว โดยมีการกรอง spinner animation ผ่าน content-hash และมีระบบฟื้นฟูอัตโนมัติ (`_auto_recover_stuck`) ด้วยคำสั่ง `--resume <uuid>` เพื่อดึงสถานะแชทล่าสุดของ pane กลับมา รวมถึงตรวจจับ runaway output loop (`_warn_lead_runaway_pane`), TTY blocking (`_maybe_surface_tty_block`), และ Update splash (`_check_stuck_panes`)

---

## ⚡ 2. Red-Teaming ราย Feature (พัง/ไร้ประโยชน์/ซ้ำของเดิม?)

### 🔴 Feature A: Phone Push Notifications (Remote MCP) + "Away Auto-Summary"
> **คำอธิบายไอเดีย:** ส่ง Push เข้ามือถือเมื่อ pane done/blocked/hit-limit ผ่าน Claude Code Remote MCP และทำการรวบยอด done-handoff เป็น 1 สรุปตอน user ไม่อยู่โดยไม่ต้อง confirm ทีละ hop

*   **ไร้ประโยชน์ / พังได้ในทางปฏิบัติ (Complexity & Security Risk):**
    *   **Dependency Bloat & Security:** การบังคับให้ผู้ใช้ติดตั้งและเชื่อมต่อผ่าน **Remote MCP** บนเครื่อง Client (PyQt6 Desktop app) เพื่อทำ Push notification นั้นขี่ช้างจับตั๊กแตนอย่างมาก ระบบ MCP ของ Claude Code มีการโหลดและ spawn subprocess ที่ช้าและมีความซับซ้อนในการตั้งค่า หาก Network ของผู้ใช้ปิดกั้น (VPN/Firewall) หรือ Remote Server ขัดข้อง ฟีเจอร์นี้จะพังทันที
    *   **Bypassing Safety Gate:** กลไกหลักของ Takkub คือ "Human-in-the-loop" เพื่อควบคุมความปลอดภัยของโค้ด การที่ Lead รวบ done-handoff เป็น 1 สรุปตอน user ไม่อยู่ (ไม่ confirm ทีละ hop) หมายความว่าเราปล่อยให้ Agent ทำงานลุยต่อไปเรื่อยๆ โดยที่ไม่มีการตรวจสอบระหว่างทาง หาก Teammate ตัวแรกทำพัง เขียนบั๊ก หรือลบไฟล์สำคัญ Teammate ตัวถัดๆ ไปจะรันต่อบนความเสียหายนั้นจนเละเทะกว่าเดิมก่อนที่ผู้ใช้จะกลับมาอ่านสรุป
*   **ซ้ำของเดิม:**
    *   ใน `orchestrator.py` มีการส่ง Signal แจ้งเตือน Lead (`leadInjected.emit`) และมีเสียงเตือนหรือการเปลี่ยนสถานะใน UI อยู่แล้ว สำหรับการแจ้งเตือนนอกเครื่อง การรันผ่าน Python (Orchestrator) สามารถยิง Webhook ตรงๆ (Slack/Discord/Telegram) ได้ในโค้ดไม่กี่บรรทัด ปลอดภัยและเสถียรกว่า Remote MCP หลายเท่า

---

### 🔴 Feature B: Stall Watchdog (Detect Only + Notify)
> **คำอธิบายไอเดีย:** ตรวจจับ pane ค้าง > N นาที แล้ว push เตือน (Scope = Detect & Notify เท่านั้น ไม่แตะหรือซ่อม pane)

*   **ซ้ำของเดิม 100% (Redundant & Downgrade):**
    *   ใน `orchestrator.py` มีฟังก์ชัน `_check_stuck_panes` ตรวจจับ pane ค้างอยู่แล้ว ซึ่งมีความซับซ้อนสูงมาก (คำนวณ Content Hash ป้องกันการตรวจจับหลอกตาจาก Spinner, เช็ค TTY Prompt, ปลดล็อก Update Splash) 
    *   นอกจากจะตรวจจับได้แล้ว **ระบบเดิมยังช่วย Auto-Recover (ปิดและสปอว์นใหม่พร้อมส่ง Nudge ไปสั่งงานต่อ)** ให้อัตโนมัติอีกด้วย!
    *   การนำเสนอ Feature B ที่ทำแค่ "Notify Only ไม่แตะ pane" จึงเป็นการ **ลดทอนความสามารถ (Downgrade)** จากระบบ Auto-healing ที่มีอยู่แล้วให้กลับไปเป็นแบบแมนนวลที่เดือดร้อนผู้ใช้ต้องมากดเอง
*   **พังได้ในทางปฏิบัติ (False Positives):**
    *   หาก Watchdog ตั้งเวลาตรวจค้างแบบทื่อๆ (เช่น N นาที) โดยไม่เช็คว่า pane กำลังทำงานที่ต้องใช้เวลาสูงหรือไม่ (เช่น `npm install`, `docker build`, `vitest` รันเทสชุดใหญ่) มันจะส่งสัญญาณเตือนหลอก (False Positives) ไปที่มือถือผู้ใช้ตลอดเวลาจนกลายเป็นสแปม

---

### 🔴 Feature G: Multi-mode Preview
> **คำอธิบายไอเดีย:** โชว์ Plan ของ feature ขนานเทียบกับ Machine Cap เพื่อให้ user กดยืนยันการ spawn pane

*   **ซ้ำของเดิมในเชิงโครงสร้าง:**
    *   ระบบมีกลไก `PROPOSE` ของ `routing_planner.py` คอยรอให้ผู้ใช้กดยืนยันแผนการสร้าง Pane อยู่แล้ว และโหมด `PARALLEL` ก็จะคำนวณ `machine_fanout_cap()` ขีดจำกัดของเครื่องมาล็อกที่ฝั่ง Lead Prompt (`lead_context.md`) อยู่แล้วตั้งแต่ต้น
*   **พังได้ในทางปฏิบัติ (Inaccurate Capacity Estimation):**
    *   การประเมิน Machine Cap (Available CPU/RAM) ตอน Preview มักไม่สะท้อนความเป็นจริงขณะรันงานหนัก (Dynamic Load) เครื่องที่รัน Idle อาจมีแรมเหลือเฟือ แต่ทันทีที่สปอว์น Teammate Pane พร้อมรันคำสั่ง Build ขนานกัน 3-4 ตัว แรมจะพุ่งทะลักจนระบบ OS สั่ง Kill process หรือเครื่องค้าง การโชว์ Preview คาดเดา Cap จึงมีความเสี่ยงที่จะหลอกตาผู้ใช้

---

## 🚀 3. ฟีเจอร์ทดแทนที่แนะนำ (High Impact, Reasonable Effort)

เพื่อตอบโจทย์ "การเพิ่มความคล่องตัวตอนผู้ใช้ไม่อยู่หน้าจอ" และ "การจัดการโหมดขนานที่เสถียร" แนะนำให้ทำ 3 ฟีเจอร์นี้แทน:

1.  **Orchestrator-Level Queue Controller (สำหรับ Parallel Mode):**
    *   *แนวคิด:* แทนที่จะปล่อยให้ Lead ตัดสินใจจัด Wave เอง (ซึ่ง AI มักจะกะเวลารอกันพลาด หรือส่งงานต่อผิดพลาด) ให้ Orchestrator ควบคุมคิวรันใน Python เอง โดยจำกัดจำนวน Pane ที่รันขนานตาม `machine_fanout_cap()` ของจริง เช่น หากแผนมี 6 ตัว แต่ Cap รับได้ 3 ระบบจะต่อคิว (Queue) ตัวที่เหลือไว้ และสปอว์นตัวถัดไปขึ้นมาทันทีที่มีตัวก่อนหน้าทำงานเสร็จ (`done`)
2.  **Simple Configurable Webhook Notification (Slack / Discord / Telegram):**
    *   *แนวคิด:* แทนที่จะเชื่อม Remote MCP ให้ยุ่งยากและอันตราย ให้เพิ่มช่องใส่ `ALERTS_WEBHOOK_URL` ใน `projects.json` / Settings เมื่อพบ pane ค้างจนถึง Limit หรือรัน Pipeline จบ ให้ Orchestrator ยิง HTTP POST ไปแจ้งเตือนทันที ปลอดภัยและตั้งค่าง่ายกว่า
3.  **Process-Tree Subprocess Watchdog Integration:**
    *   *เหตุผล:* ปรับปรุง stuck watchdog เดิมให้อ่าน Process Tree ในระบบปฏิบัติการร่วมด้วย เพื่อดูว่ายังมี Subprocess ภายใต้ Pane (เช่น node, npm, tsc) กำลังใช้ CPU ประมวลผลอยู่หรือไม่ หาก CPU load ของ subprocess ยังทำงานอยู่ แสดงว่าไม่ได้ค้างจริง ห้าม Auto-Recover เพื่อป้องกันการทำงานขาดตอน

---

## 📊 4. ลำดับความสำคัญและสรุป Actionable Plan

| Feature/ไอเดีย | ลำดับความสำคัญ | Effort | Impact | สถานะข้อเสนอ |
| :--- | :--- | :--- | :--- | :--- |
| **1. Queue-based Wave Executor (ปรับปรุง G)** | **High (อันดับ 1)** | Medium | High | **ควรทำทันที** (ทำให้โหมดขนานรันได้เสถียรจริงโดยผู้ใช้ไม่ต้องคอยนั่งกด) |
| **2. Simple Webhook Alerts (ปรับปรุง A)** | **Medium (อันดับ 2)** | Low | High | **ควรทำ** (แจ้งเตือนเข้ามือถือปลอดภัยและง่ายกว่า Remote MCP) |
| **3. Process-Tree Stuck Watchdog (ปรับปรุง B)** | **Low (อันดับ 3)** | Medium | Medium | **ควรพิจารณาทีหลัง** (ลด False Positives ของ stuck detector เดิม) |
| **Remote MCP Integration (A)** | - | High | Low | **ข้าม (Drop)** (สุ่มเสี่ยงความปลอดภัย/เสถียรภาพต่ำ) |
| **Stall watchdog แบบ Notify only (B)** | - | Low | Low | **ข้าม (Drop)** (ซ้ำซ้อนกับตัว auto-recover เดิมและทำลาย automation) |
| **Multi-mode Preview UI แยก (G)** | - | High | Low | **ข้าม (Drop)** (ซ้ำซ้อนกับระบบแชท Propose-then-fire และประเมินพิกัดเครื่องคลาดเคลื่อน) |
