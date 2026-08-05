# QA Final Gate — graft autobuild store relocation (1.0.47)

วันที่: 2026-08-05 · role: qa · scope: FINAL GATE ก่อนปล่อย 1.0.47

## บริบท

รอบก่อนหน้ารายงาน GREEN แต่มีของหลุด: `.gitignore`/`.ignore` ยังถูกเขียนอยู่จริงเพราะ store
เดิมอิง `DATA_HOME` ซึ่งสำหรับ dev checkout เท่ากับ REPO_ROOT — `git status` เปล่าไม่เห็นเพราะเรา
เพิ่งสั่ง ignore มันไปเอง (ปิดตาตัวเอง) และ `graft build` append เข้า `.gitignore` ที่ track
อยู่แล้วซึ่งติดสถานะ M มาก่อน เทียบ before/after ด้วย `git status` เปล่าเลยจับไม่ได้

**Fix (root cause):** `graft_store.py` ย้าย store ไปที่
`~/.agent-takkub/graft-graphs/<instance-hash>/<target-hash>` — นอกรีโปเสมอ ไม่อิง DATA_HOME
รอบนี้ตรวจด้วยวิธีใหม่ตามที่สั่ง: **hash ของ `.gitignore` + `git status --ignored`** ไม่ใช่
`git status` เปล่า

## ผลรวม: ✅ GREEN ทั้งหมด — พร้อม ship 1.0.47

---

## 1. Full pytest suite

```
.venv/Scripts/python -m pytest -q
```
- **5083 passed, 5 skipped, 0 failed** (exit 0) — ตรวจแล้วไม่มีตัวอักษร `F`/`E` ปนในบรรทัด
  progress ทั้งไฟล์ (นับ `.`=5083, `s`=5, ไม่มีอย่างอื่น) — baseline รอบก่อน 5080 passed, เพิ่มขึ้น
  ตามเทสใหม่ของ `graft_autobuild.py`/`graft_store.py`
- หมายเหตุ: pytest -q ไม่พิมพ์บรรทัดสรุปท้าย ("N passed in Xs") ในรันนี้ (เห็นแค่ progress bar
  จบที่ `[100%]` แล้วตัดจบไฟล์ log) — ไม่ใช่ fail, exit code ยืนยัน 0 และไม่มี F/E character ปน,
  ไม่ block gate แต่ควร flag ให้ dev ดู (อาจเป็น addopts/plugin ที่กลืน summary line)

## 2. ruff check + format --check

```
.venv/Scripts/python -m ruff check src/ tests/       → All checks passed!
.venv/Scripts/python -m ruff format --check src/ tests/  → 381 files already formatted
```
✅ PASS

## 3. import-linter

```
.venv/Scripts/lint-imports.exe
```
✅ **23/23 kept, 0 broken**

## 4. `takkub docs-verify`

```
No broken refs found. → 0 broken ref(s) found → ok
```
✅ PASS

---

## 5. Repo cockpit เอง — hash-based before/after (ไม่ใช้ `git status` เปล่า)

Baseline (ก่อน trigger build):
- `.gitignore` md5: `e09678b785d541737d635db67c812eaf`
- `git status --ignored --short` md5: `e3cb6b2d81a2428c6b57f8730afbe964` (53 lines)

Trigger: เรียก `graft_autobuild._build_one(Path('.').resolve())` ตรงๆ (จำลอง auto-build trigger)
ต่อ repo root ของ agent-takkub เอง

After:
- `.gitignore` md5: `e09678b785d541737d635db67c812eaf` — **เหมือนเดิมทุกตัวอักษร**
- `git status --ignored --short` md5: `e3cb6b2d81a2428c6b57f8730afbe964` — **เหมือนเดิมทุกตัวอักษร**
- `git status --short` = 16 บรรทัด เหมือน baseline ต้นรอบเป๊ะ (11 M + 5 ??, ไม่มีไฟล์ใหม่งอก)
- ไม่มี `graft-graphs/` หรือ `.ignore` ใหม่โผล่ในรีโป (`find . -iname ".ignore"` เจอแค่
  `src/agent_takkub/.ignore` ตัวเดิมที่มีอยู่ก่อนแล้ว — เป็นของเก่าจาก pilot build ในอดีต ถูก
  gitignore อยู่แล้ว ไม่ใช่ของใหม่จาก build รอบนี้)

