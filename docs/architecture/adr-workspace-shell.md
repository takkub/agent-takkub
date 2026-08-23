# ADR: Workspace Shell + Project Explorer (#365 phase 0–1, phase 2)

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

## Phase 2 — Monaco read-only

**One Monaco WebView, not one per project.** `13_PERFORMANCE_AND_QT_RULES.md` rule 6 says
"one Monaco Editor WebView *per project*, with internal file tabs"; the master dev plan's
RAM hard rule (`2026-08-23-master-dev-plan.md` §4, explicitly binding "ต่อแผนภายนอก") overrides
that to exactly **one WebView for the whole app**, lazily created on first file open and fully
destroyed once every internal tab closes. `EditorHost` (new `editor_widget.py`) owns that
lifecycle; `main_window.py` parks it in a `QDockWidget` outside every `ProjectTab` (same shell
pattern as the existing `_logs_dock`/`_tasks_dock`), hidden until the first file opens.
Switching projects switches the WebView's *content* (Monaco tabs/models), never the widget
itself — avoids the reparent-after-paint Chromium crash the same way phase-1's per-project
`ProjectExplorer` avoids it for its own tree.

**Diff is a per-tab view toggle, not a second tab.** A file open in two places (source +
diff-vs-HEAD) at once would need `EditorHost._open_paths` (path → project) to track two
distinct keys for one real file, and closing either one first would be ambiguous for the
"destroy when empty" RAM rule. Simpler: each open tab carries `viewMode: 'source' | 'diff'`
and a lazily-fetched `diffModels` pair; a small "±" button on the tab flips between two shared
editor instances (one `IStandaloneCodeEditor`, one `IStandaloneDiffEditor` — still just 2 DOM
editors total, not N). `bridge.requestDiff(path)` only round-trips to Python the first time a
tab's diff is opened; the JS-side toggle after that is free.

**Monaco bundle presence is a runtime feature-detect, not a hard dependency.** No network
access to vendor `monaco-editor` from this pane — `static/editor/vendor/` ships empty (see its
README, left for devops packaging) and `index.html`'s AMD loader script is loaded dynamically
with an `onerror`/timeout fallback. Missing bundle → the page still opens tabs, respects
containment/size-cap/binary-detection, and serves a plain read-only `<pre>` view (no syntax
highlight, no diff editor) instead of failing to load. This means phase 2 is fully testable and
mergeable before devops's packaging step lands, and degrades the same way phase 1's
`ProjectTab` degrades when `ProjectExplorer` construction fails (existing project convention,
not a new pattern).

**Terminal path-click now opens in the editor, not the OS default app.** `terminal_widget.py`'s
`_on_open_path` (already gated by the M3#13 containment/exec-extension checks from phase 0's
predecessor work) used to call `QDesktopServices.openUrl` directly; it now emits
`openInEditorRequested` instead, forwarded through `AgentPane` → `Orchestrator.register_pane`
(closure-binds the pane's project, mirroring how `inputBytes`/`closeRequested` are already
wired there) → `MainWindow._editor_host.open_file`. The exec-extension guard is untouched
(still reveals in the file manager, never hands an executable to an "open" verb); only the
non-exec path changed destination. "Open externally" stays reachable from the Explorer's own
context-menu action and from a button on the editor's binary/too-large placeholder tab.

**Security boundary is the same containment gate, called from a new module.** `EditorHost`
never re-implements path safety — every open/diff/reveal/open-externally call routes through
`project_file_index.resolve_and_contain` against that project's configured roots, the identical
gate `ProjectExplorer`'s context-menu actions already use. `project_file_index.py`'s "zero PyQt
widget imports" contract (see that module's own docstring) is why `editor_widget.py` keeps its
own small `_reveal_in_file_manager` copy instead of importing `ProjectExplorer._reveal` —
promoting that helper into `project_file_index.py` would pull `QtGui`/`QtWidgets` into a module
that's deliberately widget-free.

**Known limitation (deferred, not silently dropped):** phase 2 is read-only end-to-end — there
is no `saveFile` bridge slot, and every Monaco model is created with `readOnly: true`. Ctrl+S
inside the editor shows a toast instead of writing. Phase 3 (`editor_service.py`,
`file_watch_service.py`) owns atomic writes, dirty-state, and the mtime+size+sha256 conflict UI
already speced in `04_MONACO_EDITOR_SPEC.md`.

## Phase 3 — safe edit (UI over the merged `editor_service.py`/`file_watch_service.py`)

