"""Tests for permission_gates.py (#243).

Verifies:
  1. resolve_claude_ask_rules merges settings.json + settings.local.json
     rules found walking up from cwd to the git root, de-duplicated.
  2. render_claude_gate_appendix is empty when no ask rules apply (token
     discipline — no gate section on a normal spawn).
  3. render_claude_gate_appendix lists a known pattern's alternative and
     the FAILED-report instruction when rules exist.
  4. An unrecognized ask pattern still gets listed, with the generic
     "report FAILED" fallback rather than being silently dropped.
  5. render_generic_gate_note (non-claude providers) states the gap
     explicitly instead of returning empty/silent.
"""

from __future__ import annotations

import json
import pathlib

from agent_takkub.permission_gates import (
    render_claude_gate_appendix,
    render_generic_gate_note,
    resolve_claude_ask_rules,
)


def _write_settings(
    claude_dir: pathlib.Path, ask: list[str], filename: str = "settings.json"
) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / filename).write_text(json.dumps({"permissions": {"ask": ask}}), encoding="utf-8")


class TestResolveClaudeAskRules:
    def test_no_settings_returns_empty(self, tmp_path: pathlib.Path) -> None:
        assert resolve_claude_ask_rules(str(tmp_path)) == []

    def test_reads_rules_at_cwd(self, tmp_path: pathlib.Path) -> None:
        _write_settings(tmp_path / ".claude", ["Bash(git reset --hard:*)"])
        assert resolve_claude_ask_rules(str(tmp_path)) == ["Bash(git reset --hard:*)"]

    def test_merges_settings_and_settings_local(self, tmp_path: pathlib.Path) -> None:
        _write_settings(tmp_path / ".claude", ["Bash(git reset --hard:*)"], "settings.json")
        _write_settings(tmp_path / ".claude", ["Bash(npm install -g:*)"], "settings.local.json")
        rules = resolve_claude_ask_rules(str(tmp_path))
        assert set(rules) == {"Bash(git reset --hard:*)", "Bash(npm install -g:*)"}

    def test_walks_up_to_git_root(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / ".git").mkdir()
        _write_settings(tmp_path / ".claude", ["Bash(git push --force:*)"])
        sub = tmp_path / "api"
        sub.mkdir()
        assert resolve_claude_ask_rules(str(sub)) == ["Bash(git push --force:*)"]

    def test_stops_at_git_root_boundary(self, tmp_path: pathlib.Path) -> None:
        outer = tmp_path / "outer"
        repo = outer / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        _write_settings(outer / ".claude", ["Bash(git reset --hard:*)"])
        assert resolve_claude_ask_rules(str(repo)) == []

    def test_dedupes_across_levels(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / ".git").mkdir()
        _write_settings(tmp_path / ".claude", ["Bash(git reset --hard:*)"])
        sub = tmp_path / "api"
        _write_settings(sub / ".claude", ["Bash(git reset --hard:*)"])
        assert resolve_claude_ask_rules(str(sub)) == ["Bash(git reset --hard:*)"]


class TestRenderClaudeGateAppendix:
    def test_empty_when_no_rules(self, tmp_path: pathlib.Path) -> None:
        assert render_claude_gate_appendix(str(tmp_path)) == ""

    def test_known_pattern_includes_alternative(self, tmp_path: pathlib.Path) -> None:
        _write_settings(tmp_path / ".claude", ["Bash(git reset --hard:*)"])
        text = render_claude_gate_appendix(str(tmp_path))
        assert "git reset --hard" in text
        assert "git checkout -B" in text
        assert "takkub done --fail" in text

    def test_unknown_pattern_gets_generic_fallback(self, tmp_path: pathlib.Path) -> None:
        _write_settings(tmp_path / ".claude", ["Bash(docker system prune -a:*)"])
        text = render_claude_gate_appendix(str(tmp_path))
        assert "docker system prune -a" in text
        assert "ไม่มีทางเลือกที่ cockpit รู้จัก" in text


class TestRenderGenericGateNote:
    def test_states_gap_explicitly(self) -> None:
        text = render_generic_gate_note("Codex", ["--ask-for-approval", "never"])
        assert "Codex" in text
        assert "#103" in text
        assert "takkub done --fail" in text
