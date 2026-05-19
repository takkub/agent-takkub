# Gemini CLI Role / Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google Gemini CLI as a third provider (alongside `claude` and `codex`) with a new `gemini` role that replaces `designer` in the default grid, mirroring the existing codex integration.

**Architecture:** Codex-pattern mirror. New `gemini_helper.py` + `gemini_md.py` modules sit beside their codex counterparts; orchestrator gets a third spawn branch keyed on `provider_for(role) == GEMINI`; `takkub gemini "<prompt>"` adds one-shot CLI; provider dialog gains a third dropdown choice. Designer is removed from defaults but its `.claude/agents/designer.md` file is preserved.

**Tech Stack:** Python 3.11+, PyQt6, pytest, `gemini` CLI (`npm install -g @google/gemini-cli`).

**Spec reference:** `docs/superpowers/specs/2026-05-19-gemini-cli-role-design.md`

---

## Pre-flight check

Before starting, verify the dev environment:

```bash
where.exe gemini       # must print at least one path
gemini --version       # should print a version string
pytest --version       # should print pytest version
```

If `gemini` is missing: `npm install -g @google/gemini-cli`, then re-run the check.

---

## Task 1: Provider config — add GEMINI constant

**Why first:** `provider_config.GEMINI` is imported by orchestrator, provider_dialog, and tests. Landing it first means subsequent tasks compile without forward references.

**Files:**
- Modify: `src/agent_takkub/provider_config.py`
- Test: `tests/test_provider_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_config.py` (inside the `TestProviderFor` class, after `test_codex_role_is_always_codex`):

```python
    def test_gemini_role_is_always_gemini(self, redirect_config_path: Path) -> None:
        # User mapping a "gemini" key to "claude" would be nonsensical;
        # the role's whole point is gemini.
        redirect_config_path.write_text('{"gemini": "claude"}', encoding="utf-8")
        assert provider_config.provider_for("gemini") == "gemini"

    def test_user_override_routes_to_gemini(self, redirect_config_path: Path) -> None:
        redirect_config_path.write_text('{"backend": "gemini", "qa": "gemini"}', encoding="utf-8")
        assert provider_config.provider_for("backend") == "gemini"
        assert provider_config.provider_for("qa") == "gemini"
```

And inside `TestLoadProviders`:

```python
    def test_accepts_gemini_provider(self, redirect_config_path: Path) -> None:
        # gemini joins claude/codex as a recognised provider — must
        # survive the sanitizer instead of being dropped.
        redirect_config_path.write_text(
            '{"backend": "gemini", "qa": "codex"}', encoding="utf-8"
        )
        loaded = provider_config.load_providers()
        assert loaded == {"backend": "gemini", "qa": "codex"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_provider_config.py -v
```

Expected: 3 new tests FAIL — `provider_for("gemini")` returns `"claude"` (default fallback), and the loader drops `gemini` entries because they're not in `VALID_PROVIDERS`.

- [ ] **Step 3: Add `GEMINI` constant + forced provider**

Edit `src/agent_takkub/provider_config.py`:

Replace lines 30-32:
```python
CLAUDE = "claude"
CODEX = "codex"
VALID_PROVIDERS = frozenset({CLAUDE, CODEX})
```

With:
```python
CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
VALID_PROVIDERS = frozenset({CLAUDE, CODEX, GEMINI})
```

Replace lines 37-40:
```python
_FORCED_PROVIDER = {
    "lead": CLAUDE,
    "codex": CODEX,
}
```

With:
```python
_FORCED_PROVIDER = {
    "lead": CLAUDE,
    "codex": CODEX,
    "gemini": GEMINI,
}
```

Also update the module docstring (lines 1-23) so the rule list reads:
```
- `lead`   → always `claude` (cockpit infrastructure assumes claude
             for Lead: CLAUDE.md auto-discovery, --append-system-prompt,
             session-resume `--continue`, token-meter JSONL, etc.)
- `codex`  → always `codex` (the role's whole point)
- `gemini` → always `gemini` (the role's whole point)
- everything else → user config wins; default `claude`
```

And the example mapping in the docstring:
```
    {"backend": "codex", "qa": "gemini"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_provider_config.py -v
```

Expected: all tests PASS (the 3 new ones + the existing ones).

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/provider_config.py tests/test_provider_config.py
git commit -m "feat(provider): add GEMINI as third provider constant

Adds gemini to VALID_PROVIDERS and forces the 'gemini' role to always
resolve to the gemini binary. Other roles can opt in via the JSON config.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Roles registry — swap designer for gemini

**Files:**
- Modify: `src/agent_takkub/roles.py`
- Test: `tests/test_roles.py`

- [ ] **Step 1: Rewrite the failing tests**

Replace the body of `TestDefaults` in `tests/test_roles.py` with:

