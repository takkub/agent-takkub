# รายงานที่ต้องแชร์ให้ user (#367) — takkub report publish + build (#626)

## publish — แชร์ report ที่สร้างเสร็จแล้ว

เมื่อ user ต้องการลิงก์รายงาน/dashboard ที่แชร์ต่อได้ **แต่ไม่ต้องการให้ผู้รับเห็น URL claude** (เช่น `claude.ai/code/artifact/...`) → ใช้ `takkub report publish <file.html> [--name n] [--project p] [--expires 30d] [--label "..."]` แทน Claude Artifact (Lead-only mutation) รายละเอียด flag ทั้งหมด + `list`/`revoke`/`rotate` → `docs/lead/cli-reference.md`

**ข้อจำกัด (บอก user ทุกครั้งที่ publish):** ลิงก์เปิดจากนอกเครื่องได้เฉพาะตอน Remote เปิดอยู่จริง (Settings → Remote enabled + tunnel connect) — คำสั่งนี้ไม่เปิด Remote ให้อัตโนมัติ, publish/list จะพิมพ์บรรทัดสถานะ Remote ให้เสมอ (เปิด/ปิดอยู่)

**ลิงก์เดิมคงเดิม:** publish ด้วย `--name` เดิมซ้ำแล้วจะได้ URL เดิมเสมอ — ใช้ได้เมื่อ rebuild + republish ใหม่

---

# Report builder — `takkub report build` (#626)

ตัวออกรายงาน HTML **ส่วนกลาง ใช้ได้ทุก provider** (claude/codex/gemini-agy/opencode/kimi/cursor) ทั้ง Windows ConPTY และ macOS — ไม่ใช่ Claude skill ที่ provider อื่นมองไม่เห็น template/CSS/lightbox + mobile-check ออกมาจากชุดเดียวกัน (`assets/report/` → ship ใน wheel เป็น `_assets/report/`)

## Command

```bash
takkub report build --type customer|dev|boss \
  --content ./report-content \
  [--out report.html] \
  [--title "My Report Title"] \
  [--lint]        # customer เท่านั้น: เตือนคำต้องห้ามก่อน publish
```

### โครงสร้าง content dir

```
report-content/
  content.html          # เนื้อหาส่วน body (sections)
  images.txt            # mapping รูป: name|path
  template.html         # (optional) template ส่วนตัว — ชนะ template ที่ ship มา
```

### รูปภาพ
- รองรับ PNG/JPG/GIF/WebP → แปลงเป็น **JPEG q80 กว้างสุด 1440px** ฝังเป็น base64 data URI (25 รูป ≈ 2.4MB)
- `images.txt` รูปแบบ `name|path` หนึ่งบรรทัดต่อรูป (`#` = comment)
- **กันภาพซ้ำ/ภาพเก่า:** check MD5 — ถ้ารูปเดิมมาอ้างอิงซ้ำเป็นชื่อใหม่ จะเตือน warning ว่า duplicate hash (หลักฐานเก่ามาใช้ซ้ำจะจับได้)
- ภาพที่หายไป → build **ไม่ผ่าน** (error + รายชื่อรูป) กัน report เดินหน้าด้วยหลักฐานเปล่า
- ห้ามใส่ secret/token/URL ที่มี key ลงรายงาน
- ภาษาไทยเป็นค่าเริ่มต้น

### Mobile check (360/390/768 + dark/light)
- ต้องผ่านเช็กมือถือ: viewport 360 (เล็ก)/390 (ปกติ)/768 (แท็บเล็ต) → ไม่มี element ล้น viewport, ตารางไม่ต้องเลื่อนข้าง
- พร้อมใช้งานในโค้ดเป็น `report_builder.check_mobile(html)` (playwright; ถ้าเครื่องไม่มี playwright ข้ามไปก่อน ไม่ถือเป็น error) + สคริปต์อ้างอิง Node/Playwright `assets/report/mobile-check.cjs`, `tables-check.cjs`
- template ที่ ship มากันทะลุไว้แล้ว ≤900px → `grid-template-columns:1fr`, `minmax(0,1fr)`, `.shots` เป็น `minmax(min(260px,100%),1fr)`, `pre` → `white-space:pre-wrap`; ≤640px → ถอด `nowrap` จาก `td:first-child`/`.status`, `overflow-wrap:break-word` (**ห้าม `anywhere`** — ตัดคำไทยกลางคำ)

