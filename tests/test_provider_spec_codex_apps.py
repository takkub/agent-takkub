"""#283 — the cockpit must disable codex's built-in `apps` feature at spawn.

`codex_apps` (feature flag `apps`, stable, default ON) boots for minutes and
paints "esc to interrupt" in the status bar the whole time, while its composer
is already usable. That string is codex's own `ready_hard_blockers`, so the
cockpit reads the pane as busy and never delivers. Measured on the reporter's
machine: apps ON → ready at 342s/388s, task never delivered; apps OFF → ready
0s, delivered in 1s.

It cannot be removed from the outside — `codex_apps` is internal to codex, so
the injected `shared-mcp-*.json` has no say. `--disable apps` at spawn is the
only lever, and it stays session-scoped.
"""

from __future__ import annotations

from agent_takkub.provider_spec import PROVIDER_REGISTRY


def _flags(platform_key: str) -> list[str]:
    return list(PROVIDER_REGISTRY["codex"].autonomy_flags[platform_key])


def test_apps_feature_disabled_on_windows() -> None:
    flags = _flags("win32")
    assert "--disable" in flags
    assert flags[flags.index("--disable") + 1] == "apps"


def test_apps_feature_disabled_on_posix() -> None:
    flags = _flags("default")
    assert "--disable" in flags
    assert flags[flags.index("--disable") + 1] == "apps"


def test_existing_autonomy_flags_are_preserved() -> None:
    """The sandbox/approval flags are what make a codex pane autonomous at
    all — the feature switch must be additive, never a replacement."""
    assert "--dangerously-bypass-approvals-and-sandbox" in _flags("win32")
    posix = _flags("default")
    for expected in ("--ask-for-approval", "never", "-s", "workspace-write"):
        assert expected in posix


def test_config_toml_is_not_touched() -> None:
    """Session-scoped only: the user's own codex config must stay theirs
    (same rule `mcp_adapter_variant="session_override"` already follows)."""
    for key in ("win32", "default"):
        joined = " ".join(_flags(key))
        assert "config.toml" not in joined
        assert "features.apps" not in joined, "must use the CLI flag, not a config write"
