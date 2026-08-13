"""Unit tests for the autoskills CLI bridge. All subprocess calls are mocked
— no network, no real npx/autoskills invocation."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from agent_takkub import autoskills_installer as ai

SAMPLE_OUTPUT = """Detected stack: Next.js, TypeScript, Tailwind CSS

Skills to install:
  - nextjs-best-practices (https://skills.sh/nextjs-best-practices)
  - typescript-strict (https://skills.sh/typescript-strict)
  - tailwind-conventions
"""


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["autoskills"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# _resolve_autoskills_cmd
# ---------------------------------------------------------------------------


def test_resolve_prefers_direct_binary():
    with patch.object(
        ai.shutil,
        "which",
        side_effect=lambda name: "/bin/autoskills" if name == "autoskills" else None,
    ):
        assert ai._resolve_autoskills_cmd() == ["/bin/autoskills"]


def test_resolve_falls_back_to_npx():
    def which(name):
        return "/bin/npx" if name == "npx" else None

    with patch.object(ai.shutil, "which", side_effect=which):
        assert ai._resolve_autoskills_cmd() == ["/bin/npx", "--yes", "autoskills@latest"]


def test_resolve_prefers_windows_cmd_shims():
    def which(name):
        return {"autoskills.cmd": "C:\\npm\\autoskills.cmd"}.get(name)

    with patch.object(ai.shutil, "which", side_effect=which):
        assert ai._resolve_autoskills_cmd() == ["C:\\npm\\autoskills.cmd"]


def test_resolve_none_when_nothing_available():
    with patch.object(ai.shutil, "which", return_value=None):
        assert ai._resolve_autoskills_cmd() is None


# ---------------------------------------------------------------------------
# preview()
# ---------------------------------------------------------------------------


def test_preview_no_runtime_available(tmp_path):
    with patch.object(ai, "_resolve_autoskills_cmd", return_value=None):
        result = ai.preview(tmp_path)
    assert result.ok is False
    assert "npx" in result.error


def test_preview_runs_dry_run_only_no_yes_flag(tmp_path):
    """Preview must invoke exactly `--dry-run --agent claude-code` — never
    `--yes` — and must never write anything, so it's safe to call
    speculatively."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _completed(stdout=SAMPLE_OUTPUT)

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.preview(tmp_path)

    assert captured["cmd"] == ["autoskills", "--dry-run", "--agent", "claude-code"]
    assert "--yes" not in captured["cmd"]
    assert captured["kwargs"]["stdin"] == ai.subprocess.DEVNULL
    assert captured["kwargs"]["timeout"] == ai.PREVIEW_TIMEOUT_DEFAULT
    assert result.ok is True
    assert result.stack == ["Next.js", "TypeScript", "Tailwind CSS"]
    names = {s.name for s in result.skills}
    assert names == {"nextjs-best-practices", "typescript-strict", "tailwind-conventions"}
    by_name = {s.name: s.source for s in result.skills}
    assert by_name["nextjs-best-practices"] == "https://skills.sh/nextjs-best-practices"
    assert by_name["tailwind-conventions"] == ""
    assert result.raw_output == SAMPLE_OUTPUT


def test_preview_sets_non_interactive_env(tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        ai.preview(tmp_path)

    assert captured["env"]["npm_config_yes"] == "true"
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_preview_timeout(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(
            ai.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="autoskills", timeout=60),
        ),
    ):
        result = ai.preview(tmp_path, timeout=60)
    assert result.ok is False
    assert "หมดเวลา" in result.error


def test_preview_missing_binary_raises_oserror(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=FileNotFoundError("no such file")),
    ):
        result = ai.preview(tmp_path)
    assert result.ok is False
    assert result.error


def test_preview_nonzero_exit_preserves_raw_output(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(stderr="boom", returncode=1)),
    ):
        result = ai.preview(tmp_path)
    assert result.ok is False
    assert result.raw_output == "boom"
    assert "1" in result.error


