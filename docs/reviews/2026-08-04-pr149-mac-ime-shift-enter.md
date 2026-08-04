# Code review — PR #149 "Fix/mac ime shift enter" (@than-aa)

- **Reviewer:** reviewer pane (Windows 11 · ไม่มีเครื่อง Mac)
- **วันที่:** 2026-08-04
- **Branch:** `pr-149-review` (fetch จาก `pull/149/head`) · commit เดียว `fb8115d`
- **Base:** `25a1be1` — ตามหลัง `main` (`24492a2`) 1 commit (ไม่ทับซ้อนไฟล์ → rebase ง่าย)
- **ไฟล์:** `src/agent_takkub/static/terminal.html` (+92/-26) · `src/agent_takkub/orchestrator_text.py` (+1/-1) · `tests/test_paste_payload.py` (+7)

> **วิธีตรวจ:** static trace ผ่านโค้ดจริง + bundle `src/agent_takkub/static/xterm.js` (282 KB, xterm.js v5 minified) ที่ pane ใช้จริง
> **ยังไม่ได้รัน:** ไม่ได้เปิด webview/QtWebEngine และไม่ได้ทดสอบบน macOS — ห้ามอ่านข้อสรุปด้านล่างว่า "ทดสอบแล้ว" ทุกข้อที่เป็น browser behaviour คือ code trace

---

## สรุปผู้บริหาร

**ต้องแก้ก่อน merge** — พบ **2 blocker** ที่กระทบผู้ใช้ทุก OS ไม่ใช่เฉพาะ Mac

| # | ประเด็น | Verdict | Severity |
|---|---|---|---|
| 1 | `_paste_payload` wrap multiline สั้น | **ไม่จริง (ไม่ใช่ปัญหา)** — เป็น bug fix ที่ถูกต้อง | ✅ APPROVE |
| 2 | บล็อก backquote ทุก OS | **จริง — พิมพ์ ` ไม่ได้ + ยังทำให้ textarea ปนเปื้อน** | 🔴 BLOCKER |
| 3 | CapsLock `return false` ทุก platform | **จริงบางส่วน** — input ไม่หาย แต่มี side-effect | 🟡 MEDIUM |
| 4 | register ก่อน QWebChannel boot | **ไม่จริงว่า regress** — แต่ comment เท็จ | 🟢 LOW (แก้ comment) |
| 5 | `setTimeout(flush, 5)` + heuristic 50 ms | **จริง — CJK IME พังใหม่ + fallback ตายหลัง Shift+Enter** | 🔴 BLOCKER |

---

## ประเด็นที่ 1 — `_paste_payload` wrap multiline สั้นทุกอัน

### Verdict: **ไม่ใช่ปัญหา — เป็นการแก้บั๊กจริง APPROVE**

`orchestrator_text.py:375`
```python
-    if len(text) < BRACKETED_PASTE_THRESHOLD:
+    if "\n" not in text and len(text) < BRACKETED_PASTE_THRESHOLD:
```

**หลักฐานว่าโค้ดเดิมผิด:** `lead_draft_state.py:76`
```python
_ENTER_BYTES = (0x0D, 0x0A)
```
โปรเจคนี้ยอมรับเองว่า **LF (0x0A) = ปุ่ม Enter/submit** ที่ระดับ TUI ส่วน `_sanitize_pane_text` (`orchestrator_text.py:141-146`) **จงใจไม่ strip `\n`** ("TAB and LF are preserved — both are intentional in multi-line task bodies")

ผลคือก่อน patch นี้ payload multiline ที่ **สั้นกว่า 200 ตัว** ถูก write ดิบลง PTY → `\n` ตัวแรกกลายเป็น submit → เนื้อความถูกตัดเป็นหลาย turn เคสที่เกิดจริง:
- `lead_inbox.py:1669` — `body = "\n\n".join(...)` รวม pending done-notice หลายอัน เช่น `"[qa done] ok\n\n[reviewer done] ok"` = 33 ตัวอักษร multiline → เดิมโดนตัด
- `lead_inbox.py:1465` (notify pump), `orchestrator.py:1437` (`takkub send`), `lead_inbox.py:690` (task delivery) — สั้น+multiline ได้ทั้งหมด

### ผลต่อ provider ทุกตัว — ไม่มี provider ไหนพัง

**หลักฐาน:** `_paste_payload` เป็น **global function ตัวเดียว ไม่อ่าน provider spec เลย** (grep `paste_threshold` → พบเฉพาะ `provider_spec.py` 6 จุด ที่เป็น declarative field ตั้ง `200` ทุกตัว **ยังไม่ wire เข้า `_paste_payload`** — Phase 0 ของ #103)

⇒ ทุก provider (claude / codex / gemini-agy / opencode / kimi / cursor) **ได้รับ bracketed paste อยู่แล้ววันนี้** สำหรับ task spec ทุกอัน (task spec จาก Lead ยาวเกิน 200 เสมอ) patch นี้แค่ขยายไปครอบ payload สั้น+multiline เท่านั้น → **ไม่ต้องการ capability ใหม่จาก provider ตัวไหน**

ถ้า provider ตัวไหนไม่รองรับ bracketed paste มันจะพังตั้งแต่ก่อน PR นี้แล้ว

### ready-marker / echo / self-heal ไม่ regress

`lead_inbox.py:421` เรียก `session.shows_pending_input(content_fragment)` → `pty_session.py:315`
```python
def _input_has_content(region, fragment):
    if _PASTED_PLACEHOLDER in region:   # "[pasted text"
        return True
    frag = fragment.strip().lower()[:24]
    return bool(frag) and frag in region
