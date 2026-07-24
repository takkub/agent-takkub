# Team Settings (👥 Team) — redesign pass, 2026-07-24

## บริบท

Task spec ระบุ "SettingsWindow (👥 Team)" + อ้าง `settings_management/` ว่าเป็นที่ที่มี 3 หน้าจริง
(Providers & Roles / MCP Matrix / Plugins Matrix) แต่ตรวจโค้ดจริงพบว่า:

- `status_header.py`'s `👥 Team` chip → `user_actions._on_team_chip_clicked()` →
  เปิด **`settings_window.SettingsWindow`** (ไฟล์เดียว 2888 บรรทัด ไม่ใช่
  `settings_management/window.SettingsManagementWindow`)
- `settings_window.py` มี native QWidget builder ของตัวเอง (`_build_providers_roles_view`,
  `_build_mcp_matrix_view`, `_build_plugins_matrix_view`, ...) — **ไม่ได้** import
  `settings_management/pages/*` เลย
- `settings_management/window.py` (`SettingsManagementWindow`) เป็น window แยกที่ไม่มี
  entry point ในแอปจริง (เข้าถึงได้ผ่าน `python -m agent_takkub.settings_management` เท่านั้น
  — dev-only harness)

→ ไฟล์ที่ user เห็นจริงเมื่อกด 👥 Team คือ `settings_window.py` จึงแก้ที่นี่ (ไม่ใช่
`settings_management/`) เพื่อให้ผลลัพธ์ตรงกับสิ่งที่ user เห็นในแอปจริง — scope ยังคงจำกัดที่
3 หน้าที่ task ระบุ (Providers & Roles / MCP Matrix / Plugins Matrix) บวกกับ shared chrome
(sidebar/heading/footer) ที่ทุกหน้าใช้ร่วมกันอยู่แล้ว — ไม่แตะเนื้อหาภายในของหน้าอื่น
(Pipeline Builder/Templates/Role Overlap/New Role/Users/Skill Catalog/Skill Matrix)

## สิ่งที่เปลี่ยน

**Shared chrome** (`settings_window.py` + `cockpit_theme.py`):
1. Sidebar nav — เพิ่ม icon glyph หน้าแต่ละรายการ (ported จาก mockup's `icon()` mapping ต่อหมวด:
   Pipeline `⌘` / Providers `◉` / Matrix `⊞` / Skill `✦` / Users `♙` / อื่นๆ `◇`) ผ่าน `_NAV_ICONS` dict
2. Heading block — เพิ่ม pre-title เล็ก caps สีทอง "CONFIGURATION" เหนือชื่อหน้า
   (`QLabel#contentPreTitle`, mono font, gold) ตามลำดับ mockup: pre-title → ชื่อหน้าใหญ่ → subtitle
3. Sticky save bar (footer) — คำเตือน dirty-state เปลี่ยนเป็นไทยตรงตาม mockup:
   "มีการแก้ไขที่ยังไม่บันทึก" (เดิม "unsaved changes") ปุ่ม Revert/Cancel/Save && Apply คงพฤติกรรมเดิม
   (save-on-apply ของจริงอยู่แล้ว — ไม่ได้หลอกว่า auto-save)

**Providers & Roles** (`_build_providers_roles_view` / `_build_role_row`):
4. Provider rows + Role rows ห่อเป็น "card" จริง — objectName `providerRow`/`roleRow`,
   พื้นหลัง `GROUND_PANEL_ALT` + border `BORDER_HAIRLINE` + `RADIUS_SM`, padding 10/8px
   (เดิมเป็นแค่ QHBoxLayout ไม่มีพื้นหลัง แยกด้วย spacing เฉยๆ) — badge/desc/model-select/toggle
   เดิมครบอยู่แล้ว จัดเป็น card layout ตาม mockup

