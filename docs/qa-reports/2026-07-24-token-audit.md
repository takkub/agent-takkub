# Cockpit token audit — 2026-07-24

## Executive summary

The dominant cost is not fresh user input. Across the rolling seven-day
window, **98.2514% of prompt tokens were cache reads**:

| Prompt component | Tokens | Share of prompt |
|---|---:|---:|
| `cache_read_input_tokens` | 4,245,836,776 | 98.2514% |
| `cache_creation_input_tokens` | 75,382,112 | 1.7444% |
| `input_tokens` | 183,193 | 0.0042% |
| **Prompt total** | **4,321,402,081** | **100%** |
| `output_tokens` | 15,205,739 | — |
| **Prompt + output** | **4,336,607,820** | — |

The actionable order is:

1. Compact/roll long sessions around 200k prompt tokens.
2. Remove the extra model round-trip used only to read a task handoff file.
3. Batch Lead inbox traffic (`done` + peer CC) into short digests.
4. Keep MCP schemas role-scoped and lazy.
5. Make auto-reminders non-turn-triggering.

Trimming answer verbosity is not a first-order win here: output was only
15,205,739 tokens, or 0.3506% of total traffic.

## Scope and method

- Window: **2026-07-17 09:10:28 through 2026-07-24 09:10:28**
  (Asia/Bangkok; rolling seven days).
- Transcript roots:
  - `C:\Users\monch\.agent-takkub\claude-config\projects`
  - `C:\Users\monch\.claude\projects`
- Cockpit identity came from `session_report` records in the installed and
  dev `runtime/events.log` files. The `[ROLE:]` task prefix was the fallback.
- 332 JSONL files had an mtime in the window; 330 contained usable usage and
  mapped to a cockpit session. Of those, 313 had API requests in the exact
  timestamp window and 312 had their first request in the window.
- The final cohort contained **25,329 unique API requests**.
- Claude writes multiple assistant JSONL rows for one response (for example,
  separate `thinking` and `tool_use` blocks with the same usage). Deduplicating
  by `requestId`, with `message.id` as fallback, removed **24,324 duplicate
  assistant rows**. Summing JSONL rows directly would therefore be badly wrong.
