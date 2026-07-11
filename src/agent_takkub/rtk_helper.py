"""RTK install helper.

We support a one-click enable of rtk's PreToolUse Bash hook. Historically
this wrote the hook into every project's `.claude/settings.json`, which
dirtied the user's repo (an rtk hook the cockpit added, not the user). As of
the central-home migration (docs/design/2026-07-11-central-home-audit.md,
item A3) rtk is a **personal, central** toggle instead:

- Enabling flips a flag file in `SETTINGS_HOME` (`rtk-enabled.json`). No
  project file is ever written.
- At spawn time, `hook_wiring.ensure_hook_settings_file()` merges the rtk
  PreToolUse Bash hook into the SAME central settings file it already passes
  to every claude pane via `--settings` — so the hook reaches panes without
  touching any repo. Gated by `rtk_should_inject()` so a pane never gets a
  `rtk hook claude` command when rtk isn't actually on PATH (that would make
  every Bash call fail).
- `uninstall_rtk()` scrubs the legacy per-project entry a prior cockpit build
  wrote, preserving the user's own keys.

This is intentionally narrower than `rtk init --auto-patch`: we only register
the hook (the mechanism that matters), skipping the 140-line CLAUDE.md doc
append. We never look at `~/.claude/settings.json` because the cockpit skips
the user layer (orchestrator's `--setting-sources project,local` default).
"""

from __future__ import annotations

import json
from pathlib import Path
from shutil import which

from . import config

# Marker we look for inside a hook's `command` to consider it the rtk
# hook. Picked to match what `rtk init -g` writes; using a substring match
# lets us tolerate flag variants like `rtk hook claude --ultra-compact`.
RTK_HOOK_COMMAND_MARKER = "rtk hook claude"
RTK_HOOK_COMMAND = "rtk hook claude"

# Well-known locations rtk lands in on each platform. We probe these
# directly when `shutil.which` comes up empty — typically because the
# cockpit's pythonw was launched via `start ""` which can present a
# stripped-down PATH / PATHEXT vs. the cmd that spawned it.
_FALLBACK_RTK_PATHS: tuple[Path, ...] = (
    Path.home() / "bin" / "rtk.exe",
    Path.home() / "bin" / "rtk",
    Path("/usr/local/bin/rtk"),
    Path("/opt/homebrew/bin/rtk"),
)


# Cache the resolved path once found. find_rtk_binary() runs on the Qt main
# thread on every pane spawn; each call did two PATH scans (`which`). The
# binary location doesn't move within a session, so cache the positive result
# (re-validated cheaply with one stat). We deliberately do NOT cache a negative
# result, so installing rtk mid-session is still picked up.
_RTK_BINARY_CACHE: str | None = None


def find_rtk_binary() -> str | None:
    """Resolve an absolute path to the rtk binary, or None if not present.

    Search order: PATH (`shutil.which`) for both `rtk` and `rtk.exe`, then a
    list of well-known install locations. The fallback list covers the case
    where pythonw inherits a thinner PATH than the cmd that launched it."""
    global _RTK_BINARY_CACHE
    if _RTK_BINARY_CACHE is not None and Path(_RTK_BINARY_CACHE).is_file():
        return _RTK_BINARY_CACHE
    for name in ("rtk", "rtk.exe"):
        found = which(name)
        if found:
            _RTK_BINARY_CACHE = found
            return found
    for cand in _FALLBACK_RTK_PATHS:
        if cand.is_file():
            _RTK_BINARY_CACHE = str(cand)
            return str(cand)
    return None


def rtk_binary_available() -> bool:
    """True when the `rtk` CLI is reachable, either via PATH or a known
    install location. See `find_rtk_binary` for the search order."""
    return find_rtk_binary() is not None


def _settings_path(project_root: Path) -> Path:
    return project_root / ".claude" / "settings.json"


def _enabled_flag_path() -> Path:
    """Central per-user flag file recording whether rtk is enabled.

    Under ``SETTINGS_HOME`` (``~/.takkub`` on a dev checkout, ``DATA_HOME`` on
    an installed build) — resolved at call time so tests that monkeypatch
    ``config.SETTINGS_HOME`` land it under their own tmp dir."""
    return config.SETTINGS_HOME / "rtk-enabled.json"


