"""Unit tests for the autoskills CLI bridge. All subprocess calls are mocked
— no network, no real npx/autoskills invocation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_takkub import autoskills_installer as ai

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "autoskills"

# Real `autoskills@0.3.6` `--dry-run --agent claude-code` output (numbered
# listing, box-drawing headers) — not the "key:" + bullet format this module
# used to assume. See tests/fixtures/autoskills/*.txt for the live-captured
# transcripts this mirrors (docs/audit/2026-08-13-autoskills-installer.md).
SAMPLE_OUTPUT = """Auto-install the best AI skills for your project · v0.3.6
   Scanning project...[K   ◆ Detected technologies:
     ✔ Next.js   ✔ TypeScript      ✔ Tailwind CSS
   ◆ Skills to install (3)
    1. vercel › nextjs-best-practices                   ← Next.js
    2. someorg › typescript-strict                       ← TypeScript
    3. someorg › tailwind-conventions
   Agents: claude-code
   --dry-run: nothing was installed.
"""


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["autoskills"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _no_staging():
    """Patch that forces install() onto the direct/fallback path, so tests
    can exercise the original in-place behavior deterministically."""
    return patch.object(ai, "_build_staging_mirror", return_value=None)


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
    assert by_name["nextjs-best-practices"] == "vercel › Next.js"
    assert by_name["tailwind-conventions"] == "someorg"
    assert result.no_skills_for_stack is False
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
# install() — top-level dispatch / guard clauses (staging-path irrelevant)
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


# ---------------------------------------------------------------------------
# install() — direct/fallback path (staging forced off)
# ---------------------------------------------------------------------------


def test_install_direct_writes_only_selected_and_removes_the_rest(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"
    (skills_dir / "existing-skill").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        (skills_dir / "skill-a").mkdir(parents=True)
        (skills_dir / "skill-b").mkdir(parents=True)
        return _completed(stdout="ok")

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.staging_used is False
    assert result.written == ["skill-a"]
    assert result.skipped == ["skill-b"]
    assert (skills_dir / "skill-a").is_dir()
    assert not (skills_dir / "skill-b").exists()
    assert (skills_dir / "existing-skill").is_dir()  # untouched pre-existing entry
    assert result.overwritten == []
    assert result.overwrite_failed == []


def test_install_direct_full_cmd_uses_yes_flag(tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        ai.install(tmp_path, ["skill-a"])

    assert captured["cmd"] == ["autoskills", "--yes", "--agent", "claude-code"]


def test_install_direct_timeout(tmp_path):
    with (
        _no_staging(),
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


def test_install_direct_nonzero_exit_writes_nothing(tmp_path):
    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(returncode=2, stderr="fail")),
    ):
        result = ai.install(tmp_path, ["skill-a"])
    assert result.ok is False
    assert result.written == []
    assert result.raw_output == "fail"


def test_install_direct_rolls_back_on_path_escape(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"

    def fake_run(cmd, **kwargs):
        (skills_dir / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
        patch.object(ai, "_escaped_entries", return_value={"skill-a"}),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is False
    assert "นอก .claude/skills" in result.error
    assert not (skills_dir / "skill-a").exists()  # rolled back


def test_install_direct_missing_binary_raises_oserror(tmp_path):
    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=FileNotFoundError("no such file")),
    ):
        result = ai.install(tmp_path, ["skill-a"])
    assert result.ok is False
    assert result.error


def test_install_direct_selected_collision_reports_overwritten_not_restored(tmp_path):
    """A pre-existing entry whose NAME the user selected gets legitimately
    replaced — reported via `overwritten`, never restored (that would defeat
    the user's own selection)."""
    skills_dir = tmp_path / ".claude" / "skills"
    existing = skills_dir / "skill-a"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("old content", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        shutil_mod = ai.shutil
        shutil_mod.rmtree(existing)
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("new content", encoding="utf-8")
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.overwritten == ["skill-a"]
    assert result.overwrite_failed == []
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "new content"


def test_install_direct_unselected_collision_is_restored(tmp_path):
    """The core bug this fix closes: autoskills' non-filtered install
    rewrites a pre-existing entry the user never picked. Same name in
    before/after means the old before/after-diff-of-names logic couldn't
    see it at all. Now it's detected via content signature and restored."""
    skills_dir = tmp_path / ".claude" / "skills"
    existing = skills_dir / "existing-skill"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("original content", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        ai.shutil.rmtree(existing)
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("clobbered by autoskills", encoding="utf-8")
        (skills_dir / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.written == ["skill-a"]
    assert result.overwritten == ["existing-skill"]
    assert result.overwrite_failed == []
    # Restored to the original content — never silently left overwritten.
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "original content"


def test_install_direct_unselected_collision_reports_failure_when_restore_fails(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"
    existing = skills_dir / "existing-skill"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("original", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        ai.shutil.rmtree(existing)
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("clobbered", encoding="utf-8")
        (skills_dir / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
        patch.object(ai, "_restore_entry", return_value=False),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is False
    assert result.overwrite_failed == ["existing-skill"]
    assert "กู้คืนไม่ได้" in result.error


def test_install_direct_backup_failure_still_detects_and_reports(tmp_path):
    """Even when the pre-run backup couldn't be taken at all (e.g. disk/perm
    issue), the overwrite must still surface as `overwrite_failed`, never
    silently disappear."""
    skills_dir = tmp_path / ".claude" / "skills"
    existing = skills_dir / "existing-skill"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("original", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        ai.shutil.rmtree(existing)
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("clobbered", encoding="utf-8")
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
        patch.object(ai, "_backup_entry", return_value=None),
    ):
        result = ai.install(tmp_path, ["some-other-skill"])

    assert result.ok is False
    assert result.overwrite_failed == ["existing-skill"]


# ---------------------------------------------------------------------------
# install() — staging path (default)
# ---------------------------------------------------------------------------


def test_install_staging_used_by_default_and_cwd_is_the_mirror(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cwd"] = kwargs["cwd"]
        staged_skills = Path(kwargs["cwd"]) / ".claude" / "skills"
        (staged_skills / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.staging_used is True
    assert captured["cwd"] != str(tmp_path)
    assert Path(captured["cwd"]).parent == tmp_path  # staging lives inside project_root
    assert (tmp_path / ".claude" / "skills" / "skill-a").is_dir()
    # Staging dir is torn down afterward — nothing left behind under project_root.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(ai._STAGING_PREFIX)]
    assert leftovers == []


def test_install_staging_unselected_entries_never_touch_real_project(tmp_path):
    def fake_run(cmd, **kwargs):
        staged_skills = Path(kwargs["cwd"]) / ".claude" / "skills"
        (staged_skills / "skill-a").mkdir(parents=True)
        (staged_skills / "skill-b").mkdir(parents=True)
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.written == ["skill-a"]
    assert result.skipped == ["skill-b"]
    real_skills = tmp_path / ".claude" / "skills"
    assert (real_skills / "skill-a").is_dir()
    assert not (real_skills / "skill-b").exists()


def test_install_staging_selected_collision_reports_overwritten(tmp_path):
    real_skills = tmp_path / ".claude" / "skills"
    existing = real_skills / "skill-a"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("old", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        staged_skills = Path(kwargs["cwd"]) / ".claude" / "skills"
        staged_entry = staged_skills / "skill-a"
        staged_entry.mkdir(parents=True)
        (staged_entry / "SKILL.md").write_text("new", encoding="utf-8")
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.overwritten == ["skill-a"]
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "new"


def test_install_staging_rolls_back_on_path_escape(tmp_path):
    def fake_run(cmd, **kwargs):
        staged_skills = Path(kwargs["cwd"]) / ".claude" / "skills"
        (staged_skills / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
        patch.object(ai, "_escaped_entries", return_value={"skill-a"}),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is False
    assert "นอก .claude/skills" in result.error
    assert not (tmp_path / ".claude" / "skills" / "skill-a").exists()


def test_install_falls_back_to_direct_when_staging_unavailable(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"

    def fake_run(cmd, **kwargs):
        assert kwargs["cwd"] == str(tmp_path)
        (skills_dir / "skill-a").mkdir(parents=True)
        return _completed()

    with (
        _no_staging(),
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", side_effect=fake_run),
    ):
        result = ai.install(tmp_path, ["skill-a"])

    assert result.ok is True
    assert result.staging_used is False
    assert (skills_dir / "skill-a").is_dir()


# ---------------------------------------------------------------------------
# _build_staging_mirror
# ---------------------------------------------------------------------------


def test_build_staging_mirror_hardlinks_files_and_excludes_git_and_skills(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export {}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    skills_dir = tmp_path / ".claude" / "skills" / "pre-existing"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("pre-existing content", encoding="utf-8")

    staging = ai._build_staging_mirror(tmp_path)
    assert staging is not None
    try:
        mirrored = staging / "src" / "index.ts"
        assert mirrored.is_file()
        assert mirrored.stat().st_ino == (tmp_path / "src" / "index.ts").stat().st_ino
        assert not (staging / ".git").exists()
        assert (staging / ".claude" / "skills").is_dir()
        assert list((staging / ".claude" / "skills").iterdir()) == []
    finally:
        ai.shutil.rmtree(staging, ignore_errors=True)


def test_build_staging_mirror_none_on_root_creation_failure(tmp_path):
    with patch.object(ai.tempfile, "mkdtemp", side_effect=OSError("nope")):
        assert ai._build_staging_mirror(tmp_path) is None


def test_build_staging_mirror_copies_manifest_files_instead_of_hardlinking(tmp_path):
    """A hardlinked manifest shares the real file's inode — an in-place
    rewrite of the staged copy (e.g. a stack-detection tool normalizing a
    lockfile) would silently corrupt the real project's file too. Manifest
    files must be real, independent copies."""
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")

    staging = ai._build_staging_mirror(tmp_path)
    assert staging is not None
    try:
        for name in (
            "package.json",
            "pyproject.toml",
            "tsconfig.json",
            "package-lock.json",
            ".env",
        ):
            mirrored = staging / name
            assert mirrored.is_file()
            assert mirrored.stat().st_ino != (tmp_path / name).stat().st_ino
            assert mirrored.read_bytes() == (tmp_path / name).read_bytes()
    finally:
        ai.shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# _entry_signature — content fingerprint used for overwrite detection
# ---------------------------------------------------------------------------


def test_entry_signature_file_changes_when_content_changes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one", encoding="utf-8")
    sig1 = ai._entry_signature(f)
    f.write_text("two", encoding="utf-8")
    sig2 = ai._entry_signature(f)
    assert sig1 != sig2


def test_entry_signature_dir_stable_when_untouched(tmp_path):
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("content", encoding="utf-8")
    assert ai._entry_signature(d) == ai._entry_signature(d)


def test_entry_signature_dir_changes_on_nested_file_change(tmp_path):
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("content", encoding="utf-8")
    sig1 = ai._entry_signature(d)
    (d / "SKILL.md").write_text("different content", encoding="utf-8")
    sig2 = ai._entry_signature(d)
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# _escaped_entries — path-escape guard, tested directly (no real symlinks
# needed for the top-level case, so this passes without elevated privileges
# on Windows)
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


def test_escaped_entries_nested_symlink_outside_project_is_flagged(tmp_path):
    """A top-level entry that is a REAL directory (not itself a symlink) can
    still escape via a symlink nested somewhere inside it — this must be
    caught too, not just the top-level entry check."""
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_dir = tmp_path / "project" / ".claude" / "skills"
    nested = skills_dir / "skill-a" / "assets"
    nested.mkdir(parents=True)
    link = nested / "evil"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest

        pytest.skip("symlink creation not permitted in this environment")
    assert ai._escaped_entries(skills_dir, {"skill-a"}) == {"skill-a"}


def test_escaped_entries_nested_dir_with_no_symlinks_is_safe(tmp_path):
    skills_dir = tmp_path / ".claude" / "skills"
    nested = skills_dir / "skill-a" / "assets"
    nested.mkdir(parents=True)
    (nested / "notes.md").write_text("fine", encoding="utf-8")
    assert ai._escaped_entries(skills_dir, {"skill-a"}) == set()


# ---------------------------------------------------------------------------
# _parse_preview_output — parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_preview_output_unrecognized_format_yields_empty_no_crash():
    raw = "Some banner text\nnothing resembling a skill listing here\n"
    stack, skills = ai._parse_preview_output(raw)
    assert stack == []
    assert skills == []


def test_parse_preview_output_real_install_completion_fallback():
    """Belt-and-suspenders: if a --dry-run run somehow still reaches the
    real-install completion renderer ("✔ org/repo/skill"), names are still
    recovered instead of silently parsing to zero skills."""
    raw = (
        "   ◆ Installing skills...\n"
        "   Agents: universal, claude-code\n"
        "   ✔ inferen-sh/skills/python-executor\n"
        "   ✔ wshobson/agents/bash-defensive-patterns\n"
        "   ✔ Done! 2 skills installed in 20ms.\n"
    )
    _stack, skills = ai._parse_preview_output(raw)
    names = {s.name for s in skills}
    assert names == {"python-executor", "bash-defensive-patterns"}
    by_name = {s.name: s.source for s in skills}
    assert by_name["python-executor"] == "inferen-sh/skills/python-executor"


def test_no_skills_reported_true_for_genuine_negative():
    assert ai._no_skills_reported("No skills available for your stack yet.") is True


def test_no_skills_reported_false_for_unrelated_text():
    assert ai._no_skills_reported("Some banner text with nothing relevant") is False


def test_parse_preview_output_empty_string():
    stack, skills = ai._parse_preview_output("")
    assert stack == []
    assert skills == []


def test_parse_preview_output_captures_arbitrary_annotation_not_just_security():
    """The parser must not hardcode against "security check" text — any
    parenthetical annotation the CLI emits has to be captured verbatim."""
    raw = (
        "   ◆ Skills to install (1)\n"
        "    1. someorg › experimental-skill (beta, unmaintained) ← Rust\n"
    )
    _stack, skills = ai._parse_preview_output(raw)
    assert len(skills) == 1
    assert skills[0].notes == "beta, unmaintained"


def test_parse_preview_output_no_annotation_yields_empty_notes():
    stack, skills = ai._parse_preview_output(SAMPLE_OUTPUT)
    assert stack == ["Next.js", "TypeScript", "Tailwind CSS"]
    assert all(s.notes == "" for s in skills)


# ---------------------------------------------------------------------------
# _parse_preview_output — live-captured real CLI transcripts
# (tests/fixtures/autoskills/*.txt — `npx autoskills@0.3.6 --dry-run --agent
# claude-code`, captured 2026-08-13; see docs/audit/2026-08-13-autoskills-installer.md)
# ---------------------------------------------------------------------------


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_real_fixture_multi_skill():
    raw = _fixture("dry_run_v0.3.6_multi_skill.txt")
    stack, skills = ai._parse_preview_output(raw)
    assert stack == ["Node.js", "Bash", "Python", "Pytest"]
    names = [s.name for s in skills]
    assert names == [
        "nodejs-backend-patterns",
        "nodejs-best-practices",
        "bash-defensive-patterns",
        "python-executor",
        "python-testing-patterns",
        "frontend-design",
        "accessibility",
        "seo",
    ]
    by_name = {s.name: s.source for s in skills}
    assert by_name["nodejs-backend-patterns"] == "wshobson › Node.js"
    # "(security check ⚠)" annotation must not leak into the name or source...
    assert by_name["python-executor"] == "inferen-sh › Python"
    assert by_name["python-testing-patterns"] == "wshobson › Python, Pytest"
    # ...but must not be silently dropped either — it's a CLI-issued warning
    # that has to reach the user, so it's captured separately on `notes`.
    notes_by_name = {s.name: s.notes for s in skills}
    assert notes_by_name["python-executor"] == "security check ⚠"
    assert notes_by_name["nodejs-backend-patterns"] == ""
    assert notes_by_name["python-testing-patterns"] == ""
    assert ai._no_skills_reported(raw) is False


def test_preview_end_to_end_real_fixture_multi_skill(tmp_path):
    """Same fixture, through the full preview() path (not just the parser)."""
    raw = _fixture("dry_run_v0.3.6_multi_skill.txt")
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(stdout=raw)),
    ):
        result = ai.preview(tmp_path)
    assert result.ok is True
    assert len(result.skills) == 8
    assert result.no_skills_for_stack is False


def test_parse_real_fixture_no_match_is_genuine_negative():
    raw = _fixture("dry_run_v0.3.6_no_match.txt")
    stack, skills = ai._parse_preview_output(raw)
    assert stack == ["Express"]
    assert skills == []
    assert ai._no_skills_reported(raw) is True


def test_preview_end_to_end_real_fixture_no_match_sets_negative_flag(tmp_path):
    raw = _fixture("dry_run_v0.3.6_no_match.txt")
    with (
        patch.object(ai, "_resolve_autoskills_cmd", return_value=["autoskills"]),
        patch.object(ai.subprocess, "run", return_value=_completed(stdout=raw)),
    ):
        result = ai.preview(tmp_path)
    assert result.ok is True
    assert result.skills == []
    assert result.no_skills_for_stack is True


def test_parse_real_fixture_single_line_stack():
    raw = _fixture("dry_run_v0.3.6_single_line_stack.txt")
    stack, skills = ai._parse_preview_output(raw)
    assert stack == ["Bash", "Python", "Pytest", "Express"]
    assert {s.name for s in skills} == {
        "bash-defensive-patterns",
        "python-executor",
        "python-testing-patterns",
    }


def test_parse_real_fixture_with_combos_ignores_combo_lines():
    raw = _fixture("dry_run_v0.3.6_with_combos.txt")
    stack, skills = ai._parse_preview_output(raw)
    assert stack == ["Node.js", "Bash", "Python", "Pytest", "Express"]
    # "⚡ Node.js + Express" combo line must not be treated as a stack entry
    assert "Node.js + Express" not in stack
    names = {s.name for s in skills}
    assert names == {
        "nodejs-backend-patterns",
        "nodejs-best-practices",
        "bash-defensive-patterns",
        "python-executor",
        "python-testing-patterns",
        "nodejs-express-server",
    }
    # last entry has no "← Tech" suffix in the real output — source falls
    # back to just the author
    by_name = {s.name: s.source for s in skills}
    assert by_name["nodejs-express-server"] == "aj-geddes"
