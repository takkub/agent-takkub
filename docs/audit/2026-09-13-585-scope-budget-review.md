# รีวิว #585 "scope budget" (commit 7536b8fc) — จุดที่ทำให้ของเสียหลุดถึง user

**คำถามเดียว:** มีทางไหนบ้างที่การผ่อนการตรวจสอบตามขนาดงานทำให้ของเสียหลุด?
**คำตอบสั้น:** มี 2 จุด **สูงมาก** (F1, F2) ที่ probability ไม่ใช่ edge case แต่เป็น
พฤติกรรมปกติของระบบ/ภาษาธรรมชาติทั่วไป, อีก 3 จุด (F3–F5) เป็น compounding
risk ที่ทำให้ F1/F2 ไม่มีอะไรมาจับซ้ำ ทุกข้อพิสูจน์ด้วยการรันโค้ดจริงหรืออ่าน
call-site จริง ไม่ใช่การเดา — วิธีรันซ้ำอยู่ท้ายไฟล์

อ่านเฉพาะ diff ของ 7536b8fc ไม่พอ — ทุกข้อด้านล่างต้องอ่าน call site จริง
(cli.py / orchestrator.py) ที่ diff ไม่ได้แตะ เพื่อดูว่ากลไกใหม่ถูกเรียกยังไงจริง

---

## ตารางสรุป (เรียงตาม probability × impact)

| # | เรื่อง | Probability | Impact | ไฟล์:บรรทัด |
|---|---|---|---|---|
| F1 | Lead direct-edit ceiling **รีเซ็ตทุกครั้งที่ `takkub assign` สำเร็จ** — ไม่ใช่แค่ทุก 30 นาที | **สูงมาก** (เกิดทุก assign ปกติ) | **สูง** (แก้ source เองไม่จำกัดจริง ไม่มีเทส ไม่มี review) | `cli.py:884,928,987` |
| F2 | `task_scope.classify()` deep-keyword list ขาดคำอังกฤษ/ไทยทั่วไปของ trust-boundary (password/permission/session/otp/2fa/role/rate-limit/bypass) | **สูง** (คำพูดปกติ ไม่ใช่ adversarial) | **สูง** (auth/security ถูกตี tiny → ข้ามเทส+review+gate) | `task_scope.py:37-150`, `pane_guard.py:816-828` |
| F3 | busy-machine gate เป็น no-op ~83% ของเวลา เพราะ staleness cutoff (30s) สั้นกว่ารอบเขียน snapshot (180s) | กลาง-สูง (ทุก batch ที่ยิงขนาน) | กลาง (ปกป้องเครื่อง ไม่ใช่ปกป้องความถูกต้องของโค้ดโดยตรง — แต่ไม่มี retry บังคับเมื่อเจอ deny) | `pane_guard.py:737-739` vs `orchestrator.py:1023,9578-9592` |
| F4 | "qa-gate ผูกกับ scope สูงสุดของ batch" เป็น prose ล้วน — ไม่มีโค้ดคำนวณ "batch มี deep ไหม" จริง | กลาง | สูงเมื่อรวมกับ F2 (ทั้ง batch ไม่มี gate เลย) | `docs/qa-gate-policy.md:11-24`, `task_ledger.py:369-380` (มีแค่ per-role lookup) |
| F5 | `is_tiny_style_or_text_diff` เหมา `.json/.yaml/.yml/.csv/.tsv` เป็น "style/text" เหมือน `.png` → ข้าม screenshot evidence ได้ทั้งที่เป็นไฟล์ runtime อ่าน | กลาง | กลาง (ข้าม evidence gate เดียว ไม่ใช่ข้ามเทส) | `orchestrator_text.py:147-182` |

---

## F1 — Lead tiny-fix ceiling รีเซ็ตทุก assign (ตัวใหญ่สุด)

