# Master Upgrade Pack — Phase 0 re-audit of `main` (2026-08-24)

Pack: `docs/plans/workspace-master-upgrade-2026-08-24/` (supersedes workspace-1.2.0-design + remaining-fixes packs)
HEAD at audit: `2f4ec50` · version `1.2.1` · open issues: #362 only (Phase 10/V2 authority — NOT touched by this roadmap)

## Delta matrix (Lead read the code, not the pack's claims)

| ID | Item | State on main | Evidence |
|---|---|---|---|
| BUG-001 | Preview file:// normalization | **BUG PRESENT** | `preview_controller.navigation_allowed` file mode: `target_url == current.target` (raw path vs `file:///…` from `QUrl.fromLocalFile`) |
| BUG-002 | Shared Preview rebound on project switch | **BUG PRESENT** | `main_window._on_tab_switched` never touches `_preview_host`; `previewOpened/Updated → show_state` unconditionally (background publish replaces the active project's view) |
| BUG-003 | Preview state cleared on project close | **BUG PRESENT** | `_close_project_tab` never calls `PreviewController.close` / `PreviewHost.close_project` |
| BUG-004 | POSIX mode preserved on save | **BUG PRESENT** | `editor_service._write_atomic_text` — no `stat.S_IMODE` / `os.chmod` on temp before replace |
| BUG-005 | Strict UTF-8 / BOM | **BUG PRESENT** | `editor_service.stat_snapshot` decodes with `errors="replace"`; no read-only/unsupported-encoding state |
| BUG-006 | Revise → Designer | **BUG PRESENT** | `orchestrator.design_revise` only `_notify_lead` |
| BUG-007 | Deleted-file diff-only | **PARTIAL** | `parse_status_v2` emits `D`; explorer resolves `repo_root/change.path` before diff (missing file path) |
| BUG-008 | Rename old_path | **MISSING** | `FileChange` has no `old_path`; rename record's orig token consumed and dropped |
| GAP-009 | Multi-root Git | **MISSING** | `project_explorer.__init__`: one `GitChangesService(first_root, …)` |
| GAP-010 | Explorer Ask Agent | **MISSING (disabled)** | `project_explorer.py:349` `act_ask_agent.setEnabled(False)` "Coming in a later phase" |
| GAP-011 | Git-native ignore parity | **PARTIAL** | `project_file_index._parse_gitignore` handwritten chain; no `git check-ignore` |
| GAP-012 | Real QWebEngine visual acceptance | **PARTIAL** | unit + widget tests only; manual script `27_MANUAL_END_TO_END_SCRIPT.md` |
| GAP-013 | OpenViking adapter | **MISSING** | only docstring mentions (`obsidian_boundary.py`, `obsidian_metadata.py`, `project_identity.py`) |
| GAP-014/15/16 | 21st / Figma / Penpot real clients | **STUB** | `core/capabilities/design_integrations.py` registry-only; Storybook detection is real |
| GAP-017 | unified Design Context source | **MISSING** | — |
| GAP-018 | retrieval observability / context trace | **PARTIAL** | `core/brain/context_builder.py` = Brain + Conversation only, no per-source trace, no OV/Graft/resource sources |
| — | Explorer / Monaco / atomic save / conflict / watcher / device presets / publish / approve-revise UI / Obsidian metadata+dedup / Graft | **DONE** | per 1.2.1 (`docs/audit/2026-08-23-365-workspace-ram-acceptance.md`) |

## Execution plan (batches per `25_MASTER_PROMPT_FOR_LEAD.md`, waves per Lead patterns)

- Wave 1 (parallel, `--isolation worktree`): A1 preview 001/002/003 · A2 editor 004/005 · A3 revise 006 · B git 007/008/009
- Wave 2: C explorer 010/011 (serialized after B — same `project_explorer.py`)
- Wave 3 (parallel): D design integrations 014/015/016 + doctor · E OpenViking 013/017/018 + context sources + doctor
- Wave 4: F — reviewer/security pass, qa full gate (CI), visual QA script, release recommendation

Release: A+B strictly corrective → 1.2.2 candidate; C/D/E user-visible → 1.3.0 per `22_RELEASE_STRATEGY.md`. #362 / Phase 10 untouched.
