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

**Correction (2026-09-07, follow-up pass):** `--setting-sources` was NOT an
open candidate — it is already wired (`spawn_engine.py`, `sources =
os.environ.get("TAKKUB_SETTING_SOURCES", "project,local")`, feeding
`assemble_claude_argv`'s `setting_sources`), defaulting every claude pane
(Lead and teammates alike) to `project,local` — `user`-scope
`~/.claude/settings.json` is already excluded. It was added for an unrelated
reason (working around a crashing claude-obsidian SessionStart hook), not
for this issue, but it already delivers the effect this section originally
proposed as a candidate. This audit doc's original text was wrong on this
point; corrected here rather than silently reworded.

Still NOT wired into spawn_engine — real follow-up items for #516 point 2:

- **`--disable-slash-commands`** — "Disable all skills." Investigated this
  pass and deliberately NOT wired: this flag is all-or-nothing per session
  and disables Claude Code's entire built-in skill catalog (F2's 16-skill
  `skill_listing`, e.g. `artifact-design`, `debug-mantra`,
  `provider-integration` — general-purpose skills every role can invoke,
  not just Skill-Matrix-assigned ones). Gating it on
  `skill_policy.effective_skills(role)` being empty would fire for EVERY
  role in this project right now (the Skill Matrix has no assignments at
  all — §1), silently removing the entire built-in skill catalog from every
  teammate pane project-wide. The 30-day usage scan below shows real,
  non-trivial use of some of those skills — shipping this blind was judged
  too risky for this pass. Needs an explicit decision from whoever owns
  which built-in skills teammates should keep, not an automatic token-diet
  toggle.
- **`--setting-sources <user,project,local>`** — see correction above;
  already delivers this.
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

## 6. Follow-up pass (2026-09-07, second session) — F1 fix, F2 fix, #516b addendum, plugin-usage measurement

### F1 fixed: non-Lead roles now get their own transcript dir

`pane_env.claude_project_dir_name(project_ns, base_role=None)` and
`inject_claude_project_dir_name_env(..., base_role=None)` now suffix the
directory (`takkub-project-<project>-<role>`) for every role except Lead
(`base_role` `None`/`"lead"` keeps the exact old, unsuffixed name — Lead's
`--resume`/session continuity and `remote/notify.py`'s resume picker are
keyed off that historical value and must not move). Threaded through
`spawn_engine.py`'s claude branch: the env injection call, and both
`_resume_uuid_matches_cwd`/`_resume_uuid_matches_provider_cwd` (which
reconstruct the same directory name to validate a caller-picked
`resume_uuid`, and would otherwise silently look in the wrong directory for
a non-Lead role and reject a legitimate resume). `boot_context.
measure_native_project_memory(project_ns, base_role=None)` and
`build_report` updated to read each role's own memory file instead of
Lead's shared one — every non-Lead role now reports `None` for
`native_project_memory` until that role is actually respawned under its new
directory name (expected, not a bug — no pane has run under the new naming
yet at the time of this pass). Tests: `tests/test_resume_session_picker.py`
`TestClaudeProjectDirNameRoleSuffix` (4 new tests) + existing suite kept
green (3 test lambdas needed their arity updated for the new optional
`base_role` parameter — behavior unchanged, no production regression).

Real-token impact of this fix is NOT independently re-measured against a
fresh transcript in this pass (would need spawning an actual non-Lead pane
under the new naming and burning real quota) — the ~13k real-token/pane
estimate from §1/F1 above stands as the projected saving, not a re-verified
number. Flagged, not silently assumed.

### F2 fixed: Thai-weighted token estimator, all 4 copies unified

New `src/agent_takkub/token_estimate.py` (pure, stdlib-only `re` — same
leaf-module bar as `boot_context.py`) replaces the 4 independent
`_CHARS_PER_TOKEN = 4` copies (`core/context_sources/base.py`, `core/brain/
context_builder.py`, `core/brain/retrieval.py`, `boot_context.py`) with one
shared `estimate_tokens(text)`: non-Thai characters keep the exact old
chars/4 ratio (verified: `"x"*400` still estimates to exactly 100, so every
English/code caller sees zero behavior change), Thai-script characters
(U+0E00–U+0E7F) are weighted at 0.9 tokens/char per this task's calibration
target. `lint-imports` confirms no contract broken by the new module (29/29
contracts kept) — precedent already exists for `core/brain/*` importing a
top-level leaf module (`agent_takkub.bm25_search`).

