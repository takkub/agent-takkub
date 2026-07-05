"""cmd_release must refuse to run in an installed build (site-packages ships
no pyproject.toml, so `release()` would crash with FileNotFoundError instead
of a clear message — see docs/audit/2026-07-05-installed-build-audit-gemini.md,
finding 9)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest

from agent_takkub import cli


def _release_args(**overrides) -> argparse.Namespace:
    defaults = {
        "part": "patch",
        "version": None,
        "no_commit": False,
        "no_tag": False,
        "dry_run": True,
        "allow_empty": False,
        "github_release": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_installed_build_refuses_with_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_takkub.config.is_installed_package", lambda: True)
    release_fn = MagicMock()
    monkeypatch.setattr("agent_takkub.release.release", release_fn)

    result = cli.cmd_release(_release_args())

    assert result["ok"] is False
    assert "dev checkout" in result["msg"]
    release_fn.assert_not_called()


def test_dev_checkout_still_calls_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_takkub.config.is_installed_package", lambda: False)
    release_fn = MagicMock(
        return_value={
            "dry_run": True,
            "current": "1.0.0",
            "new_version": "1.0.1",
            "tag": "v1.0.1",
            "date": "2026-07-05",
        }
    )
    monkeypatch.setattr("agent_takkub.release.release", release_fn)

    result = cli.cmd_release(_release_args())

    assert result["ok"] is True
    release_fn.assert_called_once()
