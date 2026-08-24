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
`editor_widget.read_head_blob` — `editor_widget.py` was frontend-owned and
off-limits when this module was first written; `find_rename_old_path` below
(#375) is instead imported directly by `editor_widget.build_diff_result`,
since its own diff-open path needed the same rename fallback and a second
copy of *that* logic wasn't worth carrying.
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ._win_console import SUBPROCESS_NO_WINDOW
from .project_file_index import SUBPROCESS_LOCK as _SUBPROCESS_LOCK
from .project_file_index import PathEscapesRootsError, _safe_resolve, resolve_and_contain

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 10.0
MAX_DIFF_FILE_BYTES = 2_000_000
_BINARY_SNIFF_BYTES = 8192

# Every `git` subprocess this module spawns goes through this lock (#375):
# `RepoDiscoveryService` added one more subprocess-calling QThreadPool
# worker per `ProjectExplorer`, and with several of those plus status/diff
# workers landing on the (global, or the dedicated one-thread discovery)
# pool at once, concurrent `subprocess.run()` calls from separate threads
# reproducibly crashed the interpreter with a Windows access violation
# inside `CreateProcess` (observed via faulthandler during `takkub
# qa-gate`, not test flakiness — see #349's note on unrelated native-abort
# noise for the different issue that isn't this). Imported as
# `project_file_index.SUBPROCESS_LOCK` (aliased here to the name every call
# site below already reads) so `_GitStatusWorker`'s own `subprocess.run()`
# there can never race one of these either — both are background-worker-only,
# never the Qt main thread, so holding it for a whole `git` call is fine.
#
# `Path.resolve()` turned out to be the *other* half of the same
# thread-safety issue (crashes observed inside `ntpath.realpath` too, not
# just `CreateProcess`) but it is guarded by a separate, always-fast lock —
# `project_file_index._safe_resolve` / `RESOLVE_LOCK` — because resolve()
# calls also happen on the Qt main thread (service constructors resolving
# configured roots) and must never wait behind a multi-second `git` call.


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    with _SUBPROCESS_LOCK:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )


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
    old_path: str | None = None  # "R" only — repo-relative source path
    repo_root: Path | None = None  # which repo this row belongs to (#375 multi-root)


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
            old_path = tokens[i] if i < n else None
            changes.append(FileChange(path=parts[9], status="R", old_path=old_path))
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
        proc = _run_git(
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=normal"], cwd=repo_root
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: status failed for %s: %s", repo_root, exc)
        return [], f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return [], f"git exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    # parse_status_v2 stays a pure parser (no repo context) — stamp the repo
    # this run was against onto every row here, once, so a multi-root
    # project's CHANGES panel can tell which repo a row belongs to (#375).
    changes = [dataclasses.replace(c, repo_root=repo_root) for c in parse_status_v2(proc.stdout)]
    return changes, None


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


# ── repo discovery (#375 GAP-009 — multi-root / repo-subdirectory roots) ───


def resolve_repo_root(path: Path) -> Path | None:
    """`git rev-parse --show-toplevel` for `path` — best-effort None on any
    failure (not inside a repo, git missing, timeout).

    Git always reports `status --porcelain` paths relative to this
    top-level, never to `cwd` (verified empirically: running `git status`
    from a subdirectory still returns top-level-relative paths) — so a
    configured project root that is itself a *subdirectory* of a repo must
    resolve to the repo's real top-level before it's used as
    `GitChangesService.repo_root`, or every changed-file path silently
    fails containment against that root and gets dropped.
    """
    try:
        proc = _run_git(["git", "rev-parse", "--show-toplevel"], cwd=path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: rev-parse toplevel failed for %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return _safe_resolve(Path(out)) if out else None


def discover_repo_roots(roots: Sequence[Path]) -> dict[Path, Path]:
    """Resolve each configured root to its git top-level (`{root: toplevel}`).
    A root outside any repo is simply omitted — the caller falls back to
    treating that root as its own standalone "repo", matching every other
    best-effort contract in this module. Blocking (subprocess per root) —
    always dispatched from `_RepoDiscoveryWorker`, never the Qt main thread.
    """
    result: dict[Path, Path] = {}
    for root in roots:
        resolved = _safe_resolve(Path(root))
        top = resolve_repo_root(resolved)
        if top is not None:
            result[resolved] = top
    return result


# ── diff against HEAD ────────────────────────────────────────────────────


def read_head_blob(repo_root: Path, rel_posix: str) -> str | None:
    """`git show HEAD:<relpath>` — best-effort None on any failure (new/
    untracked file, no HEAD yet, not a repo, git missing, timeout)."""
    try:
        proc = _run_git(["git", "show", f"HEAD:{rel_posix}"], cwd=repo_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: show HEAD:%s failed: %s", rel_posix, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def find_rename_old_path(repo_root: Path, rel_new_path: str) -> str | None:
    """Best-effort: if `rel_new_path` is currently a pending rename (staged
    or unstaged), return the source path git status reports it moved from.

    `read_head_blob(repo_root, rel_new_path)` alone can't tell a renamed
    file from a genuinely new one — both come back None, since HEAD has no
    blob under the new name either way — so `diff_sync` only calls this as
    a fallback once that first lookup has already failed.

    Deliberately *not* scoped to `-- rel_new_path`: git's rename detection
    needs to see both the old and new side of the diff to pair them up —
    scope the pathspec to just the new path and git can no longer see the
    old side was removed too, so it reports a plain "A" (add) instead of a
    rename (verified empirically). A full, unscoped status is the only way
    to get the pairing.
    """
    try:
        proc = _run_git(
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=normal"], cwd=repo_root
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("git_changes_service: rename lookup for %s failed: %s", rel_new_path, exc)
        return None
    if proc.returncode != 0:
        return None
    for change in parse_status_v2(proc.stdout):
        if change.status == "R" and change.path == rel_new_path:
            return change.old_path
    return None


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

    Rules per status (never stats/opens a file that isn't there):
      M: HEAD:path -> current            A: (empty) -> current
      D: HEAD:path -> (empty), current never read (`_read_current_text`
         reports a missing file as `(None, None)` — valid "no content", not
         an error)
      R: HEAD:old_path -> current — `read_head_blob` on the *new* path
         always misses (HEAD has no blob there yet), so a rename falls back
         to `find_rename_old_path` to locate the source path first.
    """
    try:
        resolved = resolve_and_contain(Path(abs_path), roots)
    except PathEscapesRootsError as exc:
        return DiffResult(path=Path(abs_path), unified=None, error=str(exc))
    try:
        rel = resolved.relative_to(_safe_resolve(Path(repo_root))).as_posix()
    except ValueError as exc:
        return DiffResult(path=resolved, unified=None, error=str(exc))

    modified, err = _read_current_text(resolved, max_bytes)
    if err:
        return DiffResult(path=resolved, unified=None, error=err)

    original = read_head_blob(repo_root, rel)
    if original is None and modified is not None:
        old_rel = find_rename_old_path(repo_root, rel)
        if old_rel is not None:
            original = read_head_blob(repo_root, old_rel)
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


# A dedicated, single-worker pool for repo discovery — deliberately not
# `QThreadPool.globalInstance()`, which status/diff workers already share.
# `_RepoDiscoveryWorker.run()` calls both `subprocess.run()` (rate-limited
# by `_SUBPROCESS_LOCK` above) and `Path.resolve()` per configured root;
# several of those in flight at once across different `ProjectExplorer`
# instances reproducibly crashed the interpreter with a Windows access
# violation — observed via faulthandler inside both
# `subprocess._execute_child` and `ntpath.realpath` (`Path.resolve()`),
# i.e. concurrent calls into either from multiple threads is not safe on
# this platform/Python combination, not just a subprocess-specific issue.
# Capping this pool to one worker serializes every discovery run app-wide;
# a `git rev-parse` per root is fast enough that this has no user-visible
# effect.
_REPO_DISCOVERY_POOL = QThreadPool()
_REPO_DISCOVERY_POOL.setMaxThreadCount(1)


class _RepoDiscoverySignals(QObject):
    finished = pyqtSignal(dict)  # {resolved_root: toplevel}


class _RepoDiscoveryWorker(QRunnable):
    def __init__(self, roots: Sequence[Path]) -> None:
        super().__init__()
        self.roots = list(roots)
        self.signals = _RepoDiscoverySignals()

    def run(self) -> None:  # called by _REPO_DISCOVERY_POOL, never QThreadPool.globalInstance()
        result = discover_repo_roots(self.roots)
        _safe_emit(self.signals.finished, result)


class RepoDiscoveryService(QObject):
    """One-shot, background `git rev-parse --show-toplevel` per configured
    root (#375 GAP-009). `ProjectExplorer` uses `discovered`'s result to
    group its configured roots into distinct repos — and to correct a root
    that turns out to be a subdirectory of a repo rather than its
    top-level — before creating one `GitChangesService` per distinct repo.
    Not debounced like `GitChangesService`: `discover()` is meant to run
    once (`ProjectExplorer` guards repeat calls with `_repo_discovery_started`
    so it fires on the first `refresh_changes()`, not eagerly at
    construction — see that method's docstring), not on every refresh."""

    discovered = pyqtSignal(dict)  # {resolved_root: toplevel}

    def __init__(self, roots: Sequence[Path], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.roots = [_safe_resolve(Path(r)) for r in roots]

    def discover(self) -> None:
        worker = _RepoDiscoveryWorker(self.roots)
        # #375 regression: this queued cross-thread connection must target a
        # plain bound method of `self` (a QObject), never `self.discovered.emit`
        # directly — a bound signal's `.emit` isn't the QObject itself, so
        # PyQt's auto-disconnect-on-destroy can't tie the connection to
        # `self`'s lifetime. When `self` (and its owning `ProjectExplorer`)
        # was destroyed while this worker was still running — e.g. a test
        # widget falling out of scope right after dispatching `discover()` —
        # the stale connection stayed live and later delivery of the queued
        # signal into the by-then-dead receiver crashed the interpreter with
        # a Windows access violation (reproduced via faulthandler; every
        # other worker in this file and project_file_index.py already
        # connects to a plain method for this reason, `discover()` was the
        # one exception).
        worker.signals.finished.connect(self._on_discovered)
        _REPO_DISCOVERY_POOL.start(worker)

    def _on_discovered(self, mapping: dict) -> None:
        self.discovered.emit(mapping)


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
        self.roots = [_safe_resolve(Path(r)) for r in roots]
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
