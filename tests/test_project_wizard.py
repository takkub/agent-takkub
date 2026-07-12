"""_run_map_paths_dialog: flat-repo auto-skip (no subdir to map → no dialog).

Also covers `MainWindow._on_new_tab_clicked`'s merged D1+D2 dialog (3
buttons: open-existing / new-with-rules / import) that replaced the old
two-hop "new vs existing" then "new-with-rules vs import" chain.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QInputDialog, QMessageBox, QWidget

from agent_takkub.main_window import MainWindow
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


class _NewTabHost(QWidget):
    """Minimal host exposing only what `_on_new_tab_clicked` touches on
    `self` — avoids booting the full MainWindow (orchestrator, CLI server,
    Lead pane)."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def _open_projects(self):
        return []

    def _open_project_tab(self, name):
        self.calls.append(("open_tab", name))

    def _new_project_with_rules(self):
        self.calls.append(("new_with_rules",))

    def _import_existing_project(self):
        self.calls.append(("import_existing",))


def _stub_dialog(monkeypatch, pick_text_substr):
    """Make QMessageBox.exec() a no-op and clickedButton() return the
    button whose text contains `pick_text_substr` (None → nothing clicked)."""
    picked: dict = {}

    def _fake_exec(self):
        picked["box"] = self
        return 0

    def _fake_clicked_button(self):
        box = picked.get("box")
        if box is None or pick_text_substr is None:
            return None
        for b in box.buttons():
            if pick_text_substr in b.text():
                return b
        return None

    monkeypatch.setattr(QMessageBox, "exec", _fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", _fake_clicked_button)
    return picked


def test_new_tab_dialog_has_exactly_three_action_buttons(monkeypatch):
    monkeypatch.setattr("agent_takkub.main_window.list_project_names", lambda: [])
    host = _NewTabHost()
    picked = _stub_dialog(monkeypatch, None)

    MainWindow._on_new_tab_clicked(host)

    labels = [b.text() for b in picked["box"].buttons()]
    assert len(labels) == 4  # existing / new-with-rules / import / cancel
    assert any("เปิดโปรเจคที่ตั้งไว้" in t for t in labels)
    assert any("AI rules" in t for t in labels)
    assert any("Import" in t for t in labels)
    assert host.calls == []  # cancelled → no downstream action fired


def test_new_tab_dialog_routes_new_to_rules_wizard(monkeypatch):
    monkeypatch.setattr("agent_takkub.main_window.list_project_names", lambda: [])
    host = _NewTabHost()
    _stub_dialog(monkeypatch, "AI rules")

    MainWindow._on_new_tab_clicked(host)

    assert host.calls == [("new_with_rules",)]


def test_new_tab_dialog_routes_import_to_import_flow(monkeypatch):
    monkeypatch.setattr("agent_takkub.main_window.list_project_names", lambda: [])
    host = _NewTabHost()
    _stub_dialog(monkeypatch, "Import")

    MainWindow._on_new_tab_clicked(host)

    assert host.calls == [("import_existing",)]


def test_new_tab_dialog_existing_button_disabled_when_nothing_to_open(monkeypatch):
    monkeypatch.setattr("agent_takkub.main_window.list_project_names", lambda: [])
    host = _NewTabHost()
    picked = _stub_dialog(monkeypatch, None)

    MainWindow._on_new_tab_clicked(host)

    btn_existing = next(b for b in picked["box"].buttons() if "เปิดโปรเจคที่ตั้งไว้" in b.text())
    assert not btn_existing.isEnabled()


def test_new_tab_dialog_routes_existing_to_project_picker(monkeypatch):
    monkeypatch.setattr("agent_takkub.main_window.list_project_names", lambda: ["alpha", "beta"])
    host = _NewTabHost()
    _stub_dialog(monkeypatch, "เปิดโปรเจคที่ตั้งไว้")
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("beta", True))

    MainWindow._on_new_tab_clicked(host)

    assert host.calls == [("open_tab", "beta")]


def test_generate_rules_cancels_and_joins_before_deleting_thread(monkeypatch):
    """Dismissing the busy dialog (Esc / window close) returns from
    busy.exec() WITHOUT going through on_cancel. The generator thread may
    still be running; it must be cancelled and joined BEFORE deleteLater(),
    because deleting a running QThread aborts the whole process. Regression
    guard for the 2026-07 full-system review (project_wizard HIGH)."""
    from PyQt6.QtWidgets import QWidget

    import agent_takkub.project_wizard as pw

    calls: list = []

    class _FakeSignal:
        def connect(self, *a, **k):
            pass

    class _FakeThread:
        def __init__(self, *a, **k):
            self.rulesReady = _FakeSignal()
            self.failed = _FakeSignal()
            self._running = False

        def start(self):
            self._running = True
            calls.append("start")

        def isRunning(self):
            return self._running

        def cancel(self):
            calls.append("cancel")
            self._running = False  # proc.kill() -> run() exits

        def wait(self, timeout=None):
            calls.append("wait")
            return True  # joined

        def terminate(self):
            calls.append("terminate")

        def deleteLater(self):
            calls.append("deleteLater")

    monkeypatch.setattr(pw, "_RulesGeneratorThread", _FakeThread)
    # busy.exec() returns immediately as if the user pressed Esc (no signal fired)
    monkeypatch.setattr("PyQt6.QtWidgets.QDialog.exec", lambda self: 0)

    class _Host(QWidget, pw.ProjectWizardMixin):
        pass

    host = _Host()
    try:
        result = host._generate_rules_with_ui("prompt", "proj")
    finally:
        host.deleteLater()

    assert result is None
    assert "cancel" in calls, "a still-running thread must be cancelled on dismiss"
    assert "deleteLater" in calls
    # cancel + wait must both precede deleteLater; never delete a live thread
    assert calls.index("cancel") < calls.index("deleteLater")
    assert calls.index("wait") < calls.index("deleteLater")
    assert "terminate" not in calls  # cancel()+wait() succeeded, no force-kill needed
