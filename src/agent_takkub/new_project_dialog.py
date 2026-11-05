""" "โปรเจคใหม่" template picker dialog (approved mockup:
docs/mockups/2026-11-05-new-project-template.html).

The catalog, presets and every file it writes live in ``project_template``;
this module is only the Qt surface: name + parent folder, preset chips, one
card per layer (single-pick chips with a "ไม่ใช้" chip, or multi-pick), and a
live right-hand panel (folder tree, compose services, AGENTS.md summary).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme as ct
from . import project_template as pt


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to the next row (chips)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, *, apply: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisibleTo(
                item.widget().parentWidget()
            ):
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + self._gap, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


def _flow_widget() -> tuple[QWidget, FlowLayout]:
    w = QWidget()
    lay = FlowLayout(w)
    sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    sp.setHeightForWidth(True)
    w.setSizePolicy(sp)
    return w, lay


def _swatch_icon(color: str) -> QIcon:
    """Solid colour dot for the สีหลัก chips (mockup: dot + plain text)."""
    pm = QPixmap(20, 20)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, 20, 20)
    painter.end()
    return QIcon(pm)


def _extra_qss() -> str:
    accent, chip_text = ct.ACCENT_GOLD, ct.GOLD_CHIP_TEXT
    return f"""
    QFrame#tplTitle, QFrame#tplFoot {{ background: {ct.GROUND_TITLEBAR}; }}
    QFrame#tplTitle {{ border-bottom: 1px solid {ct.BORDER_CARD}; }}
    QFrame#tplFoot {{ border-top: 1px solid {ct.BORDER_CARD}; }}
    QLabel#tplTitleText {{ font-size: 14px; font-weight: 700; color: {ct.TEXT_PRIMARY}; }}
    QLabel#tplMuted {{ color: {ct.TEXT_MUTED}; font-size: 12px; }}
    QLabel#tplFieldLabel {{ color: {ct.TEXT_SECONDARY}; font-size: 12px; }}
    QFrame#tplCard {{ background: {ct.GROUND_PANEL}; border: 1px solid {ct.BORDER_CARD}; border-radius: 8px; }}
    QFrame#tplBox {{ background: {ct.GROUND_INSET}; border: 1px solid {ct.BORDER_CARD}; border-radius: 8px; }}
    QLabel#tplCardTitle {{ font-weight: 700; color: {ct.TEXT_PRIMARY}; }}
    QLabel#tplDir {{ color: {chip_text}; font-family: Consolas, monospace; font-size: 12px; }}
    QLabel#tplHint, QLabel#tplSub {{ color: {ct.TEXT_MUTED}; font-size: 11px; }}
    QLabel#tplBoxTitle {{ color: {ct.TEXT_SECONDARY}; font-size: 12px; font-weight: 600; }}
    QLabel#tplTag {{ background: {ct.GROUND_PANEL_ALT}; border: 1px solid {ct.BORDER_CARD_ROW};
        border-radius: 4px; padding: 2px 7px; color: {ct.TEXT_SECONDARY}; font-size: 11px; }}
    QPushButton#tplChip {{ background: {ct.GROUND_INSET}; border: 1px solid {ct.BORDER_CONTROL};
        border-radius: 13px; padding: 4px 11px; color: {ct.TEXT_PRIMARY}; font-size: 12px; }}
    QPushButton#tplChip:hover {{ border-color: {ct.TEXT_MUTED}; }}
    QPushButton#tplChip:checked {{ background: rgba(99, 102, 241, 46); border-color: {accent}; color: #FFFFFF; }}
    QPushButton#tplChipNone {{ background: {ct.GROUND_INSET}; border: 1px dashed {ct.BORDER_CONTROL};
        border-radius: 13px; padding: 4px 11px; color: {ct.TEXT_MUTED}; font-size: 12px; }}
    QPushButton#tplChipNone:checked {{ border-color: {ct.TEXT_MUTED}; color: {ct.TEXT_SECONDARY}; }}
    QScrollArea#tplScroll, QWidget#tplScrollBody {{ background: transparent; border: none; }}
    """


class NewProjectDialog(ct.CockpitDialog):
    """Returns ``(name, target_dir, selection)`` through :meth:`result_value`."""

    def __init__(
        self, parent: QWidget | None = None, *, default_parent: Path | None = None
    ) -> None:
        self.sel: dict = pt.default_selection()
        self._chips: list[tuple[str, str, bool, QPushButton]] = []
        self._tag_labels: dict[QPushButton, tuple[QLabel, str, str]] = {}
        self._cards: dict[str, tuple[QFrame, QWidget | None]] = {}
        self._result: tuple[str, Path, dict] | None = None
        super().__init__(parent)
        self.setWindowTitle("โปรเจคใหม่")
        self.resize(1180, 900)
        self._build(default_parent or Path.home())
        self._refresh()

    # ── theme ──
    def retheme(self) -> None:
        fonts = ct.ensure_fonts_loaded()
        self.setStyleSheet(
            ct.build_stylesheet(str(fonts["sans"]), str(fonts["mono"])) + _extra_qss()
        )

    # ── build ──
    def _build(self, default_parent: Path) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title = QFrame()
        title.setObjectName("tplTitle")
        tl = QHBoxLayout(title)
        tl.setContentsMargins(16, 10, 16, 10)
        t1 = QLabel("โปรเจคใหม่")
        t1.setObjectName("tplTitleText")
        t2 = QLabel("· เลือกว่าจะมีระบบอะไรบ้าง")
        t2.setObjectName("tplMuted")
        tl.addWidget(t1)
        tl.addWidget(t2)
        tl.addStretch()
        root.addWidget(title)

        top = QGridLayout()
        top.setContentsMargins(16, 14, 16, 6)
        top.setHorizontalSpacing(12)
        lbl_name = QLabel("ชื่อโปรเจค")
        lbl_name.setObjectName("tplFieldLabel")
        lbl_parent = QLabel("สร้างใน")
        lbl_parent.setObjectName("tplFieldLabel")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("เช่น evc-charging")
        self.name_edit.textChanged.connect(lambda _t: self._refresh_side())
        self.parent_edit = QLineEdit(str(default_parent))
        browse = ct.secondary_button("เลือก…")
        browse.clicked.connect(self._browse)
        prow = QHBoxLayout()
        prow.setSpacing(6)
        prow.addWidget(self.parent_edit, 1)
        prow.addWidget(browse)
        top.addWidget(lbl_name, 0, 0)
        top.addWidget(lbl_parent, 0, 1)
        top.addWidget(self.name_edit, 1, 0)
        top.addLayout(prow, 1, 1)
        top.setColumnStretch(0, 10)
        top.setColumnStretch(1, 16)
        root.addLayout(top)

        presets_w, presets = _flow_widget()
        lbl = QLabel("เริ่มจาก preset:")
        lbl.setObjectName("tplFieldLabel")
        presets.addWidget(lbl)
        for name in pt.PRESETS:
            b = QPushButton(name)
            b.setObjectName("tplChip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, n=name: self._apply_preset(n))
            presets.addWidget(b)
        pwrap = QVBoxLayout()
        pwrap.setContentsMargins(16, 6, 16, 10)
        pwrap.addWidget(presets_w)
        root.addLayout(pwrap)

        main = QHBoxLayout()
        main.setContentsMargins(16, 0, 16, 14)
        main.setSpacing(12)
        scroll = QScrollArea()
        scroll.setObjectName("tplScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("tplScrollBody")
        cards = QVBoxLayout(body)
        cards.setContentsMargins(0, 0, 4, 0)
        cards.setSpacing(8)
        for layer in pt.LAYERS:
            cards.addWidget(self._build_card(layer))
        cards.addStretch()
        scroll.setWidget(body)
        main.addWidget(scroll, 1)
        main.addWidget(self._build_side())
        root.addLayout(main, 1)

        foot = QFrame()
        foot.setObjectName("tplFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 12, 16, 12)
        self.count_label = QLabel()
        self.count_label.setObjectName("tplMuted")
        fl.addWidget(self.count_label)
        fl.addStretch()
        cancel = ct.secondary_button("ยกเลิก")
        cancel.clicked.connect(self.reject)
        create = ct.gold_button("สร้างโปรเจค")
        create.clicked.connect(self._accept)
        fl.addWidget(cancel)
        fl.addWidget(create)
        root.addWidget(foot)

    def _chip(
        self, text: str, key: str, value: str, multi: bool, *, none: bool = False, tag: str = ""
    ) -> QPushButton:
        b = QPushButton("" if tag else text)
        b.setObjectName("tplChipNone" if none else "tplChip")
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _c=False: self._pick(key, value, multi))
        if tag:
            # Mockup: the runtime tag ("Node", "Python") sits after the name,
            # smaller and muted (accent when picked) — a QPushButton can't
            # style part of its text, so a click-through rich-text label
            # carries it and the button is sized around that label.
            lbl = QLabel(b)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setStyleSheet("background: transparent; border: none;")
            lay = QHBoxLayout(b)
            lay.setContentsMargins(12, 0, 12, 0)
            lay.addWidget(lbl)
            self._tag_labels[b] = (lbl, text, tag)
            self._paint_tag(b, False)
        self._chips.append((key, value, multi, b))
        return b

    def _paint_tag(self, b: QPushButton, on: bool) -> None:
        lbl, text, tag = self._tag_labels[b]
        color = ct.GOLD_CHIP_TEXT if on else ct.TEXT_MUTED
        lbl.setText(
            f'<span style="color:{"#FFFFFF" if on else ct.TEXT_PRIMARY}">{text}</span>'
            f'&nbsp;<span style="color:{color};font-size:11px">{tag}</span>'
        )
        hint = lbl.sizeHint()
        b.setFixedSize(hint.width() + 26, max(hint.height() + 10, 28))

    def _build_card(self, layer: pt.Layer) -> QFrame:
        card = QFrame()
        card.setObjectName("tplCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(7)
        head = QHBoxLayout()
        head.setSpacing(8)
        t = QLabel(layer.title)
        t.setObjectName("tplCardTitle")
        head.addWidget(t)
        if layer.dir:
            d = QLabel(layer.dir)
            d.setObjectName("tplDir")
            head.addWidget(d)
        head.addStretch()
        if layer.hint:
            h = QLabel(layer.hint)
            h.setObjectName("tplHint")
            head.addWidget(h)
        v.addLayout(head)
        chips_w, chips = _flow_widget()
        if not layer.multi and not layer.required:
            chips.addWidget(self._chip(pt.NONE, layer.id, "", False, none=True))
        for name, tag in layer.options:
            chips.addWidget(self._chip(name, layer.id, name, layer.multi, tag=tag))
        v.addWidget(chips_w)
        subs_box: QWidget | None = None
        if layer.subs:
            subs_box = QWidget()
            sv = QVBoxLayout(subs_box)
            sv.setContentsMargins(0, 2, 0, 0)
            sv.setSpacing(5)
            for sub in layer.subs:
                sl = QLabel(sub.label)
                sl.setObjectName("tplSub")
                sv.addWidget(sl)
                sw, sf = _flow_widget()
                for opt in sub.options:
                    b = self._chip(opt, sub.id, opt, False)
                    if sub.swatch and opt in pt.SWATCH:
                        b.setIcon(_swatch_icon(pt.SWATCH[opt]))
                        b.setIconSize(QSize(10, 10))
                    sf.addWidget(b)
                sv.addWidget(sw)
            v.addWidget(subs_box)
        self._cards[layer.id] = (card, subs_box)
        return card

    def _build_side(self) -> QWidget:
        side = QWidget()
        side.setFixedWidth(340)
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        def box(title: str) -> tuple[QFrame, QVBoxLayout]:
            f = QFrame()
            f.setObjectName("tplBox")
            lay = QVBoxLayout(f)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(6)
            t = QLabel(title)
            t.setObjectName("tplBoxTitle")
            t.setWordWrap(True)
            lay.addWidget(t)
            return f, lay

        f1, l1 = box("โครง folder ที่จะสร้าง")
        self.tree_label = QLabel()
        self.tree_label.setTextFormat(Qt.TextFormat.RichText)
        self.tree_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        l1.addWidget(self.tree_label)
        v.addWidget(f1)

        f2, l2 = box("docker-compose.yml จะมี service")
        self.svc_w, self.svc_flow = _flow_widget()
        l2.addWidget(self.svc_w)
        v.addWidget(f2)

        f3, l3 = box(
            "AGENTS.md จะเขียนให้ว่า "
            f'<span style="font-weight:400;color:{ct.TEXT_MUTED}">'
            "· claude, codex, agy, opencode, cursor อ่านไฟล์นี้ทุกตัว</span>"
        )
        self.rules_label = QLabel()
        self.rules_label.setTextFormat(Qt.TextFormat.RichText)
        self.rules_label.setWordWrap(True)
        l3.addWidget(self.rules_label)
        v.addWidget(f3)
        v.addStretch()
        return side

    # ── state ──
    def _pick(self, key: str, value: str, multi: bool) -> None:
        if multi:
            cur = list(self.sel.get(key) or [])
            self.sel[key] = [x for x in cur if x != value] if value in cur else [*cur, value]
        elif value:
            self.sel[key] = value
        else:
            self.sel.pop(key, None)
        self._refresh()

    def _apply_preset(self, name: str) -> None:
        self.sel = pt.preset(name)
        self._refresh()

    def _refresh(self) -> None:
        for key, value, multi, btn in self._chips:
            if multi:
                on = value in (self.sel.get(key) or [])
            elif value:
                on = self.sel.get(key) == value
            else:
                on = not self.sel.get(key)
            btn.setChecked(on)
            if btn in self._tag_labels:
                self._paint_tag(btn, on)
        for layer in pt.LAYERS:
            card, subs = self._cards[layer.id]
            used = layer.multi or layer.required or bool(self.sel.get(layer.id))
            if subs is not None:
                subs.setVisible(used)
            if used:
                card.setGraphicsEffect(None)
            elif card.graphicsEffect() is None:
                eff = QGraphicsOpacityEffect(card)
                eff.setOpacity(0.62)
                card.setGraphicsEffect(eff)
        self._refresh_side()

    def _refresh_side(self) -> None:
        name = self.name_edit.text().strip() or "project"
        accent, muted = ct.GOLD_CHIP_TEXT, ct.TEXT_MUTED
        rows = pt.tree_entries(name, self.sel)
        html = []
        for i, (label, comment, is_dir) in enumerate(rows):
            indent = label.startswith("  ")
            text = label.strip()
            color = accent if is_dir else ct.TEXT_PRIMARY
            if i == 0:
                prefix = ""
            elif indent:
                last_sub = i + 1 >= len(rows) or not rows[i + 1][0].startswith("  ")
                prefix = "│&nbsp;&nbsp;" + ("└─ " if last_sub else "├─ ")
            else:
                prefix = "└─ " if i == len(rows) - 1 else "├─ "
            cmt = f'&nbsp;&nbsp;<span style="color:{muted}"># {comment}</span>' if comment else ""
            html.append(f'{prefix}<span style="color:{color}">{text}</span>{cmt}')
        self.tree_label.setText(
            "<div style=\"font-family:Consolas,'Cascadia Code',monospace;font-size:12px;"
            f'line-height:150%;white-space:pre">{"<br>".join(html)}</div>'
        )
        while self.svc_flow.count():
            item = self.svc_flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        services = pt.compose_services(self.sel)
        for svc in services or ["— ไม่ได้ติ๊ก docker-compose —"]:
            tag = QLabel(svc)
            tag.setObjectName("tplTag")
            self.svc_flow.addWidget(tag)
        self.svc_w.updateGeometry()
        items = "".join(f"<li>{line}</li>" for line in pt.rules_lines(self.sel))
        self.rules_label.setText(
            f'<ul style="margin:0;margin-left:-22px;color:{ct.TEXT_SECONDARY};font-size:12px">{items}</ul>'
        )
        parts, extras, tools = pt.summary_counts(self.sel)
        self.count_label.setText(f"เลือกไว้ {parts} ส่วน · บริการเสริม {extras} · อื่นๆ {tools}")

    # ── actions ──
    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "เลือกโฟลเดอร์ที่จะสร้างโปรเจค", self.parent_edit.text()
        )
        if d:
            self.parent_edit.setText(d)

    def _accept(self) -> None:
        name = self.name_edit.text().strip()
        parent = Path(self.parent_edit.text().strip() or ".")
        if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|'):
            QMessageBox.warning(
                self, "ชื่อโปรเจคไม่ถูกต้อง", 'ใส่ชื่อโปรเจคที่ไม่มีเครื่องหมาย / \\ : * ? " < > |'
            )
            return
        if not self.parent_edit.text().strip() or not parent.is_dir():
            QMessageBox.warning(self, "โฟลเดอร์ไม่ถูกต้อง", 'โฟลเดอร์ "สร้างใน" ต้องมีอยู่จริง')
            return
        target = parent / name
        if target.exists() and any(target.iterdir()):
            ans = QMessageBox.question(
                self,
                "โฟลเดอร์มีอยู่แล้ว",
                f"'{target}' มีไฟล์อยู่แล้ว ระบบจะเพิ่มเฉพาะไฟล์ที่ยังไม่มี ไม่ทับของเดิม ทำต่อไหม?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._result = (name, target, dict(self.sel))
        self.accept()

    def result_value(self) -> tuple[str, Path, dict] | None:
        return self._result
