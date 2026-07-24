# Issue #125 — agy effort fallback trace and fix

## Scope

Regression introduced by `54baec5` and released in 1.0.29/1.0.30. The invariant for
this fix is that an optional effort flag must never cause agy to discard an explicit
model selection. Claude and Codex effort behavior must remain unchanged.

## Trace: argv construction

The generic provider spawn path in `spawn_engine.py` constructs agy's argv in this
order:

1. Start with the resolved agy binary and its autonomy flag:
   `agy --dangerously-skip-permissions`.
2. Resolve the model using assign override, then role model, then provider model.
   If present, append `--model <model>`.
3. Resolve effort from
   `os.environ.get("TAKKUB_TEAMMATE_EFFORT", tier_effort).strip()` and pass it to
   `_append_provider_effort`.

For a backend pane, `_teammate_tier("backend")` is
`("claude-sonnet-5", "high", "claude-haiku-4-5")`. In the reproduced environment
`TAKKUB_TEAMMATE_EFFORT` was unset, so the resolved effort was `high`. If the
variable is present it overrides the tier; an explicitly empty value suppresses the
flag.

The failing combination therefore resolves to:

```text
agy --dangerously-skip-permissions \
  --model "Gemini 3.1 Pro (Low)" \
  --effort high
```

`(Low)` does **not** come from the backend effort tier. It is part of the explicit
model value selected through the assign/role/provider model precedence. The backend
tier independently contributes `--effort high`, creating the conflict. A structural
Graphify extraction also confirmed the call edge from `SpawnEngineMixin.spawn` to
`_append_provider_effort` and the tier lookup at the generic provider branch.

## agy 1.1.6 verification

Binary tested:

```text
C:\Users\monch\AppData\Local\agy\bin\agy.EXE
agy --version => 1.1.6
```

`agy models` advertised these effort-bearing slugs:

- `gemini-3.6-flash-high`, `gemini-3.6-flash-medium`,
  `gemini-3.6-flash-low`
- `gemini-3.5-flash-high`, `gemini-3.5-flash-medium`,
  `gemini-3.5-flash-low`
- `gemini-3.1-pro-high`, `gemini-3.1-pro-low`
- `gpt-oss-120b-medium`

The Claude slugs in the same list do not advertise effort variants. Although
`agy --help` exposes `--effort low|medium|high`, compatibility is model-specific.

Observed behavior:

| Invocation | Result |
| --- | --- |
| `--model "Gemini 3.1 Pro (Low)" --effort high` in print mode | Exit 1: effort is not supported for the selected model |
| Same pair in the real TUI/PTY spawn | Warning: `--effort is not supported for model "Gemini 3.1 Pro (Low)". Using the default model instead.`; TUI then showed `Gemini 3.1 Pro (High)` |
| `--model gemini-3.1-pro-high --effort high` | Accepted; prompt returned `OK` |
| `--model gemini-3.1-pro-low --effort low` | Accepted; prompt returned `OK` |
| No model plus `--effort high` | Accepted with the current account default, but the default model is not declared in argv and can change |

The exact interactive reproduction transcript is stored at:

```text
C:\Users\monch\WebstormProjects\agent-takkub\runtime\exports\2026-07-24\agent-takkub\issue-125-agy-invalid-effort-reproduced.ansi
```

## Fix choice

Chosen option: **(c), do not send `--effort` to agy**.

- Mapping effort into a different model slug would mutate an explicit Low model
  override into High, violating model precedence rather than preserving it.
- Static validation would drift as agy's model list changes.
- With no model override, Takkub cannot deterministically validate the account's
  current default before launch.
- When the explicit model already has an effort suffix, a matching effort flag is
  redundant; when it does not match, it is destructive.

`gemini_spec.effort_flag` is therefore `None`, using the existing unsupported-provider
contract in `_append_provider_effort`. Codex retains
`-c model_reasoning_effort=<level>`, and Claude retains `--effort <level>`.

## Targeted verification

Tests cover:

- agy + explicit `Gemini 3.1 Pro (Low)` model override + backend high effort:
  model flag preserved, no effort flag;
- agy + no model override + backend high effort: no guessed effort flag;
- Codex: existing config-backed effort override retained;
- Claude: existing backend role-tier `--effort high` retained.

Command:

```powershell
$env:PYTHONPATH = '<this-worktree>\src'
python -m pytest tests/test_provider_models.py tests/test_spawn_codex_argv.py -q
```

Result: `32 passed`.

Documentation verification also passed: `31 passed` in `tests/test_docs_verify.py`.

A full `python -m pytest -q` run was also attempted, but exceeded the 303-second
command limit before pytest produced a final result. No full-suite pass/fail claim is
made from that run.
