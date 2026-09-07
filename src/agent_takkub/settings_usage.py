"""Usage Settings view — the "Usage" page (issue #507): real per-provider/
account/model token history + quota-% delta, backed by :mod:`usage_ledger`.

A mixin (`UsageSettingsMixin`) mixed into `settings_window.SettingsWindow`,
same shape as `settings_knowledge_design.KnowledgeDesignSettingsMixin` (see
that module's own docstring) — kept in its own file rather than growing
`settings_window.py` further. **This module must never import from
`settings_window`** (that direction already goes the other way).

Every number shown comes straight from `usage_ledger.query_usage()` — no
estimating, no "นับไม่ได้" provider ever renders a fabricated total (#507
directive). `usage_ledger.import_all()` (a full transcript walk — can touch
thousands of session files) NEVER runs on the Qt main thread: the view
renders instantly from whatever `daily.json` already holds (a `query_usage()`
call only re-rolls the already-small current-month raw file — cheap even at
view-construction time), and "Refresh" runs the real import on a background
`_CallableThread` (reused from `settings_knowledge_design.py` — same "run()
emits result-or-Exception" shape), re-rendering only once it completes.
Nothing here fetches the expensive import eagerly at construction time,
mirroring Core V2/Knowledge's own "press Refresh to load" precedent (an
eager fetch would cost a transcript scan on every Settings open even when
this page is never visited).
"""

from __future__ import annotations

from datetime import UTC, datetime

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme
from .settings_knowledge_design import _CallableThread
from .token_meter import format_tokens

_RANGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("today", "วันนี้"),
    ("week", "7 วันที่ผ่านมา"),
    ("month", "เดือนนี้"),
)

_TABLE_HEADERS: tuple[str, ...] = (
    "Provider",
    "Account",
    "Model",
    "Turns",
    "Input",
    "Cache wr",
    "Cache rd",
    "Output",
    "Total",
)
_QUOTA_HEADERS: tuple[str, ...] = ("Provider", "Account", "Window", "Δ%", "Samples")

_SPARKLINE_DAYS = 14


def _range_query_kwargs(key: str) -> dict:
    if key == "today":
        return {"days": 1}
    if key == "month":
        return {"month": datetime.now(tz=UTC).strftime("%Y-%m")}
    return {"days": 7}


