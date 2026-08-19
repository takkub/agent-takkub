"""FileSecretBackend — reads/writes the JSON credential file a provider CLI
already owns, at its current location. The whole file's text is "the
secret" (callers already parse the JSON blob themselves, e.g.
`limit_status._load_raw_credentials` pulling `access_token` out of it) —
this backend does not pick a key out of it, so it never has to know a
provider's credential schema.

`account_id` is accepted but ignored: Phase 2 has not landed multi-account
credential files yet (today's blueprint is one implicit profile per
provider), so every ref for a given provider resolves to the same file.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import BackendStatus


class FileSecretBackend:
    name = "file"

    def __init__(self, path: Path) -> None:
        self._path = path

    def status(self, account_id: str) -> BackendStatus:
        return BackendStatus.FOUND if self._path.is_file() else BackendStatus.MISSING

    def get(self, account_id: str) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8")
        except OSError:
            return None

    def set(self, account_id: str, value: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, self._path)

    def delete(self, account_id: str) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
