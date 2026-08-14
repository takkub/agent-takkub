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
    docs/reviews/2026-07-11-100-mcp-bridge.md). Never touches
    `~/.codex/config.toml` (verified: `-c` overrides are request-scoped,
    not persisted). Every role with an explicit cockpit MCP policy — empty
    *or* non-empty (e.g. a graft-only allowlist) — is deny-by-default: the
    bridge always emits `_CODEX_DISABLE_ALL_MCP_ARGV` (`-c mcp_servers={}
    -c features.plugins=false`) first. Re-verified 2026-08-05 against real
    codex-cli binaries at 0.144.1, 0.145.0, AND 0.146.0 (Windows): a bare
    `-c mcp_servers.<name>.*` PARTIAL override with no `mcp_servers={}`
    prefix merges with codex's own inherited config (`~/.codex/config.toml`,
    project config) rather than replacing it — the original #121 finding —
    but `-c mcp_servers={}` itself fully clears the whole table on all
    three versions, and it STAYS cleared through any partial overrides
    layered on after it (only the explicitly-named servers show up; the
    inherited ones do not come back). That makes the disable-all prefix a
    complete deny-by-default on its own, on every version tested — not
    something that depends on also resolving and disabling each inherited
    name one by one. Because untested older/unknown codex builds could
    still behave like the original #121 finding, `_codex_mcp_argv` keeps
    that per-name resolve-and-disable as defense-in-depth gated by
    `_CODEX_RESOLVE_SAFE_MIN_VERSION`: codex-cli at or above that floor
    skips the resolve subprocess entirely (and can never hard-abort a spawn
    over it); anything older, or whose version can't be parsed, keeps the
    original conservative behaviour (resolve codex's live MCP name list via
    `mcp list --json`, hard-abort the spawn if that fails — see
    `McpResolutionError`). A role with NO cockpit MCP policy at all
    (`role_mcp_allowlist` returns `None`) stays untouched passthrough, same
    as always.
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
import os
import pathlib
import re
import shutil
import subprocess

from ._win_console import SUBPROCESS_NO_WINDOW

_log = logging.getLogger(__name__)

# codex's `mcp_servers.<name>.*` schema (per `codex mcp add --help` /
# `codex mcp list --json` transport shape): only these keys are
# meaningful for a stdio server. Our shared JSON config's `"type"` key
# (used to disambiguate stdio vs http for OUR own tooling) has no codex
# equivalent — stdio is implied by `command` being present — so it's
# dropped rather than forwarded as an override codex doesn't recognize.
_CODEX_SERVER_KEYS = ("command", "args", "env")
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CODEX_DISABLE_ALL_MCP_ARGV = [
    "-c",
    "mcp_servers={}",
    "-c",
    "features.plugins=false",
]

# Oldest codex-cli build empirically confirmed (2026-08-05, real binaries,
# not guessed) to fully clear `mcp_servers` from `-c mcp_servers={}` alone —
# see the module docstring's session_override bullet. codex-cli at or above
# this floor skips `_codex_resolved_mcp_names` entirely: the disable-all
# prefix is already sufficient, so there is nothing left to resolve and
# nothing that can hard-abort the spawn. Anything older, or a version string
# we can't parse, falls back to the pre-existing resolve-and-hard-abort path
# — we have not tested every codex build that has ever shipped, only these
# three (0.144.1, 0.145.0, 0.146.0), so this stays a conservative floor
# rather than a blanket "codex never merges" assumption.
_CODEX_RESOLVE_SAFE_MIN_VERSION = (0, 144, 1)
_CODEX_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class McpResolutionError(RuntimeError):
    """Raised when the "session_override" (codex) adapter can't safely
    resolve which MCP servers to deny.

    Only reachable for a codex-cli older than `_CODEX_RESOLVE_SAFE_MIN_VERSION`
    (or one whose version we couldn't parse): the disable-all prefix already
    covers every verified codex build on its own (module docstring), so
    `_codex_mcp_argv` skips this resolve step — and this error — entirely
    for those. For the older/unverified path, deny-by-default falls back to
    depending on the full inherited name list being known — an incomplete
    or failed resolution there would silently under-deny, handing the pane
    whatever MCPs happen to be in `~/.codex/config.toml`. So this is not
    a degrade-and-continue error: callers (`spawn_engine.py`) must treat
    it as fatal to the spawn attempt, not log-and-proceed like the
    surrounding context-rendering `except Exception` blocks do.
    """


