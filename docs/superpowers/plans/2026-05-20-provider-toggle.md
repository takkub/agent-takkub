# Provider Toggle (codex / gemini) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global UI toggle in the cockpit status bar that enables/disables codex and gemini providers. While disabled, Lead must not propose them in routing tables or cross-checks. State persists across cockpit restart.

**Architecture:** Single source of truth is `~/.takkub/disabled-providers.json` Orchestrator owns the state and broadcasts via (1) `--append-system-prompt-file` snippet at Lead spawn time, and (2) `[system] <provider> DISABLED/ENABLED` message injected into every Lead pane on toggle change Same injection mechanism as `[<role> done]` Soft block (routing-only) — no hard CLI gate

**Tech Stack:** Python 3.11+, PyQt6, pytest

**Spec:** `docs/superpowers/specs/2026-05-20-provider-toggle-design.md`

---

## File Structure

### New files
- `src/agent_takkub/provider_state.py` — load/save/query disabled providers (per-provider gate, separate from per-role provider_config.py)
- `tests/test_provider_state.py` — unit tests for the state module

### Modified files
- `src/agent_takkub/routing_planner.py` — `classify()` honors `context["disabled_providers"]`
- `tests/test_routing_planner.py` — 5 new test cases for disabled filtering
- `src/agent_takkub/orchestrator.py` — `toggle_provider()` method, broadcast on change, append snippet in `_render_lead_context()`
- `src/agent_takkub/main_window.py` — 2 status bar chip buttons + wiring
- `CLAUDE.md` — add "Disabled providers" section

---

## Task 1: provider_state module (TDD)

**Files:**
- Create: `src/agent_takkub/provider_state.py`
- Test: `tests/test_provider_state.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_state.py`:

```python
"""Unit tests for provider_state — per-provider enable/disable state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import provider_state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    """Redirect provider_state to a tmp file so tests don't touch the real config."""
    path = tmp_path / "disabled-providers.json"
    monkeypatch.setattr(provider_state, "_PATH", path)
    return path


def test_load_missing_file_returns_empty(tmp_state_path):
    assert provider_state.load() == {}


def test_save_then_load_roundtrip(tmp_state_path):
    provider_state.save({"codex": True, "gemini": False})
    assert provider_state.load() == {"codex": True, "gemini": False}


def test_save_writes_atomically_via_tmp_file(tmp_state_path):
    provider_state.save({"codex": True})
    # final file exists
    assert tmp_state_path.exists()
    # no leftover .tmp file
    assert not tmp_state_path.with_suffix(tmp_state_path.suffix + ".tmp").exists()


def test_corrupt_json_returns_empty_without_crash(tmp_state_path):
    tmp_state_path.write_text("{not valid json", encoding="utf-8")
    assert provider_state.load() == {}


def test_set_disabled_then_is_disabled(tmp_state_path):
    assert provider_state.is_disabled("codex") is False
    provider_state.set_disabled("codex", True)
    assert provider_state.is_disabled("codex") is True
    provider_state.set_disabled("codex", False)
    assert provider_state.is_disabled("codex") is False


def test_unknown_provider_dropped_on_save(tmp_state_path):
    provider_state.save({"codex": True, "bogus": True})
    assert provider_state.load() == {"codex": True}


def test_all_disabled_returns_set_of_truthy_providers(tmp_state_path):
    provider_state.save({"codex": True, "gemini": False})
    assert provider_state.all_disabled() == {"codex"}
    provider_state.save({"codex": True, "gemini": True})
    assert provider_state.all_disabled() == {"codex", "gemini"}


def test_set_disabled_unknown_provider_raises(tmp_state_path):
    with pytest.raises(ValueError):
        provider_state.set_disabled("bogus", True)


def test_default_path_is_under_home_takkub(monkeypatch, tmp_path):
    # Sanity check: the real default points at ~/.takkub/disabled-providers.json
    # (use the module-level _PATH before any monkeypatch from other fixtures)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import importlib
    import agent_takkub.provider_state as ps_module
    importlib.reload(ps_module)
    assert ps_module._PATH == tmp_path / ".takkub" / "disabled-providers.json"
    # Reload back so other tests aren't affected
    importlib.reload(ps_module)
```

- [ ] **Step 2: Run test to verify they fail**

Run: `pytest tests/test_provider_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_takkub.provider_state'`

- [ ] **Step 3: Write the implementation**

Create `src/agent_takkub/provider_state.py`:

