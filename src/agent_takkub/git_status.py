"""Read-only git status snapshot per project — engine for the Task dock Git panel.

Pure I/O module (no PyQt import) so it is unit-testable without a Qt event
loop. Every public function is a plain read against real git via
``subprocess`` and NEVER raises — a missing git binary, a non-repo path, or a
timeout all surface as :attr:`RepoStatus.error` instead of an exception, so
the UI can render an error row rather than crash a 30s refresh timer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from ._win_console import SUBPROCESS_NO_WINDOW

_UNIT_SEP = "\x1f"


@dataclass(frozen=True)
class Commit:
    sha: str  # 7-char short
    subject: str
    author: str
    rel_time: str  # "3 hours ago"


@dataclass(frozen=True)
class Worktree:
    branch: str  # "wt/backend-1785141771"
    path: str
    commits_ahead: int  # vs base branch (main/master)
    dirty: bool


@dataclass(frozen=True)
class RepoStatus:
    key: str  # path key from projects.json ("web"/"api"/"main"/"root")
    path: str
    branch: str  # detached -> "(detached @ abc1234)"
    upstream: str | None
    ahead: int
    behind: int
    staged: int
    modified: int
    untracked: int
    commits: list[Commit] = field(default_factory=list)
    worktrees: list[Worktree] = field(default_factory=list)
    error: str | None = None


def _run_git(args: list[str], cwd: str, timeout_s: float) -> str | None:
    """Run ``git <args>`` in *cwd*; return stdout, or None on any failure.

    Never raises — a missing git binary, a bad/vanished cwd, a non-zero exit,
    or a timeout are all folded into the same None result so every caller has
    one branch to handle instead of catching exceptions everywhere.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_toplevel(path: str, timeout_s: float) -> str | None:
    out = _run_git(["rev-parse", "--show-toplevel"], path, timeout_s)
    if out is None:
        return None
    top = out.strip()
    return top or None


def _base_branch(top: str, timeout_s: float) -> str:
    """ "main" if it exists, else "master" (falls back to "main" either way
    when neither ref resolves — commits_ahead just reads 0)."""
    for candidate in ("main", "master"):
        if _run_git(["rev-parse", "--verify", "--quiet", candidate], top, timeout_s) is not None:
            return candidate
    return "main"


def _parse_status_v2(output: str) -> tuple[str, str | None, int, int, int, int, int]:
    """Parse ``git status --porcelain=v2 --branch`` output.

    Returns (branch, upstream, ahead, behind, staged, modified, untracked).
    """
    branch = ""
    detached = False
    oid = ""
    upstream: str | None = None
    ahead = 0
    behind = 0
    staged = 0
    modified = 0
    untracked = 0
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("# branch.oid "):
            oid = line[len("# branch.oid ") :].strip()
        elif line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            if head == "(detached)":
                detached = True
            else:
                branch = head
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab ") :].strip().split():
                if token.startswith("+"):
                    ahead = int(token[1:] or 0)
                elif token.startswith("-"):
                    behind = int(token[1:] or 0)
        elif line[0] in ("1", "2", "u"):
            fields = line.split(" ")
            xy = fields[1] if len(fields) > 1 else ".."
            x, y = (xy + "..")[:2]
            if x != ".":
                staged += 1
            if y != ".":
                modified += 1
        elif line.startswith("?"):
            untracked += 1
        # '!' (ignored) entries are intentionally skipped.
    if detached:
        branch = f"(detached @ {oid[:7] if oid else '?'})"
    elif not branch:
        branch = oid[:7] if oid else ""
    return branch, upstream, ahead, behind, staged, modified, untracked


def _collect_commits(top: str, limit: int, timeout_s: float) -> list[Commit]:
    out = _run_git(
        ["log", f"-n{max(limit, 0)}", f"--format=%h{_UNIT_SEP}%s{_UNIT_SEP}%an{_UNIT_SEP}%ar"],
        top,
        timeout_s,
    )
    if out is None:
        return []
    commits: list[Commit] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(_UNIT_SEP)
        if len(parts) != 4:
            continue
        sha, subject, author, rel_time = parts
        commits.append(Commit(sha=sha, subject=subject, author=author, rel_time=rel_time))
    return commits


