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
        assert 'id="lead-refresh-btn"' in html
        assert 'id="resume-sheet"' in html
        assert 'id="resume-sheet-list"' in html
        assert 'id="resume-sheet-close"' in html
        assert 'id="resume-sheet-provider"' in html
        assert 'id="lead-attach"' in html
        assert 'id="lead-image-input"' in html

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
        assert "function updateLeadActionVisibility" in js
        m = re.search(r"function updateLeadActionVisibility\(\)\s*\{(.*?)\n  \}", js, re.DOTALL)
        assert m is not None
        body = m.group(1)
        assert 'state.view === "lead"' in body
        assert 'state.mode === "control"' in body

    def test_refresh_button_is_available_in_lead_view_in_both_modes(self):
        js = _read("app.js")
        chunk = js.split("function updateLeadActionVisibility()")[1].split(
            "function resumeTimeLabel"
        )[0]
        assert 'refreshBtn.classList.toggle("show", inLead)' in chunk
        assert 'refreshBtn.classList.toggle("show", inLead && state.mode' not in chunk

    def test_refresh_forces_history_without_restarting_stream(self):
        js = _read("app.js")
        chunk = js.split("function refreshProjectHistory(project, announce)")[1].split(
            "function refreshOpenProjectHistories"
        )[0]
        assert "loadHistory(project, true)" in chunk
        assert "stopLeadStream" not in chunk

        load_chunk = js.split("function loadHistory(project, force)")[1].split(
            "function refreshProjectHistory"
        )[0]
        assert 'apiFetch(path, { cache: "no-store" })' in load_chunk

    def test_return_from_mobile_background_resyncs_warm_projects(self):
        js = _read("app.js")
        chunk = js.split('document.addEventListener("visibilitychange"')[1]
        assert "state.backgroundedAt" in chunk[:700]
        assert ">= 30000" in chunk[:700]
        assert "refreshOpenProjectHistories()" in chunk[:700]

    def test_history_cache_is_not_persisted_across_devices(self):
        js = _read("app.js")
        state_chunk = js.split("var state = {")[1].split("};", 1)[0]
        assert "leadByProject: {}" in state_chunk
        assert "localStorage.getItem" not in state_chunk.split("leadByProject", 1)[1]

    def test_embedded_raster_images_are_safe_and_self_contained(self):
        js = _read("app.js")
        chunk = js.split("function mdInline(raw)")[1].split("function renderMarkdown")[0]
        assert 'class="remote-image"' in chunk
        assert "data:image\\/(?:png|jpeg|webp|gif);base64" in chunk
        assert "svg" not in chunk
        assert "https?:" not in chunk.split("remote-image", 1)[0]
        html = _read("index.html")
        assert ".remote-image" in html

    def test_mobile_image_picker_uploads_to_project_scoped_endpoint(self):
        js = _read("app.js")
        html = _read("index.html")
        chunk = js.split("function sendLeadImage(file)")[1].split("function setLeadEmptyText")[0]
        assert 'apiFetch("api/lead/upload"' in chunk
        assert "MAX_IMAGE_BYTES" in chunk
        assert "project: project" in chunk
        assert "reader.readAsDataURL(file)" in chunk
        assert 'accept="image/png,image/jpeg,image/webp,image/gif"' in html

    def test_mobile_image_picker_is_control_mode_only(self):
        js = _read("app.js")
        control_chunk = js.split("function updateControlNote()")[1].split(
            "function updateLeadActionVisibility"
        )[0]
        assert '$("lead-attach").disabled = !isControl' in control_chunk
        assert 'if (state.mode === "control") $("lead-image-input").click()' in js

    def test_confirms_before_resuming(self):
        js = _read("app.js")
        assert "function confirmResume" in js
        assert "window.confirm(" in js.split("function confirmResume")[1][:400]

    def test_reconnects_only_resumed_project_after_success(self):
        js = _read("app.js")
        chunk = js.split("function confirmResume")[1]
        assert "stopLeadStream(project, true)" in chunk[:2200]
        assert "startLeadStream(project)" in chunk[:2200]

    def test_resume_response_seeds_history_before_reconnect(self):
        js = _read("app.js")
        chunk = js.split("function confirmResume")[1]
        assert "Array.isArray(res.data.messages)" in chunk[:2600]
        assert "resumedLead.messages.push" in chunk[:2600]
        assert "resumedLead.historyLoaded" in chunk[:2600]

    def test_resume_request_is_project_scoped_and_debounced(self):
        js = _read("app.js")
        chunk = js.split("function confirmResume")[1]
        assert "state.resumePending" in chunk[:700]
        assert "project: project" in chunk[:1000]

    def test_resume_button_wired_to_open_sheet(self):
        js = _read("app.js")
        assert '$("lead-resume-btn").addEventListener("click", openResumeSheet)' in js

    def test_resume_button_has_discoverable_text_label(self):
        html = _read("index.html")
        line = next(line for line in html.splitlines() if 'id="lead-resume-btn"' in line)
        assert "Resume" in line

    def test_close_button_wired(self):
        js = _read("app.js")
        assert '$("resume-sheet-close").addEventListener("click", closeResumeSheet)' in js


