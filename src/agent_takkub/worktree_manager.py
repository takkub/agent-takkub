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
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
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
    symlinks = raw.get("symlinks", [])
    if not isinstance(symlinks, list):
        warnings.append("symlinks ต้องเป็น array")
        symlinks = []
    for entry in symlinks[:_MAX_SYMLINKS]:
        rel = _safe_rel_entry(entry)
        if rel is None:
            warnings.append(f"symlinks entry ไม่ปลอดภัย/ไม่ใช่ relative path: {entry!r}")
        else:
            links.append(rel)

    cmds: list[str] = []
    post_create = raw.get("postCreate", [])
    if not isinstance(post_create, list):
        warnings.append("postCreate ต้องเป็น array")
        post_create = []
    for cmd in post_create[:_MAX_POST_CREATE]:
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
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
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


def dir_stats(path: Path) -> tuple[int, int]:
    """(total_bytes, file_count) recursively under *path*. Best-effort.

    Lives here (not disk_usage.py) because worktree_manager is the leaf
    module disk_usage already imports FROM (never the reverse — the
    `worktree-manager-leaf` import-linter contract forbids it) — disk_usage
    imports this instead of keeping its own copy of the same walk.
    """
    total = 0
    count = 0
    if not path.is_dir():
        return 0, 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                    count += 1
                except OSError:
                    continue
    except OSError:
        pass
    return total, count


def _dir_has_node_modules(path: Path) -> bool:
    """True when *path* itself, or anything under it, is/contains a
    ``node_modules`` dir. Cheap existence probe for `list_orphans`'s report
    row — stops at the first hit (disk_usage's `find_node_modules` instead
    enumerates every one, for deletion, so isn't a fit here)."""
    if (path / "node_modules").is_dir():
        return True
    try:
        for _root, dirnames, _files in os.walk(path, onerror=lambda _e: None):
            if "node_modules" in dirnames:
                return True
    except OSError:
        pass
    return False


def _candidate_project_worktree_dirs(git_root: Path, registered_paths: set[Path]) -> set[Path]:
    """Best-effort set of ``<DATA_HOME>/worktrees/<project>`` dirs that may
    hold checkouts of *git_root*, for :meth:`WorktreeManager.list_orphans`.

    Primary signal: the parent dir of any currently-registered worktree of
    this repo — exact, no naming heuristic needed. When git knows about
    NONE (every worktree of this repo has already been orphaned/pruned —
    #355's own repro: only 2 of 44 on-disk dirs were still registered, but
    that still leaves 2 to anchor on), fall back to matching
    ``<DATA_HOME>/worktrees/*`` entries whose sanitized name equals the
    repo dir's own sanitized name — the same slug `worktree_root()` uses at
    creation time. That fallback is a heuristic (a project's registered
    name can differ from its checkout folder name) and only runs when the
    primary signal is empty.
    """
    from_registered = {p.parent for p in registered_paths}
    if from_registered:
        return from_registered
    root = DATA_HOME / "worktrees"
    if not root.is_dir():
        return set()
    slug = sanitize_ref_component(git_root.name).lower()
    out: set[Path] = set()
    try:
        for d in root.iterdir():
            if d.is_dir() and sanitize_ref_component(d.name).lower() == slug:
                out.add(d.resolve())
    except OSError:
        pass
    return out


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


def _dev_venv_site_packages(git_root: Path) -> Path | None:
    """Locate *git_root*'s dev-checkout ``.venv`` site-packages, or ``None``
    when there is no ``.venv`` (most projects the cockpit manages aren't
    Python at all). Windows: ``.venv/Lib/site-packages``; POSIX:
    ``.venv/lib/python<major.minor>/site-packages``."""
    venv = git_root / ".venv"
    win_site = venv / "Lib" / "site-packages"
    if win_site.is_dir():
        return win_site
    posix_matches = sorted(venv.glob("lib/python*/site-packages"))
    return posix_matches[0] if posix_matches else None


def _dev_venv_python(git_root: Path) -> Path | None:
    """Locate *git_root*'s dev-checkout ``.venv`` interpreter, or ``None``
    when it doesn't exist. Must NEVER fall back to ``sys.executable`` — that
    is the *cockpit process's* interpreter, which on a prod/installed cockpit
    is a totally different venv from the repo's own ``.venv`` (#202 follow-up:
    using ``sys.executable`` here would editable-install the dev checkout
    into the prod cockpit's venv instead of the repo's)."""
    venv = git_root / ".venv"
    win_python = venv / "Scripts" / "python.exe"
    if win_python.is_file():
        return win_python
    posix_python = venv / "bin" / "python"
    if posix_python.is_file():
        return posix_python
    return None


