"""Tests for the render-path coalescing buffer (issue #35).

Verifies that:
 1. Multiple bytesIn chunks are coalesced into a single write_bytes call
    when the timer fires.
 2. The buffer is force-flushed immediately when it exceeds _RENDER_FLUSH_CAP.
 3. detach_session flushes any remaining bytes so no output is silently dropped.
 4. _tp_total_bytes increments correctly across chunks (throughput watchdog data).

No PyQt6 event loop required: we instantiate AgentPane via __new__, wire up
stubs for QTimer and the terminal widget, then drive _coalesce_bytes directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_pane():
    """Build a minimal AgentPane without the Qt event loop."""
    from agent_takkub.agent_pane import AgentPane
    from agent_takkub.agent_pane_model import AgentPaneModel
    from agent_takkub.roles import by_name

    pane = AgentPane.__new__(AgentPane)

    # session/_tp_total_bytes/... live on self.model (issue #105 Phase A).
    pane.model = AgentPaneModel(by_name("backend"))

    # Minimal state that __init__ would normally set.
    pane._render_buf = bytearray()
    pane._tp_total_bytes = 0

    # Real QTimer is not available; use a simple fake that records calls.
    timer = MagicMock()
    timer.isActive.return_value = False
    pane._render_timer = timer

    # The terminal widget — we only care about write_bytes calls.
    terminal = MagicMock()
    pane._terminal = terminal

    return pane, terminal, timer


class TestCoalescing:
    def test_single_chunk_schedules_timer(self) -> None:
        pane, terminal, timer = _make_pane()
        pane._coalesce_bytes(b"hello")
        # Should NOT have written to terminal yet (timer hasn't fired).
        terminal.write_bytes.assert_not_called()
        # Timer should be started.
        timer.start.assert_called_once()
        # Buffer holds the data.
        assert pane._render_buf == bytearray(b"hello")

    def test_multiple_chunks_coalesced_on_flush(self) -> None:
        pane, terminal, _timer = _make_pane()
        pane._coalesce_bytes(b"aaa")
        pane._coalesce_bytes(b"bbb")
        pane._coalesce_bytes(b"ccc")
        # Still no terminal write — timer not fired yet.
        terminal.write_bytes.assert_not_called()
        # Simulate timer firing.
        pane._flush_render_buf()
        # All three chunks merged into one write_bytes call.
        terminal.write_bytes.assert_called_once_with(b"aaabbbccc")

    def test_flush_clears_buffer(self) -> None:
        pane, _terminal, _timer = _make_pane()
        pane._coalesce_bytes(b"data")
        pane._flush_render_buf()
        assert pane._render_buf == bytearray()

    def test_flush_empty_buffer_is_noop(self) -> None:
        pane, terminal, _timer = _make_pane()
        pane._flush_render_buf()
        terminal.write_bytes.assert_not_called()

    def test_cap_triggers_immediate_flush(self) -> None:
        from agent_takkub.agent_pane import AgentPane

        cap = AgentPane._RENDER_FLUSH_CAP
        pane, terminal, _timer = _make_pane()
        # Send exactly cap bytes — should force-flush without waiting for timer.
        big = b"x" * cap
        pane._coalesce_bytes(big)
        terminal.write_bytes.assert_called_once_with(big)
        assert pane._render_buf == bytearray()

    def test_cap_minus_one_does_not_flush(self) -> None:
        from agent_takkub.agent_pane import AgentPane

        cap = AgentPane._RENDER_FLUSH_CAP
        pane, terminal, _timer = _make_pane()
        pane._coalesce_bytes(b"x" * (cap - 1))
        terminal.write_bytes.assert_not_called()

    def test_tp_total_bytes_accumulates(self) -> None:
        pane, _term, _timer = _make_pane()
        pane._coalesce_bytes(b"abc")
        pane._coalesce_bytes(b"de")
        assert pane._tp_total_bytes == 5

    def test_tp_total_bytes_on_force_flush(self) -> None:
        from agent_takkub.agent_pane import AgentPane

        cap = AgentPane._RENDER_FLUSH_CAP
        pane, _term, _timer = _make_pane()
        pane._coalesce_bytes(b"x" * cap)
        assert pane._tp_total_bytes == cap

    def test_timer_not_restarted_when_already_active(self) -> None:
        pane, _terminal, timer = _make_pane()
        timer.isActive.return_value = True
        pane._coalesce_bytes(b"first")
        pane._coalesce_bytes(b"second")
        # timer.start should NOT be called a second time (already running).
        timer.start.assert_not_called()
        # But both chunks should be buffered.
        assert pane._render_buf == bytearray(b"firstsecond")


class TestDetachFlushes:
    def test_detach_flushes_pending_bytes(self) -> None:
        """detach_session must flush the buffer so final output isn't dropped."""
        pane, _terminal, timer = _make_pane()
        # Wire a minimal session so detach_session doesn't crash on None.
        sess = MagicMock()
        pane.session = sess
        pane._last_idle = None

        # Also stub other state detach_session touches.
        pane._token_timer = MagicMock()
        pane._session_jsonl = None
        pane._last_usage = None
        pane._token_label = MagicMock()
        pane._terminal = MagicMock()
        # detach_session reads self._exit_conn (added when processExited was
        # wired into teardown). On a __new__-built pane a *missing* attr read
        # raises RuntimeError ("super-class __init__ never called") rather than
        # AttributeError, so getattr(..., None) can't swallow it — seed it here.
        pane._exit_conn = None

        # Buffer some bytes — timer hasn't fired.
        pane._render_buf = bytearray(b"pending output")
        pane._render_timer = timer

        from agent_takkub.agent_pane import AgentPane

        AgentPane.detach_session(pane)

        # detach must have flushed by calling write_bytes.
        pane._terminal.write_bytes.assert_called_once_with(b"pending output")
        # Timer stopped.
        timer.stop.assert_called()
