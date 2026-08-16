# #252 (frontend half) — PWA "Online" chip lied when the tunnel was down

## หลักฐานที่พิสูจน์แล้ว

- ตอน cockpit ปิด remote (tunnel ตาย) — `https://oooo.sabuytube.xyz/` ตอบ **HTTP 530**, `Content-Type: text/plain`, body `error code: 1033` — หน้า error ของ Cloudflare edge เอง ไม่ใช่ response จาก cockpit
- `app.js` เดิม (`apiFetch`, บรรทัด ~245-288): `fetch()` แค่ resolve ก็เรียก `setOffline(false)` ทันที **ไม่เช็ค `res.status` เลย** → 530 จาก Cloudflare ก็ยังโชว์ chip เขียว "Online"
- `apiFetch` เช็คเฉพาะ 404 (token ตาย) กับ 403 (password_required) — ไม่มีสาขา 5xx → response ที่ไม่ใช่ JSON ถูกส่งต่อให้ `r.json()` แล้วพัง → ตกเข้า `.catch` ของ `loadProjects` โชว์ "โหลด projects ไม่สำเร็จ ลองใหม่อีกครั้ง" ซึ่งไม่บอกสาเหตุจริง (ผู้ใช้เข้าใจผิดว่าต้องกด retry แทนที่จะไปเปิดเครื่อง)

## Root cause

`apiFetch` ใช้ "fetch resolved โดยไม่ throw" เป็นสัญญาณว่า "คุยกับ cockpit ได้" — แต่ Cloudflare tunnel/edge จะ intercept request ก่อนถึง cockpit จริงเวลา tunnel ล่ม แล้วตอบ error page ของตัวเอง (5xx) กลับมาเป็น **response ที่ resolve สำเร็จ** (ไม่ใช่ network exception / `TypeError`) — โค้ดเดิมแยกแยะไม่ออกระหว่าง "cockpit ตอบสำเร็จ" กับ "edge ตอบแทน cockpit"

## Fix

`src/agent_takkub/remote/static/app.js`

1. **`apiFetch`** — เพิ่มการเช็ค `res.status >= 500` เป็นด่านแรกก่อน `setOffline(false)` เดิม: cockpit เอง (`http_server.py`) ไม่เคยตอบ 5xx ในทุก endpoint (200/400/403/404/409 เท่านั้น, ยืนยันจาก `_send_json`/`_reject`) — ดังนั้น 5xx ใดๆ ที่เห็นคือ edge/tunnel error เสมอ ไม่ใช่ cockpit → `setOffline(true)` + throw `Error("cockpit_unreachable")` แทนที่จะปล่อยให้ `r.json()` พังแบบไม่มีสาเหตุ
2. **`isConnError(err)` + `CONN_ERROR_MSG`** — helper ใหม่ข้าง `setOffline`: รวม `TypeError` (fetch reject จริง เช่น DNS/connection refused) กับ `cockpit_unreachable` (edge ตอบแทน) เป็นเคสเดียวกัน พร้อมข้อความไทย "เชื่อมต่อ cockpit ไม่ได้ (tunnel ล่ม หรือ cockpit ปิด remote อยู่)"
3. ใช้ `isConnError`/`CONN_ERROR_MSG` ในทุกจุดที่ยิง `apiFetch` แล้วโชว์ error ให้ผู้ใช้เห็นตรงๆ: `loadProjects`, verify-password form, `openProject`, `closeProject`, `sendLeadMessage`, `sendLeadImage`, `confirmResume` — แยกจาก generic message เดิม ("โหลดไม่สำเร็จ ลองใหม่") ที่ยังใช้กับ error อื่นที่ไม่ใช่ connectivity (เช่น 400/409 จาก cockpit จริง)
4. Retry spam: `scheduleEsRetry` (SSE reconnect) มี exponential backoff (`1000 * 2^retries`, cap 15s) อยู่แล้ว — ไม่ต้องแก้, ตรวจสอบแล้วว่าไม่ยิงรัวๆ ตอน edge error

## ไม่แตะ

- `res.status === 404 && hadToken` (token ตาย → pairing) และ `res.status === 403` (`password_required` → password prompt) — auth path เดิมทุกจุด ยืนยันด้วยเทส
- `src/agent_takkub/remote/*.py` (backend ทำคู่ขนานอยู่)

## Tests

`tests/test_remote_pwa_liveness.py` (ใหม่, 12 tests, source-level structural check ตามแพทเทิร์นเดียวกับ `test_remote_pwa_*` อื่นๆ — ไม่มี JS runtime ใน suite นี้):

- `apiFetch` เช็ค `res.status >= 500` ก่อน `setOffline(false)` เสมอ (ordering)
- 404/hadToken → `forgetToken()` และ 403 → `showPasswordPrompt()` ยังอยู่ครบ (regression guard)
- `isConnError`/`CONN_ERROR_MSG` ถูก define และมีคำว่า cockpit/tunnel ในข้อความ
- ทุก call site (`loadProjects`, verify-password, `openProject`, `closeProject`, `sendLeadMessage`, `sendLeadImage`, `confirmResume`) เรียก `isConnError(err)`
- `scheduleEsRetry` ยังมี exponential backoff + cap

รันเฉพาะไฟล์นี้ (ไม่รัน full suite ตามนโยบาย targeted-tests):
```
PYTHONPATH=<repo>/src python -m pytest tests/test_remote_pwa_liveness.py -v
```
ผล: **12 passed**. รันคู่กับ `test_remote_pwa_resume.py` / `test_remote_pwa_quick_reply.py` / `test_remote_pwa_pulse_roles.py` / `test_remote_pwa_scroll_pin.py` เพื่อเช็ค regression — ผ่านหมด

หมายเหตุ: venv shared install ชี้ไปที่ checkout อื่น (#202 — ห้าม `pip install -e .` ในนี้) → รันผ่าน `PYTHONPATH` override แทนตามนโยบาย ไม่ได้แก้ shared venv
