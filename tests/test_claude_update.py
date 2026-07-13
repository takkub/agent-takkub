"""Tests for the Claude CLI updater helpers and npm command construction."""

from __future__ import annotations

import subprocess

from agent_takkub import claude_update as cu


class TestParseVersion:
    def test_claude_version_output(self):
        assert cu.parse_version("2.1.156 (Claude Code)") == "2.1.156"

    def test_bare_npm_output(self):
        assert cu.parse_version("2.1.156\n") == "2.1.156"

    def test_four_component(self):
        assert cu.parse_version("1.2.3.4") == "1.2.3.4"

    def test_no_version(self):
        assert cu.parse_version("no digits here") is None

    def test_empty(self):
        assert cu.parse_version("") is None


class TestCompareVersions:
    def test_less(self):
        assert cu.compare_versions("2.1.155", "2.1.156") == -1

    def test_greater(self):
        assert cu.compare_versions("2.2.0", "2.1.156") == 1

    def test_equal(self):
        assert cu.compare_versions("2.1.156", "2.1.156") == 0

    def test_zero_pad_shorter(self):
        assert cu.compare_versions("2.1", "2.1.0") == 0
        assert cu.compare_versions("2.1", "2.1.1") == -1

    def test_major_dominates(self):
        assert cu.compare_versions("3.0.0", "2.9.9") == 1

    def test_nonnumeric_component_treated_as_zero(self):
        # Should not raise; "x" → 0
        assert cu.compare_versions("2.1.x", "2.1.0") == 0


class TestSliceChangelog:
    SAMPLE = """\
# Changelog

## 2.1.156
- new flag --foo
- fixed bar

## 2.1.155
- something

## 2.1.154
- old stuff
"""

    def test_keeps_only_newer_than_current(self):
        out = cu.slice_changelog(self.SAMPLE, "2.1.155")
        assert "2.1.156" in out
        assert "new flag --foo" in out
        # current and older versions are dropped
        assert "2.1.154" not in out
        assert "old stuff" not in out

    def test_current_is_latest_returns_minimal(self):
        out = cu.slice_changelog(self.SAMPLE, "2.1.156")
        # nothing strictly newer → header only (no version sections)
        assert "2.1.156\n- new flag" not in out

    def test_current_newer_than_all_returns_minimal(self):
        # Degenerate: installed version newer than everything listed → no
        # entry is strictly newer, so no version section survives.
        out = cu.slice_changelog(self.SAMPLE, "9.9.9")
        assert "2.1.156" not in out
        assert "new flag --foo" not in out

    def test_no_preamble_all_old_falls_back_to_full(self):
        # Changelog starts at a heading <= current with no preamble → kept is
        # empty after the immediate break → fall back to the full (capped) text
        # so the analyzer still gets context.
        cl = "## 1.0.0\n- old\n"
        out = cu.slice_changelog(cl, "2.0.0")
        assert "1.0.0" in out

    def test_max_chars_cap(self):
        big = "## 9.9.9\n" + ("x" * 50_000)
        out = cu.slice_changelog(big, "1.0.0", max_chars=1_000)
        assert len(out) <= 1_000

    def test_empty(self):
        assert cu.slice_changelog("", "2.1.0") == ""


class TestBuildAnalysisPrompt:
    def test_contains_versions_and_usage(self):
        p = cu.build_analysis_prompt("2.1.155", "2.1.156", "## 2.1.156\n- foo")
        assert "2.1.155" in p and "2.1.156" in p
        # the cockpit-usage block must be embedded so the model knows what we use
        assert "--append-system-prompt-file" in p
        assert "--resume" in p
        # changelog content embedded
        assert "foo" in p
        # asks for Thai output structure
        assert "ภาษาไทย" in p

    def test_asks_for_machine_verdict_block(self):
        p = cu.build_analysis_prompt("2.1.155", "2.1.156", "x")
        assert "<<<TAKKUB" in p and ">>>" in p
        assert "ACTION_REQUIRED" in p and "ISSUE_TITLE" in p


class TestParseVerdict:
    REPORT_YES = (
        "## ⚠️ กระทบ\n- --resume เปลี่ยน\n\n"
        "คำแนะนำ: อัพเดตได้แต่ระวัง\n\n"
        "<<<TAKKUB\nACTION_REQUIRED: yes\nSEVERITY: high\nISSUE_TITLE: ปรับ --resume ให้เข้ากับ flag ใหม่\n>>>"
    )
    REPORT_NO = (
        "## ✅ ปลอดภัย\n— ไม่มี\n\n<<<TAKKUB\nACTION_REQUIRED: no\nSEVERITY: low\nISSUE_TITLE: -\n>>>"
    )

    def test_yes(self):
        required, sev, title = cu.parse_verdict(self.REPORT_YES)
        assert required is True
        assert sev == "high"
        assert title == "ปรับ --resume ให้เข้ากับ flag ใหม่"

    def test_no(self):
        required, sev, title = cu.parse_verdict(self.REPORT_NO)
        assert required is False
        assert sev == "low"
        assert title is None  # "-" → None

    def test_missing_block_is_conservative(self):
        required, sev, title = cu.parse_verdict("just some prose, no block")
        assert required is False
        assert sev == "med"
        assert title is None

    def test_empty(self):
        assert cu.parse_verdict("") == (False, "med", None)


