"""Tests for security ingress hardening (Round 2 findings #3, #4, #7).

Covers:
  validate_name — rejects traversal, uppercase, spaces, empty, etc.
  _cwd_within_project — REPO_ROOT bypass is Lead-only
  _write_json_atomic — tmp file guarantees; original survives a write abort
"""

from __future__ import annotations

import json
import pathlib

import pytest

import agent_takkub.orchestrator as orch_mod
from agent_takkub import config as config_mod
from agent_takkub.config import _write_json_atomic, validate_name
from agent_takkub.orchestrator import (
    LEAD,
    _cwd_within_project,
    _describe_valid_project_cwds,
    _project_root_dir,
    cwd_validation_error,
)

# ──────────────────────────────────────────────────────────────────────────────
# validate_name
# ──────────────────────────────────────────────────────────────────────────────


class TestValidateName:
    def test_valid_lowercase_simple(self) -> None:
        assert validate_name("backend", "role") == "backend"

    def test_valid_with_hyphen(self) -> None:
        assert validate_name("data-eng", "role") == "data-eng"

    def test_valid_with_underscore(self) -> None:
        assert validate_name("my_role", "role") == "my_role"

    def test_valid_alphanumeric(self) -> None:
        assert validate_name("role2", "role") == "role2"

    def test_valid_with_dot_domain(self) -> None:
        assert validate_name("www.abc.com", "project") == "www.abc.com"

    def test_strips_and_lowercases(self) -> None:
        # validate_name normalises before matching
        assert validate_name("  Backend  ", "role") == "backend"

    def test_traversal_dots_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("../../etc/passwd", "role")

    def test_windows_traversal_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name(r"..\..\..\x", "role")

    def test_slash_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("/etc/passwd", "role")

    def test_backslash_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("a\\b", "role")

    def test_space_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("name with space", "role")

    def test_uppercase_only_raises(self) -> None:
        # After lowercasing "UPPER" becomes "upper" which is valid;
        # the function normalises first, so uppercase input is accepted.
        # (If the caller pre-lowercased, result is same.)
        assert validate_name("UPPER", "role") == "upper"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("", "role")

    def test_dot_only_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name(".", "role")

    def test_double_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("..", "role")

    def test_leading_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name(".hidden", "role")

    def test_trailing_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid role"):
            validate_name("trailing.", "role")

    def test_null_byte_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_name("\x00", "role")

    def test_kind_in_message(self) -> None:
        with pytest.raises(ValueError, match="invalid project"):
            validate_name("bad/name", "project")

    def test_max_length_63_extra_chars_ok(self) -> None:
        # 63-char suffix → total 64 chars (1 leading + 63) — should pass
        name = "a" + "b" * 63
        assert validate_name(name, "role") == name

    def test_too_long_raises(self) -> None:
        name = "a" + "b" * 64  # 65 chars — exceeds 63 suffix limit
        with pytest.raises(ValueError):
            validate_name(name, "role")


