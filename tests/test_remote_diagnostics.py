"""Tests for `agent_takkub.remote.diagnostics` (#193): port-collision
transparency, reachability probing, ingress-hostname-mismatch detection.
All network/process calls are stubbed — this is a test of the diagnostic
logic itself, not of a real cloudflared/psutil environment.
"""

from __future__ import annotations

import types
import urllib.error

from agent_takkub.remote import diagnostics


class _FakeConn:
    def __init__(self, port, pid, status="ESTABLISHED"):
        self.laddr = types.SimpleNamespace(port=port)
        self.pid = pid
        self.status = status


class TestDescribePortOwner:
    def test_finds_the_listening_process(self, monkeypatch):
        fake_psutil = types.SimpleNamespace(
            net_connections=lambda kind="tcp": [_FakeConn(9999, 4242, status="LISTEN")],
            CONN_LISTEN="LISTEN",
            Process=lambda pid: types.SimpleNamespace(name=lambda: "pythonw.exe"),
            Error=Exception,
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        assert diagnostics.describe_port_owner(9999) == "pid 4242 (pythonw.exe)"

    def test_returns_none_when_nothing_listens_on_that_port(self, monkeypatch):
        fake_psutil = types.SimpleNamespace(
            net_connections=lambda kind="tcp": [_FakeConn(10000, 4242, status="LISTEN")],
            CONN_LISTEN="LISTEN",
            Process=lambda pid: types.SimpleNamespace(name=lambda: "x"),
            Error=Exception,
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        assert diagnostics.describe_port_owner(9999) is None

    def test_ignores_non_listen_connections(self, monkeypatch):
        fake_psutil = types.SimpleNamespace(
            net_connections=lambda kind="tcp": [_FakeConn(9999, 4242, status="ESTABLISHED")],
            CONN_LISTEN="LISTEN",
            Process=lambda pid: types.SimpleNamespace(name=lambda: "x"),
            Error=Exception,
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        assert diagnostics.describe_port_owner(9999) is None

    def test_process_lookup_failure_still_returns_pid(self, monkeypatch):
        class _Boom(Exception):
            pass

        def _raise_process(pid):
            raise _Boom("access denied")

        fake_psutil = types.SimpleNamespace(
            net_connections=lambda kind="tcp": [_FakeConn(9999, 4242, status="LISTEN")],
            CONN_LISTEN="LISTEN",
            Process=_raise_process,
            Error=_Boom,
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        assert diagnostics.describe_port_owner(9999) == "pid 4242 (unknown process)"

    def test_any_failure_returns_none_not_raise(self, monkeypatch):
        def _boom(kind="tcp"):
            raise RuntimeError("permission denied")

        fake_psutil = types.SimpleNamespace(net_connections=_boom)
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        assert diagnostics.describe_port_owner(9999) is None


class TestProbeHttp:
    def test_2xx_response_is_reachable(self, monkeypatch):
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(diagnostics.urllib.request, "urlopen", lambda url, timeout: _Resp())
        ok, detail = diagnostics.probe_http("http://127.0.0.1:9999/sek/", timeout=1.0)
        assert ok is True
        assert "200" in detail

    def test_http_error_response_still_counts_as_reachable(self, monkeypatch):
        def _raise(url, timeout):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        monkeypatch.setattr(diagnostics.urllib.request, "urlopen", _raise)
        ok, detail = diagnostics.probe_http("http://127.0.0.1:9999/wrong/", timeout=1.0)
        assert ok is True
        assert "404" in detail

    def test_network_failure_is_not_reachable(self, monkeypatch):
        def _raise(url, timeout):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(diagnostics.urllib.request, "urlopen", _raise)
        ok, detail = diagnostics.probe_http("https://dead.example.com/sek/", timeout=1.0)
        assert ok is False
        assert "no route to host" in detail

    def test_timeout_is_not_reachable(self, monkeypatch):
        def _raise(url, timeout):
            raise TimeoutError("timed out")

        monkeypatch.setattr(diagnostics.urllib.request, "urlopen", _raise)
        ok, _detail = diagnostics.probe_http("https://slow.example.com/sek/", timeout=1.0)
        assert ok is False

    def test_never_raises_on_unexpected_exception(self, monkeypatch):
        def _raise(url, timeout):
            raise ValueError("garbage url")

        monkeypatch.setattr(diagnostics.urllib.request, "urlopen", _raise)
        ok, detail = diagnostics.probe_http("not a url", timeout=1.0)
        assert ok is False
        assert "garbage url" in detail


class TestProbeLocalAndPublic:
    def test_probe_local_builds_the_loopback_url(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            diagnostics,
            "probe_http",
            lambda url, timeout: captured.setdefault("url", url) or (True, "HTTP 200"),
        )
        diagnostics.probe_local(9999, "sek123")
        assert captured["url"] == "http://127.0.0.1:9999/sek123/"

    def test_probe_public_builds_the_public_url_and_strips_trailing_slash(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            diagnostics,
            "probe_http",
            lambda url, timeout: captured.setdefault("url", url) or (True, "HTTP 200"),
        )
        diagnostics.probe_public("https://x.example.com/", "sek123")
        assert captured["url"] == "https://x.example.com/sek123/"


class TestCheckIngressMismatch:
    def test_no_config_file_returns_none(self, tmp_path):
        assert (
            diagnostics.check_ingress_mismatch("https://a.example.com", tmp_path / "config.yml")
            is None
        )

    def test_empty_public_url_returns_none(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("ingress:\n  - hostname: a.example.com\n    service: http://localhost:1\n")
        assert diagnostics.check_ingress_mismatch("", cfg) is None

    def test_matching_hostname_returns_none(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("ingress:\n  - hostname: a.example.com\n    service: http://localhost:1\n")
        assert diagnostics.check_ingress_mismatch("https://a.example.com", cfg) is None

    def test_mismatched_hostname_is_reported(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("ingress:\n  - hostname: old.example.com\n    service: http://localhost:1\n")
        msg = diagnostics.check_ingress_mismatch("https://new.example.com", cfg)
        assert msg is not None
        # Not URL sanitization — just confirming the returned diagnostic
        # string mentions both hostnames; no trust decision is gated on
        # this substring check.
        assert "old.example.com" in msg  # codeql[py/incomplete-url-substring-sanitization]
        assert "new.example.com" in msg  # codeql[py/incomplete-url-substring-sanitization]

    def test_malformed_yaml_returns_none_not_raise(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("not: valid: yaml: at: all:::")
        assert diagnostics.check_ingress_mismatch("https://a.example.com", cfg) is None

    def test_missing_ingress_key_returns_none(self, tmp_path):
        cfg = tmp_path / "config.yml"
        cfg.write_text("tunnel: abc123\n")
        assert diagnostics.check_ingress_mismatch("https://a.example.com", cfg) is None
