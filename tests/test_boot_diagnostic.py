"""Tests for the boot-stall diagnostic follow-up (issue #273).

`_warn_lead_delivery_boot_stall`'s existing notice stays generic ("ค้างอยู่
ที่ boot phase") — this ALSO fires `_run_boot_diagnostic_async`, a best-effort
ASYNC (QProcess, never blocking) probe of the stuck provider's own
health-check command (e.g. `codex mcp list`), so a follow-up notice can carry
the provider's OWN real error text instead of leaving Lead with only the
generic wording.

No test here spawns a real subprocess: guard-condition tests patch
`lead_inbox.QProcess` to detect whether construction was even attempted, and
the notice-text decision itself is exercised as the pure
`_boot_diagnostic_notice_text` staticmethod.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    yield o
    # #344/#345: test_provider_with_argv_and_binary_does_spawn reaches
    # _run_boot_diagnostic_async's real path, which arms an 8s QTimer(self)
    # watchdog (lead_inbox.py) — QProcess is mocked so it never fires, so
    # stop it here rather than leaving it ticking against this torn-down
    # Orchestrator for the rest of the session.
    o.shutdown_timers()


class TestBootDiagnosticNoticeText:
    """Pure decision logic — no process involved."""

    def test_nonzero_exit_with_output_reports(self) -> None:
        text = Orchestrator._boot_diagnostic_notice_text(
            "frontend",
            "codex",
            ("mcp", "list"),
            1,
            "Error: failed to load bootstrap configuration",
        )
        assert text is not None
        assert "boot-diagnostic" in text
        assert "frontend" in text
        assert "codex mcp list" in text
        assert "failed to load bootstrap configuration" in text
        assert "#273" in text

    def test_clean_exit_is_silent(self) -> None:
        # A stuck-boot diagnostic that comes back clean is not itself news.
        text = Orchestrator._boot_diagnostic_notice_text(
            "frontend", "codex", ("mcp", "list"), 0, "No MCP servers configured yet"
        )
        assert text is None

    def test_nonzero_exit_with_empty_output_is_silent(self) -> None:
        text = Orchestrator._boot_diagnostic_notice_text(
            "frontend", "codex", ("mcp", "list"), 1, ""
        )
        assert text is None

    def test_output_truncated_to_500_chars(self) -> None:
        text = Orchestrator._boot_diagnostic_notice_text(
            "frontend", "codex", ("mcp", "list"), 1, "x" * 2000
        )
        assert text is not None
        assert len(text) < 700


class TestRunBootDiagnosticAsyncGuards:
    """No argv / no binary must never even construct a QProcess."""

    def test_provider_without_diagnostic_argv_never_spawns(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for", lambda *_a, **_kw: "claude"
        )
        monkeypatch.setattr(orch, "_resolve_project", lambda p=None: p or "P")
        with patch("agent_takkub.lead_inbox.QProcess") as mock_qprocess:
            orch._run_boot_diagnostic_async("backend", "P", 120.0)
        mock_qprocess.assert_not_called()

    def test_provider_with_argv_but_no_binary_never_spawns(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for", lambda *_a, **_kw: "codex"
        )
        monkeypatch.setitem(
            PROVIDER_REGISTRY,
            "codex",
            replace(
                PROVIDER_REGISTRY["codex"],
                boot_diagnostic_argv=("mcp", "list"),
                custom_discovery_fn=lambda: None,
            ),
        )
        monkeypatch.setattr(orch, "_resolve_project", lambda p=None: p or "P")
        with patch("agent_takkub.lead_inbox.QProcess") as mock_qprocess:
            orch._run_boot_diagnostic_async("backend", "P", 120.0)
        mock_qprocess.assert_not_called()

    def test_discovery_exception_never_spawns(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str | None:
            raise OSError("boom")

        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for", lambda *_a, **_kw: "codex"
        )
        monkeypatch.setitem(
            PROVIDER_REGISTRY,
            "codex",
            replace(
                PROVIDER_REGISTRY["codex"],
                boot_diagnostic_argv=("mcp", "list"),
                custom_discovery_fn=_boom,
            ),
        )
        monkeypatch.setattr(orch, "_resolve_project", lambda p=None: p or "P")
        with patch("agent_takkub.lead_inbox.QProcess") as mock_qprocess:
            orch._run_boot_diagnostic_async("backend", "P", 120.0)
        mock_qprocess.assert_not_called()

    def test_provider_with_argv_and_binary_does_spawn(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Confirms the positive path actually reaches QProcess construction
        # — patched away so nothing real ever runs.
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for", lambda *_a, **_kw: "codex"
        )
        monkeypatch.setitem(
            PROVIDER_REGISTRY,
            "codex",
            replace(
                PROVIDER_REGISTRY["codex"],
                boot_diagnostic_argv=("mcp", "list"),
                custom_discovery_fn=lambda: "C:/fake/codex.exe",
            ),
        )
        monkeypatch.setattr(orch, "_resolve_project", lambda p=None: p or "P")
        with patch("agent_takkub.lead_inbox.QProcess") as mock_qprocess:
            mock_proc = MagicMock()
            mock_qprocess.return_value = mock_proc
            orch._run_boot_diagnostic_async("backend", "P", 120.0)
        mock_qprocess.assert_called_once()
        mock_proc.start.assert_called_once_with("C:/fake/codex.exe", ["mcp", "list"])


class TestCodexSpecConfirmed:
    def test_codex_has_a_confirmed_boot_diagnostic(self) -> None:
        assert PROVIDER_REGISTRY["codex"].boot_diagnostic_argv == ("mcp", "list")

    def test_claude_has_no_boot_diagnostic_confirmed(self) -> None:
        # Never guess an argv for a provider it hasn't been observed on.
        assert PROVIDER_REGISTRY["claude"].boot_diagnostic_argv is None