**Conflict baseline is server-tracked, never client-supplied.** `EditorHost._file_states`
(path → `EditorFileState`) is the single source of truth for what a save is checked against —
set on open and on a successful save, and deliberately **not** updated by a disk-watch
notification (`_on_disk_changed`'s docstring) or by a failed/conflicting save. The alternative —
trusting whatever "expected version" JS echoes back — would let a compromised or buggy renderer
forge a stale-looking version and force an overwrite; keeping the baseline server-side means the
worst a hostile page content could do is *fail* to save, never silently win a conflict it lost.

**A conflict resolves in one round-trip, not three.** `_SaveWorker` already runs off the Qt main
thread, so when `save_atomic` reports a conflict the same worker immediately re-reads the current
disk text (`read_file_for_editor`) and ships it to JS in the same `editorConflict(path, diskText)`
call — the JS-side `[Compare]` button builds its diff view from data it already has, no second
`requestDiff` needed. `[Keep mine]` re-snapshots the disk right before writing (`force=True` in
`_SaveWorker`) instead of trusting the conflict-time snapshot, so a second genuinely-new race
during the dialog is caught rather than blindly overwritten.

**Compare reuses the live model, not a copy.** `04_MONACO_EDITOR_SPEC.md`'s "diff editor" is one
shared `IStandaloneDiffEditor` (phase 2 decision, still true) — a conflict's "Compare" sets its
`modified` side to `tab.model` itself (not a fresh model with a snapshot of the text), so the
diff keeps reflecting the user's edits as they keep typing instead of freezing at conflict-time.
The cost is one piece of bookkeeping: disposeDiffModels() (JS ใน static/editor/index.html) never disposes a `modified` model
that turns out to be `tab.model`, and any code that's about to write a *different* diff pair
(the git-HEAD "±" toggle) checks for and discards a live-linked leftover first rather than
`setValue`-ing over the user's buffer through it.

**`FileWatchService` roots accumulate; they aren't fixed at construction.** The module's own
constructor takes a fixed root list (right for a per-project service), but `EditorHost` is one
app-wide instance whose open files can belong to *any* project a tab has open — so `add_roots()`
was added (small, additive) and `EditorHost` calls it with each project's roots the first time
one of its files opens. The alternative (one `FileWatchService` per project, torn down with its
last tab) would have duplicated `EditorHost`'s own "one instance total" shape for no benefit —
the watcher's containment check is defense-in-depth over paths that were already
`resolve_and_contain`-checked once by `read_file_for_editor` on open.

**Disk-changed is a banner, never an auto-reload — including when the buffer is clean.** Even a
non-dirty tab doesn't silently pull in the new disk content; the user always clicks
`[Reload disk]`. The self-save-echo problem (the watcher firing for the file *we* just wrote) is
solved by diffing the watcher's snapshot against `_file_states` (`_states_differ`, a local
mtime_ns/size/sha256 comparison — not a shared import of `editor_service`'s private
`_has_conflict`, kept as its own small copy since the two call sites want different consequences
from the same comparison) rather than by suppressing the watch during a save.

## Phase 4 — CHANGES panel (UI over the merged `git_changes_service.py`)

**A second git service per explorer, not a superset replacing the first.** `GitStatusService`
(phase 1, `project_file_index.py`) keeps badging tree rows exactly as it did; `GitChangesService`
(this phase) is a sibling instance in the same `ProjectExplorer`, feeding a distinct "CHANGES (n)"
tree row. Merging them into one service would have meant either porcelain-v2-only badges (a
behavior change to an already-shipped feature, out of scope) or maintaining two parsers behind
one service — two small, single-purpose services was the smaller change.

**Click → diff is one round-trip through `EditorHost`, not a second diff surface.** A CHANGES row
click emits `ProjectExplorer.changeActivated` → `ProjectTab.openDiffRequested` →
`EditorHost.open_file(project, path, show_diff=True)`, which opens the file tab and immediately
calls the existing `_on_diff_requested` once the read lands — reusing phase 2's git-HEAD diff
path verbatim instead of giving `GitChangesService`'s own (unused by the UI) `request_diff`/
`diffReady` a second Monaco surface to feed. Exactly 2 DOM editors total stays true.

**No eager refresh at construction.** `GitChangesService.request_refresh()` is triggered by the
same events that already trigger `GitStatusService`'s: a real directory listing
(`_on_dir_listed`), plus (new) `EditorHost.gitRefreshNeeded` after a save or an external
disk-change — never unconditionally in `ProjectExplorer.__init__`. An unconditional refresh would
arm both services' debounce `QTimer`s for every opened project tab, including ones the user never
touches git through — a real regression risk for the existing `#344`/`#345` leaked-QTimer guard
this test suite enforces, for a panel that's meant to be lazy like the rest of the tree.

**Ask Agent sends through `Orchestrator.send`, the same path `takkub send` uses — never a new
channel.** The editor's "Ask Agent" (Monaco context-menu action + a per-tab "?" button) collects a
bounded selection (≤4000 chars, capped client-side and again in `MainWindow._on_editor_ask_agent`
defensively) and free-text request, then calls `orch.send(LEAD.name, msg, from_role="editor",
project=project_name)` — the identical call `Orchestrator.send()` already serves for CLI/IPC
sends, so it inherits that path's existing queueing-for-unspawned-Lead and delivery-tracking
behavior for free instead of re-implementing a parallel one.

## Post-phase-1 revision — Explorer moved into the sidebar (2026-08-23)

User feedback on the 1.2.0 build: the phase-1 `QSplitter` inside `ProjectTab` (decision above,
"Where the QSplitter lives") put the file tree between the `ProjectNav` sidebar and the pane
area, reading as an unwanted third column. Requested instead: the tree nests **under its own
project's sidebar card** (VS Code/Obsidian-style — click a project, its card expands to show the
tree below it; other cards stay collapsed), with a chevron on the card itself as the only manual
toggle.

`ProjectTab` still constructs and signal-wires its `ProjectExplorer` (nothing in phase 1's
containment/lazy-tree/CHANGES logic changes) but no longer lays it out — `project_nav.py`'s
`_ProjectRow` reparents the widget into a slot under the row's header strip, showing it only for
the selected row, and hands it back to `ProjectTab` on `removeTab` so the tab's own `deleteLater()`
teardown still owns its lifecycle. The per-project expanded/collapsed flag keeps the exact
`explorer/<safe_segment(project_name)>/collapsed` QSettings key phase 1 used — only which widget
reads/writes it changed. The sidebar itself changed from a fixed `212px` to a `QSplitter`-resizable
minimum-`212px` (drag up to unbounded, same "release maximumWidth after the expand animation"
convention `_tasks_dock_collapse_toggled` already used in `main_window.py`) since a tree needs more
room than the old rail-width sidebar gave it. See `project_nav.py`'s module docstring for the
current shape.
