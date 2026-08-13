# QA batch gate — autoskills installer + New Role redesign + UI wiring

Date: 2026-08-13
Branch: `wt/frontend-1786602192` (merged frontend redesign + backend `autoskills_installer`)
Scope commits: `41b5d00` `f024997` `c2bab3b` `1cdecac` `dd5b294`

## 0. Environment

No pre-existing venv in this worktree. Created one fresh:

```
python -m venv .venv-qa
./.venv-qa/Scripts/python.exe -m pip install -e ".[dev]"
```

Verified editable install resolves into `src/` (not a stale wheel):
`pip show agent-takkub` → `Editable project location: <worktree>`,
`import agent_takkub` → `<worktree>/src/agent_takkub/__init__.py`.

All commands below ran through `.venv-qa/Scripts/python.exe`. `.venv-qa/`
left in place, not deleted — safe to remove, nothing depends on it outside
this session.

## 1. Full pytest suite (once, no `-k`)

```
./.venv-qa/Scripts/python.exe -m pytest -q --junit-xml=...
```

**Result: 5457 tests, 3 failed, 7 skipped, 0 errors, 390.17s. Exit code 1.**

No exit-127 / silent PyQt6 abort — the run completed a full FAILURES section
and short test summary every time it was invoked (checked 3x with different
capture methods to rule out a truncated/killed run).

### The 3 failures — all pre-existing, unrelated to this branch's diff

Confirmed by: (a) isolated re-run of each test alone, (b) `git diff main...HEAD --stat`
showing this branch never touches the files these tests exercise
(`pane_env.py`, `roles.py`, `custom_roles.py`, `pane_tools_policy.py` — the
branch only touched `autoskills_installer.py`, `settings_window.py`,
`cockpit_theme.py`, `settings_management/window.py`).

1. `tests/test_orchestrator_env_allowlist.py::test_build_pane_env_includes_path`
   — asserts `_build_pane_env()`'s PATH equals exactly the monkeypatched
   value, but `pane_env.py`'s Windows PATH sanitizer (`_sanitize_windows_path`,
   from `4dc974a`, unrelated commit) intentionally prepends `%APPDATA%\npm`
   when it's missing. The test's exact-equality assertion is stale relative
   to that intentional behavior; not something this branch introduced.

2. `tests/test_pane_tools_policy.py::TestKnownRoles::test_includes_registered_custom_role`
   and
3. `tests/test_roles.py::TestByName::test_unknown_role_returns_none`
   — cross-test pollution: 11 different test files register a role named
   `data-eng` via `custom_roles.create_role()` without full teardown, so
   whichever of these two runs after another such test in full-suite order
   sees `"data-eng"` already registered globally. Reproduced in isolation:
   running all 3 failing tests together with `-p no:randomly` → only test
   #1 fails, #2 and #3 pass — confirming pollution, not a real defect in
   either module.

**None of the 3 are caused by this round's work.** No exit-127/abort was
observed for any test in the suite.

## 2. Ruff + import-linter

```
./.venv-qa/Scripts/python.exe -m ruff check src/ tests/     → All checks passed!
./.venv-qa/Scripts/lint-imports.exe                          → Contracts: 23 kept, 0 broken.
```

Both green.

## 3. Targeted risk areas

### 3.1 `autoskills_installer.install()` — overwrite/rollback/staging fallback

`tests/test_autoskills_installer.py` — 37 passed, 2 skipped (0.47s).

- **Unselected skills never touch real disk in staging mode**: already
  covered and asserted directly —
  `test_install_staging_unselected_entries_never_touch_real_project`
  (`tests/test_autoskills_installer.py:446`) asserts
  `not (real_skills / "skill-b").exists()` for a name the staged run
  produced but the user didn't select. No gap here; nothing added.
- Direct/fallback overwrite-and-restore, rollback-on-path-escape, and
  staging-unavailable fallback are each covered by a dedicated test
  (`test_install_direct_unselected_collision_is_restored`,
  `test_install_direct_rolls_back_on_path_escape`,
  `test_install_staging_rolls_back_on_path_escape`,
  `test_install_falls_back_to_direct_when_staging_unavailable`). All pass.
- The 2 skips are the two nested-symlink path-escape tests
  (`test_escaped_entries_symlink_outside_project_is_flagged`,
  `test_escaped_entries_nested_symlink_outside_project_is_flagged`) —
  each wraps `Path.symlink_to()` in `try/except OSError: pytest.skip(...)`.
  Confirmed this is a legitimate Windows non-admin permission skip, not a
  papered-over failure: ran them verbosely, both hit the `except OSError`
  branch and print the skip reason `"symlink creation not permitted in
  this environment"` — this machine's account can't create symlinks.

### 3.2 UI worker thread — preview/install never on the Qt main thread

