# QA Batch Gate — Token-reduction wave (2026-08-04)

Scope: 3 changes on `main` — CLAUDE.md diet + new `docs/lead/`, `qa.md` trim, role_memory entry-cap + spawn-snapshot prune.

## Result: **FAIL**

## 1. Full suite

- `pytest -q`: **1 FAILED**, rest green (72 files-worth of dots, 2 skipped as usual).

```
FAILED tests/test_agent_role_files_have_git_guard.py::TestRoleFileGitGuard::test_forbids_git_push[qa.md]
AssertionError: qa.md does not mention 'git push' prohibition
assert 'git push' in <qa.md content>
```

Root cause (read, not guessed): `.claude/agents/qa.md`'s trim pass (commit `bc47e6c`, "ลด token qa.md ~4.7k → ~2.6k") rewrote the version-control section to say **"ห้าม `git commit`/`push`/`reset --hard`/`push --force`/..."** — the literal substring `git push` no longer appears (it's now folded into a slash-joined list: `commit`/`push`/...). `tests/test_agent_role_files_have_git_guard.py` asserts the literal substring `"git push"` is present in every role file to guard against a trimmed role file silently dropping the push prohibition. This is a real regression introduced by the qa.md diet commit, not a stale/flaky test — the guard is doing its job.

- `ruff check src tests`: clean, no issues.
- `ruff format --check src tests`: clean, 372 files already formatted.
- `lint-imports`: clean, 23/23 contracts kept (0 broken).

## 2. Targeted smoke tests

- **`lc._build_lead_context_text('agent-takkub', REPO_ROOT)` length**: **18,531 chars** — within the expected ~18-19k range (down from the pre-diet ~40k). No error.
- **`docs/lead/cli-reference.md` and `docs/lead/patterns.md` exist**: confirmed (8.3K and 8.7K respectively).
- **CLAUDE.md references both**: confirmed — `docs/lead/cli-reference.md` referenced at CLAUDE.md:41, `docs/lead/patterns.md` referenced at CLAUDE.md:35/80/81.
- **role_memory entry-cap**: `_MEM_MAX_ENTRY_CHARS = 600` present at `src/agent_takkub/role_memory.py:156`; `tests/test_role_memory.py -k "truncat or curat or cap"` → 14/14 pass, covering truncation behavior on long entries.

## Verdict

Everything **except the qa.md role-file git-guard** is green. The `git push` prohibition wording survived semantically (still forbidden, still in a `ห้าม` list) but no longer contains the literal substring the guard test checks for — this is a genuine test failure caused by the trim wording change, not a false positive to wave through. Needs either the guard test relaxed to match the new phrasing, or `qa.md`'s prohibition line adjusted to keep the literal `git push` substring.

**Not fixed here per task scope ("แดง = รายงาน test+traceback ห้ามแก้เอง").**
