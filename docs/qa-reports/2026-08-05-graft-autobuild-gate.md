# QA batch gate — graft auto-build + external graph store (pre-1.0.47)

**Date:** 2026-08-05
**Scope:** `graft_autobuild.py` + `graft_store.py` (new), MCP `--dir` templating through `shared_dev_tools`/`mcp_bridge`/`spawn_engine`, `disk_usage.py` `graft-graphs` category
**Verdict:** ⚠️ **1 blocker found** — everything else PASS

## 1. Full suite

| Check | Result |
|---|---|
| `.venv/Scripts/python.exe -m pytest` | exit 0 — **5080 passed, 5 skipped**, 0 failed |
| `ruff check src tests` | All checks passed! |
| `ruff format --check src tests` | **2 files would be reformatted**: `tests/test_disk_usage.py`, `tests/test_graft_autobuild.py` (both are part of this feature's new test code, not pre-existing) |
| `.venv/Scripts/lint-imports` | Analyzed 138 files, 515 dependencies — **23/23 contracts kept, 0 broken** |
| `takkub docs-verify` | 0 broken refs |

Note (harness quirk, not a defect, previously documented in `2026-08-05-release-1045-gate.md`): pytest's final summary line does not print with this project's `addopts = "-q"` + default invocation — confirmed NOT a silent-failure risk by deliberately injecting a failing test (`assert False`) into `tests/`, which correctly surfaced `FAILED ... EXIT:1` with full traceback. Exit code 0 + this control test together are sufficient proof of 0 failures.

Sanity control: intentionally added `tests/test_zz_scratch_sanity.py::test_intentional_failure` (`assert False`), confirmed pytest reports it correctly (exit 1, `FAILED` line, traceback) — then deleted it before the real run. Confirms the "no summary line" harness quirk does not mask real failures.

## 2. `[blocker?]` No stray files in target repos — CONFIRMED SAFE for third-party repos, but **regresses the cockpit's own dev checkout**

**Third-party repos: PASS.** Snapshotted `git status --porcelain` for two real configured user repos before/after triggering `graft_autobuild.build_all_projects_async()` through the real code path (not manual CLI) against an isolated `projects.json` pointing at them:

- `fastwork-finder` — clean before, **clean after** (byte-identical)
- `e2e-demo-clicker` — `?? index.html` before (pre-existing, unrelated), **identical after**

Confirmed via `graph_store_dir()` inspection that both graphs (`.graph/`, `.cache/`, `INDEX.md`, per-file `.md` cards, `source.json`) landed entirely under the external store (`DATA_HOME/graft-graphs/<sha256>/`), never inside the target directories.

**Blocker: the cockpit's own dev checkout is not protected the same way.** `graft_store.GRAFT_STORE_ROOT = DATA_HOME / "graft-graphs"`. In a dev checkout, `config.DATA_HOME == REPO_ROOT` (by design — `config.py`'s `_resolve_data_home()`/`_resolve_settings_home()` comments this explicitly, and this IS a dev checkout of agent-takkub, self-hosting itself). `.gitignore` already carries explicit carve-outs for exactly this class of problem — `runtime/` (line 16) and `worktrees/` (lines 65-67, comment: *"In a dev checkout DATA_HOME == repo root, so isolated worktrees land here; never track it"*) — but has **no equivalent entry for `graft-graphs/`**.

