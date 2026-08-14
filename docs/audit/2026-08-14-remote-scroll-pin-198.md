# #198: remote mobile ไม่ควรดีด scroll ลงล่างสุดขณะอ่านย้อนหลัง

## Root cause (ยืนยันแล้วจากอ่านโค้ด)

`src/agent_takkub/remote/static/app.js` มี atBottom heuristic ที่ถูกต้องอยู่แล้วสำหรับ
`appendMsgDom`/`appendLeadLive` (streaming path) แต่ 3 จุดเขียนทับ heuristic นั้นแบบไม่มีเงื่อนไข:

1. `showThinking()` จบด้วย `log.scrollTop = log.scrollHeight;` แบบไม่เช็คอะไรเลย —
   ถูกเรียกทุกครั้งที่ SSE `working` event เข้ามา (`setProjectWorking`), รวมถึงหลัง
   `appendMsgDom`/`appendLeadLive` ตัดสินใจแล้วว่าไม่ควร scroll → override ทิ้งทันที
2. `renderSelectedProject()` (full DOM rebuild) จบด้วย `log.scrollTop = log.scrollHeight;`
   แบบไม่มีเงื่อนไขเช่นกัน โดยมี comment อธิบายว่าเป็นเคส "เพิ่งเปิดแชท" — แต่ถูกเรียกจาก
   `es.onopen` (SSE **reconnect**, เกิดบ่อยและเงียบ) ด้วย ซึ่งไม่ใช่เคสนั้น
3. `visibilitychange` → `refreshOpenProjectHistories()` → `refreshProjectHistory(project, false)`
   → `loadHistory(project, true)` → success handler เรียก `renderSelectedProject()` เดิมพาธ
   เดียวกับ (2) — คือเคส **พับแอปแล้วกลับมา** (background→foreground) ที่ user รายงานว่าโดนดีด

## การแก้ไข

### 1. Shared pinned-to-bottom helper
เพิ่ม `isPinnedToBottom(log)` / `scrollToBottom(log)` / `showNewMessagesButton()` /
`hideNewMessagesButton()` เป็น helper กลาง — ทุก path ที่แต่ก่อนเขียน
`log.scrollTop = log.scrollHeight` ตรงๆ เปลี่ยนมาเรียกผ่าน helper นี้ทั้งหมด
(`appendMsgDom`, `appendLeadLive` merge path, `showThinking`, `renderSelectedProject`)

Threshold จาก `24px` → `SCROLL_PIN_PX = 48px` เผื่อ momentum scrolling ของ
iOS Safari / Android Chrome (scrollTop อ่านค่าคลาดใกล้ bottom ได้)

### 2. `showThinking()` เคารพ pinned state
คำนวณ `isPinnedToBottom(log)` **ก่อน** append thinking bubble แล้วค่อยตัดสินใจ scroll
(เดิม scroll ทันทีไม่มีเงื่อนไข)

### 3. `renderSelectedProject(forceScroll)` แยกเจตนา "เปิดแชทใหม่" ออกจาก "รีเฟรชเงียบๆ"
- คำนวณ `pinned` = `forceScroll || isPinnedToBottom(log)` **ก่อน** เคลียร์ DOM เก่า
  (วัดได้แค่ตอนนี้เท่านั้น หลังเคลียร์แล้ววัดไม่ได้อีก)
- ระหว่าง rebuild ส่ง `skipScroll=true` ให้ `appendMsgDom` ทุกข้อความ (ไม่งั้น
  per-message atBottom heuristic จะ auto-scroll เองระหว่าง loop เพราะ log เพิ่งถูก
  เคลียร์เป็นค่าว่าง — เกือบทุกข้อความแรกๆ จะอ่านว่า "atBottom=true" เสมอ)
- ตัดสินใจ scroll ครั้งเดียวหลัง rebuild เสร็จ: `pinned` → jump bottom, ไม่งั้น
  restore `savedScrollTop` เดิม (clamp ด้วย `Math.min` กัน scrollHeight เปลี่ยน)

**Call sites ที่ pass `forceScroll=true`** (เจตนา "เปิดแชทใหม่จริงๆ" — clientHeight
อาจอ่านได้ 0 ตอน view ยังไม่ active วัด pinned ไม่ได้อยู่แล้ว):
`switchView("lead")`, `selectProject()`, `session_changed` SSE event, `confirmResume()`

**Call sites ที่ปล่อยให้วัด pinned state จริง** (ไม่ force):
`es.onopen` (reconnect), background history refresh (`visibilitychange` → `announce=false`)

`loadHistory(project, force, jumpToBottom)` เพิ่ม param ที่ 3 — `refreshProjectHistory`
ส่ง `announce` ต่อเป็น `jumpToBottom` ตรงๆ (ปุ่ม refresh ที่ user กดเอง = jump ได้,
background catch-up หลังพับแอป = ห้าม jump)

