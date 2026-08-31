# #318 — ANTHROPIC_DEFAULT_MODEL + Concise output style (token diet wave 4)

## Scope

1. `pane_env.py`: inject `ANTHROPIC_DEFAULT_MODEL` from the role/provider model pin,
   in place of the persistent-pin half of the existing `--model` flag.
2. Built-in **Concise** output style for teammate panes only (Lead keeps the default).
3. A/B measurement plan (single pilot role, `qa`) before widening to every teammate.
4. Multi-provider gap flag (#103) — both knobs are claude-only.
5. V2 alignment (`TAKKUB_V2_ROUTER`) — checked; no change required (see below).

## 1. `ANTHROPIC_DEFAULT_MODEL` vs `--model` — which one respects the user's own choice

Facts below are quoted from `code.claude.com/docs/en/model-config` (fetched 2026-08-20,
current dev-box install is 2.1.237) — not guessed. Cross-checked against the shipped
`claude.exe` binary strings (`ANTHROPIC_DEFAULT_MODEL` → attribution `"env"`, label
"Set by ANTHROPIC_DEFAULT_MODEL" in the `/model` picker) and against
`code.claude.com/docs/en/changelog`:

> v2.1.236: Added `ANTHROPIC_DEFAULT_MODEL` environment variable: sets the model new
> sessions start on, while a `/model` pick still overrides it and persists across
> restarts (unlike `ANTHROPIC_MODEL`)

Full precedence order for picking a session's model (highest to lowest), from
`model-config.md`:

1. `/model <name>` during the session (with `Enter`: also **saved** to the `model`
   field of the user's settings file, since v2.1.153)
2. `--model` flag at launch
3. `ANTHROPIC_MODEL` env var
4. a `model` value in any settings file — **including a choice saved with `/model`**
5. `ANTHROPIC_DEFAULT_MODEL` (only when nothing above selects a model)

Two sentences settle the "which respects the user more" question:

> A choice you save with `/model` takes precedence over the variable [`ANTHROPIC_
> DEFAULT_MODEL`] on later launches too. With `ANTHROPIC_MODEL` set instead, Claude
> Code returns to that variable's model on the next launch, whatever you saved with
> `/model`.

> A model you pick for the new launch with `--model` or `ANTHROPIC_MODEL` still takes
> precedence over the restored model [on `--resume`/`--continue`/`/resume`].

i.e. **`--model` always wins**, on a fresh spawn AND on a crash-respawn's `--resume`
(same argv is re-sent), over anything the user did with `/model` — including a choice
they explicitly saved. `ANTHROPIC_DEFAULT_MODEL` is the opposite: it is silently
ignored the moment a `model` value exists anywhere in settings, `/model`-saved or not,
and the docs draw the same distinction for resume explicitly:

> When a new session would start on the variable's model, a session you resume with
> `claude --resume`, `--continue`, or the `/resume` picker starts on it too. Claude
> Code doesn't restore the model saved in that session's transcript. Otherwise Claude
> Code doesn't use the variable when you resume a session.

This cockpit already spawns teammate/Lead panes with `--model <role tier pin>` on
every fresh spawn AND on every crash-recovery `--resume <uuid>` (same argv is built
before `--resume` is appended, `spawn_engine.py`, `RESUME_WINDOW_SEC` = 5 min). Under
the old mechanism that means a teammate could never actually keep a model its own
session picked — the very next crash-respawn (or the very next fresh spawn for that
role) reasserts the tier pin regardless. `ANTHROPIC_DEFAULT_MODEL` fixes exactly this,
matching #318's acceptance criterion verbatim: *"pane spawn ใหม่ได้ model ตาม pin โดย
/model ของ user ยัง override ได้."*

### Decision: one mechanism per purpose, not stacked

- **Persistent role/provider pin** (`role_models.model_for` → `provider_models.model_for`
  → role tier default / Lead's Pro-plan `[1m]`-avoidance safety net) → now injected as
  `ANTHROPIC_DEFAULT_MODEL` via `pane_env.apply_default_model()`. This is a *default*,
  not an enforcement — a user's own `/model` choice should stick.
- **Deliberate one-off override** (`takkub assign --model <x>`, or the operator's
  `TAKKUB_TEAMMATE_MODEL` env force) → stays on the `--model` argv flag, unchanged.
  These are narrower, explicit instructions for *this* spawn specifically, and per the
  precedence table `--model` is exactly the right tool for "beat everything, including
  a saved `/model` choice" — which is the correct behavior for an explicit override,
  just not for a standing default.

Both mechanisms are mutually exclusive per spawn (branch-selected, never both set),
so this is not "stacking two mechanisms for the same job" — `--model` now serves only
the narrow-override job it precedence-wise is meant for.

Implementation: `pane_env.apply_default_model(env, model)` (new); called from
`spawn_engine.py`'s claude branch (teammate and Lead) wherever the code used to append
`["--model", pin]` for the *pin* case. `_ps_initial.model_override` and
`TAKKUB_TEAMMATE_MODEL` keep going through `argv.extend(["--model", ...])`.
`_remap_pinned_model` (proxy `ANTHROPIC_DEFAULT_<TIER>_MODEL` alias rewrite) is applied
identically before either target — it already resolves to a final concrete id, so the
proxy-remap fix from `test_remap_pinned_model.py` is unaffected regardless of whether
the result lands in `--model` or `ANTHROPIC_DEFAULT_MODEL`.

### `ready_marker` / done-gate scanner — checked, no impact

`is_at_ready_prompt()` (`pty_session.py`) classifies readiness off the terminal's
bottom **footer/status region** — hardcoded Claude Code TUI chrome (`bypass
permissions`, `shift+tab to cycle`), scoped via `_ready_region()` specifically so
conversation-body text can never poison the verdict. Per `docs/en/output-styles`,
output styles "modify the system prompt" and change model-generated response text
only — they do not touch the TUI's own footer strings. The model-selection change
(§1) doesn't touch response text at all. No regression path exists for either change;
flagging this as verified-by-design rather than guessed, since neither change was
exercised against a live pane in this task's scope (see §3).

### V2 router alignment — checked, no change needed

`core.routing` (`facade.py`, `flag.py`, `policy.py`, `router.py`) resolves **provider**
selection only (`effective_provider_for_v2`, gated by `TAKKUB_V2_ROUTER`) plus, via
`_apply_v2_account_env_override`, which `ProviderAccount` (auth/config-dir profile)
backs a spawn. Neither owns **model** selection — grepped `core/`, no model-resolution
logic exists there; `role_models.py`/`provider_models.py` remain the sole authority,
V1 and V2 alike. There is no cross-layer read to fix today. Flagged forward: if V2
routing ever grows a model-selection concern, `pane_env.apply_default_model()` is the
single injection point to redirect.

## 2. Concise output style — teammate-only, per-session

Confirmed via `code.claude.com/docs/en/changelog`:

> v2.1.237: Added a built-in "Concise" output style: Claude leads with results and
> skips preamble and narration, while doing the work just as thoroughly. Select it
> under Output style in `/config`.

Confirmed via `code.claude.com/docs/en/output-styles`: the **only** mechanisms are the
`outputStyle` field in a settings file, or the `/config` menu (which writes that same
field to `.claude/settings.local.json`). There is **no CLI flag, no env var** for
output style — ruling that class of mechanism out for a per-session, per-role toggle.

`docs/en/settings` settings precedence (highest to lowest): **Managed** (org policy) >
**Command line arguments** ("temporary session overrides", explicitly including
`--settings <file>`) > Local > Project > User. `--settings` therefore wins over
anything a project/user settings file sets, loses only to org Managed settings (which
is the correct, desired outcome), and is scoped to *that one invocation* — it never
touches a shared `.claude/settings*.json` a human or another pane could see or inherit.

This cockpit already hands every claude pane `--settings <hook-settings.json>` for
Stop/Notification/SessionStart hook wiring (`hook_wiring.py`). Folding `outputStyle`
into that same mechanism gives exactly the per-session, per-role toggle needed, no new
CLI surface required:

- `hook_wiring._rendered_settings(concise: bool)` adds `"outputStyle": "Concise"`
  when `concise=True`.
- `hook_wiring.ensure_hook_settings_file(concise: bool)` writes to one of two on-disk
  files (`hook-settings.json` / `hook-settings-concise.json`) rather than mutating one
  file in place, so a Lead and a Concise-enabled teammate spawning back-to-back can
  never race each other's `--settings` content.
- `hook_wiring.role_wants_concise(role_name, *, is_lead)` — Lead is hard-excluded
  regardless of config (its done-note/summary is the one artifact a human actually
  reads, and Concise trims exactly the narration that makes those legible — matches
  the acceptance note "role ที่ต้อง report ละเอียด" exception). Teammates are opt-in
  per role via `TAKKUB_CONCISE_ROLES` (comma-separated, `"*"` = every teammate role,
  `""` = disabled). Default: `{"qa"}` — the pilot role for §3.
- `spawn_engine.py`'s existing `--settings` injection site now resolves `concise` and
  passes it through; no new argv flag, no new call site.
- #458 added a same-shaped sibling lever on that injection site: `TAKKUB_REMOTE_CONTROL_ROLES`
  (`hook_wiring.role_wants_remote_control`) stamps `remoteControlAtStartup`, default Lead-only.

## 3. A/B measurement — method (not run in this task's scope)

This task is a backend code change in an isolated git worktree with no live cockpit
process to spawn real panes against — there is no orchestrator runtime here to run an
actual A/B and collect before/after token numbers, so none are fabricated. What ships
instead is the mechanism plus a conservative default (`qa` only) so Lead can run the
comparison live:

1. Baseline: pick 2-3 upcoming `qa` tasks of comparable size/shape. Before running
   them, confirm `TAKKUB_CONCISE_ROLES` is unset (default `qa`-only pilot is already
   active) or temporarily set `TAKKUB_CONCISE_ROLES=""` to force Default style, and
   record each task's token usage from the existing usage/session accounting (see
   `docs/audit/2026-08-16-token-cost-measurement.md` for the established measurement
   path) alongside the done-note actually posted.
2. Treatment: re-enable Concise (`TAKKUB_CONCISE_ROLES` unset — `qa` pilot default),
   run comparably-sized tasks, record the same two numbers.
3. Compare: token usage delta, and read the done-notes side by side — Concise is only
   worth widening if the done-note is still *sufficient* for Lead to act on without
   asking follow-up questions, not merely shorter.
4. Widen gradually: `TAKKUB_CONCISE_ROLES=qa,backend,devops,...` one role at a time,
   repeating step 3 spot-checks; skip/exclude any role whose done-note quality
   regresses (`reviewer`/`critic`/`maintainer` — the gate roles whose entire job is a
   detailed report — are the most likely candidates to stay excluded permanently,
   matching the acceptance note's carve-out for roles that must report in detail).
5. Lead is the pane best positioned to actually run this (it owns the live cockpit,
   task assignment, and `takkub inbox`/usage history this needs) — this doc hands off
   the ready-to-use lever (`TAKKUB_CONCISE_ROLES`) and method rather than claiming a
   result that wasn't measured.

## 4. Multi-provider gap (#103)

Both `ANTHROPIC_DEFAULT_MODEL` and the `outputStyle` settings-file field are
claude-specific — neither exists for codex, gemini/agy, opencode, kimi, or cursor.
codex's nearest counterpart is its own `model_reasoning_effort` config (already tracked
separately, possibly landing alongside #323 per the issue body); none of the others
expose an equivalent "default model for new sessions that a live pick still overrides"
or "response verbosity style" knob as of 2026-08-20. Flagged to #103 rather than
silently claude-only: a pane spawned on any non-claude provider gets neither of these
two token-diet levers, and closing that gap (if a provider ever adds one) is future
work, not silently out of scope.

## Test coverage

- `tests/test_spawn_default_model_env.py` (new) — the `ANTHROPIC_DEFAULT_MODEL` vs
  `--model` split for both teammate and Lead branches: tier default, role pin,
  provider-level pin, role-over-provider precedence, explicit assign override,
  `TAKKUB_TEAMMATE_MODEL` env force, Lead with no pin, Lead role pin.
- `tests/test_provider_models.py::TestClaudeTeammateModelPrecedence` — updated the
  three precedence tests that used to assert `--model <pin>` in argv to instead assert
  `ANTHROPIC_DEFAULT_MODEL` in env (and `--model` absent); the explicit-override and
  env-force tests now also assert `ANTHROPIC_DEFAULT_MODEL` is *not* set alongside
  `--model` (single mechanism per spawn).
- `tests/test_hook_wiring.py::TestConciseOutputStyle` (new) — `role_wants_concise`
  default pilot / Lead exclusion / wildcard / disable / custom roster, plus the
  rendered-settings-file content (`outputStyle` key present/absent, two distinct
  on-disk paths) and two spawn-level checks (`qa` gets the concise file by default,
  `lead` never does even with `TAKKUB_CONCISE_ROLES=*`).
- `tests/test_remap_pinned_model.py`, `tests/test_lead_model_override.py`,
  `tests/test_role_models.py`, `tests/test_spawn_codex_argv.py`,
  `tests/test_spawn_v2_account_env.py`, `tests/test_pane_env_no_autoupdate.py`,
  `tests/test_cli.py` — re-run unchanged as regression coverage; all pass.