```
รองรับ **ทั้งสองรูป** — ถ้า claude ยุบเป็น `[Pasted text +2 lines]` ก็เจอ ถ้า render เป็นข้อความดิบก็เจอผ่าน fragment ⇒ repaste/resend logic (#22/#26/#79) ไม่ false-positive

### ผลข้างเคียงเดียวที่มีจริง (ยอมรับได้)

`_enter_delay_ms` (`orchestrator_text.py:540`) ตัดสินจาก `payload.startswith(_PASTE_START)` ⇒ multiline สั้นเปลี่ยนจาก `_TYPING_ENTER_DELAY_MS = 200` → `_PASTE_ENTER_DELAY_MS = 800`
**latency +600 ms เฉพาะ notice/peer-message สั้นที่มีหลายบรรทัด** — แลกกับการไม่โดนตัดข้อความ คุ้ม

### ตรวจ regression อื่น

- `spawn_engine.py:187` `_CURRENT_TASK_TRIGGER = "Start the current task from the one-shot system-prompt block now."` → **single-line** ⇒ comment ที่ `spawn_engine.py:1046` ("never bracket-pasted") **ยังจริง** ไม่ stale
- `IDLE_REMINDER_TEXT` (`orchestrator.py:487`) → implicit concat ไม่มี `\n` และยาวเกิน 200 อยู่แล้ว ⇒ ไม่เปลี่ยน
- รัน `tests/test_paste_payload.py` + `tests/test_orchestrator_notify_lead.py` บน main = **39 passed** ไม่มี test ไหน pin "multiline สั้น = raw" ⇒ ไม่มี test ที่ patch นี้ทำให้แดง
- test ที่ PR เพิ่ม (`test_short_multiline_message_is_wrapped`) ถูกต้อง แต่ **ยังขาด test เชิงเจตนา**: `_enter_delay_ms(_paste_payload("a\nb")) == _PASTE_ENTER_DELAY_MS` (pin ว่า delay เปลี่ยนโดยตั้งใจ)

---

## ประเด็นที่ 2 — บล็อก backquote ทุก OS 🔴 BLOCKER

### Verdict: **จริง และแย่กว่าที่ตั้งข้อสังเกต — ไม่มี path ไหนที่ ` ตัวเดียวหลุดเข้าไปได้ และมันยัง "ค้าง" ใน textarea**

