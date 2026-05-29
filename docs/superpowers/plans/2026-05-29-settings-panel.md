# Unified Settings Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scattered status-bar config controls (plan chip, codex/gemini chips, 🤖 Providers + Claude Auth buttons) with a single ⚙ Settings dialog, and swap the `[system]` broadcast-to-Lead mechanism for a deterministic Lead-pane restart that preserves context via `--resume`.

**Architecture:** A pure-logic core (`restart_needed`, `settings_button_label`, `SettingsSnapshot`) that is unit-tested without Qt; an `orchestrator.restart_leads()` method that respawns every Lead pane while preserving its cached session UUID so `spawn()` picks `--resume`; a `SettingsDialog` (QDialog) that reads `plan_tier`/`provider_state` and opens the existing `RoleProviderDialog`/`ClaudeAuthDialog` as sub-dialogs; and `main_window` wiring that replaces 5 widgets with 1 ⚙ button and runs the save→confirm→restart flow.

**Tech Stack:** Python 3, PyQt6, pytest. State files unchanged: `~/.takkub/plan.json`, `~/.takkub/disabled-providers.json`.

**Spec:** `docs/superpowers/specs/2026-05-29-settings-panel-design.md`

---

## File Structure

- **Create** `src/agent_takkub/settings_dialog.py` — `SettingsSnapshot` dataclass, `restart_needed()`, `settings_button_label()`, `SettingsDialog(QDialog)`. One responsibility: the unified settings UI + its pure decision helpers.
- **Create** `tests/test_settings_dialog.py` — unit tests for the pure helpers (+ optional offscreen dialog test).
- **Modify** `src/agent_takkub/orchestrator.py` — add `restart_leads()`; strip the `[system]` broadcast loop from `set_plan_tier()` and `toggle_provider()`.
- **Create** `tests/test_restart_leads.py` — unit test for `restart_leads()` with fake panes.
- **Modify** `src/agent_takkub/main_window.py` — remove 3 chips + 2 buttons + their handlers/style-helpers; add the ⚙ Settings button + `_on_settings_clicked` save flow + `_refresh_settings_button`.
- **Modify** `CLAUDE.md` — update "Account plan toggle" + "Disabled providers" sections to describe the dialog + restart (not broadcast).

---

## Task 1: Pure helpers — `SettingsSnapshot` + `restart_needed()`

**Files:**
- Create: `src/agent_takkub/settings_dialog.py`
- Test: `tests/test_settings_dialog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings_dialog.py
from __future__ import annotations

from agent_takkub.settings_dialog import SettingsSnapshot, restart_needed


def test_restart_needed_plan_change():
    before = SettingsSnapshot(tier="max", disabled=frozenset())
    after = SettingsSnapshot(tier="pro", disabled=frozenset())
    assert restart_needed(before, after) is True


def test_restart_needed_provider_change():
    before = SettingsSnapshot(tier="max", disabled=frozenset())
    after = SettingsSnapshot(tier="max", disabled=frozenset({"codex"}))
    assert restart_needed(before, after) is True


def test_restart_needed_no_change():
    snap = SettingsSnapshot(tier="max", disabled=frozenset({"gemini"}))
    assert restart_needed(snap, snap) is False


def test_restart_needed_both_change():
    before = SettingsSnapshot(tier="max", disabled=frozenset())
    after = SettingsSnapshot(tier="pro", disabled=frozenset({"codex", "gemini"}))
    assert restart_needed(before, after) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings_dialog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_takkub.settings_dialog'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_takkub/settings_dialog.py
"""Unified Settings dialog + its pure decision helpers.

Replaces the scattered status-bar config controls. The pure helpers
(`SettingsSnapshot`, `restart_needed`, `settings_button_label`) are kept
free of Qt-widget construction so they unit-test without a QApplication.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingsSnapshot:
    """The config that, when changed, requires a live Lead restart.

    `tier` is the plan tier ("max" | "pro"); `disabled` is the set of
    disabled provider names. Role-provider mapping and claude-auth are
    deliberately NOT here — they apply at the next teammate spawn and
    never need a Lead restart.
    """

    tier: str
    disabled: frozenset[str]


def restart_needed(before: SettingsSnapshot, after: SettingsSnapshot) -> bool:
    """True iff the plan tier or the disabled-provider set changed."""
    return before.tier != after.tier or before.disabled != after.disabled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings_dialog.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
rtk git add src/agent_takkub/settings_dialog.py tests/test_settings_dialog.py
rtk git commit -m "feat(settings): SettingsSnapshot + restart_needed pure helper"
```