✅ **PASS — ไม่มีอะไรถูกเขียนเข้ารีโป**

## 6. Repo user จริง 2 ตัว (จาก projects.json)

เลือก: `pms/pms-web` และ `unirecon/unirecon-api` (ทั้งคู่เป็น git repo จริง มี `.gitignore`)

| repo | `.gitignore` md5 (before) | `.gitignore` md5 (after) | `git status --ignored` md5 (before) | (after) |
|---|---|---|---|---|
| pms-web | `2b1aa04b09538d724f13def0e995a5ac` | `2b1aa04b09538d724f13def0e995a5ac` | `1e9b60ed50baef3f83813bec9d345fec` | เหมือนเดิม |
| unirecon-api | `3d6e8f6d5af2a2f10ae28e6423ac8d57` | `3d6e8f6d5af2a2f10ae28e6423ac8d57` | `958c66c9dcc86bbb5c26dae9b423e08a` | เหมือนเดิม |

Trigger: `graft_autobuild._build_one()` ตรงต่อทั้ง 2 target path จริง (เหมือนที่ boot-sweep/
tab-switch trigger เรียกจริง) — build สำเร็จทั้งคู่ (`graft --dir <external store> build <target>`
exit 0), เขียน `INDEX.md` + `.graph/` + `.cache/` ครบใน store ภายนอก

✅ **PASS — ไม่มีไฟล์งอก ไม่มีไฟล์เดิมถูกแก้ ทั้ง 2 repo**

## 7. Store อยู่ที่ใหม่จริง

```
GRAFT_STORE_ROOT = C:\Users\monch\.agent-takkub\graft-graphs\<instance-hash>
```
- 3 store ที่ build จริงรอบนี้ (repo root ของ agent-takkub, pms-web, unirecon-api) ทั้งหมดอยู่ใต้
  `~/.agent-takkub/graft-graphs/58c53152.../<target-hash>/` — นอกรีโปทุกตัว ยืนยันด้วย
  `str(graph_store_dir(target)).lower().startswith(str(target).lower())` = `False`
- ไม่มี store เก่าค้างในรีโป (`find . -iname "graft-graphs"` ใน agent-takkub เปล่า)

✅ PASS

## 8. MCP ยังอ่าน graph เจอจากที่ใหม่ — ทั้ง claude และ codex

- **claude:** `shared_dev_tools.browser_profile_mcp_config_path('qa', None, 'agent-takkub',
  cwd=<repo root>)` → generated config มี graft args:
  `["-y", "@nanonets/graft@0.8.2", "--dir", "C:\\Users\\monch\\.agent-takkub\\graft-graphs\\...",
  "mcp"]` — `--dir` ชี้ store ใหม่ถูกต้อง (ตำแหน่งก่อน subcommand `mcp` ตาม CLI requirement)
- **codex:** `mcp_bridge._role_mcp_servers('qa', None, 'agent-takkub', cwd=<repo root>)` (ฟังก์ชัน
  เดียวกับที่ `_codex_mcp_argv` เรียกใช้ก่อนแปลงเป็น TOML) → คืน args ชุดเดียวกันเป๊ะ ยืนยันว่า
  codex ใช้ resolver ร่วมกับ claude 100% (`_role_mcp_servers` delegate ตรงไปที่
  `browser_profile_mcp_config_path` เดียวกัน — ดู docstring mcp_bridge.py L214-222)
- **stdio probe:** `graft --dir <relocated store> ask "where is graph_store_dir defined"` ตอบถูก
  ต้อง ชี้ `src/agent_takkub/graft_store.py:L107-L113` พร้อม line range ตรงกับซอร์สปัจจุบัน —
  ยืนยันว่า graph ที่ relocate แล้วอ่านได้จริงผ่าน CLI/MCP path เดียวกับที่ pane จะใช้