**MCP Matrix / Plugins Matrix** (`_populate_matrix_grid`, ใช้ร่วมกันทั้ง 2 หน้า):
5. เพิ่ม "Role" header label ที่ col 0 (เดิมไม่มี header ของคอลัมน์ role)
6. Header row (`matrixHeaderCell`) ได้ mono font + bottom border แยกจาก body ชัดเจนขึ้น
   (เดิมใช้ `panelHint` style เดียวกับ body cell) — role color-dot ต่อแถวมีอยู่แล้ว (`role_chip`)
   **หมายเหตุ:** sticky-on-scroll header (จริง ๆ ล็อคตำแหน่งตอน scroll) ไม่ทำ — grid อยู่ใน
   `QScrollArea` เดียวกับทั้งหน้า ต้องแยก header ออกเป็น widget คนละตัวที่ sync คอลัมน์กว้างเอง
   ซับซ้อนเกินสัดส่วนงาน (task ระบุ "ถ้าทำได้" = optional) — ใช้ visual band (bold + border-bottom)
   แทนเพื่อให้อ่านง่ายขึ้นโดยไม่เพิ่มความเสี่ยง

## Constraints ที่รักษาไว้

- ไม่แตะ DTO/repositories/services ใน `settings_management/` เลย (ไม่ได้ใช้งานจริงในหน้านี้)
- ไม่แตะ `QComboBox::down-arrow` (commit 82e92fd)
- ไม่ hardcode สี/font ใหม่ — ทุก token มาจาก `cockpit_theme.py` (gold `ACCENT_GOLD`,
  grounds, borders ตามระบบเดิม)
- ฟังก์ชัน save/reset/dirty-tracking เดิมไม่เปลี่ยน — แก้เฉพาะ view layer (widget structure + QSS)
- Cross-platform: ไม่มี path/OS-specific code ในงานนี้ (Qt widget/QSS ล้วน — เหมือนกันทั้ง Win/mac)

## Tests

- `pytest tests/test_settings_window.py tests/test_settings_window_routing.py` — 81 passed
- `pytest tests/test_cockpit_theme.py` — 23 passed
- ไม่ได้รัน full suite (targeted only ตาม policy — full suite รันที่ qa batch gate)

## Round 2 fix loop — design review 2026-07-24-team-settings.md (frontend, cockpit_theme.py scope)

Re-scoped mid-task by Lead to `cockpit_theme.py` + font files only — `settings_window.py`/
`static/icons/` owned by a parallel frontend#2 pane (avoid edit collision). ครอบเฉพาะ:

