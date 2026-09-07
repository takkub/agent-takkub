"""token_estimate.py — issue #516 F2 Thai-weighted estimator + #516b merge
of the core `estimate_tokens` callers with the marketplace/skill cost
helpers (`estimate_file_tokens`/`estimate_dir_tokens`/`format_tokens`)."""

from __future__ import annotations

from pathlib import Path

from agent_takkub import token_estimate
from agent_takkub.token_estimate import estimate_tokens


def test_empty_and_short_text_never_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("x") == 1


def test_ascii_only_keeps_chars_per_4_ratio():
    # No Thai characters: behaviour must match the old chars/4 convention
    # exactly, so every non-Thai caller (English docs, code, JSON) sees no
    # change from this estimator swap.
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("a" * 40) == 10


def test_thai_text_weighted_far_higher_than_chars_per_4():
    thai = "ก" * 100
    naive = len(thai) // 4  # what the old estimator would have said: 25
    weighted = estimate_tokens(thai)
    assert weighted == 90  # 100 * 0.9
    assert weighted > naive * 3


def test_mixed_thai_and_ascii_blends_both_weights():
    text = "ก" * 100 + "x" * 400
    # 100 Thai chars * 0.9 + 400 other chars / 4 = 90 + 100 = 190
    assert estimate_tokens(text) == 190


def test_real_role_file_is_far_above_naive_chars_over_4():
    # Regression guard for #516 F2's actual motivation: Thai-heavy repo
    # content must estimate meaningfully higher than plain chars/4, not
    # just equal it because the sample happened to be ASCII.
    thai_heavy = "อ่านก่อนเริ่มงานที่แตะ dependency, lockfile, docker, ports" * 20
    naive = max(1, len(thai_heavy) // 4)
    assert estimate_tokens(thai_heavy) > naive


def test_estimate_file_tokens_missing_file_is_zero(tmp_path: Path) -> None:
    assert token_estimate.estimate_file_tokens(tmp_path / "nope.md") == 0


def test_estimate_file_tokens_reads_real_file(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("a" * 400, encoding="utf-8")
    assert token_estimate.estimate_file_tokens(f) == 100


def test_estimate_dir_tokens_sums_text_files_only(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "pre.py").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    assert token_estimate.estimate_dir_tokens(tmp_path) == 20


def test_estimate_dir_tokens_missing_dir_is_zero(tmp_path: Path) -> None:
    assert token_estimate.estimate_dir_tokens(tmp_path / "nope") == 0


def test_format_tokens_under_1000() -> None:
    assert token_estimate.format_tokens(340) == "~340 tok"


def test_format_tokens_over_1000_shows_k() -> None:
    assert token_estimate.format_tokens(1234) == "~1.2k tok"
