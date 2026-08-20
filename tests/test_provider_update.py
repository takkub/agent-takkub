"""Tests for provider_update.py — boot-time provider CLI update logic (#313).

Pure business logic, no Qt — every subprocess/network call is mocked, never
real (targeted-tests policy: no real download/spawn in CI).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

import agent_takkub.provider_update as pu
from agent_takkub.provider_spec import PROVIDER_REGISTRY


class TestEligibilityGap:
    def test_not_installed_when_discover_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pu, "_discover", lambda spec: None)
        gap = pu.eligibility_gap("codex")
        assert gap is not None
        assert gap.status == pu.STATUS_SKIPPED_NOT_INSTALLED

    def test_disabled_short_circuits_before_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = []
        monkeypatch.setattr(pu, "_discover", lambda spec: called.append(1) or "/bin/codex")
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda name: name == "codex")
        gap = pu.eligibility_gap("codex")
        assert gap is not None
        assert gap.status == pu.STATUS_SKIPPED_DISABLED

    def test_eligible_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pu, "_discover", lambda spec: "/bin/codex")
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda name: False)
        assert pu.eligibility_gap("codex") is None

    def test_unknown_provider(self) -> None:
        gap = pu.eligibility_gap("nonexistent")
        assert gap is not None
        assert gap.status == pu.STATUS_FAILED

    def test_eligible_providers_filters_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda name: name == "kimi")
        monkeypatch.setattr(
            pu, "_discover", lambda spec: None if spec.name == "cursor" else "/bin/x"
        )
        result = pu.eligible_providers()
        assert "kimi" not in result
        assert "cursor" not in result
        assert "claude" in result
        assert set(result) <= set(PROVIDER_REGISTRY)


class TestGenericUpdateArgv:
    def test_npm_reused_verbatim(self) -> None:
        spec = PROVIDER_REGISTRY["codex"]
        assert pu._generic_update_argv(spec) == spec.install_command

    def test_uv_becomes_tool_upgrade(self) -> None:
        spec = PROVIDER_REGISTRY["kimi"]
        assert pu._generic_update_argv(spec) == ["uv", "tool", "upgrade", "kimi-cli"]

    def test_no_install_command_returns_none(self) -> None:
        spec = PROVIDER_REGISTRY["cursor"]
        assert spec.install_command is None
        assert pu._generic_update_argv(spec) is None


class TestUpdateClaude:
    def _patch_common(self, monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
        import agent_takkub.claude_update as cu

        monkeypatch.setattr(pu, "_discover", lambda spec: "/bin/claude")
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda name: False)
        monkeypatch.setattr(
            cu, "current_version", overrides.get("current_version", lambda: "2.1.0")
        )
        monkeypatch.setattr(
            cu, "latest_version", overrides.get("latest_version", lambda: (True, "2.1.0"))
        )

    def test_already_up_to_date_skips_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        import agent_takkub.claude_update as cu

        called = []
        monkeypatch.setattr(cu, "apply_update", lambda: called.append(1))
        outcome = pu.update_provider("claude")
        assert outcome.status == pu.STATUS_UP_TO_DATE
        assert not called

    def test_update_applied_and_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch, latest_version=lambda: (True, "2.2.0"))
        import agent_takkub.claude_update as cu
        from agent_takkub import _pty_backend

        monkeypatch.setattr(cu, "apply_update", lambda: (True, "claude CLI updated"))
        monkeypatch.setattr(_pty_backend, "_looks_like_valid_executable", lambda path: True)
        from agent_takkub import config

        monkeypatch.setattr(config, "find_claude_executable", lambda: "/bin/claude")
        outcome = pu.update_provider("claude")
        assert outcome.status == pu.STATUS_UPDATED
        assert "2.1.0" in outcome.detail and "2.2.0" in outcome.detail

    def test_npm_install_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch, latest_version=lambda: (True, "2.2.0"))
        import agent_takkub.claude_update as cu

        monkeypatch.setattr(cu, "apply_update", lambda: (False, "npm install timed out"))
        outcome = pu.update_provider("claude")
        assert outcome.status == pu.STATUS_FAILED
        assert "timed out" in outcome.detail

    def test_placeholder_binary_after_update_fails_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real 2026-08-20 incident: npm exits 0 but the optional-dep binary
        fetch failed, leaving claude.exe as a ~500B placeholder."""
        self._patch_common(monkeypatch, latest_version=lambda: (True, "2.2.0"))
        import agent_takkub.claude_update as cu
        from agent_takkub import _pty_backend, config

        monkeypatch.setattr(cu, "apply_update", lambda: (True, "claude CLI updated"))
        monkeypatch.setattr(config, "find_claude_executable", lambda: "/bin/claude")
        monkeypatch.setattr(_pty_backend, "_looks_like_valid_executable", lambda path: False)
        outcome = pu.update_provider("claude")
        assert outcome.status == pu.STATUS_FAILED
        assert "placeholder" in outcome.detail
        # Regression (macOS report 2026-08-20): the hint used to hardcode
        # `win32-x64`, sending mac users to install a Windows binary.
        assert f"claude-code-{pu._node_optional_dep_tag()}" in outcome.detail
        assert sys.platform in outcome.detail

    def test_version_check_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch, latest_version=lambda: (False, "npm view timed out"))
        outcome = pu.update_provider("claude")
        assert outcome.status == pu.STATUS_FAILED
        assert "npm view timed out" in outcome.detail


