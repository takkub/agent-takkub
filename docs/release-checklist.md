# Release checklist (agent-takkub)

วิธี release จริงของ 1.0.10+ (manual, ไม่มี git tag, ไม่มี GitHub Release สำหรับเวอร์ชันเหล่านี้)
— ไม่ใช่ `takkub release` (ตัวนั้น parse heading `## [vNEXT]` ซึ่ง CHANGELOG.md ปัจจุบันไม่มีแล้ว)

## 0. Pre-flight — ต้องเขียวก่อนแตะเวอร์ชัน

```bash
python -m pytest -v            # full suite รวม installed-mode gate (tests/test_installed_mode_gate.py,
                                # tests/test_installed_cli_bin_integration.py) — ต้องรันจาก venv ที่มี
                                # `build` package (dev extra `.[dev]`) ไม่งั้น wheel-build fixture fail
python -m ruff check .
python -m ruff format --check .
lint-imports                    # import-linter contracts (pyproject.toml [tool.importlinter])
```

- **CI ต้องเขียวทั้ง 2 OS** (`windows-latest` + `macos-latest`, `.github/workflows/ci.yml`) — ถ้าเพิ่งพุช
  commit สุดท้าย รอ `gh run list` เขียวก่อนตัดเวอร์ชัน อย่า release ทับ CI ที่ยังไม่จบ
- **installed-mode gate** (`tests/test_installed_mode_gate.py`) proves the ACTUAL packaged behavior
  (DATA_HOME/ASSETS_ROOT/CLI_BIN_DIR/pane-env/CLI wiring) works when running from a real installed
  wheel — not just a dev checkout where `DATA_HOME == REPO_ROOT` masks installed-only bugs
  (the TAKKUB_PORT_FILE bug fixed in 8a06c52/c55c3e0 is exactly this bug class). ถ้าอันนี้แดง **ห้าม
  release** ต่อให้ pytest อื่นเขียวหมด — แปลว่า wheel ที่กำลังจะ ship พังตอนติดตั้งจริง
- `takkub doctor` มีหมวด `[installed]` (`check_installed_integrity`, Phase D) ที่รันเฉพาะตอน
  `DATA_HOME != REPO_ROOT` (installed build) — ใช้เช็ค production instance ตัวจริงบนเครื่อง user
  ได้ด้วย ไม่ใช่แค่ตอน release

## 1. ตัดสินเวอร์ชันใหม่ (SemVer)

ดู diff ตั้งแต่ release ก่อนหน้า (`git log <last-release-commit>..HEAD`) แล้วเลือก patch/minor/major
ตามหลัก SemVer ปกติ ปัจจุบัน (2026-07-05) อยู่ที่ **1.0.12** — เวอร์ชันถัดไปที่เป็น bugfix = **1.0.13**

## 2. เขียน CHANGELOG.md (ภาษาไทย)

**อย่าสร้าง heading เวอร์ชันใหม่** — CHANGELOG.md ใช้ `## [Unreleased]` เป็น heading เดียวถาวร
(ไม่มี `## [1.0.13]` ตั้งแต่ 1.0.10) เพิ่ม bullet ใหม่ใต้ section ที่ตรงกับประเภทงาน
(`### Fixed (แก้)`, `### Added (เพิ่ม)`, ฯลฯ) — commit message ต่างหากที่เป็นตัวบอกขอบเขต release

รูปแบบ bullet (ดูตัวอย่างจริงที่ commit c55c3e0):
```markdown
- **<หัวข้อสั้นเป็นภาษาไทย>** — <อาการที่ user เจอ> → root cause: <สาเหตุจริง> → แก้: <วิธีแก้ + ไฟล์/ฟังก์ชันหลัก>
  + N tests (`test_xxx.py`)
```

## 3. Bump เวอร์ชัน — 2 ไฟล์ ต้องตรงกันเป๊ะ

```bash
# pyproject.toml
version = "1.0.13"

# package.json
"version": "1.0.13",
```

ไม่มีไฟล์ที่ 3 ให้ sync — ไม่มี git tag, ไม่มี GitHub Release สำหรับเวอร์ชันเหล่านี้ (ยกเว้น v1.0.0)

## 4. Build wheel

```bash
rm -f dist/*.whl   # ลบของเก่าก่อนเสมอ — npm files:"dist/*.whl" ship ทุกไฟล์ที่เจอ และ
                    # npm/scripts/postinstall.js เลือกตัว sort ท้ายสุด (string sort ธรรมดา —
                    # "1.0.9" > "1.0.10" ตาม lexicographic order! ของเก่าค้างไว้ = เสี่ยง ship ผิดตัว)

# รันจาก cwd นอก repo เสมอ — python -m build สร้าง build/ staging dir ใน srcdir ระหว่างมันทำงาน;
# ถ้า cwd == srcdir และมี build/ ค้างจากรอบก่อน สภาพแวดล้อม shell เดียวกันที่ import agent_takkub
# ต่อจากนั้น (เช่น pytest/python -c ทันทีหลัง build) อาจ resolve เข้า build/lib/agent_takkub
# (module shadow) แทน editable install ตัวจริง — ไม่ใช่ปัญหาของ `python -m build` เอง (PEP 517
# ใช้ isolated build backend) แต่เป็นปัญหาของ *shell state หลัง build* ในรอบ manual release
cd /some/other/dir
python -m build --wheel --outdir /path/to/agent-takkub/dist /path/to/agent-takkub
cd /path/to/agent-takkub

ls dist/*.whl   # ต้องมีไฟล์เดียว ชื่อมีเวอร์ชันใหม่ตรงกับที่ bump
```