```python
"""Per-provider enable/disable state.

`provider_config.py` answers "which CLI backs role X" (per-role mapping).
This module answers "is provider Y currently usable" (per-provider gate) —
a different boundary, persisted in a different file, surfaced through a
different UI flow (status bar toggle, not config edit). Keep them apart.

State file: `~/.takkub/disabled-providers.json`
Format: `{"codex": true, "gemini": false}` — provider name → disabled flag
Missing file or corrupt JSON → treated as empty mapping (all enabled).

Persists across cockpit restart by design: user-level intent, not
session-scoped (see spec 2026-05-20-provider-toggle-design.md).
"""

from __future__ import annotations

import json
from pathlib import Path

CODEX = "codex"
GEMINI = "gemini"

# Providers that can be toggled. Adding a new togglable provider:
# (1) add to this frozenset, (2) add a chip in main_window status bar,
# (3) update routing_planner if it has provider-specific routing rules.
TOGGLABLE: frozenset[str] = frozenset({CODEX, GEMINI})

_PATH = Path.home() / ".takkub" / "disabled-providers.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def load() -> dict[str, bool]:
    """Return current state mapping. Missing file or corrupt JSON → empty dict.

    Always returns a fresh dict — callers can mutate without side effects.
    """
    if not _PATH.exists():
        return {}
    try:
        raw = _PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Sanitize: drop entries with providers not in TOGGLABLE so a stale
    # entry from a previous build doesn't silently survive.
    return {
        str(k): bool(v) for k, v in data.items() if str(k) in TOGGLABLE
    }


def save(state: dict[str, bool]) -> None:
    """Persist `state` atomically. Drops keys not in TOGGLABLE."""
    cleaned = {
        str(k): bool(v) for k, v in state.items() if str(k) in TOGGLABLE
    }
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)


def is_disabled(provider: str) -> bool:
    """True iff `provider` is currently disabled. Unknown providers → False."""
    return bool(load().get(provider, False))


def set_disabled(provider: str, flag: bool) -> None:
    """Flip `provider` to disabled (True) or enabled (False).

    Raises ValueError if `provider` is not in TOGGLABLE (catches typos
    at the call site rather than silently no-op'ing).
    """
    if provider not in TOGGLABLE:
        raise ValueError(f"unknown provider: {provider!r}")
    state = load()
    state[provider] = bool(flag)
    save(state)


def all_disabled() -> set[str]:
    """Return the set of provider names currently disabled."""
    return {k for k, v in load().items() if v}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_provider_state.py -v`
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/provider_state.py tests/test_provider_state.py
git commit -m "feat(provider-toggle): add provider_state module

Per-provider enable/disable state persisted to
~/.takkub/disabled-providers.json. Separate from provider_config.py
which handles per-role mapping (different boundary, different file,
different UI flow).

Covers: load/save roundtrip, atomic write, corrupt JSON recovery,
unknown provider rejection on set, default path resolution.
"
```

---

## Task 2: routing_planner honors disabled providers

**Files:**
- Modify: `src/agent_takkub/routing_planner.py:299-361` (classify function)
- Test: `tests/test_routing_planner.py` (add new test class at end)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_routing_planner.py` (add at end of file, before any trailing main guard):

