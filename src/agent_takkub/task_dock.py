"""Task Tree dock (A8): a read-only, right-hand `QDockWidget` panel that
renders the Task Ledger (A7) as a live tree — one collapsible card per
project, then 🎯 goal → feature → task rows with a status badge.

Reads `task_ledger.load_state()` (the JSON sidecar A7 already writes on every
assign/done/fail/close) directly — never parses `INDEX.md` markdown, so the
tree can't drift from the ledger's own structured view.

Pure helpers (`status_glyph`, `project_progress`, `has_any_rows`) are kept
free of any Qt import so they're unit-testable without a QApplication; only
`TaskDockWidget` itself touches PyQt.

Visuals (A8-polish) match the left PROJECTS sidebar's design language
(`project_nav._SIDEBAR_QSS`/`_ProjectRow`): dark card background, rounded
hover/selected rows, and the same deterministic avatar coloring — reused
directly from `project_nav` (and `token_meter.usage_color` for badge/bar
color) so the same project shows the same avatar tint in both places.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, task_ledger
from .project_nav import _avatar_color, _initials
from .token_meter import usage_color

# Dock width targets for the collapse-to-rail toggle (mirrors project_nav's
# _EXPANDED_W/_COLLAPSED_W pattern) — exported so main_window can animate the
# containing QDockWidget's width in lockstep with this widget's own layout
# swap (a QDockWidget's width isn't a property this widget controls itself).
EXPANDED_MIN_W = 260
COLLAPSED_W = 64

# (glyph, hex color) per ledger row status — mirrors task_ledger._ROW_SYMBOL
# but with the dock's own richer glyphs/colors (INDEX.md uses plain ASCII
# checkboxes since markdown can't carry color). Colors are cockpit_theme state
# tokens (semantic — never gold): the value survives migration, only the
# literal is tokenized. superseded reuses the parallel-chip purple.
_STATUS_GLYPH = {
    "working": ("◔", cockpit_theme.STATE_WARN_BRIGHT),  # ◔ amber
    "ok": ("✓", cockpit_theme.STATE_OK_BRIGHT),  # ✓ green
    "fail": ("✕", cockpit_theme.STATE_ERROR),  # ✕ red
    "closed": ("➖", cockpit_theme.TEXT_MUTED),  # ➖ gray
    "superseded": ("›", cockpit_theme.PARALLEL_CHIP_TEXT),  # › purple
}
# Any status the ledger doesn't emit yet (e.g. a future "queued" row) falls
# back to an empty checkbox in neutral gray instead of crashing render.
_STATUS_FALLBACK = ("☐", cockpit_theme.TEXT_MUTED)  # ☐

# Extra QTreeWidgetItem data role (column 0): the row's un-prefixed label
# text — goal/feature items re-derive their ▸/▾-prefixed text from this on
# every expand/collapse.
_ROLE_BASE_LABEL = Qt.ItemDataRole.UserRole + 1

# Extra QTreeWidgetItem data role (column 0): max wrapped-line count for a
# header row (goal/feature) — `_WrapItemDelegate` caps the grown row height
# to this many lines so a long goal doesn't eat the whole dock (critic
# round-2 finding 1); plain task rows carry no such data and keep growing
# to fit their (already short) summary text untouched.
_ROLE_MAX_LINES = Qt.ItemDataRole.UserRole + 2
_HEADER_MAX_LINES = 2

# Goal/feature header label text is clamped to this many characters (plus a
# "…" ellipsis) before it ever reaches the tree — belt-and-braces alongside
# the delegate's line cap, and what actually produces a real, consistent
# ellipsis character (the delegate only clips overflow, it doesn't paint
# "…"). The full, untruncated text is always set as the row's tooltip.
_GOAL_LABEL_MAX_CHARS = 140
_FEATURE_LABEL_MAX_CHARS = 100


def _clamp_label(text: str, limit: int) -> str:
    """*text* truncated to *limit* chars with a trailing "…" if it overflows.

    Short/blank text is returned unchanged (no-op below the limit) — this is
    what makes the ellipsis consistent: every label over the limit gets cut
    at the exact same character, instead of wrapping wherever a line break
    happens to land."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_DOCK_QSS = f"""