`terminal.html:154-163`
```js
const isLanguageToggleKey = (
  ev.key === 'CapsLock' || ev.code === 'CapsLock' || ev.keyCode === 20 ||
  ev.keyCode === 192 || ev.code === 'Backquote' || (ev.key === '`' && !ev.shiftKey)
);
if (isLanguageToggleKey) { helperTextarea.value = ''; return false; }
```

### หลักฐานจาก bundle จริง — `return false` **ไม่ preventDefault**

`static/xterm.js` offset 41488 (`_keyDown`):
```js
_keyDown(e){ this._keyDownHandled=false; this._keyDownSeen=true;
  if(this._customKeyEventHandler && false===this._customKeyEventHandler(e)) return false;   // ← ไม่มี cancel()
```
offset 42978 (`_keyPress`):
```js
_keyPress(e){ this._keyPressHandled=false;
  if(this._keyDownHandled) return false;
  if(this._customKeyEventHandler && false===this._customKeyEventHandler(e)) return false;   // ← return ก่อน this.cancel(e)
  if(this.cancel(e), e.charCode) ...
```
**ปกติ** xterm กิน `keypress` ด้วย `this.cancel(e)` (= `preventDefault`) ⇒ ตัวอักษร **ไม่เคย** ถูก insert เข้า helper textarea แล้ว xterm emit เองผ่าน `triggerDataEvent`

**เมื่อ handler ของ PR return false** → `_keyPress` ออกก่อนถึง `cancel(e)` ⇒ **default action ทำงาน → `` ` `` ถูกใส่ลง `<textarea>`**

### แล้ว `_inputEvent` ของ xterm ไม่ช่วย

offset 43746:
```js
_inputEvent(e){
  if(e.data && "insertText"===e.inputType && (!e.composed || !this._keyDownSeen) && !screenReaderMode){ ... }
  return false;
}
```
คีย์บอร์ดจริง ⇒ `e.composed === true` **และ** `_keyDownSeen === true` ⇒ เงื่อนไข `(!e.composed || !this._keyDownSeen)` เป็น **false** ⇒ xterm ไม่ emit และ **ไม่ล้าง textarea**

### fallback ของ PR เองก็ทิ้งมันไว้

`terminal.html:174`
```js
if (!val || val.includes('\n') || val.includes('\r') || val === '`' || val === 'Dead') return;
```
`return` **ก่อน** ถึงบรรทัดล้างค่า (`terminal.html:188`) ⇒ `` ` `` **ค้างอยู่ใน textarea**

### ผลลัพธ์จริง (trace)

| กด | textarea ก่อน | ส่งเข้า PTY | textarea หลัง |
|---|---|---|---|
| `` ` `` ครั้งที่ 1 | `""` | **ไม่ส่งอะไรเลย** | `` "`" `` |
| `` ` `` ครั้งที่ 2 | `` "`" `` | **`` "``" `` (2 ตัวรวด)** | `""` (ล้างที่บรรทัด 188) |
| `` ` `` ครั้งที่ 3 | `""` | ไม่ส่ง | `` "`" `` |

⇒ พิมพ์ code fence ` ``` ` ได้ **2 ตัว** ไม่ใช่ 3 · พิมพ์ `` `cmd` `` inline ได้ผลไม่แน่นอน

### path ที่ ` ยังหลุดเข้าไปได้ (ตอบคำถามตรง)

| Path | หลุดไหม |
|---|---|
| พิมพ์ทีละตัว | ❌ ไม่หลุด |
| กด 2 ครั้งติด | ⚠️ หลุดเป็น `` `` `` พร้อมกัน 2 ตัว |
| Ctrl+V paste | ✅ หลุด — `handlePasteEvent` (offset 9434) เรียก `paste()` → `triggerDataEvent` ตรง ไม่ผ่าน keypress |
| orchestrator เขียนลง PTY (`assign`/`send`) | ✅ หลุด — Python เขียนตรง ข้าม xterm |
| หลัง Shift+Enter (textarea ค้าง `\n`) | ❌ ตันถาวรจนกด Enter จริง (ดูข้อ 5) |

### `~` (Shift+`) ก็โดนบล็อกด้วย

guard `(ev.key === '`' && !ev.shiftKey)` **ไม่มีผล** เพราะ `ev.keyCode === 192` (keydown) และ `ev.code === 'Backquote'` (keydown+keypress) เป็นจริงกับ Shift+` ด้วย ⇒ `~` ตกไป fallback path ซึ่ง **ส่งได้** แต่จะพ่วง `` ` `` ที่ค้างอยู่ออกไปด้วย (เช่นได้ `` `~ ``)

### บรรเทาบางส่วน (ไม่ใช่ทางแก้)

offset 42443 — `_keyDown` ล้าง textarea เมื่อ resolved key เป็น CR/ETX: `(i.key!==C0.ETX && i.key!==C0.CR)||(this.textarea.value="")` ⇒ **กด Enter ธรรมดา / Ctrl+C จะล้างขยะที่ค้าง** และ `_handleTextAreaBlur` (offset 26062) ก็ล้าง ⇒ ความเสียหายจำกัดอยู่ระหว่าง Enter สองครั้ง แต่ **ไม่ได้ทำให้พิมพ์ ` ได้**

### ทางแก้ที่แนะนำ

1. **แยก CapsLock ออกจาก Backquote** — ปุ่มสลับภาษา macOS คือ CapsLock (`keyCode 20`) และ Ctrl+Space ส่วน `keyCode 192`/`Backquote` เป็นปุ่มสลับภาษาเฉพาะบางเลย์เอาต์ (เช่น Windows TH/EN บางเครื่อง) — **ต้อง gate ด้วย platform**
2. ถ้าจำเป็นต้องบล็อก ให้ **`ev.preventDefault()` ด้วย** ก่อน `return false` เพื่อไม่ให้ค้างใน textarea
3. อย่าใส่ `val === '`'` ใน early-return ของ `flushTextareaInput` — ให้ตกไปเส้นทางส่งปกติ หรืออย่างน้อยล้าง textarea ก่อน return

---

## ประเด็นที่ 3 — CapsLock `return false` ทุก platform 🟡 MEDIUM

### Verdict: **จริงว่าไม่แยก platform แต่ "input หาย" ไม่จริง — เป็น side-effect ระดับกลาง**

CapsLock ไม่ผลิตข้อมูลอยู่แล้ว: `evaluateKeyboardEvent` ไม่คืน key สำหรับ `keyCode 20` ⇒ การ `return false` ที่ keydown **ไม่ได้ทำให้ตัวอักษรหาย** และ **ไม่ได้ห้าม OS toggle caps** (เพราะไม่ preventDefault ตามที่พิสูจน์ในข้อ 2) ⇒ Windows/Linux ยังใช้ CapsLock ปกติ

**สิ่งที่กระทบจริง 3 อย่าง:**

1. **ล้าง `helperTextarea.value` ทุกครั้งที่แตะ CapsLock** (`terminal.html:161`) — บน Windows/Linux ที่ CapsLock **ไม่ใช่** ปุ่มสลับภาษา นี่คือการทำลาย buffer โดยไม่มีเหตุผล ถ้ามี IME composition ค้างอยู่ (จีน/ญี่ปุ่น/เกาหลี) จะโดนล้างทิ้ง
2. **`_compositionHelper.keydown(e)` ไม่ถูกเรียก** — `_keyDown` (offset 41488) return ก่อนถึง `if(!t && !this._compositionHelper.keydown(e))` ⇒ composition helper ไม่เห็น keydown นี้
3. **`_keyUp` ถูกบล็อกด้วย** (offset 43261): `this.focus()` และ `updateCursorStyle(e)` ไม่ถูกเรียก ⇒ cursor style ไม่อัปเดตตาม CapsLock (คอสเมติก)

**แนะนำ:** gate ด้วย `navigator.platform`/`userAgent` — บล็อกเฉพาะ `darwin` และ **ไม่ล้าง textarea** เมื่อกำลัง compose อยู่

---

## ประเด็นที่ 4 — register ก่อน QWebChannel boot 🟢 LOW

### Verdict: **ไม่ regress — แต่ comment ที่เขียนไว้เท็จ ต้องแก้ถ้อยคำ**

**Guard ครบ** — ทุกจุดที่แตะ bridge มี null-check:
- `terminal.html:132` `if (bridge && bridge.sendInput)`
- `terminal.html:145` `if (!inputLocked && bridge && bridge.sendInput)`
- `terminal.html:182` `if (!inputLocked && bridge && bridge.sendInput)`

**ไม่มี TDZ:** `let bridge = null` อยู่บรรทัด 121 ก่อน `term.onData` (127) และ `attachCustomKeyEventHandler` (136) · closure ที่อ้าง `bridge` ก่อนหน้า (`terminal.html:71` WebLinks, `:106` `activate`) รันตอนคลิกเท่านั้น · `term.open()` (113) ไม่แตะ `bridge`

**แต่ comment ผิด** — `terminal.html:118-119`:
> "registered synchronously at term.open time so no keystrokes are missed during WebChannel boot"

**ไม่มีการ buffer ใดๆ** keystroke ช่วงก่อน `bridge` พร้อมยัง **หายเงียบเหมือนเดิม** (เดิมไม่มี `onData` handler → xterm ทิ้ง / ใหม่มี handler แต่ `bridge` null → ทิ้ง) เท่ากันทั้งคู่ ไม่แย่ลง แต่ก็ไม่ได้ดีขึ้นตามที่ comment อ้าง

ถ้าอยากให้ comment เป็นจริงต้อง queue ไว้:
```js
const pending = [];
term.onData(d => { if (inputLocked) return;
  if (bridge && bridge.sendInput) bridge.sendInput(d); else pending.push(d); });
// ใน QWebChannel callback: pending.splice(0).forEach(d => bridge.sendInput(d));
```

**ผลข้างเคียงเล็ก:** `onData` ตอนนี้จับ device report ของ xterm ด้วย (`_reportFocus` ยิง `ESC[I`/`ESC[O`, offset 44805) ⇒ `lastEmittedText`/`lastEmittedTime` ถูกปนด้วยข้อมูลที่ไม่ใช่ keystroke ทำให้ heuristic 50 ms ในข้อ 5 เพี้ยนได้

---

## ประเด็นที่ 5 — `setTimeout(flush, 5)` + heuristic 50 ms 🔴 BLOCKER

### Verdict: **จริง — พบ 2 เคสที่พังชัดเจน**

#### 5a. พิมพ์ ASCII ปกติ — **ปลอดภัย ไม่ double-send** ✅

keydown → keypress → `this.cancel(e)` preventDefault ⇒ ตัวอักษร **ไม่เข้า textarea** ⇒ `beforeinput`/`input` **ไม่ยิง** ⇒ `flushTextareaInput` ไม่ถูกเรียกเลย

#### 5b. CJK IME composition — **พังใหม่** 🔴

`terminal.html:193-198`
```js
helperTextarea.addEventListener('beforeinput', (e) => {
  if (e.data) { pendingCharText = e.data; setTimeout(flushTextareaInput, 5); }
});
```
ตอน compose ภาษาจีน/ญี่ปุ่น/เกาหลี `beforeinput` ยิงด้วย `inputType === 'insertCompositionText'` และ **`e.data` มีค่า** (ข้อความที่กำลัง compose)

`_inputEvent` ของ xterm ไม่จับเคสนี้ (`"insertText"===e.inputType` เป็น false) ⇒ ไม่ emit, ไม่ล้าง textarea

5 ms ต่อมา `flushTextareaInput` ทำงาน:
- `val` = ข้อความ compose ระหว่างทาง เช่น `に`
- ผ่าน guard ทุกตัว (ไม่มี `\n`, ไม่ใช่ `` ` ``, ไม่ใช่ `Dead`)
- `emittedByXterm` = false (xterm ยังไม่ emit)
- ⇒ **`bridge.sendInput('に')` ส่งข้อความที่ยัง compose ไม่เสร็จเข้า PTY**
- ⇒ **`helperTextarea.value = ''` ล้าง buffer กลาง composition**

พิมพ์ต่อ → `にほ` → `lastEmittedText('に').includes('にほ')` = false ⇒ ส่ง `にほ` ซ้ำอีก ⇒ PTY ได้ `にには...` เละ

และตอน `compositionend` xterm's `_finalizeComposition` (offset 67500 บริเวณ CompositionHelper) อ่าน `this._textarea.value.substring(start, end)` — ซึ่งถูกล้างไปแล้ว ⇒ ได้ string ว่าง

**ขาด guard ที่ควรมี:** ไม่มีการเช็ค `e.isComposing` หรือ flag จาก `compositionstart` เลย ทั้งที่ comment เขียนว่า "Non-CJK Fallback Guard" — เจตนาถูก แต่ **ไม่ได้ implement guard นั้นจริง**

**ทางแก้:**
```js
let composing = false;
helperTextarea.addEventListener('compositionstart', () => { composing = true; });
helperTextarea.addEventListener('compositionend', () => { composing = false; setTimeout(flushTextareaInput, 5); });
helperTextarea.addEventListener('beforeinput', (e) => {
  if (e.isComposing || composing) return;      // ← ที่ขาดไป
  if (e.data) { pendingCharText = e.data; setTimeout(flushTextareaInput, 5); }
});
```

#### 5c. Shift+Enter ทำให้ fallback ตายถาวร 🔴

`terminal.html:140-149` — บน keydown ล้าง textarea แล้วส่ง `\x1b\r` และ `return false` **แต่ไม่ preventDefault**
⇒ default action ของ `<textarea>` **แทรก `\n` ลงไปหลังจากที่เพิ่งล้าง** ⇒ `helperTextarea.value === '\n'`

`beforeinput` ของ Enter มี `e.data === null` ⇒ ไม่เข้า branch แรก แต่ listener `'input'` (`terminal.html:200`) ยิง flush อยู่ดี:
```js
if (!val || val.includes('\n') || ...) return;   // ← return โดยไม่ล้าง
```
⇒ **`'\n'` ค้างใน textarea ถาวร** ⇒ ทุก flush หลังจากนี้เจอ `\n` แล้ว return ทันที

**⇒ หลังกด Shift+Enter หนึ่งครั้ง fallback ทั้งชุด (ซึ่งคือ "ฟีเจอร์หลัก" ของ PR สำหรับ Mac IME) ตายจนกว่าจะกด Enter ธรรมดา** (ซึ่งจะไป trigger `textarea.value=""` ที่ offset 42443)

นี่คือฟีเจอร์ 2 อย่างใน PR เดียวกันที่ **ทำลายกันเอง** — Shift+Enter (สำหรับ multiline) ปิด Mac-IME fallback ซึ่งเป็นสองอาการที่ผู้ใช้ Mac คนเดียวกันเจอ

**ทางแก้:** เรียก `ev.preventDefault()` ใน branch Shift+Enter และเปลี่ยน early-return ให้ล้าง textarea ก่อน return

#### 5d. Ctrl+Enter / Cmd+Enter ถูก remap ด้วย — scope creep 🟡

`terminal.html:139` `if (isEnter && (ev.shiftKey || ev.altKey || ev.ctrlKey || ev.metaKey))`

เดิม xterm ส่ง `\r` (submit) ให้ทุก modifier+Enter ตอนนี้กลายเป็น `\x1b\r` ทั้งหมด ⇒ **เปลี่ยนพฤติกรรมของ Ctrl+Enter และ Cmd+Enter ในทุก pane ทุก provider** ทั้งที่ PR ตั้งใจแก้แค่ Shift+Enter

**ประเด็น multi-provider (ตาม directive ใน CLAUDE.md):** `terminal.html` เป็นไฟล์เดียวที่ทุก pane ใช้ **ไม่มี provider awareness เลย** — `\x1b\r` (ESC ตามด้วย CR) ถูกต้องสำหรับ Ink (claude, gemini-agy) แต่ **codex ใช้ ratatui ซึ่งตีความ ESC เป็น interrupt/clear composer** ⇒ Shift+Enter บน pane codex อาจกลายเป็น "ล้างช่องพิมพ์แล้ว submit บรรทัดว่าง" แทนที่จะขึ้นบรรทัดใหม่ · opencode (bubbletea) / kimi / cursor ยังไม่มีข้อมูล

**ผมไม่ได้ทดสอบข้อนี้** (ต้องเปิด pane จริงต่อแต่ละ provider) — แต่มันคือ multi-provider gap ที่ต้องตอบก่อน merge ตามกฎของโปรเจค

#### 5e. `emittedByXterm` heuristic เปราะ 🟡

`terminal.html:180`
```js
const emittedByXterm = (lastEmittedText === capturedVal || lastEmittedText.includes(capturedVal)) && (Date.now() - lastEmittedTime <= 50);
```
- `lastEmittedText.includes(capturedVal)` — `capturedVal` ตัวเดียว (เช่น `'a'`) จะ match กับ output ก่อนหน้าที่ยาวกว่าและมี `'a'` อยู่ **เป็น false-positive ที่ทำให้ตัวอักษรที่ควรส่งถูกกลืน**
- `Date.now()` วัด wall-clock — ถ้า Qt event loop backlog (ปัญหาเดิมของโปรเจคนี้ ดู #133 ใน `lead_inbox.py:312-320`) 50 ms หมดไปโดยที่ยังไม่มีอะไรเกิด ⇒ ตัดสินผิด
- `lastEmittedTime` ถูกปนโดย device report ของ xterm (ดูข้อ 4)

โปรเจคนี้มี precedent ตรงกันเป๊ะ (`_delayed_enter_verified` เลิกเชื่อ timing แล้วหันไปใช้ structural signal) — heuristic แบบนี้จะกลายเป็นบั๊ก flaky ในภายหลัง

---

## ประเด็นเพิ่มเติมที่พบระหว่าง review

- **`terminal.html:142,160,170` — `document.querySelector('.xterm-helper-textarea')` 3 ที่** ควรใช้ตัวแปรเดียวที่ผูกไว้แล้วบรรทัด 170 (บรรทัด 142/160 อยู่ใน handler ที่รันหลัง 170 เสมอ) — ลดโค้ดและกัน drift
- **`terminal.html:172` `function flushTextareaInput()` ประกาศใน block** — พึ่ง Annex B semantics ทำงานได้บน Chromium แต่ควรเป็น `const flushTextareaInput = () => {...}`
- **ไม่มี test ครอบ JS เลย** — ทั้ง repo ไม่มี test harness ฝั่ง `terminal.html` ⇒ logic ~90 บรรทัดที่แตะ input path ของทุก pane เข้ามาโดยไม่มี regression net ใดๆ อย่างน้อยควรมี smoke test ที่ qa เปิด pane แล้วพิมพ์จริง
- **branch ตามหลัง main 1 commit** (`24492a2`) — rebase ก่อน merge

---

## บทสรุป: **ต้องแก้ก่อน merge** (ไม่ใช่ merge ได้)

### ต้องแก้ (blocking)

1. 🔴 **backquote** — เอา `keyCode 192` / `ev.code === 'Backquote'` / `` (ev.key === '`') `` ออกจาก `isLanguageToggleKey` หรือ gate ด้วย platform + `preventDefault()` และเอา `val === '`'` ออกจาก early-return · **ตอนนี้พิมพ์ ` ตัวเดียวไม่ได้ทุก OS และกด 2 ครั้งได้ 2 ตัวรวด**
2. 🔴 **CJK IME** — เพิ่ม `compositionstart`/`compositionend` flag + เช็ค `e.isComposing` ใน `beforeinput`/`input` ก่อน flush
3. 🔴 **Shift+Enter ทิ้ง `\n` ค้าง** — `ev.preventDefault()` ใน branch Shift+Enter และให้ early-return ของ `flushTextareaInput` ล้าง textarea ก่อน return
4. 🟡 **multi-provider** — ตอบให้ได้ว่า `\x1b\r` ปลอดภัยกับ codex / opencode / kimi / cursor หรือไม่ ถ้าไม่ ต้องมี gate (ตาม directive multi-provider ของโปรเจค)
5. 🟡 **จำกัด scope ของ modifier** — Ctrl+Enter / Cmd+Enter ไม่ควรถูก remap ถ้า PR ตั้งใจแก้แค่ Shift+Enter

### ควรแก้ (non-blocking)

6. CapsLock gate ด้วย platform (macOS เท่านั้น) และไม่ล้าง textarea ระหว่าง compose
7. แก้ comment `terminal.html:118-119` ที่อ้างว่า buffer keystroke ระหว่าง boot (ไม่จริง) หรือ implement queue ให้จริง
8. เอา `lastEmittedText.includes(capturedVal)` ออก (false-positive กลืนตัวอักษร) และเลิกพึ่ง wall-clock 50 ms
9. รวม `document.querySelector('.xterm-helper-textarea')` 3 ที่เป็นตัวเดียว
10. เพิ่ม test `_enter_delay_ms(_paste_payload("a\nb")) == _PASTE_ENTER_DELAY_MS` (pin latency ที่เปลี่ยนโดยตั้งใจ)

### ต้องให้ผู้ส่ง PR ยืนยันบน Mac ก่อน

เครื่องที่ review นี้เป็น **Windows 11** — ทุกข้อข้างบนคือ code trace บน `xterm.js` bundle จริง **ไม่ได้รัน webview และไม่ได้ทดสอบบน macOS** สิ่งที่ต้องยืนยันบน Mac จริง:

- อาการเดิม (Thai/IME ตัวอักษรหายบน Mac) เกิดจริงและ patch นี้แก้ได้จริง — และ **path ไหน** (Cocoa NSTextInput ผ่าน `beforeinput`?) เพราะถ้าตัวอักษรถูก xterm cancel ที่ keypress อยู่แล้ว fallback นี้จะไม่ถูกเรียกเลย
- Shift+Enter บน Mac ได้ newline จริงหลังแก้ (และยังทำงานหลังจากแก้ blocker ข้อ 3)
- ทดสอบพิมพ์ `` ` `` และ code fence ` ``` ` บน **ทั้ง Mac และ Windows** หลังแก้ blocker ข้อ 1
- ทดสอบ CJK IME (ญี่ปุ่น/จีน) อย่างน้อย 1 ภาษาหลังแก้ blocker ข้อ 2

### ข้อดีที่ควรเก็บไว้

- **การแก้ `_paste_payload` (ข้อ 1) ถูกต้องและควร merge** — เป็น bug fix จริงที่แยกออกมา merge ก่อนได้เลย ปลอดภัยกับทุก provider ไม่ต้องรอฝั่ง JS
- แนวคิด fallback ผ่าน helper textarea สมเหตุสมผล — แค่ยังขาด composition guard และ preventDefault
- `ev.keyCode !== 229 && ev.key !== 'Process'` ใน `isEnter` เป็นการกัน IME ที่ถูกต้อง

**ข้อเสนอ:** แยก PR — ส่วน Python (`orchestrator_text.py` + test) merge ได้ทันที · ส่วน `terminal.html` ให้แก้ blocker 1-3 แล้วรีวิวรอบสอง + ให้ qa เทสจริงบน Windows และผู้ส่ง PR เทสบน Mac
