"""_run_map_paths_dialog: flat-repo auto-skip (no subdir to map → no dialog)."""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from agent_takkub.project_wizard import ProjectWizardMixin


class _Host(QWidget, ProjectWizardMixin):
    """Minimal QWidget host so the mixin's QDialog(self) parenting works."""


def test_flat_repo_skips_dialog_and_maps_main(tmp_path, monkeypatch):
    # tmp_path itself is shared with the autouse _isolate_runtime fixture,
    # which creates its own subdirs (_isolated_runtime, _isolated_takkub) in
    # it — use a fresh empty subdir so this "flat repo" is actually flat.
    repo = tmp_path / "flat_repo"
    repo.mkdir()
    host = _Host()

    def _fail_exec(self):  # pragma: no cover - fails the test if reached
        raise AssertionError("dialog.exec() must not be called for a flat repo")

    monkeypatch.setattr("PyQt6.QtWidgets.QDialog.exec", _fail_exec)
    monkeypatch.setattr(
        "agent_takkub.config.load_projects",
        lambda: {"active": None, "projects": {}},
    )

    result = host._run_map_paths_dialog(repo)

    assert result == {"main": str(repo.resolve().as_posix())}


def test_repo_with_subdir_still_opens_dialog(tmp_path, monkeypatch):
    (tmp_path / "web").mkdir()
    host = _Host()

    calls: list[bool] = []

    def _fake_exec(self):
        calls.append(True)
        from PyQt6.QtWidgets import QDialog

        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("PyQt6.QtWidgets.QDialog.exec", _fake_exec)
    monkeypatch.setattr(
        "agent_takkub.config.load_projects",
        lambda: {"active": None, "projects": {}},
    )

    result = host._run_map_paths_dialog(tmp_path)

    assert calls, "dialog.exec() should be called when a mappable subdir exists"
    assert result is None
