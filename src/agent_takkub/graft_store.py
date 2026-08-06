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

That store lives under `DATA_HOME` — EXCEPT the one case where `DATA_HOME
== REPO_ROOT` (a dev checkout with `AGENT_TAKKUB_HOME` unset, config.py's
`_resolve_data_home`), where the cockpit's own repo is routinely also a
configured project (self-hosting/testing on itself) — a DATA_HOME-relative
store would then land INSIDE that project's own target tree, reproducing
exactly the bug this module exists to prevent, just aimed at our own repo
instead of a user's (found 2026-08-05: with the store nested inside the
target this way, `graft build` still appended a `graft-graphs/<hash>/` line
to the target's tracked `.gitignore` and wrote an un-ignoring `.ignore` at
the target root on every build — even though the CLI genuinely writes zero
files *directly* into `target` when `--dir` points somewhere truly
external). That one case falls back to `Path.home()` instead.

Using DATA_HOME as the default base (M5, 2026-08-05 cross-OS audit) matters
because `AGENT_TAKKUB_HOME` exists precisely so a user can move cockpit data
off a small/boot drive — hard-coding `Path.home()` ignored that choice and
put multi-GB graph stores (H1) right back on the drive the user moved away
from. An installed build's unset-env `DATA_HOME` already defaults to
`~/.agent-takkub` (nothing changes for it either way); the two cases that
now differ from the old hard-coded behavior are an explicit
`AGENT_TAKKUB_HOME` override and an installed build whose `DATA_HOME` was
derived from a non-default venv location — both now correctly follow.

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
import shutil
import sys
import time
from pathlib import Path

from .config import DATA_HOME, REPO_ROOT

# 16 hex chars = 64 bits of a SHA-256 digest — collision risk is irrelevant
# for the handful of distinct project paths one cockpit instance ever tracks
# (birthday bound on 64 bits needs billions of entries before 50% collision
# odds), and halving each hash segment reclaims 32 of Windows' 260-char
# MAX_PATH budget per segment — 64 total across the two nested hash dirs
# (`<instance>/<target>/...`) that sit in front of graft's own mirrored tree
# (H2: 3080 of 4954 files in one real store already exceeded 259 chars with
# the un-truncated 64-char digests, and a default Windows 11 install has
# LongPathsEnabled=0, unlike this dev machine).
_KEY_HEX_LEN = 16


def _normalize_for_key(target: Path) -> str:
    """Resolved absolute path, `~`-expanded, case-folded on a
    case-insensitive-by-default filesystem (Windows NTFS, macOS APFS/HFS+)
    so two panes pointing at the same directory via different casing hash to
    the SAME store instead of silently splitting the graph in two.

    `expanduser()` runs here — not at each of `graft_autobuild.py`'s and
    `disk_usage.py`'s own call sites — so a raw `~`-prefixed path (M2:
    `default_cwd_for_role` returns projects.json's raw string, unlike
    `lead_cwd` which absolutizes) resolves identically everywhere a key is
    computed, instead of two call sites remembering to expand it and a third
    (`shared_dev_tools.py`'s per-pane MCP templating) silently not.

    macOS case-folding (M1): `posixpath.realpath` is lexical + `readlink`
    only — it does NOT restore canonical case the way Windows' `resolve()`
    (via `GetFinalPathNameByHandle`) already does, and APFS/HFS+ default to
    case-insensitive. Without the fold, `/Users/x/proj` and `/Users/x/Proj`
    mint two different keys for what is, on the default filesystem, one
    directory — one of the two panes then reads a permanently-empty graph.
    A deliberately case-sensitive APFS volume would share one store across
    two truly-distinct same-spelling-different-case paths; that trade-off is
    accepted here the same way `_instance_key` already accepts a comparable
    one.
    """
    resolved = target.expanduser().resolve()
    s = str(resolved)
    if sys.platform in ("win32", "darwin"):
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
    return hashlib.sha256(_normalize_for_key(data_home).encode("utf-8")).hexdigest()[:_KEY_HEX_LEN]


def _graft_store_base() -> Path:
    """Where `GRAFT_STORE_ROOT` is rooted, one level above `graft-graphs`
    itself — mirrors `config.py`'s own `RUNTIME_DIR = DATA_HOME / "runtime"`
    sibling pattern: `DATA_HOME` (so `AGENT_TAKKUB_HOME` is honoured, M5),
    already correctly namespaced (an installed build's default `DATA_HOME`
    already IS `~/.agent-takkub` — appending another `.agent-takkub` segment
    on top would double-nest it). The one exception is `DATA_HOME ==
    REPO_ROOT` (see module docstring), which is NOT already namespaced under
    the user's home, so that case alone still adds the `.agent-takkub`
    segment onto a bare `Path.home()`.
    """
    if DATA_HOME == REPO_ROOT:
        return Path.home() / ".agent-takkub"
    return DATA_HOME


