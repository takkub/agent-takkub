"""Structural checks for the #198 fix — remote mobile chat must not force
scroll-to-bottom on every new event while the user is reading scrollback.
No JS runtime in this repo's test suite — these assert the shared pinned-
to-bottom heuristic exists and is actually used by every path that appends
into #lead-log, and that the "new messages" affordance is wired up, same
spirit as other static-asset structural tests in this project
(test_remote_pwa_quick_reply.py).
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestSharedScrollHelpers:
    def test_pinned_heuristic_and_scroll_helpers_defined(self):
        js = _read("app.js")
        assert "function isPinnedToBottom(log)" in js
        assert "function scrollToBottom(log)" in js
        assert "function showNewMessagesButton" in js
        assert "function hideNewMessagesButton" in js

    def test_threshold_widened_for_mobile_momentum_scroll(self):
        js = _read("app.js")
        # Was a bare "- 24" inline in two call sites; now a single named
        # constant used by isPinnedToBottom, bumped to tolerate iOS/Android
        # momentum-scroll overshoot near the bottom.
        assert "var SCROLL_PIN_PX = 48;" in js
        assert "log.scrollTop + log.clientHeight >= log.scrollHeight - 24" not in js

    def test_no_raw_unconditional_scrollTop_assignment_outside_helper(self):
        js = _read("app.js")
        # Every remaining `log.scrollTop = log.scrollHeight` must live inside
        # scrollToBottom() itself, or the restore-exact-offset branch in
        # renderSelectedProject (Math.min(savedScrollTop, ...)) — not
        # scattered as a bare unconditional assignment at a call site (that
        # scattering is exactly what caused #198: showThinking() overrode
        # the streaming atBottom heuristic on every 'working' SSE event).
        occurrences = js.count("log.scrollTop = log.scrollHeight;")
        assert occurrences == 1  # only inside scrollToBottom()
        helper_idx = js.index("function scrollToBottom(log) {")
        assignment_idx = js.index("log.scrollTop = log.scrollHeight;")
        next_fn_idx = js.index("function showNewMessagesButton")
        assert helper_idx < assignment_idx < next_fn_idx


class TestShowThinkingRespectsPin:
    def test_show_thinking_no_longer_force_scrolls(self):
        js = _read("app.js")
        thinking_start = js.index("function showThinking(category)")
        thinking_end = js.index("function hideThinking()")
        body = js[thinking_start:thinking_end]
        assert "if (pinned) scrollToBottom(log); else showNewMessagesButton();" in body
        assert "log.scrollTop = log.scrollHeight;" not in body

    def test_append_msg_dom_accepts_skip_scroll_for_bulk_rebuild(self):
        js = _read("app.js")
        assert "function appendMsgDom(kind, text, skipScroll)" in js
        assert "if (!skipScroll) {" in js


class TestRenderSelectedProjectForceScroll:
    def test_takes_force_scroll_param(self):
        js = _read("app.js")
        assert "function renderSelectedProject(forceScroll)" in js

    def test_switch_view_and_select_project_force_scroll(self):
        js = _read("app.js")
        # Both represent "just opened this conversation" — clientHeight can
        # read 0 for the view-switch case (view not yet active), so these
        # must not rely on the measured-heuristic branch.
        assert "renderSelectedProject(true);" in js

    def test_reconnect_does_not_force_scroll(self):
        js = _read("app.js")
        onopen_idx = js.index("es.onopen = function () {")
        onopen_end = js.index("};", onopen_idx)
        body = js[onopen_idx:onopen_end]
        assert "renderSelectedProject();" in body
        assert "renderSelectedProject(true)" not in body

    def test_background_refresh_does_not_force_scroll_but_explicit_refresh_does(self):
        js = _read("app.js")
        assert "function loadHistory(project, force, jumpToBottom)" in js
        assert "renderSelectedProject(!force || jumpToBottom)" in js
        assert "return loadHistory(project, true, announce)" in js


class TestNewMessagesButton:
    def test_markup_present_inside_lead_log(self):
        html = _read("index.html")
        log_idx = html.index('<div id="lead-log">')
        empty_idx = html.index('id="lead-empty"')
        wrap_idx = html.index('id="new-msg-wrap"')
        btn_idx = html.index('id="new-msg-btn"')
        assert log_idx < empty_idx < wrap_idx < btn_idx

    def test_click_handler_scrolls_to_bottom(self):
        js = _read("app.js")
        assert '$("new-msg-btn").addEventListener("click"' in js

    def test_manual_scroll_to_bottom_hides_button(self):
        js = _read("app.js")
        assert '$("lead-log").addEventListener("scroll"' in js
        assert "if (isPinnedToBottom(this)) hideNewMessagesButton();" in js

    def test_css_uses_sticky_not_absolute_so_it_tracks_scrollport(self):
        html = _read("index.html")
        wrap_css_idx = html.index("#new-msg-wrap {")
        wrap_css_end = html.index("}", wrap_css_idx)
        assert "position: sticky;" in html[wrap_css_idx:wrap_css_end]