**[high] ข้อ 1 — Thai font fallback (root cause #2 ของ critic):**
- bundle `static/fonts/NotoSansThai-Regular.ttf` (Google Fonts OFL, variable font wght 100–900)
  + `static/fonts/LICENSE-NotoSansThai.txt` (Noto Project Authors OFL) — ตาม pattern ไฟล์เดิมของ
  IBM Plex (`ensure_fonts_loaded`/`_load_font_family` โหลดผ่าน `QFontDatabase.addApplicationFont`
  เหมือนกัน) — เพิ่ม `pyproject.toml`'s package-data ให้ license ใหม่ติดไปกับ wheel ด้วย
- `ensure_fonts_loaded()` คืนค่าเพิ่ม key `"thai"` (family ที่โหลดได้ หรือ fallback ต่อ candidate
  list ใหม่ `FONT_THAI_FALLBACK_CANDIDATES = (Leelawadee UI, Thonburi, Noto Sans Thai, Tahoma)`
  — ครอบ Win/mac/Linux ตาม critic's cross-platform note)
- root cause ที่แท้จริงคือ `build_stylesheet:366` (เดิม) ประกาศ `font-family: "{sans_family}"`
  **ตัวเดียว ไม่มี fallback chain เลย** — แม้ IBM Plex เองก็ไม่มี glyph ไทย และ Qt ไม่มีที่ให้ fallback
  ต่อ. Fix: `_sans_font_stack()` ใหม่ประกอบ comma-list `"{sans}", "{thai}", "Leelawadee UI",
  "Thonburi", "Tahoma"` (ตัด duplicate) แล้วให้ top-level `QWidget#settingsWindow` ใช้ stack นี้แทน
  ชื่อเดียว — ทุก child label ที่ไม่ได้ override font-family ของตัวเอง (subtitle/notice/dirty
  message) inherit สแตกนี้อัตโนมัติ แก้ tofu ไทยทั้งแอปจากจุดเดียว ไม่ต้องไล่แก้ทีละ label
- verify manual: `ensure_fonts_loaded()` คืน `{"sans": "IBM Plex Sans", "mono": "IBM Plex Mono",
  "thai": "Noto Sans Thai", "bundled": True}` จริงบนเครื่อง dev + font-family string ที่ build_stylesheet
  ส่งออกมามี `"Noto Sans Thai"` อยู่ในสแตกจริง

**[med] ข้อ 4 (เฉพาะส่วน theme) — toggle off-track contrast + footer separation + dirty dot:**
- `ToggleSwitch.paintEvent` — unchecked track เปลี่ยนจาก `GROUND_SELECT` (#232732, แทบไม่ต่างจาก
  card bg `GROUND_PANEL` #181b21) เป็น `TOGGLE_TRACK_OFF` = `#2d323e` (constant ใหม่) + เพิ่ม inner
  border บางๆ (`rgba(255,255,255,20)`, 1px) เฉพาะ state unchecked ให้เห็นทรง rounded-rect ชัดขึ้น
- `#footer` border-top ยกจาก `BORDER_HAIRLINE` (rgba .06 — จางมาก) เป็น `BORDER_STRONG2` (rgba .14)
  ให้แยกจาก content ที่ scroll ชัดขึ้น
- `#unsavedDot`/`QFrame#unsavedDot` QSS ใหม่: background gold + `border-radius:4px` +
  min/max-width/height 8px (พร้อมใช้ทั้ง QLabel เดิมแบบ empty-text หรือ QFrame) แทน glyph "●"
  ที่ font ไม่มี — เพิ่ม helper `cockpit_theme.color_dot()` ให้ตัวที่แก้ widget-instantiation ฝั่ง
  `settings_window.py` (frontend#2 หรือรอบถัดไป) เรียกใช้แทนการ hardcode QLabel("●") ใหม่ — **ยังไม่ได้
  แก้ widget จริงใน `settings_window.py`** (out of scope รอบนี้ตาม re-scope: ไฟล์นั้น frontend#2 คุมอยู่)
  เช่นเดียวกับ status-strip provider indicator dots (บรรทัด ~470 เดิม) ที่ยังใช้ `QLabel("●")` อยู่ —
  ต้องสลับไปเรียก `color_dot()` ในไฟล์นั้นเพื่อให้ tofu หายสมบูรณ์

**Refactor เล็กประกอบ:** แยก `_load_font_family()` ออกจาก `ensure_fonts_loaded()` (เดิม copy-paste
โค้ดโหลด font 2 ชุดเกือบเหมือนกันสำหรับ sans/mono) — ใช้ซ้ำสำหรับ thai ด้วย ลด duplication

### Tests
- `pytest tests/test_cockpit_theme.py` — 23 passed
- `pytest tests/test_settings_window.py` — 81 passed (sanity — build_stylesheet signature ไม่เปลี่ยน)
- `pytest tests/test_settings_management_main.py` — 3 passed (window.py อีกตัวที่เรียก
  `build_stylesheet`/`ensure_fonts_loaded` เหมือนกัน)
- `ruff check src/agent_takkub/cockpit_theme.py` — clean
- ไม่ได้รัน full suite (targeted only ตาม policy)

### สิ่งที่เหลือ (นอก scope รอบนี้ — flag ให้ Lead/รอบถัดไป)
- Nav icon SVG QIcon (ข้อ 2) + window-control glyph + matrix legend/category/row-separator/
  empty-state (ข้อ 3) + card header kicker+tag chip ในส่วน widget (ข้อ 4 ที่เหลือ) — อยู่ใน
  `settings_window.py`/`static/icons/`, มอบให้ frontend#2 ตาม re-scope
- `status_header.py`'s provider dot ("●" glyph, ถ้ามี) และ `settings_window.py`'s status-strip
  provider indicator dots + footer `_unsaved_dot` ต้องเปลี่ยนจาก `QLabel("●", ...)` เป็น
  `cockpit_theme.color_dot(color, parent)` เพื่อใช้ QSS ใหม่นี้จริง — theme พร้อมแล้ว รอ widget-side wiring

## Round 3 — frontend#2: nav icons + matrix polish + card header (`settings_window.py` + `static/icons/nav/`)

รับช่วงต่อจาก Round 2 (`settings_window.py` + `static/icons/` เท่านั้น — ไม่แตะ `cockpit_theme.py`/font
ไฟล์ ตามที่ Lead ประสาน กันชนกับ frontend#1). ครอบข้อ 2/3/4 ที่เหลือทั้งหมด:

**[high] ข้อ 2 — nav icons เป็น SVG QIcon (root cause #1):**
- สร้าง `static/icons/nav/nav-<name>-{muted,gold}.svg` 12 ไฟล์ (6 glyph มุมมอง × 2 tone) —
  pipeline (3-node flow), diamond (Templates/Role Overlap), target (Providers & Roles), grid (MCP/
  Plugins Matrix), star (Skill Catalog/Matrix), user (Users) — geometric line-art 16×16 แทนที่
  emoji-glyph เดิม (`⌘ ◇ ◉ ⊞ ✦ ♙`) ที่ IBM Plex ไม่มี glyph ให้
- `_NAV_ICON_NAMES` (แทนที่ `_NAV_ICONS` เดิม) + `_nav_icon(view_idx, active=...)` โหลด `QIcon` จาก
  `static/icons/nav/` — สีเปลี่ยนตาม active state จริง (gold ตอน active, muted ตอนไม่ active)
  ผ่านการสลับไฟล์ svg คนละสี ไม่ใช่พึ่ง QSS `color:` (QIcon ไม่รับ inheritance สีจาก QSS)
- `_goto_view()` เรียก `btn.setIcon()` ใหม่ทุกครั้งที่ active state เปลี่ยน (เดิม property `active`
  แค่เปลี่ยน background ผ่าน QSS, ตอนนี้ต้อง sync icon สีคู่กันด้วย)
- **window-control glyph มุมขวาบน (item เดียวกัน) — ไม่พบใน scope นี้**: faux titlebar ที่เคยมี
  traffic-light glyph ถูกถอดไปแล้วตั้งแต่ UI walkthrough #55 (2026-07-11, ดู docstring หัวไฟล์
  `settings_window.py`) — `SettingsWindow` เป็น `QDialog` ธรรมดา ใช้ปุ่ม close ของ OS titlebar
  จริง (native, ไม่ใช่ Qt widget ที่ควบคุมสีได้จากในแอป) → tofu ที่ critic เห็นน่าจะมาจาก
  OS window-manager font ไม่ใช่โค้ดในไฟล์นี้ — flag ให้ Lead ตรวจว่าเป็น environment เฉพาะเครื่องที่
  capture screenshot หรือเปล่า ไม่มีอะไรให้แก้ฝั่ง `settings_window.py`

**[med] ข้อ 3 — matrix polish (`_populate_matrix_grid`, ใช้ร่วม MCP/Plugins/Skill Matrix):**
- Legend footer ใหม่ (`_build_matrix_legend`) ใต้ทุก matrix panel: gold dot "Allowed" / faint dot
  "Blocked" + ข้อความ "Security policy จะถูก validate ก่อน Apply" (ตรง `➕` item ของ review)
- Column header เป็น 2 บรรทัดผ่าน `_build_matrix_column_header(item, category, parent)`: ชื่อ item
  (mono, `matrixHeaderCell` เดิม) + sublabel หมวดสีจาง ("MCP" ใน MCP Matrix, "Plugin" ใน Plugins
  Matrix — Skill Matrix ไม่ส่ง category เข้ามาก็ยัง fallback ไม่มี sublabel เหมือนเดิม ไม่กระทบ)
- Row hairline: ทุก cell (role-chip cell + toggle cell ทุกคอลัมน์) ได้ `border-bottom: 1px solid
  BORDER_HAIRLINE` แบบ inline ต่อ widget (ไม่ใช่ QSS ใหม่ใน `cockpit_theme.py`) ให้ไล่สายตาตามแถวง่ายขึ้น
- Empty-state MCP/Plugins Matrix: เปลี่ยนจาก `panelHint` เล็กๆ เหนือ grid ที่ยังโชว์ role-list ลอย
  (grid panel ไม่ซ่อน) → ซ่อน matrix panel ทั้งก้อนเมื่อไม่มี item + โชว์กล่อง dashed-border ใหญ่
  (`placeholderBadge` — QSS ที่มีอยู่แล้วใน `cockpit_theme.py`, ไม่ต้องขอเพิ่ม) ข้อความ "ยังไม่มี MCP
  server — กด "+ Add MCP server" ด้านบนเพื่อเริ่ม" ชัดเจนขึ้น

**[med] ข้อ 4 (ส่วน window) — card header kicker+tag chip + dirty indicator:**
- `_build_card_header(kicker, title, tag, parent)` ใหม่: kicker บรรทัดบน (mono, uppercase, faint,
  letter-spacing) + title (`panelTitle` เดิม) + tag chip ขวา (`cockpit_theme.gold_soft_chip`,
  compact) — ใช้กับ 2 card ใน Providers & Roles: "MODEL CONNECTIONS / Providers / N enabled" และ
  "TEAM ROSTER / Roles / N/M active" (นับสดจาก provider_state/roles_enabled ตอน build ไม่ hardcode)
- Card อื่น (Skill Catalog/Templates/Pipeline Builder/...) ยังใช้ `panelTitle` เดิม — ไม่ใช่ scope
  ที่ review ระบุ (mockup อ้างถึงเฉพาะ Providers/Roles panel)
- Dirty indicator + status-strip provider dots: สลับจาก `QLabel("●", ...)` (glyph ที่ทำให้ tofu)
  เป็น `cockpit_theme.color_dot(color, parent, size=8)` ตามที่ frontend#1 เตรียม helper รอไว้แล้ว —
  wiring ครบทั้ง 2 จุด (footer `_unsaved_dot` + status-strip per-provider dots)

**objectName/QSS ใหม่ที่ขอ:** ไม่มี — ทุกจุดใช้ QSS class ที่มีอยู่แล้ว (`panelTitle`, `matrixHeaderCell`,
`placeholderBadge`, `gold_soft_chip()`, `color_dot()`) + inline `setStyleSheet()` เฉพาะจุด (border-bottom
hairline, kicker label) เพื่อเลี่ยงชนกับ `cockpit_theme.py` ที่ frontend#1 คุมคู่ขนาน

### Tests
- `pytest tests/test_settings_window.py tests/test_settings_window_routing.py` — 85 passed
- `ruff check src/agent_takkub/settings_window.py` — clean
- ไม่ได้รัน full suite (targeted only ตาม policy — full suite รันที่ qa batch gate)

### สิ่งที่เหลือ (flag ให้ Lead)
- window-control glyph มุมขวาบน — ไม่พบ code path ให้แก้ใน `settings_window.py` (ดูเหตุผลข้างบน,
  ข้อ 2) — ถ้า critic ยัง repro ได้ ต้องดูใน environment/OS ตอน capture ไม่ใช่ widget นี้
- Provider row monogram avatar + "Test connection" button, help "?" button, card gap เพิ่ม — เป็น
  item `[low]` ใน review, ไม่ทำรอบนี้ (ตาม priority ordering)
