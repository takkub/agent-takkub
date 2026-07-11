---
date: 2026-07-11
author: maintainer
status: implemented (Part 1 — ทุกอย่างยกเว้น spawn_engine.py)
topic: central-home migration — เอาของ cockpit ออกจาก repo จริงของ user
follows: 2026-07-11-central-home-audit.md
---

# Central Home — Implementation (Part 1)

Implement ตาม audit + user decision: **ย้ายทุกอย่างออกจาก repo จริง — docs ด้วย,
rtk hook ด้วย (personal ไม่ commit)**. ทำครบ A1 / A2 / A3 / C โดย**ไม่แตะ**
`spawn_engine.py` · `provider_config.py` · `lead_context.py` (backend#2) และ
`settings_management/` (frontend#5).

**หัวใจการออกแบบ: ทุกจุดต่อ (wire point) หลบไฟล์ต้องห้ามได้หมด** เพราะ helper ที่
ไฟล์ต้องห้ามเรียกอยู่แล้ว ถูกทำให้ "ฉลาดขึ้น" ในไฟล์ที่แตะได้ — จึงไม่มี TODO ค้างให้
backend#2 wire ต่อ (ดู §A3).

---

## A1 — Skills → central + junction/symlink

**ของจริงเขียนลง central, project path เป็นแค่ junction (win) / symlink (mac)**
→ New-Skill ไม่ทำ repo ของ user รก (`git status` สะอาด) แต่ทุก CLI ยัง discover
`.claude/skills/<name>` จาก cwd ได้ตามเดิม (junction โปร่งใส).

| จุด | ไฟล์ | ทำอะไร |
|---|---|---|
| central path | `config.py` | `PROJECT_SKILLS_HOME = DATA_HOME/project-skills` + `project_skills_dir(ns)` (traversal-safe เหมือน `_ledger_dir`) |
| create | `skill_scan.create_skill(root, …, project_ns=…)` | เขียน `SKILL.md` ลง `project_skills_dir(ns)/<name>/` (atomic temp→rename) แล้ว `_link_skill_into_project` junction `<root>/.claude/skills/<name>` → central · link fail → rollback (ลบ link + central) |
| repair on open | `skill_scan.ensure_project_skill_links(root, ns)` · เรียกจาก `main_window._open_project_tab` (ไม่ใช่ spawn_engine) | re-link ทุก central skill เข้า project ตอนเปิด tab · ไม่ทับ real user skill ที่ชื่อชนกัน |
| delete | `skill_scan.delete_skill` | **junction-safe**: `_remove_link` reparse point ก่อน แล้วค่อย `rmtree` central real dir — กัน rmtree ทะลุ junction ไปลบ central ผ่าน link (ตาม learned-note) |
| writable gate | `skill_scan.is_writable_skill(path, roots, extra_dirs=…)` + `settings_window._central_skill_dirs()` | junctioned skill resolve เข้า central → ปุ่ม Delete โผล่เพราะ `extra_dirs=[project_skills_dir(ns)]` |
| UI wiring | `settings_window` | `_on_create_skill_clicked` ส่ง `project_ns=self._project` · `_on_catalog_skill_selected` ส่ง `extra_dirs` |

**ทำไมไม่ต้องแตะ `_skill_roots_for_project` / `render_skill_appendix` (spawn_engine/skill_policy):**
scanner พวกนั้น scan `<project>/.claude/skills` อยู่แล้ว — junction ทำให้ central skill
โผล่ที่ path นั้น**โปร่งใส** claude (native cwd discovery), codex/agy (`render_skill_appendix`
อ่าน roots เดิม) เห็นครบ **โดยไม่ต้องเพิ่ม root ใหม่** = multi-provider ผ่านโดยไม่แตะไฟล์ต้องห้าม.

**link helper reuse:** `worktree_manager._make_link` / `_remove_link` (win `_winapi.CreateJunction`
ไม่ต้อง admin · posix `os.symlink`) — ไม่ reinvent.

---

## A2 — AGENTS.md → `.git/info/exclude` (ไม่แตะ .gitignore)

`codex_agents_md._ensure_git_excluded(cwd, "AGENTS.md")` เรียกหลัง plant ไฟล์ที่ **cockpit
เป็นเจ้าของ** (marker) สำเร็จ → append `AGENTS.md` ลง `<repo>/.git/info/exclude` (per-clone,
ไม่ commit) → หายจาก `git status`.

- user-owned AGENTS.md (ไม่มี marker) → return `"user-owned"` ก่อนถึง exclude → **ไม่ถูกซ่อน**
- ไม่ใช่ git repo → no-op · `.git` เป็น **ไฟล์** (linked worktree/submodule) → skip (ไม่ parse)
- idempotent (ไม่เพิ่มบรรทัดซ้ำ) · preserve entry เดิมใน exclude

---

## A3 — rtk hook → central `--settings` (personal, ไม่แตะ repo)

**rtk กลายเป็น toggle ส่วนตัว central** ไม่ใช่ hook ใน `<project>/.claude/settings.json` อีก:

- flag กลาง `SETTINGS_HOME/rtk-enabled.json` (`rtk_helper.rtk_hook_enabled` / `set_rtk_enabled`)
- **hook ฉีดตอน spawn ผ่านไฟล์ `--settings` กลางที่มีอยู่แล้ว** — `hook_wiring._rendered_settings()`
  merge `rtk_hook_fragment()` (PreToolUse Bash) เข้าไฟล์เดียวกับ Stop/Notification/SessionStart
  เมื่อ `rtk_should_inject()` (enabled **และ** binary อยู่บน PATH — กัน `rtk hook claude` พังทุก Bash call)
- `install_rtk(project_root=None)` → set flag + `uninstall_rtk` เก็บกวาด rtk entry เก่าใน project
  settings.json (เก็บ key อื่นของ user, prune container ว่าง, ไม่ลบไฟล์)
- `is_rtk_installed()` → อ่าน flag กลาง (param project_root รับไว้เพื่อ compat แต่ ignore)
- UI: `update_panel._on_install_rtk_clicked` เปลี่ยน copy เป็น "Enable rtk (central, ไม่แตะ repo)"

**ไม่มี TODO ค้างสำหรับ backend#2:** `spawn_engine` เรียก `ensure_hook_settings_file()` อยู่แล้ว
(บรรทัด ~1565, `argv += ["--settings", …]`) → rtk เข้า pane อัตโนมัติเมื่อ enable. **ข้อควรระวัง
สำหรับ merge:** อย่าลบ/ย้าย call `ensure_hook_settings_file()` ออกจาก spawn argv — เป็นทางเข้า rtk เดียว.

> rtk verify: audit ระบุว่า claude รวม PreToolUse hook จาก `--settings` file ได้จริง — ยึดตามนั้น.
> ถ้า field พบว่า rtk เงียบ ให้เช็คว่า claude เวอร์ชันนั้น honour PreToolUse จาก `--settings` layer.

---

## C — LLM docs + screenshots → central (env pointer)

- `config.DOCS_DIR = RUNTIME_DIR/docs` + `project_docs_dir(ns)` (traversal-safe)
- **env pointer:** `pane_env._apply_artifacts_dir` (เรียกทุก spawn รวม codex/agy อยู่แล้ว) stamp
  `TAKKUB_DOCS_DIR = RUNTIME_DIR/docs/<project>` เพิ่มจาก `TAKKUB_ARTIFACTS_DIR` — คิดจาก `RUNTIME_DIR`
  ณ call time (monkeypatch/multi-instance safe) + allowlist. **ไม่ต้องแตะ spawn_engine** เพราะ call มีอยู่แล้ว
- **wording** ชี้ path กลางผ่าน env:
  - `CLAUDE.md` (repo นี้): pipeline shots → `$TAKKUB_ARTIFACTS_DIR/screenshots/` · design-review/system-overview/guides → `$TAKKUB_DOCS_DIR/...`
  - role files: `critic.md` (design-review + shots read), `docs.md` (guides), `qa.md` (SHOT_DIR → `$TAKKUB_ARTIFACTS_DIR/screenshots` แก้บั๊ก relative `runtime/exports/...` ที่เคยตกใน repo)
- **converter รับ path กลางได้:** `design_review_html._inline_shot` `expandvars`+`expanduser` shot path
  → `$TAKKUB_ARTIFACTS_DIR/screenshots/x.png` ใน front matter (เป็น file content ไม่ผ่าน shell) resolve
  ตอน convert · md path argument ผ่าน shell expand อยู่แล้ว (absolute) — converter รับตรง

---

## Cross-platform

- link ทุกจุดผ่าน `_make_link`/`_remove_link` — gate `sys.platform` ครบ 2 ฝั่ง (junction/symlink)
- test `TestCentralSkills` รัน create→link→scan→delete **จริง native** → บน Windows คือ junction,
  บน macOS CI คือ symlink → **CI matrix win+mac exercise ทั้ง 2 link kind** โดยไม่ mock semantics
- rollback / delete junction-safe test ครอบ path ที่ลบผ่าน link ได้

## Tests (targeted, ผ่านหมด)
`test_rtk_helper` (rewrite: central flag/inject/uninstall) · `test_skill_scan` (+`TestCentralSkills`,
`TestConfigCentralPaths`) · `test_codex_agents_md` (+`TestGitExclude`) · `test_hook_wiring`
(+`TestRtkInjection`) · `test_design_review_html` (+expandvars/absolute) · `test_pane_artifacts_dir`
(+`TestApplyDocsDir`) · settings_window/skill_policy/worktree/main_window/spawn-argv regression เขียว ·
ruff + ruff format + import-linter (19/19 KEPT) ผ่าน.

## เหลือ / follow-up (ไม่อยู่ scope Part 1)
1. **one-time migration ของ legacy `<project>/.claude/skills` ที่ cockpit สร้างไว้ก่อน** — audit 3.4
   (แยก git-tracked=user vs untracked=cockpit) ยังไม่ทำอัตโนมัติ (เสี่ยงลากของ user ไป central ผิด) —
   ปัจจุบัน skill เก่ายังใช้ได้ (real dir ใน project), ของใหม่ไป central. เสนอทำเป็น explicit migrate ปุ่ม.
2. **rtk enable/disable UI ที่ชัดกว่าปุ่ม Install** (toggle + disable) — Part 1 แค่ repurpose ปุ่มเดิม.
3. spawn_engine ฝั่ง backend#2: ยืนยันว่า `ensure_hook_settings_file()` ยังถูกเรียกใน argv หลัง settings-redesign.