`evaluate_lead_direct_edit()` (`pane_guard.py:780-959`) จำกัด Lead แก้ source เอง
สะสมได้ไม่เกิน **2 ไฟล์ / 30 บรรทัดต่อ task** (rule 3-4, `pane_guard.py:920-942`)
โดยอ้างอิง state file `runtime/lead_edits/<project>.json` ที่ reset เมื่อ idle
เกิน 30 นาที (`pane_guard.py:897-909`) — ตรงนี้ตรงกับที่ระบุใน
`docs/lead/role-and-workflow.md:106` ("รีเซ็ตทุก 30 นาที **หรือเมื่อ assign**")

ปัญหาอยู่ที่คำว่า "หรือเมื่อ assign" — `reset_lead_edits()` ถูกเรียกจาก
**ทุก branch ที่ `cmd_assign` สำเร็จ**, ไม่ใช่แค่ SessionStart:

- `cli.py:884` — assign เดี่ยว (`resp.get("ok")` แล้ว reset ทันที)
- `cli.py:928` — assign แบบ shard fan-out
- `cli.py:987` — assign อีก branch (isolation worktree)
- `cli.py:4134` — **อันเดียวที่ถูกต้อง**: SessionStart hook

3 จุดแรกไม่เกี่ยวอะไรกับ Lead แก้โค้ดเองเลย — เป็นแค่ Lead มอบหมายงานให้
role อื่น (backend/frontend/qa/...) ซึ่งเป็น**การทำงานปกติที่เกิดหลายสิบครั้ง
ต่อ session**. ผลคือ: ทุกครั้งที่ Lead สั่ง `takkub assign` อะไรก็ตาม (ไม่ว่าจะ
เกี่ยวกับไฟล์ที่ Lead เพิ่งแก้เองหรือไม่) ตัวนับ 2-ไฟล์/30-บรรทัดจะ**กลับเป็น 0**

รูปแบบที่หลุดได้จริง: Lead แก้ไฟล์ A เอง 15 บรรทัด (ผ่าน, สะสม 1/2 ไฟล์
15/30 บรรทัด) → สั่ง `takkub assign --role qa "เช็คอะไรสักอย่าง"` (งานปกติ
ไม่เกี่ยวกับไฟล์ A) → ตัวนับรีเซ็ตเป็น 0/2, 0/30 ทันที → Lead แก้ไฟล์ A ต่ออีก
15 บรรทัด (รอบใหม่ "ผ่าน" อีกครั้งเพราะนับจาก 0) → ทำซ้ำแบบนี้ได้ไม่จำกัดรอบ
ตราบใดที่แต่ละ Edit call ≤15 บรรทัดและมี assign คั่นระหว่างนั้น (ซึ่ง Lead ทำอยู่
แล้วเป็นปกติ) — เพดานสะสมที่ตั้งใจ "กันไม่ให้ Lead แก้ทีละ 15 บรรทัดหลายรอบจน
กลายเป็นงานใหญ่" (คำพูดจาก commit message เอง) จึงแทบไม่เคยทำงานจริงในเซสชัน
ที่ Lead assign งานอื่นสลับไปด้วยตามปกติ — เหลือแค่ cap 15 บรรทัด/ครั้งเดียว
ที่ยังกันได้จริง

**หลักฐาน:** ไม่มีเทสไหนใน `tests/test_pane_guard.py` (`test_rolling_window_
resets_after_30_minutes`, `test_reset_lead_edits_function`, `test_deny_message_
informs_about_auto_reset_and_command` — ดู `tests/test_pane_guard.py:1190-1300`)
ที่ทดสอบ interaction ระหว่าง reset-on-assign กับการสะสมข้าม assign หลายครั้ง
— ทุกเทสทดสอบ `reset_lead_edits()` แบบแยกเดี่ยว ไม่ใช่ผ่าน `cmd_assign` จริง

