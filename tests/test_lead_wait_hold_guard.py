"""`pane_guard`'s `lead_wait` rule (2026-10-12).

A Lead assigned a task, then sat in a foreground `takkub wait` for the whole
turn: the docs had called `takkub wait` "the" way to wait, so it became the
step after every assign. The default is to end the turn; a Lead that really
has nothing else to do says so with `--hold`.
"""

from __future__ import annotations

import pytest

from agent_takkub.pane_guard import LEAD_WAIT_RULE_TEXT, classify

# Verbatim from the Lead pane, 2026-10-12.
OBSERVED = "takkub wait --timeout 1500 2>&1 | Select-Object -Last 30"


def test_observed_lead_wait_is_denied() -> None:
    verdict = classify(OBSERVED, "lead")
    assert verdict.allowed is False
    assert verdict.rule == "lead_wait:piped"
    assert LEAD_WAIT_RULE_TEXT in verdict.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "takkub wait",
        "takkub wait --role backend --timeout 600",
        "takkub wait --timeout 600 2>&1",
        "takkub assign --role backend 'x' && takkub wait --role backend",
        "takkub.exe wait --role qa",
    ],
)
def test_wait_without_hold_is_denied(cmd: str) -> None:
    verdict = classify(cmd, "lead")
    assert verdict.allowed is False
    assert verdict.rule == "lead_wait:no-hold"
    assert "--hold" in verdict.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "takkub wait --hold",
        "takkub wait --hold --role backend --timeout 600 2>&1",
        "takkub wait --cancel",
        "takkub wait --help",
        # --hold on a later wait does not excuse an earlier one, but a
        # separate command after it is not part of the wait's args.
        "takkub wait --hold --role qa; git status",
    ],
)
def test_wait_with_hold_or_cancel_is_allowed(cmd: str) -> None:
    assert classify(cmd, "lead").allowed is True


@pytest.mark.parametrize(
    "pipe", ["| tail -5", "| head", "| Select-Object -Last 5", "| findstr done"]
)
def test_piped_wait_is_denied_even_with_hold(pipe: str) -> None:
    verdict = classify(f"takkub wait --hold --role backend {pipe}", "lead")
    assert verdict.allowed is False
    assert verdict.rule == "lead_wait:piped"


def test_quoted_payload_mentioning_wait_is_inert() -> None:
    cmd = 'takkub assign --role backend "then run takkub wait | tail to check"'
    assert classify(cmd, "lead").allowed is True


@pytest.mark.parametrize(
    "cmd",
    [
        'grep -rn "takkub wait" tests',
        'python - <<\'EOF\'\nassert classify("takkub wait --role x", "lead")\nEOF',
        "echo takkub wait is not the default",
    ],
)
def test_mentioning_wait_is_not_running_it(cmd: str) -> None:
    assert classify(cmd, "lead").allowed is True


def test_rtk_prefixed_and_loop_body_waits_are_caught() -> None:
    assert classify("rtk takkub wait --role qa", "lead").rule == "lead_wait:no-hold"
    assert classify("if true; then takkub wait; fi", "lead").rule == "lead_wait:no-hold"


def test_or_chain_after_hold_is_not_a_pipe() -> None:
    assert classify("takkub wait --hold || echo timed-out", "lead").allowed is True


@pytest.mark.parametrize("role", ["shell", "backend"])
def test_other_roles_are_not_held_to_it(role: str) -> None:
    verdict = classify("takkub wait --timeout 600", role)
    assert verdict.rule is None or not str(verdict.rule).startswith("lead_wait")
