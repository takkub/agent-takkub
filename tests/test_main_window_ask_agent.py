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
