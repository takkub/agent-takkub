"""RTK install helper.

We support a one-click *quick install* of rtk's PreToolUse Bash hook into a
project's `.claude/settings.json`. This is intentionally narrower than
`rtk init --auto-patch`: we only register the hook (the mechanism that
matters), and we skip the 140-line CLAUDE.md doc append so we don't bloat
every project the cockpit touches.

Detection logic mirrors the install: a project counts as "rtk-enabled"
when its `.claude/settings.json` contains a PreToolUse entry for `Bash`
that runs `rtk hook claude`. We never look at `~/.claude/settings.json`
here because the cockpit explicitly skips the user layer (see
orchestrator.py's `--setting-sources project,local` default).
"""

from __future__ import annotations

import json
from pathlib import Path
from shutil import which

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


def is_rtk_installed(project_root: str | Path | None) -> bool:
    """True when the project's `.claude/settings.json` contains a Bash
    PreToolUse hook pointing at rtk. False on any read/parse error so the
    cockpit defaults to *offering* the install rather than hiding it."""
    if not project_root:
        return False
    path = _settings_path(Path(project_root))
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") or {}
    pre = hooks.get("PreToolUse") or []
    if not isinstance(pre, list):
        return False
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        if entry.get("matcher") != "Bash":
            continue
        for h in entry.get("hooks") or []:
            cmd = (h or {}).get("command", "")
            if isinstance(cmd, str) and RTK_HOOK_COMMAND_MARKER in cmd:
                return True
    return False


def install_rtk(project_root: str | Path) -> tuple[bool, str]:
    """Idempotently add the rtk PreToolUse Bash hook to the project's
    `.claude/settings.json`. Creates the file (and `.claude/` dir) when
    missing. Returns (ok, message).

    Refuses to install when:
      - `project_root` doesn't exist or isn't a directory
      - the rtk binary isn't on PATH
      - the existing settings.json is malformed JSON (so we don't clobber)
    """
    if not rtk_binary_available():
        return False, "rtk binary not on PATH — install it first"

    root = Path(project_root)
    if not root.is_dir():
        return False, f"project root not a directory: {project_root}"

    path = _settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return False, f"existing settings.json is malformed — edit manually: {e}"
        if not isinstance(data, dict):
            return False, "existing settings.json must be a JSON object"
    else:
        data = {}

    # Walk into hooks.PreToolUse and ensure exactly one Bash entry contains
    # an rtk hook. Other matchers / other Bash hooks are preserved.
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False, "hooks key in settings.json must be an object"

    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        return False, "hooks.PreToolUse must be an array"

    bash_entry: dict | None = None
    for entry in pre:
        if isinstance(entry, dict) and entry.get("matcher") == "Bash":
            bash_entry = entry
            break

    if bash_entry is None:
        bash_entry = {"matcher": "Bash", "hooks": []}
        pre.append(bash_entry)

    inner = bash_entry.setdefault("hooks", [])
    if not isinstance(inner, list):
        return False, "Bash hooks entry must have an array `hooks`"

    already = any(
        isinstance(h, dict) and RTK_HOOK_COMMAND_MARKER in (h.get("command") or "") for h in inner
    )
    if already:
        return True, "already installed (no changes)"

    inner.append({"type": "command", "command": RTK_HOOK_COMMAND})

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True, f"rtk hook added to {path}"
