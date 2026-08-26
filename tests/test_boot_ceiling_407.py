"""#407 — the delivery boot-stall ceiling scales with the number of MCP
servers the pane was spawned with, and the boot-timeout FAILED notice names
low RAM when the governor's sample is under its own pause line."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_inbox import LeadInboxMixin
from agent_takkub.spawn_engine import PaneState


def _fake(count: int | None) -> SimpleNamespace:
    ps = PaneState()
    if count is not None:
        ps.mcp_server_count = count
    return SimpleNamespace(
        _resolve_project=lambda p: "proj",
        _pane_state={"proj::qa": ps} if count is not None else {},
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (None, 300),  # no pane state at all → original ceiling
        (0, 300),  # spawned without MCP servers → original ceiling
        (1, 300),  # 110 + 120 < 300 → the base still wins
        (2, 350),  # codex + playwright + chrome-devtools (#407's repro)
        (3, 470),
    ],
)
def test_boot_stall_ceiling_scales_with_mcp_server_count(monkeypatch, count, expected) -> None:
    monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 300)
    monkeypatch.setattr(orch_mod, "BOOT_STALL_GRACE_SEC", 110)
    monkeypatch.setattr(orch_mod, "MCP_STARTUP_TIMEOUT_SEC", 120)
    assert LeadInboxMixin._boot_stall_ceiling_sec(_fake(count), None, "qa") == expected


def test_env_override_of_base_ceiling_still_wins_when_larger(monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 900)
    monkeypatch.setattr(orch_mod, "MCP_STARTUP_TIMEOUT_SEC", 120)
    assert LeadInboxMixin._boot_stall_ceiling_sec(_fake(3), None, "qa") == 900


def test_pane_state_default_mcp_server_count_is_zero() -> None:
    assert PaneState().mcp_server_count == 0


class _Gov:
    def __init__(self, ram: float, floor: float = 12.0) -> None:
        self._ram = ram
        self.limits = SimpleNamespace(min_available_ram_percent=floor)

    def current_metrics(self):
        return 30.0, self._ram


def test_ram_context_only_when_under_governor_floor() -> None:
    assert LeadInboxMixin._boot_stall_ram_context(SimpleNamespace(_resource_governor=None)) == ""
    ok_ram = SimpleNamespace(_resource_governor=_Gov(25.0))
    assert LeadInboxMixin._boot_stall_ram_context(ok_ram) == ""
    low = LeadInboxMixin._boot_stall_ram_context(SimpleNamespace(_resource_governor=_Gov(11.0)))
    assert "11%" in low and "#407" in low
