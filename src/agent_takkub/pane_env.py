"""Per-pane env construction — allowlist + mute helpers for spawned panes.

Seven concerns live here:
1. `_PANE_ENV_ALLOWLIST` + `_build_pane_env()` — keep secret-bearing env
   vars (API keys, GH tokens, AWS creds) out of teammate panes by
   filtering to a known-safe set.
2. `_LEAD_ENV_EXTRA_ALLOWLIST` + `_build_lead_env()` — Lead is privileged
   (commits, runs gh CLI, orchestrates) so it gets a wider allowlist, but
   still not `os.environ.copy()` — defense-in-depth against secrets leaking
   into Lead's subprocesses / MCP tools.
3. `_apply_port_file()` — stamp the effective cli_server port-file path
   into every pane (teammate and Lead alike) so its `takkub` CLI always
   dials *this* cockpit's server, even in single-instance mode where
   `TAKKUB_PORT_FILE` was never set in the host process env.
4. `_apply_mcp_timeout()` — raise the CC 2.1.142+ MCP per-call timeout
   default from 60s to 3min so browser MCP work (Playwright, Chrome
   DevTools, Lighthouse) doesn't trip on first page load.
5. `_apply_non_interactive_env()` — prevent npx/npm/git from blocking on
   interactive y/N or credential prompts (issue #52). Sets npm_config_yes
   and GIT_TERMINAL_PROMPT at process level so every shell command inside
   the pane is non-interactive by default.
6. `_apply_color_term()` — advertise a truecolor terminal so claude/ink
   renders ANSI colours. The cockpit front-end is xterm.js on every OS
   (full 256-colour + truecolor palette), but a GUI-launched cockpit on
   macOS inherits no `TERM`, so the allowlist had nothing to forward and
   claude fell back to monochrome.
7. `_apply_artifacts_dir()` — when the caller supplies ``project_ns``, stamp
   the central artifacts/docs paths inside the env builder itself so an
   early-returning provider branch (especially Gemini/agy) cannot omit them.

H1 (cross-platform audit 2026-07-10): #4-6 used to be called explicitly only
from `spawn_engine.py`'s claude branch, *after* the shell/codex/gemini
branches had already early-returned — so non-claude panes got no truecolor
fix (breaks-mac: codex/agy rendered monochrome) and no non-interactive env
(both-OS: those panes could hang on an `npx`/`git` y/N prompt). Calling them
from inside `_build_pane_env()`/`_build_lead_env()` themselves means every
branch gets all three for free the moment it calls either builder — no
per-branch call site to forget.

Extracted from orchestrator.py to keep that file focused on pane
lifecycle (spawn/send/done/close) rather than environment plumbing.
The orchestrator re-exports these names for backwards-compatibility
with existing test imports.
"""

from __future__ import annotations

import os
from datetime import datetime

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
        # L3 (cross-platform audit 2026-07-10): TEMP/TMP above are the
        # Windows env vars; POSIX's equivalent is TMPDIR, which was missing
        # — a mac pane lost its per-user tmp dir and fell back to bare
        # `/tmp`. XDG_* cover the modern Linux/POSIX user-dir convention
        # some CLI tools (npm, git, browsers) consult for cache/config/data
        # homes instead of hardcoding `~/.cache` etc.
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        # Node / npm tooling (used by some claude internals + RTK)
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        # Cockpit-injected (will be reset below anyway, but listed for clarity)
        "TAKKUB_ROLE",
        "TAKKUB_PROJECT",
        "TAKKUB_SETTING_SOURCES",
        # Per-PID port file — in multi-instance mode app.py sets this in the
        # cockpit process env so panes dial *this* cockpit's cli_server instead
        # of a stale runtime/port fossil left by a dead instance. Listed here
        # for clarity only: `_apply_port_file()` (below) recomputes and stamps
        # the effective value into every pane's env unconditionally, in both
        # single- and multi-instance mode, so this allowlist entry is never
        # actually relied on to carry the value through.
        "TAKKUB_PORT_FILE",
        # Browser MCP (chrome-devtools needs to find Chrome)
        "CHROME_BIN",
        # User override for MCP per-call timeout (default injected below).
        "MCP_TOOL_TIMEOUT",
        # Scratch dir for temp files/screenshots/test scripts, out of the
        # project repo (plan item #1). Listed here for clarity only —
        # `_apply_artifacts_dir()` stamps the effective value into every
        # pane's env unconditionally, same contract as TAKKUB_PORT_FILE.
        "TAKKUB_ARTIFACTS_DIR",
        # Central per-project docs dir for LLM-authored design-review /
        # reviews / guides / system-overview markdown+html (central-home
        # migration item C). Same stamped-unconditionally contract —
        # `_apply_artifacts_dir()` sets it so instructions can point at
        # `$TAKKUB_DOCS_DIR/...` instead of a repo-relative `docs/...`.
        "TAKKUB_DOCS_DIR",
    }
)

