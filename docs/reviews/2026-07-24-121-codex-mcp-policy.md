# Issue #121 — Codex MCP policy leak

Date: 2026-07-24
Scope: trace, fix, and targeted argv tests for Codex panes

## Proven leak paths

### A. Cockpit injection: proven root cause

Before this fix, `shared_dev_tools._ROLE_MCP_POLICY` did not contain
`codex`, although its comment said Codex/Gemini were classified. The resulting
call chain was:

1. `spawn_engine` calls `mcp_argv_for_provider("codex", base_role, ...)`.
2. `_role_mcp_servers()` asks
   `browser_profile_mcp_config_path("codex", ...)`.
3. `effective_mcps("codex", _ROLE_MCP_POLICY.get("codex"))` returns `None`.
4. `None` means legacy master passthrough, so the full
   `runtime/shared-mcp.json` is selected.
5. `_codex_mcp_argv()` converts every server into additive
   `-c mcp_servers.<name>.*=...` arguments.

The live machine's master file contained four servers during the trace:
`playwright`, `chrome-devtools`, `context7`, and `notebooklm`. Its
`~/.takkub/pane-tools.json` had no `codex` override. Cockpit therefore injected
all four despite the intended default policy.

This directly explains why `chrome-devtools` appeared in issue #121.

### B. User Codex configuration: no direct `mcp_servers`, but a plugin MCP

The live `%USERPROFILE%\.codex\config.toml` contained no
`[mcp_servers]` or `[mcp_servers.<name>]` table during the trace. Thus a direct
user `mcp_servers` entry was not the source of the four cockpit servers.

It did contain an enabled `[plugins."github@openai-curated"]` entry. On
Codex CLI 0.145.0, `codex mcp list --json` showed the plugin-bundled `github`
MCP even though the config had no direct MCP table. Four cockpit-injected
servers plus this one user plugin server reproduce the reported startup count
of five.

Codex's documented configuration precedence puts CLI `-c` overrides above
project and user config. Local probes established two important distinctions:

- `-c mcp_servers={}` does **not** remove a server from a lower-precedence
  user `config.toml`; Codex merges the empty table with the lower table.
- `-c mcp_servers.<resolved-name>.enabled=false` keeps that server from booting.
- The plugin-bundled `github` MCP remained.
- `-c features.plugins=false` suppresses the plugin-bundled MCP source.

References:

- [Codex advanced configuration](https://developers.openai.com/codex/config-advanced)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)

## Fix

The fix preserves the policy's three states instead of collapsing them:

- non-empty allowlist: inject the selected cockpit MCP servers exactly as before;
- `None`: retain legacy master passthrough for genuinely unclassified roles;
- explicit empty allowlist: run Codex's read-only `mcp list --json` resolver
  with plugins disabled, append
  `-c mcp_servers.<name>.enabled=false` for every inherited config server,
  and append `-c features.plugins=false` for plugin-bundled servers.

If the resolver fails, spawn fails closed instead of launching a pane whose
MCP policy cannot be proven. `mcp_servers={}` remains in argv as a clean
top-level session value, but the per-name `enabled=false` overrides are what
neutralize lower-precedence user/project entries.

`codex` now has a built-in empty MCP policy, matching the intended default.
The Codex deny arguments are session-scoped and never edit user files.

Disabling the plugin feature also suppresses non-MCP components from user Codex
plugins for that invocation. Codex 0.145.0 exposes no reliable generic
session override that disables only every plugin-bundled MCP while retaining
the rest of every unknown plugin. The absolute invariant requested by #121
("no MCP means boot no MCP") therefore takes precedence. Roles with a non-empty
MCP allowlist are unchanged.

## Cross-provider audit (#103)

| Provider | Same user/global leak possible? | Current Takkub enforcement |
|---|---|---|
| AGY 1.1.6 | Yes. AGY loads global customizations/plugins; imported plugin MCP state is machine-wide. Its startup help exposes no MCP deny-all flag. | Gap flagged in `provider_spec.py`; `plugin_import` remains a no-op to avoid mutating global state. |
| OpenCode 1.18.4 | Yes. Global, project, and inline configs are merged, and `mcp` entries survive unless individually overridden. `--pure` suppresses external plugins, not configured MCPs. | Gap flagged in `provider_spec.py`; no generic deny-all adapter yet. See [OpenCode config](https://opencode.ai/docs/config/). |
| Kimi 1.49.0 | Yes. The installed CLI auto-loads its user MCP file; current Kimi docs also describe user/project `mcp.json`. `--mcp-config` and `--mcp-config-file` add config and do not document a deny-all startup switch. | Gap flagged in `provider_spec.py`; no adapter yet. See [Kimi MCP docs](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html). |
| Cursor | Yes. Cursor CLI automatically detects the IDE's `mcp.json`. Current docs expose interactive MCP enable/disable, but no verified deny-all launch flag. The binary was not installed on the trace machine. | Gap flagged in `provider_spec.py`; no adapter yet. See [Cursor CLI MCP](https://docs.cursor.com/en/cli/using). |

These gaps are not silently treated as enforced policy: each provider's
`mcp_adapter_variant` comment now names the inheritance route and references
#103/#121.

## Tests

Targeted coverage is in:

- `tests/test_mcp_bridge.py`
  - default Codex policy emits both deny overrides;
  - every MCP name resolved from lower config gets `enabled=false`;
  - explicit-empty non-Codex role backed by Codex emits both overrides;
  - non-empty Codex allowlist retains the existing per-server translation.
- `tests/test_spawn_codex_argv.py`
  - Windows, macOS, and Linux spawn argv all contain the deny overrides;
  - a role that allows `demo` receives its MCP overrides without deny flags;
  - a missing cockpit master file still blocks inherited user/plugin MCPs.
