"""#991: codex died in its own TUI bootstrap ("account/read failed during TUI
bootstrap … workspace routing discovery timed out") three times in 40 s; the
crash-respawn loop hit its cap and the task never started. Such an exit now
reroutes the outstanding task to another provider, like a quota hit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_takkub.provider_spec import bootstrap_failure_marker
from agent_takkub.spawn_engine import SpawnEngineMixin

_SCREEN = [
    "Error: account/read failed during TUI bootstrap: account/read failed: "
    "workspace routing discovery timed out (code -32603)",
]


def _self(new_provider: str | None):
    return SimpleNamespace(
        _pick_reroute_provider=MagicMock(return_value=new_provider),
        _reroute_pane_to_provider=MagicMock(),
    )


def _session(lines):
    return SimpleNamespace(display_lines=lambda: list(lines))


def test_codex_bootstrap_failure_reroutes_the_task() -> None:
    fake = _self("claude")
    ps = SimpleNamespace(last_assigned_task="do the thing")
    rerouted = SpawnEngineMixin._maybe_reroute_bootstrap_failure(
        fake, "ai", "devops", ps, "codex", _session(_SCREEN)
    )
    assert rerouted is True
    args = fake._reroute_pane_to_provider.call_args
    assert args.args[:5] == ("ai", "devops", ps, "claude", "codex")
    assert "bootstrap" in args.kwargs["cause"]


def test_no_reroute_without_a_task_or_marker_or_target() -> None:
    ps = SimpleNamespace(last_assigned_task="x")
    idle = SimpleNamespace(last_assigned_task="")
    fake = _self("claude")
    assert not SpawnEngineMixin._maybe_reroute_bootstrap_failure(
        fake, "ai", "devops", idle, "codex", _session(_SCREEN)
    )
    assert not SpawnEngineMixin._maybe_reroute_bootstrap_failure(
        fake, "ai", "devops", ps, "codex", _session(["Segmentation fault"])
    )
    nowhere = _self(None)
    assert not SpawnEngineMixin._maybe_reroute_bootstrap_failure(
        nowhere, "ai", "devops", ps, "codex", _session(_SCREEN)
    )
    fake._reroute_pane_to_provider.assert_not_called()


def test_marker_is_provider_scoped() -> None:
    assert bootstrap_failure_marker("codex", _SCREEN) == "account/read failed during tui bootstrap"
    # an agent quoting codex's error inside a claude pane is not claude failing
    assert bootstrap_failure_marker("claude", _SCREEN) is None
