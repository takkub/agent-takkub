# #157 — role prompts didn't warn about image token cost

## Bug

`BIG_FILE_GUARD` (`lead_context.py`) already told Lead + every teammate to
avoid `Read`-ing large **text** files wholesale (offset/limit, grep first).
It said nothing about images. Incident: a 1.7MB mockup PNG got `Read` across
4 frontend rounds + 3 critic rounds in the same pipeline. Two frontend panes
hit their usage limit in the same turn (16% of usage in one turn) from the
repeated vision-token cost — images are billed by resolution (vision
tiling), not linearly by byte size, so they're much more expensive per byte
than the text case the existing guard covered, and the offset/limit
workaround doesn't even apply to them.

## Fix

Extended the existing `BIG_FILE_GUARD` string in `lead_context.py` (the same
constant already injected into both the Lead spawn prompt at
`_render_lead_context` and every teammate's role-md appendix in
`spawn_engine.py`) with a `🖼️ รูปภาพ` subsection:

- threshold: image > ~300KB or longest side > ~1500px
- since offset/limit doesn't apply to images, the guidance is behavioral:
  check whether you (or an earlier hop in the same pipeline) already read
  this image and reuse its written note/finding instead of re-`Read`ing;
  ask for a cropped/downscaled version when only part of the image matters;
  don't re-open the same image "just to be sure" across iteration rounds.
- deliberately did **not** build an actual crop/downscale helper — task
  scope was warn + advise, not tooling.

No new injection plumbing needed — reusing the existing `BIG_FILE_GUARD`
constant means both Lead and every teammate role picked up the new section
automatically through the code paths that already existed
(`lead_context.py:428`, `spawn_engine.py:2050`).

Also added a matching warning to two Lead-facing docs so the *orchestration*
side (not just the read-time reflex) considers this before fanning out:

- `CLAUDE.md` → `บทเรียน (anti-patterns)` → `กฎที่เคยพลาด`: before assigning
  multiple roles to review the same image, have one role summarize it to a
  text note for the others to reference instead of every role opening the
  raw file.
- `docs/lead/patterns.md` → Critic pipeline recipe: flagged Hop 2
  (critic + gemini both open the same screenshot set, then frontend often
  re-opens the same mockup across multiple implement-fix-reimplement
  rounds) as the concrete highest-risk spot for this pattern.

## Files changed

- `src/agent_takkub/lead_context.py` — extended `BIG_FILE_GUARD`
- `CLAUDE.md` — added anti-pattern bullet
- `docs/lead/patterns.md` — added warning to Critic pipeline section

## Verification

Targeted tests (`test_lead_context_compact.py`, `test_session_brief.py`,
`test_lead_write_guard.py`, `test_provider_substitution_note.py`,
`test_orchestrator_reexports.py`, `test_skill_policy.py`, plus
`test_lead_project_rules.py` / `test_project_scoping.py` for the same
`_render_lead_context` code path) — all pass except a handful of pre-existing
failures confirmed unrelated to this change via `git stash` A/B (missing
`runtime/lead-context.md` fixture setup and a `test_solo_mode_has_no_parallel_block`
monkeypatch/encoding issue — both reproduce identically on clean HEAD before
this change).

No full-suite run per project's targeted-tests-mid-flight convention; this is
a prompt-text-only change with no behavior branching, so it doesn't meet the
"behavior-neutral refactor" exception that would require one either.