def repair_editable_pth_if_stale(git_root: str, removed_path: str) -> str:
    """After a worktree checkout is removed, check whether *git_root*'s
    dev-checkout venv had an editable-install ``.pth`` pointing INTO it — if
    so, the shared venv every other pane in this repo uses just went stale
    (#202: a `backend` pane's `pip install -e .` from inside the worktree
    repointed it there; once the worktree was removed the whole cockpit's
    `.venv` broke with ``ModuleNotFoundError``). Repairs by reinstalling from
    *git_root* using **that venv's own interpreter** (never ``sys.executable``
    — the cockpit process running this code may be a different install
    entirely, e.g. a prod cockpit repairing a dev checkout's venv) and returns
    a human message; empty string when nothing needed fixing (no venv, no
    editable install, or it already pointed elsewhere).
    """
    root = Path(git_root)
    site_packages = _dev_venv_site_packages(root)
    if site_packages is None:
        return ""
    try:
        removed = Path(removed_path).resolve()
    except OSError:
        return ""
    for pth in sorted(site_packages.glob("__editable__.agent_takkub-*.pth")):
        try:
            lines = pth.read_text(encoding="utf-8").splitlines()
            target_raw = next((ln.strip() for ln in lines if ln.strip()), "")
        except OSError:
            continue
        if not target_raw:
            continue
        try:
            target = Path(target_raw).resolve()
        except OSError:
            continue
        if target != removed and removed not in target.parents:
            continue
        venv_python = _dev_venv_python(root)
        if venv_python is None:
            return (
                f"⚠ {pth.name} เคยชี้ worktree ที่เพิ่งลบ "
                f"แต่หา python ของ {root / '.venv'} ไม่เจอ ซ่อมอัตโนมัติไม่ได้ — "
                f"รัน `<{root / '.venv'} ของคุณเอง>/python -m pip install -e . --no-deps` "
                f"จาก {root} เอง (ห้ามใช้ python อื่นซ่อม venv นี้)"
            )
        try:
            proc = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-e", ".", "--no-deps"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (
                f"⚠ {pth.name} เคยชี้ worktree ที่เพิ่งลบ "
                f"แต่ซ่อมอัตโนมัติไม่สำเร็จ ({exc}) — "
                f"รัน `{venv_python} -m pip install -e . --no-deps` จาก {root} เอง"
            )
        if proc.returncode == 0:
            return (
                f"\U0001f527 ซ่อม {pth.name} อัตโนมัติ "
                f"(เคยชี้ worktree ที่เพิ่งลบ #202) "
                f"— reinstall จาก {root} แล้ว"
            )
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        return (
            f"⚠ {pth.name} เคยชี้ worktree ที่เพิ่งลบ "
            f"แต่ซ่อมอัตโนมัติไม่สำเร็จ ({tail}) — "
            f"รัน `{venv_python} -m pip install -e . --no-deps` จาก {root} เอง"
        )
    return ""


# ── Long-path-safe delete (#226 / #227) ─────────────────────────────────────
#
# `git worktree remove` does its own recursive delete of the working tree
# before it will drop the administrative metadata. On Windows that walk goes
# through plain (non-extended-length) paths, which are capped at MAX_PATH
# (260 chars) — a worktree holding an installed pnpm project's nested
# node_modules routinely exceeds that, so the delete fails partway with
# "Filename too long" (#226) having ALREADY removed some entries (#227: a
# real incident where `.git` and `apps/` were gone but `packages/` survived,
# reported to the Lead as "merged ... but could not delete worktree (kept)" —
# a lie, since the worktree could no longer be worked in and `.git` itself
# was gone).
#
# Fix: never let git's own delete touch the tracked path at all.
#   1. `_stage_for_delete` renames the checkout dir aside first. A rename
#      only rewrites one directory entry — it is NOT a recursive walk, so it
#      is immune to the MAX_PATH problem regardless of how deep the tree
#      inside is, and it is atomic: it either fully succeeds (tracked path
#      now fully gone) or fully fails (tracked path fully untouched). There
#      is no partial state at the path anything else still points at.
#   2. `_rmtree_long_path_safe` then deletes the staged copy through the
#      Win32 extended-length (`\\?\`) path form, which has no MAX_PATH limit.
#   3. Only once the tracked path is confirmed gone does the caller run
#      `git worktree remove --force` — at that point it touches no files,
#      just the small `.git/worktrees/<id>` admin refs, so it can't repeat
#      the partial-delete failure mode.
# A failure in step 2 still means step 1 succeeded, so the caller never has
# to report "kept" for something that no longer exists at the tracked path —
# only genuine step-1 (rename) failures are truly "kept, fully intact".


def _win_long_path(path: Path) -> str:
    """Extended-length (``\\\\?\\``) form of *path* — routes Win32 file APIs
    around the 260-char MAX_PATH limit. UNC roots need the ``\\\\?\\UNC\\``
    variant (``\\\\server\\share`` -> ``\\\\?\\UNC\\server\\share``)."""
    s = str(path)
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _path_exists_long_safe(path: Path) -> bool:
    """``path.exists()`` that stays correct past MAX_PATH on Windows — a
    plain (non-prefixed) check on a too-long path raises/returns False for
    the wrong reason (path-too-long, not missing), which would make a still-
    present partial delete look like a clean success."""
    if sys.platform == "win32":
        return os.path.exists(_win_long_path(path))
    return path.exists()