Reproduced twice independently (once via `build_all_projects_async()` against two probe repos with `PROJECTS_JSON` swapped, once via `browser_profile_mcp_config_path()`'s per-pane `--dir` templating alone): both times, `git status` in `agent-takkub`'s own repo afterward showed `?? graft-graphs/` as a new untracked entry. This is exactly the class of regression #146 set out to fix — untracked cockpit-generated files appearing in a git tree the moment a build/spawn runs — just scoped to the cockpit's own repo instead of a third-party one. A distracted `git add -A` in this repo would commit it.

**Fix needed:** add a `graft-graphs/` entry to `.gitignore`, mirroring the existing `runtime/`/`worktrees/` treatment.

## 3. Pane MCP `--dir` resolves to the correct store — PASS (both variants)

- **claude**: `shared_dev_tools.browser_profile_mcp_config_path('qa', None, 'probe-project', cwd)` writes `graft.args` as `[..., "--dir", "<store>", "mcp"]` where `<store>` == `graft_store.graph_store_dir(Path(cwd))` — exact match.
- **codex**: `mcp_bridge._codex_mcp_argv('qa', None, 'probe-project', cwd=..., ...)` emits `-c mcp_servers.graft.args=["-y","@nanonets/graft@0.8.2","--dir","<store>","mcp"]` — store hash matched `graph_store_dir(cwd).name` exactly (`20251703cf137c...`).
- Both `spawn_engine.py` call sites (claude at L2320, codex at L1720 via `mcp_argv_for_provider`) thread the pane's real `spawn_cwd` through — confirmed by reading the diff, not just the function signature.

## 4. Key collision handling — PASS

- Two different resolved paths → different `graph_key()` (confirmed, plus confirmed NOT a `decode_project_dir`-style lossy transform: `my-project.web` vs `my_project_web` hash distinctly).
- Same path, different case (`probe-A` vs `PROBE-A`) on Windows → **same** `graph_key()` (case-folded before hashing) — confirmed live, matches `test_graph_key_case_insensitive_on_windows`.

## 5. Boot time — PASS, no measurable blocking

Measured `Orchestrator()` construction wall-clock, 3 trials each, real `projects.json` (46 dirs across 27 projects):

| Variant | Trials (ms) |
|---|---|
| Real `build_all_projects_async()` (kill switch unset, spawn stubbed to isolate thread-fanout overhead from real subprocess execution time) | 300.6 / 462.7 / 377.0 |
| Baseline (`TAKKUB_SKIP_GRAFT_BUILD=1`, no graft call at all) | 371.2 / 273.3 / 327.1 |

The two distributions overlap completely — the graft boot trigger's thread-spawn fan-out for 46 directories adds no measurable delay over normal `Orchestrator()` init noise (real subprocess `graft build` execution happens off the constructor's call stack in background daemon threads, capped at 3 concurrent via `_build_semaphore`, so it never blocks boot regardless of how long any individual build takes).

## 6. Kill switch — PASS

With `TAKKUB_SKIP_GRAFT_BUILD=1`, patched `subprocess.run` to raise if called, then exercised all three triggers directly: `build_all_projects_async()`, `ensure_project_graph_async('agent-takkub')`, `schedule_rebuild_after_done(<real cwd>)`. Zero `subprocess.run` calls across all three — confirmed the kill switch is checked at each trigger's own entry point, not just in one shared function.

## 7. `disk_usage` graft-graphs category + prune — PASS

Built a probe `DATA_HOME` with one live store (source path still in `projects.json`) and one orphan store (source path never configured):

- `disk_report()`'s `graft-graphs` category reported `orphan 390 bytes (1) · live 273 bytes (1, ไม่แตะ)` — correct split.
- `prune(['graft-graphs'], dry_run=True)` listed exactly the 1 orphan target, `would_free_bytes=390`, made no changes.
- `prune(['graft-graphs'], dry_run=False)` deleted exactly the orphan (`removed_count=1, freed_bytes=390`); the live store was confirmed still present on disk afterward.

## Conclusion

Full suite, lint, import-linter, and docs-verify all green. Every graft-autobuild-specific behavior (isolation, per-pane `--dir` resolution for both providers, key collisions, boot non-blocking, kill switch, disk prune) verified against real code paths and confirmed correct — **except** one confirmed blocker: `graft-graphs/` needs a `.gitignore` entry for dev checkouts (`DATA_HOME == REPO_ROOT`), same as the existing `runtime/`/`worktrees/` treatment, or every dev-checkout cockpit instance will show an untracked `graft-graphs/` after its very first boot.

**Do not commit fixes — reporting only, per QA role scope.**

## Re-verify (2026-08-05, after blocker-fix claim)

**Verdict: ⚠️ still 1 blocker — the original `.gitignore` fix is incomplete.**

Claimed fixes going into this re-verify: (1) `graft-graphs/` added to `.gitignore`, (2) `ruff format` re-run to catch the `tests/test_disk_usage.py` file the first formatting pass missed.

| Check | Result |
|---|---|
| `ruff format --check src/ tests/` | **382 files already formatted** — clean, includes `test_disk_usage.py` this time |
| `ruff check src/ tests/` | No issues found |
| Targeted (`.venv/Scripts/python.exe -m pytest tests/test_graft_autobuild.py tests/test_graft_store.py tests/test_graft_mcp.py tests/test_disk_usage.py tests/test_mcp_bridge.py -q`) | **127 passed, 0 failed** (ran via `.venv` per project convention — system python fails with `ModuleNotFoundError: agent_takkub`, not a real defect) |
| `takkub docs-verify` | 0 broken refs |
| `git status` baseline (before triggering any build) | clean of `graft-graphs/`; `git check-ignore -v graft-graphs/` confirms line 75 matches |

