"""Backlog popup (#684): a center-screen dialog over the cockpit.

Redesigned (2026-09-24) to Mockup A "Two-Column Structured Card List":
- Left: structured cards (status pill, severity, impact, age, title wrapping <=2 lines, source/reason)
- Right: inspector panel (top header + meta grid + scrollable detail + action buttons)
  with empty state summary metrics when no card is selected
- Header: project title + progress bar + filter chips with item counts
- Footer: order-mode toggle + pick count + close / confirm buttons

Reads and writes go through `orchestrator.backlog_command(...)` in-process on
the Qt main thread — the same entry point the CLI reaches over the socket, so
there is one source of truth for the store.

The dialog is display-only glue; the orderable-selection logic
(`ordered_selection`) is a pure function so it can be tested without Qt.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import backlog, cockpit_theme

# Filter chips, in owner reading order. "" = all.
_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "ทั้งหมด"),
    ("open", "ค้างอยู่"),
    ("todo", "ยังไม่ทำ"),
    ("doing", "กำลังทำ"),
    ("review", "รอยืนยัน"),
    ("waiting", "รอเจ้าของ"),
    ("blocked", "ติดอยู่"),
    ("deferred", "พักไว้"),
    ("done", "เสร็จ"),
)


def ordered_selection(picked_ids: list[str], items: list[dict]) -> list[dict]:
    """Return the *items* named by *picked_ids*, in the order they were picked.

    Pure (no Qt) so the "assign in the order the owner clicked" contract is
    testable directly. Ids not present in *items* are skipped."""
    by_id = {it.get("id"): it for it in items}
    return [by_id[i] for i in picked_ids if i in by_id]


def _to_qcolor(color_str: str) -> QColor:
    """Safely convert a hex or rgba(...) string to a valid QColor."""
    if color_str.startswith("rgba(") and color_str.endswith(")"):
        parts = color_str[5:-1].split(",")
        if len(parts) == 4:
            r = int(parts[0].strip())
            g = int(parts[1].strip())
            b = int(parts[2].strip())
            a_val = float(parts[3].strip())
            a = round(a_val * 255) if a_val <= 1.0 else round(a_val)
            return QColor(r, g, b, a)
    return QColor(color_str)


def _get_status_style(status: str) -> tuple[str, str, str, str]:
    """Return (label, text_color, bg_color, border_color) for status badge."""
    t = cockpit_theme
    label = backlog.STATUS_LABELS.get(status, status)
    if status == "doing":
        return label, t.STATE_DOING_TEXT, t.STATE_DOING_BG, t.STATE_DOING_BORDER
    if status == "review":
        return label, t.STATE_REVIEW_TEXT, t.STATE_REVIEW_BG, t.STATE_REVIEW_BORDER
    if status == "todo":
        return label, t.STATE_TODO_TEXT, t.STATE_TODO_BG, t.STATE_TODO_BORDER
    if status == "blocked":
        return label, t.STATE_BLOCKED_TEXT, t.STATE_BLOCKED_BG, t.STATE_BLOCKED_BORDER
    if status == "done":
        return label, t.STATE_DONE_TEXT, t.STATE_DONE_BG, t.STATE_DONE_BORDER
    return label, t.STATE_DEFERRED_TEXT, t.STATE_DEFERRED_BG, t.STATE_DEFERRED_BORDER


def _get_sev_style(sev: str) -> tuple[str, str, str]:
    """Return (text, text_color, bg_color) for severity badge."""
    t = cockpit_theme
    s = (sev or "med").lower()
    if s == "high":
        return "HIGH", t.SEV_HIGH_TEXT, t.SEV_HIGH_BG
    if s == "low":
        return "LOW", t.SEV_LOW_TEXT, t.SEV_LOW_BG
    return "MED", t.SEV_MED_TEXT, t.SEV_MED_BG


class MiniProgressBar(QWidget):
    """Mini 60x6px rounded progress bar matching Mockup A."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pct = 0.0
        self.setFixedSize(60, 6)

    def set_progress(self, done: int, total: int) -> None:
        self._pct = (done / total) if total > 0 else 0.0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        r = rect.height() / 2
        # track
        painter.setPen(Qt.PenStyle.NoPen)
        track_color = _to_qcolor(cockpit_theme.BORDER_STRONG)
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, r, r)
        # fill
        if self._pct > 0:
            fill_w = max(int(rect.width() * self._pct), int(r * 2))
            fill_rect = self.rect().adjusted(0, 0, -(rect.width() - fill_w), 0)
            painter.setBrush(QColor(cockpit_theme.STATE_OK))
            painter.drawRoundedRect(fill_rect, r, r)


