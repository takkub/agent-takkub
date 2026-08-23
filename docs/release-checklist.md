# Release checklist (agent-takkub)

วิธี release จริงของ 1.0.10+ (manual ทุกขั้น) — **ไม่ใช่** `takkub release` (คำสั่งนั้นมีอยู่จริง
ใน CLI: "bump version + roll CHANGELOG [vNEXT] + git commit" แต่มัน parse heading `## [vNEXT]`
ซึ่ง CHANGELOG.md ไม่ได้ใช้รูปแบบนั้น — อย่าเรียก)

> **เอกสารนี้เคยเพี้ยนจากของจริงมาก่อน** (ตรวจ 2026-08-20 ตอน release 1.0.79) — ข้อ 2 เคยสั่ง
> "ห้ามสร้าง heading เวอร์ชัน" ทั้งที่ CHANGELOG.md มี heading อยู่ 77 อัน และหลายที่เคยเขียนว่า
> "ไม่มี git tag / ไม่มี GitHub Release ตั้งแต่ 1.0.10" ทั้งที่ v1.0.76/77/78 มีทั้ง tag และ
> Release object ครบ และ CI ถูกเขียนว่ารัน 2 OS ทั้งที่จริง 3 **ถ้าเจอเอกสารขัดกับ repo อีก
> ให้เชื่อ repo แล้วแก้เอกสารทันที** — คำสั่งตรวจของจริง:
>
> ```bash
> grep -c '^## \[' CHANGELOG.md            # heading เวอร์ชันใน CHANGELOG
> git tag --list --sort=-creatordate | head # tag ล่าสุด
> gh release list --limit 5                 # GitHub Release ล่าสุด
> grep -n 'os:' .github/workflows/ci.yml    # matrix ของ CI
> ```

## 0. Pre-flight — ต้องเขียวก่อนแตะเวอร์ชัน

```bash
takkub qa-gate     # venv-check -> full pytest -> ruff check -> lint-imports, fail-fast + report
```

**ห้ามพิมพ์ `pytest` / `ruff check` / `lint-imports` ดิบเอง** — ตั้งแต่ #325 entrypoint เดียวคือ
`takkub qa-gate` (root CLAUDE.md บังคับ) มันรัน 4 step เดียวกับที่ CI รัน แล้วเขียน report ลง
`docs/qa/` ให้ commit ไปกับ release ได้เลย · full suite รวม installed-mode gate
(`tests/test_installed_mode_gate.py`, `tests/test_installed_cli_bin_integration.py`) ซึ่งต้องรัน
จาก venv ที่มี `build` package (dev extra `.[dev]`) ไม่งั้น wheel-build fixture fail

> **อย่าแตะเวอร์ชันระหว่าง gate กำลังรัน** — `test_version_sync` อ่าน `__version__` ตอน import
> แต่อ่าน pyproject ตอนรันเทส bump คร่อมกันเมื่อไหร่จะตกแบบหลอกๆ (เจอจริงตอน 1.0.79,
> `docs/qa/2026-08-20-165059-qa-gate.md`) — รอ gate จบก่อนค่อย bump

- **CI ต้องเขียวครบทุก job** (`.github/workflows/ci.yml`) — **5 job ไม่ใช่ 4**:
  `lint-and-test` รัน 3 OS (`windows-latest` · `macos-latest` · `ubuntu-latest`) และ
  `installed-gate` รัน 2 OS (`windows-latest` · `macos-latest`) — ถ้าเพิ่งพุช commit สุดท้าย
  รอ `gh run watch <id> --exit-status` เขียวก่อนตัดเวอร์ชัน อย่า release ทับ CI ที่ยังไม่จบ
- **installed-mode gate** (`tests/test_installed_mode_gate.py`) proves the ACTUAL packaged behavior
  (DATA_HOME/ASSETS_ROOT/CLI_BIN_DIR/pane-env/CLI wiring) works when running from a real installed
  wheel — not just a dev checkout where `DATA_HOME == REPO_ROOT` masks installed-only bugs
  (the TAKKUB_PORT_FILE bug fixed in 8a06c52/c55c3e0 is exactly this bug class). ถ้าอันนี้แดง **ห้าม
  release** ต่อให้ pytest อื่นเขียวหมด — แปลว่า wheel ที่กำลังจะ ship พังตอนติดตั้งจริง
- `takkub doctor` มีหมวด `[installed]` (`check_installed_integrity`, Phase D) ที่รันเฉพาะตอน
  `DATA_HOME != REPO_ROOT` (installed build) — ใช้เช็ค production instance ตัวจริงบนเครื่อง user
  ได้ด้วย ไม่ใช่แค่ตอน release