#taskDockRoot {{
    background: {cockpit_theme.GROUND_SIDEBAR};
}}
#taskDockHeader {{
    color: {cockpit_theme.TEXT_FAINT};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 6px 6px 4px 6px;
}}
QTreeWidget#taskTree {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    color: {cockpit_theme.TEXT_SECONDARY};
    border: 1px solid {cockpit_theme.BORDER_STRONG};
    border-radius: {cockpit_theme.RADIUS_SM}px;
    outline: 0;
    padding: 4px;
}}
QTreeWidget#taskTree::item {{
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 3px 2px;
    margin: 1px 0;
}}
QTreeWidget#taskTree::item:hover {{
    background: {cockpit_theme.GROUND_PANEL};
}}
QTreeWidget#taskTree::item:selected {{
    background: {cockpit_theme.GROUND_SELECT};
}}
QTreeWidget#taskTree::branch {{
    background: transparent;
    border-image: none;
    image: none;
}}
#taskDockToggleBtn {{
    background: transparent;
    color: {cockpit_theme.TEXT_FAINT};
    border: none;
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 7px;
    margin: 4px 0 0 0;
    font-size: 12px;
    font-weight: 600;
}}
#taskDockToggleBtn:hover {{
    background: {cockpit_theme.GROUND_PANEL};
    color: {cockpit_theme.TEXT_MUTED};
}}
#taskRail {{
    background: transparent;
}}
QTabWidget#taskDockTabs::pane {{
    border: none;
    background: {cockpit_theme.GROUND_SIDEBAR};
}}
QTabWidget#taskDockTabs QTabBar {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    qproperty-drawBase: 0;
}}
QTabWidget#taskDockTabs QTabBar::tab {{
    background: transparent;
    color: {cockpit_theme.TEXT_MUTED};
    padding: 6px 10px;
    margin: 0;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 11px;
}}
QTabWidget#taskDockTabs QTabBar::tab:hover {{
    color: {cockpit_theme.TEXT_SECONDARY};
    background: {cockpit_theme.GROUND_PANEL};
}}
QTabWidget#taskDockTabs QTabBar::tab:selected {{
    color: {cockpit_theme.TEXT_PRIMARY};
    border-bottom: 2px solid {cockpit_theme.ACCENT_GOLD};
    background: {cockpit_theme.GROUND_PANEL};
}}
QTreeWidget#gitStatusTree {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    color: {cockpit_theme.TEXT_SECONDARY};
    border: 1px solid {cockpit_theme.BORDER_STRONG};
    border-radius: {cockpit_theme.RADIUS_SM}px;
    outline: 0;
    padding: 4px;
}}
QTreeWidget#gitStatusTree::item {{
    border-radius: {cockpit_theme.RADIUS_SM}px;
    padding: 3px 2px;
    margin: 1px 0;
}}
QTreeWidget#gitStatusTree::item:hover {{
    background: {cockpit_theme.GROUND_PANEL};
}}
QTreeWidget#gitStatusTree::branch {{
    background: transparent;
    border-image: none;
    image: none;
}}
"""


# Content-width padding for the wrap delegate: the status icon column (16px)
# plus item padding/spacing. Approximate — the delegate only needs a width
# close enough to grow the row to the right line count.
_WRAP_ICON_PAD = 30
_WRAP_V_PAD = 6


class _WrapItemDelegate(QStyledItemDelegate):
    """Grow a plain tree row to fit its word-wrapped text.

    `QTreeView` paints wrapped text when `wordWrap(True)` is set, but it never
    *grows the row* to fit it — row heights come from a single-line sizeHint,
    so a long goal/feature/task label clips to one line (proven: an identical
    tree with a 110-char row stayed the same 12px height as a 10-char row).
    Only the project row escaped this because it's an item widget that sizes
    itself. This delegate measures the wrapped height against the row's actual
    content width (viewport minus this row's indentation) and returns it, so
    the view allocates enough vertical space. Rows with no text (the project
    row, whose text is cleared once its `ProjectCardWidget` mounts) fall
    through to the base single-line/explicit-sizeHint behavior untouched.
    """

    def __init__(self, tree: QTreeWidget) -> None:
        super().__init__(tree)
        self._tree = tree

    def _content_width(self, index) -> int:
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        indent = self._tree.indentation()
        viewport_w = self._tree.viewport().width()
        return max(viewport_w - (depth + 1) * indent - _WRAP_ICON_PAD, 40)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        base = super().sizeHint(option, index)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not text:
            return base
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        width = self._content_width(index)
        metrics = QFontMetrics(opt.font)
        wrapped = metrics.boundingRect(
            0, 0, width, 100_000, int(Qt.TextFlag.TextWordWrap), str(text)
        )
        height = wrapped.height() + _WRAP_V_PAD
        max_lines = index.data(_ROLE_MAX_LINES)
        if isinstance(max_lines, int):
            height = min(height, max_lines * metrics.lineSpacing() + _WRAP_V_PAD)
        return QSize(base.width(), max(base.height(), height))


def status_glyph(status: str) -> tuple[str, str]:
    """(unicode glyph, hex color) for a ledger row's `status` field."""
    return _STATUS_GLYPH.get(status, _STATUS_FALLBACK)


def project_progress(state: dict) -> tuple[int, int]:
    """(done rows, total rows) across every group/feature in *state*.

    "done" counts `ok` only, matching the `progress: X/Y` line `task_ledger`
    itself renders into INDEX.md — `fail`/`closed`/`superseded` are terminal
    but not successes, `working` is still open.
    """
    total = 0
    done = 0
    for group in state.get("groups", []):
        for feat in group.get("features", []):
            for row in feat.get("rows", []):
                total += 1
                if row.get("status") == "ok":
                    done += 1
    return done, total


def has_any_rows(state: dict) -> bool:
    """True if *state* has at least one task row — an empty/never-assigned
    project's card is skipped instead of rendered blank."""
    return project_progress(state)[1] > 0


def _is_fallback_goal(goal: str) -> bool:
    """True for a ledger group with no real goal — `task_ledger`'s own
    `_FALLBACK_GOAL` placeholder, or blank (defensive: any future writer that
    forgets to fall back)."""
    goal = (goal or "").strip()
    return not goal or goal == task_ledger._FALLBACK_GOAL


def feature_emoji(feature: dict) -> str:
    """Mirrors `task_ledger._feature_emoji` (rows-in-progress → 🔨, any
    failure → ⚠️, all terminal → ✅, empty → ⏳) so the dock and INDEX.md
    never disagree about a feature's at-a-glance state."""
    statuses = {r.get("status") for r in feature.get("rows", [])}
    if not statuses:
        return "⏳"  # ⏳
    if "working" in statuses:
        return "\U0001f528"  # 🔨
    if "fail" in statuses:
        return "⚠️"  # ⚠️
    if statuses <= {"ok", "closed", "superseded"}:
        return "✅"  # ✅
    return "⏳"  # ⏳


# ──────────────────────────────────────────────────────────────
# 🌿 Git tab pure helpers — no Qt import, so they're unit-testable without a
# QApplication. Operate on `git_status.RepoStatus`/`Commit`/`Worktree`
# (attribute access, not dict) but are duck-typed against any object with the
# same attributes so tests don't need the real dataclasses.
# ──────────────────────────────────────────────────────────────
def repo_state_color(repo) -> str:
    """Badge color for a repo's header row: an unreadable repo (`error` set)
    is faint gray (not red — it's a read failure, not a git state to fix),
    any dirty/untracked file is warn-amber, ahead-of-upstream-only is the
    gold accent, and a clean, up-to-date repo is plain muted text."""
    if getattr(repo, "error", None):
        return cockpit_theme.TEXT_FAINT
    if repo.staged or repo.modified or repo.untracked:
        return cockpit_theme.STATE_WARN_BRIGHT
    if repo.ahead:
        return cockpit_theme.ACCENT_GOLD
    return cockpit_theme.TEXT_MUTED


def repo_status_summary(repo) -> str:
    """`<branch>  ↑<ahead> ↓<behind>  ●<dirty> +<untracked>` (or `clean`
    when nothing is outstanding) — the repo's `error` message stands in for
    the whole summary when the repo couldn't be read."""
    if getattr(repo, "error", None):
        return str(repo.error)
    parts = [repo.branch or "?"]
    if repo.upstream:
        parts.append(f"↑{repo.ahead} ↓{repo.behind}")
    dirty = repo.staged + repo.modified
    if dirty or repo.untracked:
        bits = []
        if dirty:
            bits.append(f"●{dirty}")
        if repo.untracked:
            bits.append(f"+{repo.untracked}")
        parts.append(" ".join(bits))
    else:
        parts.append("clean")
    return "  ".join(parts)


def repo_header_label(repo) -> str:
    """`🌿 <key>   <status summary>` — a repo's top-level tree row text."""
    return f"\U0001f33f {repo.key}   {repo_status_summary(repo)}"


def commit_row_label(commit) -> str:
    """`·  <short sha>  <subject>   <rel time>` — a commit child-row's text."""
    sha = (commit.sha or "")[:7]
    return f"·  {sha}  {commit.subject}   {commit.rel_time}"


def commit_tooltip(commit) -> str:
    """Full subject + author — the short row label elides neither, but a
    long subject can still exceed the dock's width, so the tooltip repeats
    it in full alongside the author."""
    return f"{commit.subject}\n{commit.author}"


def worktree_row_label(wt) -> str:
    """`<branch>  ↑<commits ahead> dirty` — a worktree child-row's text,
    omitting the ahead/dirty suffix entirely when there's nothing to flag."""
    bits = []
    if wt.commits_ahead:
        bits.append(f"↑{wt.commits_ahead}")
    if wt.dirty:
        bits.append("dirty")
    suffix = f"  {' '.join(bits)}" if bits else ""
    return f"{wt.branch}{suffix}"


def worktree_color(wt) -> str:
    return cockpit_theme.STATE_WARN_BRIGHT if wt.dirty else cockpit_theme.TEXT_MUTED


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _row_status_icon(glyph: str, color: str) -> QIcon:
    """A small rounded pill icon (tinted circle + glyph) for a task row's
    status column — painted in-memory so it needs no bundled image asset and
    stays crisp on both Windows and macOS. Rendered as a real `QIcon` (not an
    item widget) so it paints through the normal item delegate alongside the
    item's own text — an item widget here would sit over transparent space
    and let the delegate's separately-painted text bleed through underneath,
    doubling the text visually.
    """
    size = 16
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    r, g, b = _hex_to_rgb(color)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(r, g, b, 40))
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor(color))
    font = painter.font()
    font.setPointSize(8)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pix)


