"""Per-pane env construction — allowlist + mute helpers for spawned panes.

Five concerns live here:
1. `_PANE_ENV_ALLOWLIST` + `_build_pane_env()` — keep secret-bearing env
   vars (API keys, GH tokens, AWS creds) out of teammate panes by
   filtering to a known-safe set.
2. `_LEAD_ENV_EXTRA_ALLOWLIST` + `_build_lead_env()` — Lead is privileged
   (commits, runs gh CLI, orchestrates) so it gets a wider allowlist, but
   still not `os.environ.copy()` — defense-in-depth against secrets leaking
   into Lead's subprocesses / MCP tools.
3. `_apply_mcp_timeout()` — raise the CC 2.1.142+ MCP per-call timeout
   default from 60s to 3min so browser MCP work (Playwright, Chrome
   DevTools, Lighthouse) doesn't trip on first page load.
4. `_apply_non_interactive_env()` — prevent npx/npm/git from blocking on
   interactive y/N or credential prompts (issue #52). Sets npm_config_yes
   and GIT_TERMINAL_PROMPT at process level so every shell command inside
   the pane is non-interactive by default.
5. `_apply_color_term()` — advertise a truecolor terminal so claude/ink
   renders ANSI colours. The cockpit front-end is xterm.js on every OS
   (full 256-colour + truecolor palette), but a GUI-launched cockpit on
   macOS inherits no `TERM`, so the allowlist had nothing to forward and
   claude fell back to monochrome.

Extracted from orchestrator.py to keep that file focused on pane
lifecycle (spawn/send/done/close) rather than environment plumbing.
The orchestrator re-exports these names for backwards-compatibility
with existing test imports.
"""

from __future__ import annotations

import os

# Env vars that MUST pass through to claude/codex/gemini panes for them to
# function. Anything not in this list is dropped to avoid leaking secrets
# from the cockpit shell.
_PANE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Windows essentials
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        # Anthropic proxy base URL only — the bearer token (ANTHROPIC_AUTH_TOKEN)
        # is intentionally excluded from the default allowlist to limit blast radius
        # if a pane is compromised (prompt injection, malicious MCP, dependency).
        # Opt-in by adding ANTHROPIC_AUTH_TOKEN to TAKKUB_PANE_ENV_ALLOW.
        "ANTHROPIC_BASE_URL",
        # COMSPEC = path to cmd.exe — Node.js child_process.spawn() falls back to
        # this when launching subprocesses on Windows; missing → ENOENT crash in
        # MCP servers (codex_apps) that shell out. Top hypothesis for codex early-crash.
        "COMSPEC",
        # Session identity — some Windows auth flows + .NET apps consult these
        "SESSIONNAME",
        "LOGONSERVER",
        # POSIX essentials (forward-compat for mac-port branch)
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        # Node / npm tooling (used by some claude internals + RTK)
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        # Cockpit-injected (will be reset below anyway, but listed for clarity)
        "TAKKUB_ROLE",
        "TAKKUB_PROJECT",
        "TAKKUB_SETTING_SOURCES",
        # Per-PID port file — in multi-instance mode app.py sets this in the
        # cockpit process env so panes dial *this* cockpit's cli_server instead
        # of a stale runtime/port fossil left by a dead instance. Without it on
        # the allowlist the value is filtered out here and the pane's `takkub`
        # CLI falls back to the default runtime/port → connection refused when
        # that file points at a dead port left by a previous instance.
        # Not a secret (a temp path); safe to forward. Unset in single-instance
        # mode → pane correctly falls back to runtime/port, which the lone
        # server owns.
        "TAKKUB_PORT_FILE",
        # Browser MCP (chrome-devtools needs to find Chrome)
        "CHROME_BIN",
        # User override for MCP per-call timeout (default injected below).
        "MCP_TOOL_TIMEOUT",
    }
)

# Default per-call MCP timeout (milliseconds). Claude Code's built-in
# ceiling for HTTP/SSE MCP tool calls is 60s, which trips on browser MCP
# operations like Playwright page loads, Chrome DevTools traces, or
# Lighthouse audits. Raising to 3min covers realistic UI work without
# hiding genuinely-stuck calls forever. Honoured per-pane only when the
# user hasn't already set MCP_TOOL_TIMEOUT in the cockpit env.
_DEFAULT_MCP_TOOL_TIMEOUT_MS = "180000"


def _build_pane_env() -> dict[str, str]:
    """Build a clean env for spawned panes — only allowlisted keys.

    Why: codex's OMA review (docs/security-audit-2026-05-21.md, Check 1)
    flagged unbounded env inheritance as a HIGH-severity issue. Teammate
    panes don't need ANTHROPIC_API_KEY (Max OAuth handles auth) or any
    other secret-bearing var. This builds the minimum env claude needs
    to run on this OS.

    ANTHROPIC_AUTH_TOKEN is opt-in: set TAKKUB_PANE_ENV_ALLOW=ANTHROPIC_AUTH_TOKEN
    (comma-separated). Note: opting in weakens pane isolation — any compromised
    pane (prompt injection, malicious MCP, dependency) can exfiltrate the bearer
    token.
    """
    allow = set(_PANE_ENV_ALLOWLIST)
    extra = os.environ.get("TAKKUB_PANE_ENV_ALLOW", "")
    for k in extra.split(","):
        k = k.strip()
        if k:
            allow.add(k.upper())
    return {k: v for k, v in os.environ.items() if k.upper() in allow}


