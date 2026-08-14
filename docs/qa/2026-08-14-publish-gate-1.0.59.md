# Publish gate — 1.0.59 (npm) — 2026-08-14

**Verdict: GO** — all checks green, no blockers found. One non-blocking housekeeping note (stale `dist/` wheel).

## Context

Rerun of the release gate on `main` after the prior 5768-passed baseline, covering two additions:
1. fix #192 (remote blank chat reason) — `remote/notify.py`, `remote/api.py`, `remote/static/app.js` + 2 new test files
2. release prep — version bump 1.0.58 → 1.0.59 (`package.json`, `pyproject.toml`, `__init__.py`) + CHANGELOG

Env: `.venv` editable install (per project convention — never system python + `PYTHONPATH=src`).

## 1. Full test suite

```
5777 passed, 7 skipped in 780.10s (0:13:00)
```

**0 failed.** Up from the 5768-passed baseline by exactly 9 (matches the 2 new #192 test files: `tests/test_remote_api.py` +93 lines / `tests/test_remote_notify.py` +45 lines of new assertions).

⚠️ Gotcha found and worked around: `python -m pytest -q` produced **no summary line at all** (dots stop at `[100%]` with nothing after, though exit code was correctly 0) — reproduced twice, including on a 2-test file. Root cause: `pyproject.toml`'s `[tool.pytest.ini_options] addopts = "-q"` already sets quiet mode; passing `-q` again on the command line doubles it to effective `-qq`, and pytest 9.0.3 suppresses the final summary line at that verbosity. Not a project bug — just don't pass `-q` on the CLI when `addopts` already has it. Reran as plain `python -m pytest` (no extra flag) to get the real summary.

## 2. Ruff

```
ruff check src/ tests/     → All checks passed!
ruff format --check src/ tests/  → 404 files already formatted
```

## 3. lint-imports

```
Analyzed 140 files, 544 dependencies.
Contracts: 24 kept, 0 broken.
```
(Note: `lint-imports` is a console script, not a `-m` module — `.venv/Scripts/lint-imports.exe` invokes it directly.)

## 4. Import graph freshness

```
depgraph check: docs/architecture/depgraph.json is fresh (module_count=140)
```
exit 0.

## 5. pre-commit (all hooks, all files)

```
Detect hardcoded secrets ......... Passed
ruff (legacy alias) .............. Passed
ruff format ....................... Passed
takkub docs-verify ................ Passed
import-linter architecture contracts .. Passed
depgraph freshness check .......... Passed
```
6/6 passed, 0 skipped.

## 6. git status

Clean after all runs — no stray artifacts written to the tree.

## 7. Version consistency (1.0.59)

| File | Value |
|---|---|
| `package.json` | `"version": "1.0.59"` |
| `pyproject.toml` | `version = "1.0.59"` |
| `src/agent_takkub/__init__.py` | `__version__ = "1.0.59"` |

All 3 match. Repo-wide grep for stray `1.0.5[0-8]` found no live code/config references outside expected historical locations:
- `CHANGELOG.md` — historical entries (expected, e.g. `## [1.0.58]`)
- `runtime/agents/*/CLAUDE.spawn-*.md` — frozen per-spawn task snapshots (not live code)
- `build/lib/agent_takkub/__init__.py` — **stale build artifact** at 1.0.58 (see §8)
- `dist/agent_takkub-1.0.58-py3-none-any.whl` — **stale build artifact** at 1.0.58 (see §8)
- `runtime/exports/2026-08-10/.../venv/.../__init__.py` — frozen inside an old QA export's venv, not live

No live source/config drift.

## 8. Packaging sanity — npm publish payload

`package.json` `"files"` field:
```json
["npm/", "dist/*.whl", "assets/icon.ico", "assets/icon.png", "assets/cockpit-main.png"]
```
The actual Python source ships **inside the wheel**, not as raw files — so the real question is whether `remote/static/app.js` (the file #192 changed) lands in the wheel, not whether it's in the npm files list directly.

Confirmed **it does**: `pyproject.toml` `[tool.setuptools.package-data]` explicitly lists:
```toml
agent_takkub = [..., "remote/static/*"]
"agent_takkub.remote" = ["static/*"]
```
Both the umbrella `agent_takkub` package-data list and the dedicated `agent_takkub.remote` entry include `static/*`, so `app.js` is packaged whether setuptools resolves package-data per-package or via the flat glob. `MANIFEST.in` also has `recursive-include src/agent_takkub/remote/static *` for the sdist. Remote/mobile PWA will not silently break for npm installers.

**⚠️ Non-blocking finding — stale wheel in `dist/`:**
```
dist/agent_takkub-1.0.58-py3-none-any.whl   1.9M
```
This is a leftover from the 1.0.58 build. `package.json`'s `files` field grabs `dist/*.whl` — if the publish script does not delete/rebuild `dist/` first, npm could ship the **old 1.0.58 wheel** under the 1.0.59 npm package version, meaning the JS entry (`package.json` version) and the actual Python payload inside would disagree, and the #192 fix would NOT be in what users get despite the npm version number saying 1.0.59. This directly matches the project's own documented publish-flow note ("ลบ wheel เก่าใน dist/ ก่อน build" — delete old wheel before build). **Action needed before publish: delete `dist/agent_takkub-1.0.58-py3-none-any.whl` and rebuild the 1.0.59 wheel**, then re-verify only one wheel exists in `dist/` matching 1.0.59.

`build/lib/agent_takkub/__init__.py` (1.0.58) is a stale `setuptools build` intermediate directory, not part of the npm payload — cosmetic only, safe to ignore or clean.

**`claude_auth_dialog.py` removal:** confirmed clean. No references anywhere in `src/` (grep for `claude_auth_dialog` in source returns nothing). The only hits repo-wide are: CHANGELOG (expected historical mention), stale `.pytest_cache/v/cache/nodeids` (cached test IDs from before the file was deleted — not live test source), old `.playwright-mcp` session snapshots (screenshot text, not code), and `runtime/agents/*/CLAUDE.spawn-*.md` (frozen task specs). The still-present `claude_auth_config.py` (note: `_config`, not `_dialog`) is a **different, still-live module** correctly imported by `spawn_engine.py`, `orchestrator.py`, `settings_window.py` — not a stray reference to the deleted file.

## 9. CHANGELOG cross-check (1.0.59 section vs `git log`)

Sampled 5 of the entries (more than the requested 3) against `git log --oneline`:

| CHANGELOG claim | Commit | Match |
|---|---|---|
| #192 blank Lead chat reason | `dfe187d fix(remote): explain blank Lead chat instead of silent empty state (#192)` | ✅ |
| dead-code sweep 8 items ~230 LOC incl. `claude_auth_dialog.py` | `6209dad chore: remove proven-dead code from L12 audit (8 items, ~230 LOC)` + `6c33407 docs(architecture): ลบ claude_auth_dialog.py` | ✅ |
| security hardening 3 spots (img alt escape / control-byte strip / manifest hardlink) | `b2a5804 fix(security): escape img alt, strip control chars, copy manifest files` | ✅ |
| architecture guardrail enforced in CI on all 3 OS | `4f1daa7 ci: enforce import-linter + depgraph freshness in CI, fix cross-platform pre-commit hooks` | ✅ |
| README screenshot pinned to old `v1.0.5` tag → `main` | `a8c3eb4 docs(readme): point cockpit screenshot at main instead of stale v1.0.5 tag` | ✅ |

No overclaiming found in the sampled entries.

## 10. CI cross-platform risk (ubuntu/macos vs windows)

Diff-scanned the #192 commit (`dfe187d`) for platform-sensitive patterns (hardcoded path separators, `subprocess`, `sys.platform`, `os.path`) across the touched files (`remote/notify.py`, `remote/api.py`): **none found**. The change is pure Python classification logic (string/dict construction) + JSON serialization + client-side JS rendering — no filesystem path handling, no subprocess calls, no OS-conditional branches. Low CI risk; nothing expected to diverge between ubuntu/macos/windows runners.

## Summary

| Check | Result |
|---|---|
| Full suite | 5777 passed / 7 skipped / **0 failed** |
| ruff check | pass |
| ruff format --check | pass (404 files) |
| lint-imports | 24 kept / 0 broken |
| depgraph --check | fresh |
| pre-commit --all-files | 6/6 passed |
| git status | clean |
| Version consistency | 1.0.59 × 3 files, no stray old-version code |
| Packaging (app.js in wheel) | confirmed included |
| claude_auth_dialog.py stray refs | none in live code |
| CHANGELOG accuracy | 5/5 sampled entries match git log |
| CI platform risk | none found in #192 diff |

**Blocking for publish:** none.
**Recommended before running the actual npm/PyPI-wheel build:** delete the stale `dist/agent_takkub-1.0.58-py3-none-any.whl` and rebuild fresh for 1.0.59, so the wheel npm ships actually matches the version number.
