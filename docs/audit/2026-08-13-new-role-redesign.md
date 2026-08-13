# New Role view redesign — 2026-08-13

Scope: `settings_window.py` `_build_new_role_view()` (~1435-1566 pre-change)
and `_reload_new_role_skills()` (~1602-1622 pre-change). No behavior change
to `_on_create_role_clicked` / `_apply_new_role_tools_policy` — the create
transaction is untouched.

## Findings (debug-mantra applied)

### #1 Horizontal overflow — root cause confirmed by measurement

`_reload_new_role_skills` built each skill row as a single
`QCheckBox(f"{name} — {description}")` with no wrap. A long real skill
description (e.g. `debug-mantra`'s ~250-char description) forced the
checkbox's unwrapped `sizeHint().width()` to blow out the whole form.

Measured with an offscreen repro (`debug-mantra`-length description seeded
into a temp `.claude/skills/`):

| | skills container `sizeHint().width()` |
|---|---|
| before (packed checkbox text) | **2405px** |
| after (name-only checkbox + wrapped description label) | **554px** |

Since the view sits in a `QScrollArea` with `setWidgetResizable(True)`, a
child minimum width that large forces a horizontal scrollbar and pushes
right-anchored widgets (the Label field next to Name, the MCP+Plugins
toggle) off the visible area — exactly what the user's screenshot showed.

**Fix:** each skill is now its own row widget — `QCheckBox(skill.name)` only,
plus a separate `QLabel` with `setWordWrap(True)` for the description
(the same wrapped-label pattern already used for `panelHint` text elsewhere
in this file, which never overflows). The skill list also moved into a
`QScrollArea` with `setHorizontalScrollBarPolicy(ScrollBarAlwaysOff)` and a
bounded `setMaximumHeight(220)`, so no row can force page width again.

### #2 Overlapping text at the first skill row — reproduced, root cause confirmed

Reproduced with an offscreen script instrumenting `_reload_new_role_skills`
call sites: it fires **twice** during dialog construction with no event-loop
turn in between —
`_build_new_role_view` → `_reload_new_role_skills()` (direct, populates the
picker for the first time), then immediately after,
`_build_skill_catalog_view` → `_reload_skill_catalog()` →
`_reload_new_role_skills()` (the Skill Catalog view build also refreshes the
New Role picker to pick up any skill the Skill Catalog created).

The old clear-loop only called `w.deleteLater()` on removed rows.
`QLayout.takeAt()` detaches a widget from the layout but does **not** hide
or reparent it — the widget stays a live, visible child at its old geometry
until Qt's deferred `DeferredDelete` event actually runs (next event-loop
tick). Because the second reload ran before that tick, the second call's
fresh `addWidget()` placed a new row at the same position while the first
call's row was still on screen and unhidden → the observed overlapping
text.

Verified via an offscreen script inspecting widget state directly (proof,
not inference):
- `dlg._nr_skills_container.findChildren(QCheckBox)` returned **2** checkbox
  objects after init (one stale, one live) — confirming the double-reload.
- Before the fix, the stale row's `isHidden()` was `False` (rendering,
  overlapping the live row).
- After adding `w.hide()` before `w.deleteLater()` in the clear-loop, the
  stale row's `isHidden()` became `True` and it was confirmed detached from
  the layout (`indexOf(row) == -1`), while the live row stayed visible and
  in-layout — same object-count-alive-pending-deletion (expected, since
  actual C++ destruction is still deferred) but no longer paints.

**Fix:** `w.hide()` before `w.deleteLater()` in the clear-loop — standard
Qt idiom for "detach now, reclaim later" when a rebuild can race the
deferred-delete event.

## Redesign (item #3-#6 from task spec)

- Split the single flat form into 5 cards matching the design system's
  panel + `_build_card_header` pattern already used in
  `_build_providers_roles_view`: **Identity** (Name/Label/Accent),
  **Placement** (Grid column/row), **Tools** (MCP+Plugins toggle),
  **Skills**, **Instructions**.
- Skills card: added a filter `QLineEdit` (hides non-matching rows via
  `setVisible`, doesn't remove them — `_nr_skill_checks` still reflects
  every scanned skill regardless of filter text, so existing tests asserting
  on that list are unaffected), a `checked/total` counter label, and a
  per-row source badge (`· project` / `· cockpit`, via the existing
  `skill_scan.is_writable_skill()` check also used by the Skill Catalog's
  delete-button gate).
- Placement card: added a live hint label ("role นี้จะแสดงที่ ... แถวที่ ...")
  updated on both the column combo and row spinbox changing.
- Instructions card: added a "เริ่มจากเทมเพลต" combo + button sourcing from
  `config.AGENTS_DIR` (`.claude/agents/{analyst,designer,docs,security}.md`
  — the 4 docs that ship with no matching `roles.py` `Role()` entry, so
  they were otherwise unreachable). Strips the leading curation HTML
  comment + YAML frontmatter before seeding the Instructions box; prompts
  before overwriting non-empty Instructions.

## Test coverage

`tests/test_settings_window.py::TestNewRoleView` and
`::TestNewRoleSkillPicker` (12 tests) run unmodified against the new
implementation and pass — the `_nr_skill_checks` list shape
(`list[tuple[SkillInfo, QCheckBox]]`) and every field's `.setText`/
`.setChecked` API surface used by those tests is preserved.

Full file: `pytest tests/test_settings_window.py -q` → all green (no `-k`
filter), run via system Python + `PYTHONPATH=src` (no `.venv` exists in this
worktree; `ruff check` also passes clean on the touched file).
