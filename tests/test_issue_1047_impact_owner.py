"""#1047: the impact gate asks a pane for the checks its own role owns."""

from __future__ import annotations

from agent_takkub.impact_contract import (
    FLOWS,
    checks_owned_by,
    outstanding_checks,
    plan_digest,
    validate_evidence,
)


def _plan() -> dict:
    def check(cid, flow, owner):
        return {"id": cid, "flow": flow, "expected": "ok", "method": "run it", "owner": owner}

    return {
        "trigger": "audio mode switch",
        "before": "old",
        "after": "new",
        "source_of_truth": "project.audio_mode",
        "upstream": ["settings"],
        "downstream": ["export"],
        "checks": [
            check("api-mode", "api", "backend"),
            check("queue-mode", "state", "backend"),
            check("voice-ui", "ui", "frontend"),
            check("export-audio", "export", "qa"),
        ],
        "not_applicable": {
            f: "This change does not reach this flow."
            for f in FLOWS
            if f not in {"api", "state", "ui", "export"}
        },
    }


def _passed(cid):
    return {"id": cid, "status": "pass", "evidence": "fake ledger checked", "kind": "fake"}


def _evidence(plan, checks):
    return {"task_id": "t1", "plan_digest": plan_digest(plan), "revision": "abc", "checks": checks}


def _check(plan, checks, role):
    return validate_evidence(plan, _evidence(plan, checks), task_id="t1", revision="abc", role=role)


def test_a_pane_closes_with_only_its_own_roles_checks():
    plan = _plan()
    own = [_passed("api-mode"), _passed("queue-mode")]
    assert _check(plan, own, "backend") == ""
    assert outstanding_checks(plan, _evidence(plan, own)) == [
        "voice-ui (frontend)",
        "export-audio (qa)",
    ]
    # the pre-#1047 gate (no role) still wants the whole plan
    assert "do not match the plan" in _check(plan, own, None)


def test_own_required_check_cannot_be_skipped_or_left_pending():
    plan = _plan()
    assert "missing for backend: queue-mode" in _check(plan, [_passed("api-mode")], "backend")
    pending = {"id": "queue-mode", "status": "pending", "limitation": "later"}
    assert "queue-mode is not passed" in _check(plan, [_passed("api-mode"), pending], "backend")


def test_another_owners_check_may_be_reported_pending_but_not_blank():
    plan = _plan()
    own = [_passed("api-mode"), _passed("queue-mode")]
    other = {"id": "voice-ui", "status": "pending", "limitation": "frontend pane owns this"}
    assert _check(plan, [*own, other], "backend") == ""
    assert "naming who covers it" in _check(
        plan, [*own, {"id": "voice-ui", "status": "pending"}], "backend"
    )
    assert "not in the plan: nope" in _check(plan, [*own, _passed("nope")], "backend")


def test_a_shard_defers_its_roles_checks_to_a_sibling_by_name():
    plan = _plan()
    mine = _passed("api-mode")
    sibling = {"id": "queue-mode", "status": "pending", "limitation": "backend#2 covers the queue"}
    assert _check(plan, [mine, sibling], "backend#1") == ""
    assert "missing for backend#1: queue-mode" in _check(plan, [mine], "backend#1")


def test_checker_positions_are_one_owner_and_an_unnamed_role_owns_everything():
    plan = _plan()
    assert checks_owned_by(plan, "reviewer") == {"export-audio"}
    assert checks_owned_by(plan, "critic#2") == {"export-audio"}
    assert checks_owned_by(plan, "devops") is None
    assert "do not match the plan" in _check(plan, [_passed("api-mode")], "devops")