---

## Task 2: Pure helper — `settings_button_label()`

**Files:**
- Modify: `src/agent_takkub/settings_dialog.py`
- Test: `tests/test_settings_dialog.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_settings_dialog.py`)

```python
from agent_takkub.settings_dialog import settings_button_label


def test_button_label_max_no_disabled():
    assert settings_button_label("max", False) == "⚙ Max"


def test_button_label_pro_no_disabled():
    assert settings_button_label("pro", False) == "⚙ Pro"


def test_button_label_max_with_disabled():
    assert settings_button_label("max", True) == "⚙ Max ⚠"


def test_button_label_pro_with_disabled():
    assert settings_button_label("pro", True) == "⚙ Pro ⚠"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings_dialog.py -k button_label -v`
Expected: FAIL — `ImportError: cannot import name 'settings_button_label'`

- [ ] **Step 3: Write minimal implementation** (append to `src/agent_takkub/settings_dialog.py`, after `restart_needed`)

```python
def settings_button_label(tier: str, any_provider_disabled: bool) -> str:
    """Status-bar ⚙ button text. Shows the plan inline so the user keeps
    the at-a-glance plan visibility the old plan chip gave, plus a ⚠ badge
    when any provider is disabled."""
    from .plan_tier import PRO

    base = "⚙ Pro" if tier == PRO else "⚙ Max"
    return f"{base} ⚠" if any_provider_disabled else base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings_dialog.py -k button_label -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
rtk git add src/agent_takkub/settings_dialog.py tests/test_settings_dialog.py
rtk git commit -m "feat(settings): settings_button_label pure helper"
```

---

## Task 3: `orchestrator.restart_leads()`

**Files:**
- Modify: `src/agent_takkub/orchestrator.py` (add method on `Orchestrator`, after `set_plan_tier`, ~line 2078)
- Test: `tests/test_restart_leads.py`

**Why a dedicated method (not `close()` + `spawn()`):** `close()` pops `self._session_uuids[key]` (orchestrator.py:1973), which destroys the `--resume` path → the restarted Lead would lose its conversation. `restart_leads()` instead keeps the UUID, stamps a fresh `_recent_exits` timestamp so `spawn()`'s resume-window check (orchestrator.py:1363-1368) passes, marks the exit expected so the auto-respawn watcher stays out of the way, and defers the spawn via `QTimer` so the `PtySession` is fully torn down first (mirrors `_auto_respawn`, orchestrator.py:1557).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_restart_leads.py
"""restart_leads() respawns every live Lead while preserving its resume
UUID, so the new model pin applies and the conversation survives.

Qt event loop isn't running in the test, so we monkeypatch QTimer.singleShot
to fire the deferred spawn synchronously, and replace spawn() with a recorder.
"""

from __future__ import annotations

import agent_takkub.orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, _exit_key


class _FakeSession:
    def __init__(self):
        self.is_alive = True
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.is_alive = False


class _FakePane:
    def __init__(self):
        self.session = _FakeSession()
        self.expected_exit = False

    def mark_expected_exit(self):
        self.expected_exit = True


def test_restart_leads_preserves_uuid_and_respawns(monkeypatch):
    # Fire the deferred spawn immediately instead of via the Qt loop.
    monkeypatch.setattr(
        orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, cb: cb())
    )

    orch = Orchestrator()
    pane = _FakePane()
    project = "demo"
    orch._panes_by_project = {project: {LEAD.name: pane}}
    key = _exit_key(project, LEAD.name)
    orch._session_uuids[key] = {"uuid": "abc-123", "cwd": "/work/demo"}

    spawn_calls: list[tuple] = []
    monkeypatch.setattr(
        orch, "spawn", lambda r, c, p: spawn_calls.append((r, c, p)) or (True, "ok")
    )

    n, projects = orch.restart_leads()

    assert n == 1
    assert projects == [project]
    assert pane.session.terminated is True
    assert pane.expected_exit is True
    # UUID preserved (NOT popped like close() does) so spawn() can --resume
    assert orch._session_uuids[key] == {"uuid": "abc-123", "cwd": "/work/demo"}
    # exit stamped so the resume-window check passes at spawn time
    assert orch._recent_exits[key]["cwd"] == "/work/demo"
    # deferred spawn ran with the preserved cwd
    assert spawn_calls == [(LEAD.name, "/work/demo", project)]


