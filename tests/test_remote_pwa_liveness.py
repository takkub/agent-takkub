"""Structural checks for #252/#254: the PWA's connectivity chip must not lie
in EITHER direction.

A Cloudflare/tunnel edge failure (530, 502, 503, ...) is intercepted before
it ever reaches the cockpit and answers with its own error page (5xx,
text/plain or text/html body) — never one of the cockpit's own JSON
responses. `apiFetch` must treat that as "can't reach cockpit" (Offline).

But the cockpit itself CAN legitimately answer 5xx: when the Qt main thread
doesn't reply within `_BRIDGE_TIMEOUT_SEC`, `_respond_marshaled` in
`remote/http_server.py` (lines 602-611) sends its own 504
`{"ok": false, "msg": "orchestrator did not respond"}` as
`application/json`. That means "reached cockpit, it's slow/stuck" — the
chip must stay Online, not flip to the tunnel-down message.

No JS runtime in this repo's test suite — these assert the pieces exist and
are wired the way the rest of `test_remote_pwa_*` does (source-level
regex/structural checks against `app.js`).
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
        chunk = js[body_start : body_start + 2200]
        status_check = chunk.find("res.status >= 500")
        offline_true = chunk.find("setOffline(true)")
        unreachable_throw = chunk.find('new Error("cockpit_unreachable")')
        # The unconditional setOffline(false) that gates the rest of the
        # non-5xx response handling — must be the one AFTER the
        # cockpit_unreachable throw (there's now an earlier, branch-local
        # setOffline(false) inside the JSON-content-type 5xx path too, see
        # test_5xx_with_our_json_content_type_clears_offline_before_throw).
        offline_false = chunk.find("setOffline(false)", unreachable_throw)
        assert status_check != -1, "apiFetch must guard on res.status >= 500"
        assert offline_true != -1 and unreachable_throw != -1
        assert offline_false != -1
        # The 5xx guard (and its setOffline(true)) must run *before* the
        # unconditional setOffline(false) that used to fire on every resolve.
        assert status_check < offline_false
        assert offline_true < offline_false
        assert unreachable_throw < offline_false

    def test_5xx_with_our_json_content_type_is_not_unreachable(self):
        """#254: a 5xx whose Content-Type is application/json is the
        cockpit's own bridge-timeout response (see module docstring) — must
        NOT setOffline(true) and must NOT throw cockpit_unreachable. It gets
        its own distinct error so the chip stays Online."""
        js = _read("app.js")
        m = re.search(r"function apiFetch\(", js)
        assert m, "apiFetch not found"
        body_start = m.start()
        chunk = js[body_start : body_start + 2200]
        status_check = chunk.find("res.status >= 500")
        ct_check = chunk.find('indexOf("application/json")')
        unresponsive_throw = chunk.find('new Error("cockpit_unresponsive")')
        offline_true = chunk.find("setOffline(true)")
        unreachable_throw = chunk.find('new Error("cockpit_unreachable")')
        assert status_check != -1
        assert ct_check != -1, "apiFetch must branch on Content-Type before deciding Offline"
        assert unresponsive_throw != -1
        # The JSON-content-type branch (cockpit_unresponsive) must be
        # decided, and its throw must sit, BEFORE the unconditional
        # setOffline(true)/cockpit_unreachable edge-failure path — otherwise
        # a cockpit-answered 504 would still flip the chip Offline.
        assert status_check < ct_check < unresponsive_throw < offline_true
        assert unresponsive_throw < unreachable_throw

    def test_5xx_with_our_json_content_type_clears_offline_before_throw(self):
        """Regression: a JSON-typed 5xx (cockpit_unresponsive) IS proof the
        cockpit was reached, even if a *prior* request had gone through the
        edge-failure branch and flipped the chip Offline. This branch must
        call setOffline(false) before it throws, not leave the chip/banner
        stuck Offline just because this particular request timed out too."""
        js = _read("app.js")
        m = re.search(r"function apiFetch\(", js)
        assert m, "apiFetch not found"
        body_start = m.start()
        chunk = js[body_start : body_start + 2200]
        ct_check = chunk.find('indexOf("application/json")')
        unresponsive_throw = chunk.find('new Error("cockpit_unresponsive")')
        offline_false_in_branch = chunk.find("setOffline(false)", ct_check, unresponsive_throw)
        assert ct_check != -1
        assert unresponsive_throw != -1
        assert offline_false_in_branch != -1, (
            "the JSON-content-type 5xx branch must call setOffline(false) "
            "before throwing cockpit_unresponsive"
        )
        assert ct_check < offline_false_in_branch < unresponsive_throw

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


class TestBridgeTimeoutHelper:
    def test_bridge_timeout_message_and_helper_defined(self):
        js = _read("app.js")
        assert "function isBridgeTimeoutError(err)" in js
        assert "var BRIDGE_TIMEOUT_MSG" in js
        assert '"cockpit_unresponsive"' in js

    def test_bridge_timeout_message_is_distinct_from_conn_error(self):
        js = _read("app.js")
        m = re.search(r'var BRIDGE_TIMEOUT_MSG = "([^"]+)"', js)
        assert m, "BRIDGE_TIMEOUT_MSG not found"
        msg = m.group(1)
        conn_m = re.search(r'var CONN_ERROR_MSG = "([^"]+)"', js)
        assert conn_m
        assert msg != conn_m.group(1)
        # Must not claim a tunnel/edge outage — this is "cockpit is slow",
        # not "cockpit is unreachable".
        assert "tunnel" not in msg


class TestCallSitesUseConnError:
    """Every user-facing apiFetch call site must distinguish an edge/tunnel
    failure from a generic app-level error, per #252 requirement 2 — and
    (#254) must also distinguish a cockpit-answered bridge timeout from
    both of those. Call sites now route through the shared `errMsg(err,
    fallback)` helper (fix-loop round 3 dedup) instead of repeating the
    isConnError/isBridgeTimeoutError ternary inline; the helper's own
    conn-vs-bridge-timeout distinction is covered by TestConnErrorHelper /
    TestBridgeTimeoutHelper above."""

    def test_load_projects_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function loadProjects()")
        assert idx != -1
        chunk = js[idx : idx + 600]
        assert "errMsg(err" in chunk

    def test_verify_password_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find('"password-form"')
        assert idx != -1
        chunk = js[idx : idx + 1500]
        assert "errMsg(err" in chunk

    def test_open_project_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function openProject(")
        assert idx != -1
        chunk = js[idx : idx + 2200]
        assert "errMsg(err" in chunk

    def test_close_project_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function closeProject(")
        assert idx != -1
        chunk = js[idx : idx + 1800]
        assert "errMsg(err" in chunk

    def test_send_lead_message_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function sendLeadMessage(")
        assert idx != -1
        chunk = js[idx : idx + 1800]
        assert "errMsg(err" in chunk

    def test_send_lead_image_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function sendLeadImage(")
        assert idx != -1
        chunk = js[idx : idx + 2200]
        assert "errMsg(err" in chunk

    def test_confirm_resume_uses_isconnerror(self):
        js = _read("app.js")
        idx = js.find("function confirmResume(")
        assert idx != -1
        chunk = js[idx : idx + 3000]
        assert "errMsg(err" in chunk


class TestErrMsgHelper:
    """Fix-loop round 3: 6 near-identical isConnError/isBridgeTimeoutError
    ternaries were deduped into one `errMsg(err, fallback)` helper."""

    def test_errmsg_defined_and_prioritizes_conn_then_bridge_then_fallback(self):
        js = _read("app.js")
        idx = js.find("function errMsg(err, fallback)")
        assert idx != -1
        chunk = js[idx : idx + 300]
        assert "isConnError(err)" in chunk
        assert "isBridgeTimeoutError(err)" in chunk
        assert "return fallback" in chunk
        # isConnError must be checked before isBridgeTimeoutError.
        assert chunk.find("isConnError(err)") < chunk.find("isBridgeTimeoutError(err)")


class TestSseRetryHasBackoff:
    """#252 requirement 3: no silent rapid retry spam against a dead edge."""

    def test_es_retry_uses_exponential_backoff_with_cap(self):
        js = _read("app.js")
        assert "function scheduleEsRetry(" in js
        idx = js.find("function scheduleEsRetry(")
        chunk = js[idx : idx + 1200]
        assert "Math.pow(2, lead.esRetries)" in chunk
        assert "Math.min(" in chunk
