"""SecretManager: ref parsing, resolving to the right backend, and a clear
(never-crashing) error when no backend is registered for a provider."""

from __future__ import annotations

import pytest

from agent_takkub.core.secrets.backends import BackendStatus, SecretUnavailableError
from agent_takkub.core.secrets.manager import SecretManager, SecretRefError, parse_secret_ref


class _FakeBackend:
    name = "fake"

    def __init__(self, value: str | None = None) -> None:
        self._value = value

    def status(self, account_id: str) -> BackendStatus:
        return BackendStatus.FOUND if self._value is not None else BackendStatus.MISSING

    def get(self, account_id: str) -> str | None:
        return self._value

    def set(self, account_id: str, value: str) -> None:
        self._value = value

    def delete(self, account_id: str) -> None:
        self._value = None


def test_parse_secret_ref_splits_provider_and_account():
    assert parse_secret_ref("secret://claude/default") == ("claude", "default")


@pytest.mark.parametrize(
    "bad_ref",
    ["not-a-secret-ref", "secret://", "secret://claude", "secret:///default"],
)
def test_parse_secret_ref_rejects_malformed_refs(bad_ref):
    with pytest.raises(SecretRefError):
        parse_secret_ref(bad_ref)


def test_get_secret_resolves_to_correct_backend():
    claude_backend = _FakeBackend("claude-token")
    codex_backend = _FakeBackend("codex-token")
    manager = SecretManager({"claude": claude_backend, "codex": codex_backend})

    assert manager.get_secret("secret://claude/default") == "claude-token"
    assert manager.get_secret("secret://codex/default") == "codex-token"


def test_set_secret_writes_through_to_backend():
    backend = _FakeBackend()
    manager = SecretManager({"claude": backend})
    manager.set_secret("secret://claude/default", "new-value")
    assert backend.get("default") == "new-value"


def test_delete_secret_clears_backend():
    backend = _FakeBackend("value")
    manager = SecretManager({"claude": backend})
    manager.delete_secret("secret://claude/default")
    assert backend.get("default") is None


def test_rotate_secret_replaces_value():
    backend = _FakeBackend("old-value")
    manager = SecretManager({"claude": backend})
    manager.rotate_secret("secret://claude/default", "rotated-value")
    assert backend.get("default") == "rotated-value"


def test_status_reports_backend_status():
    manager = SecretManager({"claude": _FakeBackend("value")})
    assert manager.status("secret://claude/default") == BackendStatus.FOUND


def test_status_unavailable_for_unregistered_provider():
    manager = SecretManager({})
    assert manager.status("secret://gemini/default") == BackendStatus.UNAVAILABLE


def test_get_secret_raises_clear_error_for_unregistered_provider():
    manager = SecretManager({})
    with pytest.raises(SecretUnavailableError):
        manager.get_secret("secret://gemini/default")


def test_get_secret_raises_clear_error_when_backend_has_no_value():
    manager = SecretManager({"claude": _FakeBackend(None)})
    with pytest.raises(SecretUnavailableError):
        manager.get_secret("secret://claude/default")


def test_set_secret_raises_for_unregistered_provider():
    manager = SecretManager({})
    with pytest.raises(SecretUnavailableError):
        manager.set_secret("secret://gemini/default", "value")


def test_manager_never_crashes_on_missing_provider_it_raises_typed_error():
    manager = SecretManager({})
    for op in (
        lambda: manager.get_secret("secret://opencode/default"),
        lambda: manager.set_secret("secret://opencode/default", "x"),
        lambda: manager.delete_secret("secret://opencode/default"),
    ):
        with pytest.raises(SecretUnavailableError):
            op()
