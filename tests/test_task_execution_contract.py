"""Provider-neutral task contract: every assignment receives one clear work/report rule."""

from agent_takkub.orchestrator_text import _append_task_execution_contract


def test_contract_keeps_operator_task_first_and_sets_evidence_report() -> None:
    task = "Fix the login regression in src/auth.py"
    out = _append_task_execution_contract(task)
    assert out.startswith(task)
    assert "CHANGED" in out and "EVIDENCE" in out and "REMAINING" in out
    assert "previous failure and attempts" in out


def test_contract_is_idempotent_on_replay() -> None:
    once = _append_task_execution_contract("Fix the login regression")
    assert _append_task_execution_contract(once) == once
    assert once.count("takkub-task-execution-v1") == 1
