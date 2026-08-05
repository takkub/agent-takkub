"""Cockpit-managed shared dev-tool config.

Some dev tools should follow Lead into every project tab without
requiring per-project `.claude/settings.json` edits:

  * **Pordee** — handled at the plugin layer via `_SAFE_PLUGINS` in
    orchestrator.py, no settings file involved.
  * **Browser MCPs** — playwright + chrome-devtools are injected into
    every pane via `runtime/shared-mcp.json` so smoke tests, UX checks,
    and crawls are available from any project's Lead without per-project
    wiring.
  * **graft** — a code-intelligence MCP (symbol search / call-graph /
    file-API skeletons over the project's `graft/` graph) is injected the
    same way, into every role that reads code (see `_ROLE_MCP_POLICY`).
    Separate dict/functions from the browser MCPs (`GRAFT_MCP`,
    `ensure_graft_mcp`, `warm_graft_mcp`) so browser-MCP tests/behavior
    stay untouched, but the same protection (can't be overridden/removed
    via `add_mcp_server`/`remove_mcp_server`, wins over a same-named user
    MCP) applies via `MANAGED_MCP_NAMES`.
  * **User MCPs** — allowlisted entries from the user's own `~/.claude.json`
    mcpServers are merged into `runtime/shared-mcp.json` so every cockpit
    pane inherits them automatically without manual setup. Browser MCPs
    (playwright, chrome-devtools) take precedence on name collision.
    Credential-bearing entries and entries without a `type` field are skipped.
  * **rtk hook** — still per-project (the PreToolUse Bash hook lives
    in `.claude/settings.json`). Use the `⚡ Install rtk` button to add
    it to a specific project.

The shared-mcp.json file lives under `runtime/` (gitignored). The
cockpit writes it at startup via `ensure_browser_mcps()` +
`ensure_user_mcps()` and every claude spawn receives it via `--mcp-config`.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import RUNTIME_DIR
from .graft_store import graft_cli_path, graph_store_dir, has_completed_build, staging_dir_for
from .pane_tools_policy import effective_mcps
from .worktree_manager import worktree_root

_log = logging.getLogger(__name__)

SHARED_MCP_FILE = RUNTIME_DIR / "shared-mcp.json"


def _write_private_mcp_json(path: pathlib.Path, data: dict) -> None:
    """Write an MCP config owner-only on POSIX; Windows uses profile ACLs."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if os.name == "nt":
        path.write_text(payload, encoding="utf-8")
        return

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


# Browser MCPs that the cockpit forces into every pane so smoke tests,
# UX checks, and crawls are available from any project's Lead. These
# are vanilla npx-stdio servers with no auth, so we hard-code their
# config rather than asking the user to wire them up per project.
#
# Why ship them via shared-mcp.json instead of letting claude read
# them from the user's ~/.claude.json:
#   The cockpit launches every pane with `--setting-sources project,local`
#   to dodge claude-obsidian's crashing SessionStart hook. That flag
#   also blocks user-level mcpServers from loading, so even though the
#   user has `playwright` + `chrome-devtools` registered in
#   ~/.claude.json, panes don't see them. Folding the configs into the
#   cockpit's --mcp-config restores them without re-opening the
#   user-level settings can-of-worms.
# Versions pinned 2026-05-17 — the latest tags on npm at the time
# `BROWSER_MCPS` was authored. Pinning matters because `@latest` makes
# npx hit the npm registry on every spawn to resolve the dist-tag,
# which can take long enough on a cold Windows machine to blow past
# claude code's MCP startup window — the server then shows up as
# "not connected" and the user has to retry. With a literal version
# string, npx checks the local cache first and skips the registry
# round-trip when the package is already there.
#
# Bump these when you want to take a new release. Recipe: `npm view
# @playwright/mcp version` and `npm view chrome-devtools-mcp version`,
# update here, ship a commit, then call `ensure_browser_mcps` on next
# boot (it only adds missing names, so an explicit version bump needs
# the old entry to differ from the new desired config — changing the
# version string is enough to trigger an update).
_PLAYWRIGHT_MCP_VERSION = "0.0.75"
_CHROME_DEVTOOLS_MCP_VERSION = "0.26.0"

BROWSER_MCPS: dict = {
    "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", f"@playwright/mcp@{_PLAYWRIGHT_MCP_VERSION}"],
        "env": {},
    },
    "chrome-devtools": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", f"chrome-devtools-mcp@{_CHROME_DEVTOOLS_MCP_VERSION}"],
        "env": {},
    },
}


# graft (https://github.com/NanoNets/graft) — code-intelligence MCP: symbol
# search, call-graph tracing, and per-file API skeletons over the project's
# prebuilt `graft/` graph. Same rationale as BROWSER_MCPS (vanilla npx-stdio,
# no auth, so cockpit hard-codes it rather than asking each project to wire
# it up), but kept as its OWN dict/functions rather than folded into
# BROWSER_MCPS: `ensure_browser_mcps()` writes EXACTLY BROWSER_MCPS's names
# on a fresh install and tests assert that — mixing graft in would break
# that invariant for a tool that isn't a browser.
#
# `graft mcp` serves the graph over stdio. `GRAFT_MCP` below holds the bare
# template with NO `--dir`; `browser_profile_mcp_config_path` templates a
# per-pane `--dir <store>` in front of the `mcp` subcommand at spawn time
# (mirroring its browser-profile `--user-data-dir` dance), where *store* is
# `graft_store.graph_store_dir(<pane's own cwd>)` — the SAME external,
# hash-keyed location `graft_autobuild.py` builds into (#146 follow-up: the
# graph must never live inside the target repo, see graft_store.py's module
# docstring). A pane whose provider bridge has no cwd to template with (or
# whose role has no cwd-aware bridge) falls back to the untemplated form,
# which resolves relative to npx's inherited cwd same as before.
#
# Confirmed empirically 2026-08-05 (this task, direct stdio JSON-RPC probe):
# `graft mcp` pointed at a store with no graph built yet does NOT crash or
# hang — a tool call returns a graceful "no matching nodes ... try `graft
# build`" text result (isError: false). So a project that hasn't run `graft
# build` yet just gets empty-but-valid answers, never a wedged/crashed pane.
#
# Pinned to the version docs/audit/2026-08-05-graft-pilot.md evaluated —
# see _PLAYWRIGHT_MCP_VERSION's comment above for why pinning (not @latest)
# matters for cold-npx-spawn latency.
_GRAFT_MCP_VERSION = "0.8.2"

