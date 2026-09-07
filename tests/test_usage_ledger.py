"""Tests for `agent_takkub.usage_ledger` — the append-only token/quota
ledger (#507). `config.RUNTIME_DIR` is isolated per test by
`tests/conftest.py`'s autouse `_isolate_runtime` fixture; every path this
module touches is derived from `config.RUNTIME_DIR` at call time (never
bound at import time), so no extra isolation setup is needed here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from agent_takkub import usage_ledger as ul

# ── record_turn / record_quota_sample ───────────────────────────────────


def test_record_turn_dedupes_by_request_id():
    usage = {"input": 1, "cache_creation": 2, "cache_read": 3, "output": 4}
    assert ul.record_turn("claude", "default", "2026-09-01T00:00:00Z", "req_1", "m", usage)
    # Same request id again: no new row, even with different usage numbers.
    assert not ul.record_turn(
        "claude", "default", "2026-09-01T00:00:00Z", "req_1", "m", {**usage, "input": 999}
    )
    rows = ul._read_jsonl(ul._turn_file("claude", "default", "2026-09"))
    assert len(rows) == 1
    assert rows[0]["input"] == 1


def test_record_turn_rejects_missing_request_id_or_ts():
    assert not ul.record_turn("claude", "default", "", "req", "m", {})
    assert not ul.record_turn("claude", "default", "2026-09-01T00:00:00Z", "", "m", {})


def test_record_quota_sample_never_fabricates_a_none_utilization():
    assert not ul.record_quota_sample(
        "claude", "default", "2026-09-01T00:00:00Z", "five_hour", None, None
    )
    rows = ul._read_jsonl(ul._quota_file("claude", "default", "2026-09"))
    assert rows == []


def test_record_quota_sample_skips_exact_duplicate_same_tick():
    ts = "2026-09-01T00:00:00Z"
    assert ul.record_quota_sample("claude", "default", ts, "five_hour", 12.5, None)
    assert not ul.record_quota_sample("claude", "default", ts, "five_hour", 12.5, None)
    rows = ul._read_jsonl(ul._quota_file("claude", "default", "2026-09"))
    assert len(rows) == 1


def test_record_quota_sample_keeps_a_later_different_sample():
    ul.record_quota_sample("claude", "default", "2026-09-01T00:00:00Z", "five_hour", 10.0, None)
    ul.record_quota_sample("claude", "default", "2026-09-01T01:00:00Z", "five_hour", 15.0, None)
    rows = ul._read_jsonl(ul._quota_file("claude", "default", "2026-09"))
    assert [r["utilization"] for r in rows] == [10.0, 15.0]


# ── import_claude ────────────────────────────────────────────────────────


def _write_claude_session(base, project="proj", uuid="sess-1", lines=()):
    d = base / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _claude_assistant_line(ts, request_id, model="claude-sonnet-5", **usage_overrides):
    usage = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "output_tokens": 4,
    }
    usage.update(usage_overrides)
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "message": {"id": f"msg_{request_id}", "model": model, "usage": usage},
    }


def test_import_claude_counts_new_turns_and_is_idempotent(tmp_path):
    base = tmp_path / "claude-home"
    _write_claude_session(
        base,
        lines=[
            _claude_assistant_line("2026-09-01T10:00:00.000Z", "req_1"),
            # Real-world duplicate: same requestId logged twice (observed on
            # a live transcript) — must count once.
            _claude_assistant_line("2026-09-01T10:00:00.500Z", "req_1"),
            _claude_assistant_line("2026-09-01T10:01:00.000Z", "req_2", model="claude-fable-5"),
        ],
    )
    profiles = [{"name": "default", "config_dir": str(base)}]

    stats1 = ul.import_claude(profiles)
    assert stats1["new_turns"] == 2
    assert stats1["scanned_files"] == 1

    # Rerun with no changes: file is skipped entirely via the cursor.
    stats2 = ul.import_claude(profiles)
    assert stats2["new_turns"] == 0
    assert stats2["skipped_files"] == 1
    assert stats2["scanned_files"] == 0


def test_import_claude_skips_non_assistant_and_missing_usage(tmp_path):
    base = tmp_path / "claude-home"
    _write_claude_session(
        base,
        lines=[
            {"type": "user", "timestamp": "2026-09-01T10:00:00Z"},
            {
                "type": "assistant",
                "timestamp": "2026-09-01T10:00:00Z",
                "requestId": "r",
                "message": {},
            },
            _claude_assistant_line("2026-09-01T10:00:00Z", "req_ok"),
        ],
    )
    stats = ul.import_claude([{"name": "default", "config_dir": str(base)}])
    assert stats["new_turns"] == 1


def test_import_claude_never_double_counts_a_shared_session_profile(tmp_path):
    """A shared-session profile junctions its `projects/` dir into another
    profile's (`user_profile.provision_shared_profile`) — same physical
    files under two account names. Import must count it once, not twice."""
    base = tmp_path / "claude-home"
    _write_claude_session(base, lines=[_claude_assistant_line("2026-09-01T10:00:00Z", "req_1")])
    profiles = [
        {"name": "default", "config_dir": str(base)},
        {"name": "office", "config_dir": str(base)},  # same dir == shared session store
    ]
    stats = ul.import_claude(profiles)
    assert stats["new_turns"] == 1
    assert stats["scanned_files"] == 1


def test_import_claude_rescans_a_grown_file_without_double_counting(tmp_path):
    base = tmp_path / "claude-home"
    path = _write_claude_session(
        base, lines=[_claude_assistant_line("2026-09-01T10:00:00Z", "req_1")]
    )
    profiles = [{"name": "default", "config_dir": str(base)}]
    ul.import_claude(profiles)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_claude_assistant_line("2026-09-01T10:05:00Z", "req_2")) + "\n")
    stats = ul.import_claude(profiles)
    assert stats["new_turns"] == 1
    assert stats["scanned_files"] == 1

    rows = ul._read_jsonl(ul._turn_file("claude", "default", "2026-09"))
    assert {r["request_id"] for r in rows} == {"req_1", "req_2"}


# ── import_codex ─────────────────────────────────────────────────────────


def _write_codex_rollout(base, name="rollout-2026-09-01T10-00-00-abc.jsonl", lines=()):
    d = base / "sessions" / "2026" / "09" / "01"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def test_import_codex_attributes_model_from_turn_context(tmp_path):
    home = tmp_path / "codex-home"
    _write_codex_rollout(
        home,
        lines=[
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {},
            },
            {
                "timestamp": "2026-09-01T10:00:01Z",
                "ordinal": 1,
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra"},
            },
            {
                "timestamp": "2026-09-01T10:00:02Z",
                "ordinal": 2,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 5,
                            "cache_write_input_tokens": 2,
                            "output_tokens": 7,
                        }
                    },
                },
            },
        ],
    )
    stats = ul.import_codex([{"name": "default", "config_dir": str(home)}])
    assert stats["new_turns"] == 1
    rows = ul._read_jsonl(ul._turn_file("codex", "default", "2026-09"))
    assert rows[0]["model"] == "gpt-5.6-terra"
    assert rows[0]["input"] == 10
    assert rows[0]["cache_read"] == 5
    assert rows[0]["cache_creation"] == 2
    assert rows[0]["output"] == 7


def test_import_codex_falls_back_to_generic_model_without_turn_context(tmp_path):
    home = tmp_path / "codex-home"
    _write_codex_rollout(
        home,
        lines=[
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "ordinal": 0,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 1}},
                },
            }
        ],
    )
    stats = ul.import_codex([{"name": "default", "config_dir": str(home)}])
    assert stats["new_turns"] == 1
    rows = ul._read_jsonl(ul._turn_file("codex", "default", "2026-09"))
    assert rows[0]["model"] == "codex"


def test_import_codex_is_idempotent_on_rerun(tmp_path):
    home = tmp_path / "codex-home"
    _write_codex_rollout(
        home,
        lines=[
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "ordinal": 0,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 1}},
                },
            }
        ],
    )
    profiles = [{"name": "default", "config_dir": str(home)}]
    ul.import_codex(profiles)
    stats = ul.import_codex(profiles)
    assert stats["new_turns"] == 0
    assert stats["skipped_files"] == 1


# ── import_opencode ──────────────────────────────────────────────────────


def _make_opencode_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
    )
    ts_ms = int(datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (
            "msg_1",
            "sess_1",
            ts_ms,
            json.dumps(
                {
                    "role": "assistant",
                    "modelID": "claude-sonnet-5",
                    "tokens": {"input": 10, "output": 20, "cache": {"read": 30, "write": 5}},
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("msg_user", "sess_1", ts_ms, json.dumps({"role": "user"})),
    )
    conn.commit()
    conn.close()


def test_import_opencode_reads_real_tokens_and_model(tmp_path, monkeypatch):
    db_path = tmp_path / "opencode.db"
    _make_opencode_db(db_path)
    from agent_takkub import opencode_helper

    monkeypatch.setattr(opencode_helper, "opencode_db_path", lambda: db_path)

    stats = ul.import_opencode()
    assert stats["new_turns"] == 1
    rows = ul._read_jsonl(ul._turn_file("opencode", "default", "2026-09"))
    assert rows[0]["model"] == "claude-sonnet-5"
    assert rows[0]["input"] == 10
    assert rows[0]["output"] == 20
    assert rows[0]["cache_read"] == 30
    assert rows[0]["cache_creation"] == 5

    # Idempotent: db file unchanged -> skipped entirely.
    stats2 = ul.import_opencode()
    assert stats2["new_turns"] == 0
    assert stats2["skipped_files"] == 1


def test_import_opencode_no_db_found_returns_zero_stats(monkeypatch):
    from agent_takkub import opencode_helper

    monkeypatch.setattr(opencode_helper, "opencode_db_path", lambda: None)
    stats = ul.import_opencode()
    assert stats["new_turns"] == 0


# ── import_all / countability ─────────────────────────────────────────────


def test_import_all_reports_uncountable_provider_without_crashing():
    result = ul.import_all(provider="gemini")
    assert "error" in result["gemini"]


def test_countability_report_shape():
    report = ul.countability_report()
    assert report["turn"]["claude"][0] is True
    assert report["turn"]["gemini"][0] is False
    assert isinstance(report["turn"]["gemini"][1], str)
    assert report["quota"]["opencode"][0] is False


def test_import_all_with_source_reads_that_dir_without_profile_registry(tmp_path):
    base = tmp_path / "prod-claude-config"
    _write_claude_session(base, lines=[_claude_assistant_line("2026-09-01T10:00:00Z", "req_1")])
    stats = ul.import_all(provider="claude", source=str(base))
    assert stats["claude"]["new_turns"] == 1


def test_import_all_source_never_writes_into_the_source_dir(tmp_path):
    base = tmp_path / "prod-claude-config"
    _write_claude_session(base, lines=[_claude_assistant_line("2026-09-01T10:00:00Z", "req_1")])
    before = sorted(p.relative_to(base) for p in base.rglob("*") if p.is_file())
    ul.import_all(provider="claude", source=str(base))
    after = sorted(p.relative_to(base) for p in base.rglob("*") if p.is_file())
    assert before == after  # importer only read from `base`, wrote nothing there


def test_import_all_source_requires_a_known_importer():
    result = ul.import_all(provider="gemini", source="/somewhere")
    assert "error" in result["gemini"]


def test_usage_ledger_dir_env_override(tmp_path, monkeypatch):
    override = tmp_path / "scratch-ledger"
    monkeypatch.setenv("TAKKUB_USAGE_LEDGER_DIR", str(override))
    assert ul.usage_root() == override
    ul.record_turn(
        "claude",
        "default",
        "2026-09-01T00:00:00Z",
        "r1",
        "m",
        {"input": 1, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    assert (override / "claude" / "default" / "2026-09.jsonl").is_file()


# ── daily_series ─────────────────────────────────────────────────────────


def test_daily_series_returns_one_entry_per_day_zero_filled():
    series = ul.daily_series("claude", days=5)
    assert len(series) == 5
    assert all(total == 0 for _date_str, total in series)


def test_daily_series_sums_across_models_and_accounts():
    today = datetime.now(tz=UTC).date().isoformat()
    ul.record_turn(
        "claude",
        "default",
        f"{today}T00:00:00Z",
        "r1",
        "m1",
        {"input": 1, "cache_creation": 2, "cache_read": 3, "output": 4},
    )
    ul.record_turn(
        "claude",
        "office",
        f"{today}T01:00:00Z",
        "r2",
        "m2",
        {"input": 5, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    series = dict(ul.daily_series("claude", days=1))
    assert series[today] == 15


def test_daily_series_skips_uncountable_providers():
    ul.account_dir("gemini", "default").mkdir(parents=True, exist_ok=True)
    assert ul.daily_series("gemini", days=3) == [
        (d, 0) for d, _ in ul.daily_series("gemini", days=3)
    ]


# ── rollup_daily ─────────────────────────────────────────────────────────


def test_rollup_daily_aggregates_by_date_and_model():
    ul.record_turn(
        "claude",
        "default",
        "2026-09-01T10:00:00Z",
        "r1",
        "m1",
        {"input": 1, "cache_creation": 2, "cache_read": 3, "output": 4},
    )
    ul.record_turn(
        "claude",
        "default",
        "2026-09-01T11:00:00Z",
        "r2",
        "m1",
        {"input": 10, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    ul.record_turn(
        "claude",
        "default",
        "2026-09-02T10:00:00Z",
        "r3",
        "m2",
        {"input": 5, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    daily = ul.rollup_daily("claude", "default")
    assert daily["2026-09-01"]["m1"]["turns"] == 2
    assert daily["2026-09-01"]["m1"]["input"] == 11
    assert daily["2026-09-02"]["m2"]["turns"] == 1


def test_rollup_daily_prunes_old_raw_month_but_keeps_the_data(monkeypatch):
    old_month = ul._month_n_ago(2)
    ul.record_turn(
        "claude",
        "default",
        f"{old_month}-01T00:00:00Z",
        "r1",
        "m",
        {"input": 1, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    old_file = ul._turn_file("claude", "default", old_month)
    assert old_file.is_file()

    daily = ul.rollup_daily("claude", "default")
    assert not old_file.is_file()
    assert daily[f"{old_month}-01"]["m"]["input"] == 1


# ── query_usage / format_usage_table ───────────────────────────────────


def test_query_usage_aggregates_claude_and_flags_uncountable_gemini():
    ul.record_turn(
        "claude",
        "default",
        "2026-09-05T00:00:00Z",
        "r1",
        "claude-sonnet-5",
        {"input": 1, "cache_creation": 2, "cache_read": 3, "output": 4},
    )
    result = ul.query_usage(days=7, provider=None)
    claude_rows = [r for r in result["rows"] if r["provider"] == "claude"]
    assert claude_rows and claude_rows[0]["total"] == 10

    # gemini has no ledger dir at all yet — still must not appear as a
    # countable row nor crash the aggregation.
    assert all(r["provider"] != "gemini" for r in result["rows"])


def test_query_usage_provider_filter_and_uncountable_listing():
    ul.record_turn(
        "claude",
        "default",
        "2026-09-05T00:00:00Z",
        "r1",
        "m",
        {"input": 1, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    ul.account_dir("gemini", "default").mkdir(parents=True, exist_ok=True)
    result = ul.query_usage(days=7, provider=None)
    assert any(u["provider"] == "gemini" for u in result["uncountable"])

    only_claude = ul.query_usage(days=7, provider="claude")
    assert all(r["provider"] == "claude" for r in only_claude["rows"])


def test_query_usage_quota_delta_sums_only_positive_movement():
    ul.record_quota_sample("claude", "default", "2026-09-05T00:00:00Z", "five_hour", 10.0, None)
    ul.record_quota_sample("claude", "default", "2026-09-05T01:00:00Z", "five_hour", 40.0, None)
    # A reset (utilization drops) must contribute 0, not a negative delta.
    ul.record_quota_sample("claude", "default", "2026-09-05T02:00:00Z", "five_hour", 5.0, None)
    ul.record_quota_sample("claude", "default", "2026-09-05T03:00:00Z", "five_hour", 20.0, None)
    result = ul.query_usage(days=7, provider="claude")
    window = next(q for q in result["quota"] if q["window"] == "five_hour")
    assert window["delta_pct"] == 45.0  # (40-10) + (20-5), reset drop ignored


def test_query_usage_never_crashes_with_empty_ledger():
    result = ul.query_usage(days=7)
    assert result["rows"] == []
    assert ul.format_usage_table(result)  # renders without raising


def test_format_usage_table_renders_uncountable_rows():
    ul.account_dir("kimi", "default").mkdir(parents=True, exist_ok=True)
    result = ul.query_usage(days=7)
    text = ul.format_usage_table(result)
    assert "นับไม่ได้" in text
