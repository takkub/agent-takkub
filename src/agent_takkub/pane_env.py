"""Per-pane env construction — allowlist + mute helpers for spawned panes.

Four concerns live here:
1. `_PANE_ENV_ALLOWLIST` + `_build_pane_env()` — keep secret-bearing env
   vars (API keys, GH tokens, AWS creds) out of teammate panes by
   filtering to a known-safe set.
2. `_LEAD_ENV_EXTRA_ALLOWLIST` + `_build_lead_env()` — Lead is privileged
   (commits, runs gh CLI, orchestrates) so it gets a wider allowlist, but
   still not `os.environ.copy()` — defense-in-depth against secrets leaking
   into Lead's subprocesses / MCP tools.
3. `_apply_ecc_mute()` — silence ECC's two noisiest hooks when ECC is
   loaded into a pane (escape hatch via `TAKKUB_ECC_FULL=1`).
4. `_apply_mcp_timeout()` — raise the CC 2.1.142+ MCP per-call timeout
   default from 60s to 3min so browser MCP work (Playwright, Chrome
   DevTools, Lighthouse) doesn't trip on first page load.

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
        # Anthropic Proxy / Claude API Auth (allows bypassing login using global environment settings)
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
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
        "TAKKUB_ECC_FULL",
        "ECC_GATEGUARD",
        "ECC_DISABLED_HOOKS",
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
    """
    return {k: v for k, v in os.environ.items() if k.upper() in _PANE_ENV_ALLOWLIST}


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


# ECC plugin hooks we mute in every pane. See cockpit CLAUDE.md
# "ECC plugin noise — auto-muted ใน pane env" for the rationale.
_ECC_MUTED_HOOKS: tuple[str, ...] = (
    "pre:edit-write:gateguard-fact-force",
    "post:ecc-context-monitor",
)


def _apply_ecc_mute(env: dict[str, str]) -> None:
    """Mutate `env` in place so spawned claude sessions skip ECC's two
    noisiest hooks: GateGuard fact-force and the cost-critical alerter.

    Invariants the wire-ups depend on:
      - Sets both `ECC_GATEGUARD=off` and `ECC_DISABLED_HOOKS`. Either
        knob alone is enough to silence GateGuard, but `ecc-context-
        monitor` only honours the disabled-hooks list, so both go in.
      - Never clobbers a user-provided `ECC_GATEGUARD` (e.g. if the
        operator deliberately set it elsewhere).
      - Appends to any existing `ECC_DISABLED_HOOKS` rather than
        replacing it, so a user-disabled hook stays disabled.
      - Skipped entirely when `TAKKUB_ECC_FULL=1` is set — escape
        hatch for the rare case a future ECC hook gets caught in the
        mute net.
    """
    if os.environ.get("TAKKUB_ECC_FULL") == "1":
        return
    env.setdefault("ECC_GATEGUARD", "off")
    extra = ",".join(_ECC_MUTED_HOOKS)
    existing = env.get("ECC_DISABLED_HOOKS", "").strip()
    env["ECC_DISABLED_HOOKS"] = f"{existing},{extra}" if existing else extra


def _apply_mcp_timeout(env: dict[str, str]) -> None:
    """Set a 3-minute MCP per-call timeout when the user hasn't picked one.

    CC 2.1.142 fixed `MCP_TOOL_TIMEOUT` so it actually raises the per-request
    fetch timeout for HTTP/SSE MCP servers (was hard-capped at 60s before).
    Browser-heavy roles routinely exceed 60s on first page load, Lighthouse
    audits, or screenshot capture with network idle — leave the env var
    alone if the operator has already set one at the cockpit level.
    """
    env.setdefault("MCP_TOOL_TIMEOUT", _DEFAULT_MCP_TOOL_TIMEOUT_MS)