class TestUpdateGeneric:
    def _patch_installed(self, monkeypatch: pytest.MonkeyPatch, name: str, path: str) -> None:
        monkeypatch.setattr(pu, "_discover", lambda spec: path)
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda n: False)

    def test_npm_update_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "codex", "/bin/codex")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(pu, "_run", return_value=fake) as mock_run:
            outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_UPDATED
        argv = mock_run.call_args[0][0]
        assert argv == ["/usr/bin/npm", "install", "-g", "@openai/codex"]

    def test_npm_update_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "codex", "/bin/codex")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="npm ERR! network timeout"
        )
        with patch.object(pu, "_run", return_value=fake):
            outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_FAILED
        assert "network timeout" in outcome.detail

    def test_update_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "codex", "/bin/codex")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")
        with patch.object(pu, "_run", side_effect=subprocess.TimeoutExpired(cmd="npm", timeout=1)):
            outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_FAILED
        assert "timed out" in outcome.detail

    def test_uv_tool_upgrade_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "kimi", "/bin/kimi")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(pu, "_run", return_value=fake) as mock_run:
            outcome = pu.update_provider("kimi")
        assert outcome.status == pu.STATUS_UPDATED
        argv = mock_run.call_args[0][0]
        assert argv == ["/usr/bin/uv", "tool", "upgrade", "kimi-cli"]

    def test_package_manager_missing_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "codex", "/bin/codex")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: None)
        outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_FAILED
        assert "npm" in outcome.detail

    def test_binary_vanishes_after_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch, "codex", "/bin/codex")
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        calls = {"n": 0}

        def fake_discover(spec):
            calls["n"] += 1
            return "/bin/codex" if calls["n"] == 1 else None

        monkeypatch.setattr(pu, "_discover", fake_discover)
        with patch.object(pu, "_run", return_value=fake):
            outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_FAILED
        assert "no longer resolves" in outcome.detail

    def test_no_mechanism_providers_skip_without_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("gemini", "cursor"):
            self._patch_installed(monkeypatch, name, f"/bin/{name}")
            with patch.object(pu, "_run") as mock_run:
                outcome = pu.update_provider(name)
            assert outcome.status == pu.STATUS_SKIPPED_NO_MECHANISM
            mock_run.assert_not_called()

    def test_disabled_provider_never_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda n: True)
        discover_calls = []
        monkeypatch.setattr(pu, "_discover", lambda spec: discover_calls.append(1))
        outcome = pu.update_provider("codex")
        assert outcome.status == pu.STATUS_SKIPPED_DISABLED
        # eligibility_gap short-circuits on is_disabled before ever calling
        # _discover — a disabled provider must cost zero PATH-probe work too.
        assert not discover_calls


