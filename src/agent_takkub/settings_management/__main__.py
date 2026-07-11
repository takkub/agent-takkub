"""Dev entry point for the redesigned Settings window, standalone:

    python -m agent_takkub.settings_management

Does NOT touch the legacy status-bar Settings button — see
``feature_flags.py`` for the ``TAKKUB_SETTINGS_UI`` flag this window is
built behind.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .window import SettingsManagementWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = SettingsManagementWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