def test_restart_leads_skips_dead_lead(monkeypatch):
    monkeypatch.setattr(
        orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, cb: cb())
    )
    orch = Orchestrator()
    pane = _FakePane()
    pane.session.is_alive = False
    orch._panes_by_project = {"demo": {LEAD.name: pane}}

    n, projects = orch.restart_leads()

    assert n == 0
    assert projects == []
    assert pane.session.terminated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_restart_leads.py -v`
Expected: FAIL — `AttributeError: 'Orchestrator' object has no attribute 'restart_leads'`

- [ ] **Step 3: Write minimal implementation** (insert after `set_plan_tier`, before `done`, in orchestrator.py)

```python
    def restart_leads(self) -> tuple[int, list[str]]:
        """Restart the Lead pane in every project tab so a model-pin or
        provider change takes effect immediately.

        Preserves conversation context: unlike `close()` (which pops the
        cached session UUID), this keeps `_session_uuids[key]` and stamps a
        fresh `_recent_exits` timestamp so `spawn()`'s resume-window check
        passes and claude rejoins via `--resume <uuid>`. The exit is marked
        expected so the auto-respawn watcher stays out, and the respawn is
        deferred via QTimer so the PtySession tears down first (mirrors
        `_auto_respawn`).

        Returns (count, project_namespaces_restarted).
        """
        restarted: list[str] = []
        for project_ns, panes in list(self._panes_by_project.items()):
            lead = panes.get(LEAD.name)
            if not (lead and lead.session and lead.session.is_alive):
                continue
            key = _exit_key(project_ns, LEAD.name)
            prior = self._session_uuids.get(key)
            cwd = prior.get("cwd") if prior else None
            lead.mark_expected_exit()  # keep auto-respawn watcher out
            lead.session.terminate()
            # Stamp the exit so spawn()'s resume-window check passes the
            # moment the deferred respawn fires (idempotent with the
            # processExited callback that also records it).
            if cwd is not None:
                self._recent_exits[key] = {"cwd": cwd, "ts": time.time()}
            QTimer.singleShot(
                AUTO_RESPAWN_DELAY_MS,
                lambda r=LEAD.name, c=cwd, p=project_ns: self.spawn(r, c, p),
            )
            restarted.append(project_ns)
        _log_event("restart_leads", count=len(restarted), projects=restarted)
        return len(restarted), restarted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_restart_leads.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
rtk git add src/agent_takkub/orchestrator.py tests/test_restart_leads.py
rtk git commit -m "feat(orchestrator): restart_leads() respawns Lead panes preserving --resume"
```

---

## Task 4: Strip `[system]` broadcast from `set_plan_tier` + `toggle_provider`

**Files:**
- Modify: `src/agent_takkub/orchestrator.py` (`toggle_provider` ~lines 2007-2027, `set_plan_tier` ~lines 2053-2073)

No new test: the existing `tests/test_provider_toggle_orchestrator.py` asserts only persist + emit (never broadcast wording), so it must keep passing after removal. This is the verification.

- [ ] **Step 1: Confirm current tests pass (baseline)**

Run: `python -m pytest tests/test_provider_toggle_orchestrator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 2: Edit `toggle_provider` — remove the broadcast block**

Delete these lines (orchestrator.py ~2007-2027): the `word`/`suffix`/`notice` assignment AND the `for _project_ns, panes in self._panes_by_project.items(): ... lead.session.write(notice) ... self.leadInjected.emit(notice)` loop. The method body should go straight from `set_disabled(provider, disabled)` to:

```python
        set_disabled(provider, disabled)

        self.providerStateChanged.emit(provider, disabled)
        _log_event("provider_toggled", provider=provider, disabled=disabled)
        return True, f"{provider} {'disabled' if disabled else 'enabled'}"
```

