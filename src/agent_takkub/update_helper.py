"""Self-update helper — pure git wrapper used by the cockpit's status
bar update button.

The cockpit polls `fetch_remote()` + `local_status()` every five
minutes so the status-bar widget can flip between "Up to date" /
"Update available (N)" / "Local edits". When the user clicks the
chip, `main_window` walks through:

  local_status() -> pull_updates() -> pyproject_changed_in_pull()

and surfaces a restart-prompt. This module owns no UI — every
function returns plain data + best-effort `(ok, msg)` tuples so the
caller can compose dialogs / status messages however it wants.

Design rules:
- Read-only against the working tree (no commits, no merges, no
  resets). The one mutating call is `git pull --ff-only origin main`
  in `pull_updates`, guarded by the dirty-tree precondition.
- subprocess.run with cwd=REPO_ROOT, never shell=True, timeout
  defaults to 30 s so a stale network can't block the cockpit's Qt
  event loop forever.
- Every function swallows exceptions and surfaces them as failure
  states. The UI never crashes on a git hiccup.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import REPO_ROOT

# Official upstream for the in-cockpit "convert to git checkout" flow.
# HTTPS form, not SSH — ZIP-installed users typically don't have keys
# configured. If the cockpit gets forked, override per-installation by
# setting `TAKKUB_OFFICIAL_REPO_URL` env var or editing this constant
# before shipping the fork.
OFFICIAL_REPO_URL = "https://github.com/takkub/agent-takkub.git"


def _git(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand inside the repo root with text output.
    Caller checks returncode; this helper never raises on non-zero,
    just returns the CompletedProcess so the caller can inspect
    stderr and decide what to surface."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def is_git_repo() -> bool:
    """True when REPO_ROOT contains a `.git` directory (or git file
    for worktrees). A tarball install or a zip download won't have
    this, so the cockpit disables the update button entirely instead
    of trying to run `git fetch` on a non-repo and surfacing
    confusing errors."""
    git_dir = Path(REPO_ROOT) / ".git"
    return git_dir.exists()


def fetch_remote(timeout: float = 10.0) -> tuple[bool, str]:
    """Fetch origin/main quietly. Returns (ok, message). On
    timeout / network error / git-missing, returns
    `(False, "<short reason>")`. Caller treats failure as 'no new
    information available; show the last known status'."""
    if not is_git_repo():
        return False, "not a git repo"
    try:
        proc = _git("fetch", "--quiet", "origin", "main", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "git fetch timed out"
    except FileNotFoundError:
        return False, "git binary not on PATH"
    except Exception as e:
        return False, f"git fetch failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or "git fetch failed").strip().splitlines()
        return False, tail[-1] if tail else "git fetch failed"
    return True, "fetched"


def local_status() -> dict:
    """Snapshot of the working tree relative to `origin/main`.

    Returns a dict like:
        {
          "clean":       True/False,            # no dirty tracked files
          "ahead":       int,                   # commits on HEAD that origin lacks
          "behind":      int,                   # commits on origin that HEAD lacks
          "dirty_files": list[str],             # tracked files with local edits
          "ok":          True/False,            # False = error reading status
          "error":       str (only when ok=False),
        }

    The `dirty_files` list is what the UI shows in the "Local edits"
    warning dialog so the user knows what they'd be stomping on
    before they decide to stash + pull.
    """
    if not is_git_repo():
        return {
            "ok": False,
            "error": "not a git repo",
            "clean": False,
            "ahead": 0,
            "behind": 0,
            "dirty_files": [],
        }

    # `git status --porcelain` gives one line per modified/added/
    # untracked/etc. tracked file. Untracked files start with "??";
    # we only care about tracked-file modifications (M/A/D/R/C/U) so
    # the user's `runtime/` and `.venv/` don't pollute the list even
    # if they happen not to be gitignored on someone's machine.
    try:
        sproc = _git("status", "--porcelain")
        rproc = _git("rev-list", "--left-right", "--count", "HEAD...origin/main")
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "clean": False,
            "ahead": 0,
            "behind": 0,
            "dirty_files": [],
        }

    dirty: list[str] = []
    for line in (sproc.stdout or "").splitlines():
        if len(line) < 3:
            continue
        marker = line[:2]
        # Skip pure untracked-file entries (starts with "??"). They
        # never block a fast-forward pull and are often local scratch
        # files anyway.
        if marker == "??":
            continue
        path = line[3:].strip()
        if path:
            dirty.append(path)

    ahead = 0
    behind = 0
    if rproc.returncode == 0:
        parts = (rproc.stdout or "").strip().split()
        if len(parts) == 2:
            try:
                ahead, behind = int(parts[0]), int(parts[1])
            except ValueError:
                ahead = behind = 0

    return {
        "ok": True,
        "clean": len(dirty) == 0,
        "ahead": ahead,
        "behind": behind,
        "dirty_files": dirty,
    }


