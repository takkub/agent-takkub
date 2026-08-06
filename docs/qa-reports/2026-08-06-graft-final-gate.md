# Final gate — graft auto-build + chip + doctor/disk, ก่อน release 1.0.48

QA verify รอบสุดท้าย ครอบคลุมสิ่งที่ Lead ยังไม่ได้ตรวจเอง (staleness fix / new-file escalate / import-linter / suite รอบแรกของ Lead ตรวจแล้ว — ไม่ทำซ้ำ)

**Evidence dir:** `runtime/exports/2026-08-06/agent-takkub/` (probe scripts + logs ทั้งหมดที่อ้างถึงด้านล่าง)

## สรุปผล — ทุกข้อผ่าน ไม่มี blocker

| # | รายการ | ผล |
|---|---|---|
| 1 | Full suite รอบที่ 2 (เครื่อง QA) | ✅ exit 0, dots ล้วน ไม่มี F/E ตลอด 100% |
| 2 | Chip 4 สถานะ + failed TTL 24h | ✅ ตัวเลขตรงจริงตาม `get_build_status()`, TTL prune ทำงานถูกต้อง |
| 3 | Kill switch `TAKKUB_SKIP_GRAFT_BUILD` | ✅ 0 subprocess call รวม trigger ใหม่ (idle-watchdog resync) |
| 4 | idle-watchdog ไม่โดนถ่วง | ✅ resync call เป็น non-blocking dispatch, throttle 15s/dir ทำงานถูก |
| 5 | repo hygiene (`.gitignore` + `git status --ignored`) | ✅ เหมือนทุกตัวอักษร ก่อน/หลัง build+resync จริง (2 repos) |
| 6 | doctor + `takkub disk` เห็นขนาด store จริง | ✅ ตรงกัน (77MB / 76.8MB), ไม่พังตอน edge case |

**Non-blocking findings (2 รายการ ไม่ block release):** ดูหัวข้อ "ประเด็นที่พบ" ด้านล่าง

---

## 1) Full test suite (รอบที่ 2, เครื่อง QA)

```
.venv/Scripts/python.exe -m pytest -q > full_suite_run.log 2>&1
EXIT_CODE=0
```

- รันจริง ใช้เวลา ~10 นาที (เทสที่สร้าง venv จริงตามที่คาด ไม่ใช่ค้าง)
- ทุก progress bar เป็น `.` (pass) ล้วนตลอด 0% → 100%, ไม่มี `F`/`E`/`s` เกินจำนวนที่คาด (skip เดิม 3 จุดตามปกติของ repo)
- `EXIT_CODE=0` ยืนยันอีกชั้น — ไม่มี failed/error
- Evidence: `full_suite_run.log`

เพิ่มเติม (cheap, re-verify ซ้ำจาก Lead):
- `ruff check src/ tests/` → All checks passed
- `ruff format --check src/ tests/` → 382 files already formatted
- `lint-imports` → 23/23 kept, 0 broken (ตรงกับที่ Lead ตรวจ)
- `docs-verify` → 0 broken ref(s)

## 2) Chip 4 สถานะ — ตรวจตัวเลขจริง ไม่ใช่แค่ render

อ่านโค้ด `status_header.py::_refresh_graft_chip` + `graft_autobuild.py::get_build_status()` แล้วทดสอบตรงกับฟังก์ชันจริง (ไม่ mock) ผ่าน `chip_state_probe.py`:

- **ready** — `total>0, completed==total, failed==[]` → `"🧠 Graft ready"` (path นี้ dev เทสไว้แล้วใน `test_graft_chip.py`, cross-check ผ่าน)
- **building** — `_building` set มี entry → `get_build_status()["building"]==1` ตรงจริง (ไม่ใช่ hardcode)
- **not installed** — `graft_cli_path() is None` → `available=False` → chip ขึ้น "not installed" (โค้ดอ่านตรง, มี test อยู่แล้ว)
- **failed (พร้อม TTL 24h)** — ทดสอบ 3 เคสตรงกับ `_last_build_failed` dict จริง:
  - entry สด (0s) → ยังอยู่ใน `failed`
  - entry อายุ `TTL - 60s` (ต่ำกว่า cutoff เล็กน้อย) → **ยังอยู่** (ไม่ถูก prune ก่อนเวลา)
  - entry อายุ `TTL + 60s` → **ถูก prune ออกจากทั้ง `get_build_status()["failed"]` และ `_last_build_failed` dict เอง** (lazy prune ทำงานถูกจุด)

Evidence: `chip_state_probe.py` (รันแล้ว, `ALL CHIP-STATE PROBES PASSED`)

**⚠️ ประเด็นสำคัญที่ต้องเข้าใจให้ตรง (ไม่ใช่บั๊ก แต่ mismatch กับสิ่งที่ task บรีฟบอก):**

