# #252 (frontend half) — PWA "Online" chip lied about connectivity in both directions

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

## #252 follow-up — comment ผิด: cockpit เองก็ตอบ 5xx ได้จริง

Lead review รอบสองพบว่าคอมเมนต์ในโค้ด (fix #1 ด้านบน) ที่เขียนว่า "cockpit เอง (`http_server.py`) ไม่เคยตอบ 5xx" **ไม่จริง** — พิสูจน์แล้วจากโค้ด `remote/http_server.py:602-611` (`_respond_marshaled`): ถ้า Qt main thread ไม่ตอบภายใน `_BRIDGE_TIMEOUT_SEC` (8 วิ) จะส่ง `504 {"ok": false, "msg": "orchestrator did not respond"}` เป็น `Content-Type: application/json` ของ cockpit เอง (โค้ดเดียวกันซ้ำใน `_issue_sse_ticket` บรรทัด 613-631)

**ผลของบั๊กเดิม:** เคส "cockpit ติดต่อได้จริงแต่ Qt thread ค้าง" (504 JSON) จะถูกตีความเป็น edge/tunnel outage เหมือน 530 จาก Cloudflare — chip เด้ง Offline พร้อม `CONN_ERROR_MSG` ("tunnel ล่ม หรือ cockpit ปิด remote อยู่") ซึ่งผิดคนละสาเหตุ = liveness signal โกหกอีกทางหนึ่ง

### Fix (#252)

`apiFetch` แยก 5xx ออกเป็น 2 เส้นทางตาม `Content-Type` ก่อนตัดสินใจ Offline:

1. `Content-Type: application/json` → cockpit ตอบเอง (bridge timeout) → **ไม่** `setOffline(true)`, **ไม่** ใช้ `cockpit_unreachable`/`CONN_ERROR_MSG` — throw `Error("cockpit_unresponsive")` แทน, chip ยังคง Online
2. Content-Type อื่น (text/plain, text/html, หรือ parse ไม่ได้) → edge/tunnel outage เหมือนเดิม → `setOffline(true)` + `cockpit_unreachable`

เพิ่ม helper คู่ใหม่ข้าง `isConnError`/`CONN_ERROR_MSG`: `isBridgeTimeoutError(err)` + `BRIDGE_TIMEOUT_MSG` ("cockpit ตอบช้าหรือไม่ตอบสนอง ลองใหม่อีกครั้ง" — ไม่มีคำว่า tunnel) ใช้เป็นสาขาที่ 3 ในทุก call site ที่เดิมมี `isConnError(err) ? CONN_ERROR_MSG : ...` (`loadProjects`, verify-password, `openProject`, `closeProject`, `sendLeadMessage`, `sendLeadImage`, `confirmResume`)

แก้คอมเมนต์เดิมใน `apiFetch` ให้ตรงความจริง อ้างอิง `http_server.py:602-611` ตรงๆ แทนการเคลมว่า cockpit ไม่เคยตอบ 5xx

### ไม่แตะ (#252)

- `src/agent_takkub/remote/*.py` ยังคงไม่แตะ (backend ทำคู่ขนานอยู่ ยังไม่ merge)
- 404/403 auth path เหมือนเดิมเป๊ะ

## fix-loop รอบ 3 — Lead review รอบ 2 พบ 2 จุด

### บั๊ก: "chip stays Online" ยังไม่จริง

สาขา `Content-Type: application/json` (bridge timeout) เดิม `throw new Error("cockpit_unresponsive")` **ก่อน** ถึงบรรทัด `setOffline(false)` เดิมที่อยู่ถัดลงไปนอก `if` block — เท่ากับสาขานี้ไม่เคยเรียก `setOffline` เลย มันแค่ "คงค่า `isOffline` เดิมไว้เฉยๆ" ไม่ใช่ "ยืนยันว่า Online" ตามที่ comment/doc เคลม

เคสที่พัง: มือถือเจอ edge error 530 ก่อน (chip → Offline + banner ขึ้น) แล้ว cockpit กลับมาแต่ Qt thread ค้าง ตอบ 504 JSON — เราคุยกับ cockpit ได้จริงแล้ว (504 JSON คือหลักฐาน) แต่ chip/banner ยังค้าง Offline = โกหกอีกทางหนึ่ง (สลับด้านจากบั๊กเดิม แต่ effect เดียวกัน)

**Fix:** ย้าย `setOffline(false)` เข้าไปในสาขา `application/json` ก่อน `throw` — response นี้เป็นหลักฐานว่าเข้าถึง cockpit ได้จริง (ไม่ว่า `isOffline` ก่อนหน้าจะเป็นอะไร) จึงต้อง clear เสมอ ไม่ใช่แค่ครั้งแรก คอมเมนต์อัปเดตให้ตรง

### cleanup: nested ternary ซ้ำ 6-7 จุด

`isConnError(err) ? CONN_ERROR_MSG : isBridgeTimeoutError(err) ? BRIDGE_TIMEOUT_MSG : "<fallback>"` เคย copy 6 จุด (password/projects/open/close/say/resume) + เคส `sendLeadImage` เป็น if-return แยก 2 บรรทัดที่ทำ logic เดียวกันแต่ fallback ใช้ `err.message` แทน string คงที่ — รวมทั้งหมดเป็น helper เดียว `errMsg(err, fallback)` (ข้าง `isBridgeTimeoutError`) แล้วเรียกใช้ทุกจุด รวม `sendLeadImage` (`errMsg(err, err instanceof Error && err.message ? err.message : "ส่งรูปไม่สำเร็จ")` — พฤติกรรม/ข้อความเดิมทุกประการ เปลี่ยนแค่โครงสร้าง) — ES5 เดิม ไม่มี arrow function/template literal

## Tests

`tests/test_remote_pwa_liveness.py` (17 tests หลัง fix-loop รอบ 3, source-level structural check ตามแพทเทิร์นเดียวกับ `test_remote_pwa_*` อื่นๆ — ไม่มี JS runtime ใน suite นี้):

- `apiFetch` เช็ค `res.status >= 500` ก่อน `setOffline(false)` (unconditional, ตัวหลัง `cockpit_unreachable` throw) เสมอ (ordering)
- 5xx ที่ `Content-Type: application/json` → throw `cockpit_unresponsive` ก่อนถึงสาขา `setOffline(true)`/`cockpit_unreachable` เดิม (ordering เช็คว่า cockpit-answered path ไม่ตกไปโดน edge-outage path)
- **(รอบ 3 ใหม่)** สาขา `application/json` ต้องเรียก `setOffline(false)` **ก่อน** `throw new Error("cockpit_unresponsive")` — regression guard ตรงบั๊กที่เพิ่งแก้ (`test_5xx_with_our_json_content_type_clears_offline_before_throw`)
- 404/hadToken → `forgetToken()` และ 403 → `showPasswordPrompt()` ยังอยู่ครบ (regression guard)
- `isConnError`/`CONN_ERROR_MSG` ถูก define และมีคำว่า cockpit/tunnel ในข้อความ
- `isBridgeTimeoutError`/`BRIDGE_TIMEOUT_MSG` ถูก define, ข้อความต่างจาก `CONN_ERROR_MSG` และไม่มีคำว่า tunnel
- ทุก call site (`loadProjects`, verify-password, `openProject`, `closeProject`, `sendLeadMessage`, `sendLeadImage`, `confirmResume`) เรียกผ่าน `errMsg(err, ...)` (**รอบ 3 ใหม่** — เดิมเช็ค `isConnError`/`isBridgeTimeoutError` ตรงๆ ที่ call site, ตอนนี้ dedup เข้า helper กลาง)
- **(รอบ 3 ใหม่)** `errMsg(err, fallback)` ถูก define ข้าง `isBridgeTimeoutError`, เช็ค `isConnError` ก่อน `isBridgeTimeoutError` เสมอ, มี `return fallback` (`TestErrMsgHelper`)
- `scheduleEsRetry` ยังมี exponential backoff + cap

รันเฉพาะไฟล์นี้ (ไม่รัน full suite ตามนโยบาย targeted-tests):
```
PYTHONPATH=<repo>/src python -m pytest tests/test_remote_pwa_liveness.py -v
```
ผล: **17 passed**. รันคู่กับ `test_remote_pwa_resume.py` / `test_remote_pwa_quick_reply.py` / `test_remote_pwa_pulse_roles.py` / `test_remote_pwa_scroll_pin.py` เพื่อเช็ค regression — ผ่านหมด

หมายเหตุ: venv shared install ชี้ไปที่ checkout อื่น (#202 — ห้าม `pip install -e .` ในนี้) → รันผ่าน `PYTHONPATH` override แทนตามนโยบาย ไม่ได้แก้ shared venv
