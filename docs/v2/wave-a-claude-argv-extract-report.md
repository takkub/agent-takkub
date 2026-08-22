# 2.0.0 Wave A — Report: claude argv extraction

> epic #309 · branch `wt/backend-1-1787380952` (base `main` @ `01c7583`) · 2026-08-22
> Closes `docs/v2/2.0.0-migration-plan.md` §1.4 ("claude argv extract"), one of Wave A's four
> parallel items.

---

## 1. Scope decision — what this closes and what it deliberately doesn't

The plan (§1.4) asked to extract `spawn_engine.py`'s ~800-line claude branch's argv-building
logic into something `agent_takkub.core` can import, "mirroring `plan.py`'s
`assemble_generic_argv`" — and named the generic-branch extraction (Phase 3b) as the proven
pattern to repeat.

That pattern's actual, current shape (re-verified before writing this): `assemble_generic_argv`
is extracted, tested, importable from `core.providers.plan` — and **not called from
`spawn_engine.py`'s own live branch**, which still builds `provider_argv` inline
(`spawn_engine.py` ~2061-2184). Phase 3b's own report says so directly (§3): *"It is not yet the
branch's own code path ... flag-off behavior for argv is unaffected by construction, not just by
a flag check."*

This task repeats exactly that pattern for the claude branch, for the same reason Phase 3b gave
for not going further: rewiring `spawn_engine.py`'s live hot-path caller is a **separate,
provably-behavior-neutral change**, not something to bundle into an extraction whose entire point
is to not touch the live branch. What this task closes:

- **New, real, tested, importable**: `core.providers.claude_plan.assemble_claude_argv` — a pure
  function reproducing the claude branch's argv concatenation order (`spawn_engine.py`
  ~2639-2947, read line-by-line before writing the golden-order test).
- **Not done, tracked as the next step**: rewiring `spawn_engine.py`'s ~2639-2947 to build each
  flag-group into a local list and call `assemble_claude_argv` once at the end instead of mutating
  `argv` in place throughout. This is a mechanical, order-preserving transform (verified safe by
  hand while writing this extraction — every `argv.extend`/`argv.append` in that range only ever
  *appends*, nothing downstream reads `argv`'s partial contents before the final concatenation) —
  but it touches the single most exercised code path in the app, so it gets its own task with its
  own full-suite proof, not a slice of this one. Same call Phase 3b made for the same reason.

**`spawn_engine.py` itself is untouched by this change** (zero lines modified) — only
`core/providers/claude_plan.py` (new), `core/providers/__init__.py` (+export), and
`core/providers/claude_adapter.py`'s module docstring (updated to point at the new module instead
of describing the gap as unstarted).

## 2. Files created

```text
src/agent_takkub/core/providers/claude_plan.py            # assemble_claude_argv — pure, no PyQt6
tests/test_core_providers_claude_plan.py                  # golden-order + omits-empty-pieces + Lead-shape
tests/test_spawn_claude_argv_matches_claude_plan.py        # real-branch characterization test — §4a

docs/v2/wave-a-claude-argv-extract-report.md               # this file
```

## 0. Lead's 3 review questions, answered directly

Lead reviewed this before commit and asked 3 things to be answered in the report, not left as
"same as the generic branch" hand-waving. Answering each with evidence:

**Q1 — is `spawn_engine.py`'s claude argv code still fully intact, i.e. do we now have the same
logic living in 2 places that can drift silently?**