```python
class TestDefaults:
    def test_lead_is_column_zero(self) -> None:
        assert roles.LEAD.column == 0
        assert roles.LEAD.row == 0
        assert roles.LEAD.name == "lead"

    def test_default_teammates_registry(self) -> None:
        assert len(roles.DEFAULT_TEAMMATES) == 8
        names = {r.name for r in roles.DEFAULT_TEAMMATES}
        assert names == {
            "frontend",
            "backend",
            "mobile",
            "devops",
            "gemini",
            "qa",
            "reviewer",
            "codex",
        }
        # Designer was retired from defaults but the agent file
        # `.claude/agents/designer.md` is preserved for custom add.
        assert "designer" not in names

    def test_default_columns_assigned(self) -> None:
        cols = {r.name: r.column for r in roles.DEFAULT_TEAMMATES}
        assert cols["frontend"] == 1
        assert cols["backend"] == 1
        assert cols["codex"] == 1
        assert cols["gemini"] == 2
        assert cols["reviewer"] == 2

    def test_gemini_slot_takes_old_designer_position(self) -> None:
        # Gemini replaces designer at col=2 row=0 — the top-right slot
        # right next to qa/reviewer.
        gemini = roles.by_name("gemini")
        assert gemini is not None
        assert gemini.column == 2
        assert gemini.row == 0
        assert gemini.label == "Gemini"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_roles.py -v
```

Expected: tests asserting on `gemini` and the absence of `designer` FAIL.

- [ ] **Step 3: Update `DEFAULT_TEAMMATES`**

Edit `src/agent_takkub/roles.py` — replace lines 28-43:

```python
DEFAULT_TEAMMATES: tuple[Role, ...] = (
    Role("frontend", "Frontend", "#22d3ee", column=1, row=0),
    Role("backend", "Backend", "#3b82f6", column=1, row=1),
    Role("mobile", "Mobile", "#a855f7", column=1, row=2),
    Role("devops", "DevOps", "#22c55e", column=1, row=3),
    Role("designer", "Designer", "#ec4899", column=2, row=0),
    Role("qa", "QA", "#f97316", column=2, row=1),
    Role("reviewer", "Reviewer", "#ef4444", column=2, row=2),
    # Codex is a non-claude pane: orchestrator launches the `codex`
    # binary directly (interactive TUI) and skips all claude flags +
    # ECC mutes. Sits in column 1 (dev specialists) below devops
    # because Codex's strength is code work, not support/review.
    # Colour is OpenAI's signature teal so it visually stands apart
    # from the claude-backed roles.
    Role("codex", "Codex", "#10a37f", column=1, row=4),
)
```

With:

```python
DEFAULT_TEAMMATES: tuple[Role, ...] = (
    Role("frontend", "Frontend", "#22d3ee", column=1, row=0),
    Role("backend", "Backend", "#3b82f6", column=1, row=1),
    Role("mobile", "Mobile", "#a855f7", column=1, row=2),
    Role("devops", "DevOps", "#22c55e", column=1, row=3),
    # Gemini is a non-claude pane: orchestrator launches the `gemini`
    # binary directly (interactive TUI) and skips all claude flags +
    # ECC mutes. Sits at col=2 row=0 (the slot designer used to occupy)
    # because Gemini's role is "third brain" planning / second opinion,
    # which lives alongside qa/reviewer in the support column.
    # Designer was removed from defaults; .claude/agents/designer.md
    # is preserved so custom-slot add still works for users who want it.
    # Colour is Google's signature blue so it visually stands apart
    # from claude-backed (cyan) and codex (teal) roles.
    Role("gemini", "Gemini", "#4285f4", column=2, row=0),
    Role("qa", "QA", "#f97316", column=2, row=1),
    Role("reviewer", "Reviewer", "#ef4444", column=2, row=2),
    # Codex is a non-claude pane: orchestrator launches the `codex`
    # binary directly (interactive TUI) and skips all claude flags +
    # ECC mutes. Sits in column 1 (dev specialists) below devops
    # because Codex's strength is code work, not support/review.
    # Colour is OpenAI's signature teal so it visually stands apart
    # from the claude-backed roles.
    Role("codex", "Codex", "#10a37f", column=1, row=4),
)
```

Also update the module docstring (lines 1-10) so the grid comment reads:

