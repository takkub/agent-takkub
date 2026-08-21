"""UsageMeter — the multi-provider usage readout that lives in the pane-tabs corner.

A small static Claude "spark" next to a compact readout for whichever tracked
provider is closest to its limit. Click (or hover for the quick summary) to
see every provider's detail in a popup card list.

Pure-leaf UI: no orchestrator/app/cli imports. The parent (LimitPanelMixin)
builds the ``ProviderUsage`` list from the usage payload(s) and calls
``set_usages()`` / ``set_offline()``; this widget only owns the drawing.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPolygonF
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import cockpit_theme
from .auto_resume import CONFIRM_UTILIZATION_PCT as _AUTO_CONTINUE_HINT_PCT
from .provider_usage import ProviderUsage

_BAR_HEIGHT = 6
_BAR_LABEL_WIDTH = 24

# Claude's brand coral — the spark's calm/default colour so the corner always
# reads as "a little Claude", not a grey system chip (cockpit_theme.METER_CLAY).
_CLAUDE_CORAL = cockpit_theme.METER_CLAY

# Display name per provider key. Unknown providers fall back to the raw key.
PROVIDER_LABELS: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
    "kimi": "Kimi",
    "cursor": "Cursor",
}

_STATUS_SORT_ORDER = {"active": 0, "stale": 1, "loading": 2, "error": 3, "unsupported": 4}
_DETAIL_POPUP_WIDTH = 380
_DETAIL_POPUP_SCREEN_MARGIN = 8


def _popup_position(
    anchor_global: QRect,
    popup_size: QSize,
    available: QRect,
    *,
    margin: int = _DETAIL_POPUP_SCREEN_MARGIN,
) -> QPoint:
    """Right-align a popup to its anchor and keep it inside the usable screen."""
    left_bound = available.x() + margin
    top_bound = available.y() + margin
    right_bound = available.x() + available.width() - margin
    bottom_bound = available.y() + available.height() - margin

    preferred_x = anchor_global.x() + anchor_global.width() - popup_size.width()
    max_x = max(left_bound, right_bound - popup_size.width())
    x = min(max(preferred_x, left_bound), max_x)

    below_y = anchor_global.y() + anchor_global.height()
    above_y = anchor_global.y() - popup_size.height()
    preferred_y = below_y if below_y + popup_size.height() <= bottom_bound else above_y
    max_y = max(top_bound, bottom_bound - popup_size.height())
    y = min(max(preferred_y, top_bound), max_y)
    return QPoint(x, y)


def fmt_eta(resets_at: datetime | None, now: datetime) -> str:
    """Compact 'time until reset', e.g. '2ชม. 14น.' or '3วัน 1ชม.'."""
    if resets_at is None:
        return ""
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=UTC)
    secs = (resets_at - now).total_seconds()
    if secs <= 0:
        return "now"
    mins = int(secs // 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}วัน {hours}ชม."
    if hours:
        return f"{hours}ชม. {mins}น."
    return f"{mins}น."


def fmt_age(fetched_at: datetime | None, now: datetime) -> str:
    """Human age of a fetch, e.g. '3 นาทีที่แล้ว' or '2 เดือนที่แล้ว' (gemini's
    cache can be stale for a very long time — must stay legible that far out)."""
    if fetched_at is None:
        return ""
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    secs = max(0.0, (now - fetched_at).total_seconds())
    mins = int(secs // 60)
    if mins < 1:
        return "เมื่อครู่"
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days >= 30:
        return f"{days // 30} เดือนที่แล้ว"
    if days:
        return f"{days} วันที่แล้ว"
    if hours:
        return f"{hours} ชม.ที่แล้ว"
    return f"{mins} นาทีที่แล้ว"


def severity_color(pct: float | None) -> str:
    """Utilisation → chip colour. None (unknown) is deliberately NOT 0%/calm —
    it gets the neutral muted tone, never the "all clear" clay."""
    if pct is None:
        return cockpit_theme.TEXT_MUTED
    if pct >= 80:
        return cockpit_theme.STATE_ERROR_BRIGHT
    if pct >= 50:
        return cockpit_theme.METER_AMBER
    return _CLAUDE_CORAL


def _provider_body_entries(
    u: ProviderUsage, now: datetime
) -> list[tuple[str, str, str] | tuple[str, str, float, str, bool]]:
    """Per-provider detail entries for the popup card: ``("text", text, color)``
    for a plain line, or ``("bar", label, pct_used, color, stale)`` for a quota
    meter. Bar fill is always **% of quota already used** — the same number
    `severity_color()` takes and the same one Codex's/Claude's text already
    quotes ("ใช้ไป NN%") — so a card is never a bar meaning "left" next to a
    line meaning "used". Only real quota fractions (`utilization`/window
    `utilization`) get a bar; token-only providers (opencode's `spend`) and
    unsupported/loading/error providers never get a fabricated denominator."""
    if u.status == "unsupported":
        return [("text", "ไม่มีข้อมูลให้ดู (provider นี้ไม่รองรับ)", cockpit_theme.TEXT_FAINT)]
    if u.status == "loading":
        return [("text", "กำลังโหลด…", cockpit_theme.TEXT_MUTED)]
    if u.status == "error":
        return [("text", "ดึงข้อมูลไม่สำเร็จ", cockpit_theme.STATE_ERROR_BRIGHT)]

    entries: list[tuple[str, str, str] | tuple[str, str, float, str, bool]] = []
    stale = u.status == "stale"
    windows = (u.raw_data or {}).get("windows") if u.provider == "claude" else None
    if windows:
        for key, wlabel in (("five_hour", "5h"), ("seven_day", "7d")):
            w = windows.get(key)
            if w is None:
                continue
            pct = w.get("utilization")
            text = "—" if pct is None else f"{round(pct)}%"
            eta = w.get("eta") or ""
            line = f"{wlabel}: {text}" + (f" · reset ใน {eta}" if eta else "")
            # #322: at/near the 5h window's limit, cockpit's own park→wake
            # (limit_autoresume.py) is a safety net, not the primary
            # mechanism — Claude Code 2.1.234+ auto-continues the pane's
            # own session the instant this window resets, no cockpit nudge
            # needed. Surfaced only for five_hour (the window that actually
            # blocks a pane mid-task) and only near/at the confirm threshold
            # auto-resume itself uses, so this never claims "will
            # auto-continue" for an ordinary, still-working utilization %.
            if key == "five_hour" and pct is not None and pct >= _AUTO_CONTINUE_HINT_PCT:
                line += " · pane จะทำงานต่อเอง (auto-continue)"
            color = severity_color(pct)
            if pct is not None:
                entries.append(("bar", wlabel, pct, color, stale))
            entries.append(("text", line, color))
        entries.append(("text", "ยอดรวมทั้งบัญชี ไม่ใช่ pane นี้เท่านั้น", cockpit_theme.TEXT_FAINT))
    elif u.utilization is not None:
        pct_used = round(u.utilization)
        pct_left = max(0, round(100 - u.utilization))
        eta = fmt_eta(u.resets_at, now)
        line = f"เหลือ {pct_left}% (ใช้ไป {pct_used}%)"
        if eta:
            line += f" · reset ใน {eta}"
        color = severity_color(u.utilization)
        entries.append(("bar", "", u.utilization, color, stale))
        entries.append(("text", line, color))
    elif u.spend:
        bits = []
        input_tok = u.spend.get("input_tokens") or 0
        output_tok = u.spend.get("output_tokens") or 0
        total_tok = input_tok + output_tok
        if total_tok:
            bits.append(f"ใช้ไปแล้ว {total_tok:,} tokens")
        cost_usd = u.spend.get("cost_usd")
        if cost_usd:
            bits.append(f"${cost_usd:.2f}")
        if bits:
            # No quota denominator exists for a self-tallied token count — a
            # bar here would fabricate a "% full" that doesn't exist. Number
            # only, per contract (see docstring + provider_usage.py's own
            # "never blended into a utilization meter" rule).
            entries.append(("text", " · ".join(bits) + " (ไม่ใช่โควต้า)", cockpit_theme.TEXT_MUTED))
        else:
            entries.append(("text", "ไม่มีข้อมูลให้ดู", cockpit_theme.TEXT_FAINT))
    else:
        if stale and u.fetched_at:
            entries.append(("text", "ข้อมูลโควต้าหมดอายุ", cockpit_theme.TEXT_FAINT))
        else:
            entries.append(("text", "ไม่มีข้อมูลให้ดู", cockpit_theme.TEXT_FAINT))

    age = fmt_age(u.fetched_at, now)
    stale_enough = u.fetched_at is not None and (now - _aware(u.fetched_at)).total_seconds() > 900
    if age and (stale or stale_enough):
        entries.append(("text", f"ข้อมูลเมื่อ {age}", cockpit_theme.TEXT_FAINT))
    # Some channels cannot refresh themselves at all — gemini reads a cache
    # only the Antigravity desktop app writes, so "stale" there is permanent
    # until the user opens that app, not a poll that hasn't landed yet. Every
    # line above reads as "an update is on its way", which is the opposite of
    # the truth, so a stale snapshot carrying its adapter's own hint gets to
    # say why. `status == "error"` returned earlier, so this only ever renders
    # a hint attached to a stale (still-usable) snapshot.
    if stale and u.error:
        entries.append(("text", u.error, cockpit_theme.TEXT_FAINT))
    return entries


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class _Spark(QWidget):
    """A 4-point Claude sparkle — static (drawn once, no animation).

    Drawn with QPainter (no image asset → scales crisply at any DPI and never
    ships a binary). Deliberately still: an animated pulse in the corner of the
    eye is distracting, so the mark just sits calmly and re-tints by usage.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self._color = QColor(_CLAUDE_CORAL)

    def set_color(self, color: str) -> None:
        c = QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self.width() / 2 - 1
        inner = r * 0.34  # waist of the sparkle → concave 4-point star

        # 8 vertices: outer tip, inner waist, outer tip, … around the circle.
        pts = []
        for i in range(8):
            ang = math.pi / 2 * (i / 2)  # 0, 45, 90, … degrees in radians
            rad = r if i % 2 == 0 else inner
            pts.append(
                (cx + rad * math.cos(ang - math.pi / 2), cy + rad * math.sin(ang - math.pi / 2))
            )
        poly = QPolygonF([QPointF(x, y) for x, y in pts])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self._color))
        p.drawPolygon(poly)
        p.end()


