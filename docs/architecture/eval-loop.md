# Eval loop — ความผิดพลาดจริง → eval case → รันซ้ำ (MVP)

ที่มา: ข้อเสนออันดับ 1 จาก Dex Horthy "12 Rules" ข้อ 10 (eval ทุกอย่าง เรียนจากงานเก่า).
fix-loop ledger (`docs/lead/fix-loop.md`) จำว่างานไหนล้ม, Skill Learning จำบทเรียน —
eval loop จำ **วิธีตรวจที่รันซ้ำได้** ของเคสที่เคยพลาด แล้วเทียบผลก่อน/หลัง

## เคส

`src/agent_takkub/eval_cases.py` (leaf — import-linter `eval-cases-leaf`)

| field | ความหมาย |
|---|---|
| `id` | `ev-001`, `ev-002`, … ต่อโปรเจค |
| `title` / `expected` | เรื่องที่เคยพลาด / พฤติกรรมที่ต้องเป็น |
| `source` | `backlog` / `issue` / `fix_loop` (identity ของ sequence) |
| `check` + `cwd` + `timeout_s` | คำสั่ง shell — **exit 0 = ผ่าน** อย่างอื่น/timeout = ไม่ผ่าน |
| `created`, `status` | `draft` (ยังไม่มี check) · `active` · `retired` (เก็บไว้ ไม่รัน ไม่ลบ) |
| `last_result` | ผลรอบล่าสุด `{passed, exit, timed_out, rev, at}` ใช้เทียบรอบถัดไป |

ที่เก็บ (storage V2, 1 domain 1 โฟลเดอร์, ไม่ลบ): `RUNTIME_DIR/eval/<project>/cases.json` +
`runs.jsonl` (append ทุกรอบ: เวลา, ผลทุกเคส, `git describe --always --dirty` ของ cwd, output ท้าย 1500 ตัวอักษร)

## CLI (Lead/terminal เท่านั้น)

```
takkub eval add "<title>" --check "<cmd>" [--expected ..] [--cwd ..] [--timeout 300] [--backlog ID] [--issue #N] [--fix-loop KEY]
takkub eval list [--all] · show <id> · set <id> [--check ..] [--cwd ..] [--expected ..] · retire <id>
takkub eval run [--id ev-001 ...]
```

`run` พิมพ์ **id + cwd + check ของทุกเคสก่อนเริ่มรัน** และบรรทัด `REFUSED <id>: <เหตุ>` ของเคสที่ไม่ยอมรัน
(draft — ขาด check/cwd, retired, check ว่าง, cwd อยู่นอกโปรเจค) แล้วพิมพ์ต่อเคส: ผล (`pass`/`FAIL`/`TIMEOUT`), การเปลี่ยนจากรอบก่อน (`new`/`same`/`fixed`/`REGRESSED`),
revision, เวลา — `REGRESSED` (ผ่าน→ไม่ผ่าน) มี `<<< ผ่าน→ไม่ผ่าน` ท้ายบรรทัดและอยู่ในบรรทัดสรุป · exit code ≠ 0 ถ้ามีเคสไม่ผ่าน **หรือถูกปฏิเสธ**
(รวม `--id` ที่ชี้ draft/retired — ไม่ข้ามเงียบ)

## กติกา

