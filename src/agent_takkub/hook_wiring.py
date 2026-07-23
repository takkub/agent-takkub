"""Per-pane Claude Code hook wiring — authoritative pane-state signal.

Every spawned claude pane (Lead + teammates) is given a `--settings <file>`
pointing at a static settings file that wires `Stop` and `Notification`
(matcher `idle_prompt`) to `takkub _hook`. That command reports the event back
to the orchestrator over the existing TCP socket so turn-end/idle can be
detected the instant it happens, instead of waiting on the next PTY-scraping
poll tick (`pty_session.is_at_ready_prompt()`, which stays the fallback for
non-claude panes and for any claude pane whose hook never fires).

It also wires `SessionStart` (fires on startup / resume / clear / compact,
carrying the real `session_id` in the hook's stdin JSON) to
`takkub session-report`. This is the authoritative fix for session_uuid
drift: `PaneState.session_uuid` is otherwise only stamped once, at spawn
time — if the user manually runs `/resume` inside a pane, claude switches to
writing a different transcript uuid that the orchestrator never learns
about, so the remote mirror's exact-uuid lookup misses and shows a blank
chat. `takkub session-report` reports the CURRENT session_id every time one
starts, keeping `pane_state.session_uuid` truthful without ever guessing
(no newest-file heuristic — see `remote/notify.py`).

Both commands are bare (no args, no embedded JSON) so they need no shell
quoting on either OS — `spawn_engine.py` already prepends
`config.CLI_BIN_DIR` (`REPO_ROOT/bin` in a dev checkout, the venv's own
console-script dir in an installed build) to every pane's PATH. The settings
content itself never varies per pane, so it's written once
to a shared file under `runtime/` rather than passed as an inline JSON argv
string (Windows `list2cmdline` quote-leakage risk — see
docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md, section 3).
"""

from __future__ import annotations

import copy
import json

from . import config

HOOK_COMMAND = "takkub _hook"
SESSION_REPORT_COMMAND = "takkub session-report"
GUARD_COMMAND = "takkub _guard"

_HOOK_SETTINGS: dict = {
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND}]}],
        "Notification": [
            {
                "matcher": "idle_prompt",
                "hooks": [{"type": "command", "command": HOOK_COMMAND}],
            }
        ],
        "SessionStart": [{"hooks": [{"type": "command", "command": SESSION_REPORT_COMMAND}]}],
    }
}


def guard_hook_fragment() -> dict:
    """The `PreToolUse`/`Bash` entry that runs `pane_guard` before every Bash
    call. A fresh dict each call so a caller can't mutate shared state.

    Unlike rtk this is NOT conditional: the guard is the only thing standing
    between a teammate pane and the shell workaround for its MCP tool policy
    (`npx playwright`), and `takkub` is guaranteed on every pane's PATH
    (`spawn_engine` prepends `config.CLI_BIN_DIR`) — the same guarantee the
    Stop/SessionStart hooks already rely on."""
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": GUARD_COMMAND}],
    }


def _rendered_settings() -> dict:
    """The hook settings for this spawn: the static Stop/Notification/
    SessionStart wiring, the always-on PreToolUse Bash guard, plus rtk's
    PreToolUse Bash hook when rtk is enabled centrally AND on PATH
    (`rtk_helper.rtk_should_inject`).

    Folding rtk in here — rather than into a project's `.claude/settings.json`
    — is the central-home migration (A3): the file this returns is already
    handed to every claude pane via `--settings`, so rtk reaches panes without
    dirtying any repo. Additive: rtk lives under its own PreToolUse key and
    never perturbs the pane-state Stop/Notification/SessionStart hooks.

    Ordering matters: the guard is listed **first** so a denied command is
    blocked before rtk spends any work rewriting it."""
    settings = copy.deepcopy(_HOOK_SETTINGS)
    pre_tool_use: list[dict] = [guard_hook_fragment()]
    try:
        from . import rtk_helper

        if rtk_helper.rtk_should_inject():
            pre_tool_use.append(rtk_helper.rtk_hook_fragment())
    except Exception:
        # rtk is a best-effort optimisation — never let it break the
        # authoritative pane-state hook wiring (or the guard).
        pass
    settings["hooks"]["PreToolUse"] = pre_tool_use
    return settings


def ensure_hook_settings_file() -> str:
    """Write the hook-wiring settings file if missing/stale and return its
    path as a string (for `--settings <path>`).

    Idempotent and cheap: only writes when the on-disk content differs, so
    spawning N panes back-to-back doesn't hammer the filesystem. The content
    now varies with the central rtk toggle (`_rendered_settings`), so
    enabling rtk mid-session is picked up on the next spawn without a
    cockpit restart.

    Resolves ``config.RUNTIME_DIR`` at call time (not import time) so tests
    that monkeypatch it (as several spawn-argv tests already do) land the
    file under their own tmp dir instead of a stale path cached from
    whichever test happened to import this module first.
    """
    config.ensure_runtime()
    settings_path = config.RUNTIME_DIR / "hook-settings.json"
    rendered = json.dumps(_rendered_settings(), indent=2, ensure_ascii=False)
    try:
        current = settings_path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != rendered:
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(settings_path)
    return str(settings_path)