def rtk_hook_enabled() -> bool:
    """True when the user has enabled rtk centrally. Independent of whether
    the binary is currently on PATH — see `rtk_should_inject` for the
    spawn-time gate that also checks availability. False on any read/parse
    error so the cockpit defaults to *offering* the toggle."""
    path = _enabled_flag_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("enabled"))


def set_rtk_enabled(enabled: bool) -> None:
    """Persist the central rtk enable flag. Creates ``SETTINGS_HOME`` if
    missing; never raises on a write failure (best-effort toggle)."""
    path = _enabled_flag_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"enabled": bool(enabled)}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def rtk_hook_fragment() -> dict:
    """The PreToolUse ``Bash`` entry `hook_wiring` merges into the central
    ``--settings`` file when rtk should be injected. A fresh dict each call
    so a caller can't mutate shared state."""
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": RTK_HOOK_COMMAND}],
    }


def rtk_should_inject() -> bool:
    """Whether spawn-time hook wiring should carry the rtk hook: enabled
    centrally AND the binary is actually reachable. The availability check
    is essential — injecting `rtk hook claude` when rtk isn't on PATH would
    make every Bash tool call in the pane fail."""
    return rtk_hook_enabled() and rtk_binary_available()


def is_rtk_installed(project_root: str | Path | None = None) -> bool:
    """True when rtk is enabled centrally. `project_root` is accepted and
    ignored (kept for call-site compatibility with the old per-project
    signature — the state is now central, not per-project)."""
    return rtk_hook_enabled()


def install_rtk(project_root: str | Path | None = None) -> tuple[bool, str]:
    """Enable rtk centrally (personal, out of every repo) and scrub any
    legacy per-project hook a prior cockpit build wrote.

    Flips the central `rtk-enabled.json` flag; the hook itself is injected at
    spawn time by `hook_wiring.ensure_hook_settings_file()` via the existing
    `--settings` channel. When `project_root` is given, also removes the old
    rtk entry from `<project_root>/.claude/settings.json` (preserving the
    user's own keys) so the migration leaves no repo residue.

    Refuses only when the rtk binary isn't reachable — enabling a hook that
    would break every Bash call is never useful. Returns (ok, message).
    """
    if not rtk_binary_available():
        return False, "rtk binary not on PATH — install it first"
    set_rtk_enabled(True)
    if project_root:
        uninstall_rtk(project_root)  # best-effort legacy cleanup
    return True, "rtk enabled (central — injected via --settings, no repo files touched)"


def uninstall_rtk(project_root: str | Path) -> tuple[bool, str]:
    """Remove the legacy rtk PreToolUse Bash hook from
    `<project_root>/.claude/settings.json`, preserving every other key the
    user has. No-op (reported as success) when the file is missing or carries
    no rtk hook. Prunes now-empty `hooks`/`PreToolUse`/Bash-entry containers
    the removal leaves behind, but never deletes the file itself (other keys
    may remain, and it may be the user's own committed config). Returns
    (ok, message); malformed JSON is left untouched (edit manually)."""
    root = Path(project_root)
    path = _settings_path(root)
    if not path.is_file():
        return True, "no project settings.json (nothing to clean)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"settings.json unreadable/malformed — left untouched: {e}"
    if not isinstance(data, dict):
        return True, "settings.json not an object (left untouched)"

    hooks = data.get("hooks")
    pre = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(pre, list):
        return True, "no rtk hook present (nothing to clean)"

    changed = False
    for entry in pre:
        if not isinstance(entry, dict) or entry.get("matcher") != "Bash":
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        kept = [
            h
            for h in inner
            if not (isinstance(h, dict) and RTK_HOOK_COMMAND_MARKER in (h.get("command") or ""))
        ]
        if len(kept) != len(inner):
            changed = True
            entry["hooks"] = kept
    if not changed:
        return True, "no rtk hook present (nothing to clean)"

    # Prune empty containers so we don't leave `{"hooks": {"PreToolUse":
    # [{"matcher": "Bash", "hooks": []}]}}` skeletons behind.
    pre[:] = [
        e
        for e in pre
        if not (isinstance(e, dict) and e.get("matcher") == "Bash" and not e.get("hooks"))
    ]
    if not pre:
        hooks.pop("PreToolUse", None)
    if isinstance(hooks, dict) and not hooks:
        data.pop("hooks", None)

    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write settings.json: {e}"
    return True, f"removed legacy rtk hook from {path}"