class _GitStatusSignals(QObject):
    """`QRunnable` isn't a `QObject` (no signals of its own) — this small
    helper is what actually carries a worker's result back onto the GUI
    thread; Qt auto-queues the connection since emit happens off-thread."""

    finished = pyqtSignal(str, list)
    failed = pyqtSignal(str, str)


class _GitStatusWorker(QRunnable):
    """Runs `git_status.collect()` off the GUI thread — per-repo `git log`/
    `git status`/worktree subprocess calls are slow enough to stall the UI if
    run inline (main-thread stall is a proven bug class in this codebase).
    Imports `git_status` lazily so this module doesn't hard-fail to import
    if that module isn't present yet.
    """

    def __init__(self, project: str, signals: _GitStatusSignals) -> None:
        super().__init__()
        self._project = project
        self._signals = signals

    def run(self) -> None:
        try:
            from . import git_status
        except ImportError as exc:
            self._signals.failed.emit(self._project, f"git_status module ไม่พร้อมใช้งาน ({exc})")
            return
        try:
            repos = git_status.collect(self._project)
        except Exception as exc:
            self._signals.failed.emit(self._project, str(exc))
            return
        self._signals.finished.emit(self._project, repos)


_GIT_PLACEHOLDER = "เลือกโปรเจคเพื่อดู git status"
_GIT_LOADING = "กำลังอ่าน git…"
_GIT_POLL_MS = 30_000


