"""#422 cherry-picks from the 2026-08-28 roadmap review:

1. every watchdog recovery event carries a closed-enum `reason` + a bounded
   `snapshot` + a `recovery_id` shared with its respawn event;
2. ProviderSpec exposes a derived capability matrix and the engine logs
   `provider_capability_fallback` instead of silently doing less;
3. `done`/`close` events carry `session_uuid` for correlation;
4. `takkub skills list|effective` shows what a role really gets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent_takkub import cli, config, maintenance, skill_policy, skill_scan
from agent_takkub.orchestrator_text import (
    RECOVERY_REASONS,
    classify_stuck_reason,
    recovery_snapshot,
)
from agent_takkub.provider_spec import (
    CAPABILITY_NAMES,
    CAPABILITY_STATES,
    PROVIDER_REGISTRY,
    capability_matrix,
    capability_state,
)

# ── item 1: recovery reason + snapshot ─────────────────────────────────────


class TestRecoveryReason:
    def test_idle_rounds_win_over_live_child(self) -> None:
        assert classify_stuck_reason(idle_rounds=2, live_child_defer_since=100.0) == (
            "idle_no_response"
        )

    def test_live_child_grace_expired(self) -> None:
        assert classify_stuck_reason(idle_rounds=0, live_child_defer_since=100.0) == (
            "child_alive_grace_expired"
        )

    def test_default_is_content_static(self) -> None:
        assert classify_stuck_reason(idle_rounds=0, live_child_defer_since=0.0) == "content_static"

    def test_every_reason_is_in_the_closed_vocabulary(self) -> None:
        for r in (
            classify_stuck_reason(idle_rounds=1, live_child_defer_since=0),
            classify_stuck_reason(idle_rounds=0, live_child_defer_since=1),
            classify_stuck_reason(idle_rounds=0, live_child_defer_since=0),
            "no_first_content",
            "no_first_content_retry_failed",
            "auth_failed",
            "account_pending",
        ):
            assert r in RECOVERY_REASONS


class _FakeSession:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def display_lines(self) -> list[str]:
        return self._lines


class TestRecoverySnapshot:
    def test_tail_drops_blank_and_spinner_lines_and_clips(self) -> None:
        sess = _FakeSession(["old", "", "  esc to interrupt  ", "x" * 300, "last line"] + [""] * 3)
        snap = recovery_snapshot(
            sess,
            now=1_000.0,
            last_output_ts=900.0,
            last_content_ts=400.0,
            assign_ts=None,
            children=["node.exe", "pwsh.exe"],
            spinner_phrases=("esc to interrupt",),
        )
        assert snap["tail"] == ["old", "x" * 120, "last line"]
        assert snap["since_last_byte_s"] == 100
        assert snap["since_content_change_s"] == 600
        assert snap["since_assign_s"] is None
        assert snap["children"] == ["node.exe", "pwsh.exe"]

    def test_dead_session_never_raises(self) -> None:
        class Boom:
            def display_lines(self):
                raise RuntimeError("gone")

        snap = recovery_snapshot(
            Boom(), now=1.0, last_output_ts=None, last_content_ts=None, assign_ts=None
        )
        assert snap["tail"] == []
        assert "children" not in snap
        # JSON-serialisable — it goes straight into events.log.
        json.dumps(snap)


class TestMaintenanceReasonBuckets:
    def test_scan_events_buckets_recoveries_by_reason(self, tmp_path: Path) -> None:
        import time

        log = tmp_path / "events.log"
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        rows = [
            {"ts": stamp, "event": "stuck_pane_recover", "reason": "idle_no_response"},
            {"ts": stamp, "event": "stuck_pane_recover", "reason": "idle_no_response"},
            {"ts": stamp, "event": "no_content_pane_recover", "reason": "no_first_content"},
            {"ts": stamp, "event": "stuck_pane_recover"},  # pre-#422 line
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        check = maintenance.scan_events(log, since_hours=1)
        joined = "\n".join(check.details)
        assert "idle_no_response ×2" in joined
        assert "no_first_content ×1" in joined
        assert "unclassified ×1" in joined


# ── item 2: capability matrix ──────────────────────────────────────────────


class TestCapabilityMatrix:
    @pytest.mark.parametrize("provider", sorted(PROVIDER_REGISTRY))
    def test_every_provider_has_every_capability_with_a_valid_state(self, provider: str) -> None:
        m = capability_matrix(PROVIDER_REGISTRY[provider])
        assert set(m) == set(CAPABILITY_NAMES)
        assert set(m.values()) <= set(CAPABILITY_STATES)

    def test_claude_native_skills_others_partial(self) -> None:
        assert capability_state("claude", "skills") == "supported"
        for other in ("codex", "gemini", "opencode", "kimi", "cursor"):
            assert capability_state(other, "skills") == "partial", other

    def test_resume_follows_the_flag_the_engine_actually_checks(self) -> None:
        # spawn refuses --resume unless BOTH supports_resume and
        # session_resume_flag are set — the matrix must say the same.
        for name, spec in PROVIDER_REGISTRY.items():
            expected = (
                "supported"
                if (spec.supports_resume and spec.session_resume_flag)
                else ("unsupported")
            )
            assert capability_state(name, "resume") == expected, name

    def test_unknown_is_never_reported_as_working(self) -> None:
        assert capability_state("nope", "skills") == "unsupported"
        assert capability_state("claude", "teleport") == "unsupported"

    def test_override_wins_only_for_known_keys_and_states(self) -> None:
        import dataclasses

        spec = dataclasses.replace(
            PROVIDER_REGISTRY["claude"],
            capability_overrides={"skills": "experimental", "bogus": "supported", "mcp": "wat"},
        )
        m = capability_matrix(spec)
        assert m["skills"] == "experimental"
        assert "bogus" not in m
        assert m["mcp"] == "supported"

    def test_doctor_reports_gaps_as_info_never_fail(self) -> None:
        from agent_takkub.doctor import Status, check_provider_capabilities

        findings = check_provider_capabilities()
        assert {f.name for f in findings} == set(PROVIDER_REGISTRY)
        assert all(f.status in (Status.OK, Status.INFO) for f in findings)
        codex = next(f for f in findings if f.name == "codex")
        assert "partial: " in codex.detail and "skills" in codex.detail


# ── item 4: takkub skills CLI ──────────────────────────────────────────────


@pytest.fixture
def skills_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(config, "PROJECT_SKILLS_HOME", tmp_path / "central" / "project-skills")
    monkeypatch.setattr(config, "GLOBAL_SKILLS_HOME", tmp_path / "central" / "skills")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "cockpit")
    monkeypatch.setattr(config, "active_project", lambda: ("myproj", str(project)))
    monkeypatch.setattr(
        "agent_takkub.lead_context._allowed_project_roots", lambda p: [project.resolve()]
    )
    ok, err = skill_scan.create_skill(project, "proj-skill", "p", "b", project_ns="myproj")
    assert ok, err
    ok, err = skill_scan.create_skill(
        project, "glob-skill", "g", "b", project_ns="myproj", scope="global"
    )
    assert ok, err
    # A global skill nobody linked yet must still be listed.
    unlinked = config.GLOBAL_SKILLS_HOME / "unlinked"
    unlinked.mkdir(parents=True)
    (unlinked / "SKILL.md").write_text(
        "---\nname: unlinked\ndescription: u\n---\nx", encoding="utf-8"
    )
    monkeypatch.setattr(skill_policy, "effective_skills", lambda role: ["proj-skill", "ghost"])
    return {"project": project}


class TestSkillsCli:
    def test_list_labels_real_home(self, skills_world: dict, capsys) -> None:
        args = argparse.Namespace(skills_cmd="list", project=None, only_global=False)
        resp = cli.cmd_skills(args)
        out = capsys.readouterr().out
        assert resp["ok"]
        assert "proj-skill" in out and "project" in out
        assert "glob-skill" in out and "global" in out
        assert "unlinked" in out

    def test_list_global_only(self, skills_world: dict, capsys) -> None:
        args = argparse.Namespace(skills_cmd="list", project=None, only_global=True)
        cli.cmd_skills(args)
        out = capsys.readouterr().out
        assert "glob-skill" in out and "unlinked" in out
        assert "proj-skill" not in out

    def test_effective_flags_missing_and_provider_bridge(self, skills_world: dict, capsys) -> None:
        args = argparse.Namespace(
            skills_cmd="effective", project=None, role="backend", provider="codex"
        )
        resp = cli.cmd_skills(args)
        out = capsys.readouterr().out
        assert resp["ok"] is False and "ghost" in resp["msg"]
        assert "✓ proj-skill" in out
        assert "✗ ghost" in out
        assert "instruction-only" in out

    def test_effective_claude_is_native(self, skills_world: dict, capsys) -> None:
        args = argparse.Namespace(
            skills_cmd="effective", project=None, role="backend", provider="claude"
        )
        cli.cmd_skills(args)
        assert "native Skill tool" in capsys.readouterr().out

    def test_parser_wires_subcommands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[argparse.Namespace] = []
        monkeypatch.setattr(
            cli, "cmd_skills", lambda a: seen.append(a) or {"ok": True, "msg": "stub"}
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        cli.main(["skills", "list", "--global"])
        cli.main(["skills", "effective", "--role", "qa"])
        assert seen[0].skills_cmd == "list" and seen[0].only_global is True
        assert seen[1].skills_cmd == "effective" and seen[1].role == "qa"