---

## กฎเนื้อหา 3 ประเภท

### 1. customer — คู่มือ/ประกาศให้ลูกค้า
- เขียนแบบ user manual: ฟีเจอร์ทำอะไร, ขั้นตอนกดทีละข้อ, ภาพหน้าจอฝั่ง user, tips, ตัวอย่างผลลัพธ์
- **ห้ามมี (lint เตือนก่อน publish นอกเหนือจากนี้ให้ตรวจเอง):** รายละเอียดเทคนิค/โค้ด/`commit`/`.ts`/`/api/`/`endpoint`/`error`/`บั๊ก`/`migration`/`qa-gate`/`database`/`response`, ตัวเลขการเงินภายใน, ชื่อ role ทีม dev/provider (`codex`/`claude`/`gemini`/`opencode`/`kimi`/`cursor`/`anthropic`)
- ตัวอย่างอ้างอิง: `guide/example-user-manual.html` (คู่มือระบบโบนัสและกิจกรรม)

### 2. dev — รายงานเทคนิค
- สิ่งที่แก้ (`path:line`, commit, migration, API/error code), สาเหตุ bug + วิธีแก้, ผลเทส/qa-gate พร้อมตัวเลข, หลักฐาน (ภาพ + response), behavior change, ความเสี่ยง/สิ่งที่ยังไม่ได้ทดสอบ, ขั้นตอน deploy
- ไม่มีคำต้องห้าม (ระบุ path/โค้ดได้) — แต่อย่าลงการเงิน/กลยุทธ์ภายใน/ชื่อคน
- ตัวอย่างอ้างอิง: `rbac-report/example-dev-report.html` (รายงาน RBAC + แรงค์ฝากถอน)

### 3. boss — รายงานหัวหน้า (กลางๆ)
- ทำอะไรไปบ้าง ภาษาธุรกิจ, สถานะแต่ละเรื่อง (เสร็จ/ทดสอบ/รอขึ้นระบบ), KPI สั้น, สิ่งที่กระทบผู้ใช้, ความเสี่ยง + สิ่งที่ต้องตัดสินใจ, ขั้นต่อไป
- เล่าบั๊กได้ระดับสรุป (จำนวน/แก้แล้ว) แต่**ไม่ลง** path/โค้ด/commit/error code/runtime ค่าลึก

### ภาษาโดยรวม
- ค่าเริ่มต้นภาษาไทย; ทุกประเภท: ไม่งด put secret/credentials; ก่อน publish ตรวจอีกครั้งว่าไม่มีข้อมูลในภาพด้วย (ภาพ screenshot ก็เป็นหลักฐาน — กันของเก่ามาอ้างใหม่ด้วย hash)

---

## Content blocks & special tags

```html
<!--CUSTOMER-ONLY-->…<!--/CUSTOMER-ONLY-->   <!-- เฉพาะ customer -->
<!--DEV-ONLY-->…<!--/DEV-ONLY-->             <!-- เฉพาะ dev -->
<!--BOSS-ONLY-->…<!--/BOSS-ONLY-->           <!-- เฉพาะ boss -->

<!-- รูปฝัง: maps กับ images.txt -->
<img src="{{img:my-screenshot-name}}" alt="…">

<!-- status badge -->
<span class="status ok">Pass</span> / <span class="status bad">Fail</span> / <span class="status skip">Skip</span>

<!-- KPI tiles -->
<div class="kpis"><div class="kpi"><b>30/33</b><span>tests passed</span></div></div>

<!-- code/response block -->
<pre class="ev">GET /api/user/123 …</pre>

<!-- role badges (customer) -->
<div class="role member"><h3>ลูกค้า</h3>…</div>
<div class="role admin"><h3>แอดมิน</h3>…</div>
```

ไฟล์อ้างอิงสำเนาถาวร (template, ตัวอย่าง 3 ประเภท, build.py, mobile-check) อยู่ที่ `~/.agent-takkub/runtime/exports/2026-09-15/saas_admin_amb/report-kit-reference/`