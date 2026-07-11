"""Tests for custom_roles: A6 user-created role registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import custom_roles, roles


@pytest.fixture
def registry_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the registry JSON + role-file dir to tmp, and clear the
    runtime `roles._CUSTOM` registry so tests never leak into each other."""
    registry = tmp_path / "custom-roles.json"
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", registry)
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", agents_dir)
    saved = dict(roles._CUSTOM)
    roles._CUSTOM.clear()
    yield registry
    roles._CUSTOM.clear()
    roles._CUSTOM.update(saved)


class TestValidateRoleName:
    def test_valid_name_ok(self, registry_files: Path) -> None:
        assert custom_roles.validate_role_name("data-eng") == (True, "")

    def test_empty_name_rejected(self, registry_files: Path) -> None:
        ok, err = custom_roles.validate_role_name("")
        assert ok is False
        assert err

    def test_collides_with_builtin_role(self, registry_files: Path) -> None:
        ok, err = custom_roles.validate_role_name("backend")
        assert ok is False
        assert "backend" in err

    def test_collision_check_is_case_insensitive(self, registry_files: Path) -> None:
        ok, _err = custom_roles.validate_role_name("BACKEND")
        assert ok is False

    def test_shard_hash_rejected(self, registry_files: Path) -> None:
        ok, err = custom_roles.validate_role_name("qa#1")
        assert ok is False
        assert "#" in err

    def test_path_traversal_rejected(self, registry_files: Path) -> None:
        ok, _err = custom_roles.validate_role_name("../evil")
        assert ok is False

    def test_slash_rejected(self, registry_files: Path) -> None:
        ok, _err = custom_roles.validate_role_name("a/b")
        assert ok is False

    def test_uppercase_normalizes_and_accepts(self, registry_files: Path) -> None:
        ok, _err = custom_roles.validate_role_name("Data-Eng")
        assert ok is True

    def test_too_long_rejected(self, registry_files: Path) -> None:
        ok, _err = custom_roles.validate_role_name("a" * 65)
        assert ok is False