> หมายเหตุ: `tests/test_installed_mode_gate.py` และ `tests/test_installed_cli_bin_integration.py`
> เรียก `python -m build` แบบเดียวกันนี้จาก **pytest subprocess** (cwd = repo root ก็ได้ ไม่พัง) —
> เพราะเป็น subprocess ใหม่ทั้งกระบวนการ ไม่มี shell state ให้ contaminate คำเตือนข้างบนใช้กับ
> **manual release flow ในเทอร์มินัลเดียวกัน** เท่านั้น

## 5. Commit (ไฟล์ที่แตะ: CHANGELOG.md, pyproject.toml, package.json — ไม่มี dist/, gitignored)

```bash
git add CHANGELOG.md pyproject.toml package.json
git commit -m "chore(release): 1.0.13 — <หัวข้อสั้น>"
```

ไม่ต้อง `git tag` — เลิกทำตั้งแต่ 1.0.10

## 6. Push + รอ CI เขียว 2 OS

```bash
git push origin main
gh run list --branch main --limit 3   # รอ 4 job เขียวหมด:
                                       #   lint-and-test (windows-latest, macos-latest)
                                       #   installed-gate (windows-latest, macos-latest)
```

**ห้าม publish ก่อน CI เขียว** — ถ้า `installed-gate` แดงโดยเฉพาะ (แม้ `lint-and-test` เขียว) แปลว่า
wheel ที่กำลังจะ ship พังตอนติดตั้งจริงบน OS นั้น ต่อให้ pre-flight ในเครื่องตัวเองเขียวไปแล้วก็ตาม
(env drift ระหว่างเครื่อง — `installed-gate` proves the packaged artifact, not just the dev checkout)

## 7. npm publish

```bash
npm whoami   # ต้อง login แล้ว (บัญชีที่ตั้ง Bypass 2FA ไว้ — ไม่งั้น publish ตรงๆ โดน E403)
npm publish
```

ถ้า `npm whoami` ไม่ผ่านหรือ publish โดน E403: สร้าง granular access token ใหม่ (Read/write ·
All packages · ติ๊ก **Bypass two-factor authentication**) แล้ว publish แบบ one-shot:
```bash
npm publish --userconfig /tmp/temp-npmrc   # ลบไฟล์ temp-npmrc ทิ้งทันทีหลัง publish — อย่า set ถาวร
```

## 8. Verify

```bash
npm view agent-takkub version   # ต้องตรงกับเวอร์ชันที่เพิ่ง publish (npm registry sync มีดีเลย์สั้นๆ)
```

เปิด `takkub doctor` บนเครื่องที่ติดตั้งจริง (หรือรัน `npm install -g agent-takkub` ในเครื่องทดสอบ) —
เช็คหมวด `[installed]` เขียวหมด (assets-claude-md, assets-role-files, cli-bin, runtime-writable)

---

## Reference: known gotchas (จาก release ที่ผ่านมา)

- **npm 2FA**: บัญชี user ไม่ได้เปิด 2FA เริ่มต้น → `npm publish` ปกติเคยโดน E403 เสมอ ก่อน 1.0.12
  แก้ด้วย granular token bypass-2FA แบบ one-shot; ตั้งแต่ที่ login session คงอยู่ (`npm whoami` ผ่าน)
  publish ตรงๆ ใช้ได้แล้ว — ลองแบบตรงก่อนเสมอ ค่อย fallback ไป token dance
- **wheel version-sort bug**: `postinstall.js` เลือก wheel ด้วย string sort ธรรมดา ("1.0.9" >
  "1.0.10" แบบ lexicographic) — ต้องมี wheel เดียวใน `dist/` ต่อ release เท่านั้น (ลบของเก่าก่อน build
  ทุกครั้ง — ดูข้อ 4)
- **build/ module shadow**: `python -m build` สร้าง staging dir `build/` ใน srcdir — ปัญหาจริงคือ
  shell เดิมที่ import/run pytest ต่อทันทีหลัง build อาจ resolve ผิดโมดูล ไม่ใช่ตัว build เอง (ดูข้อ 4)
- **ไม่มี CI publish workflow** — publish มือจากเครื่อง dev เท่านั้น ไม่มี auto-publish-on-tag
- **ไม่มี git tag ตั้งแต่ 1.0.10** — commit message `chore(release): X.Y.Z — ...` คือ source of truth
  ของ "เวอร์ชันนี้ออกตอนไหน" (`git log --oneline | grep 'chore(release)'`)
