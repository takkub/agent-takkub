# ADR: Workspace Shell + Project Explorer (#365 phase 0–1)

> อ่านคู่กับ `docs/plans/2026-08-23-master-dev-plan.md` §4 และ
> `docs/plans/workspace-1.2.0-design/` (18_MASTER_PROMPT, 02_TARGET_ARCHITECTURE,
> 03_PROJECT_EXPLORER_SPEC, 13_PERFORMANCE_AND_QT_RULES, 12_SECURITY_THREAT_MODEL).

## Phase 0 — delta audit vs. the external plan

The external plan (`18_MASTER_PROMPT_FOR_TAKKUB_LEAD.md`) assumes seams that partly don't
match current `main` — verified by reading `project_tab.py`, `main_window.py`,
`terminal_widget.py`, `agent_pane.py` before writing any code (not grep-and-guess).

| Plan's assumption | Reality on `main` (2026-08-23) | Adaptation |
|---|---|---|
| Explorer is a new left panel added *somewhere* in the shell | `ProjectTab` (`project_tab.py`) already owns a `QVBoxLayout` → single `QTabWidget` (`pane_tabs`) holding Lead + teammate panes as tabs (2026-06-26 redesign, panes-as-tabs). There is **no existing QSplitter anywhere in ProjectTab**; `MainWindow` only has the top-level `ProjectNav` sidebar / `ProjectTab` stack split. | The Explorer's `QSplitter` is added **inside `ProjectTab`**, wrapping `[explorer, pane_tabs]` — not in `MainWindow`. This keeps the explorer scoped per-project (matches "roots come from project config" — one explorer instance per open project tab, not a global singleton), and keeps the change local to one already-isolated widget. |
| `main_window.py` is a file phase 1 touches | Nothing in phase 1's deliverable (explorer construction, splitter, collapse, context menu, root containment) needs anything from `MainWindow` — `ProjectTab` already receives `project_name` at construction and is self-sufficient. | **`main_window.py` is untouched by phase 1.** Noted explicitly per 18_MASTER_PROMPT's own instruction to "adapt seams if current architecture has improved" — this also shrinks the PR-conflict surface named by the plan's own rule ("ห้ามมี 2 branch แตะ ProjectTab/MainWindow พร้อมกัน"): only `ProjectTab` is touched, not both. |
| Root containment logic doesn't exist yet | It does: `lead_context._allowed_project_roots(project)` already resolves every path in a project's `paths` dict and is the source of truth for Lead's own write-deny rules. | `project_file_index.resolve_and_contain()` is the Explorer's own containment gate, built independently (kept the UI layer from importing the heavier, Lead-spawn-focused `lead_context` module) but reads the **same** underlying source — `projects.json` → `projects[name].paths` — so the two containment boundaries can never disagree about what's "in" a project. |
| No existing per-project UI-state store | Confirmed: `custom_role_colors` (also per-ProjectTab) is in-memory only, never persisted. The one existing UI-state persistence mechanism is `MainWindow`'s `self._settings = QSettings("agent-takkub", "cockpit")`, used today only for `window/geometry`. `projects.json` (via `config.py`) holds project *config* (paths, active project, open tabs), not transient UI state. | Explorer width/collapsed state persists via a **new `QSettings("agent-takkub", "cockpit")` instance inside `ProjectTab`** (same org/app pair as MainWindow's, so it's the same underlying store), keyed `explorer/<safe_segment(project_name)>/{width,collapsed}` — same mechanism, not a second competing one. |
| Git status badges | No `git_changes_service.py` exists yet (that's phase 4's file). | Phase 1 ships a **working skeleton**: `GitStatusService` in `project_file_index.py` — background `QRunnable` + debounced `QTimer`, wired into `ProjectExplorer` to color-badge tree rows on expand. Full watcher-triggered refresh + diff view stays phase 4 scope, called out explicitly in both modules' docstrings. |

**Keepalive tests that must not be touched or broken:** `tests/test_keepalive_suspend.py`
(`TestPaneKeepalive`, `TestLeadUnreadDot`) and `tests/test_project_nav.py`'s
`ProjectTab("proj-a")` corner-widget tests — both construct `ProjectTab` directly and reach
into `tab.pane_tabs`. The splitter change keeps `self.pane_tabs` as the same live attribute
(just re-parented into the splitter instead of directly into the layout), so these are
unaffected; ran targeted to confirm (see phase 1 DoD below).