# Default per-call MCP timeout (milliseconds). Claude Code's built-in
# ceiling for HTTP/SSE MCP tool calls is 60s, which trips on browser MCP
# operations like Playwright page loads, Chrome DevTools traces, or
# Lighthouse audits. Raising to 3min covers realistic UI work without
# hiding genuinely-stuck calls forever. Honoured per-pane only when the
# user hasn't already set MCP_TOOL_TIMEOUT in the cockpit env.
_DEFAULT_MCP_TOOL_TIMEOUT_MS = "180000"


def _build_pane_env(project_ns: str | None = None) -> dict[str, str]:
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
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    _apply_port_file(env)
    _apply_mcp_timeout(env)
    _apply_non_interactive_env(env)
    _apply_color_term(env)
    if project_ns is not None:
        _apply_artifacts_dir(env, project_ns)
    return env


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


def _build_lead_env(project_ns: str | None = None) -> dict[str, str]:
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
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    _apply_port_file(env)
    _apply_mcp_timeout(env)
    _apply_non_interactive_env(env)
    _apply_color_term(env)
    if project_ns is not None:
        _apply_artifacts_dir(env, project_ns)
    return env


def _apply_port_file(env: dict[str, str]) -> None:
    """Stamp the effective cli_server port-file path into every pane's env.

    ``TAKKUB_PORT_FILE`` is only present in ``os.environ`` in multi-instance
    mode (app.py sets it per-PID so panes dial *this* cockpit's cli_server
    instead of a stale runtime/port fossil left by a dead instance). In
    single-instance mode nothing sets it, so the allowlist copy above simply
    omits the key — the pane's ``takkub`` CLI then falls back to whichever
    ``runtime/port`` its own DATA_HOME resolves to, which can be a *different*
    cockpit's file entirely when a dev checkout's ``bin/`` sits ahead of an
    installed prod cockpit's on PATH → the CLI dials the wrong server and every
    ``takkub send/assign/done`` call fails with connection refused.

    ``config._get_port_file()`` already honours a ``TAKKUB_PORT_FILE`` env
    override when present and falls back to this process's own
    ``RUNTIME_DIR/port`` otherwise — exactly the value every pane should use
    regardless of instance mode. Stamping it here unconditionally (not
    ``setdefault``) is safe: in multi-instance mode the allowlist copy already
    equals what this recomputes, so overwriting is a no-op in value.
    """
    from . import config

    env["TAKKUB_PORT_FILE"] = str(config._get_port_file())


def _apply_artifacts_dir(env: dict[str, str], project_ns: str) -> None:
    """Stamp ``TAKKUB_ARTIFACTS_DIR`` + ``TAKKUB_DOCS_DIR`` and create them,
    per pane spawn (plan #1 + central-home item C).

    ``TAKKUB_ARTIFACTS_DIR`` reuses the existing ``runtime/exports/<date>/
    <project>/`` convention the screenshot scanner already reads
    (``orchestrator._compute_last_progress_ts`` checks ``.../screenshots``) so
    shots keep landing where they always have — an explicit, allowlisted
    scratch dir for temp files/images/test scripts instead of littering the
    project repo.

    ``TAKKUB_DOCS_DIR`` (``runtime/docs/<project>/``) is the central home for
    LLM-authored docs (design-review / reviews / guides / system-overview) the
    CLAUDE.md routing tells panes to produce — pointing those at
    ``$TAKKUB_DOCS_DIR/...`` keeps them out of the user's repo too. Both are
    stamped unconditionally at spawn time (not just allowlisted) so every pane
    — claude, codex, agy alike — sees a real, already-existing directory.
    """
    from . import config

    today = datetime.now().strftime("%Y-%m-%d")
    artifacts_dir = config.RUNTIME_DIR / "exports" / today / project_ns
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env["TAKKUB_ARTIFACTS_DIR"] = str(artifacts_dir)

    # Recomputed from RUNTIME_DIR at call time (not the frozen config.DOCS_DIR
    # constant) so a monkeypatched / multi-instance RUNTIME_DIR is honoured —
    # same contract as the artifacts dir above.
    docs_dir = config.RUNTIME_DIR / "docs" / project_ns
    try:
        docs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env["TAKKUB_DOCS_DIR"] = str(docs_dir)


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
    """Set ``CLAUDE_CONFIG_DIR`` in *env* when it should differ from a plain
    ``claude`` default invocation.

    For a dev checkout, the implicit default profile IS ``~/.claude`` (what
    ``claude`` uses when the var is unset at all), so the var is left unset
    for the ``"default"`` profile — unchanged historical behaviour. Installed
    builds isolate their default profile under DATA_HOME
    (``config.default_claude_config_dir()``), so even the ``"default"``
    profile must set the var there — otherwise every pane would fall through
    to the OS-wide ``~/.claude`` instead of the prod-scoped profile. A
    project's own explicit profile choice always wins either way.
    """
    from . import config
    from .user_profile import DEFAULT_PROFILE, config_dir_for, profile_for

    try:
        name = profile_for(project)
        if name != DEFAULT_PROFILE or config.DATA_HOME != config.REPO_ROOT:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir_for(project))
    except Exception:
        pass
