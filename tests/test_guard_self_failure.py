"""2026-10-24: a half-edited module broke the import chain the guards use.
pane_guard then told a solo-lead Lead "deep category, delegate" (impossible
under solo-lead) and the lead_context guard denied EVERY tool, so the Lead
could not even `git checkout` the file back — only the user could unlock it."""

from __future__ import annotations

import argparse
import io
import json

from agent_takkub import cli, pane_guard


def test_unreadable_team_policy_is_reported_as_such_not_as_deep_category(monkeypatch, tmp_path):
    import agent_takkub.team_preset as team_preset

    def boom(_project=None):
        raise ImportError("cannot import name 'x' from 'y'")

    monkeypatch.setattr(team_preset, "lead_may_implement", boom)
    target = tmp_path / "core" / "migration" / "step.py"
    verdict = pane_guard.evaluate_lead_direct_edit(
        "Edit",
        {"file_path": str(target), "old_string": "a", "new_string": "b"},
        cwd=str(tmp_path),
        project="demo",
    )
    assert not verdict.allowed
    assert verdict.rule == "lead_direct_edit:policy_unreadable"
    assert "ImportError" in verdict.reason


def test_context_guard_fails_open_when_its_own_check_errors(monkeypatch, capsys):
    import agent_takkub.lead_readiness as lead_readiness

    def boom():
        raise ImportError("cannot import name 'write_json_atomic'")

    monkeypatch.setattr(cli, "_from_role", lambda: "lead")
    monkeypatch.setattr(lead_readiness, "current_reason", boom)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {}}))
    )
    res = cli.cmd_context_guard(argparse.Namespace())
    assert res.get("exit_code", 0) == 0  # never locks the Lead out of repair tools
    assert "allowing" in capsys.readouterr().err