Task บรีฟข้อ 2 ขอให้ "ตรวจว่า dir ที่ถูกลบออกจาก projects.json หายจาก chip จริง" — ทดสอบแล้วพบว่า **`get_build_status()` ไม่มี cross-check กับ projects.json เลย** เป็น TTL-based ล้วนๆ ตามที่ code docstring + `test_get_build_status_prunes_failure_after_ttl` ของ dev เขียนไว้ตรงกัน (2026-08-06 follow-up comment ในโค้ดบอกชัดว่า "a dir removed from projects.json has no trigger left to ever re-attempt/clear it... the TTL only ever prunes entries nothing has retried in a day")

→ ทดสอบยืนยัน (`REMOVED_FROM_PROJECTS_JSON_BUT_FRESH` case ใน `chip_state_probe.py`): dir ที่ลบจาก projects.json ไปแล้ว แต่ entry ใน `_last_build_failed` ยังอายุไม่ถึง 24 ชม. → **ยังค้างใน chip** ไม่หายทันที จะหายก็ต่อเมื่อครบ 24 ชม.เท่านั้น

**สรุป:** พฤติกรรมนี้ตรงกับ design ที่ dev ตั้งใจทำ (documented, tested) ไม่ใช่บั๊ก — แต่ user/Lead ควรรู้ว่า "ลบ project ออกจาก projects.json" ไม่ทำให้ chip เคลียร์ทันที ต้องรอ TTL 24h หรือ restart cockpit (ซึ่งจะ rebuild `_last_build_failed` dict ใหม่จากศูนย์อยู่แล้วเพราะเป็น in-memory dict)

## 3) Kill switch `TAKKUB_SKIP_GRAFT_BUILD`

ทดสอบ `idle_watchdog_kill_switch_probe.py` แทน subprocess.Popen ด้วย tripwire (raise ทันทีถ้าถูกเรียก):

