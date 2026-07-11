"""Standalone dev-entry ("Open legacy settings" wiring) tests for
agent_takkub.settings_management.__main__ — codex cross-check MEDIUM-3: the
hook was a no-op ``lambda: None`` before, leaving the button dead in
standalone mode (window.py only gets a real wire-up via user_actions.py's
cockpit path)."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub.settings_management import __main__ as settings_main


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_open_legacy_settings_opens_settings_window_as_modal_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _StubDialog:
        def __init__(self, parent, project=None, initial_view=None) -> None:
            calls["parent"] = parent
            calls["project"] = project
            calls["initial_view"] = initial_view

        def exec(self) -> None:
            calls["executed"] = True

    monkeypatch.setattr("agent_takkub.settings_window.SettingsWindow", _StubDialog)
    monkeypatch.setattr("agent_takkub.config.active_project", lambda: ("test-project", {}))

    parent = QWidget()
    settings_main._open_legacy_settings(parent)

    assert calls["parent"] is parent
    assert calls["project"] == "test-project"
    assert calls["executed"] is True


def test_open_legacy_settings_survives_active_project_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cockpit process/config around a bare dev-entry run is a real
    scenario, not just a test contrivance — the hook must not crash."""
    calls: dict[str, object] = {}

    class _StubDialog:
        def __init__(self, parent, project=None, initial_view=None) -> None:
            calls["project"] = project

        def exec(self) -> None:
            calls["executed"] = True

    monkeypatch.setattr("agent_takkub.settings_window.SettingsWindow", _StubDialog)

    def _raise():
        raise RuntimeError("no active project")

    monkeypatch.setattr("agent_takkub.config.active_project", _raise)

    settings_main._open_legacy_settings(QWidget())

    assert calls["project"] is None
    assert calls["executed"] is True


def test_main_wires_open_legacy_requested_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` must not leave the default no-op ``lambda: None`` hook in
    place — clicking "Open legacy settings" must actually forward to
    ``_open_legacy_settings``. Stubs out QApplication construction/exec:
    the qapp fixture already owns the one real QApplication instance for
    this process, and PyQt does not support constructing a second one."""
    from agent_takkub.settings_management.window import SettingsManagementWindow

    class _StubApp:
        def __init__(self, argv) -> None:
            pass

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(settings_main, "QApplication", _StubApp)
    monkeypatch.setattr(SettingsManagementWindow, "show", lambda self: None)

    captured: list[SettingsManagementWindow] = []
    real_init = SettingsManagementWindow.__init__

    def _capture_init(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.append(self)

    monkeypatch.setattr(SettingsManagementWindow, "__init__", _capture_init)

    hook_calls: list[object] = []
    monkeypatch.setattr(
        settings_main, "_open_legacy_settings", lambda parent: hook_calls.append(parent)
    )

    settings_main.main()

    assert len(captured) == 1
    window = captured[0]
    window.open_legacy_requested()
    assert hook_calls == [window]
