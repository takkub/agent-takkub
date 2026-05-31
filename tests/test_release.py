"""Tests for the `takkub release` ceremony (release.py).

String transforms are pure and fully covered here. release() is exercised
with do_commit/do_tag off (and dry_run) so no git is invoked.
"""

from __future__ import annotations

import pytest

from agent_takkub.release import (
    bump_version,
    read_pyproject_version,
    release,
    roll_changelog,
    set_pyproject_version,
)

_PYPROJECT = '[project]\nname = "agent-takkub"\nversion = "0.3.9"\nrequires-python = ">=3.11"\n'
_CHANGELOG = "# Changelog\n\n## [vNEXT]\n\n### Changed\n- did a thing\n\n(end)\n"


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
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
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
