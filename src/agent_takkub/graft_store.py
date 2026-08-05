"""External storage location for graft's code-graph (#146 follow-up).

`graft build <target>` with no `--dir` writes `.gitignore`, `.ignore`, and a
`graft/` directory straight into *target* — fine for the cockpit's own repo,
but the boot-time auto-build sweep runs this across every configured project
path (46 across 27 projects on the pilot machine), most of which are OTHER
git repos the cockpit does not own. Writing untracked files into a user's
repo the moment they open the cockpit is a real regression: the tree stops
being clean, and a distracted `git add -A` can commit cockpit-generated
files into someone else's project.

The fix routes every `graft build`/`mcp`/`ask` invocation through graft's
global `--dir <path>` flag (verified empirically 2026-08-05 against the real
CLI: `graft --dir <outside> build <target>` writes ZERO files into *target*,
and `graft --dir <outside> ask "<query>" <target>` still answers correctly
reading the externalized graph) so the graph itself lives entirely outside
every target repo.

That store MUST live under the user's home directory, never under
`DATA_HOME`: for a dev checkout `DATA_HOME == REPO_ROOT` (config.py's
`_resolve_data_home`), and the cockpit's own repo is routinely also a
configured project (self-hosting/testing on itself) — a DATA_HOME-relative
store then lands INSIDE that project's own target tree, reproducing exactly
the bug this module exists to prevent, just aimed at our own repo instead of
a user's (found 2026-08-05: with the store nested inside the target this
way, `graft build` still appended a `graft-graphs/<hash>/` line to the
target's tracked `.gitignore` and wrote an un-ignoring `.ignore` at the
target root on every build — even though the CLI genuinely writes zero files
*directly* into `target` when `--dir` points somewhere truly external).
Rooting under `Path.home()` instead is safe for both cases: an installed
build's `DATA_HOME` already defaults to `~/.agent-takkub` (nothing changes
for it), and a dev checkout's `DATA_HOME` is some unrelated repo path that
is never nested under `~/.agent-takkub`.

Keying: a target path is turned into a store dir name via a SHA-256 hash of
its resolved, OS-normalized absolute path — NOT `decode_project_dir` or any
other lossy path<->name encoding. `decode_project_dir` is documented
(memory: decode-project-dir-lossy) as unsafe for exactly this: a project
whose name contains a hyphen, underscore, dot, or space round-trips to the
wrong path, which here would silently point a pane's graft MCP at another
project's graph. A content hash of the normalized path has no such
ambiguity and needs no decoder — the mapping is one-directional by design,
which is why `write_store_manifest` exists to keep the reverse (store ->
source path) direction recoverable for humans/tooling.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .config import DATA_HOME


def _normalize_for_key(target: Path) -> str:
    """Resolved absolute path, case-folded on Windows so two panes pointing
    at the same directory via different casing (NTFS is case-insensitive)
    hash to the SAME store instead of silently splitting the graph in two."""
    resolved = target.resolve()
    s = str(resolved)
    if os.name == "nt":
        s = s.lower()
    return s


def _instance_key(data_home: Path) -> str:
    """Stable identifier for the cockpit instance whose `DATA_HOME` is
    *data_home* — namespaces the store so two cockpit instances (dev + prod,
    or two dev checkouts) never share one `graft-graphs` root.

    Deliberately per-instance, NOT shared: the single-flight guard around
    `graft build` is a `threading.Lock` + in-process dict, so it only
    serializes builds within one cockpit process. This machine routinely
    runs two cockpit instances at once (dev + prod, different ports/
    DATA_HOME), and pointing both at one shared store would let two
    independent `graft build --dir <same store>` calls race with nothing to
    stop them, silently corrupting the graph while agents keep trusting
    stale/bad reads. Sharing across instances needs a cross-process file
    lock around the build first — this key alone does not add one.
    """
    return hashlib.sha256(_normalize_for_key(data_home).encode("utf-8")).hexdigest()


# Central home for every project's externalized graft graph. Always under
# the user's home directory (see module docstring for why this must never
# be DATA_HOME-relative), namespaced first by this cockpit instance's own
# identity (`_instance_key`) and then by a hash of the target path itself
# (`graph_key`) so each distinct target still gets its own isolated graph.
GRAFT_STORE_ROOT = Path.home() / ".agent-takkub" / "graft-graphs" / _instance_key(DATA_HOME)

_MANIFEST_NAME = "source.json"


def graph_key(target: Path) -> str:
    """Stable, collision-safe identifier for *target*'s graph store.

    SHA-256 over the normalized absolute path — 256 bits of digest is far
    beyond any realistic collision risk for the handful of distinct project
    paths a single cockpit instance ever tracks, and (unlike a truncated
    hash or a name-mangling encoder) needs no uniqueness bookkeeping at all.
    """
    return hashlib.sha256(_normalize_for_key(target).encode("utf-8")).hexdigest()


def graph_store_dir(target: Path) -> Path:
    """External `graft --dir` value for *target* — never inside *target*
    itself. Callers are responsible for `mkdir(parents=True, exist_ok=True)`
    before handing this to the graft CLI; this function only computes the
    path (kept side-effect-free so it's safe to call from a hot path like
    per-pane MCP config templating, not just the build sweep)."""
    return GRAFT_STORE_ROOT / graph_key(target)


def write_store_manifest(target: Path) -> None:
    """Record *target*'s resolved source path inside its own store dir.

    The key->path mapping is one-directional (a hash cannot be decoded back
    into a path), so anything that needs to reason about stores in bulk —
    `disk_usage.py`'s report/prune, an orphan sweep after a project is
    removed from projects.json — reads this file instead. Best-effort:
    never raises. A missing manifest degrades a cleanup report to showing
    the bare hash instead of the path; it never loses the graph itself.
    """
    store = graph_store_dir(target)
    try:
        store.mkdir(parents=True, exist_ok=True)
        manifest = store / _MANIFEST_NAME
        manifest.write_text(
            json.dumps({"source": str(target.resolve())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_store_manifest(store: Path) -> str | None:
    """Best-effort source path recorded by `write_store_manifest` for a
    given store dir, or `None` if absent/unreadable/malformed."""
    manifest = store / _MANIFEST_NAME
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source = data.get("source") if isinstance(data, dict) else None
    return source if isinstance(source, str) and source else None


def iter_store_dirs() -> list[Path]:
    """Every existing per-target store directory under `GRAFT_STORE_ROOT`."""
    if not GRAFT_STORE_ROOT.is_dir():
        return []
    try:
        return [p for p in GRAFT_STORE_ROOT.iterdir() if p.is_dir()]
    except OSError:
        return []
