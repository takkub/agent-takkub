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
    # H5 (2026-09-07): codex's `input_tokens` already INCLUDES
    # `cached_input_tokens` — stored net of the cached portion (10-5=5) so
    # input+cache_read+output (5+5+7=17... plus cache_creation 2 = 19)
    # reconciles with the provider's own total_tokens instead of double-
    # counting the 5 cached tokens via both "input" and "cache_read".
    assert rows[0]["input"] == 5
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


def test_import_codex_reported_total_matches_provider_total_not_double_counted(tmp_path):
    """H5 (2026-09-07) — real fixture numbers from a live rollout event:
    provider `total_tokens=19421` (`input_tokens=19275` already includes
    `cached_input_tokens=12160`: 19275+146=19421, not 19275+12160+146).
    Before the fix this reported 31,581."""
    home = tmp_path / "codex-home"
    _write_codex_rollout(
        home,
        lines=[
            {
                "timestamp": "2026-09-07T05:21:35.118Z",
                "ordinal": 2,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 19275,
                            "cached_input_tokens": 12160,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 146,
                            "total_tokens": 19421,
                        }
                    },
                },
            }
        ],
    )
    ul.import_codex([{"name": "default", "config_dir": str(home)}])
    result = ul.query_usage(month="2026-09", provider="codex")
    assert result["rows"][0]["total"] == 19421


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


