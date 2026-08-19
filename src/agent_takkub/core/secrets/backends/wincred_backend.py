"""Windows Credential Manager-backed secret backend, via ctypes bindings to
`advapi32.dll` (`CredReadW`/`CredFree`). Read-only for now: `CredWrite`'s
`CREDENTIALW` struct is easy to get wrong (wide-char marshaling, blob
byte-vs-char-count) and nothing in this repo writes through this backend
yet — no provider's credential is confirmed to live in the Windows
Credential Manager today (`config.PROVIDER_ISOLATION_GAPS`), so this class
is forward-looking infra, not wired into `manager.default_backends()`.

TODO(#309): implement set()/delete() (CredWriteW/CredDeleteW) once a real
caller needs to write a Windows-Credential-Manager-backed secret.
"""

from __future__ import annotations

import ctypes
import sys

from . import BackendStatus, SecretUnavailableError

_CRED_TYPE_GENERIC = 1


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_byte * 8),
        ("CredentialBlobSize", ctypes.c_ulong),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", ctypes.c_ulong),
        ("AttributeCount", ctypes.c_ulong),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _advapi32():
    """Isolated so tests can monkeypatch it instead of touching the real
    Windows Credential Manager."""
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("advapi32", use_last_error=True)


def _cred_read_bytes(target_name: str) -> bytes | None:
    """The one function tests monkeypatch to fake a Credential Manager
    entry: `ctypes.byref()`'s output-pointer semantics only work through a
    genuine foreign-function call, so a fake `advapi32` object can't stand
    in for `CredReadW` itself the way `subprocess.run` can be swapped in
    `keychain_backend.py` — isolating the boundary here is what makes this
    backend testable without one."""
    advapi32 = _advapi32()
    if advapi32 is None:
        return None
    pcred = ctypes.POINTER(_CREDENTIAL)()
    ok = advapi32.CredReadW(
        ctypes.c_wchar_p(target_name), _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)
    )
    if not ok:
        return None
    try:
        cred = pcred.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi32.CredFree(pcred)


class WinCredBackend:
    name = "wincred"

    def __init__(self, target_prefix: str) -> None:
        self._target_prefix = target_prefix

    def _target_name(self, account_id: str) -> str:
        return f"{self._target_prefix}/{account_id}"

    def _available(self) -> bool:
        return sys.platform == "win32"

    def status(self, account_id: str) -> BackendStatus:
        if not self._available():
            return BackendStatus.UNAVAILABLE
        return BackendStatus.FOUND if self.get(account_id) is not None else BackendStatus.MISSING

    def get(self, account_id: str) -> str | None:
        if not self._available():
            return None
        blob = _cred_read_bytes(self._target_name(account_id))
        if blob is None:
            return None
        return blob.decode("utf-16-le", errors="replace")

    def set(self, account_id: str, value: str) -> None:
        raise SecretUnavailableError(
            "WinCredBackend.set() is not implemented yet (TODO #309) — "
            "Windows Credential Manager writes are deferred, see module docstring"
        )

    def delete(self, account_id: str) -> None:
        raise SecretUnavailableError(
            "WinCredBackend.delete() is not implemented yet (TODO #309) — "
            "Windows Credential Manager writes are deferred, see module docstring"
        )
