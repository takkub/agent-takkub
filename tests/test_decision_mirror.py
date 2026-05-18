"""Tests for the decision-note mirror — `_resolve_vault_dir` and
`_render_decision_note`, the two helpers behind orchestrator's
mirror-to-Obsidian behaviour on `takkub done`.

The mirror is best-effort and silently degrades when no vault is
configured, so the unit tests have to pin three things explicitly:
  - the vault probe ignores stray empty dirs at the default path
  - the override env var beats the default
  - the rendered body matches the local log byte-for-byte (so the
    two copies stay in sync without a separate template).
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from agent_takkub.orchestrator import (
    _DEFAULT_VAULT,
    _VAULT_ENV,
    _render_decision_note,
    _resolve_vault_dir,
)


class TestResolveVaultDir:
    def test_returns_none_when_neither_path_has_projects_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # No override, and the default vault path either doesn't exist
        # or lacks the `01-Projects/` marker. Mirror must opt out
        # silently.
        monkeypatch.delenv(_VAULT_ENV, raising=False)
        monkeypatch.setattr("agent_takkub.orchestrator._DEFAULT_VAULT", tmp_path / "nope")
        assert _resolve_vault_dir() is None

    def test_default_vault_used_when_projects_dir_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Author's default layout: `~/WebstormProjects/second-brain/01-Projects/`.
        fake_vault = tmp_path / "vault"
        (fake_vault / "01-Projects").mkdir(parents=True)
        monkeypatch.delenv(_VAULT_ENV, raising=False)
        monkeypatch.setattr("agent_takkub.orchestrator._DEFAULT_VAULT", fake_vault)
        assert _resolve_vault_dir() == fake_vault

    def test_env_override_beats_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # If $TAKKUB_VAULT_DIR is set and valid, it wins even when the
        # default vault would also resolve. Otherwise migrating between
        # vaults requires editing source code.
        override = tmp_path / "override"
        (override / "01-Projects").mkdir(parents=True)
        decoy = tmp_path / "decoy"
        (decoy / "01-Projects").mkdir(parents=True)
        monkeypatch.setenv(_VAULT_ENV, str(override))
        monkeypatch.setattr("agent_takkub.orchestrator._DEFAULT_VAULT", decoy)
        assert _resolve_vault_dir() == override

    def test_env_override_falls_through_to_default_if_invalid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # An override pointing at a directory that exists but has no
        # `01-Projects/` should fall back to the default vault rather
        # than silently writing into the wrong place.
        bad_override = tmp_path / "bad"
        bad_override.mkdir()
        good = tmp_path / "good"
        (good / "01-Projects").mkdir(parents=True)
        monkeypatch.setenv(_VAULT_ENV, str(bad_override))
        monkeypatch.setattr("agent_takkub.orchestrator._DEFAULT_VAULT", good)
        assert _resolve_vault_dir() == good

    def test_whitespace_only_override_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # `TAKKUB_VAULT_DIR=   ` is treated as "unset" — bash exports
        # of empty values are common and shouldn't poison the probe.
        good = tmp_path / "good"
        (good / "01-Projects").mkdir(parents=True)
        monkeypatch.setenv(_VAULT_ENV, "   ")
        monkeypatch.setattr("agent_takkub.orchestrator._DEFAULT_VAULT", good)
        assert _resolve_vault_dir() == good

    def test_default_constant_points_at_expected_layout(self) -> None:
        # Guard against an accidental rename of the default location.
        # The author's vault is the documented default; renaming this
        # without updating CLAUDE.md / project page is a footgun.
        assert _DEFAULT_VAULT.name == "second-brain"
        assert _DEFAULT_VAULT.parent.name == "WebstormProjects"


class TestRenderDecisionNote:
    def test_body_includes_role_project_and_note(self) -> None:
        now = dt.datetime(2026, 5, 17, 14, 30, 45)
        body = _render_decision_note("agent-takkub", "backend", "all green", now)
        assert "backend done" in body
        assert "**Project:** agent-takkub" in body
        assert "all green" in body

    def test_body_uses_iso_seconds_timestamp(self) -> None:
        now = dt.datetime(2026, 5, 17, 14, 30, 45)
        body = _render_decision_note("p", "r", "n", now)
        assert "2026-05-17T14:30:45" in body
        # No microseconds — the format is `isoformat(timespec='seconds')`.
        assert ".000000" not in body

    def test_note_is_stripped_of_outer_whitespace(self) -> None:
        # `takkub done "  text  "` shouldn't produce a body with leading
        # or trailing blank lines around the note block — those break
        # the section parser used by some downstream wiki tools.
        body = _render_decision_note("p", "r", "  text  ", dt.datetime.now())
        assert "## Note\n\ntext\n" in body

    def test_thai_unicode_survives(self) -> None:
        body = _render_decision_note("p", "r", "เสร็จแล้วครับ", dt.datetime.now())
        assert "เสร็จแล้วครับ" in body