## 1. ตัดสินเวอร์ชันใหม่ (SemVer)

> **ใช้ SemVer ให้ถูกจริง (user directive 2026-08-23):** ที่ผ่านมา bump แค่ patch 87 รอบทั้งที่หลายรอบมี
> feature — ตั้งแต่ 1.1.0 เป็นต้นไป: **minor** เมื่อมีความสามารถใหม่ที่ของเดิมยังใช้ได้ · **patch** เฉพาะ
> รอบแก้บั๊กล้วน · **major** (2.0.0) เมื่อมีของพังของเดิมจริง — กรณี Core V2 คือตอน Phase 10 ลบ V1 /
> สลับ authority ไม่ใช่ตอน engine เปิด

ดู diff ตั้งแต่ release ก่อนหน้า แล้วเลือก patch/minor/major ตามหลัก SemVer ปกติ

**อย่า hardcode เลขไว้ในเอกสารนี้** (เคยเขียน "ปัจจุบันอยู่ที่ 1.0.12" แล้วเน่าค้าง 66 เวอร์ชัน) —
อ่านของจริงทุกครั้ง:

```bash
grep '^version' pyproject.toml                          # เวอร์ชันปัจจุบัน
git log --oneline --grep='release):\? 1\.' -1          # release commit ล่าสุด
git log --oneline <last-release-commit>..HEAD           # อะไรเข้ามาบ้างตั้งแต่นั้น
```

## 2. เขียน CHANGELOG.md (ภาษาไทย)

**สร้าง heading เวอร์ชันใหม่ทุก release** — แทรก `## [X.Y.Z] - YYYY-MM-DD` ไว้**ใต้**
`## [Unreleased]` (ซึ่งเป็น placeholder ว่างถาวร ไม่ต้องลบ ไม่ต้องเติมอะไรใต้มัน) แล้วเขียน
bullet ใต้ `### Fixed (แก้)` / `### Added (เพิ่ม)` / ฯลฯ ของ heading ใหม่นั้น

โครงที่ได้:

```markdown
## [Unreleased]

## [1.0.79] - 2026-08-20

### Fixed (แก้)

- **<หัวข้อสั้นเป็นภาษาไทย>** — <อาการที่ user เจอ> → root cause: <สาเหตุจริง> → แก้: <วิธีแก้ +
  ไฟล์/ฟังก์ชันหลัก>
  + N tests (`test_xxx.py`)

## [1.0.78] - 2026-08-20
```

ตัวอย่างจริงล่าสุด: `git show cf54eee -- CHANGELOG.md` (1.0.79) หรือ `git show 3129b74 -- CHANGELOG.md` (1.0.78)

## 3. Bump เวอร์ชัน — 3 ไฟล์ ต้องตรงกันเป๊ะ

```bash
# pyproject.toml
version = "X.Y.Z"

# package.json
"version": "X.Y.Z",

# src/agent_takkub/__init__.py
__version__ = "X.Y.Z"
```

`tests/test_version_sync.py` บังคับให้ทั้ง 3 ค่าเท่ากัน — รันเช็คเร็วๆ ด้วย
`takkub qa-gate --targeted tests/test_version_sync.py` ก่อนไปต่อ

## 4. Build wheel

**#340 (แก้แล้ว):** `postinstall.js` เลือก wheel จากเวอร์ชันใน `package.json` ตรงๆ แล้ว (ไม่ใช่
string sort สุดท้าย) และ `npm publish`/`npm publish --dry-run` มี `prepublishOnly` guard
(`npm/scripts/preflight-publish.js`) เทียบเวอร์ชันใน `package.json` กับชื่อไฟล์ wheel ใน `dist/` —
ไม่ตรง/ไม่มี/มีมากกว่า 1 ไฟล์ → publish ตายทันที ไม่ warn เฉยๆ ยังต้อง build เองตามข้อนี้ (manual flow นี้
ยังไม่เรียก `takkub release`) แต่ผิดเวอร์ชันแล้ว publish จะไม่หลุดเงียบๆ อีกต่อไป