class TestServiceWorkerCacheBump:
    def test_cache_version_is_a_v_number(self):
        js = _read("sw.js")
        m = re.search(r'CACHE_NAME = "takkub-remote-shell-v(\d+)"', js)
        assert m is not None


class TestConcurrentProjectStreams:
    def test_project_switch_is_cache_render_without_stream_restart(self):
        js = _read("app.js")
        chunk = js.split("function selectProject")[1].split("function renderProjects")[0]
        assert "renderSelectedProject()" in chunk
        assert "syncProjectStreams()" in chunk
        assert "stopLeadStream()" not in chunk
        assert "loadHistory(" not in chunk

    def test_each_project_owns_transport_and_history_state(self):
        js = _read("app.js")
        assert "leadByProject: {}" in js
        assert "function projectLeadState(project)" in js
        assert "function syncProjectStreams()" in js
        assert "function startProjectStream(project)" in js
        assert "body: JSON.stringify({ project: project })" in js

    def test_stopping_transport_invalidates_pending_ticket(self):
        js = _read("app.js")
        chunk = js.split("function stopProjectTransport(project)")[1].split(
            "function stopLeadStream"
        )[0]
        assert "lead.transportGeneration += 1" in chunk

    def test_reconnect_preserves_in_progress_turn(self):
        js = _read("app.js")
        chunk = js.split("es.onopen = function ()")[1].split("es.addEventListener")[0]
        assert "lead.working = false" not in chunk

    def test_reconnect_loop_never_gives_up_after_fixed_retry_count(self):
        js = _read("app.js")
        chunk = js.split("function scheduleEsRetry(project)")[1].split(
            "function stopProjectTransport"
        )[0]
        assert "ES_RETRY_WARNING_THRESHOLD" in chunk
        assert "setTimeout(function ()" in chunk
        assert "if (lead.esRetries >" not in chunk
        assert "Math.min(1000 * Math.pow(2, lead.esRetries), 15000)" in chunk

    def test_send_is_scoped_to_the_visible_project(self):
        js = _read("app.js")
        chunk = js.split("function sendLeadMessage(text)")[1].split("function setLeadEmptyText")[0]
        assert "var project = visibleProject()" in chunk
        assert "sayBody = { text: text, project: project }" in chunk

    def test_background_events_are_stamped_to_captured_project(self):
        js = _read("app.js")
        chunk = js.split("function connectSse(ticket, project)")[1]
        assert "appendLeadLive(parseSseData(evt.data, project), project)" in chunk[:3500]
        assert 'appendProjectMessage(project, "done"' in chunk[:3500]

    def test_desktop_user_turns_stream_to_mobile_without_refetch(self):
        js = _read("app.js")
        chunk = js.split('es.addEventListener("user"')[1].split('es.addEventListener("done"')[0]
        assert 'appendProjectMessage(project, "me", text)' in chunk
        assert "payload.remote" in chunk

    def test_working_state_has_server_reconciliation_and_optimistic_timeout(self):
        js = _read("app.js")
        assert "workingConfirmed: false" in js
        assert "function beginOptimisticWorking(project)" in js
        assert "}, 30000);" in js
        history = js.split("function loadHistory(project, force)")[1].split("function connectSse")[
            0
        ]
        assert "!!(data && data.working)" in history
        assert "!lead.optimisticWorkingTimer" in history
        assert "setProjectWorking(project, false, null, true)" in js

    def test_desktop_session_change_reloads_only_that_project_history(self):
        js = _read("app.js")
        chunk = js.split('es.addEventListener("session_changed"')[1].split(
            'es.addEventListener("blocked_on_picker"'
        )[0]
        assert "lead.historyGeneration += 1" in chunk
        assert "lead.historyLoaded = false" in chunk
        assert "loadHistory(project)" in chunk
        assert "stopLeadStream" not in chunk

    def test_all_builtin_provider_labels_are_native(self):
        js = _read("app.js")
        for provider in ("claude", "openai", "gemini", "opencode", "kimi", "cursor"):
            assert f"{provider}: {{" in js


class TestMobileViewportLayout:
    def test_app_root_is_pinned_so_ios_cannot_scroll_header_offscreen(self):
        html = _read("index.html")
        body_css = html.split("\n  body {")[1].split("}", 1)[0]
        app_css = html.split("\n  #app {")[1].split("}", 1)[0]
        header_css = html.split("\n  header {")[1].split("}", 1)[0]

        assert "position: fixed" in body_css
        assert "inset: 0" in body_css
        assert "position: fixed" in app_css
        assert "height: 100dvh" in app_css
        assert "overflow: hidden" in app_css
        assert "z-index: 20" in header_css
        assert "min-height: 50px" in header_css