class GitStatusView(QWidget):
    """🌿 Git tab body: one tree row per repo (branch/ahead-behind/dirty
    badge), expanding to recent commits + `wt/*` worktrees underneath.

    Refresh is event-driven, never a background poll while unattended: it
    fires on project switch, on tab-select (`set_active(True)`), on the ↻
    button, and — only while both the dock is open *and* this tab is the one
    on screen — every 30s via an internal `QTimer` that `set_active(False)`
    stops.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: str | None = None
        self._busy = False
        self._pending = False
        self._live_signals: _GitStatusSignals | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(4)

        header = QHBoxLayout()
        header.addStretch(1)
        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("↻")
        self._refresh_btn.setToolTip("รีเฟรช git status")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setAutoRaise(True)
        self._refresh_btn.setStyleSheet(
            f"QToolButton {{ color: {cockpit_theme.TEXT_MUTED}; background: transparent;"
            f" border: none; font-size: 13px; }}"
            f"QToolButton:hover {{ color: {cockpit_theme.TEXT_SECONDARY}; }}"
        )
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setObjectName("gitStatusTree")
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._tree.setWordWrap(True)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.setUniformRowHeights(False)
        self._tree.setItemDelegate(_WrapItemDelegate(self._tree))
        header_view = self._tree.header()
        header_view.setStretchLastSection(True)
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._tree, 1)

        self._set_placeholder(_GIT_PLACEHOLDER)

        self._timer = QTimer(self)
        self._timer.setInterval(_GIT_POLL_MS)
        self._timer.timeout.connect(self.refresh)

    # ──────────────────────────────────────────────────────────────
    def set_project(self, project: str | None) -> None:
        if project == self._project:
            return
        self._project = project
        if project is None:
            self._timer.stop()
            self._set_placeholder(_GIT_PLACEHOLDER)
            return
        self.refresh()

    def set_active(self, active: bool) -> None:
        """Start/stop the 30s poll — only while the dock is open and this is
        the tab actually on screen; never poll in the background."""
        if active and self._project is not None:
            if not self._timer.isActive():
                self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    # ──────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        if self._project is None:
            return
        if self._busy:
            # A refresh is already in flight — queue exactly one follow-up
            # instead of piling up parallel git subprocess calls.
            self._pending = True
            return
        self._busy = True
        self._set_placeholder(_GIT_LOADING)
        signals = _GitStatusSignals()
        signals.finished.connect(self._on_finished)
        signals.failed.connect(self._on_failed)
        self._live_signals = signals  # keep alive until the callback fires
        QThreadPool.globalInstance().start(_GitStatusWorker(self._project, signals))

    def _on_finished(self, project: str, repos: list) -> None:
        self._busy = False
        if project == self._project:
            self._render(repos)
        self._maybe_run_pending()

    def _on_failed(self, project: str, message: str) -> None:
        self._busy = False
        if project == self._project:
            self._set_placeholder(f"⚠ อ่าน git ไม่สำเร็จ: {message}", error=True)
        self._maybe_run_pending()

    def _maybe_run_pending(self) -> None:
        if self._pending:
            self._pending = False
            self.refresh()

    # ──────────────────────────────────────────────────────────────
    def _set_placeholder(self, text: str, error: bool = False) -> None:
        self._tree.clear()
        item = QTreeWidgetItem([text])
        color = cockpit_theme.STATE_ERROR if error else cockpit_theme.TEXT_FAINT
        item.setForeground(0, QColor(color))
        self._tree.addTopLevelItem(item)

    def _render(self, repos: list) -> None:
        self._tree.clear()
        if not repos:
            self._set_placeholder("ไม่มี git repo ในโปรเจคนี้")
            return
        for repo in repos:
            top = QTreeWidgetItem([repo_header_label(repo)])
            top.setForeground(0, QColor(repo_state_color(repo)))
            self._tree.addTopLevelItem(top)
            if getattr(repo, "error", None):
                continue
            for commit in getattr(repo, "commits", None) or []:
                citem = QTreeWidgetItem([commit_row_label(commit)])
                citem.setForeground(0, QColor(cockpit_theme.TEXT_MUTED))
                citem.setToolTip(0, commit_tooltip(commit))
                top.addChild(citem)
            worktrees = getattr(repo, "worktrees", None) or []
            if worktrees:
                wt_group = QTreeWidgetItem([f"🔀 worktrees ({len(worktrees)})"])
                wt_group.setForeground(0, QColor(cockpit_theme.TEXT_MUTED))
                top.addChild(wt_group)
                for wt in worktrees:
                    witem = QTreeWidgetItem([worktree_row_label(wt)])
                    witem.setForeground(0, QColor(worktree_color(wt)))
                    wt_group.addChild(witem)
            top.setExpanded(True)
        self._tree.updateGeometries()
        self._tree.scheduleDelayedItemsLayout()


class TaskDockWidget(QWidget):
    """Right-dock panel body: 📋 Tasks (a QTreeWidget of the active project's
    ledger) and 🌿 Git (per-repo status) as two tabs."""

    # Emitted after a collapse/expand toggle so main_window can animate the
    # containing QDockWidget's width in lockstep (this widget only controls
    # its own internal layout, not the dock's outer size).
    collapseToggled = pyqtSignal(bool)

    _GIT_TAB_INDEX = 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("taskDockRoot")
        self.setStyleSheet(_DOCK_QSS)

        # The dock only ever shows one project's ledger — the active tab's
        # (set via `set_project`), not every open project mixed together.
        # `None` until main_window wires it up on boot/tab-switch.
        self._project: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        self._title = QLabel("Task List")
        self._title.setObjectName("taskDockHeader")
        root.addWidget(self._title)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("taskDockTabs")
        # currentChanged connects only after both tabs (and `_git_view`) exist
        # below — QTabWidget fires it the moment the first tab lands (index
        # -1 → 0), and the slot reads `self._git_view`.
        root.addWidget(self._tabs, 1)

        self._tree = QTreeWidget()
        self._tree.setObjectName("taskTree")
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        # No ellipsis anywhere — project/goal/feature/task labels show in
        # full; word-wrap (not horizontal scroll) is what makes a long
        # label fit a narrow dock (A8-polish, gemini spec Method A).
        self._tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._tree.setWordWrap(True)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Explicit (Qt's own default is already False) — uniform row heights
        # would force every row to the tallest single-line height instead of
        # letting a wrapped 2-line goal/feature label grow its own row.
        self._tree.setUniformRowHeights(False)
        # The default delegate word-wraps painting but never grows the row —
        # this delegate returns the wrapped height so long goal/feature/task
        # labels actually reflow to a 2nd line instead of clipping (the
        # project row is an item widget and sizes itself, so it's unaffected).
        self._tree.setItemDelegate(_WrapItemDelegate(self._tree))
        header = self._tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # QTreeView caches row heights and won't recompute them just because
        # the column got narrower/wider — force the cache to drop on every
        # resize so wrapped rows don't overlap the row below them.
        header.sectionResized.connect(lambda *_a: self._tree.updateGeometries())
        self._tabs.addTab(self._tree, "📋 Tasks")

        self._git_view = GitStatusView()
        self._tabs.addTab(self._git_view, "🌿 Git")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Collapsed-rail body: one avatar per open project, stacked vertically
        # (mirrors project_nav's collapsed sidebar). Hidden until collapsed.
        self._rail = QWidget()
        self._rail.setObjectName("taskRail")
        self._rail_layout = QVBoxLayout(self._rail)
        self._rail_layout.setContentsMargins(0, 4, 0, 4)
        self._rail_layout.setSpacing(6)
        self._rail_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._rail.hide()
        root.addWidget(self._rail, 1)
        self._rail_avatars: dict[str, QLabel] = {}

        # Collapse toggle — same idiom as project_nav's #sidebarToggleBtn
        # («  Collapse ↔ »), sitting at the very bottom of the dock body.
        self._collapsed = False
        self._toggle_btn = QPushButton("«  Collapse")
        self._toggle_btn.setObjectName("taskDockToggleBtn")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setToolTip("ยุบ Task List เป็นแถบ avatar")
        self._toggle_btn.clicked.connect(self.toggle_collapsed)
        root.addWidget(self._toggle_btn)

        # Chevron toggle button per open project card, keyed by project name
        # (not item identity — the item/widget are rebuilt on every refresh,
        # and Python `id()` can be reused after GC, so keying by identity
        # risks updating a stale/deleted button).
        self._chevron_labels: dict[str, QToolButton] = {}

        # Collapse state survives a refresh (else every ledger write would
        # snap every open card shut again) — keyed by the item's own
        # `UserRole` id string, recording every explicit expand/collapse the
        # user makes. In-memory only; does NOT persist across app restarts
        # (design tradeoff — see the A8 done-note to Lead).
        self._manual_state: dict[str, bool] = {}
        self._tree.itemCollapsed.connect(self._on_item_collapsed)
        self._tree.itemExpanded.connect(self._on_item_expanded)

        self.refresh_all()

    # ──────────────────────────────────────────────────────────────
    # 🌿 Git tab lifecycle — active only while the dock is visible AND the
    # Git tab is the one on screen (never poll git in the background).
    # ──────────────────────────────────────────────────────────────
    def _on_tab_changed(self, index: int) -> None:
        self._git_view.set_active(index == self._GIT_TAB_INDEX and self.isVisible())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._git_view.set_active(self._tabs.currentIndex() == self._GIT_TAB_INDEX)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._git_view.set_active(False)

    # ──────────────────────────────────────────────────────────────
    # collapse-to-rail toggle
    # ──────────────────────────────────────────────────────────────
    def toggle_collapsed(self) -> bool:
        """Flip between the full tree and the narrow avatar rail."""
        self.set_collapsed(not self._collapsed)
        return self._collapsed

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._title.setVisible(not collapsed)
        self._tabs.setVisible(not collapsed)
        self._rail.setVisible(collapsed)
        if collapsed:
            self._git_view.set_active(False)
        else:
            self._git_view.set_active(self._tabs.currentIndex() == self._GIT_TAB_INDEX)
        self._toggle_btn.setText("»" if collapsed else "«  Collapse")
        self._toggle_btn.setToolTip("ขยาย Task List" if collapsed else "ยุบ Task List เป็นแถบ avatar")
        self.collapseToggled.emit(collapsed)

    # ──────────────────────────────────────────────────────────────
    def _remember(self, item: QTreeWidgetItem, expanded: bool) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self._manual_state[key] = expanded

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._remember(item, True)
        self._apply_expanded_visual(item, True)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        self._remember(item, False)
        self._apply_expanded_visual(item, False)

    def _apply_expanded_visual(self, item: QTreeWidgetItem, expanded: bool) -> None:
        """Repaint the ▸/▾ chevron for *item* to match its expanded state —
        a project card's chevron button, or the text-embedded chevron on a
        goal/feature row (task rows carry neither and are left untouched)."""
        chevron = "▾" if expanded else "▸"
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str) and key.startswith("project:"):
            btn = self._chevron_labels.get(key[len("project:") :])
            if btn is not None:
                btn.setText(chevron)
            return
        base = item.data(0, _ROLE_BASE_LABEL)
        if isinstance(base, str):
            item.setText(0, f"{chevron}  {base}")

    def _restore_expansion(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        # Default open: a card/group the user never touched starts expanded
        # so first-run isn't a wall of collapsed rows; feature rows default
        # shut (tasks are the noisiest level). A row the user explicitly
        # expanded/collapsed keeps that state across refreshes.
        default_open = isinstance(key, str) and not key.startswith("feature:")
        expanded = self._manual_state.get(key, default_open)
        item.setExpanded(expanded)
        # setExpanded() only *emits* itemExpanded/itemCollapsed (and thus
        # fixes up the chevron) when the state actually changes — apply the
        # visual explicitly too so a no-op (already-default) case is correct.
        self._apply_expanded_visual(item, expanded)
        for i in range(item.childCount()):
            self._restore_expansion(item.child(i))

    # ──────────────────────────────────────────────────────────────
    def set_project(self, project: str | None) -> None:
        """Restrict the dock to *project*'s ledger card only — mirrors the
        active project tab so the Task List doesn't show every open
        project's tasks mixed together. Cards for any other project are
        dropped from the tree/rail; `None` clears the dock (no tabs open)."""
        if project == self._project:
            return
        self._project = project
        for i in reversed(range(self._tree.topLevelItemCount())):
            item = self._tree.topLevelItem(i)
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str) and key.startswith("project:"):
                other = key[len("project:") :]
                if other != project:
                    self._tree.takeTopLevelItem(i)
                    self._chevron_labels.pop(other, None)
                    self._remove_rail_avatar(other)
        if project is not None:
            self.refresh_project(project)
        self._relayout_tree()
        self._git_view.set_project(project)

    def refresh_all(self) -> None:
        """Reload the active project's ledger state and rebuild its card.

        Kept under the old name (used to loop every open project) — the dock
        only ever shows the active project now (see `set_project`), so this
        just re-runs `refresh_project` for it."""
        if self._project is not None:
            self.refresh_project(self._project)

    def refresh_project(self, project: str) -> None:
        """Reload just *project*'s ledger state — the slot `Orchestrator.
        ledgerChanged` connects to, so a single assign/done/fail/close only
        repaints the one card that actually changed. Ignored for any project
        other than the active one (see `set_project`)."""
        if self._project is not None and project != self._project:
            return
        state = task_ledger.load_state(project)
        existing = self._find_top_level(f"project:{project}")
        if not has_any_rows(state):
            if existing is not None:
                self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(existing))
                self._chevron_labels.pop(project, None)
            self._remove_rail_avatar(project)
            self._relayout_tree()
            return
        if existing is not None:
            self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(existing))
        item = self._build_project_item(project, state)
        # The subtree must already be attached to the tree (addTopLevelItem)
        # before setItemWidget() calls are valid — mount after adding, then
        # restore expansion (which also depends on the chevron button
        # existing, since _apply_expanded_visual looks it up by project name).
        self._tree.addTopLevelItem(item)
        self._mount_widgets(project, item, state)
        self._restore_expansion(item)
        self._update_rail_avatar(project, state)
        self._relayout_tree()

    def _relayout_tree(self) -> None:
        """Force Qt to recompute wrapped-row heights right after a rebuild.

        `QTreeView` only recomputes a wrapped text item's row height as a
        *side effect* of a column resize (see the `sectionResized` connection
        above) — it does not do so just because rows were added/replaced, so
        a goal/feature label that needs 2 lines renders clipped to 1 line
        until the user happens to resize the dock (A8-regression item 3).
        `updateGeometries()` drops the cached geometry immediately;
        `scheduleDelayedItemsLayout()` queues the full item layout pass
        (row heights included) that actually re-measures wrapped text.
        """
        self._tree.updateGeometries()
        self._tree.scheduleDelayedItemsLayout()

    def _update_rail_avatar(self, project: str, state: dict) -> None:
        """Add/refresh *project*'s avatar in the collapsed rail — kept in
        sync with the tree so the rail is never stale after the user
        collapses the dock."""
        done, total = project_progress(state)
        label = self._rail_avatars.get(project)
        if label is None:
            label = QLabel()
            label.setFixedSize(28, 28)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(project)
            self._rail_layout.addWidget(label)
            self._rail_avatars[project] = label
        label.setText(_initials(project))
        label.setStyleSheet(
            f"background: {_avatar_color(project)}; color: {cockpit_theme.TEXT_PRIMARY};"
            f" font-size: 10px; font-weight: 800; border-radius: 14px;"
        )
        label.setToolTip(f"{project}  ({done}/{total})")

    def _remove_rail_avatar(self, project: str) -> None:
        label = self._rail_avatars.pop(project, None)
        if label is not None:
            self._rail_layout.removeWidget(label)
            label.deleteLater()

    def _find_top_level(self, key: str) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == key:
                return it
        return None

    # ──────────────────────────────────────────────────────────────
    # tree construction (text + data only — no Qt item widgets yet)
    # ──────────────────────────────────────────────────────────────
    def _build_project_item(self, project: str, state: dict) -> QTreeWidgetItem:
        done, total = project_progress(state)
        item = QTreeWidgetItem([f"{project}  ({done}/{total})"])
        item.setData(0, Qt.ItemDataRole.UserRole, f"project:{project}")
        for group in state.get("groups", []):
            # Goalless assigns (`create_assignment(..., goal=None)`) each land
            # in their own ledger group keyed by date — a project assigned-to
            # goalless on 2 different days rendered 2 separate "(ไม่ระบุ
            # เป้าหมาย)" 🎯 headers, each with its own confusing sub-count
            # (walkthrough cluster D item 3). Skip the 🎯 header for these and
            # add their features straight under the project row instead — one
            # flat list, no duplicate noise.
            if _is_fallback_goal(group.get("goal", "")):
                for feat in group.get("features", []):
                    item.addChild(self._build_feature_item(project, group, feat))
            else:
                item.addChild(self._build_group_item(project, group))
        return item

    def _build_group_item(self, project: str, group: dict) -> QTreeWidgetItem:
        rows_all = [r for f in group.get("features", []) for r in f.get("rows", [])]
        done = sum(1 for r in rows_all if r.get("status") == "ok")
        goal = group.get("goal", "")
        key = f"group:{project}:{group.get('date', '')}:{goal}"
        goal_display = _clamp_label(goal, _GOAL_LABEL_MAX_CHARS)
        base_label = f"\U0001f3af {goal_display}  ({done}/{len(rows_all)})"
        item = QTreeWidgetItem([f"▸  {base_label}"])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setData(0, _ROLE_BASE_LABEL, base_label)
        item.setData(0, _ROLE_MAX_LINES, _HEADER_MAX_LINES)
        item.setToolTip(0, goal)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(0, QColor(cockpit_theme.TEXT_PRIMARY_ALT))
        for feat in group.get("features", []):
            item.addChild(self._build_feature_item(project, group, feat))
        return item

    def _build_feature_item(self, project: str, group: dict, feature: dict) -> QTreeWidgetItem:
        name = feature.get("name", "")
        key = f"feature:{project}:{group.get('date', '')}:{group.get('goal', '')}:{name}"
        name_display = _clamp_label(name, _FEATURE_LABEL_MAX_CHARS)
        base_label = f"{feature_emoji(feature)} {name_display}"
        item = QTreeWidgetItem([f"▸  {base_label}"])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setData(0, _ROLE_BASE_LABEL, base_label)
        item.setData(0, _ROLE_MAX_LINES, _HEADER_MAX_LINES)
        item.setToolTip(0, name)
        item.setForeground(0, QColor(cockpit_theme.TEXT_MUTED))
        for row in feature.get("rows", []):
            item.addChild(self._build_row_item(row))
        return item

    def _build_row_item(self, row: dict) -> QTreeWidgetItem:
        glyph, color = status_glyph(row.get("status", ""))
        summary = row.get("summary", "")
        label = f"{glyph} {row.get('role', '')} · {summary}"
        item = QTreeWidgetItem([label])
        item.setIcon(0, _row_status_icon(glyph, color))
        item.setForeground(0, QColor(cockpit_theme.TEXT_MUTED))
        item.setToolTip(0, f"{row.get('cwd', '')}\n{summary}")
        return item

    # ──────────────────────────────────────────────────────────────
    # item widget (project card) — mounted only once the whole subtree is
    # attached to the tree, since setItemWidget() needs a valid model index.
    # ──────────────────────────────────────────────────────────────
    def _mount_widgets(self, project: str, item: QTreeWidgetItem, state: dict) -> None:
        self._mount_project_row(project, item, state)

    def _mount_project_row(self, project: str, item: QTreeWidgetItem, state: dict) -> None:
        """Replace the project item's column-0 rendering with a `ProjectCardWidget`."""
        card = ProjectCardWidget(item, self._tree, project, state)
        self._chevron_labels[project] = card.chevron
        self._tree.setItemWidget(item, 0, card)
        # The item's own text (set in _build_project_item) is only a
        # placeholder for before the widget mounts — leaving it set makes
        # Qt paint it *underneath* the (partly transparent) card, doubling
        # the project name/progress visually (A8-regression item 1). The
        # card widget is the sole renderer for this row from here on; no
        # data is lost since project/done/total already live in `state`.
        item.setText(0, "")

    @staticmethod
    def _open_index(project: str) -> None:
        path = task_ledger.index_path(project)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class ProjectCardWidget(QWidget):
    """Sidebar-style project row: chevron toggle + avatar (color shared with
    the PROJECTS sidebar via `_avatar_color`) + word-wrapped name + progress
    badge/mini-bar + ↗ open-INDEX.md button.

    `QTreeWidget` never queries an item-widget's own layout to size its row
    (unlike native text items, which `setWordWrap`+`updateGeometries` handle
    for free) — this widget propagates its post-layout height into the
    owning `QTreeWidgetItem.setSizeHint()` on every resize so a wrapped
    2-line name doesn't overlap the row below it. Below `_NARROW_W` the
    progress bar/badge and open button hide so the name keeps room to wrap
    instead of clipping (A8-polish, gemini spec section 2).
    """

    _NARROW_W = 230

    def __init__(
        self,
        item: QTreeWidgetItem,
        tree: QTreeWidget,
        project: str,
        state: dict,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.tree = tree
        self.project = project
        self.setObjectName("taskProjectCard")
        # Transparent — an opaque fill here sat on top of the tree item and
        # hid QTreeWidget#taskTree::item:hover/:selected underneath it,
        # making hover/selection look broken (A8-polish, gemini spec §3).
        self.setStyleSheet(
            "#taskProjectCard {"
            " background: transparent;"
            f" border: 1px solid {cockpit_theme.BORDER_STRONG};"
            f" border-radius: {cockpit_theme.RADIUS_MD}px;"
            "}"
        )

        done, total = project_progress(state)
        ratio = (done / total) if total else 0.0
        color = usage_color(ratio) if total else cockpit_theme.TEXT_FAINT

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 6, 8, 6)
        self._row.setSpacing(8)

        self.chevron = QToolButton()
        self.chevron.setText("▸")
        self.chevron.setAutoRaise(True)
        self.chevron.setFixedSize(16, 16)
        self.chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chevron.setStyleSheet(
            "QToolButton {"
            f" color: {cockpit_theme.TEXT_MUTED}; background: transparent; border: none;"
            " font-size: 10px; font-weight: 700; }"
            f"QToolButton:hover {{ color: {cockpit_theme.TEXT_SECONDARY}; }}"
        )
        self.chevron.clicked.connect(lambda: item.setExpanded(not item.isExpanded()))
        self._row.addWidget(self.chevron)

        avatar = QLabel(_initials(project))
        avatar.setFixedSize(24, 24)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {_avatar_color(project)}; color: {cockpit_theme.TEXT_PRIMARY};"
            f" font-size: 10px; font-weight: 800; border-radius: 12px;"
        )
        self._row.addWidget(avatar)

        name = QLabel(project)
        name.setWordWrap(True)
        name.setStyleSheet(
            f"color: {cockpit_theme.TEXT_PRIMARY_ALT}; font-size: 13px; font-weight: 700;"
        )
        self._row.addWidget(name, 1)

        self._progress_container = QWidget()
        prog_lay = QHBoxLayout(self._progress_container)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        prog_lay.setSpacing(6)

        badge = QLabel(f"{done}/{total}")
        badge.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 700;")
        prog_lay.addWidget(badge)

        bar = QProgressBar()
        bar.setObjectName("taskMiniBar")
        bar.setMaximum(max(total, 1))
        bar.setValue(done)
        bar.setTextVisible(False)
        bar.setFixedWidth(48)
        bar.setFixedHeight(6)
        bar.setStyleSheet(
            "QProgressBar#taskMiniBar {"
            f" background: {cockpit_theme.GROUND_SELECT}; border: none; border-radius: 3px; }}"
            "QProgressBar#taskMiniBar::chunk {"
            f" background: {color}; border-radius: 3px; }}"
        )
        prog_lay.addWidget(bar)
        self._row.addWidget(self._progress_container)

        self._open_btn = QToolButton()
        self._open_btn.setText("↗")  # ↗
        self._open_btn.setToolTip(f"เปิด INDEX.md เต็ม ({project})")
        self._open_btn.setFixedSize(20, 20)
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.setStyleSheet(
            "QToolButton {"
            f" color: {cockpit_theme.TEXT_MUTED}; background: transparent; border: none;"
            f" border-radius: {cockpit_theme.RADIUS_SM}px; }}"
            f"QToolButton:hover {{ background: {cockpit_theme.GROUND_SELECT};"
            f" color: {cockpit_theme.TEXT_SECONDARY}; }}"
        )
        self._open_btn.clicked.connect(lambda: TaskDockWidget._open_index(project))
        self._row.addWidget(self._open_btn)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = event.size().width()

        is_narrow = width < self._NARROW_W
        self._progress_container.setVisible(not is_narrow)
        self._open_btn.setVisible(not is_narrow)
        if is_narrow:
            self._row.setContentsMargins(4, 4, 4, 4)
            self._row.setSpacing(4)
        else:
            self._row.setContentsMargins(8, 6, 8, 6)
            self._row.setSpacing(8)

        # Force the layout to recompute under the new width, then push the
        # resulting height into the owning QTreeWidgetItem's size hint so
        # QTreeWidget allocates enough row space for a wrapped name.
        self._row.activate()
        needed_height = self._row.sizeHint().height()
        current_hint = self.item.sizeHint(0)
        if current_hint.height() != needed_height:
            self.item.setSizeHint(0, QSize(0, needed_height))
            # Deferred: calling updateGeometries() synchronously from inside
            # resizeEvent can recurse (it may trigger another resize of this
            # same widget) — a single-shot timer lets this resize finish first.
            QTimer.singleShot(0, self.tree.updateGeometries)