```python
class TestDisabledProviders:
    """classify() respects context['disabled_providers'] — drops codex/gemini
    from cross_check and degrades FIRE_ONESHOT to ASK_CLARIFY."""

    def test_disabled_codex_dropped_from_cross_check(self):
        """Refactor message normally proposes backend + codex cross-check.
        With codex disabled, cross_check should be empty (or None)."""
        action = classify(
            "refactor the auth module to use the new session helper",
            context={"disabled_providers": {"codex"}},
        )
        assert action.kind == ActionKind.PROPOSE
        # codex was the only cross-check entry for refactor — should be gone
        assert not action.cross_check  # None or empty list

    def test_disabled_gemini_blocks_rollout_proposal(self):
        """Rollout/strategy normally routes to gemini as primary.
        With gemini disabled, classifier should ASK_CLARIFY (no fallback)."""
        action = classify(
            "rollout plan for deploying the auth changes safely",
            context={"disabled_providers": {"gemini"}},
        )
        assert action.kind == ActionKind.ASK_CLARIFY
        assert "gemini" in action.reason.lower()

    def test_both_disabled_no_codex_no_gemini_in_output(self):
        """Refactor with both disabled: cross_check empty (codex gone).
        Primary still routes to backend (refactor's content-derived role)."""
        action = classify(
            "refactor backend to extract auth service",
            context={"disabled_providers": {"codex", "gemini"}},
        )
        assert action.kind == ActionKind.PROPOSE
        assert action.role == "backend"
        assert not action.cross_check

    def test_oneshot_codex_disabled_becomes_ask_clarify(self):
        """FIRE_ONESHOT to a disabled provider → ASK_CLARIFY with explanation."""
        action = classify(
            "ขอ codex review function นี้",
            context={"disabled_providers": {"codex"}},
        )
        assert action.kind == ActionKind.ASK_CLARIFY
        assert "codex" in action.reason.lower()

    def test_none_disabled_is_backward_compat(self):
        """Default behavior (no context, or empty disabled set) unchanged."""
        action_none = classify("refactor the auth module")
        action_empty = classify(
            "refactor the auth module",
            context={"disabled_providers": set()},
        )
        assert action_none.kind == action_empty.kind == ActionKind.PROPOSE
        assert action_none.cross_check == action_empty.cross_check == ["codex"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routing_planner.py::TestDisabledProviders -v`
