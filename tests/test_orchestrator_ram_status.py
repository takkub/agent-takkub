"""#364 lever 6 — `Orchestrator.pane_ram_specs`/`ram_status`.

Called unbound against a bare `SimpleNamespace` (only the attributes these
two methods actually touch) rather than constructing a full `Orchestrator`
(5.8k-LOC Qt object) — same technique used throughout this test suite for
testing one orchestrator method in isolation."""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.orchestrator import Orchestrator


class _FakeSession:
    def __init__(self, pid: int | None) -> None:
        self.pid = pid


class _FakeTerminal:
    def __init__(self, is_discarded: bool = False) -> None:
        self.is_discarded = is_discarded


class _FakePane:
    def __init__(
        self, provider_name: str | None, pid: int | None, *, discarded: bool = False
    ) -> None:
        self.provider_name = provider_name
        self.session = _FakeSession(pid) if pid is not None else None
        self._terminal = _FakeTerminal(discarded)


def test_pane_ram_specs_is_cheap_and_covers_every_project() -> None:
    fake = SimpleNamespace(
        _panes_by_project={
            "proj1": {
                "backend": _FakePane("claude", 200),
                "lead": _FakePane("claude", None),
            },
            "proj2": {"qa": _FakePane("codex", 300)},
        }
    )

    specs = Orchestrator.pane_ram_specs(fake)

    by_key = {(s["project"], s["role"]): s for s in specs}
    assert by_key[("proj1", "backend")] == {
        "role": "backend",
        "project": "proj1",
        "provider": "claude",
        "pid": 200,
        "discarded": False,
    }
    assert by_key[("proj1", "lead")]["pid"] is None
    assert by_key[("proj2", "qa")]["pid"] == 300


def test_pane_ram_specs_reports_discarded_flag() -> None:
    """#364 lever 1: `doctor --ram` needs to know which pane's renderer is
    currently discarded, not just its (near-zero) RSS number."""
    fake = SimpleNamespace(
        _panes_by_project={
            "proj1": {
                "backend": _FakePane("claude", 200, discarded=True),
                "lead": _FakePane("claude", 201, discarded=False),
            }
        }
    )

    specs = Orchestrator.pane_ram_specs(fake)

    by_key = {(s["project"], s["role"]): s for s in specs}
    assert by_key[("proj1", "backend")]["discarded"] is True
    assert by_key[("proj1", "lead")]["discarded"] is False


def test_pane_ram_specs_defaults_discarded_false_without_terminal() -> None:
    """A pane double lacking `_terminal` (older test doubles, __new__-built
    panes) must not raise — defaults to not-discarded."""
    fake = SimpleNamespace(
        _panes_by_project={"proj1": {"backend": SimpleNamespace(provider_name="claude")}}
    )

    specs = Orchestrator.pane_ram_specs(fake)

    assert specs[0]["discarded"] is False


def test_ram_status_forwards_pane_specs_and_governor_threshold(monkeypatch) -> None:
    import agent_takkub.ram_report as ram_report_mod

    captured: dict = {}

    def fake_collect(specs, *, main_pid, governor_min_ram_percent=None, **_kw):
        captured["specs"] = specs
        captured["main_pid"] = main_pid
        captured["min_ram"] = governor_min_ram_percent
        return {"ok": True}

    monkeypatch.setattr(ram_report_mod, "collect_ram_report", fake_collect)

    governor = SimpleNamespace(limits=SimpleNamespace(min_available_ram_percent=12.0))
    fake = SimpleNamespace(
        _panes_by_project={"proj1": {"backend": _FakePane("claude", 200)}},
        _resource_governor=governor,
    )
    fake.pane_ram_specs = lambda: Orchestrator.pane_ram_specs(fake)

    result = Orchestrator.ram_status(fake)

    assert result == {"ok": True}
    assert captured["min_ram"] == 12.0
    assert captured["specs"] == [
        {
            "role": "backend",
            "project": "proj1",
            "provider": "claude",
            "pid": 200,
            "discarded": False,
        }
    ]


def test_ram_status_tolerates_a_missing_governor(monkeypatch) -> None:
    import agent_takkub.ram_report as ram_report_mod

    captured: dict = {}
    monkeypatch.setattr(
        ram_report_mod,
        "collect_ram_report",
        lambda specs, **kw: captured.update(kw) or {"ok": True},
    )

    fake = SimpleNamespace(_panes_by_project={}, _resource_governor=None)
    fake.pane_ram_specs = lambda: Orchestrator.pane_ram_specs(fake)
    Orchestrator.ram_status(fake)

    assert captured["governor_min_ram_percent"] is None


def test_ram_profile_delegates_to_ram_report(monkeypatch) -> None:
    """#364 lever 5 — `Orchestrator.ram_profile` is a thin, argument-free
    delegation to `ram_report.collect_main_process_profile` (that function
    can only ever describe the process it runs in, so there is nothing else
    for this method to gather or pass through)."""
    import agent_takkub.ram_report as ram_report_mod

    sentinel = {"ok": True, "gc_object_count": 42}
    monkeypatch.setattr(ram_report_mod, "collect_main_process_profile", lambda: sentinel)

    fake = SimpleNamespace()
    result = Orchestrator.ram_profile(fake)

    assert result is sentinel
