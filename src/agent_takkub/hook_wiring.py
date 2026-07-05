"""Per-pane Claude Code hook wiring — authoritative pane-state signal.

Every spawned claude pane (Lead + teammates) is given a `--settings <file>`
pointing at a static settings file that wires `Stop` and `Notification`
(matcher `idle_prompt`) to `takkub _hook`. That command reports the event back
to the orchestrator over the existing TCP socket so turn-end/idle can be
detected the instant it happens, instead of waiting on the next PTY-scraping
poll tick (`pty_session.is_at_ready_prompt()`, which stays the fallback for
non-claude panes and for any claude pane whose hook never fires).

The command is a bare `takkub _hook` (no args, no embedded JSON) so it needs
no shell quoting on either OS — `spawn_engine.py` already prepends
`config.CLI_BIN_DIR` (`REPO_ROOT/bin` in a dev checkout, the venv's own
console-script dir in an installed build) to every pane's PATH. The settings
content itself never varies per pane, so it's written once
to a shared file under `runtime/` rather than passed as an inline JSON argv
string (Windows `list2cmdline` quote-leakage risk — see
docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md, section 3).
"""

from __future__ import annotations

import json

from . import config

HOOK_COMMAND = "takkub _hook"

_HOOK_SETTINGS: dict = {
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND}]}],
        "Notification": [
            {
                "matcher": "idle_prompt",
                "hooks": [{"type": "command", "command": HOOK_COMMAND}],
            }
        ],
    }
}


def ensure_hook_settings_file() -> str:
    """Write the static hook-wiring settings file if missing/stale and
    return its path as a string (for `--settings <path>`).

    Idempotent and cheap: only writes when the on-disk content differs, so
    spawning N panes back-to-back doesn't hammer the filesystem.

    Resolves ``config.RUNTIME_DIR`` at call time (not import time) so tests
    that monkeypatch it (as several spawn-argv tests already do) land the
    file under their own tmp dir instead of a stale path cached from
    whichever test happened to import this module first.
    """
    config.ensure_runtime()
    settings_path = config.RUNTIME_DIR / "hook-settings.json"
    rendered = json.dumps(_HOOK_SETTINGS, indent=2, ensure_ascii=False)
    try:
        current = settings_path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != rendered:
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(settings_path)
    return str(settings_path)