```
The cockpit reserves 8 slots in a 3-column grid:

  col 0 (left):   Lead (always-on)
  col 1 (middle): frontend / backend / mobile / devops / codex
  col 2 (right):  gemini / qa / reviewer + dynamic add-slot
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_roles.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/roles.py tests/test_roles.py
git commit -m "feat(roles): swap designer for gemini in default grid

Gemini takes col=2 row=0 — the top-right slot designer used to occupy.
Designer is preserved as a custom-add role via .claude/agents/designer.md
(orchestrator.register_role still works for it).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Gemini helper module

**Why now:** New file with no internal deps. Orchestrator (Task 5) and CLI (Task 6) both import from it, so it lands before them.

**Files:**
- Create: `src/agent_takkub/gemini_helper.py`
- Create: `tests/test_gemini_helper.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gemini_helper.py`:

```python
"""Tests for `gemini_helper` — the Gemini CLI wrapper behind
`takkub gemini "<prompt>"`. Mocks shutil.which + subprocess.run so
no real `gemini` calls leak from CI; the goal is to pin the argv
construction (gemini uses `-p` flag, NOT a subcommand) + the
error-surfacing contract.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub import gemini_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["gemini"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestFindGeminiExecutable:
    def test_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/usr/local/bin/gemini")
        assert gemini_helper.find_gemini_executable() == "/usr/local/bin/gemini"

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        assert gemini_helper.find_gemini_executable() is None


class TestGeminiExec:
    def test_returns_install_hint_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: None)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "npm install -g @google/gemini-cli" in msg

    def test_rejects_empty_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        ok, msg = gemini_helper.gemini_exec("   ")
        assert ok is False
        assert "empty prompt" in msg

    def test_builds_argv_without_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Gemini's headless flag is `-p <prompt>` — NOT a subcommand
        # like codex's `exec`. Pinning this is the whole point of the
        # test: a future refactor that reuses codex's argv shape would
        # send `gemini exec "..."` which would fail with "unknown
        # command".
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hello world")
        assert ok is True
        assert msg == "ok"
        assert seen["argv"] == ["/x/gemini", "-p", "hello world"]

    def test_builds_argv_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `-m <model>` goes before `-p <prompt>` (both are flags so
        # ordering is technically irrelevant to yargs, but we pin a
        # stable shape for diffability).
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="out")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, _ = gemini_helper.gemini_exec("review", model="gemini-2.5-pro")
        assert ok is True
        assert seen["argv"] == ["/x/gemini", "-m", "gemini-2.5-pro", "-p", "review"]

    def test_propagates_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["kwargs"] = kwargs
            return _proc(0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", cwd="C:/projects/foo")
        assert seen["kwargs"]["cwd"] == "C:/projects/foo"

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="gemini", timeout=120.0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "timed out" in msg

    def test_returns_false_when_binary_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("gemini")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "disappeared" in msg

    def test_surfaces_stderr_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="ERROR: auth expired. Re-run `gemini`."),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "auth expired" in msg

    def test_falls_back_to_stdout_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(2, stdout="rate-limited", stderr=""),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "rate-limited" in msg

    def test_trims_trailing_whitespace_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_gemini_executable", lambda: "/x/gemini")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(0, stdout="answer text\n\n  "),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is True
        assert msg == "answer text"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_gemini_helper.py -v
```

Expected: collection error — `agent_takkub.gemini_helper` module doesn't exist yet.

- [ ] **Step 3: Create the module**

Create `src/agent_takkub/gemini_helper.py`:

```python
"""Google Gemini CLI wrapper — non-interactive one-shot mode.

Mirror of codex_helper.py for the gemini CLI. Lets the user fire
Gemini via the cockpit's `takkub gemini` command for quick second-
opinion / planning / brainstorm questions without spawning a full
pane. No PTY, no orchestrator IPC — just
`subprocess.run(["gemini", "-p", "<prompt>"])` with the prompt
text routed through and the result printed back.

Auth is whatever Gemini CLI itself uses (Google login on first run
or `GEMINI_API_KEY` env var). The cockpit never touches Gemini's
credentials. If Gemini isn't logged in, its own stderr surfaces the
error verbatim.