# ──────────────────────────────────────────────────────────────────────────────
# _cwd_within_project — role-aware REPO_ROOT bypass
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def project_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set up a minimal two-path project and redirect REPO_ROOT + PROJECTS_JSON."""
    proj_web = tmp_path / "myproject" / "web"
    proj_api = tmp_path / "myproject" / "api"
    proj_web.mkdir(parents=True)
    proj_api.mkdir(parents=True)

    repo = tmp_path / "cockpit"
    repo.mkdir()

    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "myproject",
                "projects": {
                    "myproject": {
                        "paths": {
                            "web": str(proj_web),
                            "api": str(proj_api),
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", pj)
    monkeypatch.setattr(config_mod, "REPO_ROOT", repo)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", repo)

    return {"web": proj_web, "api": proj_api, "repo": repo, "project": "myproject"}


class TestCwdWithinProject:
    def test_project_path_allowed_for_any_role(self, project_env: dict) -> None:
        cwd = str(project_env["web"])
        assert _cwd_within_project(cwd, project_env["project"], "backend") is True

    def test_subdir_of_project_path_allowed(self, project_env: dict) -> None:
        subdir = project_env["api"] / "src"
        subdir.mkdir()
        assert _cwd_within_project(str(subdir), project_env["project"], "backend") is True

    def test_repo_root_allowed_for_lead(self, project_env: dict) -> None:
        cwd = str(project_env["repo"])
        assert _cwd_within_project(cwd, project_env["project"], LEAD.name) is True

    def test_repo_root_denied_for_teammate(self, project_env: dict) -> None:
        cwd = str(project_env["repo"])
        assert _cwd_within_project(cwd, project_env["project"], "backend") is False

    def test_repo_subdir_denied_for_teammate(self, project_env: dict) -> None:
        subdir = project_env["repo"] / "src"
        subdir.mkdir()
        assert _cwd_within_project(str(subdir), project_env["project"], "frontend") is False

    def test_repo_subdir_allowed_for_lead(self, project_env: dict) -> None:
        subdir = project_env["repo"] / "src"
        subdir.mkdir()
        assert _cwd_within_project(str(subdir), project_env["project"], LEAD.name) is True

    def test_unrelated_path_denied_for_all(self, project_env: dict, tmp_path: pathlib.Path) -> None:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        assert _cwd_within_project(str(unrelated), project_env["project"], LEAD.name) is False
        assert _cwd_within_project(str(unrelated), project_env["project"], "backend") is False


# ──────────────────────────────────────────────────────────────────────────────
# _project_root_dir / _cwd_within_project — project-root acceptance (#143)
# ──────────────────────────────────────────────────────────────────────────────


class TestProjectRootDir:
    def test_returns_common_parent_when_it_exists(self, project_env: dict) -> None:
        # project_env's web/api paths are both under tmp_path/"myproject",
        # which mkdir(parents=True) creates as a real, existing directory.
        root = _project_root_dir(project_env["project"])
        assert root is not None
        assert root == project_env["web"].parent
        assert root == project_env["api"].parent

    def test_none_for_unknown_project(self, project_env: dict) -> None:
        assert _project_root_dir("nonexistent-project") is None

    def test_none_when_common_parent_does_not_exist(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Configured paths whose common parent was never created on disk —
        # must not fabricate a bypass for a directory that doesn't exist.
        pj = tmp_path / "projects.json"
        pj.write_text(
            json.dumps(
                {
                    "active": "ghost",
                    "projects": {
                        "ghost": {
                            "paths": {
                                "web": str(tmp_path / "never-created" / "web"),
                                "api": str(tmp_path / "never-created" / "api"),
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "PROJECTS_JSON", pj)
        assert _project_root_dir("ghost") is None


class TestCwdWithinProjectAcceptsProjectRoot:
    def test_project_root_itself_allowed_for_teammate(self, project_env: dict) -> None:
        root = project_env["web"].parent
        assert _cwd_within_project(str(root), project_env["project"], "backend") is True

    def test_subdir_of_project_root_allowed_for_teammate(self, project_env: dict) -> None:
        root = project_env["web"].parent
        extra = root / "docs"
        extra.mkdir()
        assert _cwd_within_project(str(extra), project_env["project"], "devops") is True

    def test_sibling_of_project_root_still_denied(
        self, project_env: dict, tmp_path: pathlib.Path
    ) -> None:
        # A directory next to (not under) the project root must stay denied —
        # the bypass is scoped to the root and its descendants only.
        sibling = tmp_path / "sibling-of-myproject"
        sibling.mkdir()
        assert _cwd_within_project(str(sibling), project_env["project"], "backend") is False


class TestCwdValidationError:
    def test_none_for_valid_cwd(self, project_env: dict) -> None:
        assert (
            cwd_validation_error(str(project_env["web"]), project_env["project"], "backend") is None
        )

    def test_message_names_valid_paths_for_invalid_cwd(
        self, project_env: dict, tmp_path: pathlib.Path
    ) -> None:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        err = cwd_validation_error(str(unrelated), project_env["project"], "backend")
        assert err is not None
        assert str(unrelated) in err
        assert str(project_env["web"]) in err
        assert str(project_env["api"]) in err

    def test_message_includes_project_root_when_it_exists(
        self, project_env: dict, tmp_path: pathlib.Path
    ) -> None:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        err = cwd_validation_error(str(unrelated), project_env["project"], "backend")
        assert err is not None
        assert str(project_env["web"].parent) in err


# ──────────────────────────────────────────────────────────────────────────────
# _describe_valid_project_cwds — single-path project must not duplicate the
# path when its "project root" (common parent of one path) is itself (#150)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def single_path_project_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A project configured with exactly one path (e.g. agent-takkub's `main`)."""
    main = tmp_path / "myproject"
    main.mkdir(parents=True)

    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "myproject",
                "projects": {
                    "myproject": {
                        "paths": {
                            "main": str(main),
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", pj)

    return {"main": main, "project": "myproject"}


class TestDescribeValidProjectCwds:
    def test_single_path_project_lists_path_once(self, single_path_project_env: dict) -> None:
        # The common parent of one path is that path itself, so the naive
        # "roots + project root" concatenation used to print the same path
        # twice. It must appear exactly once, labelled as the project root.
        desc = _describe_valid_project_cwds(single_path_project_env["project"])
        main = str(single_path_project_env["main"])
        assert desc.count(main) == 1
        assert f"{main} (project root)" in desc

    def test_multi_path_project_lists_root_as_extra_entry(self, project_env: dict) -> None:
        # A project with several distinct paths must still see its common
        # parent listed as an additional "(project root)" entry.
        desc = _describe_valid_project_cwds(project_env["project"])
        web = str(project_env["web"])
        api = str(project_env["api"])
        root = str(project_env["web"].parent)
        assert web in desc
        assert api in desc
        assert f"{root} (project root)" in desc
        # exactly one "(project root)" label — not duplicated per configured path
        assert desc.count("(project root)") == 1


# ──────────────────────────────────────────────────────────────────────────────
# _write_json_atomic
# ──────────────────────────────────────────────────────────────────────────────


class TestWriteJsonAtomic:
    def test_writes_valid_json(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "data.json"
        _write_json_atomic(target, {"a": 1, "b": [2, 3]})
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"a": 1, "b": [2, 3]}

    def test_no_tmp_file_left_after_success(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "data.json"
        _write_json_atomic(target, {"x": 42})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"unexpected tmp files: {tmp_files}"

    def test_original_survives_when_tmp_exists_at_start(self, tmp_path: pathlib.Path) -> None:
        """If a stale .tmp file exists from a previous crash, atomic write
        overwrites the tmp file and replaces the original cleanly — the
        original is never in a half-written state."""
        target = tmp_path / "data.json"
        target.write_text(json.dumps({"old": True}), encoding="utf-8")

        # Simulate stale tmp from prior crash
        stale_tmp = target.with_suffix(target.suffix + ".tmp")
        stale_tmp.write_text("CORRUPT", encoding="utf-8")

        _write_json_atomic(target, {"new": True})

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"new": True}
        assert not stale_tmp.exists()

    def test_roundtrip_unicode(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "unicode.json"
        _write_json_atomic(target, {"key": "ภาษาไทย 🎉"})
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["key"] == "ภาษาไทย 🎉"
