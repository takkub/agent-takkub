"""SecretManager (Phase 3, epic #309) — resolves a `secret://provider/
<account-id>` ref to the backend that actually holds that provider's
credential today, per docs/v2/V2_IMPLEMENTATION_PLAN.md §2 Phase 3.
Structurally satisfies `core.contracts.secret_manager.SecretManager`
(get_secret/set_secret/delete_secret); `rotate_secret` is additive, not
part of that Protocol, so adding it here can never break the existing
`isinstance()` structural check in tests/test_core_contracts.py.

Nothing is migrated: every ref still points at the file/Keychain/Credential
Manager location the provider CLI itself already writes to ("ยังไม่ย้าย
credential" — the blueprint requirement this phase follows).

Providers with no confirmed credential-file location (gemini/opencode/kimi/
cursor — see `doctor.check_provider_auth`'s "unknown" INFO finding and
`config.PROVIDER_ISOLATION_GAPS`) have no backend registered here; asking
for one of those raises `SecretUnavailableError` with a clear reason rather
than guessing a path.

The three optional design-tool integrations (#373 — `core.capabilities.
design_integrations.OPTIONAL_DESIGN_MCPS`: `reference-21st`/`figma`/
`penpot`) are registered here too, each behind its own `FileSecretBackend`
under `SETTINGS_HOME/secrets/` — a cockpit-owned file, never `projects.json`
or any other world-readable project file. `design_integrations.build_client`
is the only caller; the whole file's text is the secret (a bare token for
figma, a small JSON blob of `{token, base_url}`/`{api_key, base_url}` for
penpot/reference-21st — same "backend doesn't know the schema" contract
`FileSecretBackend`'s own docstring already states for provider credential
files).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

from agent_takkub import codex_helper, config

from .backends import BackendStatus, SecretBackend, SecretUnavailableError
from .backends.file_backend import FileSecretBackend
from .backends.keychain_backend import KeychainBackend

_SCHEME = "secret://"
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


class SecretRefError(ValueError):
    """secret_ref does not match `secret://provider/<account-id>`."""


def parse_secret_ref(secret_ref: str) -> tuple[str, str]:
    if not secret_ref.startswith(_SCHEME):
        raise SecretRefError(f"not a secret ref (missing {_SCHEME!r} scheme): {secret_ref!r}")
    rest = secret_ref[len(_SCHEME) :]
    provider, _, account_id = rest.partition("/")
    if not provider or not account_id:
        raise SecretRefError(
            f"malformed secret ref, expected 'secret://provider/account-id': {secret_ref!r}"
        )
    return provider, account_id


def default_backends() -> dict[str, SecretBackend]:
    """One backend per provider, chosen by where that provider's credential
    lives on THIS platform today — mirrors `doctor.check_claude` and
    `doctor._codex_auth_finding`, the existing source of truth for these
    paths."""
    claude_file = FileSecretBackend(config.default_claude_config_dir() / ".credentials.json")
    secrets_dir = config.SETTINGS_HOME / "secrets"
    backends: dict[str, SecretBackend] = {
        "claude": (
            KeychainBackend(_CLAUDE_KEYCHAIN_SERVICE) if sys.platform == "darwin" else claude_file
        ),
        "codex": FileSecretBackend(codex_helper.codex_home() / "auth.json"),
        "reference-21st": FileSecretBackend(secrets_dir / "reference-21st.json"),
        "figma": FileSecretBackend(secrets_dir / "figma.json"),
        "penpot": FileSecretBackend(secrets_dir / "penpot.json"),
        "openviking": FileSecretBackend(secrets_dir / "openviking.json"),
    }
    return backends


class SecretManager:
    """Concrete `core.contracts.secret_manager.SecretManager`."""

    def __init__(self, backends: Mapping[str, SecretBackend] | None = None) -> None:
        self._backends: dict[str, SecretBackend] = (
            dict(backends) if backends is not None else default_backends()
        )

    def _backend_for(self, provider: str) -> SecretBackend:
        backend = self._backends.get(provider)
        if backend is None:
            raise SecretUnavailableError(f"no secret backend registered for provider {provider!r}")
        return backend

    def status(self, secret_ref: str) -> BackendStatus:
        provider, account_id = parse_secret_ref(secret_ref)
        backend = self._backends.get(provider)
        if backend is None:
            return BackendStatus.UNAVAILABLE
        return backend.status(account_id)

    def get_secret(self, secret_ref: str) -> str:
        provider, account_id = parse_secret_ref(secret_ref)
        value = self._backend_for(provider).get(account_id)
        if value is None:
            raise SecretUnavailableError(f"secret not found: {secret_ref}")
        return value

    def set_secret(self, secret_ref: str, value: str) -> None:
        provider, account_id = parse_secret_ref(secret_ref)
        self._backend_for(provider).set(account_id, value)

    def delete_secret(self, secret_ref: str) -> None:
        provider, account_id = parse_secret_ref(secret_ref)
        self._backend_for(provider).delete(account_id)

    def rotate_secret(self, secret_ref: str, new_value: str) -> None:
        """Replace the credential at *secret_ref* with *new_value*. Same
        mechanics as `set_secret` — named separately because "rotate"
        implies a value that already existed (plan §2 Phase 3 lists
        get/set/delete/rotate explicitly)."""
        self.set_secret(secret_ref, new_value)
