# #123 — mb native Chrome บน Windows + Gemini artifacts env

วันที่: 2026-07-24
ขอบเขต: browser roles (`qa`, `critic`, `designer`), provider ทุกตัว, non-shard เท่านั้น

## Trace ก่อนแก้

- `spawn_engine.py` มี Chrome discovery อยู่ใน Claude spawn branch หลัง
  non-Claude branch early-return ไปแล้ว และจำกัดเฉพาะ `base_role == "qa"`.
  Codex/Gemini ที่ถูก map มาเป็น browser role จึงไม่ได้ contract เดียวกัน.
- Windows ยังให้ agent เรียก `mb-start-chrome`; wrapper นี้ตกไปใช้ WSL และไม่
  launch Chrome ตามผล empirical ใน issue #123.
- `_apply_artifacts_dir()` ถูกเรียกแยกในแต่ละ spawn branch แม้
  `_build_pane_env()`/`_build_lead_env()` จะเป็นจุดสร้าง env กลาง. รูปแบบนี้
  เคยทำให้ env อื่นตกหล่นใน early-return branch และไม่รับประกันว่า Gemini/agy
  หรือ provider ใหม่จะได้รับ central path.
- non-Claude `AGENTS.md` ไม่มีคำสั่งให้เก็บ evidence ที่
  `$TAKKUB_ARTIFACTS_DIR`; Gemini จึงสามารถใช้ private brain/cache path แม้
  process env มีค่าอยู่.
- เอกสาร shard เดิมเสนอ port-file workaround แต่ `mb` client ยัง hardcode
  CDP 9222 และไม่อ่าน port-file ของ launcher (#92), จึงเป็นคำแนะนำที่ใช้จริง
  ไม่ได้และทำให้ shards ชนกัน.

## การแก้ไข

### Native Chrome lifecycle

- เพิ่ม `browser_chrome.NativeChromeManager`.
- เฉพาะ Windows + `BROWSER_ROLES` + role ที่ไม่มี `#N`:
  - probe `http://127.0.0.1:9222/json/version`;
  - reuse endpoint ที่มีอยู่โดยไม่ถือ ownership;
  - ถ้ายังไม่มี ให้ cockpit launch `chrome.exe` ตรงด้วย
    `--remote-debugging-port=9222 --headless=new`;
  - ใช้ profile กลางที่
    `runtime/browser-profiles/mb-native-chrome`;
  - shutdown จะ `taskkill /PID <pid> /T /F` เฉพาะ process tree ที่ cockpit
    เป็นผู้เปิด ไม่ kill Chrome ที่ผู้ใช้เปิดไว้ก่อน.
- เรียก manager ก่อนแยก Claude/non-Claude provider จึงครอบคลุม Claude,
  Codex, Gemini และ provider mapping อื่นเท่ากัน.
- `CHROME_BIN` discovery ย้ายเป็น provider-neutral helper สำหรับ browser role
  ทั้งสาม. macOS/Linux ยังใช้ `mb-start-chrome` เดิม; ไม่มี native-launch
  behavior ใหม่บนสอง platform นี้.
- cleanup ถูกต่อเข้าทั้ง GUI close, signal/atexit และ headless shutdown.

### Shard constraint (#92)

- cockpit ไม่ launch shared Chrome ให้ `qa#N`/`critic#N`/`designer#N`.
- shell guard บล็อก `mb` และ `mb-start-chrome` สำหรับ browser shard พร้อม
  ชี้ให้ใช้ shard-isolated Playwright MCP.
- แก้ `qa.md` ให้ลบ port-file workaround และระบุ constraint ชัดเจน.

### Gemini artifacts env

- `_build_pane_env(project_ns)` และ `_build_lead_env(project_ns)` stamp
  `$TAKKUB_ARTIFACTS_DIR`/`$TAKKUB_DOCS_DIR` ภายใน builder กลาง.
- spawn ทุก branch ส่ง `project_ns` เข้า builder; ตัด call-site stamp ที่ซ้ำ.
- เพิ่ม contract ใน non-Claude `AGENTS.md` ให้ Gemini/Codex เก็บ screenshot,
  temp script และ evidence ใน `$TAKKUB_ARTIFACTS_DIR` ไม่ใช่ private
  brain/cache.

### Doctor

- เพิ่ม `[browser]/mini-browser` check.
- ถ้า `mb` หายแต่ npm มีอยู่ `takkub doctor --fix` จะติดตั้ง
  `@runablehq/mini-browser` แบบ global ด้วย fixed argv/non-interactive env.
- ถ้า npm หายจะแจ้ง manual Node.js/install hint โดยไม่พยายามติดตั้ง.

## Verification

- Targeted tests:
  - `275 passed, 3 skipped`
  - ครอบคลุม native Chrome discovery/launch/reuse/ownership cleanup,
    Windows/macOS/Linux gating, BROWSER_ROLES, shard denial, doctor auto-fix,
    Gemini spawn env, artifacts builder, role-doc sync.
- Spawn/lifecycle regression selection:
  - `161 passed`
  - ครอบคลุม spawn gate, Codex argv, provider models, OpenCode/Cursor,
    Claude env leak, headless teardown และ Lead lifecycle.
- `ruff check`, `ruff format --check`, `git diff --check` ผ่าน.
- import-linter contracts ผ่าน.
- Windows smoke บนเครื่องจริง:
  - พบ Chrome ที่
    `C:\Program Files\Google\Chrome\Application\chrome.exe`;
  - พบ mb ที่ `C:\Users\monch\AppData\Roaming\npm\mb.cmd`;
  - manager launch native Chrome สำเร็จ, CDP 9222 ready;
  - cleanup สำเร็จและ CDP 9222 ปิดหลัง cleanup.

## ข้อจำกัดคงเดิม

`mb` client target ได้เฉพาะ CDP 9222. งาน browser แบบ shard ต้องใช้
Playwright MCP เท่านั้นจนกว่า upstream mini-browser จะรองรับ per-client
port/endpoint.
