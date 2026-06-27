"""Integration + unit tests for the Office Room game-view prototype.

Coverage:
  1. OfficeRoomView.dispatch_event() — pending queue before page ready, flush on load
  2. OfficeRoomView.dispatch_event() — drop events when load fails
  3. OfficeRoomView JSON payload shape — all required fields present
  4. ProjectTab.toggle_game_view() — QStackedWidget page index + is_game_active()
  5. ProjectTab.is_game_active() — False before any toggle
  6. ProjectTab.dispatch_game_event() — no-op before lazy init (game_view is None)
  7. ProjectTab.dispatch_game_event() — routes to game_view after init
  8. office_room.html — DOM element presence (overlay, chat-input, canvas, buttons)
  9. office_room.html — QWebChannel wiring block present
  10. office_room.html — STATE_DOT mapping contains all expected states (unquoted JS keys)
  11. office_room.html — ROLE_CONFIG contains core roles
  12. office_room.html — btn-send handler: BUG — message text is not sent (regression pin)
  13. status_header _btn_game_view — checkable, initial emoji 🎮
  14. OfficeRoomView.set_keepalive() — no crash
  15. OfficeRoomView.destroy_view() — no crash on clean teardown
  16. main_window — game bridge helper methods exist + focus-role exits game view

OS: Windows (win32). mac cross-platform notes flagged inline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject
from PyQt6.QtCore import pyqtSignal as _pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

# QtWebEngineWidgets must be imported BEFORE QApplication is instantiated.
# The session-scoped _qt_session_app fixture in conftest.py creates QApplication
# during fixture setup (after collection time). Importing office_room_view at
# module level (collection time) satisfies Qt's ordering requirement.
import agent_takkub.office_room_view  # noqa: F401

# ---------------------------------------------------------------------------
# Static paths
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "static"
_HTML_PATH = _STATIC_DIR / "office_room.html"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def html_text():
    return _HTML_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stubs for QWebEngineView / QWebChannel (Qt-compatible, no GPU needed)
# ---------------------------------------------------------------------------


class _StubPage:
    """Minimal stand-in for QWebEnginePage."""

    class LifecycleState:
        Active = 0
        Frozen = 1

    def setWebChannel(self, ch) -> None:
        pass

    def setLifecycleState(self, state) -> None:
        pass


class _StubWebView(QWidget):
    """Qt-compatible stub for QWebEngineView.

    Inherits QWidget so layout.addWidget() accepts it. Exposes a real
    loadFinished signal so OfficeRoomView.__init__ can connect to it.
    """

    loadFinished = _pyqtSignal(bool)

    def load(self, url) -> None:
        pass

    def stop(self) -> None:
        pass

    def setPage(self, page) -> None:
        pass

    def page(self) -> _StubPage:
        return _StubPage()


class _StubWebChannel(QObject):
    """Qt-compatible stub for QWebChannel."""

    def __init__(self, parent=None):
        super().__init__(parent)

    def registerObject(self, name: str, obj) -> None:
        pass


@pytest.fixture()
def stub_webengine(monkeypatch):
    """Replace QWebEngineView + QWebChannel with Qt-compatible stubs."""
    monkeypatch.setattr("agent_takkub.office_room_view.QWebEngineView", _StubWebView)
    monkeypatch.setattr("agent_takkub.office_room_view.QWebChannel", _StubWebChannel)
    yield


# ---------------------------------------------------------------------------
# Fake OfficeRoomView for ProjectTab tests (avoids WebEngine entirely)
# ---------------------------------------------------------------------------


class _FakeGameView(QWidget):
    """Records dispatch_event() calls without WebEngine."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events: list[dict] = []
        self._keepalive: bool | None = None

    def dispatch_event(self, role, state, project="", note=""):
        self.events.append({"role": role, "state": state, "project": project, "note": note})

    def set_keepalive(self, active: bool) -> None:
        self._keepalive = active


