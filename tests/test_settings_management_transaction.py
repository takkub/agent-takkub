from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.settings_management.transaction import FileTransaction, TransactionRollbackError


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


def test_rollback_uses_temp_write_and_replace_not_direct_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOW-1: rollback must restore via temp+replace, never a direct
    in-place ``write_bytes`` (a crash mid-restore could otherwise leave a
    half-written file)."""
    f = tmp_path / "a.json"
    f.write_text("original", encoding="utf-8")

    real_write_bytes = Path.write_bytes
    calls: list[Path] = []

    def spy_write_bytes(self: Path, data: bytes) -> int:
        calls.append(self)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy_write_bytes)

    with pytest.raises(RuntimeError):
        with FileTransaction([f]):
            f.write_text("mutated", encoding="utf-8")
            raise RuntimeError("boom")

    assert f.read_text(encoding="utf-8") == "original"
    # The rollback write must have landed on a *different* path than `f`
    # itself (the temp file), then been renamed over it via `.replace()`.
    assert any(p != f and p.name.startswith("a.json") for p in calls)


def test_rollback_failure_raises_transaction_rollback_error_chained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MED-5/LOW-1: when rollback itself can't restore a path, the caller
    must get a distinct, clearly-labeled exception — not a silently
    incomplete restore masquerading as an ordinary operation failure."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("a0", encoding="utf-8")
    b.write_text("b0", encoding="utf-8")

    real_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.name.startswith("b.json"):
            raise OSError("simulated disk failure during rollback")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    original = RuntimeError("boom")
    with pytest.raises(TransactionRollbackError) as exc_info:
        with FileTransaction([a, b]):
            a.write_text("a1", encoding="utf-8")
            b.write_text("b1", encoding="utf-8")
            raise original

    err = exc_info.value
    assert err.__cause__ is original
    assert str(b) in err.paths
    assert "ROLLBACK INCOMPLETE" in str(err)
    # `a` (the path whose rollback succeeded) is still restored correctly —
    # only `b`'s failure is reported, not a total abort of the loop.
    assert a.read_text(encoding="utf-8") == "a0"
