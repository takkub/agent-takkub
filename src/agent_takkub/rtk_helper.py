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


def rtk_binary_available() -> bool:
    """True when the `rtk` CLI is on PATH (or its Windows .exe variant)."""
    return which("rtk") is not None or which("rtk.exe") is not None


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
        isinstance(h, dict) and RTK_HOOK_COMMAND_MARKER in (h.get("command") or "")
        for h in inner
    )
    if already:
        return True, "already installed (no changes)"

    inner.append({"type": "command", "command": RTK_HOOK_COMMAND})

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True, f"rtk hook added to {path}"
