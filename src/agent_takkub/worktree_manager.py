"""Per-pane git worktree isolation — issue #81, Tier 3 MVP (Phase 1).

Build-only isolation: an opt-in ``--isolation worktree`` pane runs in its own
git worktree + branch so parallel feature builds don't race on the shared
working tree (commit race / mid-QA HMR recompile). NO dev-server is started in
the worktree — that is what triggers the two heaviest blind spots
(node_modules propagation + Windows file locks on running compilers), so Phase
1 deliberately keeps the worktree *build-only*: the pane edits + commits on its
own branch, and QA still runs in the **main tree** after the Lead merges the
branch. Merge is always a PROPOSAL to the Lead — never automatic.

Design cross-checked against ``AgentWrapper/agent-orchestrator``'s
``gitworktree`` adapter (the closest peer that ships this). Adopted from it:

* **2-tier destroy** — :meth:`WorktreeManager.safe_remove` runs
  ``git worktree remove`` *without* ``--force`` and refuses (rather than
  force-deletes) a worktree that still holds uncommitted work, surfacing that
  as a dirty refusal. :meth:`WorktreeManager.force_remove` is the explicit
  unconditional teardown. Default path never loses an agent's work.
* **Path safety** — the managed worktree root is resolved absolute + real, and
  every destination is verified to live *under* it (:class:`UnsafePathError`)
  so a crafted role/project name can't escape into an arbitrary directory.
* **Branch-checked-out awareness** — ``git worktree add`` failures (e.g. a
  branch already checked out elsewhere) are returned as a reason string so the
  caller can fall back to the shared cwd + warn, never crash.

Cross-platform: all paths are built with :mod:`pathlib` (no ``\\`` / ``.exe``
literals) and git is invoked through a small injectable runner so the whole
lifecycle is unit-tested on both OS without a real repository.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_HOME

# git ops here are all local (no network) and fast; bound them so a wedged git
# can never freeze the caller (the orchestrator runs create/finalize on the Qt
# main thread — see orchestrator._assign_with_worktree, which additionally moves
# the slow `worktree add` off-thread via QProcess).
_GIT_TIMEOUT_S = 30

# Branch/dir prefix so isolated worktrees are unmistakable in `git worktree
# list`, `git branch`, and the pane title chip.
_BRANCH_PREFIX = "wt"


@dataclass(frozen=True)
class WorktreeInfo:
    """Everything needed to finalize (diff / merge-propose / remove) a worktree.

    Serialisable to a plain dict (:meth:`as_dict` / :meth:`from_dict`) so it can
    ride along in ``PaneState.worktree`` and survive the atomic pop in
    ``done()`` / ``close()``.
    """

    path: str  # absolute worktree checkout dir
    branch: str  # e.g. "wt/frontend-1-1720000000"
    base_sha: str  # HEAD sha at creation — the merge base for diff/rev-list
    git_root: str  # toplevel of the source repo the worktree belongs to

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "git_root": self.git_root,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorktreeInfo:
        return cls(
            path=d["path"],
            branch=d["branch"],
            base_sha=d.get("base_sha", ""),
            git_root=d["git_root"],
        )


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class UnsafePathError(Exception):
    """A resolved worktree destination escaped the managed root."""


# ── Env-propagation config (Phase 2 — P2.1) ────────────────────────────────
#
# Opt-in, per project: `<git root>/.takkub/worktree.json` declares what an
# isolated worktree needs before it is buildable. Absent / invalid file =
# Phase-1 behavior (bare worktree). Blueprint: agent-orchestrator's workspaces
# plugin (`symlinks:` + `postCreate:`), mined 2026-07-04 — see issue #81.
#
#   {
#     "symlinks":   [".env.local", "node_modules"],   // linked FROM the main tree
#     "postCreate": ["pnpm install --prefer-offline"], // run in the new worktree
#     "base_port":  5310                               // dev-server port pool base
#   }

_WORKTREE_CONFIG_RELPATH = Path(".takkub") / "worktree.json"

# Guardrails on config values so a malformed/hostile file can't turn the link
# step into an arbitrary-path primitive: entries must be RELATIVE paths inside
# the repo (no absolute, no drive letter, no parent traversal).
_MAX_SYMLINKS = 16
_MAX_POST_CREATE = 8


@dataclass(frozen=True)
class WorktreeConfig:
    """Validated env-propagation settings for one project's worktrees."""

    symlinks: tuple[str, ...] = ()
    post_create: tuple[str, ...] = ()
    base_port: int = 0  # 0 = no dev-server port allocation

    @property
    def is_empty(self) -> bool:
        return not (self.symlinks or self.post_create or self.base_port)


