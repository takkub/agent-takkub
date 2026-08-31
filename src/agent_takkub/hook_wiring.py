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
import os

from . import config

HOOK_COMMAND = "takkub _hook"
SESSION_REPORT_COMMAND = "takkub session-report"
GUARD_COMMAND = "takkub _guard"

# #318 token-diet wave 4: the built-in "Concise" output style (CC 2.1.237 —
# "Claude leads with results and skips preamble and narration, while doing
# the work just as thoroughly"). Confirmed against code.claude.com/docs/en/
# output-styles: the ONLY mechanisms are the `outputStyle` field in a
# settings file or the `/config` menu — there is no CLI flag or env var for
# it. `--settings <file>` (already used for hook wiring below) is a
# "command line arguments: temporary session override" per docs/en/settings
# — priority 2, above Local/Project/User (3-5) and specific to *this*
# invocation only, so folding `outputStyle` into that same file gives a true
# per-session, per-role toggle without writing to any shared
# `.claude/settings*.json` a human or another pane could see/inherit.
#
# Scoped to teammates only (Lead keeps the project/user default — Lead's
# done-note/summary needs full detail, not a stripped one) via role
# allowlist so a rollout can A/B one role before widening (acceptance
# criterion in #318: measure token usage + done-note quality on one role
# first). Default pilot is `qa` only; see docs/audit/2026-08-20-issue-318-*
# for the measurement method. TAKKUB_CONCISE_ROLES overrides the allowlist:
# comma-separated role names, "*" for every teammate role, "" to disable
# entirely (Lead is never included regardless of this var).
_CONCISE_OUTPUT_STYLE = "Concise"
_DEFAULT_CONCISE_ROLES = frozenset({"qa"})


def role_wants_concise(role_name: str, *, is_lead: bool) -> bool:
    """Whether *role_name* should get the Concise output style this spawn.

    Lead is hard-excluded regardless of ``TAKKUB_CONCISE_ROLES`` — its
    done-note/summary is the one artifact a human actually reads, and
    Concise trims exactly the narration that makes those legible.
    """
    if is_lead:
        return False
    raw = os.environ.get("TAKKUB_CONCISE_ROLES")
    roles = _DEFAULT_CONCISE_ROLES if raw is None else None
    if raw is not None:
        raw = raw.strip()
        if not raw:
            return False
        if raw == "*":
            return True
        roles = {r.strip() for r in raw.split(",") if r.strip()}
    return role_name in (roles or ())


# #458: Claude Code 2.1.251 turns Remote Control on by default whenever
# nothing in the settings stack says otherwise (rollout
# "remote-control-auto-on" — the CLI's resolver falls back to a GB default of
# `true` once --remote-control/project/local/policy/flag/user/legacy all
# leave it unset). Cockpit never passed `--remote-control`, so every pane —
# not just Lead — was picking up that GB default. `remoteControlAtStartup`
# in the flag-tier `--settings` file (this module's existing injection
# point) is the one layer that can force it either way, so every spawn now
# stamps an explicit true/false here instead of leaving it to inherit.
#
# Same allowlist shape as `role_wants_concise` above, but the default and
# the empty-string case are both inverted: rc defaults to Lead ONLY (not a
# pilot roster), and an explicit empty override turns it off for EVERY role
# including Lead (concise's empty override still excludes only teammates,
# since Lead is hard-excluded from concise regardless). TAKKUB_REMOTE_
# CONTROL_ROLES: comma-separated role names, "*" for every role, "" to
# disable for everyone, unset = Lead only.
_DEFAULT_REMOTE_CONTROL_ROLES = frozenset({"lead"})


def role_wants_remote_control(role_name: str, *, is_lead: bool) -> bool:
    """Whether *role_name* should get `remoteControlAtStartup: true` this
    spawn. Unlike `role_wants_concise`, Lead is not hard-coded: the default
    (env unset) grants it via ``is_lead`` so it survives a role rename, but
    an explicit ``TAKKUB_REMOTE_CONTROL_ROLES`` override matches by name —
    including "" turning it off for Lead too, for a deliberate full-off
    test/debug run."""
    raw = os.environ.get("TAKKUB_REMOTE_CONTROL_ROLES")
    if raw is None:
        return is_lead
    raw = raw.strip()
    if not raw:
        return False
    if raw == "*":
        return True
    roles = {r.strip() for r in raw.split(",") if r.strip()}
    return role_name in roles


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


def _rendered_settings(*, concise: bool = False, remote_control: bool = True) -> dict:
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
    blocked before rtk spends any work rewriting it.

    ``concise=True`` (#318) adds ``outputStyle: "Concise"`` — see
    `role_wants_concise` for who gets it and why this rides `--settings`
    instead of a shared settings file.

    ``remote_control`` (#458) is always stamped as an explicit
    ``remoteControlAtStartup: true/false`` — never omitted — so this layer
    never falls through to Claude Code's own GB-rollout default (which is
    what caused rc to be on for every role in the first place). Defaults to
    ``True`` so `doctor.check_hook_wiring()`'s no-arg call keeps behaving
    like it always has."""
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
    if concise:
        settings["outputStyle"] = _CONCISE_OUTPUT_STYLE
    settings["remoteControlAtStartup"] = remote_control
    return settings


def ensure_hook_settings_file(*, concise: bool = False, remote_control: bool = True) -> str:
    """Write the hook-wiring settings file if missing/stale and return its
    path as a string (for `--settings <path>`).

    Idempotent and cheap: only writes when the on-disk content differs, so
    spawning N panes back-to-back doesn't hammer the filesystem. The content
    now varies with the central rtk toggle (`_rendered_settings`), so
    enabling rtk mid-session is picked up on the next spawn without a
    cockpit restart.

    ``concise`` and ``remote_control`` each pick a distinct on-disk file
    (`hook-settings[-concise][-norc].json`) rather than mutating one shared
    file in place — panes with different combinations can spawn back-to-back
    without one clobbering another's `--settings` content mid-race. The
    all-default combo (concise=False, remote_control=True) keeps the
    original bare `hook-settings.json` name so `doctor.check_hook_wiring()`'s
    no-arg call and any external reference to that path stay unaffected.

    Resolves ``config.RUNTIME_DIR`` at call time (not import time) so tests
    that monkeypatch it (as several spawn-argv tests already do) land the
    file under their own tmp dir instead of a stale path cached from
    whichever test happened to import this module first.
    """
    config.ensure_runtime()
    name = "hook-settings"
    if concise:
        name += "-concise"
    if not remote_control:
        name += "-norc"
    settings_path = config.RUNTIME_DIR / f"{name}.json"
    rendered = json.dumps(
        _rendered_settings(concise=concise, remote_control=remote_control),
        indent=2,
        ensure_ascii=False,
    )
    try:
        current = settings_path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != rendered:
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(settings_path)
    return str(settings_path)
