# graft token economics — measured, not guessed (2026-08-06)

Evaluator: devops · scope: measure the real MCP-schema cost and the real Read/Grep/Glob
savings ceiling from actual session logs, then close the infra L-items (package.json,
doctor, README). No `graft_store.py` / `graft_autobuild.py` / `shared_dev_tools.py` /
`mcp_bridge.py` / `orchestrator.py` / `main_window.py` edits — those are backend/frontend's
files; this report only informs decisions there.

**Bottom line: net-positive, by a wide margin, for every role that already carries graft in
`_ROLE_MCP_POLICY`.** The fixed schema cost is ~0.5% of a typical session's real token spend
even in the worst case (never called once); the ceiling of what it could save is ~14%.
No code change to the injection policy is recommended — see "Decision: doctor auto-install"
below for why.

---

## 1. MCP schema cost — measured against the real server, not estimated

Spawned the actual `graft mcp` stdio server (`graft 0.8.2`, the pinned version in
`shared_dev_tools._GRAFT_MCP_VERSION`) and sent it a real MCP `initialize` +
`tools/list` JSON-RPC handshake — same protocol a pane's Claude Code process uses.
Script: `runtime/exports/2026-08-06/agent-takkub/devops/mcp_probe.mjs` (newline-delimited
JSON framing — the server does **not** use LSP-style `Content-Length` framing, confirmed by
trial: the header-framed probe timed out, the newline-delimited one returned in <1s).

Raw response captured verbatim: `runtime/exports/2026-08-06/agent-takkub/devops/tools_list.json`
(6 tools: `graft_find_code`, `graft_file_api`, `graft_check_freshness`, `graft_trace_calls`,
`graft_find_all`, `graft_repo_map`).

Token count: installed `tiktoken` into the project `.venv` (removed after use — not a
project dependency), measured with `cl100k_base` and cross-checked with `o200k_base`:

| | chars | cl100k_base | o200k_base |
|---|---:|---:|---:|
| **all 6 tools, whole `tools/list` result** | 3,436 | **847** | 846 |
| `graft_trace_calls` (largest) | 1,083 | 266 | 265 |
| `graft_find_code` | 715 | 175 | 175 |
| `graft_find_all` | 663 | 163 | 164 |
| `graft_repo_map` | 432 | 100 | 99 |
| `graft_file_api` | 341 | 91 | 91 |
| `graft_check_freshness` (smallest) | 179 | 47 | 48 |

