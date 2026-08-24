"""_on_editor_ask_agent's server-side 4000-char bound (#365 phase 3 /
2026-08-23 phase 3-5 security review, "Ask Agent bound" item 5).

`selected_text` is already client-bounded at 4000 chars
(`static/editor/index.html`), but `main_window._on_editor_ask_agent` must
never trust that — this pins the defensive server-side slice so a future
edit can't silently drop it. No Qt application needed: same `Mock(self)` +
unbound-method-call pattern `test_main_window_status_bar.py`'s
`TestOnTabSwitchedNoTabsLeft` uses, since the method only touches
`self.orch` and a module-level `project_roots` lookup.
"""

from __future__ import annotations

from unittest.mock import Mock

from agent_takkub import main_window as mw


def _ask_agent(monkeypatch, *, project_roots: dict | None = None, **kwargs):
    monkeypatch.setattr(mw, "project_roots", lambda _name: project_roots or {})
    fake_self = Mock()
    fake_self.orch.send.return_value = (True, None)
    defaults = dict(
        project_name="demo",
        path="/proj/src/x.py",
        start_line=1,
        end_line=2,
        selected_text="hello",
        request="explain this",
    )
    defaults.update(kwargs)
    mw.MainWindow._on_editor_ask_agent(fake_self, **defaults)
    return fake_self


class TestAskAgentServerSideBound:
    def test_oversized_selection_is_truncated_to_4000_chars_before_send(self, monkeypatch) -> None:
        fake_self = _ask_agent(monkeypatch, selected_text="x" * 9000)

        (_lead, msg), _kwargs = fake_self.orch.send.call_args
        # the fenced code block carries only the bounded text, never the raw 9000 chars
        fenced = msg.split("```\n", 1)[1].rsplit("\n```", 1)[0]
        assert len(fenced) == 4000
        assert fenced == "x" * 4000

    def test_selection_under_the_bound_passes_through_unchanged(self, monkeypatch) -> None:
        fake_self = _ask_agent(monkeypatch, selected_text="short selection")

        (_lead, msg), _kwargs = fake_self.orch.send.call_args
        assert "short selection" in msg

    def test_empty_selection_omits_the_fenced_block(self, monkeypatch) -> None:
        fake_self = _ask_agent(monkeypatch, selected_text="")

        (_lead, msg), _kwargs = fake_self.orch.send.call_args
        assert "```" not in msg

    def test_send_is_routed_to_lead_from_editor_role(self, monkeypatch) -> None:
        fake_self = _ask_agent(monkeypatch, project_name="demo")

        args, kwargs = fake_self.orch.send.call_args
        assert args[0] == mw.LEAD.name
        assert kwargs == {"from_role": "editor", "project": "demo"}

    def test_send_failure_logs_but_does_not_raise(self, monkeypatch) -> None:
        monkeypatch.setattr(mw, "project_roots", lambda _name: {})
        fake_self = Mock()
        fake_self.orch.send.return_value = (False, "pane not found")
        logged: list[dict] = []
        monkeypatch.setattr(mw, "_log_event", lambda event, **details: logged.append(details))

        mw.MainWindow._on_editor_ask_agent(fake_self, "demo", "/proj/x.py", 1, 2, "sel", "explain")

        assert logged and logged[0]["project"] == "demo"


class _FakeInputDialog:
    """Stand-in for `QInputDialog` — no `QApplication`/event loop needed,
    same headless `Mock(self)` shape as the rest of this file. Configure
    `item_result`/`text_result` per test; `calls` records which static
    method ran, so a cancel-at-the-picker test can assert the text dialog
    never opened."""

    item_result: tuple[str, bool] = ("", False)
    text_result: tuple[str, bool] = ("", False)
    calls: list[str]

    @classmethod
    def getItem(cls, *args, **kwargs):
        cls.calls.append("getItem")
        return cls.item_result

    @classmethod
    def getMultiLineText(cls, *args, **kwargs):
        cls.calls.append("getMultiLineText")
        return cls.text_result


