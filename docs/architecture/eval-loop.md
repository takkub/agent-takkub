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

`run` พิมพ์ต่อเคส: ผล (`pass`/`FAIL`/`TIMEOUT`), การเปลี่ยนจากรอบก่อน (`new`/`same`/`fixed`/`REGRESSED`),
revision, เวลา — `REGRESSED` (ผ่าน→ไม่ผ่าน) มี `<<< ผ่าน→ไม่ผ่าน` ท้ายบรรทัดและอยู่ในบรรทัดสรุป · exit code ≠ 0 ถ้ามีเคสไม่ผ่าน

## กติกา

- check **รันเฉพาะเมื่อมีคนสั่ง `takkub eval run`** — ไม่มี path อัตโนมัติ (เป็นคำสั่งที่เก็บในไฟล์)
- รันใน process ของ CLI (ไม่ใช่ GUI thread), ทีละเคส, มี timeout; timeout ฆ่าทั้ง tree ด้วย PID
  (`taskkill /PID /T` บน Windows, `killpg` บน POSIX) — ไม่ฆ่าด้วยชื่อ (#169)
- shell = shell ของเครื่อง (`cmd.exe` บน Windows, `/bin/sh` บน macOS) — **gap:** เคสที่เขียนด้วย syntax
  เฉพาะ shell ใด shell หนึ่งจะรันข้ามเครื่องไม่ได้ ให้ใช้คำสั่งที่เป็นกลาง เช่น `python -m pytest tests/x.py -k y`
- provider-neutral: ไม่แตะ pane/provider เลย

## Draft อัตโนมัติจาก fix-loop

เมื่อ verify role (`fix_loop.VERIFY_ROLES`) ส่ง done สะอาดและปิด sequence ที่เคยล้ม ≥1 รอบ
(`Orchestrator._fix_loop_close_on_verify`) → สร้างเคส `draft` (title จาก issue ref + ความล้มครั้งแรก,
`failures` = สรุปทุกรอบ, ไม่มี check) หนึ่งใบต่อ work identity · แจ้งผ่าน `events.log` (`eval_draft_created`)
เท่านั้น ไม่เด้งหา user (#464) · Lead เห็นใน `takkub eval list` แล้วเติม `takkub eval set <id> --check .. --cwd ..` → active

## ยังไม่อยู่ในขอบเขต

รัน agent ซ้ำเพื่อวัดผล, UI ใน cockpit/Remote, วัด false positive ของกฎ