**ข้อแก้ที่เล็กที่สุด:** ลบการเรียก `pane_guard.reset_lead_edits(...)` ออกจาก
3 จุดใน `cmd_assign` (`cli.py:884, 928, 987`) เก็บไว้แค่จุด SessionStart
(`cli.py:4134`) และคำสั่ง manual `takkub lead-edits --reset` — ถ้าเจตนาจริง
คือ "reset เมื่อ Lead เริ่ม task ใหม่" ต้อง key state ด้วย task/assignment id
ไม่ใช่ reset แบบไม่มีเงื่อนไขทุกครั้งที่ assign สำเร็จ ไม่ต้องเพิ่ม rule ใหม่ —
แก้ 3 บรรทัดที่มีอยู่แล้ว

---

## F2 — `task_scope.classify()` deep-keyword list ขาดคำ trust-boundary ทั่วไป

รันจริงกับ `task_scope.classify()` (สคริปต์อยู่ท้ายไฟล์, รันซ้ำได้) — 23 เคส
ใช้คำพูดปกติที่คนพิมพ์งานจริงๆ ไม่ใช่ adversarial phrasing:

| Scope ที่ได้ | ข้อความ | เหตุผลที่ได้ |
|---|---|---|
| **tiny** ⚠️ | แก้ password field นิดเดียว | explicit_small ('นิดเดียว') |
| **tiny** ⚠️ | change password validation, just 5 lines | line_budget ('5 lines') |
| **tiny** ⚠️ | แก้ admin privilege check นิดเดียว | explicit_small |
| **tiny** ⚠️ | แก้ role-based access, 1 จุด | pinpoint_spot |
| **tiny** ⚠️ | single spot fix: **skip signature verification** in dev | pinpoint_spot ('single spot') |
| **tiny** ⚠️ | แก้ 2FA bypass logic นิดเดียว | explicit_small |
| **tiny** ⚠️ | แก้ OTP validation logic 3 บรรทัด | line_budget |
| **tiny** ⚠️ | เปลี่ยนค่า config ตัวเดียว ปิด rate limit | single_config |
| **tiny** ⚠️ | change feature flag value, single config | single_config |
| normal | 1-line fix to permission check bypass | (ไม่มี signal ใดๆ ชน — ตกไป normal เฉยๆ ไม่ใช่ tiny แต่ก็ไม่ deep) |
| normal | fix session timeout, one line | เช่นกัน |
| deep ✅ | single config value: disable CSRF check for testing | 'CSRF' ชนจริง |
| deep ✅ | แก้ jwt expiry, นิดเดียว | 'jwt' ชนจริง |
| deep ✅ | one-line fix: remove input sanitize call | 'sanitize' ชนจริง |
| deep ✅ | bump lodash version, 1 line in package.json | 'package.json' ชนจริง |

(23 เคสเต็มอยู่ในสคริปต์ท้ายไฟล์ — ตารางนี้ตัดเฉพาะที่ให้ผลต่างที่ชัด)

