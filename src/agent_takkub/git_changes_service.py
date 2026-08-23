"""Background git changes + per-file diff for the Explorer's CHANGES panel
(#365 phase 4).

Baseline policy (05_GIT_DIFF_AND_AGENT_CHANGES.md): exactly one baseline for
V1 — every diff is against HEAD, no staged/unstaged distinction. Never guess
agent attribution from git alone (`FileChange` carries only path + status).

Same QRunnable + sibling-QObject-signals + debounce-timer shape as
`project_file_index.py`'s `GitStatusService` (which stays as-is for its
existing porcelain-v1 M/A/D badge use); this module is the phase-4
superset — porcelain v2 (proper rename detection) plus the diff endpoint —
so status/diff subprocess calls never run on the Qt main thread
(13_PERFORMANCE_AND_QT_RULES.md rules 1/2/8/9).

`read_head_blob` here is a small, deliberate duplicate of
`editor_widget.read_head_blob` — `editor_widget.py` is frontend-owned and
off-limits for this task; a follow-up frontend PR can point it at this copy
instead of carrying two.
"""

from __future__ import annotations

import difflib
import logging
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ._win_console import SUBPROCESS_NO_WINDOW
from .project_file_index import PathEscapesRootsError, resolve_and_contain

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 10.0
MAX_DIFF_FILE_BYTES = 2_000_000
_BINARY_SNIFF_BYTES = 8192


def _safe_emit(signal, *args) -> None:
    """Tolerate a receiver QObject torn down mid-flight — same guard as
    project_file_index.py's `_safe_emit`."""
    try:
        signal.emit(*args)
    except RuntimeError:
        logger.debug("git_changes_service: receiver deleted before emit — dropping result")


@dataclass(frozen=True)
class FileChange:
    path: str  # repo-relative, posix separators (as git reports it)
    status: str  # "M" | "A" | "D" | "R"


@dataclass(frozen=True)
class DiffResult:
    path: Path
    unified: str | None
    error: str | None  # "binary" | "too_large" | "no_content" | escape/OS message


# ── porcelain v2 parsing (pure — no I/O) ────────────────────────────────────


def _status_letter(xy: str) -> str:
    if "D" in xy:
        return "D"
    if "A" in xy:
        return "A"
    return "M"


def parse_status_v2(output: str) -> list[FileChange]:
    """Parse `git status --porcelain=v2 -z` output.

    Record shapes (see git-status(1) "Porcelain Format Version 2"):
      ``1 XY sub mH mI mW hH hI <path>``               — ordinary change
      ``2 XY sub mH mI mW hH hI Xscore <path>\\0<orig>`` — rename/copy
      ``u XY sub m1 m2 m3 mW h1 h2 h3 <path>``          — unmerged
      ``? <path>``                                      — untracked
      ``! <path>``                                      — ignored (dropped)

    With ``-z`` there's no C-style quoting, so a Thai/Unicode/space-
    containing path round-trips as raw UTF-8 with no unescaping needed —
    the whole reason to prefer ``-z`` over the line-based short format.
    """
    changes: list[FileChange] = []
    if not output:
        return changes
    tokens = output.split("\0")
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        kind = tok[0]
        if kind == "1":
            parts = tok.split(" ", 8)
            if len(parts) < 9:
                continue
            changes.append(FileChange(path=parts[8], status=_status_letter(parts[1])))
        elif kind == "2":
            parts = tok.split(" ", 9)
            if len(parts) < 10:
                continue
            changes.append(FileChange(path=parts[9], status="R"))
            i += 1  # consume the paired <origPath> token (already NUL-split)
        elif kind == "u":
            parts = tok.split(" ", 10)
            if len(parts) < 11:
                continue
            changes.append(FileChange(path=parts[10], status="M"))
        elif kind == "?":
            path = tok[2:] if len(tok) > 2 else ""
            if path:
                changes.append(FileChange(path=path, status="A"))
        # "!" (ignored) intentionally dropped — not part of the Changes panel.
    return changes