Design rules (mirror codex_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s.
- No file writes by this module. Gemini writes its own session
  artefacts under `~/.gemini/` independently.
"""

from __future__ import annotations

import shutil
import subprocess


def find_gemini_executable() -> str | None:
    """Return the absolute path to the `gemini` binary, or None when
    it isn't on PATH. Caller surfaces a friendly "install with
    `npm install -g @google/gemini-cli`" message in the None case.

    On Windows npm installs `gemini` as a `.cmd` shim alongside the
    Node script; `shutil.which` handles the extension probing
    automatically (uses %PATHEXT%)."""
    return shutil.which("gemini")


def gemini_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `gemini -p "<prompt>"` and return `(ok, output)`.

    Gemini's non-interactive entry point is the `-p`/`--prompt` flag,
    NOT a subcommand like codex's `exec`. Don't reuse codex's argv
    shape — `gemini exec "..."` would fail with "unknown command".

    `cwd` lets the caller scope Gemini to a specific project. Defaults
    to the process cwd so `takkub gemini` from inside any pane targets
    that pane's project naturally.

    `model` is optional and gets forwarded as `-m <name>`; when None,
    Gemini uses whatever its config defaults to.

    `timeout` defaults to 120 s. Timeout returns
    (False, "gemini exec timed out").
    """
    binary = find_gemini_executable()
    if binary is None:
        return False, (
            "gemini binary not on PATH. Install with "
            "`npm install -g @google/gemini-cli`, then run `gemini` once to authenticate."
        )
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv: list[str] = [binary, "-p", prompt]
    if model:
        argv = [binary, "-m", model, "-p", prompt]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "gemini exec timed out"
    except FileNotFoundError:
        return False, "gemini binary disappeared from PATH"
    except Exception as e:
        return False, f"gemini exec failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "gemini exec failed").strip()
        return False, tail
    return True, (proc.stdout or "").strip()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_gemini_helper.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/gemini_helper.py tests/test_gemini_helper.py
git commit -m "feat(gemini): add gemini_helper for one-shot CLI invocations

Mirror of codex_helper.py. Pins the argv shape to `gemini -p <prompt>`
because the headless entry point is a flag, not a subcommand like
codex's exec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Gemini context-file planter (`GEMINI.md`)

**Files:**
- Create: `src/agent_takkub/gemini_md.py`
- Create: `tests/test_gemini_md.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gemini_md.py`:

```python
"""Tests for `gemini_md.ensure_gemini_md` — plants the takkub
cheatsheet as `GEMINI.md` in the spawn cwd. Mirror of
test_codex_agents_md.py. Guards the two safety rules: never clobber
a user-authored GEMINI.md, refresh our marker-tagged file idempotently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.gemini_md import TAKKUB_GEMINI_MARKER, ensure_gemini_md


class TestEnsureGeminiMd:
    def test_creates_when_missing(self, tmp_path: Path) -> None:
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is True
        assert reason == "written"
        target = tmp_path / "GEMINI.md"
        assert target.exists()
        first = target.read_text(encoding="utf-8").splitlines()[0]
        assert TAKKUB_GEMINI_MARKER in first

    def test_refreshes_when_marker_present(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        target.write_text(f"{TAKKUB_GEMINI_MARKER}\n\nold body\n", encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is True
        assert reason == "written"
        body = target.read_text(encoding="utf-8")
        assert TAKKUB_GEMINI_MARKER in body
        assert "old body" not in body
        assert "takkub send" in body  # cheatsheet content present

    def test_skips_user_owned_file(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        original = "# Project GEMINI\n\nrules: be careful with rm.\n"
        target.write_text(original, encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"
        assert target.read_text(encoding="utf-8") == original

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        ok, reason = ensure_gemini_md(str(tmp_path))
        assert ok is True
        assert reason == "written"

    def test_returns_failure_when_target_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_write_text(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert "write failed" in reason

    def test_handles_empty_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "GEMINI.md"
        target.write_text("", encoding="utf-8")
        ok, reason = ensure_gemini_md(tmp_path)
        assert ok is False
        assert reason == "user-owned"

    def test_marker_is_distinct_from_codex(self) -> None:
        # Guard against accidental copy-paste: each planter must use
        # its own marker so the two files can coexist in a single cwd
        # without one clobbering the other on refresh.
        from agent_takkub.codex_agents_md import TAKKUB_MARKER as codex_marker

        assert TAKKUB_GEMINI_MARKER != codex_marker
        assert "GEMINI" in TAKKUB_GEMINI_MARKER
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_gemini_md.py -v
```

Expected: collection error — `agent_takkub.gemini_md` doesn't exist.

- [ ] **Step 3: Create the module**

Create `src/agent_takkub/gemini_md.py`:

```python
"""Gemini pane GEMINI.md auto-plant.

Gemini CLI auto-discovers `GEMINI.md` from its cwd. The cockpit plants
a short cheatsheet there before spawning the gemini pane so the agent
knows about `takkub send/done` — letting Gemini behave like a real
teammate (peer coordination + report-back-to-Lead) instead of a
detached terminal.

Mirror of codex_agents_md.py. Lives as a separate module (rather than
generalising codex_agents_md) because:
- Marker text is distinct (so the two files coexist without overwriting)
- Filename is different (AGENTS.md vs GEMINI.md)
- Cheatsheet body addresses the agent as "Gemini Teammate"

Safety rule:
- We only manage files we tagged with our marker header. If a user
  already has their own `GEMINI.md` (no marker) we leave it alone —
  Gemini will use theirs and our `takkub` cheatsheet just won't be
  available. Acceptable degradation.
"""

from __future__ import annotations

from pathlib import Path

TAKKUB_GEMINI_MARKER = "<!-- takkub-managed GEMINI.md · do not commit -->"

GEMINI_MD = f"""{TAKKUB_GEMINI_MARKER}

# Gemini Teammate · agent-takkub cockpit

You are running inside an **agent-takkub** pane spawned by a human
operator (or by a Claude Lead pane via `takkub assign --role gemini
"<task>"`). Behave like a focused specialist:

## Hard rules

- **Do the task yourself.** Don't try to spawn sub-agents or delegate.
  You are the specialist; if you're stuck, ask Lead via `takkub send`.
- **One task per session.** When the work is done, call
  `takkub done "<one-line summary>"` so Lead is notified and the
  cockpit can free the pane.
- **No long-running foreground commands.** Background docker/dev
  servers with `&` + redirect, or use `-d`. Never `npm run dev` in
  the foreground — it never returns and the pane hangs.

## Communication with the rest of the team

| Command | When to use |
|---|---|
| `takkub send --to lead "<msg>"` | Ask Lead a clarifying question, request more context, or surface a blocker. Don't wait silently. |
| `takkub send --to <role> "<msg>"` | Coordinate directly with a peer pane (e.g. `frontend`, `backend`, `qa`). Lead is auto-CC'd. |
| `takkub done "<note>"` | Final step when your task is complete. Pane closes after this. |
| `takkub list` | See which other panes are open in the same project. |

The `takkub` binary is on `PATH` inside this pane — just run it as a
shell command.

## When the user said "brainstorm"

If the prompt is exploratory ("ideas for X", "how should we approach Y"):
respond with 3-5 concrete options + the main trade-off of each.
Don't write code until the user picks a direction. **Do not call
`takkub done` for brainstorm sessions** — the user will close the
pane manually when they've absorbed the answer.

## Working directory

The cockpit set your cwd to the project the operator is currently
focused on. Treat that as your workspace root. Read files, run tests,
commit when explicitly asked — don't push without permission.
"""


def ensure_gemini_md(spawn_cwd: str | Path) -> tuple[bool, str]:
    """Plant `<spawn_cwd>/GEMINI.md` with the cockpit cheatsheet.

    Returns `(planted, reason)`:
      - `(True, "written")` — file was created or refreshed.
      - `(False, "user-owned")` — existing GEMINI.md without our
        marker; left untouched.
      - `(False, "<error>")` — disk failure (permission, etc.).

    Idempotent: if the file already carries our marker, we overwrite
    it (refresh). If a user-owned GEMINI.md exists we skip.
    """
    target = Path(spawn_cwd) / "GEMINI.md"
    try:
        if target.exists():
            head = target.read_text(encoding="utf-8", errors="replace").splitlines()
            first = head[0] if head else ""
            if TAKKUB_GEMINI_MARKER not in first:
                return False, "user-owned"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(GEMINI_MD, encoding="utf-8")
        return True, "written"
    except OSError as e:
        return False, f"write failed: {e}"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_gemini_md.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/gemini_md.py tests/test_gemini_md.py
git commit -m "feat(gemini): plant GEMINI.md cheatsheet in gemini pane cwd

Mirror of codex_agents_md.py for the gemini CLI. Distinct marker so
codex's AGENTS.md and gemini's GEMINI.md can coexist in one cwd
without either planter overwriting the other.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Orchestrator spawn branch for gemini

**Why now:** Depends on provider_config (Task 1), gemini_helper (Task 3), gemini_md (Task 4). PTY spawn is hard to unit-test (codex branch has no test either), so this task is implementation-only with smoke check.

**Files:**
- Modify: `src/agent_takkub/orchestrator.py`

- [ ] **Step 1: Add gemini branch after codex branch**

Edit `src/agent_takkub/orchestrator.py`. Find line 723:

```python
        from .provider_config import CODEX, provider_for
```

Replace with:

```python
        from .provider_config import CODEX, GEMINI, provider_for
```

Then find the line that says `return True, f"codex spawned in {spawn_cwd}"` (around line 772). **Right after** that line, before the comment block starting with `# Resolve cwd:`, insert the new gemini branch:

```python

        # ── gemini pane: non-claude path ─────────────────────────────
        # Mirror of the codex branch above but for Google's Gemini CLI.
        # `gemini` uses different flags than codex: `-y` for yolo
        # (auto-approve everything) is the closest parity to codex's
        # `--ask-for-approval never -s workspace-write`.
        #
        # Entry condition uses `provider_for(role_name)` so the user
        # can remap any teammate role (e.g. "backend") to the gemini
        # binary via `~/.takkub/role-providers.json`. The `gemini` role
        # itself is forced into this branch by provider_config's
        # `_FORCED_PROVIDER` table.
        if provider_for(role_name) == GEMINI:
            from .gemini_helper import find_gemini_executable
            from .gemini_md import ensure_gemini_md

            gemini_bin = find_gemini_executable()
            if gemini_bin is None:
                return False, (
                    "gemini binary not on PATH. Install with "
                    "`npm install -g @google/gemini-cli`, then run `gemini` once."
                )
            spawn_cwd = cwd or default_cwd_for_role(role_name) or str(REPO_ROOT)
            # Plant the takkub cheatsheet so Gemini auto-discovers it
            # on boot and knows how to call `takkub send/done`. Safe:
            # only writes when the file is absent or already takkub-
            # managed (marker check inside the helper).
            ensure_gemini_md(spawn_cwd)
            env = os.environ.copy()
            env["TAKKUB_ROLE"] = role_name
            env["TAKKUB_PROJECT"] = project_ns
            bin_dir = str(REPO_ROOT / "bin")
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            # `-y` = `--approval-mode yolo` — skips trust prompt and
            # auto-approves every tool call so the pane is unattended-
            # runnable. Parity with codex's autonomy flags. Blast
            # radius stays cwd-scoped just like codex.
            gemini_argv = [gemini_bin, "-y"]
            session = PtySession(cols=110, rows=36, parent=self)
            try:
                session.spawn(argv=gemini_argv, cwd=spawn_cwd, env=env)
            except Exception as e:
                return False, f"failed to spawn gemini: {e}"
            pane.attach_session(session, cwd=spawn_cwd)
            session.processExited.connect(
                lambda _code, r=role_name, c=spawn_cwd, p=project_ns: self._on_session_exit(r, c, p)
            )
            if role_name in self._recent_exits:
                del self._recent_exits[role_name]
            self._auto_trust(role_name)
            self.statusChanged.emit()
            _log_event("spawn", role=role_name, cwd=spawn_cwd, resumed=False)
            return True, f"gemini spawned in {spawn_cwd}"
```

- [ ] **Step 2: Verify the import & syntax**

```bash
python -c "from agent_takkub import orchestrator; print('import ok')"
```

Expected: prints `import ok`. Any `SyntaxError` or `ImportError` means a typo in the diff above — fix it before continuing.

- [ ] **Step 3: Run the full existing test suite to confirm no regression**

```bash
pytest -x
```

Expected: all tests PASS. Spawn branch isn't unit-tested but the import path must stay clean.

- [ ] **Step 4: Commit**

```bash
git add src/agent_takkub/orchestrator.py
git commit -m "feat(orchestrator): spawn gemini pane via gemini -y

Adds a third spawn branch keyed on provider_for(role) == GEMINI.
Mirrors the codex branch shape (binary lookup, GEMINI.md plant,
PTY spawn with TAKKUB_ROLE/PROJECT env). Uses gemini -y for yolo-mode
parity with codex's --ask-for-approval never autonomy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Add `takkub gemini` one-shot CLI

**Files:**
- Modify: `src/agent_takkub/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`, inside the `TestArgparse` class:

```python
    def test_gemini_one_shot_routes_to_helper(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `takkub gemini "<prompt>"` is pure-local (does NOT go through
        # the orchestrator socket). Mock gemini_exec and assert the CLI
        # routes the prompt + flags through correctly.
        seen: dict[str, object] = {}

        def fake_gemini_exec(prompt: str, *, cwd: str | None = None,
                             timeout: float = 120.0, model: str | None = None):
            seen["prompt"] = prompt
            seen["cwd"] = cwd
            seen["timeout"] = timeout
            seen["model"] = model
            return True, "gemini answered"

        from agent_takkub import gemini_helper
        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        rc = cli.main(["gemini", "review this approach"])
        assert rc == 0
        assert seen["prompt"] == "review this approach"
        assert seen["cwd"] is None
        assert seen["model"] is None
        out = capsys.readouterr().out
        assert "gemini answered" in out

    def test_gemini_forwards_cwd_and_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_gemini_exec(prompt: str, *, cwd: str | None = None,
                             timeout: float = 120.0, model: str | None = None):
            seen["cwd"] = cwd
            seen["model"] = model
            seen["timeout"] = timeout
            return True, ""

        from agent_takkub import gemini_helper
        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        cli.main([
            "gemini",
            "--cwd", "C:/x/proj",
            "--model", "gemini-2.5-pro",
            "--timeout", "30",
            "do thing",
        ])
        assert seen["cwd"] == "C:/x/proj"
        assert seen["model"] == "gemini-2.5-pro"
        assert seen["timeout"] == 30.0
```

Also add inside `TestRoleGate`:

```python
    def test_teammate_can_run_gemini_one_shot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `gemini` is local — not in LEAD_ONLY_COMMANDS — so a teammate
        # pane can fire it for a second opinion mid-task.
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        from agent_takkub import gemini_helper
        monkeypatch.setattr(
            gemini_helper, "gemini_exec",
            lambda *_a, **_kw: (True, "answer"),
        )
        rc = cli.main(["gemini", "ping"])
        assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cli.py -v -k gemini
```

Expected: 3 tests FAIL — `argparse` rejects `gemini` as an unknown subcommand.

- [ ] **Step 3: Add `cmd_gemini` + parser**

Edit `src/agent_takkub/cli.py`. Find `cmd_codex` (line ~142-166), and **right after** the `cmd_codex` function (before `cmd_search`), insert:

```python
def cmd_gemini(args: argparse.Namespace) -> dict:
    """Fire Google Gemini CLI non-interactively and print the result.

    Mirror of `cmd_codex`. Pure local invocation — no orchestrator IPC.
    Gemini uses its own auth (Google login on first run or
    `GEMINI_API_KEY` env); cockpit doesn't touch those credentials.
    Works whether or not the cockpit is running.

    `cwd` defaults to the calling pane's working directory so a
    `takkub gemini "review this"` inside a project pane naturally
    runs Gemini against that project's files.
    """
    from .gemini_helper import gemini_exec

    ok, output = gemini_exec(
        args.prompt,
        cwd=args.cwd,
        timeout=args.timeout,
        model=args.model,
    )
    if output:
        print(output)
    return {
        "ok": ok,
        "msg": "gemini done" if ok else "gemini failed",
    }
```

Then find the codex subparser block (`sx = sub.add_parser("codex", ...)` around line 273) and **right after** that block (after `sx.set_defaults(func=cmd_codex)`), insert:

```python

    sg = sub.add_parser(
        "gemini",
        help="one-shot Google Gemini CLI query (non-interactive, pure local)",
    )
    sg.add_argument("prompt", help="prompt text to send to Gemini (positional)")
    sg.add_argument(
        "--cwd",
        default=None,
        help="working directory for the Gemini run (default: current dir)",
    )
    sg.add_argument(
        "--model",
        default=None,
        help="override Gemini's default model (e.g. gemini-2.5-pro)",
    )
    sg.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait before killing the gemini process (default: 120)",
    )
    sg.set_defaults(func=cmd_gemini)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cli.py -v
```

Expected: all tests PASS (3 new gemini tests + the existing CLI suite).

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/cli.py tests/test_cli.py
git commit -m "feat(cli): add takkub gemini one-shot subcommand

Mirror of takkub codex. Calls gemini_helper.gemini_exec(); routes
--cwd/--model/--timeout through. Not lead-only — teammate panes can
fire it for second opinions mid-task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Provider dialog — 3-way dropdown + locked gemini row

**Files:**
- Modify: `src/agent_takkub/provider_dialog.py`

**Note:** No automated tests — the dialog is a PyQt widget and the project has no headless Qt test fixture. Verify by running the cockpit manually after committing (Task 9 below).

- [ ] **Step 1: Update import + dropdown**

Edit `src/agent_takkub/provider_dialog.py`. Replace line 27:

```python
from .provider_config import CLAUDE, CODEX, provider_for, save_providers
```

With:

```python
from .provider_config import CLAUDE, CODEX, GEMINI, provider_for, save_providers
```

- [ ] **Step 2: Add locked gemini row + 3-way dropdown**

Find the loop body inside `__init__` (lines 70-81):

```python
        self._combos: dict[str, QComboBox] = {}
        for role in DEFAULT_TEAMMATES:
            if role.name == "codex":
                locked = QLabel("codex   (locked — role identity)")
                locked.setStyleSheet("color: #71717a; font-style: italic;")
                form.addRow(f"{role.label}:", locked)
                continue
            combo = QComboBox()
            combo.addItems([CLAUDE, CODEX])
            combo.setCurrentText(provider_for(role.name))
            self._combos[role.name] = combo
            form.addRow(f"{role.label}:", combo)
```

Replace with:

```python
        self._combos: dict[str, QComboBox] = {}
        for role in DEFAULT_TEAMMATES:
            if role.name == "codex":
                locked = QLabel("codex   (locked — role identity)")
                locked.setStyleSheet("color: #71717a; font-style: italic;")
                form.addRow(f"{role.label}:", locked)
                continue
            if role.name == "gemini":
                locked = QLabel("gemini   (locked — role identity)")
                locked.setStyleSheet("color: #71717a; font-style: italic;")
                form.addRow(f"{role.label}:", locked)
                continue
            combo = QComboBox()
            combo.addItems([CLAUDE, CODEX, GEMINI])
            combo.setCurrentText(provider_for(role.name))
            self._combos[role.name] = combo
            form.addRow(f"{role.label}:", combo)
```

- [ ] **Step 3: Update intro text**

Find the intro `QLabel` (lines 48-54) and update the final sentence:

Replace:
```python
        intro = QLabel(
            "Choose which CLI backs each teammate role. Saving applies\n"
            "to the next pane you spawn — no restart needed. Already-\n"
            "running panes keep their original CLI; close + respawn to\n"
            "flip them. Lead is locked to Claude; Codex role is locked\n"
            "to Codex."
        )
```

With:
```python
        intro = QLabel(
            "Choose which CLI backs each teammate role. Saving applies\n"
            "to the next pane you spawn — no restart needed. Already-\n"
            "running panes keep their original CLI; close + respawn to\n"
            "flip them. Lead is locked to Claude; Codex role is locked\n"
            "to Codex; Gemini role is locked to Gemini."
        )
```

- [ ] **Step 4: Verify the dialog still imports**

```bash
python -c "from agent_takkub.provider_dialog import RoleProviderDialog; print('import ok')"
```

Expected: prints `import ok`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/provider_dialog.py
git commit -m "feat(provider-dialog): 3-way dropdown + locked gemini row

Dropdown now offers claude/codex/gemini for every overridable role.
Gemini gets its own locked row alongside the existing codex locked
row.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Main window tooltip + CLAUDE.md updates

**Files:**
- Modify: `src/agent_takkub/main_window.py`
- Modify: `CLAUDE.md` (project root)

- [ ] **Step 1: Update the providers button tooltip**

Edit `src/agent_takkub/main_window.py`. Find line 298:

```python
            "Configure which CLI (claude / codex) backs each teammate role.\n"
```

Replace with:

```python
            "Configure which CLI (claude / codex / gemini) backs each teammate role.\n"
```

- [ ] **Step 2: Update the teammate roster in CLAUDE.md**

Edit `CLAUDE.md`. Find line 8:

```markdown
- **designer** — design spec, design tokens, UX review, a11y (ไม่เขียน feature code)
```

Replace with:

```markdown
- **gemini** — Google Gemini CLI "สมองที่ 3" สำหรับ planning / second opinion / brainstorm ฝั่ง multi-perspective — ใช้คู่ codex เพื่อเทียบ 3 มุมมอง (claude / codex / gemini)
```

- [ ] **Step 3: Add "เมื่อไหร่ควรเรียก gemini" section**

In `CLAUDE.md`, find the line `### เมื่อไหร่ควรเรียก codex` (line 16) and the section it heads (lines 16-28). **Right after** that section (after the closing triple-backtick of the codex example block on line 28), insert:

```markdown

### เมื่อไหร่ควรเรียก gemini

- **Planning / outline** — ดี large-context (1M tokens) เหมาะอ่านโค้ดทั้ง repo แล้วเสนอ phase plan กว้างๆ ก่อน claude/codex ลงรายละเอียด
- **Second opinion มุมที่ 3** — มี codex review แล้ว ส่ง diff เดียวกันให้ gemini หา angle ที่ codex/claude ไม่เห็น
- **Long-context summarisation** — สรุป log / chat transcript ยาว ไม่กิน context ของ claude main
- **One-shot brainstorm** — `takkub gemini "3 ideas for X + tradeoffs"` (ไม่ต้องเปิด pane)

ตัวอย่างเทียบ 3 มุมในงานเดียว:
```bash
takkub assign --role backend "implement POST /auth/logout reset session"            &
takkub assign --role codex   "review this approach: POST /auth/logout. Edge cases?" &
takkub assign --role gemini  "plan rollout: deploy /auth/logout safely 3 phases"    &
wait
# backend เขียน code, codex หา edge case, gemini วาง rollout — Lead รวม report ทั้ง 3
```
```

- [ ] **Step 4: Run the full test suite for a final regression check**

```bash
pytest
```

Expected: every test (including the new gemini ones) PASSES. If any fail, the prior tasks introduced a regression — fix before committing this task.

- [ ] **Step 5: Commit**

```bash
git add src/agent_takkub/main_window.py CLAUDE.md
git commit -m "docs(lead): document gemini role + 3-way provider tooltip

Adds Gemini to the teammate roster and a 'when to call gemini' section
parallel to the codex one. Provider button tooltip lists all three CLIs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Manual smoke verification

This is a checklist, not a code task. Run only after tasks 1-8 are merged.

- [ ] **Smoke 1: Provider dialog renders 3-way dropdown**

Launch the cockpit and click the `🤖 Providers` button. Verify:
- Dropdown for `Frontend`, `Backend`, `Mobile`, `DevOps`, `QA`, `Reviewer` has three options: `claude`, `codex`, `gemini`
- `Lead`, `Codex`, `Gemini` rows show locked italic text (no dropdown)

- [ ] **Smoke 2: Default grid swaps designer → gemini**

In the cockpit, after launch the right column shows: `Gemini` (top), `QA`, `Reviewer` — not `Designer`.

- [ ] **Smoke 3: Spawn gemini pane**

From the Lead pane:
```bash
takkub assign --role gemini "list the python files in this repo"
```

Verify:
- A new pane opens labelled `Gemini`
- The pane shows the gemini CLI banner + the prompt
- After completion, `takkub list` shows `gemini` state cleared

- [ ] **Smoke 4: One-shot `takkub gemini`**

From any pane (or terminal):
```bash
takkub gemini "say hello"
```

Verify:
- Exit 0
- Some response text printed
- No "binary not on PATH" error (would mean gemini isn't installed)

- [ ] **Smoke 5: `GEMINI.md` planted in spawn cwd**

After Smoke 3, check the gemini pane's cwd contains a `GEMINI.md` file starting with `<!-- takkub-managed GEMINI.md · do not commit -->`.

- [ ] **Smoke 6: Provider override actually applies**

Open `~/.takkub/role-providers.json` and set:
```json
{"backend": "gemini"}
```
Then from Lead:
```bash
takkub close --role backend
takkub assign --role backend "say hi"
```
Verify the new backend pane runs `gemini`, not `claude`.

If any smoke fails, file follow-up. No retroactive task split — the failing path is the place to fix.

---

## Self-Review (writing-plans skill)

**Spec coverage check** — every spec section mapped to a task:

| Spec section | Task |
|---|---|
| Architecture (file layout table) | Tasks 1-8 (one per row) |
| Slot Decision (col=2 row=0) | Task 2 |
| `gemini_helper.py` | Task 3 |
| `gemini_md.py` | Task 4 |
| Orchestrator spawn branch | Task 5 |
| `provider_config.py` constant | Task 1 |
| `provider_dialog.py` dropdown | Task 7 |
| `cli.py` one-shot | Task 6 |
| `roles.py` swap | Task 2 |
| `main_window.py` tooltip | Task 8 |
| `CLAUDE.md` update | Task 8 |
| Spawn argv `gemini -y` | Task 5 |
| Testing strategy (all 5 test files) | Tasks 1, 2, 3, 4, 6 |
| Auth / runtime concerns | (docs only, covered in module docstrings of Task 3 & 5) |
| Pitfalls | Covered by Task 3 test `test_builds_argv_without_model` (pins `-p` not `exec`) + Task 4 test `test_marker_is_distinct_from_codex` |
| Acceptance criteria | Task 9 smoke checklist |

No gaps detected.

**Placeholder scan:** Searched for "TBD", "TODO", "later", "appropriate", "similar to" — none found in steps. Each test step has actual code, each impl step has actual diff content.

**Type consistency:** `find_gemini_executable` / `gemini_exec` / `ensure_gemini_md` / `TAKKUB_GEMINI_MARKER` / `GEMINI_MD` names match across Tasks 3, 4, 5, 6. `GEMINI` constant matches across Tasks 1, 5, 7.

Plan is internally consistent.
