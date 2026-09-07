from __future__ import annotations

from pathlib import Path

from agent_takkub import token_estimate


def test_estimate_tokens_empty() -> None:
    assert token_estimate.estimate_tokens("") == 0


def test_estimate_tokens_english_uses_char_per_3_8() -> None:
    text = "a" * 38
    assert token_estimate.estimate_tokens(text) == round(38 / 3.8)


def test_estimate_tokens_thai_is_pricier_per_char_than_english() -> None:
    thai = "ก" * 38
    english = "a" * 38
    assert token_estimate.estimate_tokens(thai) > token_estimate.estimate_tokens(english)


def test_estimate_file_tokens_missing_file_is_zero(tmp_path: Path) -> None:
    assert token_estimate.estimate_file_tokens(tmp_path / "nope.md") == 0


def test_estimate_file_tokens_reads_real_file(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("a" * 380, encoding="utf-8")
    assert token_estimate.estimate_file_tokens(f) == 100


def test_estimate_dir_tokens_sums_text_files_only(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("a" * 38, encoding="utf-8")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "pre.py").write_text("a" * 38, encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    assert token_estimate.estimate_dir_tokens(tmp_path) == 20


def test_estimate_dir_tokens_missing_dir_is_zero(tmp_path: Path) -> None:
    assert token_estimate.estimate_dir_tokens(tmp_path / "nope") == 0


def test_format_tokens_under_1000() -> None:
    assert token_estimate.format_tokens(340) == "~340 tok"


def test_format_tokens_over_1000_shows_k() -> None:
    assert token_estimate.format_tokens(1234) == "~1.2k tok"