def _clear_readonly_and_retry(func: Callable, path: str, exc_info) -> None:
    """``shutil.rmtree`` onerror hook: files checked out by git (especially
    inside an installed node_modules) are sometimes read-only on Windows,
    which blocks unlink/rmdir. Clear the bit and retry once; any further
    failure is swallowed here — ``rmtree`` must never raise mid-walk — and is
    instead caught by the caller's post-hoc existence check."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree_long_path_safe(path: Path) -> tuple[bool, str]:
    """Recursively delete *path*, immune to MAX_PATH on Windows.

    Because ``onerror`` swallows exceptions (rmtree must never raise
    mid-walk), a clean return does NOT prove full success — success is
    verified with an explicit post-hoc existence check instead of trusting
    ``shutil.rmtree``'s silence.
    """
    target = _win_long_path(path) if sys.platform == "win32" else str(path)
    try:
        shutil.rmtree(target, onerror=_clear_readonly_and_retry)
    except OSError as exc:
        return False, str(exc)
    if _path_exists_long_safe(path):
        return False, f"{path} ลบไม่หมด (ไฟล์/โฟลเดอร์บางส่วนยังเหลืออยู่)"
    return True, ""


def _stage_for_delete(path: Path) -> tuple[Path | None, str]:
    """Rename *path* aside before the recursive delete that follows (#227).

    A plain rename moves only the directory entry, not its contents, so —
    unlike a recursive walk — it can't fail partway through a deep tree: it
    either fully succeeds (the tracked *path* is now completely gone) or
    fully fails (nothing touched, *path* still fully intact). Returns
    ``(path, "")`` unchanged when there was nothing there to move.
    """
    if not _path_exists_long_safe(path):
        return path, ""  # nothing to stage — already gone
    staged = path.parent / f".trash-{path.name}-{os.getpid()}"
    src = _win_long_path(path) if sys.platform == "win32" else str(path)
    dst = _win_long_path(staged) if sys.platform == "win32" else str(staged)
    try:
        os.rename(src, dst)
    except OSError as exc:
        return None, str(exc)
    return staged, ""


def remove_worktree_tree(path: Path) -> tuple[bool, str, str]:
    """Best-effort on-disk delete of a worktree checkout directory.

    Returns ``(removed_from_original, message, leftover_path)``:

    * ``removed_from_original`` is False ONLY when *path* is still fully
      intact — the one case where reporting it as "kept" is accurate. It is
      True the moment *path* itself no longer exists, whether there was
      nothing to delete, the delete fully succeeded, or it partially failed
      (any survivors live only under *leftover_path*, a staged sibling —
      never at *path*).
    * ``message`` is empty on a full clean delete, else a human reason.
    * ``leftover_path`` is non-empty only when staged content survives a
      failed/partial delete — surfaced so stale disk usage can be found and
      retried later (the existing orphan-worktree sweep in ``disk_usage.py``
      already picks up any unregistered dir under the managed worktree root,
      which is exactly where the staged sibling lives).
    """
    staged, err = _stage_for_delete(path)
    if staged is None:
        return False, f"ลบไม่ได้ ({err})", ""
    if staged == path:
        return True, "", ""  # nothing was there to begin with
    ok, rm_err = _rmtree_long_path_safe(staged)
    if ok:
        return True, "", ""
    return (
        True,
        f"ย้ายออกจาก {path} แล้ว แต่ไฟล์ที่เหลือค้างที่ {staged} ลบไม่หมด ({rm_err})",
        str(staged),
    )


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

    def shared_tree_baseline(
        self, cwd: str
    ) -> tuple[str | None, str | None, DirtyTreeSnapshot | None]:
        """Capture the cheap assign-time baseline for a shared checkout.

        ``rev-parse`` returns both the root and HEAD in one process; the only
        other git process is porcelain status.  File metadata is read only for
        paths status already declared dirty, so this never hashes or walks the
        tracked tree.  ``None`` for the dirty snapshot means status itself
        failed; an empty dict is a successfully observed clean tree.
        """
        discovery = self._run(["-C", cwd, "rev-parse", "--show-toplevel", "HEAD"], None)
        lines = [line.strip() for line in discovery.stdout.splitlines() if line.strip()]
        if not discovery.ok or len(lines) < 2:
            return None, None, None
        git_root, head_sha = lines[0], lines[1]
        porcelain = self.shared_tree_status_porcelain(cwd)
        if porcelain is None:
            return head_sha, git_root, None
        return head_sha, git_root, self.dirty_snapshot(git_root, porcelain)

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

    def commits_since(self, cwd: str, base_sha: str) -> int:
        """Commits reachable from *cwd*'s HEAD but not from *base_sha*.

        Generic form of :meth:`commit_count` (which is now a thin wrapper over
        this for a worktree's own base) — also used directly for a SHARED-tree
        pane's digest facts (#245), where there is no :class:`WorktreeInfo` to
        wrap, only a plain assign-time HEAD snapshot (``PaneState.assign_base_sha``).
        """
        res = self._run(["-C", cwd, "rev-list", "--count", f"{base_sha}..HEAD"], None)
        if not res.ok:
            return 0
        try:
            return int(res.stdout.strip() or "0")
        except ValueError:
            return 0

    def commit_count(self, info: WorktreeInfo) -> int:
        """Commits the pane added on its branch beyond the creation base."""
        return self.commits_since(info.path, info.base_sha)

    def status_porcelain(self, cwd: str) -> str:
        """Raw ``git status --porcelain`` output for *cwd* (empty string on
        any git failure — callers treat that the same as "nothing changed",
        matching every pre-existing caller's error handling)."""
        res = self._run(["-C", cwd, "status", "--porcelain"], None)
        return res.stdout if res.ok else ""

    def shared_tree_status_porcelain(self, cwd: str) -> str | None:
        """Checked porcelain result for #251's shared-tree snapshot.

        Unlike the long-established :meth:`status_porcelain` API, failure is
        ``None`` rather than an empty string.  A failed done-time probe must
        render "ตรวจไม่ได้", not claim every baseline-dirty path disappeared.
        Keeping this separate also leaves isolated-worktree behavior exactly
        unchanged.
        """
        # NUL form prevents git's platform/config-dependent C quoting from
        # turning a Unicode/space-containing filename into a non-existent
        # literal path when we lstat it.
        res = self._run(["-C", cwd, "status", "--porcelain", "-z"], None)
        return res.stdout if res.ok else None

    def is_dirty(self, info: WorktreeInfo) -> bool:
        """True when the worktree has uncommitted changes (blocks safe_remove)."""
        return self.is_dirty_at(info.path)

    def is_dirty_at(self, cwd: str) -> bool:
        """Same as :meth:`is_dirty` but for a bare checkout path (no
        :class:`WorktreeInfo` needed) — used to inspect an orphan checkout
        that git can still read directly (#132)."""
        return bool(self.status_porcelain(cwd).strip())

    def current_branch(self, cwd: str) -> str | None:
        """Branch checked out at *cwd*, or None when detached/unresolvable."""
        res = self._run(["-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], None)
        name = res.stdout.strip() if res.ok else ""
        return name if name and name != "HEAD" else None

    def dirty_snapshot(self, git_root: str, porcelain: str) -> DirtyTreeSnapshot:
        """Live-runner wrapper over :func:`snapshot_porcelain_paths` (#261 follow-up).

        Passes this manager's own runner so a collapsed ``?? dir/`` entry gets
        expanded to its file-level contents instead of always being treated
        as changed. Assign-time (:meth:`shared_tree_baseline`) and done-time
        (orchestrator digest) both go through this same method so the
        expansion is symmetric — comparing an expanded baseline against an
        un-expanded current (or vice versa) would never match.
        """
        return snapshot_porcelain_paths(git_root, porcelain, self._run)

    def commits_ahead(self, git_root: str, branch: str) -> int:
        """Commits reachable from *branch* but not from *git_root*'s current
        HEAD — used to warn instead of silently deleting an orphan checkout
        whose branch still carries unmerged work (#132)."""
        res = self._run(["-C", git_root, "rev-list", "--count", f"HEAD..{branch}"], None)
        try:
            return int(res.stdout.strip() or "0") if res.ok else 0
        except ValueError:
            return 0

    def diffstat_since(self, cwd: str, base_sha: str) -> str:
        """Generic form of :meth:`diffstat` — diff summary of *cwd*'s HEAD vs
        *base_sha*, for a plain checkout path with no :class:`WorktreeInfo`
        (shared-tree digest facts, #245)."""
        res = self._run(["-C", cwd, "diff", "--stat", f"{base_sha}..HEAD"], None)
        return res.stdout.strip() if res.ok else ""

    def diffstat(self, info: WorktreeInfo) -> str:
        """Human-readable diff summary of the branch vs its base (for the Lead
        merge proposal). Empty string if it can't be computed."""
        return self.diffstat_since(info.path, info.base_sha)

    def uncommitted_count_at(self, cwd: str) -> int:
        """Generic form of :meth:`uncommitted_count` for a plain checkout path
        (#245 — shared-tree panes have no :class:`WorktreeInfo` to wrap)."""
        return len([ln for ln in self.status_porcelain(cwd).splitlines() if ln.strip()])

    def uncommitted_count(self, info: WorktreeInfo) -> int:
        """Number of changed paths in the worktree (#244) — `is_dirty` alone
        only answers yes/no; the Lead-facing merge proposal needs the actual
        count so "⚠ N ไฟล์ยังไม่ commit" is a real number, not a guess."""
        return self.uncommitted_count_at(info.path)

    def merge_conflicts_with_base(self, git_root: str, branch: str) -> bool | None:
        """Whether a 3-way merge of *branch* against the CURRENT base HEAD
        would conflict (#244 — "merge สะอาดไหม เทียบ base ปัจจุบัน").

        Deliberately compares against ``git_root``'s HEAD *now*, not the
        worktree's creation-time ``base_sha`` — other work may have merged
        into base since the isolated pane started. Read-only: `git
        merge-tree <merge-base> HEAD <branch>` writes conflict markers to
        stdout without ever touching the index or working tree, so it is
        safe to run on a live main tree. Returns ``None`` (unknown, not
        "clean") when the merge-base or merge-tree probe itself fails —
        callers must treat that as "couldn't verify", never as a green
        light.
        """
        base = self._run(["-C", git_root, "merge-base", "HEAD", branch], None)
        base_sha = base.stdout.strip() if base.ok else ""
        if not base_sha:
            return None
        mt = self._run(["-C", git_root, "merge-tree", base_sha, "HEAD", branch], None)
        if not mt.ok:
            return None
        return "<<<<<<<" in mt.stdout

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
        # Delete the checkout ourselves (long-path-safe, #226/#227) BEFORE
        # letting git touch it — see the module-level comment above
        # `remove_worktree_tree`. Only genuine step-1 (rename) failures leave
        # *path* untouched, which is the one case "kept" is accurate for.
        removed, disk_msg, leftover = remove_worktree_tree(Path(info.path))
        if not removed:
            return False, disk_msg
        # Directory is gone — this only touches the small admin refs now, so
        # it can't repeat the partial-delete failure mode.
        rm = self._run(["-C", info.git_root, "worktree", "remove", "--force", info.path], None)
        self._run(["-C", info.git_root, "worktree", "prune"], None)
        if not rm.ok:
            tail = (rm.stderr or rm.stdout).strip().splitlines()
            detail = tail[-1] if tail else f"worktree remove exit {rm.returncode}"
            return False, (
                f"ลบไฟล์ออกจาก {info.path} แล้ว แต่ git ยังลบ metadata ไม่ได้ ({detail}) "
                f"— รัน `git -C {info.git_root} worktree prune` เอง"
            )
        # Worktree gone. Delete the branch too ONLY when it added no commits —
        # a branch with work is left for the Lead to merge/inspect.
        if self.commit_count(info) == 0:
            self._run(["-C", info.git_root, "branch", "-D", info.branch], None)
        repair_note = repair_editable_pth_if_stale(info.git_root, info.path)
        if leftover:
            note = f"ไฟล์บางส่วนค้างที่ {leftover} ลบเองทีหลังได้"
            return True, f"{note} · {repair_note}" if repair_note else note
        return True, repair_note

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

    def list_orphans(
        self, git_root: str, live_paths: frozenset[str] | set[str] = frozenset()
    ) -> list[dict]:
        """On-disk worktree checkout dirs for this repo that git has
        completely forgotten (#355).

        `list_isolated`/`clean_isolated` both iterate ``git worktree list
        --porcelain`` — a dir whose registration is already gone (branch
        deleted, worktree pruned from the registry) but whose files
        survived on disk is invisible to either, because neither ever looks
        at the DISK. That shape is exactly what pre-#226/#227 left behind:
        git's own (pre-fix) recursive delete hit Windows' MAX_PATH mid
        ``node_modules`` and silently stopped, while the branch/registry
        cleanup that followed still ran to completion — no ``.git``
        pointer, no branch, nothing left for git-based discovery to anchor
        on. This walks the managed root(s) on disk instead and diffs
        against git's current view.

        Row shape: ``{"path", "size_bytes", "file_count",
        "has_node_modules"}``, sorted largest-first. *live_paths* mirrors
        `clean_isolated`'s #187 guard — a dir a live pane is currently
        sitting in is never listed, even if git has (for whatever reason)
        already forgotten it.

        Only ever scans under ``<DATA_HOME>/worktrees`` (the managed root
        every isolated worktree is created under, `worktree_root`) — never
        the repo's OWN parent dir. Anchoring on the wrong registered entry
        (e.g. the repo's main, non-isolated worktree — its path can resolve
        to a drive root on Windows) would otherwise turn this into an
        unbounded scan of an unrelated, potentially huge directory tree; the
        managed-root check below is the actual guard, kept even though the
        anchor is already filtered to isolated ``wt/*`` entries.
        """
        root = Path(git_root).resolve()
        res = self._run(["-C", git_root, "worktree", "list", "--porcelain"], None)
        registered_isolated = (
            {
                Path(ent["path"]).resolve()
                for ent in parse_worktree_list(res.stdout)
                if ent.get("branch") and ent["branch"].startswith(f"{_BRANCH_PREFIX}/")
            }
            if res.ok
            else set()
        )
        live = {str(Path(p).resolve()) for p in live_paths}
        managed_root = (DATA_HOME / "worktrees").resolve()
        orphans: list[dict] = []
        for project_dir in _candidate_project_worktree_dirs(root, registered_isolated):
            if project_dir != managed_root and managed_root not in project_dir.parents:
                continue  # never scan outside the managed worktrees root
            if not project_dir.is_dir():
                continue
            try:
                children = [c for c in project_dir.iterdir() if c.is_dir()]
            except OSError:
                continue
            for child in children:
                child_r = child.resolve()
                if child_r in registered_isolated or str(child_r) in live:
                    continue
                size, count = dir_stats(child)
                orphans.append(
                    {
                        "path": str(child),
                        "size_bytes": size,
                        "file_count": count,
                        "has_node_modules": _dir_has_node_modules(child),
                    }
                )
        orphans.sort(key=lambda r: -r["size_bytes"])
        return orphans

    def merge_isolated(
        self,
        git_root: str,
        branch: str,
        keep: bool = False,
        live_paths: frozenset[str] | set[str] = frozenset(),
    ) -> tuple[bool, str]:
        """``merge --no-ff`` an isolated branch into the main tree's HEAD, then
        (unless *keep*) remove its worktree + branch.

        On a merge conflict the merge is aborted and the worktree left intact —
        the caller reports the conflict instead of leaving the main tree in a
        conflicted state. The pre-removal link sweep makes cleanup safe even
        when the links record died with a crashed cockpit.

        *live_paths* mirrors :meth:`clean_isolated`'s #187 live-pane guard —
        a worktree a currently-alive pane still sits in is never removed
        (#227: the pane can't continue if its cwd is yanked out from under
        it, and had already committed everything only by luck in the
        incident that motivated this). The merge itself still happens; only
        the removal step is skipped.
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
        live = {str(Path(p).resolve()) for p in live_paths}
        if str(Path(row["path"]).resolve()) in live:
            return True, (
                f"merged {branch} — worktree ยังมี pane ใช้งานอยู่ (live) จึงไม่ลบ; "
                f"ปิด pane ก่อนด้วย `takkub close --role <role>` แล้วค่อย `takkub worktree clean`"
            )
        sweep_link_points(Path(row["path"]))
        removed, disk_msg, leftover = remove_worktree_tree(Path(row["path"]))
        if not removed:
            return True, f"merged {branch} แต่ลบ worktree ไม่ได้ ({disk_msg}) — เก็บที่ {row['path']}"
        remove = self._run(["-C", git_root, "worktree", "remove", "--force", row["path"]], None)
        self._run(["-C", git_root, "worktree", "prune"], None)
        if not remove.ok:
            lines = (remove.stderr or remove.stdout).strip().splitlines()
            detail = lines[-1] if lines else f"exit {remove.returncode}"
            return True, (
                f"merged {branch} — ลบไฟล์ออกจาก {row['path']} แล้ว แต่ git ยังลบ metadata ไม่ได้ "
                f"({detail}) — รัน `git -C {git_root} worktree prune` เอง"
            )
        self._run(["-C", git_root, "branch", "-d", branch], None)
        repair_note = repair_editable_pth_if_stale(git_root, row["path"])
        msg = f"merged {branch} + cleanup เรียบร้อย"
        if leftover:
            msg += f" (ไฟล์บางส่วนค้างที่ {leftover} ลบเองทีหลังได้)"
        return True, f"{msg} · {repair_note}" if repair_note else msg

    def clean_isolated(
        self,
        git_root: str,
        force: bool = False,
        live_paths: frozenset[str] | set[str] = frozenset(),
    ) -> list[str]:
        """Sweep leftover ``wt/*`` worktrees (crashed panes, forgotten probes).

        Default: remove only SAFE leftovers — clean tree AND no commits ahead
        (nothing of value can be lost). ``force=True`` removes every wt/*
        worktree + branch regardless of dirty/unmerged status (that work is
        dropped — the CLI makes the caller opt in explicitly). Returns
        human-readable result lines.

        Two safety rules, both unconditional (#187 — a real incident where
        `--force` deleted the branch of a worktree a just-spawned pane still
        held, seconds after `git worktree remove` itself failed on a Windows
        file lock):

        * **Live-pane guard** — a path present in *live_paths* (worktrees a
          currently-alive pane is sitting in, see
          :meth:`Orchestrator.live_worktree_paths`) is ALWAYS skipped, dirty
          or not, ``force`` or not. There is no bypass flag: yanking the
          checkout out from under a running pane corrupts its cwd and can
          orphan uncommitted work with zero chance to recover it — the only
          safe sequence is ``takkub close --role <r>`` first, then clean.
        * **Atomicity** — the branch is deleted only when ``git worktree
          remove`` actually succeeded. A failed removal (permission denied,
          the directory still locked, ...) now leaves BOTH the worktree
          directory and its branch untouched and is reported as such, instead
          of the pre-#187 behavior where the branch was deleted unconditionally
          right after the (possibly failed) remove call.
        """
        live = {str(Path(p).resolve()) for p in live_paths}
        out: list[str] = []
        for row in self.list_isolated(git_root):
            if str(Path(row["path"]).resolve()) in live:
                out.append(
                    f"KEEP  {row['branch']} — pane ยังใช้งาน worktree นี้อยู่ (live pane); "
                    "ปิด pane ก่อน (`takkub close --role <role>`) แล้วค่อย clean ใหม่"
                )
                continue
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
            removed, disk_msg, leftover = remove_worktree_tree(Path(row["path"]))
            if not removed:
                out.append(
                    f"FAILED  {row['branch']} — {disk_msg} "
                    "(ไม่ได้ลบอะไร — worktree และ branch ยังอยู่ครบ)"
                )
                continue
            rm = self._run(["-C", git_root, "worktree", "remove", "--force", row["path"]], None)
            self._run(["-C", git_root, "worktree", "prune"], None)
            if not rm.ok:
                detail = (rm.stderr or rm.stdout).strip()[:120]
                out.append(
                    f"FAILED  {row['branch']} — ลบไฟล์ออกจาก {row['path']} แล้ว "
                    f"แต่ git ยังลบ metadata ไม่ได้ ({detail or f'exit {rm.returncode}'}) "
                    "— รัน `worktree prune` เอง (branch ยังอยู่)"
                )
                continue
            self._run(["-C", git_root, "branch", "-D", row["branch"]], None)
            repair_note = repair_editable_pth_if_stale(git_root, row["path"])
            note = f"REMOVED {row['branch']}"
            if leftover:
                note += f" (ไฟล์บางส่วนค้างที่ {leftover} ลบเองทีหลังได้)"
            if repair_note:
                note += f" · {repair_note}"
            out.append(note)
        return out

    def force_remove(self, info: WorktreeInfo) -> tuple[bool, str]:
        """Unconditional teardown (``--force`` + prune + branch -D). Used for
        explicit cleanup where losing uncommitted scratch is acceptable."""
        self._unlink_links(info)  # never recurse through a junction (#81 P2.2)
        # Long-path-safe delete ourselves first (#226/#227) — see the
        # module-level comment above `remove_worktree_tree`.
        removed, disk_msg, leftover = remove_worktree_tree(Path(info.path))
        rm = self._run(["-C", info.git_root, "worktree", "remove", "--force", info.path], None)
        self._run(["-C", info.git_root, "worktree", "prune"], None)
        self._run(["-C", info.git_root, "branch", "-D", info.branch], None)
        if not removed:
            return False, disk_msg
        if not rm.ok:
            tail = (rm.stderr or rm.stdout).strip().splitlines()
            detail = tail[-1] if tail else f"worktree remove --force exit {rm.returncode}"
            return False, f"ลบไฟล์ออกจาก {info.path} แล้ว แต่ git ยังลบ metadata ไม่ได้ ({detail})"
        repair_note = repair_editable_pth_if_stale(info.git_root, info.path)
        if leftover:
            note = f"ไฟล์บางส่วนค้างที่ {leftover} ลบเองทีหลังได้"
            return True, f"{note} · {repair_note}" if repair_note else note
        return True, repair_note


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


DirtyPathFingerprint = tuple[str, int | None, int | None]
DirtyTreeSnapshot = dict[str, DirtyPathFingerprint]


def _parse_porcelain_entries(porcelain: str) -> list[tuple[str, str]]:
    """Return ``(XY, repo-relative path)`` pairs from porcelain v1."""
    entries: list[tuple[str, str]] = []
    if "\0" in porcelain:
        fields = porcelain.split("\0")
        index = 0
        while index < len(fields):
            field = fields[index]
            if not field:
                index += 1
                continue
            status = field[:2] if len(field) >= 2 else field.strip()
            path = field[3:] if len(field) > 3 else field.strip()
            if path:
                entries.append((status, path))
            # In -z form a rename/copy stores `to\0from\0` (no arrow). Keep
            # the destination, matching the legacy line parser, and skip the
            # source field before reading the next status record.
            if "R" in status or "C" in status:
                index += 1
            index += 1
        return entries
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        status = line[:2] if len(line) >= 2 else line.strip()
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip()
        if rest:
            entries.append((status, rest))
    return entries


def parse_porcelain_paths(porcelain: str) -> list[str]:
    """Extract changed file paths from ``git status --porcelain`` output.

    Pure — no I/O. Accepts line-based short format and the unquoted ``-z``
    form used by #251's metadata snapshots. A rename keeps only the NEW path,
    matching what a diffstat would show for the same change. Used by shared-
    tree digest facts to union "files touched" with committed diffstat paths.
    """
    return [path for _status, path in _parse_porcelain_entries(porcelain)]


# Cap on how many files a single collapsed untracked-dir entry may expand
# into via a scoped `-uall` rerun. A dir this large is very likely
# node_modules/ or similar that escaped .gitignore rather than real pane
# output — walking+stat'ing thousands of files defeats the point of only
# fingerprinting paths git already flagged dirty, so past the cap we fall
# back to the pre-expansion "always changed" folder-level entry instead.
_DIR_EXPAND_CAP = 2000


def _expand_dir_entry(
    runner: GitRunner, git_root: str, dir_path: str, cap: int
) -> list[tuple[str, str]] | None:
    """Re-run status scoped to *dir_path* with ``-uall`` to list its files.

    Returns ``None`` on any git failure or when the expansion exceeds *cap*
    entries — both signal the caller to fall back to treating the whole
    directory as a single always-changed entry. An empty list is a genuine
    (if unusual) "nothing there" result and is returned as-is.
    """
    expanded, _extra = _expand_dir_entries(runner, git_root, [dir_path], cap)
    return expanded.get(dir_path)


def _expand_dir_entries(
    runner: GitRunner, git_root: str, dir_paths: list[str], cap: int
) -> tuple[dict[str, list[tuple[str, str]] | None], list[tuple[str, str]]]:
    """Batch form of :func:`_expand_dir_entry` — ONE git process for all dirs.

    #291: the per-directory version spawned one `git status` per collapsed
    `?? dir/` entry, serially, on whichever thread called it — which for the
    `done`/`assign` digest is the Qt main thread (`cli_server._dispatch` runs
    handlers inline). Each spawn measured 29-42 ms on the reference Windows
    box even against a clean tree, so a working copy with a dozen untracked
    directories froze the whole cockpit for over a second on every `takkub
    done`. Git accepts many pathspecs in one invocation, so the cost becomes
    one process regardless of how many directories are involved.

    Attribution back to each directory is by path prefix, which is exactly how
    git reports scoped results (every returned path lives under the pathspec
    that matched it). The per-directory *cap* is applied after attribution, so
    an oversized directory still degrades to ``None`` on its own without
    dragging its siblings down with it.

    Returns ``(per_directory, unattributed)``. Anything git reports that sits
    under none of the pathspecs is handed back rather than dropped — it is
    still a real dirty path, and silently losing it would turn a batching
    optimisation into a correctness change.
    """
    if not dir_paths:
        return {}, []
    res = runner(
        ["-C", git_root, "status", "--porcelain", "-z", "-uall", "--", *dir_paths],
        None,
    )
    if not res.ok:
        return dict.fromkeys(dir_paths), []
    entries = _parse_porcelain_entries(res.stdout)
    if len(dir_paths) == 1:
        # Single pathspec: no attribution to do, and skipping it keeps this
        # byte-identical to the pre-batching behaviour.
        only = dir_paths[0]
        return {only: (None if len(entries) > cap else entries)}, []
    buckets: dict[str, list[tuple[str, str]]] = {path: [] for path in dir_paths}
    unattributed: list[tuple[str, str]] = []
    # Longest prefix first: with both `a/` and `a/b/` in the pathspec list, a
    # file under `a/b/` belongs to `a/b/`, not to `a/`.
    ordered = sorted(dir_paths, key=len, reverse=True)
    for status, path in entries:
        for dir_path in ordered:
            if path.startswith(dir_path):
                buckets[dir_path].append((status, path))
                break
        else:
            unattributed.append((status, path))
    return {
        dir_path: (None if len(bucket) > cap else bucket) for dir_path, bucket in buckets.items()
    }, unattributed


def _lstat_fingerprint(root: Path, path: str) -> tuple[int | None, int | None]:
    try:
        stat_result = (root / Path(path)).lstat()
    except OSError:
        return None, None
    return stat_result.st_mtime_ns, stat_result.st_size


def snapshot_porcelain_paths(
    git_root: str,
    porcelain: str,
    runner: GitRunner | None = None,
    *,
    dir_expand_cap: int = _DIR_EXPAND_CAP,
) -> DirtyTreeSnapshot:
    """Fingerprint only paths already reported dirty by git.

    The tuple is ``(XY status, mtime_ns, size)``.  Metadata equality is used,
    never ordering, because timestamp epochs/resolution differ across Windows
    and macOS filesystems.  ``lstat`` avoids following a symlink outside the
    checkout.  A missing path (normally a deletion) is represented by two
    ``None`` values rather than treated as a snapshot failure.

    #261 follow-up: a collapsed ``?? dir/`` entry is expanded to its
    file-level contents (via *runner*, scoped to just that directory) so an
    untouched pre-existing untracked folder stops being reported as changed
    on every single done just because it's present. When *runner* is
    ``None`` (pure unit-test callers with no git process available) or the
    expansion fails/exceeds *dir_expand_cap*, the entry is kept as the bare
    directory path — :func:`changed_dirty_paths` already treats any
    trailing-``/`` entry as always-changed, which is the safe (false
    positive, never false negative) fallback #261 established.
    """
    root = Path(git_root)
    snapshot: DirtyTreeSnapshot = {}
    entries = _parse_porcelain_entries(porcelain)
    # #291: resolve every collapsed directory in a single git call up front
    # instead of shelling out once per directory inside the loop below.
    expansions: dict[str, list[tuple[str, str]] | None] = {}
    if runner is not None:
        collapsed = [path for _status, path in entries if path.endswith("/")]
        if collapsed:
            expansions, extra = _expand_dir_entries(runner, git_root, collapsed, dir_expand_cap)
            for extra_status, extra_path in extra:
                mtime_ns, size = _lstat_fingerprint(root, extra_path)
                snapshot[extra_path] = (extra_status, mtime_ns, size)
    for status, path in entries:
        if path.endswith("/") and runner is not None:
            expanded = expansions.get(path)
            if expanded is not None:
                for sub_status, sub_path in expanded:
                    mtime_ns, size = _lstat_fingerprint(root, sub_path)
                    snapshot[sub_path] = (sub_status, mtime_ns, size)
                continue
        mtime_ns, size = _lstat_fingerprint(root, path)
        snapshot[path] = (status, mtime_ns, size)
    return snapshot


def changed_dirty_paths(baseline: DirtyTreeSnapshot, current: DirtyTreeSnapshot) -> list[str]:
    """Paths whose dirty state or cheap metadata changed since assign.

    The symmetric comparison intentionally includes a baseline-dirty path
    that disappeared (restored, removed, or committed) as well as a newly
    dirty path.  An unchanged pre-existing untracked screenshot therefore
    disappears from the pane report instead of being attributed to it.

    #261: ``git status`` (without ``-uall``) collapses an untracked directory
    down to its outer folder name (``?? new_folder/``), so a file created or
    edited deep inside an already-existing untracked directory never touches
    that folder's own mtime/size — metadata equality would silently drop a
    real edit (false negative). Any entry ending in ``/`` is therefore always
    reported as changed whenever it's present in either snapshot, trading a
    possible false positive (dir listed with nothing new inside) for never
    missing a real one — matching the project rule that confident-looking
    wrong data is worse than no data.
    """
    changed: set[str] = set()
    for path in baseline.keys() | current.keys():
        if path.endswith("/"):
            changed.add(path)
        elif baseline.get(path) != current.get(path):
            changed.add(path)
    return sorted(changed)


def summarize_diffstat(diffstat: str) -> tuple[int, list[str]]:
    """Parse `git diff --stat` output into (files_touched, top_level_dirs).

    Pure — no I/O. Reads the per-file lines (each carries a literal ``|``
    column separator; the trailing "N files changed, ..." summary line has
    none) so a proposal/digest can say WHERE a change landed without
    dumping the full stat block. Order-preserving, deduped.
    """
    dirs: list[str] = []
    files = 0
    for line in diffstat.strip().splitlines():
        if "|" not in line:
            continue
        files += 1
        path = line.split("|", 1)[0].strip()
        top = path.split("/", 1)[0] if "/" in path else path
        if top and top not in dirs:
            dirs.append(top)
    return files, dirs


def build_merge_proposal(
    role: str,
    info: WorktreeInfo,
    commits: int,
    diffstat: str,
    *,
    dirty: bool = False,
    uncommitted: int = 0,
    merge_conflicts: bool | None = None,
) -> str:
    """Lead-facing PROPOSAL when an isolated pane finishes with commits to merge.

    Never auto-merges — mirrors the cockpit's propose-then-fire doctrine (same
    as the verify-fail handoff). The worktree is kept until the Lead merges.

    #244 (real near-miss, twice in one night): commits > 0 does NOT mean
    "ready to merge" — the branch can carry accepted commits AND still hold
    fresh uncommitted work on top, and this used to unconditionally open
    with "N commit พร้อม merge กลับ base" regardless. `dirty`/`uncommitted`
    gate the readiness claim and demote the merge command out of the first
    actionable step; `merge_conflicts` (``None`` when undetermined —
    see :meth:`WorktreeManager.merge_conflicts_with_base`) reports whether a
    3-way merge against the CURRENT base would conflict.
    """
    stat = diffstat.strip() or "(diffstat ว่าง)"
    files_touched, top_dirs = summarize_diffstat(diffstat)
    dirs_note = f" ({', '.join(top_dirs[:5])})" if top_dirs else ""
    header = (
        f"🌿 [{role} worktree] ทำงานบน branch `{info.branch}` (isolated) — "
        f"{commits} commit ahead ของ base"
    )
    if dirty:
        readiness = (
            f"⚠ ยังมี {uncommitted} ไฟล์ที่ยังไม่ commit ใน worktree — "
            "ของจริงอาจยังไม่อยู่ใน branch — ยังไม่พร้อมให้ merge"
        )
    elif merge_conflicts is True:
        readiness = "⚠ merge-tree เจอ conflict กับ base ปัจจุบัน — ต้อง resolve ก่อน merge"
    elif merge_conflicts is False:
        readiness = f"✅ {commits} commit พร้อม merge กลับ base (merge-tree clean กับ base ปัจจุบัน)"
    else:
        readiness = f"{commits} commit — merge-tree ตรวจสถานะไม่ได้ (unknown) · review diff ก่อน merge"
    lines = [
        header,
        readiness,
        "",
        f"ไฟล์ที่แตะ: {files_touched} ไฟล์{dirs_note}",
        "",
        f"diffstat:\n{stat}",
        "",
        "เสนอ merge (propose-then-fire, ห้าม auto):",
        f"1. review: `git -C {info.git_root} diff {info.base_sha}..{info.branch}`",
    ]
    if dirty:
        lines.append(f"2. ให้ pane commit ให้ครบก่อนที่ {info.path} — merge/cleanup รอหลังจากนั้น")
    else:
        lines.append(f"2. merge:  `git -C {info.git_root} merge --no-ff {info.branch}`")
        lines.append(
            f"3. cleanup: `git -C {info.git_root} worktree remove {info.path}` "
            f"แล้ว `git -C {info.git_root} branch -d {info.branch}`"
        )
    lines.append("worktree ยังอยู่จนกว่าจะ merge — อย่าลบก่อน · render proposal ให้ user confirm ก่อน fire")
    return "\n".join(lines)
