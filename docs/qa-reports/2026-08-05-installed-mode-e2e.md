# Installed-mode E2E gate — 1.0.47 (commit 099b15d)

**Role:** devops · **Date:** 2026-08-05 · **Verdict: proceed to publish**, with one CI caveat flagged below (not a production bug, see §3).

Directive under test: "ทุกคนที่ใช้จะไม่บั๊ก ได้ของเหมือนฉัน" — proving the *installed* wheel behaves identically to the dev checkout, specifically that the new graft-autobuild / externalized-store feature (099b15d) never writes into a user's repo.

No code was changed. No commit/push/publish was performed — reporting only.

---

## 1. Wheel + npm package (1.0.47)

- Removed stale `dist/agent_takkub-1.0.46-py3-none-any.whl` before building (lexicographic-sort footgun documented in `docs/release-checklist.md`).
- Built via `python -m build --wheel --outdir <repo>/dist <repo>` from a **cwd outside the repo** (scratchpad), using the repo's `.venv` — avoids the `build/` staging-dir module-shadow trap.
- `dist/` now contains exactly one wheel: `agent_takkub-1.0.47-py3-none-any.whl`.
- Verified present inside the wheel (the 1.0.44 docs-pointer regression class does **not** recur):
  - `agent_takkub/graft_autobuild.py`
  - `agent_takkub/graft_store.py`
  - `agent_takkub/_assets/docs/lead/cli-reference.md`, `agent_takkub/_assets/docs/lead/patterns.md`
  - `agent_takkub/_assets/CLAUDE.md` + all 16 `.claude/agents/*.md`
- `npm pack --dry-run`: tarball is `agent-takkub-1.0.47.tgz`, 14 files, and ships `dist/agent_takkub-1.0.47-py3-none-any.whl` (the freshly built one — no 1.0.46 leftover). Confirmed dry-run wrote no file to the repo (`git status` clean after).

## 2. Installed-mode real venv (not editable, not PYTHONPATH)

Built a clean venv outside the repo, `pip install`ed the wheel with no `-e`, ran everything from an outside-repo cwd:

- `import agent_takkub.graft_store` → OK, resolves to `site-packages/agent_takkub/graft_store.py` (not the repo checkout).
- `config.DATA_HOME` → `C:\Users\monch\.agent-takkub` (installed default, **≠ REPO_ROOT** — this is the condition the whole feature exists to handle correctly).
- `graft_store.GRAFT_STORE_ROOT` → `C:\Users\monch\.agent-takkub\graft-graphs\<instance-hash>` — under `Path.home()`. Correct.
- `takkub doctor` (from the installed `Scripts\takkub.exe`, outside-repo cwd): exit 0, `Summary: 32 ok, 1 skip, 6 info`. `[installed]` section fully green (`assets-claude-md`, `assets-role-files`, `assets-skill-files`, `cli-bin`, `runtime-writable`). `[graft]` section shows `check_graft` → `✓ cli 0.8.2` (found on PATH in this baseline run).

## 3. No repo pollution — direct proof (the core claim)

Created a throwaway git repo (`pollution-test-repo`) outside the cockpit's own tree, with a tracked `.gitignore` and one committed file. Recorded SHA-256 of `.gitignore` and `git status --ignored` **before**. Called `graft_autobuild._build_one(target)` directly from the **installed venv** against that repo (real `graft build` subprocess, not mocked/skipped).