### 4. ปุ่มลอย "ข้อความใหม่ ↓"
`#new-msg-wrap` เป็น `position: sticky; bottom: 8px;` **child ตัวสุดท้าย** ของ
`#lead-log` เอง (ไม่ใช่ absolute overlay แยก) — เกาะกับขอบล่างจริงของ scrollport
เสมอไม่ว่า composer/quick-replies/picker-banner จะสูงเท่าไหร่ ทุกจุดที่ append
ข้อความใหม่ตอนไม่ pinned จะเรียก `showNewMessagesButton()` ซึ่ง `appendChild` ปุ่ม
กลับไปเป็น last child เสมอ (กัน sticky หลุดตำแหน่งเมื่อมีข้อความใหม่แทรกเข้ามาทีหลัง)
กด `scrollToBottom()` + ปุ่มหายเองเมื่อ user เลื่อนลงมาถึงล่างสุดเองผ่าน `scroll` listener

### 5. Threshold
`24px` → `48px` (ดูข้อ 1)

## ไฟล์ที่แก้

- `src/agent_takkub/remote/static/app.js` — logic ทั้งหมดข้างบน
- `src/agent_takkub/remote/static/index.html` — markup + CSS ของปุ่ม "ข้อความใหม่ ↓"
- `tests/test_remote_pwa_scroll_pin.py` — ใหม่ ครอบ helper + wiring ทุกจุด
- `tests/test_remote_pwa_resume.py` — อัปเดต 3 assertion ให้ตรงกับ signature ใหม่
  (`loadHistory(project, force, jumpToBottom)`, `renderSelectedProject(true)` ใน
  `selectProject`)

## ผลทดสอบ

### Targeted tests (โค้ด)
```
tests/test_remote_pwa_scroll_pin.py .......... (ใหม่ทั้งหมด, ผ่าน)
tests/test_remote_pwa_quick_reply.py .......... (ผ่าน, ไม่มี regression)
tests/test_remote_pwa_resume.py ............... (ผ่านหลังอัปเดต 3 assertion)
tests/test_terminal_links.py + test_installed_mode_gate.py .... (ผ่าน, sanity)
```
`node --check app.js` ผ่าน (syntax valid)

full pytest suite ยังไม่รัน — ตาม test-tier policy รันครั้งเดียวที่ qa batch gate

### 5 เคสที่ task ระบุ — verify ทาง code path (ดูหัวข้อ "การแก้ไข" ด้านบนสำหรับ mapping)
ยืนยันได้จากการอ่าน call-site ทุกจุดที่กระทบ `#lead-log.scrollTop` ครบทุกจุดแล้ว
(ไม่เหลือจุดที่ scroll แบบไม่มีเงื่อนไขนอก `scrollToBottom()`/`Math.min(savedScrollTop,...)`
— มี regression test คุมไว้ที่ `test_no_raw_unconditional_scrollTop_assignment_outside_helper`):

| เคส | Call path | ผล |
|---|---|---|
| (a) เลื่อนขึ้นอ่านย้อนหลังระหว่าง pane working ต้องไม่ถูกดีดลง | `showThinking()` ผ่าน SSE `working` event | แก้แล้ว — เคารพ `isPinnedToBottom` |
| (b) อยู่ล่างสุด ต้องตามข้อความใหม่อัตโนมัติ | `appendMsgDom`/`appendLeadLive` เมื่อ `atBottom=true` | ไม่เปลี่ยนพฤติกรรมเดิม (`scrollToBottom` เมื่อ pinned) |
| (c) เปิดแชทครั้งแรกต้องลงล่างสุด | `switchView("lead")` → `renderSelectedProject(true)` | แก้แล้ว — force jump |
| (d) สลับ pane/project ต้องลงล่างสุด | `selectProject()` → `renderSelectedProject(true)` | แก้แล้ว — force jump |
| (e) พับแอปแล้วกลับมาต้องไม่ดีด | `visibilitychange` → `refreshProjectHistory(project, false)` → `renderSelectedProject(false)` | แก้แล้ว — ไม่ force, pinned state ตัดสิน |

**⚠️ ยังไม่ได้ verify จริงบนมือถือ/browser** — ตาม tool policy ของ role นี้
(ห้ามรัน Playwright/browser driver เอง — เป็นหน้าที่ qa) ต้องส่งต่อให้ qa ทดสอบ
5 เคสข้างบนจริงบน remote link ก่อนปิด issue #198 สนิท
(`https://oooo.sabuytube.xyz/KCwH-ccLz9cCmngZ_FqNJQ/` หรือ
`http://127.0.0.1:9999/KCwH-ccLz9cCmngZ_FqNJQ/`)

## ไม่กระทบ #192 / merge window

ไม่ได้แตะ `emptyReason` logic ของ #192 หรือ merge-window comment ที่บรรทัด
933-941 เดิม (`showThinking()`'s "Do NOT reset lastMsgKind/lastLeadBodyEl here")
— comment block นั้นยังอยู่ครบ, เปลี่ยนแค่บรรทัดสุดท้ายของ scroll assignment
