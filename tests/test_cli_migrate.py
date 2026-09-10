"""`takkub migrate {inspect,plan,dry-run,apply,validate,rollback}` (#309
Phase 4) — pure-local, no orchestrator socket, same rationale as `takkub
worktree`/`takkub doctor`."""

from __future__ import annotations

import json

import pytest

from agent_takkub import cli


def _json_body(out: str):
    """`cli.main` appends a trailing "ok: <msg>"/"err: <msg>" status line
    after any command's JSON output (cli.py's own epilogue) — strip it
    before parsing."""
    body, _, _tail = out.rpartition("\nok: ")
    if not body:
        body, _, _tail = out.rpartition("\nerr: ")
    return json.loads(body or out)


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    """#309 Phase 8b widened the default ladder from 1 step to 8, #360 to 9
    (plan §5.3) — several of those write real V2-layout files derived from
    `config.DATA_HOME`/`config.SETTINGS_HOME`. Without this, `apply` would
    read this machine's real ~/.takkub and write a stray `v2/` dir into the
    dev checkout's DATA_HOME (a real side effect a CLI test must never
    have)."""
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data_home")
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path / "settings_home")


def test_migrate_inspect_returns_ok(capsys):
    rc = cli.main(["migrate", "inspect", "--json"])
    assert rc == 0
    out = _json_body(capsys.readouterr().out)
    assert len(out) == 11
    assert out[0]["step_id"] == "version-marker"
    assert out[0]["stage"] == "inspect"
    assert all(r["ok"] for r in out)


def test_migrate_plan_and_dry_run_are_read_only(capsys):
    assert cli.main(["migrate", "plan", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["migrate", "dry-run", "--json"]) == 0
    out = _json_body(capsys.readouterr().out)
    assert out[0]["stage"] == "dry_run"
    assert all(r["ok"] for r in out)


def test_migrate_apply_validate_rollback_full_cycle(capsys):
    rc = cli.main(["migrate", "apply", "--json"])
    assert rc == 0
    apply_out = _json_body(capsys.readouterr().out)
    assert len(apply_out) == 11
    assert all(r["ok"] for r in apply_out)

    rc = cli.main(["migrate", "validate", "--json"])
    assert rc == 0
    validate_out = _json_body(capsys.readouterr().out)
    assert all(r["ok"] for r in validate_out)

    rc = cli.main(["migrate", "rollback", "--json"])
    assert rc == 0
    rollback_out = _json_body(capsys.readouterr().out)
    assert all(r["ok"] for r in rollback_out)


def test_migrate_text_output_shows_step_id(capsys):
    rc = cli.main(["migrate", "inspect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "version-marker" in out


def test_migrate_requires_a_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["migrate"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# #504 H1/H3 (2026-09-10 acceptance review): `restore-v1 --list`/`--archive`
# and stop-on-failure, driven through the real CLI.
# ---------------------------------------------------------------------------


def test_restore_v1_list_reports_no_archives_on_a_clean_install(capsys):
    rc = cli.main(["migrate", "restore-v1", "--list", "--json"])
    assert rc == 0
    out = _json_body(capsys.readouterr().out)
    assert out == []


def test_restore_v1_list_and_archive_select_after_a_real_archive(capsys):
    from agent_takkub import config

    config.DATA_HOME.mkdir(parents=True, exist_ok=True)
    (config.DATA_HOME / "projects.json").write_text(
        '{"active": null, "projects": {}}', encoding="utf-8"
    )

    rc = cli.main(["migrate", "apply", "--json"])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["migrate", "restore-v1", "--list", "--json"])
    assert rc == 0
    archives = _json_body(capsys.readouterr().out)
    assert len(archives) == 1
    ts = archives[0]["ts"]

    rc = cli.main(["migrate", "restore-v1", "--archive", ts, "--json"])
    assert rc == 0
    out = _json_body(capsys.readouterr().out)
    assert any(r["step_id"] == "archive-v1-legacy" and r["ok"] for r in out)
    assert (config.DATA_HOME / "projects.json").exists()


def test_restore_v1_never_runs_promote_rollback_after_a_failed_archive_rollback(capsys):
    """#504 H3: `cli.py` used to run `promote-v2-root`'s rollback even when
    `archive-v1-legacy`'s just failed, reconstructing only half of the
    pre-2.1.0 shape while reporting the command as having tried both."""
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.report import StepReport

    calls: list[str] = []
    real_get_step = MigrationEngine.get_step

    class _FailingArchiveStep:
        step_id = "archive-v1-legacy"

        def rollback(self, archive_ts=None):
            calls.append("archive")
            return StepReport(self.step_id, "rollback", False, "boom")

    def _patched_get_step(self, step_id):
        if step_id == "archive-v1-legacy":
            return _FailingArchiveStep()
        return real_get_step(self, step_id)

    def _patched_rollback_step(self, step_id):
        calls.append(step_id)
        return StepReport(step_id, "rollback", True, "should never run")

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(MigrationEngine, "get_step", _patched_get_step)
        mp.setattr(MigrationEngine, "rollback_step", _patched_rollback_step)
        rc = cli.main(["migrate", "restore-v1", "--json"])
    finally:
        mp.undo()

    assert rc != 0
    out = _json_body(capsys.readouterr().out)
    assert [r["step_id"] for r in out] == ["archive-v1-legacy"]
    assert "promote-v2-root" not in calls


# ---------------------------------------------------------------------------
# 2026-09-10 acceptance review Round 3 — `disk_cli_apply`/`disk_cli_restore-v1`:
# `apply`/`restore-v1` used to mutate storage with ZERO free-space checks,
# unlike the automatic boot-time path (`auto_migrate_boot.run_boot_stage`).
# ---------------------------------------------------------------------------


def test_migrate_apply_refuses_on_insufficient_disk_space(capsys, monkeypatch):
    from agent_takkub import auto_migrate_boot as boot

    monkeypatch.setattr(boot, "_disk_has_room", lambda *a, **k: False)
    rc = cli.main(["migrate", "apply", "--json"])
    assert rc != 0
    assert "insufficient free disk space" in capsys.readouterr().out


def test_migrate_restore_v1_refuses_on_insufficient_disk_space(capsys, monkeypatch):
    from agent_takkub import auto_migrate_boot as boot

    monkeypatch.setattr(boot, "_disk_has_room", lambda *a, **k: False)
    rc = cli.main(["migrate", "restore-v1", "--json"])
    assert rc != 0
    assert "insufficient free disk space" in capsys.readouterr().out


def test_migrate_restore_v1_list_archives_is_exempt_from_the_disk_gate(capsys, monkeypatch):
    """`--list` is read-only — it must never be refused for lack of disk
    space, since it doesn't write anything."""
    from agent_takkub import auto_migrate_boot as boot

    monkeypatch.setattr(boot, "_disk_has_room", lambda *a, **k: False)
    rc = cli.main(["migrate", "restore-v1", "--list", "--json"])
    assert rc == 0
