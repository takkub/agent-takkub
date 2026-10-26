"""#927: `takkub report build --type customer` looked too plain to send — no
heading on the page, unstyled tables, 5-column tables crushed on phones, and
every Lead hand-wrote ~60 lines of <style> per report."""

from __future__ import annotations

import pytest

from agent_takkub.report_builder import ReportBuilder, label_table_cells

_TABLE = (
    "<table><thead><tr><th>#</th><th>หัวข้อ</th><th>คำตอบ <b>ตั้งต้น</b></th></tr></thead>"
    '<tbody><tr><td>1</td><td>คืนตู้</td><td class="hl">ก.</td></tr></tbody></table>'
)


def _build(tmp_path, content: str, **kw) -> str:
    (tmp_path / "content.html").write_text(content, encoding="utf-8")
    return ReportBuilder("customer", str(tmp_path)).build(**kw)


def test_title_becomes_a_hero_without_any_content_style(tmp_path) -> None:
    html = _build(tmp_path, _TABLE, title="คำถาม <UAT>", subtitle="wash-locker · 26 ต.ค.")
    assert '<header class="hero"><h1>คำถาม &lt;UAT&gt;</h1><p>wash-locker · 26 ต.ค.</p>' in html
    assert ".hero{" in html and "table.cards" in html


def test_content_with_its_own_heading_gets_no_second_hero(tmp_path) -> None:
    html = _build(tmp_path, "<h1>คู่มือ</h1>" + _TABLE, title="x")
    assert 'class="hero"' not in html.split("</style>", 1)[1]


def test_cells_get_column_labels_for_phone_cards() -> None:
    out = label_table_cells(_TABLE)
    assert '<table class="cards">' in out
    assert '<td data-l="#">1</td>' in out
    assert '<td class="hl" data-l="คำตอบ ตั้งต้น">ก.</td>' in out


def test_header_row_outside_thead_is_hidden_on_phones() -> None:
    out = label_table_cells(
        '<table class="q"><tr><th>a</th><th>b</th></tr><tr><td>1</td><td data-l="own">2</td></tr></table>'
    )
    assert '<table class="cards q">' in out
    assert '<tr class="head-row">' in out
    assert '<td data-l="a">1</td><td data-l="own">2</td>' in out


def test_table_without_header_is_untouched() -> None:
    raw = "<table><tr><td>1</td></tr></table>"
    assert label_table_cells(raw) == raw


def test_accent_must_be_a_colour(tmp_path) -> None:
    assert "--accent:#0f766e" in _build(tmp_path, _TABLE, accent="#0f766e")
    with pytest.raises(ValueError):
        _build(tmp_path, _TABLE, accent="red;}body{display:none")
