"""Per-provider MCP injection adapter (issue #100).

Single source of truth for translating the cockpit's role-based MCP
policy (`pane_tools_policy.py` + `shared_dev_tools.py`'s
`runtime/shared-mcp.json`) into each provider's own wire format.
`spawn_engine.py` calls `mcp_argv_for_provider()` once per provider
branch instead of hand-rolling the claude-only logic that used to live
there — dispatch is driven by `ProviderSpec.mcp_adapter_variant`
(`provider_spec.py`), matching what phase 0 documented but never wired.

Every variant resolves the SAME role→server set as claude's
`--mcp-config` branch already did (`shared_dev_tools.
browser_profile_mcp_config_path` — role allowlist + per-pane browser
profile isolation for playwright/chrome-devtools), then translates it
to that provider's own injection surface:

  - ``"strict"`` (claude): unchanged — `--mcp-config <path>
    --strict-mcp-config`. Byte-identical to the pre-#100 inline code.
  - ``"session_override"`` (codex): codex has native per-session `-c
    mcp_servers.<name>.<key>=<toml-value>` dotted overrides (confirmed
    against the real `codex-cli 0.144.1` binary 2026-07-11 — see
    docs/reviews/2026-07-11-100-mcp-bridge.md). Additive only: never
    touches `~/.codex/config.toml` (verified: `-c` overrides are
    request-scoped, not persisted).
  - ``"plugin_import"`` (gemini/agy): documented no-op. `agy` genuinely
    CAN bridge MCP servers from a Claude-style plugin's `.mcp.json` via
    `agy plugin import <path>` (proven empirically against the real
    `agy 1.1.1` binary), but the staged result lands in
    `~/.gemini/config/plugins/<name>/` — a GLOBAL, machine-wide
    registry with no per-session/per-cwd scoping. Auto-driving that at
    every spawn would (a) leak cockpit-controlled MCP servers into the
    user's own non-cockpit `agy` sessions, and (b) have two different
    roles mapped to agy in the same project clobber each other's
    imported set (worse than the already-documented one-AGENTS.md-per-
    cwd race, since this isn't even per-cwd). Left as a graceful no-op
    until `agy` grows a session-scoped MCP mechanism.
"""

from __future__ import annotations

import json
import logging
import pathlib

_log = logging.getLogger(__name__)

# codex's `mcp_servers.<name>.*` schema (per `codex mcp add --help` /
# `codex mcp list --json` transport shape): only these keys are
# meaningful for a stdio server. Our shared JSON config's `"type"` key
# (used to disambiguate stdio vs http for OUR own tooling) has no codex
# equivalent — stdio is implied by `command` being present — so it's
# dropped rather than forwarded as an override codex doesn't recognize.
_CODEX_SERVER_KEYS = ("command", "args", "env")


def _toml_literal(value: object) -> str:
    """Render *value* as a TOML value literal for a `-c key=value` override.

    Handles exactly the shapes our MCP server configs use: strings,
    lists (of strings), and string->string dicts (inline tables). codex
    parses the value portion of `-c key=value` as TOML, falling back to
    a raw string only when TOML parsing fails — so the literal must be
    valid TOML, not just a shell-safe string.
    """
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={_toml_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"mcp_bridge: unsupported TOML literal type {type(value)!r}")


def _role_mcp_servers(base_role: str, shard_idx: int | None, project_ns: str) -> dict[str, dict]:
    """Resolve the role's effective MCP server set as a plain dict.

    Delegates entirely to `shared_dev_tools.browser_profile_mcp_config_path`
    — the same role-allowlist + browser-profile-isolation resolution
    claude's injection already used — so every provider agrees on "what
    MCPs does this role get" from one place. Returns `{}` on no policy,
    no master config yet, or any read error (never raises — MCP
    injection is a nice-to-have, not a spawn-blocking dependency).
    """
    try:
        from .shared_dev_tools import browser_profile_mcp_config_path

        cfg_path = browser_profile_mcp_config_path(base_role, shard_idx, project_ns)
    except Exception:
        return {}
    if not cfg_path:
        return {}
    try:
        data = json.loads(pathlib.Path(cfg_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _claude_mcp_argv(base_role: str, shard_idx: int | None, project_ns: str) -> list[str]:
    """`--mcp-config <path> --strict-mcp-config`, or `[]` if the role has
    no MCPs — byte-identical to the pre-#100 inline spawn_engine.py code."""
    try:
        from .shared_dev_tools import browser_profile_mcp_config_path

        cfg_path = browser_profile_mcp_config_path(base_role, shard_idx, project_ns)
    except Exception:
        cfg_path = None
    if not cfg_path:
        return []
    return ["--mcp-config", cfg_path, "--strict-mcp-config"]


def _codex_mcp_argv(base_role: str, shard_idx: int | None, project_ns: str) -> list[str]:
    """`-c mcp_servers.<name>.<key>=<toml>` per server, per allowed key.

    Session-scoped: these argv tokens only affect the one codex process
    they're passed to. Never reads or writes `~/.codex/config.toml`.
    """
    servers = _role_mcp_servers(base_role, shard_idx, project_ns)
    argv: list[str] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        for key in _CODEX_SERVER_KEYS:
            if key not in cfg:
                continue
            try:
                literal = _toml_literal(cfg[key])
            except TypeError:
                _log.warning("mcp_bridge: skipping unencodable %s.%s for codex", name, key)
                continue
            argv.extend(["-c", f"mcp_servers.{name}.{key}={literal}"])
    return argv


def mcp_argv_for_provider(
    provider_name: str, base_role: str, shard_idx: int | None, project_ns: str
) -> list[str]:
    """Extra argv tokens to append for *provider_name*'s MCP injection.

    Dispatches on `PROVIDER_REGISTRY[provider_name].mcp_adapter_variant`
    so the decision of which bridge a provider gets is driven by its
    `ProviderSpec`, not a hardcoded if/else per provider. Returns `[]`
    for any variant with no session-scoped bridge (`"plugin_import"`,
    `"none"`, unknown) — always a safe no-op, never raises.
    """
    from .provider_spec import PROVIDER_REGISTRY

    spec = PROVIDER_REGISTRY.get(provider_name)
    if spec is None:
        return []
    variant = spec.mcp_adapter_variant
    if variant == "strict":
        return _claude_mcp_argv(base_role, shard_idx, project_ns)
    if variant == "session_override":
        return _codex_mcp_argv(base_role, shard_idx, project_ns)
    # "plugin_import" (gemini/agy) and "none" — documented no-op, see
    # module docstring for why plugin_import isn't auto-driven per spawn.
    return []