class BacklogCardWidget(QFrame):
    """Individual card widget in the backlog list matching Mockup A."""

    def __init__(
        self,
        item: dict,
        order_index: int | None = None,
        list_widget: QListWidget | None = None,
        list_item: QListWidgetItem | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._item = item
        self._order_index = order_index
        self._list_widget = list_widget
        self._list_item = list_item
        self._selected = False
        self._has_subline = False
        self._build()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._update_frame_style()

    def mousePressEvent(self, event) -> None:
        if self._list_widget and self._list_item:
            self._list_widget.setCurrentItem(self._list_item)
            self._list_widget.itemClicked.emit(self._list_item)
        super().mousePressEvent(event)

    def _update_frame_style(self) -> None:
        t = cockpit_theme
        if self._selected:
            self.setStyleSheet(f"""
                QFrame#backlogCard {{
                    background: {t.GROUND_PANEL};
                    border: 1px solid {t.ACCENT_GOLD};
                    border-radius: 7px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame#backlogCard {{
                    background: {t.GROUND_PANEL_ALT};
                    border: 1px solid {t.BORDER_HAIRLINE};
                    border-radius: 7px;
                }}
                QFrame#backlogCard:hover {{
                    background: {t.HOVER_WEAK};
                    border-color: {t.BORDER_MED};
                }}
            """)

    def _build(self) -> None:
        self.setObjectName("backlogCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_frame_style()

        t = cockpit_theme
        fonts = t.ensure_fonts_loaded()
        sans = fonts["sans"]
        mono = fonts["mono"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        # Header line: Badges left + age right
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(5)
        hdr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Order badge
        if self._order_index is not None:
            order_badge = QLabel(str(self._order_index), self)
            order_badge.setFixedSize(18, 18)
            order_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            order_badge.setStyleSheet(f"""
                background: {t.ACCENT_GOLD};
                color: {t.GOLD_TEXT_ON};
                border-radius: 9px;
                font-family: "{sans}";
                font-size: 10px;
                font-weight: 700;
            """)
            hdr.addWidget(order_badge)

        # Status badge
        st_lbl, st_fg, st_bg, st_border = _get_status_style(self._item.get("status", ""))
        status_badge = QLabel(st_lbl, self)
        status_badge.setStyleSheet(f"""
            background: {st_bg};
            color: {st_fg};
            border: 1px solid {st_border};
            border-radius: 4px;
            padding: 2px 7px;
            font-family: "{sans}";
            font-size: 10.5px;
            font-weight: 600;
        """)
        hdr.addWidget(status_badge)

        # Severity badge
        sev_txt, sev_fg, sev_bg = _get_sev_style(self._item.get("severity", ""))
        sev_badge = QLabel(sev_txt, self)
        sev_badge.setStyleSheet(f"""
            background: {sev_bg};
            color: {sev_fg};
            border-radius: 4px;
            padding: 2px 6px;
            font-family: "{sans}";
            font-size: 10.5px;
            font-weight: 700;
        """)
        hdr.addWidget(sev_badge)

        # Customer badge
        if self._item.get("impact") == "customer":
            cust_badge = QLabel("🧑‍💼 ลูกค้า", self)
            cust_badge.setStyleSheet(f"""
                background: {t.BADGE_CUSTOMER_BG};
                color: {t.BADGE_CUSTOMER_TEXT};
                border: 1px solid {t.BADGE_CUSTOMER_BORDER};
                border-radius: 4px;
                padding: 2px 6px;
                font-family: "{sans}";
                font-size: 10.5px;
                font-weight: 600;
            """)
            hdr.addWidget(cust_badge)

        hdr.addStretch(1)

        # Age badge
        age = backlog.age_days(self._item)
        age_text = "วันนี้" if age == 0 else f"ดอง {age}d"
        age_fg = t.BADGE_AGE_WARN if age > 0 else t.TEXT_MUTED
        age_badge = QLabel(age_text, self)
        age_badge.setStyleSheet(f"""
            color: {age_fg};
            font-family: "{mono}";
            font-size: 11px;
            font-weight: 500;
        """)
        hdr.addWidget(age_badge)
        layout.addLayout(hdr)

        # Title line (wrapped <= 2 lines)
        title_lbl = QLabel(self._item.get("title", ""), self)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"""
            color: {t.TEXT_PRIMARY};
            font-family: "{sans}";
            font-size: 12.5px;
            font-weight: 600;
            line-height: 1.35;
        """)
        layout.addWidget(title_lbl)

        # Subline or reason
        st = self._item.get("status", "")
        reason = (self._item.get("reason") or "").strip()
        source = (self._item.get("source") or "").strip()
        links = self._item.get("links") or []

        sub_text = ""
        sub_style = f'color: {t.TEXT_MUTED}; font-family: "{sans}"; font-size: 10.5px;'

        if st == "blocked" and reason:
            sub_text = f"⚠️ {reason}"
            sub_style = f'color: {t.STATE_BLOCKED_TEXT}; font-family: "{sans}"; font-size: 11px; font-style: italic;'
        elif source:
            icon = (
                "👤" if "lead" in source.lower() else ("🔍" if "test" in source.lower() else "📁")
            )
            sub_text = f"{icon} {source}"
        elif links:
            roles = ", ".join(ln.get("role", "") for ln in links if ln.get("role"))
            if roles:
                sub_text = f"👤 {roles}"
        elif self._item.get("files"):
            sub_text = f"📄 {', '.join(self._item['files'][:2])}"

        if sub_text:
            self._has_subline = True
            sub_lbl = QLabel(sub_text, self)
            sub_lbl.setStyleSheet(sub_style)
            layout.addWidget(sub_lbl)

        for lbl in self.findChildren(QLabel):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _title_line_count(self) -> int:
        title = (self._item.get("title") or "").strip()
        if not title:
            return 1
        fonts = cockpit_theme.ensure_fonts_loaded()
        f = QFont(fonts["sans"])
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        # Inner width for title in a ~900px dialog list is ~405px
        rect = fm.boundingRect(QRect(0, 0, 405, 1000), Qt.TextFlag.TextWordWrap, title)
        line_spacing = max(1, fm.lineSpacing())
        lines = max(1, min(2, round(rect.height() / line_spacing)))
        return lines

    def sizeHint(self) -> QSize:
        lines = self._title_line_count()
        # Badges header: ~24px + 5px spacing
        # Title: 1 line ~18px, 2 lines ~38px
        # Subline: 5px spacing + ~16px subline = 21px
        # Layout margins: top 8px + bottom 8px = 16px
        # List item padding/margin buffer in QListWidget: 8px
        title_h = 18 if lines == 1 else 38
        sub_h = 21 if self._has_subline else 0
        total_h = 24 + 5 + title_h + sub_h + 16 + 8
        return QSize(400, total_h)


class BacklogDialog(cockpit_theme.CockpitDialog):
    """Center-screen backlog popup. `orch` is the live Orchestrator; `project`
    the namespace whose backlog to show."""

    def __init__(self, orch, project: str, parent: QWidget | None = None) -> None:
        self._orch = orch
        self._project = project
        self._filter = ""
        self._order_mode = False
        self._picked: list[str] = []
        self._items: list[dict] = []
        super().__init__(parent)
        self.setWindowTitle(f"Backlog — {self._project}")
        self.setModal(True)
        width, height = 1200, 800
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(width, int(avail.width() * 0.9))
            height = min(height, int(avail.height() * 0.9))
        self.resize(width, height)
        self.setMinimumSize(880, 560)
        self._build()
        self._reload()
        self._center_on_parent()

    def retheme(self) -> None:
        if not hasattr(self, "_filter_btns"):
            return
        self.setStyleSheet(self._qss())
        self._rebuild_list()
        self._update_inspector_for_item(self._current_item())

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # header: title + search + progress
        header = QHBoxLayout()
        title = QLabel("📋 Backlog ของโปรเจค", self)
        title.setObjectName("blTitle")
        header.addWidget(title)

        header.addSpacing(12)
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("blSearch")
        self._search_input.setPlaceholderText("🔍 ค้นหาชื่องาน หรือ #id…")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedWidth(240)
        self._search_input.textChanged.connect(self._on_search_changed)
        header.addWidget(self._search_input)

        header.addStretch(1)

        prog_box = QHBoxLayout()
        prog_box.setSpacing(8)
        prog_box.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._progress_lbl = QLabel("", self)
        self._progress_lbl.setObjectName("blProgress")
        prog_box.addWidget(self._progress_lbl)
        self._progress_bar = MiniProgressBar(self)
        prog_box.addWidget(self._progress_bar)
        header.addLayout(prog_box)
        root.addLayout(header)

        # filter chips
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._filter_btns: dict[str, QPushButton] = {}
        for value, label in _FILTERS:
            b = QPushButton(label, self)
            b.setCheckable(True)
            b.setObjectName("blFilter")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, v=value: self._set_filter(v))
            self._filter_btns[value] = b
            chips.addWidget(b)
        chips.addStretch(1)
        root.addLayout(chips)

        # body: left card list | right detail inspector
        body = QHBoxLayout()
        body.setSpacing(12)

        self._list = QListWidget(self)
        self._list.setObjectName("blList")
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentItemChanged.connect(self._on_select)
        self._list.itemClicked.connect(self._on_click)
        body.addWidget(self._list, 115)

        self._build_inspector()
        body.addWidget(self._inspector, 95)
        root.addLayout(body, 1)

        # footer: order-mode toggle + pick count + close / confirm buttons
        footer = QHBoxLayout()
        footer.setSpacing(10)

        self._order_cb = QCheckBox("โหมดเลือกลำดับงาน", self)
        self._order_cb.toggled.connect(self._toggle_order_mode)
        footer.addWidget(self._order_cb)

        self._pick_lbl = QLabel("", self)
        self._pick_lbl.setObjectName("blPick")
        footer.addWidget(self._pick_lbl)
        footer.addStretch(1)

        self._btn_close = QPushButton("ปิด", self)
        self._btn_close.setObjectName("blBtnClose")
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.clicked.connect(self.reject)
        footer.addWidget(self._btn_close)

        self._btn_ok = cockpit_theme.gold_button("ตกลง — สั่งทำงานที่เลือก", self)
        self._btn_ok.setObjectName("blBtnOk")
        self._btn_ok.clicked.connect(self._confirm)
        footer.addWidget(self._btn_ok)
        root.addLayout(footer)

        self._filter_btns[""].setChecked(True)
        self._refresh_order_ui()

    def _build_inspector(self) -> None:
        t = cockpit_theme
        fonts = t.ensure_fonts_loaded()
        sans = fonts["sans"]
        mono = fonts["mono"]

        self._inspector = QFrame(self)
        self._inspector.setObjectName("detailInspector")
        self._inspector.setStyleSheet(f"""
            QFrame#detailInspector {{
                background: {t.GROUND_INSET};
                border: 1px solid {t.BORDER_CARD};
                border-radius: 8px;
            }}
        """)
        insp_lay = QVBoxLayout(self._inspector)
        insp_lay.setContentsMargins(0, 0, 0, 0)
        insp_lay.setSpacing(0)

        # ── Empty State Panel ───────────────────────────────────────────────
        self._panel_empty = QWidget(self._inspector)
        empty_lay = QVBoxLayout(self._panel_empty)
        empty_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.setContentsMargins(20, 30, 20, 30)
        empty_lay.setSpacing(12)

        icon_circle = QLabel("📋", self._panel_empty)
        icon_circle.setFixedSize(52, 52)
        icon_circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_circle.setStyleSheet(f"""
            background: {t.GROUND_PANEL_ALT};
            border: 1px dashed {t.BORDER_STRONG};
            border-radius: 26px;
            font-size: 22px;
        """)
        empty_lay.addWidget(icon_circle, 0, Qt.AlignmentFlag.AlignCenter)

        empty_title = QLabel("ยังไม่ได้เลือกรายการ", self._panel_empty)
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setStyleSheet(f"""
            color: {t.TEXT_SECONDARY};
            font-family: "{sans}";
            font-size: 13.5px;
            font-weight: 700;
        """)
        empty_lay.addWidget(empty_title)

        empty_desc = QLabel(
            "คลิกเลือกการ์ดงานจากรายการฝั่งซ้ายเพื่อดูรายละเอียด ลิงก์ไฟล์ และสั่งเริ่มงาน",
            self._panel_empty,
        )
        empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_desc.setWordWrap(True)
        empty_desc.setMaximumWidth(260)
        empty_desc.setStyleSheet(f"""
            color: {t.TEXT_MUTED};
            font-family: "{sans}";
            font-size: 11.5px;
            line-height: 1.4;
        """)
        empty_lay.addWidget(empty_desc)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)
        metrics_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        pill_qss = f"""
            background: {t.GROUND_PANEL};
            border: 1px solid {t.BORDER_HAIRLINE};
            border-radius: 6px;
            padding: 4px 8px;
            font-family: "{sans}";
            font-size: 10.5px;
            color: {t.TEXT_SECONDARY};
        """
        self._empty_metric_todo = QLabel("⏳ รอทำ 0", self._panel_empty)
        self._empty_metric_todo.setStyleSheet(pill_qss)
        metrics_row.addWidget(self._empty_metric_todo)

        self._empty_metric_doing = QLabel("⚡ กำลังทำ 0", self._panel_empty)
        self._empty_metric_doing.setStyleSheet(pill_qss)
        metrics_row.addWidget(self._empty_metric_doing)

        self._empty_metric_blocked = QLabel("🚫 ติดขัด 0", self._panel_empty)
        self._empty_metric_blocked.setStyleSheet(pill_qss)
        metrics_row.addWidget(self._empty_metric_blocked)
        empty_lay.addLayout(metrics_row)
        insp_lay.addWidget(self._panel_empty)

        # ── Populated State Panel ───────────────────────────────────────────
        self._panel_populated = QWidget(self._inspector)
        pop_lay = QVBoxLayout(self._panel_populated)
        pop_lay.setContentsMargins(0, 0, 0, 0)
        pop_lay.setSpacing(0)

        # Top section: ID pill + Badges + Title + Meta Grid
        top_sec = QWidget(self._panel_populated)
        top_sec.setStyleSheet(f"""
            background: {t.GROUND_PANEL};
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            border-bottom: 1px solid {t.BORDER_CARD_ROW};
        """)
        top_lay = QVBoxLayout(top_sec)
        top_lay.setContentsMargins(14, 12, 14, 12)
        top_lay.setSpacing(8)

        id_row = QHBoxLayout()
        id_row.setContentsMargins(0, 0, 0, 0)
        id_row.setSpacing(6)
        self._insp_id = QLabel("", top_sec)
        self._insp_id.setStyleSheet(f"""
            font-family: "{mono}";
            font-size: 11px;
            color: {t.TEXT_MUTED};
            background: {t.GROUND_INSET};
            padding: 2px 7px;
            border-radius: 4px;
            border: 1px solid {t.BORDER_MED};
        """)
        id_row.addWidget(self._insp_id)
        id_row.addStretch(1)

        self._insp_status = QLabel("", top_sec)
        id_row.addWidget(self._insp_status)

        self._insp_sev = QLabel("", top_sec)
        id_row.addWidget(self._insp_sev)

        self._insp_cust = QLabel("🧑‍💼 ลูกค้า", top_sec)
        self._insp_cust.setStyleSheet(f"""
            background: {t.BADGE_CUSTOMER_BG};
            color: {t.BADGE_CUSTOMER_TEXT};
            border: 1px solid {t.BADGE_CUSTOMER_BORDER};
            border-radius: 4px;
            padding: 2px 6px;
            font-family: "{sans}";
            font-size: 10.5px;
            font-weight: 600;
        """)
        id_row.addWidget(self._insp_cust)
        top_lay.addLayout(id_row)

        self._insp_title = QLabel("", top_sec)
        self._insp_title.setWordWrap(True)
        self._insp_title.setStyleSheet(f"""
            color: {t.TEXT_PRIMARY};
            font-family: "{sans}";
            font-size: 13.5px;
            font-weight: 700;
            line-height: 1.35;
        """)
        top_lay.addWidget(self._insp_title)

        # Meta grid (2 cols)
        meta_box = QFrame(top_sec)
        meta_box.setObjectName("metaBox")
        meta_grid = QGridLayout(meta_box)
        meta_grid.setContentsMargins(10, 8, 10, 8)
        meta_grid.setHorizontalSpacing(16)
        meta_grid.setVerticalSpacing(8)

        lbl_qss = f'color: {t.TEXT_MUTED}; font-family: "{sans}"; font-size: 10px; font-weight: 600; text-transform: uppercase;'
        val_qss = (
            f'color: {t.TEXT_PRIMARY}; font-family: "{sans}"; font-size: 11px; font-weight: 500;'
        )

        def _make_meta_cell(title_text: str, val_widget: QWidget) -> tuple[QWidget, QLabel]:
            cell = QWidget(meta_box)
            c_lay = QVBoxLayout(cell)
            c_lay.setContentsMargins(0, 0, 0, 0)
            c_lay.setSpacing(2)
            lbl = QLabel(title_text, cell)
            lbl.setStyleSheet(lbl_qss)
            c_lay.addWidget(lbl)
            c_lay.addWidget(val_widget)
            return cell, lbl

        self._meta_impact_val = QLabel("", meta_box)
        self._meta_impact_val.setStyleSheet(val_qss)
        self._cell_impact, _ = _make_meta_cell("ผลกระทบ", self._meta_impact_val)
        meta_grid.addWidget(self._cell_impact, 0, 0)

        self._meta_age_val = QLabel("", meta_box)
        self._meta_age_val.setStyleSheet(val_qss)
        self._cell_age, _ = _make_meta_cell("ระยะเวลาในคลัง", self._meta_age_val)
        meta_grid.addWidget(self._cell_age, 0, 1)

        self._meta_src_val = QLabel("", meta_box)
        self._meta_src_val.setStyleSheet(val_qss)
        self._cell_src, self._meta_src_lbl = _make_meta_cell("ที่มา", self._meta_src_val)
        meta_grid.addWidget(self._cell_src, 1, 0)

        self._meta_files_val = QLabel("", meta_box)
        self._meta_files_val.setObjectName("metaFilesVal")
        self._cell_files, self._meta_files_lbl = _make_meta_cell("ไฟล์ที่แตะ", self._meta_files_val)
        meta_grid.addWidget(self._cell_files, 1, 1)

        self._meta_links_val = QLabel("", meta_box)
        self._meta_links_val.setStyleSheet(val_qss)
        self._cell_links, self._meta_links_lbl = _make_meta_cell(
            "ผู้รับงาน (ROLE)", self._meta_links_val
        )
        meta_grid.addWidget(self._cell_links, 2, 0)

        self._meta_reason_val = QLabel("", meta_box)
        self._meta_reason_val.setStyleSheet(
            f'color: {t.STATE_BLOCKED_TEXT}; font-family: "{sans}"; font-size: 11px; font-weight: 500;'
        )
        self._cell_reason, self._meta_reason_lbl = _make_meta_cell("เหตุผล", self._meta_reason_val)
        meta_grid.addWidget(self._cell_reason, 2, 1)

        top_lay.addWidget(meta_box)
        pop_lay.addWidget(top_sec)

        # Content section: Detail body
        content_box = QVBoxLayout()
        content_box.setContentsMargins(14, 10, 14, 10)
        content_box.setSpacing(6)

        sec_lbl = QLabel("📝 รายละเอียด / ข้อสังเกต:", self._panel_populated)
        sec_lbl.setStyleSheet(
            f'color: {t.TEXT_SECONDARY}; font-family: "{sans}"; font-size: 11px; font-weight: 600;'
        )
        content_box.addWidget(sec_lbl)

        self._detail = QTextEdit(self._panel_populated)
        self._detail.setReadOnly(True)
        self._detail.setObjectName("blDetail")
        self._detail.setStyleSheet(f"""
            QTextEdit#blDetail {{
                background: {t.GROUND_PANEL};
                color: {t.TEXT_PRIMARY};
                border: 1px solid {t.BORDER_HAIRLINE};
                border-radius: 6px;
                padding: 10px;
                font-family: "{sans}";
                font-size: 12px;
                line-height: 1.5;
            }}
        """)
        content_box.addWidget(self._detail, 1)
        pop_lay.addLayout(content_box, 1)

        # Actions bar
        actions_bar = QWidget(self._panel_populated)
        actions_bar.setStyleSheet(f"""
            background: {t.GROUND_PANEL};
            border-bottom-left-radius: 8px;
            border-bottom-right-radius: 8px;
            border-top: 1px solid {t.BORDER_CARD_ROW};
        """)
        act_lay = QHBoxLayout(actions_bar)
        act_lay.setContentsMargins(14, 10, 14, 10)
        act_lay.setSpacing(8)

        act_btn_qss = f"""
            QPushButton {{
                padding: 5px 12px;
                font-family: "{sans}";
                font-size: 11.5px;
                border-radius: 6px;
                font-weight: 600;
                border: 1px solid {t.BORDER_MED};
                background: {t.GROUND_PANEL_ALT};
                color: {t.TEXT_SECONDARY};
            }}
            QPushButton:hover {{
                background: {t.HOVER_WEAK};
                color: {t.TEXT_PRIMARY};
            }}
        """

        self._btn_defer = QPushButton("⏸ พักไว้", actions_bar)
        self._btn_defer.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_defer.setStyleSheet(act_btn_qss)
        self._btn_defer.clicked.connect(lambda: self._quick("defer"))
        act_lay.addWidget(self._btn_defer)

        self._btn_done = QPushButton("✓ เสร็จแล้ว", actions_bar)
        self._btn_done.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_done.setStyleSheet(act_btn_qss)
        self._btn_done.clicked.connect(lambda: self._quick("done"))
        act_lay.addWidget(self._btn_done)

        act_lay.addStretch(1)

        self._btn_start_single = QPushButton("🚀 สั่งทำใบนี้", actions_bar)
        self._btn_start_single.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_start_single.setStyleSheet(f"""
            QPushButton {{
                padding: 5px 14px;
                font-family: "{sans}";
                font-size: 11.5px;
                border-radius: 6px;
                font-weight: 700;
                border: 1px solid {t.ACCENT_GOLD};
                background: {t.ACCENT_GOLD};
                color: {t.GOLD_TEXT_ON};
            }}
            QPushButton:hover {{
                background: {t.GOLD_GRAD_TOP};
            }}
        """)
        self._btn_start_single.clicked.connect(self._confirm)
        act_lay.addWidget(self._btn_start_single)
        pop_lay.addWidget(actions_bar)

        insp_lay.addWidget(self._panel_populated)

    # ── data ────────────────────────────────────────────────────────────────
    def _reload(self, keep_id: str | None = None) -> None:
        # Fetch all items to compute filter chip counts & empty state metrics
        ok_all, _msg_all, payload_all = self._orch.backlog_command(
            "list", {"status": "", "from": "lead"}, project=self._project
        )
        all_items = payload_all.get("backlog_items", []) if ok_all else []
        done = payload_all.get("done", 0) if ok_all else 0
        total = payload_all.get("total", 0) if ok_all else 0

        # Update progress in header
        pct = round((done / total) * 100) if total > 0 else 0
        self._progress_lbl.setText(f"{done}/{total} เสร็จ ({pct}%)")
        self._progress_bar.set_progress(done, total)

        # Update filter chip counts
        counts = {
            "": total,
            "open": sum(1 for it in all_items if it.get("status") in backlog._OPEN_STATUSES),
        }
        for st in ("todo", "doing", "review", "waiting", "blocked", "deferred", "done"):
            counts[st] = sum(1 for it in all_items if it.get("status") == st)

        for v, b in self._filter_btns.items():
            cnt = counts.get(v, 0)
            base_lbl = dict(_FILTERS).get(v, v)
            b.setText(f"{base_lbl}  {cnt}")

        # Update empty state metrics
        self._empty_metric_todo.setText(f"⏳ รอทำ {counts.get('todo', 0)}")
        self._empty_metric_doing.setText(f"⚡ กำลังทำ {counts.get('doing', 0)}")
        self._empty_metric_blocked.setText(f"🚫 ติดขัด {counts.get('blocked', 0)}")

        # Fetch filtered items
        if self._filter:
            ok, _msg, payload = self._orch.backlog_command(
                "list", {"status": self._filter, "from": "lead"}, project=self._project
            )
            self._items = payload.get("backlog_items", []) if ok else []
        else:
            self._items = all_items

        self._rebuild_list(keep_id=keep_id)

    def _on_search_changed(self, text: str) -> None:
        self._search_query = text.strip().lower()
        self._rebuild_list()

    def _rebuild_list(self, keep_id: str | None = None) -> None:
        target_id = keep_id or self._current_id()
        self._list.clear()
        selected_row: QListWidgetItem | None = None
        query = (getattr(self, "_search_query", "") or "").strip().lower()
        clean_id = query.lstrip("#")

        for it in self._items:
            item_id = str(it.get("id") or "")
            if query:
                id_lower = item_id.lower()
                title_lower = str(it.get("title") or "").lower()
                match_id = bool(clean_id and clean_id in id_lower)
                match_title = bool(query in title_lower or (clean_id and clean_id in title_lower))
                if not (match_id or match_title):
                    continue

            order_idx = (
                (self._picked.index(item_id) + 1)
                if (self._order_mode and item_id in self._picked)
                else None
            )
            row = QListWidgetItem()
            # Not setText(): the list paints item text under the card widget on
            # hover/selection. The summary stays available to screen readers.
            row.setData(Qt.ItemDataRole.AccessibleTextRole, self._card_text(it))
            row.setData(Qt.ItemDataRole.UserRole, item_id)
            card = BacklogCardWidget(
                it,
                order_index=order_idx,
                list_widget=self._list,
                list_item=row,
            )
            row.setSizeHint(card.sizeHint())
            self._list.addItem(row)
            self._list.setItemWidget(row, card)
            if target_id and item_id == target_id:
                selected_row = row

        if selected_row is not None:
            self._list.setCurrentItem(selected_row)
            card = self._list.itemWidget(selected_row)
            if isinstance(card, BacklogCardWidget):
                card.set_selected(True)
            self._update_inspector_for_item(self._get_item_by_id(target_id))
        else:
            self._list.setCurrentItem(None)
            self._update_inspector_for_item(None)

        self._refresh_order_ui()

    def _card_text(self, it: dict) -> str:
        label = backlog.STATUS_LABELS.get(it.get("status", ""), it.get("status", ""))
        sev = it.get("severity", "")
        imp = " 🧑‍💼ลูกค้า" if it.get("impact") == "customer" else ""
        age = backlog.age_days(it)
        aged = f"  · ดอง {age}d" if age > 0 else ""
        prefix = ""
        if self._order_mode and it.get("id") in self._picked:
            prefix = f"⟨{self._picked.index(it['id']) + 1}⟩ "
        reason = f" ({it['reason']})" if it.get("reason") else ""
        return f"{prefix}[{label}{reason}] {sev}{imp}  ·  {it.get('title', '')}{aged}"

    def _get_item_by_id(self, item_id: str | None) -> dict | None:
        if not item_id:
            return None
        for it in self._items:
            if it.get("id") == item_id:
                return it
        return None

    def _current_id(self) -> str | None:
        cur = self._list.currentItem()
        return cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None

    def _current_item(self) -> dict | None:
        return self._get_item_by_id(self._current_id())

    def _update_inspector_for_item(self, item: dict | None, detail_text: str = "") -> None:
        t = cockpit_theme
        fonts = t.ensure_fonts_loaded()
        sans = fonts["sans"]

        if item is None:
            self._panel_empty.setVisible(True)
            self._panel_populated.setVisible(False)
            self._detail.setPlainText("")
            return

        self._panel_empty.setVisible(False)
        self._panel_populated.setVisible(True)

        self._insp_id.setText(f"#{item.get('id', '')}")
        self._insp_title.setText(item.get("title", ""))

        st_lbl, st_fg, st_bg, st_border = _get_status_style(item.get("status", ""))
        self._insp_status.setText(st_lbl)
        self._insp_status.setStyleSheet(f"""
            background: {st_bg};
            color: {st_fg};
            border: 1px solid {st_border};
            border-radius: 4px;
            padding: 2px 7px;
            font-family: "{sans}";
            font-size: 10.5px;
            font-weight: 600;
        """)

        sev_txt, sev_fg, sev_bg = _get_sev_style(item.get("severity", ""))
        self._insp_sev.setText(sev_txt)
        self._insp_sev.setStyleSheet(f"""
            background: {sev_bg};
            color: {sev_fg};
            border-radius: 4px;
            padding: 2px 6px;
            font-family: "{sans}";
            font-size: 10.5px;
            font-weight: 700;
        """)

        self._insp_cust.setVisible(item.get("impact") == "customer")

        # Meta grid
        self._meta_impact_val.setText(
            "ลูกค้า (customer)" if item.get("impact") == "customer" else "ภายใน (internal)"
        )
        age = backlog.age_days(item)
        ts = item.get("created_ts", 0)
        try:
            fmt_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        except (OverflowError, OSError, ValueError):
            fmt_date = ""
        self._meta_age_val.setText(f"ดอง {age} วัน ({fmt_date})" if age > 0 else f"วันนี้ ({fmt_date})")

        source = (item.get("source") or "").strip()
        self._cell_src.setVisible(bool(source))
        self._meta_src_val.setText(source)

        files = item.get("files") or []
        self._cell_files.setVisible(bool(files))
        if files:
            first = files[0]
            extra = f" (+{len(files) - 1})" if len(files) > 1 else ""
            self._meta_files_val.setText(f"{first}{extra}")
            self._meta_files_val.setToolTip(", ".join(files))

        links = item.get("links") or []
        roles = ", ".join(ln.get("role", "") for ln in links if ln.get("role"))
        self._cell_links.setVisible(bool(roles))
        self._meta_links_val.setText(roles)

        reason = (item.get("reason") or "").strip()
        self._cell_reason.setVisible(bool(reason))
        self._meta_reason_val.setText(reason)

        raw_detail = (item.get("detail") or "").strip()
        display_detail = (
            raw_detail if raw_detail else (detail_text.strip() or "(ไม่มีรายละเอียดเพิ่มเติม)")
        )
        self._detail.setPlainText(display_detail)

    # ── interactions ────────────────────────────────────────────────────────
    def _set_filter(self, value: str) -> None:
        self._filter = value
        for v, b in self._filter_btns.items():
            b.setChecked(v == value)
        self._reload()

    def _on_select(self, cur: QListWidgetItem | None, prev: QListWidgetItem | None = None) -> None:
        if prev is not None:
            w = self._list.itemWidget(prev)
            if isinstance(w, BacklogCardWidget):
                w.set_selected(False)
        if cur is None:
            self._update_inspector_for_item(None)
            return
        w = self._list.itemWidget(cur)
        if isinstance(w, BacklogCardWidget):
            w.set_selected(True)
        item_id = cur.data(Qt.ItemDataRole.UserRole)
        ok, _msg, payload = self._orch.backlog_command(
            "show", {"id": item_id, "from": "lead"}, project=self._project
        )
        item = payload.get("item") or self._get_item_by_id(item_id)
        detail_text = payload.get("detail", "") if ok else ""
        self._update_inspector_for_item(item, detail_text=detail_text)

    def _on_click(self, row: QListWidgetItem) -> None:
        if not self._order_mode:
            return
        item_id = row.data(Qt.ItemDataRole.UserRole)
        if item_id in self._picked:
            self._picked.remove(item_id)
        else:
            self._picked.append(item_id)
        self._rebuild_list(keep_id=item_id)
        self._refresh_order_ui()

    def _toggle_order_mode(self, on: bool) -> None:
        self._order_mode = on
        if not on:
            self._picked.clear()
        self._rebuild_list()
        self._refresh_order_ui()

    def _refresh_order_ui(self) -> None:
        if self._order_mode:
            self._pick_lbl.setText(f"เลือกไว้ {len(self._picked)} งาน (คลิกการ์ดเพื่อเรียงลำดับ)")
            self._btn_ok.setEnabled(bool(self._picked))
            self._btn_ok.setText(f"ตกลง — สั่งทำ {len(self._picked)} งานตามลำดับ")
        else:
            self._pick_lbl.setText("")
            self._btn_ok.setEnabled(True)
            self._btn_ok.setText("ตกลง — สั่งทำงานที่เลือก")

    def _current_id(self) -> str | None:
        cur = self._list.currentItem()
        return cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None

    def _quick(self, verb: str) -> None:
        item_id = self._current_id()
        if item_id is None:
            return
        self._orch.backlog_command(verb, {"id": item_id, "from": "lead"}, project=self._project)
        self._reload(keep_id=item_id)

    def _confirm(self) -> None:
        if self._order_mode:
            ids = list(self._picked)
        else:
            cur = self._current_id()
            ids = [cur] if cur else []
        ordered = [it["id"] for it in ordered_selection(ids, self._items)]
        if not ordered:
            return
        self._orch.backlog_command(
            "dispatch", {"ids": ordered, "from": "lead"}, project=self._project
        )
        self.accept()

    def _center_on_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        pg = parent.frameGeometry()
        g = self.frameGeometry()
        g.moveCenter(pg.center())
        self.move(g.topLeft())

    # ── style ───────────────────────────────────────────────────────────────
    def _qss(self) -> str:
        t = cockpit_theme
        fonts = t.ensure_fonts_loaded()
        sans = fonts["sans"]
        mono = fonts["mono"]
        return f"""
        QDialog {{
            background: {t.GROUND_PANEL};
            color: {t.TEXT_PRIMARY};
            font-family: "{sans}";
        }}
        QLabel#blTitle {{
            color: {t.TEXT_PRIMARY};
            font-family: "{sans}";
            font-size: 15px;
            font-weight: 700;
        }}
        QLineEdit#blSearch {{
            background: {t.GROUND_INPUT};
            border: 1px solid {t.BORDER_MED};
            border-radius: 6px;
            padding: 4px 10px;
            font-family: "{sans}";
            font-size: 11.5px;
            color: {t.TEXT_PRIMARY};
        }}
        QLineEdit#blSearch:focus {{
            border: 1px solid {t.ACCENT_GOLD};
        }}
        QLineEdit#blSearch::placeholder {{
            color: {t.TEXT_MUTED};
        }}
        QLabel#blProgress {{
            color: {t.TEXT_SECONDARY};
            font-family: "{sans}";
            font-size: 12px;
        }}
        QLabel#blPick {{
            color: {t.ACCENT_GOLD_TEXT};
            font-family: "{sans}";
            font-size: 11.5px;
            font-weight: 600;
        }}
        QPushButton#blFilter {{
            color: {t.TEXT_SECONDARY};
            background: transparent;
            border: 1px solid {t.BORDER_STRONG};
            border-radius: 999px;
            padding: 3px 10px;
            font-family: "{sans}";
            font-size: 11.5px;
            font-weight: 600;
        }}
        QPushButton#blFilter:hover {{
            background: {t.HOVER_WEAK};
            color: {t.TEXT_PRIMARY};
        }}
        QPushButton#blFilter:checked {{
            color: {t.GOLD_CHIP_TEXT};
            border-color: {t.ACCENT_GOLD};
            background: {t.GOLD_CHIP_BG};
        }}
        QListWidget#blList {{
            background: {t.GROUND_INSET};
            color: {t.TEXT_PRIMARY};
            border: 1px solid {t.BORDER_CARD};
            border-radius: 8px;
            padding: 5px;
            outline: none;
        }}
        QListWidget#blList::item {{
            background: transparent;
            border: none;
            padding: 2px 0px;
            margin-bottom: 4px;
        }}
        QListWidget#blList::item:selected {{
            background: transparent;
        }}
        QListWidget#blList::item:hover {{
            background: transparent;
        }}
        QFrame#metaBox {{
            background: {t.GROUND_PANEL_ALT};
            border: 1px solid {t.BORDER_HAIRLINE};
            border-radius: 6px;
        }}
        QFrame#metaBox QLabel {{
            background: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }}
        QLabel#metaFilesVal {{
            font-family: "{mono}";
            font-size: 10px;
            color: {t.ACCENT_GOLD_TEXT};
            background: {t.GOLD_CHIP_BG};
            border: 1px solid {t.GOLD_CHIP_BORDER};
            padding: 2px 6px;
            border-radius: 4px;
        }}
        QPushButton#blBtnClose {{
            background: {t.GROUND_PANEL_ALT};
            border: 1px solid {t.BORDER_MED};
            color: {t.TEXT_SECONDARY};
            border-radius: 6px;
            padding: 6px 16px;
            font-family: "{sans}";
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton#blBtnClose:hover {{
            background: {t.HOVER_WEAK};
            color: {t.TEXT_PRIMARY};
            border-color: {t.BORDER_STRONG};
        }}
        QPushButton#blBtnOk {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {t.GOLD_GRAD_TOP}, stop:1 {t.GOLD_GRAD_BOTTOM});
            color: {t.GOLD_TEXT_ON};
            font-family: "{sans}";
            font-size: 12px;
            font-weight: 700;
            border: none;
            border-radius: 6px;
            padding: 7px 22px;
            min-height: 18px;
        }}
        QPushButton#blBtnOk:hover {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {t.GOLD_GRAD_HOVER_TOP}, stop:1 {t.GOLD_GRAD_TOP});
        }}
        QPushButton#blBtnOk:disabled {{
            background: {t.GROUND_SELECT};
            color: {t.TEXT_FAINT};
        }}
        QCheckBox {{
            color: {t.TEXT_SECONDARY};
            font-family: "{sans}";
            font-size: 12.5px;
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 15px;
            height: 15px;
            border: 1px solid {t.BORDER_STRONG};
            border-radius: 3px;
            background: {t.GROUND_INPUT};
        }}
        QCheckBox::indicator:checked {{
            background: {t.ACCENT_GOLD};
            border: 1px solid {t.ACCENT_GOLD};
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 8px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {t.BORDER_CONTROL};
            border-radius: 4px;
            min-height: 20px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {t.TEXT_MUTED};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
            border: none;
            background: none;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}
        """
