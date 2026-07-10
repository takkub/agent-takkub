# Takkub Cockpit — Settings UI Design Review (Phase 1)

This document contains the design fidelity and UX review of the new Settings Window UI (Phase 1) implemented in [settings_window.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py) and [cockpit_theme.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py), compared against the canonical design specifications in [2026-07-10-cockpit-settings-design-system.md](file:///C:/Users/monch/WebstormProjects/agent-takkub/docs/design-review/2026-07-10-cockpit-settings-design-system.md).

---

## 🎨 Theme Tokens & Color Fidelity

> [!NOTE]
> All core grounds, borders, text, and role colors are highly accurate and correspond perfectly to the user's hex design tokens.

| Token Group | Design Hex | Theme Implementation | Fidelity Status | Notes / Evidence |
| :--- | :--- | :--- | :---: | :--- |
| **Grounds** | window `#15171c`, titlebar `#0f1114`, status-strip `#181b21` to `#141519`, sidebar `#101216`, panel `#181b21`, panel-alt `#191c22`, input `#1c1f26`, select `#232732` | Matches | ✅ **Perfect** | Defined in [cockpit_theme.py:L24-33](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L24-L33) and applied in QSS. |
| **Accent Gold** | solid `#E3B341`, gradient `linear-gradient(180deg,#EEC25A,#E3B341)`, text `#241a00`, soft-chip bg `rgba(227,179,65,0.12)`, border `rgba(227,179,65,0.35)`, text `#ECCB6A` | Matches | ✅ **Perfect** | Defined in [cockpit_theme.py:L49-55](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L49-L55) and applied in QSS. |
| **Text** | primary `#f2f3f5`/`#e9ebef`, secondary `#c7ccd4`/`#cfd3da`, muted `#7b828f`/`#828a95`, faint `#5b626e`/`#6b7280` | Matches | ✅ **Perfect** | Defined in [cockpit_theme.py:L60-67](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L60-L67) and applied in QSS. |
| **Role Colors** | exact mapping for 12 roles (lead, frontend, backend, mobile, devops, qa, reviewer, critic, designer, analyst, security, docs) | Matches | ✅ **Perfect** | Fully populated in `ROLE_COLORS` dictionary in [cockpit_theme.py:L82-95](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L82-L95). |

---

## 🔍 UI Fidelity & Components Review

This table lists specific mismatches, heuristic issues, and recommendations identified in the Phase 1 implementation.

| จุด (Element/UX Point) | ตรง/ไม่ตรง design | หลักฐาน hex/line | แก้ยังไง (How to fix) |
| :--- | :---: | :--- | :--- |
| **Gold Button Glow** | ❌ ไม่ตรง | [cockpit_theme.py:L284-292](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L284-L292) | QSS ไม่สามารถแสดง drop-shadow / glow ได้โดยตรง ต้องสร้าง `QGraphicsDropShadowEffect` ใน Python และผูกกับ widget ปุ่มหลักที่สร้างใน [cockpit_theme.py:L409-414](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L409-L414):<br>```python\neffect = QGraphicsDropShadowEffect(btn)\neffect.setBlurRadius(18)\neffect.setColor(QColor(227, 179, 65, 153)) # 0.6 opacity\neffect.setOffset(0, 6)\nbtn.setGraphicsEffect(effect)\n``` |
| **Provider Substitute Badge** | ❌ ไม่ตรง | [cockpit_theme.py:L72-73](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L72-L73)<br>[settings_window.py:L424-431](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py#L424-L431) | ตัวแปรสี/ขอบมีครบใน code แต่ไม่ได้ถูกนำมาแสดงผลจริงในหน้า UI (แสดงเป็นตัวอักษรธรรมดา `→ Claude` ใน Info Banner)<br>ควรปรับให้ส่วน `→ Claude` แสดงผลเป็น badge (โดยใช้ Rich Text HTML ภายใน QLabel หรือแยก widget ย่อยสำหรับ badge เพื่อให้แสดงผลสี `#E9A876` และเส้นขอบ `rgba(217,119,87,0.4)` ตาม design) |
| **Active Nav Indicator** | ❌ ไม่ตรง | [cockpit_theme.py:L242-247](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L242-L247) | ใน QSS ทำเป็น `border-left: 3px solid {ACCENT_GOLD}` แต่ในดีไซน์ระบุเป็น `5px rounded color-bar` (แถบสีมนกว้าง 5px บนขอบซ้าย)<br>เนื่องจาก `border-left` ใน QSS ไม่สามารถทำขอบมนแยกฝั่งได้ ควรใช้ custom paint event บน nav button หรือจัดวาง `QFrame` สีทองหนา 5px ขอบมน 2px ด้านข้างปุ่มในเลย์เอาต์แทน เพื่อให้ตรงตามดีไซน์เป๊ะๆ |
| **Font-family of Chips / Badges** | ❌ ไม่ตรง | [cockpit_theme.py:L425-449](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L425-L449) | `role_chip` และ `gold_soft_chip` ถูกจัดเป็น badge/label ซึ่งตามดีไซน์ควรใช้ `IBM Plex Mono` (mono_family) แต่ใน code ปัจจุบันไม่ได้ระบุ `font-family` ทำให้มันสืบทอด `sans_family` มาจากตัวหน้าต่างหลักแทน<br>แก้โดยใส่ `font-family: "{mono_family}"` หรือดึงค่าจาก font cache เข้าไปใน stylesheet ของชิป |
| **Lead Toggle Switch State** | ❌ ไม่ตรง (UX) | [settings_window.py:L480-488](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py#L480-L488)<br>[cockpit_theme.py:L383-408](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L383-L408) | บทบาท Lead ถูกบังคับเปิดใช้งานตลอดเวลาและปิดไม่ได้ (Locked) ซึ่ง code ได้ตั้ง `toggle.setEnabled(False)` ไว้ถูกต้อง แต่ใน `ToggleSwitch.paintEvent` ไม่ได้เช็ค `self.isEnabled()` ทำให้มันวาดสีทองสว่างสะดุดตาเหมือนสวิตช์ที่ยังกดได้ตามปกติ (ละเมิด Heuristic ด้าน Visibility of System Status)<br>แก้โดยการลดความโปร่งแสง (opacity) หรือใช้วาดสีเทาหม่นเมื่อสวิตช์โดน disabled |
| **Toggle Switch Micro-interaction** | ⚠️ UX Improvement | [cockpit_theme.py:L383-408](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/cockpit_theme.py#L383-L408) | `ToggleSwitch` ไม่มีเอฟเฟกต์ตอบสนองตอนนำเมาส์ไปชี้ (Hover State) ทำให้ UI รู้สึกนิ่งเกินไป<br>แนะนำให้เช็ค `self.underMouse()` ใน `paintEvent` เพื่อวาดขอบหรือสีสวิตช์ให้สว่างขึ้นเล็กน้อยเพิ่มความพรีเมียม |
| **Default Swatch Selection** | ⚠️ UX Improvement | [settings_window.py:L634-645](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py#L634-L645)<br>[project_nav.py:L111-L122](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/project_nav.py#L111-L122) | สีเริ่มต้นของ New Role ถูกเซ็ตเป็น `#94a3b8` (สีเทา) ทว่าสีเทานี้ไม่อยู่ในพาเลทตัวเลือก `_AVATAR_COLORS` ที่มี 10 สี ทำให้ตอนเปิดหน้าจอครั้งแรกจะไม่มีสีใดโดนไฮไลท์ว่ากำลังเลือกอยู่เลย<br>แนะนำให้เปลี่ยนสีเริ่มต้น `self._nr_color` ให้ตรงกับสีแรกในพาเลท (เช่น `#6366f1` อินดิโก้) เพื่อให้สอดคล้องกันตั้งแต่เปิด |
| **Save & Apply Button State** | ❌ ไม่ตรง (UX) | [settings_window.py:L359-361](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py#L359-L361) | ปุ่ม "Save & Apply" เปิดใช้งานตลอดเวลา (Always Enabled) แม้ว่าจะยังไม่มีการเปลี่ยนแปลงใดๆ ใน Settings หน้าต่างเลย (ไม่สอดคล้องกับ Heuristic เรื่อง Error Prevention)<br>แก้โดยการเชื่อมปุ่ม Save ให้เป็น disabled ตั้งแต่เริ่มต้น และปรับเป็น enabled เมื่อ `_dirty` กลายเป็น `True` และกลับเป็น disabled อีกครั้งเมื่อบันทึก/รีเซ็ตเสร็จสิ้น |
| **Bundled Font Checking** | ⚠️ UX Improvement | [settings_window.py:L143-L146](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/settings_window.py#L143-L146) | ฟังก์ชัน `ensure_fonts_loaded()` คืนค่าคีย์ `"bundled": bool` เพื่อระบุว่าการโหลดไฟล์ฟอนต์ IBM Plex TTF จากโฟลเดอร์ static สำเร็จหรือไม่ แต่หน้าจอ SettingsWindow ไม่ได้นำค่านี้ไปใช้งานหรือแจ้งเตือนใดๆ หากฟอนต์โหลดไม่สำเร็จ<br>แนะนำให้เพิ่ม log warning ใน debug console หรือแสดง fallback icon หากระบบต้องถอยไปใช้ฟอนต์ดีฟอลต์ของ OS |

---

## 💡 สรุปภาพรวม (Summary)
- **Fidelity ในด้านสี (Colors)**: ทำได้ยอดเยี่ยมมาก สีตรงตามระบบดีไซน์จริงเกือบ 100% ไม่มีหลุดโทน Indigo/Teal เก่ามาปะปน
- **โครงสร้าง Layout และสัดส่วน**: Titlebar 38px, Status 56px, Sidebar 236px และ Footer 60px ทำมาได้เป๊ะและถูกต้องตามโครงสร้างที่ User ต้องการ
- **จุดที่ต้องแก้สำหรับ Phase 2**:
  1. การแสดงผล Drop Shadow/Glow ของปุ่มสีทอง (ต้องแก้ผ่าน Python GraphicsEffect)
  2. การวาดขอบมน 5px ของแถบข้างปุ่มเมนูที่กำลังแอคทีฟ (Active Nav indicator)
  3. ปรับฟอนต์ของ Badge / Role Chip ต่างๆ ให้ใช้ IBM Plex Mono
  4. ทำสถานะ Disabled ให้กับสวิตช์ปิด-เปิดของ Lead ที่โดนล็อกไว้ให้หม่นลง
  5. บังคับปิด/เปิดปุ่ม Save ตามความ Dirty ของข้อมูล เพื่อความถูกต้องทาง Heuristic UX