def _compute_store_root() -> Path:
    """Central home for every project's externalized graft graph, namespaced
    first by this cockpit instance's own identity (`_instance_key`) and then
    by a hash of the target path itself (`graph_key`) so each distinct
    target still gets its own isolated graph. See `_graft_store_base` +
    module docstring for why the base isn't simply `DATA_HOME`
    unconditionally."""
    return _graft_store_base() / "graft-graphs" / _instance_key(DATA_HOME)


def _compute_staging_root() -> Path:
    """Persistent mirror of each target's git-non-ignored files (H1
    follow-up, 2026-08-06) — a TRUE SIBLING of `GRAFT_STORE_ROOT`, never
    nested inside it or inside the target: nesting reproduces the exact
    self-ignoring-.gitignore bug `_graft_store_base`'s docstring already
    documents for `DATA_HOME == REPO_ROOT` (graft appending an un-ignoring
    `graft-graphs/<hash>/` line to the target's own tracked `.gitignore`
    whenever the store isn't *truly* external to whatever `dir` it's pointed
    at). See `staging_dir_for`'s docstring for why this directory has to
    exist and persist at all."""
    return _graft_store_base() / "graft-staging" / _instance_key(DATA_HOME)


# `GRAFT_STORE_ROOT`/`GRAFT_STAGING_ROOT` are deliberately NOT plain
# module-level constants (M5, 2026-08-05 cross-OS audit): the old
# `NAME = _compute_...()` form ran a filesystem `.resolve()` syscall as a
# SIDE EFFECT of merely importing this module, at whatever moment Python
# first imported it — before `AGENT_TAKKUB_HOME`/test env patching had
# necessarily happened, and with no way for anything to redirect it
# afterwards short of re-importing the module. Module-level `__getattr__`
# (PEP 562) computes fresh on every access instead — cheap (one `.resolve()`
# + one sha256 digest) and correct even if `DATA_HOME` is patched after this
# module first loads. Explicit `setattr(graft_store, "GRAFT_STORE_ROOT",
# ...)` (tests/conftest.py's isolated-runtime redirect) still works exactly
# as a plain constant would: `__getattr__` only fires on a MISS, so a value
# placed directly in the module's `__dict__` shadows it, and `delattr`
# (monkeypatch's teardown) restores lazy computation.
def __getattr__(name: str) -> Path:
    if name == "GRAFT_STORE_ROOT":
        return _compute_store_root()
    if name == "GRAFT_STAGING_ROOT":
        return _compute_staging_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _store_root() -> Path:
    """Current `GRAFT_STORE_ROOT` value, honouring a test's `setattr` override
    the same way an external `graft_store.GRAFT_STORE_ROOT` read would.

    Functions in THIS module cannot simply reference the bare name
    `GRAFT_STORE_ROOT` — Python resolves a bare global name via the module's
    own `__dict__`/`LOAD_GLOBAL`, which never consults `__getattr__` (that
    hook only fires for the attribute-access protocol, i.e. `module.NAME`
    from outside, or here, via `sys.modules[__name__]`). Going through the
    module object explicitly is what makes both paths agree: a monkeypatched
    value (placed directly in `__dict__`) is seen either way, and the lazy
    `__getattr__` fallback fires either way when nothing overrode it.
    """
    return sys.modules[__name__].GRAFT_STORE_ROOT


def _staging_root() -> Path:
    """`GRAFT_STAGING_ROOT` counterpart to `_store_root` — see its docstring."""
    return sys.modules[__name__].GRAFT_STAGING_ROOT


_MANIFEST_NAME = "source.json"


def graph_key(target: Path) -> str:
    """Stable, collision-safe identifier for *target*'s graph store.

    SHA-256 truncated to `_KEY_HEX_LEN` hex chars — see that constant's
    comment for why 64 bits is still far beyond any realistic collision risk
    for the handful of distinct project paths a single cockpit instance ever
    tracks, and (unlike a name-mangling encoder) needs no uniqueness
    bookkeeping at all.
    """
    return hashlib.sha256(_normalize_for_key(target).encode("utf-8")).hexdigest()[:_KEY_HEX_LEN]