(Note: the old `word.lower()` return is replaced with the explicit ternary since `word` is gone.)

- [ ] **Step 3: Edit `set_plan_tier` — remove the broadcast block**

Delete the `if tier == plan_tier.PRO: notice = ... else: notice = ...` assignment AND the broadcast `for` loop (orchestrator.py ~2053-2073). The method body should go from `plan_tier.set_current(tier)` straight to:

```python
        plan_tier.set_current(tier)

        self.planTierChanged.emit(tier)
        _log_event("plan_tier_set", tier=tier)
        return True, f"plan set to {tier}"
```

Also trim the now-stale docstring sentence "Already-running Lead panes keep their current model until respawn — we broadcast a `[system]` notice ..." → replace with "Already-running Lead panes are restarted separately by the caller via `restart_leads()`."

- [ ] **Step 4: Run tests to verify still passing**

Run: `python -m pytest tests/test_provider_toggle_orchestrator.py tests/test_plan_tier.py -v`
Expected: PASS (all)

- [ ] **Step 5: Verify `leadInjected` has no remaining callers tied to these methods**

Run: `python -m pytest tests/ -v`
Expected: PASS (full suite green — confirms nothing else depended on the broadcast)

- [ ] **Step 6: Commit**

```bash
rtk git add src/agent_takkub/orchestrator.py
rtk git commit -m "refactor(orchestrator): drop [system] broadcast from plan/provider setters"
```

---

## Task 5: `SettingsDialog` class

**Files:**
- Modify: `src/agent_takkub/settings_dialog.py`
- Test: `tests/test_settings_dialog.py`

The dialog reads `plan_tier`/`provider_state` for initial state and exposes `result_snapshot()` returning a `SettingsSnapshot` of the user's selections. It does NOT persist — `main_window` (Task 6) owns persist + restart. Role-provider and claude-auth sections are buttons that open the existing dialogs (which persist themselves on their own Save).

- [ ] **Step 1: Write the failing test** (append to `tests/test_settings_dialog.py`)

```python
import os

import pytest

# Offscreen so the dialog constructs without a display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def test_dialog_initial_state_reflects_current_config(qapp, tmp_path, monkeypatch):
    from agent_takkub import plan_tier, provider_state
    from agent_takkub.settings_dialog import SettingsDialog

    monkeypatch.setattr(plan_tier, "_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled.json")
    plan_tier.set_current("pro")
    provider_state.set_disabled("codex", True)

    dlg = SettingsDialog()
    snap = dlg.result_snapshot()
    assert snap.tier == "pro"
    assert snap.disabled == frozenset({"codex"})


def test_dialog_result_snapshot_reflects_user_edits(qapp, tmp_path, monkeypatch):
    from agent_takkub import plan_tier, provider_state
    from agent_takkub.settings_dialog import SettingsDialog

    monkeypatch.setattr(plan_tier, "_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled.json")
    plan_tier.set_current("max")

    dlg = SettingsDialog()
    dlg._radio_pro.setChecked(True)       # user flips to Pro
    dlg._chk_gemini.setChecked(False)     # user disables gemini (unchecked = disabled)

    snap = dlg.result_snapshot()
    assert snap.tier == "pro"
    assert "gemini" in snap.disabled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings_dialog.py -k dialog -v`
Expected: FAIL — `ImportError: cannot import name 'SettingsDialog'`

- [ ] **Step 3: Write the implementation** (append to `src/agent_takkub/settings_dialog.py`)

