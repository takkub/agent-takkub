# Open issues fix plan — 2026-08-13

5 open issues ทั้งหมด (ดูด้วย `takkub issue list --open`). สรุป + แนวทางแก้ต่อตัว:

## #156 (med) — gemini/agy spawn เด้ง "Select an app to open 'mb'"
**สถานะ:** fix ลงแล้ว (`wt/backend-1786586053`, commit `4dc974a`) — root cause: npm ติดตั้ง `mb` (mini-browser shim) แบบ extensionless คู่กับ `mb.cmd`, Win32 SearchPathW เจอตัว extensionless ก่อนแล้วไม่มี associated app เลยเด้ง dialog. Fix = rename shim + จัดลำดับ PATH.
**ค้าง:** QA รายงาน pass แต่ auto-flag "no evidence cited" — ต้องยืนยันซ้ำพร้อมหลักฐานจริงก่อนถือว่าจบ

## #160 (high) — shard fan-out เขียนไฟล์รายงานทับกัน
เกิดจาก `qa --plan --shards 3` ทุก shard ถูกสั่ง path เดียวกัน → shard ที่เขียนก่อนหายหมด (เสีย 1 ใน 3 ของรายงาน QA เชิงลึก)
**แนวทาง:** (1) fan-out inject shard index + แนะนำ/บังคับ path แยกต่อ shard (`<report>.shard{N}.md`) + step consolidate ท้ายสุด (2) หรือ append+lock แทน overwrite (3) เตือน Lead ตอน assign ถ้าหลาย shard ชี้ path เดียวกัน (4) เช็คว่าเนื้อหาที่ shard อ้างถึงจริงอยู่ในไฟล์ก่อนถือว่า done

## #159 (med) — screenshot เสีย (ไฟล์ว่าง/เล็กผิดปกติ) ไม่ถูกตรวจจับ
critic รายงาน evidence ครบทั้งที่ไฟล์นึงมีแค่ 5KB (เทียบกับพี่น้องชุดเดียวกัน 43-105KB) — เกือบหลุดไปโชว์ user เป็นหลักฐานจริง
**แนวทาง:** cockpit เช็คขนาด/decode ได้ของไฟล์ภาพตอน role แนบ evidence ผ่าน `takkub done`, โชว์ขนาดไฟล์ใน digest ให้ Lead สังเกตเอง, เพิ่มขั้นตอน verify ใน qa/critic role prompt

## #158 (high) — auto-resume ยอมแพ้แล้วทิ้งงานค้างเงียบๆ
frontend ชน usage limit ซ้ำจน auto-resume ยอมแพ้ ข้อความบอกแค่ "หยุดแล้ว" ไม่บอกว่างานไปถึงไหน — เคสจริงงานเสร็จสมบูรณ์แล้วด้วยซ้ำ (ผ่าน test หมด) แต่ไม่มีใครรู้จนกว่า Lead จะไล่เช็คเอง เสี่ยง discard งานที่เสร็จแล้วทิ้ง
**แนวทาง:** (1) dump git status/task/pane tail ตอนยอมแพ้ (2) เตือน Lead ว่า "งานอาจเสร็จแล้ว verify ก่อน discard" (3) progress marker file กู้สถานะได้แม้ pane ตาย (4) **auto-failover ไป provider อื่นที่ quota แยก** — ข้อนี้ตรงกับ feature auto-switch provider ที่ user สั่งไปแล้ว (gemini กำลังออกแบบ) รวมเป็น input เดียวกัน

## #157 (high) — role prompt ไม่เตือนต้นทุนโทเคนของรูปภาพ
สั่ง frontend+critic อ่าน mockup PNG 1.7MB ตัวเดียวกันซ้ำหลายรอบ → ชน usage limit พร้อมกันยกทีม token พุ่ง 16% ในเทิร์นเดียว guardrail "ไฟล์ยักษ์ห้าม Read ทั้งก้อน" ที่มีอยู่ครอบแค่ text file ไม่ครอบรูปภาพทั้งที่แพงกว่าต่อไบต์
**แนวทาง:** (1) ขยาย guardrail ให้ครอบรูปภาพ (threshold ขนาด/ความละเอียด) (2) helper crop/downscale อัตโนมัติ (3) auto-thumbnail ตอน user แนบรูปจาก mobile พร้อม log ขนาด/ประมาณโทเคน (4) Lead prompt เตือนเรื่องหั่นรูปก่อนแจกหลาย agent อ่านพร้อมกัน

---

## Wave plan (กันเครื่องค้าง — ไม่ fire พร้อมกันทั้งหมด)

**อยู่ระหว่างทำ (ไม่นับใน wave ใหม่):** backend#2 (token-burn/cache-TTL feature, `wt/backend-1786586063`)

**Wave A (fire รอบนี้ถ้า confirm):**
- qa — #156 ยืนยันซ้ำพร้อมหลักฐาน
- backend — ลบ toggle 3 ตัว (Multi/1:1, rtk on/off, Auto-resume)
- gemini — ออกแบบ auto-failover (รวม #158 point 4 เป็น input)

**Wave B (หลัง Wave A เริ่มแล้ว ค่อย fire ตามเพื่อไม่ชน fanout cap):**
- backend — #160 shard fan-out path collision
- backend — #158 auto-resume giveup status dump + hint (ไม่รวม auto-failover — รอ design จาก gemini ก่อน)
- backend — #159 screenshot/evidence validation
- backend — #157 image token guardrail ใน role/lead prompt