# Additional env vars that Lead needs beyond the base teammate allowlist.
# Lead commits (git identity), runs gh CLI (GH_TOKEN), and may push over SSH.
_LEAD_ENV_EXTRA_ALLOWLIST: frozenset[str] = frozenset(
    {
        # git identity — effective only if user set them; git normally reads ~/.gitconfig
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_EDITOR",
        # GitHub auth — Lead runs takkub issue (gh CLI) + may push
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_CONFIG_DIR",
        "GH_HOST",
        # editor — git/gh may open an editor for commit messages
        "EDITOR",
        "VISUAL",
        # SSH (git push over SSH, POSIX-side)
        "SSH_AUTH_SOCK",
        # User opt-in key — pass through so Lead can inspect it
        "TAKKUB_LEAD_ENV_ALLOW",
    }
)


def _build_lead_env() -> dict[str, str]:
    """Lead env: base teammate allowlist + Lead-only extras + user opt-in.

    Lead is privileged (commits, runs gh, orchestrates) but still uses an
    allowlist rather than os.environ.copy() so secrets in the cockpit shell
    (ANTHROPIC_API_KEY, cloud creds) don't leak into Lead's tools/subprocesses.
    User can widen via TAKKUB_LEAD_ENV_ALLOW='KEY1,KEY2' (comma-separated).
    """
    allow = set(_PANE_ENV_ALLOWLIST) | set(_LEAD_ENV_EXTRA_ALLOWLIST)
    extra = os.environ.get("TAKKUB_LEAD_ENV_ALLOW", "")
    for k in extra.split(","):
        k = k.strip()
        if k:
            allow.add(k.upper())
    return {k: v for k, v in os.environ.items() if k.upper() in allow}


def _apply_mcp_timeout(env: dict[str, str]) -> None:
    """Set a 3-minute MCP per-call timeout when the user hasn't picked one.

    CC 2.1.142 fixed `MCP_TOOL_TIMEOUT` so it actually raises the per-request
    fetch timeout for HTTP/SSE MCP servers (was hard-capped at 60s before).
    Browser-heavy roles routinely exceed 60s on first page load, Lighthouse
    audits, or screenshot capture with network idle — leave the env var
    alone if the operator has already set one at the cockpit level.
    """
    env.setdefault("MCP_TOOL_TIMEOUT", _DEFAULT_MCP_TOOL_TIMEOUT_MS)


def _apply_non_interactive_env(env: dict[str, str]) -> None:
    """Prevent npx/npm/git from blocking a pane on interactive y/N prompts.

    Two env vars cover the two most common blocking commands pane agents run:

    - ``npm_config_yes=true``  → equivalent to passing ``--yes`` to every
      ``npx`` invocation; suppresses the 'Ok to proceed? (y)' prompt that
      npx shows when it needs to download a package that isn't installed yet.
    - ``GIT_TERMINAL_PROMPT=0`` → git fails immediately (exit 128) instead
      of prompting for username/password when the credential helper is absent
      or the cached token has expired.

    Both are set via ``setdefault`` so a cockpit-level override in the host
    env still wins — same contract as ``MCP_TOOL_TIMEOUT``.  A pane that
    genuinely needs interactive npx (rare) can set ``npm_config_yes=false``
    in the cockpit shell before spawning.
    """
    env.setdefault("npm_config_yes", "true")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")


def _apply_color_term(env: dict[str, str]) -> None:
    """Advertise a truecolor terminal so claude/ink renders ANSI colours.

    Symptom this fixes: on macOS the text inside a pane (claude's TUI, qa
    output) rendered monochrome while the window chrome was fine. Root cause
    is colour *detection*, not the renderer — the cockpit front-end is
    xterm.js on every OS and its theme ships the full 256-colour + truecolor
    palette, so the screen is perfectly capable of colour.

    The gap is the spawned child's environment. claude/ink decide whether to
    emit colour from ``TERM`` + ``COLORTERM`` (plus isatty, which a PTY
    satisfies). ``COLORTERM`` was never on the pane allowlist (always
    stripped) and ``TERM`` was only *forwarded if present* in the cockpit
    process. A GUI-launched cockpit on macOS (Finder/.app/Dock) inherits no
    ``TERM`` at all, so the allowlist had nothing to forward → claude saw a
    non-colour terminal → monochrome. Windows was unaffected because claude
    forces colour through the Win32 console API regardless of ``TERM``.

    Both are set via ``setdefault`` — same contract as the other ``_apply_*``
    helpers — so a real terminal that *did* export ``TERM=xterm-256color``
    (cockpit launched from iTerm/Terminal) still wins. ``xterm-256color`` is
    the truthful descriptor for what xterm.js presents on both platforms.
    """
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")


def inject_user_profile_env(env: dict[str, str], project: str) -> None:
    """Set ``CLAUDE_CONFIG_DIR`` in *env* when the project uses a non-default profile.

    When the selected profile is ``"default"`` (or missing/corrupt) the env
    var is intentionally left unset so the existing ``~/.claude`` setup is
    used unchanged — this keeps the current behaviour for every project that
    hasn't opted into a named profile.
    """
    from .user_profile import DEFAULT_PROFILE, config_dir_for, profile_for

    try:
        if profile_for(project) != DEFAULT_PROFILE:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir_for(project))
    except Exception:
        pass
