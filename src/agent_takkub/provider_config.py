"""Per-role CLI provider mapping.

The cockpit can spawn teammate panes backed by Claude Code
(`claude.exe`), OpenAI Codex (`codex.CMD`), or Google Gemini CLI
(`gemini`). By default every role except `codex` and `gemini` runs
claude. This module lets the user override the mapping globally —
e.g. "backend always uses codex regardless of project" — by editing
a small JSON file under `~/.takkub/`.

Resolution rules:
- `lead`   → always `claude` (cockpit infrastructure assumes claude
             for Lead: CLAUDE.md auto-discovery, --append-system-prompt,
             session-resume `--continue`, token-meter JSONL, etc.)
- `codex`  → always `codex` (the role's whole point)
- `gemini` → always `gemini` (the role's whole point)
- everything else → user config wins; default `claude`

Config file: `~/.takkub/role-providers.json`. Created on first read
if missing (empty `{}`). Hand-edit to override:

    {"backend": "codex", "qa": "gemini"}

`provider_for("backend")` then returns `"codex"`. Restart cockpit
to pick up changes (no live reload in v1).
"""

from __future__ import annotations

import json
from pathlib import Path

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
VALID_PROVIDERS = frozenset({CLAUDE, CODEX, GEMINI})

# Roles whose provider is hard-coded — cannot be overridden by config.
# Lead has too much claude-specific plumbing (CLAUDE.md, JSONL token
# meter, --continue resume). The `codex` role's identity IS codex.
_FORCED_PROVIDER = {
    "lead": CLAUDE,
    "codex": CODEX,
    "gemini": GEMINI,
}

_CONFIG_PATH = Path.home() / ".takkub" / "role-providers.json"


def config_path() -> Path:
    """Where the per-role provider mapping lives. Module-level constant
    exposed as a function so tests can monkeypatch the path."""
    return _CONFIG_PATH


def load_providers() -> dict[str, str]:
    """Return the user's role→provider mapping. Creates an empty file
    on first call if the config doesn't exist yet, so the user has a
    file to discover and edit. Invalid JSON or non-dict content is
    treated as empty (silent recovery — never blocks spawn)."""
    path = config_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        except OSError:
            return {}
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Sanitize: drop entries with unknown providers so a typo in the
    # JSON doesn't silently route a role to nothing.
    return {
        str(role).lower(): str(provider).lower()
        for role, provider in data.items()
        if str(provider).lower() in VALID_PROVIDERS
    }


def save_providers(mapping: dict[str, str]) -> None:
    """Write the mapping back to disk. Best-effort: raises only if the
    user's home dir is unwritable (very rare). Caller passes the full
    desired mapping — partial updates aren't supported."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {
        str(role).lower(): str(provider).lower()
        for role, provider in mapping.items()
        if str(provider).lower() in VALID_PROVIDERS
    }
    path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")


def provider_for(role: str) -> str:
    """Resolve which CLI backs the given role.

    Returns one of `"claude"` or `"codex"`. Forced for `lead` and
    `codex`; consulted from `~/.takkub/role-providers.json` for
    everything else; defaults to `"claude"` when the role isn't in
    the config.
    """
    key = (role or "").lower().strip()
    if key in _FORCED_PROVIDER:
        return _FORCED_PROVIDER[key]
    mapping = load_providers()
    return mapping.get(key, CLAUDE)


def _provider_available(provider: str) -> bool:
    """True iff `provider` can actually run right now.

    Two ways a codex/gemini provider becomes unusable:
      1. Toggled off in the cockpit status bar (`disabled-providers.json`).
      2. Its CLI isn't installed (binary not on PATH).

    `claude` is always considered available (it's the cockpit's baseline;
    if claude itself is missing the spawn fails far louder elsewhere).
    Imports are lazy so this stays a thin per-role config module with no
    hard dependency on provider_state / the CLI helpers at import time.
    """
    if provider == CLAUDE:
        return True
    # (1) user-intent toggle
    try:
        from .provider_state import is_disabled

        if is_disabled(provider):
            return False
    except Exception:
        pass
    # (2) CLI actually installed
    try:
        if provider == GEMINI:
            from .gemini_helper import find_gemini_executable

            return find_gemini_executable() is not None
        if provider == CODEX:
            from .codex_helper import find_codex_executable

            return find_codex_executable() is not None
    except Exception:
        return False
    return True


def effective_provider_for(role: str) -> str:
    """Resolve which CLI will *actually* back the role this spawn.

    Like `provider_for()` but degrades a codex/gemini role to `claude`
    when that provider is unavailable — toggled off OR not installed.
    The role keeps its identity (a "gemini" pane is still a "gemini"
    pane); only the engine behind it changes. This is the "Claude รับ
    ตำแหน่งแทน" substitution: an assigned codex/gemini slot never fails
    or refuses — Claude fills it instead.

    `provider_for()` answers "which CLI is *configured* for this role"
    (static identity); this answers "which CLI is *usable* right now"
    (runtime). Spawn-time decisions should use this one.
    """
    desired = provider_for(role)
    if desired == CLAUDE:
        return CLAUDE
    return desired if _provider_available(desired) else CLAUDE
