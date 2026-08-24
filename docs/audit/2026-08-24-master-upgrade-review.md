# Master Upgrade batch review — 2026-08-24

**Reviewer:** reviewer pane (read-only, no subagents)
**Range:** `2f4ec50..HEAD` on `main` — commits #369, #370, #371, #372, #373, #375
**Checklist:** `docs/plans/workspace-master-upgrade-2026-08-24/17_SECURITY_MASTER.md`, `18_PERFORMANCE_MASTER.md`, `25_MASTER_PROMPT_FOR_LEAD.md` (Architectural constraints)
**Verification:** read every non-test diff in the range; ran `takkub qa-gate --targeted` against the 15 test files matching this batch (pytest PASS, 35.4s, no failures); grepped for layering/PyQt violations in new `core/` modules; cross-checked one in-progress uncommitted worktree (`worktrees/agent-takkub/backend-3-1787539487`) for context on finding #1.

---

## Findings (severity order)

### 1 — HIGH — #375's subprocess-crash fix does not cover every concurrent caller; regression is still live on current `main` HEAD

**File:** `src/agent_takkub/project_file_index.py:290-302` (`_GitStatusWorker.run`)
**Also relevant:** `src/agent_takkub/git_changes_service.py:56` (`_SUBPROCESS_LOCK`)

