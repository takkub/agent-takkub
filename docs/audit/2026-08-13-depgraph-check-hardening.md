# Depgraph freshness check hardening — 2026-08-13

## ปัญหาที่พบ (qa)

`tools/gen_import_graph.py:39` เขียน `generated_by: f"grimp {version('grimp')}"` ลงใน
`docs/architecture/depgraph.json` ทุกครั้งที่ regenerate. `pyproject.toml` เดิม pin แค่
`grimp>=3.14` ไม่มี upper bound และ CI ไม่มี lockfile — ทุก job รัน `pip install -e .[dev]`
สดใหม่ทุกครั้ง

เดิม CI step "Depgraph freshness check" และ pre-commit hook `depgraph-fresh` ทั้งคู่ทำแบบ
เดียวกัน: `python tools/gen_import_graph.py` (เขียนไฟล์ทับ) แล้ว `git diff --exit-code`
เทียบกับ working tree ผลคือ:

1. **False positive จาก provenance field** — วันที่ PyPI ปล่อย grimp เวอร์ชันใหม่ (แม้แค่ patch
   bump ที่ import graph เหมือนเดิมทุกประการ) → `generated_by` เปลี่ยน → `git diff --exit-code`
   เจอ diff → step แดงพร้อมกันทั้ง `windows-latest`/`macos-latest`/`ubuntu` โดยไม่มี drift จริง
   ในกราฟเลย บล็อกทุก PR จนกว่าจะมีคน regenerate ไฟล์ด้วยมือ
2. **CRLF flicker** — วิธีเช็คผูกกับ `git diff` ซึ่งไวต่อ line-ending normalization
   (`core.autocrlf`) ทำให้ pre-commit hook กระพริบต่างกันข้าม OS

## Fix

### 1. `tools/gen_import_graph.py` — เพิ่มโหมด `--check`
- `build()` เดิมไม่เปลี่ยน (ยังเขียน `generated_by` ตามปกติตอน regenerate จริง)
- เพิ่ม `check()`: regenerate graph **ใน memory** แล้วเทียบกับไฟล์ที่ commit ไว้ (`json.loads`
  จาก disk) โดย **ไม่แตะ filesystem เลย** (ไม่เขียนไฟล์ ไม่พึ่ง `git diff`) → ตัด CRLF flicker
  ทิ้งไปเลย เพราะไม่มี git ในสมการอีกต่อไป
- `_diff()` เทียบเฉพาะ `modules[].{imports,imported_by}` (semantic content) — ข้าม
  `PROVENANCE_KEYS = {"generated_by"}` โดยตั้งใจ พร้อม print module/edge ที่ต่างกันจริง
  (`+`/`-` ต่อ edge) ให้อ่านรู้เรื่องว่า drift ตรงไหน ไม่ใช่แค่ exit 1 เฉยๆ
- `main()` เพิ่ม `argparse` แยก `--check` (exit 0/1 ตามผล) ออกจาก regenerate-and-write เดิม

### 2. `.github/workflows/ci.yml`
Step "Depgraph freshness check" เปลี่ยนจาก `python tools/gen_import_graph.py && git diff
--exit-code ...` เหลือคำสั่งเดียว: `python tools/gen_import_graph.py --check`

### 3. `.pre-commit-config.yaml` (`depgraph-fresh`)
Entry เปลี่ยนจาก `... python tools/gen_import_graph.py && git diff --exit-code ...` เป็น
`... python tools/gen_import_graph.py --check` — **code path เดียวกับ CI เป๊ะ** (เรียก
`--check` ตัวเดียวกัน) ไม่มี logic คู่ขนานที่เพี้ยนกันได้อีกต่อไป BIN/EXT probe (Windows
`Scripts/*.exe` vs macOS/Linux `bin/*`) และ PYTHONPATH-prepend สำหรับ linked worktree ยังคงไว้
ตามเดิม ไม่แตะ

### 4. `pyproject.toml`
`grimp>=3.14` → `grimp>=3.14,<4` — major bump เปลี่ยน graph semantics ได้จริง (ไม่ใช่แค่
provenance string) ต้องเป็นการอัปที่ตั้งใจ ไม่ใช่โดนลากไปเองจาก CI ที่ไม่มี lockfile

## Verify

| ทดสอบ | ผลลัพธ์ |
|---|---|
| `python tools/gen_import_graph.py --check` บน clean tree | exit 0, `depgraph is fresh (module_count=141)` |
| Inject fake edge เข้า `depgraph.json` (`agent_takkub.imports += "...__fake_drift_module__"`) แล้ว `--check` | exit 1, print `- agent_takkub imports: agent_takkub.__fake_drift_module__` ตรงจุดที่ต่าง แล้ว restore กลับ |
| แก้ `generated_by` เป็น `"grimp 9.9.9"` ชั่วคราวแล้ว `--check` | ยัง exit 0 (`is fresh`) — พิสูจน์ว่า provenance ไม่ทำให้ false-positive แล้ว restore กลับ |
| `pre-commit run depgraph-fresh --all-files` | `Passed` |
| `ruff check tools/gen_import_graph.py` | All checks passed |
| `ruff format --check tools/gen_import_graph.py` | 1 file already formatted |
| `git diff --stat docs/architecture/depgraph.json` หลัง restore | ว่าง (ไม่มี diff ค้าง) |

Full pytest suite ไม่ได้รันในรอบนี้ตามนโยบาย targeted-tests-mid-flight — qa จะรัน batch gate
ท้ายสุดเอง

## ไฟล์ที่แก้

- `tools/gen_import_graph.py`
- `.github/workflows/ci.yml`
- `.pre-commit-config.yaml`
- `pyproject.toml`