def pull_updates() -> tuple[bool, str]:
    """`git pull --ff-only origin main`. Refuses to run when the
    working tree carries local edits to tracked files; the user has
    to commit or stash first. Returns (ok, message); on success the
    message includes how many commits were merged so the UI can
    quote it back to the user."""
    status = local_status()
    if not status.get("ok"):
        return False, f"could not read git status: {status.get('error', 'unknown')}"
    if not status.get("clean"):
        files = ", ".join(status.get("dirty_files", [])[:5])
        more = (
            ""
            if len(status.get("dirty_files", [])) <= 5
            else f" (+{len(status['dirty_files']) - 5} more)"
        )
        return False, f"local edits present, refusing pull: {files}{more}"
    if status.get("behind", 0) == 0:
        return True, "already up to date"
    behind = status["behind"]
    try:
        proc = _git("pull", "--ff-only", "origin", "main", timeout=60.0)
    except subprocess.TimeoutExpired:
        return False, "git pull timed out"
    except Exception as e:
        return False, f"git pull failed: {e}"
    if proc.returncode != 0:
        # Surface the tail of stderr so the user sees the actual git
        # complaint (non-fast-forward, network error, etc.) rather
        # than a generic "pull failed".
        msg = (proc.stderr or proc.stdout or "git pull failed").strip()
        return False, msg.splitlines()[-1] if msg else "git pull failed"
    return True, f"pulled {behind} commit{'s' if behind != 1 else ''}"


def pyproject_changed_in_pull(before_sha: str, after_sha: str) -> bool:
    """True iff `pyproject.toml` appears in the diff between the two
    SHAs. Used to decide whether the restart-prompt should also tell
    the user to re-run `pip install -e .`. The cockpit can't run
    pip itself reliably from inside its own venv while shutting
    down."""
    if not before_sha or not after_sha or before_sha == after_sha:
        return False
    try:
        proc = _git("diff", "--name-only", f"{before_sha}..{after_sha}")
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    files = (proc.stdout or "").splitlines()
    return "pyproject.toml" in {f.strip() for f in files}


def init_git_repo() -> tuple[bool, str]:
    """Convert a non-git install (ZIP download / pip install / copy)
    into a proper git checkout pointing at origin/main, so the user
    can use the cockpit's update chip from then on.

    Steps:
      1. `git init` inside REPO_ROOT
      2. `git remote add origin <OFFICIAL_REPO_URL>`
      3. `git fetch origin main`
      4. `git reset --hard origin/main` — overwrites tracked files
         with the remote version.

    Safety: only `.gitignored` paths survive untouched. The cockpit's
    `.gitignore` already covers `projects.json`, `runtime/`, `.venv/`,
    `*.log`, and `AGENTS.md`, so user data persists. Any local edits
    to *tracked* cockpit files (CLAUDE.md, README, source, etc.) are
    discarded — that's the cost of converting to upstream-tracked.

    Returns (ok, message). The caller (main_window) wraps a confirm
    dialog around this to warn before invoking.
    """
    import os

    repo_url = os.environ.get("TAKKUB_OFFICIAL_REPO_URL", "").strip() or OFFICIAL_REPO_URL
    if is_git_repo():
        return False, "already a git repo — no conversion needed"
    try:
        proc = _git("init", timeout=15.0)
    except Exception as e:
        return False, f"git init failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or "git init failed").strip()
    try:
        proc = _git("remote", "add", "origin", repo_url, timeout=15.0)
    except Exception as e:
        return False, f"git remote add failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or "git remote add failed").strip()
    try:
        proc = _git("fetch", "origin", "main", timeout=60.0)
    except subprocess.TimeoutExpired:
        return False, "git fetch timed out (check network / repo url)"
    except Exception as e:
        return False, f"git fetch failed: {e}"
    if proc.returncode != 0:
        # Surface the actual git complaint — typically "Repository not
        # found" / authentication errors that the user needs to act on.
        tail = (proc.stderr or "git fetch failed").strip().splitlines()
        return False, tail[-1] if tail else "git fetch failed"
    try:
        proc = _git("reset", "--hard", "origin/main", timeout=30.0)
    except Exception as e:
        return False, f"git reset failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or "git reset --hard origin/main failed").strip()
    return True, f"linked to {repo_url} and synced to origin/main"


def current_sha() -> str:
    """Best-effort `git rev-parse HEAD` short SHA. Returns "" on any
    error so callers don't have to handle exceptions inline when
    capturing the pre-pull commit pointer."""
    if not is_git_repo():
        return ""
    try:
        proc = _git("rev-parse", "HEAD")
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def current_sha_short() -> str:
    """First 7 chars of the HEAD SHA, for status-bar display. Empty
    string when not in a git repo or git is unavailable."""
    full = current_sha()
    return full[:7] if full else ""


def current_version_describe() -> str:
    """Live `git describe` output — `<tag>-<count>-g<sha>` if any tags
    exist, otherwise just `g<sha>`. Adds `-dirty` if the working tree
    has uncommitted changes.

    Used by the status-bar version chip so the label refreshes on
    every commit without requiring `pip install -e .` to regenerate
    egg-info. Returns empty string on any git failure / non-repo so
    the caller can fall back to pyproject's static version.
    """
    if not is_git_repo():
        return ""
    try:
        proc = _git("describe", "--tags", "--always", "--dirty")
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def pyproject_will_change_on_pull() -> bool:
    """True iff `pyproject.toml` is in the diff between HEAD and
    origin/main *before* the pull happens. Used to warn the user
    upfront — without this, the pip-reinstall reminder only appears
    after the pull, by which time the cockpit has already promised
    a restart and can't easily back out.

    Returns False on any git failure (including offline / no fetch
    yet), so the caller defaults to "probably safe to skip pip".
    """
    if not is_git_repo():
        return False
    try:
        proc = _git("diff", "--name-only", "HEAD..origin/main")
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    files = (proc.stdout or "").splitlines()
    return "pyproject.toml" in {f.strip() for f in files}
