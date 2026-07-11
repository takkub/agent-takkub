"""Tests for the rtk install helper.

Post central-home migration (A3): rtk is a PERSONAL, central toggle — a flag
file under ``SETTINGS_HOME`` — not a per-project ``.claude/settings.json``
hook. The hook itself is injected at spawn time via ``hook_wiring`` /
``--settings``. `install_rtk` flips the flag + scrubs any legacy per-project
entry; `uninstall_rtk` removes that legacy entry.

Every test that touches the enable flag monkeypatches ``config.SETTINGS_HOME``
to a tmp dir so it never reads/writes the developer's real ``~/.takkub``.
Install-path tests also stub ``rtk_binary_available`` so the suite works on
machines where the user hasn't yet downloaded rtk.exe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import config, rtk_helper


@pytest.fixture(autouse=True)
def _isolate_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the central flag file at a throwaway SETTINGS_HOME."""
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path / "settings-home")


def _write_project_rtk(project_root: Path, extra: dict | None = None) -> Path:
    """Write a legacy project settings.json carrying the rtk Bash hook (plus
    any `extra` top-level keys) and return the file path."""
    claude = project_root / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "rtk hook claude"}],
                }
            ]
        }
    }
    if extra:
        data.update(extra)
    path = claude / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestEnableFlag:
    def test_disabled_by_default(self) -> None:
        assert rtk_helper.rtk_hook_enabled() is False
        assert rtk_helper.is_rtk_installed() is False
        # `project_root` arg accepted but ignored (state is central now).
        assert rtk_helper.is_rtk_installed(Path("/whatever")) is False

    def test_set_and_read(self) -> None:
        rtk_helper.set_rtk_enabled(True)
        assert rtk_helper.rtk_hook_enabled() is True
        assert rtk_helper.is_rtk_installed() is True
        rtk_helper.set_rtk_enabled(False)
        assert rtk_helper.rtk_hook_enabled() is False

    def test_tolerates_malformed_flag(self) -> None:
        path = rtk_helper._enabled_flag_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert rtk_helper.rtk_hook_enabled() is False


class TestShouldInject:
    def test_requires_enabled_and_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        assert rtk_helper.rtk_should_inject() is False  # not enabled yet
        rtk_helper.set_rtk_enabled(True)
        assert rtk_helper.rtk_should_inject() is True

    def test_false_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rtk_helper.set_rtk_enabled(True)
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        assert rtk_helper.rtk_should_inject() is False

    def test_hook_fragment_shape(self) -> None:
        frag = rtk_helper.rtk_hook_fragment()
        assert frag["matcher"] == "Bash"
        assert frag["hooks"][0]["command"] == "rtk hook claude"
        # Fresh dict each call — a caller can't mutate shared state.
        assert rtk_helper.rtk_hook_fragment() is not frag


class TestInstall:
    @pytest.fixture(autouse=True)
    def _stub_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)

    def test_enables_central_flag(self, tmp_path: Path) -> None:
        ok, msg = rtk_helper.install_rtk()
        assert ok, msg
        assert rtk_helper.rtk_hook_enabled() is True
        # No project file is written when no project_root is given.

    def test_cleans_up_legacy_project_hook(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        path = _write_project_rtk(project, extra={"permissions": {"allow": ["Bash(ls *)"]}})

        ok, _ = rtk_helper.install_rtk(project)
        assert ok
        assert rtk_helper.rtk_hook_enabled() is True

        data = json.loads(path.read_text(encoding="utf-8"))
        # User's own key preserved…
        assert data["permissions"]["allow"] == ["Bash(ls *)"]
        # …but the rtk hook is gone (and empty containers pruned).
        assert "hooks" not in data

    def test_refuses_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        ok, msg = rtk_helper.install_rtk()
        assert not ok
        assert "not on path" in msg.lower()
        assert rtk_helper.rtk_hook_enabled() is False


class TestUninstall:
    def test_removes_rtk_keeps_other_keys(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        other_hook = {"matcher": "Read", "hooks": [{"type": "command", "command": "echo r"}]}
        path = _write_project_rtk(project, extra={"permissions": {"deny": ["x"]}})
        # Add a second matcher so we verify only the rtk entry is scrubbed.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["hooks"]["PreToolUse"].append(other_hook)
        path.write_text(json.dumps(data), encoding="utf-8")

        ok, msg = rtk_helper.uninstall_rtk(project)
        assert ok, msg

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] == ["x"]
        matchers = [e["matcher"] for e in data["hooks"]["PreToolUse"]]
        assert matchers == ["Read"]  # Bash/rtk entry removed, Read kept

    def test_keeps_coexisting_bash_hook(self, tmp_path: Path) -> None:
        """A Bash matcher that also holds a non-rtk hook keeps that hook."""
        project = tmp_path / "proj"
        claude = project / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {"type": "command", "command": "node guard.mjs"},
                                    {"type": "command", "command": "rtk hook claude"},
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        ok, _ = rtk_helper.uninstall_rtk(project)
        assert ok
        data = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        bash = next(e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
        cmds = [h["command"] for h in bash["hooks"]]
        assert cmds == ["node guard.mjs"]

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        ok, msg = rtk_helper.uninstall_rtk(tmp_path / "nope")
        assert ok
        assert "nothing to clean" in msg.lower()

    def test_noop_when_no_rtk_hook(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        claude = project / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(
            json.dumps({"permissions": {"allow": []}}), encoding="utf-8"
        )
        ok, msg = rtk_helper.uninstall_rtk(project)
        assert ok
        assert "nothing to clean" in msg.lower()

    def test_leaves_malformed_untouched(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        claude = project / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text("{bad json", encoding="utf-8")
        ok, msg = rtk_helper.uninstall_rtk(project)
        assert not ok
        assert "malformed" in msg.lower()


class TestFindRtkBinaryCache:
    """find_rtk_binary() runs on the Qt main thread per pane spawn; it caches
    a found path (re-validated) so repeated spawns don't re-scan PATH."""

    def test_caches_positive_result_no_rescan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", None, raising=False)
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        calls = {"n": 0}

        def fake_which(name: str):
            calls["n"] += 1
            return str(real) if name == "rtk" else None

        monkeypatch.setattr(rtk_helper, "which", fake_which)

        assert rtk_helper.find_rtk_binary() == str(real)
        first = calls["n"]
        assert first >= 1
        # Second call must hit the cache — `which` not invoked again.
        assert rtk_helper.find_rtk_binary() == str(real)
        assert calls["n"] == first

    def test_does_not_cache_negative_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", None, raising=False)
        monkeypatch.setattr(rtk_helper, "_FALLBACK_RTK_PATHS", [], raising=False)
        monkeypatch.setattr(rtk_helper, "which", lambda name: None)
        assert rtk_helper.find_rtk_binary() is None
        # A later install is picked up (no negative caching).
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr(rtk_helper, "which", lambda name: str(real) if name == "rtk" else None)
        assert rtk_helper.find_rtk_binary() == str(real)

    def test_revalidates_stale_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cached path that no longer exists must be re-resolved, not returned.
        stale = tmp_path / "gone.exe"
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", str(stale), raising=False)
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr(rtk_helper, "which", lambda name: str(real) if name == "rtk" else None)
        assert rtk_helper.find_rtk_binary() == str(real)
