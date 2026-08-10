# agent-takkub v1.0.50 — Final Release QA Gate (closure rerun)

Date: 2026-08-10 (Asia/Bangkok)
Role: final release re-verification QA
Verdict: **GO**

## Executive summary

The v1.0.50 closure gate is green. The formatting-only follow-up in
`tests/test_resume_session_picker.py` exactly matches the sole Ruff change
recorded by the preceding gate: the multiline
`_resume_uuid_matches_provider_cwd(...)` assertion near line 302 is now on one
line; no behavior, test data, or assertion semantics changed in that closure
delta. Both Ruff gates now pass across the exact **16-file** release target.

The focused session-picker suite passes **48/48**, Import Linter keeps all
**23/23** contracts, and `git diff --check` passes. The wheel remains the exact
previously verified artifact, and the npm dry-run still contains only that
wheel. Because the closure delta is confined to an unshipped test-file format
change, the already-recorded installed-mode **7/7** and release matrix
**444/444** evidence remains applicable and was not needlessly rerun.

## 1. Artifact identity

**PASS.**

- Exactly one `dist/*.whl` exists.
- File: `dist/agent_takkub-1.0.50-py3-none-any.whl`
- Size: **1,930,818 bytes**
- SHA256: `6aabc2c8046fa915c4ac14a63234d367c3f36fc07e3b3cd225fa2dee664e7fc4`
- Wheel members: **193**
- Metadata: `agent_takkub-1.0.50.dist-info/METADATA`
- Metadata version: **1.0.50**
- Neither root scratch file nor `tests/test_user_actions_provider_switch.py`
  is present in the wheel.

## 2. Exact-wheel installed-mode proof

**PASS.**

Created a fresh venv with `.venv/Scripts/python.exe -m venv`, then installed
the exact wheel with that venv's Python:

```powershell
<fresh-venv>/Scripts/python.exe -m pip install --no-deps ./dist/agent_takkub-1.0.50-py3-none-any.whl
```

Fresh isolated root:

`runtime/exports/2026-08-10/agent-takkub/qa-1050-final-8a91d48f3dc945a982a19c1b09d7fcde`

Evidence from the installed interpreter with `PYTHONPATH` removed and an
isolated `AGENT_TAKKUB_HOME`:

- Imported version: `1.0.50`.
- Import path: fresh venv `Lib/site-packages/agent_takkub/__init__.py`.
- `config.is_installed_package()` is `True`.
- `config.DATA_HOME` is the isolated QA home.
- `config.ASSETS_ROOT` is the installed
  `site-packages/agent_takkub/_assets` directory.
- `_assets/CLAUDE.md` exists.
- Claude Lead context rendered successfully. SHA256:
  `26c06c45a201f67c91086440095bbfde717c7162adcb0ff80f9b4943895cb965`.
- Codex, Gemini, OpenCode, Kimi, and Cursor each declare
  `context_strategy == "agents_md_file"` and each rendered a Lead `AGENTS.md`.
- All five non-Claude outputs had the same SHA256:
  `e1f60658cd15fb553c67f40836aa1c1f5855d0b7c9baf22dfeacece1cff297c9`.
- Claude and every non-Claude output contained every checked policy marker:
  provider-neutral applicability; the explicit provider list; provider
  substitution; cockpit source/test protection; the direct-edit prohibition;
  and the "when unsure, delegate immediately" boundary.

## 3. Architecture and test gates

### Import Linter

**PASS — architecture blocker resolved.**

Command:

```powershell
.venv/Scripts/lint-imports.exe
```

Result:

- Analyzed 138 files and 528 dependencies.
- **23 contracts kept, 0 broken.**
- `remote-bolt-on-isolation` is **KEPT**.
- The former forbidden `spawn_engine -> remote.notify` dependency is gone;
  provider-aware Gemini resume validation now reaches the core
  `gemini_helper` resolver without making core depend on the optional remote
  bolt-on.

### Installed-mode gate

**PASS: 7/7.**

Command used `.venv/Scripts/python.exe` directly with `PYTHONPATH` removed:

```powershell
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.venv/Scripts/python.exe -m pytest -q tests/test_installed_mode_gate.py
```

### Release regression matrix

**PASS: 444/444.**

The same 21-file matrix from the previous final gate was run with
`.venv/Scripts/python.exe` and `PYTHONPATH=<repo>/src`:

