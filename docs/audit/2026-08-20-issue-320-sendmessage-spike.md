# Issue #320 spike — native SendMessage/ListAgents/@mention/notify_when_idle บน Windows

**Scope (re-scope 2026-08-20):** spike report เท่านั้น — ไม่แตะ delivery pipeline จริง ปิด issue เมื่อ probe ครบ + go/no-go + design sketch ครบตามนี้ ถ้า go จริง ให้เปิด issue ใหม่แยกสำหรับ implement (ลูกของ conversation V2)

**Environment:** Claude Code `2.1.237` · Windows 11 · `C:\Users\monch\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe` (314.9 MB pkg'd binary) · probe รันจาก backend pane นี้เอง (`backend-2-1787199199-d3`, `kind: "interactive"`, spawn โดย takkub cockpit ผ่าน ConPTY paste) ขณะที่ Lead (`agent-takkub-a6`) และ backend-5 pane อีกตัวกำลังทำงานจริงพร้อมกัน — จึงได้ทดสอบ "2+ claude session ใน ConPTY pane context" ตามที่ acceptance ต้องการ โดยไม่ต้องปลอมสภาพแวดล้อม

## สรุปสั้น (go/no-go)

**No-go** — สำหรับ "fast-path ที่ทับ/เสริม PTY-paste pipeline เดิมแบบเนียน (transparent optimization layer)" ตามที่ตั้งคำถามในใบนี้

เหตุผลหลัก **ไม่ใช่** "Windows ไม่รองรับ" (ยืนยันด้วยหลักฐานด้านล่างว่าฟีเจอร์พวกนี้ compile/run และมี code path จริงบน Windows build นี้) แต่เป็นข้อจำกัดเชิงสถาปัตยกรรมของตัวฟีเจอร์เอง ซึ่งน่าจะเหมือนกันทุก OS: **`SendMessage`/`ListAgents`-equivalent ใน Claude Code 2.1.237 addressable เฉพาะ agent ที่ session ปัจจุบัน spawn เอง (ผ่าน Task tool หรือ `claude --bg`) เท่านั้น** — ไม่ใช่ registry แบบเปิดที่ session อิสระใดๆ บนเครื่องเดียวกันคุยกันได้ตามชื่อ ในขณะที่ takkub spawn ทุก pane เป็น **sibling process อิสระ** ผ่าน external orchestrator + ConPTY paste (ไม่ใช่ parent→child dispatch ในความหมายที่ SendMessage เข้าใจ) ต่อให้ Lead รู้ชื่อ session ของ teammate (`claude agents --json` เห็นจริง) ก็ยังส่งข้อความหาไม่ได้

รายละเอียด + evidence เต็มด้านล่าง

---

## ตาราง works / ไม่ works / inconclusive

| Feature | ผล | หลักฐาน | หมายเหตุ |
|---|---|---|---|
| `claude agents --json` (CLI-level, cross-process discovery) | **✅ works บน Windows** | เห็น Lead (`agent-takkub-a6`, pid 7536), backend-5 (pid 21076), และตัวเอง (pid 21072) ครบทั้ง 3 process พร้อม `sessionId`/`name`/`status`/`cwd` — evidence #1 | นี่คือ CLI subcommand ไม่ใช่ model tool — ไม่มีวิธีเรียกจากใน turn ของโมเดลตรงๆ ต้องผ่าน Bash |
| `SendMessage` tool → session อื่นที่**กำลังรันอยู่จริง** โดยใช้ชื่อจาก `claude agents --json` | **❌ ไม่ works (cross-process, ไม่ใช่ quota)** | ส่งหา `"lead"` → `No agent named 'lead' is reachable` · ส่งหาชื่อจริง `"agent-takkub-a6"` (Lead, status busy ขณะนั้น) → error เดิม — evidence #2, #3 | error message ระบุตรงๆ ว่า "use the agent ID **from a background agent's spawn result**" → ยืนยันว่า reachable-set คือ agent ที่ตัวเอง dispatch เท่านั้น ไม่ใช่ machine-wide registry |
| `SendMessage` tool → agent ที่ตัวเอง dispatch จริง (`claude --bg --name probe320-bg`) | **⚠️ inconclusive** — ไม่ใช่ quota ไม่ใช่ Windows-gate | bg agent boot ไม่สำเร็จเพราะ `API Error: Unable to connect to API: Self-signed certificate detected` (local corporate-proxy cert บนเครื่อง dev นี้ — เห็นได้จาก `ANTHROPIC_BASE_URL=http://127.0.0.1:20128/v1` ใน `~/.claude/settings.json`) ก่อนจะ bind inbox ได้ → SendMessage ไปหามันก็ยัง "not reachable" — evidence #4 | สภาพแวดล้อมกวน ไม่ใช่ evidence เชิงลบต่อ Windows หรือ quota — ต้อง retest วันที่ proxy/cert เครื่องนี้ไม่กวน ถ้าต้องการปิด gap นี้จริง |
| `SendMessage` tool → `"main"` | ✅ works (แต่ trivial) | `"You are the main conversation — \"main\" addresses you."` — evidence #5 | ยืนยันแค่ tool มี identity model ของตัวเอง ไม่เกี่ยวกับ cross-pane |
| `notify_when_idle` | **✅ ฟีเจอร์มีจริง มี code path** แต่ยังไม่ได้ live-test เพราะต้อง reachable agent ก่อน (บล็อกโดยแถวบน) | พบ string ตรงๆ ในไบนารี: `"notify_when_idle is only supported for Claude sessions on this machine in this release (not teammate…"` + frame schema (`notifyWhenIdleFrameSchema`, `cross_session_notify_idle`) — evidence #6 | ข้อความยืนยันว่า scope คือ **"sessions on this machine"** ไม่ใช่ macOS/Linux-only — Windows ไม่ถูก exclude โดย wording นี้ |
| `crossSessionInbound` setting (`hold`/`refuse`/opt-out) | **✅ มีจริง**, default ปัจจุบัน = ไม่ได้ set ในทั้ง global (`~/.claude/settings.json`) และ project settings → ใช้ default ภายใน ซึ่งจาก string ในไบนารีคือพฤติกรรม **"hold"** (เก็บ log ไว้ ไม่ inject เข้า context ทันที) | `"this session holds ALL inbound peer traffic (crossSessionInbound: hold), so it will only be logged here, not delivered to you. Carry on…"` — evidence #6 | ตอบคำถามใน acceptance ข้อ 3 โดยตรง — ดู "Interaction กับของเดิม" ด้านล่าง |
| `dialogExpiry` setting | ✅ มีจริง (ชื่อ setting + คำอธิบาย "Dialog expiry" อยู่ใน settings schema ที่ฝังในไบนารี) | evidence #6 | ยังไม่ trigger เพราะไม่มี dialog เกิดขึ้นจริง (reachable-set ว่างเปล่าตามแถวบน) |
| `@mention` (พิมพ์ `@session-name` ใน prompt แล้ว autocomplete/route) | **⚠️ ไม่ได้ทดสอบตรง (inconclusive)** | ไม่พบ `--mention`/CLI flag ใดๆ ใน `claude --help` (สแกนครบ 242 บรรทัด) และไม่พบ string อ้างอิงเฉพาะใน `sdk-tools.d.ts` — เป็น TUI input-box UX feature ล้วนๆ ต้อง keystroke ในจอ interactive จริง ซึ่ง harness นี้ (Bash/Task-based) ไม่มีทางพิมพ์ `@` ใส่ prompt-box แล้วเห็น autocomplete render ได้ | โดย design น่าจะเป็นแค่ UI sugar เรียก delivery layer เดียวกับ SendMessage → สืบ (infer) ว่าจะเจอ reachable-set restriction เดียวกัน แต่**ไม่ได้พิสูจน์ตรง** ต้องมีคนกดคีย์บอร์ดจริงในเทอร์มินัล Windows ถึงจะปิด gap นี้ได้ |
| `ListAgents` (model tool ชื่อนี้ตรงๆ) | ไม่พบเป็น tool แยก | `ToolSearch("select:SendMessage,ListAgents")` และคำค้นอื่นๆ คืนแค่ `SendMessage` ไม่มี `ListAgents` เลย | สิ่งที่ทำหน้าที่เทียบเท่าคือ CLI `claude agents --json` (ไม่ใช่ model tool) — ถ้า SDK เวอร์ชันอื่น/role อื่นมี `ListAgents` เป็น tool จริงๆ ยังไม่ยืนยัน (ToolSearch หา keyword ตรงๆ ไม่เจอในเซสชันนี้) |

---

## หลักฐาน (evidence, ย่อ — full transcript อยู่ใน session log ของ pane นี้)

**#1 — `claude agents --json`** (รันจาก Bash ใน pane นี้ ระหว่างที่ Lead + backend-5 กำลัง busy จริง):
```json
[
  {"pid":7536,"cwd":"...\\agent-takkub","kind":"interactive","name":"agent-takkub-a6","status":"idle"},
  {"pid":21076,"cwd":"...\\backend-5-1787198022","kind":"interactive","name":"backend-5-1787198022-ff","status":"busy"},
  {"pid":21072,"cwd":"...\\backend-2-1787199199","kind":"interactive","name":"backend-2-1787199199-d3","status":"busy"}
]
```
→ ยืนยัน cross-process discovery ทำงานจริงบน Windows ConPTY pane ของ cockpit

**#2** — `SendMessage({to:"lead", ...})` → `{"success":false,"message":"No agent named 'lead' is reachable.\nCheck the spelling, or use the agent ID from a background agent's spawn result."}`

**#3** — `SendMessage({to:"agent-takkub-a6", ...})` (ชื่อจริงจาก evidence #1, Lead ที่กำลังรันอยู่จริง) → error เดิมทุกตัวอักษร

**#4** — spawn `claude --bg --dangerously-skip-permissions --name probe320-bg "..."` → `claude agents --json` เห็น entry ใหม่ `{"id":"5ae37198","kind":"background","status":"idle","state":"blocked"}` → `claude logs 5ae37198` โชว์ `API Error: Unable to connect to API: Self-signed certificate detected. Check your proxy or corporate SSL certificates` → `SendMessage({to:"probe320-bg"})` ยัง `not reachable` เหมือนเดิม → หยุด agent ด้วย `claude stop 5ae37198` (cleanup แล้ว)

**#5** — `SendMessage({to:"main", ...})` → `{"success":false,"message":"You are the main conversation — \"main\" addresses you. Send to a named agent instead."}`

**#6** — string ที่ดึงจากไบนารี `claude.exe` โดยตรง (`Select-String` ข้าม node_modules wrapper, อ่านจาก compiled bundle จริง ไม่ใช่เดา):
```
notify_when_idle is only supported for Claude sessions on this machine in this release (not teammate...
this session holds ALL inbound peer traffic (crossSessionInbound: hold), so it will only be logged
  here, not delivered to you. Carry on; do not p[ause]... [read] its reply instead.
notify_when_idle: this session already holds ... pending idle subs[cription]
[uds-messaging] Routed user message to queue (priority=...)
[uds-messaging] peer_message_status dropped: no outstanding send matches orig_msg_id=(none)
peer_message_status: held | denied | expired | delivered
notifyWhenIdleFrameSchema / peerIdleNoticeFrameSchema / notePeerIdleStatus
settings keys ที่อยู่ใน schema เดียวกัน: dialogExpiry, crossSessionInbound, askUserQuestionTimeout,
  autoUploadSessions, inputNeededNotifEnab[led], companyAnnouncements, daemonColdStart
```
→ ยืนยันว่าเป็น protocol จริง มี state machine (`held/denied/expired/delivered`) ไม่ใช่ placeholder ที่ไม่ได้ implement — และไม่มี string ไหนบอกว่า "unsupported on win32" ตรงๆ (ต่างจากที่ควรเห็นถ้าเป็น platform gate — เทียบกับ string อื่นในไบนารีที่ gate ด้วย `win32`/`darwin`/`linux` ตรงๆ ซึ่งเจอเยอะมากในโค้ด Node runtime แต่ **ไม่เจอคู่กับ uds-messaging/crossSessionInbound เลยสักจุด**)

`~/.claude/settings.json` และ project `.claude/settings.json`/`settings.local.json` **ไม่มี** key `crossSessionInbound`/`dialogExpiry` ตั้งไว้เลย → เครื่องนี้ใช้ default ภายใน (`hold`)

---

## Interaction กับของเดิม (acceptance ข้อ 3)

- **pane_token auth** (`provider_spec.py`, `pane_guard.py`, `cli_server.py`) — SendMessage เป็น mechanism คนละชั้นกับ pane_token ทั้งหมด: pane_token คุม "takkub CLI คุยกับ pane ผ่าน PTY ได้ไหม", SendMessage คุม "session A ส่งข้อความหา session B ในความหมายของ Claude Code เองได้ไหม" — ไม่มี overlap โดยตรง แต่ถ้าจะเอามาใช้จริง ต้องมี mapping ชั้นใหม่ (`pane role name ↔ claude session name/agentId`) ซึ่งยังไม่มีใครเก็บอยู่ตอนนี้
- **done-gate / digest ordering** (`lead_inbox.py`, `orchestrator.py`) — ไม่กระทบ เพราะ SendMessage ใช้ไม่ได้กับ topology ปัจจุบันอยู่แล้ว (ดูสรุป go/no-go) จึงไม่มี race/ordering ใหม่ให้ปวดหัว
- **crossSessionInbound เมื่อ pane ใช้ bypassed permissions** — ตอบตรงคำถามในใบนี้: **ข้อความ "จะโดน hold" จริง** ไม่ใช่ auto-approve และไม่ใช่ auto-deliver — ค่า default คือ `hold` (เก็บ log แต่ไม่ inject เข้า context จนกว่าจะมีคน/กลไก unhold) การ bypass permissions (`--dangerously-skip-permissions`) เป็นคนละ setting กับ `crossSessionInbound` และไม่ได้ปลด hold ให้อัตโนมัติจากที่เห็นในโค้ด — แปลว่าต่อให้แก้ topology ให้ reachable ได้ ก็ยังต้องตัดสินใจเรื่อง `crossSessionInbound` policy ต่อ (เปิด opt-out ทุก pane เอง มีความเสี่ยง เพราะ inbound message จาก session อื่นจะแทรกเข้า conversation โดยไม่ผ่าน busy-deliver 5s ของ takkub เลย — เป็นช่อง "prompt injection ข้าม pane" ใหม่ที่ pipeline เดิมไม่มี เพราะ PTY paste ทุกอันมาจาก orchestrator ที่ trusted อยู่แล้ว)

---

## Multi-provider (#103)

ฟีเจอร์นี้ผูกกับ Claude Code binary โดยตรง (`claude agents`, `SendMessage` tool, `crossSessionInbound` setting) — **claude-only ชัดเจน**, ไม่มีทางเทียบเท่าใน codex/gemini-agy/opencode/kimi/cursor CLI ที่ตรวจสอบมา ถ้า implement ในอนาคตต้องเป็น **optimization layer ที่ถอดได้เสมอ** (fallback = PTY paste เดิม 100% สำหรับ non-claude pane ทั้งหมด และสำหรับ claude pane เองก็ fallback เสมอเมื่อ reachable-set ไม่ครอบคลุม) — ไม่ใช่ dependency ของ delivery pipeline

---

## Design sketch (deferred — เป็น input ของ conversation V2, ไม่ใช่แผน implement ตอนนี้)

เนื่องจากผล spike คือ **no-go สำหรับตอนนี้** ส่วนนี้จึงเป็นแค่ sketch ของสิ่งที่ *ต้องเป็นจริงก่อน* ถ้าจะกลับมาเปิด go ในอนาคต ไม่ใช่ architecture ที่พร้อม implement

### เงื่อนไขที่ต้องจริงก่อนถึงจะ go ได้
1. **Spawn topology ต้องเปลี่ยนจาก sibling-process ไปเป็น parent-dispatch** สำหรับคู่ pane ที่อยากใช้ fast-path — เช่น ให้ Lead session เอง (ไม่ใช่ external orchestrator) เป็นคนยิง `claude --bg --name <role>` เพื่อ dispatch teammate โดยตรง แทนที่ takkub cockpit (Python/Qt process ภายนอก) จะ spawn ConPTY แล้ว paste เข้าไปเหมือนตอนนี้ — นี่คือการเปลี่ยนสถาปัตยกรรมระดับ spawn_engine.py ใหม่ทั้งหมด ไม่ใช่ "เสริม layer" เล็กๆ
2. **retest evidence #4 ให้จบ** (ต้องมี environment ที่ proxy/cert ไม่กวน) เพื่อพิสูจน์ parent→bg-child SendMessage ใช้ได้จริงบน Windows ก่อนลงทุนออกแบบต่อ
3. ตัดสินใจ policy `crossSessionInbound` ต่อ pane แบบ explicit (ไม่ใช้ default `hold` เงียบๆ) — มิฉะนั้นข้อความ native จะไม่ถูก deliver จริง กลายเป็นแค่ log ที่ไม่มีใครอ่าน

### ถ้าเงื่อนไขข้างบนครบ — ทิศทางที่เข้ากับ V2 conversation layer (`core/models/conversation.py`)
- `Message.source` มี field นี้อยู่แล้ว (default `"live_pty"`) — จุดต่อที่สะอาดที่สุดคือเพิ่มค่า `"native_sendmessage"` เป็นอีก source แทนที่จะสร้าง side-channel คู่ขนานที่ V2 ไม่รู้จัก (ตรงกับที่ issue ขอไว้ใน "V2 alignment")
- `ProviderSessionBinding` (มี `provider_id`, `session_id` อยู่แล้ว) คือจุดที่ผูก capability flag ใหม่ เช่น `native_addressable: bool` — คำนวณจากการ cross-check `claude agents --json` (หรือ SDK เทียบเท่า) ว่า binding นี้อยู่ใน reachable-set ของ dispatcher จริงไหม ก่อนจะลองยิง SendMessage เลย
- fallback ต้อง**เสมอ**เป็น PTY paste เดิม ไม่มีเงื่อนไขไหนที่ทำให้ fallback หายไปได้ — ตรงตาม acceptance ของ #103

### สิ่งที่ไม่ต้องออกแบบต่อ (ปิดไปเลยจากผล spike นี้)
- ❌ ใช้ SendMessage เป็น fast-path ทับ topology ปัจจุบัน (sibling ConPTY panes) แบบไม่เปลี่ยน spawn model — พิสูจน์แล้วว่าใช้ไม่ได้ ไม่ใช่แค่ "ยังไม่ลอง"
- ❌ พึ่ง `@mention` เป็น entry point หลัก — เป็น TUI keystroke feature ล้วนๆ ทดสอบอัตโนมัติไม่ได้อยู่ดี ต่อให้ SendMessage ใช้ได้ก็ไม่ควรผูก UX หลักไว้กับมัน

---

## ข้อจำกัดของ spike นี้ (สิ่งที่ยังไม่ปิด)

- `@mention` TUI autocomplete/routing — ต้อง manual keystroke test ในเทอร์มินัล Windows จริง (ไม่ทำในนี้เพราะ harness เป็น headless/Bash-driven)
- parent→`--bg`-child SendMessage — confound ด้วย local self-signed-cert/proxy issue ของเครื่อง dev นี้ ยังไม่ได้ผลลัพธ์สะอาด (ไม่ใช่ quota, ไม่ใช่ Windows-gate — เป็น local network/cert setup)
- ไม่ได้ทดสอบข้าม **user account** หรือข้าม **machine** — สโคปทั้งหมดคือ same-machine same-user ตามที่ issue ระบุ