**Honesty note on the ±15% target**: this pass could NOT independently
verify the weighted estimator lands within ±15% of a real
`cache_creation_input_tokens` transcript number end-to-end, because (a) the
one real calibration point this repo has (37,338 tokens, §0) predates the
F1 fix and included the ~13k-token shared native-memory chunk F1 just
removed from non-Lead roles, so it is no longer the right target to compare
against, and (b) boot_context.py still only measures a subset of what
actually boots (per-spawn task block, git status, env block, Claude's own
dynamic system-prompt sections — §2's original gap, still open). A fresh
calibration needs a new pane spawn under the post-F1/F2 code to get a real
number to check against — not done here to avoid burning quota on a
measurement pass basis, consistent with this doc's existing quota
discipline (§4/F3's MCP-schema note). What IS verified: role-appendix
`repo_controlled_est_tokens` moved from a naive ~4,850 tok (backend) to a
weighted ~7,030 tok — directionally correct (higher, since the content is
majority Thai) and it is now the number the CI ceiling ratchets against.

### #516b (same-day addendum from Lead): ceiling test false-positive fixed

The ceiling test (`tests/test_boot_context_ceiling.py`) went red on a dev
machine after F1/F2 landed for an unrelated reason: `learned_notes`
(role_memory.py's per-role accumulated notes file, real per-machine runtime
state a pane's own `takkub done` writes throughout the day) had simply grown
since the baseline was captured, and a static ceiling gated on it produces a
false regression on whichever machine has the most accumulated notes at gate
time, while a clean CI checkout (no accumulated state) stays green. Fixed
two ways:

1. `RoleBootReport.repo_controlled_est_tokens`/`repo_controlled_chars`
   (`boot_context.DYNAMIC_STATE_CATEGORIES` = `learned_notes`,
   `learned_notes_empty_pointer`, `project_memory_pointer`,
   `native_project_memory`, `native_project_memory_LEAD`) excludes
   per-machine dynamic state from the gated sum; the ceiling test and
   baseline JSON now key off this subset. Dynamic categories are still
   measured and shown in `format_report`'s output (tagged
   `[dynamic-state, not gated]`), just never failed on.
2. `role_memory.py` gained a second, tighter cap —
   `_MEM_MAX_LIVE_CHARS = 1_500` — on top of the existing 6,000-byte
   whole-document cap, checked against `_bullet_content_chars(sections)`
   (real bullet content only, not the fixed header/seed skeleton) inside
   `_curate_text`'s existing `_over_budget()` check. Reuses the SAME
   oldest-first trim-and-archive loop that cap already had (`_trim_oldest_
   bullet` + `_archive_entries`, both unchanged) — nothing new to write,
   just a second trigger condition, so a busy role's notes rotate into the
   L2 archive sooner instead of growing unbounded toward the 6,000-byte
   ceiling. `doctor.check_boot_context` now also emits a WARN Finding if a
   role's `learned_notes` category ever exceeds `_MEM_MAX_LIVE_CHARS` at
   measurement time — should be unreachable in practice (curation runs on
   every read, including this one), so a hit means curation itself silently
   failed (e.g. an `OSError` swallowed) and is worth investigating.

Tests: `tests/test_boot_context_ceiling.py` re-baselined against
`repo_controlled_est_tokens` + a new
`test_dynamic_state_categories_reported_not_gated`;
`tests/test_role_memory.py` gained `test_live_content_cap_is_1500_chars` +
`test_oldest_bullets_rotate_to_archive_once_live_cap_exceeded`;
`tests/test_boot_context.py` gained a doctor-WARN wiring test. `docs/audit/
boot-context-baseline.json` regenerated against the current repo state
(schema changed: `total_est_tokens`/`total_chars` per role →
`repo_controlled_est_tokens`/`repo_controlled_chars`).

### Point 3 measurement: real 30-day Skill-tool usage (dev + prod transcripts)

Read-only scan (script kept in this session's scratchpad, not committed —
`$TAKKUB_ARTIFACTS_DIR` policy) over every `*agent-takkub*` project
directory under both `~/.claude/projects` (dev) and
`~/.agent-takkub/claude-config/projects` (prod), files modified in the last
30 days, counting `"type":"tool_use","name":"Skill"` blocks and resolving
each `input.skill` value (bare name, or `<plugin>:<skill>` for a
plugin-provided one) to its marketplace via the actual installed plugin
cache directory listing (real skill names, not guessed):

- **587 transcript files in the 30-day window**, **68 total Skill-tool
  invocations**.
- **`superpowers-dev`: 15 invocations** (`test-driven-development` x9,
  `systematic-debugging` x4, `verification-before-completion` x2) — real,
  non-trivial use. Do NOT default this off.
- **`ui-ux-pro-max-skill`: 1 invocation** (`design`) — marginal but
  non-zero; already scoped to design roles only (`_ROLE_PLUGIN_POLICY`), no
  change indicated.
- **`pordee`: 0 Skill-tool invocations observed.** Per `_default_plugin_
  dirs`'s own comment it's meant to work as automatic Thai-context
  compression, not a user-invoked skill, so zero here does not necessarily
  mean unused — this scan can only see explicit Skill-tool calls, not
  automatic/hook-driven plugin behavior. Not a recommendation to remove it;
  flagged as a measurement blind spot instead.
- **`addy-agent-skills`: 0 invocations** — consistent with it already being
  excluded from every role's default (`_TEAMMATE_PLUGINS`/`_ROLE_PLUGIN_
  POLICY` in `lead_context.py` never lists it); no action needed, this
  confirms the existing exclusion rather than finding new information.
- **`claude-plugins-official`'s pane-facing skill (`frontend-design`): 0
  invocations observed.**

**Methodology caveat, stated plainly rather than hidden**: role attribution
(matching a transcript to `frontend`/`backend`/`qa`/etc. via a `[ROLE:
<name>` prefix scan of each file's first 8 lines) worked for only 1 of the
68 invocations (`qa`); the other 67 fell into an `lead_or_unknown` bucket.
Either most Skill-tool usage in this window genuinely happened in Lead's own
session (plausible — Lead does most of the direct work in this project's
observed workflow), or the role-prefix detection heuristic itself needs
refinement (the exact `[ROLE:` marker position/format in a real teammate
transcript was not independently verified against a live sample beyond the
one file that matched). Numbers above are trustworthy in aggregate
(skill/marketplace totals); the per-role breakdown is not — reported as an
open gap rather than a false per-role table. `_ROLE_PLUGIN_POLICY`'s
existing per-role defaults were not changed on the strength of this
measurement; it substantiates keeping `superpowers-dev` as a teammate
default, nothing more.

### Explicitly NOT done this pass (same "measure before guess" discipline)

- **`--disable-slash-commands` wiring** — investigated, deliberately not
  shipped; see corrected F3 above for why (would remove every teammate's
  entire built-in skill catalog project-wide since the Skill Matrix has zero
  assignments right now, and the usage scan above shows real skill use).
- **`--exclude-dynamic-system-prompt-sections`** — not attempted; needs a
  live before/after cache-hit-ratio experiment (two real spawns, comparing
  `cache_read_input_tokens` share of the first turn), which this pass judged
  out of scope for a token/quota-conscious measurement session.
- **Idle-pane reuse (point 4/5)** — no engine change. This is a scheduling
  feature touching `orchestrator`'s assign/spawn path (which pane is idle,
  which role it was last running, session-resume-vs-fresh-spawn decision,
  the 10-minute window, and interaction with `--resume`/context-window
  checks) — a correctness-sensitive change to shared spawn/assign machinery
  that this pass did not have the remaining scope to design, implement, and
  test to the bar the rest of this document holds itself to. Left for a
  dedicated follow-up task rather than shipped half-verified.
- **Multi-provider boot-context measurement (point 6)** — unchanged gap from
  §4; still only claude's `--append-system-prompt-file` path is measured.

## 7. Follow-up pass (2026-09-10, third session) — F2 measurement gap closed, idle-pane reuse investigated

### F2 measurement gap closed: `native_skill_catalog` category

§3/F2 flagged the native `skill_listing` catalog (16 skills, 8,025 chars in
the original transcript) as a real cost `boot_context.py` never actually
measured — only inferred from a one-off transcript read. New
`boot_context.measure_native_skill_catalog(project_ns)` scans
`CLAUDE_CONFIG_DIR/skills/` (`user_profile.config_dir_for`, the real
directory every claude pane of a project reads its global catalog from —
NOT a hardcoded `~/.claude/skills`, since this project's own dev panes run
under an isolated `CLAUDE_CONFIG_DIR`), cross-referenced against this
project's own `.claude/skills/` (`skill_scan.scan_skills`) so the report
separates "this repo's own skill" from "an unrelated global skill riding
along on every pane." Wired into `doctor.check_boot_context` (new
`native_skill_catalog` Finding, INFO) and `boot_context.format_report`.
Added to `DYNAMIC_STATE_CATEGORIES` — per-machine operator state (what the
user personally has installed), not repo content, same class as
`native_project_memory`. Confirmed live on the dev machine that produced
this doc: **34 skills** currently load into every agent-takkub pane this
way (a different, machine-specific number from the original 16-skill
transcript — expected, since it depends on what's installed on whichever
machine runs it, which is exactly why this is dynamic-state and not
ceiling-gated). Tests: `tests/test_boot_context.py::
TestNativeSkillCatalogMeasurement` (5 new tests).

### Point 2 (skills gate knob) revisited: still not wired, for the same reason as before

Re-investigated whether a default per-role gate could now be shipped (the
literal ask this pass started from: gate user-level skills unrelated to
agent-takkub out of every pane by default, keep this repo's own skills,
except where a role has real, demonstrated use of a specific catalog
skill). Conclusion unchanged from §3/F3: **no safe lever exists.**
`--disable-slash-commands` is still the only discovered CLI switch, is
still all-or-nothing per session, and disabling it would remove BOTH the
unrelated global catalog AND this project's own `.claude/skills/` skills in
the same stroke — directly violating the "keep this repo's own skills"
half of the ask, not just the risky half. It would also re-remove
`superpowers-dev`'s real, measured usage (`test-driven-development` x9,
`systematic-debugging` x4, `verification-before-completion` x2 — §6's
30-day scan) for ANY role, since that usage could not be attributed to a
specific role (67 of 68 invocations landed in `lead_or_unknown` — same
methodology caveat as §6) and those are universal engineering practices
every teammate role plausibly exercises, not a design-specific skill a
`_ROLE_PLUGIN_POLICY` split could safely gate by role today.

The one thing that changed this pass: the gap is now **measured**
(previous section), so a future decision — either building the real fix
(a per-project curated `CLAUDE_CONFIG_DIR` with only the wanted skills
symlinked in, mirroring `user_profile.py`'s existing multi-account-profile
machinery and its "first-boot profile clone" allowlist precedent for
`config`/`skills`/`agents`/`plugins`) or a fresh 30-day usage scan with
better role attribution — has a real "before" number to check its "after"
against via `takkub doctor --boot-context`, instead of a one-off transcript
read. Not shipped this pass: the curated-`CLAUDE_CONFIG_DIR` fix needs to
mirror real auth credentials into the new directory without breaking pane
login, which is a correctness-sensitive change to how every pane
authenticates and deserves its own dedicated design/test pass rather than
riding in on this one.

### Point 4 (idle-pane reuse) investigated: the "already-alive pane" case already works; the "already-exited" case is intentionally NOT resumed

Traced the full `assign()` → `spawn()` path rather than guessing:

- **Pane still alive (not yet auto-closed)** — `spawn_engine.spawn()`'s
  very first check (`pane.session is not None and pane.session.is_alive`)
  already short-circuits as a no-op, and `_assign_dispatch` already falls
  through to `_send_when_ready` (paste into the SAME running session)
  instead of any fresh boot. This needed no engine change — it already
  delivers a new task into a role's `done`-state-but-still-alive pane for
  free. Locked in with a new regression test:
  `tests/test_idle_pane_reuse_assign.py`.
- **Pane already fully exited** (`done()`/`close()` already tore it down)
  — `spawn_engine.py`'s existing `RESUME_WINDOW_SEC` (5 min) `--resume
  <uuid>` mechanism looks like it should cover this (the comment at
  `_auto_respawn` even says so), but its `can_resume` check reads
  `prior_uuid`/`prior_uuid_cwd` from the LIVE `self._pane_state` dict —
  which `done()`/`close()` deliberately pop before this ever runs. This
  looks like a dead code path on first read, but `tests/
  test_orchestrator_session_uuid.py` (`TestManualCloseClears`/
  `TestDoneClears`, module docstring: "option-B session UUID fix
  (resume-bleed prevention)") proves it is INTENTIONAL: resuming a role's
  transcript across a `done()`/`close()` boundary was a past bug (bleed
  between an old finished task's conversation and a new one), fixed by
  forcing every post-close/post-done spawn to start a brand-new session.
  Wiring `--resume` back in for this case — even gated on `RESUME_WINDOW_
  SEC` — would revert that fix and needs its own explicit design/test pass
  (how to safely resume a task-level session without reintroducing bleed,
  e.g. summarizing rather than replaying the old transcript), not a blind
  change riding in on this task. Not shipped this pass.