Verified by code inspection (`src/agent_takkub/settings_window.py:491-524`):
`_AutoskillsPreviewThread`/`_AutoskillsInstallThread` are real `QThread`
subclasses whose `run()` is the only call site of
`autoskills_installer.preview()`/`.install()`; the click handlers
(`_on_autoskills_scan_clicked` L2948, install path L2987) call `.start()`,
never `.run()` directly — `.start()` is what makes Qt actually spawn the
thread and invoke `run()` off the main thread. Existing tests
(`TestAutoskillsPanel` in `test_settings_window.py`) monkeypatch `.start()`
and assert it's called with the right `project_root`/`selected_names`,
confirming the wiring without needing a real thread in the test process.
No gap; nothing added.

### 3.3 New Role skill-row regression guard — **gap found, test added**

No existing test measured the New Role skills container's width with a
realistic long description, despite this being the exact bug the user saw
(`docs/audit/2026-08-13-new-role-redesign.md` finding #1: 2405px before →
554px after, measured with a debug-mantra-length description).

Added `TestSkillDescriptionClamp::test_skills_container_does_not_widen_with_long_descriptions`
in `tests/test_settings_window.py` — seeds a real `debug-mantra`-scale
(~280 char) skill description into a temp `.claude/skills/`, builds the
New Role view, and asserts `dlg._nr_skills_container.sizeHint().width() < 900`.

Verified red→green manually (temporarily reverted
`_build_new_role_skill_row` to the old single-checkbox
`f"{name} — {description}"` pattern): the test failed at **2000px** against
the broken version (matching the audit's finding), then passed again once
the real fix (name-only checkbox + wrapped label) was restored. `git diff`
confirmed the temporary revert left no trace afterward.

## 4. Live `autoskills` CLI check — network was available

Network was up (`curl -sI https://registry.npmjs.org/` → 200 OK), so ran
the real CLI instead of guessing:

```
npx --yes autoskills@latest --dry-run --agent claude-code
```

against this project root. **It worked** — v0.3.6, detected Node.js / Bash
/ Python / Pytest and proposed 8 skills, exit 0. This is evidence *against*
the specific worry backend flagged (staging-mirror breaking stack
detection) — the CLI ran fine from a real cwd with no special setup.

### 4.1 New finding: `_parse_preview_output` doesn't parse the real CLI's current output format — **BLOCKING**

Fed the real dry-run output above into
`agent_takkub.autoskills_installer._parse_preview_output()` directly:

```
stack: []
skills: []
```

Root cause: the parser's regexes assume `key:` header lines (`_HEADER_RE`)
and `-`/`*`/`•` bullets (`_BULLET_RE`). The real CLI (v0.3.6) instead uses
box-drawing prompts (`◆ Detected technologies:` — starts with `◆`, not
`[A-Za-z]`, so `_HEADER_RE` never matches) and numbered entries
(`1. wshobson › nodejs-backend-patterns ← Node.js` — starts with a digit,
not a bullet char, so `_BULLET_RE` never matches; no URLs present either,
so the URL-bullet fallback also finds nothing).

Practical effect, traced through `settings_window.py:2961`
(`_on_autoskills_preview_ready`): with `result.skills == []`, the UI shows
`QMessageBox.information("autoskills ไม่พบ skill ที่เข้ากับ stack ของโปรเจคนี้")`
— **"no matching skill found"** — for every real user on the actual
published CLI right now, even though it detected a stack and proposed 8
skills. The "Auto-detect skills" button that was just wired up in this
branch does not currently work end-to-end against the real tool it's a
bridge to.

This is new code from this branch (`41b5d00`), not pre-existing, and it was
only catchable with live network access (which is why backend flagged it
as unverifiable in their environment). `raw_output` is preserved on the
`PreviewResult` so no data is lost, but the current UI path never surfaces
it to the user when `skills` parses empty — it just says nothing was found.

Reproduce anytime with the same `npx --yes autoskills@latest --dry-run
--agent claude-code` invocation from this project root, then feed stdout+stderr
into `agent_takkub.autoskills_installer._parse_preview_output()`.

## Verdict

- Full suite / ruff / import-linter: **green modulo 3 pre-existing,
  unrelated failures** (documented above, not from this round's work).
- Targeted risk areas 3.1–3.2: **covered already**, no gaps.
- Targeted risk area 3.3: **gap found and closed** — regression test added,
  red→green verified.
- Live CLI check (item 4): **BLOCKING correctness bug found** —
  `_parse_preview_output` doesn't understand the real `autoskills@0.3.6`
  output format, so the auto-detect feature silently reports "no skills
  found" against the actual tool. Recommend routing back to backend before
  merge: either rewrite the parser against real CLI output samples, or add
  an `--json`-shaped flag if the CLI supports one on a newer version, and
  add a fixture test using this transcript so a future CLI format change
  is caught automatically instead of only on a manually-triggered live run.
