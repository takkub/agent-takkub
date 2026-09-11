"""Unit tests for `_decision_note_project_label` (#546).

A real incident: a backend/devops task assigned with an explicit `--cwd`
pointing at an unrelated repo got its `takkub done` session file + vault
frontmatter filed under the Lead's then-active project namespace instead of
the repo actually touched — searching later by that repo's own name found
nothing. `_decision_note_project_label` is the pure helper `Orchestrator.
done()` consults right before `_save_decision_note()`; it leaves the pane's
runtime `project_ns` (pane registry / resource governor / wait / digest)
completely untouched — only the on-disk filing label for the decision note.
"""

from __future__ import annotations

import pathlib

from agent_takkub import config
from agent_takkub.orchestrator_text import _decision_note_project_label


def _write_projects_json(
    tmp_path: pathlib.Path, monkeypatch, seed_projects, projects: dict
) -> None:
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    seed_projects(tmp_path, projects, active="proj_a")


class TestDecisionNoteProjectLabel:
    def test_cwd_inside_project_keeps_project_unchanged(self, tmp_path, monkeypatch, seed_projects):
        _write_projects_json(
            tmp_path,
            monkeypatch,
            seed_projects,
            {"proj_a": {"paths": {"backend": str(tmp_path / "proj_a" / "backend")}}},
        )
        cwd = str(tmp_path / "proj_a" / "backend")

        label = _decision_note_project_label("proj_a", cwd, "backend")

        assert label == "proj_a"

    def test_cross_repo_cwd_derives_label_from_repo_folder_name(
        self, tmp_path, monkeypatch, seed_projects
    ):
        """The reported scenario: `--cwd` resolves OUTSIDE every root
        `proj_a` has registered — file the note under the repo's own
        folder name instead of the umbrella project."""
        _write_projects_json(
            tmp_path,
            monkeypatch,
            seed_projects,
            {"proj_a": {"paths": {"backend": str(tmp_path / "proj_a" / "backend")}}},
        )
        other_repo = tmp_path / "weid_gateway_api"
        other_repo.mkdir()

        label = _decision_note_project_label("proj_a", str(other_repo), "devops")

        assert label == "weid_gateway_api"

    def test_no_cwd_keeps_project_unchanged(self, tmp_path, monkeypatch, seed_projects):
        _write_projects_json(tmp_path, monkeypatch, seed_projects, {"proj_a": {"paths": {}}})

        assert _decision_note_project_label("proj_a", None, "backend") == "proj_a"

    def test_lead_cockpit_repo_root_bypass_keeps_project_unchanged(
        self, tmp_path, monkeypatch, seed_projects
    ):
        """`_cwd_within_project`'s Lead-only cockpit-repo-root bypass must
        still short-circuit here — Lead editing the cockpit itself is not
        a cross-repo task needing re-attribution."""
        _write_projects_json(tmp_path, monkeypatch, seed_projects, {"proj_a": {"paths": {}}})
        cockpit = tmp_path / "cockpit"
        cockpit.mkdir()

        label = _decision_note_project_label("proj_a", str(cockpit), "lead")

        assert label == "proj_a"

    def test_unresolvable_cwd_falls_back_to_project(self, tmp_path, monkeypatch, seed_projects):
        _write_projects_json(tmp_path, monkeypatch, seed_projects, {"proj_a": {"paths": {}}})

        # A path with an illegal component on Windows/POSIX alike still
        # must not raise out of this helper — best-effort fallback.
        label = _decision_note_project_label("proj_a", "\x00bad", "backend")

        assert label == "proj_a"