Expected: 5 tests FAIL (classify doesn't honor disabled_providers yet)

- [ ] **Step 3: Modify classify() to filter**

Edit `src/agent_takkub/routing_planner.py`, replacing the `classify()` function (currently lines 299-361):

```python
def classify(user_message: str, context: dict | None = None) -> RoutingAction:
    """Classify a user message and return the routing action Lead should take.

    Args:
        user_message: Raw message from the user.
        context: Optional state dict. Keys:
            ``pending_proposal`` (bool) — True when Lead has shown a plan
            table and is waiting for the user to confirm/abort/edit it.
            ``disabled_providers`` (set[str]) — provider names that user has
            disabled via the cockpit status bar toggle. codex/gemini in this
            set get dropped from cross_check; FIRE_ONESHOT and gemini-primary
            routes degrade to ASK_CLARIFY.

    Returns:
        RoutingAction with kind, role(s), cross_check, reason, mixed.
    """
    msg = user_message.strip()
    disabled: set[str] = set((context or {}).get("disabled_providers") or set())

    # 1. Explicit role ("ให้ backend ทำ X") → FIRE_ASSIGN immediately
    explicit = _detect_explicit_role(msg)
    if explicit:
        if explicit in disabled:
            return RoutingAction(
                kind=ActionKind.ASK_CLARIFY,
                reason=f"{explicit} provider is disabled — ask user to enable first",
            )
        return RoutingAction(
            kind=ActionKind.FIRE_ASSIGN,
            role=explicit,
            task_hint=msg,
            reason="explicit role specified by user",
        )

    # 2. One-shot codex/gemini → FIRE_ONESHOT (no pane spawn)
    oneshot = _detect_oneshot(msg)
    if oneshot:
        if oneshot in disabled:
            return RoutingAction(
                kind=ActionKind.ASK_CLARIFY,
                reason=f"{oneshot} provider is disabled — ask user to enable first",
            )
        return RoutingAction(
            kind=ActionKind.FIRE_ONESHOT,
            role=oneshot,
            task_hint=msg,
            reason="one-shot query to AI peer (no pane)",
        )

    # 3. Pending proposal → handle confirm/abort/edit phrases
    if context and context.get("pending_proposal"):
        result = _handle_confirm(msg.lower())
        if result:
            result.task_hint = msg
            return result

    # 4. Classify intent
    is_act = _is_actionable(msg)
    is_info = _is_informational(msg)
    is_mixed = is_act and is_info

    if is_info and not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="informational query")

    if not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="no actionable verb detected")

    # 5. Route actionable message to role(s)
    routing = _route(msg)
    primary = routing.get("role")
    cross_check = routing.get("cross_check")

    # Degrade if the *primary* role itself is disabled (e.g. rollout→gemini
    # when gemini is off): there's no automatic fallback role, so surface
    # the conflict to the user rather than silently picking something else.
    if primary in disabled:
        return RoutingAction(
            kind=ActionKind.ASK_CLARIFY,
            reason=f"{primary} provider is disabled — ask user to enable first",
        )

    # Filter cross_check: drop any disabled providers. None stays None;
    # empty list collapses to None for backward-compat with existing tests.
    if cross_check:
        cross_check = [r for r in cross_check if r not in disabled] or None

    return RoutingAction(
        kind=ActionKind.PROPOSE,
        role=primary,
        roles=routing.get("roles"),
        task_hint=msg,
        cross_check=cross_check,
        reason=routing.get("reason", ""),
        mixed=is_mixed,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routing_planner.py -v`
Expected: all tests PASS (new TestDisabledProviders + every existing test)

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/routing_planner.py tests/test_routing_planner.py
git commit -m "feat(provider-toggle): routing_planner honors disabled_providers

classify() now reads context['disabled_providers'] (set[str]) and:
- drops disabled providers from cross_check
- degrades FIRE_ONESHOT to ASK_CLARIFY for disabled provider
- degrades primary=gemini routes (rollout/strategy) to ASK_CLARIFY when gemini disabled
- degrades explicit-role assigns to ASK_CLARIFY when target disabled

Backward-compatible: context=None or missing disabled_providers behaves
exactly as before.
"
```

---

## Task 3: Orchestrator — append disabled section to Lead system prompt

**Files:**
- Modify: `src/agent_takkub/orchestrator.py:483-544` (`_render_lead_context` function)

- [ ] **Step 1: Read current `_render_lead_context()` to confirm location**

Run: `grep -n "_render_lead_context" src/agent_takkub/orchestrator.py`
Expected: function defined around line 483, called around line 966

- [ ] **Step 2: Add disabled-providers section to the rendered suffix**

In `src/agent_takkub/orchestrator.py`, modify `_render_lead_context()` (around line 483). Add a new section after the BLOCKED_DIRS suffix. Find this block in the function:

```python
    suffix = f"""

---

## 🚫 BLOCKED_DIRS (auto-injected at spawn)

{header}
```

…and after the entire `suffix = f"""..."""` block (right before `ensure_runtime()`), add:

```python
    # Append disabled-providers section (only if any are disabled — saves tokens
    # when everything is enabled, which is the common case). Lead reads this on
    # spawn and treats codex/gemini in the list as forbidden in proposals.
    from .provider_state import all_disabled as _all_disabled

    disabled = _all_disabled()
    if disabled:
        disabled_list = ", ".join(sorted(disabled))
        suffix += f"""

---

## ⛔ Disabled providers (cockpit toggle)

ขณะนี้ provider ต่อไปนี้ถูกปิดโดย user: **{disabled_list}**

**ห้าม** propose role เหล่านี้ใน routing table หรือ cross-check
**ห้าม** fire `takkub assign --role <disabled>` หรือ `takkub <disabled>`
ถ้า user ขอตรงๆ → ตอบว่า provider นั้นถูกปิดอยู่ ให้ user enable ก่อน

Status เปลี่ยนระหว่าง session: cockpit จะ inject `[system] <provider> ENABLED/DISABLED` message
"""
```

- [ ] **Step 3: Verify import works (no circular import)**

Run: `python -c "from agent_takkub.orchestrator import _render_lead_context; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Smoke test the rendered file**

```bash
python -c "
from agent_takkub import provider_state
from agent_takkub.orchestrator import _render_lead_context

# enable both → snippet should NOT appear
provider_state.save({})
out = _render_lead_context('agent-takkub')
text = open(out, encoding='utf-8').read()
assert '⛔ Disabled providers' not in text, 'should be absent when no disables'

# disable codex → snippet should appear
provider_state.save({'codex': True})
out = _render_lead_context('agent-takkub')
text = open(out, encoding='utf-8').read()
assert '⛔ Disabled providers' in text
assert 'codex' in text
print('snippet injected correctly')

# clean up
provider_state.save({})
print('ok')
"
```
Expected: `snippet injected correctly` then `ok`

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/orchestrator.py
git commit -m "feat(provider-toggle): inject disabled-providers section into Lead prompt

_render_lead_context() now appends a section listing currently disabled
providers (read from provider_state.all_disabled()) so Lead knows on
spawn which providers to skip in routing proposals.

Section omitted entirely when nothing is disabled — saves prompt tokens
in the common case.
"
```

---

## Task 4: Orchestrator — `toggle_provider()` method + runtime broadcast

**Files:**
- Modify: `src/agent_takkub/orchestrator.py` (class Orchestrator)

- [ ] **Step 1: Add Qt signal declaration**

In `src/agent_takkub/orchestrator.py`, find the `Orchestrator` class signal block (around line 642). The current state has:

```python
    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    paneResumed = pyqtSignal(str, str)
    paneRequested = pyqtSignal(
```

Add a new signal immediately after `leadInjected`:

```python
    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    # Emitted when user toggles a provider on/off via status bar. main_window
    # listens to refresh chip color/label without polling.
    providerStateChanged = pyqtSignal(str, bool)  # (provider, disabled)
    paneResumed = pyqtSignal(str, str)
```

- [ ] **Step 2: Add `toggle_provider()` method**

Add to the `Orchestrator` class. Find a good spot near other public coordination methods (e.g. right after `send()` around line 1486, or at end of class before any final helpers). Insert:

```python
    def toggle_provider(self, provider: str, disabled: bool) -> tuple[bool, str]:
        """Flip codex or gemini between enabled/disabled globally across all tabs.

        Persists to ~/.takkub/disabled-providers.json then broadcasts a
        `[system] <provider> ENABLED/DISABLED ...` message into every Lead
        pane in every project so live sessions notice the change without
        having to poll the file.

        Returns (ok, message). Currently only fails on unknown provider.
        """
        from .provider_state import TOGGLABLE, set_disabled

        provider = provider.lower().strip()
        if provider not in TOGGLABLE:
            return False, f"unknown provider: {provider!r}"

        set_disabled(provider, disabled)

        word = "DISABLED" if disabled else "ENABLED"
        suffix = (
            "Do not propose this in routing or cross-check."
            if disabled
            else "Available again."
        )
        notice = f"[system] {provider} provider {word}. {suffix}"

        # Broadcast to every Lead pane across all project tabs. Iterate
        # _panes_by_project directly because we want every Lead, not just
        # the active project's Lead.
        for project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                lead.session.write(notice)
                # Same trailing-CR delay as done() so the inject lands
                # after the inline text not before it.
                QTimer.singleShot(150, lambda l=lead: l.session and l.session.write(b"\r"))
                self.leadInjected.emit(notice)
            # If Lead isn't alive in this project, the next spawn's
            # _render_lead_context() will read the fresh state — no need
            # to queue per-message for this case (unlike done notices,
            # which carry per-event info that mustn't be lost).

        self.providerStateChanged.emit(provider, disabled)
        _log_event("provider_toggled", provider=provider, disabled=disabled)
        return True, f"{provider} {word.lower()}"
```

- [ ] **Step 3: Add a smoke test for the broadcast plumbing**

Create `tests/test_provider_toggle_orchestrator.py`:

```python
"""Integration smoke test: toggle_provider() writes state + emits signal.

Verifies the orchestrator method without spawning a full Lead pane —
the broadcast-into-pane path is exercised only when a live pane exists,
which is covered by manual smoke testing in spec section 8.2.
"""
from __future__ import annotations

import pytest

from agent_takkub import provider_state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    path = tmp_path / "disabled-providers.json"
    monkeypatch.setattr(provider_state, "_PATH", path)
    return path


def test_toggle_provider_persists_and_emits(tmp_state_path, qtbot):
    """toggle_provider() writes to disk and emits providerStateChanged."""
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    received: list[tuple[str, bool]] = []
    orch.providerStateChanged.connect(lambda p, d: received.append((p, d)))

    ok, msg = orch.toggle_provider("codex", True)
    assert ok
    assert "disabled" in msg.lower()
    assert provider_state.is_disabled("codex") is True
    assert received == [("codex", True)]

    ok, msg = orch.toggle_provider("codex", False)
    assert ok
    assert provider_state.is_disabled("codex") is False
    assert received == [("codex", True), ("codex", False)]


def test_toggle_provider_rejects_unknown(tmp_state_path):
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    ok, msg = orch.toggle_provider("bogus", True)
    assert not ok
    assert "unknown provider" in msg
```

Note: if `qtbot` fixture isn't already used in this repo, this test may need adjustment. Run `pytest tests/test_provider_toggle_orchestrator.py -v` first — if `qtbot` is missing, replace it with a plain `QObject.connect` test using a Python `list` and remove the fixture parameter.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_provider_toggle_orchestrator.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Verify no existing tests broke**

Run: `pytest tests/ -x -q`
Expected: full suite PASS

- [ ] **Step 6: Commit**

```bash
git add src/agent_takkub/orchestrator.py tests/test_provider_toggle_orchestrator.py
git commit -m "feat(provider-toggle): orchestrator toggle_provider() + broadcast

New method writes state via provider_state.set_disabled() and broadcasts
'[system] <provider> ENABLED/DISABLED' into every Lead pane across all
project tabs (multi-tab safe). Emits providerStateChanged Qt signal so
the status bar UI can refresh chip color/label.

Lead panes that aren't alive at toggle time pick up the new state on
their next spawn via _render_lead_context() (already wired in previous
commit) — no per-message queue needed.
"
```

---

## Task 5: Status bar chips in main_window

**Files:**
- Modify: `src/agent_takkub/main_window.py` (around line 275 for chip creation, around line 428 for status bar add)

- [ ] **Step 1: Add helper for chip styling**

In `src/agent_takkub/main_window.py`, add a helper near the top of `MainWindow.__init__()` body where other UI styling helpers live. Insert this as a method of `MainWindow`:

```python
    @staticmethod
    def _provider_chip_style(provider: str, disabled: bool) -> str:
        """QPushButton stylesheet for the codex/gemini status-bar chips.

        Enabled: bright provider-brand color + white text.
        Disabled: dim gray + strikethrough so the off state is unambiguous.
        """
        if disabled:
            return (
                "QPushButton { "
                "background:#3f3f46; color:#71717a; "
                "border:1px solid #52525b; border-radius:10px; "
                "padding:2px 10px; font-weight:500; "
                "text-decoration: line-through; "
                "}"
                "QPushButton:hover { background:#52525b; color:#a1a1aa; }"
            )
        # Brand colors: codex teal (#10a37f) / gemini blue (#4285f4)
        brand = "#10a37f" if provider == "codex" else "#4285f4"
        return (
            "QPushButton { "
            f"background:{brand}; color:white; "
            "border:none; border-radius:10px; "
            "padding:2px 10px; font-weight:600; "
            "}"
            f"QPushButton:hover {{ background:{brand}; opacity:0.85; }}"
        )
```

- [ ] **Step 2: Create the chip buttons + wire click handlers**

In the `__init__` method, find the existing `self._btn_install_rtk = QPushButton(...)` block (around line 275). Right *after* that block (before `addPermanentWidget` at line 428 — note these are separated; insertion goes near where the button is *created*), add:

```python
        # ── provider toggle chips (codex / gemini) ─────────────────
        # State source-of-truth lives in provider_state.json; orchestrator
        # owns the broadcast on toggle. We just create the buttons and
        # subscribe to providerStateChanged to redraw.
        from .provider_state import CODEX, GEMINI, is_disabled

        self._chip_codex = QPushButton("Codex", self)
        self._chip_codex.setToolTip("Codex provider — click to toggle")
        self._chip_codex.setStyleSheet(self._provider_chip_style(CODEX, is_disabled(CODEX)))
        self._chip_codex.clicked.connect(
            lambda: self._on_provider_chip_clicked(CODEX)
        )

        self._chip_gemini = QPushButton("Gemini", self)
        self._chip_gemini.setToolTip("Gemini provider — click to toggle")
        self._chip_gemini.setStyleSheet(self._provider_chip_style(GEMINI, is_disabled(GEMINI)))
        self._chip_gemini.clicked.connect(
            lambda: self._on_provider_chip_clicked(GEMINI)
        )
```

- [ ] **Step 3: Add the chips to the status bar layout**

Find the existing `self._status.addPermanentWidget(self._btn_install_rtk)` call (around line 428). Add right after it:

```python
        self._status.addPermanentWidget(self._btn_install_rtk)
        self._status.addPermanentWidget(self._chip_codex)
        self._status.addPermanentWidget(self._chip_gemini)
```

- [ ] **Step 4: Wire click handler + signal listener**

Add two methods to the `MainWindow` class (place near other `_on_*_clicked` handlers):

```python
    def _on_provider_chip_clicked(self, provider: str) -> None:
        """Toggle a provider on the orchestrator. Orchestrator persists state,
        broadcasts to all Lead panes, and emits providerStateChanged → we
        update the chip style via _on_provider_state_changed."""
        from .provider_state import is_disabled

        currently_disabled = is_disabled(provider)
        # Flip
        ok, msg = self._orchestrator.toggle_provider(provider, not currently_disabled)
        if not ok:
            self._status.showMessage(f"Toggle failed: {msg}", 4000)

    def _on_provider_state_changed(self, provider: str, disabled: bool) -> None:
        """Repaint the affected chip when provider state flips. Triggered by
        Orchestrator.providerStateChanged so both user click and future
        config-file changes from other sources land here."""
        if provider == "codex" and hasattr(self, "_chip_codex"):
            self._chip_codex.setStyleSheet(self._provider_chip_style("codex", disabled))
            self._chip_codex.setToolTip(
                "Codex: disabled — click to enable" if disabled else "Codex: enabled — click to disable"
            )
        elif provider == "gemini" and hasattr(self, "_chip_gemini"):
            self._chip_gemini.setStyleSheet(self._provider_chip_style("gemini", disabled))
            self._chip_gemini.setToolTip(
                "Gemini: disabled — click to enable" if disabled else "Gemini: enabled — click to disable"
            )
```

- [ ] **Step 5: Connect the signal in `__init__`**

Find where `MainWindow.__init__` connects other orchestrator signals (search for `self._orchestrator.statusChanged.connect`). After that connection, add:

```python
        self._orchestrator.providerStateChanged.connect(self._on_provider_state_changed)
```

- [ ] **Step 6: Set initial tooltips correctly**

The Step 2 chip creation used a static tooltip. To make initial tooltips reflect actual state, replace the two `setToolTip(...)` calls in Step 2 with calls that match the format used in `_on_provider_state_changed`:

```python
        self._chip_codex.setToolTip(
            "Codex: disabled — click to enable" if is_disabled(CODEX) else "Codex: enabled — click to disable"
        )
        # (same shape for gemini)
        self._chip_gemini.setToolTip(
            "Gemini: disabled — click to enable" if is_disabled(GEMINI) else "Gemini: enabled — click to disable"
        )
```

- [ ] **Step 7: Manual smoke test**

```bash
python -m agent_takkub
```

Verify:
1. Status bar shows two new chips: **Codex** (teal) and **Gemini** (blue)
2. Click Codex chip → it dims to gray + strikethrough
3. Tooltip text updates ("Codex: disabled — click to enable")
4. Check the file: `cat ~/.takkub/disabled-providers.json` → `{"codex": true}`
5. Click Codex again → returns to teal, file becomes `{"codex": false}` or no codex key
6. Close cockpit + reopen → chips reflect the saved state
7. Lead pane should receive `[system] codex DISABLED ...` when chip flipped (if Lead was running)

- [ ] **Step 8: Commit**

```bash
git add src/agent_takkub/main_window.py
git commit -m "feat(provider-toggle): status bar chips for codex/gemini

Two QPushButton chips in the status bar (next to RTK install button).
Click flips state via orchestrator.toggle_provider(); style updates on
providerStateChanged signal. Brand colors for enabled, dim+strikethrough
for disabled, with descriptive tooltips.
"
```

---

## Task 6: CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add 'Disabled providers' section**

Open `CLAUDE.md`. Find the "Auto-routing (clear-rec auto-fire, propose only when ambiguous)" section. Immediately *after* that whole section ends (before "## เมื่อรับงานใหม่"), add:

```markdown
## Disabled providers (cockpit toggle)

Cockpit มี toggle 2 ตัวใน status bar ปิด/เปิด codex และ gemini ได้ตามใจ user ปิดเมื่อไรก็ได้ (ไม่ผูก rate limit) state persist ข้าม restart

**ขณะ provider ถูกปิด — Lead ห้ามทำสิ่งต่อไปนี้:**

- propose role นั้นใน routing table — ทั้ง primary และ cross-check
- fire `takkub assign --role <disabled>` หรือ `takkub <disabled>`
- เสนอ row codex/gemini ใน proposal table

ถ้า user ขอตรงๆ ("ให้ codex ทำ X") ขณะ codex ปิดอยู่ → **ห้ามทำ** ตอบว่า "codex provider ถูกปิดอยู่ user enable ก่อนได้ที่ status bar chip" ไม่เสนอ workaround หา role อื่นมาแทน (เคารพ user intent ที่ปิดมัน)

**Source of truth:** `~/.takkub/disabled-providers.json`
**Orchestrator inject สถานะ 2 ทาง:**
- Spawn time: `--append-system-prompt-file` มี section "⛔ Disabled providers"
- Runtime: เมื่อ user คลิก chip → inject `[system] <provider> ENABLED/DISABLED` เข้า Lead pane ทันที

**routing_planner.classify()** ก็เคารพ flag นี้: pass `context={"disabled_providers": {"codex"}}` → strip codex จาก cross_check และ degrade FIRE_ONESHOT เป็น ASK_CLARIFY (ดู tests `TestDisabledProviders`)

```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(lead): document provider toggle rules in CLAUDE.md

Adds 'Disabled providers (cockpit toggle)' section after the
auto-routing rules. Tells Lead to (1) skip disabled providers in
all proposals, (2) refuse explicit user requests for disabled
providers rather than silently substituting, (3) trust the
[system] runtime messages and spawn-time prompt section.
"
```

---

## Task 7: End-to-end manual smoke test (acceptance)

**Files:** none (verification only — no commit)

- [ ] **Step 1: Walk through spec section 8.2 in order**

1. Close any running cockpit. Delete `~/.takkub/disabled-providers.json` (start clean).

2. Launch cockpit: `python -m agent_takkub`
   - **Verify:** status bar shows `[Codex on (teal)]  [Gemini on (blue)]`
   - **Verify:** `~/.takkub/disabled-providers.json` either doesn't exist or is `{}`

3. Click Codex chip
   - **Verify:** chip turns gray + strikethrough
   - **Verify:** `cat ~/.takkub/disabled-providers.json` → contains `"codex": true`

4. Close cockpit + relaunch
   - **Verify:** Codex chip still off (state persisted)

5. Inside Lead pane, type: `refactor the auth module`
   - **Verify:** Lead's proposal table has NO codex row (codex would normally appear as cross-check)

6. Click Codex chip back to on
   - **Verify:** Lead pane sees `[system] codex ENABLED Available again.` (orchestrator broadcast)
   - **Verify:** chip back to teal

7. Type in Lead: `refactor the payment flow`
   - **Verify:** Lead's proposal table now includes codex as cross-check again

8. Click both Codex AND Gemini off
   - **Verify:** Lead pane receives both `[system]` messages

9. Type in Lead: `plan the rollout for deploying the new feature safely`
   - **Verify:** Lead does NOT propose gemini route. Instead Lead ASK_CLARIFY or offers role alternatives

10. Type in Lead: `ขอ codex review function นี้`
    - **Verify:** Lead refuses with explanation about codex being disabled (does not fire one-shot)

- [ ] **Step 2: Run full pytest suite one more time**

```bash
pytest tests/ -x -q
```
Expected: all tests PASS

- [ ] **Step 3: No commit — verification only**

This task records the manual checklist results. If any step fails, file an issue describing the failure and fix in a follow-up commit; do not mark the task complete until all 10 substeps pass.

---

## Self-Review Notes

**Spec coverage check:**
- Section 3 architecture (UI → Orch → file + broadcast) → Task 4, 5
- Section 4.1 provider_state module → Task 1
- Section 4.2 main_window status bar → Task 5
- Section 4.3 orchestrator toggle + signal → Task 4
- Section 4.4 routing_planner filter → Task 2
- Section 4.5 CLAUDE.md update → Task 6
- Section 5 Lead awareness spawn + runtime → Task 3 (spawn) + Task 4 (runtime)
- Section 6 UI mockup → Task 5
- Section 7 error cases covered by Task 1 tests (corrupt JSON, missing file, unknown provider)
- Section 8.1 unit tests → Task 1 + Task 2
- Section 8.2 manual smoke test → Task 7
- Section 9 file list matches Task 1–6 outputs

**Type consistency:**
- `provider_state.set_disabled(provider, flag)` — used identically in Task 1, 4, 5
- `provider_state.is_disabled(provider) -> bool` — used identically in Task 5
- `provider_state.all_disabled() -> set[str]` — used in Task 3 only
- `Orchestrator.toggle_provider(provider, disabled) -> tuple[bool, str]` — same signature in Task 4 definition + Task 5 call site
- `providerStateChanged = pyqtSignal(str, bool)` — signature `(provider, disabled)` matches the chip handler in Task 5

**Placeholder scan:** none — every step has the actual code/command/expected output.

---

## Execution notes for the implementer

- Active project at planning time = `agent-takkub`. Lead's `BLOCKED_DIRS` includes the cockpit source — Lead cannot edit these files directly. Delegate execution via `takkub assign --role backend --cwd C:/Users/monch/WebstormProjects/agent-takkub "<task description>"`.
- All test commands assume `pytest` is available via `pip install -e .[dev]` or equivalent.
- After all tasks complete, the branch can be merged to main. Then a separate branch starts for the Mac port (`docs/MACOS_PORT_PLAN.md`).