```python
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .provider_state import CODEX, GEMINI, is_disabled
from .plan_tier import MAX, PRO, current as _plan_current


class SettingsDialog(QDialog):
    """One dialog for every cockpit config. Reads current state on open;
    `result_snapshot()` returns the user's plan + provider selections. The
    role-provider and claude-auth sections open the existing dialogs (which
    persist themselves) — they never affect the snapshot or trigger a Lead
    restart."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Settings")
        self.setMinimumWidth(420)
        root = QVBoxLayout(self)

        # ── Account plan ──────────────────────────────────────
        root.addWidget(self._section_label("Account plan"))
        self._radio_max = QRadioButton("Max — full model access (incl. 1M context)")
        self._radio_pro = QRadioButton("Pro — standard context (1M unavailable)")
        self._plan_group = QButtonGroup(self)
        self._plan_group.addButton(self._radio_max)
        self._plan_group.addButton(self._radio_pro)
        (self._radio_pro if _plan_current() == PRO else self._radio_max).setChecked(True)
        root.addWidget(self._radio_max)
        root.addWidget(self._radio_pro)

        root.addWidget(self._divider())

        # ── Brains (2nd / 3rd opinion) ────────────────────────
        root.addWidget(self._section_label("Brains (2nd / 3rd opinion)"))
        self._chk_codex = QCheckBox("Codex")
        self._chk_gemini = QCheckBox("Gemini")
        self._chk_codex.setChecked(not is_disabled(CODEX))
        self._chk_gemini.setChecked(not is_disabled(GEMINI))
        brains = QHBoxLayout()
        brains.addWidget(self._chk_codex)
        brains.addWidget(self._chk_gemini)
        brains.addStretch(1)
        root.addLayout(brains)

        root.addWidget(self._divider())

        # ── Role → provider mapping (sub-dialog) ──────────────
        root.addWidget(self._section_label("Role → provider mapping"))
        btn_roles = QPushButton("Configure role providers…")
        btn_roles.clicked.connect(self._open_role_providers)
        root.addWidget(btn_roles)

        root.addWidget(self._divider())

        # ── Claude auth override (sub-dialog) ─────────────────
        root.addWidget(self._section_label("Claude auth override"))
        btn_auth = QPushButton("Configure Claude auth…")
        btn_auth.clicked.connect(self._open_claude_auth)
        root.addWidget(btn_auth)

        # ── Cancel / Save ─────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- helpers -------------------------------------------------
    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        return lbl

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _open_role_providers(self) -> None:
        from .provider_dialog import RoleProviderDialog

        RoleProviderDialog(self).exec()

    def _open_claude_auth(self) -> None:
        from .claude_auth_dialog import ClaudeAuthDialog

        ClaudeAuthDialog(self).exec()

    # -- result --------------------------------------------------
    def result_snapshot(self) -> "SettingsSnapshot":
        """The user's plan + provider selection as a SettingsSnapshot.
        Unchecked provider = disabled."""
        tier = PRO if self._radio_pro.isChecked() else MAX
        disabled = set()
        if not self._chk_codex.isChecked():
            disabled.add(CODEX)
        if not self._chk_gemini.isChecked():
            disabled.add(GEMINI)
        return SettingsSnapshot(tier=tier, disabled=frozenset(disabled))
```

> Before writing, confirm the import names against the real modules: `provider_state` must export `CODEX`, `GEMINI`, `is_disabled` (it does — used in main_window.py:378); `plan_tier` exports `MAX`, `PRO`, `current`. If `provider_dialog.RoleProviderDialog` / `claude_auth_dialog.ClaudeAuthDialog` constructors differ, match their existing call sites in main_window.py:961 / 973.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings_dialog.py -v`
Expected: PASS (all). If the offscreen `qapp` fixture cannot init in this environment, the two `dialog` tests will error on QApplication creation — in that case mark them `@pytest.mark.skipif` on `QApplication` init failure and rely on manual smoke (Task 6 Step 6). The 8 pure-helper tests must still pass.

- [ ] **Step 5: Commit**

```bash
rtk git add src/agent_takkub/settings_dialog.py tests/test_settings_dialog.py
rtk git commit -m "feat(settings): SettingsDialog with plan/providers + sub-dialog buttons"
```

---

## Task 6: Wire into `main_window` status bar

**Files:**
- Modify: `src/agent_takkub/main_window.py`

This task is UI wiring — verified by manual smoke (Step 6), since the repo has no Qt unit-test harness for the status bar (provider_dialog has none either). The decision logic it depends on (`restart_needed`, `settings_button_label`, `restart_leads`) is already unit-tested in Tasks 1-3.

- [ ] **Step 1: Remove the 3 chips + 2 buttons and their wiring**

Delete from `main_window.py`:
- chip construction: `_chip_codex` (~372-379), `_chip_gemini` (~381-388), `_chip_plan` (~398-401)
- buttons: `_btn_providers` (~535) and its `clicked.connect`, `_btn_claude_auth` (~549) and its `clicked.connect`
- the chips' entries in the `addPermanentWidget` group (~641-643: `self._chip_plan, self._chip_codex, self._chip_gemini`)
- the `_btn_providers` / `_btn_claude_auth` entries wherever they're added to the status bar
- handlers `_on_plan_chip_clicked` (~2112), `_on_provider_chip_clicked`, `_on_providers_clicked` (~952), `_on_claude_auth_clicked` (~969)
- now-unused static style/tooltip helpers used ONLY by the removed chips: `_provider_chip_style` (~156), `_plan_chip_style` (~184), `_plan_chip_tooltip` (~202)

Keep: signals `planTierChanged` / `providerStateChanged` connections (repurpose their slots in Step 3).

- [ ] **Step 2: Add the ⚙ Settings button** (in the status-bar build region, ~line 398 where `_chip_plan` was)

```python
        # Unified settings entry point — replaces the old plan/provider
        # chips + Providers/Claude-Auth buttons. Label shows the plan inline
        # (Pro/Max) with a ⚠ badge when any provider is disabled.
        self._btn_settings = QPushButton(self)
        self._btn_settings.setToolTip(
            "Cockpit settings: account plan, brains (codex/gemini),\n"
            "role→provider mapping, Claude auth. Changing the plan or a\n"
            "provider restarts the Lead pane(s) (context preserved via --resume)."
        )
        self._btn_settings.clicked.connect(self._on_settings_clicked)
        self._refresh_settings_button()
