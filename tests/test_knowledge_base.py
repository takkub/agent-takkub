"""Issue #168: local (non-vault) knowledge-base distillation.

Covers `distill_to_knowledge_base` in vault_mirror.py — the vault-free
sibling of `distill_session_facts` that writes durable `takkub done` notes
into `runtime/knowledge/<project>.md` regardless of whether an Obsidian
vault is configured, provider, or role.
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.vault_mirror import (
    _DEDUP_HASHES,
    _KB_MAX_BYTES,
    _KB_MAX_ENTRIES,
    _cap_knowledge_base,
    _kb_header,
    distill_to_knowledge_base,
    knowledge_base_enabled,
    knowledge_base_path,
)

TEST_PROJECT = "agent-takkub"
_DURABLE_NOTE = "fix: root cause was race condition in pane spawn event loop"
_NOISE_NOTE = "all tests passed and build completed successfully"
_NOW = datetime(2026, 8, 13, 12, 0, 0)


class TestKnowledgeBaseEnabled:
    def test_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        assert knowledge_base_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "off", "no", "FALSE", "Off"])
    def test_disabled_by_opt_out_values(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv("TAKKUB_KNOWLEDGE_BASE", val)
        assert knowledge_base_enabled() is False

    def test_unknown_value_stays_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_KNOWLEDGE_BASE", "yes")
        assert knowledge_base_enabled() is True


class TestDistillToKnowledgeBase:
    def test_durable_note_written(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        ok = distill_to_knowledge_base(TEST_PROJECT, "backend", _DURABLE_NOTE, tmp_path, now=_NOW)
        assert ok is True
        page = knowledge_base_path(tmp_path, TEST_PROJECT)
        assert page.is_file()
        text = page.read_text(encoding="utf-8")
        assert "backend" in text
        assert "root cause" in text

    def test_noise_note_not_written(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        ok = distill_to_knowledge_base(TEST_PROJECT, "backend", _NOISE_NOTE, tmp_path, now=_NOW)
        assert ok is False
        assert not knowledge_base_path(tmp_path, TEST_PROJECT).is_file()

    def test_no_vault_required(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of #168: works without any Obsidian vault dir at all."""
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        # tmp_path here plays the role of RUNTIME_DIR; no "01-Projects" sibling
        # exists anywhere, unlike vault_mirror's _resolve_vault_dir contract.
        ok = distill_to_knowledge_base(TEST_PROJECT, "qa", _DURABLE_NOTE, tmp_path, now=_NOW)
        assert ok is True

    def test_opt_out_skips_write(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_KNOWLEDGE_BASE", "0")
        ok = distill_to_knowledge_base(TEST_PROJECT, "backend", _DURABLE_NOTE, tmp_path, now=_NOW)
        assert ok is False
        assert not knowledge_base_path(tmp_path, TEST_PROJECT).is_file()

    def test_idempotent_same_entry_twice(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        distill_to_knowledge_base(TEST_PROJECT, "backend", _DURABLE_NOTE, tmp_path, now=_NOW)
        distill_to_knowledge_base(TEST_PROJECT, "backend", _DURABLE_NOTE, tmp_path, now=_NOW)
        text = knowledge_base_path(tmp_path, TEST_PROJECT).read_text(encoding="utf-8")
        entry_lines = [ln for ln in text.splitlines() if "root cause" in ln]
        assert len(entry_lines) == 1

    def test_provider_neutral_any_role_writes(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No role/provider gating — any role string is accepted verbatim."""
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        ok = distill_to_knowledge_base(TEST_PROJECT, "codex", _DURABLE_NOTE, tmp_path, now=_NOW)
        assert ok is True
        assert "codex" in knowledge_base_path(tmp_path, TEST_PROJECT).read_text(encoding="utf-8")

    def test_error_does_not_raise(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        # Force an OSError: make the knowledge/<project>.md parent path collide
        # with a regular file, so mkdir(parents=True) fails.
        blocker = tmp_path / "knowledge"
        blocker.write_text("not a dir", encoding="utf-8")
        ok = distill_to_knowledge_base(TEST_PROJECT, "backend", _DURABLE_NOTE, tmp_path, now=_NOW)
        assert ok is False


class TestCapKnowledgeBase:
    def test_under_budget_is_noop(self) -> None:
        text = _kb_header("proj") + "- `2026-08-13T00:00:00` **backend** — one entry\n"
        assert _cap_knowledge_base(text) == text

    def test_drops_oldest_entries_over_entry_cap(self) -> None:
        header = _kb_header("proj")
        entries = "".join(
            f"- `2026-08-13T00:00:{i:02d}` **backend** — entry number {i}\n"
            for i in range(_KB_MAX_ENTRIES + 10)
        )
        capped = _cap_knowledge_base(header + entries, max_bytes=10**9, max_entries=_KB_MAX_ENTRIES)
        n_bullets = sum(1 for ln in capped.splitlines() if ln.startswith("- `"))
        assert n_bullets == _KB_MAX_ENTRIES
        # oldest (entry 0) must be gone, newest (last) must survive
        assert "entry number 0\n" not in capped
        assert f"entry number {_KB_MAX_ENTRIES + 9}" in capped

    def test_drops_oldest_entries_over_byte_cap(self) -> None:
        header = _kb_header("proj")
        long_note = "x" * 200
        entries = "".join(
            f"- `2026-08-13T00:00:{i:02d}` **backend** — {long_note} #{i}\n" for i in range(200)
        )
        capped = _cap_knowledge_base(header + entries, max_bytes=_KB_MAX_BYTES, max_entries=10**9)
        assert len(capped.encode("utf-8")) <= _KB_MAX_BYTES
        assert "#0\n" not in capped  # oldest dropped
        assert "#199" in capped  # newest survives

    def test_header_never_dropped(self) -> None:
        header = _kb_header("proj")
        entries = "".join(
            f"- `2026-08-13T00:00:{i:02d}` **backend** — {'y' * 500} #{i}\n" for i in range(500)
        )
        capped = _cap_knowledge_base(header + entries, max_bytes=1000, max_entries=10**9)
        assert capped.startswith("# proj — knowledge base")


class TestOrchestratorWiring:
    def test_save_decision_note_writes_knowledge_base_without_vault(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)
        _DEDUP_HASHES.clear()

        Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note=_DURABLE_NOTE,
            now=_NOW,
        )

        kb = knowledge_base_path(tmp_path / "runtime", TEST_PROJECT)
        assert kb.is_file(), "knowledge base must be written even with no vault configured"
        assert "backend" in kb.read_text(encoding="utf-8")

    def test_save_decision_note_skips_noise_note(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)
        _DEDUP_HASHES.clear()

        Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note="ran the test suite, everything green as expected today",
            now=_NOW,
        )

        kb = knowledge_base_path(tmp_path / "runtime", TEST_PROJECT)
        assert not kb.is_file()

    def test_done_flow_never_raises_when_knowledge_base_write_fails(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort contract (#168 requirement 2): a broken knowledge-base
        write must never break the `done()` report path it rides alongside."""
        monkeypatch.delenv("TAKKUB_KNOWLEDGE_BASE", raising=False)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)
        monkeypatch.setattr(
            "agent_takkub.orchestrator.distill_to_knowledge_base",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        _DEDUP_HASHES.clear()

        # Must not raise despite the patched-in failure.
        result = Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note=_DURABLE_NOTE,
            now=_NOW,
        )
        assert result is not None  # local session file still written
