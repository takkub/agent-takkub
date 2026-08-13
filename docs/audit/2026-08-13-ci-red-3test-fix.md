# CI แดง — แก้ 3 test fail (windows-latest)

## 1. `test_version_sync.py::test_python_and_npm_versions_match_pyproject`
- Root cause: `src/agent_takkub/__init__.py::__version__` ค้างที่ `"1.0.54"` ไม่ sync กับ `pyproject.toml`/`package.json` ที่ bump เป็น `1.0.56` แล้ว
- Fix: แก้ `__version__ = "1.0.56"`

## 2. `test_lead_context_compact.py::TestParallelModeWorktreeRule::test_solo_mode_has_no_parallel_block`
- Root cause: commit `192a283` (#104, remove 3 UI toggles) ทำให้ `exec_mode.is_parallel()` return `True` เสมอ (ไม่อ่าน `current()` อีกต่อไป) — SOLO mode ไม่มีทางเกิดขึ้นจริงในระบบแล้วโดยดีไซน์ เทสนี้ monkeypatch `exec_mode.current` (ซึ่งไม่ถูกใช้โดย render path — `lead_context.py:565` เรียก `is_parallel()` ตรงๆ) แล้วคาดหวัง behavior ที่ไม่มีอยู่จริง
- Fix: ลบ `test_solo_mode_has_no_parallel_block` ออกจาก class (เก็บ `test_parallel_block_includes_worktree_rule` ที่ยัง valid ไว้) + ปรับ class docstring ให้ตรงกับ behavior ปัจจุบัน

## 3. `test_done_note_symmetrize.py::TestEvidenceStillAppended::test_evidence_appended_after_condensed_headline`
- Root cause: commit `07303cc` (#159, flag suspect screenshot evidence) เพิ่ม suffix `(size ⚠tags)` ต่อท้ายชื่อไฟล์ evidence ทุกไฟล์ตั้งใจ — เทสเขียนก่อน #159 เลย assert exact `endswith("evidence.png")` แบบเก่า
- Fix: แก้ assertion ให้ยอมรับ format ใหม่ — `notice.rstrip().endswith(")")` + `"evidence.png (" in notice`

## Verification
- Targeted (3 ไฟล์ที่แก้): `PYTHONPATH=src pytest tests/test_version_sync.py tests/test_lead_context_compact.py tests/test_done_note_symmetrize.py` → **19 passed**
- Full suite รันครั้งเดียวก่อน commit (CI gate): พบ **11 failures อื่น** นอกเหนือจาก 3 จุดที่แก้ — ตรวจสอบด้วย `git stash` แล้วรัน full suite/เทสเดี่ยวบน clean tree (ก่อนแก้) พบว่า **failures ทั้ง 11 มีอยู่แล้วก่อนแก้** (identical กับหลังแก้) → ไม่ใช่ regression จากงานนี้:
  - `test_installed_cli_bin_integration.py` (2 tests), `test_installed_mode_gate.py` (6 tests): ต้องการ built console script (`takkub.exe`) ใน temp venv ที่ environment นี้ไม่ได้ build ไว้ — environment-specific, ไม่เกี่ยวกับ code
  - `test_orchestrator_env_allowlist.py::test_build_pane_env_includes_path`: local `PATH` มี `npm` prefix อยู่แล้วก่อน monkeypatch — environment-specific
  - `test_pane_tools_policy.py::TestKnownRoles::test_includes_registered_custom_role` + `test_roles.py::TestByName::test_unknown_role_returns_none`: ทดสอบ standalone (ไม่รวม full suite) ผ่านทั้งคู่ — เป็น test-order pollution (custom role `"data-eng"` ค้าง global state ข้ามไฟล์เทสเมื่อรันเต็ม suite) ไม่ใช่เกี่ยวกับ 3 จุดที่แก้
- สรุป: 3 จุดที่ระบุใน task แก้ครบและ verify แล้ว ไม่มี regression ใหม่จากงานนี้ 11 failures ที่เหลือเป็น pre-existing (environment + test-isolation) ที่ควรแยก issue ต่างหาก
