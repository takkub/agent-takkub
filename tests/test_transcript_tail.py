"""M4#22: `_read_tail_bytes` — bounded tail read for `takkub status`.

The status report keeps only a pane transcript's last few lines, but used to
read the ENTIRE file (MBs for a long session) into memory every call. This reads
at most the trailing N bytes regardless of file size.
"""

from __future__ import annotations

import pathlib

from agent_takkub.orchestrator import _read_tail_bytes


def test_small_file_returns_whole(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "t.log"
    f.write_bytes(b"hello world\n")
    assert _read_tail_bytes(f, 1024) == b"hello world\n"


def test_large_file_returns_only_tail(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "t.log"
    f.write_bytes(b"A" * 10_000 + b"TAILMARKER")
    out = _read_tail_bytes(f, 100)
    assert len(out) == 100
    assert out.endswith(b"TAILMARKER")
    assert b"A" * 90 in out  # the rest of the 100-byte window is the preceding A's


def test_exact_boundary(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "t.log"
    f.write_bytes(b"X" * 256)
    assert _read_tail_bytes(f, 256) == b"X" * 256
    assert len(_read_tail_bytes(f, 255)) == 255


def test_empty_file(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "t.log"
    f.write_bytes(b"")
    assert _read_tail_bytes(f, 1024) == b""


def test_tail_preserves_last_lines(tmp_path: pathlib.Path) -> None:
    # The real consumer wants the last few non-blank lines from the tail window.
    f = tmp_path / "t.log"
    body = ("noise line\n" * 5000) + "line-A\nline-B\nline-C\n"
    f.write_bytes(body.encode("utf-8"))
    raw = _read_tail_bytes(f, 64 * 1024)
    lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    assert lines[-3:] == ["line-A", "line-B", "line-C"]
