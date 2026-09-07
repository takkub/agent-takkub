# Boot-context token audit — issue #516

Scope: engine/measurement half of #516 only (backend). "ต้องได้ #1" — measure
first, real numbers, no guessing. Frontend's parallel task in this batch does
the role-file diet that acts on these numbers.

## 0. Method

Two independent, real (not synthetic) sources, cross-checked against each other:

1. **Ground truth**: this very backend pane's own transcript
   (`%CLAUDE_CONFIG_DIR%\projects\takkub-project-agent-takkub\<session>.jsonl`,
   `CLAUDE_CONFIG_DIR=C:\Users\monch\.claude-work` for this pane) — the first
   `assistant` record's `message.usage` field, which the API bills for real:
   ```json
   {"input_tokens": 2, "cache_creation_input_tokens": 37338,
    "cache_read_input_tokens": 33707, ...}
   ```
   `cache_read_input_tokens` (33,707) is Claude Code's own fixed
   system-prompt+tool-schema prefix — shared/cache-hit across ANY session on
   this machine in the last hour, matching the "~33.7k prefix" the user
   already measured. `cache_creation_input_tokens` (37,338) is what's unique
   to THIS pane's boot — everything cockpit + this repo injects. Total first
   turn: **71,045 tokens**, matching the observed "70–72k" almost exactly.
   Two more `attachment` records in the same transcript give exact real
   sizes for two more built-in-Claude-Code contributors: a `skill_listing`
   attachment (8,025 chars, 16 skills) and a `deferred_tools_delta`
   attachment (214 chars, 18 tool names) — both cache_creation, not
   cache_read, since they're filtered per-project/per-role.

2. **Static reconstruction**: `takkub doctor --boot-context [--role R]`
   (new, this task) reads the exact same files/calls the exact same
   functions `spawn_engine.spawn()`'s non-Lead branch calls when it builds
   `role_md_file`, without spawning a pane. Implementation:
   `src/agent_takkub/boot_context.py` (leaf module — spawn_engine.py imports
   PyQt6 at module level, so this module deliberately does NOT import it).

Run it yourself: `PYTHONPATH=src py -3 -m agent_takkub.cli doctor --boot-context --project agent-takkub`
(or `--role backend` for one role). Every number below came from that exact
command against this repo on 2026-09-07 — re-run it to get today's numbers,
they drift as role files/memory grow.

## 1. Real per-role numbers (static reconstruction, lower bound — see §2)

| role | role_file_base | learned_notes | guards | graft_caveats | perm_gate | global_claude.md | mcp | **cockpit total (naive tok)** |
|---|---|---|---|---|---|---|---|---|
| frontend | 3,977 | 45 | 662 | 259 | 447 | 223 | 0 | **5,234** |
| backend | 3,489 | 30* | 662 | 0 | 447 | 223 | 0 | **4,852** |
| mobile | 3,942 | 30* | 662 | 259 | 447 | 223 | 0 | **5,222** |
| devops | 4,309 | 30* | 662 | 259 | 447 | 223 | 0 | **5,931** |
| qa | 4,489 | 30* | 662 | 259 | 447 | 223 | 0 | **6,111** |
| reviewer | 3,470 | 30* | 429† | 259 | 447 | 223 | 0 | **4,859** |
| critic | 4,029 | 30* | 662 | 259 | 447 | 223 | 0 | **5,651** |

\* `learned_notes` shows as `learned_notes_empty_pointer` (30 tok fixed
skeleton) for every role except frontend right now — those roles' learned
notes were archived/cleared recently, not that they have none historically
(see `role-memory/agent-takkub/*-archive.md`, up to 150KB each — real
accumulated knowledge, just not in the live inject).
† reviewer is in `_NO_FILE_EDIT_ROLES` (`role_needs_stale_file_guard`
returns False) so it skips `STALE_FILE_GUARD`.

`skill_matrix_appendix` is 0 for every role above — the Skill Matrix
(`skill-policy.json`) has no role assigned any skill in this project right
now, so this lever (#103 phase 4) is fully available and completely unused.

Plus, **shared across every role, not role-specific**:

- `native_project_memory`: **14,743 chars (~3,685 naive tok)** at
  `C:\Users\monch\.claude-work\projects\takkub-project-agent-takkub\memory\MEMORY.md`
  — see finding **F1** below, this is the single highest-leverage fix found
  in this pass.
- Claude Code's own `skill_listing` attachment: 8,025 chars (~2,006 naive
  tok), 16 skills — global catalog, independent of `skill-policy.json`
  (see finding **F2**).