def _changes_sync_diag(repo_root: Path) -> tuple[list[FileChange], str | None]:
    """Same subprocess call `changes_sync` makes, plus the error text
    `changes_sync`'s best-effort contract otherwise swallows — used by
    `_StatusWorker` so `GitChangesService.diagnostics()` (#365 phase 10,
    13_PERFORMANCE_AND_QT_RULES.md rule 10) can report "last run ms +
    error" without a second subprocess call. `changes_sync` itself keeps
    its existing "always a list, never raises" contract."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=normal"],
            cwd=str(repo_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: status failed for %s: %s", repo_root, exc)
        return [], f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return [], f"git exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    return parse_status_v2(proc.stdout), None


def changes_sync(repo_root: Path) -> list[FileChange]:
    """Blocking `git status --porcelain=v2 -z`. Best-effort — a non-repo,
    missing git, or timeout all yield an empty list rather than raising,
    matching GitStatusService's own contract.

    `encoding="utf-8"` is explicit, not `text=True`'s locale default — git
    writes path bytes as UTF-8 regardless of the OS locale, and on a
    non-UTF-8 Windows locale (e.g. Thai cp874) the platform default would
    raise `UnicodeDecodeError` on a Unicode filename instead of decoding it.
    """
    return _changes_sync_diag(repo_root)[0]


# ── diff against HEAD ────────────────────────────────────────────────────


def read_head_blob(repo_root: Path, rel_posix: str) -> str | None:
    """`git show HEAD:<relpath>` — best-effort None on any failure (new/
    untracked file, no HEAD yet, not a repo, git missing, timeout)."""
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel_posix}"],
            cwd=str(repo_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: show HEAD:%s failed: %s", rel_posix, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _read_current_text(resolved: Path, max_bytes: int) -> tuple[str | None, str | None]:
    """`(text, error)`. A missing file (deleted) is `(None, None)` — valid
    "no current content", not an error; binary/too-large set `error`."""
    if not resolved.exists():
        return None, None
    size = resolved.stat().st_size
    if size > max_bytes:
        return None, "too_large"
    with resolved.open("rb") as fh:
        sample = fh.read(_BINARY_SNIFF_BYTES)
    if _looks_binary(sample):
        return None, "binary"
    return resolved.read_text(encoding="utf-8", errors="replace"), None


def diff_sync(
    repo_root: Path,
    roots: Sequence[Path],
    abs_path: Path,
    max_bytes: int = MAX_DIFF_FILE_BYTES,
) -> DiffResult:
    """Unified diff of `abs_path`'s current content against its HEAD blob.
    A new/untracked file (`original=None`) or a deleted one (`modified=None`)
    both fall straight out of the same `difflib.unified_diff` call."""
    try:
        resolved = resolve_and_contain(Path(abs_path), roots)
    except PathEscapesRootsError as exc:
        return DiffResult(path=Path(abs_path), unified=None, error=str(exc))
    try:
        rel = resolved.relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError as exc:
        return DiffResult(path=resolved, unified=None, error=str(exc))

    modified, err = _read_current_text(resolved, max_bytes)
    if err:
        return DiffResult(path=resolved, unified=None, error=err)

    original = read_head_blob(repo_root, rel)
    if original is None and modified is None:
        return DiffResult(path=resolved, unified=None, error="no_content")

    diff_lines = difflib.unified_diff(
        (original or "").splitlines(keepends=True),
        (modified or "").splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    return DiffResult(path=resolved, unified="".join(diff_lines), error=None)


# ── QThreadPool workers ──────────────────────────────────────────────────


class _StatusSignals(QObject):
    finished = pyqtSignal(list)  # list[FileChange]
    # #365 phase 10 diagnostics: (elapsed_ms, error_or_empty) — timed inside
    # the worker thread; the connected slot only stores two values.
    timed = pyqtSignal(float, str)


class _StatusWorker(QRunnable):
    def __init__(self, repo_root: Path) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.signals = _StatusSignals()

    def run(self) -> None:  # called by QThreadPool
        t0 = time.perf_counter()
        result, error = _changes_sync_diag(self.repo_root)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _safe_emit(self.signals.finished, result)
        _safe_emit(self.signals.timed, elapsed_ms, error or "")


class _DiffSignals(QObject):
    finished = pyqtSignal(object)  # DiffResult
    timed = pyqtSignal(float)  # elapsed_ms — error already lives on DiffResult.error


class _DiffWorker(QRunnable):
    def __init__(
        self, repo_root: Path, roots: Sequence[Path], abs_path: Path, max_bytes: int
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.roots = list(roots)
        self.abs_path = abs_path
        self.max_bytes = max_bytes
        self.signals = _DiffSignals()

    def run(self) -> None:  # called by QThreadPool
        t0 = time.perf_counter()
        result = diff_sync(self.repo_root, self.roots, self.abs_path, self.max_bytes)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _safe_emit(self.signals.finished, result)
        _safe_emit(self.signals.timed, elapsed_ms)


# ── GitChangesService — the app-facing QObject ───────────────────────────


class GitChangesService(QObject):
    """Debounced `git status --porcelain=v2` for one project root, plus a
    direct (non-debounced — a diff is one explicit click, not a repeated
    fire hose) per-file HEAD diff. Mirrors GitStatusService's debounce
    shape: only the last `request_refresh()` within `debounce_ms` runs."""

    changesChanged = pyqtSignal(list)  # list[FileChange]
    diffReady = pyqtSignal(object)  # DiffResult

    def __init__(
        self,
        repo_root: Path,
        roots: Sequence[Path],
        *,
        debounce_ms: int = 1500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.repo_root = Path(repo_root)
        self.roots = [Path(r).resolve() for r in roots]
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._run)
        # #365 phase 10 diagnostics (13_PERFORMANCE_AND_QT_RULES.md rule
        # 10, "git_changes service last run ms + error") — last completed
        # run only, not a history.
        self._last_status_ms: float | None = None
        self._last_status_error: str | None = None
        self._last_diff_ms: float | None = None
        self._last_diff_error: str | None = None
        self._status_run_count: int = 0
        self._diff_run_count: int = 0

    def request_refresh(self) -> None:
        self._timer.start()

    def _run(self) -> None:
        worker = _StatusWorker(self.repo_root)
        worker.signals.finished.connect(self._on_status_finished)
        worker.signals.timed.connect(self._on_status_timed)
        QThreadPool.globalInstance().start(worker)

    def _on_status_finished(self, result: list) -> None:
        self.changesChanged.emit(result)

    def _on_status_timed(self, elapsed_ms: float, error: str) -> None:
        self._last_status_ms = elapsed_ms
        self._last_status_error = error or None
        self._status_run_count += 1

    def request_diff(self, abs_path: Path) -> None:
        worker = _DiffWorker(self.repo_root, self.roots, Path(abs_path), MAX_DIFF_FILE_BYTES)
        worker.signals.finished.connect(self._on_diff_finished)
        worker.signals.timed.connect(self._on_diff_timed)
        QThreadPool.globalInstance().start(worker)

    def _on_diff_finished(self, result: DiffResult) -> None:
        self._last_diff_error = result.error
        self.diffReady.emit(result)

    def _on_diff_timed(self, elapsed_ms: float) -> None:
        self._last_diff_ms = elapsed_ms
        self._diff_run_count += 1

    def diagnostics(self) -> dict:
        """`takkub doctor --workspace`'s "git_changes service last run ms +
        error" row. Pure attribute reads, no subprocess call triggered."""
        return {
            "last_status_ms": self._last_status_ms,
            "last_status_error": self._last_status_error,
            "status_run_count": self._status_run_count,
            "last_diff_ms": self._last_diff_ms,
            "last_diff_error": self._last_diff_error,
            "diff_run_count": self._diff_run_count,
            "debounce_pending": self._timer.isActive(),
        }