def _collect_worktrees(top: str, timeout_s: float) -> list[Worktree]:
    out = _run_git(["worktree", "list", "--porcelain"], top, timeout_s)
    if out is None:
        return []
    base = _base_branch(top, timeout_s)
    worktrees: list[Worktree] = []
    cur: dict[str, str] = {}

    def _flush() -> None:
        branch = cur.get("branch", "")
        if not branch.startswith("wt/"):
            return
        wt_path = cur.get("path", "")
        ahead_out = _run_git(["rev-list", "--count", f"{base}..{branch}"], top, timeout_s)
        try:
            ahead = int((ahead_out or "0").strip() or "0")
        except ValueError:
            ahead = 0
        dirty_out = _run_git(["status", "--porcelain"], wt_path, timeout_s)
        worktrees.append(
            Worktree(
                branch=branch,
                path=wt_path,
                commits_ahead=ahead,
                dirty=bool((dirty_out or "").strip()),
            )
        )

    for raw_line in out.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            _flush()
            cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            cur["branch"] = ref.removeprefix("refs/heads/")
    _flush()
    return worktrees


def collect_repo(path: str, key: str = "", limit: int = 8, timeout_s: float = 5.0) -> RepoStatus:
    """Full git status snapshot for the repo containing *path*.

    Never raises: a non-repo path, a missing git binary, or a timeout all
    come back as a RepoStatus with ``error`` set instead of empty fields.
    """
    top = _git_toplevel(path, timeout_s)
    if top is None:
        return RepoStatus(
            key=key or path,
            path=path,
            branch="",
            upstream=None,
            ahead=0,
            behind=0,
            staged=0,
            modified=0,
            untracked=0,
            error="ไม่ใช่ git repo หรือ git ใช้งานไม่ได้",
        )
    status_out = _run_git(["status", "--porcelain=v2", "--branch"], top, timeout_s)
    if status_out is None:
        return RepoStatus(
            key=key or top,
            path=top,
            branch="",
            upstream=None,
            ahead=0,
            behind=0,
            staged=0,
            modified=0,
            untracked=0,
            error="git status ล้มเหลวหรือ timeout",
        )
    branch, upstream, ahead, behind, staged, modified, untracked = _parse_status_v2(status_out)
    return RepoStatus(
        key=key or top,
        path=top,
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        staged=staged,
        modified=modified,
        untracked=untracked,
        commits=_collect_commits(top, limit, timeout_s),
        worktrees=_collect_worktrees(top, timeout_s),
        error=None,
    )


def project_repo_paths(project: str) -> list[tuple[str, str]]:
    """Effective (key, path) pairs for *project*, deduped by real repo.

    Multiple path keys can point at the same repo (a monorepo where
    "root"/"api"/"web" all live under one ``.git``, or one key nested inside
    another). Each configured path is resolved to its git toplevel; entries
    that share a toplevel collapse into a single row, keeping whichever key's
    path sits closest to (or exactly at) that toplevel. Paths that aren't a
    git repo at all are kept as-is (deduped by resolved filesystem path
    instead) — ``collect_repo`` reports them with ``error`` set rather than
    silently dropping them.
    """
    data = config.load_projects()
    proj = data.get("projects", {}).get(project, {}) or {}
    paths = proj.get("paths") or {}

    groups: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for key, raw_path in paths.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        abspath = str(Path(raw_path).expanduser())
        dedupe_key = _git_toplevel(abspath, 5.0)
        if dedupe_key is None:
            try:
                dedupe_key = str(Path(abspath).resolve())
            except OSError:
                dedupe_key = abspath
        if dedupe_key not in groups:
            groups[dedupe_key] = []
            order.append(dedupe_key)
        groups[dedupe_key].append((key, abspath))

    def _closeness(entry: tuple[str, str], top: str) -> tuple[int, str]:
        entry_key, entry_path = entry
        try:
            rel_parts = len(Path(entry_path).resolve().relative_to(Path(top).resolve()).parts)
        except (ValueError, OSError):
            rel_parts = 999
        return (rel_parts, entry_key)

    out: list[tuple[str, str]] = []
    for dedupe_key in order:
        entries = groups[dedupe_key]
        best_key, _ = min(entries, key=lambda e: _closeness(e, dedupe_key))
        out.append((best_key, dedupe_key))
    return out


def collect(project: str, limit: int = 8, timeout_s: float = 5.0) -> list[RepoStatus]:
    """RepoStatus for every deduped repo path configured for *project*."""
    return [
        collect_repo(path, key=key, limit=limit, timeout_s=timeout_s)
        for key, path in project_repo_paths(project)
    ]