def _safe_rel_entry(entry: object) -> str | None:
    """Return the entry as a validated repo-relative path string, else None."""
    if not isinstance(entry, str) or not entry.strip():
        return None
    rel = entry.strip().replace("\\", "/")
    p = Path(rel)
    # p.root catches "/abs" too — on Windows Path("/abs").is_absolute() is
    # False (no drive), but it still escapes the repo when joined.
    if p.is_absolute() or p.drive or p.root or ".." in p.parts:
        return None
    return rel


def load_worktree_config(git_root: str) -> tuple[WorktreeConfig, str]:
    """Load + validate `<git_root>/.takkub/worktree.json`.

    Returns ``(config, "")`` — an empty config when the file is absent — or
    ``(empty, warning)`` when the file exists but is malformed, so the caller
    can tell the Lead the config was ignored rather than silently dropping it.
    Never raises.
    """
    path = Path(git_root) / _WORKTREE_CONFIG_RELPATH
    try:
        if not path.is_file():
            return WorktreeConfig(), ""
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return WorktreeConfig(), f"worktree.json อ่านไม่ได้ ({exc}) — ข้าม env propagation"
    if not isinstance(raw, dict):
        return WorktreeConfig(), "worktree.json ต้องเป็น JSON object — ข้าม env propagation"

    warnings: list[str] = []
    links: list[str] = []
    for entry in (raw.get("symlinks") or [])[:_MAX_SYMLINKS]:
        rel = _safe_rel_entry(entry)
        if rel is None:
            warnings.append(f"symlinks entry ไม่ปลอดภัย/ไม่ใช่ relative path: {entry!r}")
        else:
            links.append(rel)

    cmds: list[str] = []
    for cmd in (raw.get("postCreate") or [])[:_MAX_POST_CREATE]:
        if isinstance(cmd, str) and cmd.strip():
            cmds.append(cmd.strip())
        else:
            warnings.append(f"postCreate entry ต้องเป็น string: {cmd!r}")

    base_port = raw.get("base_port", 0)
    if not isinstance(base_port, int) or not (0 == base_port or 1024 <= base_port <= 65000):
        warnings.append(f"base_port ต้องเป็น int ช่วง 1024-65000: {base_port!r}")
        base_port = 0

    cfg = WorktreeConfig(symlinks=tuple(links), post_create=tuple(cmds), base_port=base_port)
    return cfg, "; ".join(warnings)


# A runner maps (args, cwd) -> GitResult. Injectable so tests never shell out.
GitRunner = Callable[[list[str], "str | None"], GitResult]


