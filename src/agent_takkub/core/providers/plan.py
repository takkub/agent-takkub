"""Pure `SpawnPlan` builders (epic #309 Phase 3b — closes the gap Phase 3's
report §3 tracked: adapters answered "is this provider usable" but never
built anything spawnable).

Two pieces of the ~800-line claude branch / the generic non-claude branch
(`spawn_engine.py` ~1690/~1723) are genuinely pure — no Qt, no `PaneState`,
no I/O — and are extracted here VERBATIM (same order, same values):

- `assemble_generic_argv` — the generic branch's final argv concatenation
  (`provider_argv = [bin, *autonomy]`, then `.extend()`/`.append()` for
  model/effort/mcp/project-scope/resume). Order verified against
  `spawn_engine.py` lines ~1873-1977 (2026-08-19); see
  `tests/test_core_providers_plan.py`'s golden-order test.
- `account_env_overrides` — NEW logic (accounts were never wired into env
  before this phase): the one place `ProviderAccount.config_dir` becomes
  `CLAUDE_CONFIG_DIR`/`CODEX_HOME`.

Everything with a side effect (MCP subprocess resolution, agents.md
rendering, model/effort resolution against `PaneState.model_override`,
token minting) stays in `spawn_engine.py` and is passed in here
pre-resolved — this module only ASSEMBLES, never resolves. That is also
why the ~800-line claude branch itself is not further split beyond the env
hook: everything else in it is exactly this kind of Qt/PaneState-bound
resolution, unchanged from Phase 3's own scope decision (phase3-report.md
§3).
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_takkub.core.models.account import ProviderAccount
from agent_takkub.core.models.spawn_plan import SpawnPlan

# Only providers with a real, documented isolation knob today
# (config.py's `_PROVIDER_HOME_SUBDIRS` / `pane_env.inject_user_profile_env`)
# get an account-driven env override. A provider with no knob
# (config.PROVIDER_ISOLATION_GAPS: gemini/kimi/cursor) cannot be
# differentiated per-account yet — silently doing nothing is correct here,
# not a bug to paper over.
_ACCOUNT_ENV_VAR: dict[str, str] = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}


def account_env_overrides(provider_id: str, account: ProviderAccount | None) -> dict[str, str]:
    """Env overrides an *account* contributes for *provider_id* — empty dict
    when there's no account, no `config_dir` on it, or no known isolation
    var for this provider. Never raises."""
    if account is None or not account.config_dir:
        return {}
    var = _ACCOUNT_ENV_VAR.get(provider_id)
    if not var:
        return {}
    return {var: account.config_dir}


def assemble_generic_argv(
    provider_bin: str,
    autonomy_argv: Sequence[str] = (),
    model_argv: Sequence[str] = (),
    effort_argv: Sequence[str] = (),
    mcp_argv: Sequence[str] = (),
    project_scope_argv: Sequence[str] = (),
    resume_argv: Sequence[str] = (),
) -> list[str]:
    """Pure re-assembly of the generic non-claude branch's argv, same order
    the branch builds it in: bin, autonomy flags, model, effort, MCP,
    project-scope, resume. Every *_argv piece is caller-resolved (already
    empty when that piece doesn't apply) — this function only concatenates.
    """
    return [
        provider_bin,
        *autonomy_argv,
        *model_argv,
        *effort_argv,
        *mcp_argv,
        *project_scope_argv,
        *resume_argv,
    ]


def build_generic_spawn_plan(
    provider_id: str,
    provider_bin: str,
    base_env: dict[str, str],
    cwd: str,
    *,
    account: ProviderAccount | None = None,
    autonomy_argv: Sequence[str] = (),
    model_argv: Sequence[str] = (),
    effort_argv: Sequence[str] = (),
    mcp_argv: Sequence[str] = (),
    project_scope_argv: Sequence[str] = (),
    resume_argv: Sequence[str] = (),
) -> SpawnPlan:
    """Combine `assemble_generic_argv` + `account_env_overrides` into one
    `SpawnPlan` for a non-claude provider. `base_env` is copied, never
    mutated in place — the caller (`spawn_engine.py`) decides whether/how
    to apply `plan.env` to its own env dict."""
    env = dict(base_env)
    env.update(account_env_overrides(provider_id, account))
    argv = assemble_generic_argv(
        provider_bin,
        autonomy_argv=autonomy_argv,
        model_argv=model_argv,
        effort_argv=effort_argv,
        mcp_argv=mcp_argv,
        project_scope_argv=project_scope_argv,
        resume_argv=resume_argv,
    )
    return SpawnPlan(provider_id=provider_id, argv=tuple(argv), env=env, cwd=cwd)
