"""Planted AGENTS.md/GEMINI.md must not outlive the panes that needed them
(2026-08-26 user report: an IDE-launched codex/gemini in a project read the
leftover takkub-managed AGENTS.md and behaved as a cockpit pane — 18 such
files were found across 14 projects). Covers the file helper, the
orchestrator release-on-close/exit/shutdown paths, and `takkub cleanup
agents-md`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_takkub import cli
from agent_takkub import codex_agents_md as cam
from agent_takkub.orchestrator import Orchestrator


def _plant(dir_: Path, name: str = "AGENTS.md", managed: bool = True) -> Path:
    target = dir_ / name
    body = f"{cam.TAKKUB_MARKER}\n\nbody\n" if managed else "# my own AGENTS.md\n"
    target.write_text(body, encoding="utf-8")
    return target


# ── file helper ───────────────────────────────────────────────────────────


def test_remove_only_touches_marker_tagged_files(tmp_path: Path) -> None:
    _plant(tmp_path, "AGENTS.md")
    _plant(tmp_path, "GEMINI.md", managed=True)
    user_owned = tmp_path / "CLAUDE.md"
    user_owned.write_text("takkub-managed mention in body, not on line 1\n", encoding="utf-8")

    removed = cam.remove_managed_context_files(tmp_path)

    assert sorted(removed) == ["AGENTS.md", "GEMINI.md"]
    assert not (tmp_path / "AGENTS.md").exists()
    assert user_owned.exists()


def test_remove_skips_user_owned_agents_md(tmp_path: Path) -> None:
    _plant(tmp_path, managed=False)
    assert cam.remove_managed_context_files(tmp_path) == []
    assert (tmp_path / "AGENTS.md").exists()


def test_remove_is_a_noop_on_missing_or_relative_dir(tmp_path: Path) -> None:
    assert cam.remove_managed_context_files(tmp_path / "nope") == []
    assert cam.remove_managed_context_files("relative/dir") == []


def test_find_reports_per_path_and_dedupes(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _plant(a)
    _plant(b, managed=False)
    found = cam.find_managed_context_files([a, a, b, tmp_path / "missing"])
    assert found == {str(a): ["AGENTS.md"]}


# ── orchestrator release paths ────────────────────────────────────────────


class _Pane:
    def __init__(self, cwd: str, *, alive: bool = True, state: str = "working") -> None:
        self._session_cwd = cwd
        self.session = object() if alive else None
        self.state = state


def _orch(panes: dict[str, _Pane]) -> SimpleNamespace:
    fake = SimpleNamespace(_panes_by_project={"proj": panes})
    fake._planted_context_cwd_in_use = lambda cwd, exclude: (
        Orchestrator._planted_context_cwd_in_use(fake, cwd, exclude)
    )
    return fake


def test_release_removes_when_no_other_live_pane_uses_cwd(tmp_path: Path) -> None:
    _plant(tmp_path)
    fake = _orch({"backend": _Pane(str(tmp_path)), "qa": _Pane(str(tmp_path), alive=False)})

    removed = Orchestrator._release_planted_context_if_unused(
        fake, str(tmp_path), exclude=("proj", "backend")
    )

    assert removed == ["AGENTS.md"]
    assert not (tmp_path / "AGENTS.md").exists()


def test_release_keeps_file_while_another_live_pane_shares_cwd(tmp_path: Path) -> None:
    _plant(tmp_path)
    fake = _orch({"backend": _Pane(str(tmp_path)), "codex": _Pane(str(tmp_path))})

    removed = Orchestrator._release_planted_context_if_unused(
        fake, str(tmp_path), exclude=("proj", "backend")
    )

    assert removed == []
    assert (tmp_path / "AGENTS.md").exists()


def test_release_ignores_none_cwd() -> None:
    fake = _orch({})
    assert Orchestrator._release_planted_context_if_unused(fake, None) == []


def test_release_all_sweeps_every_pane_cwd_including_lead(tmp_path: Path) -> None:
    lead_dir = tmp_path / "lead"
    api_dir = tmp_path / "api"
    lead_dir.mkdir()
    api_dir.mkdir()
    _plant(lead_dir)
    _plant(api_dir, "GEMINI.md")
    fake = SimpleNamespace(
        _panes_by_project={"proj": {"lead": _Pane(str(lead_dir)), "backend": _Pane(str(api_dir))}}
    )

    out = Orchestrator.release_all_planted_context(fake)

    assert out == {str(lead_dir): ["AGENTS.md"], str(api_dir): ["GEMINI.md"]}
    assert not (lead_dir / "AGENTS.md").exists()
    assert not (api_dir / "GEMINI.md").exists()


# ── takkub cleanup agents-md ──────────────────────────────────────────────


def _registry(*paths: Path) -> dict:
    return {
        "active": "p0",
        "projects": {f"p{i}": {"paths": {"main": str(p)}} for i, p in enumerate(paths)},
    }


def test_cleanup_agents_md_dry_run_lists_without_deleting(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
    _plant(tmp_path)
    monkeypatch.setattr("agent_takkub.config.load_projects", lambda: _registry(tmp_path))

    code = cli.main(["cleanup", "agents-md", "--dry-run"])

    assert code == 0
    assert "would remove" in capsys.readouterr().out
    assert (tmp_path / "AGENTS.md").exists()


def test_cleanup_agents_md_yes_removes_registered_and_extra_paths(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
    extra = tmp_path / "extra"
    extra.mkdir()
    _plant(tmp_path)
    _plant(extra)
    monkeypatch.setattr("agent_takkub.config.load_projects", lambda: _registry(tmp_path))

    code = cli.main(["cleanup", "agents-md", "--yes", "--path", str(extra)])

    assert code == 0
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (extra / "AGENTS.md").exists()


def test_cleanup_agents_md_declines_without_yes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
    _plant(tmp_path)
    monkeypatch.setattr("agent_takkub.config.load_projects", lambda: _registry(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    code = cli.main(["cleanup", "agents-md"])

    assert code == 1
    assert (tmp_path / "AGENTS.md").exists()


def test_cleanup_agents_md_nothing_found(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
    monkeypatch.setattr("agent_takkub.config.load_projects", lambda: _registry(tmp_path))

    code = cli.main(["cleanup", "agents-md", "--yes"])

    assert code == 0
    assert "nothing to clean up" in capsys.readouterr().out
