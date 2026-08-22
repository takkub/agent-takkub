"""Tests for the `takkub release` ceremony (release.py).

String transforms are pure and fully covered here. release() is exercised
with do_commit/do_tag off (and dry_run) so no git is invoked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.release import (
    build_wheel,
    bump_version,
    changelog_has_entries,
    extract_release_notes,
    read_pyproject_version,
    release,
    roll_changelog,
    set_init_version,
    set_npm_version,
    set_pyproject_version,
)

_PYPROJECT = '[project]\nname = "agent-takkub"\nversion = "0.3.9"\nrequires-python = ">=3.11"\n'
_CHANGELOG = "# Changelog\n\n## [vNEXT]\n\n### Changed\n- did a thing\n\n(end)\n"

_INIT_PY = '"""doc."""\n\n__version__ = "0.3.9"\n'
_PACKAGE_JSON = '{\n  "name": "agent-takkub",\n  "version": "0.3.9",\n  "bin": {}\n}\n'


def _make_repo(tmp_path, changelog: str = _CHANGELOG):
    """Every file `release()` rewrites. The version lives in FOUR places and
    `tests/test_version_sync.py` asserts they agree — a fixture carrying only
    two of them is how release() shipped v1.0.80 with `__version__` left
    behind at 1.0.79."""
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    pkg = tmp_path / "src" / "agent_takkub"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_INIT_PY, encoding="utf-8")
    (tmp_path / "package.json").write_text(_PACKAGE_JSON, encoding="utf-8")
    return tmp_path


class TestBump:
    def test_patch(self):
        assert bump_version("0.3.9", "patch") == "0.3.10"

    def test_minor_resets_patch(self):
        assert bump_version("0.3.9", "minor") == "0.4.0"

    def test_major_resets_minor_patch(self):
        assert bump_version("0.3.9", "major") == "1.0.0"

    def test_bad_version(self):
        with pytest.raises(ValueError):
            bump_version("v0.3", "patch")

    def test_bad_part(self):
        with pytest.raises(ValueError):
            bump_version("0.3.9", "bogus")


class TestPyproject:
    def test_read(self):
        assert read_pyproject_version(_PYPROJECT) == "0.3.9"

    def test_set_only_first(self):
        out = set_pyproject_version(_PYPROJECT, "0.4.0")
        assert 'version = "0.4.0"' in out
        assert 'version = "0.3.9"' not in out
        # requires-python's >= must be untouched
        assert 'requires-python = ">=3.11"' in out

    def test_read_missing(self):
        with pytest.raises(ValueError):
            read_pyproject_version('[project]\nname = "x"\n')


class TestRollChangelog:
    def test_renames_vnext_and_adds_fresh(self):
        out = roll_changelog(_CHANGELOG, "0.4.0", "2026-05-31")
        # fresh empty vNEXT on top, dated version heading below
        assert "## [vNEXT]\n\n## [v0.4.0] - 2026-05-31" in out
        # the existing content now lives under the version
        assert out.index("## [v0.4.0]") < out.index("- did a thing")
        # exactly one vNEXT remains
        assert out.count("## [vNEXT]") == 1

    def test_no_vnext_raises(self):
        with pytest.raises(ValueError):
            roll_changelog("# Changelog\n\n## [v0.1.0]\n", "0.2.0", "2026-05-31")


class TestRelease:
    def _repo(self, tmp_path):
        _make_repo(tmp_path, _CHANGELOG)
        return tmp_path

    def test_dry_run_touches_nothing(self, tmp_path):
        repo = self._repo(tmp_path)
        res = release(repo, part="minor", dry_run=True)
        assert res["new_version"] == "0.4.0"
        assert res["tag"] == "v0.4.0"
        assert res["committed"] is False and res["tagged"] is False
        # files unchanged
        assert 'version = "0.3.9"' in (repo / "pyproject.toml").read_text(encoding="utf-8")

    def test_writes_files_without_git(self, tmp_path):
        repo = self._repo(tmp_path)
        res = release(repo, part="patch", do_commit=False, do_tag=False, today="2026-05-31")
        assert res["new_version"] == "0.3.10"
        assert 'version = "0.3.10"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
        cl = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [v0.3.10] - 2026-05-31" in cl
        assert cl.count("## [vNEXT]") == 1

    def test_explicit_version(self, tmp_path):
        repo = self._repo(tmp_path)
        res = release(repo, explicit_version="1.2.3", do_commit=False, do_tag=False)
        assert res["new_version"] == "1.2.3"
        assert res["tag"] == "v1.2.3"


class TestExtractReleaseNotes:
    _CL = (
        "# Changelog\n\n## [vNEXT]\n\n"
        "## [v0.5.1] - 2026-06-01\n\n### Fixed\n- แก้ issue routing\n\n"
        "## [v0.5.0] - 2026-06-01\n\n### Added\n- provider substitution\n\n"
        "## [0.3.8] — 2026-05-20\n\n- old un-prefixed heading\n"
    )

    def test_extracts_only_that_section(self):
        out = extract_release_notes(self._CL, "0.5.1")
        assert "แก้ issue routing" in out
        assert "provider substitution" not in out  # stops at next ## heading
        assert "## [v0.5.1]" not in out  # heading itself excluded

    def test_middle_section(self):
        out = extract_release_notes(self._CL, "0.5.0")
        assert "provider substitution" in out
        assert "old un-prefixed" not in out

    def test_un_prefixed_heading(self):
        # older headings have no leading 'v' — must still match
        out = extract_release_notes(self._CL, "0.3.8")
        assert "old un-prefixed heading" in out

    def test_missing_version_returns_empty(self):
        assert extract_release_notes(self._CL, "9.9.9") == ""


class TestReleaseGithubStep:
    def _repo(self, tmp_path):
        _make_repo(tmp_path, _CHANGELOG)
        return tmp_path

    def test_github_release_invoked_when_committed_and_tagged(self, tmp_path):
        """do_github_release=True + commit + tag → create_github_release is
        called with the tag and the rolled changelog section as notes."""
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),  # stub real git
            patch("agent_takkub.release.build_wheel"),  # stub `python -m build`
            patch(
                "agent_takkub.release.create_github_release", return_value=(True, "url://rel")
            ) as m,
        ):
            res = release(repo, part="minor", today="2026-05-31")
        assert m.called
        call = m.call_args
        assert call.args[1] == "v0.4.0"  # tag
        assert "did a thing" in call.args[3]  # notes = rolled section body
        assert res["github_released"] is True
        assert res["github_url"] == "url://rel"

    def test_no_github_release_flag_skips_publish(self, tmp_path):
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel"),
            patch("agent_takkub.release.create_github_release") as m,
        ):
            res = release(repo, part="minor", today="2026-05-31", do_github_release=False)
        assert not m.called
        assert res["github_released"] is False

    def test_publish_failure_does_not_raise(self, tmp_path):
        """A gh/network failure is recorded, not raised — local release stands."""
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel"),
            patch(
                "agent_takkub.release.create_github_release",
                return_value=(False, "gh CLI not found"),
            ),
        ):
            res = release(repo, part="minor", today="2026-05-31")
        assert res["tagged"] is True
        assert res["github_released"] is False
        assert "gh CLI not found" in res["github_error"]

    def test_github_step_skipped_without_tag(self, tmp_path):
        repo = self._repo(tmp_path)
        with patch("agent_takkub.release.create_github_release") as m:
            release(repo, part="minor", do_commit=False, do_tag=False, today="2026-05-31")
        assert not m.called  # no tag → nothing to publish


_EMPTY_CL = "# Changelog\n\n## [vNEXT]\n\n## [v0.3.8] - 2026-05-20\n\n- old\n"


class TestChangelogHasEntries:
    def test_has(self):
        assert changelog_has_entries(_CHANGELOG) is True

    def test_empty_vnext(self):
        assert changelog_has_entries(_EMPTY_CL) is False

    def test_no_vnext(self):
        assert changelog_has_entries("# Changelog\n\n## [v0.1.0]\n- x\n") is False


class TestGuards:
    def _repo(self, tmp_path, changelog=_CHANGELOG):
        _make_repo(tmp_path, changelog)
        return tmp_path

    def test_empty_vnext_blocks(self, tmp_path):
        repo = self._repo(tmp_path, _EMPTY_CL)
        with pytest.raises(ValueError, match="no changelog entries"):
            release(repo, part="patch", do_commit=False, do_tag=False)

    def test_allow_empty_overrides(self, tmp_path):
        repo = self._repo(tmp_path, _EMPTY_CL)
        res = release(repo, part="patch", do_commit=False, do_tag=False, allow_empty=True)
        assert res["new_version"] == "0.3.10"

    def test_explicit_downgrade_blocks(self, tmp_path):
        repo = self._repo(tmp_path)
        with pytest.raises(ValueError, match="not newer"):
            release(repo, explicit_version="0.2.0", do_commit=False, do_tag=False)

    def test_explicit_same_blocks(self, tmp_path):
        repo = self._repo(tmp_path)
        with pytest.raises(ValueError, match="not newer"):
            release(repo, explicit_version="0.3.9", do_commit=False, do_tag=False)

    def test_explicit_bad_format_blocks(self, tmp_path):
        repo = self._repo(tmp_path)
        with pytest.raises(ValueError, match="SemVer"):
            release(repo, explicit_version="0.4", do_commit=False, do_tag=False)

    def test_guards_run_in_dry_run(self, tmp_path):
        # dry-run is a preflight: the empty-vNEXT guard must still fire
        repo = self._repo(tmp_path, _EMPTY_CL)
        with pytest.raises(ValueError, match="no changelog entries"):
            release(repo, part="patch", dry_run=True)


class TestEveryVersionFileMovesTogether:
    """v1.0.80 shipped with pyproject at 1.0.80 and `__version__` still at
    1.0.79 because `release()` only ever rewrote two of the four files. CI's
    own `test_version_sync` caught it — after the tag was already pushed."""

    def test_release_bumps_all_four_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel"),
            patch("agent_takkub.release.create_github_release", return_value=(True, "u")),
        ):
            release(repo, part="minor", today="2026-05-31")

        assert 'version = "0.4.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
        assert '__version__ = "0.4.0"' in (repo / "src" / "agent_takkub" / "__init__.py").read_text(
            encoding="utf-8"
        )
        assert '"version": "0.4.0"' in (repo / "package.json").read_text(encoding="utf-8")
        assert "## [v0.4.0] - 2026-05-31" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_all_four_are_staged_for_the_release_commit(self, tmp_path):
        """Writing them is not enough — a file left unstaged still ships a
        mismatched tag."""
        repo = _make_repo(tmp_path)
        with (
            patch("agent_takkub.release._git") as git,
            patch("agent_takkub.release.build_wheel"),
            patch("agent_takkub.release.create_github_release", return_value=(True, "u")),
        ):
            release(repo, part="minor", today="2026-05-31")

        add = next(c for c in git.call_args_list if len(c.args) > 1 and c.args[1] == "add")
        assert set(add.args[2:]) == {
            "pyproject.toml",
            "CHANGELOG.md",
            "src/agent_takkub/__init__.py",
            "package.json",
        }

    def test_set_init_version_rewrites_only_the_version(self):
        out = set_init_version(_INIT_PY, "9.9.9")
        assert '__version__ = "9.9.9"' in out
        assert out.startswith('"""doc."""')

    def test_set_npm_version_keeps_formatting_and_other_keys(self):
        out = set_npm_version(_PACKAGE_JSON, "9.9.9")
        assert '"version": "9.9.9"' in out
        assert '"name": "agent-takkub"' in out
        assert '"bin": {}' in out

    def test_missing_version_line_raises(self):
        with pytest.raises(ValueError):
            set_init_version('"""doc."""\n', "9.9.9")
        with pytest.raises(ValueError):
            set_npm_version('{"name": "x"}', "9.9.9")


class TestBuildWheel:
    """`build_wheel()` is what `takkub release` runs so a subsequent `npm
    publish` can never attach a stale wheel (#340): clear dist/*.whl first,
    then `python -m build --wheel`, so exactly one (the new) wheel remains."""

    def test_deletes_stale_wheels_before_building(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "agent_takkub-1.0.82-py3-none-any.whl").write_text("old", encoding="utf-8")

        def _fake_build(*args, **kwargs):
            (dist / "agent_takkub-1.0.83-py3-none-any.whl").write_text("new", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch("agent_takkub.release.subprocess.run", side_effect=_fake_build) as m:
            out = build_wheel(tmp_path)

        assert m.called
        remaining = sorted(p.name for p in dist.glob("*.whl"))
        assert remaining == ["agent_takkub-1.0.83-py3-none-any.whl"]
        assert out == dist / "agent_takkub-1.0.83-py3-none-any.whl"

    def test_invokes_python_dash_m_build_wheel(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()

        def _fake_build(*args, **kwargs):
            (dist / "agent_takkub-1.0.0-py3-none-any.whl").write_text("x", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch("agent_takkub.release.subprocess.run", side_effect=_fake_build) as m:
            build_wheel(tmp_path)

        cmd = m.call_args.args[0]
        assert cmd[1:4] == ["-m", "build", "--wheel"]

    def test_raises_if_build_produces_no_wheel(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        with patch("agent_takkub.release.subprocess.run", return_value=MagicMock(returncode=0)):
            with pytest.raises(RuntimeError, match="exactly one wheel"):
                build_wheel(tmp_path)

    def test_raises_if_build_produces_more_than_one_wheel(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()

        def _fake_build(*args, **kwargs):
            (dist / "agent_takkub-1.0.0-py3-none-any.whl").write_text("x", encoding="utf-8")
            (dist / "agent_takkub-1.0.1-py3-none-any.whl").write_text("y", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch("agent_takkub.release.subprocess.run", side_effect=_fake_build):
            with pytest.raises(RuntimeError, match="exactly one wheel"):
                build_wheel(tmp_path)

    def test_propagates_subprocess_failure(self, tmp_path):
        import subprocess as sp

        with patch(
            "agent_takkub.release.subprocess.run",
            side_effect=sp.CalledProcessError(1, ["python", "-m", "build"]),
        ):
            with pytest.raises(sp.CalledProcessError):
                build_wheel(tmp_path)

    def test_deletes_stale_build_dir_before_building(self, tmp_path):
        """A `build/` staging dir left over from a prior `python -m build`
        run can reference source files that no longer exist (renamed/removed
        _assets), failing the next build with a spurious WinError 2 even
        though the current source is fine — clear it first every time."""
        dist = tmp_path / "dist"
        dist.mkdir()
        stale_build = tmp_path / "build"
        stale_build.mkdir()
        (stale_build / "stale.txt").write_text("leftover", encoding="utf-8")

        def _fake_build(*args, **kwargs):
            assert not stale_build.exists(), "build/ must be gone before `python -m build` runs"
            (dist / "agent_takkub-1.0.0-py3-none-any.whl").write_text("x", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch("agent_takkub.release.subprocess.run", side_effect=_fake_build):
            build_wheel(tmp_path)

        assert not stale_build.exists()


class TestReleaseWheelStep:
    """release() must build a fresh wheel as part of a real (committed +
    tagged) release — never leaving a stale one from a prior version."""

    def _repo(self, tmp_path):
        _make_repo(tmp_path, _CHANGELOG)
        return tmp_path

    def test_build_wheel_invoked_when_committed_and_tagged(self, tmp_path):
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel") as m,
            patch("agent_takkub.release.create_github_release", return_value=(True, "u")),
        ):
            release(repo, part="minor", today="2026-05-31")
        assert m.called
        assert m.call_args.args[0] == repo

    def test_build_wheel_skipped_without_commit_or_tag(self, tmp_path):
        repo = self._repo(tmp_path)
        with patch("agent_takkub.release.build_wheel") as m:
            release(repo, part="minor", do_commit=False, do_tag=False, today="2026-05-31")
        assert not m.called

    def test_do_build_wheel_false_skips_it(self, tmp_path):
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel") as m,
            patch("agent_takkub.release.create_github_release", return_value=(True, "u")),
        ):
            release(repo, part="minor", today="2026-05-31", do_build_wheel=False)
        assert not m.called

    def test_build_wheel_failure_aborts_and_reverts(self, tmp_path):
        repo = self._repo(tmp_path)
        with (
            patch("agent_takkub.release._git"),
            patch("agent_takkub.release.build_wheel", side_effect=RuntimeError("build broke")),
        ):
            with pytest.raises(RuntimeError, match="build broke"):
                release(repo, part="minor", today="2026-05-31")
        # reverted — pyproject still at the pre-release version
        assert 'version = "0.3.9"' in (repo / "pyproject.toml").read_text(encoding="utf-8")


def test_release_commit_outlasts_the_pre_commit_chain(tmp_path):
    """`git commit` runs this repo's full pre-commit chain (ruff,
    docs-verify, import-linter, depgraph freshness). On a cold cache that
    outlasts `_git`'s 30 s default, and the release then aborts and rolls
    back reporting a bare "timed out" — observed twice in a row cutting
    1.0.82."""
    repo = _make_repo(tmp_path)
    with (
        patch("agent_takkub.release._git") as git,
        patch("agent_takkub.release.build_wheel"),
        patch("agent_takkub.release.create_github_release", return_value=(True, "u")),
    ):
        release(repo, part="patch", today="2026-05-31")

    commit = next(c for c in git.call_args_list if len(c.args) > 1 and c.args[1] == "commit")
    assert commit.kwargs.get("timeout", 30.0) >= 300.0