## 2. The naive estimator is wrong by ~3.5x for this repo — do not trust it for absolute budgets

Summing every category above for `backend` (the pane that produced the
ground-truth transcript in §0): role appendix (4,852) + native memory
(3,685) + skill listing (2,006) = **10,543 naive tokens**. Real
`cache_creation_input_tokens` for that exact pane's first turn: **37,338
tokens**. The naive `chars // 4` estimator (`boot_context.estimate_tokens`,
same convention as the 3 existing copies of `_CHARS_PER_TOKEN = 4` in
`core/context_sources/base.py`, `core/brain/context_builder.py`,
`core/brain/retrieval.py`) is short by **~3.5x**.

Two things this is NOT: it is not a bug in the reconstruction (every
category above is a real file read or a real function call, not a guess),
and it is not fully explained by content this pass didn't measure yet (the
per-spawn task block, git status, environment block, and Claude Code's own
dynamic-section boilerplate make up some of the remaining ~26.8k tokens, but
Thai-heavy text is also known to tokenize far denser than 4 chars/token —
this repo's role files, CLAUDE.md, and memory are majority Thai).

**Consequence for #516 point 5 (CI ceiling)**: the ceiling test added in
this pass (`tests/test_boot_context_ceiling.py`) ratchets the exact same
`chars`/naive-`tok` numbers this doc reports — valid for catching *regressions*
(a role file growing) but the absolute "≤15k" target in the issue must be
tracked against real `cache_creation_input_tokens` from `usage_ledger`
transcripts, not this estimator, until a second real calibration point
(ideally a non-Thai-heavy role, or a `claude --print --output-format json`
diff-probe per category) narrows the correction factor. Flagged, not
silently "fixed" with an invented multiplier.

## 3. Findings

### F1 — Claude's native `/memory` auto-load is shared across every role of a project (fix candidate, ~3.7k naive tok / likely ~13k real tok per pane)

`pane_env.claude_project_dir_name(project_ns)` sets
`CLAUDE_CODE_PROJECT_DIR_NAME` to `f"takkub-project-{project_ns}"` — **the
same value for every role's pane** — purely so every pane's transcript lands
under one findable directory name. Claude Code's own built-in `/memory`
feature (the "# auto memory" system-prompt section, a Claude Code product
feature, not cockpit's) keys its per-project `MEMORY.md` off that exact same
env var. Side effect: EVERY role's pane on a project — frontend, backend,
qa, devops, all of them — auto-loads and is instructed to maintain the
IDENTICAL memory index, currently 14,743 chars of what reads as Lead's own
release/session history (issue numbers, version bumps, prod status — not
generic backend/frontend/qa knowledge). This duplicates cockpit's own
`role_memory.py` design (per-role, lazy-pointer, #33), which exists
specifically to avoid this.

**Not fixed in this pass** (measure first, per the task ordering) — fix
candidates for a follow-up: (a) stop setting
`CLAUDE_CODE_PROJECT_DIR_NAME` for non-Lead teammate roles (loses the
"findable transcript dir" benefit for teammates — check if anything reads
it), or (b) give each role its own suffix
(`f"takkub-project-{project_ns}-{base_role}"`) so `/memory` naturally
partitions per role instead of pooling into one, or (c) if Claude Code ships
a flag to disable native `/memory` outright for a session, prefer that for
teammate panes (cockpit already has its own better-scoped mechanism).
Needs a real read of Claude Code's `/memory` docs for the exact off-switch —
not found in `claude --help` (see §4).

### F2 — global skill catalog (16 skills, 8,025 chars) loads regardless of Skill Matrix policy

This is Claude Code's own built-in skill-discovery listing (the
`skill_listing` attachment, distinct from `skill_policy.py`'s
`skill-policy.json` role appendix, which is currently unused — §1). It is
filtered by *something* already (16 shown here, not "35" as this project's
own SESSION GOAL text estimated for `~/.claude/skills`) — likely a
project/relevance filter Claude Code itself applies, not a cockpit knob.
`--disable-slash-commands` (§4) is the only discovered lever to drop this
entirely for a role that needs zero skills; there is no discovered
per-skill or per-role granular switch from the CLI side.

### F3 — real, shipped CLI levers relevant to this issue (from `claude --help`, this machine's installed version)

Not wired into spawn_engine yet — listed here as concrete follow-up items
for #516 point 2, not implemented in this pass:

- **`--disable-slash-commands`** — "Disable all skills." All-or-nothing per
  session, but exactly the knob point 2 asked whether exists. Candidate: add
  it to a role's argv when `skill_policy.effective_skills(role)` is empty
  AND the role has no other skill dependency — saves the full
  `skill_listing` (F2) for that pane.
- **`--setting-sources <user,project,local>`** — scope which
  `settings.json` layers load. A teammate pane currently inherits whatever
  `user`-scope settings.json the operator's own account carries (plugins,
  hooks) in addition to the project's — restricting to `project` (or
  `project,local`) for teammate panes is a candidate for point 3's "plugins
  default off" without touching `pane-tools.json` at all.
