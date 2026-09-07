"""Tests for `takkub usage [--days N|--month] [--provider p]` and
`takkub usage import` (#507) — CLI wiring only; `usage_ledger`'s own logic
is covered by `tests/test_usage_ledger.py`. `RUNTIME_DIR` is isolated per
test by `tests/conftest.py`'s autouse fixture.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli, usage_ledger


@pytest.fixture(autouse=True)
def _no_role_env(monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)


def test_usage_bare_command_prints_report_with_empty_ledger(capsys, monkeypatch):
    # No provider transcripts exist in this isolated test env — import_all
    # must be a safe no-op, and the report must still render.
    monkeypatch.setattr(usage_ledger, "import_all", lambda provider=None: {})
    rc = cli.main(["usage", "--days", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage report" in out


def test_usage_month_flag_is_threaded_through(capsys, monkeypatch):
    monkeypatch.setattr(usage_ledger, "import_all", lambda provider=None: {})
    rc = cli.main(["usage", "--month", "2026-08"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "month 2026-08" in out


def test_usage_provider_filter_is_threaded_through(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(usage_ledger, "import_all", lambda provider=None: {})

    def fake_query(*, days=None, month=None, provider=None):
        seen["provider"] = provider
        return {
            "start": "x",
            "end": "y",
            "month": None,
            "rows": [],
            "uncountable": [],
            "quota": [],
            "rtk_gain": None,
        }

    monkeypatch.setattr(usage_ledger, "query_usage", fake_query)
    cli.main(["usage", "--provider", "codex"])
    assert seen["provider"] == "codex"


def test_usage_import_subcommand_calls_import_all(monkeypatch, capsys):
    calls = []

    def fake_import_all(provider=None, source=None):
        calls.append((provider, source))
        return {"claude": {"scanned_files": 1, "skipped_files": 0, "new_turns": 5}}

    monkeypatch.setattr(usage_ledger, "import_all", fake_import_all)
    rc = cli.main(["usage", "import"])
    assert rc == 0
    assert calls == [(None, None)]
    out = capsys.readouterr().out
    assert "new_turns=5" in out


def test_usage_import_provider_flag_threaded_through(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usage_ledger,
        "import_all",
        lambda provider=None, source=None: (calls.append((provider, source)), {})[1],
    )
    cli.main(["usage", "import", "--provider", "gemini"])
    assert calls == [("gemini", None)]


def test_usage_import_source_flag_threaded_through(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usage_ledger,
        "import_all",
        lambda provider=None, source=None: (calls.append((provider, source)), {})[1],
    )
    cli.main(["usage", "import", "--provider", "claude", "--source", "C:/some/dir"])
    assert calls == [("claude", "C:/some/dir")]


def test_usage_import_source_without_provider_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(usage_ledger, "import_all", lambda provider=None, source=None: {})
    rc = cli.main(["usage", "import", "--source", "C:/some/dir"])
    assert rc != 0
    assert "--source requires --provider" in capsys.readouterr().err


def test_usage_import_prints_uncountable_provider_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_ledger,
        "import_all",
        lambda provider=None, source=None: {"gemini": {"error": "นับไม่ได้"}},
    )
    rc = cli.main(["usage", "import", "--provider", "gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "นับไม่ได้" in out