def _codex_resolved_mcp_names(provider_bin: str, cwd: str, env: dict[str, str]) -> list[str]:
    """Ask Codex for config-defined MCP names without loading plugin MCPs.

    Runs once per codex-family spawn, uncached: measured ~180-225ms
    across 3 local runs against a real `codex-cli` binary (2026-08-05),
    negligible next to the rest of the spawn path (PTY/process launch).
    The 5s timeout is a safety ceiling, not the expected case — revisit
    session-level caching only if that assumption stops holding.
    """
    try:
        result = subprocess.run(
            [provider_bin, "-c", "features.plugins=false", "mcp", "list", "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        servers = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise McpResolutionError("could not resolve inherited Codex MCP servers") from exc
    if not isinstance(servers, list):
        raise McpResolutionError("Codex MCP list returned a non-list payload")
    names = [server.get("name") for server in servers if isinstance(server, dict)]
    if not all(isinstance(name, str) and _TOML_BARE_KEY_RE.fullmatch(name) for name in names):
        raise McpResolutionError("Codex MCP list contained an unsupported server name")
    return names


def _codex_cli_version(
    provider_bin: str, cwd: str, env: dict[str, str]
) -> tuple[int, int, int] | None:
    """Best-effort `codex --version` parse, e.g. ``(0, 146, 0)``.

    `None` on any failure (binary missing, timeout, unparseable output) —
    unlike `_codex_resolved_mcp_names`, this never raises: a failed/unknown
    version must fall back to the conservative resolve-and-disable path in
    `_codex_mcp_argv`, not abort the spawn. Only used to decide whether that
    path is even needed (see `_CODEX_RESOLVE_SAFE_MIN_VERSION`).

    Uncached on purpose — `_codex_cli_version_cached` (below) is the real
    entry point every caller should use; this stays the raw, always-shells-
    out primitive so tests can exercise it without the cache getting in the
    way.
    """
    try:
        result = subprocess.run(
            [provider_bin, "--version"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _CODEX_VERSION_RE.search(result.stdout)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


# Every codex-family pane spawn calls `_codex_cli_version` once — measured
# ~363ms on this machine, effectively all of it process-launch overhead for
# a binary that never changes version mid-session. A fan-out of N codex
# panes (parallel/shard spawn) used to pay that cost N times over. Keyed by
# `(provider_bin, mtime-of-resolved-binary)`, NOT just `provider_bin`, so a
# codex-cli upgrade in place at the SAME path (e.g. `npm update -g` between
# cockpit restarts, or an unusually long-lived cockpit session) still gets
# re-probed instead of serving a stale version forever — the mtime changes
# the moment the installer replaces the file, which is far cheaper to check
# (one `os.stat`) than re-running `--version` on every spawn regardless.
# When the binary can't be resolved/stat'd (bare name not on PATH from this
# cwd, or some other OSError), the key's mtime component is just `None` and
# caching still holds for the remainder of THIS process's life — the
# `_codex_cli_version` fallback path (missing binary) is unaffected either
# way, since a `None` in means `None` out, cached or not.
_version_cache: dict[tuple[str, float | None], tuple[int, int, int] | None] = {}


def _codex_cli_version_cached(
    provider_bin: str, cwd: str, env: dict[str, str]
) -> tuple[int, int, int] | None:
    """Cached wrapper around `_codex_cli_version` — see `_version_cache`'s
    comment for the invalidation strategy. This is what `_codex_mcp_argv`
    actually calls."""
    resolved = shutil.which(provider_bin) or provider_bin
    try:
        mtime = os.stat(resolved).st_mtime
    except OSError:
        mtime = None
    key = (provider_bin, mtime)
    if key not in _version_cache:
        _version_cache[key] = _codex_cli_version(provider_bin, cwd, env)
    return _version_cache[key]


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
        if not all(isinstance(k, str) and _TOML_BARE_KEY_RE.fullmatch(k) for k in value):
            raise TypeError("mcp_bridge: invalid TOML inline-table key")
        return "{" + ",".join(f"{k}={_toml_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"mcp_bridge: unsupported TOML literal type {type(value)!r}")


def _role_mcp_servers(
    base_role: str, shard_idx: int | None, project_ns: str, cwd: str | None = None
) -> dict[str, dict]:
    """Resolve the role's effective MCP server set as a plain dict.

    Delegates entirely to `shared_dev_tools.browser_profile_mcp_config_path`
    — the same role-allowlist + browser-profile-isolation + per-pane graft
    `--dir` resolution claude's injection already used — so every provider
    agrees on "what MCPs does this role get" from one place. *cwd* (the
    pane's own working directory) is what lets graft's `--dir` point at the
    right externalized graph store (#146 follow-up); omitting it just means
    graft's args stay untemplated. Returns `{}` on no policy, no master
    config yet, or any read error (never raises — MCP injection is a
    nice-to-have, not a spawn-blocking dependency).
    """
    try:
        from .shared_dev_tools import browser_profile_mcp_config_path

        cfg_path = browser_profile_mcp_config_path(base_role, shard_idx, project_ns, cwd)
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


def _claude_mcp_argv(
    base_role: str, shard_idx: int | None, project_ns: str, cwd: str | None = None
) -> list[str]:
    """`--mcp-config <path> --strict-mcp-config`, or `[]` if the role has
    no MCPs — byte-identical to the pre-#100 inline spawn_engine.py code
    (aside from now threading *cwd* through for graft's per-pane `--dir`)."""
    try:
        from .shared_dev_tools import browser_profile_mcp_config_path

        cfg_path = browser_profile_mcp_config_path(base_role, shard_idx, project_ns, cwd)
    except Exception:
        cfg_path = None
    if not cfg_path:
        return []
    return ["--mcp-config", cfg_path, "--strict-mcp-config"]


def _codex_mcp_argv(
    base_role: str,
    shard_idx: int | None,
    project_ns: str,
    *,
    provider_bin: str | None,
    cwd: str | None,
    env: dict[str, str] | None,
) -> list[str]:
    """`-c mcp_servers.<name>.<key>=<toml>` per server, per allowed key.

    Session-scoped: these argv tokens only affect the one codex process
    they're passed to. Never reads or writes `~/.codex/config.toml`.
    """
    from .shared_dev_tools import role_mcp_allowlist

    servers = _role_mcp_servers(base_role, shard_idx, project_ns, cwd)
    argv: list[str] = []

    # `role_mcp_allowlist` returning None means "no cockpit MCP policy for
    # this role at all" — legacy passthrough, additive-only, never touch
    # what codex already has configured. Any other result (including an
    # EXPLICIT empty frozenset) means the cockpit has an opinion about this
    # role's MCPs, so it must be deny-by-default: a non-empty role allowlist
    # (e.g. graft-only) is just as much a leak risk as an empty one if we
    # only inject the role's own servers — a bare partial `-c
    # mcp_servers.<name>.*` override merges with codex's lower-precedence
    # user/project config rather than replacing it, so anything already in
    # `~/.codex/config.toml` would otherwise still be reachable. The
    # disable-all prefix below closes that on its own (module docstring)
    # — resolving the live inherited name list and disabling each one is
    # extra defense-in-depth for codex builds we haven't verified, so it
    # only runs there (see `_CODEX_RESOLVE_SAFE_MIN_VERSION`).
    if role_mcp_allowlist(base_role) is not None:
        argv.extend(_CODEX_DISABLE_ALL_MCP_ARGV)
        _bin = provider_bin or "codex"
        _cwd = cwd or os.getcwd()
        _env = env or os.environ.copy()
        version = _codex_cli_version_cached(_bin, _cwd, _env)
        if version is None or version < _CODEX_RESOLVE_SAFE_MIN_VERSION:
            names = _codex_resolved_mcp_names(_bin, _cwd, _env)
            for name in names:
                if name not in servers:
                    argv.extend(["-c", f"mcp_servers.{name}.enabled=false"])

    from .pane_tools_policy import _validate_name

    for name, cfg in servers.items():
        if not isinstance(name, str) or not _validate_name(name):
            _log.warning("mcp_bridge: skipping invalid MCP name for codex: %r", name)
            continue
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
    provider_name: str,
    base_role: str,
    shard_idx: int | None,
    project_ns: str,
    *,
    provider_bin: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Extra argv tokens to append for *provider_name*'s MCP injection.

    Dispatches on `PROVIDER_REGISTRY[provider_name].mcp_adapter_variant`
    so the decision of which bridge a provider gets is driven by its
    `ProviderSpec`, not a hardcoded if/else per provider. Returns `[]`
    for any variant with no session-scoped bridge (`"plugin_import"`,
    `"none"`, unknown) — a safe no-op, never raises for those.

    The `"strict"` (claude) and `"session_override"` (codex) variants are
    NOT no-ops: `"session_override"` shells out to the codex binary to
    resolve its deny-by-default list and raises `McpResolutionError` if
    that resolution fails (subprocess error, timeout, malformed output)
    — see `McpResolutionError`'s docstring for why this must be fatal
    rather than degrade-and-continue. Callers spawning a codex-family
    pane must let that propagate to a spawn-abort, not swallow it.
    """
    from .provider_spec import PROVIDER_REGISTRY

    spec = PROVIDER_REGISTRY.get(provider_name)
    if spec is None:
        return []
    variant = spec.mcp_adapter_variant
    if variant == "strict":
        return _claude_mcp_argv(base_role, shard_idx, project_ns, cwd)
    if variant == "session_override":
        return _codex_mcp_argv(
            base_role,
            shard_idx,
            project_ns,
            provider_bin=provider_bin,
            cwd=cwd,
            env=env,
        )
    # "plugin_import" (gemini/agy) and "none" — documented no-op, see
    # module docstring for why plugin_import isn't auto-driven per spawn.
    return []
