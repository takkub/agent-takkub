"""Pure claude argv assembly (epic #309 2.0.0 Wave A — closes the gap
`claude_adapter.py`'s own module docstring tracked: "extracting [the claude
branch] ... is a tracked gap, not this method's job").

Mirrors `plan.py`'s `assemble_generic_argv` exactly, same shape, same
reasoning: `spawn_engine.py`'s ~800-line claude branch (roughly lines
2639-2947, verified 2026-08-22 against branch `wt/backend-1-1787380152`)
builds its argv by repeatedly mutating one `argv: list[str]` in place,
interleaved with the Qt/PaneState-bound resolution that decides *what*
each flag's value is (model/effort precedence against `PaneState`, session
hook wiring via `hook_wiring.ensure_hook_settings_file`, MCP subprocess
resolution, resume-uuid bookkeeping against `self._pane_state`). None of
that resolution is pure — it stays in `spawn_engine.py`, unchanged, exactly
per this phase's scope decision (`claude_adapter.py:5-17`).

What IS pure is the final *order* the branch concatenates already-resolved
pieces in. This module extracts only that: a `assemble_claude_argv`
function `agent_takkub.core` can import without pulling PyQt6 in (no
`spawn_engine` import, no `self`, no I/O), taking each flag-group as a
caller-supplied, already-resolved `Sequence[str]` (empty when that group
doesn't apply — a per-role gate, an env override, a missing role_md_file,
...). Order verified against `spawn_engine.py` by
`tests/test_core_providers_claude_plan.py`'s golden-order test, same
method `plan.py`'s own docstring used for the generic branch.

Deliberately NOT wired into `spawn_engine.py`'s live branch this phase —
same posture `assemble_generic_argv` has had since Phase 3b (extracted,
tested, importable; the live hot path still builds its argv inline).
Rewiring the hot-path caller is a separate, later change that can be
proven behavior-neutral under the full suite on its own, not bundled into
the extraction itself.
"""

from __future__ import annotations

from collections.abc import Sequence


def assemble_claude_argv(
    claude_bin: str,
    *,
    setting_sources: str,
    settings_argv: Sequence[str] = (),
    model_argv: Sequence[str] = (),
    effort_argv: Sequence[str] = (),
    fallback_argv: Sequence[str] = (),
    disallowed_tools_argv: Sequence[str] = (),
    plugin_dir_argv: Sequence[str] = (),
    system_prompt_argv: Sequence[str] = (),
    mcp_argv: Sequence[str] = (),
    denied_tools_argv: Sequence[str] = (),
    resume_argv: Sequence[str] = (),
) -> list[str]:
    """Pure re-assembly of the claude branch's argv, same order the branch
    builds it in (`spawn_engine.py` ~2639-2947):

    1. `claude_bin`, `--dangerously-skip-permissions`, `--setting-sources`,
       `setting_sources`
    2. `settings_argv` — Stop/Notification hook wiring (`--settings <path>`)
    3. `model_argv` — `--model` (Lead's or a teammate's, whichever branch ran)
    4. `effort_argv` — `--effort`-equivalent flag (provider-gated, may be empty)
    5. `fallback_argv` — `--fallback-model`
    6. `disallowed_tools_argv` — teammate-only `--disallowedTools` (pane-mode
       restriction; empty for Lead)
    7. `plugin_dir_argv` — one `--plugin-dir <dir>` pair per resolved plugin
    8. `system_prompt_argv` — the role's system-prompt flag + rendered file
    9. `mcp_argv` — MCP config flags from `mcp_bridge.mcp_argv_for_provider`
    10. `denied_tools_argv` — `--disallowed-tools` hard-deny list (Task/
        AskUserQuestion)
    11. `resume_argv` — `--resume <uuid>` or `--session-id <uuid>`

    Every `*_argv` piece is caller-resolved (already empty when that piece
    doesn't apply) — this function only concatenates, never resolves.
    """
    return [
        claude_bin,
        "--dangerously-skip-permissions",
        "--setting-sources",
        setting_sources,
        *settings_argv,
        *model_argv,
        *effort_argv,
        *fallback_argv,
        *disallowed_tools_argv,
        *plugin_dir_argv,
        *system_prompt_argv,
        *mcp_argv,
        *denied_tools_argv,
        *resume_argv,
    ]
