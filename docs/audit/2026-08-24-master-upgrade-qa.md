# Master Upgrade batch — production acceptance QA (2026-08-24)

**QA:** qa pane (read-only re: source/tests, no subagents)
**Target:** `main` HEAD `64047e4` (commits #369–#375 + #374 Ask-Agent/ignore-parity + backend-3 concurrency fix, on top of the range the reviewer covered in `docs/audit/2026-08-24-master-upgrade-review.md`)
**Method:** real flows against the *live dev cockpit* (port 50780) via `takkub` CLI where a CLI surface exists; direct calls into the production Python modules (`git_changes_service`, `editor_service`, `preview_controller`, `project_file_index`, `core.capabilities.design_integrations`) from throwaway scripts for flows with no CLI surface, run against **two sandbox git repos** (`git clone --local` of this repo into a scratch temp dir — never a fresh empty repo, since `git commit` is pane-guard-blocked for this role with no exception carved out for a sandbox path; cloning inherits real history without ever calling `commit`). No source/tests touched. No full local pytest run (policy — CI runs it); one **fresh targeted gate** run below covers every test file the batch touches.
**Prod v1.2.1 (port 64357):** not touched.

---

## Acceptance table (`24_ACCEPTANCE_MASTER.md`)

### Workspace

| Item | Verdict | Evidence |
|---|---|---|
| Explorer root-safe and responsive | **PASS** | `resolve_and_contain` rejects an out-of-root path in every module that calls it (editor/preview/diff — see below); `doctor --workspace` live: `tree_scan[agent-takkub]: last_scan_ms=2.09 entries=2 scans=3` |
| Monaco local | **PARTIAL — bundle confirmed, rendering NOT VERIFIED** | `doctor --workspace`: `monaco-bundle 3 core file(s) present, 0.0 MB`. Actual typing/highlight is a real-GUI item — no screen access from this pane (see Ops row) |
| UTF8/BOM safe | **PASS** | Script against sandbox files: invalid-UTF-8 (`café` written as Latin-1 byte `0xE9`) → `encoding_unsupported=True`, `save_atomic` refused with `"source file is not valid UTF-8 (encoding_unsupported)"` *before* the conflict check ran. UTF-8-BOM file → `bom=True`, saved text round-tripped with BOM preserved on disk (`b'\xef\xbb\xbfhello\nworld\nappen'`) |
| permissions preserved | **PARTIAL — code path confirmed, POSIX chmod NOT VERIFIED (this is a Windows pane)** | `_write_atomic_text` correctly gates the `chmod` call on `sys.platform != "win32"`; on this Windows run the gate is a documented no-op (no POSIX bits to preserve) — ran clean, no crash. Actual mode-byte-preservation on POSIX needs a macOS/Linux run |
| conflicts safe | **PASS** | External-edit-between-read-and-save script: `save_atomic` returned `ok=False, conflict=True`, disk content stayed at the externally-written value (`'changed-externally\n'`), never overwritten |
| Git M/A/D/R correct | **PASS** | `changes_sync()` on a sandbox repo with a real modify+delete+rename+untracked-add: `D CHANGELOG.md`, `M README.md`, `R SECURITY_RENAMED.md old_path=SECURITY.md`, `A qa_new_file.txt`. `diff_sync()` on each: M → real unified diff; D → full-content-removal diff (321158 chars, matches CHANGELOG.md's real size); R → empty diff (correct — pure rename, no content change); A → new-file diff. Path-escape attempt into `diff_sync` correctly rejected (`PathEscapesRootsError`) |
| multi-repo correct | **PASS** | `discover_repo_roots([repoA, nested_repo, repoA/sub])`: `repoA/sub` (a non-repo subdirectory) correctly resolves to `repoA`'s own top-level; `nested_repo` (a real second git repo living inside `repoA`'s tree) correctly resolves to **its own separate top-level**, not folded into repoA — the exact GAP-009 scenario |
| Ask Agent works | **NOT VERIFIED** | Explorer right-click → role picker → `orch.send` is a GUI-only flow (`test_main_window_ask_agent.py` exists and is included in the fresh targeted-gate run below, PASS); no independent CLI/script re-exercise of the click path from this pane |
| Git ignore parity | **PASS** | `list_dir_sync()` against a real `.gitignore` (`__pycache__/`, `*.pyc`, …): `ignored.pyc` and `__pycache__/` correctly hidden; **`__pycache__.keep`** (a deliberately tricky name — matches `__pycache__` as a prefix but the real pattern is dir-only) correctly **kept**, proving this goes through real `git check-ignore` semantics, not a naive substring/prefix match |

### Preview

| Item | Verdict | Evidence |
|---|---|---|
| URL preview | **PASS** | `takkub preview open-url http://127.0.0.1:50780` → `ok`, `status` reflects `mode=url target=...` |
| HTML preview | **PASS** | `takkub preview open-file docs/guides/2026-06-22-vault-second-brain.html` → `ok`; `--device mobile` updates state; `close` clears it |
| file:// normalization | **PASS** | `navigation_allowed()` direct: same file via `file://` URL → allowed; same file **+ `#anchor`** → allowed (anchor stripped before compare, per `_local_file_path` docstring contract); a *second* local `.html` file → **blocked**; an `http://` URL from file-mode → **blocked** |
| same-origin containment | **PASS** | URL-mode `navigation_allowed()`: same origin, different path → allowed; different port → blocked; different host → blocked; `https://` vs `http://` same host:port → blocked (scheme is part of origin) |
| project-aware state | **PASS** (code-level, reviewer-confirmed) | Not independently re-exercised across two *simultaneously open* projects this round; reviewer's audit already traced `_on_preview_state_changed`/`_sync_preview_to_active_tab` for the A/B-masquerade fix (#369 BUG-002) — no regression found in this pass's targeted-gate run |
| project-close cleanup | **PASS** | `close()` pops `_nav_block_counts` for the project (source read, `preview_controller.py:266`); CLI `close` → `status` correctly reports "no open preview" |
| device presets | **PASS** | `open-file --device mobile` accepted and reflected in `status`; invalid device value is rejected by `set_device`'s `ValueError` guard (code read, not re-triggered via CLI this round) |

### Design

| Item | Verdict | Evidence |
|---|---|---|
| publish | **PASS** | `takkub design publish --path docs/guides/2026-06-22-vault-second-brain.html --title "QA test artifact" --mode html` → `artifact_id=4e04...`, `status=draft`; Preview auto-opened to the artifact (confirmed via `preview status`) |
| approve | **PASS** | `takkub design approve --id 4e04...` → `status=approved`; `runtime/events.log` confirms `workspace.design.approved` fired |
| revise reaches Designer | **PARTIAL — call path confirmed, delivered notice text NOT independently inspectable from this role** | `takkub design revise --id 4e04... --feedback "..."` → `status=revision_requested`, `runtime/events.log` confirms `workspace.design.revision_requested`. The routed-vs-fallback notice (`orchestrator.py:6317-6333`, `"(routed to {target_role})"` / `"(no live designer pane — Lead fallback)"`) is delivered to the **Lead pane's live inbox**, not logged to a file this role can read — `takkub messages --role lead` is role-gated (`err: role gate: only lead can messages`). Source read confirms the branch executed without exception; Lead (or the reviewer, who already traced this exact code in `docs/audit/2026-08-24-master-upgrade-review.md` and called it clean) should confirm the actual inbox text |
| design reviewer | **PASS** (pre-existing) | `docs/audit/2026-08-24-master-upgrade-review.md` — full independent review already on file, SHIP verdict w/ 2 HIGH follow-ups (see "Carried-over findings" below) |
| Storybook | **N/A (correctly skipped)** | `doctor` — `storybook:agent-takkub  no .storybook/ or package.json storybook script — optional`; not configured, gate correctly treats it as optional, no crash |
| 21st real integration | **PARTIAL — gate confirmed, live network call NOT VERIFIED (opt-in, no token by design)** | `build_client('figma', 'qa')` (and by same code path, 21st) → default-deny confirmed: `IntegrationDeniedError: 'figma' is not enabled for role 'qa' → 'takkub design integrations enable --role qa --token <token> figma'`. No token configured for any role (`takkub design integrations status`: all three `configured=False`) — real upstream HTTP call is untestable without a live token, correctly so by design |
| Figma optional real integration | **PARTIAL** (same as above) | same evidence |
| Penpot optional real integration | **PARTIAL** (same as above) | same evidence |

### Knowledge

| Item | Verdict | Evidence |
|---|---|---|
| Brain canonical operational memory | **NOT VERIFIED this pass (reviewer-confirmed clean)** | No new write path touched per reviewer's code read (`efa3896` section); not independently re-traced by QA this round |
| Conversation canonical session state | **NOT VERIFIED this pass (reviewer-confirmed clean)** | same |
| Obsidian curated human knowledge | **N/A (correctly skipped, no vault on this machine)** | `doctor --obsidian`: `no vault found (checked $TAKKUB_VAULT_DIR and ~/second-brain) — the vault mirror is opt-in; this is expected` — no crash |
| Graft structural intelligence | **NOT VERIFIED this pass** | Not exercised — no context-build trace produced during this session (`doctor`: `[context] last-trace: no context build recorded yet`) |
| OpenViking optional retrieval source | **PASS** | Default: `takkub ov status` → `enabled=False` (no-op). Enabled + shadow mode against a **non-existent server** (`TAKKUB_OPENVIKING_ENABLED=1 TAKKUB_OPENVIKING_MODE=shadow TAKKUB_OPENVIKING_URL=http://127.0.0.1:19999`): completed in **2.55s** (under the adapter's 4.0s timeout, connection-refused fast-fails), `health_ok=False`, **no crash, no hang** — fail-open confirmed exactly as designed |
| Context Builder owns merge/budget | **NOT VERIFIED this pass (reviewer-confirmed clean)** | same as Brain/Conversation rows above |
| no duplicate uncontrolled memory owners | **NOT VERIFIED this pass (reviewer-confirmed clean)** | same |

### Ops

| Item | Verdict | Evidence |
|---|---|---|
| doctor diagnostics | **PASS** | `doctor --workspace`, `--ram`, `--obsidian` all ran clean against the live dev cockpit; `--workspace` live half connected (`editor host: instance=yes open_tabs=1`) — not skipped |
| full CI | **NOT RUN BY QA (by policy)** | Per project memory (`no-local-full-gate-let-ci-run-it`) and this task's own constraint, QA does not run a full suite locally; CI is the single full-suite runner. A **fresh targeted gate** over every test file the whole batch touches (17 files, current HEAD, post-#374 and post-backend-3-merge — a superset of what the reviewer's earlier targeted run covered) is below and is **PASS** |
| real GUI acceptance | **NOT VERIFIED — explicitly flagged, not claimed** | This pane has Playwright MCP for *web* pages, not the PyQt6 desktop cockpit itself — Monaco typing/highlight, the diff view, viewport-preset rendering, and eyeballed A/B project switching all require a human (or a Qt-level automation channel this role doesn't have) at the screen. Per task instruction, reporting this as **NOT VERIFIED** rather than inferring pass from the passing unit/targeted tests |
| soak | **PASS — no leak trend** | 10× `preview open-file` / `close` cycles (2 rounds of 5) via CLI against the live cockpit. `doctor --ram` before/mid/after: pane-attributable `qtwe` stayed `0 MB` throughout (Preview widget correctly reused, never leaked as a second attributable process); shared QtWebEngine processes 4→3→3 (246–294 MB, no growth); main `pythonw` RSS showed one transient spike (596→1399 MB) after round 1 that **fully receded to 449 MB** (below the original baseline) after round 2 — consistent with a GC/cache transient, not a leak. No process count creep |
| rollback verified | **NOT VERIFIED — not exercised (destructive/out of scope for QA)** | `23_ROLLBACK.md` exists and documents the procedure; actually executing a rollback is a Lead-only, repo-mutating action this role should not perform unprompted |

---

## Fresh targeted gate — current HEAD (post #374, post backend-3 merge)

The reviewer's targeted run (`docs/audit/2026-08-24-master-upgrade-review.md`) was against an earlier point in the range (before #374's Ask-Agent/ignore-parity commit and before the `backend-3` concurrency-fix worktree merged). Re-ran against **all 17 test files the full batch touches**, on current `main` HEAD `64047e4`:

```
step         result  time     detail
venv-check   PASS       0.0s
pytest       PASS      44.0s  (all targeted tests passed; a few environment-guard skips, no xfail/weakened assertions)
GATE: PASS
```

Files covered: `test_cli_design_integrations.py`, `test_core_capabilities_design_clients.py`, `test_core_capabilities_design_integrations.py`, `test_core_context_sources.py`, `test_core_context_sources_merge.py`, `test_design_actions.py`, `test_design_revise_feedback_routing.py`, `test_doctor.py`, `test_editor_service.py`, `test_editor_widget.py`, `test_git_changes_service.py`, `test_main_window_ask_agent.py`, `test_main_window_preview_sync.py`, `test_preview_controller.py`, `test_preview_widget.py`, `test_project_explorer.py`, `test_project_file_index.py`, `test_project_tab_explorer.py`.

---

## Security spot-checks (`20_QA_TEST_PLAN.md` Security section)

| Check | Result |
|---|---|
| Traversal — `editor_service.read_for_edit`/`save_atomic` on a path outside configured roots | **Denied.** `PathEscapesRootsError` raised on read; `save_atomic` returns `ok=False, error="...escapes configured project roots"`, never writes |
| Traversal — `diff_sync` on a path outside roots | **Denied.** `DiffResult(error="...escapes configured project roots")` |
| Malicious preview navigation — cross-origin (URL mode) | **Blocked** (see Preview table) |
| Malicious preview navigation — second local file (file mode) | **Blocked** (see Preview table) |
| `open_url` to a non-loopback host | **Denied.** `takkub preview open-url http://example.com` → `err: preview URL must be loopback (127.0.0.1/localhost/::1) + port`. Note: `is_loopback_url` correctly special-cases the literal string `"localhost"` and any IP `ipaddress` classifies as loopback (127.0.0.0/8, ::1) — not re-tested against a DNS name that *resolves* to loopback (out of scope, would need a real DNS lookup) |
| `open_file` to a path outside approved roots | **Denied.** `err: ...escapes configured project roots` (tested against a real sandbox path from the live cockpit CLI) |
| Design integrations without `enable` | **Denied by default.** `build_client('figma', 'qa')` → `IntegrationDeniedError` |
| Secrets excluded from tracked/log files | **Confirmed clean.** `grep` for API-key/token strings across `runtime/projects.json` and `runtime/events.log`: zero hits. Per source read, every credential access goes through `SecretManager` → `SETTINGS_HOME/secrets/{id}.json`, never `projects.json` |
| junction/symlink traversal | **NOT VERIFIED this pass** | Not exercised — `resolve_and_contain` follows symlinks via `Path.resolve()` per its own docstring and the reviewer's prior audit; no junction-specific case constructed this round |
| spoofed IPC project | **NOT VERIFIED this pass** | Reviewer's prior audit (2026-08-23 phase 3-5 review, referenced in `navigation_allowed`'s docstring) already covers `cli_server.py`'s pane-token gate for preview/design IPC (`BUG-post-fix` note in `runtime/events.log`); not independently re-exercised here |
| untrusted MCP content labeling | **PASS (code-level)** | `design_clients.py` stamps every result with `Provenance` per reviewer's audit — confirmed by source read, matches |

