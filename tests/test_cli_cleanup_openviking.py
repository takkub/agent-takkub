"""`takkub cleanup openviking` — the replacement for the removed `takkub ov
managed remove` (docs/plans/remove-openviking-2026-08-24/07_RUNTIME_DATA_
MIGRATION.md). No real filesystem/process I/O ever runs —
`openviking_cleanup.report`/`stop_owned_process`/`remove` are all stubbed,
same posture the old `test_cli_ov_managed.py` used for its own CLI surface.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli, openviking_cleanup


@pytest.fixture(autouse=True)
def _no_role_env(monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)


def _report(**overrides):
    defaults = dict(exists=True, path="/fake/openviking", size_bytes=1024, owned_pid=None)
    defaults.update(overrides)
    return openviking_cleanup.CleanupReport(**defaults)


def test_no_install_found_fails_without_prompting(monkeypatch, capsys):
    monkeypatch.setattr(openviking_cleanup, "report", lambda: _report(exists=False))

    def _fail_input(prompt=""):
        raise AssertionError("must not prompt when nothing is installed")

    monkeypatch.setattr("builtins.input", _fail_input)

    code = cli.main(["cleanup", "openviking"])

    assert code == 1
    assert "nothing to clean up" in capsys.readouterr().out


def test_declines_without_yes_when_input_says_no(monkeypatch, capsys):
    monkeypatch.setattr(openviking_cleanup, "report", lambda: _report())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    stop_calls = []
    remove_calls = []
    monkeypatch.setattr(openviking_cleanup, "stop_owned_process", lambda: stop_calls.append(1))
    monkeypatch.setattr(openviking_cleanup, "remove", lambda **kw: remove_calls.append(kw))

    code = cli.main(["cleanup", "openviking"])

    assert code == 1
    assert stop_calls == []
    assert remove_calls == []


def test_yes_flag_skips_prompt_stops_then_removes_keeping_data(monkeypatch, capsys):
    monkeypatch.setattr(openviking_cleanup, "report", lambda: _report(owned_pid=4242))

    def _fail_input(prompt=""):
        raise AssertionError("must not prompt when --yes is passed")

    monkeypatch.setattr("builtins.input", _fail_input)
    stop_calls = []
    remove_calls = []
    monkeypatch.setattr(openviking_cleanup, "stop_owned_process", lambda: stop_calls.append(1))
    monkeypatch.setattr(openviking_cleanup, "remove", lambda **kw: remove_calls.append(kw))

    code = cli.main(["cleanup", "openviking", "--yes"])

    assert code == 0
    assert stop_calls == [1]
    assert remove_calls == [{"purge_data": False}]
    assert "config/data kept" in capsys.readouterr().out


def test_purge_data_forwards_true(monkeypatch, capsys):
    monkeypatch.setattr(openviking_cleanup, "report", lambda: _report())
    monkeypatch.setattr(openviking_cleanup, "stop_owned_process", lambda: None)
    remove_calls = []
    monkeypatch.setattr(openviking_cleanup, "remove", lambda **kw: remove_calls.append(kw))

    code = cli.main(["cleanup", "openviking", "--yes", "--purge-data"])

    assert code == 0
    assert remove_calls == [{"purge_data": True}]
    assert "data purged" in capsys.readouterr().out


def test_never_touches_a_process_the_pid_file_did_not_name(monkeypatch, capsys):
    """`report().owned_pid is None` (no PID file, or a dead one) — the CLI
    still calls `stop_owned_process`, but that function itself is the one
    that guarantees nothing gets killed; this only proves the CLI doesn't
    bypass it."""
    monkeypatch.setattr(openviking_cleanup, "report", lambda: _report(owned_pid=None))
    monkeypatch.setattr(openviking_cleanup, "remove", lambda **kw: None)
    stop_calls = []
    monkeypatch.setattr(openviking_cleanup, "stop_owned_process", lambda: stop_calls.append(1))

    code = cli.main(["cleanup", "openviking", "--yes"])

    assert code == 0
    assert stop_calls == [1]
