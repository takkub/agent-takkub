# Audit Report: Second Opinion Review (2026-08-16)

จากการตรวจสอบ commits `#251`, `#252`, `#253`, `#254`, และ `#255` ตามโจทย์ที่ได้รับ พบประเด็นความเสี่ยง 4 จุดที่ทีมอาจมองข้าม เรียงตามระดับความรุนแรงดังนี้ครับ:

## 1. Security Regression: Lost Phone / Session Re-enable (#252)
**Severity: CRITICAL**
**Commit:** `1a1dd52`

**ปัญหา:**
การแก้ให้ `idle-expire` ไม่ persist `enabled=False` และให้ UI reuse `secret_path`/`token` เดิมเป็นค่า default นั้น ช่วยแก้ปัญหา remote ดับทุกเช้าได้จริง **แต่เปิดช่องโหว่กรณีมือถือหายครับ**
ถ้า user ทำมือถือหาย -> เข้า Settings มากด **Disable** ทันที (เพื่อตัดการเชื่อมต่อ) -> วันต่อมาซื้อมือถือใหม่ แล้วมากด **Enable** กลับ
เนื่องจาก checkbox "Generate a new pairing link" ถูกตั้งให้ *unchecked by default* ระบบจึงดึง `secret_path`/`token` ของเดิมมาใช้ใหม่ ผลคือ **มือถือเครื่องเก่าที่หายไป จะสามารถใช้ QR/Link เดิมเข้าควบคุมระบบได้ทันทีที่เปิด Enable อีกครั้ง** โดยที่ user อาจไม่รู้ตัวว่าต้องกด checkbox เพื่อล้าง link เก่า

**ผลกระทบ:** คนร้ายที่เก็บมือถือได้ (หรือคนที่ copy QR ไว้) จะกลับมามีสิทธิ์เข้าถึงระบบเมื่อ user เปิดใช้งาน remote control ใหม่

## 2. Race Condition: Auto-Cancel ชนกับ Repaste (#255 + #144)
**Severity: HIGH**
**Commit:** `fd8052b` (และโยงกับโค้ดใน `lead_inbox.py`)

**ปัญหา:**
ฟีเจอร์ auto-cancel ของ `#255` จะ set state ของ delivery เป็น `CANCELLED` ทันทีเมื่อ Lead สั่ง `takkub send`
กลไกของ `PtyWriter` จะคอยดัก drop payload ที่ `CANCELLED` แล้วออกก่อนที่จะเขียนลง PTY ได้ **ก็ต่อเมื่อ** มีการแนบ `validator` ไปตอนเรียก `write()` (เหมือนที่ `_deliver` ทำ)
แต่ใน `_delayed_enter_verified` (ซึ่งดูแลการ resend/repaste) มีการเรียก `_safe_session_write` ถึง 2 ครั้ง (จังหวะส่ง `\r` และจังหวะ repaste `payload`) **โดยที่ไม่ได้ส่ง `validator` พ่วงไปด้วย!**

**ผลกระทบ:**
ถ้า Lead พิมพ์แทรกเข้ามาระหว่างที่ `QTimer` ของ `_delayed_enter_verified` กำลังหน่วงเวลาอยู่ ระบบจะ cancel delivery ได้สำเร็จ แต่ `QTimer` ที่ตื่นขึ้นมาทีหลังจะสั่ง repaste งานลง PTY ไปดื้อๆ ทะลุการป้องก้นของ `PtyWriter` ทำให้หน้าต่าง composer พังจาก race condition นี้อยู่ดี

## 3. False Negative: `takkub wait` หูหนวกกับ blocking notice ของ role ตัวเอง (#253)
**Severity: HIGH**
**Commit:** `2bf5629`

**ปัญหา:**
ฟีเจอร์ interrupt-wake ถูกสร้างมาแก้ปัญหาที่ Lead หูหนวกเวลา role อื่นพัง แต่โค้ดใน `_pending_notice_outside` จงใจข้าม role ที่กำลัง watch อยู่ (`if role in watched_roles: continue`) เพราะคิดว่า role ตัวเองจะไปหลุด wait ผ่าน `done()` / `failed()` ตามปกติ
**แต่นั่นไม่จริงเสมอไปครับ!** blocking notice บางตัว เช่น `[delivery-unconfirmed]` (ส่งงานไม่สำเร็จเพราะจอค้าง) หรือ `[spawn-stuck]` (boot นานเกิน) จะถูกส่งเข้า inbox อย่างเดียว **โดยไม่ได้เรียก `done()` หรือ `failed()`** 

**ผลกระทบ:**
ถ้าสั่ง `takkub wait --role frontend` แล้ว `frontend` เกิด `[delivery-unconfirmed]`
`_pending_notice_outside` จะมองข้าม notice นี้ (เพราะ `frontend` อยู่ใน `watched_roles`) ส่วน `poll_wait` ก็จะเห็นว่า `frontend` ยัง active และไม่จบงาน ผลคือ `wait` จะบล็อกยาวไปจนครบ timeout 30 นาทีเต็ม โดยไม่สนใจ notice เตือนภัยจาก role ที่มันจ้องอยู่เลย

## 4. Cross-Platform / Edge Case: Untracked Directories Snapshot พลาดไฟล์ (#251)
**Severity: MEDIUM**
**Commit:** `a5e57d9`

**ปัญหา:**
การ snapshot เพื่อเปรียบเทียบ `mtime_ns` และ `size` พึ่งพา path จาก `git status --porcelain -z`
ในกรณีที่มี directory ใหม่ที่ยังไม่ได้ track (untracked directory) `git status` (ที่ไม่ได้ใส่ flag `-uall`) จะยุบรวมและพ่นออกมาแค่ชื่อ directory ชั้นนอกสุด (เช่น `?? new_folder/`) 
ปัญหาคือบน Windows และ macOS ถ้ามี agent สร้าง/แก้ไขไฟล์ **ลึกเข้าไปข้างใน** (`new_folder/deep/file.py`) ตัว `mtime` และ `size` ของโฟลเดอร์แม่ (`new_folder/`) จะ **ไม่เปลี่ยนแปลงเลย**

**ผลกระทบ:**
เมื่อ `mtime` ของ `new_folder/` ต้นทางและปลายทางเท่ากันเป๊ะ ฟังก์ชัน `changed_dirty_paths` จึงมองว่ามันไม่เปลี่ยน และ **คัดทิ้งทั้งหมด** ทำให้ไฟล์ที่ agent อุตส่าห์สร้างหรือแก้ลึกลงไปใน untracked directory หายไปจากรายงาน `ไฟล์ที่แตะ` (False Negative) อย่างสมบูรณ์

---
(Note: ได้ทำการตรวจสอบตามเงื่อนไข second opinion - ไม่มีการแก้โค้ดหรือ commit ใดๆ ครับ)
