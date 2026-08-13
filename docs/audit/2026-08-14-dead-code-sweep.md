# Dead-code sweep — 2026-08-14

**Branch:** `wt/backend-6-1786663747` · **Scope:** conservative sweep, กอง A เท่านั้น (proven-unused)

## Method

1. อ่าน `docs/architecture/godfile-map.md` ก่อนเริ่ม (hidden-edges table) เพื่อรู้ว่าโค้ดที่ "ไม่มีใคร import"
   อาจถูกเรียกผ่าน string dispatch / hook wiring ได้
2. `ruff check src/ tests/ tools/ scripts/` (F401/F811/F841/B/RUF ตาม config เดิม) — **clean, 0 findings**
   (project's own per-file-ignore already exempts `orchestrator.py`'s intentional re-export façade)
3. พบว่ามี audit ที่ทำไว้แล้วเมื่อวาน (`docs/audit/2026-08-13-review-architecture.md`, section **L12**) — สแกน
   unreferenced top-level symbol ทั้ง corpus (src+tests+scripts+docs) และเจอ 8 รายการ dead code จริง
   จาก 46 commits ในคืนก่อนหน้า ผมตรวจสอบซ้ำทุกรายการ (grep whole-repo ใหม่ ณ วันนี้ ยืนยันว่ายังไม่มีใครใช้)
   ก่อนลบจริง แทนที่จะสแกนใหม่จากศูนย์ — L12 คือ corpus-wide scan ที่ครอบคลุมกว่าที่ผมทำเองในเวลาจำกัดได้
4. เพิ่มการตรวจ zero-fan-in module จาก `docs/architecture/depgraph.json` เพื่อหา orphan module เพิ่มเติม
   นอกเหนือจาก L12 — ยืนยันว่าที่เหลือทั้งหมดมี real usage (lazy import ที่ grimp บางเคสมองไม่เห็น หรือ
   intentional external-wiring hook)

## กอง A — ลบแล้ว (พิสูจน์แล้วว่าไม่มีใครใช้ ทั้ง import และ string)

| ไฟล์ | สิ่งที่ลบ | หลักฐานว่าไม่มีใครใช้ |
|---|---|---|
| `src/agent_takkub/status_header.py` | `_exec_mode_chip_style`, `_exec_mode_chip_tooltip`, `_auto_resume_chip_style`, `_auto_resume_chip_tooltip` (4 static methods, ~55 LOC) | grep ทั้ง repo (src+tests+docs) เจอแค่ definition ตัวเอง — chip UI ที่เรียกถูกลบไปแล้วใน `192a283` (2026-08-13) แต่ helper ค้าง |
| `src/agent_takkub/main_window.py:159` | property `_custom_role_colors` (backwards-compat accessor) | grep ทั้ง repo เจอแค่ definition; sibling accessors (`lead_pane`, `teammate_panes`) ยังมี 35 callsite จริง ยืนยันว่าตัวนี้ถูกทิ้งไว้ต่างหาก ไม่ใช่ pattern ที่ยังใช้อยู่ |
| `src/agent_takkub/remote/notify.py:612` | `_gemini_session_uuid` compat shim | 1 ใน 6 compat alias ที่เขียนไว้เป็นกลุ่ม (comment `:603-604`) — อีก 5 ตัวยังมี real caller, ตัวนี้ grep ทั้ง repo ไม่เจอ caller เลย |
| `src/agent_takkub/settings_management/commands.py:46` | dataclass `DeleteRoleCommand` | Create/Update role ผ่าน `CreateRoleCommand`/`UpdateRoleCommand` จริง (`roles_page.py`, `repositories/roles.py`) แต่ delete flow เรียก `roles_repo.delete(name, version)` ตรงๆ ไม่ผ่าน DTO นี้เลย (`roles_page.py:414`) — DTO ถูกสร้างไว้เผื่อแต่ไม่เคยถูก wire |
| `src/agent_takkub/claude_auth_dialog.py` | ทั้งไฟล์ (`ClaudeAuthDialog`, 164 LOC) | grep ทั้ง repo (รวม `.py`/`.md`) เจอแค่ตัวเอง — ไม่มี test, ไม่มี caller ใน `main_window.py`/`user_actions.py`/`settings_window.py` ฟีเจอร์ถูกแทนที่ด้วย Users tab ใน `settings_window.py:2399-2451` แล้ว. ลบไฟล์ + เอา `"agent_takkub.claude_auth_dialog"` ออกจาก `leaf-modules-pure` contract's forbidden list ใน `pyproject.toml` (มิฉะนั้น contract จะอ้างถึงโมดูลที่ไม่มีอยู่จริง) |