- **`--exclude-dynamic-system-prompt-sections`** — moves cwd/env
  info/memory paths/git status out of the system prompt into the first user
  message. Does not shrink the total, but improves cross-pane prompt-cache
  reuse (more of the prefix becomes shareable `cache_read` instead of
  per-pane `cache_creation`) — a real, low-risk, already-shipped lever for
  the "70k tokens, only 33.7k shared" ratio, worth a follow-up experiment.
- **`--bare` / `--safe-mode`** — both too broad (also drop cockpit's own
  CLAUDE.md/role-file injection), not usable as-is for a teammate pane.
- MCP tool-schema cost is NOT visible from `--mcp-config`'s file on disk —
  only from a live handshake. The one existing calibration point in this
  codebase is `spawn_engine.py`'s own comment: browser MCPs (playwright/
  chrome-devtools) cost **~15k tokens** of tool schemas, which is why
  they're role-gated to qa/critic/designer already. No new browser-MCP
  session was spawned to re-verify that figure (would burn real quota) —
  cited as an existing, already-documented data point.
- `backend`/`frontend`/`mobile`/`devops`/`qa`/`reviewer`/`critic` currently
  resolve **zero** `--mcp-config` servers in this repo's live
  `pane-tools.json` (confirmed by `measure_mcp_config`, and independently by
  `takkub doctor`'s own `[mcps]` section) — this project isn't currently
  paying any per-pane MCP tool-schema cost outside the browser roles.

## 4. Explicitly out of scope for this pass (point ordering: measure before fix)

- **Point 2 (skills gate knob)** — `--disable-slash-commands` exists (F3);
  wiring it into spawn_engine per-role policy is a follow-up, not done here.
- **Point 3 (plugin usage from transcripts)** — not measured this pass; a
  real transcript scan (`usage_ledger`-style, 30-day window) of
  superpowers-dev/pordee/ui-ux-pro-max/addy skill/hook invocation counts per
  role is needed before defaulting anything off. Flagged as a follow-up
  measurement task, not guessed at here.
- **Point 4 (idle-pane reuse / `--resume`)** — no engine change in this
  pass.
- **Point 5 (CI ceiling)** — implemented (`tests/test_boot_context_ceiling.py`
  + `docs/audit/boot-context-baseline.json`), but see §2's caveat: it
  ratchets naive/relative regressions, not an absolute real-token budget.
- **Point 6 (multi-provider)** — `boot_context.py` only measures the claude
  `--append-system-prompt-file` path. codex/gemini/opencode/kimi/cursor boot
  through `skill_policy`'s `agents_md_file` bridge instead (a different
  mechanism, no --append-system-prompt-file, no native `/memory` env-var
  trick) — measuring those needs a separate reconstruction against each
  provider's own AGENTS.md/session-config convention. Not done this pass;
  real gap, tracked here rather than silently skipped.

## 5. Deliverables in this pass

- `src/agent_takkub/boot_context.py` — the measurement engine (leaf module).
- `doctor.check_boot_context` + `takkub doctor --boot-context [--role R] [--project P]`.
- `docs/audit/boot-context-baseline.json` — per-role byte-count baseline for the CI ceiling test.
- `tests/test_boot_context_ceiling.py` — targeted regression test.
- This document.
