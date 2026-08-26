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
        assert headers.get("X-Content-Type-Options") == "nosniff"

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


class TestAttachmentDisposition:
    """#389: binary/office extensions and anything published with
    `--attachment` are served with `Content-Disposition: attachment` so the
    browser downloads rather than renders them — never inline, regardless of
    what `_REPORT_CONTENT_TYPES` maps the extension to."""

    def test_docx_report_gets_octet_stream_and_attachment_header(self, server, tmp_path):
        src = tmp_path / "doc.docx"
        src.write_bytes(b"PK\x03\x04fake-docx")
        record, _ = reports.publish(str(src), "doc.docx", "demo")
        status, body, headers = _get(_url(server, f"/sek/r/demo/doc.docx?k={record.token}"))
        assert status == 200
        assert body == b"PK\x03\x04fake-docx"
        assert "wordprocessingml" in headers.get("Content-Type", "")
        assert headers.get("Content-Disposition") == 'attachment; filename="doc.docx"'
        assert headers.get("X-Content-Type-Options") == "nosniff"

    def test_zip_csv_txt_all_get_attachment_header(self, server, tmp_path):
        cases = {
            "data.zip": b"PK",
            "table.csv": b"a,b\n1,2\n",
            "notes.txt": b"plain text",
        }
        for name, body in cases.items():
            src = tmp_path / name
            src.write_bytes(body)
            record, _ = reports.publish(str(src), name, "demo")
            status, resp_body, headers = _get(_url(server, f"/sek/r/demo/{name}?k={record.token}"))
            assert status == 200
            assert resp_body == body
            assert headers.get("Content-Disposition") == f'attachment; filename="{name}"'

    def test_html_default_has_no_disposition_header(self, server, published):
        status, _, headers = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 200
        assert headers.get("Content-Disposition") is None

    def test_attachment_flag_forces_disposition_on_html(self, server, tmp_path):
        src = tmp_path / "forced.html"
        src.write_text("<html><body>hi</body></html>", encoding="utf-8")
        record, _ = reports.publish(str(src), "forced.html", "demo", attachment=True)
        status, _, headers = _get(_url(server, f"/sek/r/demo/forced.html?k={record.token}"))
        assert status == 200
        assert headers.get("Content-Disposition") == 'attachment; filename="forced.html"'


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
        assert server.auth.is_report_locked_out("demo", "status.html")
        assert not server.auth.is_locked_out()  # check_token's own counter untouched

    def test_successful_request_resets_report_fail_count(self, server, published):
        _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 200
        # one more wrong guess alone must not trip the threshold (2) again
        status, _, _ = _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        assert status == 404
        assert not server.auth.is_report_locked_out("demo", "status.html")

    def test_success_on_one_report_does_not_reset_another_reports_fail_count(
        self, server, published, tmp_path
    ):
        """Should-fix #1 (review 2026-08-23-367): the lockout counter is
        scoped per (project_ns, name), not global — a legitimate success
        against report B must not reset the fail-count an attacker built up
        brute-forcing an unrelated report A."""
        src2 = tmp_path / "src2.html"
        src2.write_text("<html><body>other</body></html>", encoding="utf-8")
        other = reports.publish(src2, "other.html", "demo")[0]

        # one wrong guess against A ("status.html")
        _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        # a legitimate success against B ("other.html") — must not touch A's counter
        status, _, _ = _get(_url(server, f"/sek/r/demo/other.html?k={other.token}"))
        assert status == 200
        assert not server.auth.is_report_locked_out("demo", "status.html")
        # A's fail count is still 1 — one more wrong guess trips the threshold (2)
        status, _, _ = _get(_url(server, "/sek/r/demo/status.html?k=wrong"))
        assert status == 404
        assert server.auth.is_report_locked_out("demo", "status.html")
        assert not server.auth.is_report_locked_out("demo", "other.html")

    def test_missing_token_requests_do_not_count_toward_lockout(self, server, published):
        """Should-fix #2: a request with no `k` at all must not count as a
        failure, mirroring `check_token`'s `if token:` guard."""
        for _ in range(5):  # well past lockout_after_fails=2
            status, _, _ = _get(_url(server, "/sek/r/demo/status.html"))
            assert status == 404
        assert not server.auth.is_report_locked_out("demo", "status.html")
        status, _, _ = _get(_url(server, f"/sek/r/demo/status.html?k={published.token}"))
        assert status == 200


def test_content_disposition_attachment_neutralizes_crlf_and_quotes() -> None:
    """CodeQL #41 (py/http-response-splitting): the header helper must be a
    sanitizer in its own right — CR/LF, quotes, separators and anything
    outside `[A-Za-z0-9._-]` can never reach the response header line."""
    from agent_takkub.remote.http_server import _content_disposition_attachment

    hdr = _content_disposition_attachment('rep\r\nSet-Cookie: x=1"; ort.docx')
    assert "\r" not in hdr and "\n" not in hdr
    assert hdr.count('"') == 2
    assert hdr == 'attachment; filename="rep__Set-Cookie__x_1___ort.docx"'
    assert _content_disposition_attachment("") == 'attachment; filename="report"'
    assert _content_disposition_attachment("a.docx") == 'attachment; filename="a.docx"'