- check **รันเฉพาะเมื่อมีคนสั่ง `takkub eval run`** — ไม่มี path อัตโนมัติ (เป็นคำสั่งที่เก็บในไฟล์)
- รันใน process ของ CLI (ไม่ใช่ GUI thread), ทีละเคส, มี timeout; timeout ฆ่าทั้ง tree ด้วย PID
  (`taskkill /PID /T` บน Windows, `killpg` บน POSIX) — ไม่ฆ่าด้วยชื่อ (#169)
- check ว่าง (หลัง strip) ใส่ไม่ได้ทั้ง `add`/`set` และถ้าไฟล์ถูกแก้ให้ว่าง `run` ถือว่าไม่ผ่าน/ปฏิเสธ
  (`cmd.exe /c ""` = exit 0 = ผ่านปลอม)
- process ของ check ไม่รอด `eval run` ที่ตาย: Ctrl+C / exception ใดๆ → ฆ่าทั้ง tree แล้วโยนต่อ ·
  Windows: shell อยู่ใน Job Object `KILL_ON_JOB_CLOSE` (`job_object_manager`, ctypes ไม่มี dependency) ⇒
  `eval run` โดน TerminateProcess (Bash tool timeout) → OS ฆ่า tree ให้ · **gap:** ผูก job หลัง shell เริ่มแล้ว
  หลานที่เกิดในเสี้ยววินาทีแรกอาจหลุด · macOS: Ctrl+C ฆ่า tree ได้ แต่ `kill -9` ที่ตัว `eval run` ทิ้ง check ค้าง
  (`start_new_session` แยก process group)
- **timeout รวมอาจเกิน 2 นาที** (ต่อเคส default 300s × จำนวนเคส) — Lead เรียก `takkub eval run` ผ่าน Bash tool
  ต้องตั้ง `timeout` ≥ ผลรวม `timeout_s` ของเคส หรือใช้ `run_in_background` ไม่งั้น tool ฆ่ากลางทาง (ไม่บันทึกผล)
- `cases.json` เขียนได้จากหลาย process (GUI ร่าง draft + CLI) → read-modify-write ครอบด้วย lock ข้าม process
  (`usage_shared._file_lock`, ไฟล์ `cases.lock` แบบ O_EXCL ใช้ได้ทั้ง Windows/macOS) · จับ lock ไม่ได้ใน ~2s → error
  ให้ลองใหม่ ไม่เขียนทับ
- output ของ check: ลอง UTF-8 ก่อน ไม่ใช่ → Windows ใช้ OEM code page ของเครื่อง (`GetOEMCP`, เช่น cp874)
- shell = shell ของเครื่อง (`cmd.exe` บน Windows, `/bin/sh` บน macOS) — **gap:** เคสที่เขียนด้วย syntax
  เฉพาะ shell ใด shell หนึ่งจะรันข้ามเครื่องไม่ได้ ให้ใช้คำสั่งที่เป็นกลาง เช่น `python -m pytest tests/x.py -k y`
- provider-neutral: ไม่แตะ pane/provider เลย

## ความปลอดภัย: check คือคำสั่ง shell ที่ Lead รันทีหลัง

`cases.json` เป็นไฟล์ธรรมดา — pane ใดก็ Write ได้ตรงๆ และ Lead-only ของ `takkub eval` เป็นแค่ env gate
(`TAKKUB_ROLE`) จึง**ถือว่าเนื้อหาในไฟล์ไม่น่าเชื่อถือ** สิ่งที่กันไว้:

- **กันได้:** check ที่ cwd อยู่นอก path ที่ลงทะเบียนของโปรเจค (`projects.json` → `paths`, resolve symlink/junction
  ก่อนเทียบ) ถูกปฏิเสธทั้งตอน `add`/`set` และตอน `run` · โปรเจคที่ไม่มี path ลงทะเบียน = ปฏิเสธทุกเคส ·
  `run` พิมพ์ check+cwd ทุกเคส**ก่อน**รัน ทำให้คำสั่งที่ถูกสอดเข้ามาโผล่ใน output ที่ Lead อ่าน
- **กันไม่ได้:** คำสั่งอันตรายที่ cwd อยู่ในโปรเจค (`git push --force`, `git reset --hard`, ลบไฟล์) ยังรันได้ —
  cwd จำกัดแค่ "รันที่ไหน" ไม่ได้จำกัด "ทำอะไร" และ output ถูกพิมพ์ก่อนก็จริง แต่ Lead ผ่าน Bash tool
  เห็นมันพร้อมผลหลังรันเสร็จ (ไม่มีจังหวะยืนยัน) · คำสั่งเหล่านี้ข้าม `pane_guard`/permission gate ของ Lead
  เพราะ hook เห็นแค่ `takkub eval run` ⇒ **ก่อนสั่ง `run` ทั้งชุด ให้ Lead `takkub eval list`/`show` ดู check ของ
  เคสที่ตัวเองไม่ได้ใส่เอง** (โดยเฉพาะเคสที่ไม่ได้มาจาก `eval add`/`set` ของ Lead)

## Draft อัตโนมัติจาก fix-loop

เมื่อ verify role (`fix_loop.VERIFY_ROLES`) ส่ง done สะอาดและปิด sequence ที่เคยล้ม ≥1 รอบ
(`Orchestrator._fix_loop_close_on_verify`) → สร้างเคส `draft` (title จาก issue ref + ความล้มครั้งแรก,
`failures` = สรุปทุกรอบ, ไม่มี check) หนึ่งใบต่อ work identity · แจ้งผ่าน `events.log` (`eval_draft_created`)
เท่านั้น ไม่เด้งหา user (#464) · Lead เห็นใน `takkub eval list` แล้วเติม `takkub eval set <id> --check .. --cwd ..` → active

## ยังไม่อยู่ในขอบเขต

รัน agent ซ้ำเพื่อวัดผล, UI ใน cockpit/Remote, วัด false positive ของกฎ