- `tests/test_version_sync.py`
- `tests/test_provider_spec_effort.py`
- `tests/test_provider_project_scope.py`
- `tests/test_provider_multiline_newline_seq.py`
- `tests/test_provider_models.py`
- `tests/test_provider_install.py`
- `tests/test_provider_config.py`
- `tests/test_provider_toggle_orchestrator.py`
- `tests/test_provider_substitution_note.py`
- `tests/test_provider_state.py`
- `tests/test_cursor_provider.py`
- `tests/test_kimi_provider.py`
- `tests/test_opencode_provider.py`
- `tests/test_user_profile.py`
- `tests/test_user_actions_provider_switch.py`
- `tests/test_gemini_helper.py`
- `tests/test_lead_provider_unlock.py`
- `tests/test_project_rules.py`
- `tests/test_codex_agents_md.py`
- `tests/test_resume_session_picker.py`
- `tests/test_remote_notify.py`

Collection proof reported **444 tests**, and execution completed at 100% with
no failures. This is the prior 441-test matrix plus three new architecture-fix
regressions. Those three cases were also rerun explicitly and passed **3/3**:

- engine Gemini validation delegates to the provider-core resolver;
- Gemini Lead API resume uses provider-aware validation;
- remote history resolution delegates to the provider-core resolver.

## 4. Ruff and whitespace gates

### Release-target Ruff

Release-target scope is the 15 tracked changed Python files plus the untracked,
release-relevant `tests/test_user_actions_provider_switch.py`: **16 files**.
The deliberately excluded root scratch scripts were not included.

- `ruff check`: **PASS** — all checks passed.
- `ruff format --check`: **PASS** — all 16 files already formatted.
- The only closure delta against the preceding recorded gate is Ruff's proposed
  formatting change in `tests/test_resume_session_picker.py`: collapsing the
  multiline `_resume_uuid_matches_provider_cwd(...)` assertion near line 302
  to one line. The assertion call, arguments, and expected truth value are
  unchanged.

### Whitespace

`git diff --check`: **PASS** (exit 0).

Git emitted the existing informational warning that
`tests/test_user_profile.py` will change CRLF to LF the next time Git touches
it; no whitespace error was reported.

## 5. npm package dry run

**PASS.**

`npm pack --dry-run --json` reported:

- Package: `agent-takkub@1.0.50`.
- Total files: **14**.
- Wheel count: **1**.
- Included wheel:
  `dist/agent_takkub-1.0.50-py3-none-any.whl`.
- No older wheel is included.
- Neither `test_agy_proj.py` nor `test_gemini_chats.py` is included.
- `tests/test_user_actions_provider_switch.py` is not included in the npm
  artifact, as expected for a source regression test.

## 6. Worktree and release hygiene

Closure QA did not edit source, tests, package metadata, lock files, wheel, or
scratch files. The only repository file updated by closure QA is this required
report.

The two untracked root files are deliberately treated as scratch and excluded
from Ruff release scope, wheel contents, and npm contents:

- `test_agy_proj.py`
- `test_gemini_chats.py`

`tests/test_user_actions_provider_switch.py` is **release-relevant**. It was
included in both the 444-test matrix and the 16-file Ruff scope. It is still
untracked and must be intentionally included by the release owner when staging
the source release; it is correctly absent from binary/npm artifacts.

## 7. Closure rerun evidence

All commands used the repository `.venv` directly; `uv` was not used.

- `.venv/Scripts/python.exe -m ruff check <exact 16 files>`: **PASS**, `All
  checks passed!`
- `.venv/Scripts/python.exe -m ruff format --check <exact 16 files>`: **PASS**,
  `16 files already formatted`
- `.venv/Scripts/python.exe -m pytest -q
  tests/test_resume_session_picker.py`: **PASS, 48/48**
- `.venv/Scripts/lint-imports.exe`: **PASS**, 138 files and 528 dependencies
  analyzed; **23 kept, 0 broken**
- `git diff --check`: **PASS** (exit 0); only the existing CRLF-to-LF
  informational warning for `tests/test_user_profile.py` was emitted
- `dist/*.whl`: exactly **1** file,
  `agent_takkub-1.0.50-py3-none-any.whl`
- SHA256: `6AABC2C8046FA915C4AC14A63234D367C3F36FC07E3B3CD225FA2DEE664E7FC4`
- `npm pack --dry-run --json`: `agent-takkub@1.0.50`, **14 files**, with
  exactly one wheel entry:
  `dist/agent_takkub-1.0.50-py3-none-any.whl`
- Reused unchanged evidence: installed-mode **7/7** and release matrix
  **444/444**, because the only intervening change was formatting in an
  unshipped test file

## Final decision

**GO.**

All v1.0.50 closure gates pass. The release owner should intentionally include
the release-relevant untracked `tests/test_user_actions_provider_switch.py`
when staging the source release. The exact wheel requires no rebuild and is
ready to ship with the verified identity above.