**After:**
- `.gitignore` SHA-256: byte-identical to before.
- `git status --ignored`: empty, "nothing to commit, working tree clean" — identical to before.
- Full file listing of the repo tree: unchanged (`.gitignore`, `main.py` only — no `.ignore`, no `graft/`, no `graft-graphs/`).
- The build genuinely ran (not silently skipped): confirmed the real graph (`INDEX.md`, `main.md`, `.graph/wiring.json`, `.cache/*`, `source.json`) landed entirely under `C:\Users\monch\.agent-takkub\graft-graphs\<instance-hash>\<target-hash>\`, with `source.json` correctly pointing back at the test repo.

**This is the strongest possible evidence the #146-follow-up fix works in the actual installed artifact**, not just in a mocked unit test.

## 4. CI matrix (commit 099b15d, run 31022559742)

| Job | Result |
|---|---|
| `installed-gate (windows-latest)` | ✅ success |
| `installed-gate (macos-latest)` | ✅ success |
| `lint-and-test (windows-latest)` | ✅ success |
| `lint-and-test (macos-latest)` | ❌ **failure** — 1 test |
| `lint-and-test (ubuntu-latest)` | ❌ **failure** — same 1 test |

Note the matrix now includes `ubuntu-latest` too (added for #105 Phase B headless entrypoint, per `ci.yml`'s own comment "All three must stay green") — not just windows+macos.

**Root cause (fully diagnosed, not a production bug):** the single failure on both red jobs is the *same* new test, `tests/test_graft_store.py::test_graft_store_root_never_under_data_home` (added in 099b15d itself). It asserts `GRAFT_STORE_ROOT` (module attribute) is rooted under `Path.home()`. But `tests/conftest.py`'s **autouse** `_isolate_runtime` fixture (lines 167-169, pre-existing, added specifically to stop tests writing into this repo's own tree) unconditionally monkeypatches `graft_store.GRAFT_STORE_ROOT` to `tmp_path/_isolated_runtime/graft-graphs` for *every* test, including this one — so the test never actually observes the real computed constant, only pytest's own tmp dir.

Whether that assertion then passes or fails is an accident of where each CI runner's OS puts its temp directory relative to `$HOME`:
- **Windows** Actions runners: `TEMP` = `C:\Users\runneradmin\AppData\Local\Temp\...` → *is* under home → test passes by coincidence.
- **macOS**: `/private/var/folders/.../T/pytest-of-runner/...` → not under `/Users/runner` → fails.
- **Ubuntu**: `/tmp/pytest-of-runner/...` → not under `/home/runner` → fails.

All 5080 other tests pass on all three OSes on both red jobs (`1 failed, 5080 passed, 6 skipped`). This is a test-authoring bug (asserting against a value the test harness itself deliberately overrides), not evidence the shipped feature is broken — §3 above independently proves the real behavior is correct, outside pytest's fixture chain entirely, against the actual built wheel.

`installed-gate` — the check `docs/release-checklist.md` explicitly calls the authoritative "does the packaged artifact actually work" signal ("ถ้าอันนี้แดง ห้าม release ต่อให้ pytest อื่นเขียวหมด") — is green on both shipped OSes (windows/macos) and does **not** exercise `test_graft_store.py` at all (it only runs `test_installed_mode_gate.py`).

**Recommendation for whoever fixes the test (not done here — report only):** either give `test_graft_store_root_never_under_data_home` its own un-isolated fixture scope (e.g. `monkeypatch.undo()` before reading, or compute the constant fresh via `importlib.reload` in a subprocess), or move it to a subprocess-based check like `test_installed_mode_gate.py` does — asserting against a live process's fresh import, not the autouse-patched in-process value.

## 5. No-graft machine (graft absent from PATH)

Simulated by stripping `AppData\Roaming\npm` (where `graft.cmd` lives) from `PATH` for a child process — did not touch the real install.

- `graft_autobuild._graft_cli()` → `None`.
- `graft_autobuild._build_one(target)` → returns cleanly, no exception, no subprocess spawned.
- `graft_autobuild.build_all_projects_async()` (the boot-time sweep, run against the real `projects.json`) → returns cleanly, no exception.
- `takkub doctor` (graft-less PATH, UTF-8 forced to dodge the known cp874 console-encoding trap) → exit 0, `[graft]` section degrades to `⚠ cli — graft CLI not found — code-intelligence checks unavailable → fix: takkub doctor --fix or npm install -g @nanonets/graft@0.8.2`. Clear, actionable, no crash.
- **MCP-injected panes are unaffected by design**: `shared_dev_tools.GRAFT_MCP["graft"]["command"]` is `npx` (`npx -y @nanonets/graft@0.8.2 mcp`), entirely independent of the `shutil.which("graft")` PATH check that only gates the boot-time CLI autobuild sweep. A pane whose graft MCP has no graph built yet (exactly the state a no-CLI machine is stuck in) already has a documented, empirically-verified non-crashing behavior (`shared_dev_tools.py`'s own module comment, verified against the real CLI 2026-08-05): tool calls return a graceful "no matching nodes ... try `graft build`" text result, `isError: false`.

---

## Summary for Lead

- Wheel is correct 1.0.47, contains everything required, no packaging regressions.
- Installed-mode behavior (import path, `DATA_HOME`, `GRAFT_STORE_ROOT`, `doctor`) is correct against the real built artifact, not just a dev checkout.
- **The core promise — a freshly installed cockpit never writes into a user's repo — is directly proven** with a before/after `.gitignore` hash + `git status --ignored` diff against a real `graft build` run from the installed venv.
- Machines without the `graft` CLI degrade gracefully everywhere touched (autobuild sweep, `doctor`, MCP panes) — no crashes, no silent breakage.
- CI is red on 2/5 jobs, but it's a single self-inflicted test bug (assert-after-fixture-override, OS-temp-dir-dependent pass/fail), not a regression in the shipped feature — independently reproven correct in §3. `installed-gate`, the checklist's authoritative packaged-artifact gate, is green on both shipped OSes.

**Gate decision left to Lead**, per role scope (report only, no code/commit/publish). If Lead wants CI fully green before publishing, the fix is a small, well-understood test change (see §4 recommendation) — not a production fix.
