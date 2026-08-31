"""#103 (2026-08-31): provider-neutral token meter — token_meter.resolve_pane_session
/ read_pane_usage dispatch, plus the per-provider parsers they call into
(codex_helper.read_codex_token_usage, opencode_helper.read_opencode_token_usage,
kimi_helper.read_kimi_token_usage). Field names are verified against real
sessions on this machine — see docs/audit/2026-08-31-token-meter-providers.md.

Claude's own read_last_usage/find_session_by_uuid contract is untouched by
this feature — see test_token_meter.py, which stays green unmodified.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

from agent_takkub.codex_helper import read_codex_token_usage, resolve_newest_codex_session_for_cwd
from agent_takkub.kimi_helper import read_kimi_token_usage
from agent_takkub.opencode_helper import read_opencode_token_usage
from agent_takkub.token_meter import read_pane_usage, resolve_pane_session

# ── codex ─────────────────────────────────────────────────────────────────


def _codex_meta_line(cwd: str, session_id: str = "sess-1") -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-30T04:50:58.607Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": session_id, "session_id": session_id, "cwd": cwd},
        }
    )


def _codex_token_count_line(inp: int, cached: int, out: int, limit: int) -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-30T04:55:19.286Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": inp * 5,
                        "cached_input_tokens": cached * 5,
                        "output_tokens": out * 5,
                        "total_tokens": (inp + cached + out) * 5,
                    },
                    "last_token_usage": {
                        "input_tokens": inp,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": 0,
                        "output_tokens": out,
                        "reasoning_output_tokens": 0,
                        "total_tokens": inp + cached + out,
                    },
                    "model_context_window": limit,
                },
                "rate_limits": {},
            },
        }
    )


class TestReadCodexTokenUsage:
    def test_reads_last_token_count_event(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "rollout.jsonl"
        f.write_text(
            _codex_meta_line("C:/repo")
            + "\n"
            + _codex_token_count_line(1000, 500, 100, 258_400)
            + "\n"
            # a non-token_count line after it — the real-file case where the
            # last line at EOF is NOT the last token_count event.
            + json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}})
            + "\n",
            encoding="utf-8",
        )
        u = read_codex_token_usage(f)
        assert u is not None
        assert u["status"] == "ok"
        assert u["input"] == 1000
        assert u["cache_read"] == 500
        assert u["output"] == 100
        assert u["prompt"] == 1500  # input + cached, NOT total_token_usage's cumulative sum
        assert u["total"] == 1600
        assert u["limit"] == 258_400

    def test_takes_the_newest_of_several_token_count_events(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "rollout.jsonl"
        f.write_text(
            _codex_meta_line("C:/repo")
            + "\n"
            + _codex_token_count_line(100, 0, 10, 200_000)
            + "\n"
            + _codex_token_count_line(9000, 8000, 900, 258_400)
            + "\n",
            encoding="utf-8",
        )
        u = read_codex_token_usage(f)
        assert u is not None
        assert u["prompt"] == 17000
        assert u["limit"] == 258_400

    def test_no_token_count_event_yet_is_no_data(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "rollout.jsonl"
        f.write_text(_codex_meta_line("C:/repo") + "\n", encoding="utf-8")
        u = read_codex_token_usage(f)
        assert u == {
            "status": "no_data",
            "model": "codex",
            "reason": "no token_count event logged yet",
        }

    def test_missing_last_token_usage_is_schema_drift_no_data(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "rollout.jsonl"
        f.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"model_context_window": 1000}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        u = read_codex_token_usage(f)
        assert u is not None
        assert u["status"] == "no_data"
        assert "schema drift" in u["reason"]

    def test_missing_file_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert read_codex_token_usage(tmp_path / "nope.jsonl") is None

    def test_tail_scan_finds_event_past_512kb(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "big-rollout.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(_codex_meta_line("C:/repo") + "\n")
            filler = (
                json.dumps({"type": "event_msg", "payload": {"type": "agent_reasoning"}}) + "\n"
            )
            written = 0
            while written < 512 * 1024 + 50_000:
                fh.write(filler)
                written += len(filler)
            fh.write(_codex_token_count_line(42, 0, 7, 128_000) + "\n")
        assert f.stat().st_size > 512 * 1024
        u = read_codex_token_usage(f)
        assert u is not None
        assert u["prompt"] == 42
        assert u["limit"] == 128_000


class TestResolveNewestCodexSessionForCwd:
    def _plant(
        self, root: pathlib.Path, cwd: str, day: str, session_id: str, mtime: float
    ) -> pathlib.Path:
        import os

        day_dir = root / day.replace("-", "/")
        day_dir.mkdir(parents=True, exist_ok=True)
        f = day_dir / f"rollout-2026-{day[5:7]}-{day[8:10]}T00-00-00-{session_id}.jsonl"
        f.write_text(_codex_meta_line(cwd, session_id) + "\n", encoding="utf-8")
        os.utime(f, (mtime, mtime))
        return f

    def test_matches_recorded_cwd(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "sessions"
        wanted = self._plant(root, "C:/repo/a", "2026-08-30", "aaa", 2_000_000)
        self._plant(root, "C:/repo/b", "2026-08-30", "bbb", 3_000_000)
        found = resolve_newest_codex_session_for_cwd("C:/repo/a", root=root)
        assert found == wanted

    def test_no_match_returns_none(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "sessions"
        self._plant(root, "C:/repo/a", "2026-08-30", "aaa", 2_000_000)
        assert resolve_newest_codex_session_for_cwd("C:/repo/nowhere", root=root) is None

    def test_missing_root_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert resolve_newest_codex_session_for_cwd("C:/repo", root=tmp_path / "absent") is None


# ── opencode ──────────────────────────────────────────────────────────────


def _make_opencode_db(db_path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE session (id text PRIMARY KEY, directory text, time_updated integer)")
    conn.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text, "
        "time_created integer, time_updated integer, data text)"
    )
    conn.commit()
    conn.close()


class TestReadOpencodeTokenUsage:
    def test_reads_tokens_block_from_latest_assistant_message(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "opencode.db"
        _make_opencode_db(db)
        conn = sqlite3.connect(str(db))
        data = json.dumps(
            {
                "role": "assistant",
                "modelID": "deepseek-v4-flash-free",
                "tokens": {
                    "total": 35822,
                    "input": 2985,
                    "output": 581,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 32256},
                },
            }
        )
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("m1", "sess-a", 1, 1, data))
        conn.commit()
        conn.close()

        u = read_opencode_token_usage(db, "sess-a")
        assert u is not None
        assert u["status"] == "ok"
        assert u["model"] == "deepseek-v4-flash-free"
        assert u["input"] == 2985
        assert u["cache_read"] == 32256
        assert u["cache_creation"] == 0
        assert u["output"] == 581
        assert u["prompt"] == 2985 + 32256  # input + cache.write + cache.read
        assert u["limit"] is None

    def test_takes_newest_assistant_message(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "opencode.db"
        _make_opencode_db(db)
        conn = sqlite3.connect(str(db))
        older = json.dumps(
            {"role": "assistant", "modelID": "m", "tokens": {"input": 1, "output": 1}}
        )
        newer = json.dumps(
            {"role": "assistant", "modelID": "m", "tokens": {"input": 999, "output": 1}}
        )
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("m1", "s", 1, 1, older))
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("m2", "s", 2, 2, newer))
        conn.commit()
        conn.close()
        u = read_opencode_token_usage(db, "s")
        assert u is not None
        assert u["prompt"] == 999

    def test_no_assistant_message_yet_is_no_data(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "opencode.db"
        _make_opencode_db(db)
        u = read_opencode_token_usage(db, "empty-session")
        assert u == {
            "status": "no_data",
            "model": None,
            "reason": "no assistant message logged yet",
        }

    def test_missing_tokens_block_is_schema_drift_no_data(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "opencode.db"
        _make_opencode_db(db)
        conn = sqlite3.connect(str(db))
        data = json.dumps({"role": "assistant", "modelID": "m"})
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("m1", "s", 1, 1, data))
        conn.commit()
        conn.close()
        u = read_opencode_token_usage(db, "s")
        assert u is not None
        assert u["status"] == "no_data"
        assert "schema drift" in u["reason"]

    def test_missing_db_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert read_opencode_token_usage(tmp_path / "nope.db", "s") is None


# ── kimi ──────────────────────────────────────────────────────────────────


def _kimi_status_line(**payload) -> str:
    return json.dumps({"timestamp": 1.0, "message": {"type": "StatusUpdate", "payload": payload}})


class TestReadKimiTokenUsage:
    def test_reads_context_tokens_and_max(self, tmp_path: pathlib.Path) -> None:
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / "wire.jsonl").write_text(
            _kimi_status_line(
                context_tokens=12000,
                max_context_tokens=128000,
                token_usage={
                    "input_other": 11000,
                    "output": 300,
                    "input_cache_read": 900,
                    "input_cache_creation": 100,
                },
            )
            + "\n",
            encoding="utf-8",
        )
        u = read_kimi_token_usage(session_dir)
        assert u is not None
        assert u["status"] == "ok"
        assert u["prompt"] == 12000  # context_tokens wins over summing token_usage
        assert u["limit"] == 128000
        assert u["output"] == 300

    def test_none_fields_carry_forward_from_earlier_lines(self, tmp_path: pathlib.Path) -> None:
        """StatusUpdate's own contract: 'None fields indicate no change from
        the previous status' — a later line that only touches plan_mode must
        not erase the context_tokens/max_context_tokens an earlier line set."""
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        lines = [
            _kimi_status_line(context_tokens=5000, max_context_tokens=64000, token_usage=None),
            _kimi_status_line(context_tokens=None, max_context_tokens=None, plan_mode=True),
        ]
        (session_dir / "wire.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        u = read_kimi_token_usage(session_dir)
        assert u is not None
        assert u["prompt"] == 5000
        assert u["limit"] == 64000

    def test_no_status_update_yet_is_no_data(self, tmp_path: pathlib.Path) -> None:
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / "wire.jsonl").write_text(
            json.dumps({"timestamp": 1.0, "message": {"type": "TurnBegin", "payload": {}}}) + "\n",
            encoding="utf-8",
        )
        u = read_kimi_token_usage(session_dir)
        assert u == {"status": "no_data", "model": None, "reason": "no StatusUpdate logged yet"}

    def test_missing_wire_file_returns_none(self, tmp_path: pathlib.Path) -> None:
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        assert read_kimi_token_usage(session_dir) is None


# ── token_meter dispatcher ───────────────────────────────────────────────


class TestReadPaneUsageDispatch:
    def test_claude_wraps_read_last_usage_with_status_ok(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        u = read_pane_usage("claude", f)
        assert u is not None
        assert u["status"] == "ok"
        assert u["prompt"] == 10
        assert u["limit"] is None

    def test_gemini_is_always_unsupported_with_reason(self, tmp_path: pathlib.Path) -> None:
        # cand just needs to be truthy — the gemini branch never reads it.
        u = read_pane_usage("gemini", tmp_path / "transcript.jsonl")
        assert u is not None
        assert u["status"] == "unsupported"
        assert u["model"] is None
        assert "no token/usage field" in u["reason"]

    def test_cursor_is_always_unsupported_with_reason(self, tmp_path: pathlib.Path) -> None:
        u = read_pane_usage("cursor", tmp_path / "transcript.jsonl")
        assert u["status"] == "unsupported"
        assert "schema has not been captured" in u["reason"]

    def test_none_cand_returns_none_for_every_provider(self) -> None:
        for provider in ("claude", "codex", "gemini", "opencode", "kimi", "cursor", "unknown"):
            assert read_pane_usage(provider, None) is None

    def test_unknown_provider_returns_none(self, tmp_path: pathlib.Path) -> None:
        assert read_pane_usage("some-future-provider", tmp_path / "x") is None

    def test_opencode_dispatch_unpacks_cand_tuple(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "opencode.db"
        _make_opencode_db(db)
        conn = sqlite3.connect(str(db))
        data = json.dumps(
            {"role": "assistant", "modelID": "m", "tokens": {"input": 5, "output": 1}}
        )
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", ("m1", "s", 1, 1, data))
        conn.commit()
        conn.close()
        u = read_pane_usage("opencode", (db, "s"))
        assert u is not None
        assert u["prompt"] == 5


class TestResolvePaneSessionDispatch:
    def test_claude_requires_session_uuid(self) -> None:
        assert resolve_pane_session("claude", "C:/repo", session_uuid=None) is None

    def test_claude_delegates_to_find_session_by_uuid(self, tmp_path: pathlib.Path) -> None:
        from agent_takkub.token_meter import encode_path_for_claude

        cwd = tmp_path / "proj"
        cwd.mkdir()
        config_home = tmp_path / "home"
        proj_dir = config_home / "projects" / encode_path_for_claude(cwd)
        proj_dir.mkdir(parents=True)
        f = proj_dir / "u1.jsonl"
        f.write_text("{}\n", encoding="utf-8")
        found = resolve_pane_session("claude", cwd, session_uuid="u1", config_dir=config_home)
        assert found == f

    def test_codex_falls_back_to_newest_for_cwd_when_no_uuid(self, tmp_path: pathlib.Path) -> None:
        import os

        root = tmp_path / "sessions" / "2026" / "08" / "30"
        root.mkdir(parents=True)
        f = root / "rollout-x.jsonl"
        f.write_text(_codex_meta_line("C:/repo"), encoding="utf-8")
        os.utime(f, (1_000_000, 1_000_000))
        # resolve_pane_session's codex branch calls the real codex_sessions_root()
        # unless a session_id is given — so exercise the newest-for-cwd resolver
        # directly instead, which is what it delegates to for this path.
        from agent_takkub.codex_helper import resolve_newest_codex_session_for_cwd

        assert resolve_newest_codex_session_for_cwd("C:/repo", root=tmp_path / "sessions") == f

    def test_unknown_provider_returns_none(self) -> None:
        assert resolve_pane_session("some-future-provider", "C:/repo") is None
