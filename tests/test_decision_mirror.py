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
    _is_junk_note,
    _is_junk_project,
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
        monkeypatch.setattr("agent_takkub.vault_mirror._DEFAULT_VAULT", tmp_path / "nope")
        assert _resolve_vault_dir() is None

    def test_default_vault_used_when_projects_dir_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Author's default layout: `~/WebstormProjects/second-brain/01-Projects/`.
        fake_vault = tmp_path / "vault"
        (fake_vault / "01-Projects").mkdir(parents=True)
        monkeypatch.delenv(_VAULT_ENV, raising=False)
        monkeypatch.setattr("agent_takkub.vault_mirror._DEFAULT_VAULT", fake_vault)
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
        monkeypatch.setattr("agent_takkub.vault_mirror._DEFAULT_VAULT", decoy)
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
        monkeypatch.setattr("agent_takkub.vault_mirror._DEFAULT_VAULT", good)
        assert _resolve_vault_dir() == good

    def test_whitespace_only_override_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # `TAKKUB_VAULT_DIR=   ` is treated as "unset" — bash exports
        # of empty values are common and shouldn't poison the probe.
        good = tmp_path / "good"
        (good / "01-Projects").mkdir(parents=True)
        monkeypatch.setenv(_VAULT_ENV, "   ")
        monkeypatch.setattr("agent_takkub.vault_mirror._DEFAULT_VAULT", good)
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
        body = _render_decision_note("agent-takkub", "backend", "added /login endpoint to API", now)
        assert "backend done" in body
        # Project line is now a wikilink so Obsidian's graph clusters
        # every session of a project under the project page.
        assert "**Project:** [[01-Projects/agent-takkub|agent-takkub]]" in body
        assert "added /login endpoint to API" in body

    def test_body_has_frontmatter_tags_for_dataview(self) -> None:
        now = dt.datetime(2026, 5, 17, 14, 30, 45)
        body = _render_decision_note("agent-takkub", "backend", "added /login endpoint to API", now)
        # YAML frontmatter at the top — Dataview reads these fields.
        assert body.startswith("---\n")
        assert "role: backend" in body
        assert "project: agent-takkub" in body
        assert "date: 2026-05-17T14:30:45" in body
        assert "tags: [session, backend, agent-takkub]" in body

    def test_body_uses_iso_seconds_timestamp(self) -> None:
        now = dt.datetime(2026, 5, 17, 14, 30, 45)
        body = _render_decision_note("p", "r", "long enough note body", now)
        assert "2026-05-17T14:30:45" in body
        # No microseconds — the format is `isoformat(timespec='seconds')`.
        assert ".000000" not in body

    def test_note_is_stripped_of_outer_whitespace(self) -> None:
        # `takkub done "  text  "` shouldn't produce a body with leading
        # or trailing blank lines around the note block — those break
        # the section parser used by some downstream wiki tools.
        body = _render_decision_note("p", "r", "  long enough note body  ", dt.datetime.now())
        assert "## Note\n\nlong enough note body\n" in body

    def test_thai_unicode_survives(self) -> None:
        body = _render_decision_note("p", "r", "เสร็จแล้วครับ ทำ /login endpoint", dt.datetime.now())
        assert "เสร็จแล้วครับ" in body


class TestJunkFilters:
    """Notes / projects that look like throwaway stubs are dropped from
    the vault mirror so Obsidian's graph stays connected and meaningful.
    """

    def test_junk_note_matches_exact_list(self) -> None:
        for stub in ("ok", "wip", "done", "appended", "all green", "fixed", "."):
            assert _is_junk_note(stub) is True, stub

    def test_junk_note_is_case_and_whitespace_insensitive(self) -> None:
        assert _is_junk_note("  OK  ") is True
        assert _is_junk_note("Appended\n") is True

    def test_short_notes_are_junk(self) -> None:
        # < 15 chars → treated as throwaway, even if novel.
        assert _is_junk_note("hi there") is True
        assert _is_junk_note("oops") is True

    def test_substantive_note_passes(self) -> None:
        assert _is_junk_note("added /login endpoint with JWT") is False
        assert _is_junk_note("refactor: extract paste_payload helper") is False

    def test_junk_project_matches_known_prefixes(self) -> None:
        for proj in (
            "testproj",
            "test-vault",
            "tmp-experiment",
            "scratch-1",
            "playground",
        ):
            assert _is_junk_project(proj) is True, proj

    def test_real_project_passes(self) -> None:
        assert _is_junk_project("agent-takkub") is False
        assert _is_junk_project("pms") is False
        assert _is_junk_project("unirecon") is False

    def test_empty_project_is_junk(self) -> None:
        # An empty / None-ish project name shouldn't generate a session
        # file under `01-Projects/` (would land at `01-Projects//...`).
        assert _is_junk_project("") is True
        assert _is_junk_project("   ") is True
