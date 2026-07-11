from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.settings_management.transaction import FileTransaction


def test_rollback_restores_existing_file_content(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text("original", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with FileTransaction([f]):
            f.write_text("mutated", encoding="utf-8")
            raise RuntimeError("boom")

    assert f.read_text(encoding="utf-8") == "original"


def test_rollback_deletes_file_that_did_not_exist_before(tmp_path: Path) -> None:
    f = tmp_path / "new.json"

    with pytest.raises(RuntimeError):
        with FileTransaction([f]):
            f.write_text("created", encoding="utf-8")
            raise RuntimeError("boom")

    assert not f.exists()


def test_no_rollback_on_success(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text("original", encoding="utf-8")

    with FileTransaction([f]):
        f.write_text("updated", encoding="utf-8")

    assert f.read_text(encoding="utf-8") == "updated"


def test_multi_file_rollback_restores_all_snapshotted_paths(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("a0", encoding="utf-8")
    b.write_text("b0", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with FileTransaction([a, b]):
            a.write_text("a1", encoding="utf-8")
            b.write_text("b1", encoding="utf-8")
            raise RuntimeError("boom")

    assert a.read_text(encoding="utf-8") == "a0"
    assert b.read_text(encoding="utf-8") == "b0"
