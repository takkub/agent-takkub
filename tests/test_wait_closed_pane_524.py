"""#524: `takkub wait --role a --role b` must skip+warn a role whose pane
already closed after reporting, not fail the whole command the same way a
genuinely typo'd/never-spawned role name does (#428's protection must stay
narrow to that one real error case).
"""

from __future__ import annotations

import argparse

from agent_takkub import cli, lead_wait


def test_already_reported_gone_role_does_not_fail_whole_wait(monkeypatch):
    calls = []

    def _fake_request(payload, **kw):
        calls.append(payload)
        if payload["cmd"] == "wait-begin":
            return {"ok": True, "wait_id": "w1", "roles": payload["roles"]}
        if payload["cmd"] == "wait-poll":
            return {
                "ok": True,
                "pending": {},
                "done": {"qa": "delivered"},
                "failed": {},
                "gone": {"qa2": lead_wait._GONE_ALREADY_REPORTED_DETAIL},
                "elapsed": 1,
            }
        return {"ok": True}

    monkeypatch.setattr(cli, "_request", _fake_request)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = argparse.Namespace(role=["qa", "qa2"], timeout=60, cancel=False, no_interrupt=False)

    out = cli.cmd_wait(args)

    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert out["gone"] == {"qa2": lead_wait._GONE_ALREADY_REPORTED_DETAIL}
    assert "role ไม่พบ" not in out["msg"]


def test_already_reported_detail_never_matches_never_spawned_substring():
    assert lead_wait._GONE_NEVER_SPAWNED not in lead_wait._GONE_ALREADY_REPORTED_DETAIL
