# Design tool integrations (#373)

Real clients for the optional design-tool integrations named in
`core.capabilities.design_integrations.OPTIONAL_DESIGN_MCPS`: 21st.dev
component reference, Figma, and Penpot. Storybook detection (real,
filesystem-only, always on when a project has it) is unaffected — see
`design_integrations.detect_storybook`.

All three are **default OFF** and go through the same two gates as every
other MCP grant in the cockpit — `pane_tools_policy` (Capability Hub Layer
1) and a credential in `core.secrets.manager.SecretManager`. Nothing
constructs a live client except `design_integrations.build_client`, which
re-checks both gates every call.

## Enable one

```bash
# Lead only:
takkub design integrations enable figma --role designer --token <figma-personal-access-token>
takkub design integrations enable penpot --role designer --token <penpot-access-token> --base-url https://design.example.com
takkub design integrations enable reference-21st --role designer --token <21st-api-key>
```

`--token` is optional on its own (you can grant the policy first and
configure the credential later with a second `enable` call) but a client
cannot be built until both the grant and the credential exist.
`--base-url` is required together with `--token` for `penpot`; optional for
`reference-21st` (see below).

Credentials are stored via `SecretManager` under `SETTINGS_HOME/secrets/`
— never in `projects.json` or any other project file.

## Check status

```bash
takkub design integrations status                 # every integration, config-only (no network)
takkub design integrations status figma --role designer   # + enabled/disabled for one role
takkub design integrations doctor                  # same info, doctor-report formatted
```

`status`/`doctor` never make a network call — they only report whether a
role has the policy grant and whether a credential is stored.

## Disable

```bash
takkub design integrations disable figma --role designer
```

Removes the policy grant only; the stored credential (if any) is left in
place, same as `takkub mcp deny`.

## What each client actually does

- **Figma** (`FigmaClient`) — real, stable REST API
  (`https://api.figma.com`, `X-Figma-Token` header): file summary, local
  variables, components. No gaps.
- **Penpot** (`PenpotClient`) — self-hosted REST/RPC
  (`POST <base_url>/api/rpc/command/<name>`, `Authorization: Token
  <token>` header). `get_profile` (health check) follows Penpot's own
  documented worked example; `get_file` follows the same convention but
  its exact shape wasn't independently confirmed against a live instance —
  it degrades to `None` on any mismatch instead of guessing.
- **21st.dev** (`TwentyFirstClient`) — 21st.dev has no confirmed public
  REST search endpoint. Its real, working integration is the official MCP
  server (package `@21st-dev/magic`) — `takkub design integrations enable
  reference-21st` also registers that server in the shared MCP config
  (`register_twentyfirst_mcp`), with the API key referenced as
  `${TWENTY_FIRST_API_KEY}`, never written literally. `TwentyFirstClient.
  search`/`get_inspiration` stay available as an opt-in direct HTTP path
  for an operator-supplied `--base-url` (e.g. a self-hosted proxy); without
  one they report "not configured" rather than guessing a URL.

Every client method is fail-open: a timeout, a non-2xx response, or a
response that doesn't match the expected shape all degrade to `None` (or
an empty tuple) and a warning log line — never an exception, never a
guessed result. Every returned record carries `Provenance`
(source/url/license/fetched_at) so a caller building a pane prompt can
render it as clearly-labeled, **untrusted** external content, not paste it
in as if it were the agent's own output.

## Known limits

- No client here makes a network call from the Qt GUI thread — none of
  this module has a Qt dependency at all (`core/` cannot import PyQt6), so
  any UI-facing caller is responsible for calling off the GUI thread, same
  as every other blocking call already documented that way in `core/`.
- Enabling `reference-21st` does not itself launch the MCP server — a role
  only gets to use it once BOTH the policy grant and `pane_tools_policy`'s
  normal spawn-time `--mcp-config` wiring apply, same as any other MCP.
- Penpot's `get_file` endpoint shape is unconfirmed against a live
  instance; treat a `None` result as "endpoint didn't match", not
  "file doesn't exist".
