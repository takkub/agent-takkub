"""Tests for Ctrl/Cmd+wheel terminal font zoom.

The wheel-driven zoom itself lives in terminal.html (JS) and cannot run in
this Python-only suite, but every piece it drives on the Python side is pure
logic exercised here with ``__new__`` instances (same pattern as
test_input_lock.py) so no QApplication/QWebEngine is required:

  1. TerminalWidget.set_font_point_size() clamps to the 8-24pt bound.
  2. TerminalWidget._on_font_zoomed() (the JS -> Python bridge callback)
     propagates the already-clamped pt size via fontSizeChanged.
  3. AgentPane's font persistence: a role-specific size takes priority, but
     falls back to the global "last zoomed" default so a newly spawned pane
     of a *different* role still picks up the last size the user chose.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock

from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import Role
from agent_takkub.terminal_widget import TerminalWidget


class _FakeSettings:
    """In-memory stand-in for QSettings("agent-takkub", "cockpit").

    Real QSettings persists per (org, app) pair regardless of how many
    instances are constructed; this mirrors that with a class-level dict
    shared across all _FakeSettings() calls within a test.
    """

    _store: ClassVar[dict[str, object]] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def value(self, key):
        return self._store.get(key)

    def setValue(self, key, value):
        self._store[key] = value

    @classmethod
    def reset(cls) -> None:
        cls._store = {}


# ─────────────────────────────────────────────────────────────────────
# TerminalWidget — pt clamp + JS->Python zoom propagation
# ─────────────────────────────────────────────────────────────────────
class TestTerminalWidgetFontZoom:
    def _make(self) -> TerminalWidget:
        tw = TerminalWidget.__new__(TerminalWidget)
        tw._view = MagicMock()
        tw.fontSizeChanged = MagicMock()
        return tw

    def test_set_font_point_size_clamps_low(self) -> None:
        tw = self._make()
        tw.set_font_point_size(1)
        tw.fontSizeChanged.emit.assert_called_once_with(8)

    def test_set_font_point_size_clamps_high(self) -> None:
        tw = self._make()
        tw.set_font_point_size(99)
        tw.fontSizeChanged.emit.assert_called_once_with(24)

    def test_set_font_point_size_within_bounds_unchanged(self) -> None:
        tw = self._make()
        tw.set_font_point_size(14)
        tw.fontSizeChanged.emit.assert_called_once_with(14)

    def test_on_font_zoomed_propagates_pt_from_js(self) -> None:
        """JS already clamped + applied the size; Python just relays it for
        persistence — it must NOT call back into runJavaScript again."""
        tw = self._make()
        tw._on_font_zoomed(16)
        tw.fontSizeChanged.emit.assert_called_once_with(16)
        tw._view.page.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# AgentPane — per-role size with global "last used" fallback
# ─────────────────────────────────────────────────────────────────────
class TestFontSizePersistence:
    def _make_pane(self, role: Role) -> AgentPane:
        pane = AgentPane.__new__(AgentPane)
        pane.role = role
        pane._terminal = MagicMock()
        return pane

    def setup_method(self) -> None:
        _FakeSettings.reset()

    def test_save_writes_role_and_global_key(self, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.agent_pane.QSettings", _FakeSettings)
        pane = self._make_pane(Role("frontend", "Frontend", "#fff", column=1, row=0))
        pane._save_font_size(18)
        assert _FakeSettings._store["pane/frontend/font_pt"] == 18
        assert _FakeSettings._store["pane/_default/font_pt"] == 18

    def test_restore_prefers_role_specific_over_global(self, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.agent_pane.QSettings", _FakeSettings)
        _FakeSettings._store["pane/backend/font_pt"] = 20
        _FakeSettings._store["pane/_default/font_pt"] = 12
        pane = self._make_pane(Role("backend", "Backend", "#fff", column=1, row=1))
        pane._restore_font_size()
        pane._terminal.set_font_point_size.assert_called_once_with(20)

    def test_restore_falls_back_to_global_default(self, monkeypatch) -> None:
        """A pane spawned for a role that was never individually zoomed
        picks up the most recent zoom from any other role/pane."""
        monkeypatch.setattr("agent_takkub.agent_pane.QSettings", _FakeSettings)
        _FakeSettings._store["pane/_default/font_pt"] = 16
        pane = self._make_pane(Role("qa", "QA", "#fff", column=2, row=0))
        pane._restore_font_size()
        pane._terminal.set_font_point_size.assert_called_once_with(16)

    def test_restore_no_saved_size_does_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.agent_pane.QSettings", _FakeSettings)
        pane = self._make_pane(Role("devops", "DevOps", "#fff", column=1, row=3))
        pane._restore_font_size()
        pane._terminal.set_font_point_size.assert_not_called()
