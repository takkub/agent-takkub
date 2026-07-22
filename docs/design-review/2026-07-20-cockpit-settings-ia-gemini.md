# Design Review: Takkub Cockpit Settings Information Architecture (IA)

## 1. Information Architecture (IA) Analysis & Mental Model

**ปัญหาในปัจจุบัน (Mental Model Mismatch):**
- **Settings ซ้ำซ้อนและแยกส่วน (Fragmented Settings):** การมีหน้าต่างการตั้งค่า 2 ที่ คือ New Team Settings (Native PyQt6) และ Legacy Pipeline Settings (Web/HTML) ทำให้เกิดความสับสนอย่างรุนแรง ผู้ใช้มี Mental Model ว่า "การตั้งค่าควรอยู่ที่เดียวกัน" แต่ความจริงกลับกระจัดกระจายและมี Concept ที่ทับซ้อนกัน
- **Provider & Model Settings:** ผู้ใช้ต้องการตั้งค่า Model ของ Provider แต่เมื่อเข้าไปที่ Legacy "Providers & Roles" กลับพบเพียงแค่ Toggle เปิด/ปิด และการผูก Role เข้ากับ Provider ทำให้ผู้ใช้สับสนว่า "ที่สั่งทำไปอยู่ไหน" เนื่องจากหน้าจอตั้งค่าแบบละเอียดถูกย้ายไปที่ New Team Settings -> Providers แล้ว
- **ความซ้ำซ้อนของ Concept:** 
  - **Roles:** มีอยู่ในทั้ง Legacy (หน้า Role Overlap, Providers & Roles) และ New Settings (จัดการ Role Properties)
  - **Skills:** ปรากฏใน Legacy (Skill Catalog, Skill Matrix) และ New Settings (Skills List)

**ทิศทาง IA ที่ควรจะเป็น (Target State):**
- **Single Settings Entry Point:** ควรมีหน้าต่าง Settings เดียวจากมุมมองของผู้ใช้ (Unified Navigation) แม้ด้านหลังจะเป็นคนละเทคโนโลยี (PyQt6 และ WebEngine) ก็ตาม
- **Centralized Concepts:** ของเรื่องเดียวกันต้องอยู่ที่เดียวกัน เช่น Provider Control ต้องไม่อยู่แยกกันระหว่าง Toggle เปิด/ปิด กับหน้าใส่ API Key/Model 
- **Grouping by Domain, not Technology:** จัดกลุ่มเมนู (Sidebar) ตามเนื้อหา เช่น `Pipeline`, `Team/Roles`, `System/Providers` ไม่ใช่แบ่งตาม `New UI` กับ `Legacy UI`

---

## 2. Nielsen Heuristics & Navigation Issues

1. **Consistency and standards (กฎข้อ 4):** การออกแบบหน้า Legacy ขัดแย้งกับหน้า New Settings อย่างสิ้นเชิง ทั้งในเรื่อง Navigation (List ด้านซ้าย vs Sidebar แบบหมวดหมู่) และเรื่อง Copywriting ล้าสมัย เช่น ใน Legacy ยังเขียนว่า "เปิด/ปิด provider (codex/gemini)" ทั้งที่มี 6 providers แล้ว
2. **Recognition rather than recall (กฎข้อ 6):** แทนที่ผู้ใช้จะเห็นเมนูทั้งหมดและ "รับรู้" (Recognize) ได้ทันที กลับต้อง "จำ" (Recall) ให้ได้ว่า ฟีเจอร์ที่ต้องการ (เช่น Skill Matrix) อยู่ใน Legacy หรือ New Settings
3. **Match between system and the real world (กฎข้อ 2):** การใช้คำว่า "Team" สำหรับจุดเข้าสู่หน้าตั้งค่าทั้งหมด อาจทำให้ผู้ใช้สับสน เพราะเรื่องของ "Providers", "MCP Servers" หรือ "Plugins" มักจะผูกกับคำว่า "Settings" มากกว่าคำว่า "Team" ในบริบทของซอฟต์แวร์ทั่วไป

---

## 3. Actionable Proposals (เรียงตาม Impact)

ข้อเสนอเหล่านี้เน้นไปที่เส้นทางที่สามารถทำแบบค่อยเป็นค่อยไป (Incremental) และคำนึงถึงข้อจำกัดทางเทคโนโลยี (PyQt6 คู่กับ QWebEngineView)

### 🔴 High Impact (แก้ Pain Point หลักทันที)