**Real auto-build trigger (boot path), not a manual CLI call:** called `graft_autobuild._build_one(Path("C:/Users/monch/WebstormProjects/agent-takkub").resolve())` directly — the same function `build_all_projects_async()` (orchestrator boot) calls per-directory, against this project's real configured path (`projects.json` → `agent-takkub.paths.main == C:/Users/monch/WebstormProjects/agent-takkub`, confirmed by reading `load_projects()` live — i.e. this exact call *is* what happens on every cockpit boot for this dev checkout).

Result: `graft-graphs/` itself stayed correctly untracked (glob at `.gitignore:75` covers it). **But a NEW untracked file appeared: `.ignore` at the repo root** — `git status --short` showed `?? .ignore` after the build, containing:
```
!graft-graphs/58c53152d5ffab5932a237ada8da5e83a52c9af9cf42816ec9df012b0a6edc5a/
graft-graphs/58c53152d5ffab5932a237ada8da5e83a52c9af9cf42816ec9df012b0a6edc5a/.cache/
graft-graphs/58c53152d5ffab5932a237ada8da5e83a52c9af9cf42816ec9df012b0a6edc5a/.graph/
```

**Root cause:** `.gitignore` line 82 only excludes `src/agent_takkub/.ignore` (the pre-existing carve-out documented at lines 77-81, for graft builds targeting the `src/agent_takkub` subtree). It does **not** cover a `.ignore` written at the **repo root**, which is exactly where `graft build` writes one when the build target *is* the repo root — and the boot trigger's real configured path for this project **is** the repo root (`paths.main`, verified above). This is not a hypothetical edge case; it fires on every real boot of this dev checkout, the same class of regression the original `graft-graphs/` blocker was about (#146-style leak into the cockpit's own tree), just for a different generated filename.

Cleaned up the reproduction artifacts (`rm .ignore && rm -rf graft-graphs/`) before finishing — confirmed `git status` returns to the pre-existing baseline (the 11 pre-existing modified/untracked entries only, no `graft-graphs/`, no `.ignore`).

**Secondary, non-blocking observation:** `.gitignore:85` (`graft-graphs/<specific-sha256-hash>/`) is a leftover machine-specific single-hash entry, fully redundant with the generic glob at line 75, and not portable across dev machines (the hash is derived from this machine's absolute repo path). Doesn't cause a functional problem since line 75 already covers it, but reads like accidental diff residue worth removing for cleanliness — not blocking this gate.

**Fix needed before this can go green:** extend the `.ignore` carve-out to also cover the repo-root case — either a bare `/.ignore` entry (root-anchored, so it doesn't shadow other `.ignore` files that may legitimately live deeper in the tree) or a broader pattern that catches every location `graft build` can write one given how `_dirs_for_project()` resolves paths.

## Re-verify 2 (2026-08-05, after `.gitignore` rewrite by Lead)

**Verdict: ✅ GREEN — all blockers cleared, safe to ship 1.0.47.**

Lead's fix: `.gitignore` line 91 changed from the anchored `src/agent_takkub/.ignore` carve-out to an unanchored `.ignore` pattern (covers root **and** any subtree, and any future build target) + removed the redundant machine-specific `graft-graphs/<hash>/` line (was line 85, fully covered by the generic `graft-graphs/` glob at line 75).

| Check | Result |
|---|---|
| `git check-ignore -v` on `.ignore` (root), `src/agent_takkub/.ignore`, `graft-graphs/<hash>/foo` | all 3 match — `.gitignore:91` for both `.ignore` locations, `.gitignore:75` for `graft-graphs/` |
| **Real auto-build trigger (boot path)**, `graft_autobuild._build_one(Path("C:/Users/monch/WebstormProjects/agent-takkub").resolve())` — same function `build_all_projects_async()` calls per-directory, against this project's real configured path | `git status --porcelain` **byte-identical before and after** (diffed via `diff`) — no `.ignore`, no `graft-graphs/`, no other new untracked entry appeared. Only the 16 pre-existing modified/untracked entries remain, unchanged. |
| `ruff format --check src/ tests/` | 381 files already formatted — clean |
| `ruff check src/ tests/` | All checks passed! |
| Targeted (`test_graft_autobuild.py test_graft_store.py test_graft_mcp.py test_disk_usage.py test_mcp_bridge.py`) | exit 0, all dots pass (126 tests — no-summary-line quirk already proven benign in the first gate run above) |
| `takkub docs-verify` | 0 broken refs |

No cleanup needed this round — the build produced zero stray artifacts to remove. Both previously-found blockers (`graft-graphs/` untracked, `.ignore` untracked at repo root) are confirmed fixed. Ready for Lead to commit + release 1.0.47.