class TestLoadSaveRoundTrip:
    def test_missing_file_returns_empty(self, registry_files: Path) -> None:
        assert custom_roles.load_custom_roles() == {}

    def test_save_then_load_round_trips(self, registry_files: Path) -> None:
        payload = {
            "data-eng": roles.Role("data-eng", "Data Eng", "#ff0000", column=1, row=5),
        }
        assert custom_roles.save_custom_roles(payload) is True
        loaded = custom_roles.load_custom_roles()
        assert loaded == payload

    def test_corrupt_json_returns_empty(self, registry_files: Path) -> None:
        registry_files.write_text("{not valid json", encoding="utf-8")
        assert custom_roles.load_custom_roles() == {}

    def test_invalid_entries_filtered_out(self, registry_files: Path) -> None:
        registry_files.parent.mkdir(parents=True, exist_ok=True)
        registry_files.write_text(
            json.dumps(
                {
                    "version": 1,
                    "roles": {
                        "backend": {"label": "shadow built-in", "color": "#ff0000"},
                        "ok-role": {"label": "OK", "color": "#112233", "column": 1, "row": 2},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = custom_roles.load_custom_roles()
        assert "backend" not in loaded
        assert "ok-role" in loaded

    def test_bad_color_defaults(self, registry_files: Path) -> None:
        registry_files.parent.mkdir(parents=True, exist_ok=True)
        registry_files.write_text(
            json.dumps(
                {"version": 1, "roles": {"data-eng": {"label": "x", "color": "not-a-color"}}}
            ),
            encoding="utf-8",
        )
        loaded = custom_roles.load_custom_roles()
        assert loaded["data-eng"].color == "#94a3b8"

    def test_list_role_names(self, registry_files: Path) -> None:
        custom_roles.save_custom_roles(
            {"data-eng": roles.Role("data-eng", "Data Eng", "#112233", 1, 5)}
        )
        assert custom_roles.list_role_names() == frozenset({"data-eng"})


class TestCreateRole:
    def test_creates_registry_entry_and_role_file(self, registry_files: Path) -> None:
        ok, err = custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "Do ETL.")
        assert ok is True
        assert err == ""
        assert custom_roles.load_custom_roles()["data-eng"].label == "Data Eng"
        role_file = custom_roles.role_file_path("data-eng")
        assert role_file.read_text(encoding="utf-8") == "Do ETL."

    def test_default_template_used_when_no_instructions(self, registry_files: Path) -> None:
        ok, _err = custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, None)
        assert ok is True
        text = custom_roles.role_file_path("data-eng").read_text(encoding="utf-8")
        assert "data-eng" in text
        assert "takkub done" in text

    def test_rejects_collision_with_builtin(self, registry_files: Path) -> None:
        ok, err = custom_roles.create_role("qa", "Fake QA", "#112233", 1, 5, None)
        assert ok is False
        assert err
        assert not custom_roles.role_file_path("qa").exists()

    def test_rejects_bad_color(self, registry_files: Path) -> None:
        ok, err = custom_roles.create_role("data-eng", "Data Eng", "red", 1, 5, None)
        assert ok is False
        assert "สี" in err

    def test_rejects_bad_column(self, registry_files: Path) -> None:
        ok, err = custom_roles.create_role("data-eng", "Data Eng", "#112233", 3, 5, None)
        assert ok is False
        assert "column" in err

    def test_label_defaults_to_capitalized_name(self, registry_files: Path) -> None:
        ok, _err = custom_roles.create_role("data-eng", "", "#112233", 1, 5, None)
        assert ok is True
        assert custom_roles.load_custom_roles()["data-eng"].label == "Data-eng"

    def test_role_file_commit_failure_rolls_back_registry(
        self, registry_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #4 — role markdown is written to a temp file and
        renamed into place LAST; if that rename fails, the registry write
        that already happened must be rolled back so a retry doesn't collide
        on "already exists" against an entry with no matching role file."""
        original_replace = Path.replace

        def _boom(self: Path, target: object) -> Path:
            if str(target).endswith(".md"):
                raise OSError("disk full")
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", _boom)

        ok, err = custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")

        assert ok is False
        assert err
        assert "data-eng" not in custom_roles.load_custom_roles()
        assert not custom_roles.role_file_path("data-eng").exists()

    def test_role_file_commit_failure_restores_previous_entry(
        self, registry_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overwriting an EXISTING custom role whose role-file rename fails
        must restore the prior registry entry, not just delete the name."""
        custom_roles.create_role("data-eng", "Data Eng (old)", "#112233", 1, 5, "old")
        original_replace = Path.replace

        def _boom(self: Path, target: object) -> Path:
            if str(target).endswith(".md"):
                raise OSError("disk full")
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", _boom)

        ok, _err = custom_roles.create_role("data-eng", "Data Eng (new)", "#654321", 2, 9, "new")

        assert ok is False
        restored = custom_roles.load_custom_roles()["data-eng"]
        assert restored.label == "Data Eng (old)"
        assert restored.color == "#112233"


class TestDeleteRole:
    def test_delete_removes_from_registry(self, registry_files: Path) -> None:
        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        assert custom_roles.delete_role("data-eng") is True
        assert "data-eng" not in custom_roles.load_custom_roles()

    def test_delete_removes_role_file(self, registry_files: Path) -> None:
        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        assert custom_roles.role_file_path("data-eng").exists()
        assert custom_roles.delete_role("data-eng") is True
        assert not custom_roles.role_file_path("data-eng").exists()

    def test_delete_missing_role_is_noop_success(self, registry_files: Path) -> None:
        assert custom_roles.delete_role("nope") is True


class TestBootLoadRegistersRoles:
    def test_load_and_register_all_registers_with_roles_by_name(self, registry_files: Path) -> None:
        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        assert roles.by_name("data-eng") is None  # not registered in this process yet
        count = custom_roles.load_and_register_all()
        assert count == 1
        resolved = roles.by_name("data-eng")
        assert resolved is not None
        assert resolved.label == "Data Eng"
        assert resolved.column == 1

    def test_boot_load_with_empty_registry_is_noop(self, registry_files: Path) -> None:
        assert custom_roles.load_and_register_all() == 0

    def test_boot_load_never_raises_on_corrupt_registry(self, registry_files: Path) -> None:
        registry_files.write_text("{not valid", encoding="utf-8")
        assert custom_roles.load_and_register_all() == 0
