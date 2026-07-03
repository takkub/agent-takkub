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
        self._limit_store = limit_status.LimitStore(
            interval_s=120,
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

        def _fmt(key: str) -> str:
            """Inline readout: countdown-to-reset + utilisation, e.g. '3:45 52%'.
            The clock (time left until the window resets) is the thing worth
            glancing at — not which window it is — so no '5h'/'7d' label."""
            w = window_map.get(key)
            if w is None:
                return "—"
            pct = f"{round(w.utilization)}%"
            eta = _fmt_eta(w)
            return f"{eta} {pct}" if eta else pct

        text = " / ".join([_fmt("five_hour"), _fmt("seven_day")])

        max_util = max((w.utilization for w in (data.windows or [])), default=0.0)
        if rate_limited:
            color = "#a16207"
        elif max_util >= 80:
            color = "#f87171"
        elif max_util >= 50:
            color = "#fbbf24"
        else:
            # Calm state → Claude coral so the little spark reads as "a bit of
            # Claude" in the corner instead of a neutral grey system chip.
            color = "#d97757"

        self._limit_label.apply(text, color)
        plan = getattr(data, "plan", "")
        stale_note = " (rate-limited, showing last known)" if rate_limited else ""
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
