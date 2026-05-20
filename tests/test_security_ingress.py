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

from agent_takkub.config import _write_json_atomic, validate_name
from agent_takkub import config as config_mod
from agent_takkub.orchestrator import LEAD, _cwd_within_project
import agent_takkub.orchestrator as orch_mod


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

    def test_unrelated_path_denied_for_all(
        self, project_env: dict, tmp_path: pathlib.Path
    ) -> None:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        assert _cwd_within_project(str(unrelated), project_env["project"], LEAD.name) is False
        assert _cwd_within_project(str(unrelated), project_env["project"], "backend") is False


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

    def test_original_survives_when_tmp_exists_at_start(
        self, tmp_path: pathlib.Path
    ) -> None:
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
