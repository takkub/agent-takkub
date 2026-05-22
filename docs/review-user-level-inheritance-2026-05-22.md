# Review: User-level plugin + MCP inheritance - 2026-05-22

## Summary

Prefer Approach A over flipping `TAKKUB_SETTING_SOURCES` back to `user,project,local`.
The v0.2.1 rollback rationale still applies: the cached `claude-obsidian` 1.4.3
plugin still ships prompt hooks on `SessionStart`, which is the hook shape that
previously broke every cockpit pane with `ToolUseContext is required for prompt hooks`.
The current `ensure_user_mcps()` path is also too broad for the security model:
it re-materializes auth-bearing user MCP config, including the PMS bearer entry,
into `runtime/shared-mcp.json` and passes that one file to every Claude pane.

## Risks & blind spots

1. `claude-obsidian` should not be added to `_SAFE_PLUGINS` yet. The current
   cached 1.4.3 hooks file still declares `SessionStart` with a `type: "prompt"`
   hook and its plugin README documents plugin-hook behavior problems. The
   changelog and orchestrator comments record the same 1.4.3 prompt-hook crash
   as the reason v0.2.1 reverted user settings inheritance. Until a real
   cockpit spawn smoke for this exact Claude Code version succeeds with the
   plugin enabled, assume the breakage is still live.
2. Merging PMS back into `runtime/shared-mcp.json` is a security regression,
   not a changed threat model. `.gitignore` ignores the whole `runtime/`
   directory, which reduces commit leakage but does not protect secrets at
   rest. On this machine the runtime directory and shared config are owned by
   the user and accessible to the user, admins, system, and the Codex sandbox
   ACL group; every cockpit Claude pane also receives the same config path via
   `--mcp-config` and can read its own filesystem-visible config.
3. `~/.claude.json` currently has top-level user MCPs beyond browser tools:
   `obsidian-vault`, `postgres-pms`, and an HTTP `pms` server with an
   Authorization header. `ensure_user_mcps()` filters by MCP type and browser
   name only, so it copies the bearer-bearing HTTP entry as designed. Redacted
   log strings do not change the plaintext JSON exposure.
4. Do not rely on `claude mcp list` as the protection boundary. A local
   `claude mcp list` check timed out while health-checking MCPs, so I did not
   verify whether its display prints HTTP headers. Even if the list view hides
   headers, the pane still gets a plaintext config file and the PMS server is
   exposed as an available remote tool unless further filtered.
5. Approach B widens the blast radius from known tooling to every present and
   future user plugin, hook, permission layer, and MCP setting. v0.2.0 already
   demonstrated that one global plugin regression can turn a cockpit default
   into a pane-start failure across projects.

## Recommendation

Use Approach A, but narrow it further:

- Keep `_SAFE_PLUGINS` explicit. Adding `ecc` is reasonable if the current
  hook mute stays in pane env and the plugin is smoke-tested in a spawned pane.
- Do not add `claude-obsidian-marketplace` to `_SAFE_PLUGINS` yet. Gate that
  on a regression test or manual spawn smoke that exercises the 1.4.3
  `SessionStart` prompt hook under cockpit spawn flags.
- Do not merge all top-level user MCPs blindly. Keep browser MCPs cockpit-owned
  and introduce an explicit user-MCP allowlist or an opt-in policy that excludes
  secret-bearing remote MCPs by default. PMS needs a separate decision, ideally
  an env-based or credential-reference path that does not persist its bearer in
  a shared runtime JSON copied to every pane.

For ECC specifically, the mute looks effective for the two named noisy hooks
even when ECC is loaded via `--plugin-dir`: `_apply_ecc_mute()` mutates the
process env before Claude is spawned, ECC's hook dispatcher reads
`ECC_DISABLED_HOOKS` from `process.env` at hook execution, and GateGuard also
reads `ECC_GATEGUARD` from `process.env`. The mute does not suppress ECC's
separate `SessionStart` hook or any future ECC hook ids, so ECC still needs a
spawn smoke when it becomes default.

## Edge cases backend ต้องระวัง

- Browser MCP collisions should prefer cockpit entries, not user entries. The
  cockpit pins browser versions and startup behavior deliberately; a user-level
  `playwright` or `chrome-devtools` entry with different flags should not
  silently replace that contract.
- Top-level `~/.claude.json.mcpServers` and per-project
  `projects.<path>.mcpServers` are different scopes. If the goal is
  user-level inheritance only, read only the top-level object as the current
  implementation does; importing project-scoped entries into every cockpit
  pane would cross project trust boundaries.
- Built-in Claude connectors such as `claude.ai Google Drive` do not appear as
  ordinary top-level stdio/http/sse entries in the inspected config. The
  inspected project records carry disabled connector names separately under
  `projects.<path>.disabledMcpServers`; do not forward those through
  `--mcp-config`.
- Type filtering alone is not a secret policy. HTTP `headers`, stdio `args`,
  and `env` can all carry credentials. The current local config proves both
  HTTP headers and stdio args can contain sensitive material.
- User MCP removal is not handled by a merge-only file unless stale entries are
  pruned. If a user deletes or rotates a server in `~/.claude.json`, decide
  whether shared config is regenerated from policy or can retain old entries.
- Invalid JSON or unreadable config should remain non-fatal, but the startup
  log needs to make the loss of inherited MCPs visible enough to diagnose.
- Name-only skip logs are appropriate. Avoid serializing full user MCP configs
  into logs, errors, test snapshots, or docs.

## Open questions

- What is the intended trust boundary for non-browser user MCPs in teammate
  panes: all user tools, Lead-only tools, project allowlists, or opt-in per MCP?
- Should PMS ever be available inside cockpit panes by default after the
  prior security review removed it, or should it stay standalone-only until a
  non-plaintext credential path exists?
- Which smoke test will be accepted as proof that the `claude-obsidian` 1.4.3
  `SessionStart` prompt-hook failure no longer breaks cockpit panes?
