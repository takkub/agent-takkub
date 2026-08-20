# Issue #323 follow-up — agy `--effort` restored

## Scope

Review #323 flagged that `ProviderSpec` marked `gemini`/`agy` as having no
`--effort` knob at all, backed by a live proof that `agy --effort high
changelog` runs normally (the flag is defined) while a bogus flag hard-errors.
That marking was not an oversight: `gemini_spec.effort_flag` was deliberately
set to `None` by issue #125 (2026-07-25, `docs/reviews/2026-07-25-125-agy-
effort-fallback.md`), after live evidence showed a mismatched `--model`/
`--effort` pair made agy **silently discard the explicit `--model` and swap
to a different one**. This follow-up re-checks that evidence against the
currently installed agy build before deciding whether to re-wire the flag.

## What changed since #125

#125 was traced against agy 1.1.6. The machine now has agy **1.1.15**. Its
own changelog documents the exact fix, at **1.1.10**:

> Fixed `--model` and `--effort` being ignored in interactive sessions and in
> headless `-p` runs, where the flags were applied after model configuration
> had already been initialized so the run silently fell back to the
> persisted or default model.
>
> Fixed a bare `--effort` resolving against the default model instead of the
> model you actually have selected, which could silently move you to a
> different model.

## Live re-verification (2026-08-20, installed agy 1.1.15)

```text
$ agy --help | grep -A1 effort
  --effort                        Reasoning effort for the current CLI session (low|medium|high)

$ agy models
gemini-3.7-flash-high / medium / low
gemini-3.6-flash-high / medium / low
gemini-3.5-flash-high / medium / low
gemini-3.1-pro-high / low
claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium

$ agy --model gemini-3.1-pro-low --effort high -p "reply with the single word OK"
Error: invalid model selection (--model "gemini-3.1-pro-low" --effort "high"):
--model gemini-3.1-pro-low conflicts with --effort=high
(exit 1)

$ agy --model gemini-3.1-pro-low --effort low -p "reply with the single word OK"
OK

$ agy --effort high -p "reply with the single word OK"
OK
```

The exact regression #125 reported — a conflicting pair silently discarding
`--model` — no longer reproduces. agy now refuses to launch at all on a
conflicting pair, with an explicit error naming both flags. A matching pair,
and a bare `--effort` with no `--model` override, both still work as before.

Level tokens (`low`/`medium`/`high`) are documented by `agy --help` itself,
not guessed, and match the suffixes `agy models` advertises on every
effort-bearing gemini slug.

## Decision

`gemini_spec.effort_flag = "--effort"`, `effort_levels = ("low", "medium",
"high")` — the same generic `_append_provider_effort` /
`_resolve_teammate_effort` path claude/codex already use; no spawn_engine.py
changes were needed, only the `ProviderSpec` table entry the rest of the
system was already built to read.

## Known residual gap (not fixed by this change)

Takkub does not cross-validate an explicit `--model` override against the
resolved tier/assign effort before spawn, for any provider. For gemini this
now matters: a role pinned to an effort-suffixed model (e.g.
`gemini-3.1-pro-low`) whose effort resolves to a different level (role
setting, `TAKKUB_TEAMMATE_EFFORT`, or `takkub assign --effort`) will fail to
boot with agy's own conflict error instead of the old silent misroute. This
is a narrow, self-explaining failure mode (the pane's own output names both
conflicting flags) rather than the silent one #125 fixed — left as a
documented gap rather than adding pre-spawn cross-validation, which was out
of this follow-up's scope. Flagged to Lead in the `done` note for this task.

## Targeted verification

```powershell
$env:PYTHONPATH = '<this-worktree>\src'
python -m pytest -q tests/test_provider_spec_effort.py tests/test_teammate_effort_resolver.py tests/test_provider_config.py tests/test_provider_models.py
```
