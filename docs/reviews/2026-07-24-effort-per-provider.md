# Reasoning effort per provider

Date: 2026-07-24

## Summary

Role-tier reasoning effort is now applied to every non-Lead pane whose
`ProviderSpec` declares a supported session-scoped effort surface.
`spawn_engine.py` uses one generic `_append_provider_effort` path for both the
Claude branch and the spec-driven non-Claude branch. An explicitly empty
`TAKKUB_TEAMMATE_EFFORT` still disables the argument.

`ProviderSpec.effort_config_key` was added for providers whose effort control is
carried by a generic config flag instead of a direct effort flag. Providers
with `effort_flag=None` receive no effort argument.

## Provider status

| Provider | Status | Spawn argv |
| --- | --- | --- |
| Claude | Supported | `--effort <level>` |
| Gemini / agy | Supported | `--effort <level>` |
| Codex | Supported through session config | `-c model_reasoning_effort=<level>` |
| OpenCode | Gap: unsupported | No effort argument |
| Kimi | Gap: no equivalent three-level surface | No effort argument |
| Cursor | Gap: unsupported/documented surface absent | No effort argument |

## Evidence and gaps

- `agy --version` reported 1.1.5. Its local `agy --help` documents
  `--effort` with `low|medium|high`.
- `codex --version` reported 0.145.0. Its local help documents
  `-c, --config <key=value>`. The existing session-scoped config mechanism now
  carries `model_reasoning_effort=<level>` without changing the user's
  `config.toml`.
- `opencode --version` reported 1.18.4. Its local top-level help exposes model,
  agent, and session controls but no reasoning-effort option. The spec keeps
  `effort_flag=None`.
- `kimi --version` reported 1.49.0. Its local help exposes only the boolean
  `--thinking/--no-thinking` toggle. Mapping low, medium, and high to a boolean
  would discard role-tier meaning, so the spec keeps `effort_flag=None`.
- Cursor CLI was not installed. The official
  [CLI parameter reference](https://docs.cursor.com/en/cli/reference/parameters)
  lists model and force controls but no reasoning-effort option. The spec keeps
  `effort_flag=None`.

## Verification

- `PYTHONPATH=src python -m pytest -q tests/test_provider_models.py tests/test_spawn_codex_argv.py`
  — 23 passed.
- `PYTHONPATH=src python -m pytest -q tests/test_opencode_provider.py tests/test_kimi_provider.py tests/test_cursor_provider.py tests/test_lead_provider_unlock.py tests/test_h1_nonclaude_env.py tests/test_teammate_tier.py`
  — 45 passed.
- Ruff check and format check passed for the five changed Python/test files.

