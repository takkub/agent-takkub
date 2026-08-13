"""LimitPanelMixin — usage/limit telemetry wiring (refactor round 4, step B).

Extracted from ``MainWindow`` as a mixin. All methods access ``self.*``
attributes (``_limit_store``, ``_limit_label``, ``_usageUpdated``, ``tabs``)
initialised in ``MainWindow.__init__``.

Named ``limit_panel`` (not ``limit_status``) to avoid collision with the
data-layer module ``limit_status.py``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PyQt6 import sip

from .config import active_project
from .usage_meter import ProviderUsage, fmt_eta


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
        claude_usage = _claude_provider_usage(data)
        self._limit_label.set_usages([claude_usage, *_fake_other_provider_usages()])


def _claude_provider_usage(data) -> ProviderUsage:
    """Convert the existing per-account `UsageData` (limit_status.py) into the
    shared `ProviderUsage` shape. Claude's figures are for the WHOLE ACCOUNT,
    not this pane/project — the '5h'/'7d' windows note reflects that in the
    popup, not just here."""
    if data is None:
        return ProviderUsage(provider="claude", status="error")

    now = datetime.now(tz=UTC)
    rate_limited = getattr(data, "status", "ok") == "rate_limited"
    windows = data.windows or []
    known_utils = [w.utilization for w in windows if w.utilization is not None]
    resets = [w.resets_at for w in windows if getattr(w, "resets_at", None) is not None]
    fetched_at = getattr(data, "fetched_at", None)

    windows_raw = {}
    for w in windows:
        windows_raw[w.name] = {"utilization": w.utilization, "eta": fmt_eta(w.resets_at, now)}

    return ProviderUsage(
        provider="claude",
        status="stale" if rate_limited else "active",
        plan=getattr(data, "plan", None),
        utilization=max(known_utils) if known_utils else None,
        resets_at=min(resets) if resets else None,
        fetched_at=fetched_at,
        raw_data={"windows": windows_raw},
    )


def _fake_other_provider_usages() -> list[ProviderUsage]:
    """ponytail: placeholder data until backend's `provider_usage.py` ships
    (see docs/design/2026-08-13-provider-usage-abstraction.md). Swap this for
    real fetch results once that module lands — the state coverage here
    (active/stale/loading/unsupported/error, token-not-quota raw_data) mirrors
    the agreed contract so the UI above doesn't need to change, only this
    function's body."""
    now = datetime.now(tz=UTC)
    return [
        ProviderUsage(provider="codex", status="loading"),
        ProviderUsage(
            provider="gemini",
            status="stale",
            plan="Gemini Advanced",
            utilization=35.0,
            fetched_at=now - timedelta(days=41),
        ),
        ProviderUsage(
            provider="opencode",
            status="active",
            fetched_at=now,
            raw_data={"tokens_used": 128_430, "cost_usd": 4.32},
        ),
        ProviderUsage(provider="kimi", status="unsupported"),
        ProviderUsage(provider="cursor", status="unsupported"),
    ]
