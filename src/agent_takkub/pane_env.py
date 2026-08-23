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
import re
import subprocess
from datetime import datetime

from ._win_console import SUBPROCESS_NO_WINDOW

_CLAUDE_PROJECT_DIR_NAME_MIN_VERSION = "2.1.234"
_CLAUDE_PROJECT_DIR_NAME_PREFIX = "takkub-project-"

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
    _apply_win32_path_sanitization(env)
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
    _apply_win32_path_sanitization(env)
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

    ``config._effective_port_file_for_app()`` resolves exactly the path THIS
    cockpit instance actually writes its port to (see that function's
    docstring, #354): it honours a same-instance-derived multi-instance
    override or one that resolves under this instance's own DATA_HOME, and
    otherwise falls back to this process's own ``RUNTIME_DIR/port`` — never
    the app-side client helper ``config._get_port_file()``, which honours
    ANY inherited override unconditionally. Using that unguarded helper here
    would stamp a pane spawned by THIS (correctly-behaving) instance with a
    path pointing at a different cockpit entirely whenever this process
    itself inherited a stray cross-instance override, sending the new pane's
    ``takkub`` calls to the wrong cockpit. Stamping it here unconditionally
    (not ``setdefault``) is safe: in multi-instance mode the allowlist copy
    already equals what this recomputes, so overwriting is a no-op in value.
    """
    from . import config

    env["TAKKUB_PORT_FILE"] = str(config._effective_port_file_for_app())


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


def apply_default_model(env: dict[str, str], model: str) -> None:
    """Set ``ANTHROPIC_DEFAULT_MODEL`` so a claude pane *starts* on *model*
    without permanently overriding a model the user already picked (#318).

    Confirmed against code.claude.com/docs/en/model-config (CC 2.1.236+,
    "Set a default model for new sessions"): ``ANTHROPIC_DEFAULT_MODEL`` is
    the lowest-precedence of the five ways to pick a session's model —
    behind an explicit ``--model`` flag, ``ANTHROPIC_MODEL``, and a ``model``
    value in any settings file. Critically, ``/model`` writes that settings
    ``model`` field on save (since 2.1.153), so once a user picks a model
    with ``/model`` this var is silently ignored on every later launch —
    *including* a crash-respawn's ``--resume``, where the docs draw the same
    distinction explicitly: "Claude Code doesn't restore the model saved in
    that session's transcript [only] when a new session would start on the
    variable's model" — i.e. only when nothing else (a settings ``model``
    field included) already claimed the slot.

    This is why a *persistent* role/provider model pin belongs here and not
    on ``--model``: ``--model`` always wins over a saved ``/model`` choice,
    on every spawn AND every resume, so a teammate could never actually keep
    a model its own session picked. Reserve ``--model`` for a genuinely
    one-off, deliberate override (a single ``takkub assign --model`` call) —
    stacking both mechanisms for the same "persistent default" duty is
    exactly the double-mechanism issue #318 asks not to reintroduce.

    Claude-only (``ANTHROPIC_DEFAULT_MODEL`` has no codex/gemini/opencode/
    kimi/cursor counterpart — flagged to #103): callers must only invoke
    this on the claude spawn path, never the generic provider branch.
    """
    model = (model or "").strip()
    if model:
        env["ANTHROPIC_DEFAULT_MODEL"] = model


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


def claude_project_dir_name(project_ns: str) -> str:
    """Return the cockpit-owned Claude transcript directory for a project.

    The project namespace is the cockpit's stable identifier, unlike Claude's
    legacy cwd encoding which collapses ``-``, ``_`` and ``.`` to the same
    character.  Keep the prefix fixed so a new directory can never be
    mistaken for a legacy encoded absolute path.
    """
    return f"{_CLAUDE_PROJECT_DIR_NAME_PREFIX}{project_ns}"


def supports_claude_project_dir_name(version: str | None) -> bool:
    """Whether *version* supports ``CLAUDE_CODE_PROJECT_DIR_NAME``.

    Unknown/unparseable versions deliberately keep Claude's historical
    encoded-cwd behaviour; that is safe for older CLIs and the read side still
    checks both layouts.
    """
    if not version:
        return False
    match = re.search(r"\b(\d+(?:\.\d+){1,3})\b", version)
    if match is None:
        return False
    installed = tuple(int(part) for part in match.group(1).split("."))
    minimum = tuple(int(part) for part in _CLAUDE_PROJECT_DIR_NAME_MIN_VERSION.split("."))
    width = max(len(installed), len(minimum))
    return installed + (0,) * (width - len(installed)) >= minimum + (0,) * (width - len(minimum))


def inject_claude_project_dir_name_env(
    env: dict[str, str], project_ns: str, claude_executable: str
) -> None:
    """Opt into the reversible per-project transcript name on supported Claude.

    Claude Code introduced this per-session environment variable in 2.1.234.
    Probe the exact executable about to be spawned; older/unknown versions
    retain the default encoded-cwd layout rather than receiving an environment
    variable they do not understand.
    """
    try:
        completed = subprocess.run(
            [claude_executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        version = completed.stdout if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        version = None
    if supports_claude_project_dir_name(version):
        env["CLAUDE_CODE_PROJECT_DIR_NAME"] = claude_project_dir_name(project_ns)


def inject_provider_home_env(env: dict[str, str], provider: str) -> None:
    """Point a non-claude provider's state at DATA_HOME (user directive
    2026-08-19) — the codex/opencode counterpart of
    ``inject_user_profile_env``'s ``CLAUDE_CONFIG_DIR``.

    Scoped to the pane being spawned, never to the cockpit process: the
    OpenCode entry overrides the XDG pair, and exporting those cockpit-wide
    would move every *other* XDG-aware tool a pane runs (gh, uv, …) off the
    user's real config. A provider with no isolation knob
    (``config.PROVIDER_ISOLATION_GAPS``) yields nothing and keeps its OS-wide
    home, which `takkub doctor` reports rather than hiding.

    Assignment (not ``setdefault``): the cockpit's own inherited CODEX_HOME /
    XDG_* would otherwise win and quietly de-isolate the pane. A user who
    really wants a custom location sets ``AGENT_TAKKUB_HOME``, which moves
    DATA_HOME and therefore these with it.
    """
    from . import config

    try:
        env.update(config.provider_home_env(provider))
    except Exception:
        pass


# ── Provider self-update suppression while a pane is alive (#313) ──────────
#
# Root cause: an npm/uv self-update racing a live pane's spawn can leave the
# CLI binary mid-write, which _pty_backend._validate_spawn_target now catches
# defensively (docs/audit/2026-08-20-issue-313-spawn-deadlock.md) — but the
# better fix is simply never letting a provider update itself while cockpit
# panes might spawn it. Real update runs happen once at cockpit boot instead
# (boot_update.py), before any pane exists.
#
# Each entry is a CONFIRMED, documented knob (never a guess — a wrong env var
# either does nothing or, worse, silently misfires): claude's is stated by
# user directive; the rest were verified against each CLI's own docs on
# 2026-08-20 (see docs/audit/2026-08-20-boot-update-policy.md for the exact
# citations). A provider absent from this dict has no known knob — tracked in
# `NO_AUTOUPDATE_KNOB_GAPS` below instead of silently doing nothing.
_PROVIDER_NO_AUTOUPDATE_ENV: dict[str, dict[str, str]] = {
    "claude": {"DISABLE_AUTOUPDATER": "1"},
    # Antigravity CLI docs, "Resolve self-updater locks and failures":
    # https://antigravity.google/docs/cli/troubleshooting
    "gemini": {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
    # kimi-cli FAQ: https://moonshotai.github.io/kimi-cli/en/faq.html
    # ("export KIMI_CLI_NO_AUTO_UPDATE=1"). KIMI_CODE_NO_AUTO_UPDATE is set
    # alongside it — same FAQ entry names it as the current kimi-code rebrand
    # of the identical variable; setting both costs nothing and covers either
    # binary generation without guessing which one is actually installed.
    "kimi": {"KIMI_CLI_NO_AUTO_UPDATE": "1", "KIMI_CODE_NO_AUTO_UPDATE": "1"},
}

# Providers with NO documented env-var knob to disable self-update, kept
# explicit (mirrors config.PROVIDER_ISOLATION_GAPS) so the gap is visible
# instead of looking like an oversight. Flagged to issue #103 (2026-08-20).
NO_AUTOUPDATE_KNOB_GAPS: dict[str, str] = {
    # openai/codex GitHub issues #3855 / #4375 both ask for exactly this and
    # are still open/unimplemented as of 2026-08-20 — no config/env surface
    # exists in the shipped CLI to disable its own update check.
    "codex": "no documented env/config knob to disable codex's self-update check",
    # OpenCode disables auto-update via a config FILE key (`"autoupdate":
    # false` in opencode.json), not an env var — see
    # `_ensure_opencode_no_autoupdate_config` below, which handles it through
    # that file instead of this env-var table.
    # cursor-agent: only an unofficial community workaround exists (removing
    # exec permission on its versions dir) — no vendor-documented knob.
    "cursor": "no vendor-documented knob; only an unofficial filesystem workaround exists",
}


def _ensure_opencode_no_autoupdate_config(config_home: str) -> None:
    """Merge ``"autoupdate": false`` into OpenCode's isolated
    ``<config_home>/opencode/opencode.json`` (#313).

    OpenCode has no env-var override for this (confirmed against
    https://opencode.ai/docs/config/ — the only documented mechanism is the
    ``autoupdate`` key in its JSON config), so suppressing it means writing to
    that file instead of the env dict above.

    Only runs against the cockpit's OWN isolated config home
    (``config.provider_home_env("opencode")["XDG_CONFIG_HOME"]``, populated
    only for an installed build) — never the user's real
    ``~/.config/opencode/opencode.json``. A dev checkout has no isolated home
    (`config.provider_home_env` returns ``{}`` by design), so this is a no-op
    there rather than reaching for the user's real global config; that gap is
    intentional, not silent (see docs/audit/2026-08-20-boot-update-policy.md).
    Idempotent (skips the write once the key already reads False) and never
    raises — a config-merge failure must not block a pane from spawning.
    """
    import json as _json
    from pathlib import Path as _Path

    cfg_path = _Path(config_home) / "opencode" / "opencode.json"
    try:
        data = {}
        if cfg_path.is_file():
            try:
                parsed = _json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    data = parsed
            except (OSError, ValueError):
                pass  # corrupt/unreadable — rebuild from an empty config rather than give up
        if data.get("autoupdate") is False:
            return  # already suppressed — avoid an unnecessary write every spawn
        data["autoupdate"] = False
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def inject_provider_no_autoupdate_env(env: dict[str, str], provider: str) -> None:
    """Suppress *provider*'s own self-update while this pane is alive (#313).

    Called for every pane spawn (claude branch and the generic non-claude
    branch alike, see spawn_engine.py) — the real update run happens once at
    cockpit boot instead (boot_update.py), so no pane should ever race a
    provider's own updater against its own spawn. ``setdefault`` on every
    var, same contract as the other ``_apply_*`` helpers in this module: a
    cockpit-level override in the host env still wins.
    """
    name = str(provider or "").strip().lower()
    for key, value in _PROVIDER_NO_AUTOUPDATE_ENV.get(name, {}).items():
        env.setdefault(key, value)
    if name == "opencode":
        from . import config

        config_home = config.provider_home_env("opencode").get("XDG_CONFIG_HOME")
        if config_home:
            _ensure_opencode_no_autoupdate_config(config_home)


def _apply_win32_path_sanitization(env: dict[str, str]) -> None:
    """Sanitize Windows PATH: clean extensionless mb shims and reorder %APPDATA%\\npm."""
    import sys

    if sys.platform != "win32":
        return
    from pathlib import Path

    from ._win_console import sanitize_win32_mb_shims

    sanitize_win32_mb_shims()

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    npm_dir = os.path.join(appdata, "npm")
    if os.path.isdir(npm_dir):
        path_parts = env.get("PATH", "").split(os.pathsep)
        local_bin_str = str(Path.home() / ".local" / "bin").lower()
        npm_dir_str = npm_dir.lower()

        local_idx = -1
        npm_idx = -1
        for idx, part in enumerate(path_parts):
            p_lower = part.strip().lower()
            if local_idx == -1 and p_lower == local_bin_str:
                local_idx = idx
            if npm_idx == -1 and p_lower == npm_dir_str:
                npm_idx = idx

        if npm_idx != -1 and local_idx != -1 and npm_idx > local_idx:
            path_parts.pop(npm_idx)
            path_parts.insert(local_idx, npm_dir)
            env["PATH"] = os.pathsep.join(path_parts)
        elif npm_idx == -1:
            env["PATH"] = npm_dir + os.pathsep + env.get("PATH", "")
