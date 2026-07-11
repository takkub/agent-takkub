"""Dev entry point for the redesigned Settings window, standalone:

    python -m agent_takkub.settings_management

Does NOT touch the legacy status-bar Settings button — see
``feature_flags.py`` for the ``TAKKUB_SETTINGS_UI`` flag this window is
built behind.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QWidget

from .window import SettingsManagementWindow


def _open_legacy_settings(parent: QWidget) -> None:
    """ "Open legacy settings" hook for the standalone entry point — no
    MainWindow/orchestrator here (this is a dev entry point, run outside the
    cockpit), so just show the legacy ``SettingsWindow`` as a modal dialog in
    this same process instead of the app.py-level provider-apply dance
    ``user_actions._open_legacy_settings_window`` does for the real cockpit
    (codex cross-check MEDIUM-2: this hook was a no-op lambda before,
    leaving the button dead in standalone mode)."""
    from ..settings_window import VIEW_PROVIDERS_ROLES, SettingsWindow

    try:
        from ..config import active_project as _active_project

        project, _ = _active_project()
    except Exception:
        project = None

    dlg = SettingsWindow(parent, project=project, initial_view=VIEW_PROVIDERS_ROLES)
    dlg.exec()


def main() -> int:
    app = QApplication(sys.argv)
    window = SettingsManagementWindow()
    window.open_legacy_requested = lambda: _open_legacy_settings(window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