def _explorer_ask_agent(
    monkeypatch,
    *,
    project_roots: dict | None = None,
    live_roles=(),
    item_result: tuple[str, bool] = ("", False),
    text_result: tuple[str, bool] = ("", False),
    path: str = "/proj/src/x.py",
    project_name: str = "demo",
):
    monkeypatch.setattr(mw, "project_roots", lambda _name: project_roots or {})
    fake_dialog = type("FakeInputDialog", (_FakeInputDialog,), {"calls": []})
    fake_dialog.item_result = item_result
    fake_dialog.text_result = text_result
    monkeypatch.setattr(mw, "QInputDialog", fake_dialog)

    fake_self = Mock()
    fake_self.orch._resolve_project.return_value = project_name
    fake_self.orch._live_roles.return_value = frozenset(live_roles)
    fake_self.orch.send.return_value = (True, None)

    mw.MainWindow._on_explorer_ask_agent(fake_self, project_name, path)
    return fake_self, fake_dialog


class TestExplorerAskAgent:
    def test_no_live_roles_shows_status_and_never_opens_a_dialog(self, monkeypatch) -> None:
        fake_self, fake_dialog = _explorer_ask_agent(monkeypatch, live_roles=())

        assert fake_dialog.calls == []
        fake_self.orch.send.assert_not_called()
        assert fake_self._status.showMessage.called

    def test_role_picker_cancelled_never_opens_the_question_dialog(self, monkeypatch) -> None:
        fake_self, fake_dialog = _explorer_ask_agent(
            monkeypatch, live_roles=["backend"], item_result=("backend", False)
        )

        assert fake_dialog.calls == ["getItem"]
        fake_self.orch.send.assert_not_called()

    def test_empty_question_aborts_before_send(self, monkeypatch) -> None:
        fake_self, fake_dialog = _explorer_ask_agent(
            monkeypatch,
            live_roles=["backend"],
            item_result=("backend", True),
            text_result=("   ", True),
        )

        assert fake_dialog.calls == ["getItem", "getMultiLineText"]
        fake_self.orch.send.assert_not_called()

    def test_successful_flow_sends_to_the_picked_role_from_explorer(self, monkeypatch) -> None:
        fake_self, _fake_dialog = _explorer_ask_agent(
            monkeypatch,
            live_roles=["backend", "qa"],
            item_result=("backend", True),
            text_result=("what does this do?", True),
            path="/proj/src/x.py",
        )

        args, kwargs = fake_self.orch.send.call_args
        assert args[0] == "backend"
        assert "src/x.py" in args[1] or "src\\x.py" in args[1]
        assert "what does this do?" in args[1]
        assert kwargs == {"from_role": "explorer", "project": "demo"}

    def test_directory_path_labels_the_message_as_a_directory(self, monkeypatch, tmp_path) -> None:
        fake_self, _fake_dialog = _explorer_ask_agent(
            monkeypatch,
            live_roles=["backend"],
            item_result=("backend", True),
            text_result=("what is this for?", True),
            path=str(tmp_path),
        )

        _args, msg = fake_self.orch.send.call_args[0]
        assert "(directory)" in msg

    def test_file_path_labels_the_message_as_a_file(self, monkeypatch, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x")
        fake_self, _fake_dialog = _explorer_ask_agent(
            monkeypatch,
            live_roles=["backend"],
            item_result=("backend", True),
            text_result=("what is this for?", True),
            path=str(f),
        )

        _args, msg = fake_self.orch.send.call_args[0]
        assert "(file)" in msg

    def test_send_failure_shows_status_and_logs(self, monkeypatch) -> None:
        monkeypatch.setattr(mw, "project_roots", lambda _name: {})
        fake_dialog = type(
            "FakeInputDialog",
            (_FakeInputDialog,),
            {
                "calls": [],
                "item_result": ("backend", True),
                "text_result": ("explain", True),
            },
        )
        monkeypatch.setattr(mw, "QInputDialog", fake_dialog)
        logged: list[dict] = []
        monkeypatch.setattr(mw, "_log_event", lambda event, **details: logged.append(details))

        fake_self = Mock()
        fake_self.orch._resolve_project.return_value = "demo"
        fake_self.orch._live_roles.return_value = frozenset(["backend"])
        fake_self.orch.send.return_value = (False, "pane not found")

        mw.MainWindow._on_explorer_ask_agent(fake_self, "demo", "/proj/x.py")

        assert fake_self._status.showMessage.called
        assert logged and logged[0]["project"] == "demo" and logged[0]["role"] == "backend"
