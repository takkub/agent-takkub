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

- A running OpenViking sidecar reachable over HTTP (its own install docs:
  <https://github.com/volcengine/OpenViking>). Nothing in this repo starts
  or manages that process — point Takkub at wherever you already run it.
- An Obsidian vault configured via `TAKKUB_VAULT_DIR` (see `docs/guides/
  2026-06-22-vault-second-brain.md`) if you want local resource indexing —
  optional, the sidecar can also be used with zero vault content.

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
