# Token meter — provider coverage audit (2026-08-31, issue #103)

User directive (2026-08-31): "ปรับปรุง token meter ให้ดีๆ เลย ทั้งบน cockpit และ remote" —
the per-pane context-token meter, previously claude-only, must work for every
registered provider or say explicitly why not.

**Skill note:** the task pointed at `~/.claude/skills/provider-integration/SKILL.md`.
That path does not exist on this machine (`Glob` over `~/.claude/skills/`
confirmed no `provider-integration` directory) — proceeded using this
codebase's own established discipline instead (the "never guess a marker/field
from docs alone" rule already followed throughout `provider_spec.py`, and the
`⚠ NOT yet verified` convention used for every other unconfirmed provider
signal in this repo). `~/.claude/skills/cockpit-ui-style` (referenced for step
6) also does not exist; the pane badge's muted color reuses the existing
`token_meter.usage_color(0.0)` neutral-grey tier already used elsewhere in this
file rather than inventing a new token.

## Result summary

| Provider | Result | Source verified | Confidence |
|---|---|---|---|
| claude | unchanged | n/a — pre-existing, untouched | — |
| codex | **supported** | real rollout JSONL, live on this machine (codex-cli 0.151.0) | confirmed live |
| opencode | **supported** | real `opencode.db` sqlite, live on this machine | confirmed live |
| kimi | **supported** | kimi-cli's own installed typed source (not a live line) | confirmed via source, not a live capture |
| gemini/agy | **unsupported** (armed, always reports so) | real transcript + sqlite store checked, neither carries the data | confirmed absent |
| cursor | **unsupported** (armed, always reports so) | nothing to check — not installed anywhere reachable | not verifiable at all |

Every provider above is now armed (`ProviderSpec.supports_token_meter=True`)
so a non-claude pane's badge is never silently blank the way it was before
this change — "unsupported" is a stated, reasoned outcome now, not silence.

## codex — supported, confirmed live

Real rollout inspected: an isolated-CODEX_HOME session under
`~/.agent-takkub/codex-home/sessions/2026/08/30/rollout-...jsonl` (codex-cli
0.151.0, this machine, 2026-08-30). The relevant line shape (verified
byte-for-byte, not from docs):

```json
{"timestamp":"...","ordinal":139,"type":"event_msg",
 "payload":{"type":"token_count",
   "info":{
     "total_token_usage":{"input_tokens":875871,"cached_input_tokens":693248,
       "cache_write_input_tokens":0,"output_tokens":10146,
       "reasoning_output_tokens":4555,"total_tokens":886017},
     "last_token_usage":{"input_tokens":72866,"cached_input_tokens":0,
       "cache_write_input_tokens":0,"output_tokens":130,
       "reasoning_output_tokens":9,"total_tokens":72996},
     "model_context_window":258400},
   "rate_limits":{...}}}
```

`total_token_usage` is the whole session's cumulative sum (NOT context
occupancy — it only grows); `last_token_usage` is the most recent turn's
actual request, the same thing claude's `input + cache_creation + cache_read`
represents. `token_meter`'s `prompt` = `last_token_usage.input_tokens +
.cached_input_tokens`. `model_context_window` is codex's own live-reported
context cap (258400 for the gpt-5.6-sol session observed) — used directly as
`limit`, no static per-model table needed the way claude's is.

Codex assigns its own session id after boot — there is no `--session-id`
equivalent flag the cockpit passes at spawn (confirmed:
`spawn_engine.py`'s generic provider branch never sets `pane.model.session_uuid`
for any non-claude provider). `codex_helper.resolve_newest_codex_session_for_cwd`
walks the day-sharded `sessions/YYYY/MM/DD/` tree bounded by the pane's own
spawn timestamp, matching `session_meta.cwd` — same isolation caveat as
`token_meter.find_latest_session` (#129): two panes sharing one cwd can't be
told apart by this heuristic. Not an issue for the common case in this cockpit
(each provider pane gets its own worktree).

Implementation: `codex_helper.read_codex_token_usage` (tail-scan-then-fallback,
mirrors `token_meter.read_last_usage`'s own strategy) +
`codex_helper.resolve_newest_codex_session_for_cwd`.

## opencode — supported, confirmed live

Real `opencode.db` inspected (`~/.agent-takkub/opencode-home/data/opencode/opencode.db`,
this machine). An assistant `message.data` row:

```json
{"role":"assistant","modelID":"deepseek-v4-flash-free",
 "tokens":{"total":35822,"input":2985,"output":581,"reasoning":0,
           "cache":{"write":0,"read":32256}}}
```

No separate event stream to scan — the tokens block sits right on the message
row. `prompt` = `input + cache.write + cache.read`. OpenCode reports no
context-window size anywhere in this row, so `limit` is always `None` here —
`format_token_badge` falls back to its per-model table default
(`_DEFAULT_LIMIT`, 200k). Deliberately did NOT add a per-backend table:
opencode fans out to 75+ model backends via `-m provider/model`, and a
guessed size per backend is exactly the kind of invention the schema-drift
discipline in this file forbids. Known gap, not a bug.

Implementation: `opencode_helper.read_opencode_token_usage`. Resolution
reuses the existing `opencode_helper.resolve_opencode_session` (already
supported a `not_before` spawn-time fallback for exactly this "no known
session id yet" case — no new resolver needed).

## kimi — supported, confirmed via installed source, NOT a live line

This machine's kimi-cli has no default model configured (`LLM not set`,
reproduced live via `kimi --print`) — the one real prior recorded session on
this machine shows `TurnBegin` immediately followed by `TurnEnd` with nothing
between them (already documented in `kimi_helper.py`'s own module docstring
before this change). No real `StatusUpdate` wire message has been observed to
verify a live example against.

What WAS verified: kimi-cli's own installed, typed wire-protocol source
(`%APPDATA%\uv\tools\kimi-cli\Lib\site-packages\kimi_cli\wire\types.py`,
`kosong\chat_provider\__init__.py` — the real `uv tool install`ed package on
this machine, not upstream docs):

```python
class StatusUpdate(BaseModel):
    context_usage: float | None = None       # "% of context used"
    context_tokens: int | None = None         # "tokens currently in context"
    max_context_tokens: int | None = None     # "max the context can hold"
    token_usage: TokenUsage | None = None     # "of the current step"
    ...