**รวม:** 5 ไฟล์แก้ไข, 1 ไฟล์ลบ, ~230 LOC dead code ออก · `docs/architecture/depgraph.json` regenerate แล้ว (module_count 141→140)
+ `pyproject.toml` แก้ contract list ให้ตรง

## กอง B — list ไว้ ไม่ทำ (มี hidden-edge เป็นไปได้)

| symbol | เหตุผลที่ไม่ลบ |
|---|---|
| `src/agent_takkub/lead_bash_audit.py` (ทั้งโมดูล, fan_in=0 ใน depgraph) | Docstring ของตัวมันเองบอกชัดว่าเป็น "Phase 1, audit-only" ที่ตั้งใจให้ wire ผ่าน **`.claude/settings.json` PreToolUse hook** (config string, ไม่ใช่ Python import — เหมือน `pane_guard` ตามที่ godfile-map.md เตือนไว้เรื่อง hook round-trip) มี test จริง (`tests/test_lead_bash_audit.py`) เรียก `audit_lead_bash` ตรงๆ และอยู่ใน list พิเศษที่ `conftest.py:81` (โมดูลที่ copy `RUNTIME_DIR` ตอน import) — เป็น feature ที่จงใจสร้างไว้ให้ wire ภายนอก ไม่ใช่ dead code |
| `src/agent_takkub.design_review_html` (fan_in=0 ตาม depgraph) | grep เจอ real caller ใน `skill_scan.py` และ `routing_planner.py` — grimp มองไม่เห็นเพราะเป็น lazy import ในตัว method (ตรงกับ hidden-edge "Late / lazy import" ใน godfile-map.md) ไม่ใช่ orphan จริง |
| `src/agent_takkub.vault_graph` (fan_in=0 ตาม depgraph) | เดียวกัน — `skill_scan.py:16` `from agent_takkub.vault_graph import analyse` เป็น in-function import ที่ grimp static-scan พลาด |
| `src/agent_takkub.claude_auth_dialog` deletion side-effect: `pyproject.toml:210` มี `agent_takkub.claude_auth_config` ซ้ำ 2 ครั้งใน `leaf-modules-pure` (ไม่เกี่ยวกับไฟล์ที่ลบ แค่บังเอิญอยู่ใกล้กัน) | ไม่ใช่ dead code — เป็น duplicate list entry (M11 ใน audit เมื่อวาน) นอกขอบเขตงานนี้ (ไม่ใช่ dead code, เป็น config cleanup คนละประเภท) ไม่แตะ |

## กอง C — ไม่แตะ (intentional, cross-platform/multi-provider)

ไม่พบ candidate ใหม่ในรอบนี้ที่ต้องระบุเพิ่มเติมนอกเหนือจากของเดิมในระบบ (fallback ของ provider อื่น, platform-specific branch) — สิ่งที่ตรวจแล้วทั้งหมดในกอง A มี string-search ครอบคลุมทั้ง repo ไม่ใช่แค่ import graph จึงไม่กระทบ hidden edges ที่ godfile-map.md เตือนไว้

## เฟส 3 — verify

| check | ผล |
|---|---|
| `ruff check src/ tests/` | **All checks passed** |
| `ruff check tools/ scripts/` | **All checks passed** |
| `lint-imports` (24 contracts — นับใหม่วันนี้ ไม่ใช่ 23 ตามเอกสารเก่า) | **24 kept, 0 broken** — 140 files, 543 dependencies |
| `python tools/gen_import_graph.py --check` | **fresh** (module_count=140) — regenerated after `claude_auth_dialog.py` removal |
| targeted pytest: `test_main_window_status_bar.py`, `test_remote_notify.py`, `test_settings_management_roles.py` | **137 passed, 0 failed** |

หมายเหตุ tooling: worktree นี้ share `.venv` กับ root checkout (editable install ชี้ไป root src) —
ทุกคำสั่งข้างบนต้องรันด้วย `PYTHONPATH=<worktree>/src` prefix ไม่งั้น `grimp`/`lint-imports`/pytest จะ
สแกน root checkout แทน worktree (ยืนยันจากพฤติกรรมจริง: ไม่ใส่ PYTHONPATH แล้ว depgraph ยังเห็น
`claude_auth_dialog.py` ที่ลบไปแล้วในไฟล์นี้)

Full suite ไม่ได้รัน — ตาม test-tier policy (targeted mid-flight, full suite ที่ qa batch gate เท่านั้น)