- Sidechain rows were explicitly excluded; this cohort contained zero.
- Usage parsing follows `src/agent_takkub/token_meter.py`: prompt is
  `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.

Frozen aggregate artifact (outside the repo, analysis script not committed):

`C:\Users\monch\.agent-takkub\runtime\token-audit-2026-07-24.json`

SHA-256:
`13141FF0C36C9A3265518AE66DFCA2FB74FC7688B3BF48AF0892471FC8F9A705`

## Where the tokens went

### By pane role

Role totals include the pane's real work as well as cockpit overhead. They
show where optimization has leverage, not blame.

| Role | Sessions | Requests | Total tokens | Share | Tokens/session | Median first prompt |
|---|---:|---:|---:|---:|---:|---:|
| backend | 91 | 8,142 | 1,440,785,746 | 33.2238% | 15,832,810 | 60,876 |
| lead | 13 | 3,077 | 1,140,714,090 | 26.3043% | 87,747,238 | 54,534 |
| frontend | 94 | 6,583 | 877,681,419 | 20.2389% | 9,337,036 | 61,818 |
| qa | 67 | 5,029 | 624,609,657 | 14.4032% | 9,322,532 | 60,663 |
| devops | 52 | 1,873 | 173,598,581 | 4.0031% | 3,338,434 | 60,270 |
| designer | 4 | 342 | 53,476,279 | 1.2331% | 13,369,070 | 56,527 |
| critic | 5 | 219 | 21,342,381 | 0.4921% | 4,268,476 | 50,269 |
| reviewer | 4 | 64 | 4,399,667 | 0.1015% | 1,099,917 | 43,016 |

Lead is the standout: only 13 sessions produced 26.3% of all traffic and
averaged 237 requests and 87.7M tokens per session. That is the signature of
long-lived context being reread, not expensive first boot.

### Largest individual sessions

| Session | Project / role | Requests | Cache read | Total tokens |
|---|---|---:|---:|---:|
| `1886e7f5` | wash-locker / lead | 1,158 | 525,332,305 | 532,179,492 |
| `85268d40` | agent-takkub / lead | 598 | 232,476,070 | 237,827,076 |
| `27d94a1d` | TK-ERP / lead | 346 | 142,943,084 | 146,680,937 |
| `5345f1f2` | wash-locker / backend | 263 | 97,821,098 | 98,620,881 |
| `54164473` | pms / lead | 261 | 72,474,675 | 75,502,627 |

The largest Lead session alone used 532.2M tokens, over 28 times the combined
first-request prompt volume of all 312 newly started sessions.

## First-spawn overhead

The first API request of 312 new sessions contained:

| Metric | Value |
|---|---:|
| Prompt tokens, total | 18,454,763 |
| Prompt tokens, median | 60,446 |
| Prompt tokens, P25–P75 | 54,793–63,057 |
| Output tokens, total | 51,360 |
| Prompt + output | 18,506,123 |
| Share of all seven-day prompt traffic | 0.4271% |

Cache behavior splits the same roughly 60k envelope two ways:

- 120 cold starts: median prompt 60,777, almost entirely cache creation
  (median 60,775).
- 192 warm starts: median prompt 60,270, composed of median cache read 30,503
  plus median cache creation 29,730.

This is the only exact token-level spawn decomposition available from the
transcripts. The JSONL does **not** serialize the individual Claude base system
prompt, appended role file, project `CLAUDE.md`, or rendered Lead context.
Consequently, assigning exact token counts to those individual pieces would be
fabricated.

What the transcript does expose as exact, non-token source sizes before the
first request:

| Recorded startup material | Aggregate characters across 312 sessions |
|---|---:|
| `skill_listing` attachments | 3,095,216 |
| hook additional context | 902,560 |
| MCP instruction blocks | 61,561 |
| first user injection | 138,040 (median 291/session) |

These character counts are evidence about relative bulk only. They are not
converted to tokens because no Claude tokenizer count for each component is
present in the source data.

### Task-file handoff round-trip

301 of the 312 new sessions began with the cockpit pointer telling the agent
to read the task spec from a file. The first request used to issue that read
consumed:

- 17,872,549 total tokens across 301 sessions.
- Median 60,677 tokens per affected session.
- Mean 59,377 tokens per affected session.

The task body still has to enter context once, so this is specifically the
cost of the extra model round-trip, not the task's own content. A provider-side
attachment/preload path that retains reliable large-payload delivery but does
not require the model to decide to call Read can remove this round-trip.

## MCP tool-definition load

The transcript has `deferred_tools_delta` / `mcp_instructions_delta`
attachments, but tool deltas contain tool names rather than serialized schema
bodies. Therefore the pure schema-token cost cannot be isolated exactly.

Observed facts:

| Metric | Value |
|---|---:|
| MCP delta attachment events | 582 |
| Sessions with an MCP delta | 263 |
| Unique requests following a delta | 324 |
| Startup / late follow-up requests | 63 / 261 |
| Total usage of all delta-following requests | 23,216,832 |
| Cache creation in all delta-following requests | 5,069,477 |
| Total usage of late delta-following requests | 19,648,405 |
| Cache creation in late delta-following requests | 2,550,795 |
| Median late-request cache creation | 1,356 |
| Recorded MCP instruction text | 190,790 characters |

Tool-add event counts were led by chrome-devtools (1,537), Playwright (1,311),
claude-in-chrome (1,122), context7 (398), and notebooklm (120). These are
counts of tool names added across events, not token counts.

The 2,550,795 late-request cache creation tokens are a measured
**request-level ceiling**, not a claim that all of them came from MCP schemas;
the same request can also cache newly appended conversation/tool-result text.

## Cockpit housekeeping

For each injected message, the table counts the next unique assistant request.
The request totals are exact. They are not claimed to be the isolated token
length of the message itself.

| Kind | Events | Sessions | Median message chars | Follow-up prompt | Follow-up total |
|---|---:|---:|---:|---:|---:|
| done notice | 300 | 7 | 347 | 128,844,409 | 129,091,628 |
| peer CC | 35 | 11 | 564 | 9,874,705 | 9,923,416 |
| goal block | 190 | 189 | 469 | 11,739,681 | 11,784,058 |
| auto-reminder | 12 | 4 | 303 | 1,183,387 | 1,185,228 |

Interpretation:

- Done notices are the largest identifiable cockpit-triggered request class,
  but most are legitimate handoffs. The opportunity is batching, not deletion.
- Goal blocks are small (median 469 characters) and preserve cross-pane scope.
  Their next-request totals include reading and acting on the whole task spec,
  so those 11.8M tokens must not be attributed to the goal text alone.
- Auto-reminders are rare, but every observed reminder was followed by another
  full-context model request.

## Ranked reductions

### 1. Compact or roll sessions at a 200k prompt ceiling

A replay simulation capped every observed request at 200,000 prompt tokens:

- 6,462 requests exceeded the cap in 84 sessions.
- Observed excess above 200k: **828,764,173 prompt tokens**.
- **9,866,240 tokens per affected session**, or 2,511,407 averaged over all
  330 cockpit sessions.

This is an idealized upper estimate: it does not subtract the one-time compact
request/rewrite. Even after that cost, it is an order of magnitude larger than
the other opportunities. Trigger compaction/rollover from the same
`last_usage.prompt` value already read by the token meter, with special
attention to persistent Lead sessions.

### 2. Deliver long task files without a model-driven Read hop

The 301 pointer sessions spent **17,872,549 tokens** on their first
pointer-to-Read round-trip: median **60,677 tokens/session**.

Keep file-based reliability, but make the provider/cockpit preload or attach the
file contents before inference. Do not return to multi-kilobyte PTY pastes.

### 3. Digest Lead inbox traffic for 60 seconds

A deterministic replay grouped `done` and peer-CC events arriving no more than
60 seconds apart, retained the final request in each burst, and removed the
earlier requests:

- 335 source requests; 10 multi-event bursts.
- 12 requests removable.
- **5,083,492 observed tokens saved**.
- **1,270,873 per affected Lead session**, or 15,405 averaged over all
  cockpit sessions.

Done-only batching saved 2,034,382 tokens; combining done + CC captures more.

### 4. Tighten MCP role gates and lazy loading

Late MCP-delta follow-ups created 2,550,795 cache tokens. Spread across the 263
MCP-observed sessions, the measured request-level ceiling is **9,699
tokens/session**; the median late event was 1,356 cache-creation tokens.

Prioritize the three browser tool sets: load them for browser QA/critic roles,
not general backend/frontend/Lead work. Add component-level schema telemetry
before claiming a larger saving from the 23.2M total associated request volume.

### 5. Make auto-reminders non-turn-triggering

The 12 reminders caused 1,185,228 tokens of follow-up traffic across four
affected sessions: **296,307 tokens per affected session** (3,592 averaged over
all cockpit sessions).

Prefer a UI badge/host-side notification, or inject only when there is evidence
the prior task turn completed without `takkub done`. Do not wake the model just
to tell it to ignore the reminder when still working.

## What not to cut first

- Output verbosity: 0.3506% of total traffic.
- Goal blocks: only 469 characters median and high coordination value.
- Blindly trimming role/Lead/`CLAUDE.md` text: the aggregate first prompt is
  measurable, but the JSONL cannot attribute tokens to those subcomponents.
  Add prompt-component token telemetry first.
- Cache itself: cache reads are cheaper than uncached input economically, but
  the 98.25% share proves that reducing repeated context size and request count
  is where the raw-token leverage is.

## Measurement caveats

1. Request association means “the next assistant request after this injected
   record,” not causal token attribution inside Anthropic's serialized prompt.
2. The 60-second digest and 200k cap are replay simulations over actual request
   usage. Their policy thresholds are explicit; their token inputs are not
   estimates.
3. A transcript modified in the window can have older history. Totals include
   only assistant requests whose own timestamp is inside the exact window.
4. No character-to-token conversion was used anywhere.
5. The analysis was read-only against `src/`; only this report is tracked.
