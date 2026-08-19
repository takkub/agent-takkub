"""macOS Keychain-backed secret backend — wraps the same read-only
`security find-generic-password` call `limit_status._read_keychain_credentials`
already uses for the usage meter. Deliberately read-only: that function's
docstring explains why writing here would be wrong — "we never write the
Keychain, so Claude Code's own credential management is never disturbed".
set()/delete() keep that guarantee by refusing outright rather than risking
it silently.
"""

from __future__ import annotations

import subprocess
import sys

from . import BackendStatus, SecretUnavailableError

_TIMEOUT_S = 5


class KeychainBackend:
    name = "keychain"

    def __init__(self, service: str) -> None:
        self._service = service

    def _available(self) -> bool:
        return sys.platform == "darwin"

    def status(self, account_id: str) -> BackendStatus:
        if not self._available():
            return BackendStatus.UNAVAILABLE
        return BackendStatus.FOUND if self.get(account_id) is not None else BackendStatus.MISSING

    def get(self, account_id: str) -> str | None:
        if not self._available():
            return None
        from agent_takkub._win_console import SUBPROCESS_NO_WINDOW

        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", self._service, "-w"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_S,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        blob = proc.stdout.strip()
        return blob or None

    def set(self, account_id: str, value: str) -> None:
        raise SecretUnavailableError(
            "KeychainBackend is read-only by design — writing would risk fighting "
            "Claude Code's own Keychain credential refresh "
            "(see limit_status._read_keychain_credentials)"
        )

    def delete(self, account_id: str) -> None:
        raise SecretUnavailableError("KeychainBackend is read-only by design — see set()")