## Phase 1 — decisions

**Where the QSplitter lives.** Inside `ProjectTab`, wrapping `[ProjectExplorer, pane_tabs]`
horizontally. Rejected: putting it in `MainWindow` around the whole `ProjectNav`/tab-stack —
that would make the explorer a single global panel shared across every open project tab
(wrong: roots are per-project, and 03_PROJECT_EXPLORER_SPEC.md's "roots come from project
config" implies per-project scope). One `QSplitter` + one `ProjectExplorer` per `ProjectTab`
means switching projects switches explorers for free (it's just a different widget on a
different tab), no extra state-swapping code needed.

**Explorer is a view, only a view.** `project_explorer.py` never touches the filesystem for
anything that could block — directory listing goes through `ProjectFileIndex.request_list()`
(dispatches to `QThreadPool.globalInstance()`), and the same is true for git status
(`GitStatusService`, its own `QRunnable`). The three synchronous filesystem calls the view
*does* make directly (open externally / reveal / copy path) are all instantaneous OS calls on
an already-`resolve_and_contain()`-checked path, not scans — matching
13_PERFORMANCE_AND_QT_RULES.md rule 1 ("no recursive repo scan on Qt main thread") without
over-applying it to genuinely trivial calls.

**Services are separate from the view.** `project_file_index.py` has zero PyQt widget
imports (`QObject`/`QRunnable`/`QTimer` only) and its core logic (`resolve_and_contain`,
`list_dir_sync`, `_parse_gitignore`) is plain, synchronous, directly unit-testable functions —
called from a worker thread in production, called directly (no Qt event loop needed) in tests.
This is why the security-critical tests (containment / traversal / symlink-and-junction
escape / ignore policy) don't need a running `QTreeWidget` at all.

**Rejected alternative: `QFileSystemModel`.** Qt's built-in model already lazy-populates on a
private worker thread, which would have satisfied the "no main-thread scan" rule for free.
Rejected anyway: it can't be taught the project's custom ignore policy or `.gitignore` chain
without a `QSortFilterProxyModel` wrapping native OS-backed rows, and — the deciding factor —
its filtering logic wouldn't be unit-testable as plain Python, which the DoD explicitly asks
for (containment/traversal/symlink/ignore as tests, not manual QA).

## Security note (real finding, fixed before landing)

`list_dir_sync`'s first pass gated the per-entry containment re-check on
`os.DirEntry.is_symlink()`. On Windows, an **NTFS junction** — the exact link primitive this
codebase already uses elsewhere for admin-free directory links
(`worktree_manager._make_link`, `skill_scan._link_skill_into_project`) — reports
`is_symlink() == False` even though `Path.resolve()` follows it exactly like a symlink.
A junction pointing outside a project's roots would have silently passed through unflagged.
Fixed by re-verifying containment (`resolve_and_contain`) for **every** directory entry, not
just ones flagged as symlinks — confirmed via a smoke test that plants a real junction (via
the same `worktree_manager._make_link` helper, so no admin privilege needed) pointing outside
the sandbox and asserts it never appears in the listing.

## Known limitations (deferred, not silently dropped)

- `.gitignore` parsing is a documented subset (literal + `fnmatch` globs, `!` negation,
  trailing `/` for dir-only) — not `**`, not full git precedence beyond directory-chain
  ordering. Good enough to hide generated-repo noise; not a git reimplementation.
- Git status badges key off the project's *first* configured root only. A project spanning
  multiple independent repos only gets badges under that one root's repo. Phase 4 owns
  teaching it to walk up to each root's own nearest `.git`.
- No file-watcher yet — the tree only refreshes on manual expand; phase 4's
  `git_changes_service.py` is expected to add debounced watcher-triggered refresh.
- Editor size-limit-before-load (12_SECURITY_THREAT_MODEL.md) doesn't apply yet — there's no
  editor in phase 1 to gate.

## Rollback

Pure UI-layer addition — no persisted schema, no migration. Reverting `project_tab.py`,
deleting `project_explorer.py`/`project_file_index.py`, and clearing the
`explorer/*` keys under the `agent-takkub`/`cockpit` QSettings store (harmless if left —
they're only ever read by code that no longer exists) fully undoes phase 1 with zero data
loss, since no other phase's code depends on these two modules yet.
