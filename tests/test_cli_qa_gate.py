"""`takkub qa-gate` (#325) CLI wiring — pure-local like `doctor`/`migrate`,
callable by every role (not gated by LEAD_ONLY_COMMANDS). Heavy lifting is
tested directly in test_qa_gate.py; here we only prove argparse wiring +
dispatch + exit-code propagation, so `qa_gate.run_gate` itself is stubbed."""

from __future__ import annotations

from agent_takkub import cli, qa_gate


def _fake_report(ok: bool, exit_code: int = 0) -> qa_gate.GateReport:
    r = qa_gate.GateReport()
    r.steps.append(qa_gate.StepResult("venv-check", True, False, 0.01, "using .venv"))
    r.steps.append(
        qa_gate.StepResult(
            "pytest", ok, False, 0.02, "1 passed" if ok else "1 failed", 0 if ok else exit_code
        )
    )
    r.steps.append(qa_gate.StepResult("ruff", ok, not ok, 0.01, "clean" if ok else "skipped"))
    r.steps.append(
        qa_gate.StepResult("lint-imports", ok, not ok, 0.01, "clean" if ok else "skipped")
    )
    return r


def test_qa_gate_success_prints_table_and_returns_zero(monkeypatch, capsys):
    captured: dict = {}

    def fake_run_gate(**kwargs):
        captured.update(kwargs)
        return _fake_report(True)

    monkeypatch.setattr(qa_gate, "run_gate", fake_run_gate)
    rc = cli.main(["qa-gate"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "GATE: PASS" in out
    assert captured["targeted"] is None
    assert captured["v2_flags"] is False


def test_qa_gate_failure_returns_the_failing_steps_exact_returncode(monkeypatch, capsys):
    def fake_run_gate(**kwargs):
        return _fake_report(False, exit_code=3)

    monkeypatch.setattr(qa_gate, "run_gate", fake_run_gate)
    rc = cli.main(["qa-gate"])

    assert rc == 3
    out = capsys.readouterr().out
    assert "GATE: FAIL" in out


def test_qa_gate_targeted_passes_paths_through(monkeypatch):
    captured: dict = {}

    def fake_run_gate(**kwargs):
        captured.update(kwargs)
        return _fake_report(True)

    monkeypatch.setattr(qa_gate, "run_gate", fake_run_gate)
    cli.main(["qa-gate", "--targeted", "tests/test_x.py", "tests/test_y.py"])

    assert captured["targeted"] == ["tests/test_x.py", "tests/test_y.py"]


def test_qa_gate_v2_flags_flag_passed_through(monkeypatch):
    captured: dict = {}

    def fake_run_gate(**kwargs):
        captured.update(kwargs)
        return _fake_report(True)

    monkeypatch.setattr(qa_gate, "run_gate", fake_run_gate)
    cli.main(["qa-gate", "--v2-flags"])

    assert captured["v2_flags"] is True


def test_qa_gate_is_not_lead_only():
    # qa pane, backend pane, and a bare user terminal must all be able to run
    # this — it must never land in LEAD_ONLY_COMMANDS/TEAMMATE_ONLY_COMMANDS.
    assert "qa-gate" not in cli.LEAD_ONLY_COMMANDS