def _default_runner(args: list[str], cwd: str | None) -> GitResult:
    """Real git via subprocess, bounded by ``_GIT_TIMEOUT_S``.

    Never raises on a non-zero exit or a timeout — returns a GitResult so the
    caller's fallback logic (shared cwd + warn) stays branch-based, not
    exception-based.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        return GitResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"git timed out after {_GIT_TIMEOUT_S}s")
    except (OSError, ValueError) as exc:  # git missing, bad cwd, etc.
        return GitResult(127, "", str(exc))


# ── Pure helpers (no I/O — unit-tested directly) ────────────────────────────


def sanitize_ref_component(name: str) -> str:
    """Turn a role/project label into a git-ref-safe, filesystem-safe slug.

    ``qa#1`` -> ``qa-1``; strips anything outside ``[A-Za-z0-9._-]`` and
    collapses runs so the branch/dir name can't smuggle path separators or
    git refspec metacharacters.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing '-' and '.' only (a git ref can't start/end with a
    # dot or contain '..'); underscores are legal anywhere so they're kept.
    slug = slug.strip("-.").replace("..", ".")
    return slug or "pane"


def branch_name(role: str, ts: int) -> str:
    """Deterministic isolated-branch name. ``ts`` is passed in (never sampled
    here) so the value is reproducible and the module stays side-effect-free."""
    return f"{_BRANCH_PREFIX}/{sanitize_ref_component(role)}-{ts}"


def worktree_root(project_ns: str) -> Path:
    """Managed root that holds every worktree for a project, OUTSIDE the repo
    working tree (so dev-server file watchers / git status of the main tree
    never see it): ``<DATA_HOME>/worktrees/<project>``."""
    return (DATA_HOME / "worktrees" / sanitize_ref_component(project_ns)).resolve()


def worktree_dest(project_ns: str, role: str, ts: int) -> Path:
    """Absolute checkout dir for one isolated pane, guaranteed under the
    managed root (raises :class:`UnsafePathError` otherwise)."""
    root = worktree_root(project_ns)
    dest = (root / f"{sanitize_ref_component(role)}-{ts}").resolve()
    if root != dest and root not in dest.parents:
        raise UnsafePathError(f"worktree dest {dest} escapes managed root {root}")
    return dest


class WorktreeManager:
    """Stateless lifecycle wrapper around ``git worktree`` for one repo.

    Holds only an injectable :data:`GitRunner`; all per-worktree state lives in
    the :class:`WorktreeInfo` value objects the orchestrator threads through
    ``PaneState``.
    """

    def __init__(self, runner: GitRunner | None = None) -> None:
        self._run: GitRunner = runner or _default_runner

    # -- discovery ----------------------------------------------------------

    def git_root(self, cwd: str) -> str | None:
        """Absolute toplevel of the repo containing *cwd*, or None when *cwd*
        is not inside a git work tree (caller then falls back to shared cwd)."""
        res = self._run(["-C", cwd, "rev-parse", "--show-toplevel"], None)
        if not res.ok:
            return None
        top = res.stdout.strip()
        return top or None

    def head_sha(self, cwd: str) -> str | None:
        res = self._run(["-C", cwd, "rev-parse", "HEAD"], None)
        return res.stdout.strip() if res.ok and res.stdout.strip() else None

    # -- create -------------------------------------------------------------

    def create(
        self, base_cwd: str, project_ns: str, role: str, ts: int
    ) -> tuple[WorktreeInfo | None, str]:
        """Create an isolated worktree+branch off *base_cwd*'s HEAD.

        Returns ``(info, "")`` on success or ``(None, reason)`` when the pane
        must fall back to the shared cwd — *reason* is a short human string for
        the Lead warning. This method performs the fast preflight (rev-parse);
        the slow ``worktree add`` checkout is the last step (the orchestrator
        may run it off the main thread — see its QProcess wrapper — but the
        pure-synchronous path here is what the unit tests exercise).
        """
        root = self.git_root(base_cwd)
        if root is None:
            return None, "ไม่ใช่ git repo (worktree isolation ต้องมี .git) — ใช้ shared cwd แทน"
        base_sha = self.head_sha(base_cwd)
        if not base_sha:
            return None, "repo ยังไม่มี commit (HEAD ว่าง) — ใช้ shared cwd แทน"
        try:
            dest = worktree_dest(project_ns, role, ts)
        except UnsafePathError as exc:
            return None, f"path ไม่ปลอดภัย: {exc} — ใช้ shared cwd แทน"
        branch = branch_name(role, ts)
        # Ensure the managed root exists; the dest itself must NOT pre-exist
        # (git refuses "working tree already exists").
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, f"สร้าง worktree root ไม่ได้: {exc} — ใช้ shared cwd แทน"
        add = self._run(
            ["-C", root, "worktree", "add", str(dest), "-b", branch, base_sha],
            None,
        )
        if not add.ok:
            reason = (add.stderr or add.stdout).strip().splitlines()
            tail = reason[-1] if reason else f"exit {add.returncode}"
            return None, f"git worktree add ล้มเหลว ({tail}) — ใช้ shared cwd แทน"
        return (
            WorktreeInfo(path=str(dest), branch=branch, base_sha=base_sha, git_root=root),
            "",
        )

    # -- inspect ------------------------------------------------------------

    def commit_count(self, info: WorktreeInfo) -> int:
        """Commits the pane added on its branch beyond the creation base."""
        res = self._run(
            ["-C", info.path, "rev-list", "--count", f"{info.base_sha}..HEAD"],
            None,
        )
        if not res.ok:
            return 0
        try:
            return int(res.stdout.strip() or "0")
        except ValueError:
            return 0

    def is_dirty(self, info: WorktreeInfo) -> bool:
        """True when the worktree has uncommitted changes (blocks safe_remove)."""
        res = self._run(["-C", info.path, "status", "--porcelain"], None)
        return bool(res.ok and res.stdout.strip())

    def diffstat(self, info: WorktreeInfo) -> str:
        """Human-readable diff summary of the branch vs its base (for the Lead
        merge proposal). Empty string if it can't be computed."""
        res = self._run(
            ["-C", info.path, "diff", "--stat", f"{info.base_sha}..HEAD"],
            None,
        )
        return res.stdout.strip() if res.ok else ""

    # -- destroy (2-tier, adopted from agent-orchestrator) ------------------

    def safe_remove(self, info: WorktreeInfo) -> tuple[bool, str]:
        """Remove the worktree WITHOUT ``--force``, refusing to drop
        uncommitted work.

        Returns ``(True, "")`` when the worktree (and its now-unreferenced
        branch, if it carried no commits) is gone, or ``(False, reason)`` when
        it was preserved — typically because the tree is dirty. Callers surface
        the reason to the Lead instead of silently losing work.
        """
        if self.is_dirty(info):
            return False, "worktree มี uncommitted changes — เก็บไว้ (ไม่ลบทิ้ง)"
        rm = self._run(["-C", info.git_root, "worktree", "remove", info.path], None)
        self._run(["-C", info.git_root, "worktree", "prune"], None)
        if not rm.ok:
            tail = (rm.stderr or rm.stdout).strip().splitlines()
            return False, tail[-1] if tail else f"worktree remove exit {rm.returncode}"
        # Worktree gone. Delete the branch too ONLY when it added no commits —
        # a branch with work is left for the Lead to merge/inspect.
        if self.commit_count(info) == 0:
            self._run(["-C", info.git_root, "branch", "-D", info.branch], None)
        return True, ""

    def force_remove(self, info: WorktreeInfo) -> tuple[bool, str]:
        """Unconditional teardown (``--force`` + prune + branch -D). Used for
        explicit cleanup where losing uncommitted scratch is acceptable."""
        rm = self._run(["-C", info.git_root, "worktree", "remove", "--force", info.path], None)
        self._run(["-C", info.git_root, "worktree", "prune"], None)
        self._run(["-C", info.git_root, "branch", "-D", info.branch], None)
        if not rm.ok:
            tail = (rm.stderr or rm.stdout).strip().splitlines()
            return False, tail[-1] if tail else f"worktree remove --force exit {rm.returncode}"
        return True, ""


def build_merge_proposal(role: str, info: WorktreeInfo, commits: int, diffstat: str) -> str:
    """Lead-facing PROPOSAL when an isolated pane finishes with commits to merge.

    Never auto-merges — mirrors the cockpit's propose-then-fire doctrine (same
    as the verify-fail handoff). The worktree is kept until the Lead merges.
    """
    stat = diffstat.strip() or "(diffstat ว่าง)"
    return (
        f"🌿 [{role} worktree] ทำงานบน branch `{info.branch}` (isolated) — "
        f"{commits} commit พร้อม merge กลับ base\n\n"
        f"diffstat:\n{stat}\n\n"
        "เสนอ merge (propose-then-fire, ห้าม auto):\n"
        f"1. review: `git -C {info.git_root} diff {info.base_sha}..{info.branch}`\n"
        f"2. merge:  `git -C {info.git_root} merge --no-ff {info.branch}`\n"
        f"3. cleanup: `git -C {info.git_root} worktree remove {info.path}` "
        f"แล้ว `git -C {info.git_root} branch -d {info.branch}`\n"
        "worktree ยังอยู่จนกว่าจะ merge — อย่าลบก่อน · render proposal ให้ user confirm ก่อน fire"
    )