class _UsageBar(QWidget):
    """Thin quota meter — fill = % of quota already used, same convention as
    ``severity_color()``'s input and the sibling text line's "ใช้ไป NN%".

    ``stale=True`` swaps the flat fill for a diagonal hatch instead of a
    reduced-alpha flat fill: a dimmed-but-still-solid green bar next to
    "ข้อมูลเมื่อ 5 เดือนที่แล้ว" would still read as "confirmed mostly free"
    at a glance, which is exactly the false all-clear the task calls out.
    """

    def __init__(
        self, pct_used: float, color: str, *, stale: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._pct = max(0.0, min(100.0, pct_used))
        self._color = QColor(color)
        self._stale = stale
        self.setFixedHeight(_BAR_HEIGHT)
        self.setMinimumWidth(60)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        radius = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(cockpit_theme.GROUND_SELECT)))
        p.drawRoundedRect(r, radius, radius)

        fill_w = round(r.width() * self._pct / 100)
        if fill_w > 0:
            fill_rect = QRect(r.x(), r.y(), fill_w, r.height())
            if self._stale:
                p.setBrush(QBrush(self._color, Qt.BrushStyle.BDiagPattern))
            else:
                p.setBrush(QBrush(self._color))
            p.drawRoundedRect(fill_rect, radius, radius)
        p.end()


