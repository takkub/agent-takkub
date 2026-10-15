"""How a restricted Lead's policy is held (#842).

When the effective team policy says ``lead_may_implement: false`` the Lead may
read, brief, assign and report but must not change project files.

A Claude Lead runs with ``--dangerously-skip-permissions`` like every Claude
pane; its boundary is the cockpit's own PreToolUse guards (``pane_guard``, the
Lead edit guard), which read the preset live. 2.2.16 put a restricted Claude
Lead under Claude Code's ``dontAsk`` allowlist instead; that left the Lead
unusable on other machines and was reverted (2.2.17).

A Codex Lead runs with the same autonomy flags as every Codex pane. 2.2.15 ran
a restricted Codex Lead inside Codex's OS sandbox; that was removed
(2026-10-14) — its Windows admin setup popped a UAC prompt on every new
machine and a Mac Lead never opened. Its restriction is the Lead policy text
only, and readiness says so (``prompt_only``) instead of claiming a boundary.
"""

from __future__ import annotations

#: Enforcement modes recorded on the Lead readiness state.
MODE_CLAUDE_HOOKS = "claude_hooks"
MODE_CODEX_PROMPT = "codex_prompt"
#: Gemini/agy, OpenCode and Cursor Leads: held by the Lead policy text, like Codex.
MODE_POLICY_PROMPT = "policy_prompt"
MODE_NONE = "none"

#: (provider, mode) pairs that can run a restricted Lead → gate label. None
#: is an OS boundary — the labels say what actually holds the policy.
_ENFORCED = {
    ("claude", MODE_CLAUDE_HOOKS): "pretooluse_guard",
    ("codex", MODE_CODEX_PROMPT): "prompt_only",
    **{(p, MODE_POLICY_PROMPT): "prompt_only" for p in ("gemini", "opencode", "cursor")},
}

#: Providers that can run a restricted Lead at all.
RESTRICTED_LEAD_PROVIDERS = frozenset(provider for provider, _mode in _ENFORCED)

#: Modes that hold a restricted policy, whichever provider recorded them.
ENFORCED_MODES = frozenset(mode for _provider, mode in _ENFORCED)


def lead_is_restricted(project: str | None) -> bool:
    """True when the project's effective policy forbids the Lead to implement.
    Unreadable policy counts as restricted — the safe direction."""
    from . import team_preset

    try:
        return not bool(team_preset.current(project).get("lead_may_implement"))
    except Exception:
        return True


def enforcement_gap(provider: str, mode: str | None, restricted: bool) -> str:
    """Why a restricted Lead session is NOT actually restricted, or ``""``.

    A restricted Lead needs a recorded mode: the cockpit's PreToolUse guards
    on a Claude Lead, or the policy text on a Codex Lead. Other providers
    record neither, so report those sessions as unavailable instead of ready."""
    if not restricted or (provider, mode) in _ENFORCED:
        return ""
    return (
        f"restricted Lead policy is not enforced for {provider or 'unknown'} "
        "in this session — restart Lead with a supported runtime"
    )


def gate_label(provider: str, mode: str | None, restricted: bool) -> str:
    """Truthful one-word capability for ``takkub context status``."""
    if not restricted:
        return "not_required"
    return _ENFORCED.get((provider, mode), "unenforced")
