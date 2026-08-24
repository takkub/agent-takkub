# OpenViking sidecar setup (#372)

OpenViking (`volcengine/OpenViking`, AGPL) is an optional external context
database — knowledge index + semantic retrieval — Takkub can pull curated
resources from. It is never vendored into this repo (MIT vs. AGPL): the
integration is an HTTP client only, `src/agent_takkub/core/context_sources/
openviking_adapter.py`. See `docs/plans/workspace-master-upgrade-2026-08-24/
14_OPENVIKING_INTEGRATION.md` for the architecture this guide implements.

Takkub stays the control plane (scope, trust, budget, final prompt
composition). OpenViking only ever supplies candidate resources into the
Context Builder's merge step — it never injects into a pane directly.

## Prerequisites

- Either a **managed local install** (recommended — see below, no Docker or
  terminal needed) or a sidecar you already run yourself, reachable over
  HTTP (its own install docs: <https://github.com/volcengine/OpenViking>).
- An Obsidian vault configured via `TAKKUB_VAULT_DIR` (see `docs/guides/
  2026-06-22-vault-second-brain.md`) if you want local resource indexing —
  optional, the sidecar can also be used with zero vault content.

## Managed local (แนะนำ)

Takkub can install and run its own OpenViking instance for you — a
dedicated Python venv under `~/.agent-takkub/services/openviking/` (`pip
install openviking`, no Docker), spawned/stopped/health-polled by Takkub
itself and never touched by anything else. This is the easiest path for
most people: no separate server to set up or keep alive.

**Settings UI (recommended for most people):** Settings → **Knowledge &
Design** → OpenViking panel → **Install & Enable**. From there:

- **Start** / **Stop** / **Restart** control the managed process directly.
- **Repair** recreates the venv from scratch while keeping your config and
  indexed data.
- **Remove** stops and deletes the managed venv; you're asked separately
  whether to also delete config/indexed data (kept by default).
- **Start automatically with Cockpit** boots the managed server whenever
  Takkub starts, instead of requiring a manual Start each time.
- **View Logs** shows the managed process's own stdout.

**CLI (debugging/automation — normal users use the UI above):**

```bash
takkub ov managed status    # installed/version/running/owned/address/health
takkub ov managed install   # create the venv + pip install openviking
takkub ov managed start
takkub ov managed stop
takkub ov managed restart
takkub ov managed doctor    # runs openviking-server's own `doctor` self-check
takkub ov managed update    # explicit-only: pip install --upgrade in the
                             # managed venv; warns (never blocks) on a major
                             # version jump from what was previously installed
takkub ov managed repair    # recreate the venv, config/data preserved
takkub ov managed remove [--purge-data] [--yes]
takkub ov managed studio    # opens the running server's Web Studio (/studio)
                             # in your browser; never auto-starts the server
```

Ownership rules that hold regardless of UI vs. CLI: Takkub only ever kills
a process it itself spawned — an OpenViking you're already running
yourself (or another cockpit session's managed instance) is never touched.
Nothing here auto-installs or auto-starts on boot unless you've explicitly
turned on "Start automatically with Cockpit".

## External server (still supported)

Point Takkub at any OpenViking instance you run yourself instead of the
managed local one — set `TAKKUB_OPENVIKING_URL`, and the managed-runtime
code paths above are skipped entirely (an explicit URL override always
wins, the same way it did before managed-local existed).

## Config

```bash
TAKKUB_OPENVIKING_ENABLED=0            # default: off. Set to 1 to enable.
TAKKUB_OPENVIKING_MODE=shadow          # shadow | read | hybrid
TAKKUB_OPENVIKING_URL=http://127.0.0.1:1933
TAKKUB_OPENVIKING_API_KEY=...          # optional — see "API key" below
```

### Modes

- **`shadow`** (default once enabled) — retrieves from the sidecar and
  records a trace (visible via `takkub doctor`), but never injects
  anything into the actual context sent to a pane. Use this first: it lets
  you compare what OpenViking WOULD have surfaced without changing any
  pane's behavior.
- **`read`** — injects OpenViking's own resource search results
  (`POST /api/v1/search/find`) as a new `### Knowledge (OpenViking)`
  section, budget-permitting. Local Obsidian docs are not queried in this
  mode.
- **`hybrid`** — `read`, plus this repo's own local curated-doc search
  (`core.context_sources.resource_source`, the same allowlist `takkub ov
  index` pushes into the sidecar). Local curated docs outrank external
  OpenViking hits when the budget is tight (`16_CONTEXT_MERGE_POLICY.md`'s
  trust ordering).

### API key

`TAKKUB_OPENVIKING_API_KEY` wins if set. Otherwise the adapter reads a key
from `$TAKKUB_DATA_HOME/openviking/api_key` (the whole file's stripped text
is the key — same convention `core.secrets.backends.file_backend.
FileSecretBackend` already uses for every provider credential file), so a
key never has to live in an env var or a tracked config file:

```bash
mkdir -p "$TAKKUB_DATA_HOME/openviking"
printf '%s' 'your-openviking-key' > "$TAKKUB_DATA_HOME/openviking/api_key"
```

## Indexing your Obsidian vault into the sidecar

`resource_source.py`'s local search works with no sidecar at all (it reads
the vault directly). `takkub ov index` is a separate, opt-in step that
additionally pushes the SAME allowlisted docs (`01-Projects/`, `02-Areas/`
— see `obsidian_boundary.py`; `99-Logs/`, `.obsidian/`, raw transcripts are
always denied) into the sidecar's own knowledge base, so OpenViking's
retrieval can find vault content too:

```bash
takkub ov index              # incremental — re-run any time, unchanged
                              # docs (by content hash) are skipped
takkub ov status              # enabled/mode/health/version/indexed count
```

## Diagnostics

```bash
takkub doctor
```

includes a `[knowledge]`/`[context]` section: whether the sidecar is
enabled, reachable, its reported version against this adapter's pinned
compatible range, indexed-doc count, and the most recent Context Builder
merge trace (per-source item/token counts, dedup count, latency).

A separate `[openviking]` `managed-runtime` row (same info `takkub ov
managed status` prints) reports the managed local install specifically:
installed/version/running/owned/health — independent of whether the mode
above is even enabled, so an installed-but-not-started managed runtime
still shows up.

## Rollback

Set `TAKKUB_OPENVIKING_ENABLED=0` (or leave it unset — that's the default).
Every code path this feature touches checks that flag first and returns
its input completely unchanged when it's off:

- `core.brain.facade.build_context_for_assign` calls the existing
  Brain+Conversation `build_context()` exactly as it did before #372, then
  a merge step that is a same-text no-op while disabled — the context
  actually sent to a pane is byte-for-byte what it was before this
  feature existed.
- Second Brain, Conversation, and Graft are untouched by any of this —
  none of it is a dependency any of those three has on OpenViking.
- No data migration either direction: disabling never leaves the sidecar
  or the local `takkub ov index` bookkeeping (`$TAKKUB_DATA_HOME/
  openviking/index/*.json`) in a state that blocks re-enabling later.