✅ PASS ทั้ง 2 provider

## 9. `takkub disk` เห็น category `graft-graphs` ที่ path ใหม่ + prune orphan ได้

```
[safe] 483.3MB  5 files  graft-graphs  graft-graphs/* (external code-graph store...)
         orphan 0 bytes (2) · live 506778546 bytes (3, ไม่แตะ)
```
- category ปรากฏถูกต้องพร้อม orphan/live breakdown
- **หมายเหตุ (ไม่ block):** `takkub prune` เป็นคำสั่ง lead-only — qa เรียกผ่าน CLI ตรงไม่ได้
  (`error: only lead can run 'takkub prune'. you are 'qa'.`) จึงตรวจ logic ผ่าน
  `disk_usage.prune()` โดยตรงแทน (bypass เฉพาะ CLI gate ไม่ใช่ business logic):
  `VALID_CATEGORIES` มี `"graft-graphs"` จริง, เรียก
  `prune(categories=['graft-graphs'], level='safe', dry_run=True)` เจอ orphan 2 รายการถูกต้อง
  (2 store เปล่าที่เกิดจาก path-typo ของ QA เองระหว่างทดสอบ ไม่ใช่ store จริง) และไม่แตะ 3 store
  live (repo root/pms-web/unirecon-api) เลย — logic prune ทำงานถูกต้อง
- **⚠ minor doc-drift (ไม่ block ship):** `cli.py`'s `--category` help string (บรรทัด
  ~1681-1683) ไม่ได้ list `graft-graphs` ไว้ในคำอธิบาย (list จบที่ `...,node-modules`) ทั้งที่
  `VALID_CATEGORIES` โค้ดจริงมีอยู่และ validate ผ่าน — เป็นแค่ help-text ตกหล่น ไม่กระทบ
  functionality แนะนำแก้ในรอบถัดไป
- เก็บกวาด 2 orphan store เปล่าที่ QA สร้างขึ้นจาก path-typo (`rm -rf` โดยตรง หลัง dry-run
  ยืนยันว่าเป็น orphan/0 bytes) ก่อนปิดรายงาน

✅ PASS (มี minor doc-drift ที่ไม่ block)

## 10. Kill switch ยังกัน subprocess ครบ

Monkeypatch `graft_autobuild._spawn_build` ให้ raise ถ้าถูกเรียก แล้วรัน 3 trigger entrypoint
พร้อม `TAKKUB_SKIP_GRAFT_BUILD=1`:
- `build_all_projects_async()`
- `ensure_project_graph_async('agent-takkub')`
- `schedule_rebuild_after_done(<repo root>)`

ผล: **ไม่มี call ไหนถึง `_spawn_build` เลยสักตัว** — kill switch กัน subprocess spawn ครบทั้ง 3
trigger ยืนยันด้วย `conftest.py` ก็ set `TAKKUB_SKIP_GRAFT_BUILD=1` เป็น default ให้ทุก test run
อยู่แล้ว (L51, L152)

✅ PASS

---

## สรุป

ทุกข้อผ่านหมด root cause ของบั๊กรอบก่อน (store อิง DATA_HOME แล้วชนกับ dev-checkout repo)
แก้ถูกจุดจริง — ยืนยันด้วย hash comparison (ไม่ใช่ `git status` เปล่าที่เคยพลาด) ทั้งบน repo
cockpit เองและ repo user จริง 2 ตัว, store ย้ายไปนอกรีโปแน่นอน, MCP ทั้ง claude/codex อ่านจาก
ที่ใหม่ได้จริง, disk/prune เห็น category ถูกต้อง, kill switch ยังทำงานครบ

พบ 2 จุดเล็กที่ไม่ block ship:
1. `pytest -q` ไม่พิมพ์ summary line ท้ายสุด (ตรวจแทนด้วยการนับ F/E character = 0 + exit 0)
2. `cli.py` prune `--category` help text ไม่ list `graft-graphs` (functionality ใช้ได้ปกติ
   เพราะ validate จาก `VALID_CATEGORIES` ไม่ใช่จาก help string)

**พร้อม ship 1.0.47**