**Root cause:** `_DEEP_PATTERNS` กลุ่ม "auth" (`task_scope.py:46-53`) มีแค่
`auth|authentication|authorization|oauth|jwt|login|logout|signin|signup` +
Thai `สิทธิ์|ล็อกอิน|ยืนยันตัวตน|ระบบสมาชิก|รหัสผ่าน` — **ไม่มี** `password`
(อังกฤษ), `permission`, `privilege`, `admin`, `role`, `session`, `otp`, `2fa`,
`mfa` เลยสักคำ และไม่มีกลุ่ม "rate limit" หรือ compound "bypass/skip/disable +
verify/validation/signature" แบบที่มีให้ deploy/dependency (`task_scope.py:88-
118` ใช้ compound pattern อยู่แล้ว เช่น deploy ต้องมี "deploy" + "prod" คู่กัน
— เทคนิคเดียวกันนี้ยังไม่ถูกเอามาใช้กับ "bypass check ความปลอดภัย")

ที่ร้ายแรงกว่าคือ **gap เดียวกันซ้ำในไฟล์ที่สอง**: `evaluate_lead_direct_edit()`
มี `deep_file_patterns` ของตัวเอง (`pane_guard.py:816-828`, เช็คกับ path ไม่ใช่
ข้อความ) — auth line ที่นั่น (`pane_guard.py:819`) มี `password` แล้ว
(`\b(?:auth|oauth|jwt|login|signup|password)\b`) แต่ก็ยังไม่มี `permission`,
`session`, `role`, `otp`, `2fa`, `rate limit` เหมือนกัน — ถ้า Lead แก้ไฟล์
`src/handlers/permission_check.py` เอง 12 บรรทัด scope=tiny (จาก classify()
ผิดเป็น tiny ตามตารางบน) จะ**ผ่านทั้งสองด่าน**พร้อมกัน (F1 ยิ่งซ้ำเติมเพราะ
เพดานสะสมก็รีเซ็ตทุก assign อยู่แล้ว)

ผลของการถูกตี tiny: ห้ามเขียนเทสใหม่ (budget_block "tiny", `task_scope.py:210-
214`), ข้าม screenshot ได้ถ้าเป็น UI (`ui_evidence_gate`), และ batch ที่มีแต่
tiny/normal **ไม่มี `qa-gate` เลย** (ดู F4)

**ข้อแก้ที่เล็กที่สุด:** เพิ่มคำในกลุ่ม "auth"/"security" ของ `_DEEP_PATTERNS`
(`task_scope.py:46-61`): `password|permission|privilege|\badmin\b|\brole\b|
\bsession\b|\botp\b|\b2fa\b|\bmfa\b|rate[_-]?limit` และเพิ่ม compound pattern
`bypass|skip|disable|remove` + `verif|valid|signature|sanitiz|check` แบบเดียว
กับที่ "deploy"/"dependency" ใช้อยู่แล้ว — ทำ 2 จุด (`task_scope.py` +
`pane_guard.py:819`) ให้ครบชุดเดียวกัน ไม่ต้อง rewrite อะไร เป็นการเพิ่ม token
ใน regex ที่มีอยู่แล้ว

---

## F3 — busy-machine gate เป็น no-op ~83% ของเวลา

`_is_machine_busy_from_snapshot()` (`pane_guard.py:727-777`) fail-open ทันทีถ้า
`last-session.json` เก่ากว่า **30 วินาที** (`pane_guard.py:737-739`,
`saved_at` check ซ้ำอีกชั้นที่ 744-751)

แต่ตัวเขียนไฟล์นี้ — `write_session_snapshot()` — ถูกเรียกจาก **แค่ 3 จุด**:
1. `main_window.py:1738` (graceful close)
2. `update_panel.py:1143` (ก่อน self-update)
3. `orchestrator.py:9590` ผ่าน `_maybe_write_periodic_snapshot()` ซึ่ง throttle
   ด้วย `_PERIODIC_SNAPSHOT_INTERVAL_S = 3 * 60` = **180 วินาที**
   (`orchestrator.py:1023`)

**ไม่มีจุดไหนเขียน snapshot ตอน pane state เปลี่ยนเป็น/ออกจาก "working"** —
`pane.state` ถูกอ่านสด ๆ จาก PTY output ทุก 5 วิ (watchdog tick,
`_check_idle_teammates` ที่เรียก `_maybe_write_periodic_snapshot` อยู่ในนั้น,
`orchestrator.py:9608-9615`) แต่การเขียนไฟล์ยังโดน throttle 180s เท่าเดิม

เลขคณิตตรงไปตรงมา: ไฟล์ fresh พอจะให้ผล "busy=True" ได้จริงแค่ **30 วินาที
แรกของทุกรอบ 180 วินาที** = **16.7%** ของเวลา ส่วนที่เหลือ **83.3%** ของเวลา
ไม่ว่าจะมี pane อื่นทำงานหนักอยู่จริงแค่ไหน ฟังก์ชันนี้ return `(False, "")`
เสมอ (fail-open ตาม `pane_guard.py:735` "return False, ''"), การป้องกันเครื่อง
ค้างจากการยิง build/test ขนานกัน (จุดประสงค์ตรงตาม commit message: "pane_guard:
tiny -> deny qa-gate/full suite; busy machine ... -> deny build/suite") จึงแทบ
ไม่เคยทำงานในสถานการณ์จริง ยกเว้นจังหวะบังเอิญที่ heavy command มาชนพอดีกับ
30 วินาทีแรกหลัง periodic write

**หมายเหตุสำคัญ:** นี่คือทิศตรงข้ามกับความกลัวในโจทย์รีวิว ("3 pane ทุกตัวเห็นว่า
เครื่องไม่ว่าง แล้วข้ามหมด") — ในทางปฏิบัติ scenario นั้น**ยากเกิดเพราะ gate
เองก็แทบไม่ทำงาน** ข่าวร้ายคือด้านตรงข้าม: เมื่อ gate เผอิญ trigger จริง (Verdict
False บล็อก Bash call เดียวนั้น) **ไม่มีกลไกบังคับให้ pane รอแล้วลองใหม่** —
ข้อความ deny บอกแค่ "ใช้ targeted test หรือรอให้เครื่องว่างก่อน" เป็น prose
คำแนะนำ ไม่ใช่ enforcement เดียวกับปัญหาที่ module docstring ของ pane_guard.py
เองบ่นเรื่อง browser_driver/git_lead_only ไว้ (prose อย่างเดียวไม่พอ) — pane ที่
โดน deny คำสั่งเดียวที่มันมีไว้ verify การแก้ (เช่น `next build` ของงานที่ต้อง
compile-check) สามารถแค่ข้ามคำสั่งนั้นแล้ว `takkub done` ต่อได้เลย เพราะไม่มี
gate ไหนเช็คว่า "verification จริงเกิดขึ้นก่อน done" นอกจาก UI screenshot gate
(#433) ซึ่งใช้กับ frontend/mobile เท่านั้น ไม่ครอบ backend/devops

**ข้อแก้ที่เล็กที่สุด:** เขียน snapshot (หรือแค่ list ของ working roles สั้นๆ)
ทันทีที่ set ของ working roles เปลี่ยนในแต่ละ watchdog tick (5s) — ไม่ต้องรอ
timer 180s แยก คือแก้ `_maybe_write_periodic_snapshot`/`_check_idle_teammates`
ให้เทียบ working-set ปัจจุบันกับ tick ก่อนหน้า แล้วเขียนทันทีถ้าต่างกัน (แทนที่
จะ throttle ด้วยเวลาเพียวๆ) — event-driven แทน time-driven ในจุดเดียว ไม่ต้อง
เพิ่ม timer ใหม่หรือ mechanism ใหม่

---

## F4 — "qa-gate ผูกกับ scope สูงสุดของ batch" ไม่มีโค้ดรองรับจริง

`docs/qa-gate-policy.md:11-24` (ของใหม่จาก #585) เขียนไว้ชัดว่า:
> Batch flow is tied to the **highest scope in the batch**: tiny/normal only →
> No qa-gate is run. contains deep tasks → qa runs `qa-gate --auto` once.

แต่ไม่มีฟังก์ชันไหนคำนวณ "scope สูงสุดของ batch ที่เปิดอยู่" จริง —
`task_ledger.py` (โดนแก้ในคอมมิตเดียวกันนี้) มีแค่ `get_open_scope(project,
role)` (`task_ledger.py:369-380`) ซึ่ง**อ่านทีละ role เดียว** ไม่มี
`get_all_open_scopes()`/`batch_has_deep()` หรืออะไรที่ aggregate ทั้ง batch
เลย — การตัดสินใจ "batch นี้มี deep ไหม เลย skip/ไม่ skip gate" จึงอยู่ที่
**ความจำของ Lead ล้วนๆ** ไม่มีโค้ดช่วยเช็ค (`pane_guard`'s tiny-scope block
เช็คได้แค่ต่อ pane เดียวว่าตัวเอง scope=tiny ห้ามรัน qa-gate เอง —
`pane_guard.py:1356-1376` — ไม่ได้บอก Lead ว่าทั้ง batch ควร gate หรือไม่)

รวมกับ F2: ถ้ามีงานที่ควรเป็น deep แต่ `classify()` ตีเป็น tiny (เช่นตัวอย่าง
"แก้ 2FA bypass logic นิดเดียว") Lead จะเห็น batch ทั้งหมดเป็น tiny/normal
ล้วน → ไม่มีอะไร trigger ให้คิดว่าต้องรัน gate → **ทั้ง batch ไม่มี automated
verification ชั้นสุดท้ายเลยสักครั้ง**

**ข้อแก้ที่เล็กที่สุด:** เพิ่ม `get_all_open_scopes(project) -> dict[role,
str]` ใน `task_ledger.py` (loop สั้นๆ บน `state.get("open", {})`, รูปแบบ
เดียวกับ `get_open_scope` ที่มีอยู่แล้ว) แล้วให้ `takkub qa-gate --auto` หรือ
คำสั่งจบ batch ของ Lead อ่านค่านี้มาเตือนถ้ามี "deep" ค้างอยู่แต่ยังไม่ gate —
ไม่ต้องเพิ่มกฎ prose ใหม่ ใช้ ledger ที่มีอยู่แล้วจาก commit นี้เอง

---

## F5 — `is_tiny_style_or_text_diff` เหมา `.json/.yaml/.csv` เป็น "style/text"

`_STYLE_OR_TEXT_EXTENSIONS` (`orchestrator_text.py:147-182`, **ของใหม่ทั้งชุด
จาก #585** — ยืนยันด้วย `git log -S` ว่าไม่เคยมีมาก่อน) ใส่ `.json`, `.yaml`,
`.yml`, `.csv`, `.tsv` ไว้ในกลุ่มเดียวกับ `.css`, `.svg`, `.png`, `.woff2` —
ทั้งที่ `.json`/`.yaml` มักเป็นไฟล์ config/data ที่ runtime อ่านจริง
(feature-flag json, i18n string json, permission/role config, docker-compose
ไม่เข้าเงื่อนไขนี้แต่ config อื่นเข้าได้) ไม่ใช่ asset ที่ "ดูด้วยตา" ได้แบบ CSS

ผลตอน `scope=tiny`: ถ้า diff ทั้งหมด ≤20 บรรทัด และไฟล์ที่แตะทุกไฟล์อยู่ใน list
นี้ (`ui_evidence_gate`, `orchestrator_text.py:336-348`) → **ไม่ต้องแนบ
screenshot เลย** แม้ task text จะ match UI hint (`หน้า/ฟอร์ม/component/...`)
ตรงกับที่โจทย์รีวิวกังวลไว้พอดี: "i18n ที่เปลี่ยนความหมาย" — เปลี่ยนคำแปลใน
`locales/th.json` (ผ่านทั้งเงื่อนไข extension และ dir-hint `/locales/`
`orchestrator_text.py:184-203`) 15 บรรทัด ที่เปลี่ยนความหมายข้อความยืนยัน
("ยืนยันการลบบัญชี" → ผิดความหมายบางอย่าง) จะไม่มีใครถูกบังคับให้เปิดหน้าจอ
ดูจริงเลยสักครั้ง ทั้งที่ task text บอกว่าเป็นงาน UI ("ฟอร์ม"/"หน้า") ชัดเจน

**ข้อแก้ที่เล็กที่สุด:** ตัด `.json`, `.yaml`, `.yml`, `.csv`, `.tsv` ออกจาก
`_STYLE_OR_TEXT_EXTENSIONS` — ถ้าจะยังยอมข้าม screenshot สำหรับไฟล์แปลจริง ๆ
ให้พึ่ง `_STYLE_OR_TEXT_DIR_HINTS` (`/locales/`, `/i18n/`, ...) ที่มีอยู่แล้ว
อย่างเดียวพอ (ไฟล์ json อยู่ใน `/locales/` ผ่าน dir-hint อยู่แล้วไม่ต้องพึ่ง
extension ซ้ำ) ส่วน json/yaml ที่ไม่ได้อยู่ใน dir พวกนี้ (config ทั่วไป) จะกลับ
ไปนับเป็น "ไม่ใช่ style/text" ตามเดิม (ต้องมี screenshot ถ้า task เป็น UI) —
ลบ 5 รายการจาก tuple เดียว ไม่ต้องเพิ่ม logic ใหม่

---

## จุดที่ไล่แล้ว "ไม่พบปัญหา" (เพื่อไม่ให้ดูเหมือนข้ามไป)

- **prose 5 ชั้น (role file 17 ใบ + common.md):** ไล่ grep คำว่า `#478` และ
  "ห้ามเขียนไฟล์เทส" ทั้ง 17 ไฟล์ agent + `docs/roles/common.md` — ถ้อยคำ
  section "🧪 กติกาวางเทส" ตรงกันทุกไฟล์ ไม่พบไฟล์ไหนหลงเหลือข้อบังคับ #478 เดิม
  (เขียนเทสทุกงาน) ที่ขัดกับกติกาใหม่
