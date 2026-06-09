"""3-layer gate for ConPTY spawn safety.

Prevents RPC_E_CANTCALLOUT_ININPUTSYNCCALL (Windows fatal 0x8001010d) by
blocking winpty.PtyProcess.spawn() when the Qt main thread is inside an
input-synchronous COM/Windows call context:

  Layer 1 — Qt activeModalWidget:  QDialog.exec(), QWizard.exec(), QMessageBox
  Layer 2 — Qt activePopupWidget:  QMenu.exec(), combo-box popups
  Layer 3 — Win32 InSendMessageEx: direct detector for the COM call-out
             condition, covers drag/drop, resize loops, IME dispatch, and
             any nested native message pump that layers 1-2 miss.

Usage (from orchestrator, UI-agnostic):
    gate = is_spawn_blocked(modal_pred)   # modal_pred injected from main_window
    if gate:
        defer()
"""

from __future__ import annotations

import sys
from collections.abc import Callable


def is_in_send_blocked() -> bool:
    """True when Win32 says this thread is inside an input-synchronous SendMessage.

    This is the direct detector for RPC_E_CANTCALLOUT_ININPUTSYNCCALL.
    Returns False on non-Windows or if ctypes fails.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ISMEX_SEND = 0x1
        ISMEX_REPLIED = 0x8
        flags = ctypes.windll.user32.InSendMessageEx(None)
        return bool((flags & (ISMEX_REPLIED | ISMEX_SEND)) == ISMEX_SEND)
    except Exception:
        return False


def is_spawn_blocked(modal_pred: Callable[[], bool] | None) -> bool:
    """True when ConPTY spawn is unsafe on the current thread.

    modal_pred: Qt modal+popup predicate injected from main_window.
                Returns True when either activeModalWidget or activePopupWidget
                is not None.  Pass None in tests / non-GUI contexts.
    Also unconditionally checks Win32 InSendMessageEx on Windows.
    """
    if modal_pred is not None and modal_pred():
        return True
    return is_in_send_blocked()