class TokenUsage(BaseModel):
    input_other: int
    output: int
    input_cache_read: int = 0
    input_cache_creation: int = 0
    @property
    def input(self) -> int:
        return self.input_other + self.input_cache_read + self.input_cache_creation
```

Two things this typed source made explicit that a guess would have missed:

1. **`context_tokens`/`max_context_tokens` are the purpose-built "current
   context fill" fields** — used directly as `prompt`/`limit` rather than
   reconstructed from `token_usage`, which is explicitly scoped to "the
   current step" (one turn's own API cost), a different number.
2. **`StatusUpdate`'s own docstring: "None fields indicate no change from the
   previous status."** A wire.jsonl scan that only reads the single most
   recent `StatusUpdate` line can read `context_tokens: null` if the last
   line only touched e.g. `plan_mode`. `kimi_helper.read_kimi_token_usage`
   folds every `StatusUpdate` in the scanned window, keeping the last
   non-null value per field independently — tested explicitly
   (`test_none_fields_carry_forward_from_earlier_lines`).

This is the same standard `kimi_helper.py`'s existing module docstring already
set for this provider ("the strongest verification available without a
working model") — flip to a real captured-line-verified parser once `kimi
login`/model selection is completed on a real cockpit and a genuine
`StatusUpdate` line can be diffed against this implementation.

## gemini/agy — unsupported, confirmed absent

Checked both storage layouts a live agy pane can use, real files on this
machine:

- **Legacy** `~/.gemini/tmp/<name>/chats/session-*.jsonl` — module comment
  already noted this store stopped being written 2026-06-19 on this machine.
- **Current** `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`
  — checked 5 real transcripts from sessions after 2026-08-20: zero matches
  for `/token/i` or `/usage/i` anywhere in any of them.
- The sqlite conversation store (`~/.gemini/antigravity-cli/conversations/<id>.db`)
  has a `gen_metadata`/`steps`/`trajectory_metadata_blob` schema, but every
  payload column is an **opaque protobuf blob** (confirmed: `gen_metadata.data`
  and `steps.metadata`/`.step_payload` are raw binary, not JSON) — no schema
  available to decode it, and reverse-engineering a proprietary protobuf
  wire format from binary alone is exactly the "guess from nothing" pattern
  this codebase's discipline forbids.

`token_meter.read_pane_usage("gemini", ...)` therefore always answers
`{"status": "unsupported", "reason": "..."}` — a static, reasoned answer, not
a per-tick file scan for a field known not to exist.

## cursor — unsupported, not verifiable at all

`cursor-agent`/`agent` is not installed anywhere reachable from this machine
(`find_cursor_executable()` returns `None`; no `~/.cursor/projects/` transcript
exists to inspect). `cursor_helper.py` already documents the known JSONL
location (`~/.cursor/projects/<cwd>/agent-transcripts/<uuid>/<uuid>.jsonl`) and
a message-shape parser (`parse_cursor_record_message`) for the mobile-mirror
feature, but neither that code nor any upstream source in this codebase names
a token/usage field — there is nothing to verify against, live or typed.
`token_meter.read_pane_usage("cursor", ...)` always answers "unsupported"
rather than guessing a field name from an Anthropic-Agent-SDK-shaped guess.
Revisit once a real cursor-agent transcript is captured on any machine.

## What changed

- `codex_helper.py`: `resolve_newest_codex_session_for_cwd`,
  `read_codex_token_usage`.
- `opencode_helper.py`: `read_opencode_token_usage`.
- `kimi_helper.py`: `read_kimi_token_usage`.
- `token_meter.py`: `resolve_pane_session`/`read_pane_usage` provider
  dispatchers, `_note_schema_drift`/`_log_event` (leaf-safe, `sys.modules`
  proxy pattern — never a static `orchestrator` import).
- `provider_spec.py`: `supports_token_meter=True` for codex/gemini/opencode/
  kimi/cursor (claude unchanged).
- `agent_pane_model.py`: `format_token_badge` now prefers a provider-reported
  `limit` over the per-model table; new `format_unsupported_badge`,
  `record_token_meter_result`, `token_meter_context`.
- `agent_pane.py`: `_refresh_token_meter`/`_apply_token_meter` generalized to
  every provider via the new dispatcher (claude's own resolve path/behavior
  unchanged — still gated on a known `session_uuid`).
- `remote/api.py`: `/api/activity` gained an optional `context:
  {prompt, limit, pct, status}` per lead/role entry (DATA-MIN §7.3 — numbers
  and a status tag only).
- `remote/static/app.js` + `index.html`: Pulse role chips render a small
  `NN%` (or `n/a` for "unsupported") badge. `sw.js` `CACHE_NAME` bumped
  v34→v35 per the standing PWA rule.

## What did NOT change

- claude's `read_last_usage`/`find_session_by_uuid`/`effective_context_limit`
  — byte-identical behavior, verified by the pre-existing `test_token_meter.py`
  suite passing unmodified.
- No non-claude provider gets a `session_uuid` at spawn — that's a
  `spawn_engine.py` gap unrelated to this task's scope, worked around via
  cwd + spawn-timestamp resolution for every other provider instead of fixed.