**~847 tokens** is the real, reproducible fixed cost of the schema (both tokenizers agree
within 1 token — the number isn't tokenizer-sensitive). This is a floor: Claude's own native
tool-definition wire format may add a small constant overhead per tool that a raw-JSON count
can't see from outside the API, but 847 tokens is the right order of magnitude and the right
number to reason about relative costs with.

For scale, re-measured the comparison the pilot doc used (`docs/audit/2026-08-05-graft-pilot.md`)
on the **current** `orchestrator.py` (it has grown since that pilot — 218,089 chars now vs
218,447 bytes then) with the same real tokenizer instead of the chars/4 heuristic:

| | chars | cl100k_base tokens |
|---|---:|---:|
| full `orchestrator.py` | 218,089 | 49,425 |
| `graft skeleton orchestrator.py --no-refresh` | 12,506 | 3,707 |
| **reduction** | | **92.5% (45,718 tokens saved) — one file, one call** |

So a single `graft_file_api`/skeleton call on a large file recovers the fixed 847-token
schema cost **~54×** over. The schema only "loses" if a pane holding graft never calls any
of its 6 tools even once in a session that also never reads a file graft would have compressed
well — see §3 for how often that actually happens.

## 2. Real ceiling: what fraction of a pane's total tokens is Read/Grep/Glob-attributable

**Method.** Sampled real session transcripts from `~/.claude/projects/` (not `runtime/sessions/`,
which holds terminal-output logs, not the structured tool-call JSONL the token math needs).
31 sessions: the 15 most-recently-modified in the main project directory, plus the most recent
session from 6 `backend` worktrees, 6 `frontend` worktrees, and every available `qa` (1),
`reviewer` (1), `maintainer` (2) worktree — `mobile`, `critic`, `codex` had no worktree sessions
to sample. This is a convenience sample (recency + role coverage), not exhaustive — 65 total
agent-takkub-related session directories exist; deeper coverage would need a full pass, which
wasn't run here (time-boxed). File lists + raw per-file output are in
`runtime/exports/2026-08-06/agent-takkub/devops/` (`all_sessions.txt`, `session_analysis2.jsonl`,
`by_role.mjs`) for anyone who wants to extend the sample.

Script: `analyze_sessions.mjs`. For every session, it:

1. Sums the **real** `usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens
   + output_tokens** across every assistant turn — this is Anthropic's own metered total for
   the session, not an estimate.
2. For every `tool_result`, looks up the preceding `tool_use`'s name and buckets its content
   length (chars) as **Read/Grep/Glob** or **other tool**.
3. Computes a **resend-weighted** estimate: once a tool_result lands in context it gets
   re-sent (as `cache_read`) on every subsequent turn until the session ends (this ignores
   mid-session compaction, so it's a lower bound on the true resend cost, not an upper bound).
   `weighted_tokens = chars/4 × (turns_remaining_after_this_result)`.

**First-render number is misleading on its own.** Read/Grep/Glob content is 74.6% of all
tool-result bytes in the sample, but only **0.11%** of total real session tokens — because
first-render size ignores that content gets paid for again on every later turn via cache_read,
which is what actually dominates a long session's token bill.

**Resend-weighted number is the real ceiling:**

| Scope | sessions | Read/Grep/Glob weighted share of real usage |
|---|---:|---:|
| **All 31 sampled** | 31 | **14.3%** |
| main project dir (mixed: Lead + non-worktree roles) | 15 | 13.5% |
| `backend` | 6 | 15.3% |
| `frontend` | 6 | 15.3% |
| `maintainer` | 2 | 18.4% |
| `qa` | 1 | 1.4% (single-session, not reliable) |
| `reviewer` | 1 | 2.8% (single-session, not reliable) |

**Reading this number correctly:** ~14% is the ceiling of what graft's 6 tools *could* ever
save — only if they fully substituted for every Read/Grep/Glob call in these sessions, which
they don't (graft has no visibility into non-code files, config, logs, or anything outside
its indexed graph; `graft callers` also has a confirmed false-negative on type-annotated
instantiations — `docs/audit/2026-08-05-graft-pilot.md`). Treat 14% as the addressable budget,
not the expected savings.

## 3. Fixed cost vs. addressable budget — net economics

Computed the *worst case* directly: a pane that holds the graft schema for its entire session
and never calls any of the 6 tools even once. Since MCP tool schemas ride along on every API
call the same way system-prompt content does, their cost compounds via `cache_read` turn over
turn exactly like a Read/Grep/Glob result would (§2's mechanism, applied to the fixed 847
tokens instead of a tool_result).

```
unused_schema_tokens_over_session ≈ 847 × turns_in_session
```

Summed across the same 31-session sample (turns ranged 6–421, mean 133.4):

| | tokens |
|---|---:|
| Σ unused-schema cost (847 × turns), all 31 sessions | 3,501,498 |
| Σ real total usage, same 31 sessions | 627,755,693 |
| **worst-case unused-schema share** | **0.56%** |

So: **ceiling ≈ 14%, worst-case fixed cost ≈ 0.56%** — roughly a 25:1 ratio. Even a pane that
calls graft's tools only occasionally (not on every applicable read) comes out ahead, and the
`_ROLE_MCP_POLICY` roles (frontend/backend/mobile/devops/qa/reviewer/critic — all code-reading
roles) are exactly the population most likely to hit graft-shaped queries. Roles that don't
read code at all (`lead`, `designer`'s non-code tools, `codex`) are correctly excluded from
the policy already (`shared_dev_tools.py:692-701`) and pay **zero** schema cost — the file's
own comment already states the design intent: *"Roles with no MCPs keep an explicit EMPTY
policy so they skip `--mcp-config` entirely (no schema tokens)."*

One caveat that matters more than the average: **the MCP gate (commit `31e3599`) already means
most of this worst case never happens.** `browser_profile_mcp_config_path` drops the `graft`
server from a pane's config entirely unless the CLI is on PATH *and* the project's graph has a
completed build *and* the pane isn't a worktree checkout. A fresh install that never ran
`takkub doctor --fix` pays **none** of the 0.56% — the schema simply isn't injected. The
0.56% worst case only applies to a machine that *has* opted into graft (ran `--fix`, graph
built) but then has an individual session that happens not to touch it — a narrower and less
likely population than "any graft-eligible pane."

## Decision: keep `doctor --fix`-gated install (no auto-install by default)

Task asked whether `doctor` should install graft's CLI unconditionally now that the MCP is
"injected by default." Decision: **no change** — keep it behind `--fix`, mirroring
`check_mini_browser`'s existing pattern. Reasoning:

- The token math above argues *for* turning graft on more widely, but disk/install footprint
  is a separate axis the token numbers don't cover: `npm install -g @nanonets/graft` is an
  ~87MB global package (`docs/audit/2026-08-05-graft-pilot.md`), and `graft_autobuild`'s boot
  sweep builds a graph **per distinct project path** — 46 dirs on the audit machine, up to
  600s each. That is a real, non-token cost a plain read-only `takkub doctor` should not
  impose without the user opting in, same reasoning that already keeps `mini-browser` and
  provider installs behind `--fix`/`--install-providers`.
- The MCP gate (§3) already makes "CLI absent → zero schema cost" true today, so there's no
  token-cost argument for forcing the install — the current design already avoids paying for
  the feature until a user has asked for it.
- If this changes later (e.g. graft becomes a hard requirement), it should be a deliberate
  product decision with its own review, not a side effect of a token-economics pass.

**One thing worth doing and out of scope here:** `check_graft`'s WARN-when-missing message
currently just says the CLI is missing + how to fix it. A follow-up could cite the ~92%
skeleton-compression number from §1 so the opt-in decision is informed — didn't do it in this
pass to avoid scope creep past what was asked; flagging for whoever picks it up next.

## Closed: `doctor` now reports graft store size

`doctor.py` gained `_graft_store_size_finding()`, called from `check_graft()` right after the
CLI/graph findings. Reuses `disk_usage.scan_graft_graphs()` (the same aggregation `takkub disk`
and `takkub prune --include-live` already use) instead of duplicating the walk — so the
200MB oversized-store threshold (H1, `docs/reviews/2026-08-05-graft-crossos-audit.md`) stays
defined in exactly one place. Previously a user had to run `takkub disk` separately to see
this; now a plain `takkub doctor` (no `--fix`, no live cockpit needed) shows it:

```
[graft]
  ✓ cli               0.8.2  C:\Users\monch\AppData\Roaming\npm\graft.cmd
  · graph              a project that hasn't run `graft build` yet returns empty-but-valid answers...
  ✓ store-size         1 live store(s), 61 MB total
```

Verified live against this repo's own real store (built via the cockpit's git-filtered staging
path, not a manual unfiltered build — confirmed the 61 MB number matches the H1-fixed store
size, not the pre-fix 463 MB one). No new finding when no store exists yet (`INFO`, not `WARN`
— a fresh install shouldn't look broken). `WARN` only fires when `scan_graft_graphs` already
flags a store as oversized, reusing that exact classification rather than inventing a second
threshold.

Import-linter: no contract restricts `doctor.py`'s own imports (its docstring's
"leaf-modules-pure module" line describes it being a *forbidden target* for other pure leaves,
not a source under that contract — checked `pyproject.toml`'s `leaf-modules-pure` contract:
`source_modules` is `[config, _win_console, roles, system_baseline]`, doctor.py isn't in it).
`doctor → disk_usage` introduces no new coupling either — both already depend on
`shared_dev_tools`. Verified: `lint-imports` → **23/23 contracts kept**.

## Closed: package.json `engines.node` — documented, not bumped

Left `engines.node` at `>=18` (unchanged) rather than bumping to `>=20`. Reasoning: `>=18` is
still an accurate floor for the cockpit **as a whole** — Node 20 is only required for the
optional graft feature, which already degrades gracefully (MCP gate drops it silently; no
crash, no broken boot) on Node 18–19. Bumping `engines.node` globally would force every
cockpit user onto Node 20 for a feature most of them may never opt into, which is broader than
the actual constraint. Instead, documented the gap explicitly (this was the audit's L1 ask:
*"the Node-20 requirement belongs in README/install docs"*) — see below.

`doctor.check_graft` already had this covered for the live-check path (WARN when Node <20 and
graft CLI resolvable); this closes the static-docs half of the same gap.

## Closed: README

Added a `graft` row to the "Token efficiency & memory" table (what it does, the 92% number,
that it's opt-in, that it needs Node ≥20, that the graph lives outside the repo, and the
`TAKKUB_SKIP_GRAFT_BUILD=1` kill switch — verified this env var is real:
`graft_autobuild.py:108,171,508`) and a line under **Requirements** stating the Node ≥20 gap
explicitly instead of leaving `engines.node >=18` and graft's real floor silently
contradicting each other.

## Reproducing these numbers

```bash
# MCP schema cost
node runtime/exports/2026-08-06/agent-takkub/devops/mcp_probe.mjs <target-dir> > tools_list.json
python -m pip install tiktoken   # throwaway, uninstall after
python runtime/exports/2026-08-06/agent-takkub/devops/count_tokens.py tools_list.json

# skeleton compression, current file
graft --dir <store> build src/agent_takkub
graft --dir <store> skeleton --no-refresh orchestrator.py src/agent_takkub

# session ceiling (needs a file list — see all_sessions.txt for the sample used here)
node runtime/exports/2026-08-06/agent-takkub/devops/analyze_sessions.mjs <file1.jsonl> ...
node runtime/exports/2026-08-06/agent-takkub/devops/by_role.mjs
```

## Verification

- `.venv\Scripts\python.exe -m pytest tests/test_doctor.py -q` → all passed (no regressions
  from `_graft_store_size_finding`).
- `.venv\Scripts\lint-imports.exe` → 23/23 contracts kept.
- `ruff check` + `ruff format` on `doctor.py` → clean.
- `_graft_store_size_finding()` smoke-tested against this repo's real, live store (61 MB,
  built via the cockpit's actual staging path — not the throwaway unfiltered build used for
  §1's skeleton measurement, which was deleted after use).
- `tiktoken` uninstalled from `.venv` after measuring — not left as a project dependency.

## Not done / left for whoever picks this up next

- Session-log ceiling sample is 31/65 available session directories, recency+role-coverage
  sampled rather than exhaustive — a full pass would tighten the 14% number's confidence
  interval, especially for `qa`/`reviewer` (n=1 each here).
- `check_graft`'s WARN message doesn't yet cite the §1 savings number to make the opt-in
  decision more informed (noted in the doctor decision section above).
- No live macOS measurement — schema/tokenizer numbers are platform-independent (pure text),
  but the session-log sample and the disk-store numbers are from this Windows machine only.