```

Add `self._btn_settings` to the same `addPermanentWidget` group the chips used.

- [ ] **Step 3: Add `_refresh_settings_button` + repoint the signal slots**

```python
    def _refresh_settings_button(self) -> None:
        """Repaint the ⚙ button label/colour from current plan + provider state."""
        from .plan_tier import PRO, current as plan_current
        from .provider_state import CODEX, GEMINI, is_disabled
        from .settings_dialog import settings_button_label

        tier = plan_current()
        any_disabled = is_disabled(CODEX) or is_disabled(GEMINI)
        self._btn_settings.setText(settings_button_label(tier, any_disabled))
        # Warn colour under Pro (reuse the old plan-chip palette intent).
        self._btn_settings.setStyleSheet(
            "QPushButton { color: #d18616; font-weight: bold; }"
            if tier == PRO
            else "QPushButton { font-weight: bold; }"
        )
```

Repoint the existing slots `_on_plan_tier_changed` and `_on_provider_state_changed` to call `self._refresh_settings_button()` (replace their old chip-repaint bodies). Keep any other side effects those slots had unrelated to the chips.

- [ ] **Step 4: Add the save→confirm→restart flow**

```python
    def _on_settings_clicked(self) -> None:
        """Open the unified settings dialog. On Save, if the plan or a
        provider changed, confirm + restart every Lead pane (context kept
        via --resume) so the new model pin applies deterministically.
        Role-provider / claude-auth edits persist via their own sub-dialogs
        and never trigger a restart."""
        from PyQt6.QtWidgets import QMessageBox

        from .plan_tier import PRO, current as plan_current
        from .provider_state import CODEX, GEMINI, is_disabled
        from .settings_dialog import SettingsDialog, SettingsSnapshot, restart_needed

        def _snapshot() -> SettingsSnapshot:
            disabled = frozenset(
                p for p in (CODEX, GEMINI) if is_disabled(p)
            )
            return SettingsSnapshot(tier=plan_current(), disabled=disabled)

        before = _snapshot()
        dlg = SettingsDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        after = dlg.result_snapshot()

        if not restart_needed(before, after):
            self._status.showMessage("Settings saved.", 4_000)
            self._refresh_settings_button()
            return

        confirm = QMessageBox.question(
            self,
            "Restart Lead?",
            "เปลี่ยน plan / provider แล้ว — จะ restart Lead ของทุก tab\n"
            "(context เดิมถูกเก็บไว้ผ่าน --resume) เพื่อให้มีผลทันที\n\nตกลงไหม?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            # Revert: do NOT persist plan/provider so file + running Lead
            # stay consistent (avoids the exact Pro-1M mismatch this feature
            # exists to prevent).
            self._status.showMessage("Settings change cancelled.", 4_000)
            return

        # Persist the plan/provider deltas via the orchestrator.
        if after.tier != before.tier:
            self.orch.set_plan_tier(after.tier)
        if after.disabled != before.disabled:
            for p in (CODEX, GEMINI):
                want_disabled = p in after.disabled
                if want_disabled != (p in before.disabled):
                    self.orch.toggle_provider(p, want_disabled)

        n, projects = self.orch.restart_leads()
        self._refresh_settings_button()
        self._status.showMessage(f"Settings saved — restarted Lead in {n} tab(s).", 6_000)
```

Add `from PyQt6.QtWidgets import QDialog` to the imports if not present (check top of main_window.py first).

- [ ] **Step 5: Run the full test suite + import check**

Run: `python -m pytest tests/ -v`
Expected: PASS (full suite green)
Run: `python -c "import agent_takkub.main_window"`
Expected: no ImportError / NameError (catches a leftover reference to a removed chip/handler/helper)

- [ ] **Step 6: Manual smoke (record result)**

Launch the cockpit. Verify:
1. Status bar shows a single `⚙ Max` (or `⚙ Pro`) button — no plan/codex/gemini chips, no 🤖 Providers / Claude Auth buttons.
2. Click ⚙ → dialog shows 4 sections; plan radio + provider checkboxes match current state.
3. "Configure role providers…" / "Configure Claude auth…" open the existing dialogs and save independently.
4. Flip plan Max→Pro, Save → confirm popup → OK → Lead pane(s) restart and keep their conversation (scrollback intact). Button repaints to `⚙ Pro` (warn colour).
5. Disable a provider, Save → confirm → button shows `⚠` badge.
6. Make a plan change, Save, then Cancel the confirm → plan unchanged, button unchanged.

- [ ] **Step 7: Commit**

```bash
rtk git add src/agent_takkub/main_window.py
rtk git commit -m "feat(ui): single ⚙ Settings button replaces plan/provider chips + dialogs"
```

---

## Task 7: Update `CLAUDE.md` docs

**Files:**
- Modify: `CLAUDE.md` (sections "Account plan toggle (Pro / Max)" and "Disabled providers (cockpit toggle)")

- [ ] **Step 1: Update the two sections**

In "Account plan toggle" and "Disabled providers": replace the "Status bar chip … click to toggle" + "orchestrator … runtime `[system]` … when toggle" wording with: the toggle now lives in the ⚙ Settings dialog; changing the plan or a provider restarts the Lead pane(s) (context preserved via `--resume`) instead of broadcasting a `[system]` notice. Keep the behavioral rules (under Pro: don't propose 1M; provider disabled: don't route to it) — those are unchanged; only the delivery mechanism (restart vs broadcast) changed.

- [ ] **Step 2: Verify no stale broadcast references remain**

Run: `python -m pytest tests/test_routing_planner.py -v`
Expected: PASS (routing rules unaffected — provider-disabled handling unchanged)

Grep check: `rg -n "broadcast|\[system\] account plan|provider .*ENABLED" CLAUDE.md` should return nothing referring to the removed mechanism.

- [ ] **Step 3: Commit**

```bash
rtk git add CLAUDE.md
rtk git commit -m "docs: settings dialog + Lead restart replace status-bar toggle broadcast"
```

---

## Self-Review notes

- **Spec coverage:** §3.1 SettingsDialog → Task 5; §3.2 status bar → Task 6; §3.3 orchestrator (broadcast removal + restart_leads) → Tasks 3-4; §3.4 save flow → Task 6; §5 error handling → restart_leads skips dead Leads + per-tab spawn failures are non-fatal (Task 3 + Task 6); §6 testing → Tasks 1-3,5; §7 files → all tasks; CLAUDE.md → Task 7. ✅ no gaps.
- **Type consistency:** `SettingsSnapshot(tier, disabled)`, `restart_needed(before, after)`, `settings_button_label(tier, any_provider_disabled)`, `restart_leads() -> (int, list[str])`, `SettingsDialog.result_snapshot()` — names identical across Tasks 1-6. ✅
- **Resume mechanism:** verified against orchestrator.py:1361-1375 (spawn resume check), :1529 (`_on_session_exit` stamps `_recent_exits`, leaves `_session_uuids`), :1973 (`close()` pops `_session_uuids` — the reason restart_leads avoids it).
