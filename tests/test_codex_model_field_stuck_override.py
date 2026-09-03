"""Issue #276 round 3 (2026-09-02) — codex-cli 0.152.0's banner box can leave
its `model:` field stuck at "loading" FOREVER while `directory:`/
`permissions:` resolve normally and the composer underneath is already
genuinely interactive ("› Ask Codex to do anything").

Live-captured across 3 independent panes in one incident (reviewer, codex,
critic — all `saas_admin_amb`, transcripts under
runtime/sessions/2026-09-02/saas_admin_amb/): every one of them shows the
IDENTICAL two-frame sequence — `model: loading` + `directory: loading`
(genuine fresh boot, #380's own case) immediately followed by
`directory: ~\\...` (resolved) + `permissions: YOLO mode` (resolved) +
`model:` STILL "loading" — and none of the three ever recovered before
`BOOT_STALL_CEILING_SEC` fired `delivery_boot_timeout_failed`, even though
`heartbeat_age_s` at the ceiling reprobe was 0.02-0.17s (the pane was alive
and the composer was genuinely idle the whole time, not hung).

`_still_booting`'s wide window (#284) is RIGHT to read `model: loading` as a
boot marker in general — that is exactly the genuine #380 fresh-boot race,
where `directory:` also still reads "loading". This is the narrower, later
quirk: only `model:` gets stuck, independent of everything else the banner
reports. Root cause confirmed directly from the 3 real transcripts, not
inferred — see `pty_session._has_stuck_model_field_marker`'s docstring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.pty_session import _has_stuck_model_field_marker

# The exact two frames captured live (ANSI-stripped, lowercased) across all
# 3 panes of the 2026-09-02 incident.
_FRESH_BOOT_FRAME = (
    "model:     loading   /model to change\n"
    "directory: loading\n"
    "\n"
    "› ask codex to do anything\n"
    "  ? for shortcuts"
)
_STUCK_MODEL_FIELD_FRAME = (
    "model:       loading   /model to change\n"
    "directory:   ~\\webstormprojects\\saas_admin_amb\n"
    "permissions: yolo mode\n"
    "\n"
    "› ask codex to do anything\n"
    "  ? for shortcuts"
)


class TestStuckModelFieldMarkerPure:
    def test_fresh_boot_both_loading_is_not_the_stuck_quirk(self) -> None:
        """#380's own case: both fields loading together must NOT be read as
        the round-3 quirk — that would reopen the fresh-boot race #380
        fixed."""
        assert _has_stuck_model_field_marker(_FRESH_BOOT_FRAME) is False

    def test_model_stuck_while_directory_resolved_is_the_quirk(self) -> None:
        assert _has_stuck_model_field_marker(_STUCK_MODEL_FIELD_FRAME) is True

    def test_neither_field_loading_is_not_the_quirk(self) -> None:
        assert _has_stuck_model_field_marker("gpt-5.5 medium · ~/project · fast off") is False


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session(*, stuck: bool, provider: str) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock(return_value=True)
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    s.shows_startup_marker.return_value = True
    s.shows_account_pending_marker.return_value = False
    s.seconds_since_output.return_value = 0.1
    # The screen genuinely reads ready (composer live) — this is the whole
    # point of the round-3 quirk, unlike the ordinary MCP-splash case where
    # is_at_ready_prompt() stays False the entire time.
    s.is_at_ready_prompt.return_value = True
    s.shows_boot_phase_marker.return_value = True  # wide window: model: loading never clears
    s.shows_stuck_model_field_marker.return_value = stuck
    s.boot_phase_detail.return_value = ""  # not an MCP splash line — no "mcp" substring
    return s


def _pane(session: MagicMock, provider: str) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model.provider_name = provider
    p._session_generation = 0
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    monkeypatch.setattr(o, "_run_boot_diagnostic_async", MagicMock())
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    return o


def _written(session: MagicMock) -> list[str]:
    return [c.args[0] for c in session.write.call_args_list if c.args]


def _events(log: MagicMock) -> list:
    return [c.args[0] for c in log.call_args_list if c.args]


class TestDeliveryOverride:
    def test_delivers_once_the_settle_window_passes_instead_of_failing(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        monkeypatch.setenv(
            "TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0.45"
        )  # 3 polls, like splash test
        codex = _pane(_live_session(stuck=True, provider="codex"), "codex")
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(stuck=False, provider="claude"), "claude"),
            "reviewer": codex,
        }
        failures: list = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append((role, elapsed)),
        )

        with patch("agent_takkub.lead_inbox._log_event") as log:
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=3_000, project="P")

        assert failures == [], "must not fail a pane that is genuinely answering input"
        assert any("run smoke" in w for w in _written(codex.session)), "task must be delivered"
        assert "ready_marker_stuck_field_override" in _events(log)
        override_calls = [
            c
            for c in log.call_args_list
            if c.args and c.args[0] == "ready_marker_stuck_field_override"
        ]
        assert len(override_calls) == 1, "logged once, not every poll"
        assert override_calls[0].kwargs["role"] == "reviewer"

    def test_does_not_override_before_the_settle_window_elapses(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The override must not fire the instant the pane comes alive — it
        waits out the same settle window as the MCP-splash paste so a
        genuinely-still-settling composer is never raced."""
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "5")  # never reached at 300ms
        codex = _pane(_live_session(stuck=True, provider="codex"), "codex")
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(stuck=False, provider="claude"), "claude"),
            "reviewer": codex,
        }
        failures: list = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append((role, elapsed)),
        )
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)  # trips immediately once booting

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        assert not _written(codex.session), "must not paste before the settle window passes"
        assert len(failures) == 1, "still fails out — a fresh pane must not hang forever either"

    def test_noop_for_a_provider_with_no_splash_window(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """`_splash_paste_after_ms` is 0 for every non-codex provider, so this
        override can never fire for them — a pane genuinely stuck (for a
        real reason) on a provider without this quirk must still fail out
        normally."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)
        claude = _pane(_live_session(stuck=True, provider="claude"), "claude")
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(stuck=False, provider="claude"), "claude"),
            "reviewer": claude,
        }
        failures: list = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append((role, elapsed)),
        )

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        assert not _written(claude.session)
        assert len(failures) == 1, "no splash window for this provider — override must be a no-op"

    def test_fresh_boot_race_still_fails_normally_when_never_ready(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#380's own case must be unaffected: a pane that never reaches
        `is_at_ready_prompt()` (the genuine fresh-boot race, not the round-3
        quirk) must still hit the ordinary ceiling and fail."""
        monkeypatch.setenv("TAKKUB_BOOT_SPLASH_PASTE_AFTER_S_CODEX", "0")
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)
        session = _live_session(stuck=False, provider="codex")
        session.is_at_ready_prompt.return_value = False  # composer never settles
        codex = _pane(session, "codex")
        orch._panes_by_project["P"] = {
            "lead": _pane(_live_session(stuck=False, provider="claude"), "claude"),
            "reviewer": codex,
        }
        failures: list = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append((role, elapsed)),
        )

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        assert not _written(codex.session)
        assert len(failures) == 1