def test_preview_empty_output_parses_to_empty_lists(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(stdout="")),
    ):
        result = ai.preview(tmp_path)
    assert result.ok is True
    assert result.stack == []
    assert result.skills == []


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def test_install_no_selection_is_a_noop_error(tmp_path):
    with patch.object(ai.subprocess, "run") as run:
        result = ai.install(tmp_path, [])
    run.assert_not_called()
    assert result.ok is False
    assert "เลือก" in result.error


def test_install_no_runtime_available(tmp_path):
    with patch.object(ai, "_resolve_autoskills_cmd", return_value=None):
        result = ai.install(tmp_path, ["foo"])
    assert result.ok is False
    assert "npx" in result.error


def test_install_writes_only_selected_and_removes_the_rest(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"
    (skills_dir / "existing-skill").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        # Simulate autoskills writing two new skills for this "project".
        (skills_dir / "skill-a").mkdir(parents=True)
        (skills_dir / "skill-b").mkdir(parents=True)
        return _completed(stdout="ok")

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.written == ["skill-a"]
    assert result.skipped == ["skill-b"]
    assert (skills_dir / "skill-a").is_dir()
    assert not (skills_dir / "skill-b").exists()
    assert (skills_dir / "existing-skill").is_dir()  # untouched pre-existing entry


def test_install_full_cmd_uses_yes_flag(tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        ai.install(tmp_path, ["skill-a"])

    assert captured["cmd"] == ["autoskills", "--yes", "--agent", "claude-code"]


def test_install_timeout(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(
            ai.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="autoskills", timeout=120),
        ),
    ):
        result = ai.install(tmp_path, ["skill-a"])
    assert result.ok is False
    assert "หมดเวลา" in result.error


def test_install_nonzero_exit_writes_nothing(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(returncode=2, stderr="fail")),
    ):
        result = ai.install(tmp_path, ["skill-a"])
    assert result.ok is False
    assert result.written == []
    assert result.raw_output == "fail"


def test_install_rolls_back_on_path_escape(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"

    def fake_run(cmd, **kwargs):
        (skills_dir / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
        patch.object(ai, "_escaped_entries", return_value={"skill-a"}),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is False
    assert "นอก .claude/skills" in result.error
    assert not (skills_dir / "skill-a").exists()  # rolled back


def test_install_missing_binary_raises_oserror(tmp_path):
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=FileNotFoundError("no such file")),
    ):
        result = ai.install(tmp_path, ["skill-a"])
    assert result.ok is False
    assert result.error


# ---------------------------------------------------------------------------
# _escaped_entries — path-escape guard, tested directly (no real symlinks
# needed, so this passes without elevated privileges on Windows)
# ---------------------------------------------------------------------------


def test_escaped_entries_normal_dir_is_not_escaped(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"
    (skills_dir / "ok-skill").mkdir(parents=True)
    assert ai._escaped_entries(skills_dir, {"ok-skill"}) == set()


def test_escaped_entries_symlink_outside_project_is_flagged(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_dir = tmp_path / "project" / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    link = skills_dir / "evil-skill"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest

        pytest.skip("symlink creation not permitted in this environment")
    assert ai._escaped_entries(skills_dir, {"evil-skill"}) == {"evil-skill"}


# ---------------------------------------------------------------------------
# _parse_preview_output — parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_preview_output_no_headers_falls_back_to_url_bullets():
    raw = "Some banner text\n- foo-skill (https://skills.sh/foo-skill)\n- unrelated bullet with no url\n"
    stack, skills = ai._parse_preview_output(raw)
    assert stack == []
    assert [s.name for s in skills] == ["foo-skill"]
    assert skills[0].source == "https://skills.sh/foo-skill"


def test_parse_preview_output_empty_string():
    stack, skills = ai._parse_preview_output("")
    assert stack == []
    assert skills == []
