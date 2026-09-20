"""Backlog popup (#684): a center-screen dialog over the cockpit.

Owner-facing planning surface for the project backlog (`backlog.py`). Left is
a scrollable list of cards (one per item, grouped-sortable, aged-to-top);
right is the selected card's full detail. A "เลือกลำดับ" (pick-order) mode lets
the owner click cards to build an ordered work queue; pressing ตกลง fires a
`takkub assign` for each picked item, in the picked order.

Reads and writes go through `orchestrator.backlog_command(...)` in-process on
the Qt main thread — the same entry point the CLI reaches over the socket, so
there is one source of truth for the store.

The dialog is display-only glue; the orderable-selection logic
(`ordered_selection`) is a pure function so it can be tested without Qt.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
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


class BacklogDialog(QDialog):
    """Center-screen backlog popup. `orch` is the live Orchestrator; `project`
    the namespace whose backlog to show."""

    def __init__(self, orch, project: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orch = orch
        self._project = project
        self._filter = ""
        self._order_mode = False
        self._picked: list[str] = []  # item ids, in click order
        self._items: list[dict] = []

        self.setWindowTitle("📋 Backlog")
        self.setModal(True)
        self.setMinimumSize(860, 560)
        self._build()
        self._reload()
        self._center_on_parent()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # header: title + progress
        header = QHBoxLayout()
        title = QLabel("📋 Backlog ของโปรเจค")
        title.setObjectName("blTitle")
        header.addWidget(title)
        header.addStretch(1)
        self._progress_lbl = QLabel("")
        self._progress_lbl.setObjectName("blProgress")
        header.addWidget(self._progress_lbl)
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

        # body: left list | right detail
        body = QHBoxLayout()
        body.setSpacing(12)
        self._list = QListWidget(self)
        self._list.setObjectName("blList")
        self._list.currentItemChanged.connect(self._on_select)
        self._list.itemClicked.connect(self._on_click)
        body.addWidget(self._list, 3)

        self._detail = QTextEdit(self)
        self._detail.setReadOnly(True)
        self._detail.setObjectName("blDetail")
        body.addWidget(self._detail, 2)
        root.addLayout(body, 1)

        # footer: order-mode toggle + role picker + confirm
        footer = QHBoxLayout()
        self._order_cb = QCheckBox("โหมดเลือกลำดับงาน", self)
        self._order_cb.toggled.connect(self._toggle_order_mode)
        footer.addWidget(self._order_cb)
        self._pick_lbl = QLabel("")
        self._pick_lbl.setObjectName("blPick")
        footer.addWidget(self._pick_lbl)
        footer.addStretch(1)

        # (owner 2026-09-20) no role picker here — sizing/routing is the
        # Lead's job. ตกลง hands the picked items to the Lead, which chooses
        # who does what and fires `takkub backlog assign` itself.
        self._btn_defer = cockpit_theme.secondary_button("พักไว้", self)
        self._btn_defer.clicked.connect(lambda: self._quick("defer"))
        footer.addWidget(self._btn_defer)
        self._btn_done = cockpit_theme.secondary_button("เสร็จแล้ว", self)
        self._btn_done.clicked.connect(lambda: self._quick("done"))
        footer.addWidget(self._btn_done)

        self._btn_ok = cockpit_theme.gold_button("ตกลง — สั่งทำตามลำดับ", self)
        self._btn_ok.clicked.connect(self._confirm)
        footer.addWidget(self._btn_ok)
        root.addLayout(footer)

        self._filter_btns[""].setChecked(True)
        self._refresh_order_ui()

    # ── data ────────────────────────────────────────────────────────────────
    def _reload(self) -> None:
        ok, _msg, payload = self._orch.backlog_command(
            "list", {"status": self._filter, "from": "lead"}, project=self._project
        )
        self._items = payload.get("items", []) if ok else []
        done = payload.get("done", 0)
        total = payload.get("total", 0)
        self._progress_lbl.setText(f"{done}/{total} เสร็จ")
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        self._list.clear()
        for it in self._items:
            row = QListWidgetItem(self._card_text(it))
            row.setData(Qt.ItemDataRole.UserRole, it.get("id"))
            self._list.addItem(row)
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

    # ── interactions ────────────────────────────────────────────────────────
    def _set_filter(self, value: str) -> None:
        self._filter = value
        for v, b in self._filter_btns.items():
            b.setChecked(v == value)
        self._reload()

    def _on_select(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None:
            self._detail.setPlainText("")
            return
        item_id = cur.data(Qt.ItemDataRole.UserRole)
        ok, _msg, payload = self._orch.backlog_command(
            "show", {"id": item_id, "from": "lead"}, project=self._project
        )
        self._detail.setPlainText(payload.get("detail", "") if ok else "")

    def _on_click(self, row: QListWidgetItem) -> None:
        if not self._order_mode:
            return
        item_id = row.data(Qt.ItemDataRole.UserRole)
        if item_id in self._picked:
            self._picked.remove(item_id)
        else:
            self._picked.append(item_id)
        self._rebuild_list()
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
        self._reload()

    def _confirm(self) -> None:
        if self._order_mode:
            ids = list(self._picked)
        else:
            cur = self._current_id()
            ids = [cur] if cur else []
        ordered = [it["id"] for it in ordered_selection(ids, self._items)]
        if not ordered:
            return
        # Hand the picked items to the Lead in the picked order — the Lead
        # sizes/routes each one itself (no role picker here by owner request).
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
        return f"""
        QDialog {{ background: {t.GROUND_PANEL}; }}
        QLabel#blTitle {{ color: {t.TEXT_PRIMARY}; font-size: 15px; font-weight: 600; }}
        QLabel#blProgress {{ color: {t.TEXT_SECONDARY}; }}
        QLabel#blPick {{ color: {t.TEXT_SECONDARY}; }}
        QPushButton#blFilter {{
            color: {t.TEXT_SECONDARY}; background: transparent;
            border: 1px solid {t.BORDER_STRONG}; border-radius: {t.RADIUS_MD}px;
            padding: 3px 10px; font-weight: 600;
        }}
        QPushButton#blFilter:checked {{
            color: {t.ACCENT_GOLD_TEXT}; border-color: {t.ACCENT_GOLD};
            background: rgba(99,102,241,0.12);
        }}
        QListWidget#blList {{
            background: {t.GROUND_SIDEBAR}; color: {t.TEXT_PRIMARY};
            border: 1px solid {t.BORDER_CARD}; border-radius: {t.RADIUS_MD}px;
            padding: 4px;
        }}
        QListWidget#blList::item {{ padding: 8px 6px; border-radius: 6px; }}
        QListWidget#blList::item:selected {{ background: rgba(99,102,241,0.18); }}
        QTextEdit#blDetail {{
            background: {t.GROUND_SIDEBAR}; color: {t.TEXT_PRIMARY};
            border: 1px solid {t.BORDER_CARD}; border-radius: {t.RADIUS_MD}px;
            padding: 8px;
        }}
        """