GRAFT_MCP: dict = {
    "graft": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", f"@nanonets/graft@{_GRAFT_MCP_VERSION}", "mcp"],
        "env": {},
    },
}


def warm_browser_mcps() -> None:
    """Pre-warm the npx cache for the browser MCPs at cockpit boot.

    First-call latency for `npx -y @playwright/mcp@<v>` is high enough
    on a cold Windows machine to blow past claude code's MCP startup
    window, which is the failure mode behind "Playwright MCP ยังไม่
    connect" reports from the user. Pinning the version (already
    shipped) is half the cure; this is the other half — kick each
    server in a background daemon thread so npx has the tarball
    extracted and the entrypoint resolved by the time claude's first
    `mcp__playwright__*` call lands.

    Implementation: spawn each MCP with stdin closed (DEVNULL). The
    server starts, reads EOF on stdin, and exits cleanly within a
    second or two. We don't care about the output — the side effect
    is the npx cache. A 30 s timeout caps the worst case (slow npm
    registry / first download); errors are swallowed so a network
    blip never blocks cockpit boot.

    Daemon threads so cockpit shutdown doesn't wait on them.

    Guarded by TAKKUB_SKIP_MCP_WARM (any truthy value): every Orchestrator()
    construction calls this, so a full pytest run building dozens of
    Orchestrators would otherwise spawn dozens of real `npx @playwright/mcp`
    + `npx chrome-devtools-mcp` processes that outlive individual tests and
    pile up (#91 — CPU idle 0% mid-suite). conftest.py sets the env var for
    every test; the check lives here (not just at the caller) so no import
    path can bypass it.
    """
    if os.environ.get("TAKKUB_SKIP_MCP_WARM", "").strip() not in ("", "0"):
        _log.debug("warm_browser_mcps: skipped (TAKKUB_SKIP_MCP_WARM set)")
        return

    npx = shutil.which("npx")
    if npx is None:
        _log.debug("warm_browser_mcps: npx launcher not found on PATH")
        return

    def _warm_one(argv: list[str]) -> None:
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except Exception:
            pass

    for name, cfg in BROWSER_MCPS.items():
        argv = [npx if cfg["command"] == "npx" else cfg["command"], *cfg["args"]]
        threading.Thread(
            target=_warm_one,
            args=(argv,),
            name=f"warm-{name}",
            daemon=True,
        ).start()