```bash
rm -f dist/*.whl   # ลบของเก่าก่อนเสมอ — npm files:"dist/*.whl" ship ทุกไฟล์ที่เจอ

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

## 5. Commit

ไฟล์ที่แตะ: `CHANGELOG.md`, `pyproject.toml`, `package.json`,
`src/agent_takkub/__init__.py` — ไม่มี `dist/` (gitignored)

```bash
git add CHANGELOG.md pyproject.toml package.json src/agent_takkub/__init__.py
git commit -m "chore(release): X.Y.Z — <หัวข้อสั้น>"
```

### 5b. Tag

**ต้อง tag** — กลับมาทำตั้งแต่ 1.0.76 (`v1.0.76` / `v1.0.77` / `v1.0.78` เป็น lightweight tag
ทั้งหมด ไม่ใช่ annotated) ยิงที่ release commit:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

> หมายเหตุ: v1.0.76–78 ถูก tag ทีหลังที่ HEAD ตอนนั้น จึงไม่ได้ชี้ที่ release commit เป๊ะ
> (`v1.0.78` → `685efc5` ไม่ใช่ `3129b74`) — ตั้งแต่ 1.0.79 เป็นต้นไป tag ที่ release commit เลย

## 6. Push + รอ CI เขียวครบ 5 job

```bash
git push origin main
gh run list --branch main --limit 5    # หา run id ของ workflow ชื่อ `ci`
gh run watch <id> --exit-status        # รอ 5 job เขียวหมด:
                                       #   lint-and-test  (windows / macos / ubuntu)
                                       #   installed-gate (windows / macos)
```

**ห้าม publish ก่อน CI เขียว** — ถ้า `installed-gate` แดงโดยเฉพาะ (แม้ `lint-and-test` เขียว) แปลว่า
wheel ที่กำลังจะ ship พังตอนติดตั้งจริงบน OS นั้น ต่อให้ pre-flight ในเครื่องตัวเองเขียวไปแล้วก็ตาม
(env drift ระหว่างเครื่อง — `installed-gate` proves the packaged artifact, not just the dev checkout)

## 6b. GitHub Release

**ต้องสร้าง** — กลับมาทำพร้อม tag ตั้งแต่ 1.0.76 (`gh release list` ยืนยัน: 1.0.76 / 1.0.77 /
1.0.78 มีครบ) รูปแบบที่ใช้จริง: title = `X.Y.Z — <หัวข้อสั้นไทย>` (ไม่มี `v` นำ), body =
`## ไฮไลต์` เป็น bullet + วิธีอัพเดต

```bash
gh release create vX.Y.Z --title "X.Y.Z — <หัวข้อสั้น>" --notes "<ไฮไลต์>"
```

ตัวอย่างจริง: `gh release view v1.0.79 --json name,body` หรือ `v1.0.78`

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
- **wheel version-sort bug (#340, แก้แล้ว)**: `postinstall.js` เคยเลือก wheel ด้วย string sort
  ธรรมดา ("1.0.9" > "1.0.10" แบบ lexicographic) — ตอนนี้เลือกจากเวอร์ชันใน `package.json` ตรงๆ
  (`npm/scripts/lib.js`'s `findWheelForVersion`) และมี `prepublishOnly` preflight guard กัน publish
  ผิดเวอร์ชันไว้อีกชั้น (ดูข้อ 4) — ลบของเก่าก่อน build ทุกครั้งไว้เป็นนิสัยดีเหมือนเดิม
- **build/ module shadow**: `python -m build` สร้าง staging dir `build/` ใน srcdir — ปัญหาจริงคือ
  shell เดิมที่ import/run pytest ต่อทันทีหลัง build อาจ resolve ผิดโมดูล ไม่ใช่ตัว build เอง (ดูข้อ 4)
- **ไม่มี CI publish workflow** — publish มือจากเครื่อง dev เท่านั้น ไม่มี auto-publish-on-tag
  (workflow มีแค่ 3 ตัว: `ci.yml`, `codeql.yml`, `security.yml` — ยืนยันด้วย `ls .github/workflows/`)
- **GitHub Release กลับมาสร้างตั้งแต่ 1.0.76** พร้อมกับ tag — เอกสารนี้เคยเขียนว่าไม่มี ทำให้
  1.0.79 ตกทั้ง tag และ Release ตอนแรก (ตามไปเติมทีหลังทั้งคู่)
- **git tag กลับมาใช้ตั้งแต่ 1.0.76** (ก่อนหน้านั้นเว้นช่วง 1.0.36–1.0.75) — แต่ commit message
  ยังเป็น source of truth ของ "เวอร์ชันนี้ออกตอนไหน" เพราะ tag เก่าไม่ได้ชี้ release commit เป๊ะ
  รูปแบบ commit ที่ใช้จริงมี 3 แบบปนกัน: `chore(release):` (ส่วนใหญ่), `release:` (1.0.78),
  `fix(release):` (1.0.69/1.0.70) — grep ให้ครอบทั้งหมด: `git log --oneline | grep -iE 'release[):]'`