def graph_store_dir(target: Path) -> Path:
    """External `graft --dir` value for *target* — never inside *target*
    itself. Callers are responsible for `mkdir(parents=True, exist_ok=True)`
    before handing this to the graft CLI; this function only computes the
    path (kept side-effect-free so it's safe to call from a hot path like
    per-pane MCP config templating, not just the build sweep)."""
    return _store_root() / graph_key(target)


def staging_dir_for(target: Path) -> Path:
    """External, PERSISTENT positional `dir` argument for `graft
    build`/`mcp`/`ask` on *target* — a git-non-ignored mirror of *target*,
    keyed by the same `graph_key` as `graph_store_dir` so the two stay
    paired without any extra bookkeeping.

    Why this has to be a real directory that OUTLIVES a single `graft
    build` call (H1 follow-up, 2026-08-06 — the bug that shipped right
    after H1's original gitignore fix): every graft query (`ask`, and each
    `mcp` tool call via `ensureFreshGraph` in graft's own `graph/refresh.js`)
    re-probes the working tree at its OWN `dir` argument and, if the graph
    looks stale relative to THAT path, silently rebuilds from it —
    unfiltered, since graft has no `.gitignore` support of its own (see
    `graft_autobuild.py`'s module docstring). Two things both have to hold
    for that probe to ever come back "clean" instead of re-triggering a
    full unfiltered rebuild:

      1. every query must be pointed at the SAME `dir` the build used —
         `graft mcp [dir]`/`graft ask <query> [dir]` both default `dir` to
         "." (the process's own cwd) when not given explicitly, which for
         an MCP pane is the pane's real, unfiltered target directory, not
         wherever the build happened to run from;
      2. that `dir` must still exist and still hold the same content next
         time — a `tempfile.mkdtemp()` staging copy deleted right after
         `_run_build` returns (the original, since-reverted design) means
         EVERY subsequent query sees "no fingerprint for this path at all"
         and rebuilds from scratch against whatever `dir` it defaulted to.

    Verified empirically against the real CLI (2026-08-06): building into a
    deleted tempdir, then querying with the default `dir="."` (the real,
    gitignored-bulk-containing target) reproduces the unfiltered refresh
    exactly — `[graft] refreshed the graph (N files changed) before
    answering`, N being every file under the target regardless of
    `.gitignore`. Building into and querying against a directory that stays
    in place with the same content produces no refresh message at all.

    Trade-off accepted here: because refresh now compares against this
    mirror instead of the live target, an edit made between resync triggers
    (boot / tab-switch / a `done()`'s debounced rebuild / a live pane's own
    throttled resync — see `graft_autobuild.py`'s 4 triggers) is invisible
    to a query until the next trigger resyncs the mirror. That is the same
    staleness window `graft_autobuild.py`'s debounce/throttle already
    accepts for the graph itself; this just extends it to the one directory
    graft's own freshness check reads from.

    Side-effect-free like `graph_store_dir` — callers create/populate it
    (`graft_autobuild._sync_staging`)."""
    return _staging_root() / graph_key(target)


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
    root = _store_root()
    if not root.is_dir():
        return []
    try:
        return [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []


def graft_cli_path() -> str | None:
    """Resolved path to the `graft` CLI, or `None` if not on PATH.

    Single choke point (H3) so `graft_autobuild.py`'s build sweep and
    `shared_dev_tools.py`'s MCP-injection gate agree on "is graft available"
    from one probe instead of two copies drifting apart. `.cmd` first: on
    Windows PATHEXT would resolve the bare name anyway, and on macOS/Linux
    the `.cmd` probe simply misses and falls through to the bare name.
    """
    return shutil.which("graft.cmd") or shutil.which("graft")


_BUILD_MARKER_NAME = "built.json"


def build_marker_path(store: Path) -> Path:
    """Path to *store*'s build-completion marker (H2)."""
    return store / _BUILD_MARKER_NAME


def mark_build_complete(store: Path) -> None:
    """Record that a `graft build` into *store* finished successfully.

    `.graph` existing is NOT proof of a *complete* build — a build killed by
    the timeout, or interrupted by a Windows MAX_PATH failure partway
    through a deep repo (H2), leaves a partial `.graph` behind that looks
    identical to a good one to a bare `.is_dir()` check. Callers must write
    this marker LAST, only after the `graft build` subprocess itself
    reports exit 0, so its mere presence is sufficient proof of completion.
    Best-effort: never raises — a failed write just means the next trigger
    retries the build, never a stuck state.
    """
    try:
        build_marker_path(store).write_text(
            json.dumps({"built_at": time.time()}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def has_completed_build(store: Path) -> bool:
    """Whether *store* holds a graph from a build that ran to completion."""
    return build_marker_path(store).is_file()