Yes, confirmed. `git diff --stat` against this branch's base shows **zero lines changed** in
`spawn_engine.py`. Its claude branch (~2639-2947) still builds `argv` inline exactly as before.
This means there ARE now two places that know the claude argv order:
`spawn_engine.py`'s live inline builder (the one that actually runs) and
`core/providers/claude_plan.assemble_claude_argv` (extracted, tested, not called by anything in
production yet). **This is a real, named risk, not a detail**: if a future change adds/reorders a
flag in `spawn_engine.py`'s branch without touching `claude_plan.py`, nothing in production breaks
(the new function isn't called), but `claude_plan.py` silently stops representing reality — the
next thing that imports it as ground truth (a V2 router move, a future rewire) would build on a
stale contract. this section's Q2 answer below is the mitigation this review added: a test that fails on exactly that
drift, today, without needing the wiring to exist first.

**Q2 — what does the golden-order test actually compare against: real output, or what I think it
should be?**

Before this review: the only proof was `tests/test_core_providers_claude_plan.py`'s
`test_assemble_claude_argv_matches_branch_order`, which hand-derives the expected list by reading
`spawn_engine.py`'s source — i.e. compared against what I read the branch to do, not against
anything the branch actually produced at test time. That gap is real and is exactly what Q2 is
pushing back on.

**Added**: `tests/test_spawn_claude_argv_matches_claude_plan.py` —
`test_assemble_claude_argv_reproduces_live_branch_argv` spawns a **real** `Orchestrator.spawn()`
for a claude teammate pane (only `PtySession` is mocked, to capture the argv instead of executing
a real ConPTY launch — same harness shape `test_spawn_codex_argv.py`/`test_spawn_v2_account_env.py`
already use), captures the argv the live branch actually handed to `session.spawn()`, and asserts
it equals `assemble_claude_argv()` called with the same caller-resolved pieces (model/effort/
fallback pinned via `TAKKUB_TEAMMATE_*` env, MCP argv and the hook `--settings` path patched to
fixed sentinels, the session-id uuid patched to a fixed value). **This test does not require
`spawn_engine.py` to call `assemble_claude_argv` to be meaningful** — it independently re-derives
both sides and asserts equality, so it fails the moment the two drift, today, before any wiring
exists. Building this test surfaced a real bug in my own test setup along the way (documented in
the test file's own comment): patching `agent_takkub.orchestrator.agent_role_dir` silently did
nothing (that name is imported directly into `spawn_engine.py`'s own module namespace, not
resolved through `orchestrator` at call time, unlike `find_claude_executable`/
`_build_transcript_path` which genuinely do go through `orchestrator` via `_from_orch`) — the
first run of this test caught it immediately by producing a real, unexpected
`--append-system-prompt-file` in the captured argv. That is direct evidence the test is exercising
the real code path, not a mock that would have stayed silent either way.

**Q3 — "same posture as the generic branch" — show the precedent, not just the claim.**

Commit `0ad4ea9` ("feat(core): Core V2 Phase 3b — adapter spawn plan + quota-aware per-account
(#309)", merged to `main` before this branch's base) introduced
`core/providers/plan.assemble_generic_argv`/`build_generic_spawn_plan`. Its own report,
`docs/v2/phase3b-report.md` §3, states directly: *"It is not yet the branch's own code path —
spawn_engine.py's inline `provider_argv = [...]` / `.extend()` sequence is untouched, so flag-off
behavior for argv is unaffected by construction, not just by a flag check."* Re-verified live
today: `git grep -n "assemble_generic_argv\|build_generic_spawn_plan" src/agent_takkub/spawn_engine.py`
returns zero matches — `spawn_engine.py`'s generic (non-claude) branch has never called either
function since Phase 3b, over a month of merged history. That extraction has stood, accepted,
un-wired, exactly this shape, since 2026-08-19. This task repeats the identical posture for the
claude branch, not a novel one.

**Net**: Q1 is confirmed true (duplicate exists, real drift risk). Q3's precedent is real, cited,
and pre-existing in this project (not invented for this task). Q2's gap is now closed by the
characterization test above (`tests/test_spawn_claude_argv_matches_claude_plan.py`). See §8 for
who should close the duplication (the wiring step) and when.

## 3. Files modified

| File | Change |
|---|---|
| `src/agent_takkub/core/providers/__init__.py` | exports `assemble_claude_argv` alongside the existing generic-branch exports |
| `src/agent_takkub/core/providers/claude_adapter.py` | module docstring updated — the extraction it named as a "tracked gap, not this method's job" now exists (`claude_plan.py`), same not-yet-wired posture as the generic branch |

## 4. Order verified against the live branch (2026-08-22)

`assemble_claude_argv`'s 11 pieces, in the order the golden-order test asserts, matching
`spawn_engine.py` ~2639-2947 read line-by-line:

1. `claude_bin`, `--dangerously-skip-permissions`, `--setting-sources`, `setting_sources` (~2639-2644)
2. `settings_argv` — Stop/Notification hook wiring, `--settings <path>` (~2653-2659)
3. `model_argv` — `--model` (~2679-2802, whichever of the teammate/Lead branches ran)
4. `effort_argv` — provider-gated effort flag (~2726-2736, teammate only; empty for Lead)
5. `fallback_argv` — `--fallback-model` (~2747-2750 teammate / ~2799-2802 Lead)
6. `disallowed_tools_argv` — teammate-only `--disallowedTools` pane-mode restriction (~2755-2757)
7. `plugin_dir_argv` — one `--plugin-dir <dir>` pair per resolved plugin (~2808-2812)
8. `system_prompt_argv` — role's system-prompt flag + rendered file (~2813-2815)
9. `mcp_argv` — `mcp_bridge.mcp_argv_for_provider("claude", ...)` (~2842-2847)
10. `denied_tools_argv` — `--disallowed-tools` hard-deny list, Task/AskUserQuestion (~2882-2896)
11. `resume_argv` — `--resume <uuid>` or `--session-id <uuid>` (~2916-2948)

Every piece is caller-resolved and already empty when it doesn't apply — `assemble_claude_argv`
only concatenates, never resolves (same contract `assemble_generic_argv` documents). The
Qt/PaneState-bound resolution that decides each piece's *value* (model/effort precedence against
`PaneState`, token minting, agents.md/CLAUDE.md rendering, resume-uuid bookkeeping against
`self._pane_state`/`self._recent_exits`) stays in `spawn_engine.py`, completely unchanged.

This order was originally verified by reading the source (the list above). §0's Q2 answer
describes the stronger proof added on review: `test_spawn_claude_argv_matches_claude_plan.py`
spawns the real branch and diffs its actual output against `assemble_claude_argv()`, so this
section's claim is no longer just a transcription — it's asserted against live behavior in CI.

## 5. Multi-provider / cross-platform

No change to either axis — this is a same-provider (claude-only), same-platform (pure Python,
`pathlib`/Qt-free) extraction. Nothing here affects codex/gemini/opencode/kimi/cursor panes or
Windows/macOS behavior, since `spawn_engine.py`'s live branches (claude and generic) are both
untouched.

## 6. Tests / lint

```text
PYTHONPATH=src python -m pytest tests/test_core_providers_claude_plan.py \
  tests/test_core_providers_plan.py tests/test_core_providers.py \
  tests/test_spawn_claude_argv_matches_claude_plan.py -q
# 44 passed, 0 failed (targeted)
```

Full suite: `PYTHONPATH=src takkub qa-gate` (not `--targeted`), per the plan's own Wave A
deliverable rule and root `CLAUDE.md`'s test-tier rule (`PYTHONPATH=src` needed because the shared
venv's editable install points at a different checkout — #202, see this branch's own backend
learned-notes). Result: `docs/qa/2026-08-22-140229-qa-gate.md` — **GATE: FAIL**, `8498 passed, 8
failed, 7 skipped in 893.76s`. **All 8 failures are an artifact of running the suite inside a worktree, not a defect in the code** (Lead correction, 2026-08-22 — see the note at the end of this section),
confirmed by re-running the same 8 tests with this task's changes stashed out
(`git stash push -u`, verified with `git stash list` by unique tag, restored via `git stash apply
<sha>` per this session's shared-stash safety protocol — never a bare `stash pop`): identical 8
failures, same assertions, on the untouched baseline. Every failure is in
`tests/test_installed_mode_gate.py`/`tests/test_installed_cli_bin_integration.py` — a fixture that
`pip install`s the package into a fresh throwaway venv and asserts a `takkub`/`agent-takkub`
console script lands next to that venv's Python; in this sandbox that install doesn't produce the
console script (environment/packaging gap, orthogonal to `core.providers`/`spawn_engine.py`,
`ruff`/`lint-imports` never ran because the gate is fail-fast on any pytest failure). Not fixed
here — out of scope for a pure-Python argv-assembly extraction, and the baseline-reproduction
above is the proof it isn't this task's regression.

> **Lead correction (2026-08-22):** the paragraph above originally called these 8 failures
> "pre-existing", which measures right but names the cause wrong. They are **not** a latent bug
> waiting to be fixed by "whoever owns that" — they are an artifact of running the suite from a
> worktree. A worktree has to set `PYTHONPATH=src` to dodge the editable-install checkout
> mismatch (#202), and that leaks into the throwaway subprocess venv these installed-mode tests
> spawn, so the child resolves `src/` from the worktree instead of the package it just installed.
> backend#2 hit the identical failures earlier the same day, and qa confirmed the cause by running
> the same suite on the **main tree**, where there is no such override: `8501 passed, 7 skipped,
> 0 failed`. So the correct read of this gate result is "clean, modulo the known worktree
> override", and the `ruff`/`lint-imports` steps below were skipped for the same non-reason.
> Calling it "pre-existing" would send the next reader hunting a packaging bug that does not
> exist — the same wasted-hunt failure mode #346 caused today.

- `ruff`/`lint-imports` **did not run this cycle** — the gate is fail-fast on any pytest failure,
  and the 8 worktree-artifact failures above stopped it before reaching either step. Not run raw
  outside `takkub qa-gate` (project policy). By inspection: `claude_plan.py` imports only
  `collections.abc.Sequence`, no `core-is-bottom-layer` risk, same shape as `plan.py`, which
  already passes both checks — but this is an inspection claim, not a gate result, and should be
  re-confirmed by re-running `takkub qa-gate` once the installed-mode gap is fixed (whoever owns
  that) or by a maintainer running `ruff`/`lint-imports` directly outside this session's policy
  constraints.

## 7. Decisions made without asking

- Chose **not** to wire `spawn_engine.py`'s live branch to call `assemble_claude_argv` this task,
  matching Phase 3b's identical decision for the generic branch and for the identical reason
  (rewiring the hot-path caller needs its own dedicated, full-suite-proven task). The migration
  plan's own §1.4 wording ("extract ... into something `core` **can call**") reads as consistent
  with this scope, not as a mandate to rewire the caller in the same task.
- `assemble_claude_argv`'s parameter order/grouping follows the *literal* sequence the branch
  builds `argv` in, not a "logical" grouping (e.g. all model-related flags together) — same
  reasoning `assemble_generic_argv` used: the golden-order test's whole value is asserting byte-
  identical order against the real branch, so the function's shape should make that comparison
  trivial to eyeball, not require re-deriving the order from a different grouping.
- Left `_append_provider_effort` (the existing pure helper `spawn_engine.py` already uses to build
  the effort flag pair) where it is, not moved into `core/providers/`. It's already provider-
  generic and Qt-free, but it isn't `spawn_engine.py`'s own resolution logic either — moving it
  wasn't necessary to prove the golden-order contract (the test hand-builds `effort_argv` the same
  way `test_core_providers_plan.py`'s generic-branch test already does), and moving code that
  wasn't asked for and isn't blocking anything would be scope creep on a task explicitly scoped to
  the argv-assembly extraction only.

## 8. Next step (tracked, not this task) — duplication location, owner, wave

**Where the duplicate logic lives** (Q1): the claude argv concatenation order exists in exactly 2
places —
1. `src/agent_takkub/spawn_engine.py` ~2639-2947 (live, runs on every claude-provider pane spawn)
2. `src/agent_takkub/core/providers/claude_plan.py` `assemble_claude_argv` (new this task, not
   called by production code)

Same duplication already exists, unresolved, for the generic branch (`spawn_engine.py` ~2061-2184
vs `core/providers/plan.assemble_generic_argv`) since Phase 3b (commit `0ad4ea9`, 2026-08-19) —
this task adds a second instance of an already-accepted-but-open pattern, not a new kind of risk.

**What closes it**: rewire `spawn_engine.py`'s claude branch (~2639-2947) to build each flag-group
into a local list and call `assemble_claude_argv` once, replacing the current in-place
`argv.extend`/`argv.append` sequence. Mechanical and order-preserving (verified while writing this
extraction — nothing reads `argv`'s partial contents mid-build; further reinforced by §0 Q2's
characterization test, which pins the exact resolved values a rewire would need to reproduce).

**Owner / wave**: backend, but explicitly its own task — not bundled into Wave A, B, or C as
currently scoped in `docs/v2/2.0.0-migration-plan.md` (none of those waves name this rewire). It
touches the single most exercised code path in the app, so per the plan's own rule it needs the
full suite as its own proof of behavior-neutrality, run in isolation so a regression is
attributable to exactly this change. Recommend Lead add it to epic #309 as an explicit follow-up
item (e.g. "Wave A-follow: wire claude_plan.assemble_claude_argv into spawn_engine.py's live
branch") rather than leaving it implicit — the same recommendation applies to the generic branch's
matching gap, which has been open since Phase 3b with no tracking item found in this repo.
`tests/test_spawn_claude_argv_matches_claude_plan.py` stays green either way: it doesn't require
the wiring to exist, and continues to guard against drift until the wiring lands.

## 9. Second Lead review round — 2 more questions, answered

Lead accepted §0's answers and flagged the bigger-picture problem directly: merging this task
leaves **3** copies of argv-building logic that must be kept in sync by hand with nothing
enforcing it (`spawn_engine.py`'s 2 live branches + 2 now-extracted-but-unwired pure functions,
i.e. generic × 2 and claude × 2). Lead is tracking that as its own epic #309 item, not asking this
task to fix it — but asked for 2 things in this report so whoever does the wiring later doesn't
have to re-derive them:

**Q1 — if wiring both branches at once, what exactly has to be touched, and was there ever a
stated reason wiring didn't happen back in Phase 3b?**

*Generic branch* — `spawn_engine.py` ~2061-2184: replace the inline
`provider_argv = [provider_bin, *autonomy_flags...]` plus the following `.extend()`/`.append()`
calls (model, effort via `_append_provider_effort`, MCP, project-scope, resume) with local
per-group list captures, then call `core.providers.plan.assemble_generic_argv(...)` once at the
end. Needs `from .core.providers.plan import assemble_generic_argv` added to `spawn_engine.py`.

*Claude branch* — `spawn_engine.py` ~2639-2947: the same transform this report's §8 already
describes, using `core.providers.claude_plan.assemble_claude_argv`.

*Stated reason wiring didn't happen since `0ad4ea9`*: **checked `docs/v2/phase3b-report.md` in
full — there is no stated technical blocker.** §3 describes the generic branch's post-Phase-3b
state factually ("It is not yet the branch's own code path") but gives no reason *why not*, and no
other section names an obstacle to wiring it. The closest thing to a reason in that report is
scoped to the **claude** branch only (§3's "Tracked gap, narrowed" paragraph: the ~800-line
extraction itself, not a follow-up wiring step, "needs its own dedicated task, not a slice of
this one" — about not re-attempting the extraction, not about a wiring blocker). Best honest
reading: the generic-branch wiring simply was never picked up as a follow-up task in the month
since, not that something makes it unsafe. Both rewires are mechanical and order-preserving by
inspection — confirmed by full line-by-line read for the **claude** branch while writing this
extraction (§8: nothing reads `argv`'s partial contents mid-build); the **generic** branch was
only skimmed (~2050-2185), not read with the same rigor, so treat "also mechanical" there as
presumed-by-symmetry, not independently verified to the same standard this report holds itself to
elsewhere. Whoever picks up the generic-branch rewire should do that same line-by-line read first.

**Q2 — does the generic branch have a live-branch characterization test like this task's
`test_spawn_claude_argv_matches_claude_plan.py`? If not, say so as an open gap.**

**No — confirmed by search, and this is an open gap.** `git grep -rn
"assemble_generic_argv\|build_generic_spawn_plan" tests/` matches only
`tests/test_core_providers_plan.py` — the same class of hand-derived, read-the-source golden-order
test this report's §0 Q2 answer already named as insufficient on its own. `test_spawn_codex_argv.py`
and the other generic-branch argv tests (`test_provider_project_scope.py`,
`test_lead_provider_unlock.py`, `test_opencode_provider.py`, `test_cursor_provider.py`,
`test_h1_nonclaude_env.py`) all capture and assert on the **real** branch's argv, but none of them
cross-check that real argv against `assemble_generic_argv()`'s output — so today nothing would
catch the generic branch and `plan.py` drifting apart, the same exposure this task's claude-side
gap had before §0's fix. **Not built in this task** (Lead's own instruction: don't let scope grow
here) — flagging it so whoever wires the generic branch builds that safety net first, the same
order this task did for claude (test proving equivalence → then, separately, the wiring itself).
