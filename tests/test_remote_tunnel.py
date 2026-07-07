"""Tests for `agent_takkub.remote.tunnel` (§6.6, X-check 5.1/5.2):
dual-mode config/URL generation, platform-specific launch, and tree-kill on
stop. Subprocess spawning itself is stubbed — this isn't a test of
cloudflared/bat scripts actually running, just of the code that launches and
tears them down.
"""

from __future__ import annotations

import json
import sys
import types

import pytest
import yaml

from agent_takkub.remote import tunnel
from agent_takkub.remote.config import TunnelConfig

_UUID = "12345678-1234-1234-1234-123456789abc"


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
        creds.write_text(json.dumps({"TunnelID": _UUID}), encoding="utf-8")
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(creds))

        out = tunnel._write_named_config(cfg, "https://agent-takkub.example.com", 8899)

        parsed = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert parsed["tunnel"] == _UUID
        assert parsed["credentials-file"] == str(creds)
        assert parsed["ingress"][0] == {
            "hostname": "agent-takkub.example.com",
            "service": "http://localhost:8899",  # never host.docker.internal
        }
        assert parsed["ingress"][1] == {"service": "http_status:404"}

    def test_missing_tunnel_id_raises(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"AccountTag": "x"}), encoding="utf-8")
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(creds))
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)

    def test_non_uuid_tunnel_id_raises(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"TunnelID": "abc-123"}), encoding="utf-8")
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(creds))
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)

    def test_unreadable_credentials_raises(self, tmp_path):
        cfg = TunnelConfig(type="cloudflared", credentials_json=str(tmp_path / "missing.json"))
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)

    def test_relative_credentials_path_rejected(self, tmp_path):
        cfg = TunnelConfig(type="cloudflared", credentials_json="creds.json")
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(cfg, "https://x.example.com", 8899)


class TestPublicUrlInjectionGuard:
    """H-D: public_url ends up as an ingress hostname in generated YAML —
    anything but a bare https hostname must be rejected before it ever
    reaches the template, not just safely escaped."""

    def _cfg(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"TunnelID": _UUID}), encoding="utf-8")
        return TunnelConfig(type="cloudflared", credentials_json=str(creds))

    def test_newline_injection_rejected(self, tmp_path):
        evil = "https://x.example.com\ningress:\n  - hostname: evil.example.com"
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(self._cfg(tmp_path), evil, 8899)

    def test_path_rejected(self, tmp_path):
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(self._cfg(tmp_path), "https://x.example.com/path", 8899)

    def test_non_https_scheme_rejected(self, tmp_path):
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(self._cfg(tmp_path), "http://x.example.com", 8899)

    def test_userinfo_rejected(self, tmp_path):
        with pytest.raises(tunnel.TunnelError):
            tunnel._write_named_config(self._cfg(tmp_path), "https://user@x.example.com", 8899)

    def test_valid_hostname_accepted(self, tmp_path):
        out = tunnel._write_named_config(self._cfg(tmp_path), "https://x.example.com", 8899)
        parsed = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert parsed["ingress"][0]["hostname"] == "x.example.com"


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


class TestKillOnCloseJob:
    """H-E: Windows Job Object with KILL_ON_JOB_CLOSE — kernel-enforced
    cleanup for the case our own process dies without calling Tunnel.stop()."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
    def test_real_job_object_kills_process_on_handle_close(self):
        import subprocess
        import time

        job = tunnel._create_kill_on_close_job()
        assert job is not None
        proc = subprocess.Popen(
            ["ping", "-t", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            tunnel._assign_to_job(job, proc.pid)
            time.sleep(0.3)
            assert proc.poll() is None, "process should still be running before the job closes"
            tunnel.ctypes.windll.kernel32.CloseHandle(job)
            proc.wait(timeout=5)
            assert proc.returncode is not None
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_own_job_is_a_no_op_off_windows(self, monkeypatch):
        monkeypatch.setattr(tunnel.sys, "platform", "linux")
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(pid=1234)
        t._own_job_if_windows()
        assert t._job is None

    def test_own_job_assigns_the_spawned_process_when_windows(self, monkeypatch):
        monkeypatch.setattr(tunnel.sys, "platform", "win32")
        calls = {}
        monkeypatch.setattr(tunnel, "_create_kill_on_close_job", lambda: 555)
        monkeypatch.setattr(
            tunnel, "_assign_to_job", lambda job, pid: calls.setdefault((job, pid), True)
        )
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(pid=4242)
        t._own_job_if_windows()
        assert t._job == 555
        assert calls == {(555, 4242): True}

    def test_own_job_leaves_job_none_and_closes_handle_when_assign_fails(self, monkeypatch):
        # Job Object hardening: `_assign_to_job` returning False (e.g.
        # OpenProcess denied) must not leave `_job` set — a caller reading
        # `_job is not None` as "this process is crash-protected" would
        # otherwise be wrong.
        monkeypatch.setattr(tunnel.sys, "platform", "win32")
        closed = []
        fake_kernel32 = types.SimpleNamespace(CloseHandle=lambda h: closed.append(h))
        monkeypatch.setattr(
            tunnel.ctypes, "windll", types.SimpleNamespace(kernel32=fake_kernel32), raising=False
        )
        monkeypatch.setattr(tunnel, "_create_kill_on_close_job", lambda: 777)
        monkeypatch.setattr(tunnel, "_assign_to_job", lambda job, pid: False)
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(pid=4242)
        t._own_job_if_windows()
        assert t._job is None
        assert closed == [777]

    def test_assign_to_job_returns_false_when_open_process_fails(self, monkeypatch):
        fake_kernel32 = types.SimpleNamespace(OpenProcess=lambda *a, **kw: 0)
        monkeypatch.setattr(
            tunnel.ctypes, "windll", types.SimpleNamespace(kernel32=fake_kernel32), raising=False
        )
        assert tunnel._assign_to_job(555, 4242) is False

    def test_assign_to_job_returns_false_when_assign_call_fails(self, monkeypatch):
        fake_kernel32 = types.SimpleNamespace(
            OpenProcess=lambda *a, **kw: 99,
            AssignProcessToJobObject=lambda job, handle: 0,
            CloseHandle=lambda h: None,
        )
        monkeypatch.setattr(
            tunnel.ctypes, "windll", types.SimpleNamespace(kernel32=fake_kernel32), raising=False
        )
        assert tunnel._assign_to_job(555, 4242) is False

    def test_stop_closes_the_job_handle(self, monkeypatch):
        closed = []
        fake_kernel32 = types.SimpleNamespace(CloseHandle=lambda h: closed.append(h))
        monkeypatch.setattr(
            tunnel.ctypes, "windll", types.SimpleNamespace(kernel32=fake_kernel32), raising=False
        )
        monkeypatch.setattr(tunnel, "_tree_kill", lambda pid: None)
        cfg = TunnelConfig(type="bat", credentials_json="./quick-tunnel.sh")
        t = tunnel.Tunnel(cfg, public_url="", port=8899)
        t._proc = _FakeProc(pid=1234)
        t._job = 424242
        t.stop()
        assert closed == [424242]
        assert t._job is None


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
