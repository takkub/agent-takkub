"""LimitPanelMixin — usage/limit telemetry wiring (refactor round 4, step B).

Extracted from ``MainWindow`` as a mixin. All methods access ``self.*``
attributes (``_limit_store``, ``_limit_label``, ``_usageUpdated``, ``tabs``)
initialised in ``MainWindow.__init__``.

Named ``limit_panel`` (not ``limit_status``) to avoid collision with the
data-layer module ``limit_status.py``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from PyQt6 import sip

from . import cockpit_theme
from .config import active_project


class LimitPanelMixin:
    """Mixin for cockpit usage/limit display (background thread → GUI thread)."""

    # ──────────────────────────────────────────────────────────────
    # limit-status store (thread-safe via _usageUpdated signal)
    # ──────────────────────────────────────────────────────────────

    def _init_limit_store(self) -> None:
        """Create the shared LimitStore and register every currently open project tab.

        Called once via QTimer.singleShot(3_000) so the boot sequence
        settles before the first HTTP hit.  Per-tab register/unregister
        hooks keep the poll set in sync as tabs open and close.
        """
        from . import limit_status, user_profile
        from .project_tab import ProjectTab

        # on_update runs in a daemon thread — emit signal so Qt queues the
        # call and _on_usage_updated executes on the GUI thread.
        # 600 s (was 120): the usage endpoint is aggressively rate-limited
        # per account (2026-07-17: Retry-After up to ~60 min, re-armed by
        # further attempts) and this machine legitimately runs several
        # cockpit instances. Utilisation moves slowly; a 10-min cadence
        # keeps the chip honest without feeding the penalty box. The
        # cross-process shared state in limit_status collapses concurrent
        # instances to ~one real fetch per interval on top of this.
        self._limit_store = limit_status.LimitStore(
            interval_s=600,
            on_update=lambda cd, data: self._usageUpdated.emit((cd, data)),
        )
        self._limit_store.start()

        # Register all tabs that already exist (initial tab + any restored tabs
        # that opened during the 3 s boot window).
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, ProjectTab):
                cd = user_profile.config_dir_for(tab.project_name)
                self._limit_store.register(cd)

    def _on_usage_updated(self, payload) -> None:
        """Slot — always called on the GUI thread (queued via _usageUpdated signal)."""
        if not hasattr(self, "_limit_label"):
            return
        config_dir, data = payload
        from pathlib import Path as _Path

        from . import user_profile as _up_uu

        active = active_project()[0] or ""
        active_cd = _up_uu.config_dir_for(active)
        if _Path(config_dir).resolve() == _Path(active_cd).resolve():
            self._refresh_limit_label(data)

    def _refresh_limit_label(self, data) -> None:
        # Defensive: the underlying QLabel can be torn down (e.g. its host tab
        # closed) while a queued usage-poll signal is still in flight. hasattr
        # only proves the Python attr exists, not that the C++ object is alive,
        # so check liveness here — the choke point every caller routes through.
        if sip.isdeleted(self._limit_label):
            return
        if data is None:
            self._limit_label.set_offline()
            self._limit_label.setToolTip("Usage unavailable (offline or not logged in)")
            return

        rate_limited = getattr(data, "status", "ok") == "rate_limited"
        window_map = {w.name: w for w in (data.windows or [])}
        now = datetime.now(tz=UTC)

        def _fmt_eta(w) -> str:
            """Clock-style time-left until reset: 'H:MM' or 'D:HH:MM' with days."""
            resets_at = getattr(w, "resets_at", None)
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
                return f"{days}:{hours:02d}:{mins:02d}"
            return f"{hours}:{mins:02d}"

        def _fmt(key: str, label: str) -> str:
            """Inline readout: window label + countdown-to-reset + utilisation,
            e.g. '5h 3:45 52%'. The bare '3:45 52% / 2:12 18%' this used to
            render told you two numbers with no way to tell which was the
            5-hour window and which was the 7-day one without opening the
            tooltip — the label makes that legible at a glance."""
            w = window_map.get(key)
            if w is None:
                return f"{label} —"
            # utilization None = the API payload carried no figure — show
            # "—" (unknown), never a fabricated 0%.
            pct = "—" if w.utilization is None else f"{round(w.utilization)}%"
            eta = _fmt_eta(w)
            return f"{label} {eta} {pct}" if eta else f"{label} {pct}"

        text = " · ".join([_fmt("five_hour", "5h"), _fmt("seven_day", "7d")])

        # Stale detection: how old is the payload behind this render? A
        # rate-limited emit re-serves the last good fetch, and with the
        # endpoint's long penalties that snapshot can be an hour+ old — a
        # frozen "52%" (or a post-reset "0%") shown as if live was exactly
        # the prod "0% ตลอด" bug. Age > 15 min → visible ⏳ marker.
        stale_age_s: float | None = None
        fetched_at = getattr(data, "fetched_at", None)
        if fetched_at is not None:
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            stale_age_s = max(0.0, (now - fetched_at).total_seconds())
        is_stale = stale_age_s is not None and stale_age_s > 900
        if is_stale:
            text += " ⏳"

        known_utils = [w.utilization for w in (data.windows or []) if w.utilization is not None]
        max_util = max(known_utils, default=0.0)
        if rate_limited:
            color = cockpit_theme.BANNER_WARN_BORDER
        elif max_util >= 80:
            color = cockpit_theme.STATE_ERROR_BRIGHT
        elif max_util >= 50:
            color = cockpit_theme.METER_AMBER
        elif is_stale or not known_utils:
            # Old snapshot or no utilisation figures at all — dim the chip so
            # it doesn't read as a confident live value.
            color = cockpit_theme.TEXT_MUTED
        else:
            # Calm state → Claude coral so the little spark reads as "a bit of
            # Claude" in the corner instead of a neutral grey system chip.
            color = cockpit_theme.METER_CLAY

        self._limit_label.apply(text, color)
        plan = getattr(data, "plan", "")
        note_bits = []
        if rate_limited:
            note_bits.append("rate-limited — showing last known values")
        if stale_age_s is not None:
            mins = int(stale_age_s // 60)
            note_bits.append(f"fetched {mins}m ago" if mins else "just fetched")
        if any(w.utilization is None for w in (data.windows or [])):
            note_bits.append("— = API ไม่ส่งค่า utilization (unknown ไม่ใช่ 0%)")
        stale_note = f" ({' · '.join(note_bits)})" if note_bits else ""
        reset_lines = []
        for key, label in (
            ("five_hour", "5h"),
            ("seven_day", "7d"),
        ):
            w = window_map.get(key)
            if w is not None and (eta := _fmt_eta(w)):
                reset_lines.append(f"{label} resets in {eta}")
        reset_block = ("\n" + " · ".join(reset_lines)) if reset_lines else ""
        self._limit_label.setToolTip(
            f"Claude usage — plan: {plan}{stale_note}\n5h = five-hour · 7d = seven-day{reset_block}"
        )