Commit 3004782 (#375) adds `_SUBPROCESS_LOCK` in `git_changes_service.py` and routes every subprocess call in *that module* through it, citing a reproduced Windows access violation inside `CreateProcess` when several `QThreadPool.globalInstance()` workers call `subprocess.run()` concurrently. But `project_file_index.py`'s pre-existing `_GitStatusWorker.run()` (line 293) also calls `subprocess.run(["git", "status", ...])` on the *same* `QThreadPool.globalInstance()`, and is not routed through `_SUBPROCESS_LOCK` (or any lock) at all. `ProjectExplorer` constructs both a `GitStatusService` (this worker) and a `GitChangesService`/`RepoDiscoveryService` (the newly-locked workers) for the same project, so the exact crash scenario the commit describes — a status-badge refresh landing on the pool at the same moment as a CHANGES-panel refresh — can still reproduce today, just via this one remaining unlocked call site.

**Impact:** the native abort (interpreter-level Windows access violation, not a catchable Python exception) this batch was built to close is not fully closed on `main` HEAD. Any project with both the Explorer tree and CHANGES panel visible remains exposed.

**Evidence this is already recognized, not a hypothesis:** an in-progress, **uncommitted** worktree (`worktrees/agent-takkub/backend-3-1787539487`, branch `wt/backend-3-1787539487`) already fixes exactly this — it adds `RESOLVE_LOCK`/`SUBPROCESS_LOCK` in `project_file_index.py`, has `git_changes_service.py` import both instead of keeping its own lock, and additionally guards every `Path.resolve()` call (see finding #2 — the same root cause covers `ntpath.realpath`, not just `CreateProcess`). This confirms the mechanism and the fix shape; it just hasn't landed on `main` yet.

**Recommendation:** do not consider #375 fully closed until the companion fix (backend#3, in progress) merges. Not a rewrite request — the fix already exists in the other pane's worktree; this is a "hold the ship claim" finding, not a "go implement this" one.

---

### 2 — HIGH — New Preview code (#369) adds an unprotected `Path.resolve()` call on the Qt main thread — same root cause as #1, not covered by the in-progress fix either

**File:** `src/agent_takkub/preview_controller.py:148` (`_local_file_path`, new in commit 3c48108)

```python
return Path(raw).resolve()
```

This is called from `navigation_allowed()` (`preview_controller.py:275`), which `PreviewHost.navigation_allowed` (`preview_widget.py:371`) calls directly from `_PreviewPage.acceptNavigationRequest` — a `QWebEnginePage` override, i.e. it runs synchronously on the Qt **main** thread on every in-page navigation. The root cause backend#3's WIP fix documents (see finding #1) is that concurrent `Path.resolve()` calls from *different threads* crash on Windows inside `ntpath.realpath` — not just concurrent `subprocess.run()`. That WIP fix adds `RESOLVE_LOCK` around every `Path.resolve()` call in `project_file_index.py`/`git_changes_service.py`/`project_explorer.py` specifically because some of those calls also happen on the main thread and must not race the background workers' resolves. `preview_controller.py`'s new `_local_file_path` is exactly that same shape (main-thread resolve, background workers resolving concurrently elsewhere) but was added by a different commit in this same batch and isn't part of that fix's scope.

**Impact:** even after the backend#3 fix for finding #1 lands, a user navigating inside a file-mode Preview at the same moment a background git worker resolves a path can still hit the same class of crash — the fix would be incomplete for this call site.

**Recommendation:** route this through the same shared lock (`project_file_index._safe_resolve` once it exists, or an equivalent) rather than a bare `Path.resolve()`. Flag to whoever owns the backend#3 fix so it's folded into the same pass rather than becoming a second bug report for the identical root cause.

---

### 3 — LOW — Preview canonical-URL comparison: no regression test for case-only or UNC-vs-mapped-drive equivalence

**File:** `src/agent_takkub/preview_controller.py:84-97` (`navigation_allowed`, `os.path.normcase` compare); `tests/test_preview_controller.py:344-399`

The checklist's "canonical file URL mapping" item calls out drive-letter/case/UNC/symlink. By inspection, `os.path.normcase` correctly folds case on Windows (no-op on POSIX) and `Path.resolve()` folds symlinks — both look right. But `TestNavigationAllowedFileUrlComparison` (the test class added alongside this fix) covers drive-letter round-trip and Unicode paths, not an explicit case-difference case (`c:\...\index.html` vs `C:\...\INDEX.HTML`) or a UNC-path-vs-mapped-drive-letter case. Low practical risk — Preview targets always come from a resolved project root, never raw user input — but it's the one item on the checklist line with no pinning test, so a future refactor of this function has nothing to catch a regression here.

**Recommendation:** non-blocking; add a case-difference test when convenient. Not release-blocking.

---

## Confirmed clean (checklist items verified, no findings)

**Editor (#370, commit 29cf730)** — `editor_service.py`/`editor_widget.py`:
- Strict UTF-8 decode replaces `errors="replace"` on both `stat_snapshot` and the actual editor-open read path (`editor_widget.py:203`); the file becomes read-only (`encoding_unsupported`) instead of silently corrupting bytes on the next save. No `errors="replace"` remains on any write-back path — the one remaining `errors="replace"` in the diff (`editor_widget.py:190`, `read_head_blob`) is the read-only HEAD-diff side, never written back.
- POSIX mode preserved by reading `stat.S_IMODE` before writing the temp file and `chmod`-ing the temp file before `os.replace` (`editor_service.py:97-114`) — correctly gated off on Windows (no POSIX bits to preserve) and correctly a no-op for a brand-new file.
- `save_atomic` rejects an `encoding_unsupported` source before the conflict check even runs (`editor_service.py:262-134`) — can't be raced into a silent-corruption save.
- Deleted/renamed diff-open path (`editor_widget.build_diff_result`) checks `resolved.exists()` before stat'ing and falls back to `find_rename_old_path`, mirroring `git_changes_service.diff_sync`'s own per-status rules — no crash on a diff-open against a deleted/renamed row.

**Git (#375, commit 3004782)** correctness (independent of finding #1's concurrency gap):
- Deleted (`D`) status never stats/opens the missing file; `original=None and modified=None` reports `no_content` instead of crashing.
- Rename (`R`) correctly falls back to an *unscoped* `git status --porcelain=v2` for pairing (scoping to `-- rel_new_path` would make git report a plain add — verified against the documented rationale, matches known git rename-detection behavior).
- Multi-root (`RepoDiscoveryService`/`_on_repos_discovered` in `project_explorer.py`) correctly resolves each configured root to its real top-level, corrects a root that's actually a repo subdirectory, and groups CHANGES rows per distinct repo — every row still goes through `resolve_and_contain` before display, so containment isn't weakened by the new grouping.

**Preview (#369, commit 3c48108)** — everything except findings #2/#3:
- No `QWebChannel` anywhere in `preview_widget.py`/`preview_controller.py` (grepped; only `editor_widget.py`/`terminal_widget.py` use one, which is expected/documented).
- Active-project invariant: `MainWindow._on_preview_state_changed` routes a background project's update to a status-bar notice instead of hijacking the shared `PreviewHost` (#369 BUG-002); `_sync_preview_to_active_tab` re-syncs on every tab switch.
- Close cleanup: `_nav_block_counts.pop(project, None)` on `close()` (BUG-003); `MainWindow._on_tab_close_requested` now calls `preview_command("close", ...)` so a closed tab's Preview doesn't outlive it.
- No WebView reparenting — `show_state`/`close_project`/`set_keepalive` are state pushes onto one already-owned widget, never a parent change.

**Design integrations (#373, commit 5ddebb9)**:
- Every credential access goes through `core.secrets.manager.SecretManager`, stored under `SETTINGS_HOME/secrets/{id}.json` (cockpit-owned file), never `projects.json` or any tracked/world-readable file (`manager.py:27-36`).
- `build_client` (`design_integrations.py:296-353`) re-checks `PermissionEngine.mcp_allowed(role)` (default-deny) **and** a stored credential on every call; it is the only constructor call site for the three client classes, and `design_clients.py` itself has no notion of role/permission (verified: no other import of `TwentyFirstClient`/`FigmaClient`/`PenpotClient` anywhere in `src/`).
- All three HTTP clients (`design_clients.py`) are fail-open by construction — every public method returns `None`/`()` on timeout/connection/non-2xx/JSON-shape failure, never raises — and stamp every result with `Provenance` so a caller renders it as labeled untrusted external content.
- Pure stdlib `urllib` — no new dependency.
- No caller currently invokes a client method from the Qt GUI thread — `design_integration_client`/`build_client` are only reachable today via `cli.py`'s `cmd_design_integrations` (a separate process, not the cockpit GUI) — so the "no network on GUI thread" rule isn't yet exercised, but also isn't yet violated.

**OpenViking (#372, commit efa3896)**:
- Fail-open at two independent boundaries: `context_builder.merge_openviking_traced` (disabled → `(base_text, None)` unchanged) and `facade.build_context_for_assign`'s own `try/except` around the merge call (a bug there falls back to the pre-merge Brain/Conversation text, never discards it). When `TAKKUB_OPENVIKING_ENABLED` is unset (default), the merge step is a single boolean check — byte-identical output by construction, not by re-testing.
- API key resolution (`openviking_adapter.api_key()`) reuses the existing `FileSecretBackend` mechanism at `DATA_HOME/openviking/api_key`, same convention as every other provider credential file — never an env var required, never written to a tracked file.
- `resource_source.py` and `indexing.py` both filter through the pre-existing `obsidian_boundary.is_indexable()` allowlist (`01-Projects`/`02-Areas` only) — confirmed by reading that module (unmodified in this diff) that `99-Logs`/`.obsidian`/`secrets` are explicitly denylisted regardless of allowlist match.
- No new write path into Brain/Conversation: `indexing.py`'s state file is its own bookkeeping JSON under `DATA_HOME/openviking/index/`, `core.brain.facade.submit` is never touched.
- No vendored AGPL source — `openviking_adapter.py` is an `urllib`-only HTTP client against documented upstream endpoints; the module docstring is explicit about this.
- `git diff --stat` for this range touches nothing under a V2-authority/Phase-10/#362 path — grepped the full file list, none match.

**Revise routing (#371, commit 41b878c)**:
- `format_revision_feedback` (`design_actions.py:269-284`) sends only `artifact_id`/`title`/`kind`/`target`/`feedback` — never the artifact's HTML/file content ("ไม่ส่ง HTML" holds).
- `Orchestrator._live_design_feedback_role` (`orchestrator.py:6183-6198`) picks a target by pane liveness only (`pane.session.is_alive`), never by which CLI backs the pane — provider-agnostic across claude/codex/gemini-agy/opencode/kimi/cursor as required, and falls back to Lead-only when no live candidate exists (unchanged behavior).

**Layering / cross-platform**:
- Grepped every new file under `core/context_sources/` and `core/capabilities/design_clients.py` for PyQt/GUI imports — none found; `core-is-bottom-layer` looks preserved (not independently re-run through `lint-imports` per the "no local full gate" project rule — targeted pytest only).
- `editor_service.py`'s POSIX-mode-preservation code is correctly gated on `sys.platform != "win32"`, matching every other platform-specific block in the diff.

---

## Test evidence

`takkub qa-gate --targeted` against the 15 test files touching this batch (`test_cli_design_integrations.py`, `test_core_capabilities_design_clients.py`, `test_core_capabilities_design_integrations.py`, `test_core_context_sources.py`, `test_core_context_sources_merge.py`, `test_design_actions.py`, `test_design_revise_feedback_routing.py`, `test_doctor.py`, `test_editor_service.py`, `test_editor_widget.py`, `test_git_changes_service.py`, `test_main_window_preview_sync.py`, `test_preview_controller.py`, `test_preview_widget.py`, `test_project_explorer.py`):

```
step         result  time     detail
venv-check   PASS       0.0s
pytest       PASS      34-35s  (all targeted tests passed)
GATE: PASS
```

No `xfail`/`skip`/weakened-assertion patterns found scanning the test diff (the only `skip`s are legitimate environment guards: `git not on PATH`, `POSIX file mode has no meaning on Windows`).

---

## Ship / no-ship verdict per commit

| Commit | Summary | Verdict |
|---|---|---|
| 29cf730 | fix(#370): strict UTF-8 decode + POSIX mode preservation | **SHIP** |
| 3c48108 | fix(#369): Preview project-aware + file:// nav comparison + tab-close cleanup | **SHIP**, with finding #2 (HIGH) as a required fast-follow — same root-cause family as #375's crash, not itself a regression from before this batch (Preview had no file-url comparison at all before) |
| 41b878c | fix(#371): design_revise routing | **SHIP** |
| bdf69fa | docs: master upgrade pack + Phase 0 re-audit matrix | **SHIP** (docs-only) |
| 3004782 | fix(#375): git changes deleted-diff, rename old_path, multi-root repos | **SHIP the logic; HOLD the crash-fix claim** — correctness (deleted/rename/multi-root) is solid, but finding #1 (HIGH) means the concurrent-subprocess crash this commit set out to fix is not fully closed on `main` yet |
| 2c9f2b5 | merge(#375): reconcile with main's #370 | inherits 3004782's verdict |
| 5ddebb9 | feat(#373): real 21st.dev/Figma/Penpot integrations | **SHIP** |
| efa3896 | feat(#372): OpenViking optional sidecar + Context Sources | **SHIP** |

**Overall: SHIP the batch, with two HIGH follow-ups tracked before closing #375/#369 as fully resolved** — both are the same underlying "concurrent `Path.resolve()`/`subprocess.run()` crashes the interpreter on Windows" issue, one already being fixed (uncommitted, `backend-3-1787539487` worktree) and one (`preview_controller.py:148`) not yet covered by that fix. No blocker for the design-integrations or OpenViking feature work — both are cleanly gated, fail-open, and scoped as documented.

## Findings count

- HIGH: 2
- LOW: 1
- Confirmed-clean checklist items: 20+ (see above)