- **`docs/lead/role-and-workflow.md` token diet (12.6k→3.97k):** เช็คว่า 11
  ไฟล์ที่แยกออกมาทั้งหมด (`team-presets.md`, `worktree-isolation.md`, ...)
  มีอยู่จริงและไม่ว่าง (`test_lead_docs_guard.py::test_section_docs_exist_and_
  not_deleted` ครอบอยู่แล้ว) — Lead direct-edit policy section ที่เหลือใน core
  doc (`role-and-workflow.md:97-110`) พูดตรงกับโค้ดจริงทุกตัวเลข (15/2/30/30
  นาที) **รวมถึงพูดถึง "หรือเมื่อ assign" ตรงกับพฤติกรรมจริงด้วย** (นี่คือเหตุผล
  ที่ F1 ไม่ใช่ prose/code mismatch — เอกสารบอกความจริง แต่กลไกที่บอกไว้เอง
  ทำร้ายจุดประสงค์ของ carve-out)
- **fix-loop ceiling (`check_fix_loop_ceiling`, `task_scope.py:300-354`):**
  deep scope บังคับ `action="propose"` เสมอไม่ว่า attempt ที่เท่าไหร่ (ไม่มี
  auto-retry เล็ดลอดสำหรับงานเสี่ยง) — ตรวจ logic แล้วถูกต้อง ไม่พบช่องโหว่