class TestNodeOptionalDepTag:
    """The remediation hint must name THIS machine's optional dep."""

    @pytest.mark.parametrize(
        ("plat", "machine", "expected"),
        [
            ("win32", "AMD64", "win32-x64"),
            ("darwin", "arm64", "darwin-arm64"),
            ("darwin", "x86_64", "darwin-x64"),
            ("linux", "aarch64", "linux-arm64"),
            ("linux", "x86_64", "linux-x64"),
        ],
    )
    def test_tag_follows_node_naming(
        self, monkeypatch: pytest.MonkeyPatch, plat: str, machine: str, expected: str
    ) -> None:
        monkeypatch.setattr(pu.sys, "platform", plat)
        monkeypatch.setattr(pu.platform, "machine", lambda: machine)
        assert pu._node_optional_dep_tag() == expected


class TestErrorExcerpt:
    def test_npm_eexist_shows_the_cause_not_the_log_path(self) -> None:
        """The macOS report showed only npm's boilerplate tail — the EEXIST
        code and the colliding path (printed FIRST) were cut off."""
        stderr = "\n".join(
            [
                "npm error code EEXIST",
                "npm error path /usr/local/bin/codex",
                "npm error EEXIST: file already exists",
                "npm error File exists: /usr/local/bin/codex",
                "npm error Remove the existing file and try again, or run npm",
                "npm error with --force to overwrite files recklessly.",
                "npm error A complete log of this run can be found in: "
                "/Users/x/.npm/_logs/2026-08-20T07_25_39_770Z-debug-0.log",
            ]
        )
        excerpt = pu._error_excerpt(stderr)
        assert "EEXIST" in excerpt
        assert "/usr/local/bin/codex" in excerpt
        assert "_logs" not in excerpt
        assert "recklessly" not in excerpt

    def test_non_npm_output_still_uses_the_tail(self) -> None:
        excerpt = pu._error_excerpt("warming up\nresolving\nerror: disk full")
        assert excerpt == "resolving | error: disk full"

    def test_empty_output(self) -> None:
        assert pu._error_excerpt("") == ""
        assert pu._error_excerpt("   \n  \n") == ""

    def test_only_boilerplate_left(self) -> None:
        assert pu._error_excerpt("npm error A complete log of this run can be found in: /x") == ""


class TestNpmSerialisation:
    """npm has no cross-process lock for the global prefix — concurrent
    `npm install -g` runs corrupt each other (macOS report 2026-08-20)."""

    def _patch_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pu, "_discover", lambda spec: "/bin/x")
        import agent_takkub.provider_state as provider_state

        monkeypatch.setattr(provider_state, "is_disabled", lambda n: False)
        monkeypatch.setattr(pu.shutil, "which", lambda prog: f"/usr/bin/{prog}")

    def test_two_npm_providers_never_overlap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch)
        inflight = 0
        overlapped = False
        seen = []

        def fake_run(argv, timeout):
            nonlocal inflight, overlapped
            inflight += 1
            if inflight > 1:
                overlapped = True
            seen.append(argv[-1])
            time.sleep(0.15)  # hold the "install" open wide enough to collide
            inflight -= 1
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pu, "_run", fake_run)
        threads = [
            threading.Thread(target=pu.update_provider, args=(name,))
            for name in ("codex", "opencode")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not any(t.is_alive() for t in threads)
        assert overlapped is False
        assert sorted(seen) == ["@openai/codex", "opencode-ai"]

    def test_uv_provider_does_not_take_the_npm_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_installed(monkeypatch)
        held = pu._NPM_LOCK.acquire(timeout=1)
        assert held
        try:
            fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch.object(pu, "_run", return_value=fake) as mock_run:
                outcome = pu.update_provider("kimi")
            assert outcome.status == pu.STATUS_UPDATED
            assert mock_run.call_args[0][0][1:] == ["tool", "upgrade", "kimi-cli"]
        finally:
            pu._NPM_LOCK.release()

    def test_npm_provider_reports_lock_wait_instead_of_hanging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_installed(monkeypatch)
        monkeypatch.setattr(pu, "_NPM_LOCK_WAIT_S", 0.05)
        held = pu._NPM_LOCK.acquire(timeout=1)
        assert held
        try:
            with patch.object(pu, "_run") as mock_run:
                outcome = pu.update_provider("codex")
            assert outcome.status == pu.STATUS_FAILED
            assert "npm lock" in outcome.detail
            mock_run.assert_not_called()
        finally:
            pu._NPM_LOCK.release()
