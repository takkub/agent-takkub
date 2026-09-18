"""Structural checks for the AskUserQuestion picker banner in the Takkub
Remote PWA (`static/index.html` + `app.js`). No JS runtime in this repo's
test suite — these assert the pieces exist and are wired the way the
notify.py side expects (SSE event name, DOM ids), same spirit as other
static-asset structural tests in this project.

#660: the always-on quick-reply row ("ok ลุยเลย" / numbered-option guesses)
was removed — it was never used and hid a picker that had no options. This
file also guards that it stays gone (handler, markup, CSS).
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_has_picker_banner_container_above_the_composer(self):
        html = _read("index.html")
        assert '<div id="lead-picker-banner"></div>' in html
        assert html.index('id="lead-picker-banner"') < html.index('id="lead-composer"')

    def test_quick_reply_row_is_gone_markup_and_css(self):
        html = _read("index.html")
        assert 'id="quick-replies"' not in html
        assert "#quick-replies" not in html
        assert ".qr-num" not in html
        # the chip style itself stays — the picker banner renders .qr-chip
        assert ".qr-chip {" in html


class TestAppJsWiring:
    def test_listens_for_blocked_on_picker_sse_event(self):
        js = _read("app.js")
        assert 'addEventListener("blocked_on_picker"' in js

    def test_standard_quick_replies_and_numbered_guesses_removed(self):
        js = _read("app.js")
        for gone in (
            "ok ลุยเลย",
            "ไม่เอา หยุดก่อน",
            "ขอดูแผนก่อน",
            "STANDARD_QUICK_REPLIES",
            "function detectNumberedOptions",
            "function renderQuickReplies",
            "lastLeadRawAccum",
            '$("quick-replies")',
        ):
            assert gone not in js, gone

    def test_picker_renders_options_as_tappable_chips_safely(self):
        js = _read("app.js")
        assert "Array.isArray(payload.questions)" in js  # guards a malformed payload
        assert "btn.textContent = label" in js  # chip labels never go through innerHTML

    def test_multi_question_picker_answers_via_dedicated_endpoint(self):
        # typed label text + Enter is silently discarded by the real terminal
        # picker (proven live, docs/audit/2026-08-20-remote-askuserquestion.md)
        # — chips must submit through the key-injection endpoint.
        js = _read("app.js")
        assert "function submitPickerAnswers" in js
        assert "api/lead/answer-picker" in js
        # every question must have >= 1 staged selection before submit
        assert "function allAnswered" in js
        assert "selections.every(function (s) { return s.length > 0; })" in js

    def test_multi_select_question_toggles_chips_instead_of_desktop_fallback(self):
        # #660: multiSelect is answerable from the phone now (digits toggle,
        # Right advances, Enter confirms — proven live 2026-09-18).
        js = _read("app.js")
        assert "(เลือกได้หลายข้อ)" in js
        assert 'chip.classList.toggle("picked", pos === -1)' in js
        assert "key sequence not proven safe yet" not in js

    def test_hide_picker_banner_wired_into_new_lead_text_paths(self):
        js = _read("app.js")
        assert "function hidePickerBanner" in js
        assert "function showPickerBanner" in js
