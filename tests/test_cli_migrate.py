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
    """#309 Phase 8b widened the default ladder from 1 step to 8 (plan
    §5.3) — several of those write real V2-layout files derived from
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
    assert len(out) == 8
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
    assert len(apply_out) == 8
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
