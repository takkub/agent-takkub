"""Integration smoke test: toggle_provider() writes state + emits signal.

Verifies the orchestrator method without spawning a full Lead pane —
the broadcast-into-pane path is exercised only when a live pane exists,
which is covered by manual smoke testing in spec section 8.2.

Note: pytest-qt (qtbot fixture) is not installed in this repo. Tests use
plain PyQt6 signal connection via a Python list to capture emissions.
"""

from __future__ import annotations

import pytest

from agent_takkub import provider_state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    path = tmp_path / "disabled-providers.json"
    monkeypatch.setattr(provider_state, "_PATH", path)
    return path


def test_toggle_provider_persists_and_emits(tmp_state_path):
    """toggle_provider() writes to disk and emits providerStateChanged."""
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    received: list[tuple[str, bool]] = []
    orch.providerStateChanged.connect(lambda p, d: received.append((p, d)))

    ok, msg = orch.toggle_provider("codex", True)
    assert ok
    assert "disabled" in msg.lower()
    assert provider_state.is_disabled("codex") is True
    assert received == [("codex", True)]

    ok, msg = orch.toggle_provider("codex", False)
    assert ok
    assert provider_state.is_disabled("codex") is False
    assert received == [("codex", True), ("codex", False)]


def test_toggle_provider_rejects_unknown(tmp_state_path):
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    ok, msg = orch.toggle_provider("bogus", True)
    assert not ok
    assert "unknown provider" in msg
