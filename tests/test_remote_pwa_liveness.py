"""Structural checks for #252: the PWA's connectivity chip must not lie.

A Cloudflare/tunnel edge failure (530, 502, 503, ...) is intercepted before
it ever reaches the cockpit and answers with its own error page — never one
of the cockpit's own JSON responses. `apiFetch` must treat any 5xx as
"can't reach cockpit" (Offline), not read `fetch()` merely resolving as
proof the cockpit answered. No JS runtime in this repo's test suite — these
assert the pieces exist and are wired the way the rest of `test_remote_pwa_*`
does (source-level regex/structural checks against `app.js`).
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "remote" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


class TestApiFetchEdgeDetection:
    def test_5xx_treated_as_unreachable_before_setoffline_false(self):
        js = _read("app.js")
        m = re.search(r"function apiFetch\(", js)
        assert m, "apiFetch not found"
        body_start = m.start()
        # Grab a generous slice of the function body to check ordering.
        chunk = js[body_start : body_start + 1200]
        status_check = chunk.find("res.status >= 500")
        offline_true = chunk.find("setOffline(true)")
        offline_false = chunk.find("setOffline(false)")
        unreachable_throw = chunk.find('new Error("cockpit_unreachable")')
        assert status_check != -1, "apiFetch must guard on res.status >= 500"
        assert offline_true != -1 and unreachable_throw != -1
        assert offline_false != -1
        # The 5xx guard (and its setOffline(true)) must run *before* the
        # unconditional setOffline(false) that used to fire on every resolve.
        assert status_check < offline_false
        assert offline_true < offline_false
        assert unreachable_throw < offline_false

    def test_404_and_403_paths_untouched(self):
        # Auth semantics (#252 must not regress): bare 404 while holding a
        # token still means "token invalid" -> forgetToken/pairing screen;
        # 403 password_required still opens the password prompt.
        js = _read("app.js")
        assert "res.status === 404 && hadToken" in js
        assert "forgetToken()" in js
        assert "res.status === 403" in js
        assert "password_required" in js
        assert "showPasswordPrompt()" in js


class TestConnErrorHelper:
    def test_conn_error_message_and_helper_defined(self):
        js = _read("app.js")
        assert "function isConnError(err)" in js
        assert "var CONN_ERROR_MSG" in js
        assert "err instanceof TypeError" in js
        assert '"cockpit_unreachable"' in js

    def test_message_mentions_tunnel_and_cockpit_off(self):
        js = _read("app.js")
        m = re.search(r'var CONN_ERROR_MSG = "([^"]+)"', js)
        assert m, "CONN_ERROR_MSG not found"
        msg = m.group(1)
        assert "cockpit" in msg
        assert "tunnel" in msg


class TestCallSitesUseConnError:
    """Every user-facing apiFetch call site must distinguish an edge/tunnel
    failure from a generic app-level error, per #252 requirement 2."""

    def test_load_projects_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function loadProjects()")
        assert idx != -1
        chunk = js[idx : idx + 600]
        assert "isConnError(err)" in chunk
        assert "CONN_ERROR_MSG" in chunk

    def test_verify_password_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find('"password-form"')
        assert idx != -1
        chunk = js[idx : idx + 1500]
        assert "isConnError(err)" in chunk
        assert "CONN_ERROR_MSG" in chunk

    def test_open_project_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function openProject(")
        assert idx != -1
        chunk = js[idx : idx + 2200]
        assert "isConnError(err)" in chunk

    def test_close_project_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function closeProject(")
        assert idx != -1
        chunk = js[idx : idx + 1800]
        assert "isConnError(err)" in chunk

    def test_send_lead_message_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function sendLeadMessage(")
        assert idx != -1
        chunk = js[idx : idx + 1800]
        assert "isConnError(err)" in chunk

    def test_send_lead_image_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function sendLeadImage(")
        assert idx != -1
        chunk = js[idx : idx + 2200]
        assert "isConnError(err)" in chunk

    def test_confirm_resume_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function confirmResume(")
        assert idx != -1
        chunk = js[idx : idx + 3000]
        assert "isConnError(err)" in chunk


class TestSseRetryHasBackoff:
    """#252 requirement 3: no silent rapid retry spam against a dead edge."""

    def test_es_retry_uses_exponential_backoff_with_cap(self):
        js = _read("app.js")
        assert "function scheduleEsRetry(" in js
        idx = js.find("function scheduleEsRetry(")
        chunk = js[idx : idx + 1200]
        assert "Math.pow(2, lead.esRetries)" in chunk
        assert "Math.min(" in chunk
