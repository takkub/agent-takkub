# Backend review: provider mapping, state, spawn, and CLI

Date: 2026-07-18  
Scope: modified provider/logic paths in `provider_config.py`, `provider_state.py`, `spawn_engine.py`, and `cli.py`, with supporting review of `provider_spec.py`, `provider_install.py`, and relevant tests.

## Verdict

The current Codex, Gemini, and new OpenCode registrations are internally consistent and the targeted regression tests pass. OpenCode's `npm install -g opencode-ai` package and `--auto` permission flag also match the current official OpenCode documentation.

I found no release-blocking regression for the three currently registered non-Claude providers. I did find one medium-severity hole in the advertised registry-driven abstraction, two low-severity behavior/policy gaps, and stale provider documentation that should be cleaned up before treating the provider layer as fully generic.

## Findings

### 1. Medium: binary discovery is not actually generic across availability and spawn

Locations:

- `src/agent_takkub/provider_config.py:235-241`
- `src/agent_takkub/spawn_engine.py:1149-1152`
- Compare with the fallback already implemented in `src/agent_takkub/provider_install.py:34-47` and `src/agent_takkub/doctor.py`.

`ProviderSpec` supports both `custom_discovery_fn` and `binary_names`, and the new spawn comment says adding a provider should only require a registry entry. However:

- `_provider_available()` returns `True` when a non-Claude spec exists but has no `custom_discovery_fn`.
- The generic non-Claude spawn branch then sets `provider_bin` to `None` when there is no custom function and returns `install_instructions` instead of launching or degrading to Claude.
- Doctor and provider installer already fall back to `binary_names`, so different surfaces can disagree about the same provider.

Minimal reproduction with a temporary `ProviderSpec(name="path_only", binary_names=["python"], custom_discovery_fn=None)` prints `available_without_custom_discovery=True`, while the spawn branch cannot resolve its executable.

Impact: no current provider is broken because Codex, Gemini, and OpenCode all define a custom discovery wrapper. The next provider that relies on the documented `binary_names` fallback will appear available, but spawning will fail. This undermines the main goal of the generic refactor.

Recommendation: create one public discovery helper (the current `provider_install._discover()` behavior is suitable, but it should live at a neutral provider layer) and use it from provider availability, status chip, doctor, installer, and spawn. Add a test registering a path-only spec and assert both availability and captured spawn argv.

### 2. Low: Claude-substitute message is missing for sharded forced roles

Location: `src/agent_takkub/spawn_engine.py:1902-1909`

The Claude fallback suffix checks `role_name in FORCED_ROLES`. Sharded panes use names such as `codex#2`, `gemini#3`, or `opencode#2`, so the full role name never matches the base-role set. Provider resolution correctly uses `base_role`; only the user-facing substitution message is lost.

This was already latent for Codex/Gemini, but the generic refactor extends the same gap to every forced provider and its comment now claims generic coverage.

Recommendation: compare `base_role in FORCED_ROLES` and add a sharded forced-role case to `test_provider_substitution_note.py` (including OpenCode).

### 3. Low / policy decision: the CLI role gate blocks read-only `provider list`

Locations:

- `src/agent_takkub/cli.py:31-49`
- `src/agent_takkub/cli.py:805-839`
- `src/agent_takkub/cli.py:1953-1958`

The whole top-level `provider` command is Lead-only because installs mutate the machine-level toolchain. That also blocks `takkub provider list` for teammate panes even though `list` only performs local discovery. A direct reproduction as `TAKKUB_ROLE=backend` returns the Lead-only error before argparse dispatches `cmd_provider`.

Impact: teammates cannot perform harmless provider diagnostics or report an exact install state to Lead. This may be intended policy, but the gate comment only justifies blocking installs.

Recommendation: either document that provider enumeration is deliberately Lead-only, or make the gate subcommand-aware so only `provider install` is restricted. There are currently no CLI tests for `cmd_provider`, its output, or role-gate behavior; add terminal/Lead/teammate cases for both subcommands.

### 4. Low: state loader coerces invalid JSON value types into surprising disable flags

Location: `src/agent_takkub/provider_state.py:69-71`

The registry-derived `TOGGLABLE` set and atomic save flow are sound. The loader still uses `bool(v)`, so a manually edited value such as `{"opencode": "false"}` becomes `True` and disables the provider. This is pre-existing behavior, not introduced by the registry change, but adding more providers makes the state file more likely to be inspected or edited.

Recommendation: retain only entries whose values are actual JSON booleans (`isinstance(v, bool)`), or explicitly parse accepted strings. Add a corrupt/invalid-type state test.

## Documentation and cleanup notes

- `provider_config.py` module and function docstrings still describe only Claude/Codex/Gemini and omit the forced OpenCode role. `save_role_overrides()` also says Lead is forced even though issue #101 removed that rule. Update these comments so future mapping changes are not made from stale invariants.
- `cmd_provider()` imports `installable_providers()` solely for an empty `pass` branch. Remove the dead conditional unless it is meant to produce different guidance.
- The existing `graphify-out` query did not contain useful nodes for these runtime modules; it returned settings-management provider nodes instead. Findings above therefore come from the live diff and source/tests, not inferred graph edges.

## Verification

- `python -m pytest -q tests/test_provider_config.py tests/test_provider_state.py tests/test_opencode_provider.py tests/test_provider_install.py tests/test_spawn_codex_argv.py tests/test_lead_provider_unlock.py tests/test_cli.py` — 146 passed.
- `python -m pytest -q tests/test_spawn_gate.py tests/test_launch_session.py tests/test_provider_substitution_note.py tests/test_cli_server_role_gate.py tests/test_settings_management_providers.py tests/test_settings_management_roles.py tests/test_provider_toggle_orchestrator.py` — 138 passed.
- Ruff on the reviewed implementation and new provider tests — passed.
- `git diff --check` — passed.
- Full `python -m pytest -q` did not complete within 124 seconds; the harness timed it out and emitted an `OSError` while flushing stdout after termination. This is not counted as a pass or a test failure.
- Official OpenCode references checked: <https://opencode.ai/docs/permissions/> (`--auto`) and <https://opencode.ai/docs/> (`npm install -g opencode-ai`).

## Suggested merge order

1. Centralize executable discovery and add the path-only provider regression test.
2. Fix the sharded substitution note and add coverage.
3. Decide whether `provider list` is intentionally Lead-only; encode the decision in tests.
4. Clean stale provider docstrings and optionally tighten state value validation.