def test_rollup_daily_does_not_erase_history_when_a_pruned_month_partially_reappears():
    """H4 (2026-09-07): two historical requests roll up to a total, their
    raw month file gets pruned — then a resumed session (or a rotated
    source, or a rebuilt cursor) re-records ONE of the two original
    request ids for that same already-pruned month. The next rollup must
    not silently replace the day's published total with just that partial
    file's contents (30 -> 10, the review's own repro numbers)."""
    old_month = ul._month_n_ago(2)
    ul.record_turn(
        "claude",
        "history",
        f"{old_month}-01T00:00:00Z",
        "one",
        "m",
        {"input": 10, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    ul.record_turn(
        "claude",
        "history",
        f"{old_month}-01T01:00:00Z",
        "two",
        "m",
        {"input": 20, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    before = ul.rollup_daily("claude", "history")[f"{old_month}-01"]["m"]["input"]
    assert before == 30
    assert not ul._turn_file("claude", "history", old_month).is_file()

    # A resumed session re-records "one" — durable dedup (`_seen_ids.json`,
    # folded in by the rollup above) must recognize it as already-counted
    # and skip re-appending it, so the raw file never comes back at all.
    appended = ul.record_turn(
        "claude",
        "history",
        f"{old_month}-01T00:00:00Z",
        "one",
        "m",
        {"input": 10, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    assert appended is False
    assert not ul._turn_file("claude", "history", old_month).is_file()

    after = ul.rollup_daily("claude", "history")[f"{old_month}-01"]["m"]["input"]
    assert after == 30


def test_rollup_daily_adds_genuinely_new_rows_to_an_already_pruned_month():
    """The merge-not-replace path must still ADD real new data (not just
    guard against exact-duplicate ids) — a third, previously-unseen
    request for the same already-pruned month grows the total instead of
    replacing it."""
    old_month = ul._month_n_ago(2)
    ul.record_turn(
        "claude",
        "grow",
        f"{old_month}-01T00:00:00Z",
        "one",
        "m",
        {"input": 10, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    ul.rollup_daily("claude", "grow")
    ul.record_turn(
        "claude",
        "grow",
        f"{old_month}-01T02:00:00Z",
        "three",
        "m",
        {"input": 5, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    daily = ul.rollup_daily("claude", "grow")
    assert daily[f"{old_month}-01"]["m"]["input"] == 15
    assert daily[f"{old_month}-01"]["m"]["turns"] == 2


def test_rollup_daily_writes_atomically_no_tmp_file_left_behind():
    ul.record_turn(
        "claude",
        "atomic",
        "2026-09-01T00:00:00Z",
        "r1",
        "m",
        {"input": 1, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    ul.rollup_daily("claude", "atomic")
    daily_path = ul._daily_file("claude", "atomic")
    assert daily_path.is_file()
    assert not daily_path.with_suffix(daily_path.suffix + ".tmp").exists()


# ── B-H1/B-M4 (2026-09-07 round-2 review): month-rollover double-count ────


def test_rollup_daily_does_not_double_count_a_month_at_the_rollover_boundary(monkeypatch):
    """probe_rollover.py's exact repro: a month that is NOT yet being pruned
    (full-replace branch) transitions to being pruned (merge branch) at the
    next rollup after the calendar rolls over — the old code re-added the
    WHOLE file's contents on top of the total the full-replace branch had
    already published, doubling every field."""
    rows = [
        {
            "ts": "2026-09-10T01:00:00Z",
            "request_id": "r1",
            "model": "m",
            "input": 100,
            "cache_creation": 0,
            "cache_read": 0,
            "output": 10,
        },
        {
            "ts": "2026-09-10T02:00:00Z",
            "request_id": "r2",
            "model": "m",
            "input": 200,
            "cache_creation": 0,
            "cache_read": 0,
            "output": 20,
        },
    ]
    acct_dir = ul.account_dir("claude", "rollover")
    acct_dir.mkdir(parents=True, exist_ok=True)
    with (acct_dir / "2026-09.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    def freeze(month: str) -> None:
        monkeypatch.setattr(ul, "_month_n_ago", lambda n, _m=month: _m)

    freeze("2026-09")
    first = ul.rollup_daily("claude", "rollover")["2026-09-10"]["m"]
    assert first == {"turns": 2, "input": 300, "cache_creation": 0, "cache_read": 0, "output": 30}

    second = ul.rollup_daily("claude", "rollover")["2026-09-10"]["m"]
    assert second == first  # idempotent while still in the non-prune branch

    freeze("2026-10")  # the calendar rolls over — this month now gets pruned
    after_rollover = ul.rollup_daily("claude", "rollover")["2026-09-10"]["m"]
    assert after_rollover == first  # must NOT double


def test_rollup_daily_does_not_double_count_when_unlink_keeps_failing(monkeypatch):
    """B-M4: if `unlink()` on the pruned raw file fails every call (a
    locked file on Windows is the real-world trigger), the merge branch
    used to re-add the SAME file's full contents on every subsequent
    rollup, without bound. The seen-id delta merge must stay idempotent
    even when the raw file is never actually removed."""
    ul.record_turn(
        "claude",
        "stuck",
        "2026-08-01T00:00:00Z",
        "r1",
        "m",
        {"input": 10, "cache_creation": 0, "cache_read": 0, "output": 0},
    )
    monkeypatch.setattr(ul, "_month_n_ago", lambda n: "2026-09")  # always in the prune branch
    monkeypatch.setattr(
        ul.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked"))
    )

    first = ul.rollup_daily("claude", "stuck")["2026-08-01"]["m"]["input"]
    assert first == 10
    second = ul.rollup_daily("claude", "stuck")["2026-08-01"]["m"]["input"]
    assert second == 10  # not 20 — the file is still on disk but already fully seen
    third = ul.rollup_daily("claude", "stuck")["2026-08-01"]["m"]["input"]
    assert third == 10


# ── B-L5 (2026-09-07 round-2 review): input - cached must not go negative ─


def test_import_codex_clamps_input_minus_cached_at_zero(tmp_path):
    """A provider report where `cached_input_tokens` exceeds
    `input_tokens` (never observed live, but not provably impossible) must
    not push a negative number into the ledger."""
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
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 20,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 1,
                        }
                    },
                },
            },
        ],
    )
    stats = ul.import_codex([{"name": "default", "config_dir": str(home)}])
    assert stats["new_turns"] == 1
    rows = ul._read_jsonl(ul._turn_file("codex", "default", "2026-09"))
    assert rows[0]["input"] == 0


# ── H1: provider allowlist / path containment ───────────────────────────


def test_all_accounts_rejects_a_traversal_provider():
    victim_dir = ul.usage_root().parent / "outside" / "account"
    victim_dir.mkdir(parents=True)
    (victim_dir / "2000-01.jsonl").write_text('{"request_id":"r"}\n')

    assert ul._all_accounts("../outside") == []
    result = ul.query_usage(provider="../outside")
    assert result["rows"] == []
    # Nothing under the ledger, and nothing outside it, was touched.
    assert not (victim_dir / "daily.json").exists()


def test_all_accounts_rejects_an_unknown_provider_name():
    assert ul._all_accounts("not-a-real-provider") == []


# ── H7: concurrent writers ───────────────────────────────────────────────


def test_record_turn_concurrent_writers_never_duplicate_a_request_id():
    """Two threads calling `record_turn` for the SAME (provider, account,
    month, request_id) concurrently, repeated to make a missing lock
    likely to surface — before H7's write-time lock, both could read
    "not yet recorded" before either appended, duplicating the row."""
    import threading

    for i in range(20):
        account = f"concurrent{i}"

        def record(account=account):
            ul.record_turn("claude", account, "2026-09-07T00:00:00Z", "same", "m", {"input": 5})

        threads = [threading.Thread(target=record) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = ul._read_jsonl(ul._turn_file("claude", account, "2026-09"))
        assert len(rows) == 1, f"iteration {i}: expected exactly 1 row, got {len(rows)}"


def test_usage_lock_blocks_a_second_acquire_until_the_first_releases():
    """Direct proof `_UsageLock` actually serializes — a second `with
    _UsageLock()` started while the first is still held must not proceed
    until the first exits."""
    import threading
    import time

    order: list[str] = []
    first_holding = threading.Event()
    release_first = threading.Event()

    def first():
        with ul._UsageLock():
            first_holding.set()
            release_first.wait(timeout=5)
            order.append("first-release")

    def second():
        first_holding.wait(timeout=5)
        time.sleep(0.05)  # give `first` a head start actually holding the lock
        with ul._UsageLock():
            order.append("second-acquire")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t1.join(timeout=0.01)  # not yet — first should still be holding
    t2.start()
    time.sleep(0.2)
    assert order == [], "second acquired the lock while first still held it"
    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first-release", "second-acquire"]


def test_usage_lock_survives_permission_error_from_os_open(monkeypatch):
    """#533: on Windows, O_CREAT|O_EXCL against a lock file another process
    (or a transient AV scan) still has open can raise `PermissionError`
    instead of `FileExistsError`. Before the fix that escaped `__enter__`
    as an unhandled crash; it must be treated the same as `FileExistsError`
    (retry until the transient holder releases it)."""
    real_open = ul.os.open
    calls = {"n": 0}

    def flaky_open(path, flags, mode=0o777):
        if path == str(ul._usage_lock_path()) and flags & ul.os.O_EXCL:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(ul.os, "open", flaky_open)

    with ul._UsageLock() as lock:
        assert lock._acquired
    assert calls["n"] >= 1


def test_usage_lock_reclaims_stale_lock_after_permission_error(monkeypatch):
    """A `PermissionError` against a genuinely stale lock file (crashed
    holder, not just a transient read lock) must still be reclaimed rather
    than retried forever — same staleness check as the `FileExistsError`
    path."""
    lock_path = ul._usage_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")
    old = ul._time.time() - ul._STALE_LOCK_S - 1
    ul.os.utime(lock_path, (old, old))

    real_open = ul.os.open
    calls = {"n": 0}

    def flaky_open(path, flags, mode=0o777):
        if path == str(lock_path) and flags & ul.os.O_EXCL:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(ul.os, "open", flaky_open)

    with ul._UsageLock() as lock:
        assert lock._acquired
    assert calls["n"] >= 1


# ── M1: opencode WAL ──────────────────────────────────────────────────────


def test_import_opencode_detects_a_wal_only_write(tmp_path, monkeypatch):
    """A write committed to the `-wal` sidecar (autocheckpoint disabled, a
    real busy-connection shape) leaves the MAIN db file's own stat
    unchanged — the old size/mtime-only cursor key skipped the whole
    import and missed it entirely."""
    import sqlite3

    from agent_takkub import opencode_helper

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE message(id TEXT, time_created INTEGER, data TEXT)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(truncate)")

    def add(row_id):
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?)",
            (
                row_id,
                1788739200000,
                json.dumps({"role": "assistant", "modelID": "m", "tokens": {"input": 10}}),
            ),
        )
        conn.commit()

    add("1")
    monkeypatch.setattr(opencode_helper, "opencode_db_path", lambda: db_path)
    first = ul.import_opencode()
    assert first["new_turns"] == 1

    add("2")  # WAL-only write; main file's stat is untouched by this alone.
    second = ul.import_opencode()
    assert second["skipped_files"] == 0, "WAL-only write must not be skipped as unchanged"
    assert second["new_turns"] == 1
    conn.close()


# ── M6: malformed rows never abort the whole import ─────────────────────


def test_import_claude_skips_a_top_level_scalar_json_line(tmp_path):
    base = tmp_path / "claude-config"
    (base / "projects" / "p").mkdir(parents=True)
    (base / "projects" / "p" / "s.jsonl").write_text(
        "null\n" + json.dumps(_claude_assistant_line("2026-09-01T10:00:00Z", "r1")) + "\n"
    )
    stats = ul.import_claude([{"name": "default", "config_dir": str(base)}])
    assert stats["new_turns"] == 1
    assert stats.get("errors", 0) == 0


def test_record_turn_skips_non_numeric_usage_field_instead_of_raising():
    ok = ul.record_turn(
        "claude", "default", "2026-09-01T00:00:00Z", "r1", "m", {"input": "not-a-number"}
    )
    assert ok is False
    rows = ul._read_jsonl(ul._turn_file("claude", "default", "2026-09"))
    assert rows == []


def test_import_all_isolates_one_providers_crash_from_the_others(monkeypatch):
    def _boom():
        raise RuntimeError("blew up")

    monkeypatch.setitem(ul._IMPORTERS, "claude", _boom)
    result = ul.import_all()
    assert "error" in result["claude"]
    assert "error" not in result["codex"]
    assert "error" not in result["opencode"]


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


def test_query_usage_quota_scans_every_month_in_a_multi_month_range():
    """M9 (2026-09-07): the old `{start_month, end_month}` two-element set
    silently skipped every month strictly between them — `--days 90` can
    span 3-4 months and a middle month's quota samples never got read at
    all (no error, just a quietly smaller %)."""
    from datetime import UTC, datetime, timedelta

    middle = datetime.now(tz=UTC) - timedelta(days=45)
    ul.record_quota_sample("claude", "default", middle.isoformat(), "five_hour", 33.0, None)
    result = ul.query_usage(days=90, provider="claude")
    window = next((q for q in result["quota"] if q.get("window") == "five_hour"), None)
    assert window is not None, "a quota sample in a middle month must not be skipped"
    assert window["samples"] == 1


def test_query_usage_never_crashes_with_empty_ledger():
    result = ul.query_usage(days=7)
    assert result["rows"] == []
    assert ul.format_usage_table(result)  # renders without raising


def test_format_usage_table_renders_uncountable_rows():
    ul.account_dir("kimi", "default").mkdir(parents=True, exist_ok=True)
    result = ul.query_usage(days=7)
    text = ul.format_usage_table(result)
    assert "นับไม่ได้" in text