def _build_bar_row(
    label: str, pct_used: float, color: str, stale: bool, parent: QWidget
) -> QWidget:
    row = QWidget(parent)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    if label:
        tag = QLabel(label, row)
        tag.setFixedWidth(_BAR_LABEL_WIDTH)
        tag.setStyleSheet(f"color:{cockpit_theme.TEXT_MUTED}; font-size:10px; font-weight:600;")
        lay.addWidget(tag)
    lay.addWidget(_UsageBar(pct_used, color, stale=stale, parent=row), 1)
    return row


def _build_provider_card(u: ProviderUsage, now: datetime) -> QWidget:
    card = QFrame()
    card.setObjectName("usageProviderCard")
    card.setStyleSheet(
        f"QFrame#usageProviderCard {{"
        f" background: {cockpit_theme.GROUND_PANEL};"
        f" border: 1px solid {cockpit_theme.BORDER_HAIRLINE};"
        f" border-radius: {cockpit_theme.RADIUS_SM}px; }}"
    )
    lay = QVBoxLayout(card)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(3)

    label = PROVIDER_LABELS.get(u.provider, u.provider)
    header_text = label if not u.plan else f"{label} · {u.plan}"
    header = QLabel(header_text, card)
    header.setStyleSheet(f"color:{cockpit_theme.TEXT_PRIMARY}; font-size:12px; font-weight:600;")
    lay.addWidget(header)

    for entry in _provider_body_entries(u, now):
        if entry[0] == "bar":
            _, bar_label, pct_used, color, stale = entry
            lay.addWidget(_build_bar_row(bar_label, pct_used, color, stale, card))
        else:
            _, text, color = entry
            line = QLabel(text, card)
            line.setWordWrap(True)
            line.setStyleSheet(f"color:{color}; font-size:11px;")
            lay.addWidget(line)

    return card