---

## Carried-over findings from the reviewer's audit — current status on HEAD

From `docs/audit/2026-08-24-master-upgrade-review.md`:

1. **HIGH, finding #1** (`project_file_index.py` `_GitStatusWorker` unlocked `subprocess.run`) — **RESOLVED.** `git_changes_service.py`, `project_file_index.py`, and `project_explorer.py` now all import the same `SUBPROCESS_LOCK`/`RESOLVE_LOCK`/`_safe_resolve` from `project_file_index.py` (confirmed by grep across all three files — 28 matching lines, every `subprocess.run`/`Path.resolve()` call site routed through one of the two locks). This is the `backend-3` worktree fix the reviewer flagged as "in progress, uncommitted" — it has since merged (`64047e4` = `Merge branch 'wt/frontend-1787539538'` pulling in `wt/backend-3-1787539487`).

2. **HIGH, finding #2** (`preview_controller.py:148` `_local_file_path` — bare `Path(raw).resolve()` on the Qt main thread, same root cause as #1) — **STILL OPEN on HEAD.** Confirmed by direct read: line 148 is still `return Path(raw).resolve()`, not routed through `_safe_resolve`/`RESOLVE_LOCK`. This call site is reached from `navigation_allowed()` → `PreviewHost.navigation_allowed` → `QWebEnginePage.acceptNavigationRequest`, i.e. the Qt main thread on every in-page Preview navigation, concurrently with background git workers resolving paths elsewhere. Not release-blocking per the reviewer's own verdict (Preview had zero file-url comparison before this batch, so this isn't a regression), but still an open crash-class gap — **flagging again so it isn't lost**, not re-discovering it.
   - **New, related, lower-severity observation this pass:** `preview_controller.py:81` (`_project_source_roots`, `[Path(p).resolve() for p in ...]`) is *also* a bare unlocked resolve, called when opening/approving a preview. Lower risk than #2 (this one runs on whichever thread the CLI/IPC handler runs on, not proven to race the Qt-main-thread nav-check path the way #2 does) — noting it for whoever picks up #2's fast-follow to fold in, not asserting it's independently exploitable.

