# opencode 2.x — per-pane model, instructions and effort (#1056, #1052, #1049)

Probed 2026-11-26 on Windows with `@opencode/cli` 2.4.2 installed into a
scratch prefix (isolated `XDG_*` homes, no login) next to the machine's global
`opencode-ai` 1.19.5.

## Package (#1049)

| npm package | version | bin |
|---|---|---|
| `@opencode/cli` | 2.4.2 | `opencode`, `opencode2` |
| `opencode-ai` | 1.19.6 (frozen 1.x line) | `opencode` |

Both ship a bin named `opencode`. The cockpit now installs/updates
`@opencode/cli` and removes a still-installed `opencode-ai` first.

## Model and instructions per pane (#1056)

- 1.x TUI takes `-m provider/model`. 2.x TUI has no `--model` (only
  `opencode run -m provider/model#variant`).
- 2.x clients talk to **one background server** (`opencode service …`,
  `http://127.0.0.1:28894`). `--standalone` = "Run with a private server
  instead of the background service".
- `OPENCODE_CONFIG_CONTENT` is read by the **server**:

| step | env on the client | `opencode debug config` shows |
|---|---|---|
| first client (starts the shared server) | `model: opencode/env-model` | `env-model` |
| second client, server already up | `model: opencode/env-model2` | `env-model` (ignored) |
| `debug config --standalone` | `model: opencode/env-model5` | `env-model5` |

So with the shared server every pane gets whatever the first client's
environment held — not its own model, and not its own role instructions (they
ride the same variable). Fix: the cockpit adds `--standalone` when the
installed opencode is 2.x (`opencode_helper.private_server_argv`, version read
from the npm `package.json`, `--version` as fallback). 1.x rejects the flag and
gets nothing.

A model id without a provider prefix (`deepseek-v4-pro`) is not something
opencode resolves; the spawn now drops it (event `spawn_model_id_rejected`) so
the CLI default applies instead of an unusable id.

Not verified: an authenticated 2.x TUI session end to end (no login on the dev
machine).

## Effort (#1052)

- No `--effort` flag on 1.19.5 or 2.4.2.
- `opencode models` on 2.4.2 lists 10 models (logged out), none with a
  `#variant` suffix. The reporting machine listed 72 on 2.3.2, also none.
- `effort_flag` therefore stays `None`; Settings now says "provider นี้ยังไม่รองรับ
  effort" instead of blaming the model (#1053).

Re-check when a model shows a `#variant` in `opencode models`.
