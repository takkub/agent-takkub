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
    # #574 added `pre-migrate-backup` as ladder step 0.
    assert len(out) == 12
    assert out[0]["step_id"] == "pre-migrate-backup"
    assert out[1]["step_id"] == "version-marker"
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
    assert len(apply_out) == 12  # #574 added `pre-migrate-backup` as ladder step 0.
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


def test_restore_v1_reapplies_version_marker_so_validate_passes_after(capsys, monkeypatch):
    """#574 round11 item 5: a real "resume restore" rehearsal ran
    `restore-v1` successfully but left `runtime/core/version.json` (or
    wherever `core_home()` resolves post-restore) stamped with whatever
    build applied the ladder originally — `takkub migrate validate` then
    failed `version-marker` ("app component missing/mismatched") the
    moment the running build had moved on since. `restore-v1` must
    re-stamp the marker with the CURRENTLY running build, not leave it
    stale (never a byte-revert to the old build's value — that would
    reintroduce the exact mismatch this fix removes on any machine that
    hasn't also been downgraded)."""
    from agent_takkub import config
    from agent_takkub.core.migration import steps as steps_mod
    from agent_takkub.core.migration.engine import MigrationEngine

    config.DATA_HOME.mkdir(parents=True, exist_ok=True)
    (config.DATA_HOME / "projects.json").write_text(
        '{"active": null, "projects": {}}', encoding="utf-8"
    )

    rc = cli.main(["migrate", "apply", "--json"])
    assert rc == 0
    capsys.readouterr()

    # Simulate the app having been upgraded since the original apply.
    monkeypatch.setattr(steps_mod, "APP_VERSION", "999.0.0")

    rc = cli.main(["migrate", "restore-v1", "--json"])
    assert rc == 0
    out = _json_body(capsys.readouterr().out)
    marker_report = next(r for r in out if r["step_id"] == "version-marker")
    assert marker_report["ok"]
    # Proves the real re-apply branch ran (not #504 round11b's skip path
    # below) — the engine built by `MigrationEngine()` here has a real
    # `version-marker` step, so `get_step` must succeed.
    assert "skipped" not in marker_report["summary"]

    engine = MigrationEngine()
    validate_report = engine.get_step("version-marker").validate()
    assert validate_report.ok, validate_report.summary


def test_restore_v1_skips_version_marker_when_engine_has_no_such_step(capsys):
    """#504 round11b: harness/test callers build a `MigrationEngine` with
    only the two steps `restore-v1` actually undoes (`promote-v2-root`,
    `archive-v1-legacy`) — e.g. `MigrationEngine([promote, archive], ...)`,
    as several fault-injection harnesses do by calling
    `_cmd_migrate_restore_v1` directly. `engine.get_step("version-marker")`
    then raises `KeyError`, which used to propagate out of `restore-v1`
    uncaught — even though the docstring above already says a stale marker
    must never cascade into reporting the archive/promote restore itself
    as failed. This must degrade to a skipped-but-ok report instead."""
    from argparse import Namespace

    from agent_takkub import config
    from agent_takkub.cli import _cmd_migrate_restore_v1
    from agent_takkub.core.migration.engine import MigrationEngine

    config.DATA_HOME.mkdir(parents=True, exist_ok=True)
    (config.DATA_HOME / "projects.json").write_text(
        '{"active": null, "projects": {}}', encoding="utf-8"
    )

    rc = cli.main(["migrate", "apply", "--json"])
    assert rc == 0
    capsys.readouterr()

    full_engine = MigrationEngine()
    reduced_engine = MigrationEngine(
        [full_engine.get_step("promote-v2-root"), full_engine.get_step("archive-v1-legacy")]
    )

    with pytest.raises(KeyError):
        reduced_engine.get_step("version-marker")

    reports = _cmd_migrate_restore_v1(reduced_engine, Namespace(archive_ts=None, json=True))

    marker_report = next(r for r in reports if r.step_id == "version-marker")
    assert marker_report.ok
    assert "skipped" in marker_report.summary
    assert all(r.ok for r in reports)


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


# ---------------------------------------------------------------------------
# #504 round4 R4-H7: `promote-v2-root`'s own rollback is part of the SAME
# all-or-nothing `restore-v1` command as the archive-generation walk — a
# failure there must revert every archive generation this same call already
# restored too, not just report its own half-failure.
# ---------------------------------------------------------------------------


def test_restore_v1_reverts_archived_content_when_promote_rollback_fails(capsys, monkeypatch):
    from agent_takkub import config
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.report import StepReport

    config.DATA_HOME.mkdir(parents=True, exist_ok=True)
    (config.DATA_HOME / "projects.json").write_text('{"projects": {}}', encoding="utf-8")

    rc = cli.main(["migrate", "apply", "--json"])
    assert rc == 0
    capsys.readouterr()
    # `apply()` archived "projects.json" away — data_home no longer has it.
    assert not (config.DATA_HOME / "projects.json").exists()

    real_rollback_step = MigrationEngine.rollback_step

    def _fail_promote_rollback(self, step_id):
        if step_id == "promote-v2-root":
            return StepReport(step_id, "rollback", False, "injected promote rollback failure")
        return real_rollback_step(self, step_id)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(MigrationEngine, "rollback_step", _fail_promote_rollback)
        rc = cli.main(["migrate", "restore-v1", "--json"])
    finally:
        mp.undo()

    assert rc != 0
    out = _json_body(capsys.readouterr().out)
    step_ids = [r["step_id"] for r in out]
    assert "archive-v1-legacy" in step_ids
    assert "promote-v2-root" in step_ids
    # The whole command reverted — "projects.json" (restored by the real
    # archive-v1-legacy rollback above) is gone again, back to the state
    # this restore-v1 command actually started from.
    assert not (config.DATA_HOME / "projects.json").exists()