def warm_graft_mcp() -> None:
    """Pre-warm the npx cache for the graft MCP at cockpit boot — same
    rationale and same TAKKUB_SKIP_MCP_WARM guard as `warm_browser_mcps`
    (avoid first-call npx/registry latency blowing past a provider's MCP
    startup window). Kept as its own function so #91's browser-warm
    regression tests (asserting exactly 2 spawned processes) stay accurate.
    """
    if os.environ.get("TAKKUB_SKIP_MCP_WARM", "").strip() not in ("", "0"):
        _log.debug("warm_graft_mcp: skipped (TAKKUB_SKIP_MCP_WARM set)")
        return

    npx = shutil.which("npx")
    if npx is None:
        _log.debug("warm_graft_mcp: npx launcher not found on PATH")
        return

    def _warm() -> None:
        try:
            subprocess.run(
                [npx, *GRAFT_MCP["graft"]["args"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except Exception:
            pass

    threading.Thread(target=_warm, name="warm-graft", daemon=True).start()


def shared_mcp_config_path() -> str | None:
    """Absolute path to the shared MCP config file if it exists and has
    at least one MCP server entry. Returned to the orchestrator's argv builder."""
    if not SHARED_MCP_FILE.is_file():
        return None
    try:
        data = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers") or {}
    if not servers:
        return None
    return str(SHARED_MCP_FILE)


def shared_mcp_config_path_for_role(role: str) -> str | None:
    """Role-aware MCP config path. Returns the per-role variant if the role
    has a policy entry (from pane-tools.json or built-in) and its variant
    has servers in it; falls back to the master shared-mcp.json only when
    the role has NO policy anywhere (`role_mcp_allowlist` returns `None`).
    A role WITH a policy but no generated variant file gets `None` (skip
    --mcp-config), never the master — see `role_mcp_allowlist`'s docstring
    for why a policy with nothing to grant must not become a full grant.

    Why: lets the orchestrator send each claude pane only the MCPs that
    role actually uses, cutting browser-MCP schemas (~12-16k tokens) out
    of panes that never call them.
    """
    # Check if role has an override or built-in policy. None = no policy
    # anywhere → master passthrough. An EMPTY set is a real policy ("this
    # role gets no MCPs") and must go through the variant path so the empty
    # variant returns None (skip --mcp-config) — `if allowed:` would flip
    # that into the full master config.
    allowed = role_mcp_allowlist(role)
    if allowed is not None:  # role has policy (override or built-in)
        variant = _role_variant_path(role)
        if not variant.is_file():
            # A policy exists (built-in, pane-tools.json override, or the
            # "registered role with no entry → deny" default
            # `role_mcp_allowlist` now applies — docs/reviews/2026-08-05-
            # graft-mcp-security.md M1) but `_write_role_variants` never
            # generated a file for it, e.g. because it isn't in
            # `_ROLE_MCP_POLICY` or `pane-tools.json` at all. There is
            # nothing to grant here — falling back to the master file would
            # undo the deny-by-default this policy exists to express.
            return None
        try:
            data = json.loads(variant.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A role policy exists, so a corrupt variant must fail closed.
            # Falling back to the master would grant every shared MCP.
            return None
        servers = data.get("mcpServers") or {}
        if servers:
            return str(variant)
        # Empty allowlist intersection → no MCPs for this role: signal
        # "skip --mcp-config" by returning None.
        return None
    return shared_mcp_config_path()


# Each browser MCP's canonical "user data dir" CLI flag, taken from its own
# --help: @playwright/mcp uses kebab `--user-data-dir`; chrome-devtools-mcp
# documents camelCase `--userDataDir`. (yargs would also accept the kebab alias
# for chrome-devtools, but we hand each tool its documented form so the profile
# override never depends on camel-case expansion being enabled.)
_PROFILE_FLAG: dict[str, str] = {
    "playwright": "--user-data-dir",
    "chrome-devtools": "--userDataDir",
}

# Chromium "singleton" guard files live at the root of a user-data-dir and are
# what raise "profile is already in use / locked". A hard-killed shard (cockpit
# force-restart, watchdog os._exit, ConPTY freeze kill) leaves them behind; on
# Windows they don't self-recover, so a stale set would wedge the SAME shard's
# next run — re-introducing #39 one layer down. We best-effort clear them when
# (re)generating a shard config; the pane isn't alive yet, so no live browser
# owns the profile.
_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_singleton_locks(profile_dir: pathlib.Path) -> None:
    for fname in _SINGLETON_LOCK_FILES:
        try:
            (profile_dir / fname).unlink(missing_ok=True)
        except OSError:
            pass  # a leftover lock is recoverable; crashing here is not


def browser_profile_mcp_config_path(
    base_role: str, shard_idx: int | None, project: str, cwd: str | None = None
) -> str | None:
    """Per-pane-templated MCP config: identical to the role variant, but

      * each browser MCP (playwright, chrome-devtools) gets a PERSISTENT
        per-pane user-data-dir — via that browser's own profile flag
        (``--user-data-dir`` for playwright, ``--userDataDir`` for
        chrome-devtools);
      * the graft MCP (if present for this role) gets a per-pane ``--dir
        <store>`` pointed at *cwd*'s own externalized graph store (#146
        follow-up — see ``graft_store.py``'s module docstring for why the
        graph must never live inside the target repo) — but ONLY once that
        store holds a COMPLETED build. Otherwise the server is dropped from
        this pane's config entirely (H3, 2026-08-05 cross-OS audit): the
        graft MCP's own server-instruction block tells an agent "this repo
        is indexed... prefer these tools over grep/read", and an unbuilt or
        CLI-missing store answers every query with a graceful-looking empty
        result — an agent following those instructions treats that as proof
        a symbol doesn't exist instead of "not indexed yet". Same gate drops
        it for a worktree-isolated pane's cwd (M3): `graft_autobuild.py`
        never builds a worktree checkout's graph (see that module's
        docstring), so injecting the server there would ALWAYS hit this
        same false-negative trap, for the pane's entire lifetime.

        The templated args ALSO carry the persistent staging mirror
        (`graft_store.staging_dir_for(cwd)`) as the explicit positional
        `dir` after `mcp` (H1 follow-up, 2026-08-06) — without it, `graft
        mcp` defaults that root to "." (the pane's own, unfiltered cwd), and
        graft's own per-query freshness check (`ensureFreshGraph`) then
        rebuilds the graph from THAT unfiltered root on literally the pane's
        first tool call, re-inflating the store with everything
        `graft_autobuild.py`'s git-nonignored staging was built to keep out.
        See `graft_store.staging_dir_for`'s docstring for the full mechanism
        and `graft_autobuild.py`'s module docstring for how this was found.

    Browser-profile wins:

      * the browser **remembers its session/cookies across runs** instead of
        starting from playwright's default ephemeral temp profile — so a logged-in
        QA pane stays logged in next time;
      * parallel fan-out shards (``assign --shards N``) don't collide on one Chrome
        profile lock (#39 — only shard #1 could drive the browser, the rest hit
        "profile locked by another shard").

    The browser profile dir is keyed per (project, base_role[, shard], browser).
    ``shard_idx`` is None for a normal (non-fan-out) pane and an int for a shard,
    which is the only difference between the two callers — both get a persistent
    isolated profile. The graft store, by contrast, is keyed by *cwd* itself
    (``graft_store.graph_store_dir``) — a pane's code graph is a property of
    which directory it's reading, not of which role/shard is reading it, so two
    roles sharing one cwd correctly share one store.

    Other MCPs pass through untouched. Returns the path to a generated
    ``shared-mcp-<project>-<role>[-shard<N>].json``; falls back to the plain
    role-variant path when there is nothing to template (no browser MCP and no
    graft-with-cwd for this role) or on any read/write error (an untemplated
    config still beats no MCPs at all).
    """
    base_path = shared_mcp_config_path_for_role(base_role)
    if base_path is None:
        return None
    try:
        data = json.loads(pathlib.Path(base_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base_path
    servers = data.get("mcpServers") or {}
    browser_names = [n for n in servers if n in _BROWSER_MCP_NAMES]
    has_graft = "graft" in servers and cwd
    if not browser_names and not has_graft:
        return base_path  # nothing this role's config needs templated

    # Sanitize the project namespace for use in file/dir names.
    safe_project = re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"
    shard_suffix = f"-shard{shard_idx}" if shard_idx is not None else ""
    profiles_root = SHARED_MCP_FILE.parent / "browser-profiles"
    for name in browser_names:
        flag = _PROFILE_FLAG.get(name, "--user-data-dir")
        cfg = dict(servers[name])
        args = list(cfg.get("args") or [])
        if flag in args:
            continue  # idempotent — already templated
        # Per (project, role[, shard], browser) profile dir — distinct browsers get
        # distinct dirs too (playwright Chromium vs chrome-devtools Chrome would
        # otherwise lock each other).
        profile_dir = profiles_root / f"{safe_project}-{base_role}{shard_suffix}-{name}"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Don't go silent — a path-too-long (Windows MAX_PATH) or permission
            # failure is the only signal that the profile won't isolate.
            _log.warning("browser_profile_mcp_config_path: could not create %s: %s", profile_dir, e)
        _clear_stale_singleton_locks(profile_dir)
        cfg["args"] = [*args, flag, str(profile_dir)]
        servers[name] = cfg

    if has_graft:
        target = pathlib.Path(cwd)
        store = graph_store_dir(target)  # normalizes/expands/resolves internally
        under_worktree = False
        try:
            resolved_target = target.expanduser().resolve()
            wt_root = worktree_root(project).resolve()
            under_worktree = resolved_target == wt_root or wt_root in resolved_target.parents
        except OSError:
            pass
        if under_worktree or graft_cli_path() is None or not has_completed_build(store):
            # H3/M3: never hand an agent a graft MCP that can only answer
            # graceful-but-wrong empties — see the docstring above. The
            # store itself (and its manifest) is entirely `_run_build`'s
            # responsibility (`graft_autobuild.py`); by the time
            # `has_completed_build` is true, both already exist.
            del servers["graft"]
        else:
            cfg = dict(servers["graft"])
            args = list(cfg.get("args") or [])
            if "--dir" not in args:  # idempotent — already templated
                # Global `--dir` must precede the subcommand (verified against
                # the real CLI, see graft_store.py) — insert right before "mcp"
                # rather than appending blindly, so future extra subcommand
                # args stay in the right position.
                try:
                    idx = args.index("mcp")
                    args = [*args[:idx], "--dir", str(store), *args[idx:]]
                except ValueError:
                    args = [*args, "--dir", str(store)]
                # Positional `dir` after "mcp": the persistent, git-filtered
                # staging mirror — NOT *cwd* itself (H1 follow-up, 2026-08-06,
                # see this function's docstring + graft_store.staging_dir_for).
                # Without this, `graft mcp`'s default `dir="."` resolves to
                # *cwd* and every query's freshness check rebuilds unfiltered
                # from it.
                args = [*args, str(staging_dir_for(target))]
                cfg["args"] = args
                servers["graft"] = cfg

    data["mcpServers"] = servers

    out = SHARED_MCP_FILE.parent / f"shared-mcp-{safe_project}-{base_role}{shard_suffix}.json"
    try:
        _write_private_mcp_json(out, data)
    except OSError:
        return base_path
    return str(out)


# Per-(project, role[, shard], browser) Chromium profile dirs created above are
# persistent on purpose: a QA pane stays logged in across runs (#39, 04ee5c6).
# But each new shard index / project / browser leaves a fresh dir behind forever,
# so runtime/browser-profiles/ grows unbounded (#42). We age-prune by mtime —
# Chromium bumps the dir mtime on every run, so mtime doubles as "last used", and
# a generous window keeps recently-used login profiles while reclaiming stale
# fan-out shards. Mirrors prune_old_transcripts() in orchestrator.py.
_BROWSER_PROFILE_RETENTION_DAYS = 14


def prune_old_browser_profiles(max_age_days: int = _BROWSER_PROFILE_RETENTION_DAYS) -> int:
    """Delete per-(project, role[, shard], browser) Chromium profile dirs under
    runtime/browser-profiles/ not used (by mtime) within *max_age_days*. Reclaims
    disk from #39 fan-out shards that otherwise accumulate forever (#42).

    Best-effort: never raises, returns the number of dirs removed. Call ONLY when
    no pane is live (e.g. at cockpit boot) — at startup no browser holds a profile
    open AND a recently-used login profile has a fresh mtime, so the age window
    keeps it. Do NOT call on pane/shard close: Windows holds the dir open while
    Chromium shuts down, and it would wipe the persistent login profile every run.
    """
    import shutil as _shutil
    import time as _time

    root = SHARED_MCP_FILE.parent / "browser-profiles"
    if not root.is_dir():
        return 0
    cutoff = _time.time() - max_age_days * 86_400
    removed = 0
    try:
        for p in root.iterdir():
            if not p.is_dir():
                continue  # leave stray files alone
            try:
                if p.stat().st_mtime < cutoff:
                    _shutil.rmtree(p, ignore_errors=True)
                    if not p.exists():  # don't count a partial delete (locked file mid-tree)
                        removed += 1
            except OSError:
                continue  # locked / MAX_PATH dir — skip, never crash startup
    except OSError:
        pass
    if removed:
        _log.info(
            "prune_old_browser_profiles: removed %d stale profile dir(s) (>%dd)",
            removed,
            max_age_days,
        )
    return removed


def ensure_browser_mcps() -> tuple[bool, str]:
    """Merge BROWSER_MCPS into runtime/shared-mcp.json if they're not
    already present. Idempotent — safe to call on every cockpit launch.

    Two startup states this has to handle without losing data:
      1. File missing — write a fresh file containing only the browser MCPs.
      2. File exists with browsers already — no-op (still returns ok).

    Returns (ok, message) for logging only; failures are non-fatal —
    panes still spawn, browser MCPs just won't be available until the
    file is healed by hand.
    """
    config: dict = {}
    if SHARED_MCP_FILE.is_file():
        try:
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt file — refuse to clobber it. The user almost
            # certainly hand-edited and broke the JSON. Surface the
            # failure but leave the file alone.
            return False, f"could not parse {SHARED_MCP_FILE}; leaving as-is"
    servers = config.setdefault("mcpServers", {})
    changed = []
    for name, server_cfg in BROWSER_MCPS.items():
        desired = json.loads(json.dumps(server_cfg))  # deep copy
        if servers.get(name) != desired:
            servers[name] = desired
            changed.append(name)
    if not changed:
        # Ensure variants exist on first boot after upgrade (master may
        # be up-to-date but variants haven't been generated yet).
        _write_role_variants()
        return True, "browser MCPs already present"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_private_mcp_json(SHARED_MCP_FILE, config)
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    _write_role_variants()
    return True, f"updated browser MCPs: {', '.join(changed)}"


def ensure_graft_mcp() -> tuple[bool, str]:
    """Merge GRAFT_MCP into runtime/shared-mcp.json if not already present.
    Idempotent — safe to call on every cockpit launch. Mirrors
    `ensure_browser_mcps` (same two startup states: file missing / already
    up to date), kept as a separate function so browser-MCP tests keep
    asserting `ensure_browser_mcps()` writes EXACTLY BROWSER_MCPS's names.

    Returns (ok, message) for logging only; failure is non-fatal — panes
    still spawn, graft just won't be available until the file is healed.
    """
    config: dict = {}
    if SHARED_MCP_FILE.is_file():
        try:
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"could not parse {SHARED_MCP_FILE}; leaving as-is"
    servers = config.setdefault("mcpServers", {})
    changed = []
    for name, server_cfg in GRAFT_MCP.items():
        desired = json.loads(json.dumps(server_cfg))  # deep copy
        if servers.get(name) != desired:
            servers[name] = desired
            changed.append(name)
    if not changed:
        _write_role_variants()
        return True, "graft MCP already present"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_private_mcp_json(SHARED_MCP_FILE, config)
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    _write_role_variants()
    return True, f"updated graft MCP: {', '.join(changed)}"


# Browser MCP names are force-injected by ensure_browser_mcps() with pinned
# versions and specific flags.  User copies of the same names are skipped so
# cockpit's version always wins.
_BROWSER_MCP_NAMES = frozenset(BROWSER_MCPS.keys())

# Every cockpit-managed dev-tool MCP name (browser MCPs + graft) — the union
# used for override/removal protection and for skipping a same-named user
# MCP. `_BROWSER_MCP_NAMES` alone stays browser-only on purpose (it also
# drives the --user-data-dir profile-isolation loop, which graft has no
# use for), so this is a separate, wider set rather than an in-place edit.
MANAGED_MCP_NAMES: frozenset[str] = _BROWSER_MCP_NAMES | frozenset(GRAFT_MCP.keys())

# Explicit allowlist of user MCP names that are safe to copy into
# runtime/shared-mcp.json by default.  Criteria: stdio servers with no
# bearer token, API-key, or inline DSN credentials.  Any name NOT in this
# set is evaluated by _has_secrets(); if that check fails the entry is
# skipped with a warning.
#
# Credential-bearing entries (bearer/API-key headers, or a DSN with inline
# `user:pass@host` credentials in args) are always skipped by _has_secrets()
# so a secret never lands in the world-readable shared runtime file.
# Emptied 2026-07-02: obsidian-vault's provider (claude-obsidian plugin) was
# uninstalled after the usage audit; no user MCP is trusted by default now.
# `takkub mcp add` / the Tools dialog are the supported install paths.
_USER_MCP_DEFAULT_ALLOW: frozenset[str] = frozenset()

# Role-aware MCP policy: which MCPs each role pane sees.
#
# Why: claude loads every tool schema from --mcp-config into the session
# context at spawn time. playwright + chrome-devtools have huge schemas
# (~24 + ~28 tools, each with full JSON parameter descriptions) that add
# 12-16k tokens to every pane regardless of whether the tools are used.
# Lead and most teammates never call browser MCPs directly — they're only
# meaningful for visual/UI work (qa smoke, critic shots, designer audit).
#
# Solution: per-role allowlist filters the master shared-mcp.json into a
# role-specific variant. Roles in this dict get only their allowed MCPs;
# roles NOT in this dict fall back to the full master file (back-compat
# for any future role we haven't classified yet).
#
# Policy rationale:
#   - lead: orchestrator only — delegates code-reading + UI work, no direct
#     browser/graft use.
#   - qa: smoke + e2e tests need playwright/chrome-devtools, plus graft to
#     navigate the codebase while writing/debugging tests.
#   - critic: visual review reads shots, may inspect runtime DOM; plus graft
#     for the code side of a review.
#   - designer: visual review only — no code-reading MCP need, so no graft
#     (matches the original playwright/chrome-devtools-only scope).
#   - reviewer/frontend/backend/mobile/devops: code roles that read/navigate
#     source get graft (2026-08-05, docs/audit/2026-08-05-graft-pilot.md);
#     they work through the dev server/shell/psql directly for everything
#     else, so no browser MCPs.
#   - codex (the role slot, not the provider): provider-native pane, not a
#     code-reading role itself — unchanged, no graft.
#   - Gemini currently has no safe session-scoped MCP adapter (#103) and
#     keeps its existing fallback regardless of what's in this table.
# obsidian-vault removed from every role 2026-07-02: the claude-obsidian
# plugin (its only provider) was uninstalled after a usage audit found 68
# calls across ~3,200 sessions. Roles with no MCPs keep an explicit EMPTY
# policy so they skip --mcp-config entirely (no schema tokens) instead of
# falling through to the master file.
_ROLE_MCP_POLICY: dict[str, frozenset[str]] = {
    "lead": frozenset(),
    "qa": frozenset({"playwright", "chrome-devtools", "graft"}),
    "critic": frozenset({"playwright", "chrome-devtools", "graft"}),
    "designer": frozenset({"playwright", "chrome-devtools"}),
    "reviewer": frozenset({"graft"}),
    "frontend": frozenset({"graft"}),
    "backend": frozenset({"graft"}),
    "mobile": frozenset({"graft"}),
    "devops": frozenset({"graft"}),
    "codex": frozenset(),
}


def _role_variant_path(role: str) -> pathlib.Path:
    """Path to the per-role MCP config variant (filtered from master).
    Derived from SHARED_MCP_FILE so test fixtures that redirect that
    constant pick up the variants automatically."""
    return SHARED_MCP_FILE.parent / f"shared-mcp-{role}.json"


def role_mcp_allowlist(role: str) -> frozenset[str] | None:
    """Return the role's effective MCP policy without collapsing its states.

    ``frozenset()`` is an explicit deny-all policy; ``None`` means the role
    name isn't one the cockpit recognizes at all (typo, stale config) and
    keeps the legacy master-config passthrough as a last resort. Provider
    adapters need this distinction even when there is no shared MCP file:
    Codex, for example, must suppress MCPs inherited from its own user config
    whenever the cockpit policy explicitly denies them (#121).

    A REGISTERED role (``roles.all_role_names()``) with no `_ROLE_MCP_POLICY`
    entry and no `pane-tools.json` override is a policy GAP, not a
    passthrough licence — `gemini`/`shell` (registered defaults) and every
    freshly created A6 custom role (`custom_roles.create_role` never writes
    a `pane-tools.json` entry) used to fall through to `None` here and
    inherit the full master `shared-mcp.json` on claude / the operator's
    whole `~/.codex/config.toml` on codex (docs/reviews/2026-08-05-graft-mcp-
    security.md M1). Deny those explicitly instead — a role import from
    `roles` at call time (not module level) to avoid a needless import-time
    coupling for every other caller of this module.
    """
    explicit = effective_mcps(role, _ROLE_MCP_POLICY.get(role))
    if explicit is not None:
        return explicit
    from .roles import all_role_names

    return frozenset() if role in all_role_names() else None


def add_mcp_server(name: str, cfg: dict, force: bool = False) -> bool:
    """Add or update an MCP server in the master shared-mcp.json.

    name: MCP name (validated against pattern [a-z0-9][a-z0-9_-]*)
    cfg: MCP config dict (type, command, args, env, etc.)
    force: if False and cfg has secrets, log warning and skip; if True,
           write despite secrets (for user opt-ins).

    Returns True on success, False on validation/I/O error.
    Blocks MANAGED_MCP_NAMES (browser MCPs + graft) from being overwritten
    (always returns False). Never raises.
    """
    from .pane_tools_policy import _validate_name

    if not isinstance(name, str) or not _validate_name(name):
        _log.warning("add_mcp_server: invalid MCP name %r", name)
        return False
    if name in MANAGED_MCP_NAMES:
        _log.warning("add_mcp_server: cannot override managed MCP %r", name)
        return False
    if not isinstance(cfg, dict):
        _log.warning("add_mcp_server: cfg for %r is not dict", name)
        return False
    if not force and _has_secrets(cfg):
        _log.warning("add_mcp_server: skipping %r — credential-bearing entry", name)
        return False

    try:
        config: dict = {}
        if SHARED_MCP_FILE.is_file():
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        servers = config.setdefault("mcpServers", {})
        servers[name] = cfg
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_private_mcp_json(SHARED_MCP_FILE, config)
        _write_role_variants()
        return True
    except OSError as e:
        _log.warning("add_mcp_server: could not write %s: %s", SHARED_MCP_FILE, e)
        return False


def remove_mcp_server(name: str) -> bool:
    """Remove an MCP server from the master shared-mcp.json.

    Blocks removal of managed MCPs (browser MCPs + graft) — returns False
    without modifying the file.
    Returns True on success, False on I/O error or if name not found.
    Never raises.
    """
    if name in MANAGED_MCP_NAMES:
        _log.warning("remove_mcp_server: cannot remove managed MCP %r", name)
        return False

    try:
        if not SHARED_MCP_FILE.is_file():
            return False
        config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        if name not in servers:
            return False
        del servers[name]
        _write_private_mcp_json(SHARED_MCP_FILE, config)
        _write_role_variants()
        return True
    except OSError as e:
        _log.warning("remove_mcp_server: could not update %s: %s", SHARED_MCP_FILE, e)
        return False


def list_master_mcps() -> dict[str, dict]:
    """Return all MCP servers from the master shared-mcp.json.

    Returns {} if file is missing or corrupt. Never raises.
    """
    if not SHARED_MCP_FILE.is_file():
        return {}
    try:
        config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        return config.get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def regen_role_variants() -> int:
    """Regenerate all per-role MCP variant files from master.

    Returns count of role variants written. Non-fatal on error (logging
    only); never raises.
    """
    _write_role_variants()
    # Count files that exist and have servers.
    from .pane_tools_policy import load_policy

    count = 0
    for role in set(_ROLE_MCP_POLICY) | set(load_policy()):
        variant = _role_variant_path(role)
        if variant.is_file():
            try:
                data = json.loads(variant.read_text(encoding="utf-8"))
                if data.get("mcpServers"):
                    count += 1
            except (OSError, json.JSONDecodeError):
                pass
    return count


def role_variant_paths(extra_roles: Iterable[str] = ()) -> list[pathlib.Path]:
    """Every per-role MCP variant file path `regen_role_variants_checked`
    may write — for callers that need to include them in a
    :class:`~.settings_management.transaction.FileTransaction` alongside
    the master file and pane-tools policy, so a partial variant regen
    rolls back together with them instead of leaving master/policy/variants
    disagreeing (HIGH-4)."""
    from .pane_tools_policy import load_policy

    roles = (
        set(_ROLE_MCP_POLICY)
        | set(load_policy())
        | {role for role in extra_roles if isinstance(role, str)}
    )
    return [_role_variant_path(r) for r in sorted(roles)]


def regen_role_variants_checked() -> tuple[bool, list[str]]:
    """Regenerate every per-role MCP variant file from the master
    shared-mcp.json, same filtering as `_write_role_variants`, but
    CHECKED: returns ``(all_ok, failed_role_names)`` instead of only
    logging a warning per failure. Lets a caller inside a
    `FileTransaction` treat a partial variant write as a transaction
    failure (roll back master/policy alongside it) rather than silently
    leaving master/policy/variants disagreeing (HIGH-4).
    """
    if not SHARED_MCP_FILE.is_file():
        return True, []
    try:
        master = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ["<master>"]
    master_servers: dict = master.get("mcpServers") or {}
    # Union of built-in roles and file-override roles: a role granted MCPs
    # only via pane-tools.json still needs its variant generated, otherwise
    # the UI/CLI edit silently never reaches a pane.
    from .pane_tools_policy import load_policy

    roles = set(_ROLE_MCP_POLICY) | set(load_policy())
    failed: list[str] = []
    for role in sorted(roles):
        allowed = effective_mcps(role, _ROLE_MCP_POLICY.get(role))
        if allowed is None:
            continue  # no policy anywhere → master passthrough, no variant
        filtered = {name: cfg for name, cfg in master_servers.items() if name in allowed}
        # An empty allowlist intentionally writes an EMPTY variant — that is
        # what makes shared_mcp_config_path_for_role return None (skip
        # --mcp-config) for the role.
        variant = {"mcpServers": filtered}
        try:
            _write_private_mcp_json(_role_variant_path(role), variant)
        except OSError as e:
            _log.warning("regen_role_variants_checked: could not write %s: %s", role, e)
            failed.append(role)
    return not failed, failed


def _write_role_variants() -> None:
    """Regenerate every per-role MCP variant file from the master
    shared-mcp.json. Called after ensure_browser_mcps/ensure_user_mcps
    mutates the master so variants stay in sync.

    Best-effort wrapper around `regen_role_variants_checked` for callers
    that don't need to know which roles failed — a missing variant for a
    role with NO policy anywhere still falls back to the master file
    (back-compat); a missing variant for a role that DOES have a policy
    (built-in, override, or the registered-role-with-no-entry deny default)
    resolves to "no MCPs", not the master — see
    `shared_mcp_config_path_for_role`.
    """
    regen_role_variants_checked()


# Patterns that indicate a credential-bearing MCP entry.  Any entry that
# matches is skipped unless it is explicitly in _USER_MCP_DEFAULT_ALLOW.
_SECRET_KEY_PARTS = frozenset(
    {
        "token",
        "secret",
        "key",
        "apikey",
        "password",
        "pass",
        "bearer",
        "credential",
        "authorization",
    }
)
_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "apikey",
    "password",
    "bearer",
    "credential",
)
_SECRET_KEY_SUFFIXES = ("key", "pass")


def _secret_shaped_key(key: object) -> bool:
    text = str(key).lower()
    parts = {part for part in re.split(r"[^a-z0-9]+", text) if part}
    compact = re.sub(r"[^a-z0-9]", "", text)
    return (
        bool(parts & _SECRET_KEY_PARTS)
        or any(marker in compact for marker in _SECRET_KEY_MARKERS)
        or compact.endswith(_SECRET_KEY_SUFFIXES)
    )


def _has_secrets(cfg: dict) -> bool:
    """Return True if *cfg* contains a credential that should not be written
    to a world-accessible shared runtime file."""

    def scan(value: object, key: object | None = None) -> bool:
        if key is not None and _secret_shaped_key(key) and value not in (None, "", False):
            return True
        if isinstance(value, dict):
            return any(scan(child, child_key) for child_key, child in value.items())
        if isinstance(value, (list, tuple)):
            return any(scan(child) for child in value)
        if isinstance(value, str):
            if re.search(r"\$\{[^}]+\}", value):
                return True
            if re.search(
                r"[?&](?:[^&#=]*(?:token|secret|api[_-]?key|password|bearer|credential)[^&#=]*)=",
                value,
                re.IGNORECASE,
            ):
                return True
        return False

    # HTTP headers are arbitrary user-controlled credential channels (Cookie,
    # X-Org-Auth, vendor-specific session headers, etc.).  Treat any populated
    # header map as sensitive instead of trying to enumerate every spelling.
    if cfg.get("type") in {"http", "sse"} and isinstance(cfg.get("headers"), dict):
        if any(value not in (None, "", False) for value in cfg["headers"].values()):
            return True

    if scan(cfg):
        return True

    args = cfg.get("args") or []
    for a in args:
        arg = str(a)
        if re.match(
            r"^--?[^=\s]*(?:token|secret|api[_-]?key|password|bearer|credential)(?:=|$)",
            arg,
            re.IGNORECASE,
        ):
            return True
        # DSN with inline credentials: scheme://user:pass@host
        if re.search(r"://[^/@\s]+:[^/@\s]+@", arg):
            return True
    return False


def mask_secrets(cfg: dict) -> dict:
    """Return a copy of *cfg* with credential-bearing values replaced by a
    masked placeholder — same patterns `_has_secrets` detects, so a UI can
    show that a server carries a credential without ever displaying it
    (SPEC.md "MCP Servers" credential handling). Never raises."""
    out = dict(cfg)
    headers = out.get("headers")
    if isinstance(headers, dict):
        out["headers"] = {
            k: ("••••••••" if _secret_shaped_key(k) else v) for k, v in headers.items()
        }
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {k: ("••••••••" if _secret_shaped_key(k) else v) for k, v in env.items()}
    args = out.get("args")
    if isinstance(args, list):
        out["args"] = [re.sub(r"(://[^/@\s]+):[^/@\s]+@", r"\1:••••••••@", str(a)) for a in args]
    return out


def restore_masked_secrets(cfg: dict, raw_existing: dict) -> dict:
    """Undo ``mask_secrets()`` for fields the caller didn't actually change.

    ``cfg`` is a draft the UI submitted after loading a masked ``get()`` and
    letting the user edit some fields — any secret value the user never
    touched still equals the placeholder ``mask_secrets(raw_existing)``
    produced. Restore those fields (env values, header values, DSN
    credentials embedded in args) to their raw value from ``raw_existing``;
    anything that differs from the masked baseline is a real edit and
    passes through untouched.

    Prevents the HIGH-1 data-loss bug: saving an unrelated field change
    (e.g. only ``command``) would otherwise clobber a stored credential
    with ``••••••••`` because the draft's env/args came from a masked
    ``get()`` and were written back verbatim. Never raises.
    """
    masked_existing = mask_secrets(raw_existing)
    out = dict(cfg)

    raw_env = raw_existing.get("env") or {}
    masked_env = masked_existing.get("env") or {}
    if isinstance(out.get("env"), dict):
        out["env"] = {
            k: (raw_env[k] if k in masked_env and v == masked_env[k] and k in raw_env else v)
            for k, v in out["env"].items()
        }

    raw_headers = raw_existing.get("headers") or {}
    masked_headers = masked_existing.get("headers") or {}
    if isinstance(out.get("headers"), dict):
        out["headers"] = {
            k: (
                raw_headers[k]
                if k in masked_headers and v == masked_headers[k] and k in raw_headers
                else v
            )
            for k, v in out["headers"].items()
        }

    raw_args = raw_existing.get("args") or []
    masked_args = masked_existing.get("args") or []
    if isinstance(out.get("args"), list):
        out["args"] = [
            (
                raw_args[i]
                if i < len(masked_args) and v == masked_args[i] and i < len(raw_args)
                else v
            )
            for i, v in enumerate(out["args"])
        ]

    return out


def default_role_mcp_policy() -> dict[str, frozenset[str]]:
    """Public read accessor for the built-in per-role MCP visibility table
    (`_ROLE_MCP_POLICY`), used by `settings_management`'s MCP repository to
    compute which roles see a given MCP absent any `pane_tools_policy`
    override."""
    return dict(_ROLE_MCP_POLICY)


def ensure_user_mcps() -> tuple[bool, str]:
    """Merge allowlisted user MCPs from ~/.claude.json into shared-mcp.json.

    Called after ensure_browser_mcps() so browser MCPs are already present
    and take precedence on name collision.

    Policy:
    - Only names in _USER_MCP_DEFAULT_ALLOW are included by default.
    - Any entry not in the default allow set AND carrying a secret (bearer
      token, API key, etc.) is skipped with a warning.
    - Browser MCP names (playwright, chrome-devtools) are never overwritten;
      user copies are skipped and logged.
    - Authorization header values are never written to logs.
    - ~/.claude.json read failure → log warning, skip silently (non-fatal).
    - shared-mcp.json corrupt → refuse to touch it.

    Returns (ok, message) for logging only; failure is non-fatal.
    """
    home = pathlib.Path.home()
    claude_json = home / ".claude.json"

    # --- read user MCPs ---
    try:
        raw = claude_json.read_text(encoding="utf-8")
        user_data = json.loads(raw)
    except FileNotFoundError:
        return True, "~/.claude.json not found; skipping user MCP merge"
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("ensure_user_mcps: could not read ~/.claude.json: %s", e)
        return True, f"skipped user MCP merge: {e}"

    # top-level mcpServers only (not per-project entries nested under `projects`)
    user_servers: dict = user_data.get("mcpServers") or {}
    if not user_servers:
        return True, "no mcpServers in ~/.claude.json; nothing to merge"

    # --- classify each entry ---
    to_merge: dict[str, dict] = {}
    skipped: list[str] = []

    for name, cfg in user_servers.items():
        if name in MANAGED_MCP_NAMES:
            skipped.append(f"{name} (cockpit-managed MCP wins)")
            continue
        if not isinstance(cfg, dict):
            skipped.append(f"{name} (not a dict)")
            continue

        in_allowlist = name in _USER_MCP_DEFAULT_ALLOW
        is_secret = _has_secrets(cfg)

        if in_allowlist and is_secret:
            _log.warning(
                "ensure_user_mcps: %r is allowlisted but carries a credential "
                "(written to runtime/shared-mcp.json). Consider rotating to a "
                "credential-free config or env-based secret.",
                name,
            )

        if not in_allowlist and is_secret:
            _log.warning(
                "ensure_user_mcps: skipping %r — credential-bearing entry not in default allowlist",
                name,
            )
            skipped.append(f"{name} (skipped: credential-bearing)")
            continue

        to_merge[name] = cfg

    if not to_merge:
        return True, f"no eligible user MCPs to merge (skipped: {', '.join(skipped) or 'none'})"

    # --- read/update shared-mcp.json ---
    config: dict = {}
    if SHARED_MCP_FILE.is_file():
        try:
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"could not parse {SHARED_MCP_FILE}; leaving as-is"

    servers = config.setdefault("mcpServers", {})

    # Prune stale entries: non-managed user MCPs no longer in current policy.
    # Log name only — never the cfg value (may contain bearer tokens).
    pruned: list[str] = []
    for name in list(servers.keys()):
        if name in MANAGED_MCP_NAMES:
            continue  # managed by ensure_browser_mcps/ensure_graft_mcp; never touch
        if name not in to_merge:
            del servers[name]
            pruned.append(name)
            _log.info("ensure_user_mcps: pruned stale entry %r", name)

    changed: list[str] = []
    for name, cfg in to_merge.items():
        desired = json.loads(json.dumps(cfg))  # deep copy
        if servers.get(name) != desired:
            servers[name] = desired
            changed.append(name)

    if not changed and not pruned:
        # Even when master is unchanged, ensure variants exist (first boot
        # after upgrade: master may already be up-to-date but variants
        # haven't been generated yet).
        _write_role_variants()
        return True, "user MCPs already up-to-date in shared-mcp.json"

    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_private_mcp_json(SHARED_MCP_FILE, config)
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    _write_role_variants()

    # log names only — never cfg values (may contain bearer tokens)
    parts: list[str] = []
    if changed:
        parts.append(f"merged: {', '.join(changed)}")
    if pruned:
        parts.append(f"pruned: {', '.join(pruned)}")
    return True, "; ".join(parts)