class _ProviderDetailPopup(QWidget):
    """Frameless auto-dismissing popup listing every tracked provider.

    ``Qt.WindowType.Popup`` grabs the mouse and closes itself on the first
    click outside, so no explicit focus-out wiring is needed.
    """

    def __init__(self, usages: list[ProviderUsage], anchor: QWidget) -> None:
        super().__init__(anchor.window(), Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        now = datetime.now(tz=UTC)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setObjectName("usagePopupFrame")
        frame.setStyleSheet(
            f"QFrame#usagePopupFrame {{"
            f" background: {cockpit_theme.GROUND_WINDOW};"
            f" border: 1px solid {cockpit_theme.BORDER_HAIRLINE};"
            f" border-radius: {cockpit_theme.RADIUS_MD}px; }}"
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 12, 12, 12)
        inner.setSpacing(8)
        for u in sorted(
            usages, key=lambda u: (_STATUS_SORT_ORDER.get(u.status, 5), -(u.utilization or -1))
        ):
            inner.addWidget(_build_provider_card(u, now))
        outer.addWidget(frame)
        self.adjustSize()


class UsageMeter(QWidget):
    """Spark + compact multi-provider readout, mounted in the active tab's corner.

    Header shows only the provider closest to its limit (plus a '+N' count of
    other tracked providers) so the corner never gets cluttered by 6 chips —
    unsupported providers never appear here, only in the click-through popup.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(5)
        self._spark = _Spark(self)
        self._label = QLabel("—", self)
        self._label.setStyleSheet(f"QLabel {{ color:{cockpit_theme.TEXT_MUTED}; font-size:11px; }}")
        lay.addWidget(self._spark)
        lay.addWidget(self._label)
        self._usages: list[ProviderUsage] = []

    def set_usages(
        self, usages: list[ProviderUsage], *, primary_provider: str | None = None
    ) -> None:
        """Show usage with the active Lead provider in the compact header.

        Older callers may omit ``primary_provider`` and retain the original
        worst-utilisation selection.  The popup always keeps every provider.
        """
        self._usages = list(usages)
        now = datetime.now(tz=UTC)
        primary = next((u for u in usages if u.provider == (primary_provider or "").lower()), None)
        candidates = [
            u for u in usages if u.status in ("active", "stale") and u.utilization is not None
        ]
        if primary is not None:
            leading = primary
            label = PROVIDER_LABELS.get(leading.provider, leading.provider)
            # Claude tracks two rolling windows (5h + 7d); `leading.utilization`
            # is max(known_utils) across both (see limit_panel._claude_provider_usage),
            # which can silently surface the 7-day % here instead of the 5-hour
            # one users actually watch during a session. Pull the 5h window
            # directly so the compact chip always matches the old pre-multi-
            # provider format: countdown + percent for the window that matters.
            five_hour_pct: float | None = None
            five_hour_eta = ""
            if leading.provider == "claude":
                windows = (leading.raw_data or {}).get("windows") or {}
                five_hour = windows.get("five_hour") or {}
                five_hour_pct = five_hour.get("utilization")
                five_hour_eta = five_hour.get("eta") or ""
            if five_hour_pct is not None:
                text = f"{label} {round(five_hour_pct)}%"
                if five_hour_eta:
                    text += f" · {five_hour_eta}"
                color = severity_color(five_hour_pct)
            elif leading.utilization is not None:
                text = f"{label} {round(leading.utilization)}%"
                color = severity_color(leading.utilization)
            elif leading.status == "loading":
                text, color = f"{label} …", cockpit_theme.TEXT_MUTED
            elif leading.status == "error":
                text, color = f"{label} !", cockpit_theme.STATE_ERROR_BRIGHT
            elif leading.spend:
                spend = leading.spend or {}
                total_tok = (spend.get("input_tokens") or 0) + (spend.get("output_tokens") or 0)
                text, color = f"{label} {total_tok:,} tok", cockpit_theme.TEXT_MUTED
            else:
                text, color = f"{label} —", cockpit_theme.TEXT_FAINT
            if leading.status == "stale":
                text += " ⏳"
                color = cockpit_theme.TEXT_MUTED
            extra = sum(
                1 for u in usages if u is not leading and u.status in ("active", "stale", "loading")
            )
            if extra:
                text += f" +{extra}"
        elif candidates:
            leading = max(candidates, key=lambda u: u.utilization)
            label = PROVIDER_LABELS.get(leading.provider, leading.provider)
            text = f"{label} {round(leading.utilization)}%"
            color = (
                cockpit_theme.TEXT_MUTED
                if leading.status == "stale"
                else severity_color(leading.utilization)
            )
            if leading.status == "stale":
                text += " ⏳"
            extra = sum(
                1 for u in usages if u is not leading and u.status in ("active", "stale", "loading")
            )
            if extra:
                text += f" +{extra}"
        else:
            token_only = next(
                (u for u in usages if u.status == "active" and u.spend),
                None,
            )
            if token_only:
                label = PROVIDER_LABELS.get(token_only.provider, token_only.provider)
                spend = token_only.spend or {}
                total_tok = (spend.get("input_tokens") or 0) + (spend.get("output_tokens") or 0)
                text = f"{label} {total_tok:,} tok"
                color = cockpit_theme.TEXT_MUTED
            else:
                text, color = "usage —", cockpit_theme.TEXT_FAINT

        self._spark.set_color(color)
        self._label.setText(text)
        self._label.setStyleSheet(f"QLabel {{ color:{color}; font-size:11px; }}")
        self.setToolTip(self._quick_tooltip(now))

    def _quick_tooltip(self, now: datetime) -> str:
        lines = ["คลิกเพื่อดูรายละเอียดแต่ละ provider"]
        for u in self._usages:
            if u.status == "unsupported":
                continue
            label = PROVIDER_LABELS.get(u.provider, u.provider)
            if u.utilization is not None:
                lines.append(f"{label}: เหลือ {max(0, round(100 - u.utilization))}%")
            elif u.status == "loading":
                lines.append(f"{label}: กำลังโหลด…")
            elif u.status == "error":
                lines.append(f"{label}: ดึงข้อมูลไม่สำเร็จ")
            elif u.spend:
                total_tok = (u.spend.get("input_tokens") or 0) + (u.spend.get("output_tokens") or 0)
                lines.append(f"{label}: ใช้ไป {total_tok:,} tok (ไม่ใช่โควต้า)")
            else:
                lines.append(f"{label}: ไม่มีข้อมูล")
        return "\n".join(lines)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self._usages:
            popup = _ProviderDetailPopup(self._usages, self)
            screen = self.screen()
            available = screen.availableGeometry()
            popup.setFixedWidth(
                max(
                    1, min(_DETAIL_POPUP_WIDTH, available.width() - 2 * _DETAIL_POPUP_SCREEN_MARGIN)
                )
            )
            popup.adjustSize()
            anchor_global = QRect(self.mapToGlobal(self.rect().topLeft()), self.size())
            popup.move(_popup_position(anchor_global, popup.size(), available))
            self._detail_popup = popup
            popup.show()
        super().mousePressEvent(ev)

    def set_offline(self) -> None:
        """Dim state when usage is unavailable (offline / not logged in)."""
        self._usages = []
        self._spark.set_color(cockpit_theme.TEXT_FAINT)
        self._label.setText("—")
        self._label.setStyleSheet(f"QLabel {{ color:{cockpit_theme.TEXT_FAINT}; font-size:11px; }}")
