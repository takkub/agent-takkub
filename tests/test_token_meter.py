"""Tests for token_meter.read_last_usage — the tail-read fast path that
replaced streaming the whole session JSONL on the Qt main thread every 5 s
(periodic UI hitch; see docs/cockpit-freeze-rca-2026-05-29.md). Correctness is
preserved by a full-scan fallback when the tail holds no assistant turn.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from agent_takkub.token_meter import (
    _TAIL_SCAN_BYTES,
    context_limit_for_model,
    effective_context_limit,
    encode_path_for_claude,
    find_latest_session,
    find_session_by_uuid,
    read_last_usage,
)


class TestContextLimitForModel:
    """_MODEL_LIMITS is keyed by bare model id — every current 1M-context
    model must resolve without needing a "[1m]" suffix, per shared/models.md
    in the claude-api skill (checked live 2026-08-03)."""

    def test_opus5_no_suffix_is_1m(self) -> None:
        assert context_limit_for_model("claude-opus-5") == 1_000_000

    def test_sonnet5_no_suffix_is_1m(self) -> None:
        assert context_limit_for_model("claude-sonnet-5") == 1_000_000

    def test_fable5_no_suffix_is_1m(self) -> None:
        assert context_limit_for_model("claude-fable-5") == 1_000_000

    def test_opus_4x_no_suffix_is_1m(self) -> None:
        assert context_limit_for_model("claude-opus-4-8") == 1_000_000
        assert context_limit_for_model("claude-opus-4-7") == 1_000_000
        assert context_limit_for_model("claude-opus-4-6") == 1_000_000

    def test_sonnet_4_6_no_suffix_is_1m(self) -> None:
        assert context_limit_for_model("claude-sonnet-4-6") == 1_000_000

    def test_haiku_stays_200k(self) -> None:
        assert context_limit_for_model("claude-haiku-4-5") == 200_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        assert context_limit_for_model("claude-some-future-model") == 200_000
        assert context_limit_for_model(None) == 200_000

    def test_1m_suffix_still_forces_1m_on_any_model(self) -> None:
        # Legacy behaviour preserved: a stamped "[1m]" suffix means 1M
        # regardless of the base model's own default — even one that isn't
        # (yet) a 1M model on its own, like haiku.
        assert context_limit_for_model("claude-opus-4-8[1m]") == 1_000_000
        assert context_limit_for_model("claude-sonnet-5[1m]") == 1_000_000
        assert context_limit_for_model("claude-haiku-4-5[1m]") == 1_000_000

    def test_env_override_wins_over_model_table(self, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_CONTEXT_LIMIT", "555000")
        assert context_limit_for_model("claude-opus-5") == 555_000
        assert context_limit_for_model(None) == 555_000

    def test_env_override_ignored_when_not_an_int(self, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_CONTEXT_LIMIT", "not-a-number")
        assert context_limit_for_model("claude-haiku-4-5") == 200_000


class TestEffectiveContextLimit:
    """The badge cap must never let the percentage exceed 100% just because
    the resolved model/base cap turns out to be too small."""

    def test_under_default_uses_200k(self) -> None:
        assert effective_context_limit("claude-some-future-model", 50_000) == 200_000

    def test_prompt_over_200k_bumps_to_1m(self) -> None:
        # The 177%-badge bug: a >200k prompt on an unknown/unlisted model must read 1M.
        assert effective_context_limit("claude-some-future-model", 360_000) == 1_000_000

    def test_per_pane_base_pins_1m_from_token_zero(self) -> None:
        # A Max Lead pins base=1M so even a small prompt shows /1M, not /200k.
        assert effective_context_limit("claude-haiku-4-5", 33_000, base=1_000_000) == 1_000_000

    def test_base_overrides_model_lookup(self) -> None:
        assert effective_context_limit("anything", 10_000, base=200_000) == 200_000

    def test_prompt_exceeding_pinned_base_still_bumps(self) -> None:
        # Defensive: even with a base, a prompt above it bumps (shouldn't pin <100%).
        assert effective_context_limit("x", 250_000, base=200_000) == 1_000_000


class TestEncodePathForClaude:
    """The token badge finds a pane's session JSONL by reproducing Claude's
    project-dir encoding. A mismatch = silent missing badge (the bug where
    projects with '_' in the path, e.g. line_websupport, never showed)."""

    def test_underscore_becomes_dash(self) -> None:
        # This is the regression: '_' MUST encode to '-' like Claude does.
        enc = encode_path_for_claude("C:/Users/alice/WebstormProjects/line_websupport/client")
        assert "line-websupport-client" in enc
        assert "_" not in enc

    def test_dot_becomes_dash(self) -> None:
        enc = encode_path_for_claude("C:/Users/alice/.claude-monitor/x")
        assert "." not in enc
        assert "-claude-monitor-x" in enc

    def test_separators_and_drive(self) -> None:
        if sys.platform == "win32":
            enc = encode_path_for_claude("C:/Users/alice/WebstormProjects/agent-takkub")
            assert enc == "C--Users-alice-WebstormProjects-agent-takkub"
        else:
            # POSIX has no drive letter; an absolute path's leading "/" encodes to "-".
            enc = encode_path_for_claude("/Users/alice/WebstormProjects/agent-takkub")
            assert enc == "-Users-alice-WebstormProjects-agent-takkub"

    def test_only_alnum_and_dash_remain(self) -> None:
        enc = encode_path_for_claude("C:/a_b.c/d e/f")
        assert set(enc) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _assistant(model: str, inp: int, cc: int, cr: int, out: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                    "output_tokens": out,
                },
            },
        }
    )


def _user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}})


class TestReadLastUsage:
    def test_returns_last_assistant_turn(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text(
            _assistant("claude-a", 10, 0, 0, 5)
            + "\n"
            + _assistant("claude-b", 100, 20, 30, 7)
            + "\n",
            encoding="utf-8",
        )
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-b"
        assert u["prompt"] == 150  # 100 + 20 + 30
        assert u["total"] == 157
        assert u["output"] == 7

    def test_tail_fast_path_large_file(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "big.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            # >512 KiB of filler so the tail window is exercised...
            filler = _user("x" * 500) + "\n"
            written = 0
            while written < _TAIL_SCAN_BYTES + 100_000:
                fh.write(filler)
                written += len(filler)
            # ...then the real last assistant turn at EOF.
            fh.write(_assistant("claude-final", 1000, 0, 0, 42) + "\n")
        assert f.stat().st_size > _TAIL_SCAN_BYTES
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-final"
        assert u["prompt"] == 1000
        assert u["output"] == 42

    def test_fallback_full_scan_when_no_assistant_in_tail(self, tmp_path: pathlib.Path) -> None:
        # Assistant turn at the very start, then >512 KiB of user lines after it
        # so the tail window contains NO assistant line — the full-scan fallback
        # must still find the early turn (correctness preserved).
        f = tmp_path / "front.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(_assistant("claude-early", 77, 0, 0, 3) + "\n")
            filler = _user("y" * 500) + "\n"
            written = 0
            while written < _TAIL_SCAN_BYTES + 100_000:
                fh.write(filler)
                written += len(filler)
        assert f.stat().st_size > _TAIL_SCAN_BYTES
        u = read_last_usage(f)
        assert u is not None
        assert u["model"] == "claude-early"
        assert u["prompt"] == 77

    def test_missing_file_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert read_last_usage(tmp_path / "nope.jsonl") is None

    def test_no_assistant_turns_returns_none(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "u.jsonl"
        f.write_text(_user("hi") + "\n" + _user("there") + "\n", encoding="utf-8")
        assert read_last_usage(f) is None


class TestFindLatestSessionConfigDir:
    """find_latest_session must honour a non-default CLAUDE_CONFIG_DIR.

    A pane on a non-default user profile writes its session JSONL under
    <config_dir>/projects/, not ~/.claude/projects/. Before the fix the meter
    only ever looked under ~/.claude, so those panes never showed a context %.
    """

    def _plant_session(self, config_home: pathlib.Path, cwd: pathlib.Path) -> pathlib.Path:
        enc = encode_path_for_claude(cwd)
        proj = config_home / "projects" / enc
        proj.mkdir(parents=True)
        sess = proj / "abc.jsonl"
        sess.write_text(
            json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}) + "\n",
            encoding="utf-8",
        )
        return sess

    def test_finds_session_under_custom_config_dir(self, tmp_path: pathlib.Path) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        custom_home = tmp_path / "profileB"
        planted = self._plant_session(custom_home, cwd)
        found = find_latest_session(cwd, config_dir=custom_home)
        assert found == planted

    def test_default_lookup_misses_custom_profile_session(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        # Reproduces the bug: session lives ONLY under the custom profile home.
        cwd = tmp_path / "proj"
        cwd.mkdir()
        custom_home = tmp_path / "profileB"
        self._plant_session(custom_home, cwd)
        # Point the default (~/.claude) lookup at an empty fake home.
        fake_default = tmp_path / "defaulthome"
        (fake_default / ".claude").mkdir(parents=True)
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_default))
        # config_dir=None (default profile) → not found; custom → found.
        assert find_latest_session(cwd, config_dir=None) is None
        assert find_latest_session(cwd, config_dir=custom_home) is not None

    def test_none_config_dir_falls_back_to_home_claude(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        fake_default = tmp_path / "defaulthome"
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_default))
        planted = self._plant_session(fake_default / ".claude", cwd)
        assert find_latest_session(cwd, config_dir=None) == planted


class TestFindSessionByUuid:
    """Issue #129: multiple panes sharing one cwd (routine for a single-repo
    project where every role points at the same project root) must never have
    the token meter/session-cap watchdog read a *sibling* pane's transcript.
    `find_session_by_uuid` resolves the exact ``<uuid>.jsonl`` for the pane's
    own recorded session, ignoring every other file in the cwd's project dir
    — never falls back to a newest-mtime guess."""

    def _plant(
        self,
        config_home: pathlib.Path,
        cwd: pathlib.Path,
        session_uuid: str,
        mtime: float | None = None,
    ) -> pathlib.Path:
        enc = encode_path_for_claude(cwd)
        proj = config_home / "projects" / enc
        proj.mkdir(parents=True, exist_ok=True)
        f = proj / f"{session_uuid}.jsonl"
        f.write_text(
            json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}) + "\n",
            encoding="utf-8",
        )
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f

    def test_two_panes_same_cwd_each_read_own_file(self, tmp_path: pathlib.Path) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        config_home = tmp_path / "home"
        # QA's file is written *later* than Lead's — the regression this
        # guards: the old newest-mtime scan handed Lead's badge the QA file
        # because it was the most-recently-modified one in the shared cwd
        # dir (proven live in events.log: session_cap_crossed for role=qa
        # fired 2s after spawn, reading Lead's ~185k prompt size).
        lead_file = self._plant(config_home, cwd, "lead-uuid-1111", mtime=1_000_000)
        qa_file = self._plant(config_home, cwd, "qa-uuid-2222", mtime=2_000_000)

        assert find_session_by_uuid(cwd, "lead-uuid-1111", config_dir=config_home) == lead_file
        assert find_session_by_uuid(cwd, "qa-uuid-2222", config_dir=config_home) == qa_file

    def test_unknown_uuid_returns_none_never_a_guess(self, tmp_path: pathlib.Path) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        config_home = tmp_path / "home"
        self._plant(config_home, cwd, "some-other-pane-uuid")
        # This pane's own uuid isn't in the dir — must get nothing back,
        # not whatever else happens to live in the cwd's project dir.
        assert find_session_by_uuid(cwd, "not-planted-uuid", config_dir=config_home) is None

    def test_empty_uuid_returns_none(self, tmp_path: pathlib.Path) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        assert find_session_by_uuid(cwd, "", config_dir=tmp_path / "home") is None

    def test_honours_custom_config_dir(self, tmp_path: pathlib.Path) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        custom_home = tmp_path / "profileB"
        planted = self._plant(custom_home, cwd, "profile-uuid")
        assert find_session_by_uuid(cwd, "profile-uuid", config_dir=custom_home) == planted

    def test_default_config_dir_does_not_see_custom_profile_session(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        cwd = tmp_path / "proj"
        cwd.mkdir()
        custom_home = tmp_path / "profileB"
        self._plant(custom_home, cwd, "profile-uuid")
        fake_default = tmp_path / "defaulthome"
        (fake_default / ".claude").mkdir(parents=True)
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_default))
        assert find_session_by_uuid(cwd, "profile-uuid", config_dir=None) is None