class _Sparkline(QWidget):
    """Minimal `QPainter` line chart — no new charting dependency (none is
    used anywhere else in this codebase). `points` is `(label, value)` in
    display order; only the values are plotted, oldest first."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[str, int]] = []
        self.setFixedHeight(64)

    def set_points(self, points: list[tuple[str, int]]) -> None:
        self._points = points
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            width, height = self.width(), self.height()
            pad = 6.0
            values = [v for _label, v in self._points]
            if len(values) < 2 or max(values) <= 0:
                painter.setPen(QPen(QColor(cockpit_theme.TEXT_FAINT)))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "ยังไม่มีข้อมูลพอวาดกราฟ")
                return
            vmax = max(values)
            step = (width - 2 * pad) / (len(values) - 1)
            pen = QPen(QColor(cockpit_theme.ACCENT_GOLD))
            pen.setWidthF(2.0)
            painter.setPen(pen)
            prev: QPointF | None = None
            for i, v in enumerate(values):
                x = pad + i * step
                y = height - pad - (v / vmax) * (height - 2 * pad)
                point = QPointF(x, y)
                if prev is not None:
                    painter.drawLine(prev, point)
                prev = point
        finally:
            painter.end()


class UsageSettingsMixin:
    """Mixed into `SettingsWindow` — every method assumes `self` has the
    attributes `SettingsWindow.__init__` sets (`_fonts`, …) plus the
    QSS-driven helpers (`_build_card_header`) that class already defines."""

    # ──────────────────────────────────────────────────────────
    # view: Usage
    # ──────────────────────────────────────────────────────────

    def _build_usage_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        header_row = QHBoxLayout()
        header_row.addWidget(self._build_card_header("USAGE", "Token & Quota", "", view), 1)
        self._usage_range_combo = QComboBox(view)
        for key, label in _RANGE_CHOICES:
            self._usage_range_combo.addItem(label, key)
        self._usage_range_combo.setCurrentIndex(1)  # "week" default
        self._usage_range_combo.currentIndexChanged.connect(self._on_usage_range_changed)
        header_row.addWidget(self._usage_range_combo)
        self._usage_refresh_btn = cockpit_theme.secondary_button("Refresh", view)
        self._usage_refresh_btn.clicked.connect(self._on_usage_refresh_clicked)
        header_row.addWidget(self._usage_refresh_btn)
        lay.addLayout(header_row)

        self._usage_status_label = QLabel(
            "ตัวเลขด้านล่างมาจากรอบก่อนหน้า — กด Refresh เพื่อ import ล่าสุด (อาจใช้เวลาสักครู่ "
            "รันเบื้องหลัง ไม่ค้าง UI)",
            view,
        )
        self._usage_status_label.setObjectName("panelHint")
        self._usage_status_label.setWordWrap(True)
        lay.addWidget(self._usage_status_label)

        self._usage_cards_row = QHBoxLayout()
        self._usage_cards_row.setSpacing(10)
        cards_wrap = QWidget(view)
        cards_wrap.setLayout(self._usage_cards_row)
        lay.addWidget(cards_wrap)

        spark_panel = QWidget(view)
        spark_panel.setObjectName("panel")
        spark_lay = QVBoxLayout(spark_panel)
        spark_lay.setContentsMargins(14, 12, 14, 12)
        spark_lay.setSpacing(6)
        spark_lay.addWidget(
            self._build_card_header(
                "USAGE", f"แนวโน้ม {_SPARKLINE_DAYS} วัน (รวมทุก provider ที่นับได้)", "", spark_panel
            )
        )
        self._usage_sparkline = _Sparkline(spark_panel)
        spark_lay.addWidget(self._usage_sparkline)
        lay.addWidget(spark_panel)

        table_panel = QWidget(view)
        table_panel.setObjectName("panel")
        table_lay = QVBoxLayout(table_panel)
        table_lay.setContentsMargins(14, 12, 14, 12)
        table_lay.setSpacing(8)
        table_lay.addWidget(
            self._build_card_header("USAGE", "Provider → Account → Model", "", table_panel)
        )
        self._usage_table_grid = QGridLayout()
        self._usage_table_grid.setHorizontalSpacing(14)
        self._usage_table_grid.setVerticalSpacing(4)
        table_lay.addLayout(self._usage_table_grid)
        self._usage_uncountable_label = QLabel("", table_panel)
        self._usage_uncountable_label.setObjectName("panelHint")
        self._usage_uncountable_label.setWordWrap(True)
        table_lay.addWidget(self._usage_uncountable_label)
        lay.addWidget(table_panel)

        quota_panel = QWidget(view)
        quota_panel.setObjectName("panel")
        quota_lay = QVBoxLayout(quota_panel)
        quota_lay.setContentsMargins(14, 12, 14, 12)
        quota_lay.setSpacing(6)
        quota_lay.addWidget(self._build_card_header("USAGE", "% โควตาที่หักในช่วง", "", quota_panel))
        self._usage_quota_grid = QGridLayout()
        self._usage_quota_grid.setHorizontalSpacing(14)
        self._usage_quota_grid.setVerticalSpacing(4)
        quota_lay.addLayout(self._usage_quota_grid)
        lay.addWidget(quota_panel)

        self._usage_rtk_label = QLabel("", view)
        self._usage_rtk_label.setObjectName("panelHint")
        self._usage_rtk_label.setWordWrap(True)
        lay.addWidget(self._usage_rtk_label)
        lay.addStretch(1)

        self._usage_import_thread: _CallableThread | None = None
        self._render_usage()
        return view

    # ──────────────────────────────────────────────────────────
    # rendering (cheap — never scans transcripts, see module docstring)
    # ──────────────────────────────────────────────────────────

    def _render_usage(self) -> None:
        from . import usage_ledger

        kwargs = _range_query_kwargs(self._usage_range_combo.currentData() or "week")
        result = usage_ledger.query_usage(provider=None, **kwargs)
        self._apply_usage_result(result)
        self._usage_sparkline.set_points(usage_ledger.daily_series(days=_SPARKLINE_DAYS))

    def _apply_usage_result(self, result: dict) -> None:
        self._clear_layout(self._usage_cards_row)
        per_provider: dict[str, int] = {}
        for row in result.get("rows") or ():
            per_provider[row["provider"]] = per_provider.get(row["provider"], 0) + row["total"]
        if not per_provider:
            empty = QLabel("ยังไม่มีข้อมูล (ยังไม่เคย import หรือยังไม่มี turn ในช่วงนี้)", self)
            empty.setObjectName("panelHint")
            self._usage_cards_row.addWidget(empty)
        for provider, total in sorted(per_provider.items()):
            self._usage_cards_row.addWidget(self._build_usage_card(provider, total))
        self._usage_cards_row.addStretch(1)

        rows = result.get("rows") or []
        self._fill_grid(
            self._usage_table_grid,
            _TABLE_HEADERS,
            rows,
            lambda r: (
                r["provider"],
                r["account"],
                r["model"],
                str(r["turns"]),
                f"{r['input']:,}",
                f"{r['cache_creation']:,}",
                f"{r['cache_read']:,}",
                f"{r['output']:,}",
                f"{r['total']:,}",
            ),
            empty_text="(ไม่มีข้อมูลที่นับได้ในช่วงนี้)",
        )

        uncountable = result.get("uncountable") or []
        self._usage_uncountable_label.setText(
            " · ".join(f"{u['provider']}: นับไม่ได้ ({u['reason']})" for u in uncountable)
        )

        quota_rows = [q for q in (result.get("quota") or ()) if q.get("window") is not None]
        quota_uncountable = [q for q in (result.get("quota") or ()) if q.get("window") is None]
        self._fill_grid(
            self._usage_quota_grid,
            _QUOTA_HEADERS,
            quota_rows,
            lambda q: (
                q["provider"],
                q["account"],
                q["window"],
                "—" if q.get("delta_pct") is None else f"+{q['delta_pct']:.1f}%",
                str(q.get("samples", 0)),
            ),
            empty_text="(ไม่มีตัวอย่าง quota ในช่วงนี้)",
            trailer=" · ".join(
                f"{q['provider']}: นับไม่ได้ ({q.get('reason')})" for q in quota_uncountable
            ),
        )

        # `rtk_gain` no longer rides `query_usage()`'s own result (H2/H3,
        # 2026-09-07) — that subprocess call (~0.5s observed) has no
        # business running on every range switch/Settings open. `takkub
        # usage` (CLI) is the one place that still shows it.
        self._usage_rtk_label.setText(
            "rtk saved: รันคำสั่ง `takkub usage` (CLI) เพื่อดู — ไม่รวมกับตัวเลขข้างบน"
        )

    def _build_usage_card(self, provider: str, total: int) -> QWidget:
        card = QWidget(self)
        card.setObjectName("panel")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        name = QLabel(provider.upper(), card)
        name.setStyleSheet(
            f'font-family: "{self._fonts["mono"]}"; font-size: 11px; font-weight: 600; '
            f"color: {cockpit_theme.TEXT_FAINT};"
        )
        lay.addWidget(name)
        value = QLabel(format_tokens(total), card)
        value.setStyleSheet(
            f'font-family: "{self._fonts["mono"]}"; font-size: 20px; font-weight: 700; '
            f"color: {cockpit_theme.TEXT_PRIMARY_ALT};"
        )
        lay.addWidget(value)
        return card

    def _fill_grid(self, grid: QGridLayout, headers, rows, row_values, *, empty_text, trailer=""):
        self._clear_layout(grid)
        for col, text in enumerate(headers):
            lbl = QLabel(text, self)
            lbl.setStyleSheet(
                f'font-family: "{self._fonts["mono"]}"; font-size: 10px; font-weight: 600; '
                f"letter-spacing: 1px; color: {cockpit_theme.TEXT_FAINT};"
            )
            grid.addWidget(lbl, 0, col)
        if not rows:
            note = QLabel(empty_text, self)
            note.setObjectName("panelHint")
            grid.addWidget(note, 1, 0, 1, len(headers))
        for r_idx, row in enumerate(rows, start=1):
            for col, val in enumerate(row_values(row)):
                lbl = QLabel(str(val), self)
                lbl.setStyleSheet(
                    f'font-family: "{self._fonts["mono"]}"; color: {cockpit_theme.TEXT_SECONDARY};'
                )
                grid.addWidget(lbl, r_idx, col)
        if trailer:
            note = QLabel(trailer, self)
            note.setObjectName("panelHint")
            note.setWordWrap(True)
            grid.addWidget(note, len(rows) + 1, 0, 1, len(headers))

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    # ──────────────────────────────────────────────────────────
    # actions
    # ──────────────────────────────────────────────────────────

    def _on_usage_range_changed(self, _index: int) -> None:
        self._render_usage()

    def _on_usage_refresh_clicked(self) -> None:
        from . import usage_ledger

        # H5/H7 (2026-09-07): a rapid double-click previously started two
        # concurrent `import_all` runs — each with its own dedup cache,
        # each unaware of the other's in-flight writes — racing appends
        # into the same raw jsonl/cursor/daily.json. Ignore the click
        # while a refresh is already running instead.
        if self._usage_import_thread is not None and self._usage_import_thread.isRunning():
            return
        self._usage_refresh_btn.setEnabled(False)
        self._usage_status_label.setText("กำลัง import ข้อมูลล่าสุด (รันเบื้องหลัง)…")
        thread = _CallableThread(usage_ledger.import_all, self)
        thread.resultReady.connect(self._on_usage_import_ready)
        self._usage_import_thread = thread
        thread.start()

    def _on_usage_import_ready(self, result: object) -> None:
        self._usage_refresh_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._usage_status_label.setText(f"import ไม่สำเร็จ: {result}")
            return
        self._render_usage()
        now = datetime.now().strftime("%H:%M:%S")
        self._usage_status_label.setText(f"อัปเดตล่าสุดแล้ว ({now})")