---

## วิธีรันซ้ำ

```bash
# F2 — 23-case classify() probe (เขียนใหม่ได้จาก task_scope.classify ตรงๆ)
python -c "
import sys; sys.path.insert(0, 'src')
from agent_takkub.task_scope import classify
cases = ['แก้ password field นิดเดียว', 'change password validation, just 5 lines',
  'แก้ admin privilege check นิดเดียว', 'แก้ role-based access, 1 จุด',
  'single spot fix: skip signature verification in dev',
  'แก้ 2FA bypass logic นิดเดียว', 'แก้ OTP validation logic 3 บรรทัด',
  'เปลี่ยนค่า config ตัวเดียว ปิด rate limit']
for c in cases:
    d = classify(c); print(f'{d.scope:8s} | {c} -> {d.reason}')
"

# F1 — grep ยืนยัน 3 reset call site ใน cmd_assign vs 1 จุดที่ SessionStart
grep -n "reset_lead_edits(project=_from_project())" src/agent_takkub/cli.py

# F3 — เทียบ threshold ตรงๆ
grep -n "> 30.0" src/agent_takkub/pane_guard.py
grep -n "_PERIODIC_SNAPSHOT_INTERVAL_S" src/agent_takkub/orchestrator.py
```

ไม่ได้แก้โค้ด/เทส ไม่ได้รัน build/qa-gate ตามที่โจทย์สั่ง — ทุกข้อสรุปมาจาก
Read + grep + รัน `task_scope.classify()` เปล่า ๆ (pure stdlib, ไม่แตะ state
ของระบบจริง) เท่านั้น