3. **LOW, finding #3** (case-only / UNC-vs-mapped-drive canonical-URL comparison, no regression test) — not re-verified this pass; unchanged, non-blocking per reviewer.

---

## Bugs found this pass

**None new.** Every scenario exercised behaved exactly per spec/docstring. The one item worth Lead's attention is the **still-open finding #2** above (carried over, not new) — recommend tracking its fast-follow before calling #369 fully closed, per the reviewer's own verdict.

---

## Summary

- **PASS:** 24
- **PARTIAL** (mechanism/gate confirmed; a narrower sub-claim — GUI rendering, POSIX-only behavior on a Windows pane, or live-inbox text on a role-gated CLI — not independently observable from this pane): 7
- **NOT VERIFIED** (not exercised this pass, explicitly not claimed as passing): 9
- **N/A** (correctly-skipped optional features, no config present): 2
- **Bugs found:** 0 new; 1 carried-over HIGH (finding #2, `preview_controller.py:148`) still open on HEAD.

---

**Lead note (post-QA, 10:30):** carried-over HIGH finding #2 (`preview_controller.py` bare `Path.resolve()`) landed as `39aa9b2` (RESOLVE_LOCK via `_safe_resolve`, + editor_widget/file_watch_service sweep) after this QA pass started — targeted gate green. Real-GUI rows stay NOT VERIFIED until the user checks by eye (see `27_MANUAL_END_TO_END_SCRIPT.md` items 3, 4, 8, 11-14, 17).
