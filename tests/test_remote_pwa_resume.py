"""Structural checks for the W3 Resume button + session picker sheet in the
Takkub Remote PWA (`static/index.html` + `app.js` + `sw.js`). No JS runtime
in this repo's test suite — these assert the pieces exist and are wired the
way `api.lead_sessions`/`api.resume_lead` expect (endpoint paths, DOM ids),
same spirit as `test_remote_pwa_quick_reply.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestIndexHtmlMarkup:
    def test_has_resume_button_and_sheet_containers(self):
        html = _read("index.html")
        assert 'id="lead-resume-btn"' in html
        assert 'id="resume-sheet"' in html
        assert 'id="resume-sheet-list"' in html
        assert 'id="resume-sheet-close"' in html

    def test_resume_button_hidden_by_default(self):
        # `.show` is added by JS only in the Lead view + control mode —
        # never rendered visible by default markup.
        html = _read("index.html")
        btn_line = next(line for line in html.splitlines() if 'id="lead-resume-btn"' in line)
        assert 'class="show"' not in btn_line

    def test_sheet_hidden_by_default(self):
        html = _read("index.html")
        sheet_line = next(line for line in html.splitlines() if 'id="resume-sheet"' in line)
        assert "show" not in sheet_line


class TestAppJsWiring:
    def test_fetches_session_list_endpoint(self):
        js = _read("app.js")
        assert "api/lead/sessions" in js

    def test_posts_resume_endpoint(self):
        js = _read("app.js")
        assert "api/lead/resume" in js

    def test_session_uuid_sent_in_resume_body(self):
        js = _read("app.js")
        assert "session_uuid" in js

    def test_resume_button_toggles_only_in_lead_view_control_mode(self):
        js = _read("app.js")
        assert "function updateResumeButtonVisibility" in js
        m = re.search(r"function updateResumeButtonVisibility\(\)\s*\{(.*?)\n  \}", js, re.DOTALL)
        assert m is not None
        body = m.group(1)
        assert 'state.view === "lead"' in body
        assert 'state.mode === "control"' in body

    def test_confirms_before_resuming(self):
        js = _read("app.js")
        assert "function confirmResume" in js
        assert "window.confirm(" in js.split("function confirmResume")[1][:400]

    def test_reconnects_stream_after_successful_resume(self):
        js = _read("app.js")
        chunk = js.split("function confirmResume")[1]
        assert "stopLeadStream()" in chunk[:1500]
        assert "startLeadStream()" in chunk[:1500]

    def test_resume_button_wired_to_open_sheet(self):
        js = _read("app.js")
        assert '$("lead-resume-btn").addEventListener("click", openResumeSheet)' in js

    def test_close_button_wired(self):
        js = _read("app.js")
        assert '$("resume-sheet-close").addEventListener("click", closeResumeSheet)' in js


class TestServiceWorkerCacheBump:
    def test_cache_version_is_a_v_number(self):
        js = _read("sw.js")
        m = re.search(r'CACHE_NAME = "takkub-remote-shell-v(\d+)"', js)
        assert m is not None
