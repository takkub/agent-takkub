"""Tests for the rtk install helper.

We patch `rtk_binary_available` in tests that exercise the install path so
the suite works on machines where the user hasn't yet downloaded rtk.exe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import rtk_helper


class TestIsInstalled:
    def test_returns_false_when_no_settings_dir(self, tmp_path: Path) -> None:
        assert rtk_helper.is_rtk_installed(tmp_path) is False

    def test_returns_false_for_none(self) -> None:
        assert rtk_helper.is_rtk_installed(None) is False

    def test_returns_false_when_settings_missing_rtk_hook(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": []}]}}),
            encoding="utf-8",
        )
        assert rtk_helper.is_rtk_installed(tmp_path) is False

    def test_returns_true_when_rtk_hook_present(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "rtk hook claude"}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        assert rtk_helper.is_rtk_installed(tmp_path) is True

    def test_tolerates_flag_variants_in_hook_command(self, tmp_path: Path) -> None:
        """A hook command like `rtk hook claude --ultra-compact` should still
        be recognised — we match on a substring marker, not exact equality."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "rtk hook claude --ultra-compact",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        assert rtk_helper.is_rtk_installed(tmp_path) is True

    def test_returns_false_on_malformed_json(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{not valid json", encoding="utf-8")
        assert rtk_helper.is_rtk_installed(tmp_path) is False


class TestInstall:
    @pytest.fixture(autouse=True)
    def _stub_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pretend rtk is on PATH for every install test."""
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)

    def test_creates_settings_file_from_scratch(self, tmp_path: Path) -> None:
        ok, msg = rtk_helper.install_rtk(tmp_path)
        assert ok, msg

        path = tmp_path / ".claude" / "settings.json"
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        bash_entries = [e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
        assert len(bash_entries) == 1
        assert bash_entries[0]["hooks"][0]["command"] == "rtk hook claude"

    def test_merges_into_existing_settings(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        existing = {
            "permissions": {"allow": ["Bash(ls *)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [{"type": "command", "command": "echo r"}]}
                ]
            },
        }
        (claude / "settings.json").write_text(json.dumps(existing), encoding="utf-8")

        ok, _ = rtk_helper.install_rtk(tmp_path)
        assert ok

        data = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        # Existing permissions section untouched
        assert data["permissions"]["allow"] == ["Bash(ls *)"]
        # Existing Read matcher untouched
        matchers = [e["matcher"] for e in data["hooks"]["PreToolUse"]]
        assert "Read" in matchers and "Bash" in matchers
        # rtk hook was appended to a Bash matcher
        bash_entry = next(e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
        assert any("rtk hook claude" in h["command"] for h in bash_entry["hooks"])

    def test_idempotent_when_already_installed(self, tmp_path: Path) -> None:
        rtk_helper.install_rtk(tmp_path)
        # Capture the file's first-install state
        path = tmp_path / ".claude" / "settings.json"
        before = path.read_text(encoding="utf-8")

        ok, msg = rtk_helper.install_rtk(tmp_path)
        assert ok
        assert "already" in msg.lower()
        # File unchanged on second install.
        assert path.read_text(encoding="utf-8") == before

    def test_appends_rtk_to_existing_bash_matcher(self, tmp_path: Path) -> None:
        """A Bash matcher may already exist (e.g. cam-worker-guard.mjs).
        rtk should slot in alongside, not replace it."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "node guard.mjs"}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        ok, _ = rtk_helper.install_rtk(tmp_path)
        assert ok

        data = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        bash_entry = next(e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
        commands = [h["command"] for h in bash_entry["hooks"]]
        assert "node guard.mjs" in commands
        assert any("rtk hook claude" in c for c in commands)

    def test_refuses_malformed_existing_settings(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{bad json", encoding="utf-8")

        ok, msg = rtk_helper.install_rtk(tmp_path)
        assert not ok
        assert "malformed" in msg.lower()


class TestBinaryGuard:
    def test_refuses_install_when_rtk_not_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        ok, msg = rtk_helper.install_rtk(tmp_path)
        assert not ok
        assert "not on path" in msg.lower()

    def test_refuses_install_when_project_root_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        bogus = tmp_path / "does-not-exist"
        ok, msg = rtk_helper.install_rtk(bogus)
        assert not ok
        assert "not a directory" in msg.lower()
