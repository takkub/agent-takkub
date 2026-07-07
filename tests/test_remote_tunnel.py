"""Tests for `agent_takkub.remote.tunnel` (§6.6, X-check 5.1/5.2):
dual-mode config/URL generation, platform-specific launch, and tree-kill on
stop. Subprocess spawning itself is stubbed — this isn't a test of
cloudflared/bat scripts actually running, just of the code that launches and
tears them down.
"""

from __future__ import annotations

import json
import sys

import pytest

from agent_takkub.remote import tunnel
from agent_takkub.remote.config import TunnelConfig


class _FakeProc:
    def __init__(self, pid: int = 4242, lines: list[bytes] | None = None) -> None:
        self.pid = pid
        self._lines = lines or []
        self.stdout = iter(self._lines)
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True


class TestNamedTunnelConfigGeneration:
    def test_reads_tunnel_id_and_renders_template(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"TunnelID": "abc-123"}), encoding="utf-8")
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(creds))

        out = tunnel._write_named_config(cfg, "https://agent-takkub.example.com", 8899)

        text = out.read_text(encoding="utf-8")
        assert "tunnel: abc-123" in text
        assert f"credentials-file: {creds}" in text
        assert "hostname: agent-takkub.example.com" in text
        assert "service: http://localhost:8899" in text  # never host.docker.internal

    def test_missing_tunnel_id_raises(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"AccountTag": "x"}), encoding="utf-8")
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(creds))
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)

    def test_unreadable_credentials_raises(self, tmp_path):
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(tmp_path / "missing.json"))
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)


class TestPlatformSpecificLaunch:
    def test_windows_wraps_with_cmd(self, monkeypatch):
        monkeypatch.setattr(tunnel.sys, "platform", "win32")
        captured = {}

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(tunnel.subprocess, "Popen", _fake_popen)
        tunnel._spawn(["cloudflared", "tunnel", "run"])
        assert captured["argv"][:3] == ["cmd", "/d", "/c"]
        assert "creationflags" in captured["kwargs"]

    def test_posix_wraps_with_sh(self, monkeypatch):
        monkeypatch.setattr(tunnel.sys, "platform", "linux")
        captured = {}

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(tunnel.subprocess, "Popen", _fake_popen)
        tunnel._spawn(["./tunnel.sh", "8899"])
        assert captured["argv"][:2] == ["/bin/sh", "-c"]
        assert captured["kwargs"].get("start_new_session") is True

    def test_extra_env_is_merged_not_replaced(self, monkeypatch):
        monkeypatch.setattr(tunnel.sys, "platform", "linux")
        captured = {}
        monkeypatch.setenv("EXISTING_VAR", "keep-me")

        def _fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(tunnel.subprocess, "Popen", _fake_popen)
        tunnel._spawn(["./tunnel.sh"], extra_env={"TAKKUB_REMOTE_PORT": "8899"})
        assert captured["env"]["TAKKUB_REMOTE_PORT"] == "8899"
        assert captured["env"]["EXISTING_VAR"] == "keep-me"


class TestModeBUrlCapture:
    def test_scans_stdout_for_a_tunnel_url(self, monkeypatch):
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(
            lines=[
                b"starting up...\n",
                b"your url is: https://random-name.trycloudflare.com\n",
            ]
        )
        t._scan_for_url()
        assert t.captured_url == "https://random-name.trycloudflare.com"

    def test_named_tunnel_keeps_the_configured_public_url(self):
        cfg = TunnelConfig(type="cloudflared", credentials_json="creds.json")
        t = tunnel.Tunnel(cfg, public_url="https://agent-takkub.example.com", port=8899)
        assert t.captured_url == "https://agent-takkub.example.com"


class TestStopTreeKill:
    def test_stop_calls_tree_kill_with_the_process_pid(self, monkeypatch):
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(pid=9999)

        killed = {}
        monkeypatch.setattr(tunnel, "_tree_kill", lambda pid: killed.setdefault("pid", pid))
        t.stop()
        assert killed["pid"] == 9999
        assert t._proc is None

    def test_stop_on_never_started_tunnel_is_a_no_op(self):
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t.stop()  # must not raise


class TestStartRouting:
    def test_named_mode_requires_credentials_and_public_url(self):
        cfg = TunnelConfig(type="cloudflared", credentials_json="")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        with pytest.raises(tunnel.TunnelError):
            t.start()

    def test_bat_mode_requires_a_script_path(self):
        cfg = TunnelConfig(type="bat", credentials_json="")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        with pytest.raises(tunnel.TunnelError):
            t.start()


def test_real_platform_constant_is_sane():
    # Sanity check the module imported cleanly on whatever OS is running
    # this test — CI runs both windows-latest and macos-latest.
    assert sys.platform in ("win32", "darwin", "linux")
