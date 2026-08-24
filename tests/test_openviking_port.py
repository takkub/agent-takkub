"""Tests for `agent_takkub.openviking.port`: loopback-only port selection
(`06_SECURITY_PORT.md`). Real `bind()` calls against 127.0.0.1 are used —
safe, no network I/O — `is_healthy()` itself is stubbed so no real HTTP call
ever happens."""

from __future__ import annotations

import socket

from agent_takkub.openviking import port


def _reserve_loopback_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


class TestIsHealthy:
    def test_true_on_status_ok(self, monkeypatch):
        import io

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            port.urllib.request, "urlopen", lambda req, timeout: _Resp(b'{"status": "ok"}')
        )
        assert port.is_healthy("http://127.0.0.1:1933") is True

    def test_false_on_connection_error(self, monkeypatch):
        def _raise(req, timeout):
            raise port.urllib.error.URLError("refused")

        monkeypatch.setattr(port.urllib.request, "urlopen", _raise)
        assert port.is_healthy("http://127.0.0.1:1933") is False

    def test_false_on_unrelated_json_body(self, monkeypatch):
        import io

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            port.urllib.request, "urlopen", lambda req, timeout: _Resp(b'{"hello": "world"}')
        )
        assert port.is_healthy("http://127.0.0.1:1933") is False

    def test_false_on_non_json_body(self, monkeypatch):
        import io

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(port.urllib.request, "urlopen", lambda req, timeout: _Resp(b"not json"))
        assert port.is_healthy("http://127.0.0.1:1933") is False


class TestPickPort:
    def test_returns_preferred_port_when_free(self, monkeypatch):
        # Pick an actually-free port so the test doesn't depend on the real
        # machine's 1933 being unoccupied.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()

        decision = port.pick_port(preferred=free_port)
        assert decision.port == free_port
        assert decision.already_healthy is False

    def test_reuses_occupied_port_when_it_answers_healthy(self, monkeypatch):
        sock, occupied_port = _reserve_loopback_port()
        try:
            monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: True)
            decision = port.pick_port(preferred=occupied_port)
            assert decision.port == occupied_port
            assert decision.already_healthy is True
        finally:
            sock.close()

    def test_falls_back_to_free_port_when_occupant_is_not_healthy(self, monkeypatch):
        sock, occupied_port = _reserve_loopback_port()
        try:
            monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: False)
            decision = port.pick_port(preferred=occupied_port)
            assert decision.port != occupied_port
            assert decision.already_healthy is False
            assert decision.port > 0
        finally:
            sock.close()
