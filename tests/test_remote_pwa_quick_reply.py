"""Structural checks for the W2a quick-reply chips + AskUserQuestion picker
fallback banner in the Takkub Remote PWA (`static/index.html` + `app.js`).
No JS runtime in this repo's test suite — these assert the pieces exist and
are wired the way the notify.py side expects (SSE event name, DOM ids), same
spirit as other static-asset structural tests in this project.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_has_picker_banner_and_quick_replies_containers(self):
        html = _read("index.html")
        assert '<div id="lead-picker-banner"></div>' in html
        assert '<div id="quick-replies"></div>' in html

    def test_containers_sit_above_the_composer(self):
        html = _read("index.html")
        banner_idx = html.index('id="lead-picker-banner"')
        chips_idx = html.index('id="quick-replies"')
        composer_idx = html.index('id="lead-composer"')
        assert banner_idx < composer_idx
        assert chips_idx < composer_idx


class TestAppJsWiring:
    def test_listens_for_blocked_on_picker_sse_event(self):
        js = _read("app.js")
        assert 'addEventListener("blocked_on_picker"' in js

    def test_standard_quick_replies_defined(self):
        js = _read("app.js")
        assert "ok ลุยเลย" in js
        assert "ไม่เอา หยุดก่อน" in js
        assert "ขอดูแผนก่อน" in js

    def test_numbered_option_detection_present(self):
        js = _read("app.js")
        assert "function detectNumberedOptions" in js

    def test_send_lead_message_used_by_both_composer_and_chips(self):
        js = _read("app.js")
        assert "function sendLeadMessage" in js
        # chips call it directly
        assert "sendLeadMessage(n)" in js
        assert "sendLeadMessage(label)" in js

    def test_banner_never_forwards_full_options_payload(self):
        # data-min: the client only ever renders whatever text the server
        # sent (already stripped of options server-side) — this just checks
        # the client doesn't independently render an `options`/`label` field.
        js = _read("app.js")
        assert "payload.options" not in js
        assert "question.options" not in js

    def test_hide_picker_banner_wired_into_new_lead_text_paths(self):
        js = _read("app.js")
        assert "function hidePickerBanner" in js
        assert "function showPickerBanner" in js
