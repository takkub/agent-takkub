# J&T Inbound Checker — คู่มือติดตั้ง + ใช้งาน + อัปเดต

repo: **https://github.com/takkub/jt-inbound-checker** · เวอร์ชันล่าสุด: **v4.4.4**
รองรับ: **Windows + Google Chrome** (auto-update ต้องใช้ Windows)

---

# ส่วนที่ 1 — ติดตั้งเครื่องใหม่ (ยังไม่เคยลง)

ใช้เวลา ~5 นาที ทำครั้งเดียว

## ขั้นที่ 1 — โหลดโค้ด
เปิด https://github.com/takkub/jt-inbound-checker → ปุ่มเขียว **Code → Download ZIP** → แตกไฟล์ไว้ที่ถาวร เช่น `C:\jt-inbound-checker`

> ⚠️ ต้องโหลดจาก **repo** (Download ZIP) — **ไม่ใช่** ไฟล์ในหน้า Releases เพราะ Releases zip ไม่มีโฟลเดอร์ `tools/` (จำเป็นสำหรับ auto-update)

## ขั้นที่ 2 — ติดตั้งลง Chrome
1. เปิด `chrome://extensions`
2. เปิด **Developer mode** (มุมขวาบน)
3. กด **Load unpacked** → เลือกโฟลเดอร์ที่มี `manifest.json` (เช่น `C:\jt-inbound-checker`)
4. ตรวจ: ชื่อ **J&T Inbound Checker**, ID = **`oiglldeidblbehpagcjkjjojjpocgonb`**

## ขั้นที่ 3 — ติดตั้ง auto-updater (ทำครั้งเดียว, แนะนำ)
1. ดับเบิลคลิก **`tools\install-updater.bat`** (จะขึ้น "Installed successfully!")
2. กลับไป `chrome://extensions` → กด **Reload (⟳)** 1 ครั้ง

> ถ้าไม่ทำขั้นนี้ ก็ใช้งานได้ปกติ แค่เวลาอัปเดตต้องโหลด zip เองผ่านปุ่ม "กดโหลด"

## ขั้นที่ 4 — ตรวจว่าใช้ได้
เปิดหน้า J&T (`https://home.jtexpress.co.th`) → เห็น **badge เวอร์ชัน** มุมขวาล่าง = พร้อมใช้ ✅

---

# ส่วนที่ 2 — วิธีใช้งาน

1. เปิดหน้า **J&T Shipping Inbound** (`home.jtexpress.co.th`) ก่อน แล้วกดไอคอน extension
2. ป้อนข้อมูล 2 วิธี:
   - **อัปโหลด PDF ใบปะหน้า** (มี QR/barcode) — กดปุ่ม "อัปโหลด PDF ใบปะหน้า"
   - **วางเลข waybill** ตรงๆ ในช่อง textarea
3. ตั้งค่า (ถ้าต้องการ): **หน่วงเวลา (ms)** ระหว่างยิงแต่ละใบ (default 1000), **Retry รอบ** จำนวนลองซ้ำเมื่อ fail (default 2)
4. กด **Start** → ระบบกรอก waybill เข้า J&T อัตโนมัติทีละใบ
5. ดูผลในตาราง: **Waybill / สถานะ / ข้อความ** (pass / fail / ซ้ำ)
6. แท็บ **History (ไม่ผ่าน)** = รายการที่ fail · ปุ่ม **Retry failed** = ลองยิงเฉพาะที่ fail ซ้ำ · **Stop** = หยุด

> รายละเอียดเต็มอยู่ใน `README.md` ของ repo

---

# ส่วนที่ 3 — วิธีอัปเดต (สำหรับผู้ใช้)

เมื่อมีเวอร์ชันใหม่:
1. เปิด popup (กดไอคอน) → จะเห็น banner 🔔 **"มี vX.X.X ใหม่"**
2. **กด "อัปเดตเดี๋ยวนี้"** → ระบบโหลด + ทับไฟล์ + reload ให้อัตโนมัติ → ขึ้น "อัปเดตสำเร็จ" → เสร็จ ✅
   - (ต้องลง `install-updater.bat` ในขั้นที่ 3 แล้ว)
3. หรือกด **"กดโหลด"** = โหลด zip มาแตกทับเอง + reload เอง (วิธีสำรอง ใช้ได้ทุก OS)

> หมายเหตุ: เช็คอัปเดต**ตอนเปิด popup เท่านั้น** (ไม่เช็คเบื้องหลัง) · banner อาจช้าได้ถึง ~5 นาทีหลัง release ใหม่ (GitHub CDN cache)

---

# ส่วนที่ 4 — วิธี release เวอร์ชันใหม่ (สำหรับ dev)

ทำที่โฟลเดอร์ repo:
```bash
# 1. แก้โค้ด/ฟีเจอร์ แล้ว bump เวอร์ชัน (ให้ manifest = version.json พอดี)
#    - manifest.json : "version": "X.Y.Z"
#    - version.json  : "version": "X.Y.Z" + notes

# 2. commit + push
git add . && git commit -m "..." && git push

# 3. สร้าง zip (ตัด .git + tools ออก) ชื่อคงที่ jt-inbound-checker.zip
#    (tools/ ไม่รวม เพื่อไม่ให้ auto-update ทับ host ที่กำลังรัน)

# 4. release พร้อมแนบ zip
gh release create vX.Y.Z jt-inbound-checker.zip --title vX.Y.Z --notes "..."
```
เครื่องที่ลง ≥ เวอร์ชันก่อนหน้า จะเห็น banner เด้งเอง

> ⚠️ ชื่อ asset ต้องเป็น **`jt-inbound-checker.zip`** เป๊ะ (ไม่ใส่เลขเวอร์ชัน) ไม่งั้น URL `releases/latest/download/jt-inbound-checker.zip` พัง

---

# แก้ปัญหาเบื้องต้น

| อาการ | วิธีแก้ |
|---|---|
| กด "อัปเดตเดี๋ยวนี้" → "ยังไม่ได้ติดตั้ง updater" | รัน `tools\install-updater.bat` (ขั้นที่ 3) · เช็ค ID = `oiglld...` (ถ้าไม่ใช่ = โหลดผิดโฟลเดอร์) |
| banner ไม่เด้งทั้งที่มีเวอร์ชันใหม่ | รอ ~5 นาที (GitHub cache) แล้วเปิด popup ใหม่ |
| extension เป็นสีเทา/หาย หลังรีสตาร์ท Chrome | `chrome://extensions` → Reload |
| ย้ายโฟลเดอร์ extension แล้ว auto-update พัง | รัน `tools\install-updater.bat` ใหม่ |
| auto-updater (Windows เท่านั้น) | เครื่อง mac/linux ใช้ปุ่ม "กดโหลด" แทน |

---

## สรุปสั้น
- **เครื่องใหม่:** Download ZIP → Load unpacked → ดับเบิลคลิก `tools\install-updater.bat` → Reload
- **อัปเดต:** เปิด popup → กด "อัปเดตเดี๋ยวนี้"
- **ใช้งาน:** เปิดหน้า J&T → กดไอคอน → อัปโหลด PDF/วางเลข → Start