- **kill switch SET** → `gab.resync_staging_only(cwd)` → **0 การเรียก `subprocess.Popen` แม้แต่ครั้งเดียว** (ตรวจสอบยันตั้งแต่ `_skip_env()` guard บรรทัดแรกของฟังก์ชัน — ไม่ทันแตะ `_git_nonignored_files` เลย)
- ตรวจโค้ดยันจุดกันของทั้ง 4 trigger: `build_all_projects_async`, `ensure_project_graph_async`, `schedule_rebuild_after_done`, `resync_staging_only` — ทุกจุดมี `if _skip_env(): return` เป็นบรรทัดแรกๆ ของฟังก์ชัน ครบทั้ง 4 trigger รวม trigger ใหม่ (#4, idle-watchdog)
- conftest.py ตั้ง `TAKKUB_SKIP_GRAFT_BUILD=1` เป็น default ทุก test run อยู่แล้ว — ยืนยันด้วย assertion ใน `test_graft_autobuild.py:879`

Evidence: `idle_watchdog_kill_switch_probe.py` (รันแล้ว, Popen calls = 0)

## 4) idle-watchdog ไม่โดน trigger ใหม่ถ่วง

อ่านโค้ด `orchestrator.py::_check_idle_teammates` (บรรทัด ~3282-3289): เรียก `resync_staging_only(pane._session_cwd)` ตรงในทุก tick ของ QTimer (5s interval) แต่:

- ฟังก์ชันเองมี throttle `_LIVE_RESYNC_MIN_INTERVAL_S = 15.0` ต่อ directory — เรียกถี่กว่านั้นเป็น no-op ทันที (แค่ dict lookup + comparison, ไม่มี I/O)
- งานหนักจริง (git ls-files subprocess + staging sync) ถูก dispatch เข้า `threading.Thread(daemon=True)` แยกต่างหาก — ไม่ block QTimer thread หลักเลย
- worktree-isolated pane ถูก skip ไปเลยตั้งแต่ก่อนเรียก (เช็ค `_ps_wt.worktree` ก่อน)
- เส้นทางก่อนถึงจุด spawn thread (resolve path, `is_dir()` stat, lock, dict) เป็น syscall เร็วล้วน ไม่มี blocking I/O ใดๆ บน main thread

**ไม่มี test ใน `test_idle_watchdog.py` ที่ยืนยัน wall-clock ของ `_check_idle_teammates()` เอง** (เทสเดิมทั้งหมด mock เวลาผ่าน `monkeypatch.setattr(orch_mod.time, "time", ...)` ไม่ได้วัด real elapsed) — แต่จาก code-level review ยืนยันว่า design ไม่มีทาง block เพราะงานหนักอยู่หลัง thread spawn ทั้งหมด

## 5) Repo hygiene — real trigger, 2 repos

ทดสอบด้วย `trigger_build_resync.py` (kill switch OFF, เรียก `_build_one()` + `resync_staging_only()` จริง ไม่ mock) กับ:
- `C:/Users/monch/WebstormProjects/agent-takkub` (repo ใหญ่ 43 store/multi-mixin)
- `C:/Users/monch/WebstormProjects/line-liff-frontend` (repo แยกอิสระ)

เทียบ **ก่อน/หลัง** ด้วย `git status --ignored --porcelain=v1` (ไม่ใช้ `git status` เปล่าตามที่กำชับ) + `sha256sum .gitignore`:

```
agent-takkub:        .gitignore IDENTICAL · git status --ignored IDENTICAL (byte-for-byte)
line-liff-frontend:   .gitignore IDENTICAL · git status --ignored IDENTICAL (byte-for-byte)
```

ยืนยัน H1 fix (graph ไม่เขียนเข้า target repo, staging mirror อยู่นอก repo ทั้งหมด) ยังทำงานถูกต้องหลัง trigger จริงทั้ง full-build และ live-resync path

Evidence: `agent-takkub_status_before.txt` / `_status_after.txt` / `_status.diff` (ว่างเปล่า) และเช่นเดียวกันสำหรับ `line-liff-frontend`

## 6) doctor + `takkub disk`

```
takkub doctor  → [graft] · store-size   2 live store(s), 77 MB total
takkub disk    → graft-graphs   76.8MB   2 files   live 80568717 bytes (2, ต้อง --include-live)
```

ตัวเลขตรงกัน (77MB doctor ≈ 76.8MB disk, ปัดเศษต่างกันเล็กน้อยจาก MB calc) — เห็น store จริงหลัง trigger build 2 repos ข้างต้น ไม่ต้องพึ่ง `takkub disk` แยก

**prune orphan / edge case ไม่มี store:** ทดสอบเรียก `takkub prune --category graft-graphs` ตรงๆ ถูก role-gate บล็อก (`only lead can run 'takkub prune'`) ตามที่ตั้งใจ — QA ไม่มีสิทธิ์รัน prune จริง ตรวจแทนที่ระดับโค้ด: `disk_usage.VALID_CATEGORIES` มี `"graft-graphs"` และ `_prune_graft_graphs()` แยก orphan (safe) ออกจาก live (ต้อง `--include-live`, gated เป็น REVIEW) ถูกต้องตาม design

---

## ประเด็นที่พบ (ไม่ block release, แจ้งไว้เพื่อ backlog)

### [minor] `takkub prune --help` ข้อความ stale — ไม่มี `graft-graphs` ใน category list

`cli.py` บรรทัด ~1690-1692 hardcode ข้อความ help เป็น:
```
"comma-separated categories to prune (default: every safe category). "
"one of: browser-profiles,transcripts,exports,orphan-worktrees,"
"orphan-worktrees-review,shell-snapshots,partial,chat-history,node-modules"
```
ขาด `graft-graphs` แม้ `disk_usage.VALID_CATEGORIES` (ที่ validate จริงตอน runtime) จะมีอยู่ครบ — ฟังก์ชันจริง **ใช้งานได้ปกติ** (`--category graft-graphs` ผ่าน validation ที่ `cmd_prune`), เป็นแค่ help text ที่ไม่ sync กับ `VALID_CATEGORIES` แล้ว user ที่พึ่ง `--help` อย่างเดียวจะไม่รู้ว่า prune graft store ได้ผ่าน flag นี้ — 1-line fix

### [info] Chip "failed" ไม่ cross-check projects.json (ดูรายละเอียดเต็มในหัวข้อ 2)

พฤติกรรมตามที่ dev ตั้งใจและมี test คุมอยู่แล้ว ไม่ใช่บั๊ก แต่ต่างจากสิ่งที่ task บรีฟ QA รอบนี้สื่อว่าควรจะเป็น — บันทึกไว้ให้ Lead ตัดสินใจว่า UX นี้พอรับได้หรือควรเพิ่ม cross-check ภายหลัง (นอก scope final gate นี้)

---

## Evidence files (`runtime/exports/2026-08-06/agent-takkub/`)

- `full_suite_run.log` — full pytest run รอบที่ 2, exit 0
- `chip_state_probe.py` — 4-state chip + TTL probe (ต่อ `graft_autobuild` จริง)
- `idle_watchdog_kill_switch_probe.py` — kill switch tripwire test
- `trigger_build_resync.py` — real build+resync trigger script (2 repos)
- `agent-takkub_status_before.txt` / `_status_after.txt` / `_status.diff`
- `line-liff-frontend_status_before.txt` / `_status_after.txt` / `_status.diff`
- `doctor_output.txt`, `disk_output.txt`
