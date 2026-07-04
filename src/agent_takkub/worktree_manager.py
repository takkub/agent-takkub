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
import os
import re
import shutil
import subprocess
import sys
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
    # Repo-relative paths that were LINKED (junction/symlink) or copied in from
    # the main tree per the P2.1 config. Recorded so removal can unlink each
    # one explicitly BEFORE any recursive delete — deleting through a junction
    # would wipe the main tree's real node_modules.
    links: tuple[str, ...] = ()
    # Dev-server port reserved for this worktree (P2.3). 0 = none allocated
    # (config has no base_port). The orchestrator excludes ports of live sibling
    # worktrees so two same-second assigns can't be handed the same number.
    port: int = 0

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "git_root": self.git_root,
            "links": list(self.links),
            "port": self.port,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorktreeInfo:
        return cls(
            path=d["path"],
            branch=d["branch"],
            base_sha=d.get("base_sha", ""),
            git_root=d["git_root"],
            links=tuple(d.get("links") or ()),
            port=int(d.get("port") or 0),
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
    # Windows drive prefix must be caught by pattern, not Path.drive — on a
    # POSIX runner Path("C:/evil") has no drive and would pass (CI macos catch).
    if re.match(r"^[A-Za-z]:", rel):
        return None
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


def _port_free(port: int) -> bool:
    """True when nothing is listening on 127.0.0.1:*port* (probe by bind)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_port(
    base: int,
    exclude: frozenset[int] | set[int] = frozenset(),
    probe: Callable[[int], bool] | None = None,
    tries: int = 50,
) -> int:
    """Pick the first free dev-server port at/after *base* (P2.3).

    *exclude* carries ports already handed to live sibling worktrees — a bind
    probe alone can't see those because their dev servers may not have started
    yet (two same-second assigns would otherwise both get *base*). Returns 0
    when *base* is 0 (no allocation configured) or the pool is exhausted.
    """
    if base <= 0:
        return 0
    is_free = probe or _port_free
    for p in range(base, base + tries):
        if p in exclude:
            continue
        if is_free(p):
            return p
    return 0


def _make_link(src: Path, dst: Path) -> str | None:
    """Link *src* (in the main tree) into the worktree at *dst*.

    Windows: directories become NTFS junctions (``_winapi.CreateJunction`` —
    works without admin/Developer Mode, unlike ``os.symlink``); files are
    copied (file symlinks need privileges). macOS/Linux: plain symlinks for
    both. Returns an error string on failure, None on success. Module-level so
    tests monkeypatch it and never touch the real filesystem semantics.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            if src.is_dir():
                import _winapi

                _winapi.CreateJunction(str(src), str(dst))
            else:
                shutil.copy2(src, dst)
        else:
            os.symlink(str(src), str(dst))
        return None
    except OSError as exc:
        return str(exc)


def _remove_link(p: Path) -> None:
    """Remove a link point WITHOUT ever recursing into its target.

    Symlinks and copied files → unlink; junctions / directory symlinks →
    ``os.rmdir`` (removes the reparse point only). A REAL non-empty directory
    fails both safely (OSError swallowed) — this function can never rmtree.
    """
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            os.rmdir(p)
    except OSError:
        pass  # best-effort; git worktree remove reports anything left behind


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
        self,
        base_cwd: str,
        project_ns: str,
        role: str,
        ts: int,
        exclude_ports: frozenset[int] | set[int] = frozenset(),
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
        # P2.2: env propagation per the project's opt-in config. Failures here
        # are NON-fatal — the worktree exists and is usable bare; warnings ride
        # back on the (info, reason) success channel for the Lead notice.
        cfg, cfg_warn = load_worktree_config(root)
        linked, link_warns = self._apply_links(root, dest, cfg)
        port = allocate_port(cfg.base_port, exclude_ports)
        port_warn = (
            f"port pool จาก base {cfg.base_port} เต็ม — worktree นี้ไม่ได้ port"
            if cfg.base_port and not port
            else ""
        )
        warns = "; ".join(w for w in [cfg_warn, *link_warns, port_warn] if w)
        return (
            WorktreeInfo(
                path=str(dest),
                branch=branch,
                base_sha=base_sha,
                git_root=root,
                links=tuple(linked),
                port=port,
            ),
            warns,
        )

    def _apply_links(
        self, git_root: str, dest: Path, cfg: WorktreeConfig
    ) -> tuple[list[str], list[str]]:
        """Link each configured entry from the main tree into the worktree.

        Returns ``(linked_rel_paths, warnings)``. Skips (with a warning) any
        source missing in the main tree or destination already present in the
        checkout (a tracked path — linking over it would shadow repo content).
        """
        linked: list[str] = []
        warns: list[str] = []
        for rel in cfg.symlinks:
            src = Path(git_root) / rel
            dst = dest / rel
            if not src.exists():
                warns.append(f"link ข้าม {rel}: ไม่มีใน main tree")
                continue
            if dst.exists():
                warns.append(f"link ข้าม {rel}: มีอยู่แล้วใน worktree (tracked?)")
                continue
            err = _make_link(src, dst)
            if err is not None:
                warns.append(f"link {rel} ล้มเหลว: {err}")
            else:
                linked.append(rel)
        return linked, warns

    def _unlink_links(self, info: WorktreeInfo) -> None:
        """Remove every recorded link point before any worktree removal."""
        for rel in info.links:
            _remove_link(Path(info.path) / rel)

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
        # Unlink junctions/symlinks FIRST — a recursive delete that followed a
        # junction would destroy the main tree's real node_modules (#81 P2.2).
        self._unlink_links(info)
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

    # -- CLI ops (P2.4: takkub worktree list / merge / clean) ----------------

    def list_isolated(self, git_root: str) -> list[dict]:
        """All ``wt/*`` worktrees of the repo with commits-ahead + dirty flags.

        Row shape: {"path", "branch", "sha", "ahead": int, "dirty": bool}.
        Works from git state alone — no PaneState needed (usable after a
        cockpit crash, or with the cockpit closed entirely).
        """
        res = self._run(["-C", git_root, "worktree", "list", "--porcelain"], None)
        if not res.ok:
            return []
        rows: list[dict] = []
        for ent in parse_worktree_list(res.stdout):
            branch = ent.get("branch")
            if not branch or not branch.startswith(f"{_BRANCH_PREFIX}/"):
                continue
            ahead_res = self._run(["-C", git_root, "rev-list", "--count", f"HEAD..{branch}"], None)
            try:
                ahead = int(ahead_res.stdout.strip() or "0") if ahead_res.ok else 0
            except ValueError:
                ahead = 0
            dirty_res = self._run(["-C", ent["path"], "status", "--porcelain"], None)
            rows.append(
                {
                    "path": ent["path"],
                    "branch": branch,
                    "sha": ent.get("sha", ""),
                    "ahead": ahead,
                    "dirty": bool(dirty_res.ok and dirty_res.stdout.strip()),
                }
            )
        return rows

    def merge_isolated(self, git_root: str, branch: str, keep: bool = False) -> tuple[bool, str]:
        """``merge --no-ff`` an isolated branch into the main tree's HEAD, then
        (unless *keep*) remove its worktree + branch.

        On a merge conflict the merge is aborted and the worktree left intact —
        the caller reports the conflict instead of leaving the main tree in a
        conflicted state. The pre-removal link sweep makes cleanup safe even
        when the links record died with a crashed cockpit.
        """
        rows = [r for r in self.list_isolated(git_root) if r["branch"] == branch]
        if not rows:
            return False, f"ไม่พบ worktree ของ branch {branch}"
        row = rows[0]
        if row["dirty"]:
            return False, (
                f"worktree ของ {branch} มี uncommitted changes — ให้ pane commit ก่อน "
                f"หรือเข้าไปเก็บงานที่ {row['path']}"
            )
        merge = self._run(["-C", git_root, "merge", "--no-ff", "--no-edit", branch], None)
        if not merge.ok:
            self._run(["-C", git_root, "merge", "--abort"], None)
            tail = (merge.stderr or merge.stdout).strip().splitlines()
            return False, (
                f"merge conflict/ล้มเหลว ({tail[-1] if tail else merge.returncode}) — "
                f"abort แล้ว worktree ยังอยู่ครบที่ {row['path']}"
            )
        if keep:
            return True, f"merged {branch} (–keep: worktree ยังอยู่)"
        sweep_link_points(Path(row["path"]))
        self._run(["-C", git_root, "worktree", "remove", row["path"]], None)
        self._run(["-C", git_root, "worktree", "prune"], None)
        self._run(["-C", git_root, "branch", "-d", branch], None)
        return True, f"merged {branch} + cleanup เรียบร้อย"

    def clean_isolated(self, git_root: str, force: bool = False) -> list[str]:
        """Sweep leftover ``wt/*`` worktrees (crashed panes, forgotten probes).

        Default: remove only SAFE leftovers — clean tree AND no commits ahead
        (nothing of value can be lost). ``force=True`` removes every wt/*
        worktree + branch regardless (dirty work and unmerged commits are
        dropped — the CLI makes the caller opt in explicitly). Returns
        human-readable result lines.
        """
        out: list[str] = []
        for row in self.list_isolated(git_root):
            keep_reason = ""
            if not force:
                if row["dirty"]:
                    keep_reason = "dirty (มี uncommitted changes)"
                elif row["ahead"]:
                    keep_reason = f"{row['ahead']} commit ยังไม่ merge"
            if keep_reason:
                out.append(f"KEEP  {row['branch']} — {keep_reason}")
                continue
            sweep_link_points(Path(row["path"]))
            args = ["-C", git_root, "worktree", "remove"]
            if force:
                args.append("--force")
            rm = self._run([*args, row["path"]], None)
            self._run(["-C", git_root, "worktree", "prune"], None)
            self._run(["-C", git_root, "branch", "-D", row["branch"]], None)
            out.append(
                f"{'REMOVED' if rm.ok else 'FAILED '} {row['branch']}"
                + ("" if rm.ok else f" — {(rm.stderr or rm.stdout).strip()[:120]}")
            )
        return out

    def force_remove(self, info: WorktreeInfo) -> tuple[bool, str]:
        """Unconditional teardown (``--force`` + prune + branch -D). Used for
        explicit cleanup where losing uncommitted scratch is acceptable."""
        self._unlink_links(info)  # never recurse through a junction (#81 P2.2)
        rm = self._run(["-C", info.git_root, "worktree", "remove", "--force", info.path], None)
        self._run(["-C", info.git_root, "worktree", "prune"], None)
        self._run(["-C", info.git_root, "branch", "-D", info.branch], None)
        if not rm.ok:
            tail = (rm.stderr or rm.stdout).strip().splitlines()
            return False, tail[-1] if tail else f"worktree remove --force exit {rm.returncode}"
        return True, ""


def _is_link_point(p: Path) -> bool:
    """True for anything that must be unlinked, never recursed into: symlinks
    (both OS) and Windows reparse points (junctions — ``is_symlink()`` is False
    for those, so check the FILE_ATTRIBUTE_REPARSE_POINT bit)."""
    if p.is_symlink():
        return True
    if sys.platform == "win32":
        try:
            import stat as _stat

            attrs = p.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & _stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
    return False


def sweep_link_points(top: Path) -> list[str]:
    """Remove every link point under *top* without ever following one (P2.4).

    Crash-recovery safety net for `takkub worktree clean/merge`: when the
    cockpit died, the WorktreeInfo.links record is gone, so before ANY
    recursive removal we walk the tree with ``followlinks=False``, unlink each
    symlink/junction found, and prune it from the walk. Returns the removed
    relative paths. A tree swept by this function contains no traversable link
    into the main tree, making the follow-up ``git worktree remove`` safe.
    """
    removed: list[str] = []
    top = Path(top)
    if not top.is_dir() or _is_link_point(top):
        return removed
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        base = Path(dirpath)
        keep_dirs = []
        for name in dirnames:
            child = base / name
            if _is_link_point(child):
                _remove_link(child)
                removed.append(str(child.relative_to(top)))
            else:
                keep_dirs.append(name)
        dirnames[:] = keep_dirs  # never descend into (now-removed) link dirs
        for name in filenames:
            child = base / name
            if child.is_symlink():
                _remove_link(child)
                removed.append(str(child.relative_to(top)))
    return removed


def parse_worktree_list(porcelain: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into dicts (pure, unit-tested).

    Returns ``[{"path": str, "sha": str, "branch": str|None}]`` — branch is
    None for a detached/bare entry. Isolated cockpit worktrees are the entries
    whose branch starts with ``wt/``.
    """
    out: list[dict] = []
    cur: dict = {}
    for line in porcelain.splitlines():
        line = line.strip()
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree ") :], "sha": "", "branch": None}
        elif line.startswith("HEAD "):
            cur["sha"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            cur["branch"] = ref.removeprefix("refs/heads/")
    if cur:
        out.append(cur)
    return out


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