class TestBuildIssueTitleAndBody:
    def test_title_prefers_suggestion_with_version(self):
        t = cu.build_issue_title("2.1.155", "2.1.156", "ปรับ flag X")
        assert "2.1.155" in t and "2.1.156" in t and "ปรับ flag X" in t

    def test_title_default_when_no_suggestion(self):
        t = cu.build_issue_title("2.1.155", "2.1.156", None)
        assert "v2.1.155 → v2.1.156" in t

    def test_title_no_double_version_when_suggestion_has_it(self):
        # suggestion already carries the range → don't prepend again
        sug = "Claude CLI v2.1.155 → v2.1.156: do stuff"
        t = cu.build_issue_title("2.1.155", "2.1.156", sug)
        assert t == sug

    def test_body_strips_verdict_block(self):
        body = cu.build_issue_body("2.1.155", "2.1.156", TestParseVerdict.REPORT_YES)
        assert "<<<TAKKUB" not in body  # machine block removed
        assert "ACTION_REQUIRED" not in body
        assert "--resume เปลี่ยน" in body  # human content kept
        assert "v2.1.156" in body  # provenance header


class TestNpmRegistry:
    def test_default_and_env_override(self, monkeypatch):
        monkeypatch.delenv("TAKKUB_NPM_REGISTRY", raising=False)
        assert cu.npm_registry() == "https://registry.npmjs.org/"

        monkeypatch.setenv("TAKKUB_NPM_REGISTRY", "https://npm.corp.example/repository/npm/")
        assert cu.npm_registry() == "https://npm.corp.example/repository/npm/"

    def test_latest_version_passes_registry(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.delenv("TAKKUB_NPM_REGISTRY", raising=False)
        monkeypatch.setattr(cu, "_npm", lambda: "npm")

        def fake_run(argv, timeout):
            seen.extend(argv)
            return subprocess.CompletedProcess(argv, 0, "2.1.156\n", "")

        monkeypatch.setattr(cu, "_run", fake_run)
        assert cu.latest_version() == (True, "2.1.156")
        assert seen[-2:] == ["--registry", "https://registry.npmjs.org/"]

    def test_apply_update_passes_override_registry(self, monkeypatch):
        seen: list[str] = []
        mirror = "https://npm.corp.example/repository/npm/"
        monkeypatch.setenv("TAKKUB_NPM_REGISTRY", mirror)
        monkeypatch.setattr(cu, "_npm", lambda: "npm")

        def fake_run(argv, timeout):
            seen.extend(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(cu, "_run", fake_run)
        assert cu.apply_update() == (True, "claude CLI updated")
        assert seen[-2:] == ["--registry", mirror]


class TestBuildUpdaterScript:
    def test_windows_script(self):
        s = cu.build_updater_script(
            npm="C:/n/npm.cmd",
            python_exe="C:/p/python.exe",
            repo_root="C:/repo",
            log_path="C:/repo/runtime/u.log",
            is_windows=True,
            cockpit_pid=4242,
        )
        # Polls the cockpit pid to exit (not a blind sleep) before install.
        assert "Get-Process -Id 4242" in s
        assert f"{cu.PACKAGE}@latest" in s
        assert "--registry" in s
        assert "C:/n/npm.cmd" in s
        # captures exit code + failure sentinel
        assert "$LASTEXITCODE" in s
        assert "C:/repo/runtime/u.log.failed" in s
        # relaunch
        assert "agent_takkub" in s and "C:/p/python.exe" in s

    def test_posix_script(self):
        s = cu.build_updater_script(
            npm="/usr/bin/npm",
            python_exe="/usr/bin/python3",
            repo_root="/home/u/repo",
            log_path="/home/u/repo/runtime/u.log",
            is_windows=False,
            cockpit_pid=4242,
        )
        assert s.startswith("#!/bin/sh")
        assert "pid=4242" in s
        assert 'kill -0 "$pid"' in s  # polls for exit instead of a blind sleep
        assert "/home/u/repo/runtime/u.log.failed" in s  # failure sentinel
        assert f"{cu.PACKAGE}@latest" in s
        assert "--registry" in s
        assert "agent_takkub" in s

    def test_registry_override_is_embedded_in_both_scripts(self, monkeypatch):
        mirror = "https://npm.corp.example/repository/npm/"
        monkeypatch.setenv("TAKKUB_NPM_REGISTRY", mirror)
        for is_win in (True, False):
            script = cu.build_updater_script("npm", "py", "/r", "/r/log", is_win, 4242)
            assert "--registry" in script
            assert mirror in script

    def test_install_waits_before_running(self):
        # The pid-wait MUST come before the npm install line in both variants
        # (the whole point: let claude processes die + release locks first).
        for is_win in (True, False):
            s = cu.build_updater_script("npm", "py", "/r", "/r/log", is_win, 4242)
            wait_tok = "Get-Process -Id" if is_win else "kill -0"
            assert s.index(wait_tok) < s.index("install -g")