**Proposal 1: รวมศูนย์ Provider Settings (Centralize Providers)**
- **ปัญหา:** ผู้ใช้หาที่ตั้งค่า Model ไม่เจอ เพราะไปเข้าทาง Legacy แล้วเจอแค่สวิตช์เปิด/ปิด
- **หลักฐาน:** `05-legacy-settings.png` (มีแค่ Toggle และผูก Role) vs `01-team-providers-full.png` (หน้าต่างตั้งค่าจริง)
- **ข้อเสนอที่ทำได้จริง:**
  1. นำฟังก์ชัน "สวิตช์เปิด/ปิด Provider" ไปใส่ไว้ในหน้ารายละเอียดของ Provider ในฝั่ง **New Team Settings**
  2. ย้ายส่วน "การผูก Role เข้ากับ Provider (CLI)" ไปไว้ในแท็บใหม่ (เช่น "Model/Provider") ของเมนู **Roles** ใน New Settings
  3. **ลบเมนู "Providers & Roles" ออกจากหน้า Legacy ทิ้งถาวร** เพื่อตัดปัญหาความซ้ำซ้อนและ Copy ล้าสมัย
- **Effort:** Medium (ย้าย Logic มาฝั่ง PyQt6 ซึ่ง Widgets สำหรับฟอร์มมีพร้อมอยู่แล้ว)

**Proposal 2: รวม Navigation ไว้ที่หน้าต่างเดียว (Unified Settings Window)**
- **ปัญหา:** ต้องเปิด 2 หน้าต่างแยกกัน (กดปุ่มซ้ายล่าง Open legacy settings) ทำให้รู้สึกตัดขาด
- **หลักฐาน:** `01-team-providers-full.png` (ปุ่ม Open legacy settings อยู่มุมซ้ายล่าง)
- **ข้อเสนอที่ทำได้จริง:**
  - ใช้ **New Team Settings** เป็น Container หลักเพียงหน้าต่างเดียว
  - ใน Sidebar ซ้าย ให้เพิ่มเมนูของ Legacy ลงไปเลย (เช่น Pipeline Builder, Skill Matrix, Role Overlap)
  - เมื่อผู้ใช้คลิกเมนูที่เป็นของ Legacy ให้หน้าต่างฝั่งขวาสลับไปแสดงผล `QWebEngineView` แทนที่จะเป็น PyQt6 Widget วิธีนี้ผู้ใช้จะรู้สึกเหมือนทุกอย่างอยู่ในที่เดียวกันโดยไม่ต้องแก้โค้ดฝั่ง HTML ใหม่หมด
- **Effort:** Medium (เป็นการปรับเปลี่ยน Layout/StackedWidget ฝั่ง PyQt ให้รองรับ QWebEngineView คู่กับ Widget ปัจจุบัน)

### 🟡 Medium Impact (จัดระเบียบโครงสร้างระยะกลาง)

**Proposal 3: ชำระล้างความซ้ำซ้อนของ Roles และ Skills**
- **ปัญหา:** Concept ของ Role และ Skill กระจายอยู่ทั้ง 2 ระบบ
- **หลักฐาน:** `02-team-roles.png` (New) ทับซ้อนกับ `02-team-skill-catalog-legacy.png` (Legacy)
- **ข้อเสนอที่ทำได้จริง:**
  - กำหนดหน้าที่ให้ชัด: ให้ฝั่ง **New Settings (PyQt6)** รับผิดชอบเรื่อง **CRUD** (สร้าง/ลบ/แก้ไขรายละเอียดของ Role และ Skill)
  - ส่วนฝั่ง **Legacy (Web)** ให้รับผิดชอบแค่ **Analysis & Mapping** (Matrix, Overlap, Pipeline) เท่านั้น
  - ถอดเมนู "Skill Catalog" และฟอร์มแก้ไขข้อมูลต่างๆ ออกจากฝั่ง Legacy ป้องกันผู้ใช้สับสนว่าต้องไปแก้ที่ไหน
- **Effort:** Low-Medium (ซ่อน/ลบ UI ใน HTML และปรับ Routing ไม่ให้เข้าถึงได้)

### 🟢 Low Impact (Polish)

**Proposal 4: จัดกลุ่มเมนูใน Sidebar ให้สื่อความหมายมากขึ้น**
- **ปัญหา:** เมนูทุกอย่างเรียงติดกันหมด และอยู่ใต้กรอบชื่อ "Team" ทำให้ผู้ใช้คาดเดาไม่ได้ว่า Providers หรือ Plugins อยู่ที่นี่
- **หลักฐาน:** `01-team-providers-full.png` (เมนูฝั่งซ้ายเรียงติดกันโดยไม่มีหัวข้อแบ่งแยก)
- **ข้อเสนอที่ทำได้จริง:**
  - เพิ่ม Section Header ใน Sidebar ซ้าย (ซึ่งทำได้ง่ายใน QListWidget/QTreeView) เช่น:
    - **TEAM:** Roles, Skills, Users
    - **SYSTEM:** Providers, MCP Servers, Plugins
    - **PIPELINE:** Pipeline Builder, Skill Matrix, Role Overlap (หลังทำ Proposal 2)
  - เปลี่ยน Title ของหน้าต่างจาก "Team" เป็น "Settings" หรือ "Workspace Settings"
- **Effort:** Low (แก้ Text และเพิ่ม UI Item สำหรับ Section Header)
