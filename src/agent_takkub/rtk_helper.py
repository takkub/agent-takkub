"""RTK install helper.

Injects rtk's PreToolUse Bash hook automatically — no per-user enable toggle
any more (#515 Settings diet: "rtk on PATH = should always be used; not on
PATH = unusable either way" made the toggle a choice with only one sane
answer). `rtk_should_inject()` is a pure auto-detect: does spawn-time hook
wiring need to carry the rtk hook? Yes iff the binary is actually reachable —
`hook_wiring.ensure_hook_settings_file()` merges it into the SAME central
settings file it already passes to every claude pane via `--settings`, so
the hook reaches panes without touching any repo. `takkub doctor` reports
whether rtk is on PATH; there is nothing left to flip.

This is intentionally narrower than `rtk init --auto-patch`: we only register
the hook (the mechanism that matters), skipping the 140-line CLAUDE.md doc
append. We never look at `~/.claude/settings.json` because the cockpit skips
the user layer (orchestrator's `--setting-sources project,local` default).
"""

from __future__ import annotations

from pathlib import Path
from shutil import which

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


def rtk_hook_fragment() -> dict:
    """The PreToolUse ``Bash`` entry `hook_wiring` merges into the central
    ``--settings`` file when rtk should be injected. A fresh dict each call
    so a caller can't mutate shared state."""
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": RTK_HOOK_COMMAND}],
    }


def _archive_legacy_enabled_file() -> None:
    """One-time cleanup of the pre-#515 ``rtk-enabled.json`` toggle state —
    its value is meaningless now (rtk is auto-detected, not opted into), so
    there is nothing to carry forward, just the file itself to keep around
    per the "never delete, archive" rule."""
    from . import config

    config.archive_settings_file(config.SETTINGS_HOME / "rtk-enabled.json")


def rtk_should_inject() -> bool:
    """Whether spawn-time hook wiring should carry the rtk hook — pure
    auto-detect (#515 Settings diet): the binary being reachable is now the
    ONLY condition, there is no separate "enabled" toggle to also check."""
    _archive_legacy_enabled_file()
    return rtk_binary_available()