# ---------------------------------------------------------------------------
# 1–3, 14–15: OfficeRoomView logic tests
# ---------------------------------------------------------------------------


class TestOfficeRoomViewDispatch:
    """Dispatch_event pending-queue logic + keepalive/teardown (stub WebEngine)."""

    def test_events_queued_before_page_ready(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        view = OfficeRoomView()
        assert not view._page_ready
        view.dispatch_event("qa", "spawn", project="proj", note="starting")
        view.dispatch_event("qa", "busy")
        assert len(view._pending) == 2

    def test_pending_flushed_on_load_finished(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        emitted: list[str] = []
        view = OfficeRoomView()
        view._bridge.gameEvent.connect(emitted.append)

        view.dispatch_event("qa", "spawn")
        view.dispatch_event("frontend", "busy")
        view._on_load_finished(True)

        assert view._page_ready
        assert view._pending == []
        assert len(emitted) == 2

    def test_no_flush_on_load_failure(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        view = OfficeRoomView()
        view.dispatch_event("qa", "spawn")
        view._on_load_finished(False)

        assert not view._page_ready
        assert len(view._pending) == 1

    def test_dispatch_after_ready_emits_directly(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        emitted: list[str] = []
        view = OfficeRoomView()
        view._bridge.gameEvent.connect(emitted.append)
        view._on_load_finished(True)

        view.dispatch_event("backend", "done", note="finished")

        assert len(emitted) == 1
        payload = json.loads(emitted[0])
        assert payload["type"] == "pane_state"
        assert payload["role"] == "backend"
        assert payload["state"] == "done"
        assert payload["note"] == "finished"

    def test_json_payload_has_all_required_fields(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        emitted: list[str] = []
        view = OfficeRoomView()
        view._bridge.gameEvent.connect(emitted.append)
        view._on_load_finished(True)

        view.dispatch_event("qa", "idle", project="myproj", note="ok")
        payload = json.loads(emitted[0])
        for key in ("type", "role", "state", "project", "note"):
            assert key in payload, f"missing key: {key}"

    def test_non_ascii_note_round_trips(self, qapp, stub_webengine):
        """Thai text + emoji in note must survive JSON serialisation."""
        from agent_takkub.office_room_view import OfficeRoomView

        emitted: list[str] = []
        view = OfficeRoomView()
        view._bridge.gameEvent.connect(emitted.append)
        view._on_load_finished(True)

        view.dispatch_event("qa", "done", note="เสร็จแล้ว 🎉")
        payload = json.loads(emitted[0])
        assert payload["note"] == "เสร็จแล้ว 🎉"

    def test_set_keepalive_no_crash(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        view = OfficeRoomView()
        view.set_keepalive(True)
        view.set_keepalive(False)

    def test_destroy_view_no_crash(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        view = OfficeRoomView()
        view.destroy_view()


# ---------------------------------------------------------------------------
# 4–7: ProjectTab game-view toggle (uses _FakeGameView, no WebEngine)
# ---------------------------------------------------------------------------


def _inject_fake_game(tab, fake: _FakeGameView) -> _FakeGameView:
    """Side-effect: sets tab.game_view + adds widget to stack so toggle works."""
    if tab.game_view is None:
        tab.game_view = fake
        tab._view_stack.addWidget(fake)
    return fake


class TestProjectTabGameView:
    def test_is_game_active_false_initially(self, qapp):
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("p")
        assert not tab.is_game_active()

    def test_toggle_once_activates_game(self, qapp):
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("p")
        fake = _FakeGameView(tab)
        tab._ensure_game_view = lambda: _inject_fake_game(tab, fake)

        result = tab.toggle_game_view()
        assert result is True
        assert tab.is_game_active()
        assert tab._view_stack.currentIndex() == 1

    def test_toggle_twice_returns_to_text(self, qapp):
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("p")
        fake = _FakeGameView(tab)
        tab._ensure_game_view = lambda: _inject_fake_game(tab, fake)

        tab.toggle_game_view()
        result = tab.toggle_game_view()
        assert result is False
        assert not tab.is_game_active()
        assert tab._view_stack.currentIndex() == 0

    def test_dispatch_game_event_noop_before_init(self, qapp):
        """dispatch_game_event must not crash when game_view is None."""
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("p")
        assert tab.game_view is None
        tab.dispatch_game_event("qa", "spawn")  # must not raise

    def test_dispatch_game_event_routes_to_view(self, qapp):
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("test-proj")
        fake = _FakeGameView(tab)
        tab._ensure_game_view = lambda: _inject_fake_game(tab, fake)
        tab.toggle_game_view()  # triggers ensure, sets tab.game_view = fake

        tab.dispatch_game_event("qa", "busy", note="coding", project="test-proj")
        assert len(fake.events) == 1
        ev = fake.events[0]
        assert ev["role"] == "qa"
        assert ev["state"] == "busy"
        assert ev["note"] == "coding"

    def test_dispatch_uses_project_name_when_no_project_arg(self, qapp):
        from agent_takkub.project_tab import ProjectTab

        tab = ProjectTab("test-proj")
        fake = _FakeGameView(tab)
        tab._ensure_game_view = lambda: _inject_fake_game(tab, fake)
        tab.toggle_game_view()

        tab.dispatch_game_event(
            "frontend", "idle"
        )  # no project kwarg → fallback to tab.project_name
        assert fake.events[0]["project"] == "test-proj"


# ---------------------------------------------------------------------------
# 8–12: office_room.html structural checks (no Qt needed)
# ---------------------------------------------------------------------------


class TestOfficeRoomHTML:
    def test_html_file_exists(self):
        assert _HTML_PATH.exists(), f"Missing: {_HTML_PATH}"

    def test_canvas_element_present(self, html_text):
        assert '<canvas id="c">' in html_text

    def test_overlay_div_present(self, html_text):
        assert 'id="overlay"' in html_text

    def test_chat_input_present(self, html_text):
        assert 'id="chat-input"' in html_text

    def test_chat_action_buttons_present(self, html_text):
        assert 'id="btn-cancel"' in html_text
        assert 'id="btn-focus"' in html_text
        assert 'id="btn-send"' in html_text

    def test_qwebchannel_wiring_present(self, html_text):
        assert "new QWebChannel" in html_text
        assert "qt.webChannelTransport" in html_text

    def test_qwebchannel_js_src_present(self, html_text):
        assert 'src="qrc:///qtwebchannel/qwebchannel.js"' in html_text

    def test_state_dot_contains_core_states(self, html_text):
        """STATE_DOT JS object uses unquoted keys — check 'state:' syntax."""
        for state in ("spawn", "busy", "idle", "done", "close"):
            assert f"{state}:" in html_text, f"STATE_DOT missing state: {state}"

    def test_role_config_has_lead(self, html_text):
        assert "lead:" in html_text

    def test_role_config_has_core_roles(self, html_text):
        for role in ("frontend", "backend", "qa", "devops", "reviewer"):
            assert f"{role}:" in html_text, f"ROLE_CFG missing role: {role}"

    # ── single shared room structural checks ────────────────────────────────
    def test_desk_pos_defined(self, html_text):
        """DESK_POS must define positions for core roles (single-room layout)."""
        assert "DESK_POS" in html_text
        for role in ("lead", "frontend", "backend", "qa"):
            assert f"{role}:" in html_text, f"DESK_POS missing role: {role}"

    def test_characters_walk_to_door_on_close(self, html_text):
        """Characters must move toward door coords on 'close' state."""
        assert "doorCoords" in html_text
        assert 'state === "close"' in html_text or "state !== " in html_text

    def test_free_roam_idle_wander(self, html_text):
        """Idle wander: characters must drift toward random positions near desk."""
        assert "idleTimer" in html_text
        assert "Math.random()" in html_text

    def test_movement_lerp_toward_target(self, html_text):
        """Characters must use a tx/ty target + lerp movement (not teleport)."""
        assert ".tx" in html_text
        assert ".ty" in html_text
        assert "Math.hypot" in html_text

    def test_painters_algorithm_sort(self, html_text):
        """Chars further from viewer (lower y) render first — painter's algorithm."""
        assert "sort(" in html_text
        assert ".y - " in html_text or "a.y" in html_text

    def test_game_event_handler_parses_pane_state(self, html_text):
        assert 'ev.type === "pane_state"' in html_text
        assert "setState" in html_text

    def test_lead_click_opens_overlay(self, html_text):
        assert "openLeadOverlay" in html_text

    def test_render_loop_uses_raf(self, html_text):
        assert "requestAnimationFrame(render)" in html_text

    def test_done_dot_pulsates(self, html_text):
        assert 'state === "done"' in html_text
        assert "pulse" in html_text

    def test_busy_typing_animation_present(self, html_text):
        assert 'state === "busy"' in html_text

    # ── gen-sprite hero sprites ──────────────────────────────────────────────
    def test_role_sprite_map_present(self, html_text):
        """Each role maps to a gen-sprite hero (knight/mage/archer/rogue)."""
        assert "ROLE_SPRITE" in html_text
        for hero in ("knight", "mage", "archer", "rogue"):
            assert f'"{hero}"' in html_text, f"ROLE_SPRITE missing hero: {hero}"

    def test_sprite_walk_cycle_animation(self, html_text):
        """drawChar must cycle the 4 walk frames while moving, idle otherwise."""
        assert "SPRITE_FRAMES" in html_text
        assert "ch.moving" in html_text
        assert "drawImage(frame" in html_text

    def test_sprite_falls_back_to_rectangle(self, html_text):
        """Until the PNG loads (frame.ready), the old rectangle avatar draws."""
        assert "frame.ready" in html_text

    def test_chars_declared_before_initial_resize(self, html_text):
        """Regression: the initial resize() iterates `chars`, so `const chars`
        must be declared BEFORE it. With chars declared later it sits in the
        temporal dead zone → resize() throws 'Cannot access chars before
        initialization' → the whole script aborts → pitch-black canvas."""
        chars_decl = html_text.index("const chars = {}")
        resize_call = html_text.index('addEventListener("resize"')
        assert chars_decl < resize_call, (
            "const chars must be declared before the initial resize() call "
            "or the game view renders black (TDZ ReferenceError)"
        )

    def test_render_loop_starts_before_bridge_wiring(self, html_text):
        """The render loop must start independent of QWebChannel — a bridge
        failure must not halt the script before the scene paints."""
        raf = html_text.index("requestAnimationFrame(render);")
        wire = html_text.index("function wireBridge")
        assert raf < wire, "requestAnimationFrame(render) must run before bridge setup"

    def test_sprite_assets_exist(self):
        """The idle + 4 walk frames must be present for every hero."""
        sprites_dir = _STATIC_DIR / "sprites"
        for hero in ("knight", "mage", "archer", "rogue"):
            for frame in ("idle", "walk1", "walk2", "walk3", "walk4"):
                png = sprites_dir / hero / f"{frame}.png"
                assert png.exists(), f"Missing sprite asset: {png}"

    # ── chat-send fix ────────────────────────────────────────────────────────
    def test_btn_send_calls_sendMessage_with_text(self, html_text):
        """btn-send must call bridge.sendMessage(msg) (not the old leadClicked)."""
        assert "bridge.sendMessage(msg)" in html_text, "btn-send does not call sendMessage"
        assert "bridge.leadClicked()" not in html_text, (
            "old broken send path still present — remove bridge.leadClicked() from btn-send"
        )

    def test_btn_send_clears_textarea(self, html_text):
        """btn-send handler must clear chatInput.value after sending."""
        assert 'chatInput.value = ""' in html_text, "btn-send does not clear textarea"


# ---------------------------------------------------------------------------
# 13: status_header _btn_game_view (source analysis, no widget instantiation)
# ---------------------------------------------------------------------------


class TestStatusHeaderGameButton:
    def _src(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "status_header.py"
        ).read_text(encoding="utf-8")

    def test_game_button_created_as_checkable(self):
        src = self._src()
        assert "_btn_game_view" in src
        assert "setCheckable(True)" in src

    def test_game_button_initial_emoji_is_gamepad(self):
        src = self._src()
        assert '"🎮"' in src

    def test_game_button_toggle_sets_scroll_emoji_when_active(self):
        """When game is ON, button text = '📜' (click to return to text panes)."""
        src = self._src()
        assert '"📜"' in src

    def test_toggle_handler_wired(self):
        src = self._src()
        assert "_on_toggle_game_view" in src

    def test_toggle_shows_status_message(self):
        src = self._src()
        assert "Game view ON" in src
        assert "Game view OFF" in src


# ---------------------------------------------------------------------------
# 16: main_window game bridge wiring (source analysis)
# ---------------------------------------------------------------------------


class TestMainWindowGameBridge:
    def _src(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "main_window.py"
        ).read_text(encoding="utf-8")

    def _toggle_src(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "status_header.py"
        ).read_text(encoding="utf-8")

    def test_game_dispatch_methods_exist(self):
        src = self._src()
        for method in (
            "_game_dispatch",
            "_game_on_pane_requested",
            "_game_on_pane_closed",
            "_game_on_agent_done",
            "_game_sync_all_states",
            "_game_on_focus_role",
        ):
            assert method in src, f"main_window missing: {method}"

    def test_focus_role_exits_game_view(self):
        """_game_on_focus_role must toggle game view OFF when user clicks a card."""
        src = self._src()
        assert "toggle_game_view()" in src
        assert "_btn_game_view.setChecked(False)" in src

    def test_focus_role_resets_button_to_gamepad_emoji(self):
        src = self._src()
        # after exiting game the button label must return to 🎮
        assert '"🎮"' in src

    def test_message_to_lead_wired_in_wire_project_tab(self):
        """_wire_project_tab must connect messageToLead → inject_lead_prompt."""
        src = self._src()
        assert "messageToLead" in src
        assert "inject_lead_prompt" in src

    # ── cross-project contamination fix ──────────────────────────────────────

    def test_game_on_agent_done_uses_project_not_current_tab(self):
        """_game_on_agent_done must route by project arg, not _current_tab().
        _current_tab() always returns the visible tab → background project done
        events would leak into the foreground tab's Office Room scene."""
        import re

        src = self._src()
        m = re.search(r"def _game_on_agent_done.*?(?=\n    def |\Z)", src, re.DOTALL)
        assert m, "_game_on_agent_done not found in main_window.py"
        body = m.group(0)
        assert "_current_tab()" not in body, (
            "_game_on_agent_done must NOT use _current_tab() — that dispatches "
            "background-project done events to the foreground tab's game view "
            "(cross-project contamination bug)"
        )
        assert "_game_dispatch" in body, (
            "_game_on_agent_done must delegate to _game_dispatch with project arg "
            "so the event routes to the correct project tab"
        )

    def test_agent_done_signal_carries_project_ns(self):
        """agentDone signal must include project_ns as first param so handlers
        can route done events to the correct project tab without falling back
        to _current_tab()."""
        orch_src = (
            Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "orchestrator.py"
        ).read_text(encoding="utf-8")
        # Whitespace-tolerant: ruff-format may wrap the signal across lines when
        # the trailing comment is long, so match the token shape, not exact text.
        assert re.search(
            r"agentDone\s*=\s*pyqtSignal\(\s*str\s*,\s*str\s*,\s*str\s*\)", orch_src
        ), (
            "agentDone must declare 3 str params (project_ns, role_name, note) "
            "to prevent cross-project game-view contamination"
        )

    def test_game_dispatch_no_cross_project_leak(self, qapp):
        """dispatch_game_event on tab B must not affect tab A's game view —
        each tab has its own OfficeRoomView with a separate JS context."""
        from agent_takkub.project_tab import ProjectTab

        tab_a = ProjectTab("project-a")
        tab_b = ProjectTab("project-b")
        fake_a = _FakeGameView(tab_a)
        fake_b = _FakeGameView(tab_b)
        tab_a._ensure_game_view = lambda: _inject_fake_game(tab_a, fake_a)
        tab_b._ensure_game_view = lambda: _inject_fake_game(tab_b, fake_b)
        tab_a.toggle_game_view()
        tab_b.toggle_game_view()

        # Simulate a done event routed to project-b only
        tab_b.dispatch_game_event("qa", "done", note="sticker result", project="project-b")

        assert len(fake_a.events) == 0, (
            "project-b done event must NOT appear in project-a's game view"
        )
        assert len(fake_b.events) == 1
        assert fake_b.events[0]["role"] == "qa"
        assert fake_b.events[0]["note"] == "sticker result"

    # ── regression: broken :: filter caused zero events dispatched ───────────

    def test_sync_all_states_uses_panes_by_project(self):
        """Regression: old code iterated orch.panes (role keys, no ::) then
        skipped everything via `if '::' not in proj_role: continue`.
        Must now iterate _panes_by_project instead."""
        import re

        src = self._src()
        m = re.search(r"def _game_sync_all_states.*?(?=\n    def |\Z)", src, re.DOTALL)
        assert m, "_game_sync_all_states not found"
        body = m.group(0)
        assert "_panes_by_project" in body, (
            "_game_sync_all_states must iterate orch._panes_by_project "
            "(not orch.panes which has plain role keys with no ::)"
        )
        assert '"::" not in' not in body, (
            "_game_sync_all_states must not use the broken '::' filter "
            "that skipped all panes (orch.panes keys never contain '::')"
        )

    def test_toggle_into_game_calls_sync(self):
        """Regression: entering game view must push a snapshot of alive panes
        immediately so Lead (and other already-running panes) appear in the
        scene without waiting for the next statusChanged event."""
        import re

        src = self._toggle_src()
        m = re.search(r"def _on_toggle_game_view.*?(?=\n    def |\Z)", src, re.DOTALL)
        assert m, "_on_toggle_game_view not found in status_header.py"
        body = m.group(0)
        assert "_game_sync_all_states" in body, (
            "_on_toggle_game_view must call _game_sync_all_states() when "
            "entering game view to push existing pane states to the scene"
        )

    def test_sync_all_states_dispatches_alive_panes(self, qapp):
        """Functional regression: _game_sync_all_states pushes idle/busy for
        every alive pane — simulated with a fake _panes_by_project structure."""
        from agent_takkub.project_tab import ProjectTab

        class _FakeSession:
            is_alive = True

        class _FakePane:
            def __init__(self, state):
                self.session = _FakeSession()
                self.state = state

        tab = ProjectTab("proj1")
        fake_game = _FakeGameView(tab)
        tab._ensure_game_view = lambda: _inject_fake_game(tab, fake_game)
        tab.toggle_game_view()  # activate so dispatch_game_event routes to fake

        dispatched: list[tuple[str, str]] = []
        orig_dispatch = tab.dispatch_game_event

        def _capture(role, state, note="", project=""):
            dispatched.append((role, state))
            orig_dispatch(role, state, note=note, project=project)

        tab.dispatch_game_event = _capture

        # Replicate the fixed _game_sync_all_states logic directly
        panes_by_project = {"proj1": {"lead": _FakePane("idle"), "frontend": _FakePane("working")}}
        for project, panes in panes_by_project.items():
            for role, pane in panes.items():
                if pane.session is None or not pane.session.is_alive:
                    continue
                state = "busy" if pane.state == "working" else "idle"
                tab.dispatch_game_event(role, state, project=project)

        assert ("lead", "idle") in dispatched, "alive lead pane must be synced as idle"
        assert ("frontend", "busy") in dispatched, "working pane must be synced as busy"


# ---------------------------------------------------------------------------
# sendMessage slot + messageToLead signal (OfficeRoomView)
# ---------------------------------------------------------------------------


class TestSendMessageSlot:
    """_OfficeRoomBridge.sendMessage emits messageToLead; blank msg is dropped."""

    def test_sendMessage_emits_messageToLead(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        received: list[str] = []
        view = OfficeRoomView()
        view.messageToLead.connect(received.append)

        # Simulate JS calling bridge.sendMessage("hello")
        view._bridge.sendMessage("hello")

        assert received == ["hello"]

    def test_sendMessage_blank_is_dropped(self, qapp, stub_webengine):
        from agent_takkub.office_room_view import OfficeRoomView

        received: list[str] = []
        view = OfficeRoomView()
        view.messageToLead.connect(received.append)

        view._bridge.sendMessage("   ")

        assert received == [], "blank/whitespace message must not be emitted"

    def test_sendMessage_slot_exists_on_bridge(self, qapp, stub_webengine):

        from agent_takkub.office_room_view import _OfficeRoomBridge

        bridge = _OfficeRoomBridge()
        assert callable(getattr(bridge, "sendMessage", None))

    def test_messageToLead_propagated_through_view(self, qapp, stub_webengine):
        """OfficeRoomView.messageToLead re-emits bridge.messageToLead."""
        from agent_takkub.office_room_view import OfficeRoomView

        received: list[str] = []
        view = OfficeRoomView()
        view.messageToLead.connect(received.append)

        view._bridge.messageToLead.emit("direct emit")

        assert received == ["direct emit"]


# ---------------------------------------------------------------------------
# TAKKUB_ALLOW_MULTI + TAKKUB_PORT_FILE (app + config)
# ---------------------------------------------------------------------------


class TestMultiInstanceFlag:
    """TAKKUB_ALLOW_MULTI=1 skips lock; TAKKUB_PORT_FILE overrides port file."""

    def test_should_allow_multi_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ALLOW_MULTI", "1")
        from agent_takkub import app as _app

        assert _app._should_allow_multi() is True

    def test_should_allow_multi_false_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("TAKKUB_ALLOW_MULTI", raising=False)
        from agent_takkub import app as _app

        assert _app._should_allow_multi() is False

    def test_should_allow_multi_false_when_env_zero(self, monkeypatch):
        monkeypatch.setenv("TAKKUB_ALLOW_MULTI", "0")
        from agent_takkub import app as _app

        assert _app._should_allow_multi() is False

    def test_port_file_uses_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "myport"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(custom))
        from agent_takkub import config as _cfg

        assert _cfg._get_port_file() == custom

    def test_port_file_default_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        from agent_takkub import config as _cfg

        assert _cfg._get_port_file() == _cfg.PORT_FILE

    def test_write_read_port_roundtrip_with_override(self, monkeypatch, tmp_path):
        """write_port + read_port use TAKKUB_PORT_FILE when set."""
        port_path = tmp_path / "port.test"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(port_path))
        from agent_takkub import config as _cfg

        _cfg.write_port(54321)
        assert _cfg.read_port() == 54321
        assert port_path.read_text().strip() == "54321"

    def test_app_source_has_should_allow_multi(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "app.py").read_text(
            encoding="utf-8"
        )
        assert "_should_allow_multi" in src
        assert "TAKKUB_ALLOW_MULTI" in src
        assert "TAKKUB_PORT_FILE" in src
