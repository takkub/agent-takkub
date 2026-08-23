"""#362-followup: proves conftest.py's `_snapshot_settings_home_files` (the
`_isolate_runtime` teardown guard that fails a test writing to the REAL
SETTINGS_HOME) survives a test trapping `Path.stat`/`Path.resolve` to raise on
any filesystem call — the exact shape of test_graft_chip.py's
`test_main_thread_refresh_reads_cache_without_filesystem_calls`, which turned
into a teardown ERROR on 3-OS CI because the guard's old implementation used
`Path.is_file()`/`Path.stat()` internally and so tripped the very trap it was
running alongside.

Three things must hold:
  1. Under that exact trap, the snapshot still returns the correct result
     instead of raising — it never touches `Path.stat`/`Path.resolve` at all,
     using only the `os.scandir`/`os.stat` references conftest.py captured at
     import time, before any test could monkeypatch them.
  2. If even those captured originals somehow raise something unexpected (not
     an ordinary OSError), the snapshot degrades to `None` ("unmeasurable
     this round") instead of raising — so a freak failure there can never
     turn into a teardown ERROR on an unrelated test either.
  3. Without any trap, the snapshot still detects a real file write — proving
     the fix didn't blind the guard to the thing it exists to catch (#362).
"""

from __future__ import annotations

from pathlib import Path

from tests import conftest
from tests.conftest import _snapshot_settings_home_files


def test_snapshot_survives_a_path_stat_trap_and_still_reports_correctly(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "existing.json").write_text("{}", encoding="utf-8")

    def _filesystem_call(*_args, **_kwargs):
        raise AssertionError("filesystem call reached the Qt refresh slot")

    monkeypatch.setattr(Path, "resolve", _filesystem_call)
    monkeypatch.setattr(Path, "stat", _filesystem_call)

    snapshot = _snapshot_settings_home_files(tmp_path)

    assert snapshot is not None
    assert "existing.json" in snapshot


def test_snapshot_returns_none_when_the_captured_originals_themselves_fail(
    tmp_path, monkeypatch
) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated unexpected failure in the captured original")

    monkeypatch.setattr(conftest, "_ORIG_OS_STAT", _boom)

    assert _snapshot_settings_home_files(tmp_path) is None


def test_snapshot_still_detects_a_real_write_without_a_trap(tmp_path) -> None:
    before = _snapshot_settings_home_files(tmp_path)
    assert before == {}

    (tmp_path / "provider-models.json").write_text("{}", encoding="utf-8")

    after = _snapshot_settings_home_files(tmp_path)
    assert after != before
    assert "provider-models.json" in after
