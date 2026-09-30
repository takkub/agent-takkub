# Issues #783 and #784

## Claude discovery

Verified with installed Claude CLI 2.1.284 on Windows, 2026-09-30.
`claude --print --input-format stream-json --output-format stream-json --verbose --no-session-persistence`
received exactly one SDK control request: `initialize`, request ID `model-discovery`.
No user message, prompt, or generation request was sent. EOF ended the probe (exit 0).
The matching successful control response contained a `models` list with `value`,
`resolvedModel`, `displayName`, and effort capabilities. Account/credential fields
were not saved. The probe honors CLI auth and custom model mappings, so it does not
require a separate Anthropic API key for Claude subscription users.

The public Models API is a separate auth path; we do not assume Claude Max OAuth
credentials can call it. Discovery failure retains the cached/snapshot picker.
Claude pins remain unchanged during discovery. Settings normalizes dotted version
spellings only for `claude-*` IDs; custom backend names remain intact.

Sources: [Claude SDK reference](https://platform.claude.com/docs/en/agent-sdk/typescript),
[Models API](https://platform.claude.com/docs/en/api/http/models),
[model IDs](https://platform.claude.com/docs/en/models/overview).

## Output directories

Codex receives `--add-dir` for the exact project artifacts and docs directories,
including Lead and resumed sessions. The macOS/Linux workspace-write policy and
Windows policy remain compatible. No enclosing runtime directory is granted.
Creation failures select an existing temporary directory before spawn; explicit
screenshot paths in done notes can reference this fallback.
Other providers retain their existing output-directory environment contract.

Tests cover spawn argv for Windows/macOS/Linux and Lead/teammate, temp fallback,
active catalog caching, malformed/failed discovery, ID normalization on Save,
and fallback warnings/events before the first assistant usage record. No real
macOS sandbox session was launched on this Windows machine; CI verifies macOS
regressions and the argument contract.
