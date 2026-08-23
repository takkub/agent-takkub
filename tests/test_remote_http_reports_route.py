"""Tests for `remote/http_server.py`'s `/r/<project_ns>/<name>?k=<token>`
route (#367 Remote Reports): token auth (no bearer/password tier at all),
headers, expiry/revoke/rotate, traversal/non-whitelisted-extension
rejection, and its own lockout counter (independent of the bearer/password
lockout `test_remote_http_server.py` already covers). Same harness pattern
as `test_remote_http_server.py`; `RUNTIME_DIR`/`remote.config._PATH` are
isolated per test by `tests/conftest.py`'s autouse fixture.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from agent_takkub.remote import http_server, reports
from agent_takkub.remote.config import RemoteConfig


class _FakeOrch:
    _lead_token = "lead-tok"

    def _resolve_project(self, project):
        return "default"


@pytest.fixture
def server():
    config = RemoteConfig(
        bind_port=0, secret_path="sek", token="tok", mode="control", lockout_after_fails=2
    )
    srv = http_server.start_server(config, _FakeOrch())
    yield srv
    srv.stop()


@pytest.fixture
def published(tmp_path):
    src = tmp_path / "src.html"
    src.write_text("<html><body>report</body></html>", encoding="utf-8")
    return reports.publish(src, "status.html", "demo", label="สถานะ")[0]


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class TestReportRoute:
    def test_valid_token_returns_200_with_body_and_headers(self, server, published):
        status, body, headers = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 200
        assert body == b"<html><body>report</body></html>"
        assert headers.get("Content-Type", "").startswith("text/html")
        assert headers.get("Cache-Control") == "private, no-store"
        assert headers.get("X-Robots-Tag") == "noindex"
        csp = headers.get("Content-Security-Policy")
        assert csp is not None
        assert "script-src 'self' 'unsafe-inline'" in csp

    def test_wrong_secret_path_is_404(self, server, published):
        status, _, _ = _get(_url(server, f"/wrong/r/demo/status.html?k={published.token}"))
        assert status == 404

    def test_wrong_token_is_404(self, server, published):
        status, _, _ = _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        assert status == 404

    def test_missing_token_is_404(self, server, published):
        status, _, _ = _get(_url(server, "/sek/r/demo/status.html"))
        assert status == 404

    def test_unknown_name_is_404(self, server, published):
        status, _, _ = _get(_url(server, f"/sek/r/demo/other.html?k={published.token}"))
        assert status == 404

    def test_unknown_project_is_404(self, server, published):
        status, _, _ = _get(_url(server, f"/sek/r/other-proj/status.html?k={published.token}"))
        assert status == 404

    def test_revoked_report_is_404(self, server, published):
        reports.revoke("status.html", "demo")
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 404

    def test_expired_report_is_404(self, server, published):
        shares_path = reports.reports_root("demo") / "_shares.json"
        data = json.loads(shares_path.read_text(encoding="utf-8"))
        data["status.html"]["expires"] = "2000-01-01T00:00:00+00:00"
        shares_path.write_text(json.dumps(data), encoding="utf-8")
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 404

    def test_rotate_kills_old_link_new_link_works(self, server, published):
        rotated = reports.rotate("status.html", "demo")
        status_old, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status_old == 404
        status_new, body, _ = _get(_url(server, f"/sek/r/demo/status.html?k={rotated.token}"))
        assert status_new == 200
        assert body == b"<html><body>report</body></html>"

    def test_non_whitelisted_extension_is_404_even_with_matching_record(self, server):
        root = reports.reports_root("demo2")
        root.mkdir(parents=True)
        (root / "script.exe").write_bytes(b"MZ")
        shares_path = root / "_shares.json"
        shares_path.write_text(
            json.dumps(
                {"script.exe": {"token": "tok123", "created": "x", "expires": None, "label": ""}}
            ),
            encoding="utf-8",
        )
        status, _, _ = _get(_url(server, "/sek/r/demo2/script.exe?k=tok123"))
        assert status == 404

    def test_traversal_in_path_is_404(self, server, published):
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(b"GET /sek/r/demo/../../config.py?k=x HTTP/1.0\r\n\r\n")
            resp = sock.recv(4096)
        assert resp.startswith(b"HTTP/1.0 404")

    def test_bare_r_route_with_no_project_or_name_is_404(self, server):
        status, _, _ = _get(_url(server, "/sek/r/"))
        assert status == 404


class TestReportLockout:
    def test_repeated_wrong_tokens_lock_out(self, server, published):
        for _ in range(2):
            status, _, _ = _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
            assert status == 404
        # threshold (2, from the fixture's lockout_after_fails) reached —
        # even the CORRECT token is rejected now, until backoff expires.
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 404

    def test_lockout_is_independent_of_bearer_lockout(self, server, published):
        for _ in range(2):
            _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        assert server.auth.is_report_locked_out()
        assert not server.auth.is_locked_out()  # check_token's own counter untouched

    def test_successful_request_resets_report_fail_count(self, server, published):
        _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 200
        # one more wrong guess alone must not trip the threshold (2) again
        status, _, _ = _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        assert status == 404
        assert not server.auth.is_report_locked_out()
